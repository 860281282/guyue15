# -*- coding: utf-8 -*-
"""一条命令出安装包：构建主程序 → 瘦身 → 打 payload → 构建安装程序。

在仓库根目录运行：

    python build_release.py                 # 按 journal.py 里的版本号构建
    python build_release.py --version 2.5.4 # 顺便改版本号再构建
    python build_release.py --app-only      # 只重建主程序，不重做安装包

做的事：
1. 重新打包 release 里的程序目录（排除 numpy/reportlab/PIL/lxml 等无用依赖）
2. 跑 prune.py 删掉 Tcl/Tk 中用不到的数据
3. 压缩 Python 启动库
4. 用 --self-check 验证新主程序
5. 把主程序打成 LZMA 压缩的 payload.zip
6. 用 payload 构建安装程序，文件名带版本号
7. 打印体积与 SHA256
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / 'rebuild-20261110' / 'src'
BUILD = ROOT / '_build'
LIBS = BUILD / 'libs'
PRUNE = BUILD / 'prune.py'
RELEASE = ROOT / 'release-20261118'
INSTALLER_SRC = ROOT / 'installer'
PAYLOAD = INSTALLER_SRC / 'payload.zip'

APP_NAME = '咕月15'
COMPONENTS = '同步组件'
INSTALLER_PREFIX = '安装程序'
EXCLUDES = ['numpy', 'uharfbuzz', 'reportlab', 'PIL', 'docx', 'lxml',
            'fontTools', 'pypdf', 'pypdfium2']


def say(text):
    print('[build] %s' % text, flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest().upper()


def dir_size(path):
    files = [p for p in Path(path).rglob('*') if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def current_version():
    text = (SRC / 'journal.py').read_text(encoding='utf-8')
    match = re.search(r"VERSION\s*=\s*'([^']+)'", text)
    return match.group(1) if match else '0.0.0'


def set_version(version):
    for name in ('journal.py',):
        path = SRC / name
        text = path.read_text(encoding='utf-8')
        text = re.sub(r"VERSION\s*=\s*'[^']+'", "VERSION = '%s'" % version, text, count=1)
        path.write_text(text, encoding='utf-8')
    path = INSTALLER_SRC / 'installer.py'
    text = path.read_text(encoding='utf-8')
    text = re.sub(r"VERSION = '[^']+'", "VERSION = '%s'" % version, text, count=1)
    path.write_text(text, encoding='utf-8')
    say('版本号改为 %s' % version)


VERSION_FILE = BUILD / 'version_info.txt'
ICON = SRC / 'assets' / 'app.ico'
LICENSE = SRC / 'LICENSE.txt'
ASSETS = SRC / 'assets'


def write_version_file(version: str) -> Path:
    """写 PyInstaller 的版本资源，让 exe 属性面板显示作者与产品名。"""
    nums = [int(x) for x in (version.split('.') + ['0'] * 4)[:4]]
    while len(nums) < 4:
        nums.append(0)
    four = ', '.join(str(n) for n in nums)
    text = """VSVersionInfo(
  ffi=FixedFileInfo(filevers=(%s), prodvers=(%s), mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404B0', [
        StringStruct('CompanyName', '\u5495\u670815'),
        StringStruct('FileDescription', '\u5495\u670815 \u00b7 Notion \u2194 Obsidian \u540c\u6b65'),
        StringStruct('FileVersion', '%s'),
        StringStruct('InternalName', 'NotionObsidianSync'),
        StringStruct('LegalCopyright', '\u00a9 2026 \u5495\u670815'),
        StringStruct('OriginalFilename', '\u5495\u670815.exe'),
        StringStruct('ProductName', '\u5495\u670815'),
        StringStruct('ProductVersion', '%s')])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
