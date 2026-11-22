# -*- coding: utf-8 -*-
"""同步冲突：列出、比对、二选一。

同步时如果云端和本地都改过同一篇，程序会把云端版本写到
`<知识库>/.同步工具/冲突/<批次>/<相对路径>`，本地保持不动。
这个窗口把这些副本列出来，让你看清差异后决定留哪边。
"""

import difflib
import shutil
from datetime import datetime
from pathlib import Path

VERSION = '1.0.0'
TOOL_DIR = '.同步工具'
CONFLICT_DIR = '冲突'


def conflict_root(vault):
    return Path(vault) / TOOL_DIR / CONFLICT_DIR


def scan(vault):
    """返回 [{'run','rel','copy','local','stamp'}]，按时间倒序。"""
    root = conflict_root(vault)
    found = []
    if not root.is_dir():
        return found
    for run in sorted(root.iterdir(), reverse=True):
        if not run.is_dir():
            continue
        for path in sorted(run.rglob('*')):
            if not path.is_file():
                continue
            rel = path.relative_to(run).as_posix()
            stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime('%m-%d %H:%M')
            found.append({
                'run': run.name,
                'rel': rel,
                'copy': path,
                'local': Path(vault) / rel,
                'stamp': stamp,
            })
    return found


def diff_text(local_path, copy_path, context=3):
    try:
        local = local_path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        local = []
    try:
        cloud = copy_path.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        cloud = []
    lines = list(difflib.unified_diff(cloud, local, fromfile='云端副本', tofile='本地现状', n=context))
    if not lines:
        return ['（两边内容一样）']
    return lines


def take_copy(item, backup=True):
    """用云端副本覆盖本地；覆盖前把本地另存一份。"""
    local, copy = Path(item['local']), Path(item['copy'])
    local.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if backup and local.is_file():
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup_path = local.with_name(local.name + '.本地-被覆盖-' + stamp)
        shutil.copy2(local, backup_path)
    shutil.copy2(copy, local)
    copy.unlink(missing_ok=True)
    return backup_path


def keep_local(item):
    """保留本地，删掉云端副本。"""
    Path(item['copy']).unlink(missing_ok=True)


