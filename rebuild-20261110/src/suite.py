"""Notion 与 Obsidian 一键同步 —— 统一入口（导入 / 导出 / 排版修复）。

所有功能在同一个窗口里，共用一套日志和诊断包。
各功能的具体流程仍由 app.py / app_out.py / md_fix.py 提供，本文件只做界面与调度。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import time
import threading
import tkinter as tk
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, ttk

import app as A
import about
import app_out as O
import change_cache
import write_guard
import engine
import brand
import toolbox
import dpi
import journal
import md_fix as M
import path_engine as pe
import presets
import storage
import support
from scope_picker import ScopePicker
from notion_connection import Auth, MCP, SingleRun, SyncError, atomic_bytes

import home as syshome

HOME = syshome.home()
SHARED = syshome.shared(HOME)
SETTINGS_FIX = SHARED / '同步工具设置-排版.json'

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors='replace')
    except (AttributeError, ValueError):
        pass


def detect_vault():
    """程序装在 .同步工具 里时，知识库就是它的上一层；否则往上找带 .obsidian 的目录。"""
    for base in (HOME,) + tuple(list(HOME.parents)[:3]):
        try:
            if (base / '.obsidian').is_dir():
                return str(base)
        except OSError:
            continue
    if HOME.parent.name == '.同步工具':
        return str(HOME.parent.parent)
    for base in (Path.home() / 'Documents' / 'Obsidian Vault',
                 Path.home() / 'Obsidian'):
        try:
            if (base / '.obsidian').is_dir():
                return str(base)
        except OSError:
            continue
    return ''

import ui_kit as U

BG = U.CANVAS
CARD = U.SURFACE
LINE = U.LINE
TEXT = U.INK
MUTED = U.MUTED
ACCENT = U.ACCENT
ACCENT_DARK = U.ACCENT_HOVER
ERR = U.ERR
WARN = U.WARN
OK = U.OK
SOFT = U.SUNKEN

POLICY_TEXT = {
    'skip': '云端优先——Notion 改过就跳过',
    'overwrite': '本地优先——用本地覆盖 Notion',
    'copy': '两边都留',
}
POLICY_ORDER = ['skip', 'overwrite', 'copy']


def load_json(path, default=None):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else (default or {})
    except (OSError, ValueError):
        return default or {}


def save_json(path, data):
    atomic_bytes(Path(path), json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


def permission_hint(exc):
    if isinstance(exc, PermissionError):
        return ('。写入被系统或安全软件拦截：请把本程序加入杀毒软件信任区'
                '（火绒、360、Defender），或在弹窗中允许后重试')
    return ''


def center(window, width, height):
    """按逻辑尺寸居中；实际像素再乘缩放，避免高分屏下窗口偏小把内容裁掉。"""
    root = window.winfo_toplevel()
    width, height = U.s(width), U.s(height)
    x = root.winfo_rootx() + max(0, (root.winfo_width() - width) // 2)
    y = root.winfo_rooty() + max(U.s(30), (root.winfo_height() - height) // 3)
    window.geometry('%dx%d+%d+%d' % (width, height, x, y))


class RailButton(tk.Label):
    """深色导航栏里的按钮：用 Label 自己画，避免浅色主题的 ttk 样式打架。"""

    IDLE_DARK = '#2C3448'
    HOVER_DARK = '#39425A'
    DISABLED = '#7A8391'

    def __init__(self, parent, text, command, accent=False):
        self._accent = bool(accent)
        self._idle = U.ACCENT if accent else self.IDLE_DARK
        tk.Label.__init__(
            self, parent, text=text,
            font=U.body(9, 'bold') if accent else U.body(9),
            bg=self._idle, fg='#FFFFFF' if accent else U.ON_RAIL,
            anchor='center', padx=U.s(8), pady=U.s(8), bd=0,
            cursor='hand2', takefocus=True, highlightthickness=1,
            highlightbackground=U.RAIL, highlightcolor=U.ON_RAIL)
        self._command = command
        self._enabled = True
        self.bind('<Button-1>', self._on_click)
        self.bind('<Return>', self._on_click)
        self.bind('<space>', self._on_click)
        self.bind('<Enter>', self._on_enter)
        self.bind('<Leave>', self._on_leave)

    def _on_click(self, event=None):
        if self._enabled and self._command is not None:
            self._command()
        return 'break'

    def _on_enter(self, event=None):
        if self._enabled:
            self.configure(bg=U.ACCENT_DARK if self._accent else self.HOVER_DARK)

    def _on_leave(self, event=None):
        if self._enabled:
            self.configure(bg=self._idle)

    def set_enabled(self, flag):
        self._enabled = bool(flag)
        if self._enabled:
            self.configure(bg=self._idle,
                           fg='#FFFFFF' if self._accent else U.ON_RAIL,
                           cursor='hand2')
        else:
            self.configure(bg=self.HOVER_DARK, fg=self.DISABLED, cursor='arrow')

    def set_visible(self, flag, **pack_kwargs):
        if flag:
            if not self.winfo_ismapped():
                self.pack(**pack_kwargs)
        else:
            self.pack_forget()


class Suite:
    """主窗口。"""

    def __init__(self, smoke=False):
        dpi.make_aware()
        self.root = tk.Tk()
        dpi.apply(self.root)
        self.root.title('%s · %s 体验版' % (brand.APP_NAME, journal.VERSION))
        screen_w = self.root.winfo_screenwidth() - U.s(24)
        screen_h = self.root.winfo_screenheight() - U.s(72)
        self.root.geometry('%dx%d' % (min(U.s(1220), screen_w), min(U.s(810), screen_h)))
        self.root.minsize(min(U.s(960), screen_w), min(U.s(650), screen_h))
        self.root.configure(bg=BG)
        icon = brand.asset(brand.ICON_ICO)
        if icon is not None:
            try:
                self.root.iconbitmap(default=str(icon))
            except tk.TclError:
                pass
        if smoke:
            self.root.withdraw()
            self.root.after(400, self.root.destroy)

        self.session = journal.Journal(HOME, '窗口版')
        journal.install_excepthook(lambda mode: journal.Journal(HOME, mode))
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.worker = [None]
        self.last_tab = None
        self._persona_win = None
        self._space_win = None
        self.watch = {'thread': None, 'stop': False, 'generation': 0, 'scope': None}
        self.workspace_name = ''
        self.connect_cancel = None
        self.task_busy = False
        self.export_preview = None

        self.import_settings = A.load_settings(home=SHARED)
        self.export_settings = O.load_settings(home=SHARED)
        self.fix_settings = load_json(SETTINGS_FIX, {})

        self.scope_items = list(self.import_settings.get('scope', {}).get('items') or [])
        self.folder_items = list(self.export_settings.get('folders') or [])
        self.home_dir = str(HOME)
        self.hide_support_hint = tk.BooleanVar(
            value=bool(self.import_settings.get('hide_support_hint', False)))
        self.sync_count = int(self.import_settings.get('sync_count') or 0)
        self._about = None
        self._support = None
        self._logo_clicks = []

        self._build_style()
        self._build_shell()

        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind('<F5>', self.refresh_current_task)
        self.root.bind('<Control-l>', lambda event: self.show_log())
        self.root.bind('<Control-L>', lambda event: self.show_log())
        self.root.bind('<Control-Shift-A>', lambda event: self.open_about())
        self.root.bind('<Control-Shift-a>', lambda event: self.open_about())
        self.root.after(300, self.poll)
        self.root.after(600, self.probe_connection)
        self.root.after(1500, self._restore_auto)

    # ---------- 外观 ----------

    BG = BG
    CARD = CARD
    INK = TEXT
    MUTED = MUTED

    def _build_style(self):
        style = ttk.Style()
        try:
            style.theme_use('clam')
        except tk.TclError:
            pass
        style.configure('TFrame', background=U.CANVAS)
        style.configure('Card.TFrame', background=U.SURFACE)
        style.configure('TLabel', background=U.CANVAS, foreground=U.INK, font=U.body(10))
        style.configure('Card.TLabel', background=U.SURFACE, foreground=U.INK, font=U.body(10))
        style.configure('Muted.TLabel', background=U.CANVAS, foreground=U.MUTED, font=U.body(9))
        style.configure('CardMuted.TLabel', background=U.SURFACE, foreground=U.MUTED, font=U.body(9))
        style.configure('Field.TLabel', background=U.SURFACE, foreground=U.MUTED, font=U.body(9))
        style.configure('Field.TEntry', fieldbackground=U.SURFACE, background=U.SURFACE,
                        bordercolor=U.LINE_STRONG, lightcolor=U.LINE_STRONG, darkcolor=U.LINE_STRONG,
                        padding=(10, 8), relief='flat', foreground=U.INK)
        style.map('Field.TEntry', bordercolor=[('focus', U.INK_SOFT)])
        style.configure('Quiet.TButton', background=U.SURFACE, foreground=U.MUTED,
                        borderwidth=0, relief='flat', padding=(8, 4), font=U.body(9))
        style.map('Quiet.TButton', foreground=[('active', U.INK)],
                  background=[('active', U.SURFACE)])
        style.configure('Title.TLabel', background=U.CANVAS, foreground=U.INK, font=U.display(21))
        style.configure('TButton', font=U.body(10), padding=(12, 7))
        style.configure('Small.TButton', font=U.body(9), padding=(12, 6),
                        background=U.SURFACE, foreground=U.INK, borderwidth=1,
                        bordercolor=U.LINE_STRONG, lightcolor=U.LINE_STRONG, darkcolor=U.LINE_STRONG,
                        relief='solid')
        style.map('Small.TButton',
                  background=[('disabled', U.SUNKEN), ('active', U.SUNKEN)],
                  foreground=[('disabled', U.MUTED)],
                  bordercolor=[('focus', U.ACCENT), ('active', U.ACCENT)])
        style.configure('Accent.TButton', font=U.body(10, 'bold'), padding=(20, 10),
                        background=U.ACCENT_HOVER, foreground='#ffffff', borderwidth=0, relief='flat')
        style.map('Accent.TButton',
                  background=[('disabled', U.SUNKEN), ('active', '#1B4F4A')],
                  foreground=[('disabled', U.MUTED)])
        style.configure('TEntry', font=U.body(10), padding=(10, 8),
                        fieldbackground='#ffffff', bordercolor=U.LINE,
                        lightcolor=U.SURFACE, darkcolor=U.SURFACE,
                        insertcolor=U.INK, relief='flat', borderwidth=1)
        style.map('TEntry', bordercolor=[('focus', U.INK_SOFT)])
        style.configure('TCombobox', font=U.body(10), padding=(8, 6),
                        fieldbackground='#ffffff', bordercolor=U.LINE,
                        arrowcolor=U.MUTED, background=U.SURFACE)
        style.map('TCombobox', fieldbackground=[('readonly', '#ffffff')],
                  bordercolor=[('focus', U.INK_SOFT)])
        style.configure('TCheckbutton', background=U.SURFACE, foreground=U.INK,
                        font=U.body(9), focuscolor=U.SURFACE)
        style.configure('Card.TCheckbutton', background=U.SURFACE, foreground=U.INK,
                        font=U.body(9), focuscolor=U.SURFACE)
        style.configure('TProgressbar', background=U.ACCENT, troughcolor=U.LINE,
                        borderwidth=0, thickness=3)
        style.configure('TSeparator', background=U.LINE)
        style.configure('TScrollbar', background=U.CANVAS, troughcolor=U.CANVAS,
                        bordercolor=U.CANVAS, arrowcolor=U.FAINT, relief='flat')

    def _build_shell(self):
        """左侧导航 + 顶部状态 + 动作条 + 页面栈 + 运行记录。"""
        self.root.configure(bg=U.CANVAS)
        self.root.grid_rowconfigure(0, weight=1)
        self.root.grid_columnconfigure(1, weight=1)

        self.rail = U.NavRail(self.root, brand.APP_NAME, brand.APP_SUB,
                              journal.VERSION + ' 体验版', self.show_page, icon=self._logo(30))
        self.rail.grid(row=0, column=0, sticky='ns')
        for widget in (self.rail.mark, getattr(self.rail, 'brand_label', None)):
            if widget is not None:
                widget.bind('<Button-1>', lambda event: self._logo_secret())
        for key, name in (('import', '下载到本地'), ('export', '上传到 Notion'),
                          ('conflicts', '处理冲突'), ('tool', '工具箱'), ('convert', '文本转换')):
            self.rail.add(key, name)

        main = tk.Frame(self.root, bg=U.CANVAS)
        main.grid(row=0, column=1, sticky='nsew')
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(2, weight=1)

        # ---- 状态行 ----
        strip = tk.Frame(main, bg=U.CANVAS)
        strip.grid(row=0, column=0, sticky='ew', padx=U.s(22), pady=(U.s(14), 0))
        left = tk.Frame(strip, bg=U.CANVAS)
        line = tk.Frame(left, bg=U.CANVAS)
        line.pack(anchor='w')
        self.status = tk.StringVar(value='未连接')
        self.dot = tk.Label(line, textvariable=self.status, bg=U.CANVAS, fg=U.MUTED,
                            font=U.body(10, 'bold'))
        self.dot.pack(side='left')
        self.progress = U.Progress(line, width=110, bg=U.CANVAS)
        self.progress.pack(side='left', padx=(10, 0), pady=(1, 0))
        self.detail = tk.StringVar(
            value='连接 Notion 后即可选择同步范围。')
        detail_label = tk.Label(left, textvariable=self.detail, bg=U.CANVAS, fg=U.MUTED,
                                anchor='w', justify='left', wraplength=360, font=U.body(9))
        detail_label.pack(fill='x', pady=(3, 0))
        left.bind('<Configure>', lambda event: detail_label.configure(wraplength=max(100, event.width)))

        right = tk.Frame(strip, bg=U.CANVAS)
        right.pack(side='right')
        left.pack(side='left', fill='x', expand=True)

        self.connect_button = U.RButton(right, '连接 Notion', command=self.connect_only,
                                        variant='primary', height=36, bg=U.CANVAS)
        self.connect_button.pack(side='right')
        self.rail_tools = tk.Frame(self.rail, bg=U.RAIL)
        self.rail_tools.pack(side='bottom', fill='x', padx=U.s(14), pady=(0, U.s(10)))
        self.more_button = RailButton(self.rail_tools, '更多…',
                                      lambda: self.open_more(self.more_button))
        self.more_button.pack(fill='x')

        self.auto = tk.BooleanVar(value=bool(self.bundle().get('autosync')))

        # ---- 页面栈 ----
        holder = tk.Frame(main, bg=U.CANVAS)
        holder.grid(row=2, column=0, sticky='nsew', pady=(U.s(4), 0))
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)
        self.pages = {}
        self.current_page = 'import'

        def make_page(key, title, subtitle):
            outer, body = U.page(holder, title, subtitle, expand=key == 'tool')
            self.pages[key] = outer
            setattr(self, 'tab_' + key, body)
            return body

        make_page('import', '从 Notion 下载到本地',
                  '把 Notion 的内容同步到本地知识库。本地改过的笔记不会被覆盖。')
        make_page('export', '从本地上传到 Notion',
                  '把本地文件夹按层级对应到 Notion；Notion 里没有的栏目会自动新建。')
        make_page('fix', '整理笔记格式',
                  'Notion → Obsidian 同步时已默认开启（勾选「修掉 Notion 标记」）；'
                  '这一页给以前的笔记补跑，或单独检查一遍。')
        make_page('tool', '工具箱', '')

        self._build_import_tab()
        self._build_export_tab()
        self._build_fix_tab()
        self.tool_panel = toolbox.build_panel(self, self.tab_tool)

        # ---- 运行记录 ----
        self._build_log()


        self.nb = _PageShim(self)
        self.show_page('import')


    def open_more(self, anchor=None):
        import tkinter as tk
        menu = tk.Menu(self.root, tearoff=0, bg=U.SURFACE, fg=U.INK,
                       activebackground=U.SUNKEN, activeforeground=U.INK,
                       bd=0, relief='flat', font=U.body(9))
        menu.add_command(label='连接 / 重新连接 Notion', command=self.connect_only)
        menu.add_command(label='使用方式…', command=lambda: self.open_persona(force=True))
        order = engine.describe(self.bundle().get('persona'))
        menu.add_command(label='一键同步 · ' + order, command=self.sync_all)
        menu.add_command(label='存储占用…', command=self.open_space)
        menu.add_separator()
        menu.add_command(label='查看日志', command=self.show_log)
        menu.add_command(label='只读核对上次上传', command=self.review_upload)
        menu.add_command(label='导出诊断包', command=lambda: self.export_package(self.root))
        menu.add_command(label='复制诊断信息', command=self.copy_all)
        menu.add_separator()
        menu.add_command(label='取消当前任务', command=self.do_cancel)
        menu.add_separator()
        menu.add_command(label='关于本程序…', command=self.open_about)
        menu.add_command(label='支持作者…', command=self.open_support)
        menu.add_command(label='使用许可…', command=self.open_license)
        try:
            target = anchor if anchor is not None else self.more_button
            x = target.winfo_rootx()
            y = target.winfo_rooty() + target.winfo_height() + 4
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    # ---------- 作者与支持 ----------

    def open_about(self):
        about.open_about(self.root, self)

    def open_support(self):
        support.open_support(self.root, self)

    def open_license(self):
        support.open_license(self.root, self)

    def set_support_hint(self, hidden):
        """记住「不再显示支持提示」。"""
        self.hide_support_hint.set(bool(hidden))
        settings = A.load_settings(home=SHARED)
        settings['hide_support_hint'] = bool(hidden)
        A.save_settings(settings, home=SHARED)

    def note_import_success(self, counts):
        """同步成功后计数，够次数且用户没关提示时，在运行记录里提一句（不弹窗）。"""
        self.sync_count += 1
        settings = A.load_settings(home=SHARED)
        settings['sync_count'] = self.sync_count
        A.save_settings(settings, home=SHARED)
        if self.hide_support_hint.get():
            return
        if self.sync_count != 10 or self.import_settings.get('support_hinted'):
            return
        settings['support_hinted'] = True
        A.save_settings(settings, home=SHARED)
        self.write('顺手提一句：这程序是 %s 一个人做的，用着顺手的话可以在「更多 → 支持作者」请我喝杯咖啡；'
                   '不点也不影响任何功能。' % brand.AUTHOR, 'muted')

    def _logo_secret(self):
        """侧栏标记连点 5 次打开关于（彩蛋）。"""
        now = time.time()
        self._logo_clicks = [t for t in self._logo_clicks if now - t < 2.5]
        self._logo_clicks.append(now)
        if len(self._logo_clicks) >= 5:
            self._logo_clicks = []
            self.open_about()

    def _build_log(self):
        main_frame = self.root.grid_slaves(row=0, column=1)[0]
        self.logcard = tk.Frame(main_frame, bg=U.SURFACE, highlightbackground=U.LINE,
                                highlightthickness=1)
        self.logcard.grid(row=3, column=0, sticky='ew', padx=U.s(22), pady=(U.s(8), U.s(14)))
        bar = tk.Frame(self.logcard, bg=U.SURFACE)
        bar.pack(fill='x', padx=U.s(10), pady=U.s(4))
        self.log_toggle = U.RButton(bar, '展开运行记录', command=self.toggle_log,
                                    variant='quiet', height=28, bg=U.SURFACE, font=U.body(9))
        self.log_toggle.pack(side='left')
        self.log_body = tk.Frame(self.logcard, bg=U.SURFACE)
        head = tk.Frame(self.log_body, bg=U.SURFACE)
        head.pack(fill='x', padx=U.s(12), pady=(0, U.s(4)))
        self.autoscroll = tk.BooleanVar(value=True)
        U.Check(head, '自动滚动', var=self.autoscroll, bg=U.SURFACE,
                font=U.body(8)).pack(side='right')
        U.RButton(head, '清空', command=self.clear_log, variant='quiet', height=26,
                  font=U.body(8)).pack(side='right', padx=(0, 8))

        body = tk.Frame(self.log_body, bg=U.SURFACE)
        body.pack(fill='both', expand=True, padx=U.s(12), pady=(0, U.s(10)))
        self.output = tk.Text(body, height=5, wrap='word', relief='flat', bd=0,
                              bg=U.SURFACE, fg=U.INK_SOFT, insertwidth=0,
                              font=U.mono(9), padx=0, pady=2,
                              spacing1=1, spacing3=2)
        self.output.pack(side='left', fill='both', expand=True)
        bar = ttk.Scrollbar(body, orient='vertical', command=self.output.yview)
        bar.pack(side='right', fill='y')
        self.output.configure(yscrollcommand=bar.set, state='disabled')
        self.output.tag_configure('err', foreground=U.ERR)
        self.output.tag_configure('warn', foreground=U.WARN)
        self.output.tag_configure('ok', foreground=U.OK)

    def toggle_log(self, expanded=None):
        if expanded is None:
            expanded = not self.log_body.winfo_manager()
        if expanded:
            self.log_body.pack(fill='x')
        else:
            self.log_body.pack_forget()
        self.log_toggle.set_text('收起运行记录' if expanded else '展开运行记录')

    def do_cancel(self):
        if self.connect_cancel is not None:
            self.connect_cancel.set()
            return
        if self.worker[0] is None:
            self.write('当前没有在运行的任务', 'warn')
            return
        self.cancel.set()
        self.write('已请求取消，等当前步骤结束就会停下…', 'warn')
        self.set_status('正在取消', '已请求取消，正在结束当前步骤', WARN)

    # ---------- 日志 / 状态 ----------

    def write(self, text, tag=None):
        self.events.put(('log', (text, tag)))

    def flush_log(self, text, tag=None):
        self.output.configure(state='normal')
        stamp = journal.now().strftime('%H:%M:%S')
        self.output.insert('end', '[%s] %s\n' % (stamp, text),
                           tag if tag else ())
        self.output.configure(state='disabled')
        if self.autoscroll.get():
            self.output.see('end')

    def clear_log(self):
        self.output.configure(state='normal')
        self.output.delete('1.0', 'end')
        self.output.configure(state='disabled')

    def set_status(self, text, detail=None, color=MUTED):
        self.status.set(text)
        self.dot.configure(fg=color)
        if detail is not None:
            self.detail.set(detail)

    def refresh_sync_controls(self):
        connected = bool(self.workspace_name)
        self.rail.set_footer(self.workspace_name or 'Notion 未连接')
        if not connected and self.current_page in ('import', 'export'):
            self.connect_button.pack(side='right')
            self.connect_button.set_enabled(not self.task_busy and self.connect_cancel is None)
        else:
            self.connect_button.pack_forget()
        enabled = connected and not self.task_busy and self.connect_cancel is None
        for widget in (self.import_button, self.scope_button, self.export_button,
                       self.quick_push, self.auto_check):
            widget.configure(state='normal' if enabled else 'disabled')

    def busy(self, on, note=''):
        self.task_busy = bool(on)
        widgets = getattr(self, 'action_widgets', [])
        for widget in widgets:
            try:
                widget.configure(state='disabled' if on else 'normal')
            except tk.TclError:
                pass
        self.refresh_sync_controls()
        if on:
            if note:
                self.set_status('正在处理', note, ACCENT)
            self.progress.start(14)
        else:
            self.progress.stop()

    # ---------- 连接 ----------

    def connect_only(self):
        self.start_connection(authorize=True)

    def probe_connection(self):
        self.start_connection(authorize=False)

    def start_connection(self, authorize):
        if self.connect_cancel is not None or self.task_busy:
            return
        self.connect_cancel = threading.Event()
        cancel = self.connect_cancel
        self.set_status('正在连接', '在浏览器授权后回到这里。' if authorize else '正在检查已保存的连接。', ACCENT)
        self.refresh_sync_controls()

        def work():
            try:
                with SingleRun():
                    auth = Auth(allow_reset=authorize)
                    if authorize:
                        auth.connect(self.write, cancel)
                    elif not auth.data.get('access_token'):
                        self.events.put(('connect-fail', ''))
                        return
                    client = MCP(auth, cancel)
                    client.start()
                    account = client.call('get-self')
                    workspace = account.get('workspace', {}) or {}
                    self.events.put(('connected', str(workspace.get('name') or '未知工作区')))
            except Exception as exc:  # noqa: BLE001 - 交给界面显示
                self.events.put(('connect-fail', str(exc)))

        threading.Thread(target=work, daemon=True).start()

    # ---------- 日志窗口 / 诊断包 ----------

    def _logo(self, target_px=30):
        """应用图标按目标像素缩小，返回 PhotoImage（导航栏左上角、关于窗口共用）。"""
        img = tk.PhotoImage(file=str(brand.asset(brand.ICON_PNG)))
        factor = max(1, int(round(img.width() / max(1, U.s(target_px)))))
        if factor > 1:
            img = img.subsample(factor, factor)
        self._logo_img = img
        return img

    def show_page(self, key):
        if key == 'conflicts':
            import conflicts as _cfl
            vault = ''
            try:
                vault = self.current_vault()
            except Exception:  # noqa: BLE001
                vault = ''
            if not vault:
                import app as A
                vault = str((A.load_settings() or {}).get('vault') or '')
            if not vault or not Path(vault).is_dir():
                from tkinter import messagebox
                messagebox.showinfo('先选知识库', '主界面先选好知识库文件夹，再回来看冲突。',
                                    parent=self.root)
            else:
                self._conflicts = _cfl.open_conflicts(self.root, self, vault)
            if getattr(self, 'current_page', None):
                self.rail.select(self.current_page)
            return
        if key == 'convert':
            import converter as cv
            existing = getattr(self, '_converter', None)
            if existing is not None and getattr(existing, 'win', None) is not None:
                try:
                    if existing.win.winfo_exists():
                        existing.win.deiconify()
                        existing.win.lift()
                        existing.win.focus_force()
                        return
                except Exception:  # noqa: BLE001
                    pass
            self._converter = cv.open_converter(self.root, self)
            return
        key = key if key in self.pages else 'import'
        for name, frame in self.pages.items():
            if name == key:
                frame.grid(row=0, column=0, sticky='nsew')
            else:
                frame.grid_forget()
        self.current_page = key
        if getattr(self, 'rail', None):
            self.rail.select(key)
        self.refresh_sync_controls()
        if key == 'export':
            self.refresh_upload_destination()

    def _main_frame(self):
        return self.root.grid_slaves(row=0, column=1)[0]

    def show_log(self):
        path = getattr(self.session, 'path', None)
        window = tk.Toplevel(self.root)
        window.title('运行日志 · ' + journal.VERSION)
        center(window, 980, 640)
        window.configure(bg=BG)

        head = ttk.Frame(window, padding=(14, 12, 14, 6))
        head.pack(fill='x')
        ttk.Label(head, text='日志文件：' + (str(path) if path else '（本次未写入）'),
                  style='Muted.TLabel').pack(side='left')
        ttk.Button(head, text='关闭', style='Small.TButton',
                   command=window.destroy).pack(side='right')
        ttk.Button(head, text='导出诊断包', style='Small.TButton',
                   command=lambda: self.export_package(window)).pack(side='right', padx=8)
        ttk.Button(head, text='复制全部', style='Small.TButton',
                   command=self.copy_all).pack(side='right')

        body = ttk.Frame(window, padding=(14, 0, 14, 12))
        body.pack(fill='both', expand=True)
        text = tk.Text(body, wrap='word', relief='flat', bg=CARD, fg=TEXT,
                       font=('Consolas', 10), padx=10, pady=8)
        text.pack(side='left', fill='both', expand=True)
        bar = ttk.Scrollbar(body, orient='vertical', command=text.yview)
        bar.pack(side='right', fill='y')
        text.configure(yscrollcommand=bar.set)
        text.tag_configure('err', foreground=ERR)
        text.tag_configure('warn', foreground=WARN)
        text.insert('1.0', self.session.text())
        text.configure(state='disabled')

    def export_package(self, parent=None):
        parent = parent or self.root
        contents = self.session.diagnostic_contents()
        preview = '\n\n'.join(name + '\n' + data.strip() for name, data in contents.items())
        if not messagebox.askokcancel('预览将导出的诊断信息', preview, parent=parent):
            return
        name = 'NotionObsidian诊断-' + journal.now_id() + '.zip'
        target = filedialog.asksaveasfilename(parent=parent or self.root,
                                              title='保存诊断包',
                                              defaultextension='.zip',
                                              initialfile=name,
                                              filetypes=[('压缩包', '*.zip')])
        if not target:
            return
        try:
            path = self.session.export(Path(target), contents)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror('导出失败',
                                 '导出诊断包失败：%s%s' % (type(exc).__name__, permission_hint(exc)),
                                 parent=parent or self.root)
            return
        messagebox.showinfo('已导出', '诊断包已保存：\n%s' % path, parent=parent or self.root)

    def copy_all(self):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(brand.APP_NAME + ' ' + journal.VERSION +
                                       '\n' + self.session.text())
        except tk.TclError:
            pass

    # ---------- 主循环 ----------

    def poll(self):
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind, payload = event[0], event[1]
            if kind == 'log':
                text, tag = payload
                self.flush_log(text, tag)
            elif kind == 'status':
                text, detail, color = payload
                self.set_status(text, detail, color)
            elif kind == 'connected':
                self.connect_cancel = None
                self.workspace_name = payload
                self.export_preview = None
                self.refresh_sync_controls()
                self.refresh_upload_destination()
                self.set_status('已连接 Notion', '工作区 · ' + payload, OK)
                self.write('已连接 Notion：' + payload, 'ok')
            elif kind == 'connect-fail':
                self.connect_cancel = None
                self.workspace_name = ''
                self.export_preview = None
                self.refresh_sync_controls()
                self.refresh_upload_destination()
                self.set_status('未连接 Notion', payload or '连接后即可选择同步范围。', ERR if payload else MUTED)
            elif kind == 'summary-import':
                self.summary_label.pack(fill='x', pady=(14, 0))
            elif kind == 'summary-export':
                self.export_summary.set(payload)
                self._sync_summary()
            elif kind == 'upload-preview':
                preview_key, result = payload
                self.export_preview = (preview_key, result)
                self.root.after_idle(lambda key=preview_key, view=result: self.show_export_plan(key, view))
            elif kind == 'summary-fix':
                self.fix_summary.trace_add('write', lambda *a: self._sync_summary())
            elif kind == 'done':
                self.busy(False)
                self.refresh_upload_destination()
                self.session.close()
                self.worker[0] = None
            elif kind == 'auto-error':
                generation, message = payload
                if generation is None or generation == self.watch['generation']:
                    self._pause_auto_due_error(message)
            elif kind == 'auto-detected':
                generation, scope, rels, expected = payload
                vault, folders, _ = self.export_scope()
                if generation == self.watch['generation'] and (vault, tuple(folders)) == scope:
                    self._auto_push(rels, expected)
            elif kind == 'fail':
                self.busy(False)
                self.toggle_log(True)
                self.refresh_upload_destination()
                self.session.close()
                self.worker[0] = None
        if self.auto.get() and not self.watch['stop']:
            vault, folders, _ = self.export_scope()
            self.watch['scope'] = (vault, tuple(folders))
        self.root.after(280, self.poll)

    def close(self):
        self.watch['stop'] = True
        if self.connect_cancel is not None:
            self.connect_cancel.set()
        try:
            self.session.write('窗口关闭')
            self.session.close()
        except Exception:  # noqa: BLE001
            pass
        self.root.destroy()

    # ---------- 只推改动 / 自动同步 ----------

    def export_scope(self):
        """(知识库, 勾选的文件夹, 顶层页面名)"""
        merged = self.bundle()
        vault = merged.get('vault') or self.current_vault()
        folders = merged.get('folders') or []
        anchor = '' if merged.get('flat', True) else (merged.get('anchor') or '')
        return vault, folders, anchor

    def push_changed(self):
        options = self.upload_options()
        vault, folders, anchor = options['vault'], options['folders'], options['anchor']
        if not vault:
            messagebox.showinfo('还没有选知识库', '先点「选择文件夹」选一个 Obsidian 知识库。',
                                parent=self.root)
            return
        if not Suite._review_pending(self, vault):
            return
        path = change_cache.index_path(vault)
        try:
            previous = change_cache.load(path)
            current = change_cache.snapshot(vault, folders)
        except (SyncError, OSError) as exc:
            messagebox.showerror('无法检查改动', str(exc), parent=self.root)
            return
        changed, gone = change_cache.changed(previous, current)
        if not changed:
            self.write('没有检测到改动（本地文件都比上次同步时旧）', 'ok')
            self.set_status('已同步', '没有需要上传的改动', OK)
            return
        destination = self.workspace_name + ' / ' + (anchor or '工作区顶层')
        if not messagebox.askyesno('确认上传改动', '将检查并上传 %d 篇改动。\n\n本地来源 · %s\nNotion 目的地 · %s'
                                   % (len(changed), vault, destination), parent=self.root):
            return
        self.write('检测到 %d 篇改动：%s' % (len(changed),
                    '、'.join(PurePosixPath(rel).stem for rel in changed[:4])
                    + ('…' if len(changed) > 4 else '')), 'ok')
        def job():
            result = O.push_folders(vault, folders, anchor, options['policy'], self.write, self.cancel,
                                    home=SHARED, dry_run=False, only=set(changed),
                                    attrs=options['write_attributes'], expected={rel: current[rel] for rel in changed})
            files = change_cache.load(path)
            change_cache.mark(files, change_cache.successful_paths(result), current)
            change_cache.forget(files, gone)
            change_cache.save(path, files)
        self.run_task('上传改动', job)

    def _restore_auto(self):
        # The checkbox is persisted separately from the live watcher.
        if self.auto.get():
            self.toggle_auto()

    @staticmethod
    def _auto_pending(vault):
        return write_guard.pending_path(vault)

    def _review_pending(self, vault):
        pending = Suite._auto_pending(vault)
        try:
            info = write_guard.load(vault)
        except SyncError as error:
            messagebox.showerror('请检查上传记录', str(error) + '\n' + str(pending), parent=self.root)
            return False
        if info is None:
            return True
        entries = info.get('entries', {})
        default_status = 'pending' if 'entries' in info else 'unknown'
        rows = [(path, entries.get(path, {}).get('status', default_status)) for path in info['paths']]
        rows.sort(key=lambda row: row[1] not in ('started', 'unknown', 'failed', 'conflict'))
        names = '\n'.join('%s  %s' % (write_guard.STATUS_TEXT[status], path) for path, status in rows[:10])
        if len(rows) > 10:
            names += '\n完整记录在\n' + str(pending)
        if not messagebox.askyesno('请先核对上次上传',
                '上次上传中断或存在冲突。请检查状态仍待核对的笔记。\n\n' + names +
                '\n\n已检查并处理相关条目，清除待核对记录吗？', parent=self.root):
            return False
        try:
            with SingleRun():
                write_guard.dismiss(vault, info)
        except (OSError, SyncError) as exc:
            messagebox.showerror('无法清除待核对记录', str(exc), parent=self.root)
            return False
        return True

    def review_upload(self):
        vault = self.export_vault.get().strip() if self.current_page == 'export' else self.current_vault()
        if not vault:
            messagebox.showinfo('请先选择知识库', '选择知识库后即可核对上次上传。', parent=self.root)
            return
        self.run_task('只读核对上次上传', lambda: O.review_upload(Path(vault), self.write, self.cancel))

    def toggle_auto(self):
        merged = self.bundle()
        merged['autosync'] = bool(self.auto.get())
        if self.auto.get():
            merged.update(self.upload_options())
        download_settings = A.load_settings(home=SHARED)
        download_settings['autosync'] = merged['autosync']
        A.save_settings(download_settings, home=SHARED)
        O.save_settings(merged, home=SHARED)
        self.import_settings = A.load_settings(home=SHARED)
        self.export_settings = O.load_settings(home=SHARED)
        if not self.auto.get():
            self.watch['stop'] = True
            self.watch['generation'] += 1
            self.write('自动同步已关闭', 'ok')
            return
        vault, folders, _ = self.export_scope()
        if not vault or not (Path(vault) / '.obsidian').is_dir():
            self.auto_check.set(False)
            self.toggle_auto()
            messagebox.showinfo('还没有选知识库',
                                '自动同步需要先选一个 Obsidian 知识库文件夹。', parent=self.root)
            return
        if not self._review_pending(vault):
            self.auto_check.set(False)
            self.toggle_auto()
            return
        index = change_cache.index_path(vault)
        if not index.is_file():
            self.auto_check.set(False)
            self.toggle_auto()
            messagebox.showinfo('请先上传改动', '请先点击「上传改动」建立索引，再开启自动上传。', parent=self.root)
            return
        try:
            change_cache.load(index)
            change_cache.snapshot(vault, folders)
        except (SyncError, OSError) as exc:
            self.auto_check.set(False)
            self.toggle_auto()
            messagebox.showerror('无法开启自动上传', str(exc), parent=self.root)
            return
        self.write('自动同步已开启：改完笔记几秒后，自动上传改过的那几篇', 'ok')
        self.watch['stop'] = False
        self.watch['generation'] += 1
        generation = self.watch['generation']
        self.watch['scope'] = (vault, tuple(folders))
        thread = threading.Thread(target=self._watch_loop, args=(generation,), daemon=True)
        self.watch['thread'] = thread
        thread.start()

    def _watch_loop(self, generation):
        while not self.watch['stop'] and generation == self.watch['generation']:
            time.sleep(3)
            if self.watch['stop'] or generation != self.watch['generation']:
                return
            try:
                if self.worker[0] is not None:
                    continue
                scope = self.watch['scope']
                if not scope:
                    continue
                vault, folders = scope
                if not vault:
                    continue
                path = change_cache.index_path(vault)
                previous = change_cache.load(path)
                current = change_cache.snapshot(vault, folders)
                changed, gone = change_cache.changed(previous, current)
                if not changed:
                    continue
                first = current
                time.sleep(4)
                previous = change_cache.load(path)
                current = change_cache.snapshot(vault, folders)
                changed, gone = change_cache.changed(previous, current)
                # Only send notes unchanged across the entire settling interval.
                stable = [rel for rel in changed if first.get(rel) == current.get(rel)]
                if not stable:
                    continue
                basis = {rel: current[rel] for rel in stable}
                if not self.watch['stop'] and generation == self.watch['generation']:
                    self.events.put(('auto-detected', (generation, scope, stable, basis)))
            except Exception as exc:  # noqa: BLE001
                try:
                    self.session.error(exc, '自动同步')
                except Exception:  # noqa: BLE001
                    pass
                if generation == self.watch['generation']:
                    self.watch['stop'] = True
                    self.events.put(('auto-error', (generation, str(exc))))
                return

    def _pause_auto_due_error(self, message):
        self.watch['stop'] = True
        self.auto_check.set(False)
        try:
            self.toggle_auto()
        except OSError:
            pass
        self.write('自动上传已暂停：' + message, 'warn')
        self.set_status('自动上传已暂停', message, WARN)

    def _auto_push(self, rels, expected=None):
        if self.worker[0] is not None or not self.auto.get() or not self.workspace_name:
            return
        vault, folders, anchor = self.export_scope()
        if not vault:
            return
        merged = self.bundle()
        policy = merged.get('policy') or 'skip'
        path = change_cache.index_path(vault)
        try:
            current = change_cache.snapshot(vault, folders)
        except (SyncError, OSError) as exc:
            self._pause_auto_due_error(str(exc))
            return
        if expected is not None:
            rels = [rel for rel in rels if current.get(rel) == expected.get(rel)]
        if not rels:
            return
        self.write('自动同步：%d 篇有改动，开始上传' % len(rels), 'warn')
        pending = self._auto_pending(vault)

        def job():
            # Persist intent before any request. A crash or uncertain response keeps
            # this marker and prevents an automatic repeat on restart.
            if pending.exists():
                raise SyncError('存在待核对的自动上传记录，请先检查 Notion 与本地笔记')
            operation = write_guard.begin(vault, rels)
            result = O.push_folders(vault, folders, anchor, 'skip', self.write, self.cancel,
                                    home=SHARED, dry_run=False, only=set(rels), expected={rel: current[rel] for rel in rels},
                                    pending_operation=operation, write_ids=False,
                                    attrs=bool(merged.get('write_attributes')))
            files = change_cache.load(path)
            success = change_cache.successful_paths(result)
            change_cache.mark(files, success, current)
            change_cache.save(path, files)
            unconfirmed = sorted(set(rels) - success)
            if unconfirmed:
                raise SyncError('部分笔记上传失败或存在冲突，请先核对：' + '、'.join(unconfirmed[:5]))
            write_guard.finish(vault, operation)
        self.run_task('自动同步', job)

    # ---------- 任务调度 ----------

    def run_task(self, title, func):
        if self.worker[0] is not None:
            messagebox.showinfo('正在运行', '上一件事还没结束，请等它完成或点「取消」。',
                                parent=self.root)
            return
        self.cancel.clear()
        self.busy(True, title)
        self.write(title, 'ok')

        def work():
            try:
                func()
                self.events.put(('done', None))
            except SyncError as exc:
                self.write('未完成：' + str(exc), 'err')
                self.events.put(('status', ('没有完成', str(exc), ERR)))
                if title == '自动同步': self.events.put(('auto-error', (None, str(exc))))
                self.events.put(('fail', str(exc)))
            except KeyboardInterrupt:
                self.write('已取消', 'warn')
                self.events.put(('done', None))
            except Exception as exc:  # noqa: BLE001
                detail = type(exc).__name__ + permission_hint(exc)
                self.write('出错了：%s' % detail, 'err')
                self.session.error(exc, title)
                self.events.put(('status', ('出错了', detail + '；详情已写入日志', ERR)))
                if title == '自动同步': self.events.put(('auto-error', (None, detail)))
                self.events.put(('fail', detail))

        thread = threading.Thread(target=work, daemon=True)
        self.worker[0] = thread
        thread.start()


    # ---------- 一键同步 / 使用方式 / 空间 ----------

    def bundle(self):
        """下载与上传读同一份设置，这里合并成一份。"""
        merged = dict(self.import_settings or {})
        merged.update(self.export_settings or {})
        return merged

    def current_vault(self):
        text = (self.import_vault.get() or '').strip()
        if text:
            return text
        found = detect_vault()
        return str(found) if found else ''

    def refresh_current_task(self, _event=None):
        if self.workspace_name and not self.task_busy and self.connect_cancel is None:
            if self.current_page == 'import':
                self.start_import()
            elif self.current_page == 'export':
                self.start_export(True)
        return 'break'

    def sync_all(self, dry=False):
        if not self.workspace_name:
            messagebox.showinfo('请先连接 Notion', '连接后才能运行一键同步。', parent=self.root)
            return
        vault = self.current_vault()
        if not vault or not (Path(vault) / '.obsidian').is_dir():
            messagebox.showinfo('先选知识库',
                                '请先在「① 导入到 Obsidian」里选择你的 Obsidian 知识库文件夹。',
                                parent=self.root)
            return
        merged = self.bundle()
        if presets.needs_onboarding(merged):
            self.open_persona(force=True)
            return
        persona = merged.get('persona') or 'obsidian'
        if not dry and persona != 'notion' and not self._review_pending(vault):
            return
        scope = list(self.scope_items or []) or None

        def job():
            engine.cycle(vault, persona, merged, self.write, self.cancel,
                         home=SHARED, scope=scope, dry_run=dry)

        self.run_task('预览（不改动）' if dry else '一键同步', job)

    def apply_persona(self, persona):
        merged = presets.apply(self.bundle(), persona)
        A.save_settings(merged, home=SHARED)
        O.save_settings(merged, home=SHARED)
        self.import_settings = A.load_settings(home=SHARED)
        self.export_settings = O.load_settings(home=SHARED)
        self.write('使用方式已切换：' + presets.describe(persona)['title'], 'ok')

    def open_persona(self, force=False):
        if self._persona_win is not None and self._persona_win.winfo_exists():
            self._persona_win.lift()
            self._persona_win.focus_set()
            return
        merged = self.bundle()
        window = tk.Toplevel(self.root)
        self._persona_win = window
        window.title('先选你的用法')
        window.configure(bg=U.CANVAS)
        window.resizable(False, False)
        window.transient(self.root)
        center(window, 880, 760)

        body = tk.Frame(window, bg=U.CANVAS)
        body.pack(fill='both', expand=True, padx=U.s(24), pady=(U.s(20), U.s(16)))

        head = tk.Frame(body, bg=U.CANVAS)
        head.pack(fill='x')
        tk.Label(head, text='你平时怎么用这两个程序？', bg=U.CANVAS, fg=U.INK,
                 anchor='w', font=U.display(15)).pack(side='left')
        U.HelpDot(head, 'pick_persona', host=window, bg=U.CANVAS,
                  text='不知道怎么选').pack(side='left', padx=(U.s(10), 0))
        tk.Label(body, text='这项设置决定「一键同步」运行哪些方向。单独下载或上传时，以该页面的选项为准。',
                 bg=U.CANVAS, fg=U.MUTED, anchor='w', justify='left',
                 wraplength=U.s(800), font=U.body(9)).pack(anchor='w', pady=(U.s(6), U.s(12)))

        choice = tk.StringVar(value=merged.get('persona') or 'obsidian')
        cards = {}

        def select(key, _event=None):
            choice.set(key)
            for other, (card, radio) in cards.items():
                on = other == key
                card.configure(highlightbackground=U.ACCENT if on else U.LINE,
                               highlightthickness=2 if on else 1)
                radio.refresh()

        for item in presets.CHOICES:
            card = tk.Frame(body, bg=U.SURFACE, highlightbackground=U.LINE,
                            highlightthickness=1, cursor='hand2')
            card.pack(fill='x', pady=U.s(5))
            inner = tk.Frame(card, bg=U.SURFACE, cursor='hand2')
            inner.pack(fill='x', padx=U.s(16), pady=U.s(12))

            top = tk.Frame(inner, bg=U.SURFACE, cursor='hand2')
            top.pack(fill='x')
            radio = U.Radio(top, item['title'], value=item['key'], var=choice,
                            bg=U.SURFACE, font=U.body(10, 'bold'))
            radio.pack(side='left')
            U.HelpDot(top, 'persona_' + item['key'], host=window,
                      bg=U.SURFACE).pack(side='right')

            subtitle = tk.Label(inner, text=item['subtitle'], bg=U.SURFACE, fg=U.INK_SOFT,
                                anchor='w', font=U.body(9), cursor='hand2')
            subtitle.pack(anchor='w', pady=(U.s(2), U.s(6)))

            grid = tk.Frame(inner, bg=U.SURFACE, cursor='hand2')
            grid.pack(fill='x')
            cells = []
            for order, (what, result) in enumerate(item.get('rows', [])):
                lab1 = tk.Label(grid, text=what, bg=U.SURFACE, fg=U.MUTED, anchor='w',
                                width=14, font=U.body(8), cursor='hand2')
                lab1.grid(row=order, column=0, sticky='w', pady=U.s(1))
                lab2 = tk.Label(grid, text=result, bg=U.SURFACE, fg=U.INK_SOFT, anchor='w',
                                justify='left', wraplength=U.s(620),
                                font=U.body(8), cursor='hand2')
                lab2.grid(row=order, column=1, sticky='w', padx=(U.s(8), 0), pady=U.s(1))
                cells.extend((lab1, lab2))

            cards[item['key']] = (card, radio)
            for widget in [card, inner, top, subtitle] + cells:
                widget.bind('<Button-1>', lambda event, key=item['key']: select(key))

        select(choice.get())

        footer = tk.Frame(body, bg=U.CANVAS)
        footer.pack(fill='x', pady=(U.s(14), 0))

        def apply_and_close():
            self.apply_persona(choice.get())
            window.destroy()

        U.RButton(footer, '就用这个', command=apply_and_close, variant='primary',
                  height=40, padx=26, font=U.body(10, 'bold')).pack(side='right')
        U.RButton(footer, '取消' if force else '稍后再说', command=window.destroy,
                  variant='ghost', height=40, padx=18,
                  font=U.body(9)).pack(side='right', padx=(0, U.s(10)))
        window.bind('<Escape>', lambda event: window.destroy())
        window.grab_set()

    def open_space(self):
        vault = self.current_vault()
        if self._space_win is not None and self._space_win.winfo_exists():
            self._space_win.lift()
            return
        if not vault:
            messagebox.showinfo('先选知识库', '先选择一个知识库，才知道备份放在哪里。',
                                parent=self.root)
            return
        merged = self.bundle()
        window = tk.Toplevel(self.root)
        self._space_win = window
        window.title('空间占用')
        window.configure(bg=BG)
        window.resizable(False, False)
        window.transient(self.root)
        center(window, 560, 420)
        body = ttk.Frame(window, padding=(22, 18))
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='这些都在知识库的 .同步工具 里，不会碰你的笔记',
                  style='Card.TLabel', font=('Microsoft YaHei UI', 11, 'bold')).pack(anchor='w')
        table = ttk.Treeview(body, columns=('size',), show='tree headings', height=7)
        table.heading('#0', text='目录')
        table.heading('size', text='占用')
        table.column('size', width=120, anchor='e')
        table.pack(fill='both', expand=True, pady=(10, 0))
        total = tk.StringVar(value='')
        note = tk.StringVar(value='')

        def relist():
            table.delete(*table.get_children())
            found = storage.sizes(vault)
            for name, size in found.items():
                table.insert('', 'end', text=name, values=(storage.human(size),))
                if name in storage.FOLDERS:
                    pass
            total.set('合计 ' + storage.human(sum(found.values())))

        relist()

        def do_clean():
            keep_runs = int(merged.get('backup_runs') or storage.KEEP_RUNS)
            keep_days = int(merged.get('backup_days') or storage.KEEP_DAYS)
            conflict_days = int(merged.get('conflict_days') or storage.CONFLICT_DAYS)
            removed, freed = storage.cleanup(vault, keep_runs=keep_runs,
                                             keep_days=keep_days,
                                             conflict_days=conflict_days)
            relist()
            note.set('清理了 %d 项，释放 %s' % (removed, storage.human(freed)))
            self.write('空间清理：%d 项，释放 %s' % (removed, storage.human(freed)), 'ok')

        ttk.Label(body, textvariable=total, style='Card.TLabel').pack(anchor='e', pady=(6, 0))
        ttk.Label(body, textvariable=note, style='CardMuted.TLabel').pack(anchor='w')
        footer = ttk.Frame(body)
        footer.pack(fill='x', pady=(10, 0))
        ttk.Button(footer, text='按策略清理旧备份', style='Accent.TButton',
                   command=do_clean).pack(side='left')
        ttk.Button(footer, text='关闭', style='Small.TButton',
                   command=window.destroy).pack(side='right')

    # ---------- 导入页 ----------

    def _build_import_tab(self):
        frame = self.tab_import
        block = ttk.Frame(frame, style='Card.TFrame')
        block.pack(fill='x')
        row = ttk.Frame(block, style='Card.TFrame')
        row.pack(anchor='w')
        ttk.Label(row, text='Obsidian 知识库', style='Field.TLabel').pack(side='left')
        U.HelpDot(row, 'vault', host=self.root, bg=U.SURFACE).pack(side='left', padx=(8, 0))
        line = ttk.Frame(block, style='Card.TFrame')
        line.pack(fill='x', pady=(6, 18))
        self.import_vault = tk.StringVar(
            value=self.import_settings.get('vault') or self.export_settings.get('vault') or detect_vault())
        ttk.Entry(line, textvariable=self.import_vault).pack(side='left', fill='x', expand=True)
        ttk.Button(line, text='更换知识库', style='Small.TButton',
                   command=self.choose_import_vault).pack(side='left', padx=(8, 0))

        row = ttk.Frame(block, style='Card.TFrame')
        row.pack(anchor='w')
        ttk.Label(row, text='同步范围（不选就是全部类别）',
                  style='Field.TLabel').pack(side='left')
        U.HelpDot(row, 'scope_import', host=self.root, bg=U.SURFACE).pack(side='left', padx=(8, 0))
        line = ttk.Frame(block, style='Card.TFrame')
        line.pack(fill='x', pady=(6, 18))
        self.scope_text = tk.StringVar(value='全部类别')
        ttk.Entry(line, textvariable=self.scope_text,
                  state='readonly').pack(side='left', fill='x', expand=True)
        self.scope_button = ttk.Button(line, text='选择类别', style='Small.TButton',
                                       command=self.open_scope)
        self.scope_button.pack(side='left', padx=(8, 0))
        clear = ttk.Button(line, text='选择全部', style='Quiet.TButton',
                           command=self.clear_scope)
        clear.pack(side='left', padx=(6, 0))

        acts = ttk.Frame(frame, style='Card.TFrame')
        acts.pack(fill='x', pady=(22, 0))
        self.import_button = U.RButton(acts, '同步到本地', command=self.start_import,
                                       variant='primary', height=38, padx=24,
                                       font=U.body(10, 'bold'))
        self.import_button.pack(side='left')
        self.repair_var = tk.BooleanVar(
            value=bool(self.import_settings.get('repair_after_import', True)))
        self.repair_box = U.Check(acts, '修掉 Notion 标记',
                                  var=self.repair_var, bg=U.SURFACE, font=U.body(9))
        U.HelpDot(acts, 'repair', host=self.root,
                  bg=U.SURFACE).pack(side='right', padx=(0, 8))
        self.repair_box.pack(side='right', padx=(0, 6))
        self.action_widgets = [self.import_button]

        self.import_summary = tk.StringVar()
        self.summary_label = tk.Label(frame, textvariable=self.import_summary, bg=U.SUNKEN,
                                      fg=U.INK_SOFT, anchor='w', justify='left',
                                      padx=14, pady=9, bd=0,
                                      font=U.body(9))
        self.import_summary.trace_add('write', lambda *a: self._sync_summary())
        self.refresh_scope_text()


    def _sync_summary(self):
        pairs = ((getattr(self, 'summary_label', None), getattr(self, 'import_summary', None)),
                 (getattr(self, 'export_label', None), getattr(self, 'export_summary', None)),
                 (getattr(self, 'fix_label', None), getattr(self, 'fix_summary', None)))
        for label, var in pairs:
            if label is None or var is None:
                continue
            text = (var.get() or '').strip()
            if text and not label.winfo_ismapped():
                label.pack(fill='x', pady=(16, 0))
            elif not text and label.winfo_ismapped():
                label.pack_forget()

    def choose_import_vault(self):
        path = filedialog.askdirectory(parent=self.root, title='选择 Obsidian 知识库')
        if not path:
            return
        self.import_vault.set(path)
        A.save_settings(dict(A.load_settings(home=SHARED)), home=SHARED)

    def refresh_scope_text(self, detail=False):
        items = self.scope_items
        if not items:
            self.scope_text.set('全部类别')
            return
        names = [item.get('title') or item.get('id', '') for item in items]
        shown = '、'.join(names[:3])
        if len(names) > 3:
            shown += ' 等 %d 个' % len(names)
        self.scope_text.set(shown)

    def clear_scope(self):
        self.scope_items = []
        settings = A.load_settings(home=SHARED)
        settings.pop('scope', None)
        A.save_settings(settings, home=SHARED)
        self.refresh_scope_text()
        self.write('同步范围已清空：下次同步全部类别', 'warn')
        self.set_status(self.status.get(), '同步范围已清空，下次会同步全部类别', MUTED)

    def start_import(self):
        vault = self.import_vault.get().strip()
        if not vault:
            messagebox.showinfo('请选择知识库', '先选一个 Obsidian 知识库文件夹。', parent=self.root)
            return
        A.save_settings(dict(A.load_settings(home=SHARED)), home=SHARED)
        scope = self.scope_items or None
        settings = A.load_settings(home=SHARED)
        settings['repair_after_import'] = bool(self.repair_var.get())
        A.save_settings(settings, home=SHARED)

        def job():
            result = A.sync(vault, self.write, self.cancel, scope=scope,
                            repair=bool(self.repair_var.get()))
            counts = result['counts']
            self.import_summary.set('本次：新增 %d · 更新 %d · 未变 %d · 冲突 %d · 保留 %d' % (
                counts['added'], counts['updated'], counts['unchanged'],
                counts['conflicts'], counts['retained']))
            try:
                self.events.put(('summary-import', None))
            except Exception:  # noqa: BLE001
                pass
            self.events.put(('status', ('同步完成',
                                        A.sync_done_detail(counts['added'], counts['updated']),
                                        OK)))
            try:
                self.note_import_success(counts)
            except Exception:  # noqa: BLE001  计数失败不影响同步结果
                pass

        self.run_task('开始同步 Notion → Obsidian', job)

    def open_scope(self):
        if not self.import_vault.get().strip():
            messagebox.showinfo('请选择知识库', '先选一个 Obsidian 知识库文件夹。', parent=self.root)
            return
        picker = getattr(self, '_scope_picker', None)
        if picker and picker.window.winfo_exists():
            picker.window.lift()
            return

        def make_client(cancel):
            client = MCP(Auth(), cancel)
            client.start()
            return client

        def save(items):
            settings = A.load_settings(home=SHARED)
            settings['scope'] = {'items': items, 'updated': journal.now_id()}
            A.save_settings(settings, home=SHARED)
            self.scope_items = items
            self.refresh_scope_text()
            self.write('同步范围已保存：%s' % self.scope_text.get(), 'ok')

        self._scope_picker = ScopePicker(self.root, self.scope_items, center, make_client,
                                         SingleRun, self.write, save)

    # ---------- 导出页 ----------

    def _build_export_tab(self):
        frame = self.tab_export
        block = ttk.Frame(frame, style='Card.TFrame')
        block.pack(fill='x')
        row = ttk.Frame(block, style='Card.TFrame')
        row.pack(anchor='w')
        ttk.Label(row, text='本地来源', style='Card.TLabel', font=U.body(10, 'bold')).pack(side='left')
        U.HelpDot(row, 'vault', host=self.root, bg=U.SURFACE).pack(side='left', padx=(8, 0))
        line = ttk.Frame(block, style='Card.TFrame')
        line.pack(fill='x', pady=(U.s(5), U.s(6)))
        self.export_vault = tk.StringVar(
            value=self.export_settings.get('vault') or self.import_settings.get('vault') or detect_vault())
        ttk.Entry(line, textvariable=self.export_vault).pack(side='left', fill='x', expand=True)
        U.RButton(line, '更换知识库', command=self.choose_export_vault, variant='ghost',
                  bg=U.SURFACE, height=30, font=U.body(9)).pack(side='right', padx=(U.s(8), 0))
        line = ttk.Frame(block, style='Card.TFrame')
        line.pack(fill='x', pady=(0, U.s(14)))
        self.folder_text = tk.StringVar(value='全部笔记')
        ttk.Entry(line, textvariable=self.folder_text,
                  state='readonly').pack(side='left', fill='x', expand=True)
        U.RButton(line, '设上传范围', command=self.open_folders, variant='ghost',
                  bg=U.SURFACE, height=30, font=U.body(9)).pack(side='right', padx=(U.s(8), 0))

        destination = ttk.Frame(block, style='Card.TFrame')
        destination.pack(fill='x')
        ttk.Label(destination, text='Notion 目的地', style='Card.TLabel',
                  font=U.body(10, 'bold')).pack(side='left')
        self.upload_destination = tk.StringVar()
        ttk.Label(destination, textvariable=self.upload_destination,
                  style='CardMuted.TLabel').pack(side='left', padx=(U.s(10), 0))

        anchor_row = ttk.Frame(block, style='Card.TFrame')
        anchor_row.pack(fill='x', pady=(U.s(5), U.s(12)))
        self.flat_anchor = tk.BooleanVar(value=bool(self.export_settings.get('flat', False)))
        U.Check(anchor_row, '上传到工作区顶层', var=self.flat_anchor,
                bg=U.SURFACE, font=U.body(9),
                command=self.refresh_anchor_state).pack(side='left')
        U.HelpDot(anchor_row, 'flat', host=self.root,
                  bg=U.SURFACE).pack(side='left', padx=(8, 0))
        self.anchor_name = tk.StringVar(value=self.export_settings.get('anchor') or 'Obsidian')
        self.anchor_entry = ttk.Entry(anchor_row, textvariable=self.anchor_name, style='Field.TEntry')
        self.anchor_entry.pack(side='left', fill='x', expand=True, padx=(U.s(10), 0))

        ttk.Label(block, text='上传选项', style='Card.TLabel', font=U.body(10, 'bold')).pack(anchor='w')
        line = ttk.Frame(block, style='Card.TFrame')
        line.pack(fill='x', pady=(U.s(5), U.s(5)))
        ttk.Label(line, text='两边都改过时', style='Field.TLabel',
                  anchor='w').pack(side='left')
        U.HelpDot(line, 'policy', host=self.root,
                  bg=U.SURFACE).pack(side='left', padx=(6, 6))
        self.policy_text = tk.StringVar(
            value=POLICY_TEXT.get(self.export_settings.get('policy') or 'skip', '')
        )
        box = ttk.Combobox(line, textvariable=self.policy_text, state='readonly',
                           values=[POLICY_TEXT[key] for key in POLICY_ORDER])
        box.pack(side='left', fill='x', expand=True, padx=(U.s(6), 0))

        preferences = ttk.Frame(block, style='Card.TFrame')
        preferences.pack(fill='x')
        self.attrs_on = tk.BooleanVar(value=bool(self.export_settings.get('write_attributes')))
        self.attrs_check = U.Check(preferences, '标签和日期写入页面属性',
                                   var=self.attrs_on, command=self.save_export_options,
                                   bg=U.SURFACE, font=U.body(9))
        self.attrs_check.pack(side='left')
        U.HelpDot(preferences, 'write_attrs', host=self.root,
                  bg=U.SURFACE).pack(side='left', padx=(6, 0))
        self.auto_check = U.Check(preferences, '自动上传本地改动', var=self.auto, command=self.toggle_auto,
                                  bg=U.SURFACE, font=U.body(9))
        self.auto_check.pack(side='left', padx=(U.s(16), 0))
        U.HelpDot(preferences, 'autosync', host=self.root, bg=U.SURFACE).pack(side='left', padx=(6, 0))

        acts = ttk.Frame(frame, style='Card.TFrame')
        acts.pack(fill='x', pady=(U.s(12), 0))
        self.export_button = U.RButton(acts, '预览上传', command=lambda: self.start_export(True),
                                       variant='primary', height=36, bg=U.SURFACE)
        self.export_button.pack(side='left')
        self.quick_push = U.RButton(acts, '仅上传改动', command=self.push_changed,
                                    variant='ghost', height=36, bg=U.SURFACE, font=U.body(9))
        self.quick_push.pack(side='left', padx=(U.s(8), 0))
        U.HelpDot(acts, 'only_changed', host=self.root, bg=U.SURFACE).pack(side='left', padx=(6, 0))
        self.pending_button = U.RButton(acts, '查看待核对的上传', command=self.review_upload,
                                        variant='quiet', height=36, bg=U.SURFACE, font=U.body(9))
        self.action_widgets.extend([self.export_button, self.quick_push, self.attrs_check, self.auto_check])

        self.export_summary = tk.StringVar()
        self.export_label = tk.Label(frame, textvariable=self.export_summary, bg=U.SUNKEN,
                                     fg=ACCENT_DARK, anchor='w', justify='left',
                                     padx=U.s(10), pady=U.s(6), font=U.body(9))
        self.export_label.bind('<Configure>', lambda event: self.export_label.configure(
            wraplength=max(100, event.width - U.s(20))))
        self.refresh_folder_text()
        self.refresh_anchor_state()
        self.anchor_name.trace_add('write', lambda *_: self.refresh_upload_destination())
        self.export_vault.trace_add('write', lambda *_: self.refresh_upload_destination())

    def save_export_options(self):
        items = O.load_settings(home=SHARED)
        items['write_attributes'] = bool(self.attrs_on.get())
        self.export_settings = items
        O.save_settings(items, home=SHARED)

    def choose_export_vault(self):
        path = filedialog.askdirectory(parent=self.root, title='选择 Obsidian 知识库')
        if not path:
            return
        self.export_vault.set(path)
        self.refresh_anchor_state(hint='知识库已切换，记得重新选同步范围')

    def refresh_anchor_state(self, hint=None):
        if self.flat_anchor.get():
            self.anchor_entry.pack_forget()
        else:
            self.anchor_entry.pack(side='left', fill='x', expand=True, padx=(U.s(10), 0))
        self.refresh_upload_destination()
        if hint:
            self.set_status(self.status.get(), hint, MUTED)

    def refresh_upload_destination(self):
        workspace = self.workspace_name or '未连接工作区'
        page = '工作区顶层' if self.flat_anchor.get() else (self.anchor_name.get().strip() or 'Obsidian')
        self.upload_destination.set(workspace + ' / ' + page)
        vault = self.export_vault.get().strip()
        has_pending = bool(vault and self._auto_pending(vault).is_file())
        if has_pending:
            self.pending_button.pack(side='right')
        else:
            self.pending_button.pack_forget()

    def refresh_folder_text(self):
        items = self.folder_items
        if not items:
            self.folder_text.set('全部笔记')
            return
        shown = '、'.join(items[:3])
        if len(items) > 3:
            shown += ' 等 %d 个' % len(items)
        self.folder_text.set(shown)

    def open_folders(self):
        vault = self.export_vault.get().strip()
        if not vault:
            messagebox.showinfo('请选择知识库', '先选一个 Obsidian 知识库文件夹。', parent=self.root)
            return
        state = {'items': list(self.folder_items)}

        def changed():
            self.folder_items = list(state.get('items') or [])
            self.refresh_folder_text()
            self.set_status(self.status.get(),
                            '同步范围已保存：%s' % self.folder_text.get(), MUTED)
            self.write('同步范围已保存：%s' % self.folder_text.get(), 'ok')

        O.pick_folders(self.root, self.export_vault, state, changed)

    def policy_key(self):
        for key, text in POLICY_TEXT.items():
            if text == self.policy_text.get():
                return key
        return 'skip'

    def upload_options(self):
        flat = bool(self.flat_anchor.get())
        anchor = '' if flat else (self.anchor_name.get().strip() or 'Obsidian')
        return {'vault': self.export_vault.get().strip(), 'folders': list(self.folder_items),
                'anchor': anchor, 'flat': flat, 'policy': self.policy_key(),
                'write_attributes': bool(self.attrs_on.get())}

    def start_export(self, dry_run):
        if self.task_busy or self.connect_cancel is not None:
            return
        options = self.upload_options()
        vault = options['vault']
        if not vault:
            messagebox.showinfo('请选择知识库', '先选一个 Obsidian 知识库文件夹。', parent=self.root)
            return
        anchor, policy = options['anchor'], options['policy']
        folders, attrs = tuple(options['folders']), options['write_attributes']
        key = (vault, folders, anchor, policy, attrs)
        fingerprint = None
        if not dry_run:
            if not self.export_preview or self.export_preview[0] != key:
                messagebox.showinfo('需要重新预览', '请先预览当前上传范围。', parent=self.root)
                return
            if not self._review_pending(vault):
                return
            fingerprint = self.export_preview[1]['preview_fingerprint']
        self.export_preview = None
        saved = O.load_settings(home=SHARED)
        saved.update(options)
        O.save_settings(saved, home=SHARED)
        self.export_settings = saved

        def job():
            result = O.push_folders(vault, folders, anchor, policy, self.write,
                                    self.cancel, home=O.HOME, dry_run=dry_run, attrs=attrs,
                                    expected_preview=fingerprint)
            if dry_run:
                self.events.put(('upload-preview', (key, result)))
                self.events.put(('status', ('清单已生成', '请核对目的地和将要上传的笔记。', MUTED)))
            else:
                text = self.upload_summary(result['counts'], dry_run=False)
                self.events.put(('summary-export', text))
                self.events.put(('status', ('上传完成', '结果已显示在上传页。', OK)))

        title = '正在生成上传清单' if dry_run else '正在上传到 Notion'
        self.run_task(title, job)

    @staticmethod
    def upload_summary(counts, dry_run):
        names = ('新建', '更新', '覆盖', '副本', '未变', '冲突')
        changes = ('create', 'update', 'overwrite', 'copy') if dry_run else (
            'created', 'updated', 'overwritten', 'copied')
        return ' · '.join('%s %d' % (name, counts.get(key, 0))
                          for name, key in zip(names, (*changes, 'unchanged', 'conflict')))

    def show_export_plan(self, key, result):
        if not self.export_preview or self.export_preview[0] != key:
            return
        vault, folders, anchor, _policy, attrs = key
        window = tk.Toplevel(self.root)
        window.title('确认上传范围')
        window.configure(bg=U.CANVAS)
        window.transient(self.root)
        width = min(U.s(720), self.root.winfo_screenwidth() - U.s(32))
        height = min(U.s(470), self.root.winfo_screenheight() - U.s(60))
        window.geometry('%dx%d+%d+%d' % (width, height,
                        max(0, self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2),
                        max(0, self.root.winfo_rooty() + (self.root.winfo_height() - height) // 3)))
        body = tk.Frame(window, bg=U.CANVAS)
        body.pack(fill='both', expand=True, padx=U.s(20), pady=U.s(16))
        tk.Label(body, text='确认上传范围', bg=U.CANVAS, fg=U.INK,
                 font=U.display(17)).pack(anchor='w')
        settings = (
            '本地来源 · ' + vault,
            '上传范围 · ' + ('、'.join(folders) if folders else '全部笔记'),
            'Notion 目的地 · ' + result['workspace_name'] + ' / ' + (anchor or '工作区顶层'),
            '标签和日期 · ' + ('写入 Notion 页面属性' if attrs else '不写入页面属性'),
        )
        for text in settings:
            tk.Label(body, text=text, wraplength=max(100, width - U.s(44)), justify='left',
                     bg=U.CANVAS, fg=U.INK, font=U.body(9)).pack(anchor='w', pady=(U.s(3), 0))
        tk.Label(body, text=self.upload_summary(result['counts'], dry_run=True), bg=U.CANVAS,
                 fg=U.ACCENT_DARK, font=U.body(9, 'bold')).pack(anchor='w', pady=(U.s(10), U.s(4)))
        table = ttk.Frame(body)
        table.pack(fill='both', expand=True)
        style = ttk.Style(window)
        style.configure('Upload.Treeview', font=U.body(9), rowheight=U.s(28),
                        background=U.SURFACE, fieldbackground=U.SURFACE, foreground=U.INK)
        style.configure('Upload.Treeview.Heading', font=U.body(9, 'bold'),
                        background=U.SUNKEN, foreground=U.INK, relief='flat', padding=(U.s(8), U.s(6)))
        listing = ttk.Treeview(table, columns=('action', 'note'), show='headings',
                               height=5, style='Upload.Treeview')
        listing.heading('action', text='操作', anchor='w')
        listing.heading('note', text='笔记路径', anchor='w')
        listing.column('action', width=U.s(75), stretch=False, anchor='w')
        listing.column('note', minwidth=U.s(160), anchor='w')
        scrollbar = ttk.Scrollbar(table, orient='vertical', command=listing.yview)
        listing.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        listing.pack(side='left', fill='both', expand=True)
        actions = {'create': '新建', 'update': '更新', 'overwrite': '覆盖', 'copy': '副本', 'conflict': '跳过冲突'}
        for item in result['actions']:
            if item['kind'] in actions:
                listing.insert('', 'end', values=(actions[item['kind']], item['rel']))
        footer = tk.Frame(body, bg=U.CANVAS)
        footer.pack(fill='x', pady=(U.s(10), 0))

        def confirm():
            window.destroy()
            self.start_export(False)

        accept = U.RButton(footer, '确认上传', command=confirm, variant='primary', bg=U.CANVAS)
        accept.pack(side='right')
        accept.set_enabled(any(result['counts'].get(action, 0) for action in ('create', 'update', 'overwrite', 'copy')))
        U.RButton(footer, '返回', command=window.destroy, variant='ghost', bg=U.CANVAS).pack(
            side='right', padx=(0, U.s(8)))
        window.bind('<Escape>', lambda _event: window.destroy())
        window.grab_set()
        listing.focus_set()

    # ---------- 修复页 ----------

    def _build_fix_tab(self):
        frame = self.tab_fix
        block = ttk.Frame(frame, style='Card.TFrame')
        block.pack(fill='x')
        ttk.Label(block, text='Obsidian 知识库', style='Field.TLabel').pack(anchor='w')
        line = ttk.Frame(block, style='Card.TFrame')
        line.pack(fill='x', pady=(6, 18))
        self.fix_vault = tk.StringVar(
            value=self.fix_settings.get('vault') or self.import_vault.get()
            or detect_vault())
        ttk.Entry(line, textvariable=self.fix_vault).pack(side='left', fill='x', expand=True)
        ttk.Button(line, text='选择文件夹', style='Small.TButton',
                   command=self.choose_fix_vault).pack(side='left', padx=(8, 0))

        acts = ttk.Frame(frame, style='Card.TFrame')
        acts.pack(fill='x', pady=(22, 0))
        self.fix_button = U.RButton(acts, '开始修复', command=lambda: self.start_fix('fix'),
                                    variant='primary', height=38, padx=24,
                                    font=U.body(10, 'bold'))
        self.fix_button.pack(side='left')
        self.fix_preview_button = U.RButton(acts, '预览',
                                            command=lambda: self.start_fix('preview'),
                                            variant='quiet', height=38, font=U.body(9))
        self.fix_preview_button.pack(side='left', padx=(10, 0))
        self.scan_button = U.RButton(acts, '重新检查', command=lambda: self.start_fix('scan'),
                                     variant='quiet', height=38, font=U.body(9))
        self.scan_button.pack(side='left', padx=(4, 0))
        self.action_widgets.extend([self.scan_button, self.fix_preview_button, self.fix_button])

        back = tk.Label(frame, text='← 回到工具箱', bg=U.SURFACE, fg=U.MUTED,
                        font=U.body(9), cursor='hand2')
        back.pack(anchor='w', pady=(14, 0))
        back.bind('<Button-1>', lambda event: self.show_page('tool'))

        self.fix_summary = tk.StringVar()
        self.fix_label = tk.Label(frame, textvariable=self.fix_summary, bg=U.SUNKEN,
                                  fg=ACCENT_DARK, anchor='w', justify='left',
                                  padx=12, pady=8,
                                  font=('Microsoft YaHei UI', 10))

    def choose_fix_vault(self):
        path = filedialog.askdirectory(parent=self.root, title='选择 Obsidian 知识库')
        if not path:
            return
        self.fix_vault.set(path)
        save_json(SETTINGS_FIX, {'vault': path})

    def start_fix(self, mode):
        vault = self.fix_vault.get().strip()
        if not vault:
            messagebox.showinfo('请选择知识库', '先选一个 Obsidian 知识库文件夹。', parent=self.root)
            return
        save_json(SETTINGS_FIX, {'vault': vault})
        if not (Path(vault) / '.obsidian').is_dir():
            messagebox.showinfo('不是知识库', '这个文件夹里没有 .obsidian，可能选错了。', parent=self.root)
            return

        def job():
            if mode == 'scan':
                result = M.scan(vault)
                self.fix_summary.set('需要修复：%d 篇（共识别 %d 篇带 Notion 编号）'
                                     % (result['files'], result['indexed']))
                if result['totals']:
                    self.write('  共：' + M.describe(result['totals']))
                self.events.put(('summary-fix', None))
                self.events.put(('status', ('扫描完成',
                                            '有 %d 篇需要修复；点「预览修改」看看会改哪里'
                                            % result['files'], OK)))
                return
            dry = mode == 'preview'
            result = M.fix_vault(vault, dry, self.write)
            if dry:
                self.fix_summary.set('预览：会修改 %d 篇，尚未写入' % result['files'])
                self.events.put(('status', ('预览完成', '确认后点「开始修复」', OK)))
            else:
                self.fix_summary.set('已修复 %d 篇（原件已备份到 .同步工具\\排版备份）'
                                     % result['files'])
                self.events.put(('status', ('修复完成',
                                            '已修复 %d 篇，原件在 .同步工具\\排版备份 里'
                                            % result['files'], OK)))
            self.events.put(('summary-fix', None))

        title = {'scan': '扫描需要修复的笔记',
                 'preview': '预览会改哪些笔记',
                 'fix': '开始修复排版标记'}[mode]
        self.run_task(title, job)


class _PageShim:
    """把「页签」接口映射到新的页面栈，兼容老代码里的 nb.select/index。"""

    def __init__(self, suite):
        self.suite = suite

    def select(self, target=None):
        if target is None:
            return self.suite.pages[self.suite.current_page]
        for key, frame in self.suite.pages.items():
            if target is frame or target == key:
                self.suite.show_page(key)
                return frame
        return None

    def index(self, target=None):
        keys = list(self.suite.pages)
        if target is None:
            target = self.suite.current_page
        for key, frame in self.suite.pages.items():
            if target is frame or target == key:
                return keys.index(key)
        return 0


def gui(smoke=False):
    suite = Suite(smoke=smoke)
    suite.root.mainloop()
    return 0


def uninstall(quiet=False, purge=False):
    """卸载本机注册的程序文件，默认保留设置与日志。"""
    import shutil
    import winreg

    if not getattr(sys, 'frozen', False):
        return 2
    exe = Path(sys.executable).resolve()
    target = exe.parent
    components = target / '同步组件'
    registry_key = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\NotionObsidianSync'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_key) as key:
            registered = Path(winreg.QueryValueEx(key, 'InstallLocation')[0]).resolve()
    except (OSError, TypeError, ValueError):
        registered = None
    protected = (target / 'portable.flag').exists() or any(
        parent.name == '.同步工具' or (parent / '.obsidian').is_dir()
        for parent in (target, *target.parents))
    if (registered != target or protected or exe.name.casefold() != (brand.APP_NAME + '.exe').casefold()
            or components.is_symlink() or components.is_junction()):
        if not quiet:
            messagebox.showerror('无法卸载', '当前目录不是已注册的安装位置，或属于知识库及便携版。文件未改动。')
        return 2
    if not quiet:
        try:
            root = tk.Tk()
            root.withdraw()
            detail = '会把程序文件和快捷方式删掉。'
            detail += '\n设置、日志、缓存也会一起删。' if purge else '\n你的设置、日志、缓存会留着（重新安装还能用）。'
            ok = messagebox.askyesno('卸载 %s' % journal.VERSION, '确定卸载？\n\n%s' % detail)
            root.destroy()
            if not ok:
                return 0
        except tk.TclError:
            return 2

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, registry_key)
    except OSError as error:
        if not quiet:
            messagebox.showwarning('卸载未完成', '未能移除 Windows 卸载登记，程序文件和设置已保留。\n%s' % error)
        return 1

    desktop = Path(os.environ.get('USERPROFILE', str(Path.home()))) / 'Desktop' / (brand.APP_NAME + '.lnk')
    appdata = os.environ.get('APPDATA') or str(Path.home() / 'AppData' / 'Roaming')
    start = Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / (brand.APP_NAME + '.lnk')
    errors = []
    for link in (desktop, start):
        try:
            link.unlink(missing_ok=True)
        except OSError as error:
            errors.append('未能移除快捷方式 %s。%s' % (link, error))

    data = Path(appdata) / 'NotionObsidianSync'
    if purge and not data.is_symlink() and not data.is_junction() and data.resolve() != target:
        shutil.rmtree(data, ignore_errors=True)

    # 只处理程序自身的组件。正在运行的文件可能需要退出后再移除。
    if components.is_dir():
        shutil.rmtree(components, ignore_errors=True)
    try:
        exe.unlink()
    except OSError:
        pass
    if not quiet:
        try:
            root = tk.Tk()
            root.withdraw()
            remaining = [str(path) for path in (exe, components) if path.exists()]
            detail = '\n\n退出后可单独删除以下程序文件：\n' + '\n'.join(remaining) if remaining else ''
            retention = '\n\n设置与日志%s。' % ('仍在 %s' % data if data.exists() else '目录已不存在')
            if errors:
                messagebox.showwarning('卸载未完成', '\n'.join(errors) + detail + retention)
            else:
                messagebox.showinfo('已经卸载', '已移除卸载登记与快捷方式。' + detail + retention)
            root.destroy()
        except tk.TclError:
            pass
    return 1 if errors else 0


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=brand.APP_NAME)
    parser.add_argument('--self-check', action='store_true', help='检查环境后退出')
    parser.add_argument('--dpi-info', action='store_true', help='打印屏幕缩放信息后退出')
    parser.add_argument('--sync', action='store_true', help='命令行同步 Notion → Obsidian')
    parser.add_argument('--vault', type=str, default='', help='知识库路径')
    parser.add_argument('--scope', type=str, default='', help='类别编号，逗号分隔')
    parser.add_argument('--preview', action='store_true', help='预览上传清单（不写入 Notion）')
    parser.add_argument('--push', action='store_true', help='执行上传 Obsidian → Notion')
    parser.add_argument('--folders', type=str, default='', help='要上传的文件夹，用逗号分隔')
    parser.add_argument('--anchor', type=str, default='', help='顶层页面名（空=直接对到工作区顶层）')
    parser.add_argument('--flat', action='store_true', help='直接对到 Notion 工作区顶层')
    parser.add_argument('--policy', type=str, default='skip', choices=POLICY_ORDER)
    parser.add_argument('--scan', action='store_true', help='扫描需要修复的笔记')
    parser.add_argument('--fix', action='store_true', help='修复排版标记')
    parser.add_argument('--dry-run', action='store_true', help='修复时只预览')
    parser.add_argument('--export-diagnostics', type=Path, default=None)
    parser.add_argument('--tool', type=str, default='', help='工具箱里的转换：md2docx/md2pdf/md2html/md2txt/docx2md/html2md/notionmd')
    parser.add_argument('--src', type=str, default='', help='--tool 的源文件')
    parser.add_argument('--dst', type=str, default='', help='--tool 的输出文件（可省略）')
    parser.add_argument('--convert', type=str, default='',
                        help='把 md 转成 word/pdf/html/text：会自动把扩展名改对')
    parser.add_argument('--about', action='store_true', help='启动后直接打开「关于」')
    parser.add_argument('--support', action='store_true', help='启动后直接打开「支持作者」')
    parser.add_argument('--license', action='store_true', help='启动后直接打开许可证')
    parser.add_argument('--snapshot', action='store_true', help='给知识库打一份快照')
    parser.add_argument('--snapshots', action='store_true', help='列出已有快照')
    parser.add_argument('--snapshot-diff', metavar='ZIP', default='', help='和某个快照对比')
    parser.add_argument('--restore-snapshot', metavar='ZIP', default='', help='从某个快照写回')
    parser.add_argument('--rename-plan', action='store_true', help='预览批量改名')
    parser.add_argument('--rename-apply', action='store_true', help='执行批量改名（会先存快照）')
    parser.add_argument('--rename-date-prefix', action='store_true', help='改名时加日期前缀')
    parser.add_argument('--rename-undo', action='store_true', help='撤销最近一次批量改名')
    parser.add_argument('--orphans', action='store_true', help='列出孤立附件')
    parser.add_argument('--orphans-move', action='store_true', help='把孤立附件移到归档目录')
    parser.add_argument('--conflicts', action='store_true', help='列出待处理的同步冲突')
    parser.add_argument('--conflicts-open', action='store_true', help='打开同步冲突窗口')
    parser.add_argument('--uninstall', action='store_true', help='卸载本机安装（删除程序和快捷方式）')
    parser.add_argument('--quiet', action='store_true', help='卸载时不弹确认框')
    parser.add_argument('--purge-data', action='store_true', help='卸载时一并删除设置、日志、缓存')
    args_convert_help = ''
    args = parser.parse_args(argv)

    if getattr(args, 'uninstall', False):
        return uninstall(quiet=args.quiet, purge=args.purge_data)

    if getattr(args, 'dpi_info', False):
        dpi.make_aware()
        info = dpi.info()
        try:
            root = tk.Tk()
            dpi.apply(root)
            info['tk_fpixels'] = root.winfo_fpixels('1i')
            info['after_apply'] = dpi.scale()
            info['screen'] = (root.winfo_screenwidth(), root.winfo_screenheight())
            info['geometry'] = '%dx%d' % (U.s(1220), U.s(900))
            root.destroy()
            try:
                probe = Suite(smoke=True)
                info['suite_scale'] = dpi.scale()
                info['suite_geometry'] = probe.root.winfo_geometry()
                probe.root.destroy()
            except Exception as exc:  # noqa: BLE001
                info['suite_error'] = repr(exc)
        except Exception as exc:  # noqa: BLE001
            info['tk_error'] = repr(exc)
        print(json.dumps(info, ensure_ascii=False))
        return 0

    journal.install_excepthook(lambda mode: journal.Journal(HOME, mode))

    if args.self_check:
        report = {'ok': True, 'import': True, 'export': True, 'fix': True, 'gui': True,
                  'organizer': False, 'version': journal.VERSION}
        try:
            import organizer as _org          # noqa: F401  打包完整性检查
            import vault_tools as _vt

            _vt.clean_name('a:b')
            report['organizer'] = True
        except Exception as exc:  # noqa: BLE001
            report['ok'] = False
            report['organizer_error'] = '%s: %s' % (type(exc).__name__, exc)
        try:
            gui(smoke=True)
        except Exception as exc:  # noqa: BLE001
            report['ok'] = False
            report['gui'] = False
            report['error'] = '%s: %s' % (type(exc).__name__, exc)
        atomic_bytes(HOME / '工具检查.json',
                     json.dumps(report, ensure_ascii=False, indent=2).encode('utf-8'))
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report['ok'] else 1

    if args.convert:
        import converter as cv
        import toolbox as tb
        if not args.src or not Path(args.src).is_file():
            print('请用 --src 指定要转换的 Markdown 文件')
            return 3
        kind = args.convert.strip().lower()
        suffixes = dict((k, sfx) for k, _l, sfx in tb.FORMATS) if hasattr(tb, 'FORMATS') else {}
        suffixes = suffixes or {'word': '.docx', 'pdf': '.pdf', 'html': '.html', 'text': '.txt', 'notion': '.md'}
        if kind not in suffixes:
            print('未知格式：%s（可选 word / pdf / html / text / notion）' % kind)
            return 3
        dst = args.dst or str(Path(args.src).with_suffix(suffixes[kind]))
        target = cv.enforce_suffix(dst, suffixes[kind])
        jobs = {'word': tb.md_to_docx, 'pdf': tb.md_to_pdf, 'html': tb.md_to_html,
                'text': tb.md_to_txt, 'notion': tb.notion_md}
        try:
            jobs[kind](args.src, str(target))
        except Exception as exc:  # noqa: BLE001
            print('转换失败：%s: %s' % (type(exc).__name__, exc))
            return 3
        print(str(target))
        return 0

    if args.conflicts or args.conflicts_open:
        import conflicts as _cfl
        settings = load_settings() if 'load_settings' in globals() else {}
        vault = args.vault or (settings or {}).get('vault') or ''
        if not vault or not Path(vault).is_dir():
            print('先指定知识库：--vault <路径>')
            return 3
        if args.conflicts:
            items = _cfl.scan(vault)
            if not items:
                print('没有待处理的冲突')
                return 0
            print('共 %d 个冲突：' % len(items))
            for item in items:
                print('  %s  %s' % (item['stamp'], item['rel']))
            return 0
        app = Suite()
        app.root.after(600, lambda: _cfl.open_conflicts(app.root, app, vault))
        app.root.mainloop()
        return 0

    vault_ops = (args.snapshot or args.snapshots or args.snapshot_diff or args.restore_snapshot
                 or args.rename_plan or args.rename_apply or args.rename_undo
                 or args.orphans or args.orphans_move)
    if vault_ops:
        import vault_tools as vt
        target = args.vault or detect_vault()
        if not target or not Path(target).is_dir():
            print('缺少 --vault（或找不到知识库）')
            return 2
        session = journal.Journal(HOME, '库整理', vault=str(target))
        say = session.write
        try:
            if args.snapshot:
                info = vt.snapshot(target, log=say)
                print('快照：%s（%d 个文件，%s）' % (info['name'], info['count'], vt._human(info['size'])))
            if args.snapshots:
                items = vt.list_snapshots(target)
                print('共 %d 份快照' % len(items))
                for item in items[:20]:
                    print('  %s  %5s  %s  %s' % (item['name'], vt._human(item['size']),
                                                 item.get('created', ''), item.get('label', '')))
            if args.snapshot_diff:
                result = vt.diff(target, args.snapshot_diff)
                print('新增 %d / 修改 %d / 删除 %d / 未变 %d' % (
                    len(result['added']), len(result['modified']),
                    len(result['removed']), result['same']))
                for rel in result['added'][:10]:
                    print('  + ' + rel)
                for rel in result['modified'][:10]:
                    print('  * ' + rel)
                for rel in result['removed'][:10]:
                    print('  - ' + rel)
            if args.restore_snapshot:
                result = vt.restore(target, args.restore_snapshot, log=say)
                print('已写回 %d 个文件（回滚前快照 %s）' % (
                    len(result['written']), result['before']['name']))
            if args.rename_plan or args.rename_apply:
                plan = vt.rename_plan(target, date_prefix=args.rename_date_prefix)
                for rel, new_rel, reason in plan:
                    print('  %s -> %s   [%s]' % (rel, Path(new_rel).name, reason))
                print('共 %d 个待改名' % len(plan))
                if args.rename_apply and plan:
                    vt.snapshot(target, label='before-rename', log=say)
                    done = vt.rename_apply(target, plan)
                    print('改名 %d 个，跳过 %d 个' % (len(done['done']), len(done['skipped'])))
                    for rel, why in done['skipped'][:10]:
                        print('  跳过 %s：%s' % (rel, why))
            if args.rename_undo:
                result = vt.undo_rename(target, log=say)
                if not result['restored'] and not result['skipped']:
                    print('没有改名记录可撤销')
                for new_rel, old_rel in result['restored'][:20]:
                    print('  还原 %s -> %s' % (Path(new_rel).name, Path(old_rel).name))
                for rel, why in result['skipped'][:10]:
                    print('  跳过 %s：%s' % (rel, why))
            if args.orphans or args.orphans_move:
                plan = vt.orphan_plan(target)
                total = sum(size for _rel, size in plan)
                for rel, size in plan[:20]:
                    print('  %s  %s' % (vt._human(size), rel))
                print('孤立附件 %d 个，共 %s' % (len(plan), vt._human(total)))
                if args.orphans_move and plan:
                    vt.snapshot(target, label='before-orphans', log=say)
                    moved = vt.orphan_apply(target, plan, log=say)
                    print('已移走 %d 个到 %s' % (len(moved), vt.ORPHAN_DIR))
            return 0
        except Exception as exc:  # noqa: BLE001
            session.error(exc, '库整理失败')
            print('库整理失败：%s: %s' % (type(exc).__name__, exc))
            return 3
        finally:
            session.close()

    if args.tool:
        import toolbox as tb
        session = journal.Journal(HOME, '命令行转换')
        if not args.src:
            print('缺少 --src')
            session.close()
            return 2
        try:
            result = tb.run_tool(args.tool, Path(args.src), Path(args.dst) if args.dst else None,
                                 log=session.write)
            print(result)
            return 0
        except Exception as exc:  # noqa: BLE001
            session.error(exc, '转换失败')
            print('转换失败：%s: %s' % (type(exc).__name__, exc))
            return 3
        finally:
            session.close()

    if args.export_diagnostics:
        session = journal.Journal(HOME, '导出诊断包', vault=args.vault or None)
        try:
            path = session.export(args.export_diagnostics)
            print('诊断包：' + str(path))
            return 0
        except Exception as exc:  # noqa: BLE001
            session.error(exc, '导出失败')
            print('导出失败：%s%s' % (exc, permission_hint(exc)))
            return 4
        finally:
            session.close()

    if args.sync or args.preview or args.push or args.scan or args.fix:
        mode = ('导入' if args.sync else '上传' if (args.preview or args.push)
                else '排版')
        session = journal.Journal(HOME, '命令行' + mode, vault=args.vault or None)
        def log(text, tag=None):
            line = str(text)
            try:
                print(line)
            except (UnicodeEncodeError, ValueError):
                enc = getattr(sys.stdout, 'encoding', None) or 'utf-8'
                print(line.encode(enc, 'replace').decode(enc, 'replace'))
            session.write(line)
        cancel = threading.Event()
        code = 0
        try:
            if args.sync:
                vault = args.vault or A.load_settings(home=SHARED).get('vault') or detect_vault()
                if not vault:
                    raise SyncError('请用 --vault 指定 Obsidian 知识库')
                scope = None
                if args.scope:
                    ids = [part.strip() for part in args.scope.split(',') if part.strip()]
                    scope = []
                    for pid in ids:
                        if not pid.isalnum() or len(pid) != 32:
                            raise SyncError('类别编号不正确：' + pid)
                        scope.append({'id': pid, 'url': 'https://app.notion.com/p/' + pid,
                                      'title': '命令行指定', 'kind': 'page'})
                result = A.sync(vault, log, cancel, scope=scope,
                                repair=bool(A.load_settings(home=SHARED).get('repair_after_import', True)))
                counts = result['counts']
                log('完成：新增 %d，更新 %d，未变 %d，冲突 %d' % (
                    counts['added'], counts['updated'], counts['unchanged'],
                    counts['conflicts']))
            elif args.preview or args.push:
                vault = args.vault or O.load_settings(home=SHARED).get('vault') or detect_vault()
                if not vault:
                    raise SyncError('请用 --vault 指定 Obsidian 知识库')
                folders = [part.strip() for part in args.folders.split(',') if part.strip()]
                anchor = '' if args.flat else (args.anchor or O.load_settings(home=SHARED).get('anchor') or 'Obsidian')
                O.push_folders(vault, folders, anchor, args.policy, log, cancel,
                               home=O.HOME, dry_run=bool(args.preview))
            elif args.scan or args.fix:
                vault = args.vault or load_json(SETTINGS_FIX, {}).get('vault') or ''
                if not vault:
                    raise SyncError('请用 --vault 指定 Obsidian 知识库')
                if args.scan:
                    result = M.scan(vault)
                    log('需要修复 %d 篇' % result['files'])
                else:
                    result = M.fix_vault(vault, args.dry_run, log)
                    log('处理完成 %d 篇' % result['files'])
        except SyncError as exc:
            A.record_failure(mode + '命令行', exc, home=HOME, vault=args.vault or None, session=session)
            log('未完成：' + str(exc))
            code = 2
        except KeyboardInterrupt:
            log('已取消')
            code = 130
        except Exception as exc:  # noqa: BLE001
            session.error(exc, mode + '失败')
            log('出错了：%s: %s' % (type(exc).__name__, exc))
            code = 3
        finally:
            log('日志：' + str(session.path))
            session.close()
        return code

    app = gui_instance_or_open = None
    if args.about or args.support or args.license:
        app = Suite()
        target = (app.open_about if args.about else
                  app.open_support if args.support else app.open_license)
        app.root.after(500, target)
        app.root.mainloop()
        return 0
    return gui()


if __name__ == '__main__':
    sys.exit(main())
