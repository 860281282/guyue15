"""Notion 侧「同步账本」：多设备之间唯一共享的事实。

为什么需要它
------------
每台设备的 `.同步工具/数据/*.json` 只描述自己。设备 B 看到"本地有、云端没有"
时，无法区分两种情况：设备 A 刚删了它，还是设备 A 还没推上来。
把这份事实放进 Notion（账本页），两台设备读同一份，删除、改名、归属才有依据。

存储形态
--------
一个普通 Notion 页面，正文是一串 ```json 代码块（Notion 单块文本上限约 2000 字，
所以要切块，读取时按顺序拼回去）。内容：

{
  "rev": 12,                       # 乐观锁版本号，每次写 +1
  "devices": {"PC-A": {"last_run": "..."}},
  "notes": {
     "<nid>": {"rel": "...", "hash": "...", "deleted": false,
               "deleted_at": null, "deleted_by": null, "updated": "..."}
  }
}

写入规则：写前重读一次，`rev` 变了就合并重算（最多 3 次）；账本本身在导入时
跳过，不进知识库。
"""

from __future__ import annotations

import json

TITLE = "同步账本"
CHUNK = 1500          # 每块字符数，留足余量
MAX_RETRY = 3


def _fence(text, payload):
    return "```json\n" + payload + "\n```"


def split_payload(data, chunk=CHUNK):
    """把 JSON 切成若干块，保证每块都是完整的行边界。"""
    raw = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)
    lines = raw.split("\n")
    blocks, current, size = [], [], 0
    for line in lines:
        if size + len(line) + 1 > chunk and current:
            blocks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        blocks.append("\n".join(current))
    return blocks


def join_payload(text):
    """从页面正文里把所有 ```json 块按顺序拼回来。"""
    chunks, inside, buf = [], False, []
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if stripped.startswith("```"):
            if inside:
                chunks.append("\n".join(buf))
                inside, buf = False, []
            else:
                inside, buf = True, []
            continue
        if inside:
            buf.append(line)
    if not inside and buf:
        chunks.append("\n".join(buf))
    return "\n".join(chunks)


def render(data):
    blocks = split_payload(data)
    body = ["# 同步账本（自动生成，请勿手动编辑）", ""]
    body.append("这个页面记录各台设备同步过的笔记，用于识别删除与改名。删除它会导致删除无法在设备间传播。")
    body.append("")
    for block in blocks:
        body.append(_fence("json", block))
        body.append("")
    return "\n".join(body)


class Ledger:
    """小接口：locate / load / save。网络细节全在 client 后面。"""

    def __init__(self, client, title=TITLE):
        self.client = client
        self.title = title
        self.page_id = None

    # 找 ——
    def locate(self, create=False, parent_id=None):
        if self.page_id:
            return self.page_id
        try:
            found = self.client.call("search", query=self.title)
        except Exception:  # noqa: BLE001 —— 搜不到就当没有
            found = None
        for item in (found or {}).get("results", []) if isinstance(found, dict) else []:
            if (item.get("title") or "").strip() == self.title:
                self.page_id = (item.get("id") or "").replace("-", "")
                return self.page_id
        if create:
            self.page_id = self._create(parent_id)
        return self.page_id

    def _create(self, parent_id):
        args = {
            "pages": [{"properties": {"title": self.title}, "content": render({"rev": 0, "devices": {}, "notes": {}})}],
            "allow_async": False,
        }
        if parent_id:
            args["parent"] = {"page_id": parent_id, "type": "page_id"}
        else:
            args["creation_mode"] = "draft"
        result = self.client.write("create-pages", **args)
        page = (result or {}).get("pages", [{}])[0]
        return (page.get("id") or "").replace("-", "")

    # 读 ——
    def load(self):
        pid = self.locate()
        if not pid:
            return {"rev": 0, "devices": {}, "notes": {}}
        page = self.client.call("fetch", id=pid, include_file_urls=False, include_transcript=False)
        raw = join_payload(page.get("text", ""))
        if not raw.strip():
            return {"rev": 0, "devices": {}, "notes": {}}
        try:
            data = json.loads(raw)
        except ValueError:
            return {"rev": 0, "devices": {}, "notes": {}}
        data.setdefault("rev", 0)
        data.setdefault("devices", {})
        data.setdefault("notes", {})
        return data

    # 合并 ——
    @staticmethod
    def merge(local, remote, device=""):
        """以 updated 较新的为准；删除一旦记录就保留（墓碑不复活）。"""
        out = {
            "rev": max(int(remote.get("rev") or 0), int(local.get("rev") or 0)),
            "devices": dict(remote.get("devices") or {}),
            "notes": dict(remote.get("notes") or {}),
        }
        out["devices"].update(local.get("devices") or {})
        if device:
            out["devices"][device] = dict((local.get("devices") or {}).get(device) or {})

        for nid, mine in (local.get("notes") or {}).items():
            theirs = out["notes"].get(nid) or {}
            if not theirs:
                out["notes"][nid] = mine
                continue
            if theirs.get("deleted") and not mine.get("deleted"):
                continue                      # 墓碑优先，别把已删的复活
            if mine.get("deleted") and not theirs.get("deleted"):
                out["notes"][nid] = mine
                continue
            if str(mine.get("updated") or "") >= str(theirs.get("updated") or ""):
                out["notes"][nid] = mine
        return out

    # 写 ——
    def save(self, data, device=""):
        pid = self.locate(create=True)
        for _ in range(MAX_RETRY):
            current = self.load()
            merged = self.merge(data, current, device)
            merged["rev"] = int(current.get("rev") or 0) + 1
            self.client.write("update-page", page_id=pid, command="replace_content",
                             new_str=render(merged) + "\n")
            again = self.load()
            if int(again.get("rev") or 0) >= int(merged["rev"]):
                return merged
        raise RuntimeError("同步账本被其他设备连续修改，请稍后重试")
