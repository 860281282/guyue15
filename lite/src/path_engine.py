"""Path-based export: mirror the Obsidian folder tree into Notion by title.

Nothing here depends on notion_id inside the notes. The mapping is derived from
the local folder path plus the page titles Notion already has; missing folders
are created at the level they belong to, so a vault can build a Notion library
from zero.
"""
import re
from pathlib import Path, PurePosixPath

import export_engine as ex
from layout import atomic_bytes
from notion_connection import SyncError
from sync_engine import children_of, database_rows, identity, list_all


def norm_title(text):
    """Compare titles the way a person would: ignore case and extra spaces."""
    return ' '.join(str(text or '').replace('\u3000', ' ').split()).casefold()


def minimal_cover(folders):
    """Drop folders already covered by a selected ancestor, so nothing is read twice."""
    items = [str(f).strip('/') for f in folders if str(f).strip('/')]
    chosen = []
    for rel in sorted(set(items)):
        covered = False
        for other in items:
            if other != rel and (rel + '/').startswith(other + '/'):
                covered = True
                break
        if not covered:
            chosen.append(rel)
    return chosen


def list_folders(vault, rel=''):
    """Immediate sub folders of a vault folder, with note counts. No network."""
    vault = Path(vault)
    base = (vault / rel) if rel else vault
    if not base.is_dir():
        raise SyncError('找不到文件夹：' + (rel or str(vault)))
    out = []
    from vault_tools import is_obsidian_program
    for path in sorted(base.iterdir(), key=lambda p: p.name.casefold()):
        if not path.is_dir():
            continue
        if path.name in ex.SKIP_DIRS or path.name.startswith('.') or is_obsidian_program(path):
            continue
        child = (PurePosixPath(rel) / path.name).as_posix() if rel else path.name
        count = sum(1 for item in path.rglob('*.md') if item.is_file())
        direct = sum(1 for item in path.glob('*.md') if item.is_file())
        out.append({'rel': child, 'name': path.name, 'count': count, 'direct': direct})
    return out


