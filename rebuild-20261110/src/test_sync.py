import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import secrets

from notion_connection import CredentialStore, MCP, SingleRun, SyncError, http_json, unpack
from sync_engine import apply_update, baseline, collect, database_rows, digest, list_all, normalize_content, normalize_page, render, target_path, validate_page

class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.root=Path(self.temp.name);self.vault=self.root/'vault';self.stage=self.root/'stage'
        (self.vault/'.obsidian').mkdir(parents=True);self.stage.mkdir()
        self.rel='Notion导入/知识库/测试.md'
        self.old=b'---\n"imported_at": "old"\n---\n\nold body\n'
        self.new=b'---\n"imported_at": "new"\n---\n\nnew body\n'
        self.prior={'files':{self.rel:digest(self.old)},'mapping':{'a'*32:self.rel},
                    'remote_hashes':{self.rel:digest(normalize_content(self.old))}}
        dest=self.vault/self.rel;dest.parent.mkdir(parents=True);dest.write_bytes(self.old)
        self.manifest={'mapping':{'a'*32:self.rel},'pages':1,'databases':0,'workspace_id':'workspace'}
        p=self.stage/'Notion导入/_原始数据/manifest.json';p.parent.mkdir(parents=True);p.write_text(json.dumps(self.manifest))
    def tearDown(self):self.temp.cleanup()
    def put(self,rel,data):
        p=self.stage/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    def apply(self,prior=None,run='run'):
        return apply_update(self.vault,self.stage,run,prior or self.prior)
    def test_updates_and_backs_up_original(self):
        self.put(self.rel,self.new);result=self.apply()
        self.assertEqual((self.vault/self.rel).read_bytes(),self.new)
        self.assertEqual((self.vault/'Notion同步备份/run'/self.rel).read_bytes(),self.old)
        self.assertEqual(result['counts']['updated'],1)
    def test_local_edit_gets_conflict_and_survives_repeat(self):
        (self.vault/self.rel).write_bytes(b'my edit');self.put(self.rel,self.new)
        result=self.apply();self.assertEqual(result['counts']['conflicts'],1)
        self.assertEqual((self.vault/self.rel).read_bytes(),b'my edit')
        self.assertEqual((self.vault/'Notion同步冲突/run'/self.rel).read_bytes(),self.new)
        self.apply(baseline(self.vault),'run2')
        self.assertEqual((self.vault/self.rel).read_bytes(),b'my edit')
    def test_local_edit_and_unchanged_remote_needs_no_conflict(self):
        (self.vault/self.rel).write_bytes(b'my edit');self.put(self.rel,self.old.replace(b'old"',b'new"'))
        result=self.apply();self.assertEqual(result['counts']['conflicts'],0)
        self.assertEqual(result['counts']['retained'],1)
        self.assertEqual((self.vault/self.rel).read_bytes(),b'my edit')
    def test_local_deleted_note_is_not_silently_recreated(self):
        (self.vault/self.rel).unlink();self.put(self.rel,self.new)
        result=self.apply();self.assertFalse((self.vault/self.rel).exists())
        self.assertEqual(result['counts']['conflicts'],1)
    def test_remote_deleted_note_is_retained(self):
        result=self.apply();self.assertEqual(result['counts']['retained'],1)
        self.assertEqual((self.vault/self.rel).read_bytes(),self.old)
    def test_unchanged_remote_does_not_touch_note_mtime(self):
        dest=self.vault/self.rel;stamp=dest.stat().st_mtime_ns
        self.put(self.rel,self.old.replace(b'old"',b'new"'));self.apply()
        self.assertEqual(dest.stat().st_mtime_ns,stamp)
    def test_new_note_and_unmanaged_collision(self):
        self.put('Notion导入/new.md',b'new');self.put('Notion导入/user.md',b'cloud')
        (self.vault/'Notion导入/user.md').write_bytes(b'local')
        result=self.apply();self.assertEqual(result['counts']['added'],1)
        self.assertEqual((self.vault/'Notion导入/user.md').read_bytes(),b'local')
    def test_original_obsidian_settings_untouched(self):
        cfg=self.vault/'.obsidian/settings.json';cfg.write_bytes(b'{"user":"custom"}')
        self.put(self.rel,self.new);self.apply()
        self.assertEqual(cfg.read_bytes(),b'{"user":"custom"}')
    def test_mapping_preserved_when_cloud_note_is_renamed(self):
        snapshot={'pages':{'a'*32:{'id':'a'*32,'title':'新名称','url':'https://app.notion.com/p/'+'a'*32,
            'metadata':{'type':'page'},'text':'<properties>{"title":"新名称"}</properties>\n<content>新正文</content>'}},
            'databases':{},'schemas':{},'scope':'test'}
        stage=render(snapshot,self.root/'render',Path(__file__).parent/'renderer.py',self.prior)
        self.assertTrue((stage/self.rel).exists())
        self.assertIn('# 新名称',(stage/self.rel).read_text(encoding='utf-8'))
        self.assertFalse((stage/'Notion导入/新名称.md').exists())
    def test_path_traversal_is_rejected(self):
        for value in ('../outside.md','C:/outside.md','/outside.md','..\\outside.md','//server/share/a'):
            with self.subTest(value=value), self.assertRaises(SyncError):target_path(self.vault,value)
    def test_truncated_page_stops_export(self):
        with self.assertRaises(SyncError):validate_page({'id':'id','text':'body','truncated':True})
    def test_remote_page_id_can_be_derived_from_verified_url(self):
        pid='d'*32
        page={'url':'https://app.notion.com/p/'+pid,'text':'<content>正文</content>','metadata':{'type':'page'}}
        self.assertEqual(normalize_page(page,pid)['id'],pid)
        with self.assertRaises(SyncError):normalize_page(page,'e'*32)
        page['truncated']=True
        with self.assertRaises(SyncError):normalize_page(page,pid)
    def test_encrypted_authorization_roundtrip(self):
        store=CredentialStore(self.root/'secret.dat');secret={'access_token':'test-secret-not-a-real-token','refresh_token':'also-test'}
        store.save(secret);self.assertEqual(store.load(),secret)
        self.assertNotIn(b'test-secret',store.path.read_bytes())
    def test_default_authorization_store_survives_redirected_appdata(self):
        import notion_connection
        target='NotionObsidianSync/test-'+secrets.token_hex(12)
        payload={'client_id':'offline-test-client','access_token':'offline-fake-token'}
        try:
            with patch('notion_connection.CREDENTIAL_TARGET',target,create=True), \
                 patch('notion_connection.appdata',return_value=self.root/'redirected'), \
                 patch('notion_connection.atomic_bytes',side_effect=OSError(18,'cross-device rename')):
                store=CredentialStore();store.save(payload)
                self.assertEqual(CredentialStore().load(),payload)
        finally:
            if hasattr(notion_connection,'WindowsVault'):
                notion_connection.WindowsVault(target).delete()
    def test_parallel_process_lock_rejected(self):
        with SingleRun(self.root/'lock'):
            with self.assertRaises(SyncError):
                with SingleRun(self.root/'lock'):pass
    def test_default_lock_does_not_open_redirected_appdata(self):
        result=[]
        def another_worker():
            try:
                with SingleRun():result.append('entered')
            except SyncError:result.append('busy')
        with patch('pathlib.Path.open',side_effect=PermissionError(13,'encrypted legacy file')):
            with SingleRun():
                worker=threading.Thread(target=another_worker);worker.start();worker.join(timeout=2)
                self.assertEqual(result,['busy'])
            with SingleRun():pass
    def test_write_tools_are_always_blocked(self):
        client=MCP(None)
        with self.assertRaises(SyncError):client.call('update-page',id='anything')
    def test_tool_error_not_mistaken_for_content(self):
        with self.assertRaises(SyncError):unpack({'isError':True,'content':[]})
    def test_database_quota_is_identified_without_guessing_permissions(self):
        response={'isError':True,'content':[{'type':'text','text':'Allowance exhausted\n{"error":{"code":"usage_limit_reached"}}'}]}
        with self.assertRaises(SyncError) as caught:unpack(response,operation='query-data-sources')
        self.assertEqual(caught.exception.code,'usage_limit_reached')
        self.assertEqual(caught.exception.operation,'query-data-sources')
        self.assertNotIn('权限',str(caught.exception))
    def test_unauthorized_response_has_no_secret_in_error(self):
        error=urllib.error.HTTPError('https://mcp.notion.com/mcp',401,'test',{},io.BytesIO(b'access_token=private'))
        with patch('urllib.request.urlopen',side_effect=error), self.assertRaises(SyncError) as caught:
            http_json('https://mcp.notion.com/mcp')
        self.assertNotIn('private',str(caught.exception))
    def test_error_report_excludes_exception_text_and_locals(self):
        from app import record_failure
        try:raise OSError(18,'access_token=do-not-log-this-secret')
        except OSError as exc:path=record_failure('connect',exc,self.root)
        text=path.read_text(encoding='utf-8')
        self.assertNotIn('do-not-log-this-secret',text)
        self.assertEqual(json.loads(text)['errno'],18)
    def test_background_logging_handles_emoji_and_missing_console(self):
        from app import console_log
        buffer=io.BytesIO()
        stream=io.TextIOWrapper(buffer,encoding='gbk',errors='strict')
        with patch('sys.stdout',stream):console_log('正在读取：📐 高数笔记')
        self.assertIn('高数笔记',buffer.getvalue().decode('gbk'))
        with patch('sys.stdout',None):console_log('正在读取：📐 高数笔记')
    def test_sse_progress_then_result(self):
        class Response(io.BytesIO):
            headers={'Content-Type':'text/event-stream'}
        stream=Response(b'data: {"method":"progress"}\n\ndata: {"id":1,"result":{"ok":true}}\n\n')
        with patch('urllib.request.urlopen',return_value=stream):
            reply,_=http_json('https://mcp.notion.com/mcp',{})
        self.assertTrue(reply['result']['ok'])
    def test_question_bank_and_personal_notes_have_separate_stable_paths(self):
        from layout import note_path
        self.assertEqual(note_path('Notion导入/知识库/学习笔记/题库.md'),'知识库/题库.md')
        self.assertEqual(note_path('Notion导入/知识库/学习笔记/题库/课堂问题.md'),'知识库/题库/课堂问题.md')
        self.assertEqual(note_path('Notion导入/知识库/学习笔记/笔记库/自己的笔记.md'),'知识库/笔记库/自己的笔记.md')
        self.assertEqual(note_path('知识库/关于我/Agent Operating Guide.md'),'知识库/个人资料/Agent Operating Guide.md')
    def test_root_pagination_and_missing_cursor(self):
        class Fake:
            def call(inner,name,**args):
                if args.get('cursor'):return {'results':[{'url':'second'}]}
                return {'results':[{'url':'first'}],'next_cursor':'next','has_more':True}
        self.assertEqual(len(list_all(Fake(),'list-private-pages')),2)
        class Incomplete:
            def call(inner,*args,**kwargs):return {'results':[],'has_more':True}
        with self.assertRaises(SyncError):list_all(Incomplete(),'list-private-pages')
    def view_fixture(self,filtered=False):
        collection='collection://'+'c'*32;view='view://'+'d'*32
        config={'dataSourceUrl':collection,'name':'All','type':'table'}
        if filtered:config['filter']={'property':'Status','value':'Done'}
        text='<view url="'+view+'">\n'+json.dumps(config)+'\n</view>'
        return collection,view,config,{'title':'测试','text':text}
    def test_view_export_pages_without_sql_and_checks_configuration(self):
        collection,view,config,database=self.view_fixture();calls=[]
        class Fake:
            def call(inner,name,**args):
                calls.append((name,args))
                if name=='fetch':return database
                data=args['data'];self.assertEqual(data['mode'],'view');self.assertFalse(data['is_archived'])
                if data.get('start_cursor'):
                    return {'results':[{'url':'https://app.notion.com/p/'+'b'*32}],'has_more':False}
                return {'results':[{'url':'https://app.notion.com/p/'+'a'*32}],'has_more':True,'next_cursor':'page2'}
        result=database_rows(Fake(),database,collection)
        self.assertEqual(len(result['results']),2)
        self.assertEqual(len([name for name,args in calls if name=='fetch']),2)
        self.assertTrue(all(args['data']['mode']=='view' for name,args in calls if name=='query-data-sources'))
    def test_filtered_only_view_stops_instead_of_exporting_partial_data(self):
        collection,view,config,database=self.view_fixture(filtered=True)
        class Fake:
            def call(inner,*args,**kwargs):raise AssertionError('filtered view must not run')
        with self.assertRaises(SyncError):database_rows(Fake(),database,collection)
    def test_view_missing_pagination_cursor_stops_export(self):
        collection,view,config,database=self.view_fixture()
        class Fake:
            def call(inner,name,**args):
                if name=='fetch':return database
                return {'results':[],'has_more':True}
        with self.assertRaises(SyncError):database_rows(Fake(),database,collection)
    def test_view_filter_change_during_read_stops_export(self):
        collection,view,config,database=self.view_fixture();changed=copy.deepcopy(config);changed['filter']={'property':'Status'}
        class Fake:
            fetched=0
            def call(inner,name,**args):
                if name=='fetch':
                    inner.fetched+=1
                    return database if inner.fetched==1 else {'text':'<view url="'+view+'">'+json.dumps(changed)+'</view>'}
                return {'results':[],'has_more':False}
        with self.assertRaises(SyncError):database_rows(Fake(),database,collection)

if __name__=='__main__':unittest.main(verbosity=2)
