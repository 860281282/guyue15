"""修复 Notion 导入留下的原始标记（排版修复小工具）。

导入工具把 Notion 页面转成 Markdown 时，有些块没被识别，会原样留下
`<ul><li>`、`<br />`、`<mention url=…>`、`<table-of-contents />`、`<callout>`
之类的标签。这个模块把它们就地改成 Obsidian 认得的写法，只动这几类标记，
其余内容（正文、frontmatter、双链、代码块）一个字都不改。

用法：
    python md_fix.py --scan --vault "C:\\Obsidian"          只看统计
    python md_fix.py --fix  --vault "C:\\Obsidian" --dry-run 预览会改什么
    python md_fix.py --fix  --vault "C:\\Obsidian"          真改（自动备份）
    python md_fix.py                                       打开界面
"""
import argparse
import html
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

VERSION = '1.0.0'

SKIP_DIRS = {'.同步工具', '.obsidian', '.git', '.trash', 'node_modules'}

FENCE = re.compile(r'(?ms)^(`{3,}|~{3,})[^\n]*\n.*?^\1[ \t]*$')
INLINE = re.compile('(`+[^`\n]+`)')
UL = re.compile(r'(?is)<ul\b[^>]*>([\s\S]*?)</ul>')
OL = re.compile(r'(?is)<ol\b[^>]*>([\s\S]*?)</ol>')
LI = re.compile(r'(?is)<li\b[^>]*>([\s\S]*?)</li>')
TODO = re.compile(r'(?is)<to-do\b([^>]*)>([\s\S]*?)</to-do>')
BR = re.compile(r'(?i)<br\s*/?>')
MENTION = re.compile(r'(?is)<mention\b([^>]*?)(?:/>|>([\s\S]*?)</mention>)')
TOC = re.compile(r'(?i)<table-of-contents\b[^>]*?/?>')
TABLE = re.compile(r'(?is)<table\b[^>]*>([\s\S]*?)</table>')
TR = re.compile(r'(?is)<tr\b[^>]*>([\s\S]*?)</tr>')
TD = re.compile(r'(?is)<t([dh])\b[^>]*>([\s\S]*?)</t\1>')
CALLOUT = re.compile(r'(?is)<callout\b([^>]*)>([\s\S]*?)</callout>')
SPAN = re.compile(r'(?is)<span\b([^>]*)>([\s\S]*?)</span>')
ATTR = re.compile(r'([\w-]+)="([^"]*)"')
NOTION_ID = re.compile(r'"notion_id"\s*:\s*"([0-9a-f]{32})"')
URL_ID = re.compile(r'([0-9a-f]{32})')

CALLOUT_KIND = {
    '💡': 'tip', '🔎': 'tip', '😀': 'tip',
    '⚠': 'warning', '🚧': 'warning', '⚠️': 'warning',
    '✅': 'success', '☑': 'success', '✔': 'success',
    '❌': 'danger', '🛑': 'danger', '🚫': 'danger',
    '📌': 'note', '📝': 'note', '📄': 'note', '🔥': 'danger',
    '❓': 'question', '💬': 'quote', '📖': 'quote',
}


def attrs(text):
    return {k: html.unescape(v) for k, v in ATTR.findall(text or '')}


def clean(text):
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip('\n')


def indent_block(text, width=2):
    pad = ' ' * width
    return '\n'.join(pad + line if line.strip() else line for line in text.split('\n'))


def convert_todo_body(body):
    body = clean(body)
    return body.replace('\n', ' ').strip()


def callout_kind(info):
    icon = info.get('icon', '')
    color = info.get('color', '')
    for key, kind in CALLOUT_KIND.items():
        if icon.startswith(key):
            return kind
    if 'red' in color:
        return 'danger'
    if 'yellow' in color or 'orange' in color:
        return 'warning'
    if 'green' in color:
        return 'success'
    return 'note'


def html_to_md_table(block):
    rows = []
    for row in TR.findall(block):
        cells = [clean(re.sub(r'(?is)<[^>]+>', '', cell)) for _, cell in TD.findall(row)]
        if cells:
            rows.append(cells)
    if not rows:
        return None
    width = max(len(row) for row in rows)
    rows = [row + [''] * (width - len(row)) for row in rows]
    head, body = rows[0], rows[1:]
    lines = ['| ' + ' | '.join(head) + ' |', '| ' + ' | '.join(['---'] * width) + ' |']
    for row in body:
        lines.append('| ' + ' | '.join(cell.replace('|', '\\|') for cell in row) + ' |')
    return '\n'.join(lines)


