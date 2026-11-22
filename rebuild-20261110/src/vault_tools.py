# -*- coding: utf-8 -*-
"""库级整理工具：快照 / 对比 / 回滚 / 批量改名 / 孤立附件归档。

只用标准库，不引入新依赖（体积不变）。所有写操作都会先在
``.同步工具\\快照`` 里留一份快照，任何一步都能退回去。
"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

VERSION = '1.0.0'

TOOL_DIR = '.同步工具'
SNAP_DIR = TOOL_DIR + '/快照'
ORPHAN_DIR = TOOL_DIR + '/孤立附件'
RENAME_LOG = TOOL_DIR + '/改名记录.json'
RENAME_KEEP = 20
MANIFEST_NAME = '.guyue-snapshot-manifest.json'
SKIP_DIRS = {'.obsidian', '.同步工具', '.trash', '.git', 'node_modules', '.stfolder'}
NOTE_SUFFIXES = {'.md', '.markdown'}
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg', '.heic', '.avif'}
ATTACH_SUFFIXES = IMAGE_SUFFIXES | {'.pdf', '.docx', '.doc', '.xlsx', '.pptx', '.zip', '.mp3',
                                    '.m4a', '.wav', '.mp4', '.mov', '.webm', '.canvas', '.excalidraw'}

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MULTI_SPACE = re.compile(r'\s+')
DATE_IN_NAME = re.compile(r'(20\d{2})[-_.年]?(\d{1,2})[-_.月]?(\d{1,2})')
FRONT_DATE = re.compile(r'^\s*(?:date|created|日期)\s*:\s*["\']?(\d{4}-\d{2}-\d{2})', re.M)
HEADING = re.compile(r'^#{1,6}\s+(.+?)\s*$', re.M)


# ---------- 基础 ----------

def is_obsidian_program(folder):
    folder = Path(folder)
    return (folder / 'Obsidian.exe').is_file() and (folder / 'resources' / 'app.asar').is_file()


def vault_files(vault):
    """普通文件；工具目录及隐藏目录不纳入整理和快照。"""
    vault = Path(vault)
    if not vault.is_dir():
        raise OSError('知识库不存在或无法访问：' + str(vault))
    out = []
    def raise_walk_error(exc):
        raise exc
    for root, dirs, files in os.walk(vault, onerror=raise_walk_error):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')
                   and not is_obsidian_program(Path(root) / d)]
        for name in files:
            if name.startswith('.'):
                continue
            path = Path(root) / name
            rel = path.relative_to(vault).as_posix()
            out.append((rel, path))
    out.sort()
    return out


def note_files(vault):
    """库里的 Markdown 笔记。"""
    return [(rel, path) for rel, path in vault_files(vault)
            if path.suffix.lower() in NOTE_SUFFIXES]


def digest(path):
    h = hashlib.sha1()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b''):
            h.update(chunk)
    return h.hexdigest()


def _stamp():
    return time.strftime('%Y%m%d-%H%M%S')


def _human(size):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return ('%d %s' % (size, unit)) if unit == 'B' else ('%.1f %s' % (size, unit))
        size /= 1024.0


# ---------- 快照 ----------

def snapshot(vault, keep=20, label='', log=None, prune_old=True):
    """保存普通文件；隐藏目录和同步工具目录不包含在内。"""
    vault = Path(vault)
    stamp = _stamp()
    target_dir = vault / SNAP_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    tag = ('-' + re.sub(r'[^\w\-]', '', label)) if label else ''
    base = '%s-%s%s' % (stamp, len(note_files(vault)), tag)
    target = target_dir / (base + '.zip')
    index = 2
    while target.exists():          # 同一秒内连打两次也要各存一份
        target = target_dir / ('%s-%d.zip' % (base, index))
        index += 1

    manifest = {'created': stamp, 'label': label, 'files': {}}
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for rel, path in vault_files(vault):
            try:
                file_hash = digest(path)
                zf.write(path, rel)
                manifest['files'][rel] = file_hash
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                manifest.setdefault('skipped', []).append(rel)
                if log:
                    log('跳过（读不了）：%s（%s）' % (rel, type(exc).__name__))
    with zipfile.ZipFile(target, 'r') as zf:
        for rel, file_hash in list(manifest['files'].items()):
            try:
                saved = hashlib.sha1()
                with zf.open(rel) as src:
                    for chunk in iter(lambda: src.read(1 << 16), b''):
                        saved.update(chunk)
                if saved.hexdigest() != file_hash:
                    raise OSError('备份期间文件发生变化')
            except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
                del manifest['files'][rel]
                manifest.setdefault('skipped', []).append(rel)
                if log:
                    log('备份校验失败：%s（%s）' % (rel, type(exc).__name__))
    with zipfile.ZipFile(target, 'a', zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=1))

    info = {'path': str(target), 'name': target.name, 'size': target.stat().st_size,
            'created': stamp, 'count': len(manifest['files']), 'skipped': manifest.get('skipped', [])}
    if prune_old and not info['skipped']:
        prune(vault, keep=keep, log=log)
    return info


def list_snapshots(vault):
    vault = Path(vault)
    folder = vault / SNAP_DIR
    if not folder.is_dir():
        return []
    out = []
    for path in sorted(folder.glob('*.zip'), reverse=True):
        item = {'path': str(path), 'name': path.name, 'size': path.stat().st_size,
                'mtime': time.strftime('%Y-%m-%d %H:%M', time.localtime(path.stat().st_mtime))}
        try:
            with zipfile.ZipFile(path) as zf:
                data = json.loads(zf.read(_manifest_name(zf)).decode('utf-8'))
            item['created'] = data.get('created', '')
            item['count'] = len(data.get('files', {}))
            item['label'] = data.get('label', '')
        except (KeyError, ValueError, OSError, zipfile.BadZipFile):
            item['created'] = ''
            item['count'] = 0
            item['label'] = '（清单损坏）'
        out.append(item)
    return out


def _manifest_name(zf):
    return MANIFEST_NAME if MANIFEST_NAME in zf.namelist() else 'manifest.json'


def read_manifest(zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read(_manifest_name(zf)).decode('utf-8'))


def diff(vault, zip_path):
    """当前库 vs 快照：新增 / 修改 / 删除（快照里有、现在没有）。"""
    vault = Path(vault)
    old = read_manifest(zip_path).get('files', {})
    now = {rel: digest(path) for rel, path in vault_files(vault)}
    added = sorted(set(now) - set(old))
    removed = sorted(set(old) - set(now))
    modified = sorted(rel for rel in (set(now) & set(old)) if now[rel] != old[rel])
    return {'added': added, 'modified': modified, 'removed': removed,
            'same': len(set(now) & set(old)) - len(modified)}


def restore(vault, zip_path, rels=None, log=None):
    """从快照写回。默认会把当前状态先存一份快照。"""
    vault = Path(vault).resolve()
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise OSError('找不到选中的快照。')
    written = []
    with zipfile.ZipFile(zip_path) as zf:
        manifest_name = _manifest_name(zf)
        manifest = json.loads(zf.read(manifest_name).decode('utf-8'))
        expected = manifest.get('files')
        if not isinstance(expected, dict) or manifest.get('skipped'):
            raise ValueError('快照不完整，已停止恢复。')
        names = [n for n in zf.namelist() if n != manifest_name and not n.endswith('/')]
        if len(names) != len(set(names)) or set(names) != set(expected):
            raise ValueError('快照文件清单不一致，已停止恢复。')
        for rel in names:
            if rels is not None and rel not in rels:
                continue
            target = (vault / rel).resolve()
            if not target.is_relative_to(vault) or target == vault:
                raise ValueError('快照包含不安全的路径，已停止恢复。')
            checksum = hashlib.sha1()
            with zf.open(rel) as src:
                for chunk in iter(lambda: src.read(1 << 16), b''):
                    checksum.update(chunk)
            if checksum.hexdigest() != expected[rel]:
                raise ValueError('快照内容校验失败，已停止恢复：' + rel)
        before = snapshot(vault, label='before-restore', log=log, prune_old=False)
        if before.get('skipped'):
            raise OSError('恢复前备份不完整，已停止恢复。')
        for rel in names:
            if rels is not None and rel not in rels:
                continue
            target = (vault / rel).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.restore-', delete=False) as dst:
                    tmp = Path(dst.name)
                    with zf.open(rel) as src:
                        shutil.copyfileobj(src, dst)
                    dst.flush()
                    os.fsync(dst.fileno())
                os.replace(tmp, target)
            finally:
                if tmp is not None and tmp.exists():
                    tmp.unlink()
            written.append(rel)
    prune(vault, log=log)
    return {'written': written, 'before': before}


def prune(vault, keep=20, log=None):
    """只留最近 keep 份快照。"""
    snaps = list_snapshots(vault)
    dropped = []
    for item in snaps[keep:]:
        try:
            Path(item['path']).unlink()
            dropped.append(item['name'])
        except OSError:
            pass
    if dropped and log:
        log('旧快照清理：%d 份' % len(dropped))
    return dropped


# ---------- 批量改名 ----------

def _first_heading(text):
    match = HEADING.search(text)
    return match.group(1).strip() if match else ''


def _read_text(path):
    try:
        return path.read_text(encoding='utf-8-sig', errors='replace')
    except OSError:
        return ''


def clean_name(stem):
    """去掉 Windows 不允许的字符、压掉多余空格、截断过长标题。"""
    name = ILLEGAL.sub(' ', stem)
    name = MULTI_SPACE.sub(' ', name).strip(' .')
    return name[:80].rstrip(' .')


def rename_plan(vault, fix_illegal=True, collapse_space=True, untitled_to_heading=True,
                date_prefix=False, replace_from='', replace_to='', prefix='', suffix='',
                files=None, folder=None):
    """返回 [(相对路径, 新文件名, 原因)]，不写盘。"""
    vault = Path(vault)
    plan = []
    for rel, path in note_files(vault):
        if files is not None and rel not in files:
            continue
        if folder is not None and not rel.startswith(folder.rstrip('/') + '/'):
            continue
        if path.suffix.lower() not in NOTE_SUFFIXES:
            continue
        raw = path.stem
        name = raw
        reasons = []

        if fix_illegal:
            fixed = ILLEGAL.sub(' ', name).strip(' .')
            if fixed != name:
                name = fixed
                reasons.append('非法字符')
        if collapse_space:
            fixed = MULTI_SPACE.sub(' ', name).strip(' .')
            if fixed != name:
                name = fixed
                reasons.append('多余空格')
        if untitled_to_heading and name in ('未命名', 'Untitled', 'untitled', '无标题', '新建笔记'):
            heading = _first_heading(_read_text(path)) or ''
            heading = clean_name(heading) if heading else ''
            if heading and heading != name:
                name = heading
                reasons.append('用标题替换')
        if date_prefix:
            text = _read_text(path)
            date = ''
            match = FRONT_DATE.search(text)
            if match:
                date = match.group(1)
            else:
                match = DATE_IN_NAME.search(name)
                if match:
                    date = '%s-%02d-%02d' % (match.group(1), int(match.group(2)), int(match.group(3)))
            if date and not re.match(r'^\d{4}-\d{2}-\d{2}', name):
                name = '%s %s' % (date, name)
                reasons.append('加日期前缀')

        if replace_from and replace_from in name:
            name = name.replace(replace_from, replace_to)
            reasons.append('替换文字')
        if prefix or suffix:
            name = prefix + name + suffix
            reasons.append('添加前后缀')

        if name != raw and (not name or ILLEGAL.search(name) or name.endswith((' ', '.')) or
                re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I)):
            raise ValueError('新文件名无效：%s' % (name or '空名称'))

        if name and name != raw:
            plan.append((rel, str(path.with_name(name + path.suffix)), '、'.join(reasons)))
    return plan


def _rename_log_path(vault):
    return Path(vault) / RENAME_LOG


def _load_rename_log(vault):
    path = _rename_log_path(vault)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_rename_log(vault, batches):
    path = _rename_log_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    trimmed = batches[-RENAME_KEEP:]
    path.write_text(json.dumps(trimmed, ensure_ascii=False, indent=1), encoding='utf-8')


def rename_batches(vault):
    """从新到旧的改名记录。"""
    return list(reversed(_load_rename_log(vault)))


def undo_rename(vault, log=None):
    """撤销最近一次改名；只撤销「目标还在、旧位置空着」的那些。"""
    vault = Path(vault)
    batches = _load_rename_log(vault)
    if not batches:
        return {'restored': [], 'skipped': [], 'batch': None}
    batch = batches[-1]
    restored, skipped = [], []
    for item in batch.get('items', []):
        old_rel, new_rel = item.get('from'), item.get('to')
        if not old_rel or not new_rel:
            continue
        src, dst = vault / new_rel, vault / old_rel
        if not src.is_file():
            skipped.append((new_rel, '文件不在了'))
            continue
        if dst.exists():
            skipped.append((new_rel, '旧位置已有同名文件'))
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            restored.append((new_rel, old_rel))
        except OSError as exc:
            skipped.append((new_rel, '%s: %s' % (type(exc).__name__, exc)))
    if restored:
        remaining = {rel for rel, _reason in skipped}
        batch['items'] = [item for item in batch.get('items', []) if item.get('to') in remaining]
        if not batch['items']:
            batches.pop()
        _save_rename_log(vault, batches)
    if log:
        log('撤销改名：恢复 %d 个，跳过 %d 个' % (len(restored), len(skipped)))
    return {'restored': restored, 'skipped': skipped, 'batch': batch}


def rename_apply(vault, plan):
    """执行改名；目标已存在就跳过，避免互相覆盖。"""
    vault = Path(vault)
    done, skipped = [], []
    for rel, new_rel, reason in plan:
        src = vault / rel
        dst = Path(new_rel)
        if not src.is_file():
            skipped.append((rel, '源文件不见了'))
            continue
        if dst.exists():
            skipped.append((rel, '目标已存在：' + dst.name))
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            done.append((rel, dst.relative_to(vault).as_posix(), reason))
        except OSError as exc:
            skipped.append((rel, '%s: %s' % (type(exc).__name__, exc)))
    if done:
        batches = _load_rename_log(vault)
        batches.append({'at': time.strftime('%Y-%m-%d %H:%M:%S'),
                        'items': [{'from': old, 'to': new} for old, new, _why in done]})
        _save_rename_log(vault, batches)
    return {'done': done, 'skipped': skipped}


# ---------- 孤立附件 ----------

LINK = re.compile(r'!?\[\[([^\]\|#]+)')
MD_IMAGE = re.compile(r'!?\[[^\]]*\]\(([^)]+)\)')


def referenced_attachments(vault):
    """扫描所有笔记里被引用的附件（相对路径 + 仅文件名两种写法都算）。"""
    vault = Path(vault)
    refs = set()
    for rel, path in note_files(vault):
        if path.suffix.lower() not in NOTE_SUFFIXES:
            continue
        text = _read_text(path)
        for match in LINK.finditer(text):
            refs.add(match.group(1).strip().replace('\\|', '|'))
        for match in MD_IMAGE.finditer(text):
            target = match.group(1).strip()
            if '://' in target:
                continue
            refs.add(target)
    names = set()
    for ref in refs:
        names.add(Path(ref).name)
        names.add(ref.replace('\\', '/').split('/')[-1])
    return refs, names


def orphan_plan(vault):
    """列出没被任何笔记引用的附件（图片/PDF/音视频等）。"""
    vault = Path(vault)
    refs, names = referenced_attachments(vault)
    orphans = []
    for rel, path in vault_files(vault):
        suffix = path.suffix.lower()
        if suffix in NOTE_SUFFIXES or suffix not in ATTACH_SUFFIXES:
            continue
        if rel in refs or path.name in names:
            continue
        orphans.append((rel, path.stat().st_size))
    return orphans


def orphan_apply(vault, plan, log=None):
    """把孤立附件移到 .同步工具\\孤立附件（不删）。"""
    vault = Path(vault)
    moved = []
    for rel, _size in plan:
        src = vault / rel
        dst = vault / ORPHAN_DIR / Path(rel).name
        index = 1
        while dst.exists():
            dst = vault / ORPHAN_DIR / ('%s-%d%s' % (Path(rel).stem, index, Path(rel).suffix))
            index += 1
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dst)
            moved.append((rel, dst.relative_to(vault).as_posix()))
        except OSError as exc:
            if log:
                log('没移走：%s（%s）' % (rel, type(exc).__name__))
    return moved


# ---------- 命令行 ----------

def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description='库整理：快照 / 对比 / 回滚 / 改名 / 孤立附件')
    parser.add_argument('--vault', required=True)
    parser.add_argument('--snapshot', action='store_true', help='打一份快照')
    parser.add_argument('--list', action='store_true', help='列出快照')
    parser.add_argument('--diff', metavar='ZIP', help='和某个快照对比')
    parser.add_argument('--restore', metavar='ZIP', help='从某个快照写回')
    parser.add_argument('--rename-plan', action='store_true', help='预览改名')
    parser.add_argument('--rename-apply', action='store_true', help='执行改名')
    parser.add_argument('--date-prefix', action='store_true', help='改名时加日期前缀')
    parser.add_argument('--rename-undo', action='store_true', help='撤销最近一次改名')
    parser.add_argument('--orphans', action='store_true', help='列出孤立附件')
    parser.add_argument('--orphans-move', action='store_true', help='把孤立附件移到归档目录')
    parser.add_argument('--keep', type=int, default=20)
    args = parser.parse_args(argv)

    def say(line):
        print(line)

    if args.snapshot:
        info = snapshot(args.vault, keep=args.keep, log=say)
        say('快照：%s（%d 个文件，%s）' % (info['name'], info['count'], _human(info['size'])))
    if args.list:
        for item in list_snapshots(args.vault):
            say('%s  %5s  %s  %s' % (item['name'], _human(item['size']),
                                     item.get('created', ''), item.get('label', '')))
    if args.diff:
        result = diff(args.vault, args.diff)
        say('新增 %d / 修改 %d / 删除 %d / 未变 %d'
            % (len(result['added']), len(result['modified']), len(result['removed']), result['same']))
        for tag, key in (('+', 'added'), ('*', 'modified'), ('-', 'removed')):
            for rel in result[key][:20]:
                say('  %s %s' % (tag, rel))
    if args.restore:
        result = restore(args.vault, args.restore, log=say)
        say('已写回 %d 个文件（回滚前已存快照：%s）' % (len(result['written']), result['before']['name']))
    if args.rename_plan or args.rename_apply:
        plan = rename_plan(args.vault, date_prefix=args.date_prefix)
        if args.rename_plan:
            for rel, new_rel, reason in plan:
                say('%s\n  -> %s   [%s]' % (rel, Path(new_rel).name, reason))
            say('共 %d 个待改名' % len(plan))
        if args.rename_apply:
            snapshot(args.vault, label='before-rename', log=say)
            result = rename_apply(args.vault, plan)
            say('改名 %d 个，跳过 %d 个' % (len(result['done']), len(result['skipped'])))
            for rel, why in result['skipped'][:10]:
                say('  跳过 %s：%s' % (rel, why))
    if args.rename_undo:
        result = undo_rename(args.vault, log=say)
        if not result['restored'] and not result['skipped']:
            say('没有改名记录可撤销')
        for new_rel, old_rel in result['restored'][:20]:
            say('  还原 %s -> %s' % (Path(new_rel).name, Path(old_rel).name))
        for rel, why in result['skipped'][:10]:
            say('  跳过 %s：%s' % (rel, why))
    if args.orphans or args.orphans_move:
        plan = orphan_plan(args.vault)
        total = sum(size for _rel, size in plan)
        for rel, size in plan[:30]:
            say('  %s  %s' % (_human(size), rel))
        say('孤立附件 %d 个，共 %s' % (len(plan), _human(total)))
        if args.orphans_move and plan:
            snapshot(args.vault, label='before-orphans', log=say)
            moved = orphan_apply(args.vault, plan, log=say)
            say('已移走 %d 个到 %s' % (len(moved), ORPHAN_DIR))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
