"""回复生成引擎：规则优先，可选接入 OpenAI 兼容接口。

设计原则：
1. 默认零依赖可用 —— 没有 API Key 时走规则库，功能不降级成"不可用"；
2. 只对"低信息量"消息做自动回复 —— 判断逻辑在 autoreply.py，这里只负责"说什么"；
3. 输出永远是 2~3 条候选，交给上层决定是发出去还是给人看；
4. 绝不把消息正文写进日志。
"""

from __future__ import annotations

import json
import logging
import random
import re
import urllib.request
from typing import Any

logger = logging.getLogger("douyin-spark")

# 最近一次调用 LLM 的失败原因，供"测试连接"接口回显（不写日志正文）
LAST_LLM_ERROR = ""

# 寒暄类意图词表：命中即视为"维持性"消息，可安全自动回复
GREETING_PATTERNS = [
    (r"在吗|在么|在不在|在嘛|人呢", ["在的", "在", "刚看到"]),
    (r"睡了吗|睡了没|还没睡", ["还没呢", "准备睡了，你呢"]),
    (r"干嘛呢|干啥呢|做什么呢|忙啥", ["刚忙完，歇会儿", "在发呆哈哈"]),
    (r"^哈+$|^嘻+$|^嘿+|笑哭|笑死|233", ["哈哈哈", "笑死", "笑不活了"]),
    (r"晚安|睡了", ["晚安", "晚安啦"]),
    (r"早安|早啊|早上好", ["早", "早呀"]),
    (r"^嗯+|^哦+|^啊+|^行$|^好$|^可以$|^收到$", ["嗯嗯", "行", "好嘞"]),
    (r"吃饭了吗|吃了吗", ["吃了，你呢", "还没呢"]),
    (r"谢谢|多谢|感谢", ["客气啦", "不客气"]),
]

# 低信息量的默认应答池（不含问句，避免把话题抛回去却没内容）
GENERIC_REPLIES = [
    "嗯嗯",
    "哈哈确实",
    "是这样",
    "有道理",
    "刚看到",
]

# 硬阻断：命中任何一条，绝不自动发送（只出草稿）
HARD_BLOCKS = [
    (r"http|www\.|\.com|\.cn|【.*】", "包含链接或卡片"),
    (r"\d{5,}", "包含长数字（可能是号码/验证码）"),
    (r"转账|转你|转我|转给|红包|收款|付款|汇款|还钱|借钱|借我|借你|还我|打钱|\d+\s*(块|元|万)", "涉及金钱"),
    (r"分手|吵架|生气|难过|哭了|烦死|抑郁|不想活", "负面情绪"),
    (r"喜欢你|想你|爱你|在一起|表白", "亲密表达"),
    (r"医院|生病|住院|去世|出事|车祸", "突发状况"),
]

# 提问：需要人来回答，不做自动回复
QUESTION_RE = r"\?|？|吗[？?]?$|呢[？?]?$|吧[？?]?$|怎么办|为什么|怎么弄|几点|哪里|是不是"

# 低信息量：纯语气词 / 应答词，才允许自动接话
LOW_INFO_RE = r"^[嗯哦啊哈嘿好行可以收到对是的呗呀啦咦哇欸嗯呐~～。，、\s]{1,8}$"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def classify(text: str) -> dict[str, Any]:
    """给一条收到的消息打标签：是否可自动回复、命中了什么阻止原因。

    判定顺序很重要：硬阻断 → 寒暄 → 提问 → 纯语气词 → 其余人工。
    「在吗」走寒暄，「我转你200块」走硬阻断，两者不会互相覆盖。
    """
    t = _clean(text)
    if not t:
        return {"auto_ok": False, "reason": "空消息", "kind": "empty"}

    for pattern, reason in HARD_BLOCKS:
        if re.search(pattern, t, re.I):
            return {"auto_ok": False, "reason": reason, "kind": "blocked"}

    for pattern, _pool in GREETING_PATTERNS:
        if re.search(pattern, t, re.I):
            return {"auto_ok": True, "reason": "寒暄类", "kind": "greeting"}

    if re.search(QUESTION_RE, t):
        return {"auto_ok": False, "reason": "包含提问，需人来答", "kind": "question"}

    if re.match(LOW_INFO_RE, t):
        return {"auto_ok": True, "reason": "语气词/应答词", "kind": "short"}

    return {"auto_ok": False, "reason": "信息量较大，建议人工处理", "kind": "content"}


def _rule_replies(text: str, history: list[dict] | None = None) -> list[str]:
    t = _clean(text)
    for pattern, pool in GREETING_PATTERNS:
        if re.search(pattern, t, re.I):
            picks = random.sample(pool, k=min(2, len(pool)))
            extra = random.choice(GENERIC_REPLIES)
            return picks + ([extra] if extra not in picks else [])

    # 从我的历史回复里学语气：优先复用我过去说过、且当前这条没重复过的短句
    mine = [m.get("text", "") for m in (history or []) if m.get("direction") == "out"]
    mine = [x for x in mine if 1 <= len(x) <= 8 and x != t]
    pool = random.sample(GENERIC_REPLIES, k=min(2, len(GENERIC_REPLIES)))
    if mine:
        pool.insert(0, random.choice(mine))
    return pool


