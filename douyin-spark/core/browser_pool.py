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

import asyncio
import logging
import queue
import threading
import time
from concurrent.futures import Future
from typing import Any, Callable

from playwright.sync_api import sync_playwright

logger = logging.getLogger("douyin-spark")

IDLE_CLOSE_SEC = 900      # 空闲 15 分钟就关，别一直占着 2C4G 的资源
MAX_PAGE_AGE = 3600       # 单页最长活 1 小时，防止内存缓慢增长
WARM_ITEMS = 10
TITLE_SEL = ".conversationConversationItemtitle"

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

# --------------------------------------------------------------------------- 专属工作线程
#
# 为什么不用 ThreadPoolExecutor：
#   Playwright 同步 API 的入场检查是
#       self._loop = asyncio.get_running_loop()
#       if self._loop.is_running(): raise Error("...Sync API inside the asyncio loop...")
#   也就是说**只要当前线程里有一个正在跑的事件循环，就直接炸**。执行器线程虽然是新的，
#   但线程是「借」来的：上层（uvicorn / anyio / 某个测试脚手架）如果在这个线程里跑过
#   asyncio.run()，或者干脆把同步函数放进了 async 上下文，这个线程就会带着循环。
#   这里自己造线程，并在入口第一件事就把事件循环摘干净，从此和上层调度方式无关。
_jobs: queue.Queue = queue.Queue()
_worker: threading.Thread | None = None


class _FreshThread(Exception):
    """当前线程里还残留着上一份 Playwright 的事件循环，这个任务必须换新线程跑。"""


def _thread_has_loop() -> bool:
    """当前线程里是否有一个"正在跑"的事件循环。

    Playwright 同步 API 就是靠这个判定的：有 → 直接抛
    "It looks like you are using Playwright Sync API inside the asyncio loop"。
    注意：只要本线程里有一个活着的 Playwright 实例，这里就会是 True（它的私有循环
    跑在同线程的 greenlet 里），所以它同时也是"这个线程还能不能再 start 一次"的探针。
    """
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _ensure_worker() -> None:
    global _worker
    with _state_lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_main, daemon=True, name="chat-browser")
        _worker.start()


def _retire_self() -> None:
    """让当前工作线程退出，并保证队列里剩下的任务有人接着跑。"""
    global _worker
    with _state_lock:
        if _worker is threading.current_thread():
            _worker = None
    _ensure_worker()


def _worker_main() -> None:
    # 关键一行：让这个线程「没有当前事件循环」。
    # 之后 get_running_loop() 必然抛 RuntimeError，Playwright 才会自己新建一个私有循环。
    asyncio.set_event_loop(None)
    logger.info("浏览器工作线程已启动：%s（无事件循环）", threading.current_thread().name)
    while True:
        item = _jobs.get()
        if item is None:
            break
        fn, args, fut = item
        if not fut.set_running_or_notify_cancel():
            _done()
            continue
        try:
            fut.set_result(fn(*args))
        except _FreshThread as e:
            # 本线程已被 Playwright 的循环占用：把异常交回调用方，自己退休。
            # 调用方（call）看到这个异常会换一条新线程重试。
            fut.set_exception(e)
            _done()
            logger.info("线程 %s 残留事件循环，退休并换新线程", threading.current_thread().name)
            _retire_self()
            return
        except BaseException as e:      # noqa: BLE001 - 原样回传给调用方
            fut.set_exception(e)
        _done()


def _done() -> None:
    global _pending
    with _state_lock:
        _pending = max(0, _pending - 1)


def _worker_alive() -> bool:
    return _worker is not None and _worker.is_alive()


