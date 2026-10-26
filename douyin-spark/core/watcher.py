"""实时监听：持续发现新消息 → 归档 → 生成建议/自动回复。

为什么需要它：之前的实现只有"手动生成建议"和"每 N 分钟跑一轮"两种，
两次之间对方发来的消息完全不会被看到——等于丢消息。

工作方式（每 interval_sec 一轮）：
1. `chat.peek_list()` 只读会话列表前若干行（不点击、不滚动，开销很小）；
2. 找出"有新消息"的会话：未读角标 > 0，或列表预览这个表情/文字在归档里没见过；
3. 对这些会话**逐个** open_and_read（这是唯一有风控成本的动作），
   读到的消息全部并入 msgstore 归档（去重），所以聊天记录会持续累积；
4. 依据模式处理：半自动只准备草稿，全自动走原有 9 道闸门后发送；
5. 每轮之间 sleep，可用 stop 事件立即打断。

设计约束：
- 只有明确开启（enabled）才会启动，默认关闭；
- 每轮最多打开 max_peers_per_cycle 个会话，且两个会话之间至少间隔 min_open_gap_sec；
- 群聊跳过（收件箱范围本来也排除群聊）；
- 任何异常只记录，不中断循环。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime

from . import autoreply, chat, inbox, msgstore

logger = logging.getLogger("douyin-spark")

DEFAULTS = {
    "enabled": False,
    "interval_sec": 30,          # 轮询间隔
    "max_peers_per_cycle": 2,    # 每轮最多打开几个会话
    "min_open_gap_sec": 4,       # 两次打开之间的最小间隔
    "autodraft": True,           # 半自动模式下自动准备草稿
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_cfg() -> dict:
    cfg = dict(DEFAULTS)
    try:
        from .config import load_config
        cfg.update((load_config().get("reply") or {}).get("watch") or {})
    except Exception:
        pass
    try:
        cfg["interval_sec"] = max(10, int(cfg.get("interval_sec") or 30))
        cfg["max_peers_per_cycle"] = max(1, min(5, int(cfg.get("max_peers_per_cycle") or 2)))
        cfg["min_open_gap_sec"] = max(0, int(cfg.get("min_open_gap_sec") or 4))
    except Exception:
        pass
    cfg["enabled"] = bool(cfg.get("enabled"))
    cfg["autodraft"] = bool(cfg.get("autodraft", True))
    return cfg


def save_cfg(patch: dict) -> dict:
    """把 watch 配置写回 config.json（只合并，不动其它字段）。"""
    from .config import load_config, save_config
    cur = get_cfg()
    for k, v in (patch or {}).items():
        if k in DEFAULTS:
            cur[k] = v
    base = load_config()
    reply = dict(base.get("reply") or {})
    reply["watch"] = cur
    out = dict(base)
    out["reply"] = reply
    save_config(out)
    return get_cfg()


class _State:
    def __init__(self) -> None:
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.busy = False
        self.cycles = 0
        self.last_cycle_at: str | None = None
        self.last_new = 0
        self.last_peers: list[str] = []
        self.last_error = ""
        self.started_at: str | None = None


_state = _State()
_guard = threading.Lock()


# --------------------------------------------------------------------------- 单轮

def _candidates(rows: list[dict]) -> list[dict]:
    """挑出"有新消息"的会话。

    判定：未读角标 > 0，或列表预览与上次记录的不一样。
    不用"归档里没见过"来做判断——那会把所有历史会话都当成新消息，
    一轮轮全打开一遍，等于自己做了一次全量扫描（风控上很糟）。

    占位预览（[视频]/[语音]/[表情]）无法比对内容，只能靠未读数。
    """
    known = {i["peer"]: i for i in (inbox.summary().get("items") or [])}
    out = []
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name or row.get("is_group"):
            continue
        preview = (row.get("preview") or "").strip()
        # 「分享[视频]」这种也是占位预览，不能拿它当内容锚点
        placeholder = bool(re.search(r"\[(视频|语音|表情|图片|直播|音乐|商品|位置)\]", preview))
        unread = int(row.get("unread") or 0)
        old_preview = ((known.get(name) or {}).get("preview") or "").strip()
        changed = bool(preview) and not placeholder and preview != old_preview
        if unread > 0 or changed:
            out.append(row)
    return out


def _handle_peer(row: dict, wcfg: dict) -> dict:
    """打开一个会话：归档消息 → 按模式生成/发送。"""
    name = row["name"]
    preview = (row.get("preview") or "").strip()
    rcfg = autoreply.get_cfg()

    got = chat.open_and_read(name, limit=int(rcfg.get("read_limit") or 8), expect=preview)
    msgs = got.get("items") or []
    if not msgs:
        return {"peer": name, "error": got.get("error") or "没读到消息", "new": 0}

    added = msgstore.append(name, msgs)
    inbox.touch_item(name, preview=preview, time_text=row.get("time_text") or "",
                     unread=int(row.get("unread") or 0),
                     last_date=None, age_days=None)

    res = {"peer": name, "new": added["new"], "error": ""}
    if not added["new"]:
        return res        # 没有真正的新消息，不重复生成

    mode = rcfg.get("mode") or "off"
    if mode == "off":
        return res
    if not wcfg.get("autodraft", True):
        return res

    drafted = autoreply.draft_for(name, rcfg, pre_got={"items": msgs, "conf": got.get("conf")},
                                  preview=preview)
    if not drafted.get("ok"):
        res["error"] = drafted.get("error") or "生成建议失败"
        return res
    res["draft"] = (drafted.get("replies") or [])[:2]

    if mode == "auto":
        res["auto"] = autoreply.try_auto_send(name, drafted, rcfg)
    return res


def run_cycle(force: bool = False) -> dict:
    """跑一轮监听。也可从接口手动触发一次。"""
    cfg = get_cfg()
    if not cfg.get("enabled") and not force:
        return {"skipped": "监听未开启", "at": _now()}

    t0 = time.time()
    report = {"at": _now(), "candidates": 0, "opened": 0, "new_msgs": 0,
              "peers": [], "errors": [], "elapsed": 0.0}

    peek = chat.peek_list()
    if peek.get("error"):
        report["errors"].append(peek["error"])
        _state.last_error = str(peek["error"])[:160]
        return report

    cands = _candidates(peek.get("items") or [])
    report["candidates"] = len(cands)

    limit = int(cfg.get("max_peers_per_cycle") or 2)
    gap = float(cfg.get("min_open_gap_sec") or 4)
    for row in cands[:limit]:
        if _state.stop.is_set():
            break
        try:
            r = _handle_peer(row, cfg)
        except Exception as e:
            report["errors"].append(f"{row.get('name')}: {str(e)[:100]}")
            continue
        report["opened"] += 1
        report["peers"].append(row.get("name"))
        report["new_msgs"] += int(r.get("new") or 0)
        if r.get("error"):
            report["errors"].append(f"{row.get('name')}: {r['error']}")
        time.sleep(gap)

    report["elapsed"] = round(time.time() - t0, 1)
    _state.last_cycle_at = report["at"]
    _state.last_new = report["new_msgs"]
    _state.last_peers = report["peers"]
    if report["errors"]:
        _state.last_error = report["errors"][0][:160]
    logger.info("实时监听一轮：候选 %s，打开 %s 个，新消息 %s 条，%.1fs",
                report["candidates"], report["opened"], report["new_msgs"], report["elapsed"])
    return report


# --------------------------------------------------------------------------- 启停

def _loop() -> None:
    logger.info("实时监听已启动")
    while not _state.stop.is_set():
        cfg = get_cfg()
        if cfg.get("enabled"):
            _state.busy = True
            try:
                run_cycle(force=True)
                _state.cycles += 1
            except Exception as e:
                _state.last_error = str(e)[:160]
                logger.warning("实时监听异常：%s", str(e)[:140])
            finally:
                _state.busy = False
        # 可被打断的等待：stop 或 wake 任一触发就立刻继续
        _state.wake.wait(timeout=max(10, int(get_cfg().get("interval_sec") or 30)))
        _state.wake.clear()
    logger.info("实时监听已停止")


def start() -> dict:
    with _guard:
        if _state.thread and _state.thread.is_alive():
            return status()
        _state.stop.clear()
        _state.thread = threading.Thread(target=_loop, daemon=True, name="chat-watcher")
        _state.thread.start()
        _state.started_at = _now()
    return status()


def stop() -> dict:
    with _guard:
        _state.stop.set()
        _state.wake.set()
    return status()


def status() -> dict:
    cfg = get_cfg()
    return {
        "enabled": cfg.get("enabled"),
        "running": bool(_state.thread and _state.thread.is_alive()),
        "busy": _state.busy,
        "interval_sec": cfg.get("interval_sec"),
        "max_peers_per_cycle": cfg.get("max_peers_per_cycle"),
        "autodraft": cfg.get("autodraft"),
        "cycles": _state.cycles,
        "last_cycle_at": _state.last_cycle_at,
        "last_new": _state.last_new,
        "last_peers": _state.last_peers,
        "last_error": _state.last_error,
        "started_at": _state.started_at,
    }


def kick() -> dict:
    """立刻跑一轮（不等待下一个间隔）。"""
    _state.wake.set()
    return status()
