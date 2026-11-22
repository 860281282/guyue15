"""统一同步核心：身份、状态、三方判定、行级合并。

这一层不碰网络、不碰文件内容之外的东西，全部是纯函数 + 一个小状态类，
方便直接单测。两个方向（导入 / 导出）共用它。

身份约定
--------
一个"笔记"由 notion_id 标识（32 位十六进制，无连字符）。本地路径 `rel`
只是展示用的字段，改名、移动都不会让身份断开。

状态文件 v2
-----------
{
  "version": 2,
  "notes": {
     "<nid>": {
        "rel": "知识库/学习笔记/示例.md",
        "title": "示例",
        "local": "<本地正文 hash>",
        "remote": "<云端正文 hash>",
        "remote_edited": "2026-11-18T00:40:00.000Z",
        "base": "<上次同步时的正文 hash>",
        "updated": "2026-11-18T00:40:00.000Z"
     }
  },
  "tombstones": {"<nid>": {"rel": "...", "at": "...", "by": "..."}},
  "devices": {"PC-A": {"last_run": "..."}}
}

旧格式（`files` / `remote_hashes` 以相对路径为 key）在 `load()` 里自动迁移，
迁移后的条目先落在 `r:<rel>` 这个临时键上；一旦从 frontmatter 读到 notion_id，
调用 `adopt()` 就能把条目挂回正式身份。
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone

VERSION = 2

# —— 基本工具 —————————————————————————————————————————————


def normalize(text):
    """统一换行、去掉行尾空白差异，保证 hash 稳定。"""
    if not text:
        return ""
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n"))


def digest(text):
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:32]


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# —— 三方判定 —————————————————————————————————————————————
#
# 判定只看三个 hash：本地、云端、上次同步时的基线。返回的 action 就是
# "该做什么"，两个方向共用同一套语义，只是执行者不同。

SAME = "same"                  # 两边一致，什么都不用做
PUSH = "push"                  # 只有本地变了 → 往云端写
PULL = "pull"                  # 只有云端变了 → 往本地写
CONFLICT = "conflict"          # 两边都变了 → 需要合并或按策略选边
NEW_LOCAL = "new_local"        # 本地有、云端没有、也没基线 → 云端新建
NEW_REMOTE = "new_remote"      # 云端有、本地没有、也没基线 → 本地新建
LOCAL_GONE = "local_gone"      # 基线在、本地没了、云端没变 → 本地删除
REMOTE_GONE = "remote_gone"    # 基线在、云端没了、本地没变 → 云端删除
BOTH_GONE = "both_gone"
NOTHING = "nothing"


def classify(local, remote, base):
    """返回 action 字符串。None 表示"这一侧不存在"。"""
    if local is None and remote is None:
        return BOTH_GONE if base else NOTHING
    if base is None:
        if local is None:
            return NEW_REMOTE
        if remote is None:
            return NEW_LOCAL
        return SAME if local == remote else CONFLICT
    if remote is None:
        return REMOTE_GONE if local == base else CONFLICT
    if local is None:
        return LOCAL_GONE if remote == base else CONFLICT
    if local == remote:
        return SAME
    if local == base:
        return PULL
    if remote == base:
        return PUSH
    return CONFLICT


# —— 行级三方合并 —————————————————————————————————————————


def _lines(text):
    return normalize(text).splitlines(keepends=True)


def _edits(base, other):
    """把 base→other 的差异整理成 [(base_start, base_end, other_start, other_end)]。"""
    sm = difflib.SequenceMatcher(None, base, other, autojunk=False)
    return [(i1, i2, j1, j2) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]


def _side_text(edits, idx, base, other, start, end):
    """取 other 在 base [start, end) 区间内对应的文本。"""
    out = []
    cursor = start
    i = idx
    while i < len(edits) and edits[i][0] < end:
        s, e, js, je = edits[i]
        if s > cursor:
            out.extend(base[cursor:s])
        out.extend(other[js:je])
        cursor = e
        i += 1
    if cursor < end:
        out.extend(base[cursor:end])
    return out


def _advance(edits, idx, upto):
    while idx < len(edits) and edits[idx][0] < upto:
        idx += 1
    return idx


def merge3(base, local, remote):
    """行级三方合并。

    返回 (text, conflicts)。conflicts == 0 时 text 可直接写回；否则 text 里带
    git 风格的冲突标记（本地在前、云端在后），调用方应把它落成冲突文件而不是推上去。
    """
    base = normalize(base or "")
    local = normalize(local or "")
    remote = normalize(remote or "")
    if local == remote:
        return local, 0
    if local == base:
        return remote, 0
    if remote == base:
        return local, 0

    b, l, r = _lines(base), _lines(local), _lines(remote)
    ed_l = _edits(b, l)
    ed_r = _edits(b, r)
    out = []
    conflicts = 0
    i = ia = ib = 0

    while True:
        a = ed_l[ia] if ia < len(ed_l) else None
        c = ed_r[ib] if ib < len(ed_r) else None
        if a is None and c is None:
            out.extend(b[i:])
            break
        nxt = min(x for x in (a[0] if a else None, c[0] if c else None, len(b)) if x is not None)
        if i < nxt:
            out.extend(b[i:nxt])
            i = nxt
            continue

        if a and c and a[0] == c[0]:
            end = max(a[1], c[1])
            if end == i:
                # Both sides inserted at the same point; advance both zero-length edits.
                ltext = l[a[2]:a[3]]
                rtext = r[c[2]:c[3]]
                ia += 1
                ib += 1
                if ltext == rtext:
                    out.extend(ltext)
                else:
                    conflicts += 1
                    out.append('<<<<<<< 本地\n')
                    out.extend(ltext)
                    out.append('=======\n')
                    out.extend(rtext)
                    out.append('>>>>>>> 云端\n')
                continue
            ltext = _side_text(ed_l, ia, b, l, i, end)
            rtext = _side_text(ed_r, ib, b, r, i, end)
            ia = _advance(ed_l, ia, end)
            ib = _advance(ed_r, ib, end)
            if ltext == rtext:
                out.extend(ltext)
            else:
                conflicts += 1
                out.append("<<<<<<< 本地\n")
                out.extend(ltext)
                out.append("=======\n")
                out.extend(rtext)
                out.append(">>>>>>> 云端\n")
            i = end
            continue

        if a and a[0] == i:
            out.extend(l[a[2]:a[3]])
            i = a[1]
            ia += 1
            continue

        if c and c[0] == i:
            out.extend(r[c[2]:c[3]])
            i = c[1]
            ib += 1
            continue

        # 理论上到不了这里，保守起见把剩下的原样吐出
        out.extend(b[i:])
        break

    return "".join(out), conflicts


# —— 改名识别 ———————————————————————————————————————————


def detect_renames(previous, current):
    """previous / current 都是 {nid: rel}。返回 [(nid, old_rel, new_rel)]。"""
    out = []
    for nid, rel in current.items():
        old = previous.get(nid)
        if old and old != rel:
            out.append((nid, old, rel))
    return sorted(out, key=lambda item: item[2])


def collect_tombstones(notes_before, files_now):
    """本地这些 rel 消失了 → 返回 [(nid, rel)]，交给调用方确认后再删云端。"""
    alive = set(files_now)
    return [(nid, info["rel"]) for nid, info in notes_before.items()
            if info.get("rel") and info["rel"] not in alive]


# —— 状态 ———————————————————————————————————————————————


class State:
    """小接口：读、写、按 id 或路径查、迁移、墓碑、保存。

    复杂度都在 `load()` 的迁移和 `put()` 的字段合并里，调用方只需要
    记住「一个 notion_id 一条记录」。
    """

    def __init__(self, data=None):
        data = data or {}
        self.version = VERSION
        self.notes = dict(data.get("notes") or {})
        self.tombstones = dict(data.get("tombstones") or {})
        self.devices = dict(data.get("devices") or {})
        self.scope = data.get("scope")
        self.migrated = bool(data.get("_migrated"))

    # 读 ——
    @classmethod
    def load(cls, path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return cls()
        return cls.from_payload(raw)

    @classmethod
    def from_payload(cls, raw):
        if not isinstance(raw, dict):
            return cls()
        if raw.get("version") == VERSION and "notes" in raw:
            return cls(raw)
        return cls._migrate(raw)

    @classmethod
    def _migrate(cls, raw):
        """v1（按相对路径记 hash）→ v2（按 notion_id）。"""
        notes = {}
        legacy_files = raw.get("files") or {}
        legacy_remote = raw.get("remote_hashes") or {}
        legacy_meta = raw.get("files_meta") or {}
        for rel, local_hash in legacy_files.items():
            meta = legacy_meta.get(rel) or {}
            key = meta.get("notion_id") or ("r:" + rel)
            notes[key] = {
                "rel": rel,
                "title": meta.get("title") or "",
                "local": local_hash,
                "remote": legacy_remote.get(rel),
                "remote_edited": meta.get("remote_edited"),
                "base": local_hash,
                "updated": meta.get("updated") or "",
            }
        data = {
            "version": VERSION,
            "notes": notes,
            "tombstones": {},
            "devices": raw.get("devices") or {},
            "scope": raw.get("scope"),
            "_migrated": True,
        }
        obj = cls(data)
        obj.migrated = bool(legacy_files)
        return obj

    # 查 ——
    def note(self, nid=None, rel=None):
        if nid and nid in self.notes:
            return self.notes[nid]
        if rel:
            for info in self.notes.values():
                if info.get("rel") == rel:
                    return info
        return None

    def key_of(self, rel):
        if not rel:
            return None
        for key, info in self.notes.items():
            if info.get("rel") == rel:
                return key
        return None

    def rel_map(self):
        return {key: info.get("rel") for key, info in self.notes.items() if info.get("rel")}

    def is_deleted(self, nid):
        return nid in self.tombstones

    # 写 ——
    def put(self, key, **fields):
        info = self.notes.setdefault(key, {})
        info.update({k: v for k, v in fields.items() if v is not None})
        info["updated"] = fields.get("updated") or now_iso()
        return info

    def adopt(self, rel, nid):
        """把临时键 `r:<rel>` 升级成正式 notion_id。"""
        legacy = "r:" + rel
        if legacy in self.notes and nid not in self.notes:
            info = self.notes.pop(legacy)
            info["rel"] = rel
            self.notes[nid] = info
            return True
        return False

    def drop(self, key):
        self.notes.pop(key, None)

    def tombstone(self, key, rel=None, by=""):
        self.tombstones[key] = {"rel": rel or (self.notes.get(key) or {}).get("rel"), "at": now_iso(), "by": by}
        self.drop(key)

    def touch_device(self, name, when=None):
        self.devices[name] = {"last_run": when or now_iso()}

    # 存 ——
    def payload(self):
        return {
            "version": VERSION,
            "notes": self.notes,
            "tombstones": self.tombstones,
            "devices": self.devices,
            "scope": self.scope,
        }

    def save(self, path):
        folder = os.path.dirname(os.path.abspath(path))
        if folder:
            os.makedirs(folder, exist_ok=True)
        data = json.dumps(self.payload(), ensure_ascii=False, indent=1, sort_keys=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=folder or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(data + "\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return path
