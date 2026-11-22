# -*- coding: utf-8 -*-
"""库整理界面：快照 / 对比 / 回滚、批量改名、孤立附件归档。

全部走 ``vault_tools``，界面只负责选参数、预览、确认。
"""
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import ui_kit as U
import vault_tools as V


def _dialog(parent, title, width, height):
    win = tk.Toplevel(parent)
    win.title('%s · %s' % (title, '咕月15'))
    win.configure(bg=U.CANVAS)
    win.transient(parent.winfo_toplevel())
    width, height = U.s(width), U.s(height)
    root = parent.winfo_toplevel()
    x = root.winfo_rootx() + max(0, (root.winfo_width() - width) // 2)
    y = root.winfo_rooty() + max(U.s(24), (root.winfo_height() - height) // 4)
    win.geometry('%dx%d+%d+%d' % (width, height, x, y))
    win.attributes('-topmost', True)
    win.after(400, lambda: win.attributes('-topmost', False))
    return win


def _head(win, title, sub):
    box = tk.Frame(win, bg=U.CANVAS)
    box.pack(fill='x', padx=U.s(22), pady=(U.s(18), U.s(6)))
    tk.Label(box, text=title, bg=U.CANVAS, fg=U.INK,
             font=U.display(15), anchor='w').pack(fill='x')
    if sub:
        tk.Label(box, text=sub, bg=U.CANVAS, fg=U.MUTED,
                 font=U.body(9), anchor='w', justify='left',
                 wraplength=U.s(760)).pack(fill='x', pady=(U.s(4), 0))
    return box


def _table(parent, columns, widths, height=9):
    frame = tk.Frame(parent, bg=U.SURFACE, highlightbackground=U.LINE,
                     highlightthickness=1)
    style = ttk.Style(parent)
    style.configure('Org.Treeview', font=U.body(9), rowheight=U.s(28),
                    background=U.SURFACE, fieldbackground=U.SURFACE, borderwidth=0)
    style.configure('Org.Treeview.Heading', font=U.label(8), background=U.SUNKEN,
                    foreground=U.MUTED, relief='flat', padding=(U.s(10), U.s(7)))
    style.map('Org.Treeview',
              background=[('selected', U.ACCENT_SOFT)],
              foreground=[('selected', U.INK)])
    tree = ttk.Treeview(frame, columns=columns, show='headings', height=height,
                        style='Org.Treeview')
    tree.tag_configure('odd', background=U.SURFACE)
    tree.tag_configure('even', background='#FAFBFB')
    for name, width in zip(columns, widths):
        tree.heading(name, text=name)
        tree.column(name, width=U.s(width), anchor='w')
    bar = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
    tree.configure(yscrollcommand=bar.set)
    tree.pack(side='left', fill='both', expand=True)
    bar.pack(side='right', fill='y')
    return frame, tree


def _foot(win):
    foot = tk.Frame(win, bg=U.CANVAS)
    foot.pack(side='bottom', fill='x', padx=U.s(22), pady=U.s(14))
    return foot


def _say(suite, text):
    if suite is not None:
        suite.write(text)


# ---------- 快照 ----------

def open_snapshots(parent, suite, vault):
    win = _dialog(parent, '库快照', 880, 620)
    _head(win, '库快照与回滚',
          '快照包含普通笔记和附件，不包含隐藏目录、Obsidian 程序及同步工具数据。'
          '快照与知识库在同一磁盘，请另备份到其他位置。')

    frame, tree = _table(win, ('时间', '文件数', '大小', '标签'), (200, 90, 110, 260), height=8)
    frame.pack(fill='both', expand=True, padx=U.s(22), pady=(U.s(8), 0))

    state = {'items': []}

    def refresh():
        tree.delete(*tree.get_children())
        state['items'] = V.list_snapshots(vault)
        for item in state['items']:
            tree.insert('', 'end', iid=item['name'], values=(
                item.get('created') or item['mtime'], item.get('count', 0),
                V._human(item['size']), item.get('label', '')))
        if not state['items']:
            _say(suite, '还没有快照。点「现在打一份」即可。')
        else:
            _say(suite, '共 %d 份快照，最旧 %s' % (
                len(state['items']), state['items'][-1].get('created', '')))

    def selected():
        pick = tree.selection()
        if not pick:
            messagebox.showinfo('先选一份', '先在列表里点一行，再操作。', parent=win)
            return None
        name = pick[0]
        for item in state['items']:
            if item['name'] == name:
                return item
        return None

    def take():
        info = V.snapshot(vault, log=lambda line: _say(suite, line))
        _say(suite, '已存快照：%s（%d 个文件，%s）' % (info['name'], info['count'], V._human(info['size'])))
        refresh()

    def compare():
        item = selected()
        if not item:
            return
        result = V.diff(vault, item['path'])
        _say(suite, '和 %s 相比：新增 %d / 修改 %d / 删除 %d / 未变 %d' % (
            item['name'], len(result['added']), len(result['modified']),
            len(result['removed']), result['same']))
        for tag, key in (('+', 'added'), ('*', 'modified'), ('-', 'removed')):
            for rel in result[key][:8]:
                _say(suite, '  %s %s' % (tag, rel))
        if not any(result[key] for key in ('added', 'modified', 'removed')):
            messagebox.showinfo('没有差别', '这份快照和现在的库一样。', parent=win)

    def restore():
        item = selected()
        if not item:
            return
        if not messagebox.askyesno(
                '确认回滚',
                '把库恢复到 %s 的状态？\n\n只覆盖快照里有的文件，不会删掉你现在多出来的文件；'
                '回滚前会先存一份当前快照。' % item['name'], parent=win):
            return
        result = V.restore(vault, item['path'], log=lambda line: _say(suite, line))
        _say(suite, '已写回 %d 个文件；回滚前快照：%s' % (
            len(result['written']), result['before']['name']))
        messagebox.showinfo('完成', '已写回 %d 个文件。' % len(result['written']), parent=win)
        refresh()

    def open_folder():
        folder = Path(vault) / V.SNAP_DIR
        folder.mkdir(parents=True, exist_ok=True)
        import os
        os.startfile(str(folder))

    foot = _foot(win)
    U.RButton(foot, '现在打一份', command=take, variant='primary', height=34, padx=18).pack(side='left')
    U.RButton(foot, '和现在对比', command=compare, variant='ghost', height=34, padx=14).pack(side='left', padx=(8, 0))
    U.RButton(foot, '回到这份', command=restore, variant='ghost', height=34, padx=14).pack(side='left', padx=(8, 0))
    U.RButton(foot, '打开快照目录', command=open_folder, variant='quiet', height=34, padx=14).pack(side='left', padx=(8, 0))
    U.RButton(foot, '关闭', command=win.destroy, variant='quiet', height=34, padx=14).pack(side='right')
    win.bind('<Escape>', lambda event: win.destroy())
    refresh()
    return win


# ---------- 批量改名 ----------

def open_rename(parent, suite, vault):
    win = _dialog(parent, '批量改名', 960, 700)
    _head(win, '批量改名',
          '先选择知识库里的文件或文件夹，再勾选规则并检查预览。只改文件名，不更新双链。')

    scope_row = tk.Frame(win, bg=U.CANVAS)
    scope_row.pack(fill='x', padx=U.s(22), pady=(U.s(4), 0))
    scope_text = tk.StringVar(value='范围：请先选择文件或文件夹')
    tk.Label(scope_row, textvariable=scope_text, bg=U.CANVAS, fg=U.INK,
             font=U.body(9), anchor='w').pack(side='left', fill='x', expand=True)

    opts = tk.Frame(win, bg=U.CANVAS)
    opts.pack(fill='x', padx=U.s(22), pady=(U.s(4), 0))
    fix_illegal = tk.BooleanVar(value=True)
    collapse = tk.BooleanVar(value=True)
    untitled = tk.BooleanVar(value=True)
    date_prefix = tk.BooleanVar(value=False)
    for text, var in (('去掉非法字符（<>:"/\\|?*）', fix_illegal),
                      ('压掉多余空格', collapse),
                      ('「未命名」改成正文标题', untitled),
                      ('加日期前缀（date 或文件名里的日期）', date_prefix)):
        U.Check(opts, text, var=var, bg=U.CANVAS, font=U.body(9)).pack(side='left', padx=(0, U.s(14)))

    rules = tk.Frame(win, bg=U.CANVAS)
    rules.pack(fill='x', padx=U.s(22), pady=(U.s(8), 0))
    find_var, replacement_var, prefix_var, suffix_var = (tk.StringVar() for _ in range(4))
    for label, var in (('查找', find_var), ('替换成', replacement_var),
                       ('前缀', prefix_var), ('后缀', suffix_var)):
        tk.Label(rules, text=label, bg=U.CANVAS, fg=U.MUTED, font=U.body(9)).pack(side='left')
        ttk.Entry(rules, textvariable=var, width=13).pack(side='left', padx=(U.s(5), U.s(12)))
    rule_status = tk.StringVar(value='预览默认选中全部项目；按住 Ctrl 可取消或重新选择。')
    tk.Label(win, textvariable=rule_status, bg=U.CANVAS, fg=U.MUTED,
             font=U.body(9), anchor='w').pack(fill='x', padx=U.s(22), pady=(U.s(7), 0))

    frame, tree = _table(win, ('原路径', '改成', '原因'), (330, 330, 180), height=10)
    frame.pack(fill='both', expand=True, padx=U.s(22), pady=(U.s(8), 0))
    state = {'plan': [], 'files': set(), 'folder': None}

    def preview():
        tree.delete(*tree.get_children())
        try:
            state['plan'] = V.rename_plan(vault, fix_illegal=fix_illegal.get(),
                                          collapse_space=collapse.get(),
                                          untitled_to_heading=untitled.get(),
                                          date_prefix=date_prefix.get(),
                                          replace_from=find_var.get(), replace_to=replacement_var.get(),
                                          prefix=prefix_var.get(), suffix=suffix_var.get(),
                                          files=state['files'], folder=state['folder'])
        except (OSError, ValueError) as exc:
            state['plan'] = []
            rule_status.set(str(exc))
            return
        rule_status.set('预览默认选中全部项目；按住 Ctrl 可取消或重新选择。')
        for index, (rel, new_rel, reason) in enumerate(state['plan']):
            if Path(new_rel).exists():
                reason += '；目标已存在，将跳过'
            tree.insert('', 'end', iid=str(index), values=(rel, Path(new_rel).name, reason))
        tree.selection_set(tree.get_children())
        _say(suite, '待改名 %d 个；勾选项改了会重新预览。' % len(state['plan']))

    def choose_files():
        chosen = filedialog.askopenfilenames(parent=win, initialdir=vault,
                                             title='选择知识库内的笔记',
                                             filetypes=[('Markdown 笔记', '*.md')])
        if not chosen:
            return
        root = Path(vault).resolve()
        paths = [Path(p).resolve() for p in chosen]
        if any(not p.is_relative_to(root) for p in paths):
            messagebox.showinfo('请选择知识库内的文件', '暂时只支持整理当前知识库内的笔记。', parent=win)
            return
        state['files'] = {p.relative_to(root).as_posix() for p in paths}
        state['folder'] = None
        scope_text.set('范围：已选 %d 个文件' % len(paths))
        preview()

    def choose_folder():
        chosen = filedialog.askdirectory(parent=win, initialdir=vault, title='选择知识库内的文件夹')
        if not chosen:
            return
        root = Path(vault).resolve()
        path = Path(chosen).resolve()
        if not path.is_relative_to(root):
            messagebox.showinfo('请选择知识库内的文件夹', '暂时只支持整理当前知识库内的笔记。', parent=win)
            return
        state['files'] = None
        state['folder'] = path.relative_to(root).as_posix() if path != root else None
        scope_text.set('范围：' + (state['folder'] or '整个知识库'))
        preview()

    def select_all_vault():
        state['files'] = state['folder'] = None
        scope_text.set('范围：整个知识库')
        preview()

    U.RButton(scope_row, '选择文件', command=choose_files, variant='ghost', height=30, padx=10).pack(side='left')
    U.RButton(scope_row, '选择文件夹', command=choose_folder, variant='ghost', height=30, padx=10).pack(side='left', padx=(6, 0))
    U.RButton(scope_row, '整个知识库', command=select_all_vault, variant='quiet', height=30, padx=10).pack(side='left', padx=(6, 0))

    def apply():
        selected = [state['plan'][int(item)] for item in tree.selection()]
        if not selected:
            messagebox.showinfo('没有选中项目', '请先选择文件范围，再在预览列表中选中要改名的项目。', parent=win)
            return
        if not messagebox.askyesno('确认改名',
                                   '执行选中的 %d 个改名？\n执行前会自动存一份快照。' % len(selected),
                                   parent=win):
            return
        backup = V.snapshot(vault, label='before-rename', log=lambda line: _say(suite, line))
        if backup.get('skipped'):
            messagebox.showerror('备份不完整', '有文件无法读取，已停止改名。', parent=win)
            return
        result = V.rename_apply(vault, selected)
        _say(suite, '改名 %d 个，跳过 %d 个' % (len(result['done']), len(result['skipped'])))
        for rel, why in result['skipped'][:10]:
            _say(suite, '  跳过 %s：%s' % (rel, why))
        messagebox.showinfo('完成', '改名 %d 个，跳过 %d 个。' % (len(result['done']), len(result['skipped'])),
                            parent=win)
        preview()

    def undo():
        batches = V.rename_batches(vault)
        if not batches:
            messagebox.showinfo('没有记录', '这里只撤销用本程序改过的名字。', parent=win)
            return
        last = batches[0]
        if not messagebox.askyesno(
                '撤销上一次改名',
                '撤销 %s 那一次（%d 个文件）？\n文件会改回原来的名字。'
                % (last.get('at', ''), len(last.get('items', []))), parent=win):
            return
        result = V.undo_rename(vault, log=lambda line: _say(suite, line))
        _say(suite, '恢复 %d 个，跳过 %d 个' % (len(result['restored']), len(result['skipped'])))
        for rel, why in result['skipped'][:10]:
            _say(suite, '  跳过 %s：%s' % (rel, why))
        preview()

    foot = _foot(win)
    U.RButton(foot, '预览', command=preview, variant='primary', height=34, padx=18).pack(side='left')
    U.RButton(foot, '执行选中', command=apply, variant='ghost', height=34, padx=14).pack(side='left', padx=(8, 0))
    U.RButton(foot, '全选', command=lambda: tree.selection_set(tree.get_children()), variant='quiet', height=34, padx=12).pack(side='left', padx=(8, 0))
    U.RButton(foot, '撤销上次', command=undo, variant='quiet', height=34, padx=14).pack(side='left', padx=(8, 0))
    U.RButton(foot, '关闭', command=win.destroy, variant='quiet', height=34, padx=14).pack(side='right')
    for var in (fix_illegal, collapse, untitled, date_prefix, find_var, replacement_var, prefix_var, suffix_var):
        var.trace_add('write', lambda *_: preview())
    win.bind('<Escape>', lambda event: win.destroy())
    preview()
    return win


# ---------- 孤立附件 ----------

def open_orphans(parent, suite, vault):
    win = _dialog(parent, '孤立附件', 860, 600)
    _head(win, '孤立附件',
          '库里存在、但没有任何笔记引用的附件。移动只是挪到 .同步工具\\孤立附件，'
          '原文件不删，随时可以搬回去。')

    frame, tree = _table(win, ('附件', '大小'), (620, 120), height=11)
    frame.pack(fill='both', expand=True, padx=U.s(22), pady=(U.s(8), 0))
    state = {'plan': []}

    def scan():
        tree.delete(*tree.get_children())
        state['plan'] = V.orphan_plan(vault)
        for rel, size in state['plan']:
            tree.insert('', 'end', values=(rel, V._human(size)))
        total = sum(size for _rel, size in state['plan'])
        _say(suite, '孤立附件 %d 个，共 %s' % (len(state['plan']), V._human(total)))

    def move():
        if not state['plan']:
            messagebox.showinfo('没有可移的', '列表是空的，先点「重新扫描」。', parent=win)
            return
        if not messagebox.askyesno('确认移动',
                                   '把 %d 个附件移到 .同步工具\\孤立附件？（不删除）' % len(state['plan']),
                                   parent=win):
            return
        backup = V.snapshot(vault, label='before-orphans', log=lambda line: _say(suite, line))
        if backup.get('skipped'):
            messagebox.showerror('备份不完整', '有文件无法读取，已停止移动。', parent=win)
            return
        moved = V.orphan_apply(vault, state['plan'], log=lambda line: _say(suite, line))
        _say(suite, '已移走 %d 个' % len(moved))
        messagebox.showinfo('完成', '已移走 %d 个到 %s。' % (len(moved), V.ORPHAN_DIR), parent=win)
        scan()

    foot = _foot(win)
    U.RButton(foot, '重新扫描', command=scan, variant='primary', height=34, padx=18).pack(side='left')
    U.RButton(foot, '移到归档', command=move, variant='ghost', height=34, padx=14).pack(side='left', padx=(8, 0))
    U.RButton(foot, '关闭', command=win.destroy, variant='quiet', height=34, padx=14).pack(side='right')
    win.bind('<Escape>', lambda event: win.destroy())
    scan()
    return win
