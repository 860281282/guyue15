r"""磁盘治理：算占用、按策略清理旧备份，顺手保证"不会越用越大"。

清理对象只有三类（都在 `.同步工具` 里，绝不碰笔记本身）：
    备份\      每轮同步的覆盖前备份          → 保留最近 N 轮 或 D 天
    导出备份\  上传前保存的云端原文          → 同上
    冲突\      冲突时的两份正文              → 保留最近 D 天
    排版备份\  修复排版前的整库副本          → 保留最近 3 次
"""

from __future__ import annotations

import os
import shutil
import time

TOOL = ".同步工具"
KEEP_RUNS = 20
KEEP_DAYS = 30
CONFLICT_DAYS = 90
FIX_KEEP = 3

FOLDERS = ("备份", "导出备份", "冲突", "排版备份", "日志", "基线")


def tool_dir(home):
    return os.path.join(os.path.abspath(home), TOOL)


def _entries(path):
    try:
        names = os.listdir(path)
    except OSError:
        return []
    out = []
    for name in names:
        full = os.path.join(path, name)
        try:
            stat = os.stat(full)
        except OSError:
            continue
        out.append((name, full, stat.st_mtime, stat.st_size))
    return out


def _tree_size(path):
    total = 0
    for folder, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                continue
    return total


def sizes(home):
    root = tool_dir(home)
    out = {}
    for name in FOLDERS:
        path = os.path.join(root, name)
        out[name] = _tree_size(path) if os.path.isdir(path) else 0
    return out


def human(nbytes):
    value = float(nbytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return ("%.0f %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%.1f GB" % value


def _prune_dir(path, keep_runs=None, keep_days=None, keep_newest=None):
    """删掉超期的子项，返回 (删除数, 释放字节)。"""
    removed, freed = 0, 0
    items = _entries(path)
    if not items:
        return 0, 0
    items.sort(key=lambda item: item[2], reverse=True)
    now = time.time()
    for index, (name, full, mtime, _size) in enumerate(items):
        too_many = keep_runs is not None and index >= keep_runs
        too_old = keep_days is not None and (now - mtime) > keep_days * 86400
        beyond_newest = keep_newest is not None and index >= keep_newest
        if not (too_many or too_old or beyond_newest):
            continue
        space = _tree_size(full) if os.path.isdir(full) else os.path.getsize(full)
        try:
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.unlink(full)
        except OSError:
            continue
        removed += 1
        freed += space
    return removed, freed


def cleanup(home, keep_runs=KEEP_RUNS, keep_days=KEEP_DAYS,
            conflict_days=CONFLICT_DAYS, fix_keep=FIX_KEEP):
    root = tool_dir(home)
    removed, freed = 0, 0
    for name in ("备份", "导出备份"):
        got, size = _prune_dir(os.path.join(root, name), keep_runs=keep_runs, keep_days=keep_days)
        removed += got
        freed += size
    got, size = _prune_dir(os.path.join(root, "冲突"), keep_days=conflict_days)
    removed += got
    freed += size
    got, size = _prune_dir(os.path.join(root, "排版备份"), keep_newest=fix_keep)
    removed += got
    freed += size
    return removed, freed
