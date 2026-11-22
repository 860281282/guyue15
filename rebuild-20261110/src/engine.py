"""一键同步：按"使用方式"决定顺序，把两个方向串成一次操作。

三种使用方式（首次启动时选，之后可改）：
    obsidian  Obsidian 为主，Notion 当云中转     → 先拉后推，两边一起动
    relay     多台设备经 Notion 中转              → 先拉后推，账本记账、删除走墓碑
    notion    Notion 为主，Obsidian 当本地备份    → 只拉，不动云端

这一层只做编排，具体动作全部复用已经验证过的引擎：
    下载 = app.sync()（三方比对 + 覆盖前备份 + 冲突留档）
    上传 = app_out.push_folders()（按路径对应、认不出就新建、不覆盖别人改过的）
"""

from __future__ import annotations

import presets

ORDER = {
    'obsidian': ('pull', 'push'),
    'relay': ('pull', 'push'),
    'notion': ('pull',),
}

STEP_TEXT = {'pull': '从 Notion 下载', 'push': '上传到 Notion'}


def steps(persona):
    return ORDER.get(persona or 'obsidian', ORDER['obsidian'])


def describe(persona):
    return ' / '.join(STEP_TEXT[step] for step in steps(persona))


def cycle(vault, persona, settings, log, cancel, home=None, scope=None,
          folders=None, anchor='', policy=None, dry_run=False):
    """跑一整轮。返回 {'steps': [...], 'persona': 人设}。"""

    import app as A
    import app_out as O

    home = home or A.HOME
    settings = settings or {}
    persona = persona if persona in ORDER else 'obsidian'
    policy = policy or settings.get('policy') or 'skip'
    folders = folders if folders is not None else (settings.get('folders') or [])
    anchor = '' if settings.get('flat', True) else (anchor or '')
    results = {'persona': persona, 'steps': [], 'order': list(steps(persona))}
    if dry_run:
        # 预览只跑上传侧的清单（下载会真的写本地文件），并说明清楚
        results['order'] = [step for step in results['order'] if step == 'push']
        if not results['order']:
            log('这种使用方式只读云端，预览不改动任何文件；直接点「一键同步」即可。')
            return results

    if 'push' in results['order'] and not dry_run:
        import write_guard
        write_guard.check(vault)
    if 'pull' in results['order']:
        if cancel.is_set():
            raise AL['cancel']
        log('① ' + STEP_TEXT['pull'])
        results['pull'] = A.sync(vault, log, cancel, home=home, scope=scope,
                                repair=bool(settings.get('repair_after_import', True)))
        results['steps'].append('pull')
        if cancel.is_set():
            raise AL['cancel']

    if 'push' in results['order']:
        log('② ' + STEP_TEXT['push'])
        if dry_run:
            log('（预览模式：只出清单，不改 Notion）')
        results['push'] = O.push_folders(
            vault, folders, anchor, policy, log, cancel, home=home, dry_run=dry_run,
            attrs=bool(settings.get('write_attributes')),
            ledger_on=bool(settings.get('ledger')) and persona == 'relay')
        results['steps'].append('push')

    return results


class _Abort(Exception):
    pass


from notion_connection import SyncError  # noqa: E402  （放后面，避免循环导入）

AL = {'cancel': SyncError('已取消，本次没有改完所有内容')}
