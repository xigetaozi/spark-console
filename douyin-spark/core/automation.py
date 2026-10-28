"""Playwright 自动化：在抖音网页版私信页面给指定好友发送消息。

发送逻辑参考 douyin-cloud-streak（MIT），要点：
- 点击联系人后校验右侧会话确实切换（防止限流时错发给上一个人）；
- 列表点击失败时用搜索框兜底；
- 检测"操作频繁 / 安全验证"等提示，命中即停本轮；
- 发送前清空输入框，发送后校验输入框已清空。
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

from .config import DATA_DIR, load_config
from .runtime import load_runtime
from . import slogan

logger = logging.getLogger("douyin-spark")


def _start_playwright():
    """启动同步 Playwright，并且保证当前线程"没有事件循环"。

    playwright 同步 API 的入场检查是 `asyncio.get_running_loop().is_running()`，
    一旦当前线程正跑着事件循环，就直接抛
    "It looks like you are using Playwright Sync API inside the asyncio loop"。
    本函数都是阻塞式抓取，进门前先把循环摘掉，避免被 async 上下文间接调用时炸掉。
    """
    try:
        asyncio.set_event_loop(None)
    except Exception:
        pass
    return sync_playwright().start()

STATE_PATH = DATA_DIR / "state.json"
SCREENSHOT_PATH = DATA_DIR / "last_error.png"
CHAT_URL = "https://www.douyin.com/chat"

RATE_LIMIT_KEYWORDS = [
    "操作频繁",
    "操作太频繁",
    "发送过于频繁",
    "请稍后再试",
    "稍后再试",
    "安全验证",
    "滑动验证",
    "验证码",
    "验证中心",
    "人机验证",
    "网络异常",
    "请勿频繁",
]

LOGIN_TEXTS = ["扫码登录", "验证码登录", "登录后查看", "登录后即可"]

# 秒级发送：等待改成轮询，不再用固定 sleep 硬等。
POLL = 0.15          # 轮询间隔（秒）
SWITCH_TIMEOUT = 8.0  # 点击联系人后等待右侧会话切换的最长时间
SEND_VERIFY_TIMEOUT = 4.0  # 等待输入框清空（确认消息发出）的最长时间

# 停止信号：用 Event 而不是 sleep，间隔期间点停止可以立刻退出
STOP_EVENT = threading.Event()


def request_stop() -> None:
    STOP_EVENT.set()


def clear_stop() -> None:
    STOP_EVENT.clear()


def _stopped() -> bool:
    return STOP_EVENT.is_set()



def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _screenshot(page) -> None:
    try:
        page.screenshot(path=str(SCREENSHOT_PATH), timeout=5000)
        logger.info("已保存页面截图: %s", SCREENSHOT_PATH)
    except Exception:
        pass


def check_login(page) -> tuple[bool, str]:
    """返回 (是否已登录, 说明)。宁可误报掉线，也不要带着过期登录态硬跑。"""
    url = page.url
    if "login" in url.lower() or "passport" in url.lower():
        return False, f"页面已跳转到登录页（{url}）"

    try:
        qr = page.locator("#animate_qrcode_container")
        if qr.count() and qr.first.is_visible():
            return False, "页面出现扫码登录二维码，登录态已过期"
    except Exception:
        pass

    for text in LOGIN_TEXTS:
        try:
            loc = page.get_by_text(text, exact=False)
            for i in range(min(loc.count(), 3)):
                if loc.nth(i).is_visible():
                    return False, f"页面出现登录提示「{text}」"
        except Exception:
            continue

    cookies = page.context.cookies()
    if not any(c["name"].startswith("sessionid") for c in cookies):
        return False, "未检测到 sessionid Cookie"
    return True, "ok"


def detect_rate_limit(page) -> str | None:
    for kw in RATE_LIMIT_KEYWORDS:
        try:
            loc = page.get_by_text(kw, exact=False)
            for i in range(loc.count()):
                if loc.nth(i).bounding_box():
                    return kw
        except Exception:
            continue
    return None


def _find_contact(page, name: str):
    """优先按全文精确匹配联系人标题，避免误点其他会话里的消息预览。"""
    exact = page.get_by_text(name, exact=True)
    if exact.count():
        return exact.first
    return page.locator(".conversationConversationItemtitle").filter(has_text=name).first


def verify_in_conversation(page, name: str) -> bool:
    """右侧会话顶部标题区域（x>300 且 y<100）出现目标昵称才算切换成功，防止错发。"""
    for exact in (True, False):
        try:
            loc = page.get_by_text(name, exact=exact)
            for i in range(loc.count()):
                try:
                    box = loc.nth(i).bounding_box()
                except Exception:
                    continue
                if box and box.get("x", 0) > 300 and box.get("y", 0) < 100:
                    return True
        except Exception:
            continue
    return False


def _wait_switched(page, name: str, timeout: float = SWITCH_TIMEOUT) -> bool:
    """轮询等待右侧会话切到目标。固定 sleep(2~4) 换成轮询后，切得快就能立刻往下走。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if verify_in_conversation(page, name):
            return True
        time.sleep(POLL)
    return False


