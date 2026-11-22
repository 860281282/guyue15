# -*- coding: utf-8 -*-
"""自己读写 .docx：只覆盖本程序需要的段落类型。

为什么不用 python-docx：它依赖 lxml（6.6 MB 的二进制）。我们写的 .docx 只用到
标题、段落、列表、引用、代码、表格、图片，直接拼 OOXML 更轻，还能顺手把读回也做了。
"""

import os
import re
import zipfile
from xml.sax.saxutils import escape


CORE = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
 xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"
 xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<dc:title>{title}</dc:title>
<dc:creator>{author}</dc:creator>
<cp:lastModifiedBy>{author}</cp:lastModifiedBy>
<cp:revision>1</cp:revision>
</cp:coreProperties>
"""

AUTHOR = '咕月15'

CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Default Extension="jpeg" ContentType="image/jpeg"/>
<Default Extension="jpg" ContentType="image/jpeg"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>'''

RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''

STYLES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Microsoft YaHei" w:eastAsia="Microsoft YaHei" w:hAnsi="Microsoft YaHei"/>
<w:sz w:val="22"/></w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:styleId="Normal" w:default="1"><w:name w:val="Normal"/>
<w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/>
<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="240" w:after="120"/><w:outlineLvl w:val="0"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="40"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/>
<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="200" w:after="100"/><w:outlineLvl w:val="1"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading3"><w:name w:val="heading 3"/>
<w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="160" w:after="80"/><w:outlineLvl w:val="2"/></w:pPr>
<w:rPr><w:b/><w:sz w:val="26"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Quote"><w:name w:val="Quote"/>
<w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="360"/></w:pPr>
<w:rPr><w:i/><w:color w:val="595959"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="CodeBlock"><w:name w:val="Code Block"/>
<w:basedOn w:val="Normal"/><w:pPr><w:shd w:val="clear" w:fill="F2F2F2"/>
<w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>
<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="18"/></w:rPr></w:style>
</w:styles>'''

NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')

INLINE = re.compile(r'(\*\*[^*\n]+\*\*|`[^`\n]+`|\[\[[^\]\n]+\]\]|\[[^\]\n]+\]\([^)\s]+\))')


def _runs(text, size=None, mono=False):
    out = []
    for piece in INLINE.split(text):
        if not piece:
            continue
        bold = False
        code = mono
        if piece.startswith('**') and piece.endswith('**') and len(piece) > 4:
            piece = piece[2:-2]
            bold = True
        elif piece.startswith('`') and piece.endswith('`') and len(piece) > 2:
            piece = piece[1:-1]
            code = True
        elif piece.startswith('[[') and piece.endswith(']]'):
            label = piece[2:-2]
            piece = (label.split('|')[-1] if '|' in label else label).split('#')[0]
        elif piece.startswith('[') and '](' in piece and piece.endswith(')'):
            piece = piece[1:piece.index(']')]
        props = []
        if code:
            props.append('<w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/>')
        if bold:
            props.append('<w:b/>')
        if size:
            props.append('<w:sz w:val="%d"/>' % size)
        out.append('<w:r><w:rPr>%s</w:rPr><w:t xml:space="preserve">%s</w:t></w:r>'
                   % (''.join(props), escape(piece)))
    return ''.join(out)


def _para(text, style='Normal', size=None, mono=False, indent=None):
    props = ''
    if style != 'Normal':
        props = '<w:pStyle w:val="%s"/>' % style
    if indent:
        props += '<w:ind w:left="%d"/>' % indent
    return '<w:p><w:pPr>%s</w:pPr>%s</w:p>' % (props, _runs(text, size, mono))


def _table(rows):
    body = []
    for index, row in enumerate(rows):
        cells = ''.join(
            '<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>%s</w:tc>'
            % _para(cell, size=20) for cell in row)
        bold = '<w:trPr><w:tblHeader/></w:trPr>' if index == 0 else ''
        body.append('<w:tr>%s%s</w:tr>' % (bold, cells))
    borders = ''.join(
        '<w:%s w:val="single" w:sz="4" w:space="0" w:color="BFBFBF"/>' % side
        for side in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'))
    return ('<w:tbl><w:tblPr><w:tblW w:w="5000" w:type="pct"/>'
            '<w:tblBorders>%s</w:tblBorders></w:tblPr>%s</w:tbl>'
            % (borders, ''.join(body)))


def _image_xml(rid, doc_id, width_emu=5200000, height_emu=3500000):
    return ('<w:p><w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0">'
            '<wp:extent cx="%d" cy="%d"/><wp:docPr id="%d" name="Picture %d"/>'
            '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            '<pic:pic><pic:nvPicPr><pic:cNvPr id="%d" name="pic%d"/><pic:cNvPicPr/></pic:nvPicPr>'
            '<pic:blipFill><a:blip r:embed="%s"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
            '</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>'
            % (width_emu, height_emu, doc_id, doc_id, doc_id, doc_id, rid,
               width_emu, height_emu))


def write(blocks, dst, title='', image_dir=None):
    """把 toolbox.blocks() 的结果写成 .docx。"""
    parts = []
    if title:
        parts.append(_para(title, 'Heading1'))
    media = []
    rels = []
    PAGE_BREAK = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
    for kind, value in blocks:
        if kind == 'pagebreak':
            parts.append(PAGE_BREAK)
        elif kind == 'heading':
            level, text = value
            parts.append(_para(text, 'Heading%d' % min(3, max(1, level))))
        elif kind == 'para':
            parts.append(_para(value))
        elif kind == 'list':
            for item in value:
                parts.append(_para('• ' + item, indent=280))
        elif kind == 'ordered':
            for index, item in enumerate(value, 1):
                parts.append(_para('%d. %s' % (index, item), indent=280))
        elif kind == 'todo':
            for done, item in value:
                parts.append(_para(('☑ ' if done else '☐ ') + item, indent=280))
        elif kind == 'quote':
            for line in value.split('\n'):
                parts.append(_para(line, 'Quote'))
        elif kind == 'code':
            lang, body = value if isinstance(value, tuple) else ('', value)
            for line in (body or '').split('\n'):
                parts.append(_para(line or ' ', 'CodeBlock', size=18, mono=True))
        elif kind == 'table':
            parts.append(_table(value))
        elif kind == 'image':
            alt, src = value if isinstance(value, tuple) else ('', value)
            path = src
            if image_dir and not os.path.isabs(src):
                path = os.path.join(image_dir, src)
            if os.path.exists(path):
                ext = os.path.splitext(path)[1].lower().lstrip('.')
                if ext in ('png', 'jpg', 'jpeg'):
                    name = 'image%d.%s' % (len(media) + 1, ext)
                    media.append((name, path))
                    rid = 'rId%d' % (100 + len(media))
                    rels.append('<Relationship Id="%s" Type="http://schemas.openxmlformats.org/'
                                'officeDocument/2006/relationships/image" Target="media/%s"/>'
                                % (rid, name))
                    parts.append(_image_xml(rid, len(media)))
                else:
                    parts.append(_para('[图片未嵌入：%s]' % alt))
            else:
                parts.append(_para('[图片未嵌入：%s]' % alt))

    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<w:document %s><w:body>%s<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134"/>'
                '</w:sectPr></w:body></w:document>' % (NS, ''.join(parts)))
    doc_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
                '2006/relationships/styles" Target="styles.xml"/>%s</Relationships>'
                % ''.join(rels))
    with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('[Content_Types].xml', CONTENT_TYPES)
        zf.writestr('_rels/.rels', RELS.replace('</Relationships>',
            '<Relationship Id="rIdCore" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/></Relationships>'))
        zf.writestr('docProps/core.xml', CORE.format(
            title=(title or '未命名').replace('&', '&amp;').replace('<', '&lt;'), author=AUTHOR))
        zf.writestr('word/document.xml', document)
        zf.writestr('word/styles.xml', STYLES)
        zf.writestr('word/_rels/document.xml.rels', doc_rels)
        for name, path in media:
            with open(path, 'rb') as fh:
                zf.writestr('word/media/' + name, fh.read())
    return os.path.getsize(dst)


def read(path):
    """读 .docx 的正文，返回 [(样式, 文字)] 与表格。"""
    out = []
    with zipfile.ZipFile(path) as zf:
        try:
            xml = zf.read('word/document.xml')
        except KeyError:
            raise ValueError('不是有效的 .docx：缺少 word/document.xml')
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml)
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}

    def style_of(node):
        ppr = node.find('w:pPr/w:pStyle', ns)
        return ppr.get('{%s}val' % ns['w']) if ppr is not None else ''

    body = root.find('w:body', ns)
    for node in list(body) if body is not None else []:
        tag = node.tag.split('}')[-1]
        if tag == 'p':
            text = ''.join(t.text or '' for t in node.iter('{%s}t' % ns['w']))
            if text.strip():
                out.append((style_of(node), text))
        elif tag == 'tbl':
            rows = []
            for tr in node.findall('w:tr', ns):
                cells = []
                for tc in tr.findall('w:tc', ns):
                    cells.append(''.join(t.text or '' for t in tc.iter('{%s}t' % ns['w'])))
                rows.append(cells)
            out.append(('table', rows))
    return out


def to_markdown(path):
    """把 .docx 转成 Markdown 文本。"""
    lines = []
    for style, value in read(path):
        if style == 'table':
            if not value:
                continue
            header = value[0]
            lines.append('| ' + ' | '.join(header) + ' |')
            lines.append('|' + '---|' * len(header))
            for row in value[1:]:
                lines.append('| ' + ' | '.join(row) + ' |')
            lines.append('')
            continue
        if style.startswith('Heading'):
            level = style[-1] if style[-1].isdigit() else '1'
            lines.append('#' * int(level) + ' ' + value)
        elif style == 'Quote':
            lines.append('> ' + value)
        elif style == 'CodeBlock':
            lines.append('    ' + value)
        else:
            stripped = value.strip()
            if stripped.startswith('• '):
                lines.append('- ' + stripped[2:])
            else:
                lines.append(value)
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'
