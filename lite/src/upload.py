"""按本地路径预览、上传和核对 Notion 页面。"""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path, PurePosixPath
import re
import secrets
import urllib.parse

import notion_connection as nc
from notion_connection import Auth, MCP, SingleRun, SyncError, appdata, atomic_bytes
import export_engine as ex
import write_guard
import path_engine as pe

HOME = appdata()
POLICY_TEXT = {
    'skip': '云端优先：Notion 改过的页跳过',
    'overwrite': '本地优先：经核对后尝试覆盖冲突页',
    'copy': '两边都留：冲突时新建本地副本页',
}


def now_id():
    return datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3)


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


def push_folders(vault, folders, anchor, policy, log, cancel, home=HOME, dry_run=False,
                 attrs=False, expected_preview=None, expected_workspace=None):
    """Mirror the chosen folders into Notion by path. No page link, no notion_id."""

    vault = Path(vault).resolve()
    if not ex.vault_ready(vault):
        raise SyncError('请选择已有的 Obsidian 知识库文件夹，里面应有 .obsidian 文件夹')
    anchor = (anchor or '').strip()
    picked = pe.minimal_cover(folders)
    if '' in folders:
        picked.insert(0, '')
    if not picked:
        picked = [item['rel'] for item in pe.list_folders(vault)]
        if any(p.is_file() and not p.name.startswith('.') and p.suffix.lower() == '.md' for p in vault.iterdir()):
            picked.insert(0, '')
    if not picked:
        raise SyncError('知识库里还没有可同步的文件夹')
    if not dry_run:
        write_guard.check(vault)
        if not expected_preview:
            raise SyncError('请先预览并确认上传清单。')
    with SingleRun():
        if not dry_run:
            write_guard.check(vault)
        state = ex.load_state(vault)
        state['policy'] = policy
        client = MCP(Auth(), cancel)
        client.start()
        account = client.call('get-self')
        if expected_workspace and (account.get('workspace') or {}).get('id') != expected_workspace:
            raise SyncError('连接的工作区已变化，请重新连接并预览。')
        workspace = ex.bind_workspace(vault, state, account)
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
                               anchor_name=anchor, log=log, cancel=cancel,
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
        run_id = now_id()
        log('开始上传，请勿关闭窗口')

        hook = None
        if attrs:
            import attributes as ab

            def hook(client_, page_id, rel, _vault=vault, _log=log):
                ab.push(client_, page_id, Path(_vault) / rel, _log)
        operation = write_guard.begin(vault, [a['rel'] for a in result['actions'] if a.get('rel')])
        ex.write_json(ex.state_path(vault), state)
        applied = pe.apply_paths(client, vault, result, state, run_id, log=log,
                                 cancel=cancel, image_hook=attachments.upload, attrs_hook=hook,
                                 report=write_guard.reporter(vault, operation))
        if applied['counts'].get('failed') or applied['counts'].get('conflict'):
            raise SyncError('部分笔记上传失败或存在冲突，请先核对后再继续')
        write_guard.finish(vault, operation)
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
