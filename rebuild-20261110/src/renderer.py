import csv
import os
import hashlib
import html
import io
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from layout import note_path
from notion_connection import SyncError
from sync_engine import target_path

BASE = Path(os.environ['NOTION_EXPORT_WORK'])
DATA = json.loads((BASE / 'snapshot.json').read_text(encoding='utf-8'))
OUT = BASE / 'output'
OUT.mkdir(exist_ok=True)
PAGES = DATA['pages']
NOW = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
PREFIX = 'Notion导入'

def safe(value):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '－', value).strip().rstrip('.')[:110] or '未命名'

def props(page):
    match = re.search(r'<properties>\s*([\s\S]*?)\s*</properties>', page['text'])
    return json.loads(match[1]) if match else {}

def title(page):
    pp = props(page)
    return pp.get('title') or pp.get('标题') or pp.get('事项') or pp.get('Title') or page['title']

PATHS = {}
prior_mapping = DATA.get('_prior_mapping', {})
clean_layout = DATA.get('_clean_layout', False)
vault_root = DATA.get('_vault_root')

def destination_key(relative):
    # Compare the actual final Windows path, not just the intermediate render path.
    return (note_path(relative) if clean_layout else relative).replace('\\', '/').casefold()

# Reserve even mappings outside this sync's selected scope. Existing paths always
# take precedence over new pages, regardless of the order returned by Notion.
occupied = {}
for prior_pid, prior_path in prior_mapping.items():
    key = destination_key(prior_path)
    if key in occupied and occupied[key] != prior_pid:
        raise SyncError('已有笔记映射指向同一路径，已停止以保护原文件：' + prior_path)
    occupied[key] = prior_pid

def unavailable(relative, pid):
    key = destination_key(relative)
    if key in occupied and occupied[key] != pid:
        return True
    if vault_root and pid not in prior_mapping:
        final = note_path(relative) if clean_layout else relative
        # Validate before probing; never claim a path already occupied locally.
        target_path(vault_root, final)
        try:
            os.stat(Path(vault_root) / final, follow_symlinks=False)
            return True
        except FileNotFoundError:
            pass
    return False

for pid, page in PAGES.items():
    parent = page.get('path', '')
    if not parent and page['metadata']['type'] == 'database':
        match = re.search(r'<parent-page[^>]+title="([^"]*)"', page['text'])
        parent = html.unescape(match[1]) if match else ''
    parts = [safe(x) for x in parent.split(' / ') if x]
    relative = prior_mapping.get(pid) or '/'.join([PREFIX] + parts + [safe(title(page)) + '.md'])
    if unavailable(relative, pid):
        stem = relative[:-3]
        for number in range(1, 1000001):
            candidate = stem + '（' + str(number) + '）.md'
            if not unavailable(candidate, pid):
                relative = candidate
                break
        else:
            raise SyncError('无法为同名页面分配安全路径，已停止以保护原文件：' + relative)
    occupied[destination_key(relative)] = pid
    PATHS[pid] = relative

def pid_from_url(url):
    match = re.search(r'([0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})(?:[?#/]|$)', url)
    return match[1].replace('-', '') if match else None

def local_link(url, label=''):
    pid = pid_from_url(url)
    if pid in PATHS:
        target = PATHS[pid][:-3]
        label = (label.strip() or title(PAGES[pid])).replace('|', '｜').replace(']', '］')
        return '[[' + target + '|' + label + ']]'
    return '[' + (label.strip() or url) + '](' + url + ')'

def display(value):
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    value = str(value)
    if re.match(r'^\d{4}-\d{2}-\d{2}[ T].*Z$', value):
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M +08:00')
        except ValueError:
            pass
    return value

def convert(body):
    protected = []
    def hold(m):
        protected.append(m[0])
        return '\x00CODE' + str(len(protected)-1) + '\x00'
    body = re.sub(r'(?m)^(`{3,}|~{3,})[^\n]*\n[\s\S]*?^\1\s*$', hold, body)
    body = re.sub(r'`+[^`\n]+`+', hold, body)
    def entity(m):
        return local_link(html.unescape(m[2]), html.unescape(m[3] or ''))
    body = re.sub(r'<(mention|page|database|folder)\b[^>]*?url="([^"]+)"[^>]*/>', lambda m: local_link(html.unescape(m[2])), body)
    body = re.sub(r'<(mention|page|database|folder)\b[^>]*?url="([^"]+)"[^>]*>([\s\S]*?)</\1>', entity, body)
    body = re.sub(r'(?<!!)\[([^\]\n]*)\]\((https://(?:app\.)?notion\.(?:com|so)/[^\s)]+)\)', lambda m: local_link(m[2],m[1]), body)
    body = re.sub(r'<date\b([^>]*)/>', lambda m: ' '.join(v for k,v in re.findall(r'([\w-]+)="([^"]*)"',m[1]) if k in ('start','start-time','end','end-time','time-zone')), body)
    body = re.sub(r'<code>([\s\S]*?)</code>', lambda m: '`' + html.unescape(m[1]) + '`', body)
    for tag, marker in [('b','**'),('i','*'),('s','~~')]:
        body = re.sub('<'+tag+r'>([\s\S]*?)</'+tag+'>',lambda m:marker+m[1]+marker,body)
    body = re.sub(r'<(span|u)\b[^>]*>([\s\S]*?)</\1>',lambda m:m[2],body)
    body = re.sub(r'<toggle\b[^>]*heading="h([1-4])"[^>]*>\s*',lambda m:'\n'+'#'*int(m[1])+' ',body)
    body = re.sub(r'<h([1-4])\b[^>]*>\s*',lambda m:'\n'+'#'*int(m[1])+' ',body)
    body = re.sub(r'</h[1-4]>', '\n', body)
    body = re.sub(r'<(?:p|column|columns|toggle|quote|callout)\b[^>]*?/?>|</(?:p|column|columns|toggle|quote|callout)>','\n',body)
    body = re.sub(r'\n[ \t]+\n','\n\n',body)
    body = re.sub(r'\n{3,}','\n\n',body).strip()
    for i, code in enumerate(protected):
        body = body.replace('\x00CODE'+str(i)+'\x00',code)
    return body

