"""诊断 2：真实点击登录按钮，观察弹窗与二维码元素，并保存截图。"""
import json
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

    btn = pg.locator('button:has-text("登录")').first
    print("按钮数量:", pg.locator('button:has-text("登录")').count(),
          "| 可见:", btn.is_visible(), "| 文本:", btn.inner_text())
    btn.click(timeout=6000)
    time.sleep(6)

    probe = pg.evaluate("""() => {
      const res = {dialogs: [], canvases: [], dataimgs: [], qrlike: []};
      document.querySelectorAll('[role="dialog"], [class*="login"], [class*="Login"]').forEach(e=>{
        const r = e.getBoundingClientRect();
        if (r.width > 40 && r.height > 40) res.dialogs.push({cls:(e.className||'').toString().slice(0,70), w:Math.round(r.width), h:Math.round(r.height)});
      });
      document.querySelectorAll('canvas').forEach(e=>{
        const r = e.getBoundingClientRect();
        if (r.width > 40) res.canvases.push({w:Math.round(r.width), h:Math.round(r.height)});
      });
      document.querySelectorAll('img').forEach(e=>{
        const s = e.getAttribute('src')||'';
        if (s.startsWith('data:image') && e.getBoundingClientRect().width > 40) {
          res.dataimgs.push({w:Math.round(e.getBoundingClientRect().width), len:s.length});
        }
      });
      document.querySelectorAll('[class*="qr"], [class*="Qr"]').forEach(e=>{
        const r = e.getBoundingClientRect();
        if (r.width > 40) res.qrlike.push({cls:(e.className||'').toString().slice(0,60), w:Math.round(r.width), h:Math.round(r.height)});
      });
      return res;
    }""")
    print(json.dumps(probe, ensure_ascii=False, indent=1)[:1500])
    pg.screenshot(path="/tmp/after_click.png")
    print("截图已保存")
    b.close()