def convert_block(text, index, stats):
    def lists(match, marker):
        inner = match[1]
        items = LI.findall(inner)
        if not items:
            return match[0]
        out = []
        for item in items:
            body = clean(convert_block(item, index, stats))
            if not body:
                continue
            lines = body.split('\n')
            out.append(marker + ' ' + lines[0].strip())
            for line in lines[1:]:
                out.append('  ' + line)
        stats['list'] += len(out)
        return '\n'.join(out)

    text = UL.sub(lambda m: lists(m, '-'), text)
    text = OL.sub(lambda m: lists(m, '1.'), text)

    def todo(match):
        info = attrs(match[1])
        checked = str(info.get('checked', '')).lower() in ('true', 'yes', '1')
        body = convert_todo_body(convert_block(match[2], index, stats))
        if not body:
            return ''
        stats['todo'] += 1
        return ('- [x] ' if checked else '- [ ] ') + body

    text = TODO.sub(todo, text)

    def callout(match):
        info = attrs(match[1])
        kind = callout_kind(info)
        body = clean(convert_block(match[2], index, stats))
        if not body:
            return ''
        first = body.split('\n')[0].strip()
        rest = body.split('\n')[1:]
        title = first if len(first) <= 60 else ''
        lines = ['> [!%s]%s' % (kind, (' ' + title) if title else '')]
        body_lines = rest if title else body.split('\n')
        for line in body_lines:
            lines.append('> ' + line if line.strip() else '>')
        stats['callout'] += 1
        return '\n'.join(lines)

    text = CALLOUT.sub(callout, text)

    def table(match):
        md = html_to_md_table(match[1])
        if md is None:
            return match[0]
        stats['table'] += 1
        return md

    text = TABLE.sub(table, text)

    def mention(match):
        info = attrs(match[1])
        url = info.get('url', '')
        label = html.unescape(clean(match[2] or '')).strip() or info.get('title', '')
        pid = URL_ID.search(url)
        key = pid[1] if pid else ''
        target = index.get(key)
        stats['mention'] += 1
        if target:
            name = Path(target).stem
            return '[[' + target + '|' + (label or name) + ']]' if label and label != name else '[[' + target + ']]'
        if label:
            return '[' + label + '](' + url + ')' if url else label
        return url

    text = MENTION.sub(mention, text)

    def span(match):
        info = attrs(match[1])
        body = match[2]
        color = (info.get('color') or '').lower()
        if not color or color in ('default', 'gray'):
            return body
        stats['color'] += 1
        return '==' + body.strip() + '=='

    text = SPAN.sub(span, text)
    found = len(TOC.findall(text))
    if found:
        stats['toc'] += found
        text = TOC.sub('', text)
    text = BR.sub('\n', text)
    return text


def escape_table_links(text):
    """表格行里的 [[链接|别名]] 必须写成 [[链接\\|别名]]，否则会把单元格切开。"""
    out = []
    for line in text.split('\n'):
        if line.lstrip().startswith('|'):
            line = re.sub(r'\[\[([^\]]+)\]\]',
                          lambda m: '[[' + re.sub(r'(?<!\\)\|', r'\\|', m.group(1)) + ']]',
                          line)
        out.append(line)
    return '\n'.join(out)


def fix_text(text, index, stats):
    """只处理代码块之外的片段，代码块原样保留。"""
    parts, last = [], 0
    for match in FENCE.finditer(text):
        parts.append(('text', text[last:match.start()]))
        parts.append(('code', match.group(0)))
        last = match.end()
    parts.append(('text', text[last:]))
    out = []
    for kind, chunk in parts:
        if kind == 'code':
            out.append(chunk)
            continue
        before = chunk
        # 行内代码先换成占位符，免得把 `代码里的 <li>` 也改掉
        guarded = []
        for span in INLINE.finditer(chunk):
            guarded.append(span.group(0))
        chunk = INLINE.sub(lambda m: _stash(m, guarded), chunk)
        chunk = convert_block(chunk, index, stats)
        chunk = _unstash(chunk, guarded)
        stats['br'] += len(BR.findall(before))
        out.append(chunk)
    return escape_table_links(''.join(out))


def _stash(match, kept):
    """把行内代码换成占位符，返回占位符本身。"""
    token = '\x01IN%d\x01' % len(kept)
    kept.append(match.group(0))
    return token


def _unstash(text, kept):
    for position, span in enumerate(kept):
        text = text.replace('\x01IN%d\x01' % position, span)
    return text


def load_index(vault):
    index = {}
    for path in Path(vault).rglob('*.md'):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            head = path.read_text(encoding='utf-8', errors='ignore')[:2000]
        except OSError:
            continue
        match = NOTION_ID.search(head)
        if match:
            rel = path.relative_to(vault).as_posix()[:-3]
            index.setdefault(match[1], rel)
    return index