""" % (four, four, version, version)
    VERSION_FILE.write_text(text, encoding='utf-8')
    return VERSION_FILE


def compact_bootstrap_library(path):
    # PyInstaller 写出的是未压缩的 ZIP。当前构建解释器内置 zlib，启动时即可解压。
    if 'zlib' not in sys.builtin_module_names:
        return
    staged = path.with_suffix('.compact.zip')
    try:
        with zipfile.ZipFile(path) as original, zipfile.ZipFile(
                staged, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as compact:
            for member in original.infolist():
                compact.writestr(member.filename, original.read(member))
        if staged.stat().st_size < path.stat().st_size:
            staged.replace(path)
    finally:
        staged.unlink(missing_ok=True)


def build_app(version):
    target = RELEASE / APP_NAME
    if not RELEASE.resolve().is_relative_to(ROOT.resolve()) or target.resolve() != RELEASE.resolve() / APP_NAME:
        raise ValueError('请在项目内使用独立的构建输出目录。')
    env = dict(os.environ)
    if LIBS.is_dir():
        env['PYTHONPATH'] = str(LIBS)
    cmd = [sys.executable, '-m', 'PyInstaller',
           '--noconfirm', '--clean', '--windowed', '--onedir',
           '--name', APP_NAME, '--contents-directory', COMPONENTS,
           '--distpath', str(RELEASE), '--workpath', str(BUILD / 'work-suite'),
           '--specpath', str(BUILD),
           '--add-data', '%s;.' % (SRC / 'renderer.py'),
           '--add-data', '%s;.' % (SRC / 'README.md'),
           '--add-data', '%s;assets' % ASSETS,
           '--add-data', '%s;.' % LICENSE]
    if ICON.is_file():
        cmd += ['--icon', str(ICON)]
    if version:
        cmd += ['--version-file', str(write_version_file(version))]
    for module in EXCLUDES:
        cmd += ['--exclude-module', module]
    cmd.append('suite.py')
    subprocess.run([str(c) for c in cmd], cwd=str(SRC), env=env, check=True)
    if PRUNE.is_file():
        subprocess.run([sys.executable, PRUNE, target / COMPONENTS], check=True)
    compact_bootstrap_library(target / COMPONENTS / 'base_library.zip')
    return target


def self_check(exe):
    info = subprocess.run([str(exe), '--self-check'], capture_output=True, text=True, timeout=180)
    if info.returncode != 0:
        raise RuntimeError('自检失败：%s' % (info.stdout + info.stderr)[-800:])
    say('自检通过：%s' % (info.stdout.strip() or 'ok'))
    for junk in ('工具检查.json',):
        try:
            (Path(exe).parent / junk).unlink()
        except OSError:
            pass


def make_payload(app_dir):
    with zipfile.ZipFile(PAYLOAD, 'w', zipfile.ZIP_LZMA) as zf:
        for item in sorted(app_dir.rglob('*')):
            if not item.is_file():
                continue
            rel = item.relative_to(app_dir)
            if rel.parts[0] in (APP_NAME + '.exe', COMPONENTS):
                zf.write(item, str(rel))
    return PAYLOAD.stat().st_size


def build_installer(version):
    env = dict(os.environ)
    if LIBS.is_dir():
        env['PYTHONPATH'] = str(LIBS)
    name = '%s-%s-%s' % (APP_NAME, INSTALLER_PREFIX, version)
    cmd = [sys.executable, '-m', 'PyInstaller',
           '--noconfirm', '--clean', '--onefile', '--windowed',
           # 火绒这类 HIPS 会拦新 exe 写 %TEMP%，解包目录改成"安装包当前所在目录"
           '--runtime-tmpdir', '.',
           # 安装器只解包和写入安装信息，使用 Python 内置摘要实现即可。
           '--exclude-module', '_hashlib',
           '--name', name,
           '--distpath', str(RELEASE), '--workpath', str(INSTALLER_SRC / 'work-final'),
           '--specpath', str(INSTALLER_SRC),
           '--add-data', '%s;.' % PAYLOAD,
           '--add-data', '%s;.' % ICON, 'installer.py']
    if ICON.is_file():
        cmd += ['--icon', str(ICON)]
    if version:
        cmd += ['--version-file', str(write_version_file(version))]
    subprocess.run([str(c) for c in cmd], cwd=str(INSTALLER_SRC), env=env, check=True)
    return RELEASE / (name + '.exe')


def main():
    parser = argparse.ArgumentParser(description='一条命令出安装包')
    parser.add_argument('--version', default='', help='顺便改版本号，例如 2.5.4')
    parser.add_argument('--app-only', action='store_true', help='只重建主程序')
    parser.add_argument('--skip-check', action='store_true', help='跳过自检')
    args = parser.parse_args()

    if args.version:
        set_version(args.version)
    version = current_version()
    say('开始构建 v%s' % version)
    RELEASE.mkdir(parents=True, exist_ok=True)

    app_dir = build_app(version)
    exe = app_dir / (APP_NAME + '.exe')
    exe_files, exe_bytes = dir_size(app_dir)
    say('主程序 %s（%d 文件 %s 字节）' % (exe.name, exe_files, format(exe_bytes, ',')))
    if not args.skip_check:
        self_check(exe)
    if args.app_only:
        say('只做主程序，输出目录：%s' % app_dir)
        return 0

    payload_bytes = make_payload(app_dir)
    say('payload.zip %s 字节' % format(payload_bytes, ','))
    installer = build_installer(version)
    say('安装包 %s（%s 字节）' % (installer.name, format(installer.stat().st_size, ',')))

    print('')
    print('== 结果 ==')
    print('主程序   %-52s %s B' % (exe.name, format(exe.stat().st_size, ',')))
    print('组件     %-52s %d 文件 %s B' % (COMPONENTS, exe_files - 1, format(exe_bytes - exe.stat().st_size, ',')))
    print('安装包   %-52s %s B' % (installer.name, format(installer.stat().st_size, ',')))
    print('exe SHA256   %s' % sha256(exe))
    print('安装包 SHA256 %s' % sha256(installer))
    return 0


if __name__ == '__main__':
    sys.exit(main())
