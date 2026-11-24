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

# 历史别名表：已被 normalize_season 的 meta 权威枚举 + 关键词兜底取代
# （2026-11-23）。保留空表以兼容可能的旧引用；口径见 normalize_season。
SEASON_ALIASES: dict[str, str] = {}


def normalize_category(value: str | None) -> str:
    if not value:
        return "上装"
    return CATEGORY_ALIASES.get(str(value).strip(), str(value).strip())


def normalize_season(value: str | None) -> str:
    """季节归一到 rules/clothing_meta.json 的 season 枚举（单一权威）。

    2026-11-23 口径事故修正：旧别名表把「冬」映射成「冬季」，与
    DB/前端/引擎的「冬」口径分裂；LLM 识别输出的「冬季」原样穿透入库，
    造成评估 target season=「冬季」而衣橱=「冬」。现以 meta.season
    为规范集，别名之外按关键词规则兜底（冬季→冬、秋冬季→冬、
    春夏→夏），任何入口的自由文本都会收敛到规范枚举。
    """
    text = str(value or "").strip()
    if not text:
        return "四季"
    seasons = _meta_seasons()
    if text in seasons:
        return text
    alias = (_meta_value("season_alias", {}) or {}).get(text)
    if alias and alias in seasons:
        return alias
    # 关键词兜底：先匹配双季节（秋冬季→冬），再单季节
    if "冬" in text:
        return "冬"
    if "夏" in text:
        return "夏"
    if "春" in text and "秋" in text:
        return "春秋"
    if "春" in text:
        return "春"
    if "秋" in text:
        return "秋"
    return "四季"


def infer_season(warmth: int) -> str:
    """按暖度推断季节，直接返回规范枚举值（冬/夏/春秋/四季）。

    2026-11-23 口径修正：旧实现返回「冬季/夏季」，与 meta.season
    枚举（冬/夏）不一致，是评估链路「冬季」脏值的来源之一。
    """
    if warmth >= 4:
        return "冬"
    if warmth <= 1:
        return "夏"
    if warmth == 2:
        return "春秋"
    return "四季"


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


def effective_warmth_named(item: dict) -> int:
    """取得可用于「26℃穿衣法」的暖度。

    与 effective_warmth 的区别：当数据库里的 warmth_level 仍是占位默认 3
    （前端没有暖度录入入口，早期导入的衣橱几乎全是 3）时，优先按**衣物名字**
    推断。名字比子品类可靠——实测衣橱里「黑色羽绒服」「羊毛大衣」的
    sub_category 都被误标成「风衣」，只看子品类会把它们当薄外套。

    存储值非默认值（1/2/4/5）时视为可信，直接采用；这样
    「黑色针织连衣裙(warmth=4)」这类不会被关键词「连衣裙(2)」误伤。
    """
    stored = item.get("warmth_level")
    try:
        stored_int = int(stored)
    except (TypeError, ValueError):
        stored_int = None
    if stored_int is not None and stored_int > 0 and stored_int != 3:
        return stored_int

    name = str(item.get("name") or "")
    if name:
        for kw, level in _WARMTH_KEYWORDS:
            if kw in name:
                return level
    return effective_warmth(item)


def effective_season(item: dict) -> str:
    """取得季节；存储为「四季」或空时按暖度推断。"""
    raw = normalize_season(item.get("season"))
    if raw and raw not in ("四季", "", "None"):
        return raw
    return infer_season(effective_warmth(item))


# ------------------------------------------------------------ 重复度 Jaccard

# 子品类别名表缓存（rules/clothing_meta.json 的 sub_category_alias，
# 如 短袖→T恤、阔腿裤→长裤）。拟购单品与衣橱单品的录入路径不同，
# 不归一时同一件衣物会出现「短袖」vs「T恤」的假差异。
_SUB_ALIAS_CACHE: dict[str, str] | None = None


def _meta_aliases() -> dict[str, str]:
    global _SUB_ALIAS_CACHE
    if _SUB_ALIAS_CACHE is None:
        try:
            import json

            from app.config import RULES_DIR
            with open(RULES_DIR / "clothing_meta.json", encoding="utf-8") as f:
                _SUB_ALIAS_CACHE = dict(json.load(f).get("sub_category_alias") or {})
        except Exception:
            _SUB_ALIAS_CACHE = {}
    return _SUB_ALIAS_CACHE


