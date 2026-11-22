# -*- coding: utf-8 -*-
"""咕月15 的颜色、字阶和可用键盘操作的桌面控件。"""
from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

import dpi

def scale():
    """当前缩放比例（现算，不做缓存）。"""
    return dpi.scale()


def s(value):
    """逻辑像素 → 当前缩放下的像素。"""
    return dpi.s(value)


# ---------- 表面、按钮和状态 ----------
CANVAS = '#F3F5F4'
SURFACE = '#FFFFFF'
SUNKEN = '#EAF0EE'
RAIL = '#232A3D'
RAIL_HOVER = '#2C3448'
RAIL_ACTIVE = '#39425A'
LINE = '#DDE5E2'
LINE_STRONG = '#C3D0CB'
INK = '#10161F'
INK_SOFT = '#2A3340'
MUTED = '#4E5A66'
FAINT = '#6B7683'
ON_RAIL = '#AEB9C6'
ON_RAIL_BRIGHT = '#FFFFFF'
ACCENT = '#2F7D74'
ACCENT_HOVER = '#245F59'
ACCENT_SOFT = '#E4EFEC'
OK = '#14713F'
OK_SOFT = '#E3F1E8'
WARN = '#9A5B00'
WARN_SOFT = '#F7EEDD'
ERR = '#B3261E'
ERR_SOFT = '#F8E7E5'

# 兼容老名字（页面里还在用）
BG = CANVAS
CARD = SURFACE
SOFT = SUNKEN
TEXT = INK
ACCENT_DARK = ACCENT_HOVER


BOOST = 2
UI_FONT = 'Microsoft YaHei UI'


def _up(size):
    return int(size) + BOOST


def display(size=20, weight='normal'):
    return (UI_FONT, _up(size), 'normal' if weight == 'light' else 'bold')


def label(size=9):
    return (UI_FONT, _up(size))


def body(size=10, weight='normal'):
    return (UI_FONT, _up(size), weight)


def mono(size=10, weight='normal'):
    return ('Cascadia Mono', _up(size), weight) if weight != 'normal' else ('Cascadia Mono', _up(size))


def measure(widget, text, fnt):
    handle = tkfont.Font(root=widget, font=fnt)
    return handle.measure(text), handle.metrics('linespace')


def round_rect(canvas, x1, y1, x2, y2, radius=8, **kw):
    """用平滑多边形画圆角矩形（Tk 没有原生圆角）。"""
    radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
        x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
        x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kw)


