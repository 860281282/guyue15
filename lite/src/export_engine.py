"""Export Obsidian notes to Notion, conservatively.

Direction is the reverse of sync_engine: the local vault is the source and Notion
is the copy. Nothing here deletes cloud pages, and by default a page that changed
in Notion after our previous push is never overwritten.
"""
import hashlib
import json
import re
from datetime import datetime, timezone, timedelta
from html import unescape
from pathlib import Path, PurePosixPath

from notion_connection import SyncError, atomic_bytes, image_fingerprint
from sync_engine import baseline, children_of, identity, list_all

LOCAL_TZ = timezone(timedelta(hours=8))

POLICIES = {
    'skip': '云端优先：Notion 上改过的页跳过，不覆盖',
    'overwrite': '本地优先：用 Obsidian 覆盖 Notion（覆盖前备份云端正文）',
    'copy': '两边都留：冲突时在 Notion 新建一份冲突副本',
}
DEFAULT_POLICY = 'skip'

SKIP_DIRS = {'.obsidian', '.同步工具', '.trash', '.git'}

# 导入方向写进 frontmatter 的记账字段，导出时不值得提醒
BOOKKEEPING = {'notion_id', 'notion_url', 'imported_at', 'notion_last_edited_at'}


def digest(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def remote_guard(page):
    """Return a conservative fingerprint; incomplete reads cannot authorize a write."""
    metadata = page.get('metadata') or {}
    if not isinstance(page.get('text'), str) or any(
        obj.get('truncated') or obj.get('unknown_block_count') or obj.get('unknown_block_ids')
        for obj in (page, metadata)
    ):
        raise SyncError('云端内容读取不完整，已停止覆盖')
    if re.search(r'<(?:unknown|inaccessible-block)\b', page['text'], re.I):
        raise SyncError('云端包含未知或不可访问的内容，已停止覆盖')
    content = re.search(r'<content>\s*([\s\S]*?)\s*</content>', page['text'], re.I)
    if content and re.search(r'<(?:page|database|folder)\b', content.group(1), re.I):
        raise SyncError('云端页面含子页面或数据库，已停止整页覆盖，请手动处理')
    return (page.get('page_last_edited_at') or '', digest(page['text']))


def check_write_result(result):
    # A warning may follow a partial remote write; never retry blindly.
    if (not isinstance(result, dict) or result.get('warnings') or result.get('truncated')
            or result.get('unknown_block_ids')):
        raise SyncError('Notion 写入结果有警告或不完整，请先核对云端内容', code='ambiguous_write')


def check_remote_guard(action, remote):
    if not action.get('_remote_guard') or action['_remote_guard'] != remote_guard(remote):
        raise SyncError('云端页面在预览后发生变化，已停止覆盖；请重新同步并检查冲突')


def page_body(page):
    text = page.get('text') if isinstance(page, dict) else None
    if not isinstance(text, str) or text.count('<content>') != 1 or text.count('</content>') != 1:
        raise SyncError('云端正文无法安全识别，已停止上传')
    return normalize_body(text.split('<content>', 1)[1].split('</content>', 1)[0])


def normalize_body(body):
    return body.replace('\r\n', '\n').strip('\n')


def targeted_update(remote, action, image_hook=None):
    """Generate a unique, bounded replacement for plain Markdown; fail closed otherwise."""
    before = page_body(remote)
    after = normalize_body(render(action, image_hook))
    if before == after:
        return None
    if not before or re.search(r'<[^>]*>|%%|!\[\[|\[\[', before + after):
        raise SyncError('内容包含暂不支持的结构，已停止自动覆盖，请人工核对')
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    end_before, end_after = len(before), len(after)
    while end_before > start and end_after > start and before[end_before-1] == after[end_after-1]:
        end_before -= 1
        end_after -= 1
    left, right = start, end_before
    def unique(needle):
        first = before.find(needle) if needle else -1
        return first >= 0 and before.find(needle, first + 1) < 0
    while not unique(before[left:right]):
        if right - left >= 512:
            raise SyncError('无法找到简短且唯一的更新位置，已停止上传')
        if left > 0:
            left -= 1
        elif right < len(before):
            right += 1
        else:
            raise SyncError('无法找到唯一更新位置，已停止上传')
    if left == 0 and right == len(before):
        raise SyncError('改动涉及整个云端正文，已停止自动覆盖，请人工核对')
    old = before[left:right]
    new = before[left:start] + after[start:end_after] + before[end_before:right]
    if before.replace(old, new, 1) != after:
        raise SyncError('更新计划无法校验，已停止上传')
    return {'old_str': old, 'new_str': new}


def read_written_page(client, page_id):
    page = client.call('fetch', id=page_id, include_file_urls=False, include_transcript=False)
    remote_guard(page)
    version = page.get('page_last_edited_at')
    if not parse_iso(version):
        raise ValueError('missing remote version')
    url = page.get('url')
    if url and identity(url) != str(page_id).replace('-', '').lower():
        raise ValueError('unexpected page identity')
    return version, page_body(page)


HOSTED_IMAGE = re.compile(r'^<image source="(notion-file-block://[^"\n]+)">\n([^\n]*)\n</image>$', re.M)


def note_images(rel, upload):
    fingerprints = {}

    def upload_for_note(src):
        source, fingerprint = upload(rel, src)
        fingerprints[source] = fingerprint
        return source

    return (upload_for_note if upload else None), fingerprints


def prepared_body(body, attachments):
    body = normalize_body(body)
    if not attachments:
        return body, 0
    count = 0
    seen = set()

    def replace(match):
        nonlocal count
        checksum = attachments.get(match[2])
        if checksum is None:
            return match[0]
        seen.add(match[2])
        count += 1
        return '![%s](sha256:%s)' % (match[1], checksum)

    body = IMAGE.sub(replace, body)
    if seen != attachments.keys():
        raise SyncError('图片引用无法完整核对')
    return body, count


def verified_remote_body(client, body, image_count):
    if not image_count:
        return body
    if sum(1 for _ in HOSTED_IMAGE.finditer(body)) != image_count:
        raise SyncError('云端图片数量或格式与上传请求不符')
    return HOSTED_IMAGE.sub(lambda match: '![%s](sha256:%s)' % (
        unescape(match[2]), image_fingerprint(client, unescape(match[1]))), body)


def confirm_written_version(client, page_id, expected_body, image_count=0):
    """A successful response is not a durable baseline until reread succeeds."""
    try:
        version, body = read_written_page(client, page_id)
        if verified_remote_body(client, body, image_count) != normalize_body(expected_body):
            raise ValueError('remote body differs from the requested content')
        return version
    except Exception as exc:
        raise SyncError('云端写入后无法确认页面版本或正文，请先核对 Notion', code='ambiguous_write') from exc


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    atomic_bytes(Path(path), json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


def now_iso():
    return datetime.now(LOCAL_TZ).replace(microsecond=0).isoformat()


def parse_iso(text):
    if not text:
        return None
    raw = str(text).strip().replace('Z', '+00:00')
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def normalize(text):
    return re.sub(r'\r\n?', '\n', text).strip() + '\n'


# ---------------------------------------------------------------- conversion

FRONT = re.compile(r'\A---\n(.*?)\n---[ \t]*\n', re.S)
OBS_COMMENT = re.compile(r'%%.*?%%', re.S)
WIKI = re.compile(r'(!)?\[\[([^\]\|#]+)(?:#([^\]\|]+))?(?:\|([^\]]+))?\]\]')
CALLOUT = re.compile(r'^([ \t]*)>\s*\[!([A-Za-z]+)\][+-]?\s*(.*)$', re.M)
CALLOUT_LINE = re.compile(r'^([ \t]*)>\s?(.*)$', re.M)
TAG = re.compile(r'(?<![\w/#])#([\w\u4e00-\u9fff][\w\u4e00-\u9fff/\-]{0,39})')
FENCE = re.compile(r'^```([^\n`]*)\n(.*?)^```', re.S | re.M)
HEADING1 = re.compile(r'^#\s+(.+?)\s*$', re.M)
IMAGE = re.compile(r'!\[([^\]\n]*)\]\(([^)\s]*)\)')
REMOTE_SRC = re.compile(r'^(?:https?:|data:|file-upload:|notion-file-block:|blob:|attachment:)', re.I)


def _image_text(match, warnings, image_hook=None):
    alt, src = match.group(1), match.group(2).strip()
    if REMOTE_SRC.match(src):
        return match.group(0)
    if image_hook is not None:
        source = image_hook(src)
        if source:
            return '![%s](%s)' % ((alt or PurePosixPath(src).name).strip(), source)
    warnings.append('本地图片未同步：' + (src or alt))
    return '（本地图片未同步：%s）' % (src or alt)


def _split_images(line):
    parts, last = [], 0
    for match in IMAGE.finditer(line):
        before = line[last:match.start()].strip()
        if before:
            parts.append(before)
        parts.append(match.group(0).strip())
        last = match.end()
    tail = line[last:].strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part]


def image_blocks(body, warnings, image_hook=None):
    """Notion 只接受独占一行的图片，行内图片会让整页解析失败。"""
    result, in_fence = [], False
    for line in body.split('\n'):
        stripped = line.strip()
        if stripped.startswith('```'):
            in_fence = not in_fence
            result.append(line)
            continue
        if in_fence or '![' not in line or stripped.startswith('|'):
            result.append(line)
            continue
        line = IMAGE.sub(lambda match: _image_text(match, warnings, image_hook), line)
        chunks = _split_images(line)
        if len(chunks) <= 1:
            result.append(line)
            continue
        result.append('')
        for chunk in chunks:
            result.append(chunk)
            result.append('')
    return '\n'.join(result)


def note_title(path, text):
    body = FRONT.sub('', text.lstrip('\ufeff'), count=1)
    hit = HEADING1.search(body)
    if hit:
        return hit.group(1).strip()
    return Path(path).stem


def to_notion(text, page_urls=None, image_hook=None):
    """Return (notion_markdown, warnings). Lossy constructs are reported, not hidden."""
    page_urls = page_urls or {}
    warnings = []
    out = text.lstrip('\ufeff')

    front = FRONT.match(out)
    if front:
        keys = [line.split(':', 1)[0].strip().strip('"\'').strip()
                for line in front.group(1).splitlines() if ':' in line]
        keys = [key for key in keys if key not in BOOKKEEPING]
        if keys:
            warnings.append('已忽略属性：' + '、'.join(keys[:6]))
        out = out[front.end():]

    if OBS_COMMENT.search(out):
        warnings.append('已移除 %%注释%%')
        out = OBS_COMMENT.sub('', out)

    # 一级标题已经是页面标题，正文里不再重复一次
    head = out.lstrip('\n')
    heading = HEADING1.match(head)
    if heading:
        out = head[heading.end():].lstrip('\n')

    def wiki(match):
        bang, target, anchor, alias = match.groups()
        target = target.strip()
        label = (alias or target).strip()
        if bang:
            if image_hook is not None:
                source = image_hook(target)
                if source:
                    return '![%s](%s)' % (label, source)
            warnings.append('嵌入未同步：' + target)
            return '（嵌入未同步：%s）' % target
        url = page_urls.get(target) or page_urls.get(target + '.md')
        if url:
            return '[%s](%s)' % (label, url)
        if anchor:
            return '%s（原链接锚点：#%s）' % (label, anchor)
        warnings.append('双链降级为文字：' + target)
        return label

    if WIKI.search(out):
        out = WIKI.sub(wiki, out)

    def callout(match):
        indent, kind, title = match.groups()
        name = title.strip() or kind.capitalize()
        warnings.append('callout 转为引用块：' + name)
        return '%s> **%s**' % (indent, name)

    if CALLOUT.search(out):
        out = CALLOUT.sub(callout, out)

    if TAG.search(out):
        warnings.append('行内 #标签 按文字保留（Notion 无行内标签）')

    def fence(match):
        language = (match.group(1) or '').strip().lower()
        if language in ('mermaid', 'dataview', 'dataviewjs', 'query'):
            warnings.append('%s 代码块按普通代码保留' % language)
        return match.group(0)

    out = FENCE.sub(fence, out)
    out = image_blocks(out, warnings, image_hook)
    out = re.sub(r'\n{3,}', '\n\n', out)
    body = out.strip()
    seen = []
    for item in warnings:
        if item not in seen:
            seen.append(item)
    return body, seen


# ---------------------------------------------------------------- scanning

def vault_ready(vault):
    return (Path(vault) / '.obsidian').is_dir()


def scan(vault, root='', page_urls=None, direct_only=False):
    """List local notes under root (relative to the vault), skipping tool folders."""
    vault = Path(vault).resolve()
    if not vault_ready(vault):
        raise SyncError('请选择已有的 Obsidian 知识库文件夹')
    base = (vault / root).resolve() if root else vault
    if not base.is_relative_to(vault) or not base.is_dir():
        raise SyncError('找不到要上传的文件夹：' + (root or '知识库'))
    notes = []
    from vault_tools import is_obsidian_program, note_files
    if is_obsidian_program(base):
        raise SyncError('不能上传 Obsidian 程序目录，请选择笔记文件夹')
    paths = ((p.name, p) for p in base.iterdir() if p.is_file()) if direct_only else note_files(base)
    for _base_rel, path in paths:
        if path.name.startswith('.'):
            continue
        if not path.resolve().is_relative_to(vault):
            raise SyncError('笔记路径超出知识库，已停止上传')
        if path.suffix.lower() != '.md':
            continue
        if not path.is_file():
            continue
        rel = path.relative_to(vault).as_posix()
        if any(part in SKIP_DIRS for part in PurePosixPath(rel).parts):
            continue
        try:
            text = path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeDecodeError):
            raise SyncError('无法读取笔记：' + rel)
        body, warnings = to_notion(text, page_urls=page_urls)
        notes.append({
            'rel': rel,
            'title': note_title(path, text),
            'text': text,
            'notion': body,
            'hash': digest(normalize(FRONT.sub('', text.lstrip('\ufeff'), count=1))),
            'warnings': warnings,
        })
    return notes


