"""planner.py 的单测。运行：python test_planner.py"""

import planner
import core

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append("%s\n  期望: %r\n  实际: %r" % (name, want, got))


def node(nid, rel, local, remote, base):
    return {"nid": nid, "rel": rel, "local": local, "remote": remote, "base": base}


def test_persona_table():
    nodes = [
        node("n1", "a.md", "L", "R", "B"),     # 两边都改 → conflict
        node("n2", "b.md", "L", None, None),   # 新本地
        node("n3", "c.md", None, "B", "B"),    # 本地删除（云端没变）
        node("n4", "d.md", "B", None, "B"),    # 云端删除（本地没变）
        node("n5", "e.md", "H", "H", "H"),     # 一致
    ]

    got = {item["nid"]: item["action"] for item in planner.actions(nodes, planner.OBSIDIAN)}
    check("Obsidian 为主：冲突推上去", got["n1"], "push")
    check("Obsidian 为主：新本地推上去", got["n2"], "create_remote")
    check("Obsidian 为主：本地删除要删云端", got["n3"], "trash_remote")
    check("Obsidian 为主：云端删除重建", got["n4"], "push")
    check("Obsidian 为主：一致不动", got["n5"], "skip")

    got = {item["nid"]: item["action"] for item in planner.actions(nodes, planner.NOTION)}
    check("Notion 为主：冲突拉下来", got["n1"], "pull")
    check("Notion 为主：本地删除拉回来", got["n3"], "pull")
    check("Notion 为主：云端删除归档本地", got["n4"], "archive_local")

    got = {item["nid"]: item["action"] for item in planner.actions(nodes, planner.RELAY)}
    check("中转：冲突合并", got["n1"], "merge")
    check("中转：删除要确认", got["n3"], "trash_remote")
    check("中转：云端删除归档", got["n4"], "archive_local")


def test_confirm_and_backup_flags():
    nodes = [node("n1", "a.md", None, "B", "B")]
    item = planner.actions(nodes, planner.RELAY)[0]
    check("删除动作需要确认", item["confirm"], True)

    nodes = [node("n1", "a.md", "L2", "R", "B")]
    item = planner.actions(nodes, planner.OBSIDIAN)[0]
    check("冲突推上去要备份", item["backup"], True)


def test_sorted_and_summary():
    nodes = [node("n2", "b.md", "L", None, None), node("n1", "a.md", None, "R", None)]
    planned = planner.actions(nodes, planner.RELAY)
    check("按路径排序", [item["rel"] for item in planned], ["a.md", "b.md"])
    check("统计", planner.summarize(planned), {"create_local": 1, "create_remote": 1})
    text = planner.human_summary(planned)
    check("人话摘要含两项", ("云端新建 1" in text) and ("本地新建 1" in text), True)


def test_unknown_persona():
    try:
        planner.actions([], "whatever")
    except ValueError as exc:
        check("未知人设报错", "未知人设" in str(exc), True)
    else:
        FAILS.append("未知人设没有报错")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                FAILS.append("%s 抛异常: %r" % (name, exc))
    if FAILS:
        print("失败 %d 项：" % len(FAILS))
        for item in FAILS:
            print(" - " + item)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
