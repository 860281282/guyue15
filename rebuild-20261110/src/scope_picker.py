"""Notion 导入范围选择窗口。"""

import queue
import threading
import tkinter as tk
from tkinter import ttk

import sync_engine
import ui_kit as ui


class ScopePicker:
    def __init__(self, parent, saved, position, make_client, lock, log, save):
        self.make_client, self.lock, self.log, self.save = make_client, lock, log, save
        self.cancel = threading.Event()
        self.requests, self.results = queue.Queue(), queue.Queue()
        self.selected = {item['id']: item for item in saved
                         if isinstance(item, dict) and isinstance(item.get('id'), str) and item['id']}
        self.nodes, self.loading, self.loaded = {}, set(), set()
        self.ready = False
        self.poll_id = None

        self.window = window = tk.Toplevel(parent)
        window.title('选择同步类别')
        window.transient(parent)
        window.configure(bg=ui.CANVAS)
        position(window, 820, 570)
        window.minsize(ui.s(580), ui.s(400))

        head = ttk.Frame(window, padding=(ui.s(16), ui.s(14), ui.s(16), ui.s(8)))
        head.pack(fill='x')
        self.status = tk.StringVar()
        self.count = tk.StringVar()
        ttk.Label(head, textvariable=self.count, style='Muted.TLabel').pack(side='right')
        ttk.Label(head, textvariable=self.status, wraplength=ui.s(480),
                  font=ui.body(10, 'bold')).pack(side='left', fill='x', expand=True)
        ttk.Label(window, text='点类别名称勾选，点左侧箭头展开。也可用键盘方向键和空格。',
                  style='Muted.TLabel', padding=(ui.s(16), 0)).pack(anchor='w', pady=(0, ui.s(8)))

        footer = ttk.Frame(window, padding=(ui.s(16), ui.s(10), ui.s(16), ui.s(14)))
        footer.pack(side='bottom', fill='x')
        self.all_button = ttk.Button(footer, text='全选', style='Small.TButton', command=self.select_all)
        self.all_button.pack(side='left')
        self.none_button = ttk.Button(footer, text='全不选', style='Small.TButton', command=self.select_none)
        self.none_button.pack(side='left', padx=ui.s(8))
        self.retry_button = ttk.Button(footer, text='重试读取', style='Small.TButton', command=self.read_roots)
        self.retry_button.pack(side='left')
        self.save_button = ttk.Button(footer, text='保存', style='Accent.TButton', command=self.apply)
        self.save_button.pack(side='right')
        ttk.Button(footer, text='关闭', style='Small.TButton', command=window.destroy).pack(side='right', padx=ui.s(8))

        body = ttk.Frame(window, padding=(ui.s(16), 0))
        body.pack(fill='both', expand=True)
        ttk.Style(window).configure('Scope.Treeview', font=ui.body(10), rowheight=ui.s(34))
        self.tree = tree = ttk.Treeview(body, show='tree', selectmode='browse', style='Scope.Treeview')
        bar = ttk.Scrollbar(body, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        tree.pack(side='left', fill='both', expand=True)
        tree.bind('<<TreeviewOpen>>', self.expand)
        tree.bind('<ButtonRelease-1>', self.click)
        tree.bind('<space>', self.space)
        window.bind('<Destroy>', self.cleanup, add=True)
        window.bind('<Escape>', lambda event: window.destroy())
        window.grab_set()

        self.read_roots()
        self.poll_id = window.after(65, self.drain)
        threading.Thread(target=self.worker, daemon=True, args=(
            self.cancel, self.requests, self.results, self.make_client, self.lock)).start()

    def read_roots(self):
        self.ready = False
        self.status.set('正在读取顶层类别…')
        self.retry_button.state(['disabled'])
        self.refresh()
        self.requests.put((None, None))

    @staticmethod
    def worker(cancel, requests, results, make_client, lock):
        def log(text):
            results.put(('log', text))

        client = None
        while not cancel.is_set():
            request = requests.get()
            if request is None or cancel.is_set():
                return
            row, node = request
            try:
                with lock():
                    if cancel.is_set():
                        return
                    if node is None:
                        client = make_client(cancel)
                        items = sync_engine.discover_roots(client, log, cancel)
                    else:
                        items = sync_engine.discover_children(client, dict(node), log, cancel)
                results.put(('reply', (row, items, None)))
            except Exception as error:  # 网络、授权与数据错误统一回到界面线程。
                results.put(('reply', (row, None, str(error) or type(error).__name__)))

    def drain(self):
        self.poll_id = None
        while True:
            try:
                kind, payload = self.results.get_nowait()
            except queue.Empty:
                break
            if kind == 'log':
                self.log(payload)
                continue
            row, items, error = payload
            if row is None:
                self.ready = error is None
                if self.ready:
                    self.add('', items)
                    self.status.set('已读取顶层类别' if items else '当前连接没有可读取的类别')
                else:
                    self.status.set('读取类别失败：' + error)
                if not items:
                    self.retry_button.state(['!disabled'])
            else:
                self.loading.discard(row)
                if error:
                    self.tree.item(row, open=False)
                    self.status.set('读取子类别失败，重新展开可重试：' + error)
                else:
                    self.loaded.add(row)
                    self.tree.delete(*self.tree.get_children(row))
                    self.add(row, items)
                    self.status.set('已读取子类别' if items else '该类别没有可读取的子类别')
            self.refresh()
        self.poll_id = self.window.after(65, self.drain)

    def cleanup(self, event):
        if event.widget is self.window:
            self.cancel.set()
            self.requests.put(None)
            if self.poll_id is not None:
                self.window.after_cancel(self.poll_id)
                self.poll_id = None

    def ancestors(self, row):
        while (row := self.tree.parent(row)):
            yield self.nodes[row]['id']

    def add(self, parent, items):
        path = set()
        if parent:
            path = {self.nodes[parent]['id'], *self.ancestors(parent)}
        for node in items:
            if node['id'] in path:
                continue
            row = self.tree.insert(parent, 'end')
            self.nodes[row] = node
            if node['kind'] != 'database':
                self.tree.insert(row, 'end', text='展开后读取子类别')

    def expand(self, _event):
        row = self.tree.focus()
        node = self.nodes.get(row)
        if node and node['kind'] != 'database' and row not in self.loaded and row not in self.loading:
            self.loading.add(row)
            self.status.set('正在读取「%s」的子类别…' % node['title'])
            self.requests.put((row, node))

    def click(self, event):
        if 'indicator' not in self.tree.identify_element(event.x, event.y):
            self.toggle(self.tree.identify_row(event.y))

    def space(self, _event):
        self.toggle(self.tree.focus())
        return 'break'

    def toggle(self, row):
        node = self.nodes.get(row)
        if not node or not self.ready:
            return
        if node['kind'] == 'database':
            self.status.set('请选择该数据库所在的页面')
            return
        if any(parent in self.selected for parent in self.ancestors(row)):
            self.status.set('该类别已包含在选中的上级类别里')
            return
        pid = node['id']
        if pid in self.selected:
            del self.selected[pid]
        else:
            self.selected[pid] = node
        self.refresh()

    def select_all(self):
        for node in self.nodes.values():
            if node['kind'] != 'database':
                self.selected[node['id']] = node
        self.refresh()

    def select_none(self):
        self.selected.clear()
        self.status.set('请选择类别。要同步全部类别，可在上一个页面点击「选择全部」。')
        self.refresh()

    def refresh(self):
        for row, node in self.nodes.items():
            parent_selected = any(parent in self.selected for parent in self.ancestors(row))
            mark = '' if node['kind'] == 'database' else ('☑ ' if node['id'] in self.selected or parent_selected else '☐ ')
            self.tree.item(row, text=mark + node['title'])
        self.count.set('已选择 %d 项' % len(self.selected))
        available = self.ready and bool(self.nodes)
        self.all_button.state(['!disabled'] if available else ['disabled'])
        self.none_button.state(['!disabled'] if available else ['disabled'])
        self.save_button.state(['!disabled'] if available and self.selected else ['disabled'])

    def apply(self):
        if self.save_button.instate(['disabled']):
            return
        covered, uncovered = set(), set()
        for row, node in self.nodes.items():
            group = covered if any(parent in self.selected for parent in self.ancestors(row)) else uncovered
            group.add(node['id'])
        redundant = covered - uncovered
        items = [dict(id=pid, url=node.get('url') or ('https://app.notion.com/p/' + pid),
                      title=node.get('title') or pid, kind=node.get('kind') or 'page')
                 for pid, node in self.selected.items() if pid not in redundant]
        if not items:
            return
        try:
            self.save(items)
        except OSError as error:
            self.status.set('保存范围失败：' + str(error))
            return
        self.window.destroy()
