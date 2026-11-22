"""Keep user notes in three top-level folders and operational data in one dot-folder."""
import hashlib,json,re
from pathlib import Path
from notion_connection import SyncError,atomic_bytes

TOOL_FOLDER='.同步工具'
NOTE_FOLDERS=('知识库','日常生活','日程提醒')

def note_path(relative):
    relative=relative.replace('\\','/')
    if relative.startswith('Notion导入/'):relative=relative[len('Notion导入/'):]
    parts=relative.split('/')
    for category in ('题库','笔记库'):
        if parts[-1]==category+'.md':return '知识库/'+category+'.md'
        if category in parts:
            return '知识库/'+category+'/'+'/'.join(parts[parts.index(category)+1:])
    if parts[-1]=='Agent Operating Guide.md':return '知识库/个人资料/'+parts[-1]
    for folder in NOTE_FOLDERS:
        if relative==folder+'.md':return folder+'/'+folder+'.md'
        if relative.startswith(folder+'/'):return relative
    if relative.startswith('附件/'):return '知识库/'+relative
    if relative.startswith('Memories'):
        return '知识库/个人资料/'+relative
    return '知识库/'+relative

def rewrite_links(text,mapping):
    targets={}
    for old,new in mapping.items():
        targets[old]=new
        if old.endswith('.md'):targets[old[:-3]]=new[:-3]
    def replace(match):
        target=match[1];escape='\\' if target.endswith('\\') else ''
        plain=target.rstrip('\\');parts=plain.split('#',1)
        updated=targets.get(parts[0],parts[0])
        if len(parts)>1:updated+='#'+parts[1]
        return '[['+updated+escape+(match[2] or '')+']]'
    return re.sub(r'\[\[([^|\]]+)(\|[^\]]*)?\]\]',replace,text)

def clean_stage(stage,output):
    stage=Path(stage);output=Path(output)
    old=json.loads((stage/'Notion导入/_原始数据/manifest.json').read_text(encoding='utf-8'))
    translation={relative:note_path(relative) for relative in old['mapping'].values()}
    for src in stage.rglob('*'):
        if src.is_file() and not src.relative_to(stage).as_posix().startswith('Notion导入/_原始数据/') and src.suffix not in ('.csv','.json'):
            relative=src.relative_to(stage).as_posix()
            if relative!='Notion 导入入口.md':translation.setdefault(relative,note_path(relative))
    files={};destination_keys=set()
    for src in stage.rglob('*'):
        if not src.is_file():continue
        relative=src.relative_to(stage).as_posix()
        if relative=='Notion 导入入口.md' or relative.startswith('Notion导入/_原始数据/') or src.suffix in ('.csv','.json'):continue
        target=translation.get(relative,note_path(relative))
        key=target.replace('\\','/').casefold()
        if key in destination_keys:raise SyncError('整理后的笔记路径重名，已停止以保护原文件：'+target)
        destination_keys.add(key)
        data=src.read_bytes()
        if src.suffix=='.md':data=rewrite_links(data.decode('utf-8'),translation).encode('utf-8')
        files[target]=data
    for relative,data in files.items():
        dest=(output/relative).resolve()
        if not dest.is_relative_to(output.resolve()):raise SyncError('整理目标超出临时目录')
        atomic_bytes(dest,data)
    old['mapping']={pid:translation[path] for pid,path in old['mapping'].items()}
    old['files']=[{'path':rel,'sha256':hashlib.sha256(data).hexdigest(),'size':len(data)} for rel,data in files.items()]
    atomic_bytes(output/'Notion导入/_原始数据/manifest.json',json.dumps(old,ensure_ascii=False,indent=2).encode('utf-8'))
    return output
