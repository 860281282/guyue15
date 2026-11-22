"""把「本地文件」「云端页面」「状态」拼成统一的节点表。

这一层只做数据整理：读盘、剥 frontmatter、算 hash、对齐身份。不联网、
不做决定，决定在 `planner.py`，执行在各自的引擎里。

为什么 hash 只算正文
--------------------
frontmatter 里有 `notion_last_edited_at`、`imported_at` 这类记账字段，每次
同步都会被改写。如果连它一起 hash，每篇笔记每轮都会"看起来变过"，
冲突判断就全乱了。所以 hash 只覆盖 `---` 之后的正文。
"""

from __future__ import annotations

import os
import re

import core

SKIP_DIRS = {".obsidian", ".同步工具", ".git", ".trash", "缓存", "日志", "归档"}
ROOT_FOLDERS = ("知识库", "日常生活", "日程提醒")

FRONT_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.S)
NID_RE = re.compile(r"^\"?notion_id\"?\s*:\s*\"?([0-9a-fA-F]{32})\"?\s*$", re.M)
TITLE_RE = re.compile(r"^title\s*:\s*\"?([^\"\n]+)\"?\s*$", re.M)


def split_frontmatter(text):
    """返回 (front, body)。没有 frontmatter 时 front 为空串。"""
    text = (text or "").replace("\r\n", "\n")
    match = FRONT_RE.match(text)
    if not match:
        return "", text
    return match.group(1), text[match.end():]


def note_id(front):
    match = NID_RE.search(front or "")
    return match.group(1).lower() if match else None


def note_title(front, fallback=""):
    match = TITLE_RE.search(front or "")
    return (match.group(1).strip() if match else "") or fallback


def read_note(path, rel=""):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    front, body = split_frontmatter(text)
    name = os.path.splitext(os.path.basename(path))[0]
    return {
        "rel": rel or os.path.basename(path),
        "nid": note_id(front),
        "title": note_title(front, name),
        "body": body,
        "hash": core.digest(body),
    }


def local_notes(vault, folders=None):
    """扫本地库，返回 {rel: note}。`rel` 用 / 分隔，与 Notion 路径一致。"""
    vault = os.path.abspath(vault)
    roots = list(folders) if folders else list(ROOT_FOLDERS)
    out = {}
    for root in roots:
        base = os.path.join(vault, root)
        if not os.path.isdir(base):
            continue
        for folder, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                if not name.lower().endswith(".md"):
                    continue
                path = os.path.join(folder, name)
                rel = os.path.relpath(path, vault).replace(os.sep, "/")
                try:
                    out[rel] = read_note(path, rel)
                except OSError:
                    continue
    return out


def build_nodes(local, remote, state):
    """local: {rel: note}；remote: {nid: {'rel','title','body','edited'}}；state: core.State

    返回按路径排序的节点表，每个节点带 local/remote/base 三个 hash。
    """
    remote = remote or {}
    nodes = []
    seen = set()

    for rel, note in (local or {}).items():
        nid = note.get("nid")
        if nid:
            seen.add(nid)
        key = nid or ("r:" + rel)
        info = state.note(nid) if nid else state.note(rel=rel)
        page = remote.get(nid) if nid else None
        nodes.append({
            "nid": nid,
            "key": key,
            "rel": rel,
            "title": note.get("title") or "",
            "local": note.get("hash"),
            "remote": core.digest(page["body"]) if page else None,
            "base": (info or {}).get("base"),
            "edited": (page or {}).get("edited"),
        })

    for nid, page in remote.items():
        if nid in seen:
            continue
        info = state.note(nid)
        rel = (info or {}).get("rel") or page.get("rel") or ""
        nodes.append({
            "nid": nid,
            "key": nid,
            "rel": rel,
            "title": page.get("title") or "",
            "local": None,
            "remote": core.digest(page["body"]),
            "base": (info or {}).get("base"),
            "edited": page.get("edited"),
        })

    return sorted(nodes, key=lambda item: (item["rel"] or "", item["nid"] or ""))


def renamed(state, local):
    """状态里记的路径 vs 现在的路径。"""
    previous = {key: info["rel"] for key, info in state.notes.items() if info.get("rel")}
    current = {}
    for rel, note in (local or {}).items():
        if not note.get("nid"):
            continue
        previous_rel = previous.get(note["nid"])
        if previous_rel:
            current[note["nid"]] = rel
    return core.detect_renames(previous, current)


def vanished(state, local):
    """状态里存在、但本地已经没有的笔记（用于墓碑确认）。

    注意：改名不算消失——只要 notion_id 还在本地某处，就不进这个列表。
    """
    alive = set(local or {})
    still_here = {note.get("nid") for note in (local or {}).values() if note.get("nid")}
    candidates = {
        key: info for key, info in state.notes.items()
        if info.get("rel") and not key.startswith("r:") and key not in still_here
    }
    return core.collect_tombstones(candidates, alive)