def scan(vault, sample=6):
    vault = Path(vault)
    index = load_index(vault)
    files, totals, samples = [], {}, []
    for path in vault.rglob('*.md'):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        raw = path.read_text(encoding='utf-8', errors='ignore')
        stats = {'list': 0, 'todo': 0, 'callout': 0, 'table': 0, 'mention': 0, 'color': 0, 'toc': 0, 'br': 0}
        fixed = fix_text(raw, index, stats)
        hits = sum(stats.values())
        if hits and fixed != raw:
            rel = path.relative_to(vault).as_posix()
            files.append({'path': rel, 'hits': stats, 'bytes_before': len(raw), 'bytes_after': len(fixed)})
            for key, value in stats.items():
                totals[key] = totals.get(key, 0) + value
            if len(samples) < sample:
                samples.append({'path': rel, 'before': first_diff_line(raw, fixed), 'after': first_diff_line(fixed, raw)})
    return {'vault': str(vault), 'files': len(files), 'totals': totals, 'indexed': len(index),
            'items': files, 'samples': samples}


def first_diff_line(a, b):
    left, right = a.split('\n'), b.split('\n')
    for index in range(min(len(left), len(right))):
        if left[index] != right[index]:
            return (left[index].strip() or '（空行）')[:120]
    return ''


def fix_vault(vault, dry_run=False, log=print):
    vault = Path(vault)
    index = load_index(vault)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = vault / '.同步工具' / '排版备份' / stamp
    changed = 0
    totals = {}
    for path in sorted(vault.rglob('*.md')):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        raw = path.read_text(encoding='utf-8', errors='ignore')
        stats = {'list': 0, 'todo': 0, 'callout': 0, 'table': 0, 'mention': 0, 'color': 0, 'toc': 0, 'br': 0}
        fixed = fix_text(raw, index, stats)
        if fixed == raw:
            continue
        for key, value in stats.items():
            totals[key] = totals.get(key, 0) + value
        rel = path.relative_to(vault)
        if dry_run:
            log('会改：%s（%s）' % (rel.as_posix(), describe(stats)))
        else:
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            path.write_text(fixed, encoding='utf-8', newline='')
            log('已修复：%s（%s）' % (rel.as_posix(), describe(stats)))
        changed += 1
    return {'changed': changed, 'totals': totals, 'backup': str(backup), 'dry_run': dry_run}


def describe(stats):
    names = {'list': '列表', 'todo': '待办', 'callout': '提示块', 'table': '表格',
             'mention': '提及', 'color': '颜色', 'toc': '目录块', 'br': '换行'}
    parts = ['%s %d' % (names[key], value) for key, value in stats.items() if value]
    return '、'.join(parts) or '仅格式'


