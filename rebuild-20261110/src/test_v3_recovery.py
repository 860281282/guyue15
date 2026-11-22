import base64, contextlib, json, os, shutil, sqlite3, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
from notion_connection import SyncError
from v3_journal import Journal,prepare_recovery_copy
from v3_recovery import RemoteContent, establish, execute, recover

BACKUP_PARENT = Path(os.environ.get('GUYUE_TEST_BACKUP_ROOT',tempfile.gettempdir()))
VAULT_PARENT = Path(os.environ.get('GUYUE_TEST_VAULT_ROOT',str(Path(__file__).resolve().parent)))

class DiskRemote:
 def __init__(self, path, crash=False):self.path=Path(path);self.crash=crash
 def read(self, page):
  x=json.loads(self.path.read_text(encoding='utf-8'))
  return RemoteContent(x['version'],x['content'].encode(),True)
 def write(self,page,before,target):
  x=json.loads(self.path.read_text(encoding='utf-8'))
  assert x['content'].encode()==before
  x.update(content=target.decode(),version='2',writes=x['writes']+1)
  self.path.write_text(json.dumps(x),encoding='utf-8')
  if self.crash:
   self.path.with_suffix('.ready').write_text('ready',encoding='utf-8')
   while True:time.sleep(1)

def crash_worker(vault,backup,remote):
 journal=Journal.open(vault,backup)
 execute(journal,DiskRemote(remote,crash=True),doc_id='doc1',remote_id='page1',rel_path='note.md',target=b'new')

def recorded_worker(vault,backup,remote):
 journal=Journal.open(vault,backup)
 op=journal.prepare(doc_id='doc1',remote_id='page1',rel_path='note.md',remote_version='1',remote_before=b'base',local_before=b'base',target=b'new')
 DiskRemote(remote).write('page1',b'base',b'new')
 journal.transition(op,'REMOTE_APPLIED')
 Path(remote).with_suffix('.ready').write_text('ready',encoding='utf-8')
 while True:time.sleep(1)

