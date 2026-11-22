import tempfile,unittest
from pathlib import Path
from notion_connection import SyncError
from v3_notion_markdown import NotionMarkdown,_minimal_update
from v3_journal import Journal
from v3_recovery import establish,execute,recover
from test_v3_recovery import BACKUP_PARENT,VAULT_PARENT
import os

PID='a'*32
class Fake:
 def __init__(self):self.requests=[];self.version='v1';self.text='第一段文字\n\n这里是旧内容。\n\n尾段。';self.warning=False;self.truncated=False;self.unknown=[]
 def __call__(self,method,path,headers=None,params=None,body=None):
  self.requests.append((method,path,headers,params,body))
  if method=='GET' and path.endswith('/markdown'):
   return {'object':'page_markdown','id':PID,'markdown':self.text,'truncated':self.truncated,'unknown_block_ids':self.unknown}
  if method=='GET':return {'id':PID,'last_edited_time':self.version}
  edit=body['update_content']['content_updates'][0]
  self.text=self.text.replace(edit['old_str'],edit['new_str'],1);self.version='v2'
  result={'object':'page_markdown','id':PID,'markdown':self.text,'truncated':False,'unknown_block_ids':[]}
  if self.warning:result['warnings']=[{'message':'partial'}]
  return result

class MarkdownAdapterTests(unittest.TestCase):
 def test_read_and_targeted_write_use_v2(self):
  t=Fake();a=NotionMarkdown(t);before=a.read(PID)
  self.assertTrue(before.complete)
  self.assertEqual(t.requests[1][3],{'markdown_version':'v2'})
  desired=t.text.replace('旧内容','新内容');a.write(PID,before.content,desired.encode())
  request=t.requests[-1];self.assertEqual(request[0],'PATCH')
  self.assertEqual(request[4]['markdown_version'],'v2');self.assertEqual(request[4]['type'],'update_content')
  self.assertNotIn('allow_deleting_content',request[4]['update_content'])
  self.assertEqual(t.text,desired)
 def test_incomplete_or_unknown_data_stops(self):
  for attr,value in [('truncated',True),('unknown',['x'])]:
   with self.subTest(attr=attr):
    t=Fake();setattr(t,attr,value)
    with self.assertRaises(SyncError):NotionMarkdown(t).read(PID)
    self.assertFalse(any(x[0]=='PATCH' for x in t.requests))
 def test_warning_after_partial_write_is_failure_not_retry(self):
  t=Fake();t.warning=True;a=NotionMarkdown(t);before=a.read(PID)
  with self.assertRaises(SyncError):a.write(PID,before.content,t.text.replace('旧内容','新内容').encode())
  self.assertEqual(sum(x[0]=='PATCH' for x in t.requests),1)
 def test_private_and_unsupported_syntax_not_written(self):
  t=Fake();a=NotionMarkdown(t)
  for source in ('abc %%秘密%% def','abc [[双链]] def','abc <unknown/> def'):
   with self.subTest(source=source):
    with self.assertRaises(SyncError):a.write(PID,source.encode(),(source+'new').encode())
  self.assertFalse(t.requests)
 def test_anchors_are_unique_and_full_page_is_rejected(self):
  old,new=_minimal_update('A repeat B repeat C','A repeat B changed C')
  self.assertEqual('A repeat B repeat C'.count(old),1)
  self.assertEqual('A repeat B repeat C'.replace(old,new,1),'A repeat B changed C')
  with self.assertRaises(SyncError):_minimal_update('a','b')
 def test_version_change_during_read_fails(self):
  class Changed(Fake):
   def __call__(self,method,path,**kwargs):
    result=super().__call__(method,path,**kwargs)
    if path.endswith('/markdown'):self.version='v2'
    return result
  with self.assertRaises(SyncError):NotionMarkdown(Changed()).read(PID)
 def test_partial_write_warning_is_reconciled_without_repeating(self):
  if os.stat(VAULT_PARENT).st_dev == os.stat(BACKUP_PARENT).st_dev:self.skipTest('independent test volume unavailable')
  with tempfile.TemporaryDirectory(dir=VAULT_PARENT) as d, tempfile.TemporaryDirectory(dir=BACKUP_PARENT) as b:
   vault=Path(d);(vault/'.obsidian').mkdir();t=Fake();note=vault/'note.md';note.write_bytes(t.text.encode())
   j=Journal.initialize(vault,b)
   try:
    adapter=NotionMarkdown(t);establish(j,adapter,doc_id='d',remote_id=PID,rel_path='note.md')
    target=t.text.replace('旧内容','新内容').encode();note.write_bytes(target);t.warning=True
    with self.assertRaises(SyncError):execute(j,adapter,doc_id='d',remote_id=PID,rel_path='note.md',target=target)
    op=j.pending()[0];self.assertEqual(op['state'],'RECOVERY_PENDING')
    self.assertEqual(recover(j,adapter,op['op_id']),'committed')
    self.assertEqual(sum(x[0]=='PATCH' for x in t.requests),1)
    self.assertEqual(j.conn.execute('select base_markdown from base_snapshots').fetchone()[0],target)
   finally:j.close()

if __name__=='__main__':unittest.main()
