"""发送历史与火花热力图数据。

设计取舍：
- **按天聚合**而不是逐轮追加。面板要画「一年 365 格」，逐条日志既画不出来也太大；
  聚合成 {日期: {好友: 0/1}} 后，文件大小只与天数有关，一年也就 365 个键。
- **干跑不记账**。干跑没有真发出去，记进去会让热力图变成「每天都满」，失去意义。
- **同一天多轮合并**：手动补发 + 定时任务跑两轮，只要成功过一次就算当天续上。
  所以好友位用 `1` 覆盖 `0`，而不是后写覆盖前写。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import DATA_DIR

HISTORY_PATH = DATA_DIR / "send_history.json"
# 一年 365 天 + 对齐整周的余量；再留一段让用户能回看更久
KEEP_DAYS = 400
FRIEND_WINDOW = 90

_lock = threading.RLock()


def _empty() -> dict:
    return {"version": 1, "days": {}}


def _load() -> dict:
    if not HISTORY_PATH.exists():
        return _empty()
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except Exception:
        # 半写/手工改坏时退回空表，不让面板整体 500
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
        return _empty()
    return data


def _save(data: dict) -> None:
    """先写临时文件再原子替换，避免并发读拿到半截 JSON。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY_PATH.with_name(HISTORY_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, HISTORY_PATH)


def _today() -> str:
    return date.today().isoformat()


def _names(values) -> list[str]:
    """过滤掉系统占位与空值。"""
    out = []
    for v in values or []:
        name = v if isinstance(v, str) else str(v or "")
        name = name.strip()
        if name and name != "_system":
            out.append(name)
    return out


def record(result: dict) -> bool:
    """把一轮发送结果记进当天。返回是否真的写了盘。"""
    if not isinstance(result, dict):
        return False
    # 干跑没有真实发送，不参与统计
    if result.get("dry_run"):
        return False

    ok_names = _names(result.get("ok"))
    failed_names = _names([f.get("name") for f in (result.get("failed") or [])
                           if isinstance(f, dict)])
    if not ok_names and not failed_names:
        # 只报了 _system（登录态缺失、页面打不开）→ 当天不算"发过"
        return False

    day = _today()
    with _lock:
        data = _load()
        cur = data["days"].setdefault(day, {"friends": {}, "runs": 0})
        cur["runs"] = int(cur.get("runs") or 0) + 1
        friends = cur.setdefault("friends", {})
        for name in failed_names:
            friends.setdefault(name, 0)
        # 成功位无条件置 1：同一天先失败后补发成功，应当算续上
        for name in ok_names:
            friends[name] = 1
        _prune(data)
        _save(data)
    return True


def _prune(data: dict) -> None:
    days = data.get("days") or {}
    if len(days) <= KEEP_DAYS:
        return
    keep = sorted(days.keys())[-KEEP_DAYS:]
    data["days"] = {k: days[k] for k in keep}


def _level(ok: int, failed: int) -> int:
    """0=没续上；1=只续上一小部分；2、3=部分失败；4=全部成功。

    注意 ok=0 一律返回 0：一轮跑下来全员失败，性质上等于「今天没续上」，
    不能因为"有过尝试"就被算进连续天数——否则连续天数会虚高。
    「尝试过但失败」用单独的 tried 字段表达，前端可另行标记。

    用「成功占当天处理量的比例」而不是绝对人数：好友名单一年里会变，
    绝对人数会让 8 月和 12 月的颜色没有可比性。
    """
    if ok <= 0:
        return 0
    total = ok + failed
    if failed <= 0:
        return 4
    ratio = ok / total
    if ratio >= 0.8:
        return 3
    if ratio >= 0.5:
        return 2
    return 1


def _streak(flags: list[bool], include_today: bool = True) -> int:
    """从末尾往前数连续 True 的个数。"""
    n = 0
    end = len(flags) if include_today else len(flags) - 1
    for i in range(end - 1, -1, -1):
        if flags[i]:
            n += 1
        else:
            break
    return n


def _best_streak(flags: list[bool]) -> int:
    best = run = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return best


