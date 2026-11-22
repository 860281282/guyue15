"""工具箱：文本格式转换 + 笔记整理小工具（不联网，除了 md→pdf 借 Edge 打印）。

所有函数都是"输入路径 → 输出路径"的纯操作，既能被界面调用，也能命令行调用。
"""

from __future__ import annotations

import html
import io
import os
import re
import shutil
import subprocess
import time
import tempfile
from pathlib import Path, PurePosixPath

VERSION = '1.0.0'

def temp_dir(prefix='convert-'):
    """建一个确定能写的临时目录：优先程序的「缓存/临时」，退到系统临时目录与当前目录。"""
    candidates = []
    home = Path(os.environ.get('LOCALAPPDATA') or Path.home())
    candidates.append(home / 'NotionObsidianSync' / '临时')
    if os.environ.get('TEMP'):
        candidates.append(Path(os.environ['TEMP']))
    candidates.append(Path(__file__).resolve().parent / '缓存' / '临时')
    candidates.append(home / 'Temp')
    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix=prefix))


EDGE_CANDIDATES = (
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
)

FENCE = re.compile(r'^```(\w*)\s*$')
HEADING = re.compile(r'^(#{1,6})\s+(.*)$')
BULLET = re.compile(r'^\s*[-*+]\s+(.*)$')
NUMBERED = re.compile(r'^\s*(\d+)[.)]\s+(.*)$')
TODO = re.compile(r'^\s*[-*+]\s+\[([ xX])\]\s+(.*)$')
QUOTE = re.compile(r'^>\s?(.*)$')
IMAGE = re.compile(r'^!\[([^\]]*)\]\(([^)]+)\)\s*$')
TABLE_SEP = re.compile(r'^\s*\|?[\s:|-]+\|[\s:|-]*$')
WIKI = re.compile(r'\[\[([^\]|]+)(?:\|([^\]]+))?\]\]')
LINK = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')
INLINE_CODE = re.compile(r'`([^`]+)`')
BOLD = re.compile(r'\*\*([^*]+)\*\*')
ITALIC = re.compile(r'(?<!\*)\*([^*]+)\*(?!\*)')
STRIKE = re.compile(r'~~([^~]+)~~')
TAG = re.compile(r'(?<!\w)#([\w\u4e00-\u9fa5-]+)')


def read(path):
    return Path(path).read_text(encoding='utf-8-sig')


def write(path, text, bom=False, crlf=False):
    """写文本文件。bom=True 让 Windows 记事本/Word 能认出 UTF-8；crlf=True 输出 Windows 换行。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if crlf:
        text = text.replace('\r\n', '\n').replace('\n', '\r\n')
    path.write_text(text, encoding='utf-8-sig' if bom else 'utf-8', newline='')
    return path



def strip_front(text):
    """去掉开头的 YAML 前言（转换时不把 notion_id 之类元数据排进正文）。"""
    if not text.startswith('---'):
        return text
    match = re.match(r'(?s)^---\s*\n.*?\n---\s*\n', text)
    return text[match.end():] if match else text


def plain(text):
    """去掉 Markdown 语法，留纯文本（保留换行）。"""
    out = []
    in_code = False
    for line in text.split('\n'):
        if FENCE.match(line.strip()):
            in_code = not in_code
            continue
        if in_code:
            out.append(line)
            continue
        line = IMAGE.sub(lambda m: '（图：%s）' % (m.group(1) or '图片'), line)
        line = WIKI.sub(lambda m: m.group(2) or m.group(1).split('/')[-1], line)
        line = LINK.sub(lambda m: m.group(1), line)
        line = INLINE_CODE.sub(lambda m: m.group(1), line)
        line = BOLD.sub(lambda m: m.group(1), line)
        line = ITALIC.sub(lambda m: m.group(1), line)
        line = STRIKE.sub(lambda m: m.group(1), line)
        line = HEADING.sub(lambda m: m.group(2), line)
        line = re.sub(r'^\s*[-*+]\s+\[[ xX]\]\s+', '', line)
        line = BULLET.sub(lambda m: '· ' + m.group(1), line)
        line = QUOTE.sub(lambda m: m.group(1), line)
        out.append(line)
    return '\n'.join(out)


def blocks(text):
    """把 Markdown 切成块：(类型, 内容)。类型：heading/para/list/ordered/todo/code/quote/table/image。"""
    lines = text.split('\n')
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        fence = FENCE.match(stripped)
        if fence:
            lang, body = fence.group(1), []
            i += 1
            while i < len(lines) and not FENCE.match(lines[i].strip()):
                body.append(lines[i])
                i += 1
            i += 1
            out.append(('code', (lang, '\n'.join(body))))
            continue
        if not stripped:
            i += 1
            continue
        head = HEADING.match(stripped)
        if head:
            out.append(('heading', (len(head.group(1)), head.group(2).strip())))
            i += 1
            continue
        if IMAGE.match(stripped):
            m = IMAGE.match(stripped)
            out.append(('image', (m.group(1), m.group(2))))
            i += 1
            continue
        if stripped.startswith('|') and i + 1 < len(lines) and TABLE_SEP.match(lines[i + 1]):
            rows = [stripped]
            i += 2
            while i < len(lines) and lines[i].strip().startswith('|'):
                rows.append(lines[i].strip())
                i += 1
            table = [[cell.strip() for cell in row.strip('|').split('|')] for row in rows]
            out.append(('table', table))
            continue
        if TODO.match(line):
            items = []
            while i < len(lines) and TODO.match(lines[i]):
                m = TODO.match(lines[i])
                items.append((m.group(1).lower() == 'x', m.group(2).strip()))
                i += 1
            out.append(('todo', items))
            continue
        if BULLET.match(line):
            items = []
            while i < len(lines) and BULLET.match(lines[i]) and not TODO.match(lines[i]):
                items.append(BULLET.match(lines[i]).group(1).strip())
                i += 1
            out.append(('list', items))
            continue
        if NUMBERED.match(line):
            items = []
            while i < len(lines) and NUMBERED.match(lines[i]):
                items.append(NUMBERED.match(lines[i]).group(2).strip())
                i += 1
            out.append(('ordered', items))
            continue
        if QUOTE.match(line):
            body = []
            while i < len(lines) and QUOTE.match(lines[i]):
                body.append(QUOTE.match(lines[i]).group(1))
                i += 1
            out.append(('quote', '\n'.join(body)))
            continue
        para = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if (not nxt or FENCE.match(nxt) or HEADING.match(nxt) or BULLET.match(nxt)
                    or NUMBERED.match(nxt) or QUOTE.match(nxt) or IMAGE.match(nxt)
                    or (nxt.startswith('|') and i + 1 < len(lines) and TABLE_SEP.match(lines[i + 1]))):
                break
            para.append(nxt)
            i += 1
        out.append(('para', ' '.join(para)))
    return out


def inline_html(text):
    text = html.escape(text, quote=False)
    text = INLINE_CODE.sub(lambda m: '<code>%s</code>' % m.group(1), text)
    text = BOLD.sub(lambda m: '<strong>%s</strong>' % m.group(1), text)
    text = ITALIC.sub(lambda m: '<em>%s</em>' % m.group(1), text)
    text = STRIKE.sub(lambda m: '<del>%s</del>' % m.group(1), text)
    text = WIKI.sub(lambda m: '<span class="wiki">%s</span>' % html.escape(m.group(2) or m.group(1)), text)
    text = LINK.sub(lambda m: '<a href="%s">%s</a>' % (html.escape(m.group(2)), m.group(1)), text)
    return text

CSS = """
:root{--ink:#10161F;--muted:#4E5A66;--line:#DDE5E2;--accent:#2F7D74;--bg:#FFFFFF}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font-family:"Source Han Serif SC","Noto Serif SC","Songti SC",Georgia,serif;
 font-size:16px;line-height:1.85}
