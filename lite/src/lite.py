"""咕月15 的独立双向同步界面。"""

from datetime import datetime
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import dpi
import download
import path_engine
from scope_picker import ScopePicker
import sync_engine
import ui_kit as ui
import upload
import write_guard
from notion_connection import Auth, MCP, SingleRun, SyncError, appdata, atomic_bytes

NAME = '咕月15 · 轻量版'
VERSION = '1.0.0'
POLICIES = {'skip': '跳过云端已修改的页面', 'copy': '冲突时新建本地副本',
            'overwrite': '通过校验后更新云端页面'}
CHANGES = {'create': '新建', 'update': '更新', 'overwrite': '覆盖',
           'copy': '副本', 'unchanged': '未变', 'conflict': '冲突'}


def read_settings(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class Lite:
    def __init__(self, root, workdir=None):
        self.root = root
        self.workdir = Path(workdir) if workdir else appdata()
        self.settings_path = self.workdir / '轻量版设置.json'
        self.settings = read_settings(self.settings_path)
        self.vault = tk.StringVar(value=self.settings.get('vault', ''))
        self.repair = tk.BooleanVar(value=self.settings.get('repair', True))
        self.attrs = tk.BooleanVar(value=self.settings.get('attrs', False))
        self.flat = tk.BooleanVar(value=self.settings.get('flat', False))
        self.anchor = tk.StringVar(value=self.settings.get('anchor', 'Obsidian'))
        self.policy = tk.StringVar(value=POLICIES.get(self.settings.get('policy'), POLICIES['skip']))
        self.status = tk.StringVar(value='未连接 Notion')
        self.workspace_line = tk.StringVar(value='先连接 Notion，再选择知识库与同步范围')
        self.task_line = tk.StringVar()
        self.workspace = {}
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.active = 'download'
        self.log_open = False
        self.page_results = {'download': '', 'upload': ''}
        self.configure()
        self.build()
        self.root.after(60, self.poll)

    def configure(self):
        root = self.root
        dpi.apply(root)
        root.title(NAME)
        root.geometry('%dx%d' % (ui.s(900), ui.s(605)))
        root.minsize(ui.s(785), ui.s(550))
        root.configure(bg=ui.CANVAS)
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.bind('<F5>', lambda event: self.run_active())
        assets = Path(getattr(sys, '_MEIPASS', Path(__file__).parent)) / 'assets'
        self.mark = tk.PhotoImage(file=str(assets / 'mark.png'))
        root.iconphoto(True, self.mark)
        style = ttk.Style(root)
        style.theme_use('clam')
        style.configure('TEntry', padding=ui.s(6), fieldbackground=ui.SURFACE,
                        bordercolor=ui.LINE_STRONG, foreground=ui.INK)
        style.map('TEntry', fieldbackground=[('readonly', ui.SURFACE), ('disabled', ui.SUNKEN)])
        style.configure('TCombobox', padding=ui.s(5), fieldbackground=ui.SURFACE,
                        bordercolor=ui.LINE_STRONG)
        style.map('TCombobox', fieldbackground=[('readonly', ui.SURFACE)],
                  selectbackground=[('readonly', ui.ACCENT_SOFT)],
                  selectforeground=[('readonly', ui.INK)])
        style.configure('TRadiobutton', background=ui.CANVAS, foreground=ui.INK, font=ui.body(8))
        style.configure('Treeview', font=ui.body(9), rowheight=ui.s(30),
                        background=ui.SURFACE, fieldbackground=ui.SURFACE, foreground=ui.INK)
        style.configure('Treeview.Heading', font=ui.body(8, 'bold'))
        style.map('Treeview', background=[('selected', ui.ACCENT_SOFT)],
                  foreground=[('selected', ui.INK)])

    @staticmethod
    def label(parent, text, font=None, color=ui.INK, bg=None, **options):
        return tk.Label(parent, text=text, bg=bg or parent.cget('bg'), fg=color,
                        font=font or ui.body(9), anchor='w', justify='left', **options)

    def build(self):
        self.rail = ui.NavRail(self.root, '咕月15', 'Notion 与 Obsidian 同步', VERSION,
                               self.show, icon=self.mark)
        self.rail.pack(side='left', fill='y')
        self.rail.add('download', '导入到本地')
        self.rail.add('upload', '上传到 Notion')
        self.rail.set_footer('轻量版')

        body = tk.Frame(self.root, bg=ui.CANVAS, padx=ui.s(24), pady=ui.s(18))
        body.pack(side='left', fill='both', expand=True)
        top = tk.Frame(body, bg=ui.CANVAS)
        top.pack(fill='x')
        self.connect_button = ui.RButton(top, '连接 Notion', command=lambda: self.connect(True))
        self.connect_button.pack(side='right')
        self.status_label = self.label(top, '', ui.body(10, 'bold'), ui.MUTED,
                                       textvariable=self.status)
        self.status_label.pack(fill='x')
        self.label(top, '', ui.body(8), ui.MUTED,
                   textvariable=self.workspace_line, wraplength=ui.s(365)).pack(fill='x')

        footer = tk.Frame(body, bg=ui.CANVAS)
        footer.pack(side='bottom', fill='x', pady=(ui.s(10), 0))
        line = tk.Frame(footer, bg=ui.CANVAS)
        line.pack(fill='x')
        self.log_button = ui.RButton(line, '运行记录', command=self.toggle_log, variant='quiet', height=28)
        self.log_button.pack(side='left')
        self.stop_button = ui.RButton(line, '取消任务', command=self.request_cancel, height=28)
        self.label(line, '', ui.body(8), ui.MUTED, textvariable=self.task_line,
                   wraplength=ui.s(330)).pack(side='left', fill='x', expand=True, padx=ui.s(8))
        self.log = tk.Text(footer, height=4, wrap='word', state='disabled', bg=ui.SURFACE,
                           fg=ui.INK, padx=ui.s(8), pady=ui.s(5), font=ui.mono(8),
                           highlightthickness=1, highlightbackground=ui.LINE)

        content = tk.Frame(body, bg=ui.CANVAS)
        content.pack(fill='both', expand=True, pady=(ui.s(20), 0))
        self.viewport = tk.Canvas(content, bg=ui.CANVAS, highlightthickness=0, bd=0)
        self.viewport.pack(side='left', fill='both', expand=True)
        self.scrollbar = ttk.Scrollbar(content, orient='vertical', command=self.viewport.yview)
        self.viewport.configure(yscrollcommand=self.scrollbar.set)
        self.page = tk.Frame(self.viewport, bg=ui.CANVAS)
        self.page_window = self.viewport.create_window(0, 0, anchor='nw', window=self.page)
        self.viewport.bind('<Configure>', self.fit_page)
        self.page.bind('<Configure>', self.fit_page)
        self.root.bind('<MouseWheel>', self.scroll_page, add=True)
        self.show('download')

    def fit_page(self, event):
        if event.widget is self.viewport:
            self.viewport.itemconfigure(self.page_window, width=event.width)
        height = self.page.winfo_reqheight()
        self.viewport.configure(scrollregion=(0, 0, self.viewport.winfo_width(), height))
        if height > self.viewport.winfo_height():
            if not self.scrollbar.winfo_manager():
                self.scrollbar.pack(side='right', fill='y')
        elif self.scrollbar.winfo_manager():
            self.scrollbar.pack_forget()
            self.viewport.yview_moveto(0)

    def scroll_page(self, event):
        x = event.x_root - self.viewport.winfo_rootx()
        y = event.y_root - self.viewport.winfo_rooty()
        if (self.scrollbar.winfo_ismapped() and 0 <= x < self.viewport.winfo_width()
                and 0 <= y < self.viewport.winfo_height()):
            self.viewport.yview_scroll(-int(event.delta / 120), 'units')

    def group_title(self, parent, text, top=9):
        self.label(parent, text, ui.body(8, 'bold'), ui.INK_SOFT).pack(fill='x', pady=(ui.s(top), ui.s(3)))

    def choice_row(self, parent, value, action, callback):
        row = tk.Frame(parent, bg=ui.SURFACE)
        row.pack(fill='x')
        button = ui.RButton(row, action, command=callback, height=29, font=ui.body(8))
        button.pack(side='right', padx=(ui.s(8), 0))
        ttk.Entry(row, textvariable=value, state='readonly', font=ui.body(8)).pack(
            side='left', fill='x', expand=True)
        return button

    def show(self, key):
        if self.busy:
            return
        self.active = key
        self.rail.select(key)
        for widget in self.page.winfo_children():
            widget.destroy()
        inbound = key == 'download'
        title = '从 Notion 导入到本地' if inbound else '从本地上传到 Notion'
        hint = '按所选范围更新 Markdown，保留本地已修改的笔记。' if inbound else '按本地文件夹对应 Notion 页面，核对清单后再上传。'
        self.label(self.page, title, ui.display(22)).pack(fill='x')
        self.label(self.page, hint, ui.body(9), ui.MUTED).pack(fill='x', pady=(ui.s(3), ui.s(12)))
        card = ui.card(self.page, padx=15, pady=12)
        panel = card.inner
        card.pack(fill='x')
        self.group_title(panel, '本地知识库', top=0)
        self.vault_button = self.choice_row(panel, self.vault, '选择知识库', self.choose_vault)
        self.group_title(panel, 'Notion 导入范围' if inbound else '本地上传范围')
        items = self.scope(key)
        names = [item['title'] for item in items]
        shown = '、'.join(names[:3]) + (' 等 %d 项' % len(names) if len(names) > 3 else '')
        self.scope_value = tk.StringVar(value=shown or ('全部可读类别' if inbound else '全部笔记'))
        self.scope_button = self.choice_row(panel, self.scope_value, '选择范围', self.choose_scope)
        if inbound:
            self.group_title(panel, '导入选项')
            ui.Check(panel, '清理 Notion 标记', self.repair).pack(anchor='w')
        else:
            self.upload_fields(panel)
        actions = tk.Frame(panel, bg=ui.SURFACE)
        actions.pack(fill='x', pady=(ui.s(12), 0))
        self.main_button = ui.RButton(actions, '导入到本地' if inbound else '预览上传',
                                      self.run_active, variant='primary', height=35)
        self.main_button.pack(side='left')
        self.review_button = ui.RButton(actions, '核对上次上传', self.review, height=32)
        self.result = self.label(self.page, self.page_results[key], ui.body(8), ui.INK_SOFT,
                                 wraplength=ui.s(530))
        self.result.pack(fill='x', pady=(ui.s(8), 0))
        self.viewport.yview_moveto(0)
        self.refresh_controls()

    def upload_fields(self, panel):
        self.group_title(panel, 'Notion 目的地')
        where = tk.Frame(panel, bg=ui.SURFACE)
        where.pack(fill='x')
        entry = ttk.Entry(where, textvariable=self.anchor, font=ui.body(8))
        entry.pack(side='left', fill='x', expand=True)
        def update():
            entry.state(['disabled'] if self.flat.get() else ['!disabled'])
        ui.Check(where, '直接上传到工作区顶层', self.flat, command=update,
                 font=ui.body(8)).pack(side='left', padx=(ui.s(10), 0))
        update()
        self.group_title(panel, '两边都修改时')
        prefs = tk.Frame(panel, bg=ui.SURFACE)
        prefs.pack(fill='x')
        ttk.Combobox(prefs, textvariable=self.policy, state='readonly', font=ui.body(8),
                     values=list(POLICIES.values()), width=22).pack(side='left', fill='x', expand=True)
        ui.Check(prefs, '写入标签和日期', self.attrs, font=ui.body(8)).pack(side='left', padx=(ui.s(10), 0))

    def scope(self, key):
        items = self.settings.get(key + '_scope', [])
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)
                and isinstance(item.get('key'), str) and isinstance(item.get('title'), str)]

    def persist(self):
        self.settings.update(vault=self.vault.get(), repair=self.repair.get(),
                             attrs=self.attrs.get(), flat=self.flat.get(),
                             anchor=self.anchor.get(), policy=self.policy_value())
        try:
            atomic_bytes(self.settings_path, json.dumps(self.settings, ensure_ascii=False).encode('utf-8'))
        except OSError:
            self.append_log('本次设置无法保存，重启后请重新选择。')

    def policy_value(self):
        return next((key for key, text in POLICIES.items() if text == self.policy.get()), 'skip')

    def choose_vault(self):
        path = filedialog.askdirectory(parent=self.root, title='选择 Obsidian 知识库')
        if not path:
            return
        vault = Path(path).resolve()
        if not (vault / '.obsidian').is_dir():
            messagebox.showinfo('请选择知识库', '所选文件夹缺少 .obsidian 设置目录。', parent=self.root)
            return
        if str(vault) != self.vault.get():
            for key in ('download_scope', 'upload_scope', 'scope_workspace'):
                self.settings.pop(key, None)
            self.page_results = dict.fromkeys(self.page_results, '')
        self.vault.set(str(vault))
        self.persist()
        self.show(self.active)

    def ready_vault(self):
        return bool(self.vault.get() and (Path(self.vault.get()) / '.obsidian').is_dir())

    def refresh_controls(self):
        vault = self.ready_vault()
        connected = bool(self.workspace)
        self.connect_button.set_enabled(not self.busy)
        self.vault_button.set_enabled(not self.busy)
        self.scope_button.set_enabled(not self.busy and vault and (connected or self.active == 'upload'))
        self.main_button.set_enabled(not self.busy and vault and connected)
        pending = vault and write_guard.pending_path(self.vault.get()).is_file()
        if pending and self.active == 'upload':
            self.review_button.pack(side='left', padx=ui.s(8))
            self.review_button.set_enabled(not self.busy and connected)
        else:
            self.review_button.pack_forget()

    def choose_scope(self):
        key = self.active
        incoming = key == 'download'
        vault = Path(self.vault.get())
        workspace_id = self.workspace.get('id')

        def remote_nodes(nodes):
            return [dict(node, key=node['id'], expandable=node['kind'] != 'database')
                    for node in nodes]

        def read_remote(cancel, selected=None):
            with SingleRun():
                client = MCP(Auth(), cancel)
                client.start()
                if client.call('get-self').get('workspace', {}).get('id') != workspace_id:
                    raise SyncError('工作区已变化，请重新连接后选择。')
                if selected is None:
                    return remote_nodes(sync_engine.discover_roots(client, self.write, cancel))
                return remote_nodes(sync_engine.discover_children(client, dict(selected), self.write, cancel))

        def read_local(cancel, folder=None):
            path = '' if folder is None else folder['key']
            if not (vault / path).resolve().is_relative_to(vault.resolve()):
                raise SyncError('该目录位于知识库之外。')
            items = [dict(key=item['rel'], title=item['name'], expandable=True)
                     for item in path_engine.list_folders(vault, path)]
            if folder is None and any(p.is_file() and not p.name.startswith('.') and p.suffix.lower() == '.md'
                                      for p in vault.iterdir()):
                items.insert(0, dict(key='', title='知识库根目录的笔记', expandable=False))
            return items

        def save(items):
            self.settings[key + '_scope'] = items
            if incoming:
                self.settings['scope_workspace'] = workspace_id
            self.persist()
            self.show(key)

        read = read_remote if incoming else read_local
        ScopePicker(self, '选择导入范围' if incoming else '选择上传范围',
                    '全部可读类别' if incoming else '全部笔记', self.scope(key), read,
                    lambda folder, cancel: read(cancel, folder), save)

    def connect(self, authorize=False):
        def job(cancel):
            with SingleRun():
                auth = Auth(allow_reset=authorize)
                if authorize:
                    auth.connect(self.write, cancel)
                elif not auth.data:
                    return {}
                client = MCP(auth, cancel)
                client.start()
                workspace = client.call('get-self').get('workspace') or {}
                if not workspace.get('id'):
                    raise SyncError('Notion 未返回有效工作区。')
                return workspace

        def done(workspace):
            self.workspace = workspace
            if not workspace:
                return
            if self.settings.get('scope_workspace') not in (None, workspace['id']):
                self.settings.pop('download_scope', None)
                self.settings.pop('scope_workspace', None)
                self.persist()
            self.status.set('已连接 Notion')
            self.status_label.configure(fg=ui.OK)
            self.workspace_line.set(workspace.get('name') or 'Notion 工作区')
            self.connect_button.set_text('更换连接')
            self.show(self.active)

        def failed(error):
            self.workspace = {}
            self.status.set('未连接 Notion')
            self.status_label.configure(fg=ui.MUTED)
            self.workspace_line.set('可重新连接后继续同步')
            self.connect_button.set_text('连接 Notion')
            self.task_failed(error)

        self.start('正在连接 Notion', job, done, failed, quiet=not authorize)

    def run_active(self):
        if self.busy or self.root.grab_current() or not self.workspace or not self.ready_vault():
            return
        self.persist()
        if self.active == 'download':
            self.import_notes()
        else:
            self.preview_upload()

    def import_notes(self):
        vault = self.vault.get()
        items = self.scope('download')
        repair = self.repair.get()
        workspace = dict(self.workspace)
        scope = '、'.join(item['title'] for item in items[:4]) if items else '全部可读类别'
        if len(items) > 4:
            scope += ' 等 %d 项' % len(items)
        if not messagebox.askokcancel('确认导入', '%s\n%s\n\n写入本地知识库\n%s' %
                                      (workspace.get('name', 'Notion'), scope, vault), parent=self.root):
            return

        def done(result):
            self.present('download', '导入完成',
                         '新增 {added} · 更新 {updated} · 未变 {unchanged} · 冲突 {conflicts} · 保留 {retained}'.format(
                             **result['counts']))

        self.start('正在导入到本地',
                   lambda cancel: download.sync(vault, self.write, cancel, home=self.workdir,
                                                scope=items, repair=repair, expected_workspace=workspace['id']), done)

    def preview_upload(self):
        vault = self.vault.get()
        if write_guard.pending_path(vault).is_file():
            self.review()
            return
        folders = tuple(item['key'] for item in self.scope('upload'))
        anchor = '' if self.flat.get() else (self.anchor.get().strip() or 'Obsidian')
        policy, attrs = self.policy_value(), self.attrs.get()
        workspace = self.workspace['id']

        def run(cancel, fingerprint=None):
            return upload.push_folders(vault, folders, anchor, policy, self.write, cancel,
                                       home=self.workdir, dry_run=fingerprint is None, attrs=attrs,
                                       expected_preview=fingerprint, expected_workspace=workspace)

        def uploaded(result):
            counts = result['counts']
            self.present('upload', '上传完成',
                         '新建 {created} · 更新 {updated} · 覆盖 {overwritten} · 副本 {copied} · 未变 {unchanged}'.format(**counts))

        def planned(plan):
            changes = sum(plan['counts'].get(key, 0) for key in ('create', 'update', 'overwrite', 'copy'))
            rows = [(CHANGES.get(row['kind'], row['kind']), row['rel']) for row in plan['actions']
                    if row['kind'] != 'unchanged']
            summary = ' · '.join('%s %d' % (text, plan['counts'].get(key, 0)) for key, text in CHANGES.items())
            destination = plan['workspace_name'] + ' / ' + (anchor or '工作区顶层')

            def confirm():
                self.start('正在上传到 Notion', lambda cancel: run(cancel, plan['preview_fingerprint']), uploaded)

            self.confirmation('核对上传范围', destination, summary, rows,
                              ('确认上传', confirm) if changes else None)

        self.start('正在生成上传清单', run, planned)

    def dialog(self, title, width, height):
        win = tk.Toplevel(self.root, background=ui.CANVAS)
        win.title(title)
        win.transient(self.root)
        w, h = ui.s(width), ui.s(height)
        x = max(0, self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2)
        y = max(0, self.root.winfo_rooty() + (self.root.winfo_height() - h) // 2)
        win.geometry('%dx%d+%d+%d' % (w, h, x, y))
        win.minsize(ui.s(500), ui.s(330))
        win.bind('<Escape>', lambda event: win.destroy())
        win.grab_set()
        return win

    def confirmation(self, title, destination, detail, rows, action=None, secondary=None):
        win = self.dialog(title, 660, 420)
        panel = tk.Frame(win, bg=ui.CANVAS, padx=ui.s(22), pady=ui.s(17))
        panel.pack(fill='both', expand=True)
        self.label(panel, destination, ui.display(16), wraplength=ui.s(595)).pack(fill='x')
        self.label(panel, detail, ui.body(8), ui.MUTED, wraplength=ui.s(595)).pack(fill='x', pady=ui.s(7))
        table = tk.Frame(panel, bg=ui.CANVAS)
        tree = ttk.Treeview(table, columns=('action', 'path'), show='headings')
        tree.heading('action', text='状态')
        tree.heading('path', text='笔记')
        tree.column('action', width=ui.s(138), stretch=False)
        tree.column('path', width=ui.s(330))
        bar = ttk.Scrollbar(table, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        tree.pack(side='left', fill='both', expand=True)
        for row in rows or [('无变更', '没有需要上传的新建或更新内容')]:
            tree.insert('', 'end', values=row)
        buttons = tk.Frame(panel, bg=ui.CANVAS)
        buttons.pack(side='bottom', fill='x', pady=(ui.s(10), 0))
        table.pack(fill='both', expand=True)

        def invoke(callback):
            win.destroy()
            callback()

        if action:
            ui.RButton(buttons, action[0], lambda: invoke(action[1]), variant='primary').pack(side='right', padx=(ui.s(6), 0))
        ui.RButton(buttons, '关闭', win.destroy).pack(side='right')
        if secondary:
            ui.RButton(buttons, secondary[0], lambda: invoke(secondary[1])).pack(side='left')

    def review(self):
        vault = self.vault.get()
        try:
            pending = write_guard.load(vault)
        except SyncError as error:
            self.task_failed(error)
            return
        if not pending:
            self.refresh_controls()
            return
        entries = pending.get('entries', {})
        default = 'pending' if 'entries' in pending else 'unknown'
        rows = [(write_guard.STATUS_TEXT[entries.get(rel, {}).get('status', default)], rel)
                for rel in pending['paths']]

        def clear():
            if not messagebox.askyesno('清除待核对记录', '确认已经检查本地和云端内容，清除记录？',
                                       parent=self.root):
                return
            def job(_cancel):
                with SingleRun():
                    write_guard.dismiss(vault, pending)
            self.start('正在清除核对记录', job,
                       lambda _: self.present('upload', '已清除待核对记录', '可以重新预览上传。'))

        def read():
            self.toggle_log(True)
            self.start('正在只读核对', lambda cancel: upload.review_upload(vault, self.write, cancel),
                       lambda _: self.present('upload', '只读核对完成', '请查看运行记录。'))

        self.confirmation('上次上传待核对', '上次上传待核对',
                          '先检查本地和云端内容，再清除待核对记录。', rows,
                          ('只读核对', read), ('清除记录', clear))

    def start(self, title, job, done, failed=None, quiet=False):
        if self.busy:
            return
        self.busy = True
        self.cancel = cancel = threading.Event()
        self.task_line.set(title)
        self.stop_button.pack(side='right')
        self.refresh_controls()

        def work():
            try:
                result = job(cancel)
            except Exception as error:
                self.events.put(('done', (failed or self.task_failed, error)))
            else:
                self.events.put(('done', (done, result)))

        if not quiet:
            self.append_log(title)
        threading.Thread(target=work, daemon=True).start()

    def write(self, text):
        self.events.put(('log', str(text)))

    def poll(self):
        for _ in range(120):
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'log':
                self.append_log(value)
            else:
                callback, result = value
                self.busy = False
                self.task_line.set('')
                self.stop_button.pack_forget()
                callback(result)
                self.refresh_controls()
        self.root.after(60, self.poll)

    def append_log(self, line):
        self.log.configure(state='normal')
        self.log.insert('end', datetime.now().strftime('%H:%M:%S  ') + line + '\n')
        if int(self.log.index('end-1c').split('.')[0]) > 450:
            self.log.delete('1.0', '101.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def toggle_log(self, force=False):
        self.log_open = force or not self.log_open
        self.log_button.set_text('收起记录' if self.log_open else '运行记录')
        if self.log_open:
            self.log.pack(fill='x', pady=(ui.s(4), 0))
        else:
            self.log.pack_forget()

    def present(self, page, status, summary):
        self.page_results[page] = summary
        if page == self.active:
            self.result.configure(text=summary)
        self.task_line.set(status)

    def task_failed(self, error):
        cancelled = self.cancel.is_set()
        self.task_line.set('任务已停止' if cancelled else '未完成，查看运行记录')
        self.append_log(str(error) or type(error).__name__)
        self.toggle_log(True)

    def request_cancel(self):
        self.cancel.set()
        self.task_line.set('正在结束当前操作…')

    def close(self):
        if self.busy:
            self.request_cancel()
        else:
            self.persist()
            self.root.destroy()


def main():
    dpi.make_aware()
    root = tk.Tk()
    if '--self-check' in sys.argv:
        root.withdraw()
        app = Lite(root)
        root.update_idletasks()
        app.show('upload')
        root.update_idletasks()
        resource = download.RESOURCE / 'renderer.py'
        app.workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='selfcheck-', dir=app.workdir) as scratch:
            page_id = 'a' * 32
            fixture = {'workspace_id': 'fixture', 'scope': '自检', 'databases': {}, 'pages': {
                page_id: {'title': '自检笔记', 'url': 'https://app.notion.com/p/' + page_id,
                          'metadata': {'type': 'page'}, 'text': '<content>测试正文</content>'},
            }}
            staged = sync_engine.render(fixture, Path(scratch), resource, {})
            if '测试正文' not in (staged / 'Notion导入/自检笔记.md').read_text(encoding='utf-8'):
                raise RuntimeError('渲染自检失败。')
        atomic_bytes(app.workdir / '轻量版自检.json', json.dumps(
            {'ok': True, 'version': VERSION, 'directions': list(app.rail.items), 'renderer': True},
            ensure_ascii=False).encode('utf-8'))
        root.destroy()
        return 0
    app = Lite(root)
    root.after(160, app.connect)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
