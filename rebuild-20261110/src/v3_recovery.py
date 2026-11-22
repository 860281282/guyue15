"""Experimental, fail-closed push/recovery against an injected remote adapter.

Only byte-identical simple content is supported. This module is not enabled in
production and does not implement Notion's Markdown or Block API adapters.
"""
from dataclasses import dataclass
from pathlib import Path

from notion_connection import SyncError, atomic_bytes
from sync_engine import target_path
from v3_merge import merge


@dataclass(frozen=True)
class RemoteContent:
    version: str
    content: bytes
    complete: bool = True


def _read(remote, page_id):
    result = remote.read(page_id)
    if not isinstance(result, RemoteContent) or not result.complete or not isinstance(result.content, bytes):
        raise SyncError('远端读取不完整，停止同步')
    return result


def _pending(journal, op_id, reason, observed=None):
    op = journal.get(op_id)
    try:
        local = target_path(journal.vault, op['rel_posix_path']).read_bytes()
    except OSError:
        local = None
    journal.record_conflict(op_id,reason,remote=observed,local=local)
    raise SyncError(reason)


def establish(journal, remote, *, doc_id, remote_id, rel_path):
    """Enroll only when both sides are already byte-identical and stable."""
    if journal.pending():
        raise SyncError('存在未完成事务，请先恢复或核对')
    path = target_path(journal.vault, rel_path)
    local = path.read_bytes()
    first = _read(remote, remote_id)
    second = _read(remote, remote_id)
    if (first.version != second.version or first.content != second.content or
            local != first.content or path.read_bytes() != local):
        raise SyncError('两端未确认一致，不能建立共同基线')
    journal.establish_base(doc_id=doc_id,remote_id=remote_id,rel_path=rel_path,content=local)


def execute(journal, remote, *, doc_id, remote_id, rel_path, target):
    """Prepare one existing remote page; remote.write must not retry internally."""
    if journal.pending():
        raise SyncError('存在未完成事务，请先恢复或核对')
    path = target_path(journal.vault, rel_path)
    before = path.read_bytes()
    initial = _read(remote, remote_id)
    base = journal.conn.execute('SELECT base_markdown FROM base_snapshots WHERE doc_id=?', (doc_id,)).fetchone()
    if base is None:
        raise SyncError('缺少可信共同基线，停止写入')
    op_id = journal.prepare(doc_id=doc_id,remote_id=remote_id,rel_path=rel_path,
                            remote_version=initial.version,remote_before=initial.content,
                            local_before=before,target=target)
    try:
        plan = merge(base[0],before,initial.content)
        if plan.conflict or plan.content != target:
            return _pending(journal, op_id, '两端修改无法安全合并，需人工核对', initial.content)
        current = _read(remote, remote_id)
        if current.version != initial.version or current.content != initial.content:
            return _pending(journal, op_id, '云端内容已变化，停止写入并保留双方内容', current.content)
        if path.read_bytes() != before:
            return _pending(journal, op_id, '本地内容已变化，停止写入并保留双方内容', current.content)
        journal.verify_intent(op_id)
        # No automatic retry: a timeout can happen after the server accepted it.
        if initial.content != target:
            remote.write(remote_id, initial.content, target)
        journal.transition(op_id, 'REMOTE_APPLIED')
        return recover(journal, remote, op_id)
    except Exception:
        state = journal.get(op_id)['state']
        if state not in ('RECOVERY_PENDING','COMMITTED'):
            journal.transition(op_id, 'RECOVERY_PENDING', error='写入结果不确定；重启后须先核对远端')
        raise


def recover(journal, remote, op_id):
    """Reconcile; never resend remote writes, even when the result is unknown."""
    op = journal.get(op_id)
    if op['state'] == 'COMMITTED':
        return 'committed'
    journal.verify_intent(op_id)
    observed = _read(remote, op['notion_page_id'])
    if observed.content != op['target_content']:
        return _pending(journal, op_id, '远端结果无法确认，需人工核对', observed.content)
    path = target_path(journal.vault, op['rel_posix_path'])
    try:
        local = path.read_bytes()
    except OSError:
        return _pending(journal, op_id, '本地文件不可读取，需恢复后核对', observed.content)
    if local not in (op['local_before'], op['target_content']):
        return _pending(journal, op_id, '本地文件在中断后变化，需人工合并', observed.content)
    if local != op['target_content']:
        # Check immediately before replacing. There remains an OS-level race;
        # this experimental adapter is not cleared for production use.
        if path.read_bytes() != local:
            return _pending(journal, op_id, '本地文件在写入前变化，需人工合并', observed.content)
        atomic_bytes(path, op['target_content'])
    if path.read_bytes() != op['target_content']:
        return _pending(journal, op_id, '本地写入校验失败，需人工核对', observed.content)
    final = _read(remote, op['notion_page_id'])
    if final.version != observed.version or final.content != op['target_content']:
        return _pending(journal, op_id, '核对期间云端又发生变化，需人工合并', final.content)
    state = journal.get(op_id)['state']
    if state == 'PREPARED':
        journal.transition(op_id, 'REMOTE_APPLIED')
        state = 'REMOTE_APPLIED'
    if state in ('REMOTE_APPLIED', 'RECOVERY_PENDING'):
        journal.transition(op_id, 'VERIFIED')
    journal.transition(op_id, 'COMMITTED')
    return 'committed'