def search_and_open(page, name: str) -> bool:
    try:
        box = page.get_by_placeholder("搜索", exact=False).first
        if box.count() == 0:
            return False
        box.click()
        box.fill(name)
        # 等搜索结果渲染出来（轮询，比固定 sleep(4) 快很多）
        btn = page.get_by_text("发消息", exact=False).first
        deadline = time.time() + 2.5
        while time.time() < deadline and btn.count() == 0:
            time.sleep(POLL)
        # 优先直接点搜索结果里的「发消息」按钮，最可靠
        if btn.count():
            try:
                btn.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            try:
                btn.click(timeout=6000)
            except Exception:
                try:
                    btn.evaluate("(e) => e.click()")
                except Exception:
                    pass
            time.sleep(0.6)
            return True
        # 否则点精确匹配的结果卡片，再找「发消息」入口
        candidate = page.get_by_text(name, exact=True).first
        if candidate.count() == 0:
            candidate = page.get_by_text(name, exact=False).first
        if candidate.count() == 0:
            return False
        candidate.click(force=True)
        time.sleep(0.5)
        btn = page.get_by_text("发消息", exact=False).first
        if btn.count():
            btn.click(force=True)
            time.sleep(0.5)
        return True
    except Exception as e:
        logger.info("搜索打开 %s 失败: %s", name, e)
        return False


def _paste_text(page, input_box, msg_text: str) -> bool:
    """模拟人工复制粘贴：写剪贴板 → Ctrl+V。失败再回退一次性文本插入。

    逐字 type() 每个字符要等 100ms，一条 10 字文案就要 1 秒多，还得等渲染；
    粘贴是一次性事件，10 字和 100 字耗时一样，这是能做到秒级的关键。
    """
    # 方式一：真正的剪贴板 + Ctrl+V（最接近人工操作）
    try:
        page.evaluate("(t) => navigator.clipboard.writeText(t)", msg_text)
        page.keyboard.press("Control+V")
        deadline = time.time() + 1.2
        while time.time() < deadline:
            try:
                if msg_text in (input_box.inner_text() or ""):
                    return True
            except Exception:
                pass
            time.sleep(0.06)
    except Exception as e:
        logger.info("剪贴板写入失败，回退 insert_text：%s", str(e)[:80])

    # 方式二：insert_text，等价于系统级粘贴（一次性插入，不逐字触发 keydown）
    try:
        input_box.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        page.keyboard.insert_text(msg_text)
        deadline = time.time() + 1.2
        while time.time() < deadline:
            try:
                if msg_text in (input_box.inner_text() or ""):
                    return True
            except Exception:
                pass
            time.sleep(0.06)
    except Exception as e:
        logger.info("insert_text 失败：%s", str(e)[:80])
    return False


def _type_and_send(page, input_box, msg_text: str) -> bool:
    """把文字粘贴进输入框并按 Enter 发送，返回文字是否成功进入输入框。"""
    try:
        input_box.click()
        page.keyboard.press("Control+A")
        page.keyboard.press("Delete")
        time.sleep(0.05)
        if not _paste_text(page, input_box, msg_text):
            cur = ""
            try:
                cur = (input_box.inner_text() or "")[:30]
            except Exception:
                pass
            logger.warning("文字未进入输入框，当前内容: %r", cur)
            return False
        page.keyboard.press("Enter")
        return True
    except Exception as e:
        logger.info("输入/发送异常: %s", str(e)[:100])
        return False