# ---------------------------------------------------------------- planning

TOLERANCE = timedelta(seconds=5)
TRASHED = re.compile(r'<page\b[^>]*\bdeleted\b', re.I)


def page_trashed(remote):
    """A page the user removed in Notion still answers fetch, marked as deleted."""
    text = remote.get('text', '') if isinstance(remote, dict) else ''
    return bool(TRASHED.search(text))


def state_path(vault):
    return Path(vault) / '.同步工具' / '数据' / 'export-state.json'


def empty_state():
    return {'files': {}, 'folders': {}, 'policy': DEFAULT_POLICY, 'last_run': None}


def load_state(vault):
    path = state_path(vault)
    try:
        raw = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return empty_state()
    except OSError as exc:
        raise SyncError('无法读取上传状态，已停止同步；请检查磁盘与备份') from exc
    try:
        state = json.loads(raw)
    except ValueError as exc:
        raise SyncError('上传状态已损坏，已停止同步；请从备份恢复后再试') from exc
    if not isinstance(state, dict) or any(
        key in state and not isinstance(state[key], dict) for key in ('files', 'folders')
    ):
        raise SyncError('上传状态格式异常，已停止同步；请从备份恢复后再试')
    state.setdefault('files', {})
    state.setdefault('folders', {})
    state.setdefault('policy', DEFAULT_POLICY)
    return state


