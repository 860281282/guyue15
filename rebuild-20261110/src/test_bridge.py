"""bridge.py / presets.py 的单测。运行：python test_bridge.py"""

import os
import tempfile

import bridge
import core
import presets

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append("%s\n  期望: %r\n  实际: %r" % (name, want, got))


def check_true(name, cond, detail=""):
    if not cond:
        FAILS.append("%s%s" % (name, ("（" + detail + "）") if detail else ""))


def make_vault():
    root = tempfile.mkdtemp(prefix="vault-")
    files = {
        "知识库/学习笔记/甲.md": "---\ntitle: 甲\nnotion_id: \"11111111111111111111111111111111\"\n---\n正文甲\n",
        "知识库/学习笔记/乙.md": "---\ntitle: 乙\n---\n没有 id 的笔记\n",
        "日常生活/丙.md": "---\nnotion_id: \"22222222222222222222222222222222\"\n---\n正文丙\n",
        ".obsidian/workspace.md": "不该被读到\n",
        ".同步工具/日志/x.md": "不该被读到\n",
    }
    for rel, text in files.items():
        path = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return root


def test_split_frontmatter():
    front, body = bridge.split_frontmatter("---\ntitle: 甲\n---\n正文\n")
    check_true("剥出 front", "title: 甲" in front)
    check("剥出正文", body, "正文\n")
    front, body = bridge.split_frontmatter("没有 front\n")
    check("无 front 时为空", front, "")
    check("无 front 时正文原样", body, "没有 front\n")


def test_hash_ignores_frontmatter():
    a = "---\ntitle: 甲\nnotion_last_edited_at: 2026-11-18T00:00:00Z\n---\n正文\n"
    b = "---\ntitle: 甲\nnotion_last_edited_at: 2026-11-19T09:00:00Z\n---\n正文\n"
    check("只改记账字段，hash 不变",
          core.digest(bridge.split_frontmatter(a)[1]),
          core.digest(bridge.split_frontmatter(b)[1]))


def test_local_notes():
    root = make_vault()
    got = bridge.local_notes(root)
    check("只读三个根目录下的 md", sorted(got), ["日常生活/丙.md", "知识库/学习笔记/乙.md", "知识库/学习笔记/甲.md"])
    check("读到 notion_id", got["知识库/学习笔记/甲.md"]["nid"], "1" * 32)
    check("没有 id 时为空", got["知识库/学习笔记/乙.md"]["nid"], None)
    check("标题回退到文件名", got["知识库/学习笔记/乙.md"]["title"], "乙")


def test_build_nodes():
    root = make_vault()
    local = bridge.local_notes(root)
    remote = {
        "1" * 32: {"rel": "知识库/学习笔记/甲.md", "title": "甲", "body": "正文甲\n", "edited": "2026-11-18T00:00:00Z"},
        "3" * 32: {"rel": "知识库/学习笔记/丁.md", "title": "丁", "body": "云端才有\n", "edited": "2026-11-18T01:00:00Z"},
    }
    state = core.State()
    state.put("1" * 32, rel="知识库/学习笔记/甲.md", base=core.digest("正文甲\n"))

    nodes = {node["nid"]: node for node in bridge.build_nodes(local, remote, state) if node["nid"]}
    check_true("对上了有 id 的笔记", "1" * 32 in nodes)
    check("内容一致的笔记 local==remote", nodes["1" * 32]["local"], nodes["1" * 32]["remote"])
    check("内容一致的笔记有基线", nodes["1" * 32]["base"], core.digest("正文甲\n"))
    check_true("云端独有的笔记也进表", "3" * 32 in nodes)
    check("云端独有：本地为空", nodes["3" * 32]["local"], None)

    only_local = [node for node in bridge.build_nodes(local, {}, core.State()) if not node["nid"]]
    check("没有 id 的笔记用临时键", only_local[0]["key"].startswith("r:"), True)


def test_renamed_and_vanished():
    root = make_vault()
    local = bridge.local_notes(root)
    state = core.State()
    state.put("1" * 32, rel="知识库/学习笔记/旧名.md", base="X")
    state.put("9" * 32, rel="知识库/学习笔记/已删除.md", base="Y")

    got = bridge.renamed(state, local)
    check("识别改名", [(nid, old, new) for nid, old, new in got],
          [("1" * 32, "知识库/学习笔记/旧名.md", "知识库/学习笔记/甲.md")])

    gone = bridge.vanished(state, local)
    check("识别消失", gone, [("9" * 32, "知识库/学习笔记/已删除.md")])


def test_presets():
    s = presets.apply({"vault": "C:\\Obsidian", "target": "x"}, presets.PERSONA_RELAY)
    check("保留旧键", s["vault"], "C:\\Obsidian")
    check("人设写入", s["persona"], "relay")
    check("中转启用账本", s["ledger"], True)
    check("中转冲突走合并", s["conflict"], "merge")
    check("标记已引导", s["onboarded"], True)

    o = presets.apply({}, presets.PERSONA_OBSIDIAN)
    check("Obsidian 为主：本地优先", o["conflict"], "local")
    check("Obsidian 为主：不用账本", o["ledger"], False)

    n = presets.apply({}, presets.PERSONA_NOTION)
    check("Notion 为主：云端优先", n["conflict"], "remote")
    check("Notion 为主：云端删除归档", n["archive_local"], True)

    check("没回答过需要引导", presets.needs_onboarding({"vault": "x"}), True)
    check("回答过不再引导", presets.needs_onboarding({"onboarded": True}), False)

    check("三个人设都有文案", len(presets.CHOICES), 3)
    check_true("摘要提醒人工核对", "冲突需人工核对" in presets.summary(presets.preset_for("relay")), presets.summary(presets.preset_for("relay")))


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
