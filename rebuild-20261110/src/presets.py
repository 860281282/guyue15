"""首次启动的「预设」：问一次主要用法，推导出一整套配置。

用户只需要回答一个问题（我主要怎么用），其余开关全部由这里推导；
之后在设置里改任何一项都允许，只是不再自动改。

字段说明（都会写进 同步工具设置.json）
    persona         obsidian / notion / relay
    conflict        冲突时谁赢：local / remote / merge
    policy          导出引擎的策略：skip（云端优先）/ overwrite（本地优先）/ copy（两边都留）
    delete_remote   本地删了，是否删云端（True/False/None=每次问）
    archive_local   云端删了，本地是否归档
    ledger          是否启用 Notion 账本（多设备必须）
    download_media  all / images / none
    upload_limit_mb 单个附件上传上限
    repair_after_import  导入后是否自动修排版标记
    backup_runs / backup_days / conflict_days  保留策略
"""

from __future__ import annotations

PERSONA_OBSIDIAN = "obsidian"
PERSONA_NOTION = "notion"
PERSONA_RELAY = "relay"

PRESETS = {
    PERSONA_OBSIDIAN: {
        "persona": PERSONA_OBSIDIAN,
        "conflict": "local",
        "policy": "skip",
        "delete_remote": True,
        "archive_local": False,
        "ledger": False,
        "download_media": "all",
        "upload_limit_mb": 20,
        "repair_after_import": True,
        "backup_runs": 20,
        "backup_days": 30,
        "conflict_days": 90,
    },
    PERSONA_NOTION: {
        "persona": PERSONA_NOTION,
        "conflict": "remote",
        "policy": "skip",
        "delete_remote": False,
        "archive_local": True,
        "ledger": False,
        "download_media": "all",
        "upload_limit_mb": 20,
        "repair_after_import": True,
        "backup_runs": 20,
        "backup_days": 30,
        "conflict_days": 90,
    },
    PERSONA_RELAY: {
        "persona": PERSONA_RELAY,
        "conflict": "merge",
        "policy": "copy",
        "delete_remote": None,
        "archive_local": True,
        "ledger": True,
        "download_media": "images",
        "upload_limit_mb": 20,
        "repair_after_import": True,
        "backup_runs": 20,
        "backup_days": 30,
        "conflict_days": 90,
    },
}

# 弹窗里的三个选项（标题 + 一句人话 + 会带来什么）
CHOICES = [
    {
        "key": PERSONA_OBSIDIAN,
        "title": "我主要用 Obsidian",
        "subtitle": "Notion 只是备份/分享",
        "detail": "一键同步先下载再上传；上传时按所选冲突策略处理。删除不会自动传播。",
        "rows": [
            ("本地改了笔记", "下次同步上传到 Notion"),
            ("Notion 改了", "下次同步下载；本地也改过时保留本地并生成冲突副本"),
            ("Notion 删了一页", "本地笔记保留不动"),
            ("本地删了一篇", "不会自动删除 Notion 页面"),
        ],
    },
    {
        "key": PERSONA_NOTION,
        "title": "我主要用 Notion",
        "subtitle": "Obsidian 只是本地副本",
        "detail": "一键同步只下载；本地改动会保留，云端删除不会自动删除本地文件。",
        "rows": [
            ("Notion 改了", "下次同步下载回本地"),
            ("本地改了", "保留本地改动；此用法的一键同步不会上传"),
            ("Notion 删了一页", "本地已有笔记保留不动"),
            ("本地删了一篇", "Notion 那页保持不动"),
        ],
    },
    {
        "key": PERSONA_RELAY,
        "title": "两边都用，多台设备",
        "subtitle": "用 Notion 在设备之间中转",
        "detail": "先下载再上传；自动三方合并和多设备账本尚未接入主流程，冲突需要人工核对。",
        "rows": [
            ("A 电脑改了", "上传到 Notion，B 电脑下次同步就能拿到"),
            ("两边都改了", "保留冲突副本，请人工比较；不保证自动合并"),
            ("一边删了", "不会自动传播删除；已有本地文件保留"),
            ("多台设备", "同步账本尚未接入主流程，请逐台检查冲突"),
        ],
    },
]

DEFAULTS = {
    "persona": PERSONA_OBSIDIAN,
    "conflict": "local",
    "policy": "skip",
    "delete_remote": True,
    "archive_local": False,
    "ledger": False,
    "download_media": "all",
    "upload_limit_mb": 20,
    "repair_after_import": True,
    "backup_runs": 20,
    "backup_days": 30,
    "conflict_days": 90,
}


def preset_for(persona):
    return dict(PRESETS.get(persona) or PRESETS[PERSONA_OBSIDIAN])


def apply(settings, persona):
    """把预设并进现有设置：预设字段以预设为准，其它键原样保留。"""
    base = dict(settings or {})
    base.update(preset_for(persona))
    base["onboarded"] = True
    return base


def needs_onboarding(settings):
    """没回答过、或旧版本从没写过 onboarded → 需要弹一次。"""
    return not (settings or {}).get("onboarded")


def describe(persona):
    for item in CHOICES:
        if item["key"] == persona:
            return item
    return CHOICES[0]


def summary(settings):
    """用法摘要，显示在状态行。"""
    persona = (settings or {}).get("persona") or PERSONA_OBSIDIAN
    item = describe(persona)
    parts = [item["title"]]
    if persona == PERSONA_NOTION:
        parts.append("一键同步仅下载")
    elif persona == PERSONA_RELAY:
        parts.append("冲突需人工核对")
    else:
        parts.append("先下载再上传；按设置处理冲突")
    return " · ".join(parts)
