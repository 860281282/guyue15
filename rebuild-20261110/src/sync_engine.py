"""Collect a full snapshot and apply a conservative, hash-based local update."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import time
import contextlib
from datetime import datetime, timezone, timedelta
import urllib.parse
import urllib.request
import html
import mimetypes
from notion_connection import SyncError, atomic_bytes

def digest(data):return hashlib.sha256(data).hexdigest()
def identity(url):
    if not re.match(r'https://(?:app\.)?notion\.(?:com|so)/',url):return None
    m=re.search(r'([a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})(?:[/?#]|$)',url)
    return m[1].replace('-','') if m else None

def validate_page(page):
    meta=page.get('metadata',{})
    if not isinstance(page.get('text'),str) or not page.get('id'):
        raise SyncError('页面内容不完整，已停止更新')
    if any(d.get('truncated') or d.get('unknown_block_count') or d.get('unknown_block_ids') for d in (page,meta)):
        raise SyncError('Notion 页面含有未能读取的内容，已停止本次更新')
    if re.search(r'<(?:unknown|inaccessible-block)\b',page['text']):
        raise SyncError('Notion 页面含有未能读取的内容，已停止本次更新')

def normalize_page(page,requested):
    if not isinstance(page,dict):raise SyncError('Notion 页面返回格式不正确，已停止更新')
    result=copy.deepcopy(page)
    url_id=identity(result.get('url',''))
    expected=identity(requested) or requested.replace('-','').lower()
    explicit=str(result.get('id','')).replace('-','').lower()
    if not url_id or url_id!=expected or (explicit and explicit!=url_id):
        raise SyncError('Notion 返回的页面链接与请求不一致，已停止更新')
    result['id']=url_id
    validate_page(result)
    return result

def list_all(client,name):
    allrows=[];cursor=None;seen=set()
    while True:
        page=client.call(name,**({'limit':200,'cursor':cursor} if cursor else {'limit':200}))
        allrows.extend(page.get('results',[]))
        nxt=page.get('next_cursor') or page.get('nextCursor')
        if not nxt:
            if page.get('has_more'):raise SyncError('Notion 列表缺少后续游标，已停止本次更新')
            return allrows
        if nxt in seen:raise SyncError('Notion 分页重复，已停止本次更新')
        seen.add(nxt);cursor=nxt

CHILD_TAG=re.compile(r'<(page|database)\s+url="([^"]+)"([^>]*?)(/?)>')

TITLE_ESCAPE = re.compile(r'\\([\\`*_{}\[\]()#+\-.!$<>|~&])')


def unescape_title(text):
    """Notion 给正文里的标题加了反斜杠转义，比较标题前先还原。"""
    return TITLE_ESCAPE.sub(r'\1', text or '').strip()


def children_of(text):
    """列出页面正文里直接包含的子页面/子数据库，标签之间的文字就是标题。"""
    found=[]
    for match in CHILD_TAG.finditer(text):
        kind,url,_attributes,selfclosing=match.groups()
        title=''
        if not selfclosing:
            tail=text[match.end():match.end()+200]
            named=re.match(r'\s*([^<]{1,80}?)\s*<',tail)
            if named:title=unescape_title(named.group(1).strip())
        found.append((kind,url,title))
    return found

def ancestors_of(text):
    """页面自带的祖先链，用来判断它是否落在用户勾选的类别里。"""
    ids=set()
    for match in re.finditer(r'<(?:parent-page|ancestor-\d+-page)\s+url="([^"]+)"',text):
        pid=identity(match.group(1))
        if pid:ids.add(pid)
    return ids

def discover_roots(client,log,cancel=None):
    """只扫第一层：列出所有大类，不读取它们的正文，所以很快。"""
    roots=list_all(client,'list-private-pages')+list_all(client,'list-shared-pages')
    tree=[];seen=set()
    for item in roots:
        if cancel is not None and cancel.is_set():raise SyncError('已取消，未改变同步范围')
        url=item.get('url') or '';pid=identity(url)
        if not pid or pid in seen:continue
        seen.add(pid)
        kind=item.get('type') or item.get('object') or 'page'
        tree.append({'id':pid,'url':url,'title':item.get('title') or '未命名','kind':kind,'children':[],'loaded':False})
    log('已找到 %d 个大类' % len(tree))
    return tree

def discover_children(client,node,log,cancel=None):
    """展开单个大类：只读取这一个页面的正文，列出它的直接子类。"""
    if node.get('kind')=='database':
        node['children']=[];node['loaded']=True;return []
    log('展开类别：'+node['title'])
    try:
        text=client.call('fetch',id=node['id'],include_file_urls=True,include_transcript=True).get('text','')
    except SyncError as exc:
        if exc.code in ('object_not_found','permission_denied'):
            log('类别已失效：'+node['title']);node['children']=[];node['loaded']=True;return []
        raise
    seen={node['id']};kids=[]
    for child_kind,child_url,title in children_of(text):
        child_id=identity(child_url)
        if not child_id or child_id in seen:continue
        seen.add(child_id)
        kids.append({'id':child_id,'url':child_url,'title':title or '未命名','kind':child_kind,'children':[],'loaded':True})
    for child in kids:
        if cancel is not None and cancel.is_set():raise SyncError('已取消，未改变同步范围')
        if child['title']!='未命名':continue
        try:child['title']=client.call('fetch',id=child['id'],include_file_urls=True,include_transcript=True).get('title') or '未命名'
        except SyncError:child['title']='无法读取'
    node['children']=kids;node['loaded']=True
    log('%s 下有 %d 个小类' % (node['title'],len(kids)))
    return kids

def discover(client,log,cancel=None):
    """一次性扫描所有大类及直接子页面（命令行列出清单时用）。"""
    tree=discover_roots(client,log,cancel)
    for node in tree:
        if cancel is not None and cancel.is_set():raise SyncError('已取消，未改变同步范围')
        discover_children(client,node,log,cancel)
    return tree

def view_configs(text):
    result={}
    for match in re.finditer(r'<view\s+url="\{?\{?([^"}]+)\}?\}?">\s*([\s\S]*?)\s*</view>',text):
        try:config=json.loads(match[2])
        except ValueError:raise SyncError('数据库视图配置无法识别，已停止更新') from None
        if isinstance(config,dict):result[match[1]]=config
    return result

def unfiltered_view(config,collection):
    if str(config.get('dataSourceUrl','')).strip('{}')!=collection:return False
    if config.get('type') not in ('table','list'):return False
    for key in ('filter','filters','search','searchQuery','hiddenGroups'):
        value=config.get(key)
        if value in (None,{},[],''):continue
        if isinstance(value,dict) and value.get('operator')=='and' and value.get('filters')==[]:continue
        return False
    return True

def database_rows(client,database,collection):
    candidates=view_configs(database['text'])
    for view_url,config in candidates.items():
        if not unfiltered_view(config,collection):continue
        fetched=client.call('fetch',id=view_url)
        current=view_configs(fetched.get('text','')).get(view_url)
        if not current or not unfiltered_view(current,collection):continue
        rows=[];cursor=None;seen_cursors=set();seen_rows=set()
        while True:
            data={'mode':'view','view_url':view_url,'page_size':100,'is_archived':False}
            if cursor:data['start_cursor']=cursor
            response=client.call('query-data-sources',data=data)
            if not isinstance(response.get('results'),list) or not isinstance(response.get('has_more'),bool):
                raise SyncError('数据库视图返回不完整，已停止更新')
            for row in response['results']:
                url=row.get('url','')
                if not identity(url) or identity(url) in seen_rows:
                    raise SyncError('数据库视图出现无效或重复记录，已停止更新')
                seen_rows.add(identity(url));rows.append(row)
            if not response['has_more']:break
            cursor=response.get('next_cursor')
            if not cursor or cursor in seen_cursors:
                raise SyncError('数据库视图分页游标缺失或重复，已停止更新')
            seen_cursors.add(cursor)
        final=view_configs(client.call('fetch',id=view_url).get('text','')).get(view_url)
        if final!=current:raise SyncError('数据库视图在读取过程中发生变化，请保存编辑后重新同步')
        return {'results':rows,'has_more':False,'read_mode':'view','view_url':view_url,'view_config':current}
    raise SyncError('数据库“'+database.get('title','未命名')+'”没有可用的无筛选视图，请保留一个显示全部记录的表格或列表视图后重试')

def collect(client,log,scope=None):
    own=client.call('get-self')
    workspace=own.get('workspace',{})
    warnings=[]
    selected=[]
    for item in scope or []:
        url=item.get('url') or ''
        if not identity(url):continue
        selected.append({'url':url,'title':item.get('title') or '已选类别','type':item.get('kind') or 'page'})
    if scope and not selected:raise SyncError('同步范围里没有可用的类别，请重新扫描后再勾选')
    roots=selected if scope else list_all(client,'list-private-pages')+list_all(client,'list-shared-pages')
    allowed={identity(item['url']) for item in selected} if scope else None
    outside=set()
    teams=client.call('get-teams')
    if teams.get('joinedTeams') or teams.get('otherTeams') or teams.get('hasMore'):
        warnings.append('发现团队空间：本工具当前覆盖私人、共享与 Memory 内容，未遍历团队空间')
    pending=[(x['url'],'root') for x in roots]
    memory=client.call('fetch',id='memory',include_file_urls=True,include_transcript=True)
    for url in re.findall(r'<(?:page|database)\s+url="\{?\{?([^"}]+)',memory.get('text','')):
        pending.append((url,'reference'))
    pages={};dbrows={};schemas={};fetched_collections=set()
    while pending:
        url,origin=pending.pop(0);pid=identity(url)
        if not pid or pid in pages:continue
        try:
            page=normalize_page(client.call('fetch',id=pid,include_file_urls=True,include_transcript=True),pid)
        except SyncError as exc:
            stale=exc.code in ('object_not_found','permission_denied')
            if not stale or origin=='row' or (origin=='root' and not scope):
                log('读取失败：'+url)
                raise
            if origin=='root':
                warnings.append('已勾选的类别已失效，已跳过：'+url)
            else:
                warnings.append(('云端链接已失效，已跳过：' if exc.code=='object_not_found' else '云端链接无法读取，已跳过：')+url)
            log('已跳过失效链接：'+url)
            continue
        if allowed is not None and origin!='row':
            route=ancestors_of(page.get('text',''));route.add(pid)
            if not route & allowed:
                outside.add(pid)
                continue
        pages[pid]=page
        log('正在读取：'+page.get('title','未命名'))
        if page.get('metadata',{}).get('type')=='database':
            cols=re.findall(r'<data-source\s+url="\{?\{?(collection://[a-f0-9-]+)',page['text'])
            if len(cols)!=1:
                raise SyncError('发现多数据源或无法识别的数据库，已停止更新，避免漏记')
            for collection in cols:
                if collection in fetched_collections:continue
                fetched_collections.add(collection)
                schemas[collection]=client.call('fetch',id=collection)
                dbrows[collection]=database_rows(client,page,collection)
                pending.extend((row['url'],'row') for row in dbrows[collection]['results'])
        else:
            match=re.search(r'<content>\n?([\s\S]*)\n?</content>',page['text'])
            body=match[1] if match else ''
            urls=re.findall(r'https://(?:app\.)?notion\.(?:com|so)/[^\s"<>)}\]]+',body)
            pending.extend((u,'reference') for u in urls if identity(u) not in pages)
    if outside:warnings.append('范围外页面已跳过 %d 个（未勾选，本次不处理）' % len(outside))
    if not pages:raise SyncError('未读取到任何页面，已停止更新')
    for database in dbrows.values():
        for row in database['results']:
            page=pages.get(identity(row['url']))
            if not page:raise SyncError('有数据库记录未读取到正文，已停止更新')
            match=re.search(r'<properties>\s*([\s\S]*?)\s*</properties>',page['text'])
            if match:
                properties=json.loads(match[1])
                for key,value in properties.items():
                    if key!='url':row.setdefault(key,value)
    return {'workspace':workspace.get('name','Notion'),'workspace_id':workspace.get('id'),
            'export_date':datetime.now().strftime('%Y-%m-%d'),'pages':pages,'databases':dbrows,'schemas':schemas,
            'scope':('仅同步已勾选的类别及其子页面、数据库记录，其余区域不处理' if scope else
                     '当前连接工作区的私人、共享顶层页面及递归子页面与数据库记录，另含 Notion Memory 和 About；不含回收站、历史版本及评论'),
            'warnings':warnings}

def normalize_content(data):
    # Synchronizing unchanged pages must not touch their mtime just for an import timestamp.
    return re.sub(rb'(?m)^"imported_at": .*\r?\n',b'',data)

def target_path(root,relative):
    relative=str(relative).replace('\\','/')
    pure=PurePosixPath(relative)
    if pure.is_absolute() or any(p in ('..','') for p in pure.parts) or ':' in relative:
        raise SyncError('导入清单包含不安全路径，已停止更新')
    dest=(Path(root)/relative).resolve()
    if not dest.is_relative_to(Path(root).resolve()):
        raise SyncError('导入目标超出仓库目录，已停止更新')
    return dest

def read_json(path,default):
    return json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else default

def baseline(vault):
    clean=vault/'.同步工具/数据/sync-state.json'
    if clean.exists():return read_json(clean,{})
    original=read_json(vault/'Notion导入/_原始数据/manifest.json',{})
    state=read_json(vault/'Notion导入/_原始数据/sync-state.json',None)
    if state is None:
        state={'files':{f['path']:f['sha256'] for f in original.get('files',[])},'mapping':original.get('mapping',{})}
    state.setdefault('remote_hashes',{})
    for rel,sha in state.get('files',{}).items():
        dest=target_path(vault,rel)
        if dest.is_file() and digest(dest.read_bytes())==sha:
            state['remote_hashes'].setdefault(rel,digest(normalize_content(dest.read_bytes())))
    return state

def render(snapshot,work,renderer,prior,vault=None):
    work.mkdir(parents=True,exist_ok=True)
    snapshot=copy.deepcopy(snapshot);snapshot['_prior_mapping']=prior.get('mapping',{})
    snapshot['_clean_layout']=prior.get('layout')=='clean-v1'
    snapshot['_vault_root']=str(Path(vault).resolve()) if vault is not None else None
    atomic_bytes(work/'snapshot.json',json.dumps(snapshot,ensure_ascii=False).encode('utf-8'))
    old=os.environ.get('NOTION_EXPORT_WORK')
    os.environ['NOTION_EXPORT_WORK']=str(work)
    try:
        with contextlib.redirect_stdout(io.StringIO()):runpy.run_path(str(renderer),run_name='__main__')
    finally:
        if old is None:os.environ.pop('NOTION_EXPORT_WORK',None)
        else:os.environ['NOTION_EXPORT_WORK']=old
    return work/'output'

def localize_media(snapshot,client,work,log,cancel):
    assets={};result=copy.deepcopy(snapshot)
    def https_in(value):
        if isinstance(value,str) and value.startswith('https://'):return value
        if isinstance(value,dict):
            for item in value.values():
                found=https_in(item)
                if found:return found
        if isinstance(value,list):
            for item in value:
                found=https_in(item)
                if found:return found
        return None
    for page in result['pages'].values():
        body=page['text'];sources=set(re.findall(r'notion-file-block://[^\s"<>)}\]]+',body))
        sources.update(re.findall(r'!\[[^\]]*\]\((https://[^\s)]+)\)',body))
        sources.update(re.findall(r'<image\b[^>]*source="([^"]+)"',body))
        for source in sources:
            if source.startswith('https://') and 'notion' not in urllib.parse.urlsplit(source).netloc.lower():
                continue
            if source not in assets:
                signed=source
                if source.startswith('notion-file-block://'):
                    refs=page.get('references',{})
                    signed=https_in(refs.get(source)) if isinstance(refs,dict) else None
                    if not signed:
                        response=client.call('get-file-download-urls',references=[html.unescape(source)])
                        if response.get('unresolved'):raise SyncError('附件无法读取，已停止更新')
                        signed=https_in(response.get('files'))
                    if not signed:raise SyncError('附件链接无法解析，已停止更新')
                if not signed.startswith('https://'):raise SyncError('附件使用不受支持的地址，已停止更新')
                url=urllib.parse.urlsplit(signed)
                name=urllib.parse.parse_qs(urllib.parse.urlsplit(html.unescape(source)).query).get('name',[''])[0]
                name=name or urllib.parse.unquote(Path(url.path).name) or 'attachment'
                name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','－',name)[:100].strip().rstrip('.') or 'attachment'
                key=source if source.startswith('notion-file-block://') else signed.split('?')[0]
                relative='Notion导入/附件/'+digest(key.encode())[:20]+'-'+name
                dest=target_path(work/'assets',relative);dest.parent.mkdir(parents=True,exist_ok=True)
                log('正在保存附件：'+name)
                attempts=0
                while True:
                    attempts+=1
                    try:
                        with urllib.request.urlopen(urllib.request.Request(html.unescape(signed),headers={'User-Agent':'NotionObsidianSync/1.0'}),timeout=45) as response, dest.open('xb') as output:
                            total=0
                            while True:
                                if cancel.is_set():raise SyncError('已取消下载')
                                chunk=response.read(1024*1024)
                                if not chunk:break
                                total+=len(chunk)
                                if total>256*1024*1024:raise SyncError('附件超过当前单文件 256MB 限制，已停止更新')
                                output.write(chunk)
                        break
                    except SyncError:
                        dest.unlink(missing_ok=True)
                        raise
                    except (OSError,urllib.error.URLError) as exc:
                        dest.unlink(missing_ok=True)
                        if attempts>=3:
                            log('附件下载失败：%s（%s）'%(name,type(exc).__name__))
                            raise SyncError('附件下载未成功，本次未更新笔记，请检查网络后重试') from None
                        log('附件下载重试 %d/3：%s'%(attempts,name))
                        time.sleep(1.5*attempts)
                assets[source]=relative
            relative=assets[source]
            body=re.sub(r'!\[[^\]]*\]\('+re.escape(source)+r'\)',lambda m:'![['+relative+']]',body)
            body=re.sub(r'<(image|pdf|audio|video|file)\b[^>]*source="'+re.escape(source)+r'"[^>]*>([\s\S]*?)</\1>',lambda m:('' if m[1]=='file' else '!')+'[['+relative+']]',body)
            body=body.replace(source,relative)
        page['text']=body
    result['_asset_paths']=list(assets.values())
    return result

def repair_stage(stage,index=None):
    """把渲染进暂存区的笔记里的 Notion 残留标记改成 Markdown（写盘前完成）。"""
    import md_fix
    stage=Path(stage).resolve()
    index=index or {}
    changed=0;totals={}
    for path in sorted(stage.rglob('*.md')):
        rel=path.relative_to(stage).as_posix()
        if rel.startswith('Notion导入/'):continue
        raw=path.read_text(encoding='utf-8',errors='ignore')
        stats={'list':0,'todo':0,'callout':0,'table':0,'mention':0,'color':0,'toc':0,'br':0}
        fixed=md_fix.fix_text(raw,index,stats)
        if fixed==raw:continue
        path.write_text(fixed,encoding='utf-8',newline='')
        changed+=1
        for key,value in stats.items():totals[key]=totals.get(key,0)+value
    return {'changed':changed,'totals':totals,'text':md_fix.describe(totals) if changed else ''}

def apply_update(vault,stage,run_id,prior):
    vault=Path(vault).resolve();stage=Path(stage).resolve()
    if not (vault/'.obsidian').is_dir():raise SyncError('请选择已有的 Obsidian 知识库文件夹')
    expected=read_json(stage/'Notion导入/_原始数据/manifest.json',{})
    state=copy.deepcopy(prior)
    clean=prior.get('layout')=='clean-v1'
    state_path=vault/('.同步工具/数据/sync-state.json' if clean else 'Notion导入/_原始数据/sync-state.json')
    state.setdefault('files',{});state.setdefault('mapping',{})
    state.setdefault('remote_hashes',{})
    # Keep mappings for cloud-deleted notes as well: their files stay on disk.
    state['mapping'].update(expected['mapping'])
    changes=[];counts={'added':0,'updated':0,'unchanged':0,'conflicts':0,'retained':0}
    control={'Notion导入/_原始数据/manifest.json','Notion导入/_原始数据/sync-state.json'}
    for src in sorted(stage.rglob('*')):
        if not src.is_file():continue
        rel=src.relative_to(stage).as_posix()
        if rel in control or (clean and rel.startswith('Notion导入/_原始数据/')):continue
        dest=target_path(vault,rel);new=src.read_bytes();oldhash=state['files'].get(rel)
        current=dest.read_bytes() if dest.is_file() else None
        conflict=False
        if current is not None and (current==new or (rel.endswith('.md') and normalize_content(current)==normalize_content(new))):
            counts['unchanged']+=1
            state['files'][rel]=digest(current)
            state['remote_hashes'][rel]=digest(normalize_content(new))
            continue
        if current is not None and prior.get('remote_hashes',{}).get(rel)==digest(normalize_content(new)):
            counts['retained']+=1
            continue
        if current is None and oldhash is None:
            counts['added']+=1
        elif current is not None and oldhash==digest(current):
            counts['updated']+=1
        else:
            counts['conflicts']+=1;conflict=True
        changes.append((rel,dest,new,digest(current) if current is not None else None,conflict))
    for rel in prior.get('files',{}):
        if not (stage/rel).is_file():counts['retained']+=1
    # Check the entire plan before touching existing files.
    for rel,dest,new,seen,conflict in changes:
        if dest.exists() and not dest.is_file():raise SyncError('目标路径已被文件夹占用：'+rel)
        current=dest.read_bytes() if dest.is_file() else None
        if (digest(current) if current is not None else None)!=seen:
            raise SyncError('同步期间笔记发生变化，请保存并重新同步')
    records=[]
    for rel,dest,new,seen,conflict in changes:
        # Recheck immediately before replace, avoiding silently clobbering an edit.
        current=dest.read_bytes() if dest.is_file() else None
        if (digest(current) if current is not None else None)!=seen:
            conflict=True;counts['conflicts']+=1
        if conflict:
            candidate=target_path(vault,('.同步工具/冲突/' if clean else 'Notion同步冲突/')+run_id+'/'+rel)
            atomic_bytes(candidate,new);records.append({'path':rel,'action':'conflict','candidate':str(candidate)})
            continue
        if current is not None:
            backup=target_path(vault,('.同步工具/备份/' if clean else 'Notion同步备份/')+run_id+'/'+rel)
            atomic_bytes(backup,current)
        atomic_bytes(dest,new)
        if digest(dest.read_bytes())!=digest(new):raise SyncError('写入校验失败：'+rel)
        state['files'][rel]=digest(new)
        state['remote_hashes'][rel]=digest(normalize_content(new))
        # Commit each written file's baseline so an interrupted run is recoverable.
        atomic_bytes(state_path,json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))
        records.append({'path':rel,'action':'write'})
    state['last_sync']=run_id
    state['workspace_id']=expected.get('workspace_id') or prior.get('workspace_id')
    atomic_bytes(state_path,json.dumps(state,ensure_ascii=False,indent=2).encode('utf-8'))
    return {'counts':counts,'records':records,'pages':expected.get('pages'),'databases':expected.get('databases')}
