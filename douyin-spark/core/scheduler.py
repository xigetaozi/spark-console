"""每天定时触发发送任务。"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .config import load_config

logger = logging.getLogger("douyin-spark")
TZ = "Asia/Shanghai"

_scheduler: BackgroundScheduler | None = None
_run_func: Callable | None = None


def _daily_job() -> None:
    cfg = load_config()
    # 注意：不能写 `int(cfg.get("jitter_minutes", 30) or 30)`——
    # jitter_minutes=0 时 `0 or 30` 会得到 30，导致"抖动设为 0"被静默改成 30 分钟。
    raw_jitter = cfg.get("jitter_minutes", 30)
    try:
        jitter = int(raw_jitter)
    except (TypeError, ValueError):
        jitter = 30
    jitter = max(0, jitter)
    if jitter:
        delay = random.uniform(0, jitter * 60)
        logger.info("随机延迟 %.0f 秒后开始发送（抖动窗口 %s 分钟）", delay, jitter)
        time.sleep(delay)
    else:
        logger.info("抖动为 0，按设定时间准点开始")
    if _run_func:
        _run_func()


def configure(run_func: Callable) -> None:
    global _scheduler, _run_func
    _run_func = run_func
    if _scheduler is None:
        _scheduler = BackgroundScheduler(timezone=TZ)
        _scheduler.start()
    apply_schedule()


def apply_schedule() -> None:
    if _scheduler is None:
        return
    cfg = load_config()
    hh, mm = cfg.get("schedule_time", "21:00").split(":")
    _scheduler.add_job(
        _daily_job,
        CronTrigger(hour=int(hh), minute=int(mm), timezone=TZ),
        id="daily_send",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=3600,
    )
    logger.info("定时任务已更新：每天 %s:%s (%s)", hh, mm, TZ)


def next_run_time() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job("daily_send")
    if job and job.next_run_time:
        return job.next_run_time.isoformat()
    return None


def schedule_retry(run_func: Callable, delay_minutes: int = 45) -> None:
    if _scheduler is None:
        return
    if _scheduler.get_job("retry_send"):
        return
    run_at = datetime.now() + timedelta(minutes=delay_minutes)
    _scheduler.add_job(
        run_func,
        DateTrigger(run_date=run_at, timezone=TZ),
        id="retry_send",
        replace_existing=True,
    )
    logger.info("已安排 %s 分钟后自动补发本次失败的好友", delay_minutes)


def cancel_retry() -> None:
    if _scheduler and _scheduler.get_job("retry_send"):
        _scheduler.remove_job("retry_send")
        logger.info("已取消待执行的补发任务")


def shutdown() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
