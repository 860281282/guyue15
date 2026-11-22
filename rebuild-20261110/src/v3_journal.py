"""Experimental v3 transaction journal; not wired to the production sync entry points.

Every incomplete operation must be reconciled before another operation starts.
The database and its independent pre-run backup are deliberately separate from
both the existing JSON state and real Notion writes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from notion_connection import SyncError, atomic_bytes

SCHEMA = '''
CREATE TABLE documents (doc_id TEXT PRIMARY KEY, vault_id TEXT NOT NULL,
 notion_page_id TEXT, rel_posix_path TEXT NOT NULL, tombstone_meta_json TEXT);
CREATE TABLE base_snapshots (doc_id TEXT PRIMARY KEY REFERENCES documents(doc_id),
 base_markdown BLOB NOT NULL, base_ir_json TEXT, base_hash TEXT NOT NULL, synced_at INTEGER NOT NULL);
CREATE TABLE blocks (ir_id TEXT PRIMARY KEY, doc_id TEXT REFERENCES documents(doc_id),
 notion_block_id TEXT, is_opaque INTEGER NOT NULL DEFAULT 0, meta_json TEXT);
CREATE TABLE databases (db_id TEXT PRIMARY KEY, notion_db_id TEXT, rel_folder_path TEXT, schema_json TEXT);
CREATE TABLE assets (sha256 TEXT PRIMARY KEY, rel_asset_path TEXT, byte_size INTEGER, notion_upload_id TEXT);
CREATE TABLE sync_runs (run_id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, mode TEXT NOT NULL,
 db_backup_path TEXT, warnings_json TEXT);
CREATE TABLE sync_operations (op_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES sync_runs(run_id),
 doc_id TEXT NOT NULL REFERENCES documents(doc_id), direction TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('PREPARED','REMOTE_APPLIED','VERIFIED','COMMITTED','RECOVERY_PENDING')),
 remote_ver_before TEXT NOT NULL, remote_before BLOB NOT NULL, target_content BLOB NOT NULL,
 local_before BLOB NOT NULL, local_snapshot_ref TEXT NOT NULL, observed_remote BLOB,
 conflict_history_json TEXT NOT NULL DEFAULT '[]', remote_resp_json TEXT,
 error_detail TEXT, updated_at INTEGER NOT NULL);
CREATE INDEX pending_operations ON sync_operations(state);
CREATE UNIQUE INDEX one_pending_operation ON sync_operations((1)) WHERE state != 'COMMITTED';
'''
STATES = {
    'PREPARED': {'REMOTE_APPLIED', 'RECOVERY_PENDING'},
    'REMOTE_APPLIED': {'VERIFIED', 'RECOVERY_PENDING'},
    'VERIFIED': {'COMMITTED', 'RECOVERY_PENDING'},
    'RECOVERY_PENDING': {'VERIFIED'},
    'COMMITTED': set(),
}


def _same_volume(a, b):
    return os.stat(a).st_dev == os.stat(b).st_dev


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024 * 1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


class Journal:
    def __init__(self, vault, db, connection, vault_id, backup_root):
        self.vault, self.db, self.conn = vault, db, connection
        self.vault_id, self.backup_root = vault_id, backup_root
        self.conn.row_factory = sqlite3.Row

    @classmethod
    def initialize(cls, vault, backup_root):
        vault, backup_root = Path(vault).resolve(), Path(backup_root).resolve()
        if not (vault / '.obsidian').is_dir():
            raise SyncError('不是有效的 Obsidian 知识库')
        if not backup_root.is_dir() or backup_root.is_relative_to(vault) or _same_volume(vault, backup_root):
            raise SyncError('状态备份必须放在知识库以外的独立卷')
        control = vault / '.sync'
        if not control.resolve().is_relative_to(vault):
            raise SyncError('状态目录超出知识库')
        marker, db = control / 'vault.json', control / 'sync.sqlite3'
        if marker.exists() or db.exists():
            raise SyncError('同步身份或状态库已存在，拒绝重新初始化')
        control.mkdir(parents=True, exist_ok=True)
        vault_id = str(uuid.uuid4())
        # Marker first: a crash cannot turn an existing damaged DB into a new vault.
        atomic_bytes(marker, json.dumps({'schema': 1, 'vault_id': vault_id}, ensure_ascii=False).encode('utf-8'))
        try:
            conn = sqlite3.connect(str(db), isolation_level=None)
            conn.execute('PRAGMA journal_mode=DELETE')
            conn.execute('PRAGMA synchronous=FULL')
            conn.execute('PRAGMA foreign_keys=ON')
            conn.executescript(SCHEMA)
            conn.execute('PRAGMA user_version=1')
            conn.execute('INSERT INTO sync_runs(run_id,vault_id,mode) VALUES(?,?,?)', ('@vault',vault_id,'identity'))
            conn.close()
        except Exception:
            # Deliberately keep marker so a partial initialization is detected.
            raise
        return cls.open(vault, backup_root)

    @classmethod
    def open(cls, vault, backup_root):
        vault, backup_root = Path(vault).resolve(), Path(backup_root).resolve()
        if not (vault / '.obsidian').is_dir():
            raise SyncError('知识库不可访问，停止同步')
        if not backup_root.is_dir() or backup_root.is_relative_to(vault) or _same_volume(vault, backup_root):
            raise SyncError('独立备份卷不可用，停止同步')
        marker, db = vault / '.sync/vault.json', vault / '.sync/sync.sqlite3'
        if not marker.resolve().is_relative_to(vault) or not db.resolve().is_relative_to(vault):
            raise SyncError('状态目录超出知识库')
        conn = None
        try:
            meta = json.loads(marker.read_text(encoding='utf-8'))
            if meta.get('schema') != 1:
                raise ValueError('unsupported marker')
            vault_id = str(uuid.UUID(meta['vault_id']))
            conn = sqlite3.connect(db.as_uri() + '?mode=rw', uri=True, isolation_level=None)
            conn.execute('PRAGMA foreign_keys=ON')
            if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('integrity check failed')
            row = conn.execute("SELECT vault_id FROM sync_runs WHERE run_id='@vault' AND mode='identity'").fetchone()
            if not row or row[0] != vault_id or conn.execute('PRAGMA user_version').fetchone()[0] != 1:
                raise ValueError('vault identity mismatch')
            conn.execute('PRAGMA journal_mode=DELETE')
            conn.execute('PRAGMA synchronous=FULL')
            return cls(vault, db, conn, vault_id, backup_root)
        except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
            if conn is not None:
                conn.close()
            raise SyncError('同步状态库不可用或损坏，已停止写入；请检查独立备份') from exc

    def close(self):
        self.conn.close()

    def pending(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM sync_operations WHERE state != 'COMMITTED' ORDER BY updated_at,op_id")]

    def backup(self):
        if _same_volume(self.vault, self.backup_root):
            raise SyncError('独立备份卷不可用')
        folder = self.backup_root / ('guyue-' + self.vault_id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / ('sync-' + str(time.time_ns()) + '-' + secrets.token_hex(4) + '.sqlite3')
        temporary = folder / (target.name + '.tmp')
        try:
            dest = sqlite3.connect(str(temporary))
            try:
                self.conn.backup(dest)
                if dest.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise SyncError('独立备份校验失败')
            finally:
                dest.close()
            with temporary.open('r+b') as handle:
                os.fsync(handle.fileno())
            expected = _file_hash(temporary)
            os.replace(temporary, target)
            if _file_hash(target) != expected:
                raise SyncError('独立备份写入后校验失败')
            return target
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _ensure_document(self, doc_id, remote_id, relative):
        """Check identities while the caller holds the SQLite write lock."""
        rel = relative.as_posix()
        for existing in self.conn.execute('SELECT doc_id,vault_id,notion_page_id,rel_posix_path FROM documents'):
            other_id, other_vault, other_remote, other_rel = tuple(existing)
            if other_id == doc_id:
                if (other_vault,other_remote,other_rel) != (self.vault_id,remote_id,rel):
                    raise SyncError('文档身份或路径不匹配，停止同步')
            elif (other_vault == self.vault_id and
                  (str(other_rel).casefold() == rel.casefold() or
                   str(other_remote).replace('-','').casefold() == str(remote_id).replace('-','').casefold())):
                raise SyncError('已有其他文档使用该路径或云端身份，停止同步')
        self.conn.execute('INSERT OR IGNORE INTO documents(doc_id,vault_id,notion_page_id,rel_posix_path) VALUES(?,?,?,?)',
                          (doc_id,self.vault_id,remote_id,rel))

    def _intent_path(self, op_id):
        uuid.UUID(op_id)
        return self.backup_root / ('guyue-' + self.vault_id) / (op_id + '.intent.json')

    def _write_intent(self, op_id, backup):
        op = self.get(op_id)
        payload = {'schema':1,'vault_id':self.vault_id,'op_id':op_id,'doc_id':op['doc_id'],
                   'run_id':op['run_id'],
                   'remote_id':op['notion_page_id'],'rel_path':op['rel_posix_path'],
                   'remote_version':op['remote_ver_before'],'backup':str(backup),
                   'backup_sha256':_file_hash(backup),
                   'remote_before':base64.b64encode(op['remote_before']).decode('ascii'),
                   'local_before':base64.b64encode(op['local_before']).decode('ascii'),
                   'target':base64.b64encode(op['target_content']).decode('ascii')}
        raw = json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
        envelope = json.dumps({'payload':payload,'sha256':_hash(raw)},ensure_ascii=False,sort_keys=True).encode('utf-8')
        atomic_bytes(self._intent_path(op_id),envelope)
        self.verify_intent(op_id)

    def verify_intent(self, op_id):
        try:
            envelope = json.loads(self._intent_path(op_id).read_text(encoding='utf-8'))
            payload = envelope['payload']
            raw = json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
            if envelope['sha256'] != _hash(raw) or payload['vault_id'] != self.vault_id or payload['op_id'] != op_id:
                raise ValueError('intent mismatch')
            op = self.get(op_id)
            for key, expected in (('doc_id',op['doc_id']),('run_id',op['run_id']),('remote_id',op['notion_page_id']),
                                  ('rel_path',op['rel_posix_path']),('remote_version',op['remote_ver_before']),
                                  ('remote_before',base64.b64encode(op['remote_before']).decode('ascii')),
                                  ('local_before',base64.b64encode(op['local_before']).decode('ascii')),
                                  ('target',base64.b64encode(op['target_content']).decode('ascii'))):
                if payload.get(key) != expected:
                    raise ValueError('intent does not match database')
            backup = Path(payload['backup']).resolve()
            record = self.conn.execute('SELECT db_backup_path FROM sync_runs WHERE run_id=?',(op['run_id'],)).fetchone()
            if (not record or str(backup) != record[0] or not backup.is_relative_to(self.backup_root)
                    or _file_hash(backup) != payload['backup_sha256']):
                raise ValueError('backup mismatch')
            return payload
        except (OSError,ValueError,KeyError,TypeError) as exc:
            raise SyncError('独立事务记录不可用，停止写入') from exc

    def prepare(self, *, doc_id, remote_id, rel_path, remote_version, remote_before, local_before, target):
        if self.pending():
            raise SyncError('存在未完成事务，请先恢复或核对')
        if not all(isinstance(x, bytes) for x in (remote_before, local_before, target)):
            raise TypeError('snapshots must be bytes')
        relative = Path(rel_path)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise SyncError('笔记路径不安全')
        if not (self.vault / relative).resolve().is_relative_to(self.vault):
            raise SyncError('笔记路径超出知识库')
        backup = self.backup()
        op_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            if self.pending():
                raise SyncError('存在未完成事务，请先恢复或核对')
            self._ensure_document(doc_id,remote_id,relative)
            self.conn.execute('INSERT INTO sync_runs(run_id,vault_id,mode,db_backup_path) VALUES(?,?,?,?)',
                              (run_id,self.vault_id,'experimental',str(backup)))
            self.conn.execute('''INSERT INTO sync_operations(op_id,run_id,doc_id,direction,state,remote_ver_before,
                remote_before,target_content,local_before,local_snapshot_ref,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                (op_id,run_id,doc_id,'push','PREPARED',remote_version,remote_before,target,local_before,
                 'sqlite:sync_operations/'+op_id+'/local_before',int(time.time())))
            self.conn.execute('COMMIT')
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise
        self._write_intent(op_id, backup)
        return op_id

    def establish_base(self, *, doc_id, remote_id, rel_path, content):
        """Store an explicitly verified first common base; never replace one."""
        relative = Path(rel_path)
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise SyncError('笔记路径不安全')
        if not (self.vault / relative).resolve().is_relative_to(self.vault):
            raise SyncError('笔记路径超出知识库')
        if not isinstance(content, bytes):
            raise TypeError('base must be bytes')
        self.backup()
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            if self.pending():
                raise SyncError('存在未完成事务，请先恢复或核对')
            self._ensure_document(doc_id,remote_id,relative)
            if self.conn.execute('SELECT 1 FROM base_snapshots WHERE doc_id=?', (doc_id,)).fetchone():
                raise SyncError('共同基线已存在，不能覆盖')
            self.conn.execute('INSERT INTO base_snapshots(doc_id,base_markdown,base_hash,synced_at) VALUES(?,?,?,?)',
                              (doc_id,content,_hash(content),int(time.time())))
            self.conn.execute('COMMIT')
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise

    def get(self, op_id):
        row = self.conn.execute('''SELECT o.*,d.notion_page_id,d.rel_posix_path FROM sync_operations o
                                    JOIN documents d ON d.doc_id=o.doc_id WHERE o.op_id=?''', (op_id,)).fetchone()
        if row is None:
            raise SyncError('找不到同步事务')
        return dict(row)

    def record_conflict(self, op_id, reason, *, remote=None, local=None):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            op = self.get(op_id)
            if op['state'] == 'COMMITTED':
                raise SyncError('已提交事务不能改为冲突')
            history = json.loads(op['conflict_history_json'])
            entry = {'reason':reason,'at':int(time.time()),
                     'remote':base64.b64encode(remote).decode('ascii') if remote is not None else None,
                     'local':base64.b64encode(local).decode('ascii') if local is not None else None}
            if not any(all(item.get(k)==entry[k] for k in ('reason','remote','local')) for item in history):
                history.append(entry)
            self.conn.execute('''UPDATE sync_operations SET state='RECOVERY_PENDING',error_detail=?,
                observed_remote=?,conflict_history_json=?,updated_at=? WHERE op_id=?''',
                (reason,remote if remote is not None else op['observed_remote'],
                 json.dumps(history,ensure_ascii=False),int(time.time()),op_id))
            self.conn.execute('COMMIT')
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise

    def transition(self, op_id, state, *, error=None, response=None, observed_remote=None):
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            old = self.get(op_id)
            if state not in STATES[old['state']]:
                raise SyncError('不允许的事务状态转换')
            self.conn.execute('UPDATE sync_operations SET state=?,error_detail=?,remote_resp_json=?,observed_remote=?,updated_at=? WHERE op_id=?',
                              (state,error,json.dumps(response,ensure_ascii=False) if response is not None else old['remote_resp_json'],
                               observed_remote if observed_remote is not None else old['observed_remote'],int(time.time()),op_id))
            if state == 'COMMITTED':
                data = old['target_content']
                self.conn.execute('''INSERT INTO base_snapshots(doc_id,base_markdown,base_hash,synced_at) VALUES(?,?,?,?)
                    ON CONFLICT(doc_id) DO UPDATE SET base_markdown=excluded.base_markdown,base_hash=excluded.base_hash,synced_at=excluded.synced_at''',
                    (old['doc_id'],data,_hash(data),int(time.time())))
            self.conn.execute('COMMIT')
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise


def prepare_recovery_copy(backup_root, vault_id, op_id, output_dir):
    """Build a review-only database from an independent backup and intent.

    Never replaces the live database. The recovered operation is explicitly
    pending and must be reconciled against the remote before any write.
    """
    vault_id, op_id = str(uuid.UUID(vault_id)), str(uuid.UUID(op_id))
    root, output = Path(backup_root).resolve(), Path(output_dir).resolve()
    folder = (root / ('guyue-' + vault_id)).resolve()
    if not folder.is_relative_to(root) or not output.is_dir():
        raise SyncError('恢复路径不可用')
    try:
        envelope = json.loads((folder / (op_id + '.intent.json')).read_text(encoding='utf-8'))
        data = envelope['payload']
        raw = json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
        if envelope['sha256'] != _hash(raw) or data['vault_id'] != vault_id or data['op_id'] != op_id:
            raise ValueError('intent mismatch')
        source = Path(data['backup']).resolve()
        if not source.is_relative_to(folder) or _file_hash(source) != data['backup_sha256']:
            raise ValueError('backup mismatch')
        before = base64.b64decode(data['remote_before'],validate=True)
        local = base64.b64decode(data['local_before'],validate=True)
        target = base64.b64decode(data['target'],validate=True)
        run_id = str(uuid.UUID(data['run_id']))
        relative = Path(data['rel_path'])
        if relative.is_absolute() or '..' in relative.parts or not relative.parts:
            raise ValueError('unsafe note path')
    except (OSError,ValueError,KeyError,TypeError) as exc:
        raise SyncError('独立备份或事务记录损坏，无法生成恢复副本') from exc
    dest = output / ('recovery-' + op_id + '.sqlite3')
    if dest.exists():
        raise SyncError('恢复副本已存在，拒绝覆盖')
    source_db = target_db = None
    try:
        source_db=sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
        if source_db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('backup database corrupt')
        identity=source_db.execute("SELECT vault_id FROM sync_runs WHERE run_id='@vault'").fetchone()
        if not identity or identity[0] != vault_id:
            raise ValueError('backup identity mismatch')
        target_db=sqlite3.connect(str(dest),isolation_level=None)
        source_db.backup(target_db)
        target_db.execute('PRAGMA foreign_keys=ON')
        target_db.execute('PRAGMA synchronous=FULL')
        target_db.execute('BEGIN IMMEDIATE')
        target_db.execute('INSERT OR IGNORE INTO documents(doc_id,vault_id,notion_page_id,rel_posix_path) VALUES(?,?,?,?)',
                          (data['doc_id'],vault_id,data['remote_id'],relative.as_posix()))
        actual=target_db.execute('SELECT vault_id,notion_page_id,rel_posix_path FROM documents WHERE doc_id=?',(data['doc_id'],)).fetchone()
        if actual != (vault_id,data['remote_id'],relative.as_posix()):
            raise ValueError('document identity mismatch')
        target_db.execute('INSERT INTO sync_runs(run_id,vault_id,mode,db_backup_path) VALUES(?,?,?,?)',
                          (run_id,vault_id,'recovery',str(source)))
        target_db.execute('''INSERT INTO sync_operations(op_id,run_id,doc_id,direction,state,remote_ver_before,
            remote_before,target_content,local_before,local_snapshot_ref,error_detail,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
            (op_id,run_id,data['doc_id'],'push','RECOVERY_PENDING',data['remote_version'],before,target,local,
             'sqlite:sync_operations/'+op_id+'/local_before','状态库损坏后重建，必须先核对远端',int(time.time())))
        target_db.execute('COMMIT')
        if target_db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('recovery database corrupt')
        return dest
    except (sqlite3.Error,OSError,ValueError,KeyError) as exc:
        if target_db is not None:
            target_db.close();target_db=None
        try:dest.unlink(missing_ok=True)
        except OSError:pass
        raise SyncError('无法生成可核对的恢复副本') from exc
    finally:
        if target_db is not None:target_db.close()
        if source_db is not None:source_db.close()
