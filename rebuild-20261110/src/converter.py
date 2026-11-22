# -*- coding: utf-8 -*-
"""左右的文本转换窗口：左边输入 Markdown，右边看结果，选格式后生成。

设计约束见 DESIGN.md：一屏一个主按钮（生成），其余是文字按钮与格式芯片。
"""

import os
import shutil
import tempfile
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

import toolbox as T
import ui_kit as U
import viewer as V

FORMATS = [
    ('word', 'Word（.docx）', '.docx'),
    ('pdf', 'PDF', '.pdf'),
    ('html', 'HTML', '.html'),
    ('text', '纯文本', '.txt'),
    ('notion', '→ Notion', '.md'),
]

SAMPLE = """# 标题从这里开始

直接粘贴 Markdown，右边会显示生成后的样子。

- 列表、表格、代码块都支持
- 图片按相对路径找
"""


class Converter:
    """输入、预览，点「生成」。"""

    def __init__(self, parent, suite):
        self.suite = suite
        self.kind = 'word'
        self.temp = None
        self.suffix = '.docx'
        self.label = 'Word（.docx）'
        self.busy = False

        palette = {
            'ink': U.INK,
            'text': U.INK_SOFT,
            'muted': U.MUTED,
            'soft': U.SUNKEN,
            'accent': U.ACCENT,
            'accent_dark': U.ACCENT,
        }
        self.palette = palette

        win = tk.Toplevel(parent)
        win.title('文本转换')
        win.geometry('%dx%d' % (U.s(1180), U.s(820)))
        win.minsize(U.s(900), U.s(560))
        win.configure(bg=U.CANVAS)
        self.win = win

        head = tk.Frame(win, bg=U.CANVAS)
        head.pack(fill='x', padx=28, pady=(20, 0))
        tk.Label(head, text='文本转换', bg=U.CANVAS, fg=U.INK,
                 font=U.display(20)).pack(side='left')
        tk.Label(head, text='左边输入 Markdown，右边看结果', bg=U.CANVAS, fg=U.MUTED,
                 font=U.body(9)).pack(side='left', padx=(12, 0))

        foot = tk.Frame(win, bg=U.CANVAS)
        foot.pack(side='bottom', fill='x', padx=28, pady=(12, 18))

        body = tk.Frame(win, bg=U.CANVAS)
        body.pack(fill='both', expand=True, padx=28, pady=(14, 0))
        body.grid_columnconfigure(0, weight=1, uniform='pane')
        body.grid_columnconfigure(1, weight=1, uniform='pane')
        body.grid_rowconfigure(0, weight=1)

        # ---- 左：输入 ----
        left = U.card(body)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        inner = left.inner
        tk.Label(inner, text='输入', bg=U.SURFACE, fg=U.MUTED,
                 font=U.label(8)).pack(anchor='w')
        self.src = tk.Text(inner, wrap='word', relief='flat', bd=0, undo=True,
                           bg=U.SURFACE, fg=U.INK, insertbackground=U.INK,
                           font=U.body(10), padx=2, pady=8, spacing3=4, height=18)
        bar = tk.Scrollbar(inner, orient='vertical', command=self.src.yview,
                           width=10, bd=0, relief='flat')
        self.src.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        self.src.pack(fill='both', expand=True)
        self.src.insert('1.0', SAMPLE)
        tools = tk.Frame(inner, bg=U.SURFACE)
        tools.pack(fill='x', pady=(8, 0))
        tk.Label(tools, text='可以直接粘贴，也可以打开本地文件', bg=U.SURFACE,
                 fg=U.FAINT, font=U.body(8)).pack(side='left')
        U.RButton(tools, '载入文件', command=self.load_file, variant='quiet',
                  height=30, font=U.body(9)).pack(side='right')
        U.RButton(tools, '清空', command=self.clear, variant='quiet',
                  height=30, font=U.body(9)).pack(side='right', padx=(0, 6))

        # ---- 右：输出 ----
        right = U.card(body)
        right.grid(row=0, column=1, sticky='nsew', padx=(8, 0))
        out = right.inner
        tk.Label(out, text='输出预览', bg=U.SURFACE, fg=U.MUTED,
                 font=U.label(8)).pack(anchor='w')
        holder = tk.Frame(out, bg=U.SURFACE)
        holder.pack(fill='both', expand=True)
        self.out = tk.Text(holder, wrap='word', relief='flat', bd=0, bg=U.SURFACE,
                           fg=U.INK, insertwidth=0, padx=2, pady=8,
                           font=U.body(10), cursor='arrow')
        obar = tk.Scrollbar(holder, orient='vertical', command=self.out.yview,
                            width=10, bd=0, relief='flat')
        self.out.configure(yscrollcommand=obar.set)
        obar.pack(side='right', fill='y')
        self.out.pack(fill='both', expand=True)
        V.style(self.out, palette)
        self.out.configure(state='disabled')

        self.status = tk.StringVar(value='还没有生成')
        bottom = tk.Frame(out, bg=U.SURFACE)
        bottom.pack(fill='x', pady=(8, 0))
        tk.Label(bottom, textvariable=self.status, bg=U.SURFACE, fg=U.FAINT,
                 font=U.body(8)).pack(side='left')

        # ---- 底部：格式 + 生成 + 保存（foot 已在上面占位）----
        tk.Label(foot, text='输出格式', bg=U.CANVAS, fg=U.MUTED,
                 font=U.label(8)).pack(side='left', padx=(0, 10))
        self.chips = {}
        for key, label, suffix in FORMATS:
            chip = U.RButton(foot, label, command=lambda k=key: self.pick(k),
                             variant='ghost', height=34, padx=14, font=U.body(9))
            chip.pack(side='left', padx=(0, 6))
            self.chips[key] = chip
        self.save_button = U.RButton(foot, '保存文件', command=self.save,
                                     variant='quiet', height=38, font=U.body(9))
        self.save_button.pack(side='right')
        self.gen_button = U.RButton(foot, '生成', command=self.generate,
                                    variant='primary', height=38, padx=26,
                                    font=U.body(10, 'bold'))
        self.gen_button.pack(side='right', padx=(0, 8))
        self.save_button.set_enabled(False)

        self.preview(self.src.get('1.0', 'end-1c'))
        self.pick('word')

    # ---- 格式 ----
    def pick(self, key):
        if key != self.kind:
            self.temp = None
            self.save_button.set_enabled(False)
            self.save_button.set_primary(False)
            self.status.set('还没有生成 %s，点「生成」' % dict((k, l) for k, l, _ in FORMATS)[key])
        self.kind = key
        self.suffix = dict((k, s) for k, _, s in FORMATS)[key]
        self.label = dict((k, l) for k, l, _ in FORMATS)[key]
        for k, chip in self.chips.items():
            chip.set_primary(k == key)
        self.preview(self.src.get('1.0', 'end-1c'))

    # ---- 预览 ----
    def preview(self, text):
        self.out.configure(state='normal')
        self.out.delete('1.0', 'end')
        if not text.strip():
            self.out.insert('end', '左边输入内容后，这里显示生成结果。', 'quote')
        else:
            V.render(self.out, text, self.palette)
        self.out.configure(state='disabled')

    # ---- 输入动作 ----
    def clear(self):
        self.src.delete('1.0', 'end')
        self.temp = None
        self.save_button.set_enabled(False)
        self.status.set('还没有生成')
        self.preview('')

    def load_file(self):
        path = filedialog.askopenfilename(
            parent=self.win, title='打开文件',
            filetypes=[('文本 / Markdown', '*.md *.markdown *.txt'), ('所有文件', '*.*')])
        if not path:
            return
        try:
            text = V.text_from(Path(path))
        except Exception as exc:
            messagebox.showerror('打不开这个文件', '%s' % exc, parent=self.win)
            return
        self.src.delete('1.0', 'end')
        self.src.insert('1.0', text)
        self.status.set('已载入 %s' % Path(path).name)
        self.preview(text)

    # ---- 生成 ----
    def generate(self):
        if self.busy:
            return
        text = self.src.get('1.0', 'end-1c')
        if not text.strip():
            messagebox.showinfo('还没有内容', '先在左边粘贴一些 Markdown。', parent=self.win)
            return
        self.busy = True
        self.gen_button.set_enabled(False)
        self.status.set('正在生成…')
        threading.Thread(target=self._run, args=(text, self.kind, self.suffix),
                         daemon=True).start()

    def _run(self, text, kind, suffix):
        try:
            tmpdir = T.temp_dir('convert-')
            src = tmpdir / 'input.md'
            src.write_text(text, encoding='utf-8', newline='')
            dst = tmpdir / ('output' + suffix)
            if kind == 'word':
                T.md_to_docx(src, dst)
            elif kind == 'pdf':
                T.md_to_pdf(src, dst)
            elif kind == 'html':
                T.write(dst, T.md_to_html_text(text), bom=True)
            elif kind == 'text':
                T.write(dst, T.plain(text), bom=True, crlf=True)
            else:
                result, warnings = T.notion_md(src, dst)
                dst = Path(result)
                if warnings:
                    pass
            size = dst.stat().st_size
            self.win.after(0, lambda: self._done(dst, size))
        except Exception as exc:
            self.win.after(0, lambda e=exc: self._failed(e))

    def _done(self, path, size):
        self.temp = path
        label = dict((k, l) for k, l, _ in FORMATS).get(self.kind, self.kind)
        self.busy = False
        self.gen_button.set_enabled(True)
        self.save_button.set_enabled(True)
        self.save_button.set_primary(True)
        self.status.set('已生成 %s · %s（点「保存文件」选位置）' % (label, U.human(size)))
        self.preview(self.src.get('1.0', 'end-1c'))

    def _failed(self, exc):
        self.busy = False
        self.gen_button.set_enabled(True)
        self.temp = None
        self.save_button.set_enabled(False)
        self.save_button.set_primary(False)
        if self.kind == 'pdf':
            fallback = self._html_fallback()
            if fallback:
                self.status.set('PDF 没生成出来，已改存 HTML：%s' % fallback.name)
                if messagebox.askyesno(
                        'PDF 没生成出来',
                        '这次没能生成 PDF，已改存一份 HTML：\n%s\n\n'
                        '用浏览器打开它，Ctrl+P 另存为 PDF，效果一样。\n\n'
                        '要现在打开吗？' % fallback, parent=self.win):
                    self._open(fallback)
                    return
                return
        self.status.set('生成失败')
        messagebox.showerror('生成失败', '%s' % exc, parent=self.win)

    def _html_fallback(self):
        try:
            text = self.src.get('1.0', 'end-1c')
            tmp = T.temp_dir('convert-') / 'output.html'
            T.write(tmp, T.md_to_html_text(text), bom=True)
            return tmp
        except Exception:
            return None

    def _open(self, path):
        try:
            os.startfile(str(path))
        except Exception:
            pass

    # ---- 保存 ----
    def save(self):
        if not self.temp or not Path(self.temp).exists():
            return
        path = filedialog.asksaveasfilename(
            parent=self.win, title='保存为 %s' % self.label,
            defaultextension=self.suffix,
            initialfile='转换结果' + self.suffix,
            filetypes=[(self.label, '*' + self.suffix)])
        if not path:
            return
        target = enforce_suffix(path, self.suffix)
        renamed = str(target) != str(path)
        try:
            shutil.copyfile(self.temp, target)
        except Exception as exc:
            messagebox.showerror('保存失败', '%s' % exc, parent=self.win)
            return
        note = '（扩展名已按 %s 修正）' % self.suffix if renamed else ''
        self.status.set('已保存到 %s%s' % (target.name, note))


def enforce_suffix(path, suffix):
    """用户可能在保存框里手打别的扩展名，这里强制改成当前格式的扩展名。"""
    target = Path(path)
    if target.suffix.lower() != suffix.lower():
        target = target.with_suffix(suffix)
    return target


def open_converter(parent, suite):
    existing = getattr(suite, '_converter', None)
    if existing is not None:
        try:
            if existing.win.winfo_exists():
                existing.win.deiconify()
                existing.win.lift()
                existing.win.focus_force()
                return existing
        except Exception:  # noqa: BLE001
            pass
    window = Converter(parent, suite)
    try:
        suite._converter = window
    except Exception:  # noqa: BLE001
        pass
    return window
