"""消息归档：把每次读到的消息**累积**落盘，而不是读完就丢。

为什么需要它：之前的"生成建议"只是临时读一个窗口，读完即弃。
所以既看不到历史，也无法判断"哪些是新消息"——两次读取之间的消息等于丢了。

存储：JSONL，一行一条，只追加。
去重键 = (peer, dir, text) 的指纹；同一条消息重复读到不会重复写。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

MSG_PATH: Path = DATA_DIR / "messages.jsonl"
_lock = threading.Lock()
_seen: set[str] | None = None          # 指纹集合，首次使用时从磁盘加载


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fp(peer: str, direction: str, text: str, tkey: str = "") -> str:
    """消息指纹。

    必须带时间分量：只用 peer|direction|text 的话，同一个人隔几天再发一句
    "在吗"，会被判成重复而永久丢弃，导致不归档、不自动回复。
    优先用消息自带时间；没有就退化为"天"粒度（同一天内重复读到仍能去重）。
    """
    raw = f"{peer}|{direction}|{text}|{tkey}".encode("utf-8", "ignore")
    return hashlib.sha1(raw).hexdigest()[:16]


def _load_seen() -> set[str]:
    global _seen
    if _seen is not None:
        return _seen
    _seen = set()
    try:
        with MSG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    _seen.add(json.loads(line)["fp"])
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return _seen


def append(peer: str, items: list[dict], at: str | None = None) -> dict:
    """把一次读到的消息并入归档。返回新增了多少条、以及新增的明细。"""
    added: list[dict] = []
    stamp = at or _now()

    with _lock:
        # 必须在锁内取快照：放在锁外的话两个线程会各自读到旧集合，
        # 都认为消息是新的 → 重复写盘，且 _seen 互相覆盖丢条目。
        seen = _load_seen()
        try:
            MSG_PATH.parent.mkdir(parents=True, exist_ok=True)
            # 超过 8MB 就轮转一份，防止 recent() 全量读越来越慢
            try:
                if MSG_PATH.exists() and MSG_PATH.stat().st_size > 8 * 1024 * 1024:
                    bak = MSG_PATH.with_suffix(".jsonl.1")
                    if bak.exists():
                        bak.unlink()
                    MSG_PATH.rename(bak)
                    global _seen
                    _seen = set()
            except Exception:
                pass
            with MSG_PATH.open("a", encoding="utf-8") as f:
                for m in items or []:
                    text = (m.get("text") or "").strip()
                    if not text:
                        continue
                    direction = m.get("dir") or "in"
                    tkey = str(m.get("time") or m.get("time_text") or m.get("ts") or stamp[:10])
                    fp = _fp(peer, direction, text, tkey)
                    if fp in seen:
                        continue
                    seen.add(fp)
                    row = {
                        "peer": peer,
                        "fp": fp,
                        "dir": direction,
                        "kind": m.get("kind") or "text",
                        "text": text[:300],
                        "at": stamp,
                    }
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    added.append(row)
        except Exception:
            pass

    return {"new": len(added), "added": added}


def recent(peer: str, limit: int = 100) -> list[dict]:
    """按时间正序返回某个会话的归档消息（最新的在最后）。"""
    out: list[dict] = []
    try:
        with MSG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("peer") == peer:
                    out.append(row)
    except FileNotFoundError:
        return []
    except Exception:
        return out
    return out[-limit:]


def last_inbound(peer: str) -> dict | None:
    for row in reversed(recent(peer, 200)):
        if row.get("dir") == "in":
            return row
    return None


def stats() -> dict:
    n = 0
    peers = set()
    size = 0
    try:
        size = MSG_PATH.stat().st_size
        with MSG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                n += 1
                try:
                    peers.add(json.loads(line).get("peer"))
                except Exception:
                    continue
    except Exception:
        pass
    return {"messages": n, "peers": len(peers), "bytes": size,
            "path": str(MSG_PATH)}


def reset_cache() -> None:
    """测试/排查时用：下次调用重新从磁盘加载指纹。"""
    global _seen
    _seen = None
