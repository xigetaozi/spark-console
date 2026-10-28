"""
spark-agent —— 本机扫码助手

作用：
  常驻在本机，长轮询服务器；当你在网页上点「用本机扫码」时，
  它会立刻弹出一个真实的浏览器窗口，你扫码后自动把登录态回传给服务器。

为什么必须在本机跑：
  浏览器安全模型禁止网页静默启动本地程序，所以"网页点一下就能弹本机窗口"
  需要本机有一个常驻的小程序作为桥梁。这是唯一可行方式。

用法：
  双击「启动扫码助手.bat」即可（会读取同目录的 server.txt / local-key.txt）。
  启动后保持运行（可最小化），以后在网页点登录，本机就会自动弹窗。

手动/排障：
  python spark-agent.py <服务器地址> <控制台口令>
  python spark-agent.py --check <服务器地址> <控制台口令>   # 只自检，不常驻
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

AGENT_VERSION = "1.1"

# 日志同时写文件，窗口被关掉或用 bat 启动时也能事后回看
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.log")


def _clean(v: str) -> str:
    """去掉 BOM、首尾空白和包着的引号。

    Windows 记事本存 txt 常带 UTF-8 BOM、或者用户多敲了一个空格；
    这类"看不见的字符"会让口令校验必失败，而报错看起来像"没启动"。
    """
    return (v or "").lstrip("\ufeff").strip().strip('"').strip("'").strip()


def log(msg: str) -> None:
    line = time.strftime("[%H:%M:%S] ") + msg
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _parse_args(argv: list[str]):
    """解析 --check 与位置参数，顺序随意。"""
    check = "--check" in argv
    rest = [a for a in argv if not a.startswith("--")]
    server = _clean(rest[0]) if rest else _clean(os.environ.get("SPARK_SERVER", ""))
    key = _clean(rest[1]) if len(rest) > 1 else _clean(os.environ.get("SPARK_KEY", ""))
    return check, server.rstrip("/"), key


class AgentAuthError(RuntimeError):
    """口令不对：和"连不上"要分开报，否则用户永远猜不到问题在哪。"""


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Agent:
    def __init__(self, server: str, key: str) -> None:
        self.server = server
        self.key = key
        self._hb_stop = threading.Event()

    # ---------------- HTTP ----------------
    def _req(self, path: str, method: str = "GET", payload: dict | None = None,
             timeout: int = 20) -> tuple[int, bytes]:
        data = None
        headers = {"X-Console-Key": self.key}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.server + path, data=data, method=method,
                                     headers=headers)
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read()

    def ping(self, timeout: int = 10) -> None:
        try:
            self._req("/api/agent/ping", timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise AgentAuthError("控制台口令不正确") from e
            raise

    def wait_task(self, timeout: int = 40) -> dict | None:
        """长轮询：服务器挂着连接，有任务立刻返回。"""
        try:
            st, body = self._req("/api/agent/pending", timeout=timeout)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise AgentAuthError("控制台口令不正确") from e
            raise
        if st == 204 or not body:
            return None
        return json.loads(body)

    # ---------------- 心跳 ----------------
    def start_heartbeat(self, every: int = 10) -> None:
        """扫码弹窗期间也要报活。

        不这么做的话，长轮询停了 20 秒以上，网页就会判"助手未运行"，
        用户正在扫码却看到"请先启动助手"，非常容易误判。
        """
        self._hb_stop.clear()

        def loop() -> None:
            while not self._hb_stop.is_set():
                try:
                    self.ping(timeout=6)
                except Exception:
                    pass
                self._hb_stop.wait(every)

        threading.Thread(target=loop, daemon=True).start()

    def stop_heartbeat(self) -> None:
        self._hb_stop.set()

    # ---------------- 扫码 ----------------
    def do_login(self, acc: str) -> bytes | None:
        """弹出可见浏览器，等用户扫码，成功后返回 storage_state 的字节。"""
        from playwright.sync_api import sync_playwright

        headless = os.environ.get("SPARK_AGENT_HEADLESS", "") == "1"  # 排障用
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, args=["--start-maximized"])
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
                    log("请在这个窗口里点右上角「登录」后扫码")
            except Exception:
                log("请在这个窗口里点右上角「登录」后扫码")

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


def selfcheck(agent: Agent) -> bool:
    """握手自检：把「连不上」和「口令错」当场分开，别让用户去猜。"""
    try:
        agent.ping()
    except AgentAuthError:
        log("❌ 口令不对：服务器拒绝了请求（HTTP 401）。")
        log("   要用【管理员口令】（控制台登录用的那个），访客口令无效。")
        log("   请把正确口令写进 local-key.txt（只写一行，不要带空格），再重新双击启动。")
        return False
    except urllib.error.HTTPError as e:
        log("❌ 服务器返回 HTTP %s，可能是控制台服务异常。" % e.code)
        return False
    except Exception as e:
        log("❌ 连不上服务器 %s（%s）" % (agent.server, str(e)[:80]))
        log("   检查：1) 服务器地址是否写成 http://IP 形式；2) 本机能否打开该网址；3) 服务器是否在运行。")
        return False
    log("✅ 已连上服务器且口令正确：" + agent.server)
    return True


def main() -> int:
    check_only, server, key = _parse_args(sys.argv[1:])

    if not server:
        print("缺少服务器地址：双击「启动扫码助手.bat」，或 python spark-agent.py <服务器地址> <控制台口令>")
        return 2
    if not key:
        print("缺少控制台口令（管理员口令）：python spark-agent.py <服务器地址> <控制台口令>")
        return 2

    agent = Agent(server, key)

    if check_only:
        return 0 if selfcheck(agent) else 1

    log("扫码助手 v%s 启动，服务器: %s" % (AGENT_VERSION, server))
    if not selfcheck(agent):
        log("自检没通过，先修上面的问题；本窗口会继续重试（每 30 秒）。")
        while True:
            time.sleep(30)
            if selfcheck(agent):
                break
    log("保持本窗口运行（可最小化）。回到网页点「用本机扫码」即可。")

    backoff = 0
    while True:
        try:
            task = agent.wait_task()
            backoff = 0
        except AgentAuthError:
            log("口令被服务器拒绝（401），30 秒后重试。仍失败请检查 local-key.txt。")
            time.sleep(30)
            continue
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
        agent.start_heartbeat()
        try:
            state = agent.do_login(acc)
        except Exception as e:
            state = None
            log("扫码流程异常: %s" % str(e)[:120])
        finally:
            agent.stop_heartbeat()

        if state:
            payload = {"req_id": rid, "acc": acc, "ok": True,
                       "state_b64": base64.b64encode(state).decode()}
            try:
                st, _ = agent._req("/api/agent/result", "POST", payload, timeout=90)
                log("登录态已回传服务器（HTTP %s）" % st)
            except Exception as e:
                log("回传失败: %s" % str(e)[:120])
        else:
            try:
                agent._req("/api/agent/result", "POST",
                           {"req_id": rid, "acc": acc, "ok": False,
                            "error": "未完成扫码或超时"}, timeout=30)
            except Exception:
                pass
            log("本次未取得登录态（超时或取消）")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已退出")
