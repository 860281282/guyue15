"""运行日志与诊断包：发布版遇到问题时，用户导出这个包就能定位。"""
import json
import os
import platform
import re
import secrets
import sys
import threading
import traceback
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = '2.9.7'
BUILD = '2026-11-21'
LOCAL_TZ = timezone(timedelta(hours=8))
KEEP_LOGS = 30

_HELP = """咕月15 · 精简诊断包

本包包含程序和系统版本、运行结果计数、异常类型与自检状态。
运行日志、笔记名称、工作区名称、文件路径、代理地址和授权信息保留在本机。
"""


def now():
    return datetime.now(LOCAL_TZ)


def now_id():
    return now().strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(2)


def sanitize(text):
    text = str(text)
    text = re.sub(r'(?i)((?:access_token|refresh_token|id_token|client_secret|code_verifier)["\']?\s*[:=]\s*["\']?)[^"\'\s,&}]+', r'\1***', text)
    text = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}', r'\1***', text)
    text = re.sub(r'([?&](?:code|state|code_challenge)=)[^&\s]+', r'\1***', text)
    return text


def proxy_info():
    info = {'enable': None, 'server': None, 'auto_config': None}
    try:
        import winreg
        path = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            try:
                info['enable'] = bool(winreg.QueryValueEx(key, 'ProxyEnable')[0])
            except OSError:
                pass
            try:
                info['server'] = winreg.QueryValueEx(key, 'ProxyServer')[0]
            except OSError:
                pass
            try:
                info['auto_config'] = winreg.QueryValueEx(key, 'AutoConfigURL')[0]
            except OSError:
                pass
    except Exception:
        pass
    return info


def env_info(home=None, vault=None, extra=None):
    info = {
        'version': VERSION,
        'build': BUILD,
        'run_at': now().strftime('%Y-%m-%d %H:%M:%S %z'),
        'os': platform.platform(),
        'machine': platform.machine(),
        'python': sys.version.split()[0],
        'frozen': bool(getattr(sys, 'frozen', False)),
        'exe': sys.executable,
        'home': str(home) if home else None,
        'vault': str(vault) if vault else None,
        'cwd': os.getcwd(),
        'local_appdata': os.environ.get('LOCALAPPDATA'),
        'temp': os.environ.get('TEMP'),
        'lang': os.environ.get('LANG') or os.environ.get('LC_ALL'),
        'proxy': proxy_info(),
    }
    if extra:
        info.update(extra)
    if info.get('vault') is None and home:
        try:
            settings = json.loads((Path(home) / '同步工具设置.json').read_text(encoding='utf-8'))
            chosen = settings.get('vault')
            if chosen:
                path = Path(chosen)
                info['vault'] = str(path if path.is_absolute() else (Path(home) / path).resolve())
        except (OSError, ValueError, TypeError):
            pass
    return info


