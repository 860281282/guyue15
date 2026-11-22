import json,tempfile,unittest
from pathlib import Path
from sync_engine import render
from layout import clean_stage
from notion_connection import SyncError

class PathCollisionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.renderer=Path(__file__).parent/'renderer.py'
 def tearDown(self):self.tmp.cleanup()
 def page(self,pid,title,path):
  return {'id':pid,'title':title,'path':path,'url':'https://app.notion.com/p/'+pid,'metadata':{'type':'page'},'text':'<properties>'+json.dumps({'title':title},ensure_ascii=False)+'</properties>\n<content>正文</content>'}
 def test_new_same_title_keeps_old_path_and_stable_suffix(self):
  old='a'*32;new='b'*32
  snapshot={'pages':{new:self.page(new,'取快递','日程提醒 / 日程表'),old:self.page(old,'取快递','日程提醒 / 日程表')},'databases':{},'schemas':{},'scope':'test'}
  prior={'layout':'clean-v1','mapping':{old:'日程提醒/日程表/取快递.md'}}
  stage=clean_stage(render(snapshot,self.root/'first',self.renderer,prior),self.root/'clean')
  manifest=json.loads((stage/'Notion导入/_原始数据/manifest.json').read_text(encoding='utf-8'))
  mapping=manifest['mapping']
  self.assertEqual(mapping[old],prior['mapping'][old])
  self.assertEqual(mapping[new],'日程提醒/日程表/取快递（1）.md')
  self.assertTrue((stage/mapping[old]).is_file());self.assertTrue((stage/mapping[new]).is_file())
  stage2=clean_stage(render(snapshot,self.root/'second',self.renderer,{'layout':'clean-v1','mapping':mapping}),self.root/'clean2')
  self.assertEqual(mapping,json.loads((stage2/'Notion导入/_原始数据/manifest.json').read_text(encoding='utf-8'))['mapping'])
 def test_multiple_names_and_unmanaged_local_files(self):
  from sync_engine import apply_update, baseline
  a='1'*32;b='2'*32;c='3'*32
  vault=self.root/'vault';(vault/'.obsidian').mkdir(parents=True)
  folder=vault/'日程提醒/日程表';folder.mkdir(parents=True)
  (folder/'取快递.md').write_text('用户原文件',encoding='utf-8')
  (folder/'取快递（1）.md').write_text('用户另一文件',encoding='utf-8')
  snapshot={'pages':{p:self.page(p,'取快递','日程提醒 / 日程表') for p in (a,b,c)},'databases':{},'schemas':{},'scope':'test'}
  prior={'layout':'clean-v1','mapping':{},'files':{}}
  stage=clean_stage(render(snapshot,self.root/'many',self.renderer,prior,vault=vault),self.root/'many-clean')
  mapping=json.loads((stage/'Notion导入/_原始数据/manifest.json').read_text(encoding='utf-8'))['mapping']
  self.assertEqual([mapping[p] for p in (a,b,c)],['日程提醒/日程表/取快递（2）.md','日程提醒/日程表/取快递（3）.md','日程提醒/日程表/取快递（4）.md'])
  apply_update(vault,stage,'first',prior)
  self.assertEqual((folder/'取快递.md').read_text(encoding='utf-8'),'用户原文件')
  self.assertEqual((folder/'取快递（1）.md').read_text(encoding='utf-8'),'用户另一文件')
  self.assertTrue(all((vault/mapping[p]).is_file() for p in (a,b,c)))
  stage2=clean_stage(render(snapshot,self.root/'many2',self.renderer,baseline(vault),vault=vault),self.root/'many-clean2')
  self.assertEqual(mapping,json.loads((stage2/'Notion导入/_原始数据/manifest.json').read_text(encoding='utf-8'))['mapping'])
 def test_case_insensitive_paths_are_distinct(self):
  a='c'*32;b='d'*32
  snapshot={'pages':{a:self.page(a,'Notes','知识库'),b:self.page(b,'notes','知识库')},'databases':{},'schemas':{},'scope':'test'}
  stage=clean_stage(render(snapshot,self.root/'case',self.renderer,{'layout':'clean-v1','mapping':{}}),self.root/'case-clean')
  mapping=json.loads((stage/'Notion导入/_原始数据/manifest.json').read_text(encoding='utf-8'))['mapping']
  self.assertNotEqual(mapping[a].casefold(),mapping[b].casefold())
 def test_links_follow_numbered_path(self):
  a='a'*32;b='b'*32
  first=self.page(a,'取快递','日程提醒 / 日程表')
  first['text']='<properties>{"title":"取快递"}</properties>\n<content>[另一个](https://app.notion.com/p/'+b+')</content>'
  snapshot={'pages':{a:first,b:self.page(b,'取快递','日程提醒 / 日程表')},'databases':{},'schemas':{},'scope':'test'}
  stage=clean_stage(render(snapshot,self.root/'links',self.renderer,{'layout':'clean-v1','mapping':{}}),self.root/'links-clean')
  mapping=json.loads((stage/'Notion导入/_原始数据/manifest.json').read_text(encoding='utf-8'))['mapping']
  self.assertIn('[['+mapping[b][:-3]+'|另一个]]',(stage/mapping[a]).read_text(encoding='utf-8'))
 def test_ambiguous_prior_paths_are_rejected(self):
  a='e'*32;b='f'*32
  snapshot={'pages':{a:self.page(a,'Notes','知识库'),b:self.page(b,'notes','知识库')},'databases':{},'schemas':{},'scope':'test'}
  prior={'layout':'clean-v1','mapping':{a:'知识库/Notes.md',b:'知识库/notes.md'}}
  with self.assertRaises(SyncError):render(snapshot,self.root/'ambiguous',self.renderer,prior)

if __name__=='__main__':unittest.main()
