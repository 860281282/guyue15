"""Experimental Notion Markdown v2 adapter; requires an injected HTTP transport.

No credential handling, production wiring, retries, page creation, delete, or
rich-content conversion. Local/private Obsidian syntax is refused. This is not
cleared for real-account writes.
"""
import re
from urllib.parse import quote

from notion_connection import SyncError
from v3_recovery import RemoteContent

_VERSION = '2026-03-11'
_ID = re.compile(r'^(?:[a-fA-F0-9]{32}|[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12})$')
_UNSAFE = re.compile(r'%%|!\[\[|\[\[|<[^>]*>|^---\s*$', re.MULTILINE)


def _page(page_id):
    if not isinstance(page_id,str) or not _ID.fullmatch(page_id):
        raise SyncError('Notion 页面编号不正确')
    return quote(page_id,safe='-')


def _checked_markdown(payload):
    if (not isinstance(payload,dict) or payload.get('object') != 'page_markdown' or
        not isinstance(payload.get('markdown'),str) or payload.get('truncated') is not False or
        payload.get('unknown_block_ids') != [] or payload.get('warnings')):
        raise SyncError('Notion 内容不完整或带有警告，必须人工核对')
    text=payload['markdown']
    if re.search(r'<unknown\b|<inaccessible-block\b',text,re.I):
        raise SyncError('Notion 内容含未知块，必须人工核对')
    return text


def _minimal_update(before, after):
    if not before or before == after:
        raise SyncError('无唯一可替换内容，停止写入')
    if _UNSAFE.search(before) or _UNSAFE.search(after):
        raise SyncError('内容包含尚不支持的结构或本地私有语法，停止写入')
    start = 0
    while start < min(len(before),len(after)) and before[start] == after[start]:
        start += 1
    end_before, end_after = len(before), len(after)
    while end_before > start and end_after > start and before[end_before-1] == after[end_after-1]:
        end_before -= 1;end_after -= 1
    # Add context for insertions or duplicate text, keeping the selected anchor unique.
    left, right = start, end_before
    while not before[left:right] or before.count(before[left:right]) != 1:
        if right-left >= 512:
            raise SyncError('替换锚点过长或不唯一，停止写入')
        if left > 0:
            left -= 1
        elif right < len(before):
            right += 1
        else:
            raise SyncError('无法找到唯一替换位置，停止写入')
    if left == 0 and right == len(before):
        raise SyncError('替换范围包含整个页面，停止自动写入')
    old = before[left:right]
    new = before[left:start] + after[start:end_after] + before[end_before:right]
    if before.replace(old,new,1) != after:
        raise SyncError('替换计划无法验证，停止写入')
    return old,new


class NotionMarkdown:
    def __init__(self, transport):
        self.transport=transport

    def _request(self, method, path, *, params=None, body=None):
        # transport must perform exactly one request and must never retry writes.
        return self.transport(method,path,headers={'Notion-Version':_VERSION},params=params,body=body)

    def read(self, page_id):
        page=_page(page_id);path='/v1/pages/'+page
        before=self._request('GET',path)
        data=self._request('GET',path+'/markdown',params={'markdown_version':'v2'})
        after=self._request('GET',path)
        if not isinstance(before,dict) or not isinstance(data,dict) or not isinstance(after,dict):
            raise SyncError('Notion 页面版本读取失败')
        version=before.get('last_edited_time')
        if not isinstance(version,str) or not version or version != after.get('last_edited_time'):
            raise SyncError('读取期间云端页面发生变化')
        if str(before.get('id','')).replace('-','').lower()!=page_id.replace('-','').lower() or str(after.get('id','')).replace('-','').lower()!=page_id.replace('-','').lower():
            raise SyncError('Notion 页面身份不一致')
        if str(data.get('id','')).replace('-','').lower()!=page_id.replace('-','').lower():
            raise SyncError('Notion Markdown 身份不一致')
        return RemoteContent(version,_checked_markdown(data).encode('utf-8'))

    def write(self, page_id, before, target):
        if not isinstance(before,bytes) or not isinstance(target,bytes):
            raise TypeError('content must be bytes')
        try:
            old,new=_minimal_update(before.decode('utf-8'),target.decode('utf-8'))
        except UnicodeError as exc:
            raise SyncError('内容不是 UTF-8，停止写入') from exc
        payload={'type':'update_content','markdown_version':'v2',
                 'update_content':{'content_updates':[{'old_str':old,'new_str':new}]}}
        result=self._request('PATCH','/v1/pages/'+_page(page_id)+'/markdown',body=payload)
        # An error here may follow a successful partial write: caller must reconcile, never retry.
        if str(result.get('id','')).replace('-','').lower()!=page_id.replace('-','').lower():
            raise SyncError('更新返回的页面身份不一致，需核对远端')
        _checked_markdown(result)