def bind_workspace(vault, state, account):
    workspace = account.get('workspace') or {}
    connected_id = workspace.get('id')
    if not isinstance(connected_id, str) or not connected_id.strip():
        raise SyncError('无法确认当前 Notion 工作区，已停止上传')
    try:
        import_id = baseline(vault).get('workspace_id')
    except (OSError, ValueError, AttributeError, TypeError, KeyError) as exc:
        raise SyncError('无法读取知识库的 Notion 工作区绑定，已停止上传') from exc
    export_id = state.get('workspace_id')
    for bound_id in (import_id, export_id):
        if bound_id is not None and bound_id != connected_id:
            raise SyncError('当前 Notion 工作区与该知识库的绑定不一致，已停止上传')
    if not (import_id or export_id) and any(state.get(key) for key in ('files', 'folders', 'root_page', 'last_run')):
        raise SyncError('旧上传记录缺少工作区绑定。请先确认连接的是原工作区，再从该工作区同步到本地以建立绑定')
    state['workspace_id'] = connected_id
    return workspace


def folder_chain(rel):
    parts = PurePosixPath(rel).parts[:-1]
    chain = []
    for index in range(len(parts)):
        chain.append('/'.join(parts[:index + 1]))
    return chain


def link_urls(state):
    """Notion page URLs for notes already pushed, so wiki links can stay clickable."""
    urls = {}
    for rel, entry in (state.get('files') or {}).items():
        page = (entry or {}).get('page')
        if not page:
            continue
        url = 'https://app.notion.com/p/' + str(page).replace('-', '')
        stem = rel[:-3] if rel.lower().endswith('.md') else rel
        urls[stem] = url
        urls.setdefault(PurePosixPath(stem).name, url)
    return urls