class Resolver:
    """Find (or create) the Notion page that belongs to a local folder path."""

    def __init__(self, client, create=False, log=None, cancel=None):
        self.client = client
        self.create = create
        self.say = log or (lambda text: None)
        self.cancel = cancel
        self.kids = {}
        self.created = []
        self.taken = set()
        self.searches = 0

    def _check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise SyncError('已取消，未改动 Notion')

    def children(self, parent_id):
        if parent_id in self.kids:
            return self.kids[parent_id]
        if parent_id is None:
            items = []
            seen = set()
            for name in ('list-private-pages', 'list-shared-pages'):
                for item in list_all(self.client, name):
                    pid = identity(item.get('url'))
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    items.append((pid, item.get('title') or ''))
            self.kids[None] = items
            return items
        page = self.client.call('fetch', id=parent_id, include_file_urls=False,
                                include_transcript=False)
        text = page.get('text', '')
        items = []
        seen = set()
        match = re.search(r'data-source-url="(collection://[^"]+)"', text)
        if match:
            try:
                rows = database_rows(self.client, {'text': text, 'title': '数据库'}, match.group(1))
            except SyncError:
                raise
            if rows is not None:
                for row in rows.get('results') or []:
                    pid = identity(row.get('url'))
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    items.append((pid, row.get('title') or ''))
                self.kids[parent_id] = items
                return items
        for kind, url, title in children_of(text):
            if kind != 'page':
                continue
            pid = identity(url)
            if not pid or pid in seen or pid == parent_id:
                continue
            seen.add(pid)
            items.append((pid, title))
        self.kids[parent_id] = items
        return items

    def _ancestor_titles(self, page_id):
        page = self.client.call('fetch', id=page_id, include_file_urls=False,
                                include_transcript=False)
        text = page.get('text', '')
        names = []
        for match in re.finditer(r'<ancestor-\d+-page\s+[^>]*?title="([^"]*)"', text):
            names.append(norm_title(match.group(1)))
        if not names:
            for match in re.finditer(r'<parent-page\s+[^>]*?title="([^"]*)"', text):
                names.append(norm_title(match.group(1)))
        return names

    def search_page(self, title, want_path):
        """Search one title; accept a page whose ancestors end with want_path."""
        try:
            found = self.client.call('search', query=title)
        except SyncError:
            raise
        want = tuple(norm_title(p) for p in want_path if p)
        candidates = []
        for item in (found.get('results') or [])[:8]:
            if norm_title(item.get('title')) != norm_title(title):
                continue
            pid = identity(item.get('url'))
            if pid:
                candidates.append(pid)
        for pid in candidates:
            try:
                names = self._ancestor_titles(pid)
            except SyncError:
                raise
            if want and tuple(names[-len(want):]) == want:
                return pid
            if not want and not names:
                return pid
        return None

    def find_note(self, folder_parts, title):
        """Find an existing note page by title anywhere, preferring the same folder."""
        want = norm_title(title)
        if self.searches >= 90:
            return None
        self.searches += 1
        try:
            found = self.client.call('search', query=title)
        except SyncError:
            raise
        folder = [norm_title(p) for p in folder_parts if p]
        cands = []
        for item in (found.get('results') or [])[:8]:
            if norm_title(item.get('title')) != want:
                continue
            pid = identity(item.get('url'))
            if not pid or pid in self.taken:
                continue
            cands.append((pid, norm_title(item.get('path') or '')))
        if not cands:
            return None
        same = [pid for pid, path in cands
                if not folder or all(part in path for part in folder[-1:])]
        if not same:
            return None          # 只认同一个栏目下的同名页，避免抢别人的页
        if len(same) > 1:
            raise SyncError('Notion 存在多个可能匹配的页面「%s」，请先区分名称' % title)
        pick = same[0]
        self.taken.add(pick)
        return pick

    def find(self, parent_id, title):
        want = norm_title(title)
        hits = [pid for pid, text in self.children(parent_id) if norm_title(text) == want]
        if len(hits) > 1:
            raise SyncError('Notion 存在多个同名页面「%s」，请先区分名称再同步' % title)
        return hits[0] if hits else None

    def ensure(self, parent_id, title):
        """Return (page_id, created). page_id is None when previewing a missing page."""
        hit = self.find(parent_id, title)
        if hit:
            return hit, False
        if not self.create:
            return None, False
        self._check_cancel()
        if parent_id is None:
            pid = self._create_top(title)
            self.kids.setdefault(None, []).append((pid, title))
            self.created.append({'parent': None, 'title': title, 'id': pid})
            self.say('新建顶层栏目：' + title)
            return pid, True
        pid, _url = ex._create_page(self.client, parent_id, title, '')
        self.kids.setdefault(parent_id, []).append((pid, title))
        self.created.append({'parent': parent_id, 'title': title, 'id': pid})
        self.say('新建栏目：' + title)
        return pid, True

    def _create_top(self, title):
        self._check_cancel()
        result = self.client.write('create-pages',
                                   pages=[{'properties': {'title': title}, 'content': ''}],
                                   creation_mode='draft', allow_async=False)
        ex.check_write_result(result)
        pages = result.get('pages') or []
        if not pages or not pages[0].get('id'):
            raise SyncError('Notion 没有返回页面编号：' + title)
        return str(pages[0]['id']).replace('-', '')

    def anchor(self, name):
        """Wrapper page that holds the mirrored tree; empty name = workspace top level."""
        name = (name or '').strip()
        if not name:
            return None, False
        for item in list_all(self.client, 'list-private-pages'):
            if norm_title(item.get('title')) != norm_title(name):
                continue
            pid = identity(item.get('url'))
            if pid:
                return pid, False
        if not self.create:
            return None, False
        self._check_cancel()
        result = self.client.write('create-pages',
                                   pages=[{'properties': {'title': name}, 'content': ''}],
                                   creation_mode='draft', allow_async=False)
        pages = result.get('pages') or []
        if not pages or not pages[0].get('id'):
            raise SyncError('Notion 没有返回顶层页面的编号：' + name)
        pid = str(pages[0]['id']).replace('-', '')
        self.created.append({'parent': None, 'title': name, 'id': pid})
        self.say('新建顶层页面：' + name)
        return pid, True

    def folder_page(self, anchor_id, rel):
        """Resolve every folder segment of rel below the anchor (None = workspace root)."""
        parts = [p for p in PurePosixPath(rel).parts if p]
        parent = anchor_id
        for index, part in enumerate(parts):
            hit = self.find(parent, part)
            if hit:
                parent = hit
                continue
            found = self.search_page(part, parts[:index + 1])
            if found:
                self.kids.setdefault(parent, []).append((found, part))
                parent = found
                continue
            if not self.create:
                return None
            parent, _made = self.ensure(parent, part)
            if parent is None:
                return None
        return parent


