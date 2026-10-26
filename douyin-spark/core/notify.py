"""异常告警：本地告警记录 + 可选 Webhook 推送，同一原因 24 小时内只推一次。

设计取舍：
- 只在"需要人干预"时告警（登录失效 / 限流 / 整轮全失败），单条失败只记日志；
- 去重按 key 做，避免一天刷屏被用户屏蔽；
- 推送放子线程，Webhook 慢或挂掉都不能影响发送主链路；
- 告警永远落本地 alerts.json，没配 Webhook 也能在网页上看到。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from datetime import datetime
from urllib.parse import urlparse

from .config import DATA_DIR, load_config

ALERTS_PATH = DATA_DIR / "alerts.json"
_MAX_KEEP = 100
_DEDUPE_SECONDS = 24 * 3600
_lock = threading.Lock()

LEVEL_LABEL = {"critical": "紧急", "warning": "警告", "info": "提示"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _load() -> list[dict]:
    try:
        data = json.loads(ALERTS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(items: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ALERTS_PATH.write_text(
        json.dumps(items[-_MAX_KEEP:], ensure_ascii=False, indent=1), encoding="utf-8"
    )


def recent(limit: int = 20) -> list[dict]:
    """最近告警，新的在前。"""
    return list(reversed(_load()[-limit:]))


def _recent_same(key: str) -> dict | None:
    cutoff = time.time() - _DEDUPE_SECONDS
    for item in reversed(_load()):
        if item.get("key") != key:
            continue
        try:
            ts = datetime.fromisoformat(item.get("at", "")).timestamp()
        except Exception:
            return item
        return item if ts >= cutoff else None
    return None


def _build_payload(url: str, title: str, body: str, level: str) -> tuple[bytes, str]:
    text = f"【续火花·{LEVEL_LABEL.get(level, level)}】{title}\n{body}"
    host = (urlparse(url).hostname or "").lower()
    if "qyapi.weixin.qq.com" in host:      # 企业微信群机器人
        return json.dumps({"msgtype": "text", "text": {"content": text}},
                          ensure_ascii=False).encode(), "application/json"
    if "oapi.dingtalk.com" in host:        # 钉钉群机器人
        return json.dumps({"msgtype": "text", "text": {"content": text}},
                          ensure_ascii=False).encode(), "application/json"
    # 通用格式：同时给 title/body/text，适配 Bark、自定义网关等
    return json.dumps({"title": title, "body": body, "text": text, "level": level},
                      ensure_ascii=False).encode(), "application/json"


def _push(url: str, title: str, body: str, level: str) -> None:
    try:
        data, ctype = _build_payload(url, title, body, level)
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read(200)
    except Exception as e:  # 推送失败不能影响主流程，只留痕
        try:
            with _lock:
                items = _load()
                items.append({"at": _now(), "key": "_push_error", "level": "info",
                              "title": "Webhook 推送失败", "body": str(e)[:200]})
                _save(items)
        except Exception:
            pass


def alert(key: str, level: str, title: str, body: str = "", force: bool = False) -> bool:
    """记录一条告警；返回是否真的记了新的一条（被去重则返回 False）。"""
    if not force and _recent_same(key) is not None:
        return False

    record = {"at": _now(), "key": key, "level": level, "title": title, "body": body}
    with _lock:
        items = _load()
        items.append(record)
        _save(items)

    webhook = ""
    try:
        webhook = str(load_config().get("alert_webhook") or "").strip()
    except Exception:
        pass
    if webhook:
        threading.Thread(target=_push, args=(webhook, title, body, level),
                         daemon=True).start()
    return True
