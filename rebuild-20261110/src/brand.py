# -*- coding: utf-8 -*-
"""品牌与署名：所有对外露出的名字、版权、联系方式、素材路径都从这里取。

原则：
- 只在一个地方改名字，其余代码一律引用本模块；
- 素材路径先按可执行文件同目录找（用户能自己替换），再退回打包进 _MEIPASS 的副本；
- 缺素材不能崩：二维码、图标缺失时返回 None，由界面给出人话提示。
"""
from __future__ import annotations

import sys
from pathlib import Path

AUTHOR = '咕月15'
APP_NAME = '咕月15'          # 程序对外显示的名字（窗口标题、安装包、快捷方式）
APP_SUB = 'Notion ↔ Obsidian 同步'
APP_CN = APP_NAME
COPYRIGHT = '© 2026 ' + AUTHOR
TAGLINE = '两边改动都保留，覆盖前先备份'
CONTACT = ''          # 有就填（邮箱/主页），空着就整行隐藏
HOMEPAGE = ''         # 同上

LICENSE_FILE = 'LICENSE.txt'

# 素材文件名：放在程序目录 assets\ 下，或直接放在 exe 同级
ICON_PNG = 'icon-1024.png'
ICON_ICO = 'app.ico'
QR_WECHAT = 'qr-wechat.png'
QR_ANY = '收款码.png'      # exe 同级的通用覆盖名


def exe_dir() -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    """打包进 exe 的只读素材目录（PyInstaller 解到 _MEIPASS）。"""
    meipass = getattr(sys, '_MEIPASS', None)
    return Path(meipass) if meipass else Path(__file__).resolve().parent


def asset(name: str):
    """按「exe 同级 → 同级 assets → 打包内 assets → 打包内根」顺序找素材。"""
    if not name:
        return None
    for base in (exe_dir(), exe_dir() / 'assets', bundle_dir() / 'assets', bundle_dir()):
        path = base / name
        if path.is_file():
            return path
    return None


def qr(kind: str = 'wechat'):
    """二维码：先认同级通用覆盖名，再认分渠道文件名。"""
    names = [QR_ANY] if kind == 'any' else ['%s-%s.png' % (kind, 'wechat' if kind == 'wechat' else kind), QR_ANY]
    if kind == 'wechat':
        names = ['qr-wechat.png', QR_ANY]
    elif kind == 'alipay':
        names = ['qr-alipay.png', QR_ANY]
    for name in names:
        path = asset(name)
        if path:
            return path
    return None


def has_qr() -> bool:
    return qr('wechat') is not None or qr('alipay') is not None


def version_line() -> str:
    import journal
    return '%s · %s · %s' % (APP_CN, AUTHOR, journal.VERSION)
