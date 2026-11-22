# -*- coding: utf-8 -*-
"""在程序里直接看文本：Markdown 渲染预览 + 粘贴文本顺手转换。

不依赖浏览器：用 Tk 的 Text + tag 画标题、列表、代码、引用、表格，
所以在任何机器上都能用；PDF 那种需要 Edge 的转换仍然走 toolbox。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
import re

import toolbox as T

INLINE = re.compile(r'(\*\*[^*\n]+\*\*|\*[^*\n]+\*|`[^`\n]+`|\[\[[^\]\n]+\]\]'
                    r'|\[[^\]\n]+\]\([^)\n]+\))')

TEXT_SUFFIX = {'.md', '.markdown', '.txt', '.html', '.htm', '.docx'}
FRONT = re.compile(r'\A\s*---\r?\n(.*?)\r?\n---\r?\n', re.S)


def split_front(text):
    """把笔记开头的 frontmatter 拆出来：渲染时不显示，看源码时还在。"""
    match = FRONT.match(text or '')
    if not match:
        return '', text or ''
    return match.group(1), text[match.end():]


def text_from(path):
    """把各种文本文件变成可渲染的 Markdown。"""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in ('.md', '.markdown', '.txt'):
        return T.read(path)
    if suffix in ('.html', '.htm', '.docx'):
        scratch = Path(tempfile.mkdtemp(prefix='view-')) / 'x.md'
        if suffix == '.docx':
            T.docx_to_md(path, scratch)
        else:
            T.html_to_md(path, scratch)
        return T.read(scratch)
    return T.read(path)


def _inline_parts(text):
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith('**') and part.endswith('**') and len(part) > 4:
            yield 'bold', part[2:-2]
        elif part.startswith('`') and part.endswith('`') and len(part) > 2:
            yield 'code', part[1:-1]
        elif part.startswith('[[') and part.endswith(']]'):
            inner = part[2:-2]
            label = inner.split('|')[-1] if '|' in inner else inner
            yield 'link', label
        elif part.startswith('[') and '](' in part:
            label = part[1:part.index('](')] or part[part.index('](') + 2:-1]
            yield 'link', label
        elif part.startswith('*') and part.endswith('*') and len(part) > 2:
            yield 'italic', part[1:-1]
        else:
            yield 'text', part


def _meta_line(meta):
    """把 frontmatter 收成一行小字：notion_id、导入时间、修改时间。"""
    pairs = re.findall(r'"([^"]+)"\s*:\s*"([^"]*)"', meta or '')
    if not pairs:
        keys = [ln.split(':', 1)[0].strip() for ln in (meta or '').splitlines() if ':' in ln]
        return ' · '.join(k for k in keys if k)[:120]
    wanted = {'notion_id': 'Notion 编号', 'imported_at': '导入于', 'notion_last_edited_at': '云端改于'}
    parts = []
    for key, value in pairs:
        if key in wanted:
            parts.append('%s %s' % (wanted[key], value))
    return ' · '.join(parts)[:160]


def style(widget, palette):
    widget.tag_configure('h1', font=('Microsoft YaHei UI', 20, 'bold'),
                         foreground=palette['ink'], spacing1=10, spacing3=8)
    widget.tag_configure('h2', font=('Microsoft YaHei UI', 15, 'bold'),
                         foreground=palette['ink'], spacing1=12, spacing3=6)
    widget.tag_configure('h3', font=('Microsoft YaHei UI', 12, 'bold'),
                         foreground=palette['ink'], spacing1=10, spacing3=4)
    widget.tag_configure('body', font=('Microsoft YaHei UI', 10),
                         foreground=palette['text'], spacing3=6, lmargin2=2)
    widget.tag_configure('bold', font=('Microsoft YaHei UI', 10, 'bold'))
    widget.tag_configure('italic', font=('Microsoft YaHei UI', 10, 'italic'))
    widget.tag_configure('code_inline', font=('Consolas', 10),
                         background=palette['soft'], foreground=palette['accent_dark'])
    widget.tag_configure('link', foreground=palette['accent'], underline=True)
    widget.tag_configure('bullet', font=('Microsoft YaHei UI', 10),
                         foreground=palette['text'], lmargin1=16, lmargin2=34, spacing3=3)
    widget.tag_configure('quote', font=('Microsoft YaHei UI', 10, 'italic'),
                         foreground=palette['muted'], lmargin1=16, lmargin2=16, spacing3=6)
    widget.tag_configure('code', font=('Consolas', 9), background=palette['soft'],
                         foreground=palette['ink'], lmargin1=14, lmargin2=14,
                         spacing1=6, spacing3=8)
    widget.tag_configure('table', font=('Consolas', 9), foreground=palette['muted'],
                         lmargin1=14, lmargin2=14, spacing3=2)


def render(widget, text, palette):
    widget.configure(state='normal')
    widget.delete('1.0', 'end')
    style(widget, palette)
    for kind, value in T.blocks(text):
        if kind == 'heading':
            level, title = value
            widget.insert('end', title + '\n', 'h%d' % min(level, 3))
        elif kind == 'para':
            _inline(widget, value, 'body')
            widget.insert('end', '\n')
        elif kind == 'list':
            for item in value:
                widget.insert('end', '· ', 'bullet')
                _inline(widget, item, 'bullet')
                widget.insert('end', '\n')
            widget.insert('end', '\n')
        elif kind == 'ordered':
            for index, item in enumerate(value, 1):
                widget.insert('end', '%d. ' % index, 'bullet')
                _inline(widget, item, 'bullet')
                widget.insert('end', '\n')
            widget.insert('end', '\n')
        elif kind == 'todo':
            for done, item in value:
                widget.insert('end', ('☑ ' if done else '☐ '), 'bullet')
                _inline(widget, item, 'bullet')
                widget.insert('end', '\n')
            widget.insert('end', '\n')
        elif kind == 'quote':
            for line in value.split('\n'):
                widget.insert('end', '▏' + line + '\n', 'quote')
            widget.insert('end', '\n')
        elif kind == 'code':
            lang, body = value
            widget.insert('end', body + '\n', 'code')
        elif kind == 'table':
            for row in value:
                widget.insert('end', '  '.join(row) + '\n', 'table')
            widget.insert('end', '\n')
        elif kind == 'image':
            alt, src = value
            widget.insert('end', '[图片] %s（%s）\n' % (alt or '未命名', src), 'body')
    widget.configure(state='disabled')


def _inline(widget, text, base):
    for kind, piece in _inline_parts(text):
        if kind == 'text':
            widget.insert('end', piece, base)
        elif kind == 'code':
            widget.insert('end', piece, (base, 'code_inline'))
        elif kind == 'link':
            widget.insert('end', piece, (base, 'link'))
        elif kind == 'bold':
            widget.insert('end', piece, (base, 'bold'))
        elif kind == 'italic':
            widget.insert('end', piece, (base, 'italic'))


class Viewer:
    """一个能打开文件、粘贴文本、在渲染与源码之间切换的窗口。"""

    def __init__(self, parent, suite, path=None):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        self.suite = suite
        self.text = ''
        self.full = ''
        self.path = None
        accent = getattr(suite, 'ACCENT', '#2F7D74')
        self.accent = accent
        self.accent_dark = getattr(suite, 'ACCENT_DARK', accent)
        palette = {'ink': getattr(suite, 'INK', '#10161F'),
                   'text': getattr(suite, 'TEXT', getattr(suite, 'INK', '#10161F')),
                   'muted': getattr(suite, 'MUTED', '#4E5A66'),
                   'soft': getattr(suite, 'SOFT', '#EAF0EE'),
                   'accent': accent,
                   'accent_dark': getattr(suite, 'ACCENT_DARK', accent)}

        win = tk.Toplevel(parent)
        win.title('查看文本')
        win.geometry('%dx%d' % (U.s(980), U.s(760)))
        win.minsize(U.s(640), U.s(480))
        win.configure(bg=suite.BG)
        self.win = win

        bar = tk.Frame(win, bg=suite.BG)
        bar.pack(fill='x', padx=14, pady=(12, 0))
        tk.Label(bar, text='查看文本', bg=suite.BG, fg=suite.INK,
                 font=('Microsoft YaHei UI', 14, 'bold')).pack(side='left')
        self.name = tk.StringVar(value='没有打开文件')
        tk.Label(bar, textvariable=self.name, bg=suite.BG, fg=suite.MUTED,
                 font=('Microsoft YaHei UI', 9)).pack(side='left', padx=(10, 0))

        ttk.Button(bar, text='打开文件', style='Small.TButton',
                   command=self.open_file).pack(side='right')
        ttk.Button(bar, text='粘贴文本', style='Small.TButton',
                   command=self.paste).pack(side='right', padx=(0, 6))
        self.raw = tk.BooleanVar(value=False)
        box = tk.Checkbutton(bar, text='显示源码', variable=self.raw, command=self.refresh,
                             bg=suite.BG, fg=suite.MUTED, activebackground=suite.BG,
                             activeforeground=suite.MUTED, selectcolor=suite.BG, bd=0,
                             highlightthickness=0, font=('Microsoft YaHei UI', 9))
        box.pack(side='right', padx=(0, 10))

        card = tk.Frame(win, bg=palette['soft'])
        card.pack(fill='both', expand=True, padx=14, pady=(10, 12))
        self.body = tk.Text(card, wrap='word', relief='flat', bd=0, padx=22, pady=18,
                            bg='#FFFFFF', fg=palette['text'], insertwidth=0,
                            font=('Microsoft YaHei UI', 10))
        scroll = ttk.Scrollbar(card, orient='vertical', command=self.body.yview)
        self.body.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.body.pack(side='left', fill='both', expand=True)
        self.body.bind('<Key>', lambda event: 'break')
        self.palette = palette

        if path:
            self.load(path)
        else:
            self.show_text('# 查看文本\n\n点右上角「打开文件」或「粘贴文本」。\n\n'
                           '支持 Markdown、纯文本、HTML、Word（.docx）。')

    # ---- 操作 ----
    def open_file(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            parent=self.win, title='打开文件',
            filetypes=[('文本与笔记', '*.md *.markdown *.txt *.html *.htm *.docx'),
                       ('所有文件', '*.*')])
        if path:
            self.load(path)

    def load(self, path):
        from tkinter import messagebox
        self.path = Path(path)
        try:
            self.full = text_from(self.path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror('打不开这个文件', '读不出来：%s' % exc, parent=self.win)
            return
        self.name.set(self.path.name)
        self.refresh()

    def paste(self):
        import tkinter as tk
        win = tk.Toplevel(self.win)
        win.title('粘贴文本')
        win.geometry('%dx%d' % (U.s(760), U.s(560)))
        win.configure(bg=self.suite.BG)
        tk.Label(win, text='粘贴文本，可以直接看，也可以转成 Word、PDF、HTML', bg=self.suite.BG,
                 fg=self.suite.MUTED, font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=14, pady=(12, 6))
        area = tk.Text(win, wrap='word', relief='flat', padx=12, pady=10,
                       font=('Microsoft YaHei UI', 10), bg='#FFFFFF')
        area.pack(fill='both', expand=True, padx=14)
        area.focus_set()

        def use():
            self.full = area.get('1.0', 'end').rstrip('\n')
            self.path = None
            self.name.set('粘贴的文本')
            win.destroy()
            self.refresh()

        row = tk.Frame(win, bg=self.suite.BG)
        row.pack(fill='x', padx=14, pady=10)
        tk.Button(row, text='就用这段', command=use, relief='flat',
                  bg=self.accent, fg='#FFFFFF', activebackground=self.accent_dark,
                  activeforeground='#FFFFFF', padx=18, pady=7, bd=0,
                  font=('Microsoft YaHei UI', 10, 'bold')).pack(side='right')
        tk.Button(row, text='取消', command=win.destroy, relief='flat', bg='#EAF0EE',
                  fg=self.suite.MUTED, padx=14, pady=7, bd=0,
                  font=('Microsoft YaHei UI', 9)).pack(side='right', padx=(0, 8))

    def refresh(self):
        if self.raw.get():
            self.body.configure(state='normal')
            self.body.delete('1.0', 'end')
            self.body.insert('end', self.full, 'raw')
            self.body.configure(state='disabled')
            return
        meta, body = split_front(self.full)
        render(self.body, body, self.palette)
        line = _meta_line(meta)
        if line:
            self.body.configure(state='normal')
            self.body.insert('1.0', line + '\n\n', 'table')
            self.body.mark_set('insert', '1.0')
            self.body.configure(state='disabled')

    def show_text(self, text):
        self.text = text
        self.full = text
        self.refresh()


def open_view(parent, suite, path=None):
    return Viewer(parent, suite, path)


def paste_convert(parent, suite, palette=None):
    """粘贴文本 → 选一种格式 → 直接生成文件。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    targets = [('Word（.docx）', '.docx', T.md_to_docx),
               ('PDF', '.pdf', T.md_to_pdf),
               ('HTML', '.html', T.md_to_html),
               ('纯文本', '.txt', T.md_to_txt)]

    accent = getattr(suite, 'ACCENT', '#2F7D74')
    accent_dark = getattr(suite, 'ACCENT_DARK', accent)
    win = tk.Toplevel(parent)
    win.title('粘贴文本并转换')
    win.geometry('%dx%d' % (U.s(820), U.s(620)))
    win.configure(bg=suite.BG)
    tk.Label(win, text='粘贴文本 → 选格式 → 生成文件', bg=suite.BG, fg=suite.INK,
             font=('Microsoft YaHei UI', 13, 'bold')).pack(anchor='w', padx=14, pady=(12, 2))
    tk.Label(win, text='支持 Markdown 语法；普通文字按段落排。', bg=suite.BG,
             fg=suite.MUTED, font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=14)

    area = tk.Text(win, wrap='word', relief='flat', padx=12, pady=10,
                   font=('Microsoft YaHei UI', 10), bg='#FFFFFF')
    area.pack(fill='both', expand=True, padx=14, pady=(10, 8))
    area.focus_set()

    row = tk.Frame(win, bg=suite.BG)
    row.pack(fill='x', padx=14, pady=(0, 12))
    choice = ttk.Combobox(row, values=[name for name, _, _ in targets],
                          state='readonly', width=22, font=('Microsoft YaHei UI', 10))
    choice.current(0)
    choice.pack(side='left')

    def run():
        name, suffix, func = targets[choice.current()]
        body = area.get('1.0', 'end').rstrip('\n')
        if not body.strip():
            messagebox.showinfo('还没有内容', '先粘贴一段文本。', parent=win)
            return
        dst = filedialog.asksaveasfilename(parent=win, title='保存为', defaultextension=suffix,
                                           initialfile='粘贴文本' + suffix)
        if not dst:
            return
        scratch = Path(tempfile.mkdtemp(prefix='paste-')) / 'text.md'
        scratch.write_text(body, encoding='utf-8', newline='')
        try:
            func(scratch, Path(dst))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror('转换失败', str(exc), parent=win)
            return
        suite.write('已生成：%s（%s）' % (dst, name), 'ok')
        win.destroy()

    tk.Button(row, text='转换并保存', command=run, relief='flat',
              bg=accent, fg='#FFFFFF', activebackground=accent_dark,
              activeforeground='#FFFFFF', padx=18, pady=7, bd=0,
              font=('Microsoft YaHei UI', 10, 'bold')).pack(side='right')
    tk.Button(row, text='取消', command=win.destroy, relief='flat', bg='#EAF0EE',
              fg=suite.MUTED, padx=14, pady=7, bd=0,
              font=('Microsoft YaHei UI', 9)).pack(side='right', padx=(0, 8))
