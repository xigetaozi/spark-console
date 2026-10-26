"""收件箱状态机：把"扫到的会话"和"人的处理动作"落盘。

状态流转：
    unread ──生成草稿──> pending ──发送──> sent
       │                    │
       └────不用回──────────┴────> skipped

设计要点：扫描是幂等的——同一条预览不会把已经 sent/skipped 的会话打回 unread；
只有"预览内容变了"（对方发了新消息）才重新置为 unread。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

INBOX_PATH: Path = DATA_DIR / "inbox.json"
AUDIT_PATH: Path = DATA_DIR / "reply_audit.jsonl"
_lock = threading.Lock()

STATUS_LABEL = {
    "unread": "未读",
    "pending": "待确认",
    "sent": "已回复",
    "skipped": "不用回",
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fp(peer: str, preview: str) -> str:
    return hashlib.sha1(f"{peer}|{preview}".encode()).hexdigest()[:12]


def load() -> dict:
    try:
        data = json.loads(INBOX_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return data
    except Exception:
        pass
    return {"scanned_at": None, "scan_error": None, "items": {}}


def _save(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INBOX_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def apply_scan(result: dict) -> dict:
    """把 chat.scan_inbox() 的结果合并进收件箱。

    扫描结果已经是"推荐范围"内的会话（近 N 天 + 非群聊）。不在本轮结果里的旧条目
    会被标记为 `in_scope=False`，summary() 不再展示——否则超期的会话会永远挂在列表里。
    """
    with _lock:
        data = load()
        data["scanned_at"] = result.get("at")
        data["scan_error"] = result.get("error")
        data["scope"] = result.get("scope") or data.get("scope")
        items = data["items"]
        seen = set()

        for raw in result.get("items", []):
            peer = raw.get("name") or ""
            if not peer:
                continue
            seen.add(peer)
            preview = (raw.get("preview") or "").strip()
            fp = _fp(peer, preview)
            old = items.get(peer) or {}
            changed = old.get("fp") != fp

            item = {
                "peer": peer,
                "preview": preview,
                "time_text": raw.get("time_text") or "",
                "unread": int(raw.get("unread") or 0),
                "unread_conf": raw.get("unread_conf") or "none",
                "streak": raw.get("streak") or "",
                "last_date": raw.get("last_date"),
                "age_days": raw.get("age_days"),
                "date_source": raw.get("date_source") or "text",
                "in_scope": True,
                "exclude_reason": "",
                "fp": fp,
                "last_scan": _now(),
                "status": old.get("status", "unread"),
                "draft": old.get("draft") or [],
                "draft_source": old.get("draft_source"),
                "draft_at": old.get("draft_at"),
                "note": old.get("note") or "",
            }
            # 只有内容真的变了，才把处理过的会话重新变成未读
            if changed and item["status"] in ("sent", "skipped"):
                item["status"] = "unread"
                item["note"] = ""
            if item["unread"] > 0 and item["status"] == "skipped" and changed:
                item["status"] = "unread"
            items[peer] = item

        # 本轮没扫到的旧条目：超出时间范围 / 已转群聊 / 已删除，一律移出推荐范围
        for peer, item in items.items():
            if peer in seen:
                continue
            item["in_scope"] = False
            item["exclude_reason"] = "不在最近范围内"

        _save(data)
        return data


def touch_item(peer: str, preview: str = "", time_text: str = "", unread: int = 0,
               last_date: str | None = None, age_days: int | None = None) -> dict:
    """实时监听拿到新消息后，就地刷新这一条（不整表重扫）。"""
    with _lock:
        data = load()
        item = data["items"].get(peer)
        if not item:
            return {"ok": False, "error": "会话不存在"}
        preview = (preview or "").strip()
        new_fp = _fp(peer, preview)
        changed = item.get("fp") != new_fp
        item["preview"] = preview
        item["fp"] = new_fp
        item["in_scope"] = True
        if time_text:
            item["time_text"] = time_text
        item["unread"] = int(unread or 0)
        item["last_scan"] = _now()
        if last_date is not None:
            item["last_date"] = last_date
        if age_days is not None:
            item["age_days"] = age_days
        # 有新内容就把处理过的会话重新置为未读（同 apply_scan 的规则）
        if changed and item.get("status") in ("sent", "skipped"):
            item["status"] = "unread"
            item["note"] = ""
        _save(data)
        return {"ok": True, "item": item, "changed": changed}


def set_status(peer: str, status: str, note: str = "") -> dict:
    with _lock:
        data = load()
        item = data["items"].get(peer)
        if not item:
            return {"ok": False, "error": "会话不存在"}
        item["status"] = status
        if note:
            item["note"] = note
        if status in ("sent", "skipped"):
            item["draft"] = []
        _save(data)
        return {"ok": True, "item": item}


def set_draft(peer: str, replies: list[str], source: str = "rules", kind: str = "") -> dict:
    with _lock:
        data = load()
        item = data["items"].get(peer)
        if not item:
            return {"ok": False, "error": "会话不存在"}
        item["draft"] = replies[:3]
        item["draft_source"] = source
        item["draft_at"] = _now()
        item["draft_kind"] = kind
        if item["status"] in ("unread", "skipped"):
            item["status"] = "pending"
        _save(data)
        return {"ok": True, "item": item}


def audit(entry: dict) -> None:
    """一行一条，只追加。用于回溯"系统替我做了什么"。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        entry = dict(entry)
        entry.setdefault("at", _now())
        with AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent_audit(limit: int = 50) -> list[dict]:
    try:
        lines = AUDIT_PATH.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return list(reversed(out))


def count_sent(since_seconds: int) -> int:
    """统计最近 N 秒内自动发出的条数（用于限速）。"""
    import time
    cutoff = time.time() - since_seconds
    n = 0
    for e in recent_audit(300):
        if e.get("action") != "auto_sent":
            continue
        try:
            ts = datetime.fromisoformat(e["at"]).timestamp()
        except Exception:
            continue
        if ts >= cutoff:
            n += 1
    return n


def summary() -> dict:
    data = load()
    all_items = list(data["items"].values())
    # 只展示推荐范围（近 N 天、非群聊）内的会话；老条目保留在文件里但不参与展示
    items = [i for i in all_items if i.get("in_scope", False)]
    return {
        "scanned_at": data.get("scanned_at"),
        "scan_error": data.get("scan_error"),
        "scope": data.get("scope"),
        "total": len(items),
        "out_of_scope": len(all_items) - len(items),
        "unread": sum(1 for i in items if i.get("status") == "unread"),
        "unread_msgs": sum(1 for i in items if int(i.get("unread") or 0) > 0),
        "pending": sum(1 for i in items if i.get("status") == "pending"),
        "sent": sum(1 for i in items if i.get("status") == "sent"),
        "skipped": sum(1 for i in items if i.get("status") == "skipped"),
        "items": sorted(items, key=lambda x: (
            {"pending": 0, "unread": 1, "sent": 2, "skipped": 3}.get(x.get("status"), 4),
            x.get("age_days") if x.get("age_days") is not None else 99,
            -(x.get("unread") or 0),
        )),
    }