def _norm_sub(value) -> str:
    """子品类别名归一：短袖/T恤、阔腿裤/长裤 视为同一属性值。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return _meta_aliases().get(text, text)


# ------------------------------------------------------------ 口径统一层
# 2026-11-23 口径事故复盘：购前评估实测「黑色羊毛大衣」与衣橱里的
# 「黑色长款大衣」仅判 0.38「可新增」。根因之一是系统内并存三套词汇：
# 前端/DB 用「冬、亮色/暗色」，infer_season 返回「冬季」，识别 LLM
# 输出「冬季/深色/羊毛」原样穿透。以下把 season / fabric / brightness
# 全部收敛到 rules/clothing_meta.json 的规范枚举，任何入口共用。
_META_KEY_CACHE: dict[str, object] = {}


def _meta_value(key: str, default):
    """读 clothing_meta.json 顶层键（带缓存），供口径归一层使用。"""
    if key not in _META_KEY_CACHE:
        try:
            import json

            from app.config import RULES_DIR
            with open(RULES_DIR / "clothing_meta.json", encoding="utf-8") as f:
                _META_KEY_CACHE[key] = json.load(f).get(key) or default
        except Exception:
            _META_KEY_CACHE[key] = default
    return _META_KEY_CACHE[key]


def _meta_seasons() -> list[str]:
    seasons = _meta_value("season", [])
    return [str(s) for s in seasons] or ["春秋", "春", "夏", "秋", "冬", "四季"]


def normalize_fabric(value) -> str:
    """面料归一到 meta.fabric 枚举：羊毛→毛、涤纶→化纤、纯棉→棉。

    LLM 识别 prompt 的面料枚举与后端枚举历史上是两套（识别输出
    「羊毛/涤纶」，系统枚举是「毛/化纤」），不归一则判重时面料维度
    永远失配、形成一票否决。未收录的原样保留（不臆造映射）。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    fabric_enum = [str(x) for x in _meta_value("fabric", [])]
    if text in fabric_enum:
        return text
    alias = (_meta_value("fabric_alias", {}) or {}).get(text)
    if alias and alias in fabric_enum:
        return str(alias)
    return text


def fabric_similarity(a, b) -> float | None:
    """面料相似度：同=1；混纺与任何面料=0.5（模糊值）；同家族=0.5。

    返回 None 表示任一侧缺失，该维度不计入判重（不进分母）。
    """
    na, nb = normalize_fabric(a), normalize_fabric(b)
    if not na or not nb:
        return None
    if na == nb:
        return 1.0
    # 「混纺」按定义就是不明确成分，与任何具体面料算半匹配
    if na == "混纺" or nb == "混纺":
        return 0.5
    for family in (_meta_value("fabric_family", []) or []):
        if na in family and nb in family:
            return 0.5
    return 0.0


def season_similarity(a, b) -> float | None:
    """季节相似度：同=1；四季与任何季节=0.5；春秋覆盖春/秋=0.5。

    返回 None 表示任一侧缺失，该维度不计入判重。
    """
    na, nb = normalize_season(a), normalize_season(b)
    if not na or not nb:
        return None
    if na == nb:
        return 1.0
    if na == "四季" or nb == "四季":
        return 0.5
    if (na == "春秋" and nb in ("春", "秋")) or (nb == "春秋" and na in ("春", "秋")):
        return 0.5
    return 0.0