class Journal:
    """一次操作（同步 / 连接 / 启动）对应一个日志文件。"""

    def __init__(self, home, mode='app', vault=None, echo=None):
        self.home = Path(home)
        self.log_dir = self.home / '日志'
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            self.log_dir = Path(os.environ.get('TEMP', '.')) / 'Notion一键同步日志'
            self.log_dir.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        self.vault = vault
        self.echo = echo
        self.lines = []
        self._lock = threading.Lock()
        safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '', str(mode or '')).strip(' .') or 'app'
        safe = safe[:24]
        self.path = self.log_dir / (now_id() + '-' + safe + '.log')
        try:
            self.handle = self.path.open('a', encoding='utf-8', errors='replace', newline='\n')
        except OSError:
            self.handle = None
        self._raw('=== 咕月15 · Notion ↔ Obsidian 同步 · v' + VERSION
                  + ' / ' + str(mode) + ' ===')
        for key, value in env_info(self.home, vault).items():
            self._raw('    ' + key + ': ' + json.dumps(value, ensure_ascii=False))
        self.prune()

    def _raw(self, line):
        line = sanitize(line)
        with self._lock:
            self._append(line)

    def _append(self, line):
        self.lines.append(line)
        if len(self.lines) > 5000:
            del self.lines[:1000]
        if self.handle:
            try:
                self.handle.write(line + '\n')
                self.handle.flush()
            except (OSError, ValueError):
                self.handle = None
        if self.echo:
            try:
                self.echo(line)
            except Exception:
                pass

    def write(self, text):
        self._raw('[' + now().strftime('%H:%M:%S') + '] ' + str(text))

    def section(self, title):
        self._raw('')
        self._raw('--- ' + str(title) + ' ---')

    def error(self, exc, note=''):
        self.section('出错')
        if note:
            self._raw('    ' + note)
        self._raw('    ' + type(exc).__name__ + ': ' + str(exc))
        for frame in traceback.format_exception(type(exc), exc, exc.__traceback__):
            for line in frame.rstrip('\n').split('\n'):
                self._raw('    ' + line)
        self._raw('    日志文件: ' + str(self.path))

    def crash(self, exc, where=''):
        self.section('未捕获的异常')
        if where:
            self._raw('    位置: ' + where)
        self._raw('    ' + type(exc).__name__ + ': ' + str(exc))
        for line in traceback.format_exception(type(exc), exc, exc.__traceback__):
            for part in line.rstrip('\n').split('\n'):
                self._raw('    ' + part)

    def text(self, tail=1200):
        lines = self.lines[-tail:]
        return '\n'.join(lines)

    def close(self):
        if self.handle:
            try:
                self.handle.flush()
                self.handle.close()
            except (OSError, ValueError):
                pass
            self.handle = None

    def prune(self):
        try:
            logs = sorted(self.log_dir.glob('*.log'), key=lambda p: p.name, reverse=True)
            for old in logs[KEEP_LOGS:]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    def diagnostic_contents(self):
        environment = {'version': VERSION, 'build': BUILD, 'os': platform.system(),
                       'os_release': platform.release(), 'os_version': platform.version(),
                       'machine': platform.machine(), 'python': platform.python_version(),
                       'frozen': bool(getattr(sys, 'frozen', False))}
        files = {'说明.txt': _HELP,
                 '环境.json': json.dumps(environment, ensure_ascii=False, indent=2)}

        def read_report(path):
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return {}
            return data if isinstance(data, dict) else {}

        counts = read_report(self.home / '数据' / '最近同步.json').get('counts')
        if isinstance(counts, dict):
            keys = ('added', 'updated', 'unchanged', 'conflicts', 'retained')
            safe_counts = {key: counts[key] for key in keys if type(counts.get(key)) is int}
            files['最近同步.json'] = json.dumps({'counts': safe_counts}, indent=2)
        error = read_report(self.home / '数据' / '最近错误.json')
        error_type = error.get('type') or error.get('error_type')
        if isinstance(error_type, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', error_type):
            files['最近错误.json'] = json.dumps({'type': error_type}, indent=2)
        check = read_report(self.home / '工具检查.json')
        flags = ('ok', 'import', 'export', 'fix', 'gui', 'organizer', 'renderer', 'csv', 'encrypted_storage')
        if any(type(check.get(flag)) is bool for flag in flags):
            files['工具检查.json'] = json.dumps({flag: check[flag] for flag in flags
                                              if type(check.get(flag)) is bool}, indent=2)
        return files

    def export(self, target, contents=None):
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        contents = self.diagnostic_contents() if contents is None else contents
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in contents.items():
                archive.writestr(name, data)
        return target


def install_excepthook(make_journal):
    """未捕获异常（窗口程序里否则完全看不到）也写入日志。"""
    def hook(exc_type, exc, tb):
        try:
            journal = make_journal('crash')
            journal.crash(exc, 'sys.excepthook')
            journal.close()
        except Exception:
            pass
        try:
            sys.__excepthook__(exc_type, exc, tb)
        except Exception:
            pass
    sys.excepthook = hook
