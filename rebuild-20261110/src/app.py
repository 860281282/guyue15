import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import sys
import threading
import traceback
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from notion_connection import Auth, MCP, SingleRun, SyncError, atomic_bytes, metadata
from sync_engine import baseline, collect, discover, discover_roots, discover_children, render, apply_update, localize_media, target_path, repair_stage
from md_fix import load_index
from layout import clean_stage,NOTE_FOLDERS
import journal

import home as syshome
HOME=syshome.home()
RESOURCE=Path(getattr(sys,'_MEIPASS',HOME))
DEFAULT_VAULT=HOME.parent if HOME.name=='.同步工具' else HOME/'知识库'

def now_id():return datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)

def console_log(text):
    stream=sys.stdout
    if stream is None:return
    try:
        stream.write(str(text)+'\n')
    except UnicodeEncodeError:
        encoding=getattr(stream,'encoding',None) or 'utf-8'
        stream.write((str(text)+'\n').encode(encoding,'backslashreplace').decode(encoding))
    stream.flush()

def record_failure(mode,exc,home=HOME,vault=None,session=None):
    # 只写结构化位置和脱敏后的文本；令牌、授权码一律不会落盘。
    trace=journal.sanitize(''.join(traceback.format_exception(type(exc),exc,exc.__traceback__)))
    report={'operation':mode,'type':type(exc).__name__,'errno':getattr(exc,'errno',None),
            'winerror':getattr(exc,'winerror',None),
            'service_code':getattr(exc,'code',None),'service_operation':getattr(exc,'operation',None),
            'version':journal.VERSION,'vault':str(vault) if vault else None,
            'message':journal.sanitize(str(exc))[:400],'traceback':trace[-8000:],
            'log':str(session.path) if session and session.path else None,
            'frames':[{'file':Path(frame.filename).name,'line':frame.lineno,'function':frame.name}
                      for frame in traceback.extract_tb(exc.__traceback__)]}
    destination=home/'数据/最近错误.json'
    try:atomic_bytes(destination,json.dumps(report,ensure_ascii=False,indent=2).encode('utf-8'))
    except OSError:return None
    return destination

def _finish(code):
    try:
        sys.stdout.flush();sys.stderr.flush()
    except Exception:
        pass
    for stream in (sys.stdout,sys.stderr):
        try:
            stream.close()
        except Exception:
            pass
    os._exit(code)

def permission_hint(exc):
    if isinstance(exc,PermissionError):
        return '。写入被系统或安全软件拦截：请把本程序加入杀毒软件信任区（火绒、360、Defender），或在弹窗中允许后重试'
    return ''

def sync(vault,log,cancel,home=HOME,scope=None,repair=False):
    vault=Path(vault).resolve()
    if not (vault/'.obsidian').is_dir():raise SyncError('请选择已有的知识库文件夹，里面应有 .obsidian 文件夹')
    cache=home/'缓存';cache.mkdir(parents=True,exist_ok=True)
    with SingleRun(),tempfile.TemporaryDirectory(prefix='work-',dir=cache) as scratch:
        client=MCP(Auth(),cancel);client.start()
        account=client.call('get-self')
        workspace=account.get('workspace',{})
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
        backups=vault/'.同步工具/备份'
        if clean and backups.is_dir():
            runs=sorted((p for p in backups.iterdir() if p.is_dir()),key=lambda p:p.name,reverse=True)
            for old in runs[1:]:
                if old.is_symlink() or old.resolve().parent!=backups.resolve():raise SyncError('备份路径异常，停止清理')
                shutil.rmtree(old)
        counts=result['counts']
        log('同步结束：新增 {added}，更新 {updated}，未变 {unchanged}，保留 {retained}，待比较 {conflicts}'.format(**counts))
        for warning in result['warnings']:log('范围提示：'+warning)
        if counts['conflicts']:log('本地修改已保留，云端新版请在工具目录的“冲突”里比较')
        return result

def sync_done_detail(added,updated):
    """同步成功后的说明文字：有改动就报数量，没改动就说已是最新。"""
    changed=int(added or 0)+int(updated or 0)
    if changed:return '本次写入 %d 篇笔记；本地修改过的笔记不会被覆盖' % changed
    return '知识库已是最新；本地修改过的笔记不会被覆盖'