FILES = {}
for pid, page in PAGES.items():
    pp = props(page)
    meta = {'notion_id':pid,'notion_url':page['url'],'imported_at':NOW}
    if page.get('page_last_edited_at'):
        meta['notion_last_edited_at'] = page['page_last_edited_at']
    meta.update({k:v for k,v in pp.items() if k not in ('url','title')})
    header = '---\n' + ''.join(json.dumps(k,ensure_ascii=False)+': '+json.dumps(v,ensure_ascii=False)+'\n' for k,v in meta.items())+'---\n\n'
    result = header+'# '+title(page)+'\n\n'
    if page['metadata']['type'] == 'database':
        collection = re.search(r'<data-source url="\{?\{?(collection://[a-f0-9-]+)',page['text'])[1]
        rows = DATA['databases'][collection]['results']
        columns = list(dict.fromkeys(k for row in rows for k in row if k not in ('id','url','createdTime') and not k.endswith(':is_datetime')))
        result += '> 数据库索引，时间按北京时间显示\n\n'
        labels = [re.sub(r'^date:(.*):start$',r'\1',re.sub(r'^date:(.*):end$',r'\1结束',k)) for k in columns]
        result += '| 页面 | '+' | '.join(labels)+' |\n| --- | '+' | '.join('---' for _ in columns)+' |\n'
        for row in rows:
            cells = [local_link(row['url']).replace('|','\\|')] + [display(row.get(k)).replace('|','\\|').replace('\n','<br>') for k in columns]
            result += '| '+' | '.join(cells)+' |\n'
        if not rows:result=header+'# '+title(page)+'\n\n暂无条目，新增内容会在下次同步时出现在这里。\n'
        csvfile = PATHS[pid][:-3]+'.csv'
        buffer = io.StringIO(newline='')
        allcols = list(dict.fromkeys(k for row in rows for k in row))
        writer = csv.DictWriter(buffer,fieldnames=allcols)
        writer.writeheader()
        writer.writerows(rows)
        FILES[csvfile] = buffer.getvalue()
    else:
        match = re.search(r'<content>\n?([\s\S]*)\n?</content>',page['text'])
        body = match[1] if match else ''
        result += convert(body)
        if not body:
            fields = [(k,v) for k,v in pp.items() if k not in ('url','title','标题','事项','Title')]
            result += '\n'.join('- '+k+'：'+display(v) for k,v in fields)
    result += '\n\n---\n[在 Notion 中查看原页]('+page['url']+')\n'
    FILES[PATHS[pid]] = result

index = '# Notion 导入入口\n\n导入时间：'+NOW+'\n\n'
index += '通过一键同步工具从 Notion 更新到本地，Obsidian 内的修改不会上传到 Notion。\n\n'
for label in ['知识库','学习笔记','项目资料','日常生活','日程提醒','Memories']:
    for pid,p in PAGES.items():
        if title(p)==label:
            index += '- '+local_link(p['url'])+'\n'
index += '\n## 全部页面\n\n'
for pid,p in sorted(PAGES.items(),key=lambda x:PATHS[x[0]]):
    index += '- '+local_link(p['url'])+'\n'
index += '\n## 导入范围\n\n'+DATA['scope']+'\n\n'
index += '数据库已转换为索引表、独立笔记和原始字段 CSV；原文与属性保留在备份中。\n\n'
index += 'Notion 分栏、颜色与折叠会转换为普通正文和标题；提醒、自动化、权限、数据库交互视图和评论不随文件导入。原文中的操作说明仅作为文字保存。\n'
FILES['Notion 导入入口.md'] = index
FILES[PREFIX+'/_原始数据/snapshot.json'] = json.dumps(DATA,ensure_ascii=False,indent=2)+'\n'

for relative,content in FILES.items():
    dest=OUT/relative
    dest.parent.mkdir(parents=True,exist_ok=True)
    dest.write_text(content,encoding='utf-8',newline='')

manifest={'workspace_id':DATA.get('workspace_id'),'imported_at':NOW,'pages':len(PAGES),'databases':len(DATA['databases']),'mapping':PATHS,'files':[]}
for relative in FILES:
    dest=OUT/relative
    manifest['files'].append({'path':relative,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest(),'size':dest.stat().st_size})
(OUT/PREFIX/'_原始数据/manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

badlinks=[]
for relative,content in FILES.items():
    if relative.endswith('.md'):
        for match in re.finditer(r'\[\[([^|\]]+)(?:\|[^\]]*)?\]\]',content):
            if match[1].rstrip('\\')+'.md' not in FILES and match[1].rstrip('\\') not in DATA.get('_asset_paths', []):
                badlinks.append((relative,match[1]))
assert not badlinks,badlinks
print(json.dumps({'pages':len(PAGES),'databases':len(DATA['databases']),'files':len(FILES)+1,'broken_links':badlinks,'output':str(OUT)},ensure_ascii=False))
