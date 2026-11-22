# -*- coding: utf-8 -*-
"""全库查找替换：先出预览，确认后执行，执行前自动存快照。"""
import re
import hashlib
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import toolbox as T
import ui_kit as U
import vault_tools as V

import journal
from notion_connection import atomic_bytes

VERSION = journal.VERSION


def _notes(vault):
    return V.note_files(vault)


def search(vault, query, use_regex=False, ignore_case=True, limit=500):
    """返回 [(rel, line_no, line)]，最多 limit 条。"""
    if not query:
        return []
    flags = re.IGNORECASE if ignore_case else 0
    pattern = query if use_regex else re.escape(query)
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError('正则写错了：%s' % exc) from None
    hits = []
    for rel, path in _notes(vault):
        try:
            text = path.read_bytes().decode('utf-8-sig')
        except (OSError, UnicodeError) as exc:
            raise OSError('无法读取笔记，已停止查找：' + rel) from exc
        for match in rx.finditer(text):
            number = text.count('\n', 0, match.start()) + 1
            line_start = text.rfind('\n', 0, match.start()) + 1
            line_end = text.find('\n', match.end())
            if line_end < 0:
                line_end = len(text)
            hits.append((rel, number, text[line_start:line_end].strip().replace('\n', ' ↵ ')[:160]))
            if len(hits) >= limit:
                return hits
    return hits


def replace(vault, query, replacement, use_regex=False, ignore_case=True, dry_run=True, expected=None):
    """在整库替换。dry_run=False 时先存快照再写。"""
    if not query:
        return {'files': 0, 'hits': 0, 'changed': [], 'backup': ''}
    flags = re.IGNORECASE if ignore_case else 0
    pattern = query if use_regex else re.escape(query)
    rx = re.compile(pattern, flags)
    targets = []
    hits = 0
    for rel, path in _notes(vault):
        try:
            original = path.read_bytes()
            text = original.decode('utf-8-sig')
        except (OSError, UnicodeError) as exc:
            raise OSError('无法读取笔记，已停止替换：' + rel) from exc
        new, count = rx.subn(replacement if use_regex else (lambda _match: replacement), text)
        if count:
            encoded = (b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b'') + new.encode('utf-8')
            targets.append((rel, path, original, encoded))
            hits += count
    basis = {rel: hashlib.sha256(original).hexdigest() for rel, _path, original, _new in targets}
    if expected is not None and basis != expected:
        raise ValueError('预览后笔记发生变化或命中范围改变，请重新查找并确认。')
    backup = ''
    if not dry_run and targets:
        info = V.snapshot(vault, label='before-replace')
        if info.get('skipped'):
            raise OSError('备份未完整完成，已停止替换；请检查无法读取的文件。')
        backup = str(info.get('path') or '') if isinstance(info, dict) else str(info or '')
        for rel, path, original, _new in targets:
            if path.read_bytes() != original:
                raise OSError('备份期间笔记发生变化，已停止替换：' + rel)
        for rel, path, original, new in targets:
            if path.read_bytes() != original:
                raise OSError('替换期间笔记发生变化，已停止后续操作：' + rel)
            atomic_bytes(path, new)
    return {'files': len(targets), 'hits': hits,
            'changed': [rel for rel, _p, _old, _new in targets], 'backup': backup,
            'basis': basis}


# ---------------------------------------------------------------- 界面
def _dialog(parent, title, width=900, height=620):
    win = tk.Toplevel(parent)
    win.title(title + ' · ' + VERSION)
    win.configure(bg=U.CANVAS)
    win.geometry('%dx%d' % (U.s(width), U.s(height)))
    root = parent.winfo_toplevel()
    win.geometry('+%d+%d' % (root.winfo_rootx() + U.s(60), root.winfo_rooty() + U.s(40)))
    win.transient(root)
    try:
        win.attributes('-topmost', True)
        win.after(600, lambda: win.attributes('-topmost', False))
    except tk.TclError:
        pass
    return win


