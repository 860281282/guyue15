"""自己拼 PDF：字体子集、文本排版、表格、图片。

只用标准库。相比 reportlab，省掉 5 MB 左右的体积，代价是只支持本程序用到的版面。
"""

import re
import struct
import zlib

PAGE_W, PAGE_H = 595.28, 841.89  # A4
MARGIN_X = 56.7
MARGIN_TOP = 51.0
MARGIN_BOTTOM = 51.0
CONTENT_W = PAGE_W - MARGIN_X * 2


def _fmt(value):
    if abs(value - round(value)) < 0.001:
        return '%d' % round(value)
    return ('%.3f' % value).rstrip('0').rstrip('.')


class Font:
    """TrueType（含 .ttc 里的某一个）。只读需要的几张表。"""

    KEEP = ('head', 'hhea', 'maxp', 'hmtx', 'loca', 'glyf', 'cvt ', 'fpgm', 'prep')

    def __init__(self, path, index=0):
        with open(path, 'rb') as fh:
            self.raw = fh.read()
        data = self.raw
        base = 0
        if data[:4] == b'ttcf':
            base = struct.unpack('>I', data[12 + 4 * index:16 + 4 * index])[0]
        if data[base:base + 4] not in (b'\x00\x01\x00\x00', b'true'):
            raise ValueError('不是 TrueType 字体：%s' % path)
        count = struct.unpack('>H', data[base + 4:base + 6])[0]
        self.tables = {}
        for i in range(count):
            rec = base + 12 + 16 * i
            tag = data[rec:rec + 4]
            off, length = struct.unpack('>II', data[rec + 8:rec + 16])
            self.tables[tag] = (off, length)
        for need in (b'head', b'hhea', b'hmtx', b'maxp', b'loca', b'glyf', b'cmap'):
            if need not in self.tables:
                raise ValueError('字体缺 %s' % need.decode('latin-1'))
        head = self.tables[b'head'][0]
        self.units = struct.unpack('>H', data[head + 18:head + 20])[0] or 1000
        self.bbox = struct.unpack('>hhh h', data[head + 36:head + 44])
        hhea = self.tables[b'hhea'][0]
        self.ascent, self.descent = struct.unpack('>hh', data[hhea + 4:hhea + 8])
        self.num_hmetrics = struct.unpack('>H', data[hhea + 34:hhea + 36])[0]
        self.num_glyphs = struct.unpack('>H', data[self.tables[b'maxp'][0] + 4:
                                                  self.tables[b'maxp'][0] + 6])[0]
        self.long_loca = struct.unpack('>h', data[head + 50:head + 52])[0] == 1
        self._cmap = {}
        self._widths = {}
        self._load_cmap()

    def _load_cmap(self):
        data = self.raw
        off = self.tables[b'cmap'][0]
        n = struct.unpack('>H', data[off + 2:off + 4])[0]
        best = None
        for i in range(n):
            plat, enc, sub = struct.unpack('>HHI', data[off + 4 + 8 * i:off + 12 + 8 * i])
            sub_off = off + sub
            fmt = struct.unpack('>H', data[sub_off:sub_off + 2])[0]
            if fmt == 12 and (plat, enc) in ((3, 10), (0, 4)):
                best = ('12', sub_off)
                break
            if fmt == 4 and not best:
                best = ('4', sub_off)
        if not best:
            return
        kind, so = best
        if kind == '12':
            groups = struct.unpack('>I', data[so + 12:so + 16])[0]
            pos = so + 16
            for _ in range(groups):
                start, end, gid = struct.unpack('>III', data[pos:pos + 12])
                pos += 12
                if end - start > 0x20000:
                    continue
                for cp in range(start, end + 1):
                    self._cmap[cp] = gid + (cp - start)
        else:
            segs = struct.unpack('>H', data[so + 6:so + 8])[0] // 2
            ends = so + 14
            starts = ends + segs * 2 + 2
            deltas = starts + segs * 2
            ranges = deltas + segs * 2
            for i in range(segs):
                end = struct.unpack('>H', data[ends + 2 * i:ends + 2 + 2 * i])[0]
                start = struct.unpack('>H', data[starts + 2 * i:starts + 2 + 2 * i])[0]
                delta = struct.unpack('>h', data[deltas + 2 * i:deltas + 2 + 2 * i])[0]
                roff = struct.unpack('>H', data[ranges + 2 * i:ranges + 2 + 2 * i])[0]
                if start == 0xFFFF:
                    continue
                for cp in range(start, min(end, 0xFFFE) + 1):
                    if roff == 0:
                        gid = (cp + delta) & 0xFFFF
                    else:
                        pos = ranges + 2 * i + roff + 2 * (cp - start)
                        if pos + 2 > len(data):
                            continue
                        gid = struct.unpack('>H', data[pos:pos + 2])[0]
                        if gid:
                            gid = (gid + delta) & 0xFFFF
                    if gid:
                        self._cmap[cp] = gid

    # ---- 度量 ----
    def gid(self, ch):
        return self._cmap.get(ord(ch), 0)

    def advance(self, gid):
        given = self._widths.get(gid)
        if given is None:
            off = self.tables[b'hmtx'][0]
            if gid >= self.num_hmetrics:
                gid_w = self.num_hmetrics - 1
            else:
                gid_w = gid
            given = struct.unpack('>H', self.raw[off + 4 * gid_w:off + 4 * gid_w + 2])[0]
            self._widths[gid] = given
        return given

    def width(self, text, size):
        return sum(self.advance(self.gid(ch)) for ch in text) * size / self.units

    # ---- 子集 ----
    def _glyph_offsets(self):
        off, length = self.tables[b'loca']
        data = self.raw
        if self.long_loca:
            count = length // 4
            return list(struct.unpack('>%dI' % count, data[off:off + 4 * count]))
        count = length // 2
        return [v * 2 for v in struct.unpack('>%dH' % count, data[off:off + 2 * count])]

    def _components(self, start, end):
        """复合字形引用的其它字形编号。"""
        data = self.raw
        if end - start < 10:
            return []
        contours = struct.unpack('>h', data[start:start + 2])[0]
        if contours >= 0:
            return []
        pos = start + 10
        out = []
        while pos + 4 <= end:
            flags, gid = struct.unpack('>HH', data[pos:pos + 4])
            out.append(gid)
            pos += 4
            if flags & 0x0001:  # ARG_1_AND_2_ARE_WORDS
                pos += 4
            else:
                pos += 2
            if flags & 0x0008:      # WE_HAVE_A_SCALE
                pos += 2
            elif flags & 0x0040:    # WE_HAVE_AN_X_AND_Y_SCALE
                pos += 4
            elif flags & 0x0080:    # WE_HAVE_A_TWO_BY_TWO
                pos += 8
            if not flags & 0x0020:  # MORE_COMPONENTS
                break
        return out

    def _component_ids(self, start, end):
        """复合字形里引用的字形编号，以及它们在数据里的位置（用来改写）。"""
        data = self.raw
        if end - start < 10:
            return []
        contours = struct.unpack('>h', data[start:start + 2])[0]
        if contours >= 0:
            return []
        pos = start + 10
        out = []
        while pos + 4 <= end:
            flags = struct.unpack('>H', data[pos:pos + 2])[0]
            out.append(pos + 2)
            pos += 4
            pos += 4 if flags & 0x0001 else 2
            if flags & 0x0008:
                pos += 2
            elif flags & 0x0040:
                pos += 4
            elif flags & 0x0080:
                pos += 8
            if not flags & 0x0020:
                break
        return out

    def prepare(self, codes):
        """统计要用的字形（含复合字形依赖），建立旧→新编号映射。"""
        offsets = self._glyph_offsets()
        glyf_off = self.tables[b'glyf'][0]
        used = {0}
        queue = [self.gid(chr(c)) for c in codes]
        while queue:
            gid = queue.pop()
            if gid in used or gid >= len(offsets) - 1:
                continue
            used.add(gid)
            for pos in self._component_ids(glyf_off + offsets[gid],
                                           glyf_off + offsets[gid + 1]):
                queue.append(struct.unpack('>H', self.raw[pos:pos + 2])[0])
        order = sorted(used)
        self.order = order
        self.remap = {old: new for new, old in enumerate(order)}
        return self.remap

    def cid(self, ch):
        return self.remap.get(self.gid(ch), 0) if getattr(self, 'remap', None) else self.gid(ch)

    def hex_cids(self, text):
        return ''.join('%04X' % self.cid(ch) for ch in text)

    def cid_width(self, cid):
        order = getattr(self, 'order', None)
        if not order or cid >= len(order):
            return 1000
        return self.advance(order[cid]) * 1000.0 / self.units

    def subset_bytes(self):
        """按 prepare() 的映射生成新 TTF 字节串。"""
        offsets = self._glyph_offsets()
        glyf_off = self.tables[b'glyf'][0]
        remap = self.remap
        order = self.order

        glyf = bytearray()
        loca = []
        for new, old in enumerate(order):
            loca.append(len(glyf))
            chunk = bytearray(self.raw[glyf_off + offsets[old]:glyf_off + offsets[old + 1]])
            for pos in self._component_ids(glyf_off + offsets[old],
                                           glyf_off + offsets[old + 1]):
                rel = pos - (glyf_off + offsets[old])
                comp_old = struct.unpack('>H', chunk[rel:rel + 2])[0]
                struct.pack_into('>H', chunk, rel, remap.get(comp_old, 0))
            glyf += chunk
            while len(glyf) % 4:
                glyf.append(0)
        loca.append(len(glyf))

        hmtx = bytearray()
        for old in order:
            hmtx += struct.pack('>Hh', self.advance(old) & 0xFFFF, 0)

        tables = {}
        for tag in self.KEEP:
            if tag in (b'head', b'loca', b'glyf', b'maxp', b'hmtx'):
                continue
            if tag in self.tables:
                off, length = self.tables[tag]
                tables[tag] = bytearray(self.raw[off:off + length])
        head_off = self.tables[b'head'][0]
        head = bytearray(self.raw[head_off:head_off + 54])
        struct.pack_into('>I', head, 8, 0)
        struct.pack_into('>h', head, 50, 1)
        tables[b'head'] = head
        maxp_off = self.tables[b'maxp'][0]
        maxp = bytearray(self.raw[maxp_off:maxp_off + 32])
        struct.pack_into('>H', maxp, 4, len(order))
        tables[b'maxp'] = maxp
        hhea_off = self.tables[b'hhea'][0]
        hhea = bytearray(self.raw[hhea_off:hhea_off + 36])
        struct.pack_into('>H', hhea, 34, len(order))
        tables[b'hhea'] = hhea
        tables[b'hmtx'] = hmtx
        tables[b'loca'] = bytearray(b''.join(struct.pack('>I', v) for v in loca))
        tables[b'glyf'] = glyf

        order_tags = sorted(tables)
        count = len(order_tags)
        out = bytearray(b'\x00\x01\x00\x00')
        out += struct.pack('>HHHH', count, 0, 0, 0)
        offset = 12 + 16 * count
        body = bytearray()
        records = {}
        for tag in order_tags:
            data = bytes(tables[tag])
            pad = (4 - len(data) % 4) % 4
            padded = data + b'\x00' * pad
            checksum = sum(struct.unpack('>%dI' % (len(padded) // 4), padded)) & 0xFFFFFFFF
            records[tag] = (checksum, offset, len(data))
            body += padded
            offset += len(padded)
        for tag in order_tags:
            checksum, off, length = records[tag]
            out += tag + struct.pack('>III', checksum, off, length)
        return bytes(out + body)

    def as_pdf(self, name):
        """生成 PDF 里需要的几个对象（由 Doc 负责编号）。"""
        return name, self


class Doc:
    """一页一页往下排的版面。坐标原点在左下角。"""

    def __init__(self, fonts):
        self.fonts = fonts              # {'body': Font, 'bold': Font, 'mono': Font}
        self.used = {k: set() for k in fonts}
        self.pages = []
        self.images = []                # [(name, dict_bytes, stream)]
        self.ops = []
        self.y = PAGE_H - MARGIN_TOP
        self._new_page()

    # ---- 基础 ----
    def _new_page(self):
        if self.ops:
            self.pages.append(self.ops)
        self.ops = []
        self.y = PAGE_H - MARGIN_TOP

    def space(self, height):
        if self.y - height < MARGIN_BOTTOM:
            self._new_page()

    def text(self, x, y, role, size, txt, color=(0, 0, 0)):
        if not txt:
            return
        self.used[role].update(ord(ch) for ch in txt)
        self.ops.append(('T', role, size, x, y, txt, color))
        self.y = min(self.y, y)

    def rect(self, x, y, w, h, fill=None, stroke=None, width=0.5):
        if fill:
            self.ops.append('%s %s %s rg %s %s %s %s re f' % (
                _fmt(fill[0]), _fmt(fill[1]), _fmt(fill[2]),
                _fmt(x), _fmt(y), _fmt(w), _fmt(h)))
        if stroke:
            self.ops.append('%s %s %s RG %s w %s %s %s %s re S' % (
                _fmt(stroke[0]), _fmt(stroke[1]), _fmt(stroke[2]),
                _fmt(width), _fmt(x), _fmt(y), _fmt(w), _fmt(h)))

    def line(self, x1, y1, x2, y2, color=(0.8, 0.8, 0.8), width=0.5):
        self.ops.append('%s %s %s RG %s w %s %s m %s %s l S' % (
            _fmt(color[0]), _fmt(color[1]), _fmt(color[2]),
            _fmt(width), _fmt(x1), _fmt(y1), _fmt(x2), _fmt(y2)))

    def image(self, path, width_mm=150.0):
        try:
            info = read_image(path)
        except Exception:
            return False
        max_w = min(CONTENT_W, width_mm * 72 / 25.4)
        w = min(max_w, info['width'] * 72 / 96.0)
        h = w * info['height'] / float(info['width'])
        self.space(h + 8)
        name = 'Im%d' % (len(self.images) + 1)
        self.images.append((name, info['dict'], info['data']))
        x = MARGIN_X
        y = self.y - h
        self.ops.append('q %s 0 0 %s %s %s cm /%s Do Q' % (
            _fmt(w), _fmt(h), _fmt(x), _fmt(y), name))
        self.y = y - 8
        return True

    # ---- 排版 ----
    def wrap(self, txt, role, size, width=None):
        width = width or CONTENT_W
        font = self.fonts[role]
        lines, current, used = [], '', 0.0
        for ch in txt:
            adv = font.advance(font.gid(ch)) * size / font.units
            if ch == '\n':
                lines.append(current)
                current, used = '', 0.0
                continue
            if used + adv > width and current:
                cut = current.rfind(' ')
                if cut > 0 and ord(ch) < 0x2E80:
                    lines.append(current[:cut])
                    current = current[cut + 1:] + ch
                    used = font.width(current, size)
                else:
                    lines.append(current)
                    current, used = ch, adv
                continue
            current += ch
            used += adv
        if current:
            lines.append(current)
        return lines or ['']

    def paragraph(self, txt, role='body', size=10.5, leading=17, color=(0, 0, 0),
                  indent=0.0, width=None, space_after=6, rich=True):
        width = width or (CONTENT_W - indent)
        if rich and role != 'mono':
            lines = wrap_runs(inline_runs(txt), self.fonts, size, width)
            for line in lines:
                self.space(leading)
                self.y -= leading
                draw_runs(self, line, MARGIN_X + indent, self.y + leading * 0.28,
                          size, color)
        else:
            for line in self.wrap(txt, role, size, width):
                self.space(leading)
                self.y -= leading
                self.text(MARGIN_X + indent, self.y + leading * 0.28, role, size,
                          line, color)
        self.y -= space_after

    def bullet(self, txt, marker='·', role='body', size=10.5, leading=17):
        indent = 16.0
        lines = wrap_runs(inline_runs(txt), self.fonts, size, CONTENT_W - indent)
        for i, line in enumerate(lines):
            self.space(leading)
            self.y -= leading
            if i == 0:
                self.text(MARGIN_X + 4, self.y + leading * 0.28, role, size, marker)
            draw_runs(self, line, MARGIN_X + indent, self.y + leading * 0.28, size,
                      (0.06, 0.06, 0.06))
        self.y -= 2

    def code_block(self, txt):
        size, leading = 8.8, 12.4
        for line in txt.split('\n'):
            for piece in self.wrap(line, 'mono', size, CONTENT_W - 16) or ['']:
                self.space(leading + 6)
                self.y -= leading
                self.rect(MARGIN_X, self.y - 2.4, CONTENT_W, leading,
                          fill=(0.96, 0.96, 0.96))
                self.text(MARGIN_X + 6, self.y + 2.0, 'mono', size, piece,
                          color=(0.15, 0.15, 0.15))
        self.y -= 6

    def table(self, rows):
        rows = [[_plain_cell(cell) for cell in row] for row in rows]
        if not rows:
            return
        cols = max(len(r) for r in rows)
        cell_w = CONTENT_W / cols
        size, leading = 9.2, 13.0
        for index, row in enumerate(rows):
            cells = list(row) + [''] * (cols - len(row))
            wrapped = [self.wrap(c, 'body', size, cell_w - 8) for c in cells]
            height = max(len(w) for w in wrapped) * leading + 6
            self.space(height)
            top = self.y
            if index == 0:
                self.rect(MARGIN_X, top - height, CONTENT_W, height,
                          fill=(0.94, 0.94, 0.94))
            for c in range(cols + 1):
                self.line(MARGIN_X + c * cell_w, top, MARGIN_X + c * cell_w,
                          top - height, color=(0.78, 0.78, 0.78))
            self.line(MARGIN_X, top, MARGIN_X + CONTENT_W, top, color=(0.78, 0.78, 0.78))
            for col, lines in enumerate(wrapped):
                role = 'bold' if index == 0 else 'body'
                for line_no, line in enumerate(lines):
                    self.text(MARGIN_X + col * cell_w + 4,
                              top - 6 - leading * line_no - size * 0.78,
                              role, size, line)
            self.line(MARGIN_X, top - height, MARGIN_X + CONTENT_W, top - height,
                      color=(0.78, 0.78, 0.78))
            self.y = top - height
        self.y -= 8

    # ---- 输出 ----

    def save(self, path, title=''):
        self._new_page()
        pages = self.pages
        objects = []      # 每个元素是 bytes（对象体），索引 +1 即对象号

        def add(body):
            objects.append(body)
            return len(objects)

        for role, font in self.fonts.items():
            if self.used[role] or role == 'body':
                font.prepare(self.used[role] or {32})

        font_ids = {}
        for role, font in self.fonts.items():
            if not self.used[role] and role != 'body':
                continue
            subset = font.subset_bytes()
            base = 'F%s' % role[:1].upper() + str(len(font_ids) + 1)
            file_id = add(b'<< /Length %d /Length1 %d >>\nstream\n%s\nendstream' % (
                len(subset), len(subset), subset))
            bbox = ' '.join(_fmt(v * 1000.0 / font.units) for v in font.bbox)
            desc_id = add((
                '<< /Type /FontDescriptor /FontName /%s /Flags 4 /FontBBox [%s] '
                '/ItalicAngle 0 /Ascent %s /Descent %s /CapHeight %s /StemV 80 '
                '/FontFile2 %d 0 R >>' % (
                    base, bbox,
                    _fmt(font.ascent * 1000.0 / font.units),
                    _fmt(font.descent * 1000.0 / font.units),
                    _fmt(font.ascent * 1000.0 / font.units), file_id)).encode('ascii'))
            pairs = ['%d [%s]' % (cid, _fmt(font.cid_width(cid)))
                     for cid in range(len(font.order))]
            cid_id = add((
                '<< /Type /Font /Subtype /CIDFontType2 /BaseFont /%s '
                '/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> '
                '/FontDescriptor %d 0 R /DW 1000 /W [%s] /CIDToGIDMap /Identity >>' % (
                    base, desc_id, ' '.join(pairs))).encode('ascii'))
            cmap_lines = ['/CIDInit /ProcSet findresource begin', '12 dict begin',
                          'begincmap', '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) '
                          '/Supplement 0 >> def', '/CMapName /Adobe-Identity-UCS def',
                          '/CMapType 2 def', '1 begincodespacerange',
                          '<0000> <FFFF>', 'endcodespacerange']
            items = []
            for cp in sorted(self.used[role] or {32}):
                cid = font.cid(chr(cp))
                if cid:
                    items.append(('%04X' % cid, chr(cp)))
            for start in range(0, len(items), 100):
                chunk = items[start:start + 100]
                cmap_lines.append('%d beginbfchar' % len(chunk))
                for gid_hex, ch in chunk:
                    target = ''.join('%04X' % unit for unit in _utf16(ch))
                    cmap_lines.append('<%s> <%s>' % (gid_hex, target))
                cmap_lines.append('endbfchar')
            cmap_lines += ['endcmap', 'CMapName currentdict /CMap defineresource pop',
                           'end', 'end']
            cmap_id = add(b'<< /Length %d >>\nstream\n%s\nendstream' % (
                len('\n'.join(cmap_lines)), '\n'.join(cmap_lines).encode('ascii')))
            to_uni = add((
                '<< /Type /Font /Subtype /Type0 /BaseFont /%s /Encoding /Identity-H '
                '/DescendantFonts [%d 0 R] /ToUnicode %d 0 R >>' % (
                    base, cid_id, cmap_id)).encode('ascii'))
            font_ids[role] = (base, to_uni)

        image_ids = {}
        for name, dict_bytes, stream in self.images:
            body = dict_bytes.replace(b'__LENGTH__', str(len(stream)).encode('ascii'))
            image_ids[name] = add(b'<< %s /Length %d >>\nstream\n%s\nendstream' % (
                body, len(stream), stream))

        pages_id = len(objects) + 1 + 2 * len(pages)
        page_ids = []
        for ops in pages:
            lines = []
            for op in ops:
                if isinstance(op, str):
                    lines.append(op)
                    continue
                _, role, size, x, y, txt, color = op
                font = self.fonts[role]
                lines.append('%s %s %s rg BT /%s %s Tf 1 0 0 1 %s %s Tm <%s> Tj ET' % (
                    _fmt(color[0]), _fmt(color[1]), _fmt(color[2]), role, _fmt(size),
                    _fmt(x), _fmt(y), font.hex_cids(txt)))
            content = '\n'.join(lines).encode('latin-1', 'replace')
            content_id = add(b'<< /Length %d >>\nstream\n%s\nendstream' % (
                len(content), content))
            res_fonts = ' '.join('/%s %d 0 R' % (role, fid) for role, (_, fid) in font_ids.items())
            res_images = ' '.join('/%s %d 0 R' % (name, iid) for name, iid in image_ids.items())
            resources = '<< /Font << %s >>' % res_fonts
            if res_images:
                resources += ' /XObject << %s >>' % res_images
            resources += ' >>'
            page_ids.append(add((
                '<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %s %s] /Resources %s '
                '/Contents %d 0 R >>' % (
                    pages_id, _fmt(PAGE_W), _fmt(PAGE_H), resources, content_id)
            ).encode('ascii')))

        kids = ' '.join('%d 0 R' % page_id for page_id in page_ids)
        add(('<< /Type /Pages /Count %d /Kids [%s] >>' % (len(pages), kids)).encode('ascii'))
        catalog_id = add(('<< /Type /Catalog /Pages %d 0 R >>' % pages_id).encode('ascii'))

        out = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
        offsets = []
        for index, body in enumerate(objects):
            offsets.append(len(out))
            out += b'%d 0 obj\n' % (index + 1) + body + b'\nendobj\n'
        xref = len(out)
        out += b'xref\n0 %d\n' % (len(objects) + 1)
        out += b'0000000000 65535 f \n'
        for off in offsets:
            out += ('%010d 00000 n \n' % off).encode('ascii')
        trailer = ('trailer\n<< /Size %d /Root %d 0 R' % (len(objects) + 1, catalog_id))
        trailer += ' /Info << /Title <%s> /Author <%s> /Creator <%s> /Producer <%s> >>' % (
            _hex_text(title or '未命名'), _hex_text(AUTHOR), _hex_text(CREATOR), _hex_text(PRODUCER))
        out += trailer.encode('ascii')
        out += b' >>\nstartxref\n%d\n%%%%EOF\n' % xref
        with open(path, 'wb') as fh:
            fh.write(bytes(out))
        return len(out)


AUTHOR = '咕月15'
CREATOR = 'Notion 与 Obsidian 一键同步'
PRODUCER = 'Notion 与 Obsidian 一键同步（咕月15）'


def _hex_text(text: str) -> str:
    """PDF 文本串：UTF-16BE + BOM，输出大写十六进制。"""
    data = b'\xfe\xff' + str(text).encode('utf-16-be')
    return data.hex().upper()


def _utf16(text):
    data = text.encode('utf-16-be')
    return [int.from_bytes(data[i:i + 2], 'big') for i in range(0, len(data), 2)]


def read_image(path):
    """读 JPEG / PNG，给出 PDF 图片对象需要的字典与数据。"""
    with open(path, 'rb') as fh:
        raw = fh.read()
    if raw[:2] == b'\xff\xd8':
        w = _jpeg_size(raw)
        h = _jpeg_size(raw, height=True)
        return {'width': w, 'height': h,
                'dict': (b'/Type /XObject /Subtype /Image /Width %d /Height %d '
                         b'/ColorSpace /DeviceRGB /BitsPerComponent 8 '
                         b'/Filter /DCTDecode' % (w, h)),
                'data': raw}
    if raw[:8] == b'\x89PNG\r\n\x1a\n':
        return _png(raw)
    raise ValueError('只支持 JPEG / PNG')


def _jpeg_size(raw, height=False):
    pos = 2
    while pos + 9 < len(raw):
        if raw[pos] != 0xFF:
            pos += 1
            continue
        marker = raw[pos + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            h, w = struct.unpack('>HH', raw[pos + 5:pos + 9])
            return h if height else w
        length = struct.unpack('>H', raw[pos + 2:pos + 4])[0]
        pos += 2 + length
    raise ValueError('JPEG 头解析失败')


def _png(raw):
    pos = 8
    width = height = depth = color = 0
    palette = b''
    idat = bytearray()
    while pos + 8 <= len(raw):
        length = struct.unpack('>I', raw[pos:pos + 4])[0]
        kind = raw[pos + 4:pos + 8]
        data = raw[pos + 8:pos + 8 + length]
        if kind == b'IHDR':
            width, height, depth, color, _, _, interlace = struct.unpack('>IIBBBBB', data)
            if interlace:
                raise ValueError('不支持隔行 PNG')
        elif kind == b'PLTE':
            palette = data
        elif kind == b'IDAT':
            idat += data
        elif kind == b'IEND':
            break
        pos += 12 + length
    if depth not in (8, 16):
        raise ValueError('不支持 %d 位 PNG' % depth)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color)
    if channels is None:
        raise ValueError('不支持的 PNG 颜色类型 %d' % color)
    step = 2 if depth == 16 else 1
    stride = width * channels * step
    pixels = bytearray(zlib.decompress(bytes(idat)))
    out = bytearray(width * height * 3)
    prev = bytearray(stride)
    for row in range(height):
        start = row * (stride + 1)
        filter_type = pixels[start]
        line = bytearray(pixels[start + 1:start + 1 + stride])
        if filter_type == 1:
            for i in range(step, stride):
                line[i] = (line[i] + line[i - step]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                a = line[i - step] if i >= step else 0
                b = prev[i]
                c = prev[i - step] if i >= step else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        prev = line
        base = row * width * 3
        for x in range(width):
            i = x * channels * step
            if color == 0 or color == 4:
                g = line[i]
                r = gv = b = g
            elif color == 3:
                idx = line[i]
                r, gv, b = palette[idx * 3:idx * 3 + 3]
            else:
                r, gv, b = line[i], line[i + step], line[i + 2 * step]
            if color in (4, 6):
                alpha = line[i + (channels - 1) * step]
                if alpha != 255:
                    r = (r * alpha + 255 * (255 - alpha)) // 255
                    gv = (gv * alpha + 255 * (255 - alpha)) // 255
                    b = (b * alpha + 255 * (255 - alpha)) // 255
            j = base + x * 3
            out[j] = r
            out[j + 1] = gv
            out[j + 2] = b
    packed = zlib.compress(bytes(out), 6)
    return {'width': width, 'height': height,
            'dict': b'/Type /XObject /Subtype /Image /Width %d /Height %d '
                    b'/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode'
                    % (width, height),
            'data': packed}


BODY_FONT = r'C:\Windows\Fonts\msyh.ttc'
BOLD_FONT = r'C:\Windows\Fonts\msyhbd.ttc'
MONO_FONT = r'C:\Windows\Fonts\consola.ttf'
FALLBACK = r'C:\Windows\Fonts\simsun.ttc'


def load_fonts():
    body = Font(BODY_FONT) if _exists(BODY_FONT) else Font(FALLBACK)
    try:
        bold = Font(BOLD_FONT)
    except Exception:
        bold = body
    try:
        mono = Font(MONO_FONT)
    except Exception:
        mono = body
    return {'body': body, 'bold': bold, 'mono': mono}


def _exists(path):
    import os
    return os.path.exists(path)


GRAY = (0.42, 0.42, 0.42)
INK = (0.06, 0.06, 0.06)
CODE_TXT = (0.15, 0.15, 0.15)
RULE = (0.80, 0.80, 0.80)


_CELL_MARKS = re.compile(r'(\*\*|__|`)')


def _plain_cell(text):
    """表格单元格只保留文字，去掉行内标记符号。"""
    return _CELL_MARKS.sub('', str(text)).strip()


def render(blocks, dst, title='', image_dir=None, log=None):
    """把 toolbox.blocks() 的结果排成 PDF，返回字节数。"""
    import os
    fonts = load_fonts()
    doc = Doc(fonts)
    if title:
        doc.paragraph(title, 'bold', 21, 30, INK, space_after=4)
        doc.line(MARGIN_X, doc.y + 6, MARGIN_X + CONTENT_W, doc.y + 6, RULE, 0.8)
        doc.y -= 10
    for kind, value in blocks:
        if kind == 'heading':
            level, text = value
            size = {1: 18.0, 2: 14.5, 3: 12.5}.get(level, 12.0)
            lead = size * 1.55
            doc.space(lead + 10)
            doc.y -= 8
            doc.paragraph(text, 'bold', size, lead, INK, space_after=4)
        elif kind == 'para':
            stripped = value.strip()
            if stripped and set(stripped) <= set('-*_ ') and len(stripped) >= 3:
                doc.space(14)
                doc.y -= 6
                doc.line(MARGIN_X, doc.y, MARGIN_X + CONTENT_W, doc.y, RULE, 0.6)
                doc.y -= 8
            else:
                doc.paragraph(value, 'body', 10.5, 17, INK)
        elif kind == 'list':
            for item in value:
                doc.bullet(item, '·')
            doc.y -= 2
        elif kind == 'ordered':
            for index, item in enumerate(value, 1):
                doc.bullet(item, '%d.' % index)
            doc.y -= 2
        elif kind == 'todo':
            for done, item in value:
                doc.bullet(item, '☑' if done else '☐')
            doc.y -= 2
        elif kind == 'quote':
            lines = doc.wrap(value, 'body', 10.5, CONTENT_W - 14)
            height = len(lines) * 17
            doc.space(height + 4)
            top = doc.y
            doc.rect(MARGIN_X, top - height, 2.2, height, fill=(0.80, 0.80, 0.80))
            for line in lines:
                doc.y -= 17
                doc.text(MARGIN_X + 12, doc.y + 5, 'body', 10.5, line, GRAY)
            doc.y -= 6
        elif kind == 'code':
            doc.code_block(value[1] if isinstance(value, tuple) else value)
        elif kind == 'table':
            doc.table(value)
        elif kind == 'image':
            alt, target = value if isinstance(value, tuple) else ('', value)
            if image_dir and not os.path.isabs(target):
                target = os.path.join(image_dir, target)
            if not doc.image(target):
                doc.paragraph('[图片未能嵌入：%s]' % alt, 'body', 9.5, 15, GRAY)
        elif kind == 'rule':
            doc.space(12)
            doc.line(MARGIN_X, doc.y, MARGIN_X + CONTENT_W, doc.y, RULE, 0.6)
            doc.y -= 10
    return doc.save(dst, title)


INLINE_RE = None


def inline_runs(text):
    """把 **粗体**、`代码`、[文字](链接)、[[双链|别名]] 拆成带字体的片段。"""
    global INLINE_RE
    if INLINE_RE is None:
        INLINE_RE = __import__('re').compile(
            r'(\*\*[^*\n]+\*\*|`[^`\n]+`|\[\[[^\]\n]+\]\]|\[[^\]\n]+\]\([^)\s]+\))')
    out = []
    for piece in INLINE_RE.split(text):
        if not piece:
            continue
        if piece.startswith('**') and piece.endswith('**') and len(piece) > 4:
            out.append((piece[2:-2], 'bold'))
        elif piece.startswith('`') and piece.endswith('`') and len(piece) > 2:
            body = piece[1:-1]
            buf, buf_role = '', None
            for ch in body:
                role = 'mono' if ord(ch) < 0x2E80 else 'body'
                if buf_role and role != buf_role:
                    out.append((buf, buf_role))
                    buf = ''
                buf_role = role
                buf += ch
            if buf:
                out.append((buf, buf_role))
        elif piece.startswith('[[') and piece.endswith(']]'):
            label = piece[2:-2]
            if '|' in label:
                label = label.split('|')[-1]
            label = label.split('#')[0]
            out.append((label, 'body'))
        elif piece.startswith('[') and '](' in piece and piece.endswith(')'):
            out.append((piece[1:piece.index(']')], 'body'))
        else:
            out.append((piece, 'body'))
    return out


def wrap_runs(runs, fonts, size, width):
    """按宽度折行，返回 [[(文字, 字体角色), ...], ...]。"""
    lines, current, used = [], [], 0.0
    for text, role in runs:
        font = fonts[role]
        buf = ''
        for ch in text:
            adv = font.advance(font.gid(ch)) * size / font.units
            if ch == '\n':
                if buf:
                    current.append((buf, role))
                    buf = ''
                lines.append(current)
                current, used = [], 0.0
                continue
            if used + adv > width and (buf or current):
                if buf:
                    current.append((buf, role))
                cut = _split_space(current)
                if cut is not None:
                    head, tail = cut
                    lines.append(head)
                    current = tail
                    used = _runs_width(current, fonts, size)
                else:
                    lines.append(current)
                    current = []
                    used = 0.0
                buf = ch
                used += adv
                continue
            buf += ch
            used += adv
        if buf:
            current.append((buf, role))
    if current:
        lines.append(current)
    return lines or [[('', 'body')]]


def _runs_width(runs, fonts, size):
    total = 0.0
    for text, role in runs:
        total += fonts[role].width(text, size)
    return total


def _split_space(runs):
    """尽量在最后一个空格处断开，中文没有空格就直接整行断。"""
    if not runs:
        return None
    text, role = runs[-1]
    cut = text.rfind(' ')
    if cut <= 0:
        return None
    head = runs[:-1] + [(text[:cut], role)]
    tail = [(text[cut + 1:], role)] if text[cut + 1:] else []
    return head, tail


def draw_runs(doc, runs, x, y, size, color, log=None):
    for text, role in runs:
        if not text:
            continue
        eff = size * (0.94 if role == 'mono' else 1.0)
        doc.text(x, y, role, eff, text, color)
        x += doc.fonts[role].width(text, eff)
    return x