def plan(client, vault, root, state, policy=None, log=None, cancel=None):
    """Decide what to do for every local note without touching Notion."""
    policy = policy or state.get('policy') or DEFAULT_POLICY
    if policy not in POLICIES:
        raise SyncError('未知的冲突处理方式：' + str(policy))
    say = log or (lambda text: None)
    urls = link_urls(state)
    notes = scan(vault, root, page_urls=urls)
    files = state.get('files', {})
    actions = []
    for note in notes:
        if cancel is not None and cancel.is_set():
            raise SyncError('已取消，未改动 Notion')
        rel = note['rel']
        entry = files.get(rel) or {}
        page = entry.get('page')
        action = {'rel': rel, 'title': note['title'], 'hash': note['hash'],
                  'notion': note['notion'], 'text': note.get('text'),
                  'warnings': note['warnings'], 'urls': urls,
                  'page': page, 'policy': policy}
        if page and entry.get('local') == note['hash']:
            action['kind'] = 'unchanged'
            actions.append(action)
            continue
        if page:
            say('正在检查：' + rel)
            remote = client.call('fetch', id=page, include_file_urls=False, include_transcript=False)
            action['_remote_guard'] = remote_guard(remote)
            if page_trashed(remote):
                action['kind'] = 'conflict'
                action['warnings'] = list(action.get('warnings') or []) + [
                    '云端页面已在回收站，已停止重建，请人工核对']
                actions.append(action)
                continue
            edited = parse_iso(remote.get('page_last_edited_at'))
            pushed = parse_iso(entry.get('remote_edited') or entry.get('pushed'))
            if not edited or not pushed:
                action['kind'] = 'conflict'
                action['warnings'] = list(action.get('warnings') or []) + ['无法确认云端版本，已停止覆盖']
                actions.append(action)
                continue
            changed_remotely = edited > pushed
            if changed_remotely:
                if policy == 'skip':
                    action['kind'] = 'conflict'
                    action['remote_edited'] = remote.get('page_last_edited_at')
                    actions.append(action)
                    continue
                action['kind'] = 'copy' if policy == 'copy' else 'overwrite'
                action['remote_edited'] = remote.get('page_last_edited_at')
            else:
                action['kind'] = 'update'
            action['replace'] = True
            actions.append(action)
            continue
        action['kind'] = 'create'
        actions.append(action)
    known = {note['rel'] for note in notes}
    for rel in files:
        if rel in known:
            continue
        if not (Path(vault) / rel).is_file():
            actions.append({'rel': rel, 'title': files[rel].get('title') or rel,
                            'kind': 'kept-remote', 'page': files[rel].get('page'),
                            'policy': policy, 'warnings': []})
    counts = {}
    for action in actions:
        counts[action['kind']] = counts.get(action['kind'], 0) + 1
    return {'actions': actions, 'counts': counts, 'policy': policy, 'root': root}


