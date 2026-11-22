"""同步导入期间将常见 Notion 标记转成 Markdown。"""
import html
import json
import re
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


def describe(stats):
    names = {'list': '列表', 'todo': '待办', 'callout': '提示块', 'table': '表格',
             'mention': '提及', 'color': '颜色', 'toc': '目录块', 'br': '换行'}
    parts = ['%s %d' % (names[key], value) for key, value in stats.items() if value]
    return '、'.join(parts) or '仅格式'