class RButton(tk.Canvas):
    """圆角按钮。variant: primary / ghost / quiet / danger。"""

    VARIANTS = {
        'primary': {'bg': ACCENT_HOVER, 'fg': '#FFFFFF', 'hover': '#1B4F4A', 'border': ''},
        'ghost': {'bg': SURFACE, 'fg': INK, 'hover': SUNKEN, 'border': LINE_STRONG},
        'quiet': {'bg': '', 'fg': MUTED, 'hover': SUNKEN, 'border': ''},
        'danger': {'bg': ERR, 'fg': '#FFFFFF', 'hover': '#7F1D18', 'border': ''},
        'accent': {'bg': ACCENT, 'fg': '#FFFFFF', 'hover': ACCENT_HOVER, 'border': ''},
    }

    def __init__(self, parent, text='', command=None, variant='ghost', height=34,
                 padx=16, radius=9, font=None, width=None, bg=None):
        height, padx, radius = s(height), s(padx), s(radius)
        self.palette = dict(self.VARIANTS.get(variant, self.VARIANTS['ghost']))
        parent_bg = bg or _bg_of(parent)
        self.parent_bg = parent_bg
        self.font = font or body(10)
        self.text = text
        self.command = command
        self.height = height
        self.padx = padx
        self.radius = radius
        self.enabled = True
        self.hover = False
        text_w, _ = measure(parent, text, self.font)
        size_w = width or (text_w + padx * 2)
        super().__init__(parent, width=size_w, height=height, bg=self.parent_bg,
                         highlightthickness=0, bd=0, cursor='hand2', takefocus=True)
        self._size = (size_w, height)
        self.bind('<Configure>', lambda event: self._draw())
        self.bind('<Enter>', self._on_enter)
        self.bind('<Leave>', self._on_leave)
        self.bind('<Button-1>', self._on_press)
        self.bind('<ButtonRelease-1>', self._on_release)
        self.bind('<FocusIn>', lambda event: self._draw())
        self.bind('<FocusOut>', lambda event: self._draw())
        self.bind('<Return>', self._activate)
        self.bind('<space>', self._activate)
        self._draw()

    # ---- 绘制 ----
    def _draw(self):
        self.delete('all')
        width = max(self.winfo_width(), self._size[0])
        height = self.height
        fill = self.palette['bg']
        if self.hover and self.enabled:
            fill = self.palette['hover']
        if not self.enabled:
            fill = SUNKEN
        if fill:
            round_rect(self, 1, 1, width - 1, height - 1, self.radius,
                       fill=fill, outline=self.palette['border'] or fill)
        foreground = self.palette['fg']
        if not self.enabled:
            foreground = MUTED
        self.create_text(width / 2, height / 2 + 1, text=self.text,
                         fill=foreground, font=self.font)
        if self.enabled and self.focus_get() == self:
            round_rect(self, 1, 1, width - 1, height - 1, self.radius, fill='', outline=ACCENT, width=2)

    # ---- 事件 ----
    def _on_enter(self, _event=None):
        self.hover = True
        self._draw()

    def _on_leave(self, _event=None):
        self.hover = False
        self._draw()

    def _on_press(self, _event=None):
        if self.enabled:
            self.focus_set()
            self.move('all', 0, 1)

    def _on_release(self, event=None):
        self._draw()
        if event is None or (0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()):
            return self._activate()

    def _activate(self, _event=None):
        if self.enabled and self.command:
            self.command()
        return 'break'

    # ---- 兼容 tk 的 configure(state=…) / cget('state') ----
    def configure(self, cnf=None, **kw):
        state = kw.pop('state', None)
        text = kw.pop('text', None)
        if state is not None:
            self.set_enabled(str(state) != 'disabled')
        if text is not None:
            self.set_text(text)
        if kw or cnf:
            return super().configure(cnf, **kw)
        return None

    config = configure

    def cget(self, key):
        if key == 'state':
            return 'normal' if self.enabled else 'disabled'
        return super().cget(key)

    def set_primary(self, on):
        """在「主操作」与「次操作」两种外观之间切换。"""
        self.palette = dict(self.VARIANTS['primary' if on else 'ghost'])
        if not self.palette['bg']:
            self.palette['bg'] = self.parent_bg
        self.configure(bg=self.parent_bg)
        self._draw()

    def set_enabled(self, value):
        self.enabled = bool(value)
        self.configure(cursor='hand2' if self.enabled else 'arrow', takefocus=self.enabled)
        self._draw()

    def set_text(self, text):
        self.text = text
        text_w, _ = measure(self, text, self.font)
        width = text_w + self.padx * 2
        if width != self._size[0]:
            self._size = (width, self.height)
            super().configure(width=width)
        self._draw()


def card(parent, padx=20, pady=16, bg=SURFACE):
    padx, pady = s(padx), s(pady)
    outer = tk.Frame(parent, bg=bg, highlightbackground=LINE, highlightthickness=1,
                     bd=0)
    inner = tk.Frame(outer, bg=bg)
    inner.pack(fill='both', expand=True, padx=padx, pady=pady)
    outer.inner = inner
    return outer


