# -*- coding: utf-8 -*-
"""安装程序：把打包好的程序装进本机，并建好快捷方式与卸载项。

界面三步：选目录 → 安装（解压 + 快捷方式 + 注册表）→ 完成。
不需要管理员权限，默认装到当前用户的 `%LOCALAPPDATA%\\Programs\\NotionObsidianSync`。

用法（打包后）：双击即可。开发时调试可直接 `python installer.py`。
"""

from contextlib import contextmanager
import os
import sys
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = '咕月15'
APP_ID = 'NotionObsidianSync'
VERSION = '2.9.7'
AUTHOR = '咕月15'
PUBLISHER = AUTHOR
MAIN_EXE = '咕月15.exe'
COMPONENTS = '同步组件'
PAYLOAD = 'payload.zip'

BG = '#F4F5F7'
CARD = '#FFFFFF'
INK = '#0F131A'
MUTED = '#5A6472'
LINE = '#CBD2DC'
ACCENT = '#C2410C'
OK = '#157F3D'
ERR = '#B3261E'


class InstallPermissionError(PermissionError):
    def __init__(self, step, folder=None):
        self.step = step
        self.folder = folder
        if folder is None:
            message = 'Windows 拒绝%s。本次程序文件变更已撤回，请检查当前用户的安装权限后重试。' % step
        else:
            message = ('Windows 拒绝%s。\n\n目录：%s\n\n'
                       '请使用默认位置，或选择当前用户可写的目录后重试。') % (step, folder)
        super().__init__(message)


@contextmanager
def install_access(step, folder=None):
    try:
        yield
    except PermissionError as error:
        raise InstallPermissionError(step, folder) from error


def default_dir():
    base = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
    return Path(base) / 'Programs' / APP_ID


def payload_path():
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    candidate = base / PAYLOAD
    if candidate.is_file():
        return candidate
    raise FileNotFoundError('安装包缺少 %s，可能下载不完整' % PAYLOAD)


def shortcut(target, link, workdir, icon=None):
    """用 WScript.Shell 建 .lnk（比手写 COM 省事，Windows 自带）。"""
    link.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$ErrorActionPreference = 'Stop';"
        "$ws = New-Object -ComObject WScript.Shell;"
        "$s = $ws.CreateShortcut($env:GUYUE_SHORTCUT);"
        "$s.TargetPath = $env:GUYUE_TARGET;$s.WorkingDirectory = $env:GUYUE_WORKDIR;"
        "$s.IconLocation = $env:GUYUE_ICON;$s.Description = $env:GUYUE_NAME;$s.Save()"
    )
    env = dict(os.environ, GUYUE_SHORTCUT=str(link), GUYUE_TARGET=str(target),
               GUYUE_WORKDIR=str(workdir), GUYUE_ICON=str(icon or target), GUYUE_NAME=APP_NAME)
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script], env=env,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                            capture_output=True, text=True, errors='replace', timeout=30)
    if result.returncode or not Path(link).exists():
        raise RuntimeError('建快捷方式失败（rc=%s）%s %s' % (
            result.returncode, (result.stdout or '').strip()[-200:], (result.stderr or '').strip()[-200:]))


def desktop_link():
    desk = Path(os.environ.get('USERPROFILE', str(Path.home()))) / 'Desktop'
    return desk / (APP_NAME + '.lnk')


def start_menu_link():
    base = os.environ.get('APPDATA') or str(Path.home() / 'AppData' / 'Roaming')
    return Path(base) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / (APP_NAME + '.lnk')


