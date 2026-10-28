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

AGENT_VERSION = "1.2"

# 日志同时写文件，窗口被关掉或用 bat 启动时也能事后回看
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.log")
# 独立的浏览器配置目录：扫码后登录态留在里面，下次打开往往已经登录，
# 也避免动到用户自己 Chrome 的书签/插件。
PROFILE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agent-profile")

# 抹掉最明显的自动化指纹；抖音靠它把 Playwright 认出来弹滑块
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""


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
        self.rid = ""
        self._hb_stop = threading.Event()

    def report(self, acc: str, step: str) -> None:
        """把「做到哪一步」告诉服务器，网页上就能看到真实进度。"""
        try:
            self._req("/api/agent/progress", "POST",
                      {"acc": acc, "req_id": self.rid, "step": step}, timeout=8)
        except Exception:
            pass

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
            # 用本机真实 Chrome + 独立配置目录：
            # Playwright 内置 Chromium 几乎必被抖音的滑块拦住（"请完成下列验证"），
            # 用户看到的是一个拼图验证页，自然觉得"弹窗没用"。
            ctx = self._launch(p, headless, acc)
            try:
                ctx.add_init_script(STEALTH_JS)
            except Exception:
                pass
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            self.report(acc, "launched")
            log("👉 已弹出浏览器窗口。若没看到，请按 Alt+Tab 或看任务栏（它可能被挡在后面）")
            try:
                page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log("打开抖音失败: %s" % e)
            # 有头模式下把窗口顶到最前，避免"没反应"的错觉
            try:
                page.bring_to_front()
            except Exception:
                pass
            time.sleep(3)

            # 用真实 Chrome 打开时，抖音对未登录访客会直接把扫码弹窗摊在首页上，
            # 所以先看有没有码；没有才去点「登录」，最后才考虑滑块验证的情况。
            if self._qr_present(page):
                log("二维码已出现，请用手机抖音扫码")
                self.report(acc, "qr_ready")
                qr_shown = True
            elif self._captcha(page):
                log("⚠️ 抖音要求滑块验证：请在弹出窗口里拖动滑块，验证完二维码会自动出现")
                self.report(acc, "captcha")
                self._shot(page, acc)
                qr_shown = False
            elif self._open_login(page):
                log("二维码已出现，请用手机抖音扫码")
                self.report(acc, "qr_ready")
                qr_shown = True
            else:
                log("⚠️ 没自动找到「登录」按钮：请在这个窗口里点右上角「登录」，二维码就会出现")
                self.report(acc, "manual")
                self._shot(page, acc)
                qr_shown = False

            log("等待扫码（最多 5 分钟）… 账号=" + acc)
            deadline = time.time() + 300
            last_probe = time.time()
            while time.time() < deadline:
                if not self._alive(ctx):
                    log("浏览器窗口被关闭，本单结束")
                    return None
                try:
                    names = {c.get("name") for c in ctx.cookies()}
                except Exception:
                    names = set()
                if any((n or "").startswith("sessionid") for n in names):
                    log("检测到登录态，正在保存并回传…")
                    self.report(acc, "scanning")
                    time.sleep(2)
                    state = ctx.storage_state()
                    self._close(ctx)
                    return json.dumps(state).encode("utf-8")

                # 用户拖完滑块后，自动把二维码点出来，不用他再找按钮
                if not qr_shown and time.time() - last_probe > 6:
                    last_probe = time.time()
                    try:
                        if not self._captcha(page) and self._open_login(page, timeout=3):
                            log("二维码已出现，请用手机抖音扫码")
                            self.report(acc, "qr_ready")
                            qr_shown = True
                    except Exception:
                        pass
                time.sleep(2)

            self._close(ctx)
            return None

    @staticmethod
    def _close(ctx) -> None:
        try:
            b = ctx.browser
            if b:
                b.close()
            else:
                ctx.close()
        except Exception:
            pass

    @staticmethod
    def _launch(p, headless: bool, acc: str = "default"):
        """优先用本机 Chrome + 独立配置目录，失败再逐级回退。

        配置目录按账号分：同一个人扫 A、B 两个号时互不串味——
        否则第二次打开时浏览器已经带着 A 的登录态，扫 B 的码等于白扫。
        """
        args = ["--start-maximized", "--disable-blink-features=AutomationControlled",
                "--no-first-run", "--no-default-browser-check"]
        kw = dict(headless=headless, args=args, locale="zh-CN",
                  timezone_id="Asia/Shanghai", viewport=None)
        prof = os.path.join(PROFILE_DIR, acc or "default")
        try:
            ctx = p.chromium.launch_persistent_context(prof, channel="chrome", **kw)
            log("已用本机 Chrome 打开（独立配置目录）")
            return ctx
        except Exception as e:
            log("本机 Chrome 启动失败（%s），回退内置 Chromium" % str(e)[:60])
        try:
            return p.chromium.launch_persistent_context(prof + "-bw", **kw)
        except Exception as e:
            # 配置目录被上一轮残留进程占用时，退到临时上下文也能扫码，只是不留登录态
            log("配置目录不可用（%s），改用临时上下文（本次不留登录态）" % str(e)[:60])
            browser = p.chromium.launch(headless=headless, args=args)
            return browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai",
                                       viewport=None)

    @staticmethod
    def _alive(ctx) -> bool:
        try:
            ctx.pages
            return True
        except Exception:
            return False

    @staticmethod
    def _captcha(page) -> bool:
        """页面是不是「请完成下列验证」的滑块页。"""
        for t in ("请完成下列验证", "按住左边按钮拖动", "拖动完成上方拼图"):
            try:
                if page.locator("text=" + t).count():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _qr_present(page) -> bool:
        """登录弹窗是不是已经开着（看"扫码登录"字样，比看点击有没有报错可靠）。

        必须判「可见」：页面 DOM 里本来就藏着一些登录相关的隐藏节点，
        只数 count 会一直返回真，等于永远以为已经出码。
        """
        for t in ("扫码登录", "打开「抖音APP」", "如何扫码"):
            try:
                loc = page.locator("text=" + t).first
                if loc.count() and loc.is_visible():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _open_login(page, timeout: int = 30) -> bool:
        """把登录二维码点出来。

        抖音首页的「登录」按钮位置/文案会变，而且点击本身经常"报了错但已经生效"
        （弹窗盖住按钮 → 元素不可见），所以判定标准是「二维码弹窗有没有出来」，
        而不是「click 有没有抛异常」。否则会出现明明出码了却提示"没找到按钮"。
        """
        selectors = [
            '[data-e2e="login-button"]',
            'button:has-text("登录")',
            'div[role="button"]:has-text("登录")',
            'span:has-text("登录")',
            'text=登录/注册',
            'text=登录',
        ]
        deadline = time.time() + timeout
        while time.time() < deadline:
            if Agent._qr_present(page):
                return True
            for sel in selectors:
                try:
                    loc = page.locator(sel).first
                    if not (loc.count() and loc.is_visible()):
                        continue
                    try:
                        loc.click(timeout=3000)
                        log("已点击「登录」(%s)" % sel)
                    except Exception:
                        pass  # 点击报错也可能已经弹窗
                    time.sleep(2)
                    if Agent._qr_present(page):
                        return True
                except Exception:
                    continue
            time.sleep(2)
        return False

    @staticmethod
    def _shot(page, acc: str) -> None:
        """没点到登录按钮时留一张截图，便于远程判断是风控页还是改版了。"""
        path = os.path.join(os.path.dirname(LOG_PATH), "agent_last.png")
        try:
            page.screenshot(path=path)
            log("已截图保存到 agent_last.png")
        except Exception:
            pass


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
        agent.rid = rid
        log("收到任务：账号 %s，正在弹出扫码窗口…（窗口可能被挡在后面，必要时按 Alt+Tab）" % acc.upper())
        agent.start_heartbeat()
        agent.report(acc, "accepted")
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