def _wait_input_cleared(input_box, msg_text: str, wait: float = SEND_VERIFY_TIMEOUT) -> bool:
    """消息发出后输入框应不再包含发送文字，以此确认真正发出。"""
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(POLL)
        try:
            cur = input_box.inner_text() or ""
            if msg_text not in cur:
                return True
        except Exception:
            pass
    return False


def send_to_contact(page, name: str, msg_text: str, dry_run: bool) -> tuple[bool, str]:
    switched = False
    for attempt in range(5):
        try:
            target = _find_contact(page, name)
            if target.count():
                el = target.first
                clicked = False
                # 虚拟列表里目标行可能已渲染但不在可见区，force 点击仍会报
                # Element is not visible；先滚到可见，再退回 JS 点击兜底。
                try:
                    el.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                try:
                    el.click(timeout=6000)
                    clicked = True
                except Exception:
                    try:
                        el.evaluate("(e) => e.click()")
                        clicked = True
                        logger.info("联系人 %s：常规点击失败，已用 JS 点击兜底", name)
                    except Exception as e2:
                        logger.info("点击联系人 %s 失败: %s", name, str(e2)[:80])
                if clicked:
                    if _wait_switched(page, name):
                        switched = True
                        break
            else:
                # 目标可能因列表懒加载尚未渲染，滚动侧边栏继续找
                try:
                    page.mouse.move(200, 350)
                    page.mouse.wheel(0, 600)
                except Exception:
                    pass
                time.sleep(0.5)
        except Exception as e:
            logger.info("点击联系人 %s 异常: %s", name, str(e)[:100])
        time.sleep(0.3)

    if not switched and search_and_open(page, name):
        switched = _wait_switched(page, name)

    if not switched:
        return False, "未能切换到该好友会话（名字不在聊天列表，或页面结构变化）"

    if detect_rate_limit(page):
        return False, "检测到「操作频繁 / 安全验证」提示"

    input_box = page.locator('div[contenteditable="true"]').first
    try:
        if input_box.count() == 0 or input_box.bounding_box() is None:
            return False, "找不到聊天输入框"
        input_box.wait_for(state="visible", timeout=3000)
    except Exception:
        return False, "找不到聊天输入框"

    if dry_run:
        return True, "dry-run"

    try:
        if detect_rate_limit(page):
            return False, "发送前检测到验证提示"
        if not _type_and_send(page, input_box, msg_text):
            return False, "文字未能输入到输入框"
        if _wait_input_cleared(input_box, msg_text):
            return True, "ok"
        logger.warning("未检测到消息发出，重试一次：%s", name)
        if detect_rate_limit(page):
            return False, "重试时检测到验证提示"
        if not _type_and_send(page, input_box, msg_text):
            return False, "重试时文字未能输入"
        if _wait_input_cleared(input_box, msg_text):
            return True, "ok"
        return False, "发送后输入框未清空，消息可能未发出"
    except Exception as e:
        logger.info("向 %s 发送异常: %s", name, e)
        return False, f"发送异常: {e}"


