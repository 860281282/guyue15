"""Single-request Notion REST transport for the disabled experimental v3 adapter.

Credentials are provided by the caller; no MCP OAuth token is assumed to be a
Notion REST credential. Writes are never retried and redirects are forbidden.
"""
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from notion_connection import SyncError

BASE='https://api.notion.com'
MAX_RESPONSE=8*1024*1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None


def _delay(value):
    try:
        seconds=float(value)
    except (TypeError,ValueError):
        try:
            instant=parsedate_to_datetime(value)
            if instant.tzinfo is None:instant=instant.replace(tzinfo=timezone.utc)
            seconds=(instant-datetime.now(timezone.utc)).total_seconds()
        except (TypeError,ValueError,OverflowError):
            return None
    return max(0.0,seconds) if math.isfinite(seconds) else None


class NotionREST:
    def __init__(self, token_provider, *, opener=None, sleep=time.sleep, max_get_retries=2, max_wait=60):
        self.token_provider=token_provider
        self.opener=opener or urllib.request.build_opener(_NoRedirect()).open
        self.sleep=sleep
        self.max_get_retries=max_get_retries
        self.max_wait=max_wait

    def __call__(self,method,path,*,headers=None,params=None,body=None):
        if method not in ('GET','PATCH') or not isinstance(path,str) or not path.startswith('/v1/pages/'):
            raise SyncError('不允许的 Notion 请求')
        parsed=urllib.parse.urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or '..' in parsed.path.split('/'):
            raise SyncError('Notion 请求路径不安全')
        token=self.token_provider()
        if not isinstance(token,str) or not token:
            raise SyncError('Notion REST 授权不可用')
        url=BASE+parsed.path
        if params:url+='?'+urllib.parse.urlencode(params)
        request_headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'}
        for key,value in (headers or {}).items():
            if key.lower()!='notion-version':raise SyncError('不允许的 Notion 请求头')
            request_headers['Notion-Version']=value
        data=json.dumps(body,ensure_ascii=False).encode('utf-8') if body is not None else None
        attempt=0
        while True:
            request=urllib.request.Request(url,data=data,headers=request_headers,method=method)
            try:
                with self.opener(request,timeout=30) as response:
                    raw=response.read(MAX_RESPONSE+1)
                if len(raw)>MAX_RESPONSE:raise SyncError('Notion 返回内容过大，已停止读取')
                parsed_response=json.loads(raw.decode('utf-8'))
                if not isinstance(parsed_response,dict):raise ValueError('invalid object')
                return parsed_response
            except urllib.error.HTTPError as exc:
                if method=='GET' and exc.code in (429,529) and attempt<self.max_get_retries:
                    wait=_delay(exc.headers.get('Retry-After') if exc.headers else None)
                    if wait is not None and wait<=self.max_wait:
                        attempt+=1;self.sleep(wait);continue
                raise SyncError('Notion 请求未完成（HTTP %d），已停止并保留现有内容' % exc.code) from None
            except (urllib.error.URLError,TimeoutError,OSError,UnicodeError,ValueError):
                raise SyncError('Notion 请求未完成，已停止并保留现有内容') from None