def summary(days: int = 365) -> dict:
    """生成热力图所需的全部数据（纯读，无副作用）。"""
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 365
    days = max(7, min(days, 730))

    end = date.today()
    start = end - timedelta(days=days - 1)
    # 起点回退到下周一：这样每一列正好一周，前端 7 行网格才对得上星期
    start -= timedelta(days=start.weekday())

    with _lock:
        data = _load()
    recs = data.get("days") or {}

    cells: list[dict] = []
    cur = start
    while cur <= end:
        key = cur.isoformat()
        rec = recs.get(key) or {}
        friends = rec.get("friends") or {}
        ok = sum(1 for v in friends.values() if v)
        failed = sum(1 for v in friends.values() if not v)
        cells.append({"d": key, "ok": ok, "failed": failed, "lv": _level(ok, failed),
                      "tried": (ok + failed) > 0})
        cur += timedelta(days=1)

    real_days = len(cells)
    # 尾部补齐到整周（未来日期），前端网格才是矩形
    while len(cells) % 7:
        cur = date.fromisoformat(cells[-1]["d"]) + timedelta(days=1)
        cells.append({"d": cur.isoformat(), "ok": 0, "failed": 0, "lv": -1,
                      "tried": False, "future": True})

    flags = [c["lv"] > 0 for c in cells[:real_days]]
    today_lv = cells[real_days - 1]["lv"]
    today_sent = today_lv > 0

    # 今天还没发时，从昨天开始数——否则每天零点一过就显示「连续 0 天」，很吓人
    current = _streak(flags, include_today=today_sent)

    stats = {
        "active_days": sum(1 for f in flags if f),
        "total_days": real_days,
        "rate": round(sum(1 for f in flags if f) / real_days, 4) if real_days else 0.0,
        "current_streak": current,
        "best_streak": _best_streak(flags),
        "today_sent": today_sent,
        "today_ok": cells[real_days - 1]["ok"],
        "today_failed": cells[real_days - 1]["failed"],
    }

    last_ok = ""
    for c in reversed(cells[:real_days]):
        if c["lv"] > 0:
            last_ok = c["d"]
            break
    stats["last_send_date"] = last_ok

    return {
        "start": cells[0]["d"],
        "end": cells[real_days - 1]["d"],
        "weeks": len(cells) // 7,
        "friend_window": FRIEND_WINDOW,
        "cells": cells,
        "stats": stats,
        "warning": _warning(stats),
        "friends": _friend_rows(recs, cells, real_days, today_sent),
    }


def _warning(stats: dict) -> dict:
    """今天该发却没发时给出提示；还没到点则只做中性说明。"""
    if stats["today_sent"]:
        return {"level": "ok", "text": f"今天已续上（{stats['today_ok']} 人）"}

    try:
        from .config import load_config
        hh, mm = str(load_config().get("schedule_time") or "21:00").split(":")
        due = datetime.now().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        passed = datetime.now() > due
    except Exception:
        passed = False

    if passed:
        return {
            "level": "danger",
            "text": f"今天还没续上，已过设定时间（上次成功：{stats['last_send_date'] or '无记录'}）",
        }
    return {"level": "pending", "text": "今天还没到发送时间"}


def _friend_rows(recs: dict, cells: list[dict], real_days: int,
                 today_sent: bool = True) -> list[dict]:
    """每个好友一条近 90 天条带，按最危险的排在前面。"""
    window = cells[max(0, real_days - FRIEND_WINDOW):real_days]
    dates = [c["d"] for c in window]

    names: set[str] = set()
    for key in dates:
        names.update((recs.get(key) or {}).get("friends") or {})
    if not names:
        return []

    # 当天有记录却没成功，说明是失败；完全没记录说明今天没轮到/没跑
    today_friends = (recs.get(dates[-1]) or {}).get("friends") or {}

    rows = []
    for name in names:
        bars = []
        flags = []
        for key in dates:
            fr = (recs.get(key) or {}).get("friends") or {}
            if name in fr:
                v = 1 if fr[name] else 2
                flags.append(bool(fr[name]))
            else:
                v = 0
                flags.append(False)
            bars.append(v)
        rows.append({
            "name": name,
            "bars": bars,
            "ok_days": sum(1 for f in flags if f),
            "miss_days": len(flags) - sum(1 for f in flags if f),
            "streak": _streak(flags, include_today=today_sent),
            "best": _best_streak(flags),
            "today": "ok" if today_friends.get(name) else (
                "failed" if name in today_friends else "none"),
        })

    # 断得最多的排前面：这一栏的用途就是"谁快断了"
    rows.sort(key=lambda r: (r["streak"], -r["miss_days"], r["name"]))
    return rows


def backfill_from_runtime(runtime: dict) -> int:
    """把 runtime.json 里已有的近 30 条历史补进按天账本。

    只在新账本为空时用一次，避免每次启动重复写盘。
    """
    with _lock:
        if _load()["days"]:
            return 0
    count = 0
    for item in reversed((runtime or {}).get("history") or []):
        if not isinstance(item, dict) or item.get("dry_run"):
            continue
        at = str(item.get("at") or "")
        day = at[:10]
        if len(day) != 10:
            continue
        ok_names = _names(item.get("ok"))
        failed_names = _names([f.get("name") for f in (item.get("failed") or [])
                               if isinstance(f, dict)])
        if not ok_names and not failed_names:
            continue
        with _lock:
            data = _load()
            cur = data["days"].setdefault(day, {"friends": {}, "runs": 0})
            cur["runs"] = int(cur.get("runs") or 0) + 1
            friends = cur.setdefault("friends", {})
            for name in failed_names:
                friends.setdefault(name, 0)
            for name in ok_names:
                friends[name] = 1
            _save(data)
        count += 1
    return count


def path() -> Path:
    return HISTORY_PATH
