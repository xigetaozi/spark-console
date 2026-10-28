"""常驻浏览器会话池。

背景：生成建议每次都会 open_and_read()，而旧实现是"每次请求新开一个 Chromium →
goto → 等会话列表渐进渲染（实测最长 56 秒）→ 读完关掉"。所以第二次点仍然要 60 多秒，
慢的不是模型，是**重复启动浏览器 + 重复等列表渲染**。

做法：
- 用**单线程执行器**承载所有 Playwright 操作。sync API 的对象只能在其创建的线程用，
  而 FastAPI 的同步端点跑在线程池里，直接共享全局 page 会随机报错；
- 页面只建一次，之后同线程复用，空闲超过 IDLE_CLOSE_SEC 或存活超过 MAX_PAGE_AGE 才关；
- 预热按"目标是否已出现"提前退出，而不是死等整张列表渲染完；
- 任何一次操作抛异常就重建并重试一次，避免"页面卡死但进程还在"。

收益（实测）：首次约 15~40 秒（取决于目标在列表中的位置），之后同一目标/其他目标
约 2~4 秒。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from playwright.sync_api import sync_playwright

logger = logging.getLogger("douyin-spark")

IDLE_CLOSE_SEC = 900      # 空闲 15 分钟就关，别一直占着 2C4G 的资源
MAX_PAGE_AGE = 3600       # 单页最长活 1 小时，防止内存缓慢增长
WARM_ITEMS = 10
TITLE_SEL = ".conversationConversationItemtitle"

_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chat-browser")
_state: dict[str, Any] = {
    "p": None, "browser": None, "page": None,
    "opened_at": 0.0, "used_at": 0.0, "reuses": 0, "creates": 0,
}
_state_lock = threading.Lock()

# 排队长度：单线程执行器，一旦上一次操作卡住（页面假死 / 抖音不渲染），
# 后面点几次就排几次，每次都要等到超时才报错，表现成"连续点连续失败"。
# 这里卡个上限，超了立刻返回可读原因，而不是让用户干等 5 分钟。
MAX_QUEUE = 3
_pending = 0


# --------------------------------------------------------------------------- 内部

def _alive() -> bool:
    page = _state["page"]
    if page is None:
        return False
    try:
        return not page.is_closed()
    except Exception:
        return False


def _destroy() -> None:
    browser = _state.get("browser")
    p = _state.get("p")
    _state.update(page=None, browser=None, p=None, opened_at=0.0)
    try:
        if browser:
            browser.close()
    except Exception:
        pass
    try:
        if p:
            p.stop()
    except Exception:
        pass


def _has_target(page, target: str) -> bool:
    try:
        return bool(page.evaluate(
            """(t) => [...document.querySelectorAll('.conversationConversationItemtitle')]
                       .some(e => ((e.textContent || '').trim()) === t)""",
            target,
        ))
    except Exception:
        return False


def _warm(page, target: str | None, min_items: int, max_wait: float) -> int:
    """等列表热起来。已知目标时，目标一出现就返回（这是首屏提速的关键）。"""
    deadline = time.time() + max_wait
    seen = 0
    while time.time() < deadline:
        page.wait_for_timeout(1200)
        try:
            seen = page.locator(TITLE_SEL).count()
        except Exception:
            continue
        if seen >= min_items:
            if not target or _has_target(page, target):
                return seen
    return seen


def _create(target: str | None, min_items: int, max_wait: float):
    from . import automation

    _destroy()
    t0 = time.time()
    p = sync_playwright().start()
    t_start = time.time() - t0
    try:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox",
                  "--disable-dev-shm-usage", "--disable-gpu"],
        )
        t_launch = time.time() - t0
        context = browser.new_context(
            storage_state=str(automation.STATE_PATH),
            viewport={"width": 1366, "height": 768},
        )
        page = context.new_page()
        page.goto(automation.CHAT_URL, timeout=90000, wait_until="domcontentloaded")
        t_goto = time.time() - t0
    except Exception:
        _destroy()
        raise

    _state.update(p=p, browser=browser, page=page,
                  opened_at=time.time(), used_at=time.time(),
                  creates=_state.get("creates", 0) + 1)
    n = _warm(page, target, min_items, max_wait)
    logger.info(
        "浏览器会话就绪（新建）：%s 条会话 / 目标=%s | 启 %.1fs 开页 %.1fs 预热 %.1fs 共 %.1fs",
        n, target or "-", t_start, t_goto - t_launch, time.time() - t0 - t_goto, time.time() - t0,
    )
    return page


def _reap() -> None:
    if _state["page"] is None:
        return
    now = time.time()
    idle = now - (_state["used_at"] or now)
    age = now - (_state["opened_at"] or now)
    if idle > IDLE_CLOSE_SEC or age > MAX_PAGE_AGE:
        logger.info("浏览器会话空闲 %.0fs / 存活 %.0fs，关闭", idle, age)
        _destroy()


def _run(fn: Callable, target: str | None, min_items: int, max_wait: float):
    _reap()
    page = _state["page"]
    if page is None or not _alive():
        page = _create(target, min_items, max_wait)
    else:
        _state["reuses"] = _state.get("reuses", 0) + 1
        _state["used_at"] = time.time()

    try:
        return fn(page)
    except Exception as e:
        logger.warning("浏览器操作失败，重建会话后重试一次：%s", str(e)[:140])
        page = _create(target, min_items, max_wait)
        return fn(page)


def _run_counted(fn: Callable, target: str | None, min_items: int, max_wait: float):
    """排队计数必须是"任务真正结束"才减：

    调用方超时只是它不等了，执行线程还在跑；如果按超时减计数，
    卡住的那个就会被当成空闲，后面的请求继续往里堆。
    """
    global _pending
    try:
        return _run(fn, target, min_items, max_wait)
    finally:
        with _state_lock:
            _pending = max(0, _pending - 1)


def _submit(fn: Callable, target: str | None, min_items: int, max_wait: float):
    global _pending
    with _state_lock:
        if _pending >= MAX_QUEUE:
            raise RuntimeError("浏览器正在忙上一条任务（上一个扫描/发送还没结束），请稍后再试")
        _pending += 1
    try:
        return _exec.submit(_run_counted, fn, target, min_items, max_wait)
    except Exception:
        with _state_lock:
            _pending = max(0, _pending - 1)
        raise


# --------------------------------------------------------------------------- 对外

def call(fn: Callable, timeout: float = 240, target: str | None = None,
         min_items: int = WARM_ITEMS, max_wait: float = 80) -> Any:
    """在执行器线程内执行 fn(page)。

    fn 里不要做长时间阻塞（网络/模型调用），浏览器线程被占住会拖慢别人。
    """
    fut = _submit(fn, target, min_items, max_wait)
    return fut.result(timeout=timeout)


_warming = threading.Lock()


def warm_async(target: str | None = None, min_items: int = 2, max_wait: float = 60) -> bool:
    """后台预热：不阻塞调用方，先花一次开页成本，后续点击就是秒级。

    冷启动那一页抖音会话列表要 ~52 秒才渲染（实测：启 0.5s / 开页 1.5s / 预热 52.4s），
    所以一进页面就悄悄起一个，等用户真的点「扫描收件箱」时页面已经热了。
    """
    if _alive():
        return False
    with _state_lock:
        if _pending:          # 已经有任务在用浏览器，别去抢队列
            return False
    if not _warming.acquire(blocking=False):
        return False

    def _bg():
        try:
            _submit(lambda page: True, target, min_items, max_wait).result(timeout=150)
            logger.info("后台预热完成")
        except Exception as e:
            logger.info("后台预热失败：%s", str(e)[:100])
        finally:
            _warming.release()

    threading.Thread(target=_bg, daemon=True, name="chat-prewarm").start()
    return True


def stats() -> dict:
    now = time.time()
    return {
        "alive": _alive(),
        "creates": _state.get("creates", 0),
        "reuses": _state.get("reuses", 0),
        "idle_sec": round(now - _state["used_at"]) if _state["used_at"] else None,
        "age_sec": round(now - _state["opened_at"]) if _state["opened_at"] else None,
        "pending": _pending,
    }


def reset() -> None:
    """手动释放会话（换登录态、或怀疑页面状态坏了时用）。"""
    try:
        _exec.submit(_destroy).result(timeout=20)
    except Exception:
        pass


def shutdown() -> None:
    try:
        _exec.submit(_destroy).result(timeout=20)
    except Exception:
        pass
    try:
        _exec.shutdown(wait=False)
    except Exception:
        pass