def write_registry(install_dir, exe):
    import winreg
    key_path = r'Software\Microsoft\Windows\CurrentVersion\Uninstall\%s' % APP_ID
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        values = {
            'DisplayName': APP_NAME,
            'DisplayVersion': VERSION,
            'Publisher': PUBLISHER,
            'InstallLocation': str(install_dir),
            'DisplayIcon': str(exe),
            'UninstallString': '"%s" --uninstall' % exe,
            'QuietUninstallString': '"%s" --uninstall --quiet' % exe,
            'NoModify': 1,
            'NoRepair': 1,
        }
        for name, value in values.items():
            kind = winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ
            winreg.SetValueEx(key, name, 0, kind, value)
        try:
            size = exe.stat().st_size + sum(f.stat().st_size for f in
                                          (install_dir / COMPONENTS).rglob('*') if f.is_file())
            winreg.SetValueEx(key, 'EstimatedSize', 0, winreg.REG_DWORD, max(1, size // 1024))
        except OSError:
            pass


def extract(payload, target, progress=None):
    with zipfile.ZipFile(payload) as zf:
        members = zf.infolist()
        total = len(members)
        for i, info in enumerate(members, 1):
            zf.extract(info, target)
            if progress and (i % 20 == 0 or i == total):
                progress(i / float(total))


def install_files(payload, target, progress=None, register=None):
    target = target.absolute()
    with install_access('访问安装目录', target):
        if any(p.is_symlink() or p.is_junction() for p in (target, target / MAIN_EXE, target / COMPONENTS)):
            raise ValueError('安装目录和程序文件不能是链接或目录联接。')
        target = target.resolve()
        if (target / 'portable.flag').exists() or any(
                parent.name == '.同步工具' or (parent / '.obsidian').is_dir()
                for parent in (target, *target.parents)):
            raise ValueError('请选择独立的安装目录，知识库和便携版应使用原有入口更新。')
    with install_access('创建安装目录', target):
        target.mkdir(parents=True, exist_ok=True)
    with install_access('在安装目录内创建临时文件夹', target):
        stage = Path(tempfile.mkdtemp(prefix='.guyue-install-', dir=target))
    keep_stage = False
    try:
        incoming = stage / 'incoming'
        backup = stage / 'backup'
        with install_access('写入临时安装文件', target):
            incoming.mkdir()
            backup.mkdir()
            extract(payload, incoming, progress)
        if not (incoming / MAIN_EXE).is_file() or not (incoming / COMPONENTS).is_dir():
            raise ValueError('安装包缺少主程序或同步组件。')
        old_exe, old_components = target / MAIN_EXE, target / COMPONENTS
        if (old_exe.exists() or old_components.exists()) and not (old_exe.is_file() and old_components.is_dir()):
            raise ValueError('目标目录已有不完整的程序文件，请先选用单独的安装目录。')
        previous = []
        installed = []
        try:
            with install_access('写入程序文件', target):
                for name in (MAIN_EXE, COMPONENTS):
                    if (target / name).exists():
                        (target / name).rename(backup / name)
                        previous.append(name)
                for name in (COMPONENTS, MAIN_EXE):
                    (incoming / name).rename(target / name)
                    installed.append(name)
            if register is not None:
                with install_access('写入卸载信息'):
                    register(target, target / MAIN_EXE)
        except Exception:
            try:
                for name in reversed(installed):
                    (target / name).rename(incoming / name)
                for name in reversed(previous):
                    (backup / name).rename(target / name)
            except OSError as error:
                keep_stage = True
                raise RuntimeError('恢复不完整，原程序备份和待安装文件保留在 %s' % stage) from error
            raise
    finally:
        if not keep_stage:
            shutil.rmtree(stage, ignore_errors=True)
    return target / MAIN_EXE


class Installer:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title('%s 安装程序 %s' % (APP_NAME, VERSION))
        try:
            base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
            self.root.iconbitmap(default=str(base / 'app.ico'))
        except Exception:  # noqa: BLE001
            pass
        self.root.configure(bg=BG)
        self.root.geometry('760x520')
        self.root.minsize(680, 480)
        self.target = tk.StringVar(value=str(default_dir()))
        self.desktop = tk.BooleanVar(value=True)
        self.launch = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value='点「开始安装」把程序装到上面的目录。')
        self._build()

    def _build(self):
        wrap = tk.Frame(self.root, bg=BG, padx=28, pady=22)
        wrap.pack(fill='both', expand=True)
        tk.Label(wrap, text='安装 %s' % APP_NAME, bg=BG, fg=INK,
                 font=('Microsoft YaHei UI', 18, 'bold'), anchor='w').pack(fill='x')
        tk.Label(wrap, text='%s · Notion ↔ Obsidian 同步 · 免费使用，保留署名' % AUTHOR,
                 bg=BG, fg=ACCENT, font=('Microsoft YaHei UI', 10), anchor='w').pack(fill='x', pady=(2, 0))
        tk.Label(wrap, text='版本 %s · 装给当前用户，不需要管理员权限。' % VERSION,
                 bg=BG, fg=MUTED, font=('Microsoft YaHei UI', 10), anchor='w').pack(fill='x', pady=(2, 16))

        card = tk.Frame(wrap, bg=CARD, highlightbackground=LINE, highlightthickness=1, padx=20, pady=18)
        card.pack(fill='x')
        tk.Label(card, text='安装位置', bg=CARD, fg=MUTED,
                 font=('Microsoft YaHei UI', 9), anchor='w').pack(fill='x')
        row = tk.Frame(card, bg=CARD)
        row.pack(fill='x', pady=(6, 12))
        self.path_entry = tk.Entry(row, textvariable=self.target, font=('Microsoft YaHei UI', 10),
                                   relief='flat', bg='#F7F8FA', highlightthickness=1,
                                   highlightbackground=LINE, highlightcolor=ACCENT)
        self.path_entry.pack(side='left', fill='x', expand=True, ipady=6)
        self.pick_button = tk.Button(row, text='更改', command=self.pick_dir, relief='flat', bg='#EFEFEF',
                                     font=('Microsoft YaHei UI', 9), padx=12, cursor='hand2')
        self.pick_button.pack(side='left', padx=(8, 0))
        self.default_button = tk.Button(row, text='使用默认位置', command=self.use_default,
                                        relief='flat', bg='#EFEFEF', font=('Microsoft YaHei UI', 9), padx=10)
        self.default_button.pack(side='left', padx=(8, 0))

        tk.Checkbutton(card, text='在桌面创建快捷方式', variable=self.desktop, bg=CARD, fg=INK,
                       activebackground=CARD, selectcolor=CARD, bd=0, highlightthickness=0,
                       font=('Microsoft YaHei UI', 10), anchor='w').pack(fill='x')
        tk.Checkbutton(card, text='安装完成后直接启动', variable=self.launch, bg=CARD, fg=INK,
                       activebackground=CARD, selectcolor=CARD, bd=0, highlightthickness=0,
                       font=('Microsoft YaHei UI', 10), anchor='w').pack(fill='x', pady=(4, 0))

        bar = ttk.Progressbar(wrap, mode='determinate', maximum=100)
        bar.pack(fill='x', pady=(18, 6))
        self.bar = bar
        tk.Label(wrap, textvariable=self.status, bg=BG, fg=MUTED,
                 font=('Microsoft YaHei UI', 9), anchor='w').pack(fill='x')

        foot = tk.Frame(wrap, bg=BG)
        foot.pack(side='bottom', fill='x', pady=(16, 0))
        self.go = tk.Button(foot, text='开始安装', command=self.start, relief='flat',
                            bg='#111418', fg='white', activebackground='#000000',
                            activeforeground='white', font=('Microsoft YaHei UI', 11, 'bold'),
                            padx=26, pady=8, cursor='hand2')
        self.go.pack(side='right')
        self.exit_button = tk.Button(foot, text='退出', command=self.root.destroy, relief='flat', bg='#EFEFEF',
                                     font=('Microsoft YaHei UI', 10), padx=18, pady=8, cursor='hand2')
        self.exit_button.pack(side='right', padx=(0, 10))
        self.foot_hint = tk.Label(foot, text='请选择独立的安装目录。',
                                  bg=BG, fg=MUTED, font=('Microsoft YaHei UI', 9))
        self.foot_hint.pack(side='left')

    def pick_dir(self):
        chosen = filedialog.askdirectory(title='选择安装位置', initialdir=str(Path(self.target.get()).parent))
        if chosen:
            self.target.set(str(Path(chosen) / APP_ID))

    def use_default(self):
        self.target.set(str(default_dir()))
        self.status.set('已恢复默认安装位置。')

    def set_location_editable(self, editable):
        state = 'normal' if editable else 'disabled'
        for widget in (self.path_entry, self.pick_button, self.default_button):
            widget.configure(state=state)

    def start(self):
        selected = self.target.get().strip()
        if not selected:
            messagebox.showwarning('还没选位置', '请先选一个安装位置。')
            return
        target = Path(selected).expanduser().absolute()
        try:
            installed = (target / MAIN_EXE).exists()
        except PermissionError:
            self.fail(InstallPermissionError('访问安装目录', target))
            return
        if installed:
            if not messagebox.askyesno('已安装', '这个目录里已经有一份，覆盖安装？\n%s' % target):
                return
        self.go.configure(state='disabled', text='安装中…')
        self.root.protocol('WM_DELETE_WINDOW', lambda: None)
        self.exit_button.configure(state='disabled')
        self.set_location_editable(False)
        create_desktop = self.desktop.get()

        def work():
            try:
                self.say('正在解压文件…')
                def register(location, executable):
                    self.say('正在写入卸载信息…')
                    write_registry(location, executable)
                exe = install_files(payload_path(), target, progress=self.progress, register=register)
                warnings = []
                self.say('正在创建快捷方式…')
                try:
                    shortcut(exe, start_menu_link(), target)
                    if create_desktop:
                        shortcut(exe, desktop_link(), target)
                except Exception as exc:  # noqa: BLE001
                    warnings.append('快捷方式没建成（%s）' % exc)
                self.root.after(0, lambda: self.done(target, exe, warnings))
            except Exception as exc:  # noqa: BLE001
                self.root.after(0, lambda error=exc: self.fail(error))

        threading.Thread(target=work, daemon=True).start()

    def progress(self, ratio):
        self.root.after(0, lambda: self.bar.configure(value=int(ratio * 100)))
        return ''

    def say(self, text):
        self.root.after(0, lambda: self.status.set(text))

    def done(self, target, exe, warnings=None):
        self.root.protocol('WM_DELETE_WINDOW', self.root.destroy)
        self.exit_button.configure(state='normal')
        self.set_location_editable(True)
        self.bar.configure(value=100)
        self.status.set('装好了：%s' % target)
        self.go.configure(state='normal', text='重新安装')
        self.foot_hint.configure(text='卸载：开始菜单里右键该程序，或在「应用和功能」里卸载。', fg=OK)
        if self.launch.get():
            try:
                subprocess.Popen([str(exe)], cwd=str(target))
            except OSError:
                pass
        note = '\n\n已创建开始菜单快捷方式。'
        if warnings:
            note = '\n\n' + '\n'.join(warnings)
        messagebox.showinfo('安装完成', '已经装好了。\n\n位置：%s%s' % (target, note))

    def fail(self, error):
        self.root.protocol('WM_DELETE_WINDOW', self.root.destroy)
        self.exit_button.configure(state='normal')
        self.set_location_editable(True)
        self.go.configure(state='normal', text='重试')
        if isinstance(error, InstallPermissionError):
            self.status.set('无法%s，请查看提示后重试。' % error.step)
        else:
            self.status.set('安装未完成，请查看提示后重试。')
        messagebox.showerror('安装失败', str(error), parent=self.root)

    def run(self):
        self.root.mainloop()


def main():
    if '--check' in sys.argv:
        path = payload_path()
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if MAIN_EXE not in names or not any(name.startswith(COMPONENTS + '/') for name in names):
                raise ValueError('安装包缺少主程序或同步组件。')
            damaged = archive.testzip()
            if damaged is not None:
                raise ValueError('安装包校验失败：%s' % damaged)
        print('payload ok: %s (%d bytes)' % (path, path.stat().st_size))
        return 0
    if '--silent' in sys.argv:
        target = default_dir()
        for i, arg in enumerate(sys.argv):
            if arg == '--dir' and i + 1 < len(sys.argv):
                target = Path(sys.argv[i + 1])
        target = target.expanduser().absolute()
        try:
            exe = install_files(payload_path(), target, register=write_registry)
        except (OSError, RuntimeError, ValueError) as error:
            print('安装未完成。%s' % error, file=sys.stderr)
            return 1
        problems = []
        try:
            shortcut(exe, start_menu_link(), target)
            shortcut(exe, desktop_link(), target)
        except Exception as exc:  # noqa: BLE001
            problems.append('shortcut: %s' % exc)
        print('installed to %s' % target)
        for item in problems:
            print('warning %s' % item)
        return 1 if problems else 0
    Installer().run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
