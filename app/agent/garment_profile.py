# -*- coding: utf-8 -*-
"""单品属性画像：子品类推断、暖度推断、季节推断与 Jaccard 重复度。

存在理由（对应实测缺陷）：
1. 衣物表 ``warmth_level`` 默认值是 3，前端没有暖度录入入口，
   于是「重型羽绒服」和「薄衬衫」在引擎里都是暖度 3。
   引擎 F014/F015/F002 这些按暖度生效的硬过滤因此全部形同虚设，
   实测出现 22℃ 下「羽绒服 + 凉鞋」仍被判为合法搭配。
   本模块按子品类推断暖度，用它替换占位默认值。
2. 规范 K-ALG-05 要求重复度用 Jaccard 相似度，
   旧实现是 ``min(1, 同类目数*0.3 + 同色数*0.2)``，与规范不符。

推断是「有依据的估算」，不是用户录入值：返回值里通过
``source`` 区分，调用方应如实标注，不能伪装成用户确认过的属性。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 品类归一

CATEGORY_ALIASES = {
    "上衣": "上装",
    "上装": "上装",
    "裤子": "下装",
    "下装": "下装",
    "裙子": "下装",
    "半身裙": "下装",
    "鞋子": "鞋",
    "鞋": "鞋",
    "外套": "外套",
    "连衣裙": "连衣裙",
    "连衣装": "连衣裙",
    "套装": "套装",
    "配件": "配饰",
    "配饰": "配饰",
}

SEASON_ALIASES = {
    "春": "春季",
    "夏": "夏季",
    "秋": "秋季",
    "冬": "冬季",
    "春天": "春季",
    "夏天": "夏季",
    "秋天": "秋季",
    "冬天": "冬季",
    "春秋": "春秋",
    "四季": "四季",
}


def normalize_category(value: str | None) -> str:
    if not value:
        return "上装"
    return CATEGORY_ALIASES.get(str(value).strip(), str(value).strip())


def normalize_season(value: str | None) -> str:
    if not value:
        return "四季"
    return SEASON_ALIASES.get(str(value).strip(), str(value).strip())


# ------------------------------------------------------- 子品类关键词推断
# 顺序敏感：先长后短，避免「短袖T恤」被「T恤」抢先匹配成 1 档。
_SUBCATEGORY_KEYWORDS = [
    "羽绒服", "棉服", "皮草", "雪地靴", "毛呢大衣", "毛呢外套", "呢子大衣",
    "大衣", "厚毛衣", "毛衣", "针织开衫", "针织衫", "卫衣", "冲锋衣",
    "西装外套", "风衣", "夹克", "皮衣", "西装",
    "长袖衬衫", "衬衫", "打底衫", "长袖T恤", "短袖T恤", "短袖", "T恤",
    "背心", "吊带", "马甲",
    "阔腿裤", "牛仔裤", "西裤", "休闲裤", "长裤", "短裤", "半身裙",
    "连衣裙", "长裙", "短裙",
    "长靴", "短靴", "靴子", "运动鞋", "球鞋", "皮鞋", "乐福鞋", "单鞋",
    "高跟鞋", "凉鞋", "拖鞋",
]

_DEFAULT_SUB = {
    "上装": "上衣",
    "下装": "长裤",
    "外套": "外套",
    "鞋": "运动鞋",
    "连衣裙": "连衣裙",
    "配饰": "配饰",
}

# 暖度 1（最薄）~ 5（最厚），顺序敏感：先长后短
_WARMTH_KEYWORDS = [
    ("羽绒服", 5), ("棉服", 5), ("皮草", 5), ("雪地靴", 5),
    ("毛呢大衣", 5), ("呢子大衣", 5), ("大衣", 5), ("厚毛衣", 4),
    ("毛衣", 4), ("加绒", 4), ("抓绒", 4), ("长靴", 4),
    ("冲锋衣", 3), ("风衣", 3), ("夹克", 3), ("西装外套", 3), ("皮衣", 3),
    ("针织开衫", 3), ("针织衫", 3), ("卫衣", 3), ("西装", 3),
    ("长裤", 3), ("牛仔裤", 3), ("西裤", 3), ("长裙", 3), ("靴子", 3),
    ("短靴", 3), ("马甲", 2), ("长袖衬衫", 2), ("衬衫", 2), ("打底衫", 2),
    ("长袖T恤", 2), ("阔腿裤", 2), ("休闲裤", 2), ("半身裙", 2),
    ("连衣裙", 2), ("运动鞋", 2), ("球鞋", 2), ("皮鞋", 2), ("乐福鞋", 2),
    ("单鞋", 2), ("高跟鞋", 2),
    ("短袖T恤", 1), ("短袖", 1), ("T恤", 1), ("背心", 1), ("吊带", 1),
    ("短裤", 1), ("短裙", 1), ("凉鞋", 1), ("拖鞋", 1),
]

# 品类兜底暖度（关键词都匹配不上时）
_CATEGORY_WARMTH = {
    "上装": 2,
    "下装": 3,
    "外套": 4,
    "鞋": 2,
    "连衣裙": 2,
    "配饰": 3,
}


def infer_sub_category(name: str | None, category: str | None, given: str | None = "") -> str:
    """子品类推断：优先用传入值，其次按名称关键词，最后按品类兜底。"""
    if given and str(given).strip():
        return str(given).strip()
    text = str(name or "")
    for kw in _SUBCATEGORY_KEYWORDS:
        if kw in text:
            return kw
    return _DEFAULT_SUB.get(normalize_category(category), "单品")


def infer_warmth(sub_category: str | None, category: str | None) -> int:
    """按子品类推断暖度 1-5。"""
    text = str(sub_category or "")
    for kw, level in _WARMTH_KEYWORDS:
        if kw in text:
            return level
    return _CATEGORY_WARMTH.get(normalize_category(category), 3)


def infer_season(warmth: int) -> str:
    if warmth >= 4:
        return "冬季"
    if warmth <= 1:
        return "夏季"
    if warmth == 2:
        return "春秋"
    return "四季"


def effective_warmth(item: dict) -> int:
    """取得可用于硬过滤的暖度。

    存储值只是占位默认 3 时，改用子品类推断值——否则按暖度的规则
    （F002/F009/F011/F014/F015/F016）对全部单品都会失效。
    """
    inferred = infer_warmth(item.get("sub_category"), item.get("category"))
    stored = item.get("warmth_level")
    try:
        stored_int = int(stored)
    except (TypeError, ValueError):
        return inferred
    if stored_int <= 0 or stored_int == 3:
        return inferred
    return stored_int


def effective_season(item: dict) -> str:
    """取得季节；存储为「四季」或空时按暖度推断。"""
    raw = normalize_season(item.get("season"))
    if raw and raw not in ("四季", "", "None"):
        return raw
    return infer_season(effective_warmth(item))


# ------------------------------------------------------------ 重复度 Jaccard

def attribute_set(item: dict) -> set[str]:
    """把单品转成属性集合（K-ALG-05 的「属性集合」）。"""
    attrs: set[str] = set()

    def add(prefix: str, value) -> None:
        if value is None:
            return
        text = str(value).strip()
        if text:
            attrs.add(f"{prefix}:{text}")

    add("category", normalize_category(item.get("category")))
    add("sub_category", item.get("sub_category"))
    add("color", item.get("color"))
    add("fabric", item.get("fabric"))
    add("style", item.get("style"))
    add("fit_silhouette", item.get("fit_silhouette"))
    add("fit_snug", item.get("fit_snug"))
    attrs.add(f"warmth:{effective_warmth(item)}")
    return attrs


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def duplicate_analysis(target: dict, closet: list[dict]) -> dict:
    """按 K-ALG-05 计算重复度，并点名最相似的单品。

    只与「同类单品」比较（规范原文：衣橱中同类单品属性集合）。
    """
    target_cat = normalize_category(target.get("category"))
    target_attrs = attribute_set(target)

    best_score = 0.0
    best_item: dict | None = None
    best_shared: list[str] = []
    same_category_count = 0

    for item in closet:
        if item.get("item_id") == target.get("item_id"):
            continue
        if normalize_category(item.get("category")) != target_cat:
            continue
        same_category_count += 1
        item_attrs = attribute_set(item)
        score = jaccard(target_attrs, item_attrs)
        if score > best_score:
            best_score = score
            best_item = item
            best_shared = sorted(target_attrs & item_attrs)

    if best_score >= 0.7:
        level = "高度重复"
    elif best_score >= 0.5:
        level = "部分重复"
    else:
        level = "可新增"

    return {
        "score": round(best_score, 2),
        "level": level,
        "same_category_count": same_category_count,
        "nearest_item": (
            {
                "item_id": best_item.get("item_id"),
                "name": best_item.get("name"),
                "category": best_item.get("category"),
                "sub_category": best_item.get("sub_category"),
                "color": best_item.get("color"),
            }
            if best_item
            else None
        ),
        "shared_attributes": [
            token.replace(":", "=", 1) for token in best_shared if not token.startswith("category:")
        ],
    }
