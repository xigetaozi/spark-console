"""
spark-agent —— 本机扫码助手

作用：
  常驻在本机，长轮询服务器；当你在网页上点「用本机扫码」时，
  它会立刻弹出一个真实的浏览器窗口，你扫码后自动把登录态回传给服务器。

为什么必须在本机跑：
  浏览器安全模型禁止网页静默启动本地程序，所以"网页点一下就能弹本机窗口"
  需要本机有一个常驻的小程序作为桥梁。这是唯一可行方式。

用法：
  双击「启动扫码助手.bat」即可。启动后保持运行（可最小化），
  以后在网页点登录，本机就会自动弹窗。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

SERVER = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("SPARK_SERVER", "")).rstrip("/")
if not SERVER:
    print("缺少服务器地址：python spark-agent.py <服务器地址> <控制台口令>，或设置环境变量 SPARK_SERVER")
    raise SystemExit(1)
    raise SystemExit(1)
CONSOLE_KEY = sys.argv[2] if len(sys.argv) > 2 else ""

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def post_json(path: str, payload: dict, timeout: int = 60):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        SERVER + path, data=data, method="POST",
        headers={"Content-Type": "application/json", "X-Console-Key": CONSOLE_KEY},
    )
    with _OPENER.open(req, timeout=timeout) as r:
        return r.status, r.read()


def wait_task(timeout: int = 40):
    """长轮询：服务器挂着连接，有任务立刻返回。"""
    req = urllib.request.Request(SERVER + "/api/agent/pending",
                                 headers={"X-Console-Key": CONSOLE_KEY})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            body = r.read()
            if not body:
                return None
            return json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code == 204:
            return None
        raise
    except Exception:
        return None


def do_login(acc: str):
    """弹出可见浏览器，等用户扫码，成功后返回 storage_state 的字节。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = browser.new_context(viewport=None, locale="zh-CN",
                                  timezone_id="Asia/Shanghai")
        page = ctx.new_page()
        try:
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log("打开抖音失败: %s" % e)
        time.sleep(3)

        # 自动点一次「登录」，把二维码直接亮出来
        try:
            btn = page.locator('button:has-text("登录")').first
            if btn.count() and btn.is_visible():
                btn.click(timeout=5000)
                log("已自动点击「登录」，请用手机扫码")
            else:
                log("请手动点击右上角「登录」后扫码")
        except Exception:
            log("请手动点击右上角「登录」后扫码")

        log("等待扫码（最多 5 分钟）… 账号=" + acc)
        deadline = time.time() + 300
        while time.time() < deadline:
            try:
                names = {c.get("name") for c in ctx.cookies()}
            except Exception:
                names = set()
            if any((n or "").startswith("sessionid") for n in names):
                time.sleep(2)
                state = ctx.storage_state()
                try:
                    browser.close()
                except Exception:
                    pass
                return json.dumps(state).encode("utf-8")
            time.sleep(2)

        try:
            browser.close()
        except Exception:
            pass
        return None


def main() -> None:
    if not CONSOLE_KEY:
        print("缺少控制台口令。用法: python spark-agent.py <服务器地址> <控制台口令>")
        input("按回车退出")
        return

    log("扫码助手已启动，服务器: " + SERVER)
    log("保持本窗口运行（可最小化）。回到网页点「用本机扫码」即可。")
    backoff = 0
    while True:
        try:
            task = wait_task()
            backoff = 0
        except Exception as e:
            backoff = min(backoff + 5, 30)
            log("连接服务器失败，%s 秒后重试（%s）" % (backoff, str(e)[:60]))
            time.sleep(backoff)
            continue

        if not task:
            continue

        acc = task.get("acc", "")
        rid = task.get("req_id", "")
        log("收到任务：账号 %s，正在弹出扫码窗口…" % acc.upper())
        try:
            state = do_login(acc)
        except Exception as e:
            state = None
            log("扫码流程异常: %s" % str(e)[:120])

        if state:
            payload = {"req_id": rid, "acc": acc, "ok": True,
                       "state_b64": base64.b64encode(state).decode()}
            try:
                st, body = post_json("/api/agent/result", payload, timeout=90)
                log("登录态已回传服务器（HTTP %s）" % st)
            except Exception as e:
                log("回传失败: %s" % str(e)[:120])
        else:
            try:
                post_json("/api/agent/result",
                          {"req_id": rid, "acc": acc, "ok": False, "error": "未完成扫码或超时"})
            except Exception:
                pass
            log("本次未取得登录态（超时或取消）")


if __name__ == "__main__":
    main()
