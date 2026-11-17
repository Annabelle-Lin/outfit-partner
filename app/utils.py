# -*- coding: utf-8 -*-
"""通用工具函数。"""
import secrets
import string
from datetime import datetime


def gen_id(prefix: str = "id", length: int = 8) -> str:
    """生成业务 ID，如 u_abc12345。"""
    return f"{prefix}_{secrets.token_hex(length // 2)}"


def now_str() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def clamp(value: int | float, low: int | float, high: int | float) -> int | float:
    return max(low, min(high, value))


PREF_KEYS = ("color", "style", "comfort", "occasion", "fabric")


# ---------------------------------------------------------------------------
# 标签 → 偏好维度映射
#
# 一期的核心：让「颜色不喜欢」和「太正式」对偏好的影响不再一模一样。
# 键必须与前端 #fb-reasons 的 data-v 词表保持一致（见 frontend/index.html），
# 否则标签永远命不中，delta 会静默退回全局步长。
# 值为 (维度, 方向权重)；权重为负表示「该维度让你不满意」。
# ---------------------------------------------------------------------------
TAG_TO_DIM: dict[str, tuple[str, int]] = {
    # 颜色
    "颜色不喜欢": ("color", -1),
    "颜色太花": ("color", -1),
    "颜色太亮": ("color", -1),
    "颜色太暗": ("color", -1),
    # 版型（项目无独立 fit 维度，归入 style，避免为了一期改表结构）
    "版型不合适": ("style", -1),
    "版型太长": ("style", -1),
    "版型太短": ("style", -1),
    "版型太宽": ("style", -1),
    # 场合
    "不适合场合": ("occasion", -1),
    "太正式": ("occasion", -1),
    "太随意": ("occasion", -1),
    # 体感（暖度并入 comfort）
    "太冷": ("comfort", -1),
    "太热": ("comfort", -1),
    "不透气": ("comfort", -1),
    # 面料
    "面料扎": ("fabric", -1),
    "面料不舒服": ("fabric", -1),
    "起球": ("fabric", -1),
}


# 自由文本 → 规范标签的规则表。
# 顺序有意义：越具体的意图放越前面，命中即止（避免一句话打上多个互斥标签）。
_COMMENT_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("扎", "刺痒", "起球", "静电", "面料", "质感差", "闷汗"), "面料不舒服"),
    (("太冷", "不保暖", "冻", "凉"), "太冷"),
    (("太热", "捂", "出汗", "闷热"), "太热"),
    (("太正式", "上班太", "职业"), "太正式"),
    (("太随意", "不够正式", "见客户", "不体面"), "太随意"),
    (("场合", "约会", "开会", "婚礼", "面试"), "不适合场合"),
    (("颜色", "花", "艳", "亮", "暗", "显黑", "土", "色"), "颜色不喜欢"),
    (("版型", "宽", "紧", "肥", "瘦", "长", "短", "显胖", "显矮", "肩"), "版型不合适"),
)


def parse_comment_to_tags(comment: str | None) -> list[str]:
    """把用户的自由文本反馈映射成规范标签（规则优先，纯函数、可单测）。

    例："这件袖子太长，颜色也有点艳" -> ["颜色不喜欢", "版型不合适"]

    一期只做规则，不接 LLM：反馈接口是同步链路，接外部模型会把
    提交耗时绑在网络抖动上，且失败时用户完全无感。规则表可后续增量扩充。
    """
    if not comment or not isinstance(comment, str):
        return []
    text = comment.strip()
    if not text:
        return []

    found: list[str] = []
    for keywords, tag in _COMMENT_RULES:
        if tag in found:
            continue
        if any(kw in text for kw in keywords):
            found.append(tag)
    return found


def preference_delta(rating: int, tags: list[str] | None = None) -> dict:
    """单条反馈对 5 维偏好的增量。rating: 2=喜欢 1=一般 0=不喜欢。

    两种模式：
    1. 无标签命中（或未传标签）——退回旧行为，五维同增同减 ±1。
       保留这条路径是为了兼容历史前端与历史记录，行为不变。
    2. 有标签命中——只调整被指名的维度，幅度 ±1 或 ±2。
       这就是「颜色不喜欢」不再等价于「太正式」的关键。
    """
    step = {2: 1, 1: 0, 0: -1}.get(rating, 0)

    clean_tags = [t.strip() for t in (tags or []) if isinstance(t, str) and t.strip()]
    hit: dict[str, int] = {}
    for t in clean_tags:
        mapped = TAG_TO_DIM.get(t)
        if not mapped:
            continue
        dim, weight = mapped
        hit[dim] = hit.get(dim, 0) + abs(weight)

    # 无命中：旧口径
    if not hit:
        return {k: step for k in PREF_KEYS}

    # 有命中：方向由 rating 决定。
    # rating=1（一般）但用户点了具体问题标签时，语义仍是负向，按 -1 处理。
    sign = 1 if rating == 2 else -1
    # 喜欢/不喜欢是强信号，一般配具体标签是弱信号
    base = 2 if rating in (0, 2) else 1

    delta = {k: 0 for k in PREF_KEYS}
    for dim, weight in hit.items():
        mag = min(2, max(1, base + (weight - 1)))
        delta[dim] = sign * mag
    return delta


def recompute_preferences(records: list[dict]) -> dict:
    """由全部反馈重算 5 维偏好（2.3 幂等）。

    以中性值 5 为基准累加每条反馈的增量，再夹到 1-10。
    幂等的好处：撤销一条记录后重算一次，结果与「从未提交过该反馈」完全一致，
    不会像增量更新那样残留历史误差。
    """
    prefs = {k: 5 for k in PREF_KEYS}
    for rec in records:
        evidence = rec.get("evidence") or {}
        # 撤销采用墓碑标记：保留证据链，但不再参与偏好重算。
        if evidence.get("undone"):
            continue
        delta = evidence.get("delta") or {}
        for k in PREF_KEYS:
            prefs[k] += int(delta.get(k, 0))
    return {k: int(clamp(v, 1, 10)) for k, v in prefs.items()}
