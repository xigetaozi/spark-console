"""配置读写。配置保存在 data/config.json，由网页端编辑。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# 多账号支持：用 SPARK_DATA_DIR 指定该实例独立的数据目录；
# 不设置时保持原有行为（<项目根>/data），完全向后兼容。
DATA_DIR = Path(os.environ.get("SPARK_DATA_DIR") or (BASE_DIR / "data")).expanduser().resolve()
CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_CONFIG = {
    "schedule_time": "21:00",   # 每天发送时间 HH:MM（服务器时区 Asia/Shanghai）
    "jitter_minutes": 30,       # 时间抖动窗口：实际在 [schedule_time, schedule_time+30min] 内随机开始
    "send_gap_min": 6,          # 定时任务：相邻两个好友之间的最小间隔（秒）
    "send_gap_max": 12,         # 定时任务：相邻两个好友之间的最大间隔（秒）
    "manual_gap_min": 0,        # 手动「立即发送」：最小间隔（秒），0 = 秒级连发
    "manual_gap_max": 0,        # 手动「立即发送」：最大间隔（秒）
    "max_friends_per_run": 20,  # 每次最多发送的好友数（0 表示不限制）
    "only_with_spark": True,    # 只给"有火花"的好友发；无火花/抓不到火花状态的一律跳过
    # list：按 friends 手写名单发；all_spark：直接从抓取结果取所有有火花的会话（名单只作记录）
    "friends_mode": "list",
    # 定时任务开始前先刷新一次聊天列表，保证"有火花"判断用当天的数据
    "refresh_contacts_before_send": True,
    "alert_webhook": "",        # 异常告警推送地址（企业微信/钉钉群机器人或自定义网关，留空只记在网页里）
    # 对话系统：off 关闭 / semi 只出草稿人工确认 / auto 白名单内自动回
    "reply": {
        "mode": "off",
        "whitelist": [],
        "quiet_hours": ["23:30", "08:00"],
        "max_per_hour": 3,
        "max_per_day": 10,
        "min_gap_sec": 600,
        "delay_sec": [20, 90],
        "draft_per_cycle": 3,
        "auto_per_cycle": 3,
        "read_limit": 8,
        "persona": "简短、口语，像微信里随手回的那种",
        "llm_enabled": False,
        "llm_base_url": "",
        "llm_api_key": "",
        "llm_model": "",
    },
    "friends": [],              # 好友列表：聊天列表里显示的备注 / 昵称 / 抖音号
    "messages": ["🔥 续火花", "晚安，明天见", "今天也要开心哦"],
}

_lock = threading.Lock()


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg


def save_config(cfg: dict | None) -> dict:
    merged = dict(DEFAULT_CONFIG)
    if cfg:
        merged.update(cfg)

    merged["friends"] = [str(x).strip() for x in merged.get("friends", []) if str(x).strip()]
    merged["messages"] = [str(x) for x in merged.get("messages", []) if str(x).strip()]
    if not merged["messages"]:
        merged["messages"] = ["🔥"]

    schedule = str(merged.get("schedule_time", "21:00"))
    try:
        hh, mm = schedule.split(":")
        if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
            raise ValueError
        merged["schedule_time"] = f"{int(hh):02d}:{int(mm):02d}"
    except Exception:
        raise ValueError("schedule_time 必须是 HH:MM 格式")

    for key in ("jitter_minutes", "send_gap_min", "send_gap_max",
                "manual_gap_min", "manual_gap_max", "max_friends_per_run"):
        try:
            merged[key] = max(0, int(merged.get(key, DEFAULT_CONFIG[key])))
        except (TypeError, ValueError):
            raise ValueError(f"{key} 必须是整数")
    if merged["send_gap_max"] < merged["send_gap_min"]:
        merged["send_gap_max"] = merged["send_gap_min"]
    if merged["manual_gap_max"] < merged["manual_gap_min"]:
        merged["manual_gap_max"] = merged["manual_gap_min"]
    merged["only_with_spark"] = bool(merged.get("only_with_spark", True))
    merged["refresh_contacts_before_send"] = bool(merged.get("refresh_contacts_before_send", True))
    if merged.get("friends_mode") not in ("list", "all_spark"):
        raise ValueError("friends_mode 只能是 list / all_spark")

    # ---- 对话系统配置 ----
    reply = dict(DEFAULT_CONFIG["reply"])
    incoming = merged.get("reply")
    if isinstance(incoming, dict):
        reply.update(incoming)
    if reply.get("mode") not in ("off", "semi", "auto"):
        raise ValueError("reply.mode 只能是 off / semi / auto")
    reply["whitelist"] = [str(x).strip() for x in (reply.get("whitelist") or []) if str(x).strip()]
    qh = reply.get("quiet_hours") or ["23:30", "08:00"]
    if not (isinstance(qh, list) and len(qh) == 2):
        raise ValueError("reply.quiet_hours 必须是 [开始, 结束] 两个时间")
    for t in qh:
        try:
            hh, mm = str(t).split(":")
            if not (0 <= int(hh) <= 23 and 0 <= int(mm) <= 59):
                raise ValueError
        except Exception:
            raise ValueError("reply.quiet_hours 必须是 HH:MM")
    reply["quiet_hours"] = [str(t) for t in qh]
    for key, lo, hi in (("max_per_hour", 0, 30), ("max_per_day", 0, 200),
                        ("min_gap_sec", 0, 86400), ("draft_per_cycle", 1, 20),
                        ("auto_per_cycle", 1, 20), ("read_limit", 2, 40)):
        try:
            reply[key] = min(hi, max(lo, int(reply.get(key, DEFAULT_CONFIG["reply"][key]))))
        except (TypeError, ValueError):
            raise ValueError(f"reply.{key} 必须是整数")
    delay = reply.get("delay_sec") or [20, 90]
    if not (isinstance(delay, list) and len(delay) == 2):
        raise ValueError("reply.delay_sec 必须是 [最小, 最大]")
    try:
        d0, d1 = float(delay[0]), float(delay[1])
    except (TypeError, ValueError):
        raise ValueError("reply.delay_sec 必须是数字")
    reply["delay_sec"] = [max(0.0, min(d0, d1)), max(d0, d1, 0.0)]
    reply["llm_enabled"] = bool(reply.get("llm_enabled"))
    # 白名单为空时，auto 直接降级，避免"以为开了全自动其实没开"或反之
    merged["reply"] = reply

    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 原子写：先写临时文件再 os.replace。直接 write_text 会先截断，
        # 并发 load_config 可能读到空文件 → 回退默认配置，表现为"名单突然没了"。
        tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        # 配置里可能含大模型 API Key，权限收到仅属主可读写
        # （注意：Windows 上 chmod 只影响只读位，这项保护仅在 Linux/macOS 生效）
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, CONFIG_PATH)
    return merged
