import os, tempfile, time
import storage

FAILS = []

def check(name, got, want):
    if got != want:
        FAILS.append("%s\n  期望: %r\n  实际: %r" % (name, want, got))

def touch(path, size=10, age_days=0):
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"x" * size)
    when = time.time() - age_days * 86400
    os.utime(path, (when, when))
    # 目录的 mtime 才是清理时的判断依据，测试里也要一起老化
    os.utime(folder, (when, when))

def test_sizes_and_human():
    home = tempfile.mkdtemp(prefix="st-")
    touch(os.path.join(home, ".同步工具", "备份", "r1", "a.md"), 100)
    touch(os.path.join(home, ".同步工具", "冲突", "c1", "b.md"), 50)
    got = storage.sizes(home)
    check("备份体积", got["备份"], 100)
    check("冲突体积", got["冲突"], 50)
    check("没有的目录记 0", got["基线"], 0)
    check("人话单位 KB", storage.human(2048), "2.0 KB")
    check("人话单位 B", storage.human(512), "512 B")

def test_cleanup_keeps_newest_runs():
    home = tempfile.mkdtemp(prefix="st2-")
    base = os.path.join(home, ".同步工具", "备份")
    for index in range(5):
        touch(os.path.join(base, "run%d" % index, "a.md"), 100, age_days=index)
    removed, freed = storage.cleanup(home, keep_runs=2, keep_days=9999)
    check("删掉 3 轮", removed, 3)
    check("释放 300 字节", freed, 300)
    check("留下最新的两轮", sorted(os.listdir(base)), ["run0", "run1"])

def test_cleanup_drops_old_conflicts():
    home = tempfile.mkdtemp(prefix="st3-")
    base = os.path.join(home, ".同步工具", "冲突")
    touch(os.path.join(base, "old", "a.md"), 10, age_days=200)
    touch(os.path.join(base, "new", "a.md"), 10, age_days=1)
    removed, _ = storage.cleanup(home, conflict_days=90)
    check("删掉超期冲突", removed, 1)
    check("留下新冲突", os.listdir(base), ["new"])

def test_cleanup_fix_keeps_three():
    home = tempfile.mkdtemp(prefix="st4-")
    base = os.path.join(home, ".同步工具", "排版备份")
    for index in range(5):
        touch(os.path.join(base, "2026-%d" % index, "a.md"), 10, age_days=index)
    storage.cleanup(home)
    check("排版备份留 3 份", len(os.listdir(base)), 3)

def test_cleanup_never_touches_notes():
    home = tempfile.mkdtemp(prefix="st5-")
    note = os.path.join(home, "知识库", "a.md")
    touch(note, 10, age_days=999)
    storage.cleanup(home)
    check("笔记不受影响", os.path.isfile(note), True)

for name, fn in sorted(list(globals().items())):
    if name.startswith("test_") and callable(fn):
        try:
            fn()
        except Exception as exc:
            FAILS.append("%s 抛异常: %r" % (name, exc))

if FAILS:
    print("失败 %d 项：" % len(FAILS))
    for item in FAILS:
        print(" - " + item)
    raise SystemExit(1)
print("全部通过")
