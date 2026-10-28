"""
spark-console —— 多账号统一控制台

职责：
  1. 扫描 /opt/douyin-spark-data/*/env 列出所有账号
  2. 聚合各实例的运行状态（/api/status）
  3. 反向代理各实例面板到 /p/<账号>/  —— 一个页面内切换，无需开多个端口
  4. 扫码登录：服务端无头浏览器打开抖音登录页，截二维码给前端，
     扫码成功后把登录态写入该账号的 data/state.json

监听 80 端口。仅本机可访问各实例的内部端口。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import cv2
    import numpy as np
    _CV_OK = True
except Exception:
    _CV_OK = False
from typing import Any

from fastapi import FastAPI, File, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

BASE = Path(__file__).resolve().parent
# 账号数据根目录：默认沿用部署习惯，可用环境变量覆盖，
# 方便别人 clone 后指向自己的路径（如 ./data 或 /srv/spark-data）
ACCOUNTS_ROOT = Path(os.environ.get("ACCOUNTS_ROOT", "/opt/douyin-spark-data"))
STATIC = BASE / "index.html"
# 个人主页（可选）：页面里通常带个人信息，不随仓库分发，只在服务器上放一份。
# 存在时 `/` 展示主页、控制台留在 `/console`；不存在时 `/` 直接就是控制台。
HOME = BASE / "home.html"

# 控制台页面禁止缓存：改版后浏览器必须拿到新 JS，否则界面提示和后台逻辑会各说各话
NO_STORE = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}

app = FastAPI(title="Spark Console")


@app.middleware("http")
async def visitor_write_block(request: Request, call_next):
    """访客会话禁止控制台自身的写操作（扫码、代扫码请求、登录流程等）。

    GET 全放行；/api/auth 是登录本身、必须放行（此时还没有会话角色）。
    面板侧的写保护由面板自己的中间件 + 按角色注入的令牌双重兜底。
    """
    if _role(request) == "visitor" and request.method.upper() != "GET" \
            and request.url.path != "/api/auth":
        return JSONResponse(status_code=403,
                            content={"detail": "访客只有只读权限，无法执行该操作"})
    return await call_next(request)

# 控制台口令：不设置则拒绝启动（避免公网裸奔）
CONSOLE_PASSWORD = os.environ.get("CONSOLE_PASSWORD", "").strip()
# 访客口令（可选）：持有者登录控制台后为只读模式（看得到账号列表与面板，改不了任何东西）
VISITOR_PASSWORD = os.environ.get("VISITOR_PASSWORD", "").strip()
# sid -> 角色（"admin" / "visitor"）；访客会话的代理请求只注入访客令牌
SESSIONS: dict[str, str] = {}

# 可选通知 webhook（企业微信/钉钉/飞书/通用），不配置则只在控制台内提示
NOTIFY_WEBHOOK = os.environ.get("NOTIFY_WEBHOOK", "").strip()
AUDIT_LOG = BASE / "audit.jsonl"
HEALTH: dict[str, dict] = {}          # 账号 -> 最近一次健康判定
_HEALTH_LOCK = threading.Lock()


def audit(event: str, acc: str = "-", detail: str = "") -> None:
    """把敏感操作写入审计日志（JSON Lines），用于事后追溯。"""
    try:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event,
               "account": acc, "detail": detail[:300],
               "ip": detail if event == "auth_fail" else ""}
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def notify(title: str, body: str) -> None:
    """失效预警推送；未配置 webhook 时静默跳过。"""
    if not NOTIFY_WEBHOOK:
        return
    try:
        payload = {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(NOTIFY_WEBHOOK, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        _OPENER.open(req, timeout=8)
    except Exception:
        pass

# --------------------------------------------------------------------------- 工具


def read_env(acc: str) -> dict[str, str]:
    p = ACCOUNTS_ROOT / acc / "env"
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def list_accounts() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not ACCOUNTS_ROOT.exists():
        return items
    for d in sorted(ACCOUNTS_ROOT.iterdir()):
        if not d.is_dir() or not (d / "env").exists():
            continue
        env = read_env(d.name)
        try:
            port = int(env.get("PORT", "0"))
        except ValueError:
            continue
        items.append({"id": d.name, "port": port, "has_token": bool(env.get("AUTH_TOKEN"))})
    return items


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 绕过环境代理，直连本机


def http_call(method: str, url: str, headers: dict | None = None,
              body: bytes | None = None, timeout: int = 30):
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        if k.lower() in ("host", "content-length", "connection"):
            continue
        req.add_header(k, v)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()
    except Exception as e:  # 连接失败
        return 0, {}, str(e).encode()


def instance_status(acc: str) -> dict[str, Any]:
    env = read_env(acc)
    port = env.get("PORT")
    token = env.get("AUTH_TOKEN", "")
    if not port:
        return {"id": acc, "online": False, "error": "缺少 PORT"}
    st, _, body = http_call("GET", f"http://127.0.0.1:{port}/api/status",
                            headers={"X-Auth-Token": token}, timeout=6)
    if st != 200:
        return {"id": acc, "port": int(port), "online": False, "error": "面板无响应"}
    try:
        d = json.loads(body)
    except Exception:
        return {"id": acc, "port": int(port), "online": False, "error": "返回异常"}
    last = d.get("last_run") or {}
    return {
        "id": acc,
        "port": int(port),
        "online": True,
        "state_file": d.get("state_file_exists"),
        "session": d.get("session_status"),
        "running": d.get("running"),
        "next_run": d.get("next_run"),
        "history": d.get("history_count"),
        "last_ok": len(last.get("ok") or []),
        "last_failed": len(last.get("failed") or []),
        "last_at": last.get("at"),
        "logged_out": bool(last.get("logged_out")),
        "rate_limited": bool(last.get("rate_limited")),
    }


def account_health(s: dict) -> str:
    """登录态健康判定：ok / missing / expired / limited / offline。"""
    if not s.get("online"):
        return "offline"
    if not s.get("state_file"):
        return "missing"
    if s.get("logged_out") or s.get("session") == "expired":
        return "expired"
    if s.get("rate_limited"):
        return "limited"
    return "ok"


def health_sweep() -> None:
    """定期体检：发现登录态失效就记录并推送一次预警，避免"静默失败"。"""
    for a in list_accounts():
        s = instance_status(a["id"])
        h = account_health(s)
        with _HEALTH_LOCK:
            prev = (HEALTH.get(a["id"]) or {}).get("health")
            HEALTH[a["id"]] = {"health": h, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "next_run": s.get("next_run")}
        if h in ("expired", "missing") and prev != h:
            audit("health_" + h, a["id"])
            notify(f"续火花告警：账号 {a['id'].upper()} 登录态{'已失效' if h == 'expired' else '缺失'}",
                   f"下次定时：{s.get('next_run')}\n请在本机扫码后到控制台上传 state.json。")


def _health_loop() -> None:
    time.sleep(90)
    while True:
        try:
            health_sweep()
        except Exception:
            pass
        time.sleep(1800)  # 每 30 分钟


threading.Thread(target=_health_loop, daemon=True).start()


# --------------------------------------------------------------------------- 路由

@app.get("/")
def index() -> FileResponse:
    """站点首页：有 home.html 就展示个人主页，否则直接给控制台。

    这样仓库里（没有 home.html）clone 下来就是「/ 即控制台」，
    而服务器上放了主页时自动变成「/ 主页 + /console 控制台」。
    """
    if HOME.exists():
        return FileResponse(HOME, headers=NO_STORE)
    return FileResponse(STATIC, headers=NO_STORE)


@app.get("/console")
def console() -> FileResponse:
    """账号控制台。放在 /console，与主页的入口保持一致。"""
    # 页面是本项目的唯一入口，改版后必须让浏览器拿到新版：
    # 之前发过一次新版，浏览器却仍跑旧 JS，界面提示和后台逻辑对不上，排查方向全跑偏。
    return FileResponse(STATIC, headers=NO_STORE)


def _authed(request: Request) -> bool:
    sid = request.cookies.get("sc") or ""
    return bool(sid and sid in SESSIONS)


def _role(request: Request) -> str:
    """当前会话角色：admin / visitor / 空（未登录）。"""
    sid = request.cookies.get("sc") or ""
    return SESSIONS.get(sid, "")


@app.post("/api/auth")
def api_auth(request: Request, payload: dict) -> Response:
    if not CONSOLE_PASSWORD and not VISITOR_PASSWORD:
        return JSONResponse({"ok": False, "error": "服务端未设置控制台口令，已拒绝访问"}, 503)
    pwd = str(payload.get("password", ""))
    if pwd == CONSOLE_PASSWORD and CONSOLE_PASSWORD:
        role = "admin"
    elif pwd == VISITOR_PASSWORD and VISITOR_PASSWORD:
        role = "visitor"
    else:
        audit("auth_fail", "-", request.client.host if request.client else "")
        time.sleep(0.6)  # 轻微延时，抑制暴力尝试
        return JSONResponse({"ok": False, "error": "口令不正确"}, 401)
    sid = secrets.token_urlsafe(24)
    SESSIONS[sid] = role
    audit("auth_ok", role, request.client.host if request.client else "")
    resp = JSONResponse({"ok": True, "role": role})
    resp.set_cookie("sc", sid, httponly=True, samesite="lax", max_age=7 * 24 * 3600)
    return resp


@app.get("/api/session")
def api_session(request: Request) -> dict:
    return {"authed": _authed(request), "role": _role(request), "password_required": bool(CONSOLE_PASSWORD or VISITOR_PASSWORD)}


@app.get("/api/accounts")
def api_accounts(request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "未授权"}, 401)
    accs = list_accounts()
    out = []
    for a in accs:
        st = instance_status(a["id"])
        st["has_token"] = a["has_token"]
        st["health"] = account_health(st)
        out.append(st)
    return {"accounts": out}


# --------------------------------------------------------------------------- 扫码工具
SCAN_PAGE = BASE / "scan.html"


@app.get("/scan")
def scan_page() -> FileResponse:
    return FileResponse(SCAN_PAGE)


def _decode_image(raw: bytes) -> dict:
    """识别二维码/条形码：QR 优先，其次条码；失败时缩放再试一次。"""
    if not _CV_OK:
        return {"ok": False, "error": "服务端缺少识别引擎（opencv），请联系维护者"}
    buf = np.frombuffer(raw, np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        return {"ok": False, "error": "图片无法解析，请换一张（JPG/PNG）"}

    h, w = img.shape[:2]
    scale = 1.0
    if max(h, w) > 1600:          # 太大先缩小，提升速度
        scale = 1600.0 / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)

    qr = cv2.QRCodeDetector()
    for attempt in range(2):
        try:
            data, _pts, _ = qr.detectAndDecode(img)
            if data:
                return {"ok": True, "type": "qrcode", "text": data, "format": "QR_CODE"}
        except Exception:
            pass
        try:
            ok, texts, _pts, _ = qr.detectAndDecodeMulti(img)
            if ok and texts:
                first = [t for t in texts if t]
                if first:
                    return {"ok": True, "type": "qrcode", "text": first[0], "format": "QR_CODE"}
        except Exception:
            pass
        # zbar（对 EAN/UPC/Code128 等一维码更可靠，也支持二维码）
        try:
            from pyzbar.pyzbar import decode as _zb_decode
            for r in _zb_decode(img):
                txt = r.data.decode("utf-8", "ignore").strip()
                if txt:
                    return {"ok": True,
                            "type": "qrcode" if r.type == "QRCODE" else "barcode",
                            "text": txt, "format": r.type}
        except Exception:
            pass

        # OpenCV 条码检测（兜底）
        try:
            bd = cv2.barcode.BarcodeDetector()
            okb, infos, types, _ = bd.detectAndDecodeWithType(img)
            if okb and infos:
                for i, info in enumerate(infos):
                    if info:
                        return {"ok": True, "type": "barcode", "text": info,
                                "format": types[i] if i < len(types) else "BARCODE"}
        except Exception:
            pass
        if attempt == 0:
            img = cv2.resize(img, None, fx=1.6, fy=1.6, interpolation=cv2.INTER_CUBIC)

    return {"ok": False, "error": "未识别到二维码或条码，试试更清晰的图片或让码占满画面"}


@app.post("/api/scan")
async def api_scan(file: UploadFile = File(...)) -> Response:
    raw = await file.read()
    if not raw:
        return JSONResponse({"ok": False, "error": "空文件"}, 400)
    if len(raw) > 8 * 1024 * 1024:
        return JSONResponse({"ok": False, "error": "图片超过 8MB"}, 413)
    result = await asyncio.to_thread(_decode_image, raw)
    return JSONResponse(result)


# --------------------------------------------------------------------------- 本机扫码助手通道
# 网页点"登录" -> 入队任务 -> 本机 spark-agent 长轮询取任务 -> 在本机弹出真实扫码窗口
# -> 扫码成功 -> 把 storage_state 回传 -> 服务端落盘为该账号的 state.json

AGENT_TASKS: dict[str, dict] = {}
AGENT_LOCK = threading.Lock()
AGENT_SEEN: list[float] = []
# 最近一次「带着错误口令来敲门」的时间：用来区分「助手没启动」和「助手在跑但口令不对」。
# 没有这个，前端只能一律提示"请先启动助手"，用户看到的和真相经常不是一回事。
AGENT_BADKEY: list[float] = []
AGENT_HEARTBEAT = 50      # 心跳有效期（秒）：比长轮询的 20s 宽裕一倍
AGENT_TASK_TTL = 120      # 等接单最长（秒）：没人取就作废，前端不无限转圈
AGENT_RUN_TTL = 600       # 接单后最长（秒）：扫码本身允许 5 分钟

# 助手当前做到哪一步（弹窗、等扫码、手动点登录…），给前端做进度提示，
# 也让我在服务器端就能看到用户电脑上发生了什么，不必靠猜。
AGENT_STEPS = {
    "accepted": "本机助手已接单，正在启动浏览器",
    "launched": "已弹出浏览器窗口，正在打开抖音",
    "qr_ready": "二维码已出现，请用手机扫描",
    "captcha": "抖音要求滑块验证，请在弹出窗口里拖动滑块",
    "manual": "窗口已打开但没自动出码，请在窗口里点右上角「登录」",
    "scanning": "已检测到登录态，正在保存",
}


def _mark_seen() -> None:
    AGENT_SEEN.append(time.time())
    if len(AGENT_SEEN) > 200:
        del AGENT_SEEN[:-200]


def _agent_authed(request: Request) -> bool:
    ok = bool(CONSOLE_PASSWORD) and request.headers.get("X-Console-Key", "") == CONSOLE_PASSWORD
    if not ok:
        AGENT_BADKEY.append(time.time())
        if len(AGENT_BADKEY) > 200:
            del AGENT_BADKEY[:-200]
        audit("agent_badkey", "-", "口令与 CONSOLE_PASSWORD 不一致")
    return ok


def _agent_online() -> bool:
    return bool(AGENT_SEEN) and (time.time() - AGENT_SEEN[-1] < AGENT_HEARTBEAT)


def _agent_state() -> str:
    """助手状态三态：ok=在线；bad_key=连上了但口令错；offline=根本没启动。"""
    if _agent_online():
        return "ok"
    if AGENT_BADKEY and (time.time() - AGENT_BADKEY[-1] < 300):
        return "bad_key"
    return "offline"


def _task_expired(t: dict) -> bool:
    """任务是否该判死。

    等待接单：2 分钟没人取 → 作废。
    已接单：扫码本身要几分钟，只要助手还活着就不算超时；助手掉线才提前判死，
    总上限 10 分钟兜底。
    """
    age = time.time() - t.get("ts", 0)
    status = t.get("status")
    if status == "pending":
        return age > AGENT_TASK_TTL
    if status == "running":
        return age > AGENT_RUN_TTL or (age > AGENT_TASK_TTL and not _agent_online())
    return True


@app.post("/api/agent/progress")
def agent_progress(request: Request, payload: dict) -> Response:
    """助手汇报进度：接了单 / 弹了窗 / 出了码 / 要手动点登录。"""
    if not _agent_authed(request):
        return JSONResponse({"ok": False, "error": "未授权"}, 401)
    _mark_seen()
    acc = str(payload.get("acc", ""))
    rid = str(payload.get("req_id", ""))
    step = str(payload.get("step", ""))[:32]
    with AGENT_LOCK:
        t = AGENT_TASKS.get(acc)
        if not t or t["req_id"] != rid:
            return JSONResponse({"ok": False, "error": "任务不匹配"}, 409)
        t["step"] = step
        t["progress"] = AGENT_STEPS.get(step, step)
    return JSONResponse({"ok": True})


@app.get("/api/agent/ping")
def agent_ping(request: Request) -> Response:
    """助手自检/心跳：校验口令并刷新在线状态，不派任务。"""
    if not _agent_authed(request):
        return JSONResponse({"ok": False, "error": "口令不正确"}, 401)
    _mark_seen()
    return JSONResponse({"ok": True, "server": "spark-console", "ts": int(time.time())})


@app.post("/api/agent/request/{acc}")
def agent_request(acc: str, request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "未授权"}, 401)
    if acc not in [a["id"] for a in list_accounts()]:
        return JSONResponse({"ok": False, "error": "未知账号"}, 404)
    with AGENT_LOCK:
        old = AGENT_TASKS.get(acc)
        # 幂等：重复点按钮时复用未完成的那一单。
        # 否则每次点击都会换 req_id，助手手里的旧 req_id 回传时被判定"任务不匹配"，
        # 表现就是"窗口弹了、扫了、却一直转圈"。
        if old and old.get("status") in ("pending", "running") and not _task_expired(old):
            return JSONResponse({"ok": True, "req_id": old["req_id"], "reused": True,
                                 "status": old.get("status"), "agent_online": _agent_online(),
                                 "agent_state": _agent_state(),
                                 "progress": old.get("progress", "")})
        rid = secrets.token_hex(8)
        AGENT_TASKS[acc] = {"req_id": rid, "ts": time.time(), "status": "pending", "error": ""}
    audit("agent_request", acc, rid)
    return JSONResponse({"ok": True, "req_id": rid, "reused": False, "status": "pending",
                         "agent_online": _agent_online(), "agent_state": _agent_state()})


@app.get("/api/agent/pending")
def agent_pending(request: Request) -> Response:
    if not _agent_authed(request):
        return JSONResponse({"error": "未授权"}, 401)
    _mark_seen()
    for _ in range(20):  # 长轮询 20 秒
        with AGENT_LOCK:
            for aid, t in AGENT_TASKS.items():
                if t["status"] == "pending" and not _task_expired(t):
                    t["status"] = "running"
                    return JSONResponse({"acc": aid, "req_id": t["req_id"]})
        time.sleep(1)
    return Response(status_code=204)


@app.post("/api/agent/result")
def agent_result(request: Request, payload: dict) -> Response:
    if not _agent_authed(request):
        return JSONResponse({"ok": False, "error": "未授权"}, 401)
    _mark_seen()
    rid = str(payload.get("req_id", ""))
    acc = str(payload.get("acc", ""))
    with AGENT_LOCK:
        t = AGENT_TASKS.get(acc)
        if not t or t["req_id"] != rid:
            audit("agent_stale", acc, rid)
            return JSONResponse({"ok": False, "error": "任务不匹配（可能已超时或被新请求取代）"}, 409)
        if payload.get("ok") and payload.get("state_b64"):
            try:
                raw = base64.b64decode(payload["state_b64"])
                json.loads(raw)  # 校验是合法 JSON
                path = ACCOUNTS_ROOT / acc / "state.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                path.chmod(0o600)
                t["status"] = "success"
                audit("agent_success", acc)
            except Exception as e:
                t["status"] = "failed"
                t["error"] = "落盘失败: %s" % str(e)[:120]
        else:
            t["status"] = "failed"
            t["error"] = str(payload.get("error", ""))[:200]
        return JSONResponse({"ok": True})


@app.get("/api/agent/status/{acc}")
def agent_status(acc: str, request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "未授权"}, 401)
    t = AGENT_TASKS.get(acc) or {}
    status = t.get("status", "idle")
    err = t.get("error", "")
    # 卡在 pending/running 且已判死（助手中途被关掉/断网）时别让前端无限转圈
    if status in ("pending", "running") and _task_expired(t):
        status = "expired"
        err = err or ("助手在 2 分钟内没有接下这一单，已作废"
                      if t.get("status") == "pending" else "助手已掉线，这一单作废")
    return JSONResponse({"status": status, "error": err,
                         "step": t.get("step", ""), "progress": t.get("progress", ""),
                         "agent_online": _agent_online(), "agent_state": _agent_state()})


@app.get("/api/audit")
def api_audit(request: Request, n: int = 80) -> Response:
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "未授权"}, 401)
    lines: list[str] = []
    if AUDIT_LOG.exists():
        lines = AUDIT_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-max(1, min(n, 500)):]
    return JSONResponse({"ok": True, "lines": lines})


@app.get("/api/account/{acc}/config")
def api_account_config(acc: str, request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "未授权"}, 401)
    env = read_env(acc)
    st, _, body = http_call("GET", f"http://127.0.0.1:{env.get('PORT')}/api/config",
                            headers={"X-Auth-Token": env.get("AUTH_TOKEN", "")}, timeout=8)
    if st != 200:
        return {"ok": False, "error": "读取失败"}
    d = json.loads(body)
    return {"ok": True, "friends": len(d.get("friends") or []),
            "schedule_time": d.get("schedule_time"),
            "gap": f"{d.get('send_gap_min')}~{d.get('send_gap_max')}"}


# 反向代理：把 /p/<acc>/... 转发到该账号实例
_HOP = {"transfer-encoding", "connection", "keep-alive", "content-encoding", "content-length"}


@app.api_route("/p/{acc}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy(acc: str, path: str, request: Request) -> Response:
    if not _authed(request):
        return Response("控制台未授权：请先返回首页输入口令", status_code=401,
                        media_type="text/plain; charset=utf-8")
    env = read_env(acc)
    port = env.get("PORT")
    if not port:
        return JSONResponse({"detail": "未知账号"}, status_code=404)
    url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        url += "?" + request.url.query

    body = None
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        body = await request.body()

    if path in ("api/run", "api/upload-state", "api/config") and request.method in ("POST", "PUT"):
        audit("proxy_" + path.replace("api/", ""), acc, f"{request.method} {path}")

    hdrs = {k: v for k, v in request.headers.items()}
    # 关键：接口请求由代理侧注入正确的访问令牌，浏览器里不保存任何令牌
    if path.startswith("api/"):
        # 关键安全点：访客会话绝不注入管理员令牌。
        # 访客 → 注入该账号的 VISITOR_TOKEN（面板会拒绝其写操作）；
        #       若该账号未配置 VISITOR_TOKEN，则不注入任何令牌（面板直接 401），
        #       宁可访客看不了，也不能让他借管理员权限写数据。
        hdrs = {k: v for k, v in hdrs.items() if k.lower() != "x-auth-token"}
        if _role(request) == "visitor":
            token = env.get("VISITOR_TOKEN", "")
            if token:
                hdrs["X-Auth-Token"] = token
        else:
            hdrs["X-Auth-Token"] = env.get("AUTH_TOKEN", "")
    st, rh, rb = await asyncio.to_thread(http_call, request.method, url, hdrs, body, 120)
    if st == 0:
        return Response(f"账号 {acc} 的服务无响应（端口 {port} 未监听）", status_code=502,
                        media_type="text/plain; charset=utf-8")

    ctype = rh.get("Content-Type", rh.get("content-type", "application/octet-stream"))
    # HTML 里的绝对路径需要加前缀，否则 iframe 内会请求到控制台自身
    if "text/html" in ctype:
        text = rb.decode("utf-8", "ignore")
        text = text.replace('"/static/', f'"/p/{acc}/static/')
        text = text.replace("'/static/", f"'/p/{acc}/static/")
        text = text.replace('"/api/', f'"/p/{acc}/api/')
        text = text.replace("'/api'", f"'/p/{acc}/api'")
        text = text.replace('"/api"', f'"/p/{acc}/api"')
        rb = text.encode("utf-8")

    out_headers = {k: v for k, v in rh.items() if k.lower() not in _HOP}
    return Response(rb, status_code=st, headers=out_headers, media_type=ctype)


def _read_body_sync(request: Request) -> bytes | None:
    """已废弃：改为在 async 端点里 await request.body()。保留以免外部引用报错。"""
    return None


# --------------------------------------------------------------------------- 扫码登录

LOGIN: dict[str, dict] = {}
LOGIN_LOCK = threading.Lock()


def _qr_candidates() -> list[str]:
    return [
        'img[src^="data:image"]',
        '[class*="qrcode"] img', '[class*="qrcode"] canvas',
        '[class*="qr-code"] img', '[class*="qr-code"] canvas',
        'canvas',
    ]


def _is_blocked(page) -> bool:
    """抖音的滑块验证常常渲染在跨域 iframe 里，必须遍历所有 frame 才检测得到。"""
    keys = ("请完成下列验证", "拖动滑块", "安全验证", "验证码", "验证后继续")
    for fr in page.frames:
        try:
            txt = fr.inner_text("body")[:1500]
        except Exception:
            continue
        if any(k in txt for k in keys):
            return True
    return False


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

# 降低"无头浏览器"特征，减少被直接判定为自动化的概率
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

MAX_LOGIN_TRY = 3


def _prepare_login_page(browser):
    """新建上下文 → 打开抖音 → 点「登录」，返回 (ctx, page)。"""
    ctx = browser.new_context(
        viewport={"width": 1366, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        user_agent=UA,
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
    )
    page = ctx.new_page()
    page.add_init_script(_STEALTH_JS)
    page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
    time.sleep(random.uniform(2.0, 3.5))
    for sel in ('button:has-text("登录")', 'div:has-text("登录") >> nth=0', 'text=登录'):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=4000)
                break
        except Exception:
            continue
    time.sleep(random.uniform(2.5, 3.5))
    return ctx, page


def _grab_qr(page):
    """优先精确定位二维码元素，失败则截登录弹窗，最后截视口中心区域。"""
    for sel in _qr_candidates():
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                b = loc.bounding_box()
                if b and b["width"] > 80 and b["height"] > 80:
                    return loc.screenshot()
        except Exception:
            continue
    for sel in ('[class*="login"]', '[class*="Login"]', 'div[role="dialog"]'):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                return loc.screenshot()
        except Exception:
            continue
    return page.screenshot(clip={"x": 480, "y": 200, "width": 400, "height": 460})


def _login_worker(acc: str, state_path: Path) -> None:
    """在独立线程里跑 Playwright：自动重试取码，保持会话直到扫码成功或超时。"""
    from playwright.sync_api import sync_playwright

    box = LOGIN[acc]
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--disable-gpu", "--disable-blink-features=AutomationControlled",
                "--lang=zh-CN",
            ])
            ctx = page = None
            for attempt in range(1, MAX_LOGIN_TRY + 1):
                if box.get("cancel"):
                    box["status"] = "cancelled"
                    try:
                        browser.close()
                    except Exception:
                        pass
                    return
                box["attempt"] = attempt
                box["status"] = "starting"
                box["stage"] = "第 %d/%d 次尝试打开登录页" % (attempt, MAX_LOGIN_TRY)
                try:
                    ctx, page = _prepare_login_page(browser)
                except Exception:
                    ctx = page = None
                if page is not None and not _is_blocked(page):
                    break
                # 被风控拦截 → 关掉上下文重来
                try:
                    if ctx:
                        ctx.close()
                except Exception:
                    pass
                ctx = page = None
                time.sleep(random.uniform(2.0, 4.0))

            if page is None:
                box["status"] = "blocked"
                box["error"] = ("连续 %d 次被抖音安全验证拦截（该服务器出口 IP 被风控）。"
                                "可点「重试」；若长期如此，请在本机扫码后上传 state.json"
                                % MAX_LOGIN_TRY)
                try:
                    browser.close()
                except Exception:
                    pass
                return

            box["page"], box["context"], box["browser"] = page, ctx, browser
            box["qr"] = base64.b64encode(_grab_qr(page)).decode()
            box["status"] = "waiting"

            deadline = time.time() + 300
            while time.time() < deadline:
                if box.get("cancel"):
                    box["status"] = "cancelled"
                    break
                if _is_blocked(page):
                    box["status"] = "blocked"
                    box["error"] = "二维码弹出后又被要求安全验证，请点「重试」"
                    break
                names = {c.get("name") for c in ctx.cookies()}
                if any((n or "").startswith("sessionid") for n in names):
                    time.sleep(2)
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    ctx.storage_state(path=str(state_path))
                    box["status"] = "success"
                    box["saved_to"] = str(state_path)
                    audit("login_success", acc)
                    break
                time.sleep(2)
            else:
                box["status"] = "timeout"

            try:
                browser.close()
            except Exception:
                pass
    except Exception as e:  # 浏览器启动/页面加载失败
        box["status"] = "error"
        box["error"] = "%s: %s" % (type(e).__name__, str(e)[:300])
    finally:
        box["finished"] = True


@app.post("/api/login/start/{acc}")
def login_start(acc: str, request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "未授权"}, 401)
    if acc not in [a["id"] for a in list_accounts()]:
        return {"ok": False, "error": "未知账号"}
    with LOGIN_LOCK:
        cur = LOGIN.get(acc)
        if cur and not cur.get("finished"):
            return {"ok": True, "reused": True}
        LOGIN[acc] = {"status": "starting", "qr": None, "finished": False,
                      "created": time.time(), "error": None}
    audit("login_start", acc)
    state_path = ACCOUNTS_ROOT / acc / "state.json"
    threading.Thread(target=_login_worker, args=(acc, state_path), daemon=True).start()
    return {"ok": True}


@app.get("/api/login/qr/{acc}")
def login_qr(acc: str, request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"detail": "未授权"}, status_code=401)
    box = LOGIN.get(acc) or {}
    if not box.get("qr"):
        return JSONResponse({"detail": box.get("error") or "二维码尚未生成",
                             "status": box.get("status")}, status_code=404)
    import base64 as _b
    return Response(_b.b64decode(box["qr"]), media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/login/status/{acc}")
def login_status(acc: str, request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"status": "unauthorized"}, 401)
    box = LOGIN.get(acc) or {}
    return {"status": box.get("status", "idle"), "error": box.get("error"),
            "saved_to": box.get("saved_to")}


@app.post("/api/login/cancel/{acc}")
def login_cancel(acc: str, request: Request) -> Response:
    if not _authed(request):
        return JSONResponse({"ok": False, "error": "未授权"}, 401)
    box = LOGIN.get(acc)
    if box:
        box["cancel"] = True
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    # 允许用 PORT 环境变量改监听端口：接入 nginx 后改成 127.0.0.1:8080，
    # 由 nginx 占用 80/443 对外提供服务。默认仍是 80，行为不变。
    _port = int(os.environ.get("PORT", "80"))
    _host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run(app, host=_host, port=_port, log_level="info")
