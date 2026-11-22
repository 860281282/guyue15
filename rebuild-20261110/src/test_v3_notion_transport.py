import io,json,unittest,urllib.error
from v3_notion_transport import NotionREST
from notion_connection import SyncError

class Reply(io.BytesIO):
 def __enter__(self):return self
 def __exit__(self,*args):self.close()

class TransportTests(unittest.TestCase):
 def test_headers_and_query(self):
  calls=[]
  def open_(request,timeout):
   calls.append(request);return Reply(json.dumps({'ok':True}).encode())
  x=NotionREST(lambda:'fake-token',opener=open_)
  self.assertEqual(x('GET','/v1/pages/test/markdown',headers={'Notion-Version':'2026-03-11'},params={'markdown_version':'v2'}),{'ok':True})
  self.assertEqual(calls[0].get_header('Authorization'),'Bearer fake-token')
  self.assertIn('markdown_version=v2',calls[0].full_url)
 def test_get_rate_limit_respects_retry_after(self):
  calls=[];waits=[]
  def open_(request,timeout):
   calls.append(request)
   if len(calls)==1:raise urllib.error.HTTPError(request.full_url,429,'limit',{'Retry-After':'3'},None)
   return Reply(b'{}')
  self.assertEqual(NotionREST(lambda:'fake',opener=open_,sleep=waits.append)('GET','/v1/pages/a'),{})
  self.assertEqual(len(calls),2);self.assertEqual(waits,[3.0])
 def test_patch_is_never_retried_and_token_not_in_error(self):
  calls=[]
  def open_(request,timeout):
   calls.append(request);raise urllib.error.HTTPError(request.full_url,529,'fake-secret',{'Retry-After':'0'},io.BytesIO(b'fake-secret'))
  with self.assertRaises(SyncError) as caught:NotionREST(lambda:'fake-secret',opener=open_)('PATCH','/v1/pages/a',body={})
  self.assertEqual(len(calls),1);self.assertNotIn('fake-secret',str(caught.exception))
 def test_unsafe_path_and_headers_rejected(self):
  x=NotionREST(lambda:'fake',opener=lambda *a,**k:None)
  for path in ('https://evil.test/v1/pages/x','/v1/pages/../x','/v1/pages/a?x=1'):
   with self.subTest(path=path),self.assertRaises(SyncError):x('GET',path)
  with self.assertRaises(SyncError):x('GET','/v1/pages/a',headers={'Authorization':'bad'})
 def test_unreasonable_retry_after_fails_closed(self):
  calls=[]
  def open_(request,timeout):
   calls.append(request);raise urllib.error.HTTPError(request.full_url,429,'limit',{'Retry-After':'9999'},None)
  with self.assertRaises(SyncError):NotionREST(lambda:'fake',opener=open_,sleep=lambda _:self.fail('must not sleep'))('GET','/v1/pages/a')
  self.assertEqual(len(calls),1)

if __name__=='__main__':unittest.main()