def canonicalize_item(item: dict, *, strict: bool = True) -> dict:
    """把任意入口的单品字典口径归一到系统规范枚举（原 dict 原地更新）。

    统一处理（2026-11-23 口径事故后建立，规则以 clothing_meta.json 为
    单一权威）：
    - season  -> meta.season 枚举（冬季→冬、秋冬季→冬、春夏→夏）
    - fabric  -> meta.fabric 枚举（羊毛→毛、涤纶→化纤、纯棉→棉）
    - color_brightness -> {亮色, 暗色}（历史脏值：深色/浅色）
    - color   -> 色板主色别名归一（藏青→藏蓝，见 normalize_color）
    - sub_category -> meta.sub_category_alias（短袖→T恤）

    strict=True 时把被纠正的字段名记入 item["_canonicalized"]，供调用方
    标注来源；映射不到规范枚举的值**置空**而不是保留脏值——脏值留着
    比缺失更危险（判重失配 + 引擎枚举不命中）。
    """
    corrected: list[str] = []

    seasons = _meta_seasons()
    season = str(item.get("season") or "").strip()
    if season and season not in seasons:
        fixed = normalize_season(season)
        if fixed != season:
            item["season"] = fixed
            corrected.append("season")

    fabric = str(item.get("fabric") or "").strip()
    if fabric:
        fixed = normalize_fabric(fabric)
        if fixed != fabric:
            item["fabric"] = fixed
            corrected.append("fabric")

    brightness = str(item.get("color_brightness") or "").strip()
    if brightness and brightness not in ("亮色", "暗色"):
        item["color_brightness"] = "暗色" if "深" in brightness or "暗" in brightness else "亮色"
        corrected.append("color_brightness")

    color = str(item.get("color") or "").strip()
    if color:
        fixed = normalize_color(color)
        if fixed != color:
            item["color"] = fixed
            corrected.append("color")

    sub = str(item.get("sub_category") or "").strip()
    if sub:
        fixed = _norm_sub(sub)
        if fixed != sub:
            item["sub_category"] = fixed
            corrected.append("sub_category")

    if strict and corrected:
        item["_canonicalized"] = corrected
    return item


# ------------------------------------------------------------ 颜色别名归一
# 前端色板 23 色（COLOR_MAP）与衣橱录入值常有同色异名：藏青/藏蓝、
# 墨绿/绿、奶茶/米、牛仔蓝/蓝。重复度按 key:value 精确比对，
# 不归一时「藏青 blazer」与衣橱「藏蓝」永远不会算作同一颜色。
_COLOR_ALIASES = {
    "藏青": "藏蓝", "海军蓝": "藏蓝", "深蓝": "藏蓝", "牛仔蓝": "蓝",
    "墨绿": "绿", "军绿": "绿", "浅绿": "绿", "卡其色": "卡其",
    "奶茶": "米", "米白": "米", "米色": "米", "杏色": "米", "奶白": "米",
    "黑灰": "灰", "浅灰": "灰", "银": "灰", "银灰": "灰",
    "酒红": "红", "玫红": "粉", "浅粉": "粉", "橙": "棕",
    "浅蓝": "蓝", "天蓝": "蓝", "白色": "白", "黑色": "黑", "灰色": "灰",
}


