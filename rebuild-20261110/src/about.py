# -*- coding: utf-8 -*-
"""关于窗口：署名、版本、数据位置、许可证、支持作者入口。"""
from __future__ import annotations

import platform
import tkinter as tk

import brand
import journal
import ui_kit as U

_INFO = [
    ('版本', None),        # 运行时填
    ('构建日期', journal.BUILD),
    ('作者', brand.AUTHOR),
    ('数据目录', None),
]


def _logo(parent, size: int):
    path = brand.asset(brand.ICON_PNG)
    if not path:
        return None
    try:
        img = tk.PhotoImage(file=str(path))
        factor = max(1, img.width() // size)
        return img.subsample(factor, factor)
    except tk.TclError:
        return None


def open_about(parent, suite):
    """打开关于窗口；已开则聚焦。"""
    existing = getattr(suite, '_about', None)
    if existing is not None and existing.winfo_exists():
        existing.deiconify()
        existing.lift()
        existing.focus_force()
        return

    win = tk.Toplevel(parent)
    win.title('关于 · ' + brand.APP_CN)
    win.configure(bg=U.CANVAS)
    win.transient(parent)
    win.resizable(False, False)
    suite._about = win

    head = tk.Frame(win, bg=U.CANVAS)
    head.pack(fill='x', padx=U.s(26), pady=(U.s(24), U.s(6)))

    logo = _logo(head, U.s(88))
    if logo is not None:
        holder = tk.Label(head, image=logo, bg=U.CANVAS)
        holder.image = logo
        holder.pack(side='left', padx=(0, U.s(16)))

    titles = tk.Frame(head, bg=U.CANVAS)
    titles.pack(side='left', fill='x', expand=True)
    tk.Label(titles, text=brand.APP_CN, bg=U.CANVAS, fg=U.INK,
             font=U.display(20)).pack(anchor='w')
    tk.Label(titles, text=brand.TAGLINE, bg=U.CANVAS, fg=U.MUTED,
             font=U.body(10)).pack(anchor='w', pady=(U.s(4), 0))
    tk.Label(titles, text='%s 的作品 · %s' % (brand.AUTHOR, brand.COPYRIGHT),
             bg=U.CANVAS, fg=U.FAINT, font=U.body(9)).pack(anchor='w', pady=(U.s(3), 0))

    card = tk.Frame(win, bg=U.SURFACE, highlightbackground=U.LINE,
                    highlightthickness=1)
    card.pack(fill='x', padx=U.s(26), pady=(U.s(16), 0))

    rows = [
        ('版本', '%s（%s）' % (journal.VERSION, journal.BUILD)),
        ('运行环境', 'Python %s · %s' % (platform.python_version(), '打包版' if U.frozen() else '源码')),
        ('数据目录', str(getattr(suite, 'home_dir', '') or '')),
    ]
    for index, (key, value) in enumerate(rows):
        line = tk.Frame(card, bg=U.SURFACE)
        line.pack(fill='x', padx=U.s(18), pady=(U.s(12) if index == 0 else U.s(6), 0))
        tk.Label(line, text=key, bg=U.SURFACE, fg=U.FAINT, font=U.body(9),
                 width=8, anchor='w').pack(side='left')
        tk.Label(line, text=value, bg=U.SURFACE, fg=U.INK_SOFT, font=U.body(9),
                 anchor='w', justify='left', wraplength=U.s(420)).pack(side='left')

    foot_note = tk.Label(
        card, text='采用 MIT 许可证，分发时请保留版权和许可声明。',
        bg=U.SURFACE, fg=U.MUTED, font=U.body(9), anchor='w', justify='left',
        wraplength=U.s(460))
    foot_note.pack(fill='x', padx=U.s(18), pady=(U.s(12), U.s(14)))

    if brand.CONTACT or brand.HOMEPAGE:
        tk.Label(win, text=(brand.HOMEPAGE or brand.CONTACT), bg=U.CANVAS, fg=U.MUTED,
                 font=U.body(9)).pack(anchor='w', padx=U.s(26), pady=(U.s(8), 0))

    buttons = tk.Frame(win, bg=U.CANVAS)
    buttons.pack(fill='x', padx=U.s(26), pady=(U.s(20), U.s(22)))

    def show_license():
        import support
        support.open_license(win, suite)

    def copy_info():
        text = '\n'.join([
            brand.APP_CN,
            '版本 %s（%s）' % (journal.VERSION, journal.BUILD),
            '作者 %s' % brand.AUTHOR,
            brand.COPYRIGHT,
            'Python %s' % platform.python_version(),
            '数据目录 %s' % (getattr(suite, 'home_dir', '') or ''),
        ])
        win.clipboard_clear()
        win.clipboard_append(text)
        if suite is not None and hasattr(suite, 'write'):
            suite.write('已复制版本信息')

    U.RButton(buttons, '支持作者', command=lambda: _support(win, suite),
              variant='primary', height=36, padx=18, font=U.body(10, 'bold')).pack(side='left')
    U.RButton(buttons, '查看许可证', command=show_license, variant='ghost',
              height=36, padx=14, font=U.body(9)).pack(side='left', padx=(U.s(10), 0))
    U.RButton(buttons, '复制版本信息', command=copy_info, variant='ghost',
              height=36, padx=14, font=U.body(9)).pack(side='left', padx=(U.s(10), 0))
    U.RButton(buttons, '关闭', command=win.destroy, variant='quiet',
              height=36, padx=16, font=U.body(9)).pack(side='right')

    win.bind('<Escape>', lambda event: win.destroy())
    win.update_idletasks()
    _center(win, parent)
    win.grab_set()


def _support(parent, suite):
    import support
    support.open_support(parent, suite)


def _center(win, parent):
    win.update_idletasks()
    width, height = win.winfo_reqwidth(), win.winfo_reqheight()
    x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
    y = parent.winfo_rooty() + max(U.s(40), (parent.winfo_height() - height) // 3)
    win.geometry('+%d+%d' % (x, y))
