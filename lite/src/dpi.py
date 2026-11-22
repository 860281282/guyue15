# -*- coding: utf-8 -*-
"""让程序自己处理高分屏缩放。

不声明 DPI 感知时，Windows 会把整个窗口先按小尺寸画好、再放大到物理像素，
文字和线条就是糊的。这里在创建窗口之前声明「每个显示器单独感知」，
再由 Tk 的 scaling 决定字号，几何尺寸统一用 s() 乘上缩放比例。
"""

import ctypes
import sys

_AWARE = False
_LOCKED = None


def _try(call):
    try:
        call()
        return True
    except Exception:  # noqa: BLE001
        return False


def make_aware():
    """在 import tkinter 之前调用；失败就保持系统默认（不报错）。"""
    global _AWARE
    if _AWARE or not sys.platform.startswith('win'):
        return _AWARE
    user32 = ctypes.windll.user32
    # -4 = PER_MONITOR_AWARE_V2
    if _try(lambda: user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))):
        _AWARE = True
    elif _try(lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2)):
        _AWARE = True
    elif _try(lambda: user32.SetProcessDPIAware()):
        _AWARE = True
    return _AWARE


def set_scale(value):
    """窗口建好之后，用它自己报的 DPI 锁定缩放比例（比问系统准）。"""
    global _LOCKED
    try:
        value = float(value)
    except (TypeError, ValueError):
        return
    if value >= 1.0:
        _LOCKED = max(1.0, min(3.0, value))


def scale():
    """当前系统缩放比例，100% 返回 1.0，150% 返回 1.5。

    每次现算：声明 DPI 感知之前和之后系统给的答案不一样，缓存会出错。
    """
    if _LOCKED is not None:
        return _LOCKED
    value = 1.0
    if sys.platform.startswith('win'):
        try:
            dpi = ctypes.windll.user32.GetDpiForSystem()
            if dpi:
                value = dpi / 96.0
        except Exception:  # noqa: BLE001
            pass
        if value == 1.0:
            try:
                dpi = ctypes.windll.gdi32.GetDeviceCaps(ctypes.windll.user32.GetDC(0), 88)
                if dpi:
                    value = dpi / 96.0
            except Exception:  # noqa: BLE001
                pass
    return max(1.0, min(3.0, value))


def apply(root):
    """把缩放套到 Tk：100% 时什么都不变，150% 时字号放大一半。"""
    # 以窗口自己报的物理 DPI 为准：exe 带清单时系统接口可能给假值
    try:
        real = root.winfo_fpixels('1i') / 96.0
        if real >= 1.05:
            set_scale(real)
    except Exception:  # noqa: BLE001
        pass
    factor = scale()
    if factor > 1.0:
        try:
            root.tk.call('tk', 'scaling', factor * 96.0 / 72.0)
        except Exception:  # noqa: BLE001
            pass
    return factor


def s(value):
    """把逻辑像素换算成当前缩放下的像素。"""
    return int(round(value * scale()))


def info():
    """排查用：打印各接口给出的 DPI / 缩放。"""
    out = {'aware': _AWARE, 'locked': _LOCKED, 'scale': scale()}
    if sys.platform.startswith('win'):
        try:
            out['system_dpi'] = ctypes.windll.user32.GetDpiForSystem()
        except Exception:  # noqa: BLE001
            out['system_dpi'] = None
        try:
            dc = ctypes.windll.user32.GetDC(0)
            out['logpixelsx'] = ctypes.windll.gdi32.GetDeviceCaps(dc, 88)
            out['desktop_h'] = ctypes.windll.gdi32.GetDeviceCaps(dc, 118)
            out['screen_w'] = ctypes.windll.gdi32.GetDeviceCaps(dc, 8)
        except Exception:  # noqa: BLE001
            pass
    return out
