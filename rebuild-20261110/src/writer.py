# -*- coding: utf-8 -*-
"""合并成书：挑一个栏目（或全部），按文件名顺序合成一份带目录的 PDF / Word。"""
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import book
import ui_kit as U

import journal

VERSION = journal.VERSION


def open_book(parent, suite, vault):
    vault = str(vault or '')
    win = tk.Toplevel(parent)
    win.title('合并成书 · ' + VERSION)
    win.configure(bg=U.CANVAS)
    win.geometry('%dx%d' % (U.s(780), U.s(620)))
    root = parent.winfo_toplevel()
    win.geometry('+%d+%d' % (root.winfo_rootx() + U.s(100), root.winfo_rooty() + U.s(60)))
    win.transient(root)

    head = tk.Frame(win, bg=U.CANVAS)
    head.pack(fill='x', padx=U.s(24), pady=(U.s(18), 0))
    tk.Label(head, text='合并成书', bg=U.CANVAS, fg=U.INK,
             font=U.display(15)).pack(anchor='w')
    tk.Label(head, text='把同栏目下的笔记按文件名顺序合成一份带目录的文档。',
             bg=U.CANVAS, fg=U.MUTED, font=U.body(9)).pack(anchor='w', pady=(U.s(4), 0))

    card = tk.Frame(win, bg=U.SURFACE, highlightbackground=U.LINE, highlightthickness=1)
    card.pack(fill='both', expand=True, padx=U.s(24), pady=(U.s(12), 0))

    form = tk.Frame(card, bg=U.SURFACE)
    form.pack(fill='x', padx=U.s(16), pady=(U.s(14), 0))
    form.grid_columnconfigure(1, weight=1)
    tk.Label(form, text='栏目', bg=U.SURFACE, fg=U.MUTED,
             font=U.label(9)).grid(row=0, column=0, sticky='w')
    folder = tk.StringVar(value='知识库')
    ttk.Combobox(form, textvariable=folder, state='readonly', width=18,
                 values=['知识库', '日常生活', '日程提醒', '全部']).grid(
        row=0, column=1, sticky='w', padx=(U.s(10), 0))
    tk.Label(form, text='书名', bg=U.SURFACE, fg=U.MUTED,
             font=U.label(9)).grid(row=1, column=0, sticky='w', pady=(U.s(10), 0))
    title = tk.StringVar(value='我的合集')
    ttk.Entry(form, textvariable=title, style='Field.TEntry').grid(
        row=1, column=1, sticky='ew', padx=(U.s(10), 0), pady=(U.s(10), 0))
    tk.Label(form, text='格式', bg=U.SURFACE, fg=U.MUTED,
             font=U.label(9)).grid(row=2, column=0, sticky='w', pady=(U.s(10), 0))
    fmt = tk.StringVar(value='PDF')
    row_fmt = tk.Frame(form, bg=U.SURFACE)
    row_fmt.grid(row=2, column=1, sticky='w', padx=(U.s(10), 0), pady=(U.s(10), 0))
    for label in ('PDF', 'Word'):
        tk.Radiobutton(row_fmt, text=label, value=label, variable=fmt, bg=U.SURFACE,
                       fg=U.INK_SOFT, selectcolor=U.SURFACE, activebackground=U.SURFACE,
                       bd=0, highlightthickness=0,
                       font=U.body(9)).pack(side='left', padx=(0, U.s(16)))

    body = tk.Frame(card, bg=U.SURFACE)
    body.pack(fill='both', expand=True, padx=U.s(16), pady=(U.s(12), U.s(14)))
    status = tk.Label(body, text='', bg=U.SURFACE, fg=U.MUTED, font=U.body(9),
                      anchor='w', justify='left', wraplength=U.s(700))
    status.pack(fill='x')

    def refresh():
        folder_value = folder.get()
        sub = '' if folder_value == '全部' else folder_value
        notes = book.notes_in(vault, sub) if vault else []
        status.configure(text='这个栏目下有 %d 篇。生成时按文件名顺序排列，每篇另起一页。'
                              % len(notes))
        return notes

    def go():
        notes = refresh()
        if not notes:
            messagebox.showinfo('没有笔记', '这个栏目下没有找到 .md 笔记。', parent=win)
            return
        suffix = '.pdf' if fmt.get() == 'PDF' else '.docx'
        dst = filedialog.asksaveasfilename(
            parent=win, title='保存为', defaultextension=suffix,
            initialfile=(title.get() or '合集') + suffix)
        if not dst:
            return
        dst_path = Path(dst)
        if dst_path.suffix.lower() != suffix:
            dst_path = dst_path.with_suffix(suffix)

        def job():
            if fmt.get() == 'PDF':
                book.to_pdf(notes, dst_path, title.get() or '合集', vault=vault)
            else:
                book.to_docx(notes, dst_path, title.get() or '合集', vault=vault)
            suite.write('已生成 %s（%d 篇，%.1f KB）'
                        % (dst_path.name, len(notes), dst_path.stat().st_size / 1024), 'ok')

        suite.run_task('合并成书', job)

    foot = tk.Frame(win, bg=U.CANVAS)
    foot.pack(fill='x', padx=U.s(24), pady=(U.s(12), U.s(16)))
    ttk.Button(foot, text='刷新篇数', command=refresh, style='Small.TButton').pack(side='left')
    ttk.Button(foot, text='生成', command=go, style='Accent.TButton').pack(side='right')
    ttk.Button(foot, text='关闭', command=win.destroy, style='Small.TButton').pack(
        side='right', padx=(0, U.s(8)))
    refresh()
    return win
