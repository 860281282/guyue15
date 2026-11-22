"""变更探测：对本地笔记比较时间、大小与内容指纹。"""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path, PurePosixPath

import export_engine as ex
from vault_tools import is_obsidian_program
from notion_connection import SyncError, atomic_bytes

STATE = "变更索引.json"
SKIP = set(getattr(ex, 'SKIP_DIRS', ())) | {'.obsidian', '.同步工具', '.git', '.trash'}


def index_path(vault):
    return Path(vault) / '.同步工具' / '数据' / STATE


def snapshot(vault, folders=()):
    """{相对路径: [mtime, 字节数, SHA-256]}，只扫 md。"""
    vault = Path(vault).resolve()
    if not (vault / '.obsidian').is_dir():
        raise SyncError('知识库不可访问，已停止检查改动')
    out = {}
    roots = []
    for folder in (folders or ['']):
        base = (vault / str(folder).strip('/')).resolve() if str(folder).strip('/') else vault
        if not base.is_relative_to(vault) or not base.is_dir():
            raise SyncError('同步范围不可访问，已停止检查改动')
        if is_obsidian_program(base):
            raise SyncError('同步范围不能是 Obsidian 程序目录')
        roots.append(base)
    seen = set()
    for base in roots:
        def onerror(exc):
            raise exc
        for directory, dirs, files in os.walk(base, onerror=onerror):
            dirs[:] = [name for name in dirs if name not in SKIP and not name.startswith('.')
                       and not is_obsidian_program(Path(directory) / name)]
            for name in files:
                if not name.lower().endswith('.md') or name.startswith('.'):
                    continue
                path = Path(directory) / name
                if not path.resolve().is_relative_to(vault):
                    raise SyncError('笔记路径超出知识库，已停止检查改动')
                rel = path.relative_to(vault).as_posix()
                if rel in seen:
                    continue
                seen.add(rel)
                try:
                    before = path.stat()
                    hasher = hashlib.sha256()
                    with path.open('rb') as stream:
                        for chunk in iter(lambda: stream.read(1 << 16), b''):
                            hasher.update(chunk)
                    after = path.stat()
                except OSError as exc:
                    raise SyncError('无法读取笔记，已停止检查改动：' + rel) from exc
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise SyncError('笔记正在修改，请稍后重试：' + rel)
                out[rel] = [round(after.st_mtime, 3), after.st_size, hasher.hexdigest()]
    return out


def load(path):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise SyncError('改动索引无法读取，已停止自动上传；请检查磁盘和备份') from exc
    files = data.get('files') if isinstance(data, dict) else None
    if not isinstance(files, dict):
        raise SyncError('改动索引格式异常，已停止自动上传；请检查备份')
    return files


def save(path, files):
    path = Path(path)
    atomic_bytes(path, json.dumps({'files': files}, ensure_ascii=False).encode('utf-8'))


def changed(previous, current):
    """比两份快照，返回 (新增或改过的, 被删掉的)。"""
    changed_rels, gone = [], []
    for rel, meta in current.items():
        old = previous.get(rel)
        if not old or old != meta:
            changed_rels.append(rel)
    for rel in previous:
        if rel not in current:
            gone.append(rel)
    return sorted(changed_rels), sorted(gone)


def successful_paths(result):
    """Only acknowledge files confirmed uploaded or already unchanged."""
    success = {'create', 'update', 'overwrite', 'copy'}
    paths = {r['path'] for r in result.get('records', [])
             if r.get('action') in success and r.get('path')}
    paths.update(a['rel'] for a in result.get('actions', [])
                 if a.get('kind') == 'unchanged' and a.get('rel'))
    return paths


def mark(files, rels, current):
    for rel in rels:
        if rel in current:
            files[rel] = current[rel]
    return files


def forget(files, rels):
    for rel in rels:
        files.pop(rel, None)
    return files
