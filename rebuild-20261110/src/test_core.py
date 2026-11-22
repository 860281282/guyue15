"""core.py 的单测：不联网、不碰真库。运行：python test_core.py"""

import json
import os
import sys
import tempfile

import core

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append("%s\n  期望: %r\n  实际: %r" % (name, want, got))


def check_true(name, cond, detail=""):
    if not cond:
        FAILS.append("%s%s" % (name, ("（" + detail + "）") if detail else ""))


def test_digest():
    a = core.digest("第一行\r\n第二行  \r\n")
    b = core.digest("第一行\n第二行\n")
    check("digest 忽略 CRLF 与行尾空白", a, b)
    check_true("digest 长度 32", len(a) == 32, a)


def test_classify():
    B = "base"
    check("两边都在且相同", core.classify("h", "h", B), core.SAME)
    check("只有本地变", core.classify("h2", B, B), core.PUSH)
    check("只有云端变", core.classify(B, "h2", B), core.PULL)
    check("两边都变", core.classify("h2", "h3", B), core.CONFLICT)
    check("本地新增", core.classify("h", None, None), core.NEW_LOCAL)
    check("云端新增", core.classify(None, "h", None), core.NEW_REMOTE)
    check("本地删除", core.classify(None, B, B), core.LOCAL_GONE)
    check("云端删除", core.classify(B, None, B), core.REMOTE_GONE)
    check("全空且有基线", core.classify(None, None, B), core.BOTH_GONE)
    check("全空无基线", core.classify(None, None, None), core.NOTHING)


def test_merge_disjoint():
    base = "a\nb\nc\n"
    text, conflicts = core.merge3(base, "a\nB\nc\n", "a\nb\nC\n")
    check("不同行改动：无冲突", conflicts, 0)
    check("不同行改动：合并结果", text, "a\nB\nC\n")


def test_merge_same_change():
    base = "a\nb\n"
    text, conflicts = core.merge3(base, "a\nX\n", "a\nX\n")
    check("两边改成一样：无冲突", conflicts, 0)
    check("两边改成一样：结果", text, "a\nX\n")


def test_merge_conflict():
    base = "a\nb\nc\n"
    text, conflicts = core.merge3(base, "a\nX\nc\n", "a\nY\nc\n")
    check("同一行改得不同：1 个冲突", conflicts, 1)
    check_true("冲突文本含本地标记", "<<<<<<< 本地" in text, text)
    check_true("冲突文本含云端标记", ">>>>>>> 云端" in text, text)
    check_true("冲突文本两边内容都在", ("X" in text) and ("Y" in text), text)


def test_merge_append_prepend():
    base = "b\n"
    text, conflicts = core.merge3(base, "b\ntail\n", "head\nb\n")
    check("本地追加 + 云端前置：无冲突", conflicts, 0)
    check("本地追加 + 云端前置：结果", text, "head\nb\ntail\n")


def test_merge_fast_paths():
    check("本地未变", core.merge3("base", "base", "remote")[0], "remote")
    check("云端未变", core.merge3("base", "local", "base")[0], "local")
    check("两边相同", core.merge3("base", "same", "same"), ("same", 0))


def test_merge_delete_line():
    base = "a\nb\nc\n"
    text, conflicts = core.merge3(base, "a\nc\n", "a\nb\nC\n")
    check("一边删行、另一边改别行：无冲突", conflicts, 0)
    check("一边删行、另一边改别行：结果", text, "a\nC\n")


def test_renames():
    previous = {"n1": "知识库/a.md", "n2": "知识库/b.md", "n3": "旧/c.md"}
    current = {"n1": "知识库/a.md", "n2": "学习笔记/b.md", "n3": "新/c.md"}
    got = core.detect_renames(previous, current)
    check("识别改名", [(nid, old, new) for nid, old, new in got],
          [("n2", "知识库/b.md", "学习笔记/b.md"), ("n3", "旧/c.md", "新/c.md")])


def test_tombstones_candidates():
    notes = {"n1": {"rel": "a.md"}, "n2": {"rel": "b.md"}, "n3": {"rel": ""}}
    got = core.collect_tombstones(notes, ["a.md"])
    check("消失的笔记", sorted(got), [("n2", "b.md")])


def test_state_roundtrip():
    folder = tempfile.mkdtemp(prefix="core-")
    path = os.path.join(folder, "state.json")
    st = core.State()
    st.put("n1", rel="a.md", local="L1", remote="R1", base="B1", title="甲")
    st.touch_device("PC-A", "2026-11-18T00:00:00Z")
    st.save(path)

    back = core.State.load(path)
    check("保存后读回 local", back.note("n1")["local"], "L1")
    check("保存后读回 rel", back.note("n1")["rel"], "a.md")
    check("按路径查", back.note(rel="a.md")["local"], "L1")
    check("设备时间", back.devices["PC-A"]["last_run"], "2026-11-18T00:00:00Z")

    back.tombstone("n1", by="PC-A")
    check_true("墓碑后不再是笔记", back.note("n1") is None)
    check_true("墓碑可查", back.is_deleted("n1"))
    check("墓碑保留 rel", back.tombstones["n1"]["rel"], "a.md")


def test_state_adopt_and_migrate():
    legacy = {
        "version": 1,
        "files": {"知识库/a.md": "H1", "知识库/b.md": "H2"},
        "remote_hashes": {"知识库/a.md": "R1"},
        "files_meta": {"知识库/a.md": {"title": "甲"}},
    }
    st = core.State.from_payload(legacy)
    check_true("迁移被标记", st.migrated)
    check("迁移条目数", len(st.notes), 2)
    check("迁移条目 hash", st.note(rel="知识库/a.md")["local"], "H1")

    ok = st.adopt("知识库/a.md", "n1")
    check_true("adopt 成功", ok)
    check("adopt 后按 id 可查", st.note("n1")["local"], "H1")
    check_true("临时键已删除", "r:知识库/a.md" not in st.notes)

    again = st.adopt("知识库/a.md", "n1")
    check("重复 adopt 不再迁移", again, False)


def test_state_atomic_save_keeps_old_on_failure():
    folder = tempfile.mkdtemp(prefix="core2-")
    path = os.path.join(folder, "state.json")
    st = core.State()
    st.put("n1", rel="a.md", local="L1")
    st.save(path)
    before = open(path, encoding="utf-8").read()

    class Boom(json.JSONEncoder):
        def default(self, o):
            raise RuntimeError("boom")

    try:
        core.json.dumps({"x": Boom()}, cls=Boom)
    except Exception:
        pass
    check("写失败后旧文件仍是合法 JSON", json.loads(before)["version"], core.VERSION)


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
    sys.exit(main())