def normalize_color(value) -> str:
    """颜色别名归一到前端色板主色；未收录的原样保留（不臆造映射）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return _COLOR_ALIASES.get(text, text)


# ------------------------------------------------------------ 子品类归属
# 引擎内部品类口径（上装/连衣裙）→ clothing_meta.json 展示口径（上衣/连衣装）
_ENGINE_TO_META_CATEGORY = {
    "上装": "上衣", "下装": "下装", "连衣裙": "连衣装",
    "套装": "套装", "外套": "外套", "鞋": "鞋", "配饰": "配饰",
}
_META_CATEGORY_CACHE: dict[str, list[str]] | None = None


def _meta_categories() -> dict[str, list[str]]:
    global _META_CATEGORY_CACHE
    if _META_CATEGORY_CACHE is None:
        try:
            import json

            from app.config import RULES_DIR
            with open(RULES_DIR / "clothing_meta.json", encoding="utf-8") as f:
                raw = json.load(f).get("category") or {}
            _META_CATEGORY_CACHE = {
                str(k): [str(s) for s in v] for k, v in raw.items() if isinstance(v, list)
            }
        except Exception:
            _META_CATEGORY_CACHE = {}
    return _META_CATEGORY_CACHE


def valid_sub_category(sub: str, engine_category: str) -> bool:
    """判断二级品类是否属于该一级品类；无法判定时返回 True（不误拦）。"""
    text = str(sub or "").strip()
    if not text:
        return True
    meta_cat = _ENGINE_TO_META_CATEGORY.get(normalize_category(engine_category), "")
    allowed = _meta_categories().get(meta_cat)
    if not allowed:
        return True
    return _norm_sub(text) in allowed


def warmth_bucket(level) -> str:
    """暖度 1-5 分三档：薄(≤2) / 中(3-4) / 厚(≥5)。

    重复度判定用分档而非原值：暖度经常一边是推断、一边是录入，
    同一件衣物出现 1 档之差属于噪声（如 T 恤推断 1、录入 2），
    不构成「关键属性差异」。
    """
    try:
        lv = int(level)
    except (TypeError, ValueError):
        lv = 3
    if lv <= 2:
        return "薄"
    if lv <= 4:
        return "中"
    return "厚"


def attribute_set(item: dict, dims: set[str] | None = None) -> set[str]:
    """把单品转成属性集合（K-ALG-05 的「属性集合」）。

    - 子品类走 clothing_meta 别名归一；
    - 暖度按 薄/中/厚 分档（见 warmth_bucket）；
    - ``dims`` 非空时只保留指定维度，用于重复度的「已知维度投影」比较。
    """
    attrs: set[str] = set()

    def add(prefix: str, value) -> None:
        if value is None:
            return
        text = str(value).strip()
        if text:
            attrs.add(f"{prefix}:{text}")

    add("category", normalize_category(item.get("category")))
    add("sub_category", _norm_sub(item.get("sub_category")))
    add("color", normalize_color(item.get("color")))
    add("fabric", item.get("fabric"))
    add("style", item.get("style"))
    add("fit_silhouette", item.get("fit_silhouette"))
    add("fit_snug", item.get("fit_snug"))
    attrs.add(f"warmth:{warmth_bucket(effective_warmth(item))}")
    if dims is not None:
        attrs = {a for a in attrs if a.split(":", 1)[0] in dims}
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

    2026-11-19 修正：按拟购单品的已知维度投影比较——用户没填的维度
    既不能证明相似、也不能证明差异，不计入分母（框架沿用）。

    2026-11-23 分层加权重构（黑色羊毛大衣事故复盘）：
    旧实现的裸 Jaccard 平权有三个结构性缺陷，实测让「与衣橱完全相同
    的大衣」仅判 0.38 可新增：
    1. 平权 → 「子品类+颜色」这种身份维度与「风格」同权重，
       面料一票否决直接压垮真同款；
    2. 空属性占便宜 → 衣橱单品属性越残缺、并集越小、分数越高，
       属性全空的羽绒服(0.38)挤掉了属性全但面料微差的真同款(0.375)；
    3. 推断值硬碰录入值 → 拟购暖度按子品类推断 5，用户当年录的是 4，
       一档之差整维判负。

    新算法 = 分层加权 Jaccard + 推断容差 + 身份规则：
    - 加权：sub_category 0.30 / color 0.25 / warmth 0.15 / fabric 0.15
      / season 0.10 / style 0.10，按双方已知维度归一化；
    - 推断容差：拟购暖度是推断值时，与录入值相差一档（薄/中/厚）
      按半匹配计；面料「混纺」与任何具体面料半匹配，同家族半匹配；
    - 身份底线：用户确认的 子品类+颜色 全同 → 保底 0.5（部分重复）；
    - 子品类封顶：双方子品类都有值且不同 → 上限 0.65——不同款型的
      外套再相似也只能到「部分重复」，防止空属性单品冒充高度重复。
    """
    target = canonicalize_item(dict(target), strict=False)
    target_cat = normalize_category(target.get("category"))
    t_sub = _norm_sub(target.get("sub_category"))
    t_color = normalize_color(target.get("color"))
    t_sub_source = (target.get("_attr_source") or {}).get("sub_category", "user")

    best_score = 0.0
    best_item: dict | None = None
    best_shared: list[str] = []
    best_dims: set[str] = set()
    same_category_count = 0

    for item in closet:
        if item.get("item_id") == target.get("item_id"):
            continue
        if normalize_category(item.get("category")) != target_cat:
            continue
        same_category_count += 1
        item = canonicalize_item(dict(item), strict=False)
        i_sub = _norm_sub(item.get("sub_category"))

        num = den = 0.0
        shared: list[str] = []
        dims: set[str] = set()
        for dim, weight in _DUP_WEIGHTS.items():
            sim, text = _dim_similarity(target, item, dim)
            if sim is None:
                continue  # 任一侧缺失：不计入分母
            num += weight * sim
            den += weight
            dims.add(dim)
            if text:
                shared.append(text)

        score = (num / den) if den > 0 else 0.0

        # 身份底线：用户确认的子品类 + 颜色全同，无论如何不低于部分重复
        if (t_sub_source == "user" and t_sub and t_color
                and i_sub == t_sub
                and normalize_color(item.get("color")) == t_color):
            score = max(score, 0.5)

        # 子品类封顶：不同款型最多「部分重复」，不允许冒充高度重复
        if t_sub and i_sub and i_sub != t_sub:
            score = min(score, 0.65)

        if score > best_score:
            best_score = score
            best_item = item
            best_shared = sorted(shared)
            best_dims = dims

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
        "comparable_dims": sorted(best_dims),
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
        "shared_attributes": best_shared,
    }


