# -*- coding: utf-8 -*-
"""打包后删掉用不到的附带数据（Tcl 的时区库、Tk 的演示图）。

用法：python _build/prune.py <同步组件目录>

只删两类东西：
1. `_tcl_data/tzdata`：Tcl 的 clock 命令查时区用，本程序不查时区；
2. `_tk_data/images`、`_tk_data/demos`：Tk 自带的图标和演示。
删完请跑一次 `--self-check` 和一次四格式转换，确认没影响。
"""

import shutil
import sys
from pathlib import Path

TARGETS = (
    ('_tcl_data', 'tzdata'),
    ('_tcl_data', 'msgs'),
    ('_tk_data', 'images'),
    ('_tk_data', 'demos'),
)


def main():
    if len(sys.argv) < 2:
        print('用法：python prune.py <组件目录>')
        return 2
    root = Path(sys.argv[1])
    if not root.is_dir():
        print('目录不存在：%s' % root)
        return 2
    freed = 0
    for parent, name in TARGETS:
        target = root / parent / name
        if not target.exists():
            continue
        size = sum(f.stat().st_size for f in target.rglob('*') if f.is_file())
        shutil.rmtree(target, ignore_errors=True)
        freed += size
        print('删除 %s（%.1f KB）' % (target, size / 1024.0))
    print('共省 %.1f KB' % (freed / 1024.0))
    return 0


if __name__ == '__main__':
    sys.exit(main())
