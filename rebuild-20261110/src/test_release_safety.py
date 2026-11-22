import contextlib,io,json,sys,tempfile,threading,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch
import app_out,path_engine,export_engine,write_guard,notion_connection,presets,engine
from notion_connection import SyncError
class ReleaseSafety(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.vault=Path(self.tmp.name);(self.vault/'.obsidian').mkdir();(self.vault/'notes').mkdir();(self.vault/'notes/a.md').write_text('test')
 def tearDown(self):self.tmp.cleanup()
 def test_pending_blocks_all_manual_upload_before_network(self):
  p=self.vault/'.同步工具/数据/自动上传待核对.json';p.parent.mkdir(parents=True);p.write_text(json.dumps({'paths':['notes/a.md']}))
  with patch.object(app_out,'Auth',side_effect=AssertionError('network/auth must not be reached')):
   with self.assertRaises(SyncError):app_out.push_folders(self.vault,['notes'],'','skip',lambda _:None,threading.Event(),home=self.vault)
   with self.assertRaises(SyncError):app_out.push(self.vault,'notes','a'*32,'skip',lambda _:None,threading.Event(),home=self.vault)
 def test_untracked_matching_cloud_note_is_never_overwritten(self):
  class Resolver:
   created=[]
   def __init__(self,*a,**k):pass
   def anchor(self,*a):return None,True
   def folder_page(self,*a):return 'folder'
   def find(self,*a):return 'a'*32
  note={'rel':'notes/a.md','title':'a','hash':'local','notion':'new','text':'new','warnings':[]}
  class Client:
   def call(self,*a,**kw):return {'text':'remote','page_last_edited_at':'2026-01-01T00:00:00.000Z'}
  with patch.object(path_engine,'Resolver',Resolver),patch.object(export_engine,'scan',return_value=[note]):
   for policy in ('skip','overwrite','copy'):
    result=path_engine.plan_paths(Client(),self.vault,['notes'],{},policy=policy,anchor_name='')
    self.assertEqual(result['actions'][0]['kind'],'conflict')
 def test_incomplete_unknown_cloud_cannot_authorize_overwrite(self):
  with self.assertRaises(SyncError):export_engine.remote_guard({'text':'before <unknown url="x"/> after'})
 def test_duplicate_matching_pages_are_not_silently_chosen(self):
  obj=path_engine.Resolver(None);obj.children=lambda parent:[('a','same'),('b','same')]
  with self.assertRaises(SyncError):obj.find('folder','same')
 def test_only_owner_of_pending_marker_may_upload(self):
  p=write_guard.pending_path(self.vault);p.parent.mkdir(parents=True)
  p.write_text(json.dumps({'paths':['notes/a.md'],'operation':'expected'}))
  with self.assertRaises(SyncError):write_guard.check(self.vault)
  with self.assertRaises(SyncError):write_guard.check(self.vault,'other')
  write_guard.check(self.vault,'expected')
  p.unlink()
  with self.assertRaises(SyncError):write_guard.check(self.vault,'expected')
 def test_disconnected_vault_is_not_treated_as_no_pending(self):
  with self.assertRaises(SyncError):write_guard.check(self.vault/'missing')
 def test_529_write_is_uncertain_and_not_retried(self):
  error=urllib.error.HTTPError('https://mcp.notion.com/mcp',529,'overload',{'Retry-After':'0'},io.BytesIO(b'{}'))
  with patch('notion_connection.urllib.request.urlopen',side_effect=error) as call:
   with self.assertRaises(SyncError) as caught:notion_connection.http_json('https://mcp.notion.com/mcp',{},retry_uncertain=False)
   self.assertEqual(caught.exception.code,'ambiguous_write');self.assertEqual(call.call_count,1)
 def test_new_persona_defaults_to_non_overwriting(self):
  self.assertEqual(presets.PRESETS[presets.PERSONA_OBSIDIAN]['policy'],'skip')
 def test_remote_edit_one_second_after_last_sync_is_conflict(self):
  class Resolver:
   created=[]
   def __init__(self,*a,**k):pass
   def anchor(self,*a):return None,True
   def folder_page(self,*a):return 'folder'
   def find(self,*a):return 'a'*32
  note={'rel':'notes/a.md','title':'a','hash':'new','notion':'new','text':'new','warnings':[]}
  class Client:
   def call(self,*a,**kw):return {'text':'remote','page_last_edited_at':'2026-01-01T00:00:01.000Z'}
  state={'files':{'notes/a.md':{'page':'a'*32,'local':'old','remote_edited':'2026-01-01T00:00:00.000Z'}}}
  with patch.object(path_engine,'Resolver',Resolver),patch.object(export_engine,'scan',return_value=[note]):
   result=path_engine.plan_paths(Client(),self.vault,['notes'],state,policy='skip',anchor_name='')
   self.assertEqual(result['actions'][0]['kind'],'conflict')
 def test_manual_uncertain_write_leaves_marker_and_blocks_retry(self):
  home=self.vault/'home';home.mkdir()
  class Client:
   def __init__(self,*a,**kw):pass
   def start(self):pass
   def call(self,*a,**kw):return {'workspace':{'id':'test-workspace'}}
  action={'rel':'notes/a.md','kind':'update'}
  result={'counts':{},'actions':[action]}
  with patch.object(app_out,'SingleRun',return_value=contextlib.nullcontext()),patch.object(app_out,'MCP',Client),patch.object(app_out,'Auth',return_value=None),patch.object(app_out.pe,'plan_paths',return_value=result),patch.object(app_out.pe,'apply_paths',side_effect=SyncError('unknown',code='ambiguous_write')):
   with self.assertRaises(SyncError):app_out.push_folders(self.vault,['notes'],'','skip',lambda _:None,threading.Event(),home=home)
  marker=write_guard.pending_path(self.vault);self.assertTrue(marker.is_file())
  self.assertEqual(json.loads(marker.read_text(encoding='utf-8'))['paths'],['notes/a.md'])
  with patch.object(app_out,'Auth',side_effect=AssertionError('retry must stop before network')):
   with self.assertRaises(SyncError):app_out.push_folders(self.vault,['notes'],'','skip',lambda _:None,threading.Event(),home=home)
 def test_one_click_stops_before_pull_when_upload_unresolved(self):
  marker=write_guard.pending_path(self.vault);marker.parent.mkdir(parents=True);marker.write_text(json.dumps({'paths':['notes/a.md']}))
  with patch('app.sync',side_effect=AssertionError('must stop before pull')):
   with self.assertRaises(SyncError):engine.cycle(self.vault,'obsidian',{},lambda _:None,threading.Event())
 def test_legacy_single_page_upload_completes_without_missing_variables(self):
  home=self.vault/'home';home.mkdir()
  class Client:
   def __init__(self,*a,**kw):pass
   def start(self):pass
   def call(self,name,**kw):return {'workspace':{'id':'test-workspace'}} if name=='get-self' else {'text':''}
  result={'counts':{},'actions':[]}
  applied={'counts':{},'records':[]}
  with patch.object(app_out,'SingleRun',return_value=contextlib.nullcontext()),patch.object(app_out,'MCP',Client),patch.object(app_out,'Auth',return_value=None),patch.object(export_engine,'plan',return_value=result),patch.object(export_engine,'apply',return_value=applied):
   app_out.push(self.vault,'notes','a'*32,'skip',lambda _:None,threading.Event(),home=home)
  self.assertFalse(write_guard.pending_path(self.vault).exists())
 def test_children_never_authorize_whole_page_replacement(self):
  with self.assertRaises(SyncError):export_engine.remote_guard({'text':'<content>body<page url="https://example"/>child</content>'})
 def test_notions_partial_write_warning_is_uncertain(self):
  with self.assertRaises(SyncError) as caught:export_engine.check_write_result({'warnings':[{'message':'partial'}]})
  self.assertEqual(caught.exception.code,'ambiguous_write')
 def test_written_version_is_taken_from_server_not_local_clock(self):
  class Client:
   def call(self,*a,**kw):return {'text':'<content>ok</content>','page_last_edited_at':'2026-01-01T00:00:01.000Z'}
  self.assertEqual(export_engine.confirm_written_version(Client(),'a'*32,'ok'),'2026-01-01T00:00:01.000Z')
 def test_failed_postwrite_read_is_uncertain(self):
  class Client:
   def call(self,*a,**kw):raise OSError('network unavailable')
  with self.assertRaises(SyncError) as caught:export_engine.confirm_written_version(Client(),'a'*32,'ok')
  self.assertEqual(caught.exception.code,'ambiguous_write')
 def test_root_scan_includes_root_but_not_subfolder_notes(self):
  (self.vault/'root.md').write_text('root',encoding='utf-8')
  self.assertEqual([x['rel'] for x in export_engine.scan(self.vault,direct_only=True)],['root.md'])
 def test_default_upload_includes_root_folder_in_plan(self):
  (self.vault/'root.md').write_text('root',encoding='utf-8');home=self.vault/'home';home.mkdir();seen=[]
  class Client:
   def __init__(self,*a,**kw):pass
   def start(self):pass
   def call(self,*a,**kw):return {'workspace':{'id':'test-workspace'}}
  def plan(client,vault,folders,*a,**kw):seen.extend(folders);return {'counts':{},'actions':[]}
  with patch.object(app_out,'SingleRun',return_value=contextlib.nullcontext()),patch.object(app_out,'MCP',Client),patch.object(app_out,'Auth',return_value=None),patch.object(app_out.pe,'plan_paths',side_effect=plan):
   app_out.push_folders(self.vault,[],'','skip',lambda _:None,threading.Event(),home=home,dry_run=True)
  self.assertIn('',seen);self.assertIn('notes',seen)
 def test_root_note_enters_plan(self):
  class Resolver:
   created=[]
   def __init__(self,*a,**kw):pass
   def anchor(self,*a):return None,True
   def folder_page(self,*a):return None
   def find_note(self,*a):return None
  (self.vault/'root.md').write_text('root',encoding='utf-8')
  with patch.object(path_engine,'Resolver',Resolver):
   result=path_engine.plan_paths(object(),self.vault,[''],{},policy='skip',anchor_name='')
  self.assertEqual([a['rel'] for a in result['actions']],['root.md'])
 def test_targeted_update_uses_unique_anchor_and_preserves_context(self):
  remote={'text':'<content>前文\n\n旧文字\n\n末尾</content>'}
  edit=export_engine.targeted_update(remote,{'notion':'前文\n\n新文字\n\n末尾'})
  before='前文\n\n旧文字\n\n末尾'
  self.assertEqual(before.count(edit['old_str']),1)
  self.assertEqual(before.replace(edit['old_str'],edit['new_str'],1),'前文\n\n新文字\n\n末尾')
 def test_targeted_update_stops_for_whole_page_or_unknown_structure(self):
  for old,new in [('<content>abc</content>','xyz'),('<content><callout>旧</callout></content>','新'),('no wrapper','新')]:
   with self.subTest(old=old),self.assertRaises(SyncError):export_engine.targeted_update({'text':old},{'notion':new})
 def test_existing_page_uses_update_content_not_full_replace(self):
  class Resolver:
   def __init__(self,*a,**kw):self.created=[]
   def anchor(self,*a):return None,True
   def folder_page(self,*a):return None
  class Client:
   def __init__(self):self.body='前文\n\n旧文字\n\n末尾';self.writes=[]
   def call(self,*a,**kw):return {'text':'<content>'+self.body+'</content>','page_last_edited_at':'2026-01-01T00:00:01.000Z'}
   def write(self,name,**kw):
    self.writes.append(kw);edit=kw['content_updates'][0];self.body=self.body.replace(edit['old_str'],edit['new_str'],1);return {}
  client=Client();remote=client.call();action={'kind':'update','rel':'notes/a.md','title':'a','page':'a'*32,'folder':'','hash':'h','notion':'前文\n\n新文字\n\n末尾','urls':{},'_remote_guard':export_engine.remote_guard(remote)}
  with patch.object(path_engine,'Resolver',Resolver):result=path_engine.apply_paths(client,self.vault,{'anchor':'','actions':[action]}, {},'test',write_ids=False)
  self.assertEqual(result['counts']['updated'],1);self.assertEqual(client.writes[0]['command'],'update_content');self.assertEqual(client.body,'前文\n\n新文字\n\n末尾')
 def test_targeted_anchor_rejects_overlapping_matches(self):
  before='aaaaXtail';after='aaXtail'
  edit=export_engine.targeted_update({'text':'<content>'+before+'</content>'},{'notion':after})
  first=before.find(edit['old_str'])
  self.assertGreaterEqual(first,0);self.assertEqual(before.find(edit['old_str'],first+1),-1)
  self.assertEqual(before.replace(edit['old_str'],edit['new_str'],1),after)
 def test_previously_bound_missing_page_is_not_recreated(self):
  class Resolver:
   created=[]
   def __init__(self,*a,**kw):pass
   def anchor(self,*a):return None,True
   def folder_page(self,*a):return 'folder'
   def find(self,*a):return None
   def find_note(self,*a):return None
  note={'rel':'notes/a.md','title':'a','hash':'new','notion':'new','text':'new','warnings':[]}
  state={'files':{'notes/a.md':{'page':'a'*32,'local':'old'}}}
  with patch.object(path_engine,'Resolver',Resolver),patch.object(export_engine,'scan',return_value=[note]):
   result=path_engine.plan_paths(object(),self.vault,['notes'],state,policy='skip',anchor_name='')
  self.assertEqual(result['actions'][0]['kind'],'conflict')
 def test_previously_bound_trashed_page_is_not_recreated(self):
  class Resolver:
   created=[]
   def __init__(self,*a,**kw):pass
   def anchor(self,*a):return None,True
   def folder_page(self,*a):return 'folder'
   def find(self,*a):return 'a'*32
  class Client:
   def call(self,*a,**kw):return {'text':'<page deleted="true">','page_last_edited_at':'2026-01-01T00:00:00.000Z'}
  note={'rel':'notes/a.md','title':'a','hash':'new','notion':'new','text':'new','warnings':[]}
  state={'files':{'notes/a.md':{'page':'a'*32,'local':'old'}}}
  with patch.object(path_engine,'Resolver',Resolver),patch.object(export_engine,'scan',return_value=[note]):
   result=path_engine.plan_paths(Client(),self.vault,['notes'],state,policy='skip',anchor_name='')
  self.assertEqual(result['actions'][0]['kind'],'conflict')
if __name__=='__main__':unittest.main()
