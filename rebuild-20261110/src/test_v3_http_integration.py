import http.server,json,os,socket,tempfile,threading,unittest,urllib.request
from pathlib import Path
from notion_connection import SyncError
from v3_journal import Journal
from v3_notion_markdown import NotionMarkdown
from v3_notion_transport import NotionREST
from v3_recovery import establish,execute,recover
from test_v3_recovery import BACKUP_PARENT,VAULT_PARENT

PID='a'*32
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def reply(self,obj):
  data=json.dumps(obj,ensure_ascii=False).encode()
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
 def do_GET(self):
  if self.path.startswith('/v1/pages/'+PID+'/markdown'):
   if 'markdown_version=v2' not in self.path:self.send_error(400);return
   self.reply({'object':'page_markdown','id':PID,'markdown':self.server.text,'truncated':False,'unknown_block_ids':[]})
  else:self.reply({'id':PID,'last_edited_time':str(self.server.version)})
 def do_PATCH(self):
  body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  if body.get('type')!='update_content' or body.get('markdown_version')!='v2':self.send_error(400);return
  edit=body['update_content']['content_updates'][0]
  if self.server.text.count(edit['old_str'])!=1:self.send_error(400);return
  self.server.text=self.server.text.replace(edit['old_str'],edit['new_str'],1)
  self.server.version+=1;self.server.writes+=1
  if self.server.drop_response:
   self.server.drop_response=False
   self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
  self.reply({'object':'page_markdown','id':PID,'markdown':self.server.text,'truncated':False,'unknown_block_ids':[]})

class HttpIntegrationTests(unittest.TestCase):
 def test_lost_http_response_reconciles_without_duplicate_write(self):
  if os.stat(VAULT_PARENT).st_dev == os.stat(BACKUP_PARENT).st_dev:self.skipTest('independent test volume unavailable')
  server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
  server.text='第一段。\n\n这里是旧内容。\n\n最后一段。\n';server.version=1;server.writes=0;server.drop_response=True
  thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  try:
   def local_open(request,timeout):
    url='http://127.0.0.1:%d%s' % (server.server_port,request.selector)
    clone=urllib.request.Request(url,data=request.data,headers=dict(request.header_items()),method=request.get_method())
    return urllib.request.urlopen(clone,timeout=timeout)
   adapter=NotionMarkdown(NotionREST(lambda:'fake-test-token',opener=local_open))
   with tempfile.TemporaryDirectory(dir=VAULT_PARENT) as v, tempfile.TemporaryDirectory(dir=BACKUP_PARENT) as b:
    vault=Path(v);(vault/'.obsidian').mkdir();note=vault/'note.md';note.write_bytes(server.text.encode())
    journal=Journal.initialize(vault,b)
    try:
     establish(journal,adapter,doc_id='doc',remote_id=PID,rel_path='note.md')
     target=server.text.replace('旧内容','新内容').encode();note.write_bytes(target)
     with self.assertRaises(SyncError):execute(journal,adapter,doc_id='doc',remote_id=PID,rel_path='note.md',target=target)
     self.assertEqual(server.writes,1)
     op=journal.pending()[0];self.assertEqual(op['state'],'RECOVERY_PENDING')
     self.assertEqual(recover(journal,adapter,op['op_id']),'committed')
     self.assertEqual(server.writes,1);self.assertEqual(note.read_bytes(),target)
     self.assertEqual(journal.conn.execute('select base_markdown from base_snapshots').fetchone()[0],target)
    finally:journal.close()
  finally:
   server.shutdown();thread.join(timeout=5);server.server_close()

if __name__=='__main__':unittest.main()