def _llm_replies(text: str, history: list[dict], cfg: dict) -> list[str] | None:
    """调用 OpenAI 兼容接口。失败返回 None，由上层回退规则。"""
    global LAST_LLM_ERROR

    base = str(cfg.get("llm_base_url") or "").rstrip("/")
    key = str(cfg.get("llm_api_key") or "")
    model = str(cfg.get("llm_model") or "")
    if not (base and key and model):
        return None

    # 只给最近 8 轮，且脱敏：不带任何昵称以外的标识
    ctx_lines = []
    for m in (history or [])[-8:]:
        who = "我" if m.get("direction") == "out" else "对方"
        ctx_lines.append(f"{who}：{_clean(m.get('text'))[:60]}")
    ctx = "\n".join(ctx_lines) or "（无历史）"

    persona = str(cfg.get("persona") or "简短、口语、不说教，像微信里随手回的那种")
    prompt = (
        f"你在帮用户回复抖音私信。人设：{persona}。\n"
        f"最近对话：\n{ctx}\n\n"
        f"对方刚说：{text}\n\n"
        "要求：\n"
        "1) 只回最后一句，不要开启新话题，不要连续追问\n"
        "2) 每条必须是完整通顺的中文口语，不许出现半截话、错别字或拼凑词（例如『行发吧』这种不合格）\n"
        "3) 每条 4~15 个字，不要标点堆叠，不要颜文字\n"
        "4) 对方在提问时，用「随口应一声」的口吻，不要替我做承诺或决定\n"
        "5) 输出 3 条不同语气的候选，每行一条，不要编号，不要解释"
    )

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.8,
        # 注意：deepseek-flash 这类是推理模型，会先花 token 思考，
        # 上限给小了会出现"finish_reason=length 且 content 为空"（踩过，120 全被 reasoning 吃光）
        "max_tokens": 1200,
    }).encode()
    req = urllib.request.Request(
        base + "/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = (msg.get("content") or "").strip()
        finish = choice.get("finish_reason") or ""

        if not content:
            if msg.get("reasoning_content"):
                LAST_LLM_ERROR = (f"模型只输出了思考过程、正文为空（finish_reason={finish}）。"
                                  "这通常是推理模型 + token 上限不足，可换非推理模型或调大上限")
            elif finish == "length":
                LAST_LLM_ERROR = "输出被截断（finish_reason=length），正文为空"
            else:
                LAST_LLM_ERROR = f"返回正文为空（finish_reason={finish}）"
            logger.warning("回复生成失败，回退规则库：%s", LAST_LLM_ERROR)
            return None

        lines = [re.sub(r"^[\d\.\-\)\s、]+", "", l).strip() for l in content.splitlines()]
        lines = [l for l in lines if l and len(l) <= 30]
        return lines[:3] or None
    except Exception as e:
        LAST_LLM_ERROR = f"{type(e).__name__}: {str(e)[:160]}"
        logger.warning("回复生成失败，回退规则库：%s", LAST_LLM_ERROR)
        return None


def generate(text: str, history: list[dict] | None = None, cfg: dict | None = None) -> dict:
    """生成候选回复。返回 {replies, source, kind, auto_ok, reason}。"""
    cfg = cfg or {}
    cls = classify(text)
    replies: list[str] | None = None

    if cfg.get("llm_enabled"):
        replies = _llm_replies(text, history or [], cfg)
    source = "llm" if replies else "rules"
    if not replies:
        replies = _rule_replies(text, history)

    # 兜底清洗：去重、限长、保证至少一条
    seen, out = set(), []
    for r in replies:
        r = _clean(r)[:40]
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    if not out:
        out = [random.choice(GENERIC_REPLIES)]

    return {
        "replies": out[:3],
        "source": source,
        "kind": cls["kind"],
        "auto_ok": cls["auto_ok"],
        "reason": cls["reason"],
    }


def test_llm(cfg: dict) -> dict:
    """连通性自检：用一条固定输入跑一次真实调用，把失败原因原样回显。

    不碰浏览器、不发送任何消息，纯粹验证 base_url / model / key 是否可用。
    """
    import time as _t

    global LAST_LLM_ERROR
    LAST_LLM_ERROR = ""

    base = str(cfg.get("llm_base_url") or "").rstrip("/")
    key = str(cfg.get("llm_api_key") or "")
    model = str(cfg.get("llm_model") or "")
    missing = [n for n, v in (("接口地址", base), ("API Key", key), ("模型名", model)) if not v]
    if missing:
        return {"ok": False, "error": "缺少配置：" + "、".join(missing), "latency_ms": 0}

    t0 = _t.time()
    replies = _llm_replies("在吗", [{"direction": "in", "text": "在吗"}], dict(cfg, llm_enabled=True))
    latency = int((_t.time() - t0) * 1000)

    if not replies:
        return {"ok": False, "error": LAST_LLM_ERROR or "调用返回空", "latency_ms": latency}

    ok_shape = all(isinstance(r, str) and 0 < len(r) <= 40 for r in replies)
    return {
        "ok": True,
        "latency_ms": latency,
        "model": model,
        "replies": replies[:3],
        "shape_ok": ok_shape,
    }
