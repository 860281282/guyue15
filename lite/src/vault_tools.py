"""知识库扫描。"""
import os
from pathlib import Path

SKIP_DIRS = {'.obsidian', '.同步工具', '.trash', '.git', 'node_modules', '.stfolder'}
NOTE_SUFFIXES = {'.md', '.markdown'}


def is_obsidian_program(folder):
    folder = Path(folder)
    return (folder / 'Obsidian.exe').is_file() and (folder / 'resources' / 'app.asar').is_file()


def vault_files(vault):
    """普通文件；工具目录及隐藏目录不纳入整理和快照。"""
    vault = Path(vault)
    if not vault.is_dir():
        raise OSError('知识库不存在或无法访问：' + str(vault))
    out = []
    def raise_walk_error(exc):
        raise exc
    for root, dirs, files in os.walk(vault, onerror=raise_walk_error):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')
                   and not is_obsidian_program(Path(root) / d)]
        for name in files:
            if name.startswith('.'):
                continue
            path = Path(root) / name
            rel = path.relative_to(vault).as_posix()
            out.append((rel, path))
    out.sort()
    return out


def note_files(vault):
    """库里的 Markdown 笔记。"""
    return [(rel, path) for rel, path in vault_files(vault)
            if path.suffix.lower() in NOTE_SUFFIXES]