# ---------------------------------------------------------------- applying

def _backup(vault, run_id, rel, text):
    target = Path(vault) / '.同步工具' / '导出备份' / run_id / rel
    atomic_bytes(target, text.encode('utf-8'))
    return str(target)


def _create_page(client, parent_id, title, body):
    payload = {'properties': {'title': title}, 'content': body}
    if parent_id:
        result = client.write('create-pages', pages=[payload],
                              parent={'page_id': parent_id, 'type': 'page_id'},
                              allow_async=False)
    else:
        result = client.write('create-pages', pages=[payload],
                              creation_mode='draft', allow_async=False)
    check_write_result(result)
    pages = result.get('pages') or []
    if not pages or not pages[0].get('id'):
        raise SyncError('Notion 没有返回新建页面的编号，请先核对云端：' + title, code='ambiguous_write')
    return pages[0]['id'].replace('-', ''), pages[0].get('url', '')


def ensure_folder(client, vault, rel, state, root_page, run_id):
    """Create missing cloud pages for local folders, mirroring the vault layout."""
    if not rel:
        return root_page
    folders = state.setdefault('folders', {})
    parent = root_page
    for part in folder_chain(rel):
        if part in folders:
            parent = folders[part]
            continue
        title = PurePosixPath(part).name
        page_id, url = _create_page(client, parent, title, '')
        folders[part] = page_id
        write_json(state_path(vault), state)
        parent = page_id
    return parent


