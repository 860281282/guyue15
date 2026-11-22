"""构建 Windows 单文件便携版。"""

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
APP = '咕月15轻量版'
OPTIONAL_BUILD_LIBS = ROOT.parent / '_build' / 'libs'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pyinstaller-path', type=Path,
                        default=OPTIONAL_BUILD_LIBS if OPTIONAL_BUILD_LIBS.is_dir() else None)
    args = parser.parse_args()
    work = ROOT / 'build'
    output = ROOT / 'dist'
    work.mkdir(exist_ok=True)
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(work / 'cache'))
    if args.pyinstaller_path:
        env['PYTHONPATH'] = str(args.pyinstaller_path.resolve())
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
                    '--distpath', str(output), '--workpath', str(work),
                    str(ROOT / 'lite.spec')], cwd=ROOT, env=env, check=True)
    exe = output / (APP + '.exe')
    print('%s  %s 字节' % (exe, format(exe.stat().st_size, ',')))


if __name__ == '__main__':
    main()
