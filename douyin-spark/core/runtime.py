"""运行状态与日志。运行结果持久化到 data/runtime.json，日志同时写文件与内存环形缓冲。"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque

from . import history
from .config import DATA_DIR

RUNTIME_PATH = DATA_DIR / "runtime.json"
LOG_DIR = DATA_DIR / "logs"

# 用可重入锁：公开的写函数需要把「读-改-写」整体包住，
# 而内部又复用了 _save_locked，普通 Lock 会自锁死。
_lock = threading.RLock()
_ring: deque[str] = deque(maxlen=600)


def _default() -> dict:
    return {"session_status": "unknown", "running": False, "last_run": None, "history": []}


def load_runtime() -> dict:
    rt = _default()
    if RUNTIME_PATH.exists():
        try:
            data = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                rt.update(data)
        except Exception:
            # 文件损坏（半写/手工改坏）时回退默认值，不让整个服务起不来
            pass
    return rt


def _save_locked(rt: dict) -> None:
    """调用方必须已持有 _lock。

    先写临时文件再 os.replace 原子替换：直接 write_text 会先截断再写，
    期间并发读可能拿到空文件或半截 JSON。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = RUNTIME_PATH.with_name(RUNTIME_PATH.name + ".tmp")
    tmp.write_text(json.dumps(rt, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, RUNTIME_PATH)


def _save(rt: dict) -> None:
    with _lock:
        _save_locked(rt)


def _mutate(fn) -> dict:
    """在锁内完成「读 → 改 → 写」，避免并发更新互相覆盖。"""
    with _lock:
        rt = load_runtime()
        fn(rt)
        _save_locked(rt)
        return rt


def set_running(value: bool) -> None:
    _mutate(lambda rt: rt.__setitem__("running", bool(value)))


def record_run(result: dict) -> None:
    def _apply(rt: dict) -> None:
        rt["last_run"] = result
        history = rt.get("history", [])
        history.insert(0, result)
        rt["history"] = history[:30]

        if result.get("logged_out"):
            rt["session_status"] = "expired"
        elif result.get("ok") and not result.get("failed"):
            rt["session_status"] = "ok"
        elif result.get("ok"):
            rt["session_status"] = "partial"
        elif not result.get("failed"):
            rt["session_status"] = "ok"
        else:
            rt["session_status"] = "failed"

    _mutate(_apply)
    # 热力图的按天账本：放在 runtime 锁外写，避免两把锁嵌套
    try:
        history.record(result)
    except Exception:
        pass


def record_contacts(data: dict) -> None:
    def _apply(rt: dict) -> None:
        rt["contacts"] = data.get("names", [])
        rt["contacts_at"] = data.get("at")
        rt["contacts_error"] = data.get("error")

    _mutate(_apply)


def update_runtime(**fields) -> None:
    _mutate(lambda rt: rt.update(fields))


class RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _ring.append(self.format(record))
        except Exception:
            pass


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("douyin-spark")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    rh = RingHandler()
    rh.setFormatter(fmt)
    logger.addHandler(rh)
    return logger


def recent_logs(n: int = 300) -> list[str]:
    return list(_ring)[-n:]