# ------------------------------------------------------- 判重维度权重与相似度
# 分层权重：身份维度（子品类/颜色）显著高于修饰维度（面料/风格）。
# 权重和不必为 1——分数按「双方已知维度」归一化，缺失维度不进分母。
_DUP_WEIGHTS = {
    "sub_category": 0.30,
    "color": 0.25,
    "warmth": 0.15,
    "fabric": 0.15,
    "season": 0.10,
    "style": 0.10,
}

_WARMTH_ORDER = {"薄": 0, "中": 1, "厚": 2}


def _dim_similarity(target: dict, item: dict, dim: str) -> tuple[float | None, str | None]:
    """单维相似度。返回 (相似度, 展示文本)；相似度 None = 维度缺失不计。

    展示文本仅在该维有匹配贡献时给出，直接进入 shared_attributes。
    """
    if dim == "sub_category":
        a, b = _norm_sub(target.get("sub_category")), _norm_sub(item.get("sub_category"))
        if not a or not b:
            return None, None
        return (1.0, f"sub_category={a}") if a == b else (0.0, None)

    if dim == "color":
        a, b = normalize_color(target.get("color")), normalize_color(item.get("color"))
        if not a or not b:
            return None, None
        return (1.0, f"color={a}") if a == b else (0.0, None)

    if dim == "warmth":
        t_bucket = warmth_bucket(effective_warmth(target))
        i_bucket = warmth_bucket(effective_warmth(item))
        if t_bucket == i_bucket:
            return 1.0, f"warmth={t_bucket}"
        # 推断容差：拟购暖度是系统推断（用户未填）时，一档之差按半匹配
        t_source = (target.get("_attr_source") or {}).get("warmth_level", "")
        if (t_source == "inferred"
                and abs(_WARMTH_ORDER[t_bucket] - _WARMTH_ORDER[i_bucket]) == 1):
            return 0.5, f"warmth≈{t_bucket}/{i_bucket}"
        return 0.0, None

    if dim == "fabric":
        sim = fabric_similarity(target.get("fabric"), item.get("fabric"))
        if sim is None:
            return None, None
        if sim == 1.0:
            return 1.0, f"fabric={normalize_fabric(target.get('fabric'))}"
        if sim > 0:
            return sim, (f"fabric≈{normalize_fabric(target.get('fabric'))}"
                         f"/{normalize_fabric(item.get('fabric'))}")
        return 0.0, None

    if dim == "season":
        sim = season_similarity(target.get("season"), item.get("season"))
        if sim is None:
            return None, None
        if sim == 1.0:
            return 1.0, f"season={normalize_season(target.get('season'))}"
        if sim > 0:
            return sim, (f"season≈{normalize_season(target.get('season'))}"
                         f"/{normalize_season(item.get('season'))}")
        return 0.0, None

    if dim == "style":
        a = str(target.get("style") or "").strip()
        b = str(item.get("style") or "").strip()
        if not a or not b:
            return None, None
        return (1.0, f"style={a}") if a == b else (0.0, None)

    return None, None
