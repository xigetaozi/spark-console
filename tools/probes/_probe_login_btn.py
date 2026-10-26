"""诊断：列出抖音首页所有含"登录"的元素，用于确定准确的点击选择器。"""
import time
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=[
        "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
        "--disable-gpu", "--disable-blink-features=AutomationControlled", "--lang=zh-CN"])
    ctx = b.new_context(viewport={"width": 1366, "height": 900}, locale="zh-CN",
                        timezone_id="Asia/Shanghai", user_agent=UA)
    pg = ctx.new_page()
    pg.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    pg.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
    time.sleep(4)

    info = pg.evaluate("""() => {
      const out = [];
      const all = document.querySelectorAll('*');
      for (const el of all) {
        const t = (el.textContent || '').trim();
        if (!t || t.length > 12) continue;
        if (!t.includes('登录')) continue;
        const r = el.getBoundingClientRect();
        out.push({tag: el.tagName, cls: (el.className || '').toString().slice(0,60),
                  e2e: el.getAttribute('data-e2e') || '', text: t,
                  w: Math.round(r.width), h: Math.round(r.height),
                  x: Math.round(r.x), y: Math.round(r.y),
                  vis: r.width > 0 && r.height > 0});
      }
      return out;
    }""")
    print("=== 含“登录”的元素 ===")
    for i, it in enumerate(info[:25]):
        print(i, it)
    print("\n=== 页面标题 ===", pg.title())
    print("=== 当前 URL ===", pg.url)
    b.close()