def main():
    parser = argparse.ArgumentParser(description='修复 Notion 导入留下的原始标记')
    parser.add_argument('--vault', default='')
    parser.add_argument('--scan', action='store_true')
    parser.add_argument('--fix', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    if not args.vault:
        return gui()
    if args.scan or not args.fix:
        result = scan(args.vault)
        print(json.dumps({k: result[k] for k in ('vault', 'files', 'totals', 'indexed')}, ensure_ascii=False, indent=2))
        for item in result['items'][:10]:
            print(' -', item['path'], describe(item['hits']))
        return 0
    result = fix_vault(args.vault, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def gui():
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    BG, CARD, ACCENT, MUTED = '#f8f7f3', '#ffffff', '#245d51', '#6f6b60'
    root = tk.Tk()
    root.title('Notion 排版修复 · %s' % VERSION)
    root.geometry('880x680')
    root.minsize(760, 600)
    root.configure(background=BG)
    style = ttk.Style()
    try:
        style.theme_use('clam')
    except tk.TclError:
        pass
    style.configure('TLabel', background=BG, foreground='#26241f', font=('Microsoft YaHei UI', 10))
    style.configure('Accent.TButton', font=('Microsoft YaHei UI', 10, 'bold'), padding=(18, 9))
    style.configure('TButton', font=('Microsoft YaHei UI', 10), padding=(12, 7))

    tk.Label(root, text='修好导入留下的原始标记', background=BG, foreground='#26241f',
             font=('Microsoft YaHei UI', 18, 'bold')).pack(anchor='w', padx=26, pady=(20, 2))
    tk.Label(root, text='把 <ul>、<br />、<mention>、<callout> 这类标签改成 Obsidian 认得的写法；只动这些标记，正文与双链不动。',
             background=BG, foreground=MUTED, font=('Microsoft YaHei UI', 9)).pack(anchor='w', padx=26)

    card = tk.Frame(root, background=CARD, highlightbackground='#e4e1d8', highlightthickness=1)
    card.pack(fill='x', padx=26, pady=(14, 8))
    row = tk.Frame(card, background=CARD)
    row.pack(fill='x', padx=16, pady=12)
    tk.Label(row, text='Obsidian 知识库', background=CARD, foreground=MUTED,
             font=('Microsoft YaHei UI', 9)).pack(anchor='w')
    line = tk.Frame(row, background=CARD)
    line.pack(fill='x', pady=(6, 0))
    vault_var = tk.StringVar(value=r'C:\Obsidian' if Path(r'C:\Obsidian').is_dir() else '')
    entry = tk.Entry(line, textvariable=vault_var, font=('Microsoft YaHei UI', 10), relief='solid', bd=1)
    entry.pack(side='left', fill='x', expand=True, ipady=5)

    def choose():
        picked = filedialog.askdirectory(title='选择 Obsidian 知识库')
        if picked:
            vault_var.set(picked)

    ttk.Button(line, text='选择文件夹', command=choose).pack(side='left', padx=(10, 0))

    buttons = tk.Frame(root, background=BG)
    buttons.pack(fill='x', padx=26, pady=(4, 10))
    tools = tk.Frame(root, background=BG)
    tools.pack(side='bottom', fill='x', padx=26, pady=(0, 18))
    log_frame = tk.Frame(root, background=CARD, highlightbackground='#e4e1d8', highlightthickness=1)
    log_frame.pack(fill='both', expand=True, padx=26, pady=(0, 10))
    output = tk.Text(log_frame, height=14, font=('Consolas', 10), relief='flat', bd=0,
                     background=CARD, foreground='#26241f', wrap='word', padx=8, pady=8)
    scroll = ttk.Scrollbar(log_frame, orient='vertical', command=output.yview)
    output.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y')
    output.pack(fill='both', expand=True)
    output.tag_configure('warn', foreground='#8a6a12')
    output.tag_configure('ok', foreground='#1f6b4a')

    def write(text, tag=None):
        output.insert('end', text + '\n', tag or ())
        output.see('end')

    state = {'busy': False}

    def guard():
        if state['busy']:
            messagebox.showinfo('稍等', '正在处理，请稍候。')
            return False
        vault = Path(vault_var.get().strip())
        if not vault.is_dir():
            messagebox.showwarning('先选目录', '请先选择 Obsidian 知识库文件夹。')
            return False
        return True

    def do_scan():
        if not guard():
            return
        state['busy'] = True
        output.delete('1.0', 'end')

        def work():
            try:
                result = scan(vault_var.get().strip())
                root.after(0, lambda: show(result))
            except Exception as exc:  # noqa: BLE001
                root.after(0, lambda: write('扫描失败：%s' % exc, 'warn'))
            finally:
                state['busy'] = False

        def show(result):
            write('扫描完成：%d 篇笔记需要修复（共识别 %d 篇带 Notion 编号）' % (result['files'], result['indexed']), 'ok')
            if result['totals']:
                write('  共：' + describe(result['totals']))
            for item in result['items'][:20]:
                write('  - %s（%s）' % (item['path'], describe(item['hits'])))
            if len(result['items']) > 20:
                write('  …还有 %d 篇' % (len(result['items']) - 20))
            if not result['files']:
                write('没有需要修复的内容。', 'ok')

        threading.Thread(target=work, daemon=True).start()

    def do_fix(dry):
        if not guard():
            return
        if not dry and not messagebox.askyesno('确认修复', '会先备份到 .同步工具\\排版备份\\，然后修改笔记。继续？'):
            return
        state['busy'] = True
        output.delete('1.0', 'end')

        def work():
            try:
                result = fix_vault(vault_var.get().strip(), dry_run=dry, log=lambda text: root.after(0, lambda t=text: write(t)))
                root.after(0, lambda: finish(result))
            except Exception as exc:  # noqa: BLE001
                root.after(0, lambda: write('处理失败：%s' % exc, 'warn'))
            finally:
                state['busy'] = False

        def finish(result):
            if result['dry_run']:
                write('预览完成：会改 %d 篇，未写入任何文件。' % result['changed'], 'ok')
            else:
                write('完成：修复 %d 篇；原件备份在 %s' % (result['changed'], result['backup']), 'ok')

        threading.Thread(target=work, daemon=True).start()

    ttk.Button(buttons, text='扫描', command=do_scan).pack(side='left')
    ttk.Button(buttons, text='预览修改', command=lambda: do_fix(True)).pack(side='left', padx=(10, 0))
    ttk.Button(buttons, text='开始修复', style='Accent.TButton', command=lambda: do_fix(False)).pack(side='left', padx=(10, 0))
    tk.Label(tools, text='只改列表 / 换行 / 提及 / 提示块 / 表格 / 颜色 / 目录块这几类标记',
             background=BG, foreground=MUTED, font=('Microsoft YaHei UI', 9)).pack(side='left')
    write('选好知识库，先点「扫描」看有多少篇需要修复。')
    root.after(500, do_scan)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
