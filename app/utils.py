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


def preference_delta(rating: int, tags: list[str] | None = None) -> dict:
    """单条反馈对 5 维偏好的增量。rating: 2=喜欢 1=一般 0=不喜欢。"""
    step = {2: 1, 1: 0, 0: -1}.get(rating, 0)
    return {k: step for k in PREF_KEYS}


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


def update_preferences(prefs: dict, rating: int, tags: list[str]) -> dict:
    """根据反馈 rating 和 tags 微调长期偏好分。

    规则：
    - 喜欢(+2): color/style/comfort/occasion/fabric 各 +1
    - 一般(0): 不变
    - 不喜欢(-2): color/style/comfort/occasion/fabric 各 -1
    - 限制在 1-10
    """
    delta = {2: 1, 1: 0, 0: -1}.get(rating, 0)
    keys = ["color", "style", "comfort", "occasion", "fabric"]
    for k in keys:
        prefs[k] = int(clamp(prefs.get(k, 5) + delta, 1, 10))
    return prefs
