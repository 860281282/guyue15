# -*- coding: utf-8 -*-
"""支持作者 / 许可证：收款码页面与许可证查看。

设计原则（写在代码里，避免以后被改跑偏）：
- 全部功能免费，收款码只作为「愿意支持就支持」的入口，绝不与功能挂钩；
- 不主动弹窗打扰；入口固定在「关于」与「更多」菜单里；
- 换码不需要重新打包：exe 同级放 收款码.png 即可覆盖内置图。
"""
from __future__ import annotations

import tkinter as tk

import brand
import ui_kit as U


def _image(parent, path, size: int):
    if not path:
        return None
    try:
        img = tk.PhotoImage(file=str(path))
    except tk.TclError:
        return None
    factor = max(1, round(img.width() / size))
    if factor > 1:
        img = img.subsample(factor, factor)
    return img


def _center(win, parent):
    win.update_idletasks()
    width, height = win.winfo_reqwidth(), win.winfo_reqheight()
    x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
    y = parent.winfo_rooty() + max(U.s(30), (parent.winfo_height() - height) // 3)
    win.geometry('+%d+%d' % (x, y))


def open_support(parent, suite=None):
    """支持作者页面。"""
    existing = getattr(suite, '_support', None) if suite is not None else None
    if existing is not None and existing.winfo_exists():
        existing.deiconify()
        existing.lift()
        existing.focus_force()
        return

    win = tk.Toplevel(parent)
    win.title('支持作者 · ' + brand.AUTHOR)
    win.configure(bg=U.CANVAS)
    win.transient(parent)
    win.resizable(False, False)
    if suite is not None:
        suite._support = win

    tk.Label(win, text='支持作者', bg=U.CANVAS, fg=U.INK,
             font=U.display(20)).pack(anchor='w', padx=U.s(26), pady=(U.s(22), U.s(2)))
    tk.Label(win, text='%s 的作品。免费使用，不锁任何功能。' % brand.AUTHOR, bg=U.CANVAS,
             fg=U.MUTED, font=U.body(10)).pack(anchor='w', padx=U.s(26))

    card = tk.Frame(win, bg=U.SURFACE, highlightbackground=U.LINE, highlightthickness=1)
    card.pack(fill='both', expand=True, padx=U.s(26), pady=(U.s(14), 0))

    body = tk.Frame(card, bg=U.SURFACE)
    body.pack(fill='both', expand=True, padx=U.s(20), pady=U.s(18))

    left = tk.Frame(body, bg=U.SURFACE)
    left.pack(side='left', fill='y')
    right = tk.Frame(body, bg=U.SURFACE)
    right.pack(side='left', fill='both', expand=True, padx=(U.s(20), 0))

    wechat = brand.qr('wechat')
    target = wechat
    shot = _image(left, target, U.s(230)) if target else None
    if shot is not None:
        label = tk.Label(left, image=shot, bg=U.SURFACE)
        label.image = shot
        label.pack()
        tk.Label(left, text='用微信扫这个码', bg=U.SURFACE, fg=U.MUTED,
                 font=U.body(9)).pack(pady=(U.s(8), 0))
    else:
        tk.Label(left, text='（还没有收款码图片）', bg=U.SURFACE, fg=U.MUTED,
                 font=U.body(9)).pack()

    lines = [
        ('h', '这个程序是免费的'),
        ('p', '所有功能都不收费、不限制次数。下面的码只是「如果你愿意」时的支持入口，'
              '不扫也完全不影响使用，也不会有任何功能被锁住。'),
        ('h', '真要帮忙，这几件事比钱有用'),
        ('li', '遇到问题把「导出诊断包」发给我，比描述十句话都准'),
        ('li', '觉得好用就推荐给同样在用 Notion + Obsidian 的人'),
        ('li', '发现同步结果不对，先用示例数据复现，再报告问题'),
    ]
    for kind, text in lines:
        if kind == 'h':
            tk.Label(right, text=text, bg=U.SURFACE, fg=U.INK, font=U.body(11, 'bold'),
                     anchor='w', justify='left').pack(fill='x', pady=(0, U.s(4)))
        elif kind == 'li':
            row = tk.Frame(right, bg=U.SURFACE)
            row.pack(fill='x', pady=(0, U.s(6)))
            tk.Label(row, text='·', bg=U.SURFACE, fg=U.FAINT, font=U.body(9)).pack(
                side='left', anchor='n', padx=(0, U.s(6)))
            tk.Label(row, text=text, bg=U.SURFACE, fg=U.INK_SOFT, font=U.body(9),
                     anchor='w', justify='left', wraplength=U.s(310)).pack(
                side='left', fill='x', expand=True)
        else:
            tk.Label(right, text=text, bg=U.SURFACE, fg=U.INK_SOFT, font=U.body(9),
                     anchor='w', justify='left', wraplength=U.s(330)).pack(
                fill='x', pady=(0, U.s(12)))


    foot = tk.Frame(win, bg=U.CANVAS)
    foot.pack(fill='x', padx=U.s(26), pady=(U.s(14), U.s(18)))

    quiet = None
    if suite is not None:
        quiet = tk.BooleanVar(value=bool(getattr(suite, 'hide_support_hint', None)
                                         and suite.hide_support_hint.get()))

        def toggle():
            if hasattr(suite, 'set_support_hint'):
                suite.set_support_hint(not quiet.get())

        U.Check(foot, '不再显示支持提示', var=quiet, command=toggle,
                bg=U.CANVAS, font=U.body(9)).pack(side='left')

    U.RButton(foot, '关闭', command=win.destroy, variant='quiet',
              height=36, padx=18, font=U.body(9)).pack(side='right')
    if brand.CONTACT:
        tk.Label(foot, text=brand.CONTACT, bg=U.CANVAS, fg=U.MUTED,
                 font=U.body(9)).pack(side='right', padx=U.s(12))

    win.bind('<Escape>', lambda event: win.destroy())
    _center(win, parent)
    win.grab_set()


def open_license(parent, suite=None):
    """显示 LICENSE.txt 原文。"""
    path = brand.asset(brand.LICENSE_FILE)
    text = ''
    if path:
        try:
            text = path.read_text(encoding='utf-8')
        except OSError as exc:
            text = '读取失败：%s' % exc
    if not text:
        text = '没有找到 LICENSE.txt。\n本项目采用 MIT 许可证。'

    win = tk.Toplevel(parent)
    win.title('许可证')
    win.configure(bg=U.CANVAS)
    win.transient(parent)
    tk.Label(win, text='使用许可', bg=U.CANVAS, fg=U.INK,
             font=U.display(16)).pack(anchor='w', padx=U.s(22), pady=(U.s(18), U.s(4)))
    tk.Label(win, text='MIT 许可证 · 保留署名', bg=U.CANVAS, fg=U.MUTED,
             font=U.body(9)).pack(anchor='w', padx=U.s(22))

    frame = tk.Frame(win, bg=U.SURFACE, highlightbackground=U.LINE, highlightthickness=1)
    frame.pack(fill='both', expand=True, padx=U.s(22), pady=(U.s(12), 0))
    box = tk.Text(frame, wrap='word', relief='flat', bd=0, bg=U.SURFACE, fg=U.INK_SOFT,
                  font=U.body(9), padx=U.s(14), pady=U.s(12), height=18, width=60)
    box.insert('1.0', text)
    box.configure(state='disabled')
    box.pack(fill='both', expand=True)

    U.RButton(win, '关闭', command=win.destroy, variant='quiet', height=36,
              padx=18, font=U.body(9)).pack(anchor='e', padx=U.s(22), pady=U.s(14))
    win.bind('<Escape>', lambda event: win.destroy())
    _center(win, parent)
    win.grab_set()
