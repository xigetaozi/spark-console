"""定位火花标签在 DOM 里的确切位置：对已知有火花的好友，逐层打印祖先链。"""
import json
import time
from playwright.sync_api import sync_playwright

CHAT_URL = "https://www.douyin.com/chat"
STATE = "/opt/douyin-spark-data/a/state.json"
TARGETS = ["1Sternstunde", "天上鑫", "巷口"]

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=[
        "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
    ctx = b.new_context(storage_state=STATE, viewport={"width": 1366, "height": 900})
    pg = ctx.new_page()
    pg.goto(CHAT_URL, wait_until="domcontentloaded", timeout=90000)
    pg.wait_for_selector(".conversationConversationItemtitle", timeout=60000)
    time.sleep(4)

    res = pg.evaluate("""(targets) => {
      const out = {};
      const titles = [...document.querySelectorAll('.conversationConversationItemtitle')];
      for (const t of titles) {
        const name = (t.textContent || '').trim();
        if (!targets.includes(name)) continue;
        const chain = [];
        let cur = t;
        for (let i = 0; i < 7 && cur; i++) {
          const streakEls = [...cur.querySelectorAll('[class*="treak" i]')].map(e => ({
            cls: (e.className || '').toString().slice(0, 40),
            txt: (e.textContent || '').trim().slice(0, 12)
          })).filter(x => x.txt);
          chain.push({
            level: i,
            tag: cur.tagName,
            cls: (cur.className || '').toString().slice(0, 60),
            titleCount: cur.querySelectorAll('.conversationConversationItemtitle').length,
            streakEls: streakEls.slice(0, 3),
            textHead: (cur.innerText || '').replace(/\\s+/g, ' ').slice(0, 60)
          });
          cur = cur.parentElement;
        }
        out[name] = chain;
      }
      return out;
    }""", TARGETS)

    print(json.dumps(res, ensure_ascii=False, indent=1)[:4000])
    b.close()
