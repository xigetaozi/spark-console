"""Douyin Spark Keeper：单账号抖音续火花 Web 服务入口。"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core import (automation, autoreply, browser_pool, chat, inbox, msgstore, notify,
                   reply_engine, scheduler, slogan, watcher)
from core.config import DATA_DIR, load_config, save_config
from core.runtime import (
    load_runtime,
    recent_logs,
    record_contacts,
    record_run,
    set_running,
    setup_logging,
    update_runtime,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATE_PATH = DATA_DIR / "state.json"


def _load_env() -> None:
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_env()
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "").strip()
logger = setup_logging()

run_lock = threading.Lock()

# 服务刚启动时不可能有任务在运行；但 runtime.json 里的 running 可能因
# 进程被 kill / 崩溃而残留为 true（finally 来不及执行），会让面板永远显示
# "运行中"，按钮 :loading 一直禁用 → 用户看到的就是"点击没反应"。
try:
    if load_runtime().get("running"):
        set_running(False)
        logger.info("启动时清理残留的运行标志（上次任务被异常中断）")
except Exception:
    pass
contacts_fetching = False

# ---- 对话系统：扫描/草稿/发送都要独占浏览器，先抢锁 ----
reply_lock = threading.Lock()
reply_loop_stop = threading.Event()
last_cycle: dict = {"at": None, "report": None}


def _reply_loop() -> None:
    """全自动模式的巡检循环。mode 不是 auto 时彻底空转，不做任何浏览器动作。"""
    while not reply_loop_stop.wait(900):
        try:
            cfg = autoreply.get_cfg()
            if cfg.get("mode") != "auto":
                continue
            if autoreply.in_quiet_hours(cfg):
                continue
            if not reply_lock.acquire(blocking=False):
                continue
            try:
                report = autoreply.run_cycle("auto")
                last_cycle["at"] = report.get("at")
                last_cycle["report"] = report
                if report.get("sent"):
                    logger.info("全自动回复 %s 条", len(report["sent"]))
                    notify.alert(
                        "auto_replied", "info", f"已自动回复 {len(report['sent'])} 人",
                        "；".join(f"{x['peer']}：{x['text']}" for x in report["sent"][:5]),
                    )
            finally:
                reply_lock.release()
        except Exception as e:
            logger.warning("自动回复巡检异常: %s", str(e)[:160])


def _check_auth(token: str) -> None:
    if AUTH_TOKEN and token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="访问令牌不正确")


def _start_run(dry: bool, only_names: list[str] | None = None, fast: bool = False) -> None:
    if not run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")

    def worker() -> None:
        try:
            set_running(True)
            try:
                # 定时任务（非手动、非干跑）先刷新一次聊天列表，
                # 否则「谁有火花」用的是上次抓取的旧数据，可能漏发或错发。
                # 刷新前先留存"上一轮确认过有火花"的快照，供渲染抖动时回退
                prev_contacts = load_runtime().get("contacts") or []
                if not dry and not fast:
                    try:
                        if load_config().get("refresh_contacts_before_send", True):
                            logger.info("定时任务开始前刷新聊天列表（保证火花判断用最新数据）")
                            fres = automation.fetch_chat_contacts()
                            record_contacts(fres)
                            if fres.get("error"):
                                logger.warning("刷新聊天列表失败，改用上次缓存：%s", str(fres["error"])[:120])
                            else:
                                logger.info("聊天列表已刷新：%s 个会话", len(fres.get("items") or fres.get("names") or []))
                    except Exception as e:
                        logger.warning("刷新聊天列表异常，改用上次缓存：%s", str(e)[:120])

                result = automation.run_send(dry_run=dry, only_names=only_names, fast=fast,
                                             previous_contacts=prev_contacts)
                record_run(result)
                logger.info(
                    "本次发送完成：成功 %s 人，失败 %s 人，dry=%s",
                    len(result.get("ok", [])),
                    len(result.get("failed", [])),
                    dry,
                )
                # 只对"必须有人处理"的情况告警；单条失败只记日志，否则会变成噪音
                if result.get("logged_out"):
                    notify.alert(
                        "logged_out", "critical", "抖音登录态已失效",
                        "本轮已中止，未发出任何消息。需要重新扫码，"
                        "然后在网页「概览 → 上传登录态」提交新的 state.json。",
                    )
                elif result.get("rate_limited"):
                    notify.alert(
                        "rate_limited", "warning", "疑似触发抖音限流",
                        "本轮已提前停止。建议把「定时任务间隔」调大，并隔几小时再试。",
                    )
                elif not dry and not result.get("ok") and result.get("failed"):
                    detail = "；".join(
                        f"{f.get('name')}: {f.get('reason')}" for f in result["failed"][:3]
                        if isinstance(f, dict)
                    )
                    notify.alert("all_failed", "critical", "本轮全部失败，0 条发出", detail[:300])
                if not dry and result.get("failed") and not result.get("logged_out"):
                    failed_names = [
                        f["name"]
                        for f in result.get("failed", [])
                        if isinstance(f, dict)
                        and isinstance(f.get("name"), str)
                        and f["name"] != "_system"
                    ]
                    if failed_names:
                        rt = load_runtime()
                        today = datetime.now().date().isoformat()
                        if rt.get("retry_date") != today:
                            update_runtime(retry_date=today)
                            scheduler.schedule_retry(lambda: _start_run(False, failed_names))
                elif not dry:
                    scheduler.cancel_retry()
            finally:
                set_running(False)
        finally:
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()


def _start_fetch_contacts() -> None:
    global contacts_fetching
    if not run_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有任务在运行")

    def worker() -> None:
        global contacts_fetching
        try:
            contacts_fetching = True
            try:
                res = automation.fetch_chat_contacts()
                record_contacts(res)
                if res.get("error"):
                    notify.alert("contacts_failed", "warning", "获取聊天列表失败",
                                 str(res["error"])[:200] + "（若提示需要登录，请先扫码上传登录态）")
            finally:
                contacts_fetching = False
        finally:
            run_lock.release()

    threading.Thread(target=worker, daemon=True).start()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        scheduler.configure(lambda: _start_run(False))
    except Exception as e:  # pragma: no cover
        logger.warning("调度器启动失败: %s", e)
    threading.Thread(target=_reply_loop, daemon=True).start()
    try:
        watcher.start()      # 线程常驻，只有 enabled 时才真正轮询
    except Exception as e:
        logger.warning("实时监听启动失败: %s", e)
    yield
    reply_loop_stop.set()
    scheduler.shutdown()
    try:
        watcher.stop()
    except Exception:
        pass
    try:
        browser_pool.shutdown()
    except Exception:
        pass


app = FastAPI(title="Douyin Spark Keeper", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ConfigBody(BaseModel):
    config: dict


class RunBody(BaseModel):
    dry: bool = False
    fast: bool = True  # 手动触发默认走 manual_gap（秒级）；定时任务与补发走安全间隔


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


_expiry_cache: dict = {"mtime": None, "data": {"at": None, "days": None, "level": "none"}}


def _state_expiry() -> dict:
    """登录态剩余天数。

    取关键 cookie（sessionid*）里最早到期的时间点。这么做的好处是**不发任何请求**：
    既不额外暴露风控面，也不占用那台 2核4G 机器的浏览器资源。
    注意它只反映 cookie 的自然到期时间，被踢下线要靠运行时的 logged_out 信号兜底。
    """
    try:
        mtime = STATE_PATH.stat().st_mtime
    except Exception:
        return {"at": None, "days": None, "level": "none"}

    if _expiry_cache["mtime"] != mtime:
        at, days = None, None
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            stamps = [
                float(c["expires"])
                for c in data.get("cookies", [])
                if str(c.get("name", "")).startswith("sessionid")
                and isinstance(c.get("expires"), (int, float))
                and c["expires"] > 0
            ]
            if stamps:
                ts = min(stamps)
                at = datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")
                days = round((ts - time.time()) / 86400, 1)
        except Exception:
            pass

        if days is None:
            level = "none"
        elif days <= 0:
            level = "expired"
        elif days <= 1:
            level = "critical"
        elif days <= 3:
            level = "warning"
        else:
            level = "ok"
        _expiry_cache.update(mtime=mtime, data={"at": at, "days": days, "level": level})
    return _expiry_cache["data"]


@app.get("/api/status")
def api_status(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    rt = load_runtime()
    exp = _state_expiry()
    if exp["level"] in ("critical", "expired"):
        # 去重由 notify 内部按 key 做 24 小时窗口，这里可以放心每轮都调
        notify.alert(
            "state_expiry", "critical",
            "抖音登录态即将过期" if exp["level"] == "critical" else "抖音登录态已过期",
            f"按 cookie 到期时间估算剩余 {exp['days']} 天（{exp['at']}）。"
            "请在过期前重新扫码并上传 state.json，否则定时任务会全部失败。",
        )
    return {
        "state_file_exists": STATE_PATH.exists(),
        "session_status": rt.get("session_status", "unknown"),
        "running": rt.get("running", False),
        "last_run": rt.get("last_run"),
        "next_run": scheduler.next_run_time(),
        "history_count": len(rt.get("history", [])),
        "progress": dict(automation.PROGRESS),
        "auth_required": bool(AUTH_TOKEN),
        "state_expire_at": exp["at"],
        "state_expire_days": exp["days"],
        "state_expire_level": exp["level"],
        "alert_count": len(notify.recent(50)),
        "version": "0.1.0",
    }


@app.get("/api/alerts")
def api_alerts(token: str = Header(default="", alias="X-Auth-Token"), limit: int = 20) -> dict:
    _check_auth(token)
    return {"alerts": notify.recent(max(1, min(limit, 100)))}


@app.post("/api/alert/test")
def api_alert_test(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    notify.alert(
        "_test", "info", "这是一条测试告警",
        "看到这条说明告警链路是通的。没收到推送的话，检查「定时设置」里的 Webhook 地址。",
        force=True,
    )
    cfg = load_config()
    return {"ok": True, "webhook_configured": bool(str(cfg.get("alert_webhook") or "").strip())}


@app.get("/api/config")
def api_config(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    return load_config()


@app.get("/api/contacts")
def api_contacts(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    rt = load_runtime()
    return {
        "contacts": rt.get("contacts", []),
        "contacts_at": rt.get("contacts_at"),
        "contacts_error": rt.get("contacts_error"),
        "fetching": contacts_fetching,
    }


@app.post("/api/contacts/fetch")
def api_contacts_fetch(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    try:
        _start_fetch_contacts()
    except HTTPException:
        raise
    return {"ok": True, "started": True}


@app.put("/api/config")
def api_config_save(body: ConfigBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    try:
        cfg = save_config(body.config)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    scheduler.apply_schedule()
    return {"ok": True, "config": cfg}


@app.get("/api/slogan/preview")
def api_slogan_preview(day: str = "", token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """每日文案预览：按日期查看当天会给每个好友发什么、以及额外示例。

    ?day=YYYY-MM-DD 指定日期，缺省为今天。
    需要 X-Auth-Token（与其它接口一致）。
    """
    _check_auth(token)
    cfg = load_config()
    peers = cfg.get("friends") or []
    try:
        return slogan.preview_for_date(day, peers)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"日期格式错误：{e}")


@app.post("/api/run")
def api_run(body: RunBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    # 名单为空时直接拒绝，避免"点了没反应"——任务启动后立刻跳过，用户看不到任何反馈
    cfg = load_config()
    if not (cfg.get("friends") or []):
        raise HTTPException(
            status_code=400,
            detail="好友名单为空：请到「好友与消息」勾选好友 → 点「把勾选结果写入名单」→ 点「保存」，再回来点发送",
        )
    try:
        _start_run(bool(body.dry), fast=bool(body.fast))
    except HTTPException:
        raise
    return {"ok": True, "started": True}


@app.post("/api/stop")
def api_stop(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """请求停止当前这一轮发送。协作式停止：下一个好友前、或间隔等待中立即退出。"""
    _check_auth(token)
    automation.request_stop()
    logger.info("收到停止指令，本轮将在当前好友处理完后结束")
    return {"ok": True, "stopping": True}


class PeerBody(BaseModel):
    peer: str


class InboxStatusBody(BaseModel):
    peer: str
    status: str
    note: str = ""


class InboxReplyBody(BaseModel):
    peer: str
    text: str


class ReplyRunBody(BaseModel):
    mode: str = "semi"
    peer: str | None = None


class ReplyTestBody(BaseModel):
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""


def _grab_reply_lock() -> None:
    if not reply_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="另一个浏览器任务正在运行（发送/扫描/草稿），请稍后再试")


class WatchBody(BaseModel):
    enabled: bool | None = None
    interval_sec: int | None = None
    max_peers_per_cycle: int | None = None
    autodraft: bool | None = None
    run_now: bool = False


@app.get("/api/chat/{peer}")
def api_chat_history(peer: str, limit: int = 200,
                     token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """某个会话的**归档**聊天记录（持续累积，不是临时读一次）。"""
    _check_auth(token)
    rows = msgstore.recent(peer, max(1, min(1000, limit)))
    return {"ok": True, "peer": peer, "count": len(rows), "messages": rows,
            "last_in": (msgstore.last_inbound(peer) or {}).get("text")}


@app.post("/api/chat/{peer}/sync")
def api_chat_sync(peer: str, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """立刻打开这个会话读一次并归档（用于点开聊天记录时手动同步）。"""
    _check_auth(token)
    preview = ((inbox.load().get("items", {}).get(peer) or {}).get("preview") or "").strip()
    got = chat.open_and_read(peer, limit=20, expect=preview)
    msgs = got.get("items") or []
    added = msgstore.append(peer, msgs) if msgs else {"new": 0}
    rows = msgstore.recent(peer, 200)
    return {"ok": bool(msgs), "peer": peer, "new": added.get("new", 0),
            "count": len(rows), "messages": rows,
            "error": got.get("error"), "conf": got.get("conf")}


@app.post("/api/watch")
def api_watch(body: WatchBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """开启/关闭实时监听，或调整轮询参数。"""
    _check_auth(token)
    patch = {k: v for k, v in body.model_dump().items()
             if k != "run_now" and v is not None}
    if patch:
        watcher.save_cfg(patch)
    if body.run_now:
        watcher.kick()
    return {"ok": True, "watch": watcher.status(),
            "messages": msgstore.stats()}


@app.post("/api/watch/run")
def api_watch_run(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """立刻跑一轮监听（不等下一个间隔）。"""
    _check_auth(token)
    res = watcher.run_cycle(force=True)
    return {"ok": True, "report": res, "watch": watcher.status()}


@app.get("/api/inbox")
def api_inbox(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    data = inbox.summary()
    cfg = autoreply.get_cfg()
    data["reply"] = {
        "mode": cfg.get("mode"),
        "whitelist": cfg.get("whitelist") or [],
        "quiet_hours": cfg.get("quiet_hours"),
        "downgraded": cfg.get("_downgraded"),
    }
    data["last_cycle"] = last_cycle
    data["watch"] = watcher.status()
    data["archive"] = msgstore.stats()
    # 打开面板时顺手把浏览器会话预热起来（后台线程，不阻塞这次请求）。
    # 冷启动那页抖音列表要 ~52 秒才渲染，预热后用户点「扫描」「生成建议」就是秒级。
    try:
        data["prewarm_started"] = browser_pool.warm_async()
    except Exception:
        data["prewarm_started"] = False
    data["browser"] = browser_pool.stats()
    return data


@app.post("/api/inbox/scan")
def api_inbox_scan(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    _grab_reply_lock()
    try:
        res = chat.scan_inbox()
        inbox.apply_scan(res)
        data = inbox.summary()
        data["ok"] = not res.get("error")
        data["error"] = res.get("error")
        return data
    finally:
        reply_lock.release()


@app.post("/api/inbox/draft")
def api_inbox_draft(body: PeerBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    _grab_reply_lock()
    try:
        res = autoreply.draft_for(body.peer)
        data = inbox.summary()
        data["ok"] = res.get("ok")
        data["draft_result"] = res
        if not res.get("ok"):
            data["error"] = res.get("error")
        return data
    finally:
        reply_lock.release()


@app.post("/api/inbox/reply")
def api_inbox_reply(body: InboxReplyBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="回复内容不能为空")
    _grab_reply_lock()
    try:
        res = chat.send_text(body.peer, text)
        if res.get("ok"):
            inbox.set_status(body.peer, "sent", note=f"manual: {text[:40]}")
            inbox.audit({"action": "manual_sent", "peer": body.peer, "text": text[:120]})
        else:
            inbox.audit({"action": "manual_failed", "peer": body.peer, "reason": res.get("reason")})
        return {"ok": bool(res.get("ok")), "reason": res.get("reason")}
    finally:
        reply_lock.release()


@app.post("/api/inbox/status")
def api_inbox_status(body: InboxStatusBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    if body.status not in ("unread", "pending", "sent", "skipped"):
        raise HTTPException(status_code=400, detail="状态非法")
    return inbox.set_status(body.peer, body.status, body.note)


@app.get("/api/reply/config")
def api_reply_config(token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    cfg = autoreply.get_cfg()
    out = dict(cfg)
    out["llm_api_key"] = ""                     # 永不回传明文密钥
    out["llm_api_key_set"] = bool(cfg.get("llm_api_key"))
    out["human_boundary"] = autoreply.HUMAN_BOUNDARY
    out["defaults"] = autoreply.DEFAULTS
    return out


@app.put("/api/reply/config")
def api_reply_config_save(body: ConfigBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    current = load_config()
    incoming = dict(body.config or {})
    merged = dict(current.get("reply") or {})
    # 空字符串表示"不修改密钥"
    key_in = str(incoming.pop("llm_api_key", "") or "").strip()
    merged.update(incoming)
    if key_in:
        merged["llm_api_key"] = key_in
    merged.pop("llm_api_key_set", None)
    merged.pop("human_boundary", None)
    merged.pop("defaults", None)
    merged.pop("_downgraded", None)
    try:
        saved = save_config({**current, "reply": merged})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("对话配置已更新：mode=%s 白名单=%s",
                saved["reply"].get("mode"), len(saved["reply"].get("whitelist") or []))
    return {"ok": True, "reply": {**saved["reply"], "llm_api_key": ""},
            "llm_api_key_set": bool(saved["reply"].get("llm_api_key"))}


@app.post("/api/reply/run")
def api_reply_run(body: ReplyRunBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    _grab_reply_lock()
    try:
        report = autoreply.run_cycle(body.mode, body.peer)
        last_cycle["at"] = report.get("at")
        last_cycle["report"] = report
        return {"ok": True, "report": report, "inbox": inbox.summary()}
    finally:
        reply_lock.release()


@app.post("/api/reply/test-llm")
def api_reply_test_llm(body: ReplyTestBody, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    """验证大模型配置是否可用：发一条最小请求，原样回显失败原因。"""
    _check_auth(token)
    cur = load_config().get("reply") or {}
    cfg = {
        "llm_base_url": (body.llm_base_url or cur.get("llm_base_url") or "").strip(),
        "llm_model": (body.llm_model or cur.get("llm_model") or "").strip(),
        "llm_api_key": (body.llm_api_key or cur.get("llm_api_key") or "").strip(),
    }
    return reply_engine.test_llm(cfg)


@app.get("/api/reply/audit")
def api_reply_audit(token: str = Header(default="", alias="X-Auth-Token"), limit: int = 50) -> dict:
    _check_auth(token)
    return {"audit": inbox.recent_audit(max(1, min(limit, 200)))}


@app.post("/api/upload-state")
async def api_upload_state(
    file: UploadFile = File(...),
    token: str = Header(default="", alias="X-Auth-Token"),
) -> dict:
    _check_auth(token)
    raw = await file.read()
    if len(raw) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件过大")
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="不是合法的 JSON 文件")
    if not isinstance(data.get("cookies"), list) or not data["cookies"]:
        raise HTTPException(status_code=400, detail="缺少 cookies 字段，请确认是 Playwright 导出的登录态文件")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_bytes(raw)
    logger.info("已更新登录态 state.json（%s 字节）", len(raw))
    return {"ok": True, "size": len(raw)}


@app.get("/api/logs")
def api_logs(n: int = 300, token: str = Header(default="", alias="X-Auth-Token")) -> dict:
    _check_auth(token)
    return {"logs": "\n".join(recent_logs(max(10, min(n, 600))))}


if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host=host, port=port, log_level="info")
