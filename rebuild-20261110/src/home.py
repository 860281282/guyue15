# -*- coding: utf-8 -*-
"""程序运行时该把设置、日志、缓存放在哪。

开发时（源码直跑）：放在源码目录，方便调试。
打包后：默认放在 ``%APPDATA%\\NotionObsidianSync``，装到 Program Files 之类
没有写权限的地方也能用；如果程序目录里放了 ``portable.flag``，则继续用程序
目录（绿色版、U 盘版）。

第一次切换时会把旧版本放在程序目录里的设置、日志、数据搬过来，用户无感。
"""

import os
import shutil
import sys
from pathlib import Path

APP_NAME = 'NotionObsidianSync'
PORTABLE_FLAG = 'portable.flag'
MOVE_NAMES = ('同步工具设置.json', '同步工具设置-导出.json', '同步工具设置-排版.json',
              '导出预览.json', '最近同步.json', '最近错误.json')
MOVE_DIRS = ('数据', '日志', '缓存')


def exe_dir():
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _writable(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / '.write-test'
        probe.write_text('x', encoding='utf-8')
        probe.unlink()
        return True
    except OSError:
        return False


def data_root():
    """返回放设置/日志/缓存的目录。"""
    here = exe_dir()
    if not getattr(sys, 'frozen', False):
        return here
    if (here / PORTABLE_FLAG).exists():
        return here
    appdata = os.environ.get('APPDATA') or str(Path.home() / 'AppData' / 'Roaming')
    base = Path(appdata) / APP_NAME
    if _writable(base) or _writable(here):
        return base if _writable(base) else here
    return base


def migrate(old, new):
    """把旧目录里的设置、数据搬到新目录（只搬一次，不覆盖已有文件）。"""
    if old == new or not old.is_dir():
        return []
    try:
        new.mkdir(parents=True, exist_ok=True)
    except OSError:
        return []
    moved = []
    for name in MOVE_NAMES:
        src, dst = old / name, new / name
        if src.is_file() and not dst.exists():
            try:
                shutil.copy2(src, dst)
                moved.append(name)
            except OSError:
                pass
    for name in MOVE_DIRS:
        src, dst = old / name, new / name
        if not src.is_dir():
            continue
        for item in src.rglob('*'):
            if not item.is_file():
                continue
            target = dst / item.relative_to(src)
            if target.exists():
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
                moved.append(str(target.relative_to(new)))
            except OSError:
                pass
    return moved


def home():
    """取数据目录，顺带做一次迁移。"""
    here = exe_dir()
    root = data_root()
    if root != here:
        migrate(here, root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root


def shared(home_dir=None):
    """设置文件所在目录：装在 .同步工具 里时用上一层，让一组工具共用设置。"""
    base = Path(home_dir) if home_dir else home()
    return base.parent if base.parent.name == '.同步工具' else base