def gui(smoke=False):
    BG='#f8f7f3';CARD='#ffffff';LINE='#e4e1d8';TEXT='#233331';MUTED='#6d7b78'
    ACCENT='#245d51';ACCENT_DARK='#1a493f';ERR='#a4362c';WARN='#8a6a12';OK='#1f6b4a'
    root=tk.Tk();root.title('Notion → Obsidian 一键同步 · '+journal.VERSION)
    root.geometry('880x720');root.minsize(760,620)
    if smoke:root.withdraw();root.after(350,root.destroy)
    session=journal.Journal(HOME,'窗口版')
    journal.install_excepthook(lambda mode: journal.Journal(HOME,mode))
    root.configure(bg=BG)
    style=ttk.Style();style.theme_use('clam')
    style.configure('TFrame',background=BG)
    style.configure('TLabel',background=BG,foreground=TEXT,font=('Microsoft YaHei UI',10))
    style.configure('Card.TFrame',background=CARD)
    style.configure('Card.TLabel',background=CARD,foreground=TEXT,font=('Microsoft YaHei UI',10))
    style.configure('Muted.TLabel',background=BG,foreground=MUTED,font=('Microsoft YaHei UI',9))
    style.configure('CardMuted.TLabel',background=CARD,foreground=MUTED,font=('Microsoft YaHei UI',9))
    style.configure('Title.TLabel',background=BG,foreground=TEXT,font=('Microsoft YaHei UI',19,'bold'))
    style.configure('TButton',font=('Microsoft YaHei UI',10),padding=(14,9))
    style.configure('Accent.TButton',background=ACCENT,foreground='white',padding=(22,11),font=('Microsoft YaHei UI',11,'bold'))
    style.map('Accent.TButton',background=[('active',ACCENT_DARK),('disabled','#a8bdb8')])
    style.configure('TEntry',padding=5)
    style.configure('Small.TButton',font=('Microsoft YaHei UI',10),padding=(10,7))
    style.configure('TCheckbutton',background=BG,foreground=MUTED,font=('Microsoft YaHei UI',9))
    style.configure('Card.TCheckbutton',background=CARD,foreground=MUTED,font=('Microsoft YaHei UI',9))
    style.configure('Treeview',rowheight=26,font=('Microsoft YaHei UI',10),fieldbackground=CARD,background=CARD)
    style.configure('Horizontal.TProgressbar',background=ACCENT,troughcolor='#e7e4db',borderwidth=0)
    style.configure('Vertical.TScrollbar',background='#e7e4db',troughcolor=CARD,arrowsize=14,borderwidth=0)

    frame=ttk.Frame(root,padding=(24,20,24,14));frame.pack(fill='both',expand=True)
    head=ttk.Frame(frame);head.pack(fill='x')
    ttk.Label(head,text='Notion → Obsidian 同步',style='Title.TLabel').pack(side='left')
    tk.Label(head,text='版本 '+journal.VERSION,bg='#eaf0ec',fg=ACCENT,
             font=('Microsoft YaHei UI',9),padx=9,pady=3).pack(side='right',pady=(8,0))
    ttk.Label(frame,text='单向更新 · 保留本地修改 · 自动备份旧版',style='Muted.TLabel').pack(anchor='w',pady=(6,14))

    cfgpath=HOME/'同步工具设置.json'
    try:cfg=json.loads(cfgpath.read_text(encoding='utf-8'))
    except (OSError,ValueError):cfg={}
    configured=cfg.get('vault','..' if HOME.name=='.同步工具' else '知识库')
    vault=tk.StringVar(value=str((HOME/configured).resolve()))
    saved=cfg.get('scope') if isinstance(cfg.get('scope'),dict) else {}
    scope_state={'items':[item for item in (saved.get('items') or []) if isinstance(item,dict) and item.get('url')]}

    card=tk.Frame(frame,bg=CARD,highlightbackground=LINE,highlightthickness=1)
    card.pack(fill='x')
    inner=ttk.Frame(card,style='Card.TFrame',padding=(18,14,18,16));inner.pack(fill='both',expand=True)
    statusrow=ttk.Frame(inner,style='Card.TFrame');statusrow.pack(fill='x')
    dot=tk.Label(statusrow,text='●',bg=CARD,fg=MUTED,font=('Microsoft YaHei UI',11))
    dot.pack(side='left',padx=(0,8))
    status_text=tk.StringVar(value='未连接')
    ttk.Label(statusrow,textvariable=status_text,style='Card.TLabel',
              font=('Microsoft YaHei UI',11,'bold')).pack(side='left')
    progress=ttk.Progressbar(statusrow,mode='indeterminate',length=110,style='Horizontal.TProgressbar')
    detail=tk.StringVar(value='先连接 Notion，或直接同步已连接的工作区')
    ttk.Label(inner,textvariable=detail,style='CardMuted.TLabel',wraplength=770).pack(anchor='w',pady=(6,10))
    tk.Frame(inner,height=1,bg=LINE).pack(fill='x',pady=(0,12))

    ttk.Label(inner,text='Obsidian 知识库',style='CardMuted.TLabel').pack(anchor='w')
    row=ttk.Frame(inner,style='Card.TFrame');row.pack(fill='x',pady=(6,12))
    entry=ttk.Entry(row,textvariable=vault,font=('Microsoft YaHei UI',10))
    entry.pack(side='left',fill='x',expand=True,ipady=6)
    def choose():
        path=filedialog.askdirectory(title='选择已有的 Obsidian 知识库',initialdir=vault.get())
        if path:vault.set(path)
    choose_button=ttk.Button(row,text='选择文件夹',command=choose);choose_button.pack(side='right',padx=(10,0))

    scope_label=tk.StringVar()
    def describe_scope():
        items=scope_state['items']
        if not items:
            scope_label.set('同步范围：全部大类（每次全量扫描）');return
        names=[item.get('title') or '未命名' for item in items]
        more='' if len(names)<=2 else ' 等 {0} 个'.format(len(names))
        scope_label.set('同步范围：'+'、'.join(names[:2])+more)
    describe_scope()
    scoperow=ttk.Frame(inner,style='Card.TFrame');scoperow.pack(fill='x')
    ttk.Label(scoperow,textvariable=scope_label,style='Card.TLabel').pack(side='left')
    scope_button=ttk.Button(scoperow,text='选择同步范围',command=lambda:open_scope());scope_button.pack(side='right')

    summary=tk.StringVar(value='')
    summary_label=tk.Label(inner,textvariable=summary,bg='#edf4f0',fg=OK,anchor='w',padx=12,pady=9,
                           font=('Microsoft YaHei UI',10,'bold'))

    buttons=ttk.Frame(frame);buttons.pack(fill='x',pady=(14,0))
    tools=ttk.Frame(frame);tools.pack(side='bottom',fill='x',pady=(10,0))

    outbox=tk.Frame(frame,bg=CARD,highlightbackground=LINE,highlightthickness=1)
    outbox.pack(fill='both',expand=True,pady=(14,0))
    outhead=tk.Frame(outbox,bg=CARD);outhead.pack(fill='x',padx=12,pady=(8,0))
    tk.Label(outhead,text='运行记录',bg=CARD,fg=MUTED,font=('Microsoft YaHei UI',9)).pack(side='left')
    autoscroll=tk.BooleanVar(value=True)
    ttk.Checkbutton(outhead,text='自动滚动',variable=autoscroll,style='Card.TCheckbutton').pack(side='right')
    clear_label=tk.Label(outhead,text='清空',bg=CARD,fg=ACCENT,font=('Microsoft YaHei UI',9),cursor='hand2')
    clear_label.pack(side='right',padx=(0,14))
    outbody=tk.Frame(outbox,bg=CARD);outbody.pack(fill='both',expand=True,padx=(12,8),pady=(4,10))
    output=tk.Text(outbody,height=7,wrap='word',bg=CARD,fg=TEXT,relief='flat',
                   font=('Microsoft YaHei UI',10),padx=2,pady=2)
    outscroll=ttk.Scrollbar(outbody,command=output.yview,style='Vertical.TScrollbar')
    output.configure(yscrollcommand=outscroll.set)
    outscroll.pack(side='right',fill='y');output.pack(side='left',fill='both',expand=True)
    output.tag_configure('err',foreground=ERR)
    output.tag_configure('warn',foreground=WARN)
    output.tag_configure('ok',foreground=OK,font=('Microsoft YaHei UI',10,'bold'))
    output.configure(state='disabled')

    events=queue.Queue();cancel=threading.Event();worker=[None];last_counts={'added':0,'updated':0}
    def set_status(text,color=MUTED,note=None):
        status_text.set(text);dot.configure(fg=color)
        if note is not None:detail.set(note)
    def busy(on):
        if on:
            progress.pack(side='right');progress.start(12)
        else:
            progress.stop();progress.pack_forget()
    def append(text):
        if '失败' in text or '错误' in text or text.startswith('Traceback'):tag='err'
        elif '范围提示' in text or '跳过' in text:tag='warn'
        elif '同步结束' in text or text.startswith('已连接'):tag='ok'
        else:tag=''
        output.configure(state='normal')
        output.insert('end',text+'\n',tag)
        if autoscroll.get():output.see('end')
        output.configure(state='disabled')
    def clear_output():
        output.configure(state='normal');output.delete('1.0','end');output.configure(state='disabled')
    clear_label.bind('<Button-1>',lambda event:clear_output())
    def log(text):
        try:session.write(text)
        except Exception:pass
        events.put(('log',text))
    def save_setting(**extra):
        p=Path(vault.get()).resolve()
        try:path=p.relative_to(HOME).as_posix()
        except ValueError:path=str(p)
        merged={}
        try:merged=json.loads(cfgpath.read_text(encoding='utf-8'))
        except (OSError,ValueError):merged={}
        if not isinstance(merged,dict):merged={}
        merged['vault']=path
        merged.update(extra)
        atomic_bytes(cfgpath,json.dumps(merged,ensure_ascii=False,indent=2).encode('utf-8'))
    def finish():
        for button in (connect_button,sync_button,choose_button,scope_button,open_button):
            button.configure(state='normal')
        entry.configure(state='normal');cancel_button.configure(state='disabled');busy(False)
    def start(mode):
        if worker[0] and worker[0].is_alive():return
        cancel.clear()
        selected_vault=vault.get()
        for button in (connect_button,sync_button,choose_button,scope_button,open_button):
            button.configure(state='disabled')
        entry.configure(state='disabled');cancel_button.configure(state='normal')
        summary.set('');summary_label.pack_forget()
        if mode=='sync':
            set_status('正在同步',ACCENT,'正在读取 Notion 页面，读完之后才会改动本地笔记')
        else:
            set_status('正在连接 Notion',ACCENT,'浏览器会打开官方授权页，登录授权后回到本窗口')
        busy(True)
        def run():
            try:
                if mode=='connect':
                    with SingleRun():
                        auth=Auth(allow_reset=True);auth.connect(log,cancel)
                        account=client.call('get-self');workspace=account.get('workspace',{}) or {}
                        saved=load_settings(home=home).get('workspace_id')
                        if saved and workspace.get('id')!=saved:
                            raise SyncError('已连接其他工作区，请重新连接并选择原来那个工作区')
                        log('已连接 '+str(workspace.get('name') or 'Notion')+'，现在可以点击“立即同步”')
                else:
                    result=sync(selected_vault,log,cancel,scope=scope_state['items'] or None)
                    events.put(('result',result))
                events.put(('success',mode))
            except SyncError as exc:
                record_failure(mode,exc,vault=selected_vault,session=session)
                try:session.error(exc,'同步失败')
                except Exception:pass
                if '已取消' in str(exc):
                    events.put(('cancelled',''))
                else:
                    events.put(('error',str(exc)+'（详情已写入日志，可点“查看日志”）'))
            except Exception as exc:
                record_failure(mode,exc,vault=selected_vault,session=session)
                try:session.error(exc,'未能完成操作')
                except Exception:pass
                code=getattr(exc,'winerror',None) or getattr(exc,'errno',None)
                detail_text='，错误码 '+str(code) if code is not None else ''
                events.put(('error','未能完成操作：'+type(exc).__name__+detail_text+permission_hint(exc)
                            +'。已写入日志，请点“导出诊断包”发给我'))
            finally:events.put(('done',''))
        worker[0]=threading.Thread(target=run,daemon=True);worker[0].start()
    connect_button=ttk.Button(buttons,text='连接 Notion',command=lambda:start('connect'));connect_button.pack(side='left')
    sync_button=ttk.Button(buttons,text='立即同步',style='Accent.TButton',command=lambda:start('sync'))
    sync_button.pack(side='left',padx=12)
    def open_vault():
        if Path(vault.get()).is_dir():os.startfile(vault.get())
        else:messagebox.showinfo('知识库未找到','请先选择知识库所在的文件夹')
    open_button=ttk.Button(buttons,text='打开知识库',command=open_vault);open_button.pack(side='left')
    cancel_button=ttk.Button(buttons,text='取消',state='disabled',command=cancel.set);cancel_button.pack(side='right')

    log_window=[None]
    def export_package(parent=None):
        suggestion='Notion同步诊断-'+journal.now_id()+'.zip'
        target=filedialog.asksaveasfilename(parent=parent,title='保存诊断包',initialfile=suggestion,
                                           defaultextension='.zip',filetypes=[('压缩包','*.zip')])
        if not target:return
        try:path=session.export(target)
        except (OSError,ValueError) as exc:
            messagebox.showerror('导出失败','无法写入诊断包：'+str(exc),parent=parent);return
        messagebox.showinfo('诊断包已保存','已保存到：\n'+str(path)+'\n\n把它发给工具作者就能定位问题。',parent=parent)
    def copy_all():
        try:
            root.clipboard_clear();root.clipboard_append('Notion 一键同步 '+journal.VERSION+'\n'+session.text())
        except tk.TclError:return
        messagebox.showinfo('已复制','最近日志已复制到剪贴板，可以直接粘贴给别人')
    def center(window,width,height):
        root.update_idletasks()
        x=root.winfo_rootx()+max(0,(root.winfo_width()-width)//2)
        y=root.winfo_rooty()+max(0,(root.winfo_height()-height)//3)
        window.geometry('{0}x{1}+{2}+{3}'.format(width,height,max(x,30),max(y,30)))
    def show_log():
        window=log_window[0]
        if window and window.winfo_exists():window.lift();return
        window=tk.Toplevel(root);window.title('运行日志 · '+journal.VERSION);center(window,960,640)
        window.configure(bg=BG);log_window[0]=window
        bar=ttk.Frame(window,padding=(14,12,14,6));bar.pack(fill='x')
        ttk.Label(bar,text='日志文件：'+str(session.path),style='Muted.TLabel').pack(side='left')
        ttk.Button(bar,text='关闭',command=window.destroy).pack(side='right')
        ttk.Button(bar,text='导出诊断包',command=lambda:export_package(window)).pack(side='right',padx=8)
        ttk.Button(bar,text='复制全部',command=copy_all).pack(side='right')
        only_err=tk.BooleanVar(value=False)
        opt=ttk.Frame(window,padding=(14,0,14,8));opt.pack(fill='x')
        ttk.Checkbutton(opt,text='只看错误与跳过',variable=only_err,style='TCheckbutton').pack(side='left')
        viewbox=tk.Frame(window,bg=CARD,highlightbackground=LINE,highlightthickness=1);viewbox.pack(fill='both',expand=True,padx=14,pady=(0,14))
        view=tk.Text(viewbox,wrap='none',bg=CARD,fg=TEXT,relief='flat',font=('Consolas',9),padx=10,pady=8)
        scroll=ttk.Scrollbar(viewbox,command=view.yview,style='Vertical.TScrollbar')
        view.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');view.pack(side='left',fill='both',expand=True)
        view.tag_configure('err',foreground=ERR)
        view.tag_configure('warn',foreground=WARN)
        view.configure(state='disabled')
        state={'sig':None}
        def render():
            if not window.winfo_exists():return
            text=session.text();sig=(text,only_err.get())
            if sig!=state['sig']:
                state['sig']=sig
                view.configure(state='normal');view.delete('1.0','end')
                for line in text.splitlines():
                    if only_err.get() and not any(k in line for k in ('失败','错误','Traceback','跳过')):
                        continue
                    if '失败' in line or '错误' in line or line.startswith('Traceback'):tag='err'
                    elif '跳过' in line:tag='warn'
                    else:tag=''
                    view.insert('end',line+'\n',tag)
                view.configure(state='disabled');view.see('end')
            window.after(900,render)
        render()
    def open_scope():
        window=tk.Toplevel(root);window.title('选择同步范围 · '+journal.VERSION);center(window,820,620)
        window.configure(bg=BG)
        top=ttk.Frame(window,padding=(16,14,16,4));top.pack(fill='x')
        status=tk.StringVar(value='正在扫描大类与小类，请稍候…')
        statusrow2=ttk.Frame(top);statusrow2.pack(fill='x')
        ttk.Label(statusrow2,textvariable=status,font=('Microsoft YaHei UI',10,'bold')).pack(side='left')
        scanbar=ttk.Progressbar(statusrow2,mode='indeterminate',length=96,style='Horizontal.TProgressbar')
        scanbar.pack(side='right')
        scanbar.start(14)
        count=tk.StringVar(value='')
        ttk.Label(statusrow2,textvariable=count,style='Muted.TLabel').pack(side='right',padx=(0,10))
        rescan_button=ttk.Button(statusrow2,text='重新扫描',style='Small.TButton',command=lambda:load())
        rescan_button.pack(side='right',padx=(0,12))
        ttk.Label(top,text='点行首的方框勾选；点整行展开或收起（只有展开的那一类才会被扫描）。勾选大类等于连它的子类一起同步。',
                  style='Muted.TLabel',wraplength=770).pack(anchor='w',pady=(4,8))
        searchrow=ttk.Frame(top);searchrow.pack(fill='x')
        query=tk.StringVar()
        search=ttk.Entry(searchrow,textvariable=query);search.pack(side='left',fill='x',expand=True,ipady=5)
        ttk.Label(searchrow,text='搜索类别',style='Muted.TLabel').pack(side='left',padx=(10,0))
        footer=ttk.Frame(window,padding=(16,10,16,12));footer.pack(side='bottom',fill='x')
        body=ttk.Frame(window,padding=(16,4,16,6));body.pack(fill='both',expand=True)
        style.configure('Scope.Treeview',font=('Microsoft YaHei UI',12),rowheight=34)
        big_tri=False
        try:
            blank=tk.PhotoImage(width=1,height=1)
            style.element_create('Blank.Treeitem.indicator','image',blank)
            style.layout('Scope.Treeview.Item',[
                ('Treeitem.padding',{'sticky':'nswe','children':[
                    ('Blank.Treeitem.indicator',{'side':'left','sticky':''}),
                    ('Treeitem.image',{'side':'left','sticky':''}),
                    ('Treeitem.focus',{'side':'left','sticky':'','children':[
                        ('Treeitem.text',{'side':'left','sticky':''})]})]})])
            big_tri=True
        except tk.TclError:
            big_tri=False
        tree=ttk.Treeview(body,show='tree',selectmode='none',style='Scope.Treeview')
        scroll=ttk.Scrollbar(body,command=tree.yview,style='Vertical.TScrollbar')
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right',fill='y');tree.pack(side='left',fill='both',expand=True)
        nodes={};checked={};children={};opened=set();loading=set();roots=[]
        def marker(iid):
            if not big_tri or iid.startswith('C') or nodes[iid].get('kind')=='database':return '    '
            return ('▼  ' if iid in opened else '▶  ')
        def label(iid):
            return marker(iid)+('☑  ' if checked.get(iid) else '☐  ')+nodes[iid]['title']
        def update_count():
            chosen=sum(1 for iid in nodes if checked.get(iid))
            count.set('已勾选 {0} 个 · 已展开 {1}/{2}'.format(chosen,len(children),len(roots)))
        def paint():
            for iid in list(nodes):
                if tree.exists(iid):tree.item(iid,text=label(iid))
            update_count()
        def toggle(iid,value):
            checked[iid]=value
            for child in tree.get_children(iid):toggle(child,value)
        def on_click(event):
            iid=tree.identify_row(event.y)
            if not iid or iid not in nodes:return
            tree.focus(iid)
            if 'indicator' in str(tree.identify_element(event.x,event.y)):
                expand_node(iid);return 'break'
            box=tree.bbox(iid)
            rel=(event.x-box[0]) if box else 9999
            triw=26 if big_tri else 0
            if box and rel<=triw:
                expand_node(iid);return 'break'
            boxed=bool(box) and triw<rel<=triw+40
            if not boxed:
                expand_node(iid);return 'break'
            value=not checked.get(iid)
            toggle(iid,value)
            parent=tree.parent(iid)
            while not value and parent:
                checked[parent]=False;parent=tree.parent(parent)
            paint()
            return 'break'
        def expand_node(iid):
            if not iid or iid not in nodes or iid.startswith('C'):return
            node=nodes[iid]
            if tree.item(iid).get('open'):
                opened.discard(iid);tree.item(iid,open=False);paint();return
            opened.add(iid)
            if node['kind']!='database' and iid not in children and iid not in loading:
                loading.add(iid)
                status.set('正在展开：'+node['title'])
                if tree.exists(iid+':pending'):tree.item(iid+':pending',text='（正在扫描…）')
                queue_put(('children',node))
            tree.item(iid,open=True)
            paint()
        tree.bind('<Button-1>',on_click)
        tree.bind('<Double-1>',on_click)
        tree.bind('<<TreeviewClose>>',lambda event:opened.discard(tree.focus()))
        def render(keyword=''):
            word=(keyword or '').strip().lower()
            for iid in tree.get_children(''):tree.delete(iid)
            for node in roots:
                riid='R'+node['id'];kids=children.get(riid)
                hit=(not word) or word in node['title'].lower()
                if kids is None:
                    if word and not hit:continue
                    nodes[riid]=node
                    tree.insert('','end',iid=riid,text=label(riid),open=riid in opened)
                    if node['kind']!='database':
                        tree.insert(riid,'end',iid=riid+':pending',
                                    text='（正在扫描…）' if riid in loading else '（展开这一项才会扫描小类）')
                    continue
                shown=list(kids) if hit else [child for child in kids if word in child['title'].lower()]
                if word and not hit and not shown:continue
                nodes[riid]=node
                tree.insert('','end',iid=riid,text=label(riid),open=(riid in opened) or bool(word and shown))
                for child in shown:
                    cid='C'+node['id']+'-'+child['id'];nodes[cid]=child
                    tree.insert(riid,'end',iid=cid,text=label(cid))
            paint()
            status_text=status.get()
            if not roots and not status_text.startswith('扫描失败'):status.set('正在扫描第一层：列出所有大类…')
        def queue_put(task):
            scanbar.pack(side='right');scanbar.start(14)
            worker['queue'].put(task)
            start_worker()
        def on_roots(items):
            roots[:]=items
            loading.clear()
            chosen={item.get('id') for item in scope_state['items']}
            for node in roots:
                checked['R'+node['id']]=node['id'] in chosen
            render(query.get())
            rescan_button.configure(state='normal')
            if not items:status.set('这个工作区里没有可读取的大类')
            else:status.set('已找到 {0} 个大类；点开左侧三角只扫描你关心的那一类'.format(len(items)))
        def on_children(node):
            riid='R'+node['id']
            loading.discard(riid);opened.add(riid)
            children[riid]=list(node.get('children') or [])
            for child in children[riid]:
                cid='C'+node['id']+'-'+child['id'];nodes[cid]=child
                if checked.get(riid):checked[cid]=True
            render(query.get())
            if not children[riid]:status.set('{0} 下面没有小类，勾选它就等于同步整块'.format(node['title']))
            else:status.set('{0}：{1} 个小类'.format(node['title'],len(children[riid])))
        def on_task_error(node,message):
            if node is None:
                status.set('扫描失败：'+message);rescan_button.configure(state='normal')
            else:
                loading.discard('R'+node['id'])
                status.set('展开失败：{0}（{1}）'.format(node['title'],message))
            render(query.get())
        worker={'thread':None,'queue':queue.Queue(),'event':threading.Event()}
        def start_worker():
            if worker['thread'] and worker['thread'].is_alive():return
            ev=worker['event']
            def run():
                try:
                    with SingleRun():
                        client=MCP(Auth(),ev);client.start()
                        while not ev.is_set():
                            task=worker['queue'].get()
                            if task is None or ev.is_set():break
                            kind,node=task
                            try:
                                if kind=='roots':
                                    items=discover_roots(client,lambda text:window.after(0,status.set,text),ev)
                                    window.after(0,lambda items=items:on_roots(items))
                                else:
                                    discover_children(client,node,lambda text:window.after(0,status.set,text),ev)
                                    window.after(0,lambda node=node:on_children(node))
                            except SyncError as exc:
                                window.after(0,lambda node=node,msg=str(exc):on_task_error(node,msg))
                            except Exception as exc:
                                try:session.error(exc,'扫描同步范围失败')
                                except Exception:pass
                                window.after(0,lambda node=node,name=type(exc).__name__:on_task_error(node,name))
                except SyncError as exc:
                    window.after(0,lambda msg=str(exc):on_task_error(None,msg))
                except Exception as exc:
                    try:session.error(exc,'扫描同步范围失败')
                    except Exception:pass
                    window.after(0,lambda name=type(exc).__name__:on_task_error(None,name+'（日志已记录，可导出诊断包）'))
                finally:
                    window.after(0,lambda:(scanbar.stop(),scanbar.pack_forget()))
            worker['thread']=threading.Thread(target=run,daemon=True);worker['thread'].start()
        def load():
            children.clear();loading.clear();opened.clear();nodes.clear();roots[:]=[]
            tree.delete(*tree.get_children())
            rescan_button.configure(state='disabled')
            status.set('正在扫描第一层：列出所有大类…')
            queue_put(('roots',None))
        def on_open(event):
            iid=tree.focus()
            if not iid or iid not in nodes or iid.startswith('C'):return
            node=nodes[iid]
            opened.add(iid)
            if node['kind']=='database':
                children.setdefault(iid,[]);return
            if iid in children or iid in loading:return
            loading.add(iid)
            status.set('正在展开：'+node['title'])
            tree.item(iid+':pending',text='（正在扫描…）') if tree.exists(iid+':pending') else None
            queue_put(('children',node))
        tree.bind('<<TreeviewOpen>>',on_open)
        def select_all(value):
            for node in roots:checked['R'+node['id']]=value
            for iid in list(nodes):checked[iid]=value
            paint()
        def expand_all(value):
            for node in roots:
                iid='R'+node['id']
                if not value:opened.discard(iid);continue
                opened.add(iid)
                if node['kind']!='database' and iid not in children and iid not in loading:
                    loading.add(iid);queue_put(('children',node))
            render(query.get())
        def on_query(*_):render(query.get())
        query.trace_add('write',on_query)
        def save():
            items=[];seen=set()
            for iid,node in list(nodes.items()):
                if not tree.exists(iid):continue
                if not checked.get(iid):continue
                parent=tree.parent(iid)
                if parent and checked.get(parent):continue
                if node['id'] in seen:continue
                seen.add(node['id'])
                items.append({'id':node['id'],'url':node['url'],'title':node['title'],'kind':node['kind']})
            if not items and not messagebox.askyesno('恢复全量同步','没有勾选任何类别。保存后将恢复为“同步全部大类”。继续吗？',parent=window):
                return
            scope_state['items']=items
            save_setting(scope={'items':items,'updated':journal.now_id()})
            describe_scope()
            log('同步范围已保存：'+('全部大类' if not items else '已勾选 {0} 个类别，其余区域不处理'.format(len(items))))
            window.destroy()
        # 用 grid 摆底部按钮：右侧的“保存/关闭”先占列，窗口再窄也不会被挤出可视区。
        footer.columnconfigure(4,weight=1)
        ttk.Button(footer,text='全选',style='Small.TButton',command=lambda:select_all(True)).grid(row=0,column=0,sticky='w')
        ttk.Button(footer,text='全不选',style='Small.TButton',command=lambda:select_all(False)).grid(row=0,column=1,sticky='w',padx=(8,0))
        ttk.Button(footer,text='展开',style='Small.TButton',command=lambda:expand_all(True)).grid(row=0,column=2,sticky='w',padx=(8,0))
        ttk.Button(footer,text='折叠',style='Small.TButton',command=lambda:expand_all(False)).grid(row=0,column=3,sticky='w',padx=(8,0))
        ttk.Button(footer,text='关闭',style='Small.TButton',command=window.destroy).grid(row=0,column=5,sticky='e',padx=(8,0))
        ttk.Button(footer,text='保存',style='Accent.TButton',command=save).grid(row=0,column=6,sticky='e')
        def on_close():
            worker['event'].set()
            worker['queue'].put(None)
            window.destroy()
        window.transient(root);window.bind('<Escape>',lambda event:on_close())
        window.protocol('WM_DELETE_WINDOW',on_close)
        search.focus_set();load()
    ttk.Button(tools,text='查看日志',command=lambda:show_log()).pack(side='left')
    ttk.Button(tools,text='导出诊断包',command=lambda:export_package(root)).pack(side='left',padx=8)
    ttk.Button(tools,text='复制诊断信息',command=lambda:copy_all()).pack(side='left')
    ttk.Label(tools,text='F5 立即同步 · Ctrl+L 查看日志',style='Muted.TLabel').pack(side='right')
    def poll():
        while True:
            try:kind,text=events.get_nowait()
            except queue.Empty:break
            if kind=='log':
                append(text)
                if text.startswith('正在读取：'):detail.set(text)
                elif text.startswith('开始读取'):detail.set('正在读取 Notion 页面，读完之后才会改动本地笔记')
                elif text.startswith('读取完成'):detail.set('正在比较本地内容并写入更新，请勿拔出硬盘')
                elif text.startswith('同步范围') or text.startswith('附件'):detail.set(text)
            elif kind=='error':
                append(text);set_status('没有完成',ERR,text)
            elif kind=='cancelled':
                set_status('已取消',WARN,'本次没有改动知识库')
            elif kind=='result':
                counts=text.get('counts') or {}
                last_counts['added']=counts.get('added',0);last_counts['updated']=counts.get('updated',0)
                summary.set('本次：新增 {added} · 更新 {updated} · 未变 {unchanged} · 冲突 {conflicts} · 保留 {retained}'.format(
                    added=counts.get('added',0),updated=counts.get('updated',0),unchanged=counts.get('unchanged',0),
                    conflicts=counts.get('conflicts',0),retained=counts.get('retained',0)))
                summary_label.pack(fill='x',pady=(12,0))
            elif kind=='success':
                save_setting()
                if text=='sync':set_status('同步完成',OK,sync_done_detail(last_counts.get('added',0),last_counts.get('updated',0)))
                else:set_status('已连接',OK,'可以点击“立即同步”了')
            elif kind=='done':finish()
        root.after(120,poll)
    def close():
        if worker[0] and worker[0].is_alive():
            cancel.set();log('正在结束当前操作，请等待窗口恢复后再关闭，以保留授权和更新记录');return
        try:session.write('窗口关闭');session.close()
        except Exception:pass
        root.destroy()
    root.protocol('WM_DELETE_WINDOW',close)
    root.bind('<F5>',lambda event:start('sync'))
    root.bind('<Control-l>',lambda event:show_log())
    root.bind('<Control-L>',lambda event:show_log())
    log('准备就绪，请连接 Notion，或直接同步已连接的工作区')
    poll();root.mainloop()

def load_settings(home=HOME):
    path=home/'同步工具设置.json'
    try:
        data=json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError):
        return {}

def save_settings(data,home=HOME):
    atomic_bytes(home/'同步工具设置.json',json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8'))

def main():
    journal.install_excepthook(lambda mode: journal.Journal(HOME,mode))
    parser=argparse.ArgumentParser()
    parser.add_argument('--sync',action='store_true',help='仅执行同步，不打开窗口')
    parser.add_argument('--vault',type=Path,default=DEFAULT_VAULT)
    parser.add_argument('--probe',action='store_true',help='检查官方登录服务，不读取或修改笔记')
    parser.add_argument('--self-check',action='store_true',help='检查程序依赖与窗口，不连接个人账号')
    parser.add_argument('--check-login',action='store_true',help='检查首次连接直到网页打开之前，不读取个人页面')
    parser.add_argument('--export-diagnostics',type=Path,default=None,help='把日志与环境信息打包到指定 zip')
    parser.add_argument('--list-categories',action='store_true',help='只列出第一层的大类及其编号，不读取笔记内容')
    parser.add_argument('--parent',default='',help='配合 --list-categories：只列出该类别编号下面的小类')
    parser.add_argument('--scope',default='',help='只同步这些类别编号（逗号分隔），留空则用界面里保存的范围')
    parser.add_argument('--clear-scope',action='store_true',help='清除已保存的同步范围，恢复为同步全部大类')
    args=parser.parse_args()
    if args.list_categories:
        session=journal.Journal(HOME,'列出类别',vault=args.vault)
        try:
            with SingleRun():
                client=MCP(Auth(),threading.Event());client.start()
                if args.parent:
                    pid=args.parent.strip()
                    if not re.fullmatch(r'[a-f0-9]{32}',pid):raise SyncError('类别编号不正确：'+pid)
                    node={'id':pid,'url':'https://app.notion.com/p/'+pid,'title':pid,'kind':'page','children':[],'loaded':False}
                    kids=discover_children(client,node,lambda text:console_log(text),threading.Event())
                    console_log('类别 '+pid+' 下的小类：')
                    for kid in kids:console_log('    - '+kid['title']+'  '+kid['id'])
                    if not kids:console_log('    （没有小类，勾选它等于同步整块）')
                else:
                    tree=discover_roots(client,lambda text:console_log(text),threading.Event())
                    console_log('共 {0} 个大类（只读了第一层）：'.format(len(tree)))
                    for node in tree:
                        console_log(('* ' if node['kind']!='database' else '# ')+node['title']+'  '+node['id'])
                    console_log('查某个大类下面有什么：--list-categories --parent <编号>')
        except SyncError as exc:
            session.error(exc,'列出类别失败');console_log(str(exc));session.close();_finish(2)
        except Exception as exc:
            session.error(exc,'列出类别失败');console_log(type(exc).__name__+': '+str(exc));session.close();_finish(3)
        session.close();console_log('日志：'+str(session.path));_finish(0)
    if args.export_diagnostics:
        session=journal.Journal(HOME,'导出诊断包',vault=args.vault)
        try:
            path=session.export(args.export_diagnostics)
        except Exception as exc:
            session.error(exc,'导出失败');session.close()
            console_log(type(exc).__name__+': '+str(exc)+permission_hint(exc))
            console_log('日志：'+str(session.path));_finish(4)
        session.close()
        console_log('诊断包：'+str(path));_finish(0)
    if args.check_login:
        cancel=threading.Event();ready=[False];events=[]
        def browser_ready(url):ready[0]=True;cancel.set();return True
        try:
            with SingleRun():Auth(allow_reset=True).connect(events.append,cancel,browser_open=browser_ready)
        except SyncError as exc:
            if not ready[0]:
                record_failure('check-login',exc)
                atomic_bytes(HOME/'登录连接检查.json',json.dumps({'ok':False,'type':type(exc).__name__},ensure_ascii=False).encode('utf-8'))
                sys.exit(2)
        except Exception as exc:
            record_failure('check-login',exc)
            atomic_bytes(HOME/'登录连接检查.json',json.dumps({'ok':False,'type':type(exc).__name__},ensure_ascii=False).encode('utf-8'))
            sys.exit(2)
        atomic_bytes(HOME/'登录连接检查.json',json.dumps({'ok':ready[0],'prelogin_ready':ready[0],'browser_opened':False},ensure_ascii=False,indent=2).encode('utf-8'))
        return
    if args.self_check:
        import tempfile
        from notion_connection import CredentialStore
        try:
            with tempfile.TemporaryDirectory(dir=HOME) as temp:
                temp=Path(temp);pid='a'*32;dbid='b'*32;col='collection://'+'c'*32
                fixture={'pages':{
                    pid:{'id':pid,'title':'工具检查','url':'https://app.notion.com/p/'+pid,'metadata':{'type':'page'},
                         'text':'<properties>{"title":"工具检查"}</properties><content>正文\n\n$$\nx^2\n$$\n</content>'},
                    dbid:{'id':dbid,'title':'表格检查','url':'https://app.notion.com/p/'+dbid,'metadata':{'type':'database'},
                          'text':'<data-source url="'+col+'"/>'}},
                    'databases':{col:{'results':[{'url':'https://app.notion.com/p/'+pid,'标题':'工具检查'}]}},'schemas':{},'scope':'offline check'}
                stage=render(fixture,temp/'render',RESOURCE/'renderer.py',{'mapping':{}})
                if len(list(stage.rglob('*.csv')))!=1:raise RuntimeError('CSV check failed')
                store=CredentialStore(temp/'auth.dat');store.save({'test':'offline'});assert store.load()=={'test':'offline'}
                gui(smoke=True)
            report={'ok':True,'renderer':True,'csv':True,'encrypted_storage':True,'gui':True}
        except Exception as exc:report={'ok':False,'error_type':type(exc).__name__}
        atomic_bytes(HOME/'工具检查.json',json.dumps(report,ensure_ascii=False,indent=2).encode('utf-8'))
        _finish(0 if report['ok'] else 1)
    if args.probe:
        metadata();print('Notion 官方登录服务可连接');return
    if args.sync:
        session=journal.Journal(HOME,'命令行同步',vault=args.vault)
        def log_line(text):
            console_log(text)
            try:session.write(text)
            except Exception:pass
        code=0
        try:
            settings=load_settings()
            if args.clear_scope:
                settings.pop('scope',None);save_settings(settings)
                console_log('已清除保存的同步范围，下次同步全部大类')
            scope=None
            wanted=[value.strip() for value in args.scope.split(',') if value.strip()]
            if wanted:
                ids=[]
                for value in wanted:
                    pid=value.replace('-','').lower()
                    if not re.fullmatch(r'[a-f0-9]{32}',pid):raise SyncError('类别编号不正确：'+value)
                    ids.append(pid)
                scope=[{'id':item,'url':'https://app.notion.com/p/'+item,'title':'命令行指定','kind':'page'} for item in ids]
            else:
                saved=settings.get('scope') if isinstance(settings.get('scope'),dict) else {}
                scope=[item for item in (saved.get('items') or []) if isinstance(item,dict) and item.get('url')] or None
            if scope:console_log('同步范围：已勾选 {0} 个类别'.format(len(scope)))
            sync(args.vault,log_line,threading.Event(),scope=scope)
            console_log('日志：'+str(session.path))
        except KeyboardInterrupt:
            session.write('用户中断，本次未更新知识库')
            console_log('已中断，本次未更新知识库，日志：'+str(session.path));code=130
        except SyncError as exc:
            record_failure('sync-cli',exc,vault=args.vault,session=session)
            session.error(exc,'同步失败')
            console_log(str(exc));console_log('日志：'+str(session.path));code=2
        except Exception as exc:
            record_failure('sync-cli',exc,vault=args.vault,session=session)
            session.error(exc,'未能完成同步')
            console_log(type(exc).__name__+': '+str(exc)+permission_hint(exc));console_log('日志：'+str(session.path));code=3
        finally:session.close()
        _finish(code)
    else:gui()

if __name__=='__main__':main()