def fetch_chat_contacts() -> dict:
    """从抖音私信页左侧聊天列表读取联系人（含火花天数），供网页端勾选。"""
    result = {"at": _now(), "names": [], "error": None}
    if not STATE_PATH.exists():
        result["error"] = "尚未上传登录态 state.json"
        return result

    browser = None
    try:
        p = _start_playwright()
        try:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context = browser.new_context(
                storage_state=str(STATE_PATH),
                viewport={"width": 1366, "height": 768},
            )
            page = context.new_page()

            goto_ok = False
            for attempt in range(3):
                try:
                    page.goto(CHAT_URL, timeout=90000, wait_until="domcontentloaded")
                    goto_ok = True
                    break
                except Exception as e:
                    logger.info("获取联系人时第 %s 次打开页面失败: %s", attempt + 1, str(e)[:80])
                    time.sleep(5)
            if not goto_ok:
                result["error"] = "无法打开抖音私信页面"
                return result

            page.wait_for_timeout(10000)
            logged, why = check_login(page)
            if not logged:
                result["error"] = why
                return result

            extract_js = r"""
                () => {
                    const out = [];
                    const seen = new Set();
                    const isNum = (t) => /^\\d{6,}$/.test(t);
                    document.querySelectorAll('.conversationConversationItemtitle').forEach(t => {
                        const raw = (t.textContent || '').trim();
                        if (!raw) return;

                        // 抖音对部分会话（陌生人/无昵称）会把标题渲染成抖音号或 UID。
                        // 这里多找几个候选来源，优先取非纯数字的昵称。
                        const cands = [raw];
                        const attr = (t.getAttribute('title') || '').trim();
                        if (attr) cands.push(attr);
                        let box = t.parentElement;
                        for (let i = 0; i < 3 && box; i++) {
                            const img = box.querySelector('img');
                            if (img) {
                                const alt = (img.getAttribute('alt') || '').trim();
                                if (alt) cands.push(alt);
                            }
                            const aria = (box.getAttribute('aria-label') || '').trim();
                            if (aria) cands.push(aria);
                            box = box.parentElement;
                        }

                        const display = cands.find(c => c && !isNum(c)) || raw;
                        if (seen.has(display)) return;
                        seen.add(display);

                        // 火花标签只在"本行"内查找，绝不能向上跨到列表容器，
                        // 否则每行都会读到第一行的徽标值（曾导致全部变成同一个数字）
                        let streakText = '';
                        // 向上爬到"恰好只包含本行标题"的最高容器：
                        // 一旦父级出现第 2 个标题就说明已经跨行，必须停。
                        // 注意不能用 closest('[class*="conversationConversationItem"]')，
                        // 标题元素自身的类名就含该串，会匹配到自己。
                        let itemBox = t.parentElement;
                        while (itemBox && itemBox.parentElement
                               && itemBox.parentElement.querySelectorAll('.conversationConversationItemtitle').length <= 1) {
                            itemBox = itemBox.parentElement;
                        }
                        if (itemBox) {
                            let node = itemBox.querySelector('.commonStreaknormalText');
                            if (!node) {
                                const cands = itemBox.querySelectorAll('[class*="treak" i]');
                                for (const c of cands) {
                                    const tx = (c.textContent || '').trim();
                                    if (tx && tx.length <= 12) { node = c; break; }
                                }
                            }
                            if (node) streakText = (node.textContent || '').trim();
                            if (!streakText) {
                                const m = (itemBox.innerText || '').replace(/\s+/g, ' ')
                                    .match(/(点亮中\s*\d\/\d|重燃中\s*\d\/\d)/);
                                if (m) streakText = m[1].trim();
                            }
                        }
                        const numeric = isNum(display);

                        // ---- 预览 / 时间 / 未读 ----
                        // 真实结构（已校准）：itemBox.innerText 的行序是
                        //   [昵称, 时间, 预览, 未读数(可缺省)]
                        // 例：["2181021764691292", "34分钟前", "[早上好]", "1"]
                        let preview = '', timeText = '', unread = 0, unreadConf = 'none';
                        if (itemBox) {
                            const lines = (itemBox.innerText || '')
                                .split(String.fromCharCode(10))
                                .map(s => s.trim())
                                .filter(Boolean);
                            const TIME_RE = /^(\d{1,2}:\d{2}|刚刚|\d+分钟前|\d+小时前|昨天.*|前天.*|(\d{4}\/)?\d{1,2}\/\d{1,2}|\d{1,2}月\d{1,2}日)$/;
                            const NUM_RE = /^\d{1,3}$/;

                            timeText = lines.find(l => TIME_RE.test(l)) || '';

                            // 未读：行尾那个纯数字就是角标（比扫 class 稳）
                            const tail = lines.length ? lines[lines.length - 1] : '';
                            if (tail && NUM_RE.test(tail) && tail !== display && tail !== raw) {
                                unread = parseInt(tail, 10);
                                unreadConf = 'tail-num';
                            }

                            preview = lines.find(l => l
                                && l !== display && l !== raw
                                && l !== timeText && l !== streakText
                                && !(unreadConf === 'tail-num' && l === tail)) || '';
                        }

                        out.push({
                            name: display,
                            // 群聊标记：会话类型图标存在，或名字里明确带"群"。
                            // 注意：这个字段仅供排查参考，真正判定见 _is_group_contact()，
                            // 实测 commonConversationIcon 会把真人（91786913 等）也标上。
                            is_group: !!(itemBox && itemBox.querySelector('[class*="commonConversationIcon"]'))
                                      || /群/.test(display),
                            streak: streakText,
                            raw: raw,
                            is_id: numeric,
                            id_kind: numeric ? (display.length >= 15 ? 'uid' : 'douyin_id') : '',
                            preview: preview.slice(0, 120),
                            time_text: timeText,
                            unread: unread,
                            unread_conf: unreadConf
                        });
                    });
                    return out;
                }
            """

            collected: list[dict] = []
            for attempt in range(3):
                try:
                    page.wait_for_selector(".conversationConversationItemtitle", timeout=45000)
                except Exception:
                    logger.info("第 %s 次等待联系人列表超时", attempt + 1)

                stable = 0
                for _ in range(20):
                    data = page.evaluate(extract_js) or []
                    by_name = {x["name"]: x for x in collected}
                    changed = False
                    for x in data:
                        old = by_name.get(x["name"])
                        if old is None:
                            collected.append(x)
                            by_name[x["name"]] = x
                            changed = True
                        elif not str(old.get("streak", "")).strip() and str(x.get("streak", "")).strip():
                            # 火花是异步渲染的：首轮抓到时可能为空，后续快照到了就补上
                            old["streak"] = x["streak"]
                            changed = True
                    if changed:
                        stable = 0
                    else:
                        stable += 1
                        if stable >= 2:
                            break
                    try:
                        page.mouse.move(200, 350)
                        page.mouse.wheel(0, 800)
                    except Exception:
                        pass
                    page.wait_for_timeout(1200)

                if collected:
                    break
                try:
                    page.reload(wait_until="domcontentloaded", timeout=90000)
                    page.wait_for_timeout(12000)
                except Exception:
                    pass

            # 第二阶段：火花标签是异步渲染的，且列表是虚拟滚动（滚出视口即卸载）。
            # 回到顶部，最多扫 3 个来回；每屏先等火花元素出现再取值，
            # 避免"元素还没渲染就把空值取走"——那正是漏发好友的根因。
            if collected:
                by_name = {x["name"]: x for x in collected}
                before = sum(1 for x in collected if not str(x.get("streak", "")).strip())
                prev_len, prev_spark = -1, -1
                for lap in range(3):
                    cur_len = len(collected)
                    cur_spark = sum(1 for x in collected if str(x.get("streak", "")).strip())
                    if lap > 0 and cur_len == prev_len and cur_spark == prev_spark:
                        logger.info("本轮无新增，提前结束补火花")
                        break
                    try:
                        page.mouse.move(200, 350)
                        page.mouse.wheel(0, -40000)
                        page.wait_for_timeout(1200)
                    except Exception:
                        pass
                    # 注意：不能用"数据没变化"当停止条件——虚拟列表里滚过去的新行
                    # 本来就没有新数据，会导致三轮都在同一屏打转（实测 2.6 秒就退出）。
                    # 这里改为按固定步数扫完整屏，只有"待补归零"才提前结束。
                    for _step in range(28):
                        # 屏内等待：给火花标签的异步渲染留时间（最多 600ms，超时不算错）
                        try:
                            page.wait_for_selector('[class*="treak" i]', timeout=600)
                        except Exception:
                            pass
                        data = page.evaluate(extract_js) or []
                        for x in data:
                            old = by_name.get(x["name"])
                            if old is None:
                                collected.append(x)
                                by_name[x["name"]] = x
                            elif not str(old.get("streak", "")).strip() and str(x.get("streak", "")).strip():
                                old["streak"] = x["streak"]
                        try:
                            page.mouse.wheel(0, 480)
                        except Exception:
                            pass
                        page.wait_for_timeout(500)
                    missing = sum(1 for x in collected if not str(x.get("streak", "")).strip())
                    spark_cnt = len(collected) - missing
                    prev_len, prev_spark = len(collected), spark_cnt
                    logger.info("火花扫描第 %s 轮：共 %s 个会话，其中有火花 %s 个",
                                lap + 1, len(collected), spark_cnt)
                    if missing == 0:
                        break
                after = sum(1 for x in collected if not str(x.get("streak", "")).strip())
                logger.info("火花补齐：空值 %s -> %s", before, after)

            result["names"] = collected
            logger.info("已读取聊天列表联系人 %s 个", len(result["names"]))
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        logger.error("获取联系人异常: %s", e)
        result["error"] = f"获取联系人异常: {e}"
    return result