class NavRail(tk.Frame):
    """左侧深色导航：品牌 + 功能项 + 底部状态。"""

    def __init__(self, parent, title, subtitle, version, on_pick, icon=None):
        super().__init__(parent, bg=RAIL, width=s(194))
        self.pack_propagate(False)
        self.on_pick = on_pick
        self.items = {}
        self.active = None

        head = tk.Frame(self, bg=RAIL)
        head.pack(fill='x', padx=s(20), pady=(s(22), s(18)))
        if icon is not None:
            mark = tk.Label(head, image=icon, bg=RAIL, bd=0)
            mark.pack(side='left')
            self.mark = mark      # 供「连点五次」这类彩蛋绑定
        else:
            mark = tk.Canvas(head, width=s(26), height=s(26), bg=RAIL, highlightthickness=0)
            mark.pack(side='left')
            round_rect(mark, 1, 1, s(26) - 1, s(26) - 1, s(7), fill=ACCENT)
            mark.create_text(s(13), s(14), text='N', fill='#FFFFFF', font=display(13))
            self.mark = mark
        tk.Label(head, text=title, bg=RAIL, fg=ON_RAIL_BRIGHT, anchor='w',
                 font=display(13)).pack(side='left', padx=(s(10), 0))

        tk.Label(self, text=subtitle, bg=RAIL, fg=ON_RAIL, anchor='w', justify='left',
                 wraplength=s(180), font=body(8)).pack(fill='x', padx=s(22), pady=(0, s(18)))

        self.nav = tk.Frame(self, bg=RAIL)
        self.nav.pack(fill='x')

        self.footer = tk.Frame(self, bg=RAIL)
        self.footer.pack(side='bottom', fill='x', padx=s(20), pady=s(18))
        self.footer_label = tk.Label(self.footer, text='未连接', bg=RAIL, fg=ON_RAIL,
                                     anchor='w', justify='left', wraplength=180,
                                     font=body(8))
        self.footer_label.pack(fill='x')
        tk.Label(self.footer, text='版本 ' + version, bg=RAIL, fg=ON_RAIL,
                 anchor='w', font=label(8)).pack(fill='x', pady=(6, 0))

    def add(self, key, text, hint=''):
        row = tk.Frame(self.nav, bg=RAIL, cursor='hand2')
        row.pack(fill='x', padx=12, pady=1)
        canvas = tk.Canvas(row, height=s(36), bg=RAIL, highlightthickness=0, bd=0, takefocus=True)
        canvas.pack(fill='x')
        state = {'hover': False}

        def draw():
            canvas.delete('all')
            width = canvas.winfo_width() or 200
            height = canvas.winfo_height()
            active = self.active == key
            focused = canvas.focus_get() == canvas
            if active or state['hover'] or focused:
                round_rect(canvas, 1, 3, width - 1, height - 3, s(6),
                           fill=RAIL_ACTIVE if active else RAIL_HOVER,
                           outline=ON_RAIL if focused else '')
            if active:
                canvas.create_rectangle(2, height // 3, 4, height * 2 // 3, fill=ACCENT, outline='')
            canvas.create_text(s(11), height // 2, text=text, anchor='w',
                               fill=ON_RAIL_BRIGHT if active else ON_RAIL,
                               font=body(10, 'bold') if active else body(10))

        def enter(_event=None):
            state['hover'] = True
            draw()

        def leave(_event=None):
            state['hover'] = False
            draw()

        canvas.bind('<Configure>', lambda event: draw())
        canvas.bind('<Enter>', enter)
        canvas.bind('<Leave>', leave)
        canvas.bind('<FocusIn>', lambda event: draw())
        canvas.bind('<FocusOut>', lambda event: draw())

        def pick(_event=None):
            canvas.focus_set()
            self.on_pick(key)
            return 'break'

        for event in ('<Button-1>', '<Return>', '<space>'):
            canvas.bind(event, pick)
        self.items[key] = (canvas, draw, hint)

    def select(self, key):
        self.active = key
        for _key, (_canvas, draw, _hint) in self.items.items():
            draw()

    def set_footer(self, text, color=ON_RAIL):
        self.footer_label.configure(text=text, fg=color)


class Check(tk.Canvas):
    """自己画的勾选框：方块比系统自带的大，点击整行都能切换。

    var 必须是 tk.BooleanVar / IntVar；on_change 可选。
    """

    def __init__(self, parent, text='', var=None, command=None, bg=None, fg=None,
                 box=20, font=None):
        self.bg = bg or _bg_of(parent)
        self.var = var or tk.BooleanVar(value=False)
        self.command = command
        self.fg = fg or INK
        self.font = font or body(10)
        self.box = s(box)
        pad = s(4)
        width, height = self._measure(parent, text, pad)
        super().__init__(parent, width=width, height=height, bg=self.bg,
                         highlightthickness=1, highlightbackground=self.bg, highlightcolor=ACCENT,
                         bd=0, cursor='hand2', takefocus=True)
        self.text = text
        self.pad = pad
        self._draw()
        self.bind('<Button-1>', self._toggle)
        self.bind('<space>', self._toggle)

    def _measure(self, parent, text, pad):
        width, line = measure(parent, text, self.font)
        return width + self.box + pad * 3, max(self.box + s(6), line + s(4))

    def _draw(self):
        self.delete('all')
        h = int(self.cget('height'))
        top = (h - self.box) // 2
        on = bool(self.var.get())
        if on:
            round_rect(self, 1, top, self.box, top + self.box, s(5), fill=INK, outline=INK)
            self.create_line(s(5), top + self.box // 2, s(9), top + self.box - s(6),
                             s(16), top + s(5), fill='#FFFFFF', width=max(2, s(2)),
                             capstyle='round', joinstyle='round')
        else:
            round_rect(self, 1, top, self.box, top + self.box, s(5), fill=SURFACE,
                       outline=LINE_STRONG, width=s(1))
        self.create_text(self.box + self.pad, h // 2, text=self.text, anchor='w',
                         fill=self.fg, font=self.font)

    def _toggle(self, _event=None):
        if self.cget('state') == 'disabled':
            return 'break'
        self.focus_set()
        self.var.set(not bool(self.var.get()))
        self._draw()
        if self.command:
            self.command()
        return 'break'

    def get(self):
        return bool(self.var.get())

    def set(self, value):
        self.var.set(bool(value))
        self._draw()


def _bg_of(parent):
    try:
        return parent.cget('bg')
    except tk.TclError:
        if isinstance(parent, ttk.Widget):
            style = parent.cget('style') or parent.winfo_class()
            return ttk.Style(parent).lookup(style, 'background') or CANVAS
    return CANVAS