def render(action, image_hook=None):
    """Preview keeps local images as text; the push re-renders them as real uploads."""
    text = action.get('text')
    if image_hook is not None and text is not None:
        body, warnings = to_notion(text, page_urls=action.get('urls'), image_hook=image_hook)
        action['warnings'] = warnings
        return body
    return action['notion']


def apply(client, vault, result, state, run_id, log=None, cancel=None, image_hook=None, report=None):
    say = log or (lambda text: None)
    counts = {'created': 0, 'updated': 0, 'overwritten': 0, 'copied': 0,
              'unchanged': 0, 'conflict': 0, 'kept-remote': 0, 'failed': 0}
    records = []
    root_page = state.get('root_page')
    if not root_page:
        raise SyncError('还没有设置要上传到的 Notion 页面')
    for action in result['actions']:
        if cancel is not None and cancel.is_set():
            raise SyncError('已取消，已完成的上传保留，未处理的保持原样')
        kind = action['kind']
        rel = action['rel']
        note_image, image_hashes = note_images(rel, image_hook)
        if kind in ('unchanged', 'conflict', 'kept-remote'):
            counts[kind] += 1
            if kind != 'unchanged':
                records.append({'path': rel, 'action': kind, 'page': action.get('page')})
            if report:
                report(rel, kind, action.get('page'))
            continue
        try:
            if report:
                report(rel, 'started', action.get('page'))
            if kind == 'create':
                expected_body = render(action, note_image)
                planned, n_images = prepared_body(expected_body, image_hashes)
                parent = ensure_folder(client, vault, rel, state, root_page, run_id)
                page_id, url = _create_page(client, parent, action['title'], expected_body)
                action['page'] = page_id
                counts['created'] += 1
            elif kind in ('update', 'overwrite'):
                say('正在上传：' + rel)
                page_id = action['page']
                remote = client.call('fetch', id=page_id, include_file_urls=False, include_transcript=False)
                check_remote_guard(action, remote)
                _backup(vault, run_id, rel, remote.get('text', ''))
                edit = targeted_update(remote, action, note_image)
                expected_body = page_body(remote)
                if edit is not None:
                    expected_body = expected_body.replace(edit['old_str'], edit['new_str'], 1)
                planned, n_images = prepared_body(expected_body, image_hashes)
                if edit is not None:
                    if report:
                        report(rel, 'started', page_id, digest(planned), n_images)
                    done = client.write('update-page', page_id=page_id, command='update_content',
                                        content_updates=[edit], allow_async=False)
                    check_write_result(done)
                counts['overwritten' if kind == 'overwrite' else 'updated'] += 1
            elif kind in ('copy',):
                expected_body = render(action, note_image)
                planned, n_images = prepared_body(expected_body, image_hashes)
                parent = ensure_folder(client, vault, rel, state, root_page, run_id)
                stamp = datetime.now(LOCAL_TZ).strftime('%Y%m%d-%H%M')
                page_id, url = _create_page(client, parent, action['title'] + '（本地副本 ' + stamp + '）',
                                            expected_body)
                action['page'] = page_id
                counts['copied'] += 1
            else:
                raise SyncError('未知的上传动作：' + kind)
            if report:
                report(rel, 'started', page_id, digest(planned), n_images)
            confirmed_version = confirm_written_version(client, page_id, planned, n_images)
        except SyncError as exc:
            if getattr(exc, 'code', None) == 'ambiguous_write':
                raise
            if report:
                report(rel, 'failed', action.get('page'))
            counts['failed'] += 1
            records.append({'path': rel, 'action': 'failed', 'message': str(exc)})
            say('上传失败：%s（%s）' % (rel, exc))
            continue
        state['files'][rel] = {
            'page': action.get('page'),
            'title': action['title'],
            'local': action['hash'],
            'pushed': now_iso(),
            'remote_edited': confirmed_version,
        }
        write_json(state_path(vault), state)
        if report:
            report(rel, 'confirmed', page_id)
        records.append({'path': rel, 'action': kind, 'page': action.get('page')})
    state['last_run'] = run_id
    state['policy'] = result.get('policy') or state.get('policy')
    write_json(state_path(vault), state)
    return {'counts': counts, 'records': records, 'policy': state.get('policy')}