# 进度：供 /api/status 暴露给前端，避免"运行中但看不到在干什么"
PROGRESS = {"total": 0, "done": 0, "current": "", "phase": "idle", "started_at": ""}


def _is_group_contact(c: dict) -> bool:
    """这个会话是不是群聊——只看名字里有没有「群」字。

    踩过两次坑，所以规则收得很紧：
    - 不能用「预览带 昵称： 前缀」：账号 a 里 91786913 这个真人，预览是
      「AD：分享[评论]」，会被判成群而漏发。
    - 不能用 DOM 的 commonConversationIcon 图标：实测把 91786913、尼尼孩孩、
      18 这几个真人全标成了群（那个 class 不止是群图标），同样会漏发。
    名字里带「群」是目前唯一零误伤的信号；真漏判的代价是往群里发一条消息，
    误判的代价是好友断火花，所以宁可漏判。
    """
    return "群" in str(c.get("name") or "")


def has_spark(streak) -> bool:
    """判断火花状态是否有效。

    抖音这里有三种取值：数字天数、"重燃中 x/3"、"点亮中 x/3"（已断过正在重燃），
    以及空字符串（无火花）。只要不是空的、不是 0，就视为"有火花状态"。
    """
    s = str(streak if streak is not None else "").strip()
    if not s:
        return False
    if s in ("0", "无", "-", "—", "null", "None"):
        return False
    return True


