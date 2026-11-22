# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH).resolve()
source = root / 'src'
icon = source / 'assets' / 'app.ico'

analysis = Analysis(
    [str(source / 'lite.py')],
    pathex=[str(source)],
    datas=[(str(source / 'renderer.py'), '.'), (str(source / 'assets' / 'mark.png'), 'assets'),
           (str(root / 'LICENSE.txt'), '.')],
    hiddenimports=['csv'],
    excludes=['numpy', 'PIL', 'reportlab', 'lxml', 'docx', 'fontTools', 'pypdf',
              'pypdfium2', 'uharfbuzz', 'unittest', 'test'],
)

# 同步和时间戳均走 Python，Tk 不使用时区数据库和演示资源。
unused = ('_tcl_data/tzdata/', '_tk_data/demos/', '_tk_data/images/')
analysis.datas = [item for item in analysis.datas
                  if not item[0].replace('\\', '/').startswith(unused)]
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz, analysis.scripts, analysis.binaries, analysis.datas, [],
    name='咕月15轻量版',
    console=False,
    icon=[str(icon)],
    upx=False,
)
