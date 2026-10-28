"""会话读取层：扫描收件箱、读取最近消息、发送回复。

选择器状态（均已线上校准）：
- 会话行标题：`.conversationConversationItemtitle`
- 行内时间：`[class*="ConversationItemTagNextToTitletime"]`
- 行内预览：`[class*="ConversationItemDescleft"]`
- 行内火花：`[class*="commonStreaknormalText"]`
- 群聊标记：行内存在 `[class*="commonConversationIcon"]`（或预览带「昵称：」发送者前缀）
- 消息气泡：`[class*="MessageItem"]` + `container`，取最外层，方向按输入框中心线判断

性能：所有浏览器操作都走 `browser_pool`（常驻会话 + 预热提前退出），
首次调用约 15~40 秒，之后 2~4 秒。
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import date, datetime, timedelta
from typing import Any

from . import automation, browser_pool

logger = logging.getLogger("douyin-spark")

CHAT_URL = "https://www.douyin.com/chat"

# 收件箱推荐范围：最近 N 天有聊天记录、且不是群聊
INBOX_DAYS = 10
EXCLUDE_GROUPS = True


# --------------------------------------------------------------------------- 工具

def _today() -> date:
    return datetime.now().date()


def parse_time_to_date(text: str, now: date | None = None) -> date | None:
    """把列表里的相对时间文案解析成日期。解析不出来返回 None。

    抖音列表的时间文案实测有这几种：
        刚刚 / 8分钟前 / 3小时前 / 14:28 / 昨天 21:32 / 前天 09:10
        10/02 / 2025/10/02 / 10月2日
    只有"今天/昨天/前天/具体日期"能落到日期，其余一律当未知（按不在范围内处理）。
    """
    now = now or _today()
    s = str(text or "").strip()
    if not s:
        return None

    if s in ("刚刚", "刚才") or "分钟前" in s or "小时前" in s:
        return now
    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        return now
    if s.startswith("昨天"):
        return now - timedelta(days=1)
    if s.startswith("前天"):
        return now - timedelta(days=2)

    m = re.fullmatch(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None

    m = re.fullmatch(r"(\d{1,2})[/\-](\d{1,2})", s)
    if m:
        mo, dd = int(m.group(1)), int(m.group(2))
        try:
            d = date(now.year, mo, dd)
        except ValueError:
            return None
        return d if d <= now else date(now.year - 1, mo, dd)

    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})日", s)
    if m:
        try:
            d = date(now.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        return d if d <= now else date(now.year - 1, int(m.group(1)), int(m.group(2)))

    # 「周二」「星期一」这类：列表里只出现在 2 天以前，所以同一天按上周算
    m = re.fullmatch(r"(?:周|星期)([一二三四五六日天])", s)
    if m:
        idx = "一二三四五六日".find(m.group(1))
        if m.group(1) == "天":
            idx = 6
        delta = (now.weekday() - idx) % 7
        return now - timedelta(days=delta or 7)

    return None


# --------------------------------------------------------------------------- 列表解析

_ROWS_JS = """
() => {
  const titles = [...document.querySelectorAll('.conversationConversationItemtitle')];
  const boxOf = (t) => { let x = t; for (let k = 0; k < 4 && x.parentElement; k++) x = x.parentElement; return x; };
  const out = [];
  for (const t of titles) {
    const name = (t.textContent || '').trim();
    if (!name) continue;
    const box = boxOf(t);
    const q = (sel) => { const e = box.querySelector(sel); return e ? (e.textContent || '').trim() : ''; };

    const timeText = q('[class*="ConversationItemTagNextToTitletime"]');
    const streak = q('[class*="commonStreaknormalText"]');
    let desc = q('[class*="ConversationItemDescleft"]');

    // innerText 只有行可见时才有值：未读角标靠它兜底，拿不到就按 0 处理
    const lines = (box.innerText || '').split(String.fromCharCode(10))
                    .map(s => s.trim()).filter(Boolean);
    let unread = 0, unreadConf = 'none';
    const tail = lines.length ? lines[lines.length - 1] : '';
    if (/^\\d{1,3}$/.test(tail) && tail !== name && tail !== streak) {
      unread = parseInt(tail, 10);
      unreadConf = 'tail-num';
    }
    if (!desc) {
      desc = lines.find(l => l && l !== name && l !== timeText && l !== streak
                             && !/^\\d{1,3}$/.test(l)) || '';
    }
    // 时间兜底：个别行的专用时间节点为空，就从行文本里认一个时间样式
    let timeOut = timeText;
    if (!timeOut) {
      const TIME_RE = /^(\\d{1,2}:\\d{2}|刚刚|\\d+分钟前|\\d+小时前|昨天.*|前天.*|(\\d{4}\\/)?\\d{1,2}\\/\\d{1,2}|\\d{1,2}月\\d{1,2}日|(?:周|星期)[一二三四五六日天])$/;
      timeOut = lines.find(l => TIME_RE.test(l)) || '';
    }

    // 群聊判定：只认名字里的「群」字，与 automation._is_group_contact 保持一致。
    // 另外两种信号都实测会误伤真人（一旦误判，这个人的会话就从收件箱里消失了）：
    //   · commonConversationIcon 图标：把 18、91786913、尼尼孩孩、用户8992652113758 标成群；
    //   · 预览的「发送者：」前缀：91786913 的预览是「AD：分享[评论]」，同样被误判。
    const isGroup = /群/.test(name);

    out.push({
      name: name,
      time_text: timeOut,
      preview: desc.slice(0, 120),
      streak: streak,
      unread: unread,
      unread_conf: unreadConf,
      is_group: isGroup
    });
  }
  return out;
}
"""

_SCROLL_JS = """
() => {
  const t = document.querySelector('.conversationConversationItemtitle');
  if (!t) return false;
  let sc = null, el = t;
  while (el && el !== document.body) {
    // 只要求「能滚」，不再限制容器高度：账号 b 的列表容器高度不足 200px，
    // 原来的条件会一路找到 body 返回 null，表现成"只读到首屏十几行"。
    if (el.scrollHeight > el.clientHeight + 20) { sc = el; break; }
    el = el.parentElement;
  }
  if (!sc) return false;
  const before = sc.scrollTop;
  sc.scrollTop = before + Math.round(sc.clientHeight * 0.85);
  return Math.abs(sc.scrollTop - before) > 4;
}
"""

_SCROLL_TOP_JS = """
() => {
  const t = document.querySelector('.conversationConversationItemtitle');
  if (!t) return false;
  let sc = null, el = t;
  while (el && el !== document.body) {
    // 只要求「能滚」，不再限制容器高度：账号 b 的列表容器高度不足 200px，
    // 原来的条件会一路找到 body 返回 null，表现成"只读到首屏十几行"。
    if (el.scrollHeight > el.clientHeight + 20) { sc = el; break; }
    el = el.parentElement;
  }
  if (!sc) return false;
  sc.scrollTop = 0;
  return true;
}
"""


def _scroll_top(page) -> None:
    """把列表拉回顶部。

    页面是复用的：上一次扫描结束时会停在底部，如果直接再扫，只能看到最老的一批
    会话（实测第二次扫描命中 0 条）。所以每次操作前先归位。
    """
    try:
        deadline = time.time() + 3
        while time.time() < deadline:
            n = page.evaluate(_SCROLL_TOP_JS) and page.locator(
                ".conversationConversationItemtitle").count()
            # 归位后要等行重新渲染出来，否则紧接着找目标会找不到
            if n and n > 0:
                break
            page.wait_for_timeout(250)
        page.wait_for_timeout(220)
    except Exception:
        pass


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    """去掉「同一会话渲染成两条」的重复。

    抖音对没有昵称的会话会把标题渲染成 UID，而同一行里还挂着昵称节点，于是
    会同时出现 `3822355017373790` 和 `别问` 两条，时间和预览完全一样。
    按 (时间, 预览) 归组，优先保留非纯数字的那个名字。
    """
    out: list[dict] = []
    index: dict[tuple, int] = {}
    for r in rows:
        key = (r.get("time_text") or "", r.get("preview") or "")
        if not key[0] and not key[1]:
            out.append(r)
            continue
        i = index.get(key)
        if i is None:
            index[key] = len(out)
            out.append(r)
            continue
        cur_numeric = bool(re.fullmatch(r"\d{6,}", str(out[i].get("name") or "")))
        new_numeric = bool(re.fullmatch(r"\d{6,}", str(r.get("name") or "")))
        # 只有"一个是 UID、一个是昵称"才算重复。两个都是昵称的即使时间和预览一样，
        # 也可能是两个不同的人，不能合并。
        if cur_numeric == new_numeric:
            out.append(r)
            continue
        if cur_numeric and not new_numeric:
            out[i] = r
    return out


def _collect_rows(page, max_steps: int = 18) -> list[dict]:
    """滚动并合并整张会话列表。

    列表是虚拟渲染的：只有可见的行 innerText 才有值（时间/预览/未读都依赖它），
    所以必须"边滚边收"，按昵称合并，并用信息更全的那一份覆盖。
    """
    # 1) 归位到顶部，否则复用页面时只能看到上一轮停在底部的那批老会话
    _scroll_top(page)

    # 2) 等第一行渲染出来（冷启动时抖音的会话列表要几十秒才出第一批）
    deadline = time.time() + 55
    while time.time() < deadline:
        try:
            if page.locator(".conversationConversationItemtitle").count() > 0:
                break
        except Exception:
            pass
        page.wait_for_timeout(1000)

    # 2.5) 列表可能只渲染了寥寥几行（页面刚被预热打开、数据还没回来）。
    #      此时 scrollHeight 撑不开，_SCROLL_JS 找不到可滚动容器会直接返回 false，
    #      整轮扫描就以 2~3 条草草结束——用户看到的就是"扫描只出来几条"。
    #      先等它长到 8 行再开始滚，最多等 20 秒。
    grow_deadline = time.time() + 20
    while time.time() < grow_deadline:
        try:
            if page.locator(".conversationConversationItemtitle").count() >= 8:
                break
        except Exception:
            pass
        page.wait_for_timeout(1000)

    seen: dict[str, dict] = {}
    stall = 0
    for step in range(max_steps):
        try:
            batch = page.evaluate(_ROWS_JS) or []
        except Exception as e:
            logger.warning("解析会话行失败：%s", str(e)[:120])
            break

        fresh = 0
        for r in batch:
            name = (r.get("name") or "").strip()
            if not name:
                continue
            old = seen.get(name)
            if old is None:
                seen[name] = r
                fresh += 1
            elif not old.get("time_text") and r.get("time_text"):
                # 后一次滚动让它可见了，时间/预览/未读都更全
                seen[name] = r
                fresh += 1

        try:
            moved = bool(page.evaluate(_SCROLL_JS))
        except Exception:
            moved = False

        page.wait_for_timeout(700 if step else 1500)
        if not moved:
            # 刚重启 / 刚预热完时，列表容器可能还没撑开，这一步滚不动是正常的。
            # 前两步先等一等再试，否则会只收到 2~5 行，表现为"扫描只出来几条"。
            if step < 2:
                page.wait_for_timeout(1200)
                continue
            break
        stall = stall + 1 if fresh == 0 else 0
        if stall >= 2:
            break
    return _dedupe_rows(list(seen.values()))


# --------------------------------------------------------------------------- 收件箱

def scan_inbox(days: int = INBOX_DAYS, exclude_groups: bool = EXCLUDE_GROUPS) -> dict:
    """扫描收件箱，只保留「最近 days 天有聊天记录」且「不是群聊」的会话。

    只读：整条链路不点击任何会话，因此不会把未读标记成已读。
    """
    if not automation.STATE_PATH.exists():
        return {"at": automation._now(), "items": [], "error": "尚未上传登录态 state.json"}

    t0 = time.time()
    try:
        # 扫描不需要等"整张列表热起来"：有 2 行就先开工，靠滚动把剩下的带出来，
        # 这样首屏等待从 ~56 秒降到 ~15 秒，整轮更快。
        rows = browser_pool.call(_collect_rows, timeout=300, min_items=2, max_wait=60)
    except (TimeoutError, FuturesTimeoutError):
        # 5 分钟还没跑完：多半是浏览器会话被别的任务占着，或抖音页面一直不出列表
        return {"at": automation._now(), "items": [],
                "error": "扫描超时：浏览器会话被占用或抖音页面加载过慢，请等 10 秒再点一次"}
    except Exception as e:
        msg = str(e)[:140] or e.__class__.__name__
        return {"at": automation._now(), "items": [], "error": f"扫描失败：{msg}"}

    today = _today()

    # 第一遍：解析日期。虚拟渲染下总有几行 innerText 取不到，时间就是空的。
    dated: list[list] = []
    for r in rows:
        d = parse_time_to_date(r.get("time_text"), today)
        dated.append([r, d, "text" if d else None])

    # 第二遍：时间缺失的行按"列表按最后消息倒序"这个事实保守推断——
    # 取它下面第一个已知日期（更老的那个上界），推断不出来才真的算未知。
    for i, row in enumerate(dated):
        if row[1] is not None:
            continue
        for j in range(i + 1, len(dated)):
            if dated[j][1] is not None:
                row[1] = dated[j][1]
                row[2] = "order"
                break

    items, groups, old, unknown = [], [], [], []
    for r, d, src in dated:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        if exclude_groups and r.get("is_group"):
            groups.append(name)
            continue
        if d is None:
            unknown.append(name)
            continue
        age = (today - d).days
        if age > days:
            old.append(name)
            continue
        items.append({
            "name": name,
            "preview": r.get("preview") or "",
            "time_text": r.get("time_text") or "",
            "unread": int(r.get("unread") or 0),
            "unread_conf": r.get("unread_conf") or "none",
            "streak": r.get("streak") or "",
            "date_source": src,
            "is_group": False,
            "last_date": d.isoformat(),
            "age_days": age,
            "in_scope": True,
        })

    # 今天的排前面，其余按"越近越前"
    items.sort(key=lambda x: (x["age_days"], -int(x["unread"])))

    scope = {
        "days": days,
        "exclude_groups": bool(exclude_groups),
        "rows": len(rows),
        "kept": len(items),
        "excluded_groups": len(groups),
        "excluded_old": len(old),
        "excluded_unknown": len(unknown),
        "excluded_groups_sample": groups[:8],
        "excluded_unknown_sample": unknown[:8],
        "elapsed_sec": round(time.time() - t0, 1),
    }
    logger.info(
        "收件箱扫描：%s 行 → 命中 %s（近 %s 天、非群聊）；排除群聊 %s、超期 %s、时间未知 %s；%.1fs",
        len(rows), len(items), days, len(groups), len(old), len(unknown), time.time() - t0,
    )
    return {"at": automation._now(), "items": items, "scope": scope, "error": None}


def peek_list(limit: int = 30) -> dict:
    """只读会话列表的前若干行：不点击、不滚动，开销很小。

    用于实时监听"有没有新消息"——列表预览就是最新一条消息，
    预览变了说明有新内容，再决定要不要打开会话。
    """
    t0 = time.time()
    try:
        rows = browser_pool.call(lambda page: page.evaluate(_ROWS_JS) or [],
                                 timeout=120, min_items=2, max_wait=25)
    except Exception as e:
        return {"at": automation._now(), "items": [], "error": f"读取会话列表失败：{str(e)[:120]}"}

    items = []
    for r in (rows or [])[:limit]:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        items.append({
            "name": name,
            "preview": (r.get("preview") or "")[:120],
            "time_text": r.get("time_text") or "",
            "unread": int(r.get("unread") or 0),
            "is_group": bool(r.get("is_group")),
        })
    return {"at": automation._now(), "items": items, "error": None,
            "elapsed": round(time.time() - t0, 1)}


# --------------------------------------------------------------------------- 会话内容

_MSG_JS = """
(limit) => {
  // 页面里可能残留上一个会话的面板，必须挑"当前可见"的输入框，
  // 否则会读到别的会话的消息（实测出现过：给 A 生成建议，读到的却是 B 的内容）
  const inputs = [...document.querySelectorAll('div[contenteditable="true"]')]
    .filter(el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
    .sort((a, b) => b.getBoundingClientRect().width - a.getBoundingClientRect().width);
  const input = inputs[0];
  if (!input) return {items: [], conf: 'no-input'};

  // 面板 = 同时包含输入框和消息气泡的那个祖先，读取范围严格限制在它内部
  let panel = input;
  while (panel.parentElement && !panel.querySelector('[class*="MessageItem"]')) {
    panel = panel.parentElement;
  }

  const ir = input.getBoundingClientRect();
  const midX = (ir.left + ir.right) / 2;

  // 每条消息的容器类名形如 MessageItemTextcontainer / MessageItemImageContainer，
  // 嵌套层级会重复命中，所以只取"父级不再是同类容器"的最外层
  const isMsgBox = (el) => /MessageItem/i.test((el.className || '').toString())
                        && /container/i.test((el.className || '').toString());
  const all = [...panel.querySelectorAll('[class*="MessageItem"]')]
    .filter(el => isMsgBox(el))
    .filter(el => !(el.parentElement && isMsgBox(el.parentElement)));

  // 系统提示文案：这些不是"对方说的话"，绝不能进 last_in
  const SYS_TEXT = /^(该用户已注销|对方已注销|你撤回了一条消息|.{0,12}撤回了一条消息|以下为新消息|消息已发出.*|对方正在输入.*)$/;

  // 切换会话的过渡期里，面板中可能同时挂着"旧会话"和"新会话"两套消息列表。
  // 给每条消息标上它所属的滚动容器（sid），Python 侧再按预览锚点选对那一组。
  const sidMap = new Map();
  const sidOf = (el) => {
    let p = el.parentElement;
    while (p && p !== panel) {
      if (p.scrollHeight > p.clientHeight + 20 && p.clientHeight > 150) {
        if (!sidMap.has(p)) sidMap.set(p, sidMap.size);
        return sidMap.get(p);
      }
      p = p.parentElement;
    }
    return -1;
  };

  const seen = new Set();
  const items = [];
  for (const el of all) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 16 || rect.height < 8) continue;
    const text = (el.innerText || '').trim().replace(/\\s+/g, ' ');
    if (!text) continue;                       // 图片/语音等无文字消息直接略过

    const cls = (el.className || '').toString();
    let kind = 'text';
    if (/ShareAweme|ShareCard|Aweme/i.test(cls)) kind = 'card';       // 分享视频/作品卡片
    else if (/Image/i.test(cls)) kind = 'image';
    else if (/Video/i.test(cls)) kind = 'video';
    else if (/Emoji|Sticker|Expression/i.test(cls)) kind = 'emoji';
    else if (/System|Notice|Tip/i.test(cls)) kind = 'system';

    const cx = rect.left + rect.width / 2;
    if (kind === 'text') {
      // 系统提示常居中、字号小、无气泡背景
      if (SYS_TEXT.test(text)) kind = 'system';
      else if (Math.abs(cx - midX) < 24 && text.length < 30) kind = 'system';
    }
    if (kind === 'system') continue;           // 直接丢弃，不参与任何判断

    const key = text + '|' + Math.round(rect.top);
    if (seen.has(key)) continue;
    seen.add(key);

    items.push({
      text: text.slice(0, 300),
      dir: cx > midX ? 'out' : 'in',           // 右半区=我发的，左半区=对方
      kind: kind,                              // text / card / image / video / emoji
      x: Math.round(cx),
      top: Math.round(rect.top),
      sid: sidOf(el)          // 所属消息列表（0/1/...；-1 表示没找到滚动容器）
    });
  }

  // ⚠️ DOM 顺序不可靠（实测第一条反而是最新的），必须按屏幕坐标排：
  // y 小 = 屏幕上方 = 更早，所以升序即"从旧到新"
  items.sort((a, b) => a.top - b.top);
  const keep = Math.max(limit, 20);
  return {items: items, keep: keep, conf: items.length ? 'geom' : 'empty',
          panels: inputs.length};
}
"""


def read_recent(page, limit: int = 8) -> dict:
    """读取当前会话最近 limit 条消息。调用前需已切换到目标会话。"""
    try:
        return page.evaluate(_MSG_JS, limit) or {"items": [], "conf": "empty"}
    except Exception as e:
        return {"items": [], "conf": "error", "error": str(e)[:120]}


def pick_group(got: dict, expect: str = "", limit: int = 8) -> dict:
    """从可能混着两套列表的读取结果里，选出"当前会话"那一组。

    判定依据：列表预览（expect）必然出现在当前会话的消息里，
    所以含它的那一组才是我们要的；没给 expect 时退化为"条数最多的一组"。
    """
    items = got.get("items") or []
    keep = int(got.get("keep") or max(limit, 20))
    if not items:
        return {"items": [], "conf": got.get("conf") or "empty"}

    groups: dict[Any, list] = {}
    for m in items:
        groups.setdefault(m.get("sid", -1), []).append(m)

    chosen = None
    if expect:
        for arr in groups.values():
            if any(expect in (m.get("text") or "") for m in arr):
                chosen = arr
                break
    if chosen is None:
        chosen = max(groups.values(), key=len)

    chosen = sorted(chosen, key=lambda m: m.get("top") or 0)
    out = {"items": chosen[-keep:], "conf": got.get("conf") or "geom"}
    if len(groups) > 1:
        out["groups"] = len(groups)
    return out


def read_settled(page, limit: int = 8, tries: int = 5) -> dict:
    """连续两次读到一样的内容才采信。

    实测坑：点击会话后，右侧**标题先变、消息列表后变**。如果只看标题就开始读，
    拿到的还是上一个会话的消息——曾因此对"厉害呀_"的对话生成出与内容无关的回复。
    """
    prev = None
    for _ in range(tries):
        cur = read_recent(page, limit)
        if prev is not None and cur.get("items") == prev.get("items"):
            return cur
        prev = cur
        page.wait_for_timeout(320)
    return prev or {"items": [], "conf": "empty"}


def open_and_read(name: str, limit: int = 8, expect: str = "") -> dict:
    """打开某个联系人的会话并读最近消息。

    ⚠️ 副作用：打开会话会把未读标记成已读。
    首次调用要等列表预热；之后复用同一个页面，通常 2~4 秒。

    `expect`：该会话"最新一条消息"的预览文字（来自收件箱列表）。传入后会以它为准
    做内容校验——列表预览就是最新一条消息，读到的消息里必须包含它，
    否则说明标题已切但消息还没切（实测的稳定复现现象），直接放弃而不是给出错内容。
    """
    t0 = time.time()
    expect = (expect or "").strip()
    # 「分享[视频]」这类占位预览不含真实文字，不能当内容锚点
    placeholder = bool(re.search(r"\[(视频|语音|表情|图片|直播|音乐|商品|位置)\]", expect))
    check = bool(expect) and not placeholder

    def _hit(items) -> bool:
        return any(expect in (m.get("text") or "") for m in (items or []))

    def _work(page):
        _scroll_top(page)          # 复用页面时可能停在列表底部，先归位再找目标
        ok, why = automation.send_to_contact(page, name, "", dry_run=True)
        if not ok:
            return {"items": [], "conf": "switch-failed", "error": why}
        # 标题校验只能挡住"点错了人"，挡不住"标题已变、消息还没变"
        if not automation.verify_in_conversation(page, name):
            return {"items": [], "conf": "wrong-conversation",
                    "error": "切换后标题与目标不一致，已放弃读取（避免用到别的会话内容）"}

        page.wait_for_timeout(350)
        if not check:
            return pick_group(read_settled(page, limit), "", limit)

        # 轮询等真正的会话内容渲染出来：列表预览必须出现在选中的那一组里
        deadline = time.time() + 6
        while True:
            raw = read_settled(page, limit, tries=2)
            got = pick_group(raw, expect, limit)
            if _hit(got.get("items")):
                return got
            if time.time() > deadline:
                return {"items": [], "conf": "mismatch",
                        "error": (f"读到的内容与列表预览对不上（预览是「{expect[:16]}」），"
                                  "可能还没切换完，已放弃以免答非所问")}
            page.wait_for_timeout(400)

    try:
        got = browser_pool.call(_work, timeout=240, target=name, min_items=3, max_wait=50)
    except Exception as e:
        return {"items": [], "conf": "error", "error": str(e)[:140],
                "elapsed_sec": round(time.time() - t0, 1)}

    got["elapsed_sec"] = round(time.time() - t0, 1)
    return got


def send_text(name: str, text: str) -> dict:
    """向某个联系人发送一条文本（复用已验收的粘贴 + Enter 链路，页面复用）。"""
    t0 = time.time()

    def _work(page):
        _scroll_top(page)
        return automation.send_to_contact(page, name, text, dry_run=False)

    try:
        ok, why = browser_pool.call(_work, timeout=180, target=name,
                                    min_items=3, max_wait=50)
    except Exception as e:
        return {"ok": False, "reason": str(e)[:140],
                "elapsed_sec": round(time.time() - t0, 1)}
    return {"ok": bool(ok), "reason": why, "elapsed_sec": round(time.time() - t0, 1)}
