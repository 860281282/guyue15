# -*- coding: utf-8 -*-
"""读别的文件格式（PPTX / XLSX / CSV），只依赖标准库。

这些函数只为「格式转换」服务：把源文件读成纯文本或表格，再交给 pdfgen / docxgen 排版。
版式不追求 1:1，追求「文字不丢、结构可读」。
"""

import csv
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

VERSION = '1.0.0'

NS_A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
NS_P = '{http://schemas.openxmlformats.org/presentationml/2006/main}'


# ------------------------------------------------------------------ pptx

def _slide_number(name):
    m = re.search(r'slide(\d+)\.xml$', name)
    return int(m.group(1)) if m else 0


def pptx_slides(path):
    """返回 [[段落文本, ...], ...]，每个内层列表是一页幻灯片的段落。"""
    slides = []
    with zipfile.ZipFile(path) as zf:
        names = [n for n in zf.namelist() if re.match(r'ppt/slides/slide\d+\.xml$', n)]
        for name in sorted(names, key=_slide_number):
            root = ET.fromstring(zf.read(name))
            paragraphs = []
            for para in root.iter(NS_A + 'p'):
                chunks = [node.text or '' for node in para.iter(NS_A + 't')]
                text = ''.join(chunks).strip()
                if text:
                    paragraphs.append(text)
            slides.append(paragraphs)
    return slides


def pptx_to_md(src, dst):
    slides = pptx_slides(src)
    stem = Path(src).stem
    out = ['# %s' % stem, '']
    for index, paragraphs in enumerate(slides, 1):
        out.append('## 第 %d 页' % index)
        out.append('')
        if not paragraphs:
            out.append('（这一页没有文字）')
        else:
            for text in paragraphs:
                out.append('- ' + text)
        out.append('')
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text('\n'.join(out), encoding='utf-8')
    return dst


# ------------------------------------------------------------------ xlsx

def _col_index(cell_ref):
    letters = re.match(r'[A-Z]+', cell_ref or 'A').group(0)
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _shared_strings(zf):
    if 'xl/sharedStrings.xml' not in zf.namelist():
        return []
    root = ET.fromstring(zf.read('xl/sharedStrings.xml'))
    values = []
    for item in root:
        values.append(''.join(node.text or '' for node in item.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t')))
    return values


def xlsx_rows(path, sheet=1):
    """读出第一张（或第 N 张）工作表，返回 [[单元格字符串, ...], ...]。"""
    with zipfile.ZipFile(path) as zf:
        shared = _shared_strings(zf)
        name = 'xl/worksheets/sheet%d.xml' % sheet
        if name not in zf.namelist():
            candidates = sorted(n for n in zf.namelist() if n.startswith('xl/worksheets/sheet'))
            if not candidates:
                return []
            name = candidates[0]
        root = ET.fromstring(zf.read(name))
        rows = []
        for row in root.iter('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}row'):
            cells = {}
            for cell in row:
                ref = cell.get('r') or 'A1'
                kind = cell.get('t')
                value = ''
                if kind == 'inlineStr':
                    value = ''.join(node.text or '' for node in cell.iter(
                        '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t'))
                else:
                    tag = cell.find('{http://schemas.openxmlformats.org/spreadsheetml/2006/main}v')
                    if tag is not None and tag.text is not None:
                        value = tag.text
                        if kind == 's':
                            try:
                                value = shared[int(value)]
                            except (ValueError, IndexError):
                                pass
                cells[_col_index(ref)] = value
            if cells:
                width = max(cells) + 1
                rows.append([cells.get(i, '') for i in range(width)])
        return rows


def xlsx_to_csv(src, dst, sheet=1):
    rows = xlsx_rows(src, sheet)
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    with open(dst, 'w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        for row in rows:
            writer.writerow(row)
    return dst


# ------------------------------------------------------------------ csv

def csv_to_md(src, dst):
    raw = Path(src).read_text(encoding='utf-8-sig')
    rows = [row for row in csv.reader(io.StringIO(raw)) if any(cell.strip() for cell in row)]
    if not rows:
        raise ValueError('这个 CSV 里没有内容')
    width = max(len(row) for row in rows)
    rows = [row + [''] * (width - len(row)) for row in rows]

    def cell(text):
        return str(text).replace('|', '\\|').replace('\n', ' ').strip()

    out = ['| ' + ' | '.join(cell(c) for c in rows[0]) + ' |',
           '|' + '|'.join([' --- '] * width) + '|']
    for row in rows[1:]:
        out.append('| ' + ' | '.join(cell(c) for c in row) + ' |')
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text('\n'.join(out) + '\n', encoding='utf-8')
    return dst


# ------------------------------------------------------------------ txt

def txt_to_md(src, dst):
    text = Path(src).read_text(encoding='utf-8-sig', errors='replace')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = []
    for line in text.split('\n'):
        stripped = line.strip()
        lines.append(stripped)
    body = '\n'.join(lines)
    body = re.sub(r'\n{3,}', '\n\n', body).strip()
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(body + '\n', encoding='utf-8')
    return dst
