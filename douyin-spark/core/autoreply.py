"""自动/半自动回复的编排层：谁可以回、什么时候回、回了要留什么痕。

三条铁律（写死在代码里，配置改不动）：
1. 全自动必须配白名单；白名单为空时 auto 模式自动降级为 semi；
2. 消息内容读不准（置信度不是 rect）时，一律不自动发；
3. 命中提问 / 金钱 / 情绪 / 隐私等特征时，一律不自动发。

人工介入的边界见 HUMAN_BOUNDARY 常量，会原样返回给前端展示。
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime

from . import chat, inbox, reply_engine
from .config import load_config

logger = logging.getLogger("douyin-spark")

HUMAN_BOUNDARY = [
    "包含提问（问句、吗/呢/吧结尾）→ 只出草稿，不发",
    "涉及金钱、号码、链接 → 只把原文摆给你看，不生成",
    "负面情绪、亲密表达、突发状况 → 只出草稿并标红",
    "消息方向判断置信度不足 → 不自动发",
    "不在白名单里的联系人 → 全自动模式下永不处理",
    "静默时段内 → 全自动模式完全停止",
]

DEFAULTS = {
    "mode": "off",              # off | semi | auto
    "whitelist": [],
    "quiet_hours": ["23:30", "08:00"],
    "max_per_hour": 3,
    "max_per_day": 10,
    "min_gap_sec": 600,         # 同一个人两次自动回复的最小间隔
    "delay_sec": [20, 90],      # 发出前的随机等待，避免秒回
    "draft_per_cycle": 3,       # 半自动每轮最多准备几个草稿
    "auto_per_cycle": 3,        # 全自动每轮最多处理几个会话
    "read_limit": 8,
    "persona": "简短、口语，像微信里随手回的那种",
    "llm_enabled": False,
    "llm_base_url": "",
    "llm_api_key": "",
    "llm_model": "",
}


def get_cfg() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update(load_config().get("reply") or {})
    except Exception:
        pass
    if cfg.get("mode") not in ("off", "semi", "auto"):
        cfg["mode"] = "off"
    if cfg["mode"] == "auto" and not (cfg.get("whitelist") or []):
        cfg["mode"] = "semi"
        cfg["_downgraded"] = "白名单为空，已从全自动降级为半自动"
    return cfg


def _hhmm_minutes(s: str) -> int:
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def in_quiet_hours(cfg: dict, now: datetime | None = None) -> bool:
    rng = cfg.get("quiet_hours") or []
    if len(rng) != 2:
        return False
    now_m = (now or datetime.now()).hour * 60 + (now or datetime.now()).minute
    a, b = _hhmm_minutes(rng[0]), _hhmm_minutes(rng[1])
    return a <= now_m or now_m < b if a > b else a <= now_m < b


def _peer_recently_replied(peer: str, gap: int) -> bool:
    for e in inbox.recent_audit(120):
        if e.get("action") == "auto_sent" and e.get("peer") == peer:
            try:
                if time.time() - datetime.fromisoformat(e["at"]).timestamp() < gap:
                    return True
            except Exception:
                continue
    return False


KIND_LABEL = {"text": "文字", "card": "分享卡片", "image": "图片", "video": "视频", "emoji": "表情"}


def draft_for(peer: str, cfg: dict | None = None, pre_got: dict | None = None,
              preview: str | None = None) -> dict:
    """为单个会话生成建议回复（会打开会话，因此会消除未读）。

    `pre_got`：已经读好的消息，传了就不再重复打开会话（实时监听用，省一次打开成本）。
    """
    cfg = cfg or get_cfg()
    t0 = time.time()
    if preview is None:
        preview = ((inbox.load().get("items", {}).get(peer) or {}).get("preview") or "").strip()
    if pre_got is not None:
        got = pre_got
    else:
        got = chat.open_and_read(peer, limit=int(cfg.get("read_limit") or 8), expect=preview)
    msgs = got.get("items") or []
    if not msgs:
        return {"ok": False, "error": got.get("error") or "没有读到消息内容", "conf": got.get("conf")}

    inbound = [m for m in msgs if m.get("dir") == "in"]

    # 关键：只把"文字气泡"当消息。
    # 分享卡片（MessageItemShareAweme）里显示的是被分享作品的信息，比如
    # 「该用户已注销」是那个作品的作者状态，不是对方说的话——曾据此生成过
    # "那确实续不了了"这种完全跑偏的回复。
    texts = [m for m in msgs if (m.get("kind") or "text") == "text"]
    inbound_text = [m for m in texts if m.get("dir") == "in"]

    latest = inbound[-1] if inbound else (msgs[-1] if msgs else {})
    last_kind = latest.get("kind") or "text"
    kind_cn = KIND_LABEL.get(last_kind, last_kind)

    if not inbound_text:
        label = "分享卡片" if last_kind == "card" else kind_cn
        if last_kind == "text":
            msg = "最近几条里没有对方发的文字（可能都是你发的），已跳过，避免答非所问"
        else:
            msg = f"对方最近只发了{label}，没有可参考的文字内容，已跳过，避免答非所问"
        return {
            "ok": False,
            "peer": peer,
            "error": msg,
            "conf": got.get("conf"),
            "last_kind": last_kind,
            "elapsed_sec": round(time.time() - t0, 1),
        }

    last_in = inbound_text[-1].get("text", "")

    history = [{"direction": m.get("dir"), "text": m.get("text", "")} for m in texts]
    gen = reply_engine.generate(last_in, history, cfg)

    # 最新一条不是文字时，不管内容多"安全"都不允许自动发
    if last_kind != "text":
        gen["auto_ok"] = False
        gen["reason"] = f"对方最新一条是{kind_cn}，不是文字，建议人工确认"
        gen["kind"] = "needs_human"

    # 与收件箱预览核对：列表预览就是"最新一条消息"，读到的最新一条应该对得上。
    # 对不上说明读串了会话或还没渲染完，此时不能自动发。
    hint = ""
    bracketed = preview.startswith("[") and preview.endswith("]")   # [视频]/[语音]/[表情] 这类占位预览
    if preview and not bracketed and last_in and preview not in last_in and last_in not in preview:
        hint = f"与收件箱预览不一致（预览是「{preview[:18]}」），可能读串了会话或刚收到新消息"
        gen["auto_ok"] = False
        gen["reason"] = "读到的最后一句与列表预览对不上，建议人工核对后再发"
        gen["kind"] = "needs_human"

    inbox.set_draft(peer, gen["replies"], gen["source"], gen["kind"])
    inbox.audit({"action": "draft", "peer": peer, "kind": gen["kind"],
                 "auto_ok": gen["auto_ok"], "reason": gen["reason"],
                 "source": gen["source"], "conf": got.get("conf"),
                 "last_kind": last_kind})

    return {
        "ok": True,
        "peer": peer,
        "last_in": last_in,
        "last_kind": last_kind,
        "latest_is_text": last_kind == "text",
        "messages": msgs,
        "conf": got.get("conf"),
        "hint": hint,
        "read_sec": got.get("elapsed_sec"),
        "elapsed_sec": round(time.time() - t0, 1),
        **gen,
    }


def run_cycle(mode: str | None = None, only_peer: str | None = None) -> dict:
    """跑一轮：扫描 → 按模式处理。返回可读的执行报告。"""
    cfg = get_cfg()
    mode = (mode or cfg["mode"]).lower()
    report = {"at": inbox._now(), "mode": mode, "scanned": 0, "drafted": 0,
              "sent": [], "skipped": [], "errors": [], "downgraded": cfg.get("_downgraded")}

    if mode == "off":
        report["errors"].append("模式为关闭，未执行")
        return report

    if in_quiet_hours(cfg):
        report["errors"].append(f"处于静默时段 {cfg['quiet_hours'][0]}–{cfg['quiet_hours'][1]}，本轮跳过")
        inbox.audit({"action": "cycle_skip", "reason": "quiet_hours"})
        return report

    scan = chat.scan_inbox()
    if scan.get("error"):
        report["errors"].append(scan["error"])
        inbox.audit({"action": "scan_failed", "reason": scan["error"][:160]})
        return report

    inbox.apply_scan(scan)
    report["scanned"] = len(scan.get("items") or [])

    state = inbox.summary()
    todo = [i for i in state["items"] if i.get("status") in ("unread", "pending")]
    if only_peer:
        todo = [i for i in todo if i["peer"] == only_peer]
    if not todo:
        return report

    cap = int(cfg.get("auto_per_cycle") if mode == "auto" else cfg.get("draft_per_cycle") or 3)

    for item in todo[:max(1, cap)]:
        peer = item["peer"]
        if mode == "auto":
            if peer not in (cfg.get("whitelist") or []):
                report["skipped"].append({"peer": peer, "reason": "不在白名单"})
                inbox.audit({"action": "skip", "peer": peer, "reason": "not_in_whitelist"})
                continue
            if inbox.count_sent(3600) >= int(cfg.get("max_per_hour") or 3):
                report["skipped"].append({"peer": peer, "reason": "已达每小时上限"})
                inbox.audit({"action": "skip", "peer": peer, "reason": "hour_limit"})
                break
            if inbox.count_sent(86400) >= int(cfg.get("max_per_day") or 10):
                report["skipped"].append({"peer": peer, "reason": "已达每日上限"})
                inbox.audit({"action": "skip", "peer": peer, "reason": "day_limit"})
                break
            if _peer_recently_replied(peer, int(cfg.get("min_gap_sec") or 600)):
                report["skipped"].append({"peer": peer, "reason": "刚回复过，冷却中"})
                continue

        res = draft_for(peer, cfg)
        if not res.get("ok"):
            report["errors"].append(f"{peer}: {res.get('error')}")
            continue
        report["drafted"] += 1

        if mode != "auto":
            continue

        # 全自动二次校验 —— 任何一条不满足就只留草稿
        block = None
        if res.get("conf") not in ("rect", "geom"):
            block = f"消息方向置信度不足({res.get('conf')})"
        elif not res.get("auto_ok"):
            block = res.get("reason") or "内容需要人工判断"
        elif len(res.get("last_in") or "") > 60:
            block = "对方消息过长"
        if block:
            report["skipped"].append({"peer": peer, "reason": block})
            inbox.audit({"action": "skip", "peer": peer, "reason": block})
            continue

        reply = (res.get("replies") or [""])[0]
        lo, hi = (cfg.get("delay_sec") or [20, 90])[:2]
        wait = random.uniform(float(lo), float(hi))
        time.sleep(wait)

        sent = chat.send_text(peer, reply)
        if sent.get("ok"):
            inbox.set_status(peer, "sent", note=f"auto: {reply}")
            report["sent"].append({"peer": peer, "text": reply})
            inbox.audit({"action": "auto_sent", "peer": peer, "text": reply,
                         "reply_to": (res.get("last_in") or "")[:80], "delay": round(wait)})
        else:
            report["errors"].append(f"{peer}: {sent.get('reason')}")
            inbox.audit({"action": "auto_failed", "peer": peer, "reason": sent.get("reason")})

    return report


def try_auto_send(peer: str, res: dict, cfg: dict | None = None) -> dict:
    """全自动的发送闸门：任一条件不满足就只留草稿，返回原因。

    与 run_cycle 里的判断保持一致，抽出来是为了让"实时监听"复用同一套边界。
    """
    cfg = cfg or get_cfg()
    if cfg.get("mode") != "auto":
        return {"sent": False, "reason": "非全自动模式"}
    if in_quiet_hours(cfg):
        return {"sent": False, "reason": f"静默时段 {cfg['quiet_hours'][0]}–{cfg['quiet_hours'][1]}"}
    if peer not in (cfg.get("whitelist") or []):
        return {"sent": False, "reason": "不在白名单"}
    if inbox.count_sent(3600) >= int(cfg.get("max_per_hour") or 3):
        return {"sent": False, "reason": "已达每小时上限"}
    if inbox.count_sent(86400) >= int(cfg.get("max_per_day") or 10):
        return {"sent": False, "reason": "已达每日上限"}
    if _peer_recently_replied(peer, int(cfg.get("min_gap_sec") or 600)):
        return {"sent": False, "reason": "刚回复过，冷却中"}
    if res.get("conf") not in ("rect", "geom"):
        return {"sent": False, "reason": f"置信度不足({res.get('conf')})"}
    if not res.get("auto_ok"):
        return {"sent": False, "reason": res.get("reason") or "内容需要人工判断"}
    if len(res.get("last_in") or "") > 60:
        return {"sent": False, "reason": "对方消息过长"}

    reply = (res.get("replies") or [""])[0]
    lo, hi = (cfg.get("delay_sec") or [20, 90])[:2]
    wait = random.uniform(float(lo), float(hi))
    time.sleep(wait)

    sent = chat.send_text(peer, reply)
    if sent.get("ok"):
        inbox.set_status(peer, "sent", note=f"auto: {reply}")
        inbox.audit({"action": "auto_sent", "peer": peer, "text": reply,
                     "reply_to": (res.get("last_in") or "")[:80], "delay": round(wait)})
        return {"sent": True, "text": reply, "delay": round(wait)}

    inbox.audit({"action": "auto_failed", "peer": peer, "reason": sent.get("reason")})
    return {"sent": False, "reason": sent.get("reason") or "发送失败"}
