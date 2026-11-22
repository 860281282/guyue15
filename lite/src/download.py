"""读取 Notion 并分阶段写入 Obsidian。"""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import secrets
import shutil
import sys
import tempfile

from notion_connection import Auth, MCP, SingleRun, SyncError, appdata, atomic_bytes
from sync_engine import baseline, collect, render, apply_update, localize_media, target_path, repair_stage
from md_fix import load_index
from layout import clean_stage, NOTE_FOLDERS

HOME = appdata()
RESOURCE = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))


def now_id():return datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)


def sync(vault,log,cancel,home=HOME,scope=None,repair=False,expected_workspace=None):
    vault=Path(vault).resolve()
    if not (vault/'.obsidian').is_dir():raise SyncError('请选择已有的知识库文件夹，里面应有 .obsidian 文件夹')
    cache=home/'缓存';cache.mkdir(parents=True,exist_ok=True)
    with SingleRun(),tempfile.TemporaryDirectory(prefix='work-',dir=cache) as scratch:
        client=MCP(Auth(),cancel);client.start()
        account=client.call('get-self')
        workspace=account.get('workspace') or {}
        if not workspace.get('id') or (expected_workspace and workspace.get('id')!=expected_workspace):
            raise SyncError('连接的工作区已变化，请重新查看同步范围')
        prior=baseline(vault)
        if not prior.get('mapping'):prior['layout']='clean-v1'
        clean=prior.get('layout')=='clean-v1'
        expected=prior.get('workspace_id')
        if expected and workspace.get('id')!=expected:
            raise SyncError('登录的 Notion 工作区与已有笔记不一致，已停止更新，请连接原来那个工作区')
        from export_engine import load_state as load_export_state
        upload_workspace = load_export_state(vault).get('workspace_id')
        if upload_workspace and workspace.get('id') != upload_workspace:
            raise SyncError('当前 Notion 工作区与该知识库的上传绑定不一致，已停止导入')
        if not expected and workspace.get('id'):
            # 第一次同步时记住工作区，之后换工作区才会拦
            mark=load_settings(home=home);mark['workspace_id']=workspace.get('id')
            save_settings(mark,home=home)
        run_id=now_id();work=Path(scratch)
        log('开始读取 Notion，完成读取后才更新知识库')
        if scope:log('同步范围：已勾选 {0} 个类别，其余区域不处理'.format(len(scope)))
        snapshot=collect(client,log,scope=scope)
        for relative in prior.get('mapping',{}).values():
            prefixes=tuple(folder+'/' for folder in NOTE_FOLDERS) if clean else ('Notion导入/',)
            if not relative.startswith(prefixes) or not relative.endswith('.md'):
                raise SyncError('原有文件映射不符合导入范围，已停止更新')
            target_path(work/'output',relative)
        original=snapshot
        snapshot=localize_media(snapshot,client,work,log,cancel)
        stage=render(snapshot,work,RESOURCE/'renderer.py',prior,vault=vault)
        asset_root=work/'assets'
        if asset_root.is_dir():shutil.copytree(asset_root,stage,dirs_exist_ok=True)
        # Original references are preserved separately for future recovery.
        if clean:stage=clean_stage(stage,work/'clean-output')
        if cancel.is_set():raise SyncError('已取消，本次未更新知识库')
        repaired=None
        if repair:
            # 先修标记再落盘，这样写进状态的是修好后的内容指纹
            log('正在修掉 Notion 留下的原始标记')
            repaired=repair_stage(stage,load_index(vault))
            if repaired['changed']:log('已修排版标记：{0} 篇（{1}）'.format(repaired['changed'],repaired['text']))
        log('读取完成，正在比较本地内容并保存更新，请勿拔出硬盘')
        result=apply_update(vault,stage,run_id,prior)
        if repaired:result['repaired']=repaired
        atomic_bytes(home/'数据/source-snapshot.json',json.dumps(original,ensure_ascii=False,indent=2).encode('utf-8'))
        result['warnings']=snapshot.get('warnings',[])
        result['workspace']=workspace.get('name');result['vault']=str(vault)
        atomic_bytes(home/'数据/最近同步.json',json.dumps(result,ensure_ascii=False,indent=2).encode('utf-8'))
        counts=result['counts']
        log('同步结束：新增 {added}，更新 {updated}，未变 {unchanged}，保留 {retained}，待比较 {conflicts}'.format(**counts))
        for warning in result['warnings']:log('范围提示：'+warning)
        if counts['conflicts']:log('本地修改已保留，云端新版请在工具目录的“冲突”里比较')
        return result


def load_settings(home=HOME):
    path=home/'同步工具设置.json'
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError):
        return {}


def save_settings(data,home=HOME):
    atomic_bytes(home/'同步工具设置.json',json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8'))