def filter_by_spark(targets: list[str], contacts: list[dict],
                    previous: list[dict] | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """按火花状态过滤待发送名单，返回 (保留, [(被跳过的人, 原因)])。

    只有带火花状态的才执行续期，无火花一律跳过。

    previous 是"上一轮成功抓取"的结果。火花标签是异步渲染的，虚拟列表滚动时
    某些行可能本轮没渲染出火花——如果上一轮明确确认过有火花，就沿用上次判定，
    避免因为渲染抖动把真实好友漏掉；从未确认过的仍然跳过（宁漏勿错）。
    """
    by = {}
    for c in (contacts or []):
        n = str(c.get("name") or "").strip()
        if n:
            by[n] = c

    prev_ok = set()
    for c in (previous or []):
        n = str(c.get("name") or "").strip()
        if n and has_spark(c.get("streak")):
            prev_ok.add(n)

    keep, dropped = [], []
    for t in targets:
        c = by.get(t)
        if c is not None and has_spark(c.get("streak")):
            keep.append(t)
            continue
        if t in prev_ok:
            logger.info("沿用上次火花判定：%s（本轮 streak=%r，判定为异步渲染未补上）",
                        t, (c or {}).get("streak"))
            keep.append(t)
            continue
        if c is None:
            dropped.append((t, "抓取结果里没有该会话，无法确认火花"))
        else:
            dropped.append((t, f"无火花（streak={c.get('streak')!r}）"))
    return keep, dropped


def run_send(dry_run: bool = False, only_names: list[str] | None = None,
             fast: bool = False, previous_contacts: list[dict] | None = None) -> dict:
    """fast=True 表示手动「立即发送」，使用 manual_gap_*；定时任务走 send_gap_*。"""
    cfg = load_config()
    friends = cfg.get("friends") or []
    if only_names is not None:
        friends = [f for f in friends if f in only_names]
    messages = cfg.get("messages") or ["🔥"]
    auto_slogan = bool(cfg.get("auto_slogan", False))
    max_n = int(cfg.get("max_friends_per_run", 20) or 20)
    # 手动与定时用两套间隔：手动要秒级，定时要安全
    gap_key_min = "manual_gap_min" if fast else "send_gap_min"
    gap_key_max = "manual_gap_max" if fast else "send_gap_max"
    gap_min = max(0, int(cfg.get(gap_key_min, 0) or 0))
    gap_max = max(gap_min, int(cfg.get(gap_key_max, 0) or 0))

    result = {
        "at": _now(),
        "dry_run": bool(dry_run),
        "ok": [],
        "failed": [],
        "logged_out": False,
        "rate_limited": False,
        "stopped": False,
    }
    clear_stop()  # 每轮开始清掉上一轮残留的停止信号

    if not STATE_PATH.exists():
        result["failed"].append({"name": "_system", "reason": "尚未上传登录态 state.json"})
        return result

    # ---- 确定这一轮发给谁 ----
    # friends_mode = "all_spark"：不看手写名单，直接从抓取结果里取所有有火花的会话
    # friends_mode = "list"     ：按手写名单，再按火花过滤
    friends_mode = str(cfg.get("friends_mode") or "list")
    try:
        contacts = load_runtime().get("contacts") or []
    except Exception:
        contacts = []

    if friends_mode == "all_spark":
        if not contacts:
            logger.warning("全部火花模式：还没有抓取到会话列表，本轮跳过")
            result["failed"].append({"name": "_system",
                                     "reason": "名单模式为「全部有火花的好友」，但尚未抓取到聊天列表，本轮不发"})
            return result
        cached_names = {str(c.get("name") or "") for c in contacts}
        if only_names is not None:
            # 补发场景：只补上次失败的那几个
            targets = [n for n in only_names if n in cached_names]
        else:
            targets = [str(c.get("name")) for c in contacts
                       if c.get("name") and has_spark(c.get("streak"))
                       and not _is_group_contact(c)]
        logger.info("全部火花模式：%s 个会话中筛出 %s 个有火花", len(contacts), len(targets))
        if not targets:
            logger.warning("全部火花模式：没有任何会话带火花，本轮不发")
            result["failed"].append({"name": "_system",
                                     "reason": "抓取结果里没有任何带火花的好友，本轮跳过（0 条发出）"})
            return result
    else:
        targets = list(friends)

        # 仅火花模式：按火花状态过滤（手写名单时的保险）
        if cfg.get("only_with_spark", True):
            if not contacts:
                logger.warning("仅火花模式：还没有火花数据，本轮跳过（请先在「好友与消息」获取聊天列表）")
                result["failed"].append({"name": "_system",
                                         "reason": "已开启「仅发送给有火花的好友」，但尚未抓取到火花数据，本轮不发"})
                return result
            targets, dropped = filter_by_spark(targets, contacts, previous_contacts)
            for name, why in dropped:
                logger.info("跳过 %s：%s", name, why)
            if not targets:
                logger.warning("仅火花模式：名单里 %s 人都没有火花，本轮不发", len(dropped))
                result["failed"].append({"name": "_system",
                                         "reason": f"名单里的 {len(dropped)} 位好友都没有火花状态，本轮跳过（0 条发出）"})
                return result
            logger.info("仅火花模式：%s 人有火花待发，%s 人跳过", len(targets), len(dropped))
            # 群聊一律不发：抖音群也带火花徽标，不排除就会往群里发"续火花"
            _groups = {str(c.get("name") or "") for c in contacts if _is_group_contact(c)}
            if _groups:
                _kept = [t for t in targets if t not in _groups]
                if len(_kept) != len(targets):
                    logger.info("跳过 %s 个群聊会话", len(targets) - len(_kept))
                targets = _kept

    # 截断放在过滤之后，避免"前 N 个里一半没火花"把人挤掉
    if max_n > 0:
        targets = targets[:max_n]

    PROGRESS.update(total=len(targets), done=0, current="", phase="starting",
                    started_at=_now())
    browser = None
    try:
        p = _start_playwright()
        try:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            context = browser.new_context(
                storage_state=str(STATE_PATH),
                viewport={"width": 1366, "height": 768},
            )
            # headless 下 clipboard.writeText 默认被拒（NotAllowedError），
            # 必须先授予剪贴板权限，Ctrl+V 的粘贴路径才能真正生效。
            try:
                context.grant_permissions(["clipboard-read", "clipboard-write"])
            except Exception as e:
                logger.info("授予剪贴板权限失败（将回退 insert_text）：%s", str(e)[:80])
            page = context.new_page()

            goto_ok = False
            for attempt in range(3):
                try:
                    page.goto(CHAT_URL, timeout=60000, wait_until="domcontentloaded")
                    goto_ok = True
                    break
                except Exception as e:
                    logger.info("第 %s 次打开页面失败: %s", attempt + 1, str(e)[:80])
                    time.sleep(5)
            if not goto_ok:
                result["failed"].append({"name": "_system", "reason": "无法打开抖音私信页面"})
                return result

            time.sleep(8)
            logged, why = check_login(page)
            if not logged:
                result["logged_out"] = True
                result["failed"].append({"name": "_system", "reason": why})
                _screenshot(page)
                return result

            # 关键修复：新登录/冷启动时会话列表可能 8 秒内渲染不完，
            # 过早查找会全部报"未能切换到该好友会话"。这里等列表真正出现。
            try:
                page.wait_for_selector(".conversationConversationItemtitle", timeout=45000)
                logger.info("会话列表已渲染")
            except Exception:
                logger.warning("等待会话列表超时，继续尝试定位好友（可能失败）")
            time.sleep(2)

            if not targets:
                logger.info("未配置任何好友，跳过发送")
                return result

            logger.info("待发送好友 %s 人，dry_run=%s，%s模式，间隔 %s~%s 秒",
                        len(targets), dry_run, "手动" if fast else "定时", gap_min, gap_max)
            run_t0 = time.time()
            for idx, name in enumerate(targets, 1):
                if _stopped():
                    logger.info("收到停止指令，本轮在第 %s 人前中断", idx)
                    result["stopped"] = True
                    break
                if auto_slogan:
                    # 与预览共用同一入口，保证「预览看到的」就是「真发出去的」
                    msg = slogan.for_friend(slogan.today_key(), name)
                else:
                    msg = random.choice(messages)
                PROGRESS.update(current=name, phase="sending")
                t0 = time.time()
                ok, why = send_to_contact(page, name, msg, dry_run)
                PROGRESS.update(done=PROGRESS["done"] + 1, current="", phase="waiting")
                cost = time.time() - t0
                if ok:
                    result["ok"].append(name)
                    logger.info("已发送给 %s：%s（耗时 %.1f 秒，累计 %.1f 秒）",
                                name, msg if not dry_run else "(干跑，未真实发送)",
                                cost, time.time() - run_t0)
                else:
                    result["failed"].append({"name": name, "reason": why})
                    logger.warning("发送给 %s 失败：%s（耗时 %.1f 秒）", name, why, cost)
                    if detect_rate_limit(page):
                        result["rate_limited"] = True
                        logger.warning("疑似触发限流，停止本轮")
                        break
                # 间隔用 Event.wait 而不是 sleep：点「停止」能立刻退出
                gap = random.uniform(gap_min, gap_max)
                if gap <= 0:
                    gap = random.uniform(0.25, 0.6)  # 最小喘息，避免完全定频
                if STOP_EVENT.wait(gap):
                    logger.info("收到停止指令，间隔等待被中断")
                    result["stopped"] = True
                    break
            logger.info("本轮结束：共 %s 人，成功 %s，失败 %s，总耗时 %.1f 秒",
                        len(result["ok"]) + len(result["failed"]),
                        len(result["ok"]), len(result["failed"]), time.time() - run_t0)
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            p.stop()
    except Exception as e:
        logger.error("运行异常: %s", e)
        result["failed"].append({"name": "_system", "reason": f"运行异常: {e}"})
    return result
