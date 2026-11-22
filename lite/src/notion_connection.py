"""Official Notion MCP desktop OAuth and read-only transport."""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import http.server
import json
import mimetypes
import os
import re
from pathlib import Path
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

ORIGIN = 'https://mcp.notion.com'
REDIRECT = 'http://127.0.0.1:47653/callback'
CREDENTIAL_TARGET = 'Huang.GuyueSyncLite/OAuth/v1'
READ_TOOLS = {'get-self','list-private-pages','list-shared-pages','get-teams','fetch',
              'query-data-sources','get-file-download-urls','search'}
WRITE_TOOLS = {'create-pages','update-page','create-file-upload'}

class SyncError(Exception):
    def __init__(self,message,*,code=None,operation=None):
        super().__init__(message)
        self.code=code;self.operation=operation

def appdata():
    return Path(os.environ['LOCALAPPDATA']) / 'GuyueSyncLite'

def atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name+'.'+secrets.token_hex(6)+'.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

class Blob(ctypes.Structure):
    _fields_ = [('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_ubyte))]

def protect(data, decrypt=False):
    buf = ctypes.create_string_buffer(data)
    inp = Blob(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_ubyte)))
    out = Blob()
    crypt = ctypes.WinDLL('crypt32',use_last_error=True)
    kernel = ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,
                   ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(inp),None,None,None,None,1,ctypes.byref(out)):
        raise SyncError('无法读取本机加密授权，请重新连接 Notion')
    try:
        return ctypes.string_at(out.pbData,out.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(out.pbData,ctypes.c_void_p))

class WindowsCredential(ctypes.Structure):
    _fields_=[('Flags',wintypes.DWORD),('Type',wintypes.DWORD),('TargetName',wintypes.LPWSTR),
              ('Comment',wintypes.LPWSTR),('LastWritten',wintypes.FILETIME),
              ('CredentialBlobSize',wintypes.DWORD),('CredentialBlob',ctypes.POINTER(ctypes.c_ubyte)),
              ('Persist',wintypes.DWORD),('AttributeCount',wintypes.DWORD),('Attributes',ctypes.c_void_p),
              ('TargetAlias',wintypes.LPWSTR),('UserName',wintypes.LPWSTR)]

class WindowsVault:
    """Use the OS credential store; redirected AppData renames can return EXDEV."""
    def __init__(self,target):
        self.target=target
        self.api=ctypes.WinDLL('advapi32',use_last_error=True)
        self.api.CredWriteW.argtypes=[ctypes.POINTER(WindowsCredential),wintypes.DWORD]
        self.api.CredWriteW.restype=wintypes.BOOL
        self.api.CredReadW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.POINTER(ctypes.POINTER(WindowsCredential))]
        self.api.CredReadW.restype=wintypes.BOOL
        self.api.CredDeleteW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD]
        self.api.CredDeleteW.restype=wintypes.BOOL
        self.api.CredFree.argtypes=[ctypes.c_void_p];self.api.CredFree.restype=None
    def read(self):
        pointer=ctypes.POINTER(WindowsCredential)()
        if not self.api.CredReadW(self.target,1,0,ctypes.byref(pointer)):
            error=ctypes.get_last_error()
            if error==1168:return None
            raise SyncError('Windows 无法读取本工具的登录凭据，错误码 '+str(error))
        try:return ctypes.string_at(pointer.contents.CredentialBlob,pointer.contents.CredentialBlobSize)
        finally:self.api.CredFree(pointer)
    def write(self,data):
        if len(data)>2560:raise SyncError('登录配置超出当前凭据容量，请更新工具')
        buffer=ctypes.create_string_buffer(data)
        credential=WindowsCredential()
        credential.Type=1;credential.TargetName=self.target;credential.Persist=2
        credential.UserName='Notion OAuth';credential.Comment='Notion to Obsidian sync authorization'
        credential.CredentialBlobSize=len(data)
        credential.CredentialBlob=ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte))
        if not self.api.CredWriteW(ctypes.byref(credential),0):
            raise SyncError('Windows 无法保存本工具的登录凭据，错误码 '+str(ctypes.get_last_error()))
    def delete(self):
        if not self.api.CredDeleteW(self.target,1,0) and ctypes.get_last_error()!=1168:
            raise SyncError('无法移除本工具的测试凭据')

class CredentialStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else appdata()/'authorization.dat'
        self.vault = None if path is not None else WindowsVault(CREDENTIAL_TARGET)
    def load(self):
        if self.vault:
            value=self.vault.read()
            if value is not None:return json.loads(protect(value,True))
        if not self.path.exists():
            return {}
        return json.loads(protect(self.path.read_bytes(),True))
    def save(self, data):
        encrypted=protect(json.dumps(data).encode('utf-8'))
        if self.vault:self.vault.write(encrypted)
        else:atomic_bytes(self.path, encrypted)

class SingleRun:
    def __init__(self, path=None):
        self.path = Path(path) if path else appdata()/'sync.lock'
        self.native = path is None
    def __enter__(self):
        if self.native:
            self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            self.kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
            self.kernel.CreateMutexW.restype=wintypes.HANDLE
            self.kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
            self.kernel.WaitForSingleObject.restype=wintypes.DWORD
            self.kernel.ReleaseMutex.argtypes=[wintypes.HANDLE];self.kernel.ReleaseMutex.restype=wintypes.BOOL
            self.kernel.CloseHandle.argtypes=[wintypes.HANDLE];self.kernel.CloseHandle.restype=wintypes.BOOL
            user=os.environ.get('USERDOMAIN','')+'|'+os.environ.get('USERNAME','')
            name='Local\\Huang.NotionObsidianSync.'+hashlib.sha256(user.encode('utf-8')).hexdigest()[:20]
            self.handle=self.kernel.CreateMutexW(None,False,name)
            if not self.handle:raise SyncError('Windows 无法建立同步互斥锁，错误码 '+str(ctypes.get_last_error()))
            wait=self.kernel.WaitForSingleObject(self.handle,0)
            if wait not in (0,0x80):
                self.kernel.CloseHandle(self.handle)
                raise SyncError('另一个窗口正在连接或同步，请等待它完成')
            return self
        import msvcrt
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.stream = self.path.open('a+b')
        self.stream.seek(0)
        if self.path.stat().st_size == 0:
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            msvcrt.locking(self.stream.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            self.stream.close()
            raise SyncError('另一个窗口正在连接或同步，请等待它完成')
        return self
    def __exit__(self,*args):
        if self.native:
            self.kernel.ReleaseMutex(self.handle);self.kernel.CloseHandle(self.handle);return
        import msvcrt
        self.stream.seek(0)
        msvcrt.locking(self.stream.fileno(),msvcrt.LK_UNLCK,1)
        self.stream.close()

def http_json(url, data=None, form=False, headers=None, cancel=None, retry_uncertain=True):
    if urllib.parse.urlsplit(url).scheme != 'https':
        raise SyncError('连接地址必须使用 HTTPS')
    payload = None
    hh = {'Accept':'application/json, text/event-stream','User-Agent':'Huang-Notion-Obsidian-Sync/1.0'}
    if data is not None:
        payload = (urllib.parse.urlencode(data) if form else json.dumps(data)).encode()
        hh['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    hh.update(headers or {})
    for attempt in range(4):
        if cancel and cancel.is_set():
            raise SyncError('已取消，本次未完成的更新不会继续')
        try:
            with urllib.request.urlopen(urllib.request.Request(url,payload,hh), timeout=45) as response:
                if 'text/event-stream' in response.headers.get('Content-Type',''):
                    # A POST stream may contain progress events before its RPC response.
                    chunks=[]
                    for rawline in response:
                        if cancel and cancel.is_set():
                            raise SyncError('已取消')
                        line=rawline.decode('utf-8').rstrip('\r\n')
                        if line.startswith('data:'):
                            chunks.append(line[5:].lstrip())
                        elif not line and chunks:
                            event=json.loads('\n'.join(chunks)); chunks=[]
                            if 'result' in event or 'error' in event:
                                return event,dict(response.headers)
                    if attempt < 3 and retry_uncertain:
                        if cancel: cancel.wait(2**attempt)
                        else: time.sleep(2**attempt)
                        continue
                    if not retry_uncertain:
                        raise SyncError('Notion 写入结果不确定：请先检查云端内容，确认后再重试',
                                        code='ambiguous_write')
                    raise SyncError('Notion 返回的连接结果不完整，请重试')
                body=response.read()
                return (json.loads(body) if body else {}),dict(response.headers)
        except urllib.error.HTTPError as exc:
            if url==ORIGIN+'/token' and exc.code==400:
                raise SyncError('Notion 授权已到期或失效，请点击“连接 Notion”重新授权') from None
            reason = None
            try:
                error_data = json.loads(exc.read(65536).decode('utf-8'))
                extra = error_data.get('additional_data') or {}
                reason = extra.get('rate_limit_reason')
            except (OSError, ValueError, UnicodeError, AttributeError):
                pass
            if reason == 'public_api_request_blocked':
                raise SyncError('Notion 已限制此连接的 API 访问，请联系 Notion 支持',
                                code=reason) from None
            if not retry_uncertain and exc.code == 529:
                raise SyncError('Notion 写入结果不确定：请先检查云端内容，确认后再重试', code='ambiguous_write') from None
            if exc.code in (429,529) or (retry_uncertain and exc.code in (502,503,504)):
                if attempt < 3:
                    try: delay=max(1,float(exc.headers.get('Retry-After',2**attempt)))
                    except ValueError: delay=2**attempt
                    if cancel: cancel.wait(delay)
                    else: time.sleep(delay)
                    continue
            if exc.code in (401,403):
                raise SyncError('Notion 授权已过期或访问受限，请点击“连接 Notion”重新授权') from None
            if not retry_uncertain and exc.code in (500,502,503,504):
                raise SyncError('Notion 写入结果不确定：请先检查云端内容，确认后再重试',
                                code='ambiguous_write') from None
            raise SyncError('Notion 请求未成功，HTTP '+str(exc.code)+'；请检查连接或稍后重试') from None
        except (urllib.error.URLError,TimeoutError,OSError):
            if attempt < 3 and retry_uncertain:
                if cancel: cancel.wait(2**attempt)
                else: time.sleep(2**attempt)
                continue
            if not retry_uncertain:
                raise SyncError('Notion 写入结果不确定：请先检查云端内容，确认后再重试',
                                code='ambiguous_write') from None
            raise SyncError('无法连接 Notion，请检查网络、代理与移动硬盘，再重试') from None

def metadata():
    resource,_=http_json(ORIGIN+'/.well-known/oauth-protected-resource')
    if ORIGIN not in resource.get('authorization_servers',[]):
        raise SyncError('Notion 登录服务地址发生变化，请更新工具')
    meta,_=http_json(ORIGIN+'/.well-known/oauth-authorization-server')
    for key in ('authorization_endpoint','token_endpoint','registration_endpoint'):
        if urllib.parse.urlsplit(meta.get(key,'')).netloc != 'mcp.notion.com':
            raise SyncError('Notion 登录地址校验未通过')
    return meta

class Auth:
    def __init__(self, store=None, allow_reset=False):
        self.store = store or CredentialStore()
        try:self.data = self.store.load()
        except (SyncError,ValueError,OSError):
            if not allow_reset:raise SyncError('本机授权无法读取，请点击“连接 Notion”重新授权') from None
            self.data={}
    def save_tokens(self,tokens):
        if not tokens.get('access_token'):
            raise SyncError('Notion 未返回有效授权，请重试连接')
        self.data.update(tokens)
        self.data['expires_at']=time.time()+float(tokens.get('expires_in',3600))
        self.store.save(self.data)
    def token(self, force=False):
        if not self.data.get('access_token'):
            raise SyncError('请先点击“连接 Notion”，在官方网页完成一次授权')
        if force or time.time()>self.data.get('expires_at',0)-300:
            if not self.data.get('refresh_token'):
                raise SyncError('请重新连接 Notion')
            tokens,_=http_json(ORIGIN+'/token',{'grant_type':'refresh_token',
                'refresh_token':self.data['refresh_token'],'client_id':self.data['client_id']},form=True)
            self.save_tokens(tokens)
        return self.data['access_token']
    def connect(self, log, cancel, browser_open=None):
        log('正在检查 Notion 官方登录服务')
        meta=metadata()
        state=secrets.token_urlsafe(32)
        verifier=secrets.token_urlsafe(48)
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        received={}
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                u=urllib.parse.urlsplit(self.path)
                q=urllib.parse.parse_qs(u.query)
                valid=u.path=='/callback' and secrets.compare_digest(q.get('state',[''])[0],state)
                if not valid:
                    self.send_error(400,'Invalid callback');return
                if q.get('iss',[''])[0] not in ('',ORIGIN):
                    self.send_error(400,'Invalid issuer');return
                received.update({k:v[0] for k,v in q.items()})
                body='<meta charset="utf-8"><title>连接 Notion</title><p>授权已返回，请回到一键同步窗口查看结果，可以关闭本页</p>'.encode('utf-8')
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def log_message(self,*args):
                pass
        try:
            server=http.server.HTTPServer(('127.0.0.1',47653),Handler)
        except OSError:
            raise SyncError('登录回调端口正被占用，请关闭其他同步窗口后重试') from None
        server.timeout=0.5
        try:
            if not self.data.get('client_id'):
                registration,_=http_json(meta['registration_endpoint'],{'client_name':'咕月15 Notion Obsidian Sync',
                    'redirect_uris':[REDIRECT],'grant_types':['authorization_code','refresh_token'],
                    'response_types':['code'],'token_endpoint_auth_method':'none'})
                self.data['client_id']=registration['client_id']
                log('正在保存本机登录配置')
                self.store.save(self.data)
            params={'response_type':'code','client_id':self.data['client_id'],'redirect_uri':REDIRECT,
                    'scope':'default','state':state,'code_challenge':challenge,'code_challenge_method':'S256',
                    'resource':ORIGIN,'prompt':'consent'}
            login_url=meta['authorization_endpoint']+'?'+urllib.parse.urlencode(params)
            log('登录地址：'+login_url)
            try:
                opened=(browser_open or webbrowser.open)(login_url)
            except (OSError,webbrowser.Error):
                opened=False
            if not opened:
                raise SyncError('未能自动打开浏览器。上面的“登录地址”已写入日志，复制到浏览器打开并完成授权即可，不要关闭本窗口')
            log('已打开 Notion 官方登录网页，请选择要同步的工作区并完成授权')
            deadline=time.monotonic()+600
            while not received and time.monotonic()<deadline and not cancel.is_set():
                server.handle_request()
            if cancel.is_set(): raise SyncError('已取消连接')
            if not received.get('code'): raise SyncError('授权未完成或已取消，请重新连接')
            tokens,_=http_json(meta['token_endpoint'],{'grant_type':'authorization_code','client_id':self.data['client_id'],
                'code':received['code'],'redirect_uri':REDIRECT,'code_verifier':verifier,'resource':ORIGIN},form=True)
            self.save_tokens(tokens)
        finally:
            server.server_close()

ERROR_TEXT={'object_in_trash':'Notion 里的那一页已在回收站，请先恢复它，或换一个目标页面',
            'usage_limit_reached':'Notion 的接口额度暂时已用完，请稍后重试',
            'rate_limited':'Notion 暂时限制了请求频率，请稍后重试',
            'object_not_found':'Notion 找不到请求的页面，可能已被移动或删除',
            'unauthorized':'Notion 授权已失效，请重新连接',
            'permission_denied':'当前 Notion 连接没有这个页面的权限，请检查共享设置',
            'restricted_resource':'当前 Notion 连接不能修改这个页面，请检查共享设置',
            'conflict':'Notion 认为这次修改有冲突，请刷新页面后重试',
            'validation_error':'Notion 不接受这次写入的内容，可能是笔记里有它不支持的写法'}
OPERATION_LABEL={'fetch':'页面读取','query-data-sources':'数据库读取','create-pages':'新建页面',
                 'update-page':'更新页面','create-comment':'添加评论','create-file-upload':'上传附件'}

def unpack(result,operation=None):
    if result.get('isError'):
        text='\n'.join(c.get('text','') for c in result.get('content',[]) if c.get('type')=='text')
        tool_code=re.search(r'"tool_error_code"\s*:\s*"([a-z_]+)"',text)
        api_code=re.search(r'"code"\s*:\s*"([a-z_]+)"',text)
        pick=tool_code or api_code
        code=pick[1] if pick else None
        label=OPERATION_LABEL.get(operation,'读取')
        reason=ERROR_TEXT.get(code)
        if reason:
            raise SyncError('没有完成'+label+'：'+reason,code=code,operation=operation)
        raise SyncError('没有完成'+label+'，Notion 返回了未预期的错误；详情已记入运行日志',
                        code=code,operation=operation)
    if result.get('structuredContent') is not None:
        return result['structuredContent']
    text='\n'.join(c.get('text','') for c in result.get('content',[]) if c.get('type')=='text')
    try:return json.loads(text)
    except ValueError:raise SyncError('Notion 返回的数据格式不受支持，请更新工具') from None

class MCP:
    def __init__(self,auth,cancel=None):
        self.auth=auth;self.cancel=cancel;self.seq=0;self.session=None;self.protocol='2025-03-26'
        self.tools={}
    def rpc(self,method,params=None,notification=False,retry_uncertain=True):
        self.seq+=1
        data={'jsonrpc':'2.0','method':method,'params':params or {}}
        if not notification:data['id']=self.seq
        headers={'Authorization':'Bearer '+self.auth.token(),'MCP-Protocol-Version':self.protocol}
        if self.session:headers['Mcp-Session-Id']=self.session
        reply,hh=http_json(ORIGIN+'/mcp',data,headers=headers,cancel=self.cancel,
                           retry_uncertain=retry_uncertain)
        self.session=next((v for k,v in hh.items() if k.lower()=='mcp-session-id'),self.session)
        if reply.get('error'):raise SyncError('Notion 连接协议返回错误，请更新工具或重新连接')
        return reply.get('result',{})
    def start(self):
        result=self.rpc('initialize',{'protocolVersion':self.protocol,'capabilities':{},
                       'clientInfo':{'name':'huang-notion-obsidian-sync','version':'1.0.0'}})
        self.protocol=result.get('protocolVersion',self.protocol)
        self.rpc('notifications/initialized',notification=True)
        cursor=None
        while True:
            page=self.rpc('tools/list',{'cursor':cursor} if cursor else {})
            for item in page.get('tools',[]):
                name=item['name'];canonical=name.removeprefix('notion-')
                if canonical in READ_TOOLS or canonical in WRITE_TOOLS:self.tools[canonical]=name
            cursor=page.get('nextCursor')
            if not cursor:break
    def call(self,name,**args):
        if name not in READ_TOOLS or name not in self.tools:
            raise SyncError('当前 Notion 连接未提供必要的读取能力：'+name)
        return unpack(self.rpc('tools/call',{'name':self.tools[name],'arguments':args}),operation=name)
    def write(self,name,**args):
        """Write-side tools used by the export direction; kept apart from call()."""
        if name not in WRITE_TOOLS or name not in self.tools:
            raise SyncError('当前 Notion 连接未提供必要的写入能力：'+name)
        return unpack(self.rpc('tools/call',{'name':self.tools[name],'arguments':args},
                               retry_uncertain=False),operation=name)


IMAGE_SUFFIXES = {'.png','.jpg','.jpeg','.gif','.webp','.bmp','.svg','.heic'}
UPLOAD_LIMIT = 5 * 1024 * 1024


def upload_image(client, path, cancel=None, expected_checksum=None):
    """Return the uploaded image reference and the fingerprint of the bytes sent."""
    path = Path(path)
    with path.open('rb') as stream:
        content = stream.read(UPLOAD_LIMIT + 1)
    size = len(content)
    if size <= 0:
        raise SyncError('图片是空文件：' + path.name)
    if size > UPLOAD_LIMIT:
        raise SyncError('图片超过 5 MB，暂不同步：' + path.name)
    checksum = hashlib.sha256(content).hexdigest()
    if expected_checksum is not None and checksum != expected_checksum:
        raise SyncError('图片内容已变化，请重新预览：' + path.name)
    if cancel is not None and cancel.is_set():
        raise SyncError('已取消，图片没有上传')
    info = client.write('create-file-upload', filename=path.name)
    url = info.get('upload_url')
    if not url:
        raise SyncError('Notion 没有返回上传地址：' + path.name)
    boundary = '----HuangSync' + secrets.token_hex(8)
    kind = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    head = ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
            'Content-Type: %s\r\n\r\n' % (boundary, info.get('upload_form_field') or 'file',
                                          path.name, kind)).encode('utf-8')
    payload = head + content + ('\r\n--%s--\r\n' % boundary).encode('utf-8')
    headers = dict(info.get('upload_headers') or {})
    headers['Content-Type'] = 'multipart/form-data; boundary=' + boundary
    if cancel is not None and cancel.is_set():
        raise SyncError('已取消，图片没有上传')
    request = urllib.request.Request(url, payload, headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            done = json.loads(response.read() or b'{}')
    except urllib.error.HTTPError as exc:
        if exc.code in (500, 502, 503, 504):
            raise SyncError('图片上传结果不确定，请先检查云端后再重试：' + path.name,
                            code='ambiguous_write') from None
        raise SyncError('图片上传失败（HTTP %s）：%s' % (exc.code, path.name)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SyncError('图片上传结果不确定，请先检查云端后再重试：' + path.name,
                        code='ambiguous_write') from None
    source = done.get('markdown_source')
    if not source:
        upload_id = done.get('file_upload_id') or info.get('file_upload_id')
        if not upload_id:
            raise SyncError('Notion 没有确认图片上传：' + path.name)
        source = 'file-upload://' + upload_id
    return source, checksum


def image_fingerprint(client, reference):
    """Read back a Notion-hosted image, without forwarding auth or following redirects."""
    if not reference.startswith('notion-file-block://'):
        raise SyncError('图片回读地址不是 Notion 文件块')
    result = client.call('get-file-download-urls', references=[reference])
    matches = [item.get('url') for item in result.get('files', [])
               if isinstance(item, dict) and item.get('reference') == reference]
    if len(matches) != 1 or not isinstance(matches[0], str) or result.get('unresolved'):
        raise SyncError('无法确认云端图片的下载地址')
    address = urllib.parse.urlsplit(matches[0])
    host = address.hostname or ''
    allowed = (host == 'secure.notion-static.com' or re.fullmatch(
        r'prod-files-secure(?:-[a-z0-9-]+)?\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com', host))
    if (address.scheme != 'https' or not allowed or address.username is not None
            or address.password is not None or address.port not in (None, 443)):
        raise SyncError('云端图片返回了无法确认的下载地址')

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(matches[0], timeout=45) as response:
            content = response.read(UPLOAD_LIMIT + 1)
    except OSError:
        raise SyncError('无法读取云端图片以核对内容') from None
    if not 0 < len(content) <= UPLOAD_LIMIT:
        raise SyncError('云端图片大小异常，已停止确认')
    return hashlib.sha256(content).hexdigest()