def open_bulk(parent, suite, vault):
    vault = str(vault or '')
    win = _dialog(parent, '查找替换')

    head = tk.Frame(win, bg=U.CANVAS)
    head.pack(fill='x', padx=U.s(24), pady=(U.s(18), 0))
    tk.Label(head, text='查找替换（整库）', bg=U.CANVAS, fg=U.INK,
             font=U.display(15)).pack(anchor='w')
    tk.Label(head, text='先点「查找」看命中，再点「替换」。执行前会自动存一份快照。',
             bg=U.CANVAS, fg=U.MUTED, font=U.body(9)).pack(anchor='w', pady=(U.s(4), 0))

    card = tk.Frame(win, bg=U.SURFACE, highlightbackground=U.LINE, highlightthickness=1)
    card.pack(fill='both', expand=True, padx=U.s(24), pady=(U.s(12), 0))

    form = tk.Frame(card, bg=U.SURFACE)
    form.pack(fill='x', padx=U.s(16), pady=(U.s(14), 0))
    form.grid_columnconfigure(1, weight=1)
    tk.Label(form, text='查找', bg=U.SURFACE, fg=U.MUTED,
             font=U.label(9)).grid(row=0, column=0, sticky='w')
    query = tk.StringVar()
    ttk.Entry(form, textvariable=query, style='Field.TEntry').grid(
        row=0, column=1, sticky='ew', padx=(U.s(10), 0))
    tk.Label(form, text='替换成', bg=U.SURFACE, fg=U.MUTED,
             font=U.label(9)).grid(row=1, column=0, sticky='w', pady=(U.s(8), 0))
    repl = tk.StringVar()
    ttk.Entry(form, textvariable=repl, style='Field.TEntry').grid(
        row=1, column=1, sticky='ew', padx=(U.s(10), 0), pady=(U.s(8), 0))

    opts = tk.Frame(card, bg=U.SURFACE)
    opts.pack(fill='x', padx=U.s(16), pady=(U.s(10), 0))
    use_regex = tk.BooleanVar(value=False)
    ignore_case = tk.BooleanVar(value=True)
    U.Check(opts, '按正则表达式', var=use_regex, bg=U.SURFACE, font=U.body(9)).pack(side='left')
    U.Check(opts, '忽略大小写', var=ignore_case, bg=U.SURFACE,
            font=U.body(9)).pack(side='left', padx=(U.s(16), 0))

    body = tk.Frame(card, bg=U.SURFACE)
    body.pack(fill='both', expand=True, padx=U.s(16), pady=(U.s(10), U.s(14)))
    style = ttk.Style(win)
    style.configure('Org.Treeview', font=U.body(9), rowheight=U.s(28),
                    background=U.SURFACE, fieldbackground=U.SURFACE, borderwidth=0)
    style.configure('Org.Treeview.Heading', font=U.label(8), background=U.SUNKEN,
                    foreground=U.MUTED, relief='flat', padding=(U.s(10), U.s(7)))
    style.map('Org.Treeview', background=[('selected', U.ACCENT_SOFT)],
              foreground=[('selected', U.INK)])
    tree = ttk.Treeview(body, columns=('file', 'line', 'text'), show='headings', height=12,
                        style='Org.Treeview')
    for name, width, label in (('file', 260, '文件'), ('line', 70, '行'), ('text', 460, '内容')):
        tree.heading(name, text=label)
        tree.column(name, width=U.s(width), anchor='w')
    tree.pack(side='left', fill='both', expand=True)
    bar = ttk.Scrollbar(body, orient='vertical', command=tree.yview)
    bar.pack(side='right', fill='y')
    tree.configure(yscrollcommand=bar.set)

    foot = tk.Frame(win, bg=U.CANVAS)
    foot.pack(fill='x', padx=U.s(24), pady=(U.s(12), U.s(16)))
    status = tk.StringVar(value='输入要查找的内容，点「查找」。')
    tk.Label(foot, textvariable=status, bg=U.CANVAS, fg=U.MUTED,
             font=U.body(9)).pack(side='left')

    def say(text):
        status.set(text)
        try:
            suite.write(text)
        except Exception:  # noqa: BLE001
            pass

    def do_search():
        tree.delete(*tree.get_children())
        try:
            hits = search(vault, query.get(), use_regex.get(), ignore_case.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror('查找失败', str(exc), parent=win)
            return
        for index, (rel, number, line) in enumerate(hits):
            tree.insert('', 'end', tags=('odd' if index % 2 else 'even',),
                        values=(rel, number, line))
        say(('显示前 %d 处，可能还有更多；确认前会统计全部。' if len(hits) >= 500 else '找到 %d 处。') % len(hits))

    def do_replace():
        if not query.get():
            messagebox.showinfo('先填查找内容', '查找内容不能为空。', parent=win)
            return
        try:
            preview = replace(vault, query.get(), repl.get(), use_regex.get(),
                              ignore_case.get(), dry_run=True)
        except (OSError, ValueError) as exc:
            messagebox.showerror('替换失败', str(exc), parent=win)
            return
        if not preview['hits']:
            messagebox.showinfo('没有命中', '整库没有找到要替换的内容。', parent=win)
            return
        if not messagebox.askyesno(
                '确认替换',
                '将修改 %d 个文件、共 %d 处。\n执行前会先存一份快照，可以随时回滚。\n继续吗？'
                % (preview['files'], preview['hits']), parent=win):
            return
        try:
            result = replace(vault, query.get(), repl.get(), use_regex.get(),
                             ignore_case.get(), dry_run=False, expected=preview['basis'])
        except (OSError, ValueError) as exc:
            messagebox.showerror('替换已停止', str(exc), parent=win)
            return
        say('已替换 %d 个文件、%d 处；快照：%s' % (result['files'], result['hits'],
                                            Path(result['backup']).name or '（见快照）'))
        do_search()

    ttk.Button(foot, text='替换', command=do_replace, style='Small.TButton').pack(side='right')
    ttk.Button(foot, text='查找', command=do_search, style='Small.TButton').pack(
        side='right', padx=(0, U.s(8)))
    ttk.Button(foot, text='关闭', command=win.destroy, style='Small.TButton').pack(
        side='right', padx=(0, U.s(16)))
    return win