# ------------------------------------------------------------ page picking

def _target(node_id, title, kind, path=''):
    return {'id': node_id, 'url': 'https://app.notion.com/p/' + node_id,
            'title': title or '未命名', 'kind': kind or 'page', 'path': path or ''}


def search_targets(client, query, limit=30):
    """按关键词在 Notion 里找页面（只读，不会走创建/更新）。"""
    data = client.call('search', query=query)
    out, seen = [], set()
    for item in (data.get('results') or []):
        node = _target((item.get('id') or '').replace('-', ''), item.get('title'),
                       item.get('type'), item.get('path'))
        if not node['id'] or node['id'] in seen:
            continue
        seen.add(node['id'])
        out.append(node)
    return out[:limit]


def root_targets(client):
    """工作区顶层页面，可用于浏览。"""
    items = list_all(client, 'list-private-pages') + list_all(client, 'list-shared-pages')
    out, seen = [], set()
    for item in items:
        node = _target(identity(item.get('url') or ''), item.get('title'),
                       item.get('type') or item.get('object'))
        if not node['id'] or node['id'] in seen:
            continue
        seen.add(node['id'])
        out.append(node)
    out.sort(key=lambda n: (n['kind'] != 'page', n['title']))
    return out


def child_targets(client, node):
    """某个页面下的子页面；数据库（数据源）不能挂子页面，返回空。"""
    if node.get('kind') == 'database':
        return []
    page = client.call('fetch', id=node['id'], include_file_urls=False, include_transcript=False)
    out, seen = [], set()
    for kind, url, title in children_of(page.get('text') or ''):
        pid = identity(url)
        if not pid or pid == node['id'] or pid in seen:
            continue
        seen.add(pid)
        out.append(_target(pid, title, kind))
    return out
