# -*- coding: utf-8 -*-
"""把多篇笔记合成一本书（PDF / Word）。

用途：把某个栏目（或挑好的几篇）按标题顺序合成一份带目录的文档，
用于交作业、给客户看、打印、做成电子书。纯标准库，不引入新依赖。
"""
from pathlib import Path, PurePosixPath

import docxgen
import md_fix
import pdfgen
import toolbox as T

import journal

VERSION = journal.VERSION


def _title_of(text, rel):
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('# '):
            return line[2:].strip() or PurePosixPath(rel).stem
    return PurePosixPath(rel).stem


def notes_in(vault, folder=''):
    """收集目录下的笔记（按文件名排序）→ [{rel,title,text,blocks}]。"""
    vault = Path(vault)
    base = vault / folder if folder else vault
    if not base.is_dir():
        return []
    out = []
    from vault_tools import note_files
    for _relative, path in note_files(base):
        if path.suffix.lower() != '.md':
            continue
        rel = path.relative_to(vault).as_posix()
        if any(part.startswith('.') for part in Path(rel).parts):
            continue
        try:
            text = T.read(path)
        except OSError:
            continue
        out.append({'rel': rel, 'title': _title_of(text, rel), 'text': text})
    return out


def pick_notes(vault, rels):
    vault = Path(vault)
    out = []
    for rel in rels:
        path = vault / rel
        if path.is_file():
            text = T.read(path)
            out.append({'rel': rel, 'title': _title_of(text, rel), 'text': text})
    return out


def _body_blocks(note):
    """去掉 YAML 前言与首个一级标题，切成块；顺手清掉 Notion 残留标签。"""
    body = T.strip_front(note['text'])
    try:
        body = md_fix.fix_text(body, {}, _fresh_stats())
    except Exception:  # noqa: BLE001
        pass
    kept, dropped = [], False
    for line in body.splitlines():
        if not dropped and line.strip().startswith('# '):
            dropped = True
            continue
        kept.append(line)
    return T.blocks('\n'.join(kept))


def _fresh_stats():
    """md_fix 需要一份完整的统计字典，缺键会被 KeyError 打断。"""
    return {key: 0 for key in ('ul', 'ol', 'li', 'todo', 'br', 'mention', 'toc',
                               'table', 'callout', 'span', 'color')}

def to_markdown(notes, title='合集'):
    lines = ['# ' + title, '', '共 %d 篇。' % len(notes), '', '## 目录', '']
    for index, note in enumerate(notes, 1):
        lines.append('%d. %s' % (index, note['title']))
    for index, note in enumerate(notes, 1):
        lines += ['', '## %d. %s' % (index, note['title']), '']
        lines.append('\n'.join(
            line for line in T.strip_front(note['text']).splitlines()
            if not line.strip().startswith('# ')).strip())
    return '\n'.join(lines)


def _image_path(note, src):
    base = Path(note['rel']).parent
    path = Path(src)
    return path if path.is_absolute() else base / src


def to_pdf(notes, dst, title='合集', vault=''):
    doc = pdfgen.Doc(pdfgen.load_fonts())
    doc.space(140)
    doc.paragraph(title, role='h1', size=22, leading=30)
    doc.paragraph('共 %d 篇 · 咕月15 生成' % len(notes), role='body', size=10.5,
                  color=(0.35, 0.38, 0.42))
    doc.space(18)
    doc.paragraph('目录', role='h2', size=14, leading=22)
    for index, note in enumerate(notes, 1):
        doc.paragraph('%d. %s' % (index, note['title']), role='body', size=10.5, leading=19)

    for index, note in enumerate(notes, 1):
        doc._new_page()
        doc.paragraph('%d. %s' % (index, note['title']), role='h2', size=16, leading=24)
        for kind, value in _body_blocks(note):
            if kind == 'heading':
                level, text = value
                size = {1: 15, 2: 13, 3: 11.5}.get(level, 11)
                doc.paragraph(text, role='h2' if level <= 2 else 'h3', size=size,
                              leading=size * 1.5)
            elif kind == 'para':
                doc.paragraph(value, role='body', size=10.5, leading=17, rich=True)
            elif kind == 'list':
                for item in value:
                    doc.bullet(item)
            elif kind == 'ordered':
                for item in value:
                    doc.bullet(item, marker='1.')
            elif kind == 'todo':
                for done, item in value:
                    doc.bullet(item, marker='☑' if done else '☐')
            elif kind == 'quote':
                for line in str(value).split('\n'):
                    doc.paragraph('│ ' + line, role='body', size=10, leading=16)
            elif kind == 'code':
                _lang, body = value if isinstance(value, tuple) else ('', value)
                doc.code_block(body)
            elif kind == 'table':
                doc.table(value)
            elif kind == 'image':
                alt, src = value if isinstance(value, tuple) else ('', value)
                path = _image_path(note, src)
                if vault:
                    path = Path(vault) / path
                if path.is_file():
                    doc.image(path)
                else:
                    doc.paragraph('[图片未能嵌入：%s]' % alt, role='body', size=10)
    doc.save(dst, title)


def to_docx(notes, dst, title='合集', vault=''):
    blocks = [('heading', (1, title)),
              ('para', '共 %d 篇 · 咕月15 生成' % len(notes)),
              ('pagebreak', None),
              ('heading', (1, '目录'))]
    for index, note in enumerate(notes, 1):
        blocks.append(('para', '%d. %s' % (index, note['title'])))
    for index, note in enumerate(notes, 1):
        blocks.append(('pagebreak', None))
        blocks.append(('heading', (1, '%d. %s' % (index, note['title']))))
        for kind, value in _body_blocks(note):
            if kind == 'image' and isinstance(value, tuple) and vault:
                alt, src = value
                value = (alt, str(Path(vault) / _image_path(note, src)))
            blocks.append((kind, value))
    return docxgen.write(blocks, dst, title)