def _state_entry(state, rel):
    return (state.get('files') or {}).get(rel) or {}


def plan_paths(client, vault, folders, state, policy=None, anchor_name='Obsidian',
               log=None, cancel=None, only=None, hints=None, collect=None):
    """Decide what to do for the selected folders without writing to Notion."""
    policy = policy or state.get('policy') or ex.DEFAULT_POLICY
    if policy not in ex.POLICIES:
        raise SyncError('未知的冲突处理方式：' + str(policy))
    say = log or (lambda text: None)
    urls = ex.link_urls(state)
    resolver = Resolver(client, create=False, log=say, cancel=cancel)
    anchor_id, anchor_exists = resolver.anchor(anchor_name or '')
    name = (anchor_name or '').strip()
    if name:
        say('顶层页面：' + name if anchor_exists else '顶层页面还不存在，上传时会新建：' + name)
    actions = []
    seen = set()
    folder_pages = {}
    for folder in folders:
        rel_folder = str(folder).strip('/')
        notes = ex.scan(vault, rel_folder, page_urls=urls, direct_only=True) if not rel_folder else ex.scan(vault, rel_folder, page_urls=urls)
        for note in notes:
            if cancel is not None and cancel.is_set():
                raise SyncError('已取消，未改动 Notion')
            rel = note['rel']
            if only is not None and rel not in only:
                continue
            if rel in seen:
                continue
            seen.add(rel)
            note_folder = PurePosixPath(rel).parent.as_posix()
            if note_folder == '.':
                note_folder = ''
            if note_folder not in folder_pages:
                folder_pages[note_folder] = resolver.folder_page(anchor_id, note_folder)
            page = folder_pages[note_folder]
            entry = _state_entry(state, rel)
            action = {'rel': rel, 'title': note['title'], 'hash': note['hash'],
                      'notion': note['notion'], 'text': note.get('text'),
                      'warnings': list(note['warnings']), 'urls': urls,
                      'policy': policy, 'folder': note_folder,
                      'parent': page}
            page_id = resolver.find(page, note['title']) if page else None
            if page_id is None:
                page_id = resolver.find_note(note_folder.split('/'), note['title'])
                if page_id and page is None:
                    action['new_folder'] = False
            if page_id:
                action['page'] = page_id
                action['parent'] = page
                # A title match alone does not prove ownership of an existing page.
                recorded = str(entry.get('page') or '').replace('-', '').lower()
                if not recorded or recorded != str(page_id).replace('-', '').lower():
                    action['kind'] = 'conflict'
                    action['warnings'].append('云端同名页面尚无可靠绑定，已停止覆盖，请人工核对')
                    actions.append(action)
                    continue
                if entry.get('local') == note['hash'] and entry.get('page') == page_id:
                    action['kind'] = 'unchanged'
                    actions.append(action)
                    if collect is not None:
                        collect[rel] = [note['hash'], entry.get('remote_edited') or '']
                    continue
                hint = (hints or {}).get(rel)
                if hint and hint[0] == note['hash'] and entry.get('page') == page_id:
                    action['kind'] = 'unchanged'
                    action['hinted'] = True
                    actions.append(action)
                    if collect is not None:
                        collect[rel] = [note['hash'], hint[1]]
                    continue
                say('正在检查：' + rel)
                remote = client.call('fetch', id=page_id, include_file_urls=False,
                                     include_transcript=False)
                action['_remote_guard'] = ex.remote_guard(remote)
                if ex.page_trashed(remote):
                    action['kind'] = 'conflict'
                    action['warnings'].append('云端页面已在回收站，已停止重建，请人工核对')
                    actions.append(action)
                    continue
                if collect is not None:
                    collect[rel] = [note['hash'], remote.get('page_last_edited_at') or '']
                edited = ex.parse_iso(remote.get('page_last_edited_at'))
                pushed = ex.parse_iso(entry.get('remote_edited') or entry.get('pushed'))
                if not edited or not pushed:
                    action['kind'] = 'conflict'
                    action['warnings'].append('无法确认云端版本，已停止覆盖')
                    actions.append(action)
                    continue
                if edited > pushed:
                    action['remote_edited'] = remote.get('page_last_edited_at')
                    if policy == 'skip':
                        action['kind'] = 'conflict'
                        actions.append(action)
                        continue
                    action['kind'] = 'copy' if policy == 'copy' else 'overwrite'
                else:
                    action['kind'] = 'update'
                action['replace'] = True
                actions.append(action)
                continue
            if entry.get('page'):
                action['kind'] = 'conflict'
                action['page'] = entry.get('page')
                action['warnings'].append('已绑定的云端页面未找到或不可访问，已停止新建，请人工核对')
                actions.append(action)
                continue
            action['kind'] = 'create'
            if page is None:
                action['new_folder'] = True
            actions.append(action)
    counts = {}
    for action in actions:
        counts[action['kind']] = counts.get(action['kind'], 0) + 1
    return {'actions': actions, 'counts': counts, 'policy': policy,
            'anchor': name, 'folders': list(folders),
            'new_folders': bool(resolver.created)}