main{max-width:760px;margin:0 auto;padding:56px 28px 96px}
h1,h2,h3,h4,h5,h6{font-family:"Microsoft YaHei UI","PingFang SC",sans-serif;
 line-height:1.35;margin:1.8em 0 .6em;letter-spacing:.01em}
h1{font-size:2em;border-bottom:2px solid var(--accent);padding-bottom:.3em}
h2{font-size:1.5em;color:var(--accent)}
h3{font-size:1.2em}
p{margin:.9em 0}
a{color:var(--accent);text-decoration:none;border-bottom:1px dotted var(--accent)}
code{background:#EAF0EE;padding:.12em .35em;border-radius:3px;font-size:.92em;
 font-family:"Cascadia Code",Consolas,monospace}
pre{background:#F3F5F4;border:1px solid var(--line);border-left:3px solid var(--accent);
 padding:14px 16px;overflow:auto;border-radius:4px}
pre code{background:none;padding:0}
blockquote{margin:1.2em 0;padding:.2em 1em;border-left:3px solid var(--line);color:var(--muted)}
table{border-collapse:collapse;width:100%;margin:1.2em 0;font-size:.95em}
th,td{border:1px solid var(--line);padding:8px 10px;text-align:left}
th{background:#F3F5F4}
img{max-width:100%;border-radius:4px}
ul,ol{padding-left:1.4em}
li{margin:.35em 0}
.wiki{color:var(--accent)}
hr{border:none;border-top:1px solid var(--line);margin:2em 0}
"""


def md_to_html_text(text, title=''):
    text = strip_front(text)
    out = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
           '<meta name="generator" content="Notion 与 Obsidian 一键同步（咕月15）">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           '<title>%s</title><style>%s</style></head><body><main>' % (html.escape(title or '笔记'), CSS)]
    for kind, value in blocks(text):
        if kind == 'heading':
            level, body = value
            out.append('<h%d>%s</h%d>' % (level, inline_html(body), level))
        elif kind == 'para':
            out.append('<p>%s</p>' % inline_html(value))
        elif kind == 'list':
            out.append('<ul>%s</ul>' % ''.join('<li>%s</li>' % inline_html(i) for i in value))
        elif kind == 'ordered':
            out.append('<ol>%s</ol>' % ''.join('<li>%s</li>' % inline_html(i) for i in value))
        elif kind == 'todo':
            out.append('<ul class="todo">%s</ul>' % ''.join(
                '<li>%s %s</li>' % ('☑' if done else '☐', inline_html(text_)) for done, text_ in value))
        elif kind == 'quote':
            out.append('<blockquote>%s</blockquote>' % '<br>'.join(inline_html(x) for x in value.split('\n')))
        elif kind == 'code':
            lang, body = value
            out.append('<pre><code class="language-%s">%s</code></pre>' % (
                html.escape(lang or 'text'), html.escape(body)))
        elif kind == 'table':
            head, rest = value[0], value[1:]
            out.append('<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>' % (
                ''.join('<th>%s</th>' % inline_html(c) for c in head),
                ''.join('<tr>%s</tr>' % ''.join('<td>%s</td>' % inline_html(c) for c in row) for row in rest)))
        elif kind == 'image':
            alt, src = value
            out.append('<figure><img src="%s" alt="%s"><figcaption>%s</figcaption></figure>' % (
                html.escape(src), html.escape(alt), html.escape(alt)))
    out.append('</main></body></html>')
    return '\n'.join(out)


def md_to_html(src, dst, title=''):
    text = read(src)
    return write(dst, md_to_html_text(text, title or Path(src).stem), bom=True)


def md_to_txt(src, dst):
    return write(dst, plain(read(src)), bom=True, crlf=True)


def _runs(paragraph, text, style=None):
    """把一行 Markdown 行内语法拆成 docx 的 run（粗体/斜体/代码/链接文字）。"""
    pattern = re.compile(r'(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|\[\[[^\]]+\]\])')
    for chunk in pattern.split(text):
        if not chunk:
            continue
        bold = italic = False
        body = chunk
        if chunk.startswith('**') and chunk.endswith('**'):
            bold, body = True, chunk[2:-2]
        elif chunk.startswith('`') and chunk.endswith('`'):
            run = paragraph.add_run(chunk[1:-1])
            run.font.name = 'Consolas'
            continue
        elif chunk.startswith('[[') and chunk.endswith(']]'):
            inner = chunk[2:-2]
            body = inner.split('|')[-1]
        elif chunk.startswith('[') and '](' in chunk:
            m = LINK.match(chunk)
            body = m.group(1) if m else chunk
        run = paragraph.add_run(body)
        run.bold = bold
        run.italic = italic


def md_to_docx(src, dst, title=''):
    """写成 .docx：自己拼 OOXML，不再依赖 python-docx / lxml。"""
    import docxgen
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    text = strip_front(read(src))
    base = Path(src).resolve().parent
    if not title:
        for kind, value in blocks(text):
            if kind == 'heading':
                title = value[1]
                break
        else:
            title = Path(src).stem
    return docxgen.write(blocks(text), str(dst), title, image_dir=str(base))


def docx_to_md(src, dst):
    """读 .docx 转 Markdown：直接解 zip 里的 document.xml。"""
    import docxgen
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    return write(dst, docxgen.to_markdown(src))


def md_to_pdf(src, dst):
    """排成 PDF：自己拼 PDF，不再依赖 reportlab / PIL。"""
    import pdfgen
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    text = strip_front(read(src))
    base = Path(src).resolve().parent
    items = blocks(text)
    title = ''
    for kind, value in items:
        if kind == 'heading':
            title = value[1]
            break
    if not title:
        title = Path(src).stem
    # 第一个一级标题就是文件名/文档标题，页眉已经画了，正文里不再重复
    if items and items[0][0] == 'heading' and items[0][1][1].strip() == title.strip():
        items = items[1:]
    return pdfgen.render(items, str(dst), title, image_dir=str(base))


def md_text_to_pdf(text, dst, title=''):
    """已经是 Markdown 文本，直接排成 PDF。"""
    import pdfgen
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    items = blocks(strip_front(text))
    head = ''
    for kind, value in items:
        if kind == 'heading':
            head = value[1]
            break
    head = head or title or dst.stem
    if items and items[0][0] == 'heading' and items[0][1][1].strip() == head.strip():
        items = items[1:]
    return pdfgen.render(items, str(dst), head)


def md_text_to_docx(text, dst, title=''):
    import docxgen
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    body = strip_front(text)
    head = title
    if not head:
        for kind, value in blocks(body):
            if kind == 'heading':
                head = value[1]
                break
    return docxgen.write(blocks(body), str(dst), head or dst.stem)


def md_text_to_html(text, dst, title=''):
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    return write(dst, md_to_html_text(text, title or dst.stem), bom=True)


def html_to_md_text(text):
    """把 HTML 变成 Markdown 文本（不落盘）。"""
    text = re.sub(r'(?is)<(script|style).*?</\1>', '', text)
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)</(p|div|li|tr|h[1-6])>', '\n', text)
    for level in range(1, 7):
        text = re.sub(
            r'(?is)<h%d[^>]*>(.*?)</h%d>' % (level, level),
            lambda m, lv=level: '\n' + '#' * lv + ' ' + re.sub(r'<[^>]+>', '', m.group(1)) + '\n',
            text)
    text = re.sub(r'(?is)<li[^>]*>(.*?)</li>',
                  lambda m: '\n- ' + re.sub(r'<[^>]+>', '', m.group(1)), text)
    text = re.sub(r'(?is)<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                  lambda m: '[%s](%s)' % (re.sub(r'<[^>]+>', '', m.group(2)), m.group(1)), text)
    text = re.sub(r'(?is)<img [^>]*src="([^"]+)"[^>]*>', lambda m: '![](%s)' % m.group(1), text)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    return re.sub(r'\n{3,}', '\n\n', text).strip() + '\n'


def html_to_md(src, dst):
    text = read(src)
    text = re.sub(r'(?is)<(script|style).*?</\1>', '', text)
    text = re.sub(r'(?i)<br\s*/?>', '\n', text)
    text = re.sub(r'(?i)</(p|div|li|tr|h[1-6])>', '\n', text)
    for level in range(1, 7):
        text = re.sub(r'(?is)<h%d[^>]*>(.*?)</h%d>' % (level, level),
                      lambda m, lv=level: '\n' + '#' * lv + ' ' + re.sub(r'<[^>]+>', '', m.group(1)) + '\n', text)
    text = re.sub(r'(?is)<li[^>]*>(.*?)</li>', lambda m: '\n- ' + re.sub(r'<[^>]+>', '', m.group(1)), text)
    text = re.sub(r'(?is)<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                  lambda m: '[%s](%s)' % (re.sub(r'<[^>]+>', '', m.group(2)), m.group(1)), text)
    text = re.sub(r'(?is)<img [^>]*src="([^"]+)"[^>]*>', lambda m: '![](%s)' % m.group(1), text)
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip() + '\n'
    return write(dst, text)


def notion_md(src, dst):
    """把本地笔记转成 Notion 侧能接受的 Markdown（与本程序上传时用的是同一套转换）。"""
    import export_engine as ex
    body, warnings = ex.to_notion(read(src))
    write(dst, body + '\n')
    return dst, warnings


# ------------------------------------------------------------ 通用格式转换

def _docx_md_text(src):
    import docxgen
    return docxgen.to_markdown(src)


def _html_text(src):
    return html_to_md_text(read(src))


def _txt_text(src):
    text = Path(src).read_text(encoding='utf-8-sig', errors='replace')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    return re.sub(r'\n{3,}', '\n\n', text).strip() + '\n'


def _pptx_md_text(src):
    import docread
    slides = docread.pptx_slides(src)
    out = ['## 第 %d 页' % (i + 1) for i in range(len(slides))]
    lines = []
    for index, paragraphs in enumerate(slides, 1):
        lines.append('## 第 %d 页' % index)
        lines.append('')
        if paragraphs:
            lines.extend('- ' + text for text in paragraphs)
        else:
            lines.append('（这一页没有文字）')
        lines.append('')
    return '\n'.join(lines)


def docx_to_pdf(src, dst):
    return md_text_to_pdf(_docx_md_text(src), dst, Path(src).stem)


def docx_to_html(src, dst):
    return md_text_to_html(_docx_md_text(src), dst, Path(src).stem)


def docx_to_txt(src, dst):
    return write(dst, plain(_docx_md_text(src)), bom=True, crlf=True)


def html_to_pdf(src, dst):
    return md_text_to_pdf(_html_text(src), dst, Path(src).stem)


def html_to_docx(src, dst):
    return md_text_to_docx(_html_text(src), dst, Path(src).stem)


def html_to_txt(src, dst):
    return write(dst, plain(_html_text(src)), bom=True, crlf=True)


def txt_to_docx(src, dst):
    return md_text_to_docx(_txt_text(src), dst, Path(src).stem)


def txt_to_pdf(src, dst):
    return md_text_to_pdf(_txt_text(src), dst, Path(src).stem)


def txt_to_md(src, dst):
    import docread
    return docread.txt_to_md(src, dst)


def pptx_to_md(src, dst):
    import docread
    return docread.pptx_to_md(src, dst)


def csv_to_md(src, dst):
    import docread
    return docread.csv_to_md(src, dst)


def csv_to_docx(src, dst):
    return md_text_to_docx(Path(src).read_text(encoding='utf-8-sig'), dst, Path(src).stem)


def xlsx_to_csv(src, dst):
    import docread
    return docread.xlsx_to_csv(src, dst)


def xlsx_to_md(src, dst):
    import docread
    work = temp_dir('xlsx2md-')
    try:
        tmp = Path(work) / 'sheet.csv'
        docread.xlsx_to_csv(src, tmp)
        return docread.csv_to_md(tmp, dst)
    finally:
        shutil.rmtree(work, ignore_errors=True)


_TABLE_NEW = [
    ('docx2pdf', 'Word → PDF', '.docx', '.pdf', '按 Word 里的标题、列表、表格排版。'),
    ('docx2html', 'Word → HTML', '.docx', '.html', '转成能直接发给别人的网页。'),
    ('docx2txt', 'Word → 纯文本', '.docx', '.txt', '只留文字。'),
    ('html2pdf', 'HTML → PDF', '.html', '.pdf', '先把标签读成 Markdown，再排版。'),
    ('html2docx', 'HTML → Word', '.html', '.docx', '同上，输出 .docx。'),
    ('html2txt', 'HTML → 纯文本', '.html', '.txt', '只留文字。'),
    ('txt2md', '纯文本 → Markdown', '.txt', '.md', '段落原样保留。'),
    ('txt2docx', '纯文本 → Word', '.txt', '.docx', '每段一段落。'),
    ('txt2pdf', '纯文本 → PDF', '.txt', '.pdf', '每段一段落。'),
    ('pptx2md', 'PowerPoint → Markdown', '.pptx', '.md', '每页一节，文字按项目符号列出；图片不转。'),
    ('csv2md', 'CSV → Markdown', '.csv', '.md', '变成 Markdown 表格。'),
    ('csv2docx', 'CSV → Word', '.csv', '.docx', '变成 Word 表格。'),
    ('xlsx2csv', 'Excel → CSV', '.xlsx', '.csv', '只读第一张工作表。'),
    ('xlsx2md', 'Excel → Markdown', '.xlsx', '.md', '只读第一张工作表，转成表格。'),
]

_NEW_CONVERTERS = {
    'docx2pdf': docx_to_pdf, 'docx2html': docx_to_html, 'docx2txt': docx_to_txt,
    'html2pdf': html_to_pdf, 'html2docx': html_to_docx, 'html2txt': html_to_txt,
    'txt2md': txt_to_md, 'txt2docx': txt_to_docx, 'txt2pdf': txt_to_pdf,
    'pptx2md': pptx_to_md,
    'csv2md': csv_to_md, 'csv2docx': csv_to_docx,
    'xlsx2csv': xlsx_to_csv, 'xlsx2md': xlsx_to_md,
}

_NEW_INPUT = {}
for _key, _label, _src, _dst, _desc in _TABLE_NEW:
    _NEW_INPUT[_key] = tuple({_src, _src + '', '.doc' if _src == '.docx' else _src})


# ---------------------------------------------------------------- 笔记整理

SKIP_PARTS = {'.同步工具', '.obsidian', '.trash', '.git', '归档'}


def _notes(vault):
    from vault_tools import note_files
    for rel, path in note_files(vault):
        if path.suffix.lower() != '.md' or any(part in SKIP_PARTS for part in PurePosixPath(rel).parts):
            continue
        yield path


def vault_stats(vault):
    vault = Path(vault)
    rows = []
    total = 0
    for folder in ('知识库', '日常生活', '日程提醒'):
        base = vault / folder
        count = len(list(base.rglob('*.md'))) if base.is_dir() else 0
        total += count
        rows.append((folder, count))
    attach = vault / '知识库' / '附件'
    files = [p for p in attach.rglob('*') if p.is_file()] if attach.is_dir() else []
    size = sum(p.stat().st_size for p in files)
    return {'folders': rows, 'notes': total, 'attachments': len(files),
            'attachment_mb': round(size / 1024 / 1024, 1)}


def _unescape(text):
    return re.sub(r"\\([\\`*_{}\[\]()#+\-.!$<>|~&])", r"\1", text or "")


def broken_links(vault):
    """找出指向不存在笔记的 [[双链]]（先还原 Notion 写的反斜杠转义）。"""
    vault = Path(vault)
    names = {p.stem for p in _notes(vault)}
    paths = {p.relative_to(vault).with_suffix('').as_posix() for p in _notes(vault)}
    from vault_tools import vault_files
    files = [p for rel, p in vault_files(vault)
             if not any(part in SKIP_PARTS for part in PurePosixPath(rel).parts)]
    asset_names = {p.name for p in files}
    asset_paths = {p.relative_to(vault).as_posix() for p in files}
    bad = []
    for path in _notes(vault):
        try:
            text = path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeDecodeError):
            continue
        for match in WIKI.finditer(text):
            raw = _unescape(match.group(1)).replace('\\|', '|')
            target = raw.split('|')[0].split('#')[0].strip('/ .').rstrip('\\')
            if not target:
                continue
            if '/' in target:
                ok = target in paths or target in asset_paths or PurePosixPath(target).stem in names
            else:
                ok = target in names or target in asset_names
            if not ok:
                bad.append((path.relative_to(vault).as_posix(), target))
    return bad


def unused_attachments(vault):
    """附件目录里没有被任何笔记引用的文件。"""
    vault = Path(vault)
    attach = vault / '知识库' / '附件'
    if not attach.is_dir():
        return []
    used = set()
    for path in _notes(vault):
        try:
            text = path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeDecodeError):
            continue
        for match in re.finditer(r'!?\[\[([^\]]+)\]\]|!\[[^\]]*\]\(([^)]+)\)', text):
            name = (match.group(1) or match.group(2) or '').split('|')[0].strip()
            used.add(PurePosixPath(name).name)
    return sorted(p.relative_to(vault).as_posix() for p in attach.rglob('*')
                  if p.is_file() and p.name not in used)


def duplicate_titles(vault):
    seen, dup = {}, []
    for path in _notes(vault):
        seen.setdefault(path.stem, []).append(path.relative_to(vault).as_posix())
    for title, paths in sorted(seen.items()):
        if len(paths) > 1:
            dup.append((title, paths))
    return dup

TOOLS = [
    ('md2docx', 'Markdown → Word（.docx）', 'file', '.docx',
     '保留标题、列表、表格、代码块和图片；图片按相对路径查找。'),
    ('md2pdf', 'Markdown → PDF', 'file', '.pdf',
     '先排成网页，再用本机 Edge 无头打印，版式与 HTML 一致。'),
    ('md2html', 'Markdown → HTML', 'file', '.html',
     '自带一套适合长文阅读的排版，可直接发给别人或贴进博客。'),
    ('md2txt', 'Markdown → 纯文本', 'file', '.txt', '去掉所有标记，只留文字。'),
    ('docx2md', 'Word（.docx）→ Markdown', 'file', '.md', '标题、列表、表格尽量还原。'),
    ('html2md', 'HTML → Markdown', 'file', '.md', '去掉标签，保留标题/链接/图片。'),
    ('notionmd', '本地笔记 → Notion Markdown', 'file', '.notion.md',
     '先看 Notion 会收到什么。'),
    ('vstats', '笔记统计', 'vault', None, '统计三个栏目的篇数、附件数量与占用。'),
    ('broken', '断链检查', 'vault', None, '找出指向不存在笔记的 [[双链]]。'),
    ('unused', '未引用附件', 'vault', None, '附件目录里没有任何笔记引用的文件。'),
    ('dup', '同名笔记', 'vault', None, '同名文件（跨栏目也算），方便合并。'),
]

CONVERTERS = {
    'md2docx': lambda s, d: md_to_docx(s, d),
    'md2pdf': lambda s, d: md_to_pdf(s, d),
    'md2html': lambda s, d: md_to_html(s, d),
    'md2txt': lambda s, d: md_to_txt(s, d),
    'docx2md': lambda s, d: docx_to_md(s, d),
    'html2md': lambda s, d: html_to_md(s, d),
}


def describe(key):
    for row in TOOLS:
        if row[0] == key:
            return row
    raise KeyError(key)


INPUT_SUFFIX = {
    'md2docx': ('.md',), 'md2pdf': ('.md',), 'md2html': ('.md',), 'md2txt': ('.md',),
    'docx2md': ('.docx',), 'html2md': ('.html', '.htm'), 'notionmd': ('.md',),
}


for _key, _label, _src, _dst, _desc in _TABLE_NEW:
    TOOLS.append((_key, _label, 'file', _dst, _desc))
CONVERTERS.update(_NEW_CONVERTERS)
for _key, _label, _src, _dst, _desc in _TABLE_NEW:
    suffixes = {_src}
    if _src == '.html':
        suffixes = {'.html', '.htm'}
    if _src == '.txt':
        suffixes = {'.txt', '.text'}
    INPUT_SUFFIX[_key] = tuple(sorted(suffixes))



def run_tool(key, src, dst=None, log=None):
    say = log or (lambda text: None)
    row = describe(key)
    kind = row[2]
    if kind == 'vault':
        return _run_vault(key, src, say)

    src = Path(src)
    if src.is_dir():
        return _run_folder(key, src, dst, say)
    if not src.is_file():
        raise RuntimeError('请选择要转换的文件')
    if key == 'notionmd':
        target = Path(dst) if dst else src.with_suffix('.notion.md')
        target, warnings = notion_md(src, target)
        say('· 已生成：' + str(target))
        for warning in warnings[:30]:
            say('   提示：' + warning)
        return str(target)
    if not dst:
        dst = src.with_suffix(row[3])
    target = Path(dst)
    CONVERTERS[key](src, target)
    say('· 已生成：%s（%.1f KB）' % (target, target.stat().st_size / 1024))
    return str(target)


def _run_folder(key, src, dst, say):
    """整个文件夹批量转换：保持子目录结构，结果放进目标文件夹。"""
    row = describe(key)
    suffix = row[3]
    if not suffix:
        raise RuntimeError('这个工具只能处理单个文件')
    wanted = INPUT_SUFFIX.get(key, ('.md',))
    files = [p for p in sorted(src.rglob('*'))
             if p.is_file() and p.suffix.lower() in wanted
             and not any(part in SKIP_PARTS or part.startswith('.') for part in p.relative_to(src).parts[:-1])]
    if not files:
        raise RuntimeError('这个文件夹里没有 %s 文件' % ' / '.join(wanted))
    out_root = Path(dst) if dst else src
    done = 0
    failed = []
    say('· 共 %d 个文件，输出到：%s' % (len(files), out_root))
    for path in files:
        rel = path.relative_to(src)
        target = out_root / rel.with_suffix(suffix)
        if target.resolve() == path.resolve():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            CONVERTERS[key](path, target)
            done += 1
            say('   %s → %s' % (rel.as_posix(), target.name))
        except Exception as exc:  # noqa: BLE001
            failed.append((rel.as_posix(), str(exc)))
            say('   失败：%s（%s）' % (rel.as_posix(), exc))
    text = '完成 %d 个' % done
    if failed:
        text += '，失败 %d 个' % len(failed)
    say('· ' + text)
    return text


def _run_vault(key, src, say):
    vault = Path(src)
    if not vault.is_dir():
        raise RuntimeError('请选择一个 Obsidian 知识库文件夹')
    if key == 'vstats':
        data = vault_stats(vault)
        lines = ['%s：%d 篇' % (name, count) for name, count in data['folders']]
        lines.append('合计 %d 篇笔记；附件 %d 个，%.1f MB'
                     % (data['notes'], data['attachments'], data['attachment_mb']))
        for line in lines:
            say('· ' + line)
        return '\n'.join(lines)
    if key == 'broken':
        bad = broken_links(vault)
        if not bad:
            say('· 没有断链')
            return '没有断链'
        say('· 发现 %d 处断链（前 20 条）：' % len(bad))
        for rel, target in bad[:20]:
            say('   %s → %s' % (rel, target))
        return '%d 处断链' % len(bad)
    if key == 'unused':
        files = unused_attachments(vault)
        if not files:
            say('· 附件都被引用了')
            return '没有未引用附件'
        say('· 有 %d 个附件没被引用：' % len(files))
        for rel in files[:20]:
            say('   ' + rel)
        return '%d 个未引用附件' % len(files)
    if key == 'dup':
        dups = duplicate_titles(vault)
        if not dups:
            say('· 没有同名笔记')
            return '没有同名笔记'
        say('· 有 %d 组同名：' % len(dups))
        for title, paths in dups[:15]:
            say('   %s：%s' % (title, '、'.join(paths)))
        return '%d 组同名' % len(dups)
    raise RuntimeError('未知工具：' + key)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='工具箱子命令')
    parser.add_argument('--tool', required=True, choices=[row[0] for row in TOOLS])
    parser.add_argument('--src', required=True)
    parser.add_argument('--dst', default='')
    args = parser.parse_args(argv)
    print(run_tool(args.tool, args.src, args.dst or None, log=print))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

SHORT = {
    'md2docx': 'Word（.docx）', 'md2pdf': 'PDF', 'md2html': 'HTML',
    'md2txt': '纯文本', 'docx2md': 'Word → MD', 'html2md': 'HTML → MD',
    'notionmd': '→ Notion', 'vstats': '笔记统计', 'broken': '断链检查',
    'unused': '未引用附件', 'dup': '同名笔记',
}



def _open_viewer(suite, parent):
    import viewer
    return viewer.open_view(parent, suite)


def _vault_path(suite):
    """当前知识库；界面里没填就回落到设置里的。"""
    vault = ''
    try:
        vault = suite.current_vault()
    except Exception:
        vault = ''
    if not vault:
        try:
            import app as A
            vault = str((A.load_settings() or {}).get('vault') or '')
        except Exception:
            vault = ''
    return vault


def _with_vault(suite, parent, opener):
    from tkinter import messagebox
    vault = _vault_path(suite)
    if not vault or not Path(vault).is_dir():
        messagebox.showinfo('先选知识库', '主界面先选好知识库文件夹，再回来用这个。', parent=parent)
        return None
    return opener(parent, suite, vault)


def _run_on_vault(suite, parent, key):
    """点一下就在当前知识库上跑某个检查工具。"""
    vault = _vault_path(suite)
    if not vault or not Path(vault).is_dir():
        from tkinter import messagebox
        messagebox.showinfo('先选知识库', '主界面先选好知识库文件夹，再回来用这个。', parent=parent)
        return None
    label = describe(key)[1]

    def job():
        run_tool(key, vault, None, log=suite.write)
        suite.write('工具完成：' + label, 'ok')

    suite.run_task('工具：' + label, job)
    return True


def _open_book(suite, parent):
    import writer as _writer
    return _with_vault(suite, parent, _writer.open_book)


def _open_bulk(suite, parent):
    import bulk as _bulk
    return _with_vault(suite, parent, _bulk.open_bulk)


def _open_checkup(suite, parent):
    import checkup as _checkup
    return _with_vault(suite, parent, _checkup.open_checkup)


def _open_snapshots(suite, parent):
    import organizer as _org
    return _with_vault(suite, parent, _org.open_snapshots)


def _open_rename(suite, parent):
    import organizer as _org
    return _with_vault(suite, parent, _org.open_rename)


def _open_orphans(suite, parent):
    import organizer as _org
    return _with_vault(suite, parent, _org.open_orphans)


def build_panel(suite, parent):
    """工具箱页：通用转换 + 笔记检查 + 库整理 + 写作工具，每组带「详情」。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    import ui_kit as U

    state = {'key': CONVERTERS and next(iter(CONVERTERS)) or TOOLS[0][0]}

    area = tk.Frame(parent, bg=U.SURFACE)
    area.pack(fill='both', expand=True)
    canvas = tk.Canvas(area, bg=U.SURFACE, height=U.s(370), highlightthickness=0, bd=0)
    bar = ttk.Scrollbar(area, orient='vertical', command=canvas.yview)
    canvas.configure(yscrollcommand=bar.set)
    bar.pack(side='right', fill='y')
    canvas.pack(side='left', fill='both', expand=True)
    box = tk.Frame(canvas, bg=U.SURFACE)
    window_id = canvas.create_window((0, 0), window=box, anchor='nw')
    box.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window_id, width=event.width))

    def scroll_wheel(event):
        widget = event.widget
        while widget is not None and widget is not box:
            widget = getattr(widget, 'master', None)
        if widget is box and canvas.winfo_ismapped():
            canvas.yview_scroll(-int(event.delta / 120), 'units')
            return 'break'

    suite.root.bind_all('<MouseWheel>', scroll_wheel, add='+')

    def group(title):
        tk.Label(box, text=title, bg=U.SURFACE, fg=U.INK, anchor='w',
                 font=U.body(10, 'bold')).pack(fill='x', pady=(U.s(8), U.s(4)))
        row = tk.Frame(box, bg=U.SURFACE)
        row.pack(fill='x')
        return row

    def action_btn(row, text, command, help_key=None):
        btn = U.RButton(row, text, command=command, variant='ghost', bg=U.SURFACE,
                        height=30, padx=10, font=U.body(9))
        btn.pack(side='left', padx=(0, U.s(4 if help_key else 10)))
        if help_key:
            U.HelpDot(row, help_key, host=suite.root, bg=U.SURFACE).pack(
                side='left', padx=(0, U.s(10)))
        return btn

    row = group('检查笔记')
    action_btn(row, '库体检', lambda: _open_checkup(suite, parent), 'tool_checkup')
    action_btn(row, '笔记统计', lambda: _run_on_vault(suite, parent, 'vstats'), 'tool_stats')
    action_btn(row, '断链检查', lambda: _run_on_vault(suite, parent, 'broken'), 'tool_broken')
    action_btn(row, '附件检查', lambda: _run_on_vault(suite, parent, 'unused'), 'tool_unused')
    action_btn(row, '同名笔记', lambda: _run_on_vault(suite, parent, 'dup'), 'tool_dup')

    row = group('整理知识库')
    action_btn(row, '整理笔记格式', lambda: suite.show_page('fix'))
    action_btn(row, '库快照', lambda: _open_snapshots(suite, parent), 'tool_snap')
    action_btn(row, '批量改名', lambda: _open_rename(suite, parent), 'tool_rename')
    action_btn(row, '查找替换', lambda: _open_bulk(suite, parent), 'tool_bulk')
    action_btn(row, '附件归档', lambda: _open_orphans(suite, parent), 'tool_orphan')

    row = group('查看与成书')
    action_btn(row, '查看文本', lambda: _open_viewer(suite, parent))
    action_btn(row, '合并成书', lambda: _open_book(suite, parent), 'tool_book')

    # ---- 通用转换 ----
    row = group('文件格式转换')
    _fmt = [('md', 'Markdown'), ('docx', 'Word'), ('pdf', 'PDF'), ('html', 'HTML'),
            ('txt', '纯文本'), ('pptx', 'PowerPoint'), ('xlsx', 'Excel'), ('csv', 'CSV')]
    _ext_of = {label: ext for ext, label in _fmt}
    src_fmt = tk.StringVar(value='Word')
    dst_fmt = tk.StringVar(value='PDF')
    src_box = ttk.Combobox(row, textvariable=src_fmt,
                           values=[label for ext, label in _fmt
                                   if any(key.startswith(ext + '2') for key in CONVERTERS)],
                           width=11, state='readonly')
    src_box.pack(side='left')
    tk.Label(row, text='→', bg=U.SURFACE, fg=U.FAINT,
             font=U.body(9)).pack(side='left', padx=U.s(6))
    dst_box = ttk.Combobox(row, textvariable=dst_fmt, width=11, state='readonly')
    dst_box.pack(side='left')
    U.HelpDot(row, 'convert_matrix', host=suite.root, bg=U.SURFACE).pack(
        side='left', padx=(U.s(8), 0))

    def _apply_matrix(event=None):
        source = _ext_of.get(src_fmt.get(), '')
        target = _ext_of.get(dst_fmt.get(), '')
        key = source + '2' + target
        if key in CONVERTERS:
            select(key)

    def _resync(event=None):
        source = _ext_of.get(src_fmt.get(), '')
        targets = [label for ext, label in _fmt
                   if (source + '2' + ext) in CONVERTERS and label != src_fmt.get()]
        dst_box.configure(values=targets)
        if targets and dst_fmt.get() not in targets:
            dst_fmt.set(targets[0])
        _apply_matrix()

    src_box.bind('<<ComboboxSelected>>', _resync)
    dst_box.bind('<<ComboboxSelected>>', _apply_matrix)

    hint = tk.Label(row, text='', bg=U.SURFACE, fg=U.MUTED, justify='left', anchor='w',
                    wraplength=U.s(300), font=U.body(9))
    hint.pack(side='left', fill='x', expand=True, padx=(U.s(10), 0))
    hint.bind('<Configure>', lambda event: hint.configure(wraplength=max(100, event.width)))

    src_row = tk.Frame(box, bg=U.SURFACE)
    src_row.pack(fill='x', pady=(U.s(6), 0))
    src_label = tk.Label(src_row, text='源文件', bg=U.SURFACE, fg=U.MUTED,
                         anchor='w', width=7, font=U.body(9))
    src_label.pack(side='left')
    src_var = tk.StringVar()
    src_entry = ttk.Entry(src_row, textvariable=src_var, style='Field.TEntry')
    src_entry.pack(side='left', fill='x', expand=True)
    src_btn = ttk.Button(src_row, text='选择文件', style='Small.TButton')
    src_btn.pack(side='left', padx=(U.s(10), 0))
    dir_btn = ttk.Button(src_row, text='整个文件夹', style='Small.TButton')
    dir_btn.pack(side='left', padx=(U.s(7), 0))

    dst_row = tk.Frame(box, bg=U.SURFACE)
    dst_row.pack(fill='x', pady=(U.s(6), 0))
    tk.Label(dst_row, text='输出到', bg=U.SURFACE, fg=U.MUTED,
             anchor='w', width=7, font=U.body(9)).pack(side='left')
    dst_var = tk.StringVar()
    dst_entry = ttk.Entry(dst_row, textvariable=dst_var, style='Field.TEntry')
    dst_entry.pack(side='left', fill='x', expand=True)
    dst_btn = ttk.Button(dst_row, text='另存为', style='Small.TButton')
    dst_btn.pack(side='left', padx=(U.s(10), 0))
    run_btn = ttk.Button(dst_row, text='开始转换', style='Accent.TButton')
    run_btn.pack(side='left', padx=(U.s(7), 0))

    def current():
        return describe(state['key'])

    def select(key):
        state['key'] = key
        _key, label, kind, suffix, text = current()
        hint.configure(text=text)
        if kind == 'vault':
            src_label.configure(text='知识库文件夹')
            src_btn.configure(text='选择知识库')
            dir_btn.pack_forget()
            dst_row.pack_forget()
            run_btn.configure(text='开始检查')
        else:
            src_label.configure(text='源文件')
            src_btn.configure(text='选择文件')
            if not dir_btn.winfo_ismapped():
                dir_btn.pack(side='left', padx=(U.s(7), 0))
            if not dst_row.winfo_manager():
                dst_row.pack(fill='x', pady=(U.s(6), 0), after=src_row)
            run_btn.configure(text='开始转换')
            src = src_var.get().strip()
            if src and Path(src).is_file():
                dst_var.set(str(Path(src).with_suffix(suffix)))

    def choose():
        _key, label, kind, suffix, _text = current()
        if kind == 'vault':
            path = filedialog.askdirectory(parent=parent, title='选择 Obsidian 知识库')
        else:
            path = filedialog.askopenfilename(parent=parent, title='选择要转换的文件',
                                              filetypes=[('所有文件', '*.*')])
        if path:
            if kind != 'vault':
                ext = Path(path).suffix.lower().lstrip('.')
                ext = {'htm': 'html'}.get(ext, ext)
                label_by_ext = dict(_fmt)
                if ext not in label_by_ext or not any(key.startswith(ext + '2') for key in CONVERTERS):
                    messagebox.showinfo('暂不支持这种输入格式', '请选择支持转换的源文件。', parent=parent)
                    return
                src_var.set(path)
                src_fmt.set(label_by_ext[ext])
                _resync()
                suffix = current()[3]
                dst_var.set(str(Path(path).with_suffix(suffix)))
            else:
                src_var.set(path)

    def choose_dir():
        path = filedialog.askdirectory(parent=parent, title='选择要转换的文件夹')
        if path:
            src_var.set(path)
            dst_var.set('')

    def choose_dst():
        _key, _label, kind, suffix, _text = current()
        if src_var.get().strip() and Path(src_var.get().strip()).is_dir():
            path = filedialog.askdirectory(parent=parent, title='选择输出文件夹')
            if path:
                dst_var.set(path)
            return
        path = filedialog.asksaveasfilename(
            parent=parent, title='保存为', defaultextension=suffix,
            initialfile=Path(dst_var.get() or 'output' + suffix).name)
        if path:
            dst_var.set(path)

    def start():
        _key, label, kind, suffix, _text = current()
        src = src_var.get().strip()
        if not src:
            messagebox.showinfo('还差一步',
                                '先选%s。' % ('知识库文件夹' if kind == 'vault' else '要转换的文件或文件夹'),
                                parent=parent)
            return
        dst = dst_var.get().strip() or None

        def job():
            run_tool(_key, src, dst, log=suite.write)
            suite.write('工具完成：' + label, 'ok')
        suite.run_task('工具：' + label, job)

    src_btn.configure(command=choose)
    dir_btn.configure(command=choose_dir)
    dst_btn.configure(command=choose_dst)
    run_btn.configure(command=start)
    _resync()
    return box