def _dialog(parent, title, width, height):
    import tkinter as tk

    import ui_kit as U
    win = tk.Toplevel(parent)
    win.title(title)
    win.configure(bg=U.CANVAS)
    win.transient(parent.winfo_toplevel())
    total_w, total_h = U.s(width), U.s(height)
    root = parent.winfo_toplevel()
    x = root.winfo_rootx() + max(0, (root.winfo_width() - total_w) // 2)
    y = root.winfo_rooty() + max(U.s(30), (root.winfo_height() - total_h) // 3)
    win.geometry('%dx%d+%d+%d' % (total_w, total_h, x, y))
    win.minsize(U.s(width - 120), U.s(height - 160))
    return win


def open_conflicts(parent, suite, vault):
    """冲突列表 + 差异 + 二选一。"""
    import tkinter as tk
    from tkinter import messagebox, ttk

    import ui_kit as U

    win = _dialog(parent, '同步冲突 · ' + U.APP_TITLE if hasattr(U, 'APP_TITLE') else '同步冲突', 980, 720)
    win.title('同步冲突 · 咕月15')

    head = tk.Frame(win, bg=U.CANVAS)
    head.pack(fill='x', padx=U.s(20), pady=(U.s(16), 0))
    tk.Label(head, text='同步冲突', bg=U.CANVAS, fg=U.INK, font=U.display(15)).pack(side='left')
    count_label = tk.Label(head, text='', bg=U.CANVAS, fg=U.MUTED, font=U.body(9))
    count_label.pack(side='left', padx=(U.s(12), 0))
    tk.Label(win, text='两边都改过同一篇时，云端版本存在这里，本地没动。看完选留哪边；覆盖前会自动备份本地。',
             bg=U.CANVAS, fg=U.MUTED, font=U.body(9), anchor='w',
             justify='left', wraplength=U.s(880)).pack(fill='x', padx=U.s(20), pady=(U.s(6), 0))

    body = tk.Frame(win, bg=U.SURFACE, highlightbackground=U.LINE, highlightthickness=1)
    body.pack(fill='both', expand=True, padx=U.s(20), pady=(U.s(12), 0))

    style = ttk.Style(win)
    style.configure('Cfl.Treeview', font=U.body(9), rowheight=U.s(26),
                    background=U.SURFACE, fieldbackground=U.SURFACE, borderwidth=0)
    style.configure('Cfl.Treeview.Heading', font=U.label(8), background=U.SUNKEN,
                    foreground=U.MUTED, relief='flat', padding=(U.s(8), U.s(6)))
    style.map('Cfl.Treeview', background=[('selected', U.ACCENT_SOFT)],
              foreground=[('selected', U.INK)])

    tree = ttk.Treeview(body, columns=('rel', 'stamp'), show='headings', height=8,
                        style='Cfl.Treeview')
    tree.heading('rel', text='文件')
    tree.heading('stamp', text='冲突时间')
    tree.column('rel', width=U.s(520), anchor='w')
    tree.column('stamp', width=U.s(130), anchor='w')
    scroll = ttk.Scrollbar(body, orient='vertical', command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side='top', fill='both', expand=False, padx=U.s(1), pady=(U.s(1), 0))
    scroll.place(relx=1.0, rely=0.0, relheight=0.42, anchor='ne')

    diff_box = tk.Text(body, height=10, wrap='none', relief='flat', bd=0,
                       bg=U.SURFACE, fg=U.INK, font=U.mono(9), padx=U.s(10), pady=U.s(8))
    diff_box.pack(side='top', fill='both', expand=True, padx=U.s(1), pady=(U.s(6), U.s(1)))
    diff_box.tag_configure('add', foreground=U.OK)
    diff_box.tag_configure('del', foreground=U.ERR)
    diff_box.tag_configure('meta', foreground=U.MUTED)

    state = {'items': scan(vault), 'shown': []}

    def refresh(select_first=True):
        state['items'] = scan(vault)
        tree.delete(*tree.get_children())
        for index, item in enumerate(state['items']):
            tree.insert('', 'end', iid=str(index), values=(item['rel'], item['stamp']))
        count_label.configure(text='共 %d 个' % len(state['items']))
        diff_box.configure(state='normal')
        diff_box.delete('1.0', 'end')
        if state['items'] and select_first:
            tree.selection_set('0')
            tree.focus('0')
            show()
        elif not state['items']:
            diff_box.insert('1.0', '没有待处理的冲突。\n\n同步时如果两边都改过同一篇，云端版本会出现在这里。')
        diff_box.configure(state='disabled')
        sync_buttons()

    def current_item():
        selection = tree.selection()
        if not selection:
            return None
        return state['items'][int(selection[0])]

    def show(event=None):
        item = current_item()
        sync_buttons()
        if not item:
            return
        diff_box.configure(state='normal')
        diff_box.delete('1.0', 'end')
        diff_box.insert('1.0', '本地：%s\n云端副本：%s\n\n' % (item['local'], item['copy']), 'meta')
        for line in diff_text(item['local'], item['copy']):
            tag = ''
            if line.startswith('+') and not line.startswith('+++'):
                tag = 'add'
            elif line.startswith('-') and not line.startswith('---'):
                tag = 'del'
            elif line.startswith(('@@', '---', '+++')):
                tag = 'meta'
            diff_box.insert('end', line + '\n', tag)
        diff_box.configure(state='disabled')

    tree.bind('<<TreeviewSelect>>', show)

    def act_take():
        item = current_item()
        if not item:
            return
        if not messagebox.askyesno('用云端副本覆盖本地',
                                   '用云端副本覆盖「%s」？\n本地会先另存一份。' % item['rel'], parent=win):
            return
        backup = take_copy(item)
        refresh()
        if getattr(suite, 'write', None):
            suite.write('冲突：%s 已用云端副本覆盖%s'
                        % (item['rel'], '（本地备份：%s）' % backup.name if backup else ''))

    def act_keep():
        item = current_item()
        if not item:
            return
        if not messagebox.askyesno('保留本地', '保留本地版本，删掉云端副本？\n云端原页在 Notion 里还在，随时能再同步下来。',
                                   parent=win):
            return
        keep_local(item)
        refresh()
        if getattr(suite, 'write', None):
            suite.write('冲突：%s 保留本地，已删云端副本' % item['rel'])

    def act_open():
        item = current_item()
        if not item:
            return
        import os
        folder = Path(item['copy']).parent
        try:
            os.startfile(str(folder))
        except OSError:
            messagebox.showinfo('打不开', str(folder), parent=win)

    foot = tk.Frame(win, bg=U.CANVAS)
    foot.pack(fill='x', padx=U.s(20), pady=(U.s(12), U.s(16)))
    take_button = U.RButton(foot, '用云端副本覆盖本地', command=act_take, variant='primary', height=36, padx=18,
                            font=U.body(9, 'bold'))
    take_button.pack(side='left')
    keep_button = U.RButton(foot, '保留本地', command=act_keep, variant='quiet', height=36, padx=16,
                            font=U.body(9))
    keep_button.pack(side='left', padx=(U.s(8), 0))
    open_button = U.RButton(foot, '打开所在文件夹', command=act_open, variant='quiet', height=36, padx=16,
                            font=U.body(9))
    open_button.pack(side='left', padx=(U.s(8), 0))
    U.RButton(foot, '重新扫描', command=lambda: refresh(), variant='quiet', height=36, padx=16,
              font=U.body(9)).pack(side='left', padx=(U.s(8), 0))
    U.RButton(foot, '关闭', command=win.destroy, variant='quiet', height=36, padx=16,
              font=U.body(9)).pack(side='right')

    def sync_buttons():
        selected = current_item() is not None
        for button in (take_button, keep_button, open_button):
            button.set_enabled(selected)

    refresh()
    win.bind('<Escape>', lambda event: win.destroy())
    return win