FRONT = ex.FRONT


ID_LINE = re.compile(r'^\s*"?notion_(?:id|url)"?\s*:.*$', re.M)


def write_back(vault, rel, page_id, url):
    """Record the page id in the note itself. Convenience only, never required."""
    path = Path(vault) / rel
    try:
        text = path.read_text(encoding='utf-8-sig')
    except (OSError, UnicodeDecodeError):
        return
    lines = ['notion_id: "%s"' % page_id, 'notion_url: "%s"' % url]
    body_text = text.lstrip('\ufeff')
    match = FRONT.match(body_text)
    if match:
        front = match.group(1)
        if ID_LINE.search(front):
            kept = [line for line in front.rstrip('\n').split('\n')
                    if not ID_LINE.match(line)]
            front = '\n'.join(kept + lines) + '\n'
        else:
            front = front.rstrip('\n') + '\n' + '\n'.join(lines) + '\n'
        text = '---\n' + front + '---\n' + body_text[match.end():]
    else:
        text = '---\n' + '\n'.join(lines) + '\n---\n' + body_text
    try:
        atomic_bytes(path, text.encode('utf-8'))
    except OSError:
        pass


def apply_paths(client, vault, result, state, run_id, log=None, cancel=None,
                image_hook=None, write_ids=True, attrs_hook=None, report=None):
    """Create the missing folders and pages, then push every planned note."""
    say = log or (lambda text: None)
    counts = {'created': 0, 'updated': 0, 'overwritten': 0, 'copied': 0,
              'unchanged': 0, 'conflict': 0, 'folders': 0, 'failed': 0}
    records = []
    resolver = Resolver(client, create=True, log=say, cancel=cancel)
    anchor_name = result['anchor'] if 'anchor' in result else 'Obsidian'
    anchor_id = None
    anchor_ready = False
    state.setdefault('files', {})
    parents = {}
    for action in result['actions']:
        if cancel is not None and cancel.is_set():
            raise SyncError('已取消，已完成的上传保留，未处理的保持原样')
        kind = action['kind']
        rel = action['rel']
        note_image, image_hashes = ex.note_images(rel, image_hook)
        if kind in ('unchanged', 'conflict'):
            counts[kind] += 1
            if kind == 'conflict':
                records.append({'path': rel, 'action': kind, 'page': action.get('page')})
            if report:
                report(rel, kind, action.get('page'))
            continue
        try:
            if report:
                report(rel, 'started', action.get('page'))
            if kind in ('create', 'copy'):
                expected_body = ex.render(action, note_image)
                planned, n_images = ex.prepared_body(expected_body, image_hashes)
                folder = action.get('folder') or ''
                if folder not in parents:
                    before = len(resolver.created)
                    if not anchor_ready:
                        anchor_id, _made = resolver.anchor(anchor_name or '')
                        anchor_ready = True
                    parents[folder] = resolver.folder_page(anchor_id, folder) or anchor_id
                    counts['folders'] += len(resolver.created) - before
                parent = parents[folder]
            if kind == 'create':
                page_id, url = ex._create_page(client, parent, action['title'], expected_body)
                counts['created'] += 1
            elif kind in ('update', 'overwrite'):
                say('正在上传：' + rel)
                page_id = action['page']
                remote = client.call('fetch', id=page_id, include_file_urls=False,
                                     include_transcript=False)
                ex.check_remote_guard(action, remote)
                ex._backup(vault, run_id, rel, remote.get('text', ''))
                edit = ex.targeted_update(remote, action, note_image)
                expected_body = ex.page_body(remote)
                if edit is not None:
                    expected_body = expected_body.replace(edit['old_str'], edit['new_str'], 1)
                planned, n_images = ex.prepared_body(expected_body, image_hashes)
                if edit is not None:
                    if report:
                        report(rel, 'started', page_id, ex.digest(planned), n_images)
                    done = client.write('update-page', page_id=page_id, command='update_content',
                                        content_updates=[edit], allow_async=False)
                    ex.check_write_result(done)
                url = 'https://app.notion.com/p/' + page_id
                counts['overwritten' if kind == 'overwrite' else 'updated'] += 1
            elif kind == 'copy':
                stamp = ex.datetime.now(ex.LOCAL_TZ).strftime('%Y%m%d-%H%M')
                page_id, url = ex._create_page(
                    client, parent, action['title'] + '（本地副本 ' + stamp + '）',
                    expected_body)
                counts['copied'] += 1
            else:
                raise SyncError('未知的上传动作：' + kind)
            if report:
                report(rel, 'started', page_id, ex.digest(planned), n_images)
            confirmed_version = ex.confirm_written_version(client, page_id, planned, n_images)
        except SyncError as exc:
            if getattr(exc, 'code', None) == 'ambiguous_write':
                raise
            if report:
                report(rel, 'failed', action.get('page'))
            counts['failed'] += 1
            records.append({'path': rel, 'action': 'failed', 'message': str(exc)})
            say('上传失败：%s（%s）' % (rel, exc))
            continue
        stamp = ex.now_iso()
        state['files'][rel] = {
            'page': page_id,
            'title': action['title'],
            'local': action['hash'],
            'pushed': stamp,
            'remote_edited': confirmed_version,
            'folder': action.get('folder'),
        }
        ex.write_json(ex.state_path(vault), state)
        if write_ids and url:
            write_back(vault, rel, page_id, url)
        if attrs_hook is not None:
            try:
                attrs_hook(client, page_id, rel)
            except Exception as exc:  # noqa: BLE001  写属性失败只是降级，不影响正文
                if getattr(exc, 'code', None) == 'ambiguous_write':
                    raise
                say('属性回写失败：%s（%s）' % (rel, exc))
        if report:
            report(rel, 'confirmed', page_id)
        records.append({'path': rel, 'action': kind, 'page': page_id})
    state['anchor'] = result.get('anchor')
    state['last_run'] = run_id
    state['policy'] = result.get('policy') or state.get('policy')
    ex.write_json(ex.state_path(vault), state)
    return {'counts': counts, 'records': records, 'policy': state.get('policy'),
            'folders_created': resolver.created}