class TransactionTests(unittest.TestCase):
 def setUp(self):
  if os.stat(VAULT_PARENT).st_dev == os.stat(BACKUP_PARENT).st_dev:
   self.skipTest('set GUYUE_TEST_BACKUP_ROOT to an accessible independent volume')
  self.temp=tempfile.TemporaryDirectory(dir=VAULT_PARENT,prefix='v3-vault-')
  self.backup=tempfile.TemporaryDirectory(dir=BACKUP_PARENT,prefix='v3-backup-')
  self.vault=Path(self.temp.name);(self.vault/'.obsidian').mkdir();(self.vault/'note.md').write_bytes(b'base')
  self.remote=self.vault/'fake-remote.json';self.remote.write_text(json.dumps({'version':'1','content':'base','writes':0}),encoding='utf-8')
  self.j=Journal.initialize(self.vault,self.backup.name)
  establish(self.j,DiskRemote(self.remote),doc_id='doc1',remote_id='page1',rel_path='note.md')
 def tearDown(self):
  if self.j is not None:self.j.close()
  self.temp.cleanup();self.backup.cleanup()
 def test_crash_after_remote_applied_recovers_without_resending(self):
  (self.vault/'note.md').write_bytes(b'new')
  self.j.close();self.j=None
  self.kill_worker('--crash')
  self.j=Journal.open(self.vault,self.backup.name)
  op=self.j.pending()[0];self.assertEqual(op['state'],'PREPARED')
  self.assertEqual(recover(self.j,DiskRemote(self.remote),op['op_id']),'committed')
  self.assertEqual(json.loads(self.remote.read_text())['writes'],1)
  self.assertEqual((self.vault/'note.md').read_bytes(),b'new')
  self.assertEqual(self.j.conn.execute('select base_markdown from base_snapshots').fetchone()[0],b'new')
  self.assertEqual(self.j.pending(),[])
 def kill_worker(self,mode):
  marker=self.remote.with_suffix('.ready')
  proc=subprocess.Popen([sys.executable,__file__,mode,str(self.vault),self.backup.name,str(self.remote)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
  try:
   deadline=time.monotonic()+20
   while not marker.exists() and proc.poll() is None and time.monotonic()<deadline:time.sleep(.02)
   if not marker.exists():
    proc.kill();_,err=proc.communicate(timeout=10)
    self.fail('worker did not reach crash point: '+err.decode('utf-8','replace'))
   proc.kill();proc.communicate(timeout=10)
  finally:
   if proc.poll() is None:proc.kill();proc.wait(timeout=10)
 def test_recorded_remote_applied_survives_external_kill(self):
  self.j.close();self.j=None
  self.kill_worker('--recorded')
  self.j=Journal.open(self.vault,self.backup.name)
  op=self.j.pending()[0];self.assertEqual(op['state'],'REMOTE_APPLIED')
  self.assertEqual(recover(self.j,DiskRemote(self.remote),op['op_id']),'committed')
  self.assertEqual(json.loads(self.remote.read_text())['writes'],1)
 def test_remote_concurrency_stops_write_and_keeps_both(self):
  (self.vault/'note.md').write_bytes(b'local edit')
  class Concurrent(DiskRemote):
   def __init__(self,path):super().__init__(path);self.reads=0
   def read(inner,page):
    inner.reads+=1
    if inner.reads==2:
     inner.path.write_text(json.dumps({'version':'2','content':'remote edit','writes':0}),encoding='utf-8')
    return super().read(page)
  with self.assertRaises(SyncError):execute(self.j,Concurrent(self.remote),doc_id='doc1',remote_id='page1',rel_path='note.md',target=b'local edit')
  op=self.j.pending()[0]
  self.assertEqual(op['state'],'RECOVERY_PENDING');self.assertEqual(op['observed_remote'],b'remote edit')
  self.assertEqual(op['target_content'],b'local edit');self.assertEqual((self.vault/'note.md').read_bytes(),b'local edit')
  history=json.loads(op['conflict_history_json']);self.assertEqual(len(history),1)
  self.assertEqual(base64.b64decode(history[0]['remote']),b'remote edit')
  self.assertEqual(base64.b64decode(history[0]['local']),b'local edit')
  self.assertEqual(json.loads(self.remote.read_text())['writes'],0)
  self.assertEqual(self.j.conn.execute('select base_markdown from base_snapshots').fetchone()[0],b'base')
 def test_corrupt_database_fails_closed_and_independent_backup_valid(self):
  op=self.j.prepare(doc_id='doc1',remote_id='page1',rel_path='note.md',remote_version='1',remote_before=b'base',local_before=b'base',target=b'new')
  intent=self.j.verify_intent(op)
  vault_id=self.j.vault_id
  self.assertEqual(intent['op_id'],op);self.assertEqual(intent['rel_path'],'note.md')
  self.assertTrue(self.j._intent_path(op).is_file())
  backup=list(Path(self.backup.name).rglob('*.sqlite3'))
  self.assertGreaterEqual(len(backup),2)
  with contextlib.closing(sqlite3.connect(max(backup,key=lambda p:p.stat().st_mtime_ns))) as copy:
   self.assertEqual(copy.execute('pragma integrity_check').fetchone()[0],'ok')
   self.assertEqual(copy.execute('select base_markdown from base_snapshots').fetchone()[0],b'base')
  self.j.close();self.j=None
  db=self.vault/'.sync/sync.sqlite3';db.write_bytes(b'corrupt experiment database')
  with self.assertRaises(SyncError):Journal.open(self.vault,self.backup.name)
  self.assertEqual(db.read_bytes(),b'corrupt experiment database')
  self.assertTrue(list(Path(self.backup.name).rglob('*.intent.json')))
  with tempfile.TemporaryDirectory(dir=VAULT_PARENT) as out:
   review=prepare_recovery_copy(self.backup.name,vault_id,op,out)
   with contextlib.closing(sqlite3.connect(review)) as conn:
    self.assertEqual(conn.execute('select state from sync_operations').fetchone()[0],'RECOVERY_PENDING')
    self.assertEqual(conn.execute('select base_markdown from base_snapshots').fetchone()[0],b'base')
   self.assertEqual(db.read_bytes(),b'corrupt experiment database')
   with self.assertRaises(SyncError):prepare_recovery_copy(self.backup.name,vault_id,op,out)
 def test_backup_plus_intent_recovers_in_separate_test_vault(self):
  op=self.j.prepare(doc_id='doc1',remote_id='page1',rel_path='note.md',remote_version='1',remote_before=b'base',local_before=b'base',target=b'new')
  identity=self.j.vault_id;DiskRemote(self.remote).write('page1',b'base',b'new')
  self.j.close();self.j=None
  db=self.vault/'.sync/sync.sqlite3';db.write_bytes(b'simulated-disk-corruption')
  with self.assertRaises(SyncError):Journal.open(self.vault,self.backup.name)
  with tempfile.TemporaryDirectory(dir=VAULT_PARENT) as out:
   destination=Path(out);(destination/'.obsidian').mkdir();(destination/'.sync').mkdir()
   (destination/'note.md').write_bytes(b'base')
   shutil.copy2(self.vault/'.sync/vault.json',destination/'.sync/vault.json')
   review=prepare_recovery_copy(self.backup.name,identity,op,destination)
   shutil.copy2(review,destination/'.sync/sync.sqlite3')
   reconstructed=Journal.open(destination,self.backup.name)
   try:
    self.assertEqual(reconstructed.get(op)['state'],'RECOVERY_PENDING')
    self.assertEqual(recover(reconstructed,DiskRemote(self.remote),op),'committed')
    self.assertEqual((destination/'note.md').read_bytes(),b'new')
    self.assertEqual(json.loads(self.remote.read_text())['writes'],1)
   finally:reconstructed.close()
  self.assertEqual(db.read_bytes(),b'simulated-disk-corruption')
 def test_pending_blocks_new_transaction(self):
  args=dict(doc_id='doc1',remote_id='page1',rel_path='note.md',remote_version='1',remote_before=b'base',local_before=b'base',target=b'new')
  self.j.prepare(**args)
  with self.assertRaises(SyncError):self.j.prepare(**args)
 def test_backup_on_same_volume_rejected(self):
  with self.assertRaises(SyncError):Journal.open(self.vault,self.vault.parent)
 def test_unknown_base_cannot_push(self):
  with self.assertRaises(SyncError):execute(self.j,DiskRemote(self.remote),doc_id='unknown',remote_id='page1',rel_path='note.md',target=b'new')
  self.assertEqual(json.loads(self.remote.read_text())['writes'],0)
 def test_document_identity_and_path_cannot_be_shared(self):
  args=dict(remote_version='1',remote_before=b'base',local_before=b'base',target=b'new')
  with self.assertRaises(SyncError):self.j.prepare(doc_id='other',remote_id='page1',rel_path='different.md',**args)
  with self.assertRaises(SyncError):self.j.prepare(doc_id='other',remote_id='page2',rel_path='NOTE.md',**args)
  self.assertEqual(self.j.pending(),[])
 def test_divergent_content_cannot_become_first_base(self):
  (self.vault/'other.md').write_bytes(b'untrusted local')
  with self.assertRaises(SyncError):establish(self.j,DiskRemote(self.remote),doc_id='other',remote_id='page1',rel_path='other.md')
  self.assertIsNone(self.j.conn.execute('select 1 from base_snapshots where doc_id=?',('other',)).fetchone())
 def test_recovery_does_not_write_if_remote_result_ambiguous(self):
  op=self.j.prepare(doc_id='doc1',remote_id='page1',rel_path='note.md',remote_version='1',remote_before=b'base',local_before=b'base',target=b'new')
  with self.assertRaises(SyncError):recover(self.j,DiskRemote(self.remote),op)
  self.assertEqual(self.j.get(op)['state'],'RECOVERY_PENDING')
  self.assertEqual(json.loads(self.remote.read_text())['writes'],0)
  self.assertEqual((self.vault/'note.md').read_bytes(),b'base')
 def test_recovery_preserves_local_edit(self):
  op=self.j.prepare(doc_id='doc1',remote_id='page1',rel_path='note.md',remote_version='1',remote_before=b'base',local_before=b'base',target=b'new')
  self.remote.write_text(json.dumps({'version':'2','content':'new','writes':1}),encoding='utf-8')
  (self.vault/'note.md').write_bytes(b'later local edit')
  with self.assertRaises(SyncError):recover(self.j,DiskRemote(self.remote),op)
  self.assertEqual((self.vault/'note.md').read_bytes(),b'later local edit')
  self.assertEqual(self.j.get(op)['state'],'RECOVERY_PENDING')
 def test_missing_independent_intent_blocks_recovery(self):
  op=self.j.prepare(doc_id='doc1',remote_id='page1',rel_path='note.md',remote_version='1',remote_before=b'base',local_before=b'base',target=b'new')
  self.remote.write_text(json.dumps({'version':'2','content':'new','writes':1}),encoding='utf-8')
  self.j._intent_path(op).unlink()
  with self.assertRaises(SyncError):recover(self.j,DiskRemote(self.remote),op)
  self.assertEqual((self.vault/'note.md').read_bytes(),b'base')
  self.assertEqual(self.j.get(op)['state'],'PREPARED')
 def test_modified_backup_blocks_recovery(self):
  op=self.j.prepare(doc_id='doc1',remote_id='page1',rel_path='note.md',remote_version='1',remote_before=b'base',local_before=b'base',target=b'new')
  intent=self.j.verify_intent(op)
  Path(intent['backup']).write_bytes(b'damaged backup')
  with self.assertRaises(SyncError):recover(self.j,DiskRemote(self.remote),op)
  self.assertEqual((self.vault/'note.md').read_bytes(),b'base')
 def test_intent_write_failure_never_sends_remote_write(self):
  (self.vault/'note.md').write_bytes(b'new')
  with patch('v3_journal.atomic_bytes',side_effect=OSError('simulated backup-volume disconnect')):
   with self.assertRaises(OSError):execute(self.j,DiskRemote(self.remote),doc_id='doc1',remote_id='page1',rel_path='note.md',target=b'new')
  self.assertEqual(json.loads(self.remote.read_text())['writes'],0)
  op=self.j.pending()[0];self.assertEqual(op['state'],'PREPARED')
  with self.assertRaises(SyncError):recover(self.j,DiskRemote(self.remote),op['op_id'])
  self.assertEqual((self.vault/'note.md').read_bytes(),b'new')
 def test_change_after_write_is_left_pending(self):
  class ChangesAfterWrite(DiskRemote):
   def write(inner,page,before,target):
    super().write(page,before,target)
    inner.path.write_text(json.dumps({'version':'3','content':'someone else edited','writes':1}),encoding='utf-8')
  (self.vault/'note.md').write_bytes(b'new')
  with self.assertRaises(SyncError):execute(self.j,ChangesAfterWrite(self.remote),doc_id='doc1',remote_id='page1',rel_path='note.md',target=b'new')
  op=self.j.pending()[0]
  self.assertEqual(op['state'],'RECOVERY_PENDING')
  self.assertEqual(op['observed_remote'],b'someone else edited')
  self.assertEqual((self.vault/'note.md').read_bytes(),b'new')
  self.assertEqual(self.j.conn.execute('select base_markdown from base_snapshots').fetchone()[0],b'base')
 def test_disjoint_edits_merge_and_commit(self):
  note=self.vault/'long.md';note.write_bytes(b'a\nb\nc\n')
  remote=self.vault/'long-remote.json';remote.write_text(json.dumps({'version':'1','content':'a\nb\nc\n','writes':0}),encoding='utf-8')
  client=DiskRemote(remote);establish(self.j,client,doc_id='doc2',remote_id='page2',rel_path='long.md')
  note.write_bytes(b'A\nb\nc\n')
  remote.write_text(json.dumps({'version':'2','content':'a\nb\nC\n','writes':0}),encoding='utf-8')
  merged=b'A\nb\nC\n'
  self.assertEqual(execute(self.j,client,doc_id='doc2',remote_id='page2',rel_path='long.md',target=merged),'committed')
  self.assertEqual(note.read_bytes(),merged)
  self.assertEqual(json.loads(remote.read_text())['content'].encode(),merged)
  self.assertEqual(self.j.conn.execute('select base_markdown from base_snapshots where doc_id=?',('doc2',)).fetchone()[0],merged)
 def test_remote_only_change_does_not_write_remote(self):
  remote=DiskRemote(self.remote)
  self.remote.write_text(json.dumps({'version':'2','content':'remote edit','writes':0}),encoding='utf-8')
  self.assertEqual(execute(self.j,remote,doc_id='doc1',remote_id='page1',rel_path='note.md',target=b'remote edit'),'committed')
  self.assertEqual((self.vault/'note.md').read_bytes(),b'remote edit')
  self.assertEqual(json.loads(self.remote.read_text())['writes'],0)

if __name__=='__main__':
 if len(sys.argv)>1 and sys.argv[1]=='--crash':crash_worker(*sys.argv[2:])
 elif len(sys.argv)>1 and sys.argv[1]=='--recorded':recorded_worker(*sys.argv[2:])
 else:unittest.main()
