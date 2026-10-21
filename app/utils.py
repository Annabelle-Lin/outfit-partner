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
