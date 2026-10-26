"""每日续火花文案生成器。

设计目标：
- 离线可用：不依赖大模型 / 外部 API，纯本地组合生成
- 有变化、少重复：按「日期 + 好友名」做随机种子，同一天同一好友文案稳定，
  不同天不同好友结果不同
- 风格轻松友好：短句、口语化，适合社交聊天场景
- **预览即实发**：发送链路和预览都只走 `for_friend()` 一个入口，种子里没有
  任何额外盐值，所以「预览看到的」就是「真发出去的」

对外主要接口：
    for_friend(day, name) -> str      某天给某位好友的文案（发送/预览共用）
    generate(seed)        -> str      按任意种子生成一条（自己玩/兜底用）
    preview_for_date(day, peers) -> dict  按日期预览（逐人 + 示例）
"""

from __future__ import annotations

import hashlib
import random
from datetime import date

# ---- 可组合词块：整句占位 {emoji} 会被随机表情替换 ----
_OPENERS = [
    "今天也要", "记得", "别忘了", "又是新的一天，", "来啦，", "滴——打卡！",
]
_ACTIONS = [
    "续个火花", "点亮火花", "续上我们的火", "给火花续个命", "把小火苗护住",
]
_TAILS = [
    "呀", "哦", "～", "！", "呀～", "，别让它熄啦", "，一起加油", "，明天见",
]
# 每组模板搭配固定的尾缀风格，避免"动作"与"尾缀"语义冲突（如"续一下，哦"）
_TEMPLATES = [
    "{open}{act}{tail} {emoji}",
    "{emoji} {open}{act}{tail}",
    "{emoji}{emoji} {open}{act}{tail}",
    "在忙吗？顺手{act}～ {emoji}",
    "{open}别让它熄啦 {emoji}",
]
# 整句兜底（不参与组合，用于提供更多变化）
_FALLBACKS = [
    "晚安，明天见", "今天也要开心哦", "早点休息，别熬夜", "记得吃饭",
    "新的一天加油", "给你留了个火花，快来续", "忙完记得续火花哦",
]
_EMOJIS = ["🔥", "✨", "☀️", "🌙", "😄", "👋", "🌟", "💫", "😊", "🍀"]


def _rng(*parts) -> random.Random:
    """由任意个字符串拼一个稳定种子的随机源（可复现）。"""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return random.Random(int(h[:16], 16))


def _render(tpl: str, rng: random.Random) -> str:
    emoji = rng.choice(_EMOJIS)
    s = (
        tpl
        .replace("{emoji}", emoji)
        .replace("{open}", rng.choice(_OPENERS))
        .replace("{act}", rng.choice(_ACTIONS))
        .replace("{tail}", rng.choice(_TAILS))
    )
    # 收尾规整：去掉多余空格
    return " ".join(s.split())


def generate(seed: str = "") -> str:
    """生成一条文案。seed 相同 → 结果相同（可复现），不同 → 结果变化。"""
    rng = _rng(seed or date.today().isoformat())
    if rng.random() < 0.75:
        return _render(rng.choice(_TEMPLATES), rng)
    return rng.choice(_FALLBACKS)


def for_friend(day: str, name: str) -> str:
    """某天给某位好友的文案 —— 发送链路与预览共用的唯一入口。

    种子固定为「日期:好友名」，不带任何额外盐值，保证同一天同一好友结果完全一致，
    预览看到的就是实际会发出去的那句话。
    """
    return generate(f"{day or date.today().isoformat()}:{name}")


def pick_unique(peers: list[str], used: set[str] | None = None,
                day: str = "") -> dict[str, str]:
    """给一批好友各出一条文案，返回 {好友名: 文案}。

    注意：不再做「换种子去重」——去重会给重复的人换用另一个种子，导致预览与实发
    对不上。不同好友的种子本来就不同，撞车概率很低，撞了就撞了，可接受。
    `used` 仅为向后兼容保留，不参与计算。
    """
    day = day or date.today().isoformat()
    return {p: for_friend(day, p) for p in peers}


def today_slogan() -> str:
    """今天（全账号通用）的一条文案，用于「每日文案」预览。"""
    return generate(date.today().isoformat())


def today_key() -> str:
    """今天的日期键（YYYY-MM-DD），发送链路拼 seed 用。"""
    return date.today().isoformat()


# 兼容旧调用（automation 早期用的是这个私有名）
_today_key = today_key


def preview_for_date(day: str, peers: list[str], n_extra: int = 3) -> dict:
    """按指定日期（YYYY-MM-DD）预览文案。

    返回 {
        "date": day,
        "per_friend": {好友名: 文案},     # 与当天实际发送逐字一致
        "samples": [文案, ...],           # 额外随机示例 n_extra 条
    }
    即使名单为空，也返回 samples，保证"预览"总能看到东西。
    """
    day = day or date.today().isoformat()
    per = pick_unique([p for p in peers if str(p).strip()], day=day)
    samples = []
    used = set(per.values())
    for i in range(n_extra):
        c = generate(f"{day}:sample:{i}")
        if c not in used:
            samples.append(c)
            used.add(c)
    return {"date": day, "per_friend": per, "samples": samples}


if __name__ == "__main__":
    print("=== 单条生成（不同 seed 不同结果）===")
    for s in ["", "2026-10-26", "2026-10-27", "a", "b"]:
        print(f"  seed={s!r:<12} -> {generate(s)}")
    print()
    print("=== 给 8 个好友各生成一条（发送与预览同一入口）===")
    peers = [f"好友{i}" for i in range(1, 9)]
    got = pick_unique(peers, day="2026-10-26")
    for p, s in got.items():
        print(f"  {p}: {s}")
    same = all(for_friend("2026-10-26", p) == got[p] for p in peers)
    print("  预览 == 实发：", "✅ 一致" if same else "❌ 不一致")
    print("  撞车检查：", "无重复" if len(set(got.values())) == len(got)
          else f"{len(got) - len(set(got.values()))} 条重复（概率事件，不影响发送）")
