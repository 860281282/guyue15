"""把本地笔记前言里的属性写回 Notion 页面属性（失败温和降级）。

只处理 Notion 页面确实存在的属性：属性不存在会返回 validation_error，
这里一律吞掉并只记一条提示，绝不让同步失败。
"""

from __future__ import annotations

import re
from pathlib import Path

FRONT = re.compile(r'^\ufeff?---\r?\n(.*?)\r?\n---\r?\n?', re.S)
SKIP = {'notion_id', 'notion_url', 'imported_at', 'notion_last_edited_at'}

TEXT_TYPES = ('rich_text', 'title', 'url', 'email', 'phone_number')
DATE_TYPES = ('date',)


def parse(text):
    match = FRONT.match(text)
    if not match:
        return {}
    data, current = {}, None
    for raw in match.group(1).splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith('- ') and current:
            data.setdefault(current, []).append(line.lstrip()[2:].strip().strip('"\''))
            continue
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        current = key.strip().strip('"\'')
        value = value.strip()
        if not value:
            data.setdefault(current, [])
            continue
        if value.startswith('[') and value.endswith(']'):
            data[current] = [v.strip().strip('"\'') for v in value[1:-1].split(',') if v.strip()]
        else:
            data[current] = [value.strip('"\'')]
    return {k: v for k, v in data.items() if k and k not in SKIP}


def properties(props):
    """本地前言 → Notion update_properties 需要的结构。"""
    out = {}
    for key, values in props.items():
        values = [v for v in (values or []) if v]
        if not values:
            continue
        if key in ('tags', 'tag', '标签'):
            out['tags'] = {'multi_select': [{'name': v[:99]} for v in values[:20]]}
        elif key in ('date', '日期') and re.match(r'^\d{4}-\d{2}-\d{2}', values[0]):
            out['date'] = {'date': {'start': values[0][:10]}}
        else:
            out[key] = {'rich_text': [{'type': 'text', 'text': {'content': '、'.join(values)[:1900]}}]}
    return out


def push(client, page_id, note_path, log=None):
    say = log or (lambda text: None)
    try:
        text = Path(note_path).read_text(encoding='utf-8-sig')
    except (OSError, UnicodeDecodeError):
        return False
    props = properties(parse(text))
    if not props:
        return False
    try:
        client.write('update-page', page_id=page_id, command='update_properties',
                     properties=props, allow_async=False)
        return True
    except Exception as exc:  # noqa: BLE001  属性缺失是常态，不能影响正文同步
        if getattr(exc, 'code', None) == 'ambiguous_write':
            raise
        say('属性未写入（%s 没有对应属性）：%s' % (Path(note_path).name, exc))
        return False
