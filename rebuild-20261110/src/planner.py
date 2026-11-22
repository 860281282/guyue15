"""把 `core.classify()` 的判定翻译成"这一轮到底干什么"。

两个方向共用这一层：谁写谁由人设决定，而不是由"这是导入还是导出"决定。

人设
----
obsidian  本地优先：冲突本地赢（云端存副本），云端删除→本地保留并提示，
          本地删除→删云端（需确认），云端没有的→推上去
notion    云端优先：冲突云端赢（本地存副本），本地删除→从云端下载回来，
          云端删除→本地归档（移到 .同步工具/归档/）
relay     多设备中转：冲突走三方合并，合不了才报冲突；
          删除一律"墓碑 + 确认"，不静默
"""

from __future__ import annotations

import core

OBSIDIAN = "obsidian"
NOTION = "notion"
RELAY = "relay"
PERSONAS = (OBSIDIAN, NOTION, RELAY)

PERSONA_LABEL = {
    OBSIDIAN: "Obsidian 为主（本地是家）",
    NOTION: "Notion 为主（云端是家）",
    RELAY: "多设备中转（两边都改）",
}

# action -> 每种人设下的具体动作
TABLE = {
    core.SAME:        {OBSIDIAN: "skip",        NOTION: "skip",        RELAY: "skip"},
    core.NOTHING:     {OBSIDIAN: "skip",        NOTION: "skip",        RELAY: "skip"},
    core.NEW_LOCAL:   {OBSIDIAN: "create_remote", NOTION: "create_remote", RELAY: "create_remote"},
    core.NEW_REMOTE:  {OBSIDIAN: "create_local",  NOTION: "create_local",  RELAY: "create_local"},
    core.PUSH:        {OBSIDIAN: "push",        NOTION: "push",        RELAY: "push"},
    core.PULL:        {OBSIDIAN: "pull",        NOTION: "pull",        RELAY: "pull"},
    core.CONFLICT:    {OBSIDIAN: "push",        NOTION: "pull",        RELAY: "merge"},
    core.LOCAL_GONE:  {OBSIDIAN: "trash_remote", NOTION: "pull",       RELAY: "trash_remote"},
    core.REMOTE_GONE: {OBSIDIAN: "push",        NOTION: "archive_local", RELAY: "archive_local"},
    core.BOTH_GONE:   {OBSIDIAN: "drop_state",  NOTION: "drop_state",  RELAY: "drop_state"},
}

# 这些动作会动到"用户看不见的地方"，必须确认或至少留痕
NEEDS_CONFIRM = {"trash_remote"}
KEEPS_BACKUP = {"push", "pull", "merge"}   # 覆盖前要备份
NOTES = {
    "push": "会覆盖云端，覆盖前已备份云端原文",
    "pull": "会覆盖本地，覆盖前已备份本地原文",
    "merge": "自动合并两边改动；合不了会落冲突文件",
    "trash_remote": "会把云端页面移入回收站（需确认）",
    "archive_local": "本地文件移到 .同步工具/归档/，不删除",
    "create_remote": "在 Notion 新建页面",
    "create_local": "在本地新建笔记",
    "drop_state": "两边都没了，清掉同步记录",
}


def actions(nodes, persona=RELAY):
    """nodes: [{'nid','rel','local','remote','base','title'}]

    返回 [{'nid','rel','title','kind','action','confirm','note'}]，
    顺序按 rel 排序，便于界面稳定展示。
    """
    if persona not in PERSONAS:
        raise ValueError("未知人设：%r" % (persona,))
    out = []
    for node in nodes:
        kind = core.classify(node.get("local"), node.get("remote"), node.get("base"))
        action = TABLE[kind][persona]
        out.append({
            "nid": node.get("nid"),
            "rel": node.get("rel") or "",
            "title": node.get("title") or "",
            "kind": kind,
            "action": action,
            "confirm": action in NEEDS_CONFIRM,
            "backup": action in KEEPS_BACKUP,
            "note": NOTES.get(action, ""),
        })
    return sorted(out, key=lambda item: item["rel"])


def summarize(planned):
    counts = {}
    for item in planned:
        counts[item["action"]] = counts.get(item["action"], 0) + 1
    return counts


def human_summary(planned):
    """给界面用的一句话，只报"会写东西"的动作。"""
    counts = summarize(planned)
    labels = {
        "create_remote": "云端新建",
        "create_local": "本地新建",
        "push": "上传",
        "pull": "下载",
        "merge": "合并",
        "trash_remote": "云端删除（待确认）",
        "archive_local": "本地归档",
        "drop_state": "清理记录",
    }
    parts = ["%s %d" % (labels[k], counts[k]) for k in labels if counts.get(k)]
    return "，".join(parts) if parts else "没有需要处理的变化"
