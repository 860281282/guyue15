"""Obsidian → Notion 一键同步：把本地笔记上传回 Notion。

上传复用知识库的工作区绑定，冲突处理方式由界面上的「优先权」决定。
"""
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import secrets
import sys
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import urllib.parse

import notion_connection as nc
from notion_connection import Auth, MCP, SingleRun, SyncError, atomic_bytes
import export_engine as ex
import write_guard
import path_engine as pe
import journal

import home as syshome
HOME = syshome.home()
SETTINGS = HOME / '同步工具设置-导出.json'
LOCK = Path(os.environ.get('LOCALAPPDATA', str(HOME))) / 'NotionObsidianSync' / 'export.lock'
DEFAULT_VAULT = HOME.parent if HOME.name == '.同步工具' else HOME
ROOTS = ['知识库', '日常生活', '日程提醒']
POLICY_TEXT = {
    'skip': '云端优先：Notion 改过的页跳过',
    'overwrite': '本地优先：用 Obsidian 覆盖 Notion（覆盖前备份云端正文）',
    'copy': '两边都留：冲突时在 Notion 新建「本地副本」页',
}


def now_id():
    return datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)


def console_log(text):
    stream = sys.stdout
    if stream is None:
        return
    try:
        stream.write(str(text) + '\n')
    except UnicodeEncodeError:
        encoding = getattr(stream, 'encoding', None) or 'utf-8'
        stream.write((str(text) + '\n').encode(encoding, 'backslashreplace').decode(encoding))
    stream.flush()


def _finish(code):
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.close()
        except Exception:
            pass
    os._exit(code)


def permission_hint(exc):
    if isinstance(exc, PermissionError):
        return '。写入被系统或安全软件拦截：请把本程序加入杀毒软件信任区（火绒、360、Defender），或在弹窗中允许后重试'
    return ''


