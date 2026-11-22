"""两种同步方向共用的分层范围选择。"""

import tkinter as tk
from tkinter import ttk

import ui_kit as ui


class ScopePicker:
    def __init__(self, app, title, all_label, selected, load, children, save):
        self.app, self.children, self.save = app, children, save
        self.selected = {item['key']: item for item in selected}
        self.nodes, self.loaded = {}, set()
        self.window = win = app.dialog(title, 670, 450)
        self.mode = tk.StringVar(value='picked' if selected else 'all')
        self.hint = tk.StringVar(value='正在读取范围…')
        box = tk.Frame(win, bg=ui.CANVAS, padx=ui.s(22), pady=ui.s(18))
        box.pack(fill='both', expand=True)
        app.label(box, title, ui.display(16)).pack(anchor='w')
        modes = tk.Frame(box, bg=ui.CANVAS)
        modes.pack(fill='x', pady=(ui.s(10), ui.s(7)))
        ttk.Radiobutton(modes, text=all_label, variable=self.mode, value='all',
                        command=self.refresh).pack(side='left')
        ttk.Radiobutton(modes, text='仅同步选中项', variable=self.mode, value='picked',
                        command=self.refresh).pack(side='left', padx=ui.s(18))

        area = tk.Frame(box, bg=ui.CANVAS)
        self.tree = tree = ttk.Treeview(area, columns=('state',), show='tree headings',
                                       selectmode='browse')
        tree.heading('#0', text='范围')
        tree.heading('state', text='选择状态')
        tree.column('#0', width=ui.s(350), minwidth=ui.s(180))
        tree.column('state', width=ui.s(100), stretch=False)
        bar = ttk.Scrollbar(area, orient='vertical', command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        tree.pack(side='left', fill='both', expand=True)
        tree.bind('<<TreeviewOpen>>', self.expand)
        tree.bind('<ButtonRelease-1>', self.click)
        tree.bind('<space>', self.toggle_focused)

        foot = tk.Frame(box, bg=ui.CANVAS)
        foot.pack(side='bottom', fill='x')
        app.label(box, '', ui.body(8), ui.MUTED, textvariable=self.hint,
                  wraplength=ui.s(600)).pack(side='bottom', fill='x', pady=ui.s(8))
        area.pack(fill='both', expand=True)
        self.apply = ui.RButton(foot, '保存范围', command=self.commit, variant='primary')
        self.apply.pack(side='right')
        ui.RButton(foot, '关闭', command=self.close).pack(side='right', padx=ui.s(8))
        win.bind('<Escape>', lambda event: self.close())
        win.protocol('WM_DELETE_WINDOW', self.close)
        self.apply.set_enabled(False)
        app.start('正在读取同步范围', load, self.show_roots, self.error, quiet=True)

    def error(self, error):
        if self.window.winfo_exists():
            self.hint.set(str(error))
            self.refresh(update_hint=False)

    def close(self):
        if self.app.busy:
            self.app.request_cancel()
        self.window.destroy()

    def show_roots(self, roots):
        if not self.window.winfo_exists():
            return
        self.add('', roots)
        self.refresh()

    def add(self, parent, items):
        path = set()
        ancestor = parent
        while ancestor:
            path.add(self.nodes[ancestor]['key'])
            ancestor = self.tree.parent(ancestor)
        for item in items:
            if item['key'] in path:
                continue
            row = self.tree.insert(parent, 'end', text=item['title'])
            self.nodes[row] = item
            if item.get('expandable'):
                self.tree.insert(row, 'end', text='展开后读取')

    def expand(self, _event):
        row = self.tree.focus()
        if row not in self.nodes or row in self.loaded:
            return
        if self.app.busy:
            self.tree.item(row, open=False)
            return
        item = self.nodes[row]
        if not item.get('expandable'):
            return
        self.hint.set('正在读取「%s」…' % item['title'])
        self.apply.set_enabled(False)

        def finish(items):
            if self.window.winfo_exists():
                self.loaded.add(row)
                self.tree.delete(*self.tree.get_children(row))
                self.add(row, items)
                self.refresh()

        def failed(error):
            if self.window.winfo_exists():
                self.tree.item(row, open=False)
                self.error(error)

        self.app.start('正在读取子范围', lambda cancel: self.children(item, cancel),
                       finish, failed, quiet=True)

    def click(self, event):
        if self.tree.identify_column(event.x) == '#1':
            self.toggle(self.tree.identify_row(event.y))

    def toggle_focused(self, _event):
        self.toggle(self.tree.focus())
        return 'break'

    def toggle(self, row):
        if row not in self.nodes or self.app.busy:
            return
        item = self.nodes[row]
        self.mode.set('picked')
        if item['key'] in self.selected:
            del self.selected[item['key']]
        else:
            self.selected[item['key']] = item
        self.refresh()

    def refresh(self, update_hint=True):
        all_items = self.mode.get() == 'all'
        for row, item in self.nodes.items():
            value = '全部' if all_items else ('已选' if item['key'] in self.selected else '点击选择')
            self.tree.set(row, 'state', value)
        if update_hint:
            self.hint.set('点击右侧状态列或按空格选择，展开左侧可查看下级范围。')
        self.apply.set_enabled(not self.app.busy and (all_items or bool(self.selected)))

    def commit(self):
        if self.app.busy:
            return
        self.save([] if self.mode.get() == 'all' else list(self.selected.values()))
        self.window.destroy()
