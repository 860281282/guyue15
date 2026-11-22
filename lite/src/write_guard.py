"""Persist upload intent and per-note outcomes until the batch is resolved."""
import json
import os
import secrets
import time
from pathlib import Path
from notion_connection import SyncError, atomic_bytes


STATUS_TEXT = {'started': '写入结果待核对', 'confirmed': '正文已核对', 'unchanged': '无需更新',
               'conflict': '有冲突', 'kept-remote': '保留云端', 'failed': '上传失败',
               'pending': '尚未尝试', 'unknown': '旧记录，进度未知'}

def pending_path(vault):
    return Path(vault) / '.同步工具' / '数据' / '自动上传待核对.json'

def load(vault):
    if not (Path(vault) / '.obsidian').is_dir():
        raise SyncError('知识库不可访问，已停止上传')
    path = pending_path(vault)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise SyncError('上传待核对记录无法读取，已停止上传') from exc
    if (not isinstance(data, dict) or not isinstance(data.get('paths'), list)
            or not all(isinstance(path, str) for path in data['paths'])):
        raise SyncError('上传待核对记录损坏，已停止上传')
    entries = data.get('entries', {})
    if not isinstance(entries, dict) or any(
            not isinstance(entry, dict) or not isinstance(entry.get('status'), str)
            or entry['status'] not in STATUS_TEXT
            or not isinstance(entry.get('page'), (str, type(None)))
            or type(entry.get('image_count', 0)) is not int or entry.get('image_count', 0) < 0
            or not isinstance(entry.get('body_hash'), (str, type(None))) for entry in entries.values()):
        raise SyncError('上传待核对记录损坏，已停止上传')
    return data


def check(vault, operation=None):
    data = load(vault)
    if data is None:
        if operation is not None:
            raise SyncError('上传待核对记录丢失，已停止上传')
        return None
    if not operation or data.get('operation') != operation:
        raise SyncError('上次上传结果尚未核对，请先检查 Notion 和本地笔记，确认后再上传')
    return data


def begin(vault, paths):
    check(vault)
    path = pending_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    operation = secrets.token_hex(16)
    try:
        with path.open('x', encoding='utf-8') as handle:
            json.dump({'paths': list(paths), 'created': time.time(), 'operation': operation, 'entries': {}},
                      handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise SyncError('已有上传待核对记录，已停止新的上传') from None
    return operation


def reporter(vault, operation):
    def report(path, status, page, body_hash=None, image_count=None):
        data = check(vault, operation)
        if path not in data['paths']:
            raise SyncError('当前笔记不在上传待核对记录中，已停止上传')
        entries = data.setdefault('entries', {})
        entry = {'status': status, 'page': page}
        previous = entries.get(path, {})
        body_hash = body_hash or previous.get('body_hash')
        if body_hash:
            entry['body_hash'] = body_hash
        image_count = previous.get('image_count', 0) if image_count is None else image_count
        if image_count:
            entry['image_count'] = image_count
        entries[path] = entry
        atomic_bytes(pending_path(vault), json.dumps(data, ensure_ascii=False).encode('utf-8'))
    return report


def dismiss(vault, expected):
    if load(vault) != expected:
        raise SyncError('待核对记录已变化，请重新打开核对')
    pending_path(vault).unlink()


def finish(vault, operation):
    check(vault, operation)
    pending_path(vault).unlink()
