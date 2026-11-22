# -*- coding: utf-8 -*-
"""库体检：一次看清这个知识库的家底和毛病。"""
import json
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import toolbox as T
import ui_kit as U
import vault_tools as V

import journal

VERSION = journal.VERSION


def _folder_stats(vault):
    vault = Path(vault)
    rows = []
    for sub in ('知识库', '日常生活', '日程提醒'):
        base = vault / sub
        if base.is_dir():
            count = len(list(base.rglob('*.md')))
            words = 0
            for path in base.rglob('*.md'):
                try:
                    words += len(T.read(path))
                except OSError:
                    pass
            rows.append((sub, count, words))
    return rows


def run(vault):
    vault = Path(vault)
    notes = V.note_files(vault)
    total_bytes = sum(path.stat().st_size for _rel, path in notes if path.is_file())
    attachments = []
    roots = [vault / name for name in ('知识库', '日常生活', '日程提醒')]
    roots = [path for path in roots if path.is_dir()] or [vault]
    for base in roots:
        for path in base.rglob('*'):
            if not path.is_file():
                continue
            if any(part.startswith('.') for part in path.relative_to(vault).parts):
                continue
            if path.suffix.lower() in ('.md', '.png', '.jpg', '.jpeg', '.gif', '.webp',
                                       '.bmp', '.svg', '.pdf', '.mp3', '.mp4', '.m4a',
                                       '.wav', '.zip', '.docx', '.pptx', '.xlsx'):
                if path.suffix.lower() != '.md':
                    attachments.append(path)
    attach_bytes = sum(p.stat().st_size for p in attachments)
    broken = T.broken_links(str(vault))
    unused = T.unused_attachments(str(vault))
    dups = T.duplicate_titles(str(vault))
    conflicts = []
    try:
        import conflicts as C
        conflicts = C.scan(str(vault))
    except Exception:  # noqa: BLE001
        conflicts = []
    sync_info = {}
    recent = vault / '.同步工具' / '数据' / '最近同步.json'
    if recent.is_file():
        try:
            sync_info = json.loads(recent.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            sync_info = {}
        try:
            import datetime
            sync_info['finished_at'] = datetime.datetime.fromtimestamp(
                recent.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
        except OSError:
            pass
    return {
        'notes': len(notes), 'bytes': total_bytes,
        'attachments': len(attachments), 'attach_bytes': attach_bytes,
        'folders': _folder_stats(vault),
        'broken': broken if isinstance(broken, int) else len(broken or []),
        'unused': unused if isinstance(unused, int) else len(unused or []),
        'dups': dups if isinstance(dups, int) else len(dups or []),
        'conflicts': len(conflicts),
        'sync': sync_info,
    }


def summary_text(result):
    lines = ['笔记 %d 篇，正文约 %s' % (result['notes'], _human(result['bytes'])),
             '附件 %d 个，约 %s' % (result['attachments'], _human(result['attach_bytes']))]
    for name, count, words in result['folders']:
        lines.append('  %s：%d 篇，约 %d 字' % (name, count, words))
    lines.append('')
    lines.append('断链检查：%s' % (('发现 %d 处' % result['broken']) if result['broken'] else '没有断链'))
    lines.append('未引用附件：%s' % (('有 %d 个' % result['unused']) if result['unused'] else '没有'))
    lines.append('同名笔记：%s' % (('有 %d 组' % result['dups']) if result['dups'] else '没有'))
    lines.append('待处理冲突：%s' % (('有 %d 个' % result['conflicts']) if result['conflicts'] else '没有'))
    sync = result.get('sync') or {}
    if sync:
        counts = sync.get('counts') or {}
        lines.append('上次同步：%s（新增 %s / 更新 %s / 未变 %s）' % (
            sync.get('finished_at') or sync.get('started_at') or '未知',
            counts.get('added', 0), counts.get('updated', 0), counts.get('unchanged', 0)))
    return '\n'.join(lines)


def _human(size):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return '%.1f %s' % (size, unit) if unit != 'B' else '%d B' % size
        size /= 1024.0
    return '%d B' % size


def open_checkup(parent, suite, vault):
    win = tk.Toplevel(parent)
    win.title('库体检 · ' + VERSION)
    win.configure(bg=U.CANVAS)
    win.geometry('%dx%d' % (U.s(720), U.s(560)))
    root = parent.winfo_toplevel()
    win.geometry('+%d+%d' % (root.winfo_rootx() + U.s(120), root.winfo_rooty() + U.s(80)))
    win.transient(root)

    head = tk.Frame(win, bg=U.CANVAS)
    head.pack(fill='x', padx=U.s(24), pady=(U.s(18), 0))
    tk.Label(head, text='库体检', bg=U.CANVAS, fg=U.INK, font=U.display(15)).pack(anchor='w')
    tk.Label(head, text='一次看清家底和毛病；只读不写。', bg=U.CANVAS, fg=U.MUTED,
             font=U.body(9)).pack(anchor='w', pady=(U.s(4), 0))

    card = tk.Frame(win, bg=U.SURFACE, highlightbackground=U.LINE, highlightthickness=1)
    card.pack(fill='both', expand=True, padx=U.s(24), pady=(U.s(12), 0))
    text = tk.Text(card, wrap='word', relief='flat', bd=0, bg=U.SURFACE, fg=U.INK,
                   font=U.body(10), padx=U.s(16), pady=U.s(14))
    text.pack(side='left', fill='both', expand=True)
    bar = ttk.Scrollbar(card, orient='vertical', command=text.yview)
    bar.pack(side='right', fill='y')
    text.configure(yscrollcommand=bar.set)

    def refresh():
        text.configure(state='normal')
        text.delete('1.0', 'end')
        try:
            result = run(vault)
        except Exception as exc:  # noqa: BLE001
            text.insert('end', '检查失败：%s' % exc)
            text.configure(state='disabled')
            return
        text.insert('end', summary_text(result))
        text.configure(state='disabled')

    foot = tk.Frame(win, bg=U.CANVAS)
    foot.pack(fill='x', padx=U.s(24), pady=(U.s(12), U.s(16)))
    ttk.Button(foot, text='重新检查', command=refresh, style='Small.TButton').pack(side='left')
    ttk.Button(foot, text='关闭', command=win.destroy, style='Small.TButton').pack(side='right')
    refresh()
    return win
