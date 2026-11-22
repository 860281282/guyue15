"""ledger.py 的单测：用假 client，不联网。运行：python test_ledger.py"""

import json

import ledger

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append("%s\n  期望: %r\n  实际: %r" % (name, want, got))


def check_true(name, cond, detail=""):
    if not cond:
        FAILS.append("%s%s" % (name, ("（" + detail + "）") if detail else ""))


class Fake:
    """够用的假 MCP：一个页面、search/create/fetch/update-page。"""

    def __init__(self):
        self.pages = {}
        self.calls = []

    def call(self, name, **args):
        self.calls.append(("call", name, args))
        if name == "search":
            query = args.get("query", "")
            items = [{"id": pid.replace("-", ""), "title": p["title"]}
                     for pid, p in self.pages.items() if query and query in p["title"]]
            return {"results": items}
        if name == "fetch":
            pid = (args.get("id") or "").replace("-", "")
            if pid not in self.pages:
                raise RuntimeError("object_not_found")
            return {"text": self.pages[pid]["text"], "title": self.pages[pid]["title"]}
        raise AssertionError("unexpected call %s" % name)

    def write(self, name, **args):
        self.calls.append(("write", name, args))
        if name == "create-pages":
            pid = "p%d" % (len(self.pages) + 1)
            page = (args.get("pages") or [{}])[0]
            self.pages[pid] = {"title": page["properties"]["title"], "text": page.get("content", "")}
            return {"pages": [{"id": pid, "url": "https://app.notion.com/p/" + pid}]}
        if name == "update-page":
            pid = (args.get("page_id") or "").replace("-", "")
            self.pages[pid]["text"] = args.get("new_str", args.get("content", ""))
            return {"ok": True}
        raise AssertionError("unexpected write %s" % name)


def sample(nid="n1", rel="a.md", deleted=False, updated="2026-11-18T00:00:00Z"):
    return {"rel": rel, "hash": "H", "deleted": deleted, "deleted_at": None,
            "deleted_by": None, "updated": updated}


def test_split_join_roundtrip():
    data = {"rev": 3, "devices": {"PC-A": {"last_run": "x"}},
            "notes": {"n%d" % i: sample("n%d" % i, "笔记%d.md" % i) for i in range(200)}}
    blocks = ledger.split_payload(data)
    check_true("切成了多块", len(blocks) > 1, str(len(blocks)))
    check_true("每块不超限", all(len(b) <= ledger.CHUNK for b in blocks),
               str(max(len(b) for b in blocks)))
    text = ledger.render(data)
    back = json.loads(ledger.join_payload(text))
    check("切块拼回一致", back, data)


def test_create_and_load():
    fake = Fake()
    book = ledger.Ledger(fake)
    check("初始找不到", book.locate(), None)
    pid = book.locate(create=True)
    check_true("创建了页面", bool(pid), str(pid))
    data = book.load()
    check("新账本 rev 为 0", data["rev"], 0)
    check("新账本没有笔记", data["notes"], {})


def test_save_bumps_rev_and_persists():
    fake = Fake()
    book = ledger.Ledger(fake)
    book.locate(create=True)
    payload = {"rev": 0, "devices": {}, "notes": {"n1": sample()}}
    merged = book.save(payload, device="PC-A")
    check("写后 rev=1", merged["rev"], 1)
    check("笔记落盘", book.load()["notes"]["n1"]["rel"], "a.md")
    check("设备登记", "PC-A" in book.load()["devices"], True)

    merged = book.save(payload, device="PC-B")
    check("再写 rev=2", merged["rev"], 2)


def test_merge_prefers_newer_and_keeps_tombstone():
    newer = sample(updated="2026-11-18T02:00:00Z")
    older = sample(updated="2026-11-18T01:00:00Z")
    merged = ledger.Ledger.merge({"notes": {"n1": newer}}, {"notes": {"n1": older}})
    check("较新的赢", merged["notes"]["n1"]["updated"], "2026-11-18T02:00:00Z")

    tomb = sample(deleted=True, updated="2026-11-18T01:00:00Z")
    alive = sample(deleted=False, updated="2026-11-18T05:00:00Z")
    merged = ledger.Ledger.merge({"notes": {"n1": alive}}, {"notes": {"n1": tomb}})
    check("墓碑不被较新的存活覆盖", merged["notes"]["n1"]["deleted"], True)

    merged = ledger.Ledger.merge({"notes": {"n2": tomb}}, {"notes": {}})
    check("本地墓碑写进去", merged["notes"]["n2"]["deleted"], True)


def test_corrupt_page_is_tolerated():
    fake = Fake()
    book = ledger.Ledger(fake)
    pid = book.locate(create=True)
    fake.pages[pid]["text"] = "这不是 JSON"
    data = book.load()
    check("坏内容回退成空账本", data["notes"], {})


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