def _submit_job(fn: Callable, *args) -> Future:
    """把一个调用丢进专属线程，返回 Future。"""
    global _pending
    with _state_lock:
        if _pending >= MAX_QUEUE:
            raise RuntimeError("浏览器正在忙上一条任务（上一个扫描/发送还没结束），请稍后再试")
        _pending += 1
    _ensure_worker()
    fut: Future = Future()
    _jobs.put((fn, args, fut))
    return fut



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
    # 兜底再摘一次事件循环：Playwright 同步 API 一进场就查
    # asyncio.get_running_loop().is_running()，只要这个线程「有活着的循环」就抛
    # "Sync API inside the asyncio loop"。摘掉之后它才会自己建私有循环。
    try:
        asyncio.set_event_loop(None)
    except Exception:
        pass
    try:
        p = sync_playwright().start()
    except Exception as e:
        if "asyncio loop" in str(e):
            logger.error(
                "启动 Playwright 撞上事件循环：线程=%s has_loop=%s（说明本线程里还挂着"
                "一份没 stop 的实例，或同步代码被放进了 async 上下文）",
                threading.current_thread().name, _thread_has_loop(), exc_info=True,
            )
        raise
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
        # 关键：此处 _state 里还没有这个 p（要等下面才 update），
        # 所以 _destroy() 找不到它。必须用局部变量把刚启动的实例停掉，
        # 否则它的私有事件循环会一直挂在这个线程上，下一次 start() 就会撞上
        # "It looks like you are using Playwright Sync API inside the asyncio loop"，
        # 而且一次失败泄漏一个，之后每次调用都失败。
        try:
            p.stop()
        except Exception:
            logger.warning("清理失败的 Playwright 实例时出错（线程可能已残留事件循环）",
                           exc_info=True)
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


def _ensure_page(target: str | None, min_items: int, max_wait: float):
    page = _state["page"]
    if page is not None and _alive():
        _state["reuses"] = _state.get("reuses", 0) + 1
        _state["used_at"] = time.time()
        return page
    if _thread_has_loop():
        # 本线程已经有活着的循环（上一份 Playwright 没释放干净，或有人在里面跑过
        # asyncio.run）。此时再 start() 必然抛 Sync API 的错，只能换线程。
        _destroy()
        raise _FreshThread()
    return _create(target, min_items, max_wait)


def _run(fn: Callable, target: str | None, min_items: int, max_wait: float):
    _reap()
    page = _ensure_page(target, min_items, max_wait)

    try:
        return fn(page)
    except Exception as e:
        logger.warning("浏览器操作失败，重建会话后重试一次：%s", str(e)[:140])
        _destroy()
        page = _ensure_page(target, min_items, max_wait)
        return fn(page)


def _submit(fn: Callable, target: str | None, min_items: int, max_wait: float):
    return _submit_job(_run, fn, target, min_items, max_wait)


# --------------------------------------------------------------------------- 对外

def call(fn: Callable, timeout: float = 240, target: str | None = None,
         min_items: int = WARM_ITEMS, max_wait: float = 80) -> Any:
    """在工作线程内执行 fn(page)。

    fn 里不要做长时间阻塞（网络/模型调用），浏览器线程被占住会拖慢别人。
    """
    deadline = time.time() + timeout
    for attempt in (1, 2):
        fut = _submit(fn, target, min_items, max_wait)
        try:
            return fut.result(timeout=max(1.0, deadline - time.time()))
        except _FreshThread:
            # 工作线程里残着上一份 Playwright 的循环（已在上面标了退休），
            # 换一条全新线程再跑一次；新线程入口会把事件循环摘干净。
            if attempt == 2:
                raise
            logger.info("检测到工作线程残留事件循环，换新线程重试本次操作")
            continue


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


def diagnostics() -> dict:
    """只读线程/队列状态，**不碰 page**。

    page 是 Playwright 对象，只能在它所属的工作线程里访问；健康检查这类
    从别的线程来的调用只能用这个，别用 stats()。
    """
    return {
        "worker": _worker.name if _worker_alive() else None,
        "worker_running": _worker_alive(),
        "pending": _pending,
        "caller_has_loop": _thread_has_loop(),
    }


def stats() -> dict:
    now = time.time()
    return {
        "alive": _alive(),
        "creates": _state.get("creates", 0),
        "reuses": _state.get("reuses", 0),
        "idle_sec": round(now - _state["used_at"]) if _state["used_at"] else None,
        "age_sec": round(now - _state["opened_at"]) if _state["opened_at"] else None,
        **diagnostics(),
    }


def reset() -> None:
    """手动释放会话（换登录态、或怀疑页面状态坏了时用）。"""
    try:
        _submit_job(_destroy).result(timeout=20)
    except Exception:
        pass


def shutdown() -> None:
    global _worker
    try:
        _submit_job(_destroy).result(timeout=20)
    except Exception:
        pass
    try:
        _jobs.put(None)        # 让工作线程退出
    except Exception:
        pass
    _worker = None