def record_failure(mode, exc, home=HOME, vault=None, session=None):
    trace = journal.sanitize(''.join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
    report = {'operation': mode, 'type': type(exc).__name__, 'errno': getattr(exc, 'errno', None),
              'winerror': getattr(exc, 'winerror', None), 'service_code': getattr(exc, 'code', None),
              'version': journal.VERSION, 'vault': str(vault) if vault else None,
              'message': journal.sanitize(str(exc))[:400], 'traceback': trace[-8000:],
              'log': str(session.path) if session and session.path else None,
              'frames': [{'file': Path(f.filename).name, 'line': f.lineno, 'function': f.name}
                         for f in traceback.extract_tb(exc.__traceback__)]}
    report_path = home / '数据/最近错误-导出.json'
    try:
        atomic_bytes(report_path, json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'))
    except OSError:
        return None
    return report_path


def page_id_of(text):
    if not text:
        return None
    match = re.search(r'([a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})', str(text).strip(), re.I)
    return match.group(1).replace('-', '').lower() if match else None


def load_settings(home=HOME):
    try:
        return json.loads((home / SETTINGS.name).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save_settings(data, home=HOME):
    atomic_bytes(home / SETTINGS.name, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


class UploadAttachments:
    def __init__(self, vault, client, cancel):
        self.vault = vault
        self.client = client
        self.cancel = cancel
        self.index = None
        self.fingerprints = {}

    def _inside(self, path):
        if not Path(os.path.abspath(path)).is_relative_to(self.vault):
            raise SyncError('附件指向知识库外，已停止上传')
        path = path.resolve()
        if not path.is_relative_to(self.vault):
            raise SyncError('附件指向知识库外，已停止上传')
        if any(part.startswith('.') for part in path.relative_to(self.vault).parts):
            raise SyncError('附件位于隐藏文件或程序数据目录，已停止上传')
        return path

    def resolve(self, rel, source):
        clean = urllib.parse.unquote(str(source).split('|')[0].split('#')[0].strip())
        candidate = Path(clean)
        if candidate.suffix.lower() not in nc.IMAGE_SUFFIXES:
            raise SyncError('附件不是受支持的本地图片，已停止上传：' + candidate.name)
        if candidate.is_absolute() or candidate.drive or candidate.root:
            if not candidate.is_absolute():
                raise SyncError('附件路径缺少完整位置，已停止上传')
            matches = {self._inside(candidate)}
        elif '/' in clean or '\\' in clean:
            note_dir = (self.vault / rel).parent
            matches = {self._inside(note_dir / candidate)}
            if not any(path.is_file() for path in matches) and '..' not in candidate.parts:
                matches.add(self._inside(self.vault / candidate))
        else:
            if self.index is None:
                self.index = {}
                for path in self.vault.rglob('*'):
                    if path.suffix.lower() not in nc.IMAGE_SUFFIXES or not path.is_file():
                        continue
                    if any(part.startswith('.') for part in path.relative_to(self.vault).parts):
                        continue
                    self.index.setdefault(path.name.casefold(), []).append(path)
            matches = {self._inside(path) for path in self.index.get(candidate.name.casefold(), [])}
        matches = {path for path in matches if path.is_file()}
        if not matches:
            raise SyncError('找不到库内图片，已停止上传：' + candidate.name)
        if len(matches) != 1:
            raise SyncError('找到多份同名图片，请在笔记里填明相对路径：' + candidate.name)
        return matches.pop()

    def preflight(self, actions):
        def check(rel, src):
            path = self.resolve(rel, src)
            with path.open('rb') as stream:
                content = stream.read(nc.UPLOAD_LIMIT + 1)
            if not 0 < len(content) <= nc.UPLOAD_LIMIT:
                raise SyncError('图片为空或超过上传大小限制：' + path.name)
            self.fingerprints[rel, src] = (path, ex.digest(content))
            return 'file-upload://preflight'

        for action in actions:
            if action['kind'] in ('create', 'update', 'overwrite', 'copy') and action.get('text') is not None:
                ex.to_notion(action['text'], action.get('urls'), lambda src: check(action['rel'], src))
        return [(rel, src, path.relative_to(self.vault).as_posix(), checksum)
                for (rel, src), (path, checksum) in sorted(self.fingerprints.items())]

    def upload(self, rel, source):
        path = self.resolve(rel, source)
        if (rel, source) not in self.fingerprints or self.fingerprints[rel, source][0] != path:
            raise SyncError('附件位置已变化，请重新预览。')
        return nc.upload_image(self.client, path, self.cancel, expected_checksum=self.fingerprints[rel, source][1])


def review_upload(vault, log, cancel):
    with SingleRun():
        pending = write_guard.load(vault)
        if pending is None:
            log('当前知识库没有待核对的上传')
            return
        entries = pending.get('entries', {})
        reviewable = []
        for rel in pending['paths']:
            entry = entries.get(rel, {})
            status = entry.get('status', 'pending' if 'entries' in pending else 'unknown')
            page = (entry.get('page') or '').replace('-', '')
            if status == 'started' and re.fullmatch(r'[a-fA-F0-9]{32}', page) and entry.get('body_hash'):
                reviewable.append((rel, page, entry['body_hash'], entry.get('image_count', 0)))
            else:
                log('%s  %s' % (write_guard.STATUS_TEXT[status], rel))
        if not reviewable:
            return
        state = ex.load_state(vault)
        if not state.get('workspace_id'):
            raise SyncError('待核对记录缺少已绑定的工作区，请在原工作区人工核对')
        client = MCP(Auth(), cancel)
        client.start()
        ex.bind_workspace(vault, state, client.call('get-self'))
        for rel, page, expected, image_count in reviewable:
            if cancel.is_set():
                raise SyncError('已取消核对')
            try:
                _, body = ex.read_written_page(client, page)
                verified = ex.verified_remote_body(client, body, image_count)
                label = '云端正文与计划一致' if ex.digest(verified) == expected else '云端正文与计划不一致'
            except (SyncError, OSError, ValueError):
                label = '云端页面未能确认'
            log('%s  %s' % (label, rel))
        log('只读核对已结束。确认本地和云端都妥当后，可清除待核对记录')


def push(vault, root, target, policy, log, cancel, home=HOME, dry_run=False, limit=None):
    vault = Path(vault).resolve()
    if not ex.vault_ready(vault):
        raise SyncError('请选择已有的 Obsidian 知识库文件夹，里面应有 .obsidian 文件夹')
    target_id = page_id_of(target)
    if not target_id:
        raise SyncError('请填写要上传到的 Notion 页面链接（在 Notion 里复制页面链接）')
    if not dry_run:
        write_guard.check(vault)
    with SingleRun():
        if not dry_run:
            write_guard.check(vault)
        state = ex.load_state(vault)
        client = MCP(Auth(), cancel)
        client.start()
        workspace = ex.bind_workspace(vault, state, client.call('get-self'))
        if state.get('root_page') and state['root_page'] != target_id:
            state['folders'] = {}
            log('目标页面换了，云端文件夹会按新位置重建')
        state['root_page'] = target_id
        state['policy'] = policy
        log('已连接 Notion：' + str(workspace.get('name') or '未知工作区'))
        log('冲突处理方式：' + POLICY_TEXT.get(policy, policy))
        target_page = client.call('fetch', id=target_id, include_file_urls=False, include_transcript=False)
        if ex.page_trashed(target_page):
            raise SyncError('目标页面在 Notion 回收站里，请先在 Notion 恢复它，或者换一个页面')
        result = ex.plan(client, vault, root, state, policy=policy, log=log, cancel=cancel)
        counts = result['counts']
        log('清单：新建 %d 篇，更新 %d 篇，覆盖 %d 篇，副本 %d 篇，未变 %d 篇，冲突 %d 篇，云端仍有 %d 篇' % (
            counts.get('create', 0), counts.get('update', 0), counts.get('overwrite', 0),
            counts.get('copy', 0), counts.get('unchanged', 0), counts.get('conflict', 0),
            counts.get('kept-remote', 0)))
        for action in result['actions']:
            if action['kind'] in ('create', 'update', 'overwrite', 'copy', 'conflict'):
                log('· %s %s' % ({'create': '新建', 'update': '更新', 'overwrite': '覆盖',
                                  'copy': '副本', 'conflict': '冲突'}[action['kind']], action['rel']))
        def collect_warnings(actions):
            items = []
            for action in actions:
                if action['kind'] not in ('create', 'update', 'overwrite', 'copy'):
                    continue
                for item in action.get('warnings') or []:
                    line = action['rel'] + '：' + item
                    if line not in items:
                        items.append(line)
            return items[:200]

        result['warnings'] = collect_warnings(result['actions'])
        atomic_bytes(home / '数据/导出预览.json',
                     json.dumps({'at': ex.now_iso(), 'vault': str(vault), 'root': root,
                                 'policy': policy, 'counts': counts,
                                 'warnings': result['warnings']}, ensure_ascii=False, indent=2).encode('utf-8'))
        if dry_run:
            log('预览完成：没有改动 Notion')
            return result
        if cancel.is_set():
            raise SyncError('已取消，未改动 Notion')
        attachments = UploadAttachments(vault, client, cancel)
        attachments.preflight(result['actions'])
        run_id = now_id()
        log('开始上传，请勿关闭窗口')
        operation = write_guard.begin(vault, [a['rel'] for a in result['actions'] if a.get('rel')])
        ex.write_json(ex.state_path(vault), state)
        applied = ex.apply(client, vault, result, state, run_id, log=log, cancel=cancel,
                           image_hook=attachments.upload, report=write_guard.reporter(vault, operation))
        if applied['counts'].get('failed') or applied['counts'].get('conflict'):
            raise SyncError('部分笔记上传失败或存在冲突，请先核对后再继续')
        write_guard.finish(vault, operation)
        result['warnings'] = collect_warnings(result['actions'])
        out = dict(result)
        out.update({'counts': applied['counts'], 'records': applied['records'], 'run_id': run_id})
        atomic_bytes(home / '数据/最近导出.json', json.dumps(
            {'at': ex.now_iso(), 'vault': str(vault), 'root': root, 'policy': policy,
             'counts': applied['counts'], 'records': applied['records'],
             'warnings': result['warnings']}, ensure_ascii=False, indent=2).encode('utf-8'))
        log('上传结束：新建 %d，更新 %d，覆盖 %d，副本 %d，跳过 %d，冲突 %d，失败 %d' % (
            applied['counts'].get('created', 0), applied['counts'].get('updated', 0),
            applied['counts'].get('overwritten', 0), applied['counts'].get('copied', 0),
            applied['counts'].get('unchanged', 0), applied['counts'].get('conflict', 0),
            applied['counts'].get('failed', 0)))
        for warning in result['warnings'][:40]:
            log('格式提示：' + warning)
        return out


def push_folders(vault, folders, anchor, policy, log, cancel, home=HOME, dry_run=False,
                 only=None, attrs=False, ledger_on=False, expected=None, pending_operation=None,
                 write_ids=True, expected_preview=None):
    """Mirror the chosen folders into Notion by path. No page link, no notion_id."""

    vault = Path(vault).resolve()
    if not ex.vault_ready(vault):
        raise SyncError('请选择已有的 Obsidian 知识库文件夹，里面应有 .obsidian 文件夹')
    anchor = (anchor or '').strip()
    picked = pe.minimal_cover(folders)
    if not picked:
        picked = [item['rel'] for item in pe.list_folders(vault)]
        if any(p.is_file() and not p.name.startswith('.') and p.suffix.lower() == '.md' for p in vault.iterdir()):
            picked.insert(0, '')
    if not picked:
        raise SyncError('知识库里还没有可同步的文件夹')
    if not dry_run:
        write_guard.check(vault, pending_operation)
    with SingleRun():
        if not dry_run:
            write_guard.check(vault, pending_operation)
        state = ex.load_state(vault)
        state['policy'] = policy
        client = MCP(Auth(), cancel)
        client.start()
        workspace = ex.bind_workspace(vault, state, client.call('get-self'))
        log('已连接 Notion：' + str(workspace.get('name') or '未知工作区'))
        log('冲突处理方式：' + POLICY_TEXT.get(policy, policy))
        log('同步范围：' + '、'.join(name or '知识库根目录' for name in picked))
        if anchor:
            log('顶层页面：' + anchor + '（不存在会自动新建）')
        else:
            log('顶层对应：直接对到 Notion 工作区顶层（不新建外层页面面）')
        cache_path = Path(home) / '数据' / '预览缓存.json'
        hints, collect = None, {}
        if dry_run:
            try:
                hints = json.loads(cache_path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                hints = None
            if not isinstance(hints, dict):
                hints = None
        result = pe.plan_paths(client, vault, picked, state, policy=policy,
                               anchor_name=anchor, log=log, cancel=cancel, only=only,
                               hints=hints, collect=collect)
        attachments = UploadAttachments(vault, client, cancel)
        media = attachments.preflight(result['actions'])
        keys = ('kind', 'rel', 'hash', 'page', 'parent', '_remote_guard')
        plan = [[action.get(key) for key in keys] for action in result['actions']]
        result['preview_fingerprint'] = ex.digest(json.dumps(
            [workspace['id'], picked, anchor, policy, attrs, plan, media], ensure_ascii=False))
        result['workspace_name'] = str(workspace.get('name') or 'Notion 工作区')
        if expected_preview is not None and result['preview_fingerprint'] != expected_preview:
            raise SyncError('上传清单已变化，请重新预览后再确认。')
        if dry_run and collect:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(collect, ensure_ascii=False), encoding='utf-8')
            except OSError:
                pass
        counts = result['counts']
        log('清单：新建 %d 篇，更新 %d 篇，覆盖 %d 篇，副本 %d 篇，未变 %d 篇，冲突 %d 篇' % (
            counts.get('create', 0), counts.get('update', 0), counts.get('overwrite', 0),
            counts.get('copy', 0), counts.get('unchanged', 0), counts.get('conflict', 0)))
        label = {'create': '新建', 'update': '更新', 'overwrite': '覆盖',
                 'copy': '副本', 'conflict': '冲突'}
        for action in result['actions']:
            if action['kind'] in label:
                extra = '（栏目也要新建）' if action.get('new_folder') else ''
                if action['kind'] == 'conflict' and action.get('warnings'):
                    extra += '（' + '；'.join(action['warnings'][:2]) + '）'
                log('· %s %s%s' % (label[action['kind']], action['rel'], extra))

        def collect_warnings(actions):
            items = []
            for action in actions:
                if action['kind'] not in ('create', 'update', 'overwrite', 'copy'):
                    continue
                for item in action.get('warnings') or []:
                    line = action['rel'] + '：' + item
                    if line not in items:
                        items.append(line)
            return items[:200]

        result['warnings'] = collect_warnings(result['actions'])
        atomic_bytes(home / '数据/导出预览.json', json.dumps(
            {'at': ex.now_iso(), 'vault': str(vault), 'folders': picked, 'anchor': anchor,
             'policy': policy, 'counts': counts, 'warnings': result['warnings']},
            ensure_ascii=False, indent=2).encode('utf-8'))
        if dry_run:
            log('预览完成：没有改动 Notion')
            return result
        if cancel.is_set():
            raise SyncError('已取消，未改动 Notion')
        if expected is not None:
            import change_cache
            latest = change_cache.snapshot(vault, folders)
            if any(latest.get(rel) != meta for rel, meta in expected.items()):
                raise SyncError('笔记在上传准备期间发生变化，已暂停自动上传')
        run_id = now_id()
        log('开始上传，请勿关闭窗口')

        hook = None
        if attrs:
            import attributes as ab

            def hook(client_, page_id, rel, _vault=vault, _log=log):
                ab.push(client_, page_id, Path(_vault) / rel, _log)
        manual_operation = None
        if pending_operation is None:
            manual_operation = write_guard.begin(vault, [a['rel'] for a in result['actions'] if a.get('rel')])
        else:
            write_guard.check(vault, pending_operation)
        ex.write_json(ex.state_path(vault), state)
        applied = pe.apply_paths(client, vault, result, state, run_id, log=log,
                                 cancel=cancel, image_hook=attachments.upload, attrs_hook=hook, write_ids=write_ids,
                                 report=write_guard.reporter(vault, manual_operation or pending_operation))
        if manual_operation is not None:
            if applied['counts'].get('failed') or applied['counts'].get('conflict'):
                raise SyncError('部分笔记上传失败或存在冲突，请先核对后再继续')
            write_guard.finish(vault, manual_operation)
        result['warnings'] = collect_warnings(result['actions'])
        out = dict(result)
        out.update({'counts': applied['counts'], 'records': applied['records'],
                    'run_id': run_id, 'folders_created': applied.get('folders_created')})
        atomic_bytes(home / '数据/最近导出.json', json.dumps(
            {'at': ex.now_iso(), 'vault': str(vault), 'folders': picked, 'anchor': anchor,
             'policy': policy, 'counts': applied['counts'], 'records': applied['records'],
             'warnings': result['warnings']}, ensure_ascii=False, indent=2).encode('utf-8'))
        log('上传结束：新建 %d，更新 %d，覆盖 %d，副本 %d，跳过 %d，冲突 %d，失败 %d' % (
            applied['counts'].get('created', 0), applied['counts'].get('updated', 0),
            applied['counts'].get('overwritten', 0), applied['counts'].get('copied', 0),
            applied['counts'].get('unchanged', 0), applied['counts'].get('conflict', 0),
            applied['counts'].get('failed', 0)))
        for warning in result['warnings'][:40]:
            log('格式提示：' + warning)
        return out


# ------------------------------------------------------------------ window
BG = '#f8f7f3'
CARD = '#ffffff'
LINE = '#e4e1d8'
INK = '#22201c'
MUTED = '#6f6a5f'
ACCENT = '#245d51'
ACCENT_SOFT = '#edf4f0'
ERR = '#a4362c'
WARN = '#8a6a12'
OK = '#1f6b4a'
VERSION = '0.10.2'

BIN_ORDER = ['skip', 'overwrite', 'copy']
BIN_TEXT = {'skip': '云端优先——Notion 改过就跳过', 'overwrite': '本地优先——用本地覆盖 Notion',
            'copy': '两边都留——冲突时新建副本'}


def center(window, width, height):
    root = window.master if window.master else window
    x = root.winfo_rootx() + max(0, (root.winfo_width() - width) // 2)
    y = root.winfo_rooty() + max(30, (root.winfo_height() - height) // 3)
    window.geometry('%dx%d+%d+%d' % (width, height, x, y))


def open_picker(parent, target_var, log=None):
    """弹窗里搜索或逐级浏览 Notion，选中一页后回填目标页。

    网络请求都在后台线程里跑，结果通过队列交回主线程处理，避免 Tk 跨线程调用。
    """
    def note(text):
        if log:
            log(text)

    window = tk.Toplevel(parent)
    window.title('选择 Notion 页面 · ' + VERSION)
    window.geometry('880x620')
    window.minsize(720, 480)
    window.configure(background=BG)
    try:
        window.transient(parent)
    except tk.TclError:
        pass
    center(window, 880, 620)

    tasks = queue.Queue()
    done = queue.Queue()
    stop = threading.Event()
    nodes = {}
    loaded = set()

    head = ttk.Frame(window, padding=(18, 14, 18, 0))
    head.pack(fill='x')
    ttk.Label(head, text='选择要上传到的 Notion 页面',
              background=BG, foreground=INK,
              font=('Microsoft YaHei UI', 14, 'bold')).pack(anchor='w')
    status = tk.StringVar(value='正在连接 Notion，请稍候…')
    ttk.Label(head, textvariable=status, style='Muted.TLabel').pack(anchor='w', pady=(4, 10))

    searchrow = ttk.Frame(window, padding=(18, 0, 18, 0))
    searchrow.pack(fill='x')
    query = tk.StringVar(value='')
    entry = ttk.Entry(searchrow, textvariable=query)
    entry.pack(side='left', fill='x', expand=True)
    search_button = ttk.Button(searchrow, text='搜索')
    search_button.pack(side='left', padx=(8, 0))
    browse_button = ttk.Button(searchrow, text='看顶层页面')
    browse_button.pack(side='left', padx=(8, 0))

    body = ttk.Frame(window, padding=(18, 10, 18, 0))
    body.pack(fill='both', expand=True)
    tree = ttk.Treeview(body, show='tree headings',
                        columns=('place', 'kind'), selectmode='browse')
    tree.heading('#0', text='页面')
    tree.column('#0', width=500, stretch=True)
    tree.heading('place', text='所在位置')
    tree.column('place', width=220, stretch=True)
    tree.heading('kind', text='类型')
    tree.column('kind', width=70, anchor='center', stretch=False)
    bar = ttk.Scrollbar(body, orient='vertical', command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    tree.pack(side='left', fill='both', expand=True)
    bar.pack(side='right', fill='y')

    footer = ttk.Frame(window, padding=(18, 12, 18, 14))
    footer.pack(fill='x', side='bottom')
    use_button = ttk.Button(footer, text='使用选中', style='Accent.TButton', state='disabled')
    use_button.pack(side='right')
    close_button = ttk.Button(footer, text='关闭')
    close_button.pack(side='right', padx=(0, 8))
    count = tk.StringVar(value='')
    ttk.Label(footer, textvariable=count, style='Muted.TLabel').pack(side='left')

    def close():
        stop.set()
        tasks.put(None)
        window.destroy()

    def add(node, parent_iid=''):
        marker = '· ' if node['kind'] == 'database' else ''
        iid = tree.insert(parent_iid, 'end', text='  ' + marker + node['title'],
                          values=(node['path'] or '',
                                  '数据库' if node['kind'] == 'database' else '页面'))
        nodes[iid] = node
        if node['kind'] != 'database':
            tree.insert(iid, 'end', text='  （展开这一页才会读它的子页面）', values=('', ''))
        return iid

    def reset(rows, message):
        tree.delete(*tree.get_children(''))
        nodes.clear()
        for node in rows:
            add(node)
        count.set(message)
        status.set(message)
        if rows:
            first = tree.get_children('')[0]
            tree.selection_set(first)
            tree.focus(first)
            use_button.configure(state='normal')
        else:
            use_button.configure(state='disabled')

    def on_children(node, kids):
        iid = next((k for k, v in nodes.items() if v['id'] == node['id']), None)
        if not iid:
            return
        for child in tree.get_children(iid):
            tree.delete(child)
        loaded.add(node['id'])
        if not kids:
            tree.insert(iid, 'end', text='  （这一页下面没有子页面）', values=('', ''))
            status.set(node['title'] + '：没有子页面')
            return
        for kid in kids:
            add(kid, iid)
        tree.item(iid, open=True)
        status.set('%s：%d 个子页面' % (node['title'], len(kids)))

    def drain():
        try:
            while True:
                message = done.get_nowait()
                kind = message[0]
                if kind == 'roots':
                    rows = message[1]
                    reset(rows, '已找到 %d 个顶层页面；展开或搜索都能定位' % len(rows))
                elif kind == 'children':
                    on_children(message[1], message[2])
                elif kind == 'search':
                    word, hits = message[1], message[2]
                    reset(hits, '「%s」找到 %d 个页面（双击或点“使用选中”）' % (word, len(hits)))
                elif kind == 'status':
                    status.set(message[1])
        except queue.Empty:
            pass
        if window.winfo_exists():
            window.after(120, drain)

    def worker():
        client = None
        try:
            with SingleRun():
                client = MCP(Auth(), stop)
                client.start()
            done.put(('roots', ex.root_targets(client)))
            while True:
                task = tasks.get()
                if task is None:
                    return
                kind, payload = task
                try:
                    if kind == 'children':
                        done.put(('children', payload, ex.child_targets(client, payload)))
                    elif kind == 'search':
                        done.put(('search', payload, ex.search_targets(client, payload)))
                    elif kind == 'roots':
                        done.put(('roots', ex.root_targets(client)))
                except SyncError as exc:
                    done.put(('status', str(exc)))
                except Exception as exc:  # noqa: BLE001 - 任何网络异常都只提示，不炸窗口
                    done.put(('status', '读取失败：%s' % exc))
        except SyncError as exc:
            done.put(('status', str(exc)))
        except Exception as exc:  # noqa: BLE001
            done.put(('status', '连接失败：%s' % exc))

    def run_search(event=None):
        word = query.get().strip()
        if not word:
            status.set('先输入关键词，或点“看顶层页面”')
            return 'break'
        status.set('搜索「%s」…' % word)
        count.set('搜索中…')
        tasks.put(('search', word))
        return 'break'

    def browse():
        query.set('')
        status.set('读取顶层页面…')
        count.set('读取中…')
        tasks.put(('roots', None))

    def on_open(event=None):
        iid = tree.focus()
        node = nodes.get(iid)
        if not node or node['kind'] == 'database' or node['id'] in loaded:
            return
        status.set('正在读取「%s」的子页面…' % node['title'])
        tasks.put(('children', node))

    def use_selected(event=None):
        selection = tree.selection()
        iid = selection[0] if selection else tree.focus()
        node = nodes.get(iid)
        if not node:
            return
        if node['kind'] == 'database':
            status.set('这是数据库，不能作为上传目标；请选它所在的页面')
            return
        target_var.set(node['url'])
        note('已选择目标页面：%s' % node['title'])
        status.set('已选择：%s' % node['title'])
        window.after(180, close)

    search_button.configure(command=run_search)
    browse_button.configure(command=browse)
    use_button.configure(command=use_selected)
    close_button.configure(command=close)
    entry.bind('<Return>', run_search)
    tree.bind('<<TreeviewOpen>>', on_open)
    tree.bind('<Double-1>', use_selected)
    window.protocol('WM_DELETE_WINDOW', close)
    window.bind('<Escape>', lambda event: close())
    drain()
    threading.Thread(target=worker, daemon=True).start()


def pick_folders(parent, vault_var, folder_state, on_change):
    """Pick local folders to mirror. Reads the file system only, so it opens instantly."""
    vault = Path(vault_var.get().strip() or DEFAULT_VAULT)
    if not ex.vault_ready(vault):
        messagebox.showinfo('先选知识库', '请先选择一个含有 .obsidian 文件夹的知识库', parent=parent)
        return
    window = tk.Toplevel(parent)
    window.title('选择要同步的文件夹 · ' + VERSION)
    window.configure(background=BG)
    center(window, 880, 620)
    window.transient(parent)

    selected = set(folder_state.get('items') or [])
    opened = set()
    loaded = {}
    tops = pe.list_folders(vault, '')

    head = tk.Frame(window, background=BG)
    head.pack(fill='x', padx=18, pady=(16, 6))
    tk.Label(head, text='选择要同步到 Notion 的文件夹', background=BG, foreground=INK,
             font=('Microsoft YaHei UI', 15, 'bold')).pack(anchor='w')
    hint = tk.StringVar(value='点行首方框勾选；点整行展开或收起（展开才会列出子文件夹）。')
    tk.Label(head, textvariable=hint, background=BG, foreground=MUTED,
             font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=(4, 0))

    count = tk.StringVar(value='')
    tk.Label(head, textvariable=count, background=BG, foreground=ACCENT,
             font=('Microsoft YaHei UI', 9)).pack(anchor='w', pady=(6, 0))

    body = tk.Frame(window, background=BG)
    body.pack(fill='both', expand=True, padx=18, pady=(6, 0))
    style = ttk.Style(window)
    style.configure('Pick.Treeview', font=('Microsoft YaHei UI', 13), rowheight=46)
    big_tri = False
    try:
        blank = tk.PhotoImage(width=1, height=1)
        style.element_create('Blank.Treeitem.indicator', 'image', blank)
        style.layout('Pick.Treeview.Item', [
            ('Treeitem.padding', {'sticky': 'nswe', 'children': [
                ('Blank.Treeitem.indicator', {'side': 'left', 'sticky': ''}),
                ('Treeitem.image', {'side': 'left', 'sticky': ''}),
                ('Treeitem.focus', {'side': 'left', 'sticky': '', 'children': [
                    ('Treeitem.text', {'side': 'left', 'sticky': ''})]})]})])
        big_tri = True
    except tk.TclError:
        big_tri = False
    tree = ttk.Treeview(body, columns=('notes',), show='tree headings', style='Pick.Treeview')
    tree.heading('#0', text='文件夹')
    tree.heading('notes', text='笔记数')
    tree.column('#0', width=560, anchor='w')
    tree.column('notes', width=90, anchor='e', stretch=False)
    bar = ttk.Scrollbar(body, orient='vertical', command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    tree.pack(side='left', fill='both', expand=True)
    bar.pack(side='right', fill='y')

    def covered(rel):
        return any(rel != other and (rel + '/').startswith(other + '/') for other in selected)

    def has_kids(rel):
        try:
            for entry in (vault / rel).iterdir():
                if entry.is_dir() and not entry.name.startswith('.'):
                    return True
        except OSError:
            return False
        return False

    def marker(rel):
        if not big_tri or not has_kids(rel):
            return '    '
        return '▼  ' if rel in opened else '▶  '

    def label(rel, name):
        mark = '☑' if (rel in selected or covered(rel)) else '☐'
        return marker(rel) + mark + '  ' + name

    def kids_of(rel):
        if rel not in loaded:
            loaded[rel] = pe.list_folders(vault, rel)
        return loaded[rel]

    def paint(rel):
        pending = rel + ':pending'
        if tree.exists(pending):
            tree.delete(pending)
        for item in kids_of(rel):
            iid = item['rel']
            if tree.exists(iid):
                tree.delete(iid)
            tree.insert(rel, 'end', iid=iid, text=label(iid, item['name']),
                        values=(item['count'],), open=False)
            tree.insert(iid, 'end', iid=iid + ':pending', text='    （展开这一项才会列出子文件夹）')

    def repaint_marks():
        for item in tops:
            if tree.exists(item['rel']):
                tree.item(item['rel'], text=label(item['rel'], item['name']))
        for rel in list(loaded):
            if not tree.exists(rel):
                continue
            for kid in loaded[rel]:
                if tree.exists(kid['rel']):
                    tree.item(kid['rel'], text=label(kid['rel'], kid['name']))

    def refresh_count():
        picks = pe.minimal_cover(selected) or [item['rel'] for item in tops]
        total = 0
        for rel in picks:
            base = vault / rel
            total += sum(1 for _ in base.rglob('*.md')) if base.is_dir() else 0
        if selected:
            count.set('已选 %d 个栏目，共 %d 篇笔记' % (len(picks), total))
        else:
            count.set('没有勾选 = 同步全部 %d 个栏目，共 %d 篇笔记' % (len(picks), total))
        return picks

    def toggle(rel):
        if covered(rel):
            hint.set('「%s」包含在已选的上级文件夹里，先去取消上级。' % rel)
            return
        if rel in selected:
            selected.discard(rel)
        else:
            selected.add(rel)
            for other in list(selected):
                if other != rel and (other + '/').startswith(rel + '/'):
                    selected.discard(other)
        hint.set('点行首方框勾选；点整行展开或收起（展开才会列出子文件夹）。')
        tree.item(rel, text=label(rel, PurePosixPath(rel).name))
        for item in kids_of(rel):
            if tree.exists(item['rel']):
                tree.item(item['rel'], text=label(item['rel'], item['name']))
        refresh_count()

    def on_open(event):
        iid = tree.focus()
        if not iid or iid.endswith(':pending'):
            return
        opened.add(iid)
        if not tree.parent(iid):
            paint(iid)
        repaint_marks()
        if not tree.parent(iid):
            paint(iid)
        pending = iid + ':pending'
        if tree.exists(pending):
            tree.delete(pending)

    def on_click(event):
        iid = tree.identify_row(event.y)
        if not iid or iid.endswith(':pending'):
            return
        box = tree.bbox(iid, '#0')
        if not box:
            return None
        rel_x = event.x - box[0]
        triw = 30 if big_tri else 0
        if triw < rel_x <= triw + 46:
            toggle(iid)
            return 'break'
        was_open = iid in opened
        paint(iid)
        if was_open:
            opened.discard(iid)
        else:
            opened.add(iid)
        tree.item(iid, open=not was_open)
        repaint_marks()
        return 'break'

    for item in tops:
        tree.insert('', 'end', iid=item['rel'], text=label(item['rel'], item['name']),
                    values=(item['count'],), open=False)
        tree.insert(item['rel'], 'end', iid=item['rel'] + ':pending',
                    text='    （展开这一项才会列出子文件夹）')

    tree.bind('<<TreeviewOpen>>', on_open)
    def on_close_event(event):
        iid = tree.focus()
        if iid:
            opened.discard(iid)
        repaint_marks()

    tree.bind('<<TreeviewClose>>', on_close_event)
    tree.bind('<Button-1>', on_click)

    def collapse_at_start():
        for item in tops:
            if tree.exists(item['rel']):
                tree.item(item['rel'], open=False)
        opened.clear()
        repaint_marks()

    window.after(120, collapse_at_start)

    def select_all():
        selected.clear()
        selected.update(item['rel'] for item in tops)
        for item in tops:
            tree.item(item['rel'], text=label(item['rel'], item['name']))
        refresh_count()

    def select_none():
        selected.clear()
        for item in tops:
            tree.item(item['rel'], text=label(item['rel'], item['name']))
        refresh_count()

    def expand_all():
        for item in tops:
            paint(item['rel'])
            tree.item(item['rel'], open=True)

    def collapse_all():
        for item in tops:
            tree.item(item['rel'], open=False)

    def apply():
        items = pe.minimal_cover(selected)
        folder_state['items'] = sorted(items)
        on_change()
        window.destroy()

    footer = ttk.Frame(window)
    footer.pack(side='bottom', fill='x', padx=18, pady=(8, 14))
    for index, (text, command) in enumerate([('全选', select_all), ('全不选', select_none),
                                             ('展开全部', expand_all), ('折叠全部', collapse_all)]):
        ttk.Button(footer, text=text, style='Small.TButton', command=command).grid(
            row=0, column=index, padx=(0, 8))
    footer.columnconfigure(4, weight=1)
    ttk.Button(footer, text='关闭', style='Small.TButton', command=window.destroy).grid(
        row=0, column=5, padx=(0, 8))
    ttk.Button(footer, text='确定', style='Accent.TButton', command=apply).grid(row=0, column=6)
    refresh_count()
    window.protocol('WM_DELETE_WINDOW', window.destroy)


def gui(smoke=False):
    session = journal.Journal(HOME, '导出窗口')
    settings = load_settings()
    state = {'busy': False, 'worker': [None], 'cancel': threading.Event()}
    root = tk.Tk()
    root.title('Obsidian → Notion 一键同步 · ' + VERSION)
    root.geometry('1000x940')
    root.minsize(820, 700)
    root.configure(background=BG)
    style = ttk.Style(root)
    try:
        style.theme_use('clam')
    except tk.TclError:
        pass
    style.configure('TFrame', background=BG)
    style.configure('Card.TFrame', background=CARD)
    style.configure('TLabel', background=BG, foreground=INK, font=('Microsoft YaHei UI', 10))
    style.configure('Card.TLabel', background=CARD, foreground=INK, font=('Microsoft YaHei UI', 10))
    style.configure('Muted.TLabel', background=BG, foreground=MUTED, font=('Microsoft YaHei UI', 9))
    style.configure('CardMuted.TLabel', background=CARD, foreground=MUTED, font=('Microsoft YaHei UI', 9))
    style.configure('Title.TLabel', background=BG, foreground=INK, font=('Microsoft YaHei UI', 19, 'bold'))
    style.configure('TButton', font=('Microsoft YaHei UI', 10), padding=(12, 8))
    style.configure('Accent.TButton', font=('Microsoft YaHei UI', 11, 'bold'), padding=(22, 11),
                    background=ACCENT, foreground='#ffffff', borderwidth=0)
    style.map('Accent.TButton', background=[('disabled', '#9db3ac'), ('active', '#1d4c42')])
    style.configure('TEntry', padding=6)
    style.configure('TCombobox', padding=4)
    style.configure('TCheckbutton', background=BG, foreground=MUTED, font=('Microsoft YaHei UI', 9))
    style.configure('Card.TCheckbutton', background=CARD, foreground=MUTED, font=('Microsoft YaHei UI', 9))

    outer = ttk.Frame(root, padding=(26, 20, 26, 16))
    outer.pack(fill='both', expand=True)

    head = ttk.Frame(outer)
    head.pack(fill='x')
    ttk.Label(head, text='让 Obsidian 笔记同步到 Notion', style='Title.TLabel').pack(side='left')
    pill = tk.Label(head, text='导出 ' + VERSION, background=ACCENT_SOFT, foreground=ACCENT,
                    font=('Microsoft YaHei UI', 9), padx=10, pady=3)
    pill.pack(side='right', pady=(8, 0))
    ttk.Label(outer, text='本地为准 · 冲突可跳过 · 不删除云端页面', style='Muted.TLabel').pack(anchor='w', pady=(2, 12))

    card = tk.Frame(outer, background=CARD, highlightbackground=LINE, highlightthickness=1)
    card.pack(fill='x')
    inner = ttk.Frame(card, style='Card.TFrame', padding=(16, 14))
    inner.pack(fill='x')
    statusrow = ttk.Frame(inner, style='Card.TFrame')
    statusrow.pack(fill='x')
    dot = tk.Label(statusrow, text='●', background=CARD, foreground=MUTED, font=('Microsoft YaHei UI', 11))
    dot.pack(side='left')
    status = tk.StringVar(value='未连接')
    tk.Label(statusrow, textvariable=status, background=CARD, foreground=INK,
             font=('Microsoft YaHei UI', 12, 'bold')).pack(side='left', padx=(6, 0))
    progress = ttk.Progressbar(statusrow, mode='indeterminate', length=110)
    detail = tk.StringVar(value='选好知识库和要同步的栏目，其余自动对应')
    ttk.Label(inner, textvariable=detail, style='CardMuted.TLabel').pack(anchor='w', pady=(4, 10))

    vaultrow = ttk.Frame(inner, style='Card.TFrame')
    vaultrow.pack(fill='x')
    ttk.Label(vaultrow, text='Obsidian 知识库', style='CardMuted.TLabel').pack(anchor='w')
    vault_var = tk.StringVar(value=settings.get('vault') or str(DEFAULT_VAULT))
    entryrow = ttk.Frame(vaultrow, style='Card.TFrame')
    entryrow.pack(fill='x', pady=(4, 10))
    vault_entry = ttk.Entry(entryrow, textvariable=vault_var)
    vault_entry.pack(side='left', fill='x', expand=True)

    def choose_vault():
        picked = filedialog.askdirectory(title='选择 Obsidian 知识库', initialdir=vault_var.get() or str(HOME))
        if picked:
            vault_var.set(picked)
            save_setting(vault=str(Path(picked)))

    ttk.Button(entryrow, text='选择文件夹', command=choose_vault).pack(side='left', padx=(8, 0))

    row2 = ttk.Frame(inner, style='Card.TFrame')
    row2.pack(fill='x')
    ttk.Label(row2, text='同步范围（按本地文件夹自动对应 Notion 栏目）', style='CardMuted.TLabel').pack(anchor='w')
    row2a = ttk.Frame(row2, style='Card.TFrame')
    row2a.pack(fill='x', pady=(4, 10))
    range_var = tk.StringVar(value='')
    folder_state = {'items': list(settings.get('folders') or [])}

    def describe_range():
        picks = pe.minimal_cover(folder_state['items'])
        if not picks:
            range_var.set('全部栏目')
        elif len(picks) <= 3:
            range_var.set('、'.join(PurePosixPath(item).name for item in picks))
        else:
            names = '、'.join(PurePosixPath(item).name for item in picks[:2])
            range_var.set('%s 等 %d 个栏目' % (names, len(picks)))

    def after_pick():
        describe_range()
        save_setting(folders=folder_state['items'])
        set_status('范围已更新', '已选 %d 个栏目' % len(pe.minimal_cover(folder_state['items'])), ACCENT)

    ttk.Entry(row2a, textvariable=range_var, state='readonly').pack(side='left', fill='x', expand=True)
    ttk.Button(row2a, text='选择文件夹', style='Small.TButton',
               command=lambda: pick_folders(root, vault_var, folder_state, after_pick)).pack(
        side='left', padx=(8, 0))

    row2b = ttk.Frame(inner, style='Card.TFrame')
    row2b.pack(fill='x')
    flat_anchor = tk.BooleanVar(value=bool(settings.get('flat', True)))
    anchor_var = tk.StringVar(value=settings.get('anchor') or 'Obsidian')
    flat_box = tk.Checkbutton(
        row2b, text='直接对到 Notion 顶层页面（已有 Notion 库时选这个，不新建外层页面）',
        variable=flat_anchor, background=CARD, foreground=INK, activebackground=CARD,
        activeforeground=INK, selectcolor=CARD, bd=0, highlightthickness=0,
        font=('Microsoft YaHei UI', 9), command=lambda: sync_anchor_state())
    flat_box.pack(anchor='w', pady=(6, 6))
    anchor_row = ttk.Frame(row2b, style='Card.TFrame')
    anchor_row.pack(fill='x', pady=(0, 10))
    ttk.Label(anchor_row, text='或放进一个顶层页：', style='CardMuted.TLabel').pack(side='left')
    anchor_entry = ttk.Entry(anchor_row, textvariable=anchor_var)
    anchor_entry.pack(side='left', fill='x', expand=True, padx=(8, 0))

    def sync_anchor_state():
        anchor_entry.configure(state='disabled' if flat_anchor.get() else 'normal')

    sync_anchor_state()
    describe_range()

    row3 = ttk.Frame(inner, style='Card.TFrame')
    row3.pack(fill='x')
    ttk.Label(row3, text='冲突时谁优先', style='CardMuted.TLabel').pack(anchor='w')
    policy_var = tk.StringVar(value=BIN_TEXT.get(settings.get('policy') or ex.DEFAULT_POLICY,
                                                 BIN_TEXT[ex.DEFAULT_POLICY]))
    policy_box = ttk.Combobox(row3, textvariable=policy_var, state='readonly',
                              values=[BIN_TEXT[k] for k in BIN_ORDER])
    policy_box.pack(fill='x', pady=(4, 0))

    buttons = ttk.Frame(outer)
    buttons.pack(fill='x', pady=(14, 0))
    connect_button = ttk.Button(buttons, text='连接 Notion')
    connect_button.pack(side='left')
    push_button = ttk.Button(buttons, text='开始上传', style='Accent.TButton')
    push_button.pack(side='left', padx=(10, 0))
    preview_button = ttk.Button(buttons, text='预览')
    preview_button.pack(side='left', padx=(10, 0))
    cancel_button = ttk.Button(buttons, text='取消', state='disabled')
    cancel_button.pack(side='right')

    summary = tk.StringVar(value='')
    summary_label = tk.Label(outer, textvariable=summary, background=ACCENT_SOFT, foreground=ACCENT,
                             anchor='w', padx=12, pady=8, font=('Microsoft YaHei UI', 10))

    tools = ttk.Frame(outer)
    tools.pack(side='bottom', fill='x', pady=(10, 0))

    logcard = tk.Frame(outer, background=CARD, highlightbackground=LINE, highlightthickness=1)
    logcard.pack(fill='both', expand=True, pady=(14, 0))
    loghead = ttk.Frame(logcard, style='Card.TFrame', padding=(12, 8, 12, 0))
    loghead.pack(fill='x')
    ttk.Label(loghead, text='运行记录', style='CardMuted.TLabel').pack(side='left')
    autoscroll = tk.BooleanVar(value=True)
    tk.Checkbutton(loghead, text='自动滚动', variable=autoscroll, background=CARD, foreground=MUTED,
                   activebackground=CARD, activeforeground=MUTED, selectcolor=CARD, bd=0,
                   highlightthickness=0, font=('Microsoft YaHei UI', 9)).pack(side='right')
    text_frame = ttk.Frame(logcard, style='Card.TFrame', padding=(12, 8, 12, 12))
    text_frame.pack(fill='both', expand=True)
    output = tk.Text(text_frame, height=18, wrap='word', background=CARD, foreground=INK, relief='flat',
                     font=('Consolas', 10), padx=6, pady=6)
    scroll = ttk.Scrollbar(text_frame, orient='vertical', command=output.yview)
    output.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y')
    output.pack(side='left', fill='both', expand=True)
    output.tag_configure('err', foreground=ERR)
    output.tag_configure('warn', foreground=WARN)
    output.tag_configure('ok', foreground=OK)

    def log(text):
        session.write(text)
        line = str(text)
        tag = 'err' if any(k in line for k in ('失败', '错误', 'Traceback', '未完成')) else (
            'warn' if any(k in line for k in ('提示', '跳过', '冲突')) else ('ok' if ('结束' in line or '已连接' in line) else ''))
        try:
            output.insert('end', line + '\n', tag)
            if autoscroll.get():
                output.see('end')
        except tk.TclError:
            pass

    def save_setting(**extra):
        merged = load_settings()
        merged.update(extra)
        try:
            save_settings(merged)
        except OSError:
            pass

    def set_status(text, detail_text=None, colour=MUTED):
        status.set(text)
        dot.configure(foreground=colour)
        if detail_text is not None:
            detail.set(detail_text)

    def busy(active):
        state['busy'] = active
        for widget in (push_button, preview_button, connect_button, policy_box):
            widget.configure(state='disabled' if active else ('readonly' if widget is policy_box else 'normal'))
        cancel_button.configure(state='normal' if active else 'disabled')
        if active:
            progress.pack(side='right')
            progress.start(14)
        else:
            try:
                progress.stop()
            except tk.TclError:
                pass
            progress.pack_forget()

    def policy_key():
        chosen = policy_var.get()
        for key in BIN_ORDER:
            if BIN_TEXT[key] == chosen:
                return key
        return ex.DEFAULT_POLICY

    events = queue.Queue()

    def start(dry_run):
        if state['busy']:
            return
        vault = vault_var.get().strip()
        folders = list(folder_state['items'])
        anchor = '' if flat_anchor.get() else (anchor_var.get().strip() or 'Obsidian')
        policy = policy_key()
        save_setting(vault=vault, folders=folders, anchor=anchor or 'Obsidian',
                     flat=bool(flat_anchor.get()), policy=policy)
        summary.set('')
        summary_label.pack_forget()
        state['cancel'] = threading.Event()
        cancel = state['cancel']
        busy(True)
        set_status('正在上传' if not dry_run else '正在生成清单',
                   '正在读取本地笔记并比对 Notion，请稍候', ACCENT)

        def worker():
            try:
                result = push_folders(vault, folders, anchor, policy,
                                      lambda t: events.put(('log', t)), cancel, dry_run=dry_run)
                events.put(('done', result, dry_run))
            except SyncError as exc:
                events.put(('fail', exc, dry_run))
            except Exception as exc:  # noqa: BLE001
                record_failure('导出' + ('预览' if dry_run else '上传'), exc, vault=vault, session=session)
                session.error(exc, '导出失败')
                events.put(('crash', exc, dry_run))

        state['worker'][0] = threading.Thread(target=worker, daemon=True)
        state['worker'][0].start()

    def poll():
        while True:
            try:
                item = events.get_nowait()
                kind, payload = item[0], item[1]
                dry_run = item[2] if len(item) > 2 else False
            except queue.Empty:
                break
            if kind == 'log':
                log(payload)
            elif kind == 'done':
                busy(False)
                counts = payload.get('counts', {})
                if dry_run:
                    conflicts = counts.get('conflict', 0)
                    pending = sum(counts.get(k, 0) for k in ('create', 'update', 'overwrite', 'copy'))
                    if conflicts:
                        set_status('清单已生成', '有 %d 篇在 Notion 改过，上传时会按「云端优先」跳过' % conflicts, WARN)
                    elif not pending:
                        set_status('清单已生成', '没有要上传的改动，Notion 与本地一致', ACCENT)
                    else:
                        set_status('清单已生成', '没有写入 Notion；确认无误后点「开始上传」', ACCENT)
                    summary.set('预览：新建 %d · 更新 %d · 覆盖 %d · 副本 %d · 冲突 %d' % (
                        counts.get('create', 0), counts.get('update', 0), counts.get('overwrite', 0),
                        counts.get('copy', 0), conflicts))
                else:
                    failed = counts.get('failed', 0)
                    conflicts = counts.get('conflict', 0)
                    if failed:
                        set_status('部分完成', '有 %d 篇没有推上去，详情见运行记录，可点「导出诊断包」' % failed, ERR)
                    elif conflicts:
                        set_status('上传完成', '有 %d 篇在 Notion 改过，已按「云端优先」跳过' % conflicts, WARN)
                    else:
                        set_status('上传完成', '云端正文已更新；本地修改过的笔记不会被联动', OK)
                    summary.set('本次：新建 %d · 更新 %d · 覆盖 %d · 副本 %d · 跳过 %d · 失败 %d' % (
                        counts.get('created', 0), counts.get('updated', 0), counts.get('overwritten', 0),
                        counts.get('copied', 0), counts.get('unchanged', 0), failed))
                summary_label.pack(fill='x', pady=(10, 0), before=logcard)
            elif kind == 'fail':
                busy(False)
                set_status('没有完成', str(payload), ERR)
                log('未完成：' + str(payload))
            elif kind == 'connected':
                set_status('已连接', 'Notion 工作区：' + str(payload) + '；可以点「预览清单」了', OK)
                log('已连接 Notion：' + str(payload))
            elif kind == 'connect-fail':
                set_status('未连接', '点「连接 Notion」在浏览器里授权（' + str(payload) + '）', ERR)
                log('连接失败：' + str(payload))
            elif kind == 'crash':
                busy(False)
                set_status('没有完成', '详情已写入日志，可点「导出诊断包」发给我', ERR)
                log('错误：%s%s' % (type(payload).__name__, permission_hint(payload)))
        root.after(120, poll)

    def connect_worker():
        try:
            client = MCP(Auth(), None)
            client.start()
            account = client.call('get-self')
            name = str((account.get('workspace') or {}).get('name') or '未知')
            events.put(('connected', name))
        except SyncError as exc:
            events.put(('connect-fail', str(exc)))
        except Exception as exc:  # noqa: BLE001
            events.put(('connect-fail', '%s: %s' % (type(exc).__name__, exc)))

    def connect_only():
        if state['busy']:
            return
        set_status('正在连接', '正在确认 Notion 账号授权…', ACCENT)
        log('正在连接 Notion…')
        threading.Thread(target=connect_worker, daemon=True).start()

    def show_log():
        window = tk.Toplevel(root)
        window.title('运行日志 · ' + VERSION)
        window.configure(background=BG)
        center(window, 960, 640)
        head2 = ttk.Frame(window, padding=(14, 12))
        head2.pack(fill='x')
        ttk.Label(head2, text='日志文件：' + str(session.path), style='Muted.TLabel').pack(side='left')
        ttk.Button(head2, text='复制全部', command=copy_all).pack(side='right')
        ttk.Button(head2, text='导出诊断包', command=lambda: export_package(window)).pack(side='right', padx=(8, 0))
        view = tk.Text(window, wrap='word', background=CARD, foreground=INK, relief='flat', font=('Consolas', 9))
        view.pack(fill='both', expand=True, padx=14, pady=(0, 12))
        view.tag_configure('err', foreground=ERR)
        view.insert('1.0', session.text())
        view.see('end')
        state['log_window'] = window

    def copy_all():
        try:
            root.clipboard_clear()
            root.clipboard_append('Obsidian→Notion ' + VERSION + '\n' + session.text())
            log('诊断信息已复制到剪贴板')
        except tk.TclError:
            pass

    def export_package(parent=None):
        default = 'Obsidian到Notion诊断-%s.zip' % ex.now_id()
        path = filedialog.asksaveasfilename(parent=parent or root, title='保存诊断包',
                                            defaultextension='.zip', initialfile=default)
        if not path:
            return
        try:
            session.export(Path(path))
            messagebox.showinfo('诊断包已保存', '已保存到：\n' + path, parent=parent or root)
        except PermissionError as exc:
            messagebox.showerror('保存失败', str(exc) + permission_hint(exc), parent=parent or root)

    connect_button.configure(command=connect_only)
    push_button.configure(command=lambda: start(False))
    preview_button.configure(command=lambda: start(True))
    cancel_button.configure(command=lambda: (state['cancel'].set(), log('已请求取消，正在收尾')))

    root.after(600, connect_only)
    ttk.Button(tools, text='查看日志', command=show_log).pack(side='left')
    ttk.Button(tools, text='导出诊断包', command=lambda: export_package(root)).pack(side='left', padx=(8, 0))
    ttk.Button(tools, text='复制诊断信息', command=copy_all).pack(side='left', padx=(8, 0))
    ttk.Label(tools, text='先点「预览清单」看清会改哪些，再上传', style='Muted.TLabel').pack(side='right')

    def close():
        log('窗口关闭')
        state['cancel'].set()
        session.close()
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', close)
    log('Obsidian → Notion 一键同步 ' + VERSION + ' 已启动')
    log('步骤：1 选知识库 · 2 选同步范围 · 3 顶层页面名 · 4 预览清单 · 5 开始上传')
    poll()
    if smoke:
        root.withdraw()
        root.after(400, root.destroy)
    root.mainloop()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Obsidian → Notion 一键同步')
    parser.add_argument('--vault', type=Path)
    parser.add_argument('--folder', default='')
    parser.add_argument('--folders', default='')
    parser.add_argument('--anchor', default='')
    parser.add_argument('--flat', action='store_true',
                        help='直接对到 Notion 工作区顶层，不新建外层页面面')
    parser.add_argument('--target')
    parser.add_argument('--policy', default=ex.DEFAULT_POLICY)
    parser.add_argument('--preview', action='store_true')
    parser.add_argument('--push', action='store_true')
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--export-diagnostics', type=Path)
    args = parser.parse_args(argv)
    if args.self_check:
        try:
            gui(smoke=True)
            report = {'ok': True, 'export_engine': True, 'gui': True, 'version': VERSION}
        except Exception as exc:  # noqa: BLE001
            report = {'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)}
        atomic_bytes(HOME / '工具检查-导出.json', json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'))
        console_log(json.dumps(report, ensure_ascii=False))
        _finish(0 if report.get('ok') else 1)
    if args.export_diagnostics:
        session = journal.Journal(HOME, '导出诊断包')
        try:
            session.export(args.export_diagnostics)
            console_log('诊断包：' + str(args.export_diagnostics))
            _finish(0)
        except Exception as exc:  # noqa: BLE001
            session.error(exc, '导出失败')
            console_log('导出失败：' + type(exc).__name__ + permission_hint(exc))
            _finish(4)
    if args.preview or args.push:
        settings = load_settings()
        vault = args.vault or Path(settings.get('vault') or DEFAULT_VAULT)
        root_folder = '' if args.folder in ('', '全部') else args.folder
        target = args.target or settings.get('target')
        picked = [item.strip() for item in (args.folders or '').split(',') if item.strip()]
        if not picked and args.folder not in ('', '全部'):
            picked = [root_folder]
        if not picked:
            picked = list(settings.get('folders') or [])
        if args.flat:
            anchor = ''
        else:
            anchor = (args.anchor or settings.get('anchor') or 'Obsidian').strip()
        session = journal.Journal(HOME, '导出命令行', vault=vault)
        code = 0
        try:
            if target and not picked:
                result = push(vault, root_folder, target, args.policy,
                              lambda t: (console_log(t), session.write(t)),
                              threading.Event(), dry_run=args.preview)
            else:
                result = push_folders(vault, picked, anchor, args.policy,
                                      lambda t: (console_log(t), session.write(t)),
                                      threading.Event(), dry_run=args.preview)
            console_log('日志：' + str(session.path))
        except SyncError as exc:
            record_failure('导出命令行', exc, vault=vault, session=session)
            session.error(exc, '导出失败')
            console_log('未完成：' + str(exc))
            console_log('日志：' + str(session.path))
            code = 2
        except Exception as exc:  # noqa: BLE001
            record_failure('导出命令行', exc, vault=vault, session=session)
            session.error(exc, '导出失败')
            console_log('错误：' + type(exc).__name__ + permission_hint(exc))
            code = 3
        finally:
            session.close()
        _finish(code)
    gui()


if __name__ == '__main__':
    main()
