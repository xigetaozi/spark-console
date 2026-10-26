"""诊断：聊天列表里"纯数字"条目到底是抖音本身显示 ID，还是抓错节点。"""
import re
import time
from playwright.sync_api import sync_playwright

CHAT_URL = "https://www.douyin.com/chat"
STATE = "/opt/douyin-spark-data/a/state.json"

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=[
        "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
    ctx = b.new_context(storage_state=STATE, viewport={"width": 1366, "height": 900})
    pg = ctx.new_page()
    pg.goto(CHAT_URL, wait_until="domcontentloaded", timeout=60000)
    pg.wait_for_selector(".conversationConversationItemtitle", timeout=45000)
    time.sleep(3)

    items = pg.evaluate("""() => {
      const out = [];
      document.querySelectorAll('.conversationConversationItemtitle').forEach((el, i) => {
        if (i >= 30) return;
        const title = (el.textContent || '').trim();
        const wrap = el.closest('[class*="conversationConversationItem"]') || el.parentElement;
        // 候选字段
        const cand = {};
        try {
          const img = wrap.querySelector('img');
          cand.imgAlt = img ? (img.getAttribute('alt') || '') : '';
        } catch(e) {}
        try {
          const a = wrap.querySelector('a');
          cand.href = a ? (a.getAttribute('href') || '') : '';
        } catch(e) {}
        try { cand.aria = wrap.getAttribute('aria-label') || ''; } catch(e) {}
        try { cand.titleAttr = el.getAttribute('title') || ''; } catch(e) {}
        try {
          // 同一行内其它文本（可能是备注/昵称）
          const texts = [...wrap.querySelectorAll('*')]
            .map(n => (n.childElementCount === 0 ? (n.textContent||'').trim() : ''))
            .filter(t => t && t.length < 30);
          cand.lineTexts = [...new Set(texts)].slice(0, 6);
        } catch(e) { cand.lineTexts = []; }
        let cls = '';
        try { cls = (wrap.className || '').toString().slice(0, 60); } catch(e) {}
        out.push({i, title, cls, ...cand});
      });
      return out;
    }""")

    numeric = [it for it in items if re.fullmatch(r"\d{6,}", it["title"])]
    print("=== 列表前 30 项中，纯数字标题 %d 个 ===" % len(numeric))
    for it in numeric[:3]:
        print("\n--- 数字项 #%d ---" % it["i"])
        print("  title      :", it["title"])
        print("  imgAlt     :", it["imgAlt"][:40])
        print("  href       :", it["href"][:80])
        print("  aria       :", it["aria"][:60])
        print("  titleAttr  :", it["titleAttr"][:40])
        print("  lineTexts  :", it["lineTexts"])

    # 点开第一个数字会话，看右侧标题
    if numeric:
        target = numeric[0]["title"]
        try:
            pg.get_by_text(target, exact=True).first.click(timeout=6000)
            time.sleep(5)
            header = pg.evaluate("""() => {
              const out = [];
              document.querySelectorAll('*').forEach(el => {
                const r = el.getBoundingClientRect();
                const t = (el.textContent || '').trim();
                if (r.y < 110 && r.x > 300 && t && t.length < 24 && el.childElementCount === 0) {
                  out.push(t);
                }
              });
              return [...new Set(out)].slice(0, 10);
            }""")
            print("\n=== 点开数字会话后，右侧标题区文本 ===")
            print(" ", header)
            pg.screenshot(path="/tmp/numeric_conv.png")
            print(" 截图已存")
        except Exception as e:
            print("点击失败:", str(e)[:100])
    b.close()
