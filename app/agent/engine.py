# -*- coding: utf-8 -*-
"""Agent 推理层 —— 规则库加载 + 推荐主流程。

三层结构（计划书 7.6）：
    [1] 硬过滤 filter_rules.json   -> 合法搭配集合
    [2] 排除已展示 + 打分 score_rules.json
    [3] 差异化取 Top3 + 理由生成（模板 + 规则来源）
"""
import json
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Any

from app import config
from app.agent.garment_profile import (
    effective_warmth_named,
    fabric_similarity,
    jaccard,
    normalize_category,
    normalize_color,
    normalize_fabric,
)

_CAT_ORDER = ("上装", "下装", "外套", "连衣裙", "鞋", "配饰", "套装")

# ---------------------------------------------------------- 26℃ 穿衣法参数
# 民间经验公式：体感目标 26℃。衣物折算成「度」，穿衣保暖总额补到
# 26 - 气温。只累计「上装/外套/连衣裙」+ 名字识别出的围巾；下装与鞋不计。
COMFORT_TEMP = 26.0
# 26℃穿衣法低温收敛参数（见 RecommendEngine._warmth_need 的实测说明）：
# >= 11℃ 用线性 26-temp；< 11℃ 从 15.0 起按 0.2/℃ 缓增，避免极寒区
# 出现「需求远超单套可达折算上限」导致 F010 把全部搭配误杀。
LOW_TEMP_LINEAR_BOUNDARY = 11.0
LOW_TEMP_NEED_BASE = 15.0
LOW_TEMP_SLOPE = 0.2
# 暖度档位(1-5) -> 折算度数：薄T恤1 / 衬衫2 / 针织衫4 / 毛衣6 / 羽绒服9
WARMTH_DEGREES = {1: 1.0, 2: 2.0, 3: 4.0, 4: 6.0, 5: 9.0}
# 围巾按名字/子品类识别，折算 3℃（关键保暖配饰）
_SCARF_KEYWORDS = ("围巾", "丝巾", "脖套", "围脖")
_SCARF_DEGREES = 3.0
# 洋葱/三明治分层关键词（按「外层→中层→内层」顺序匹配，先长后短）
_OUTER_KEYWORDS = ("羽绒服", "棉服", "冲锋衣", "毛呢大衣", "呢子大衣", "大衣",
                   "风衣", "夹克", "皮衣", "西装外套")
_MIDDLE_KEYWORDS = ("厚毛衣", "毛衣", "抓绒", "针织开衫", "针织衫", "卫衣",
                    "马甲", "加绒")
_INNER_KEYWORDS = ("长袖衬衫", "打底衫", "长袖T恤", "短袖T恤", "衬衫", "T恤",
                   "Polo衫", "背心", "吊带")

# ---------- 面料冲突 / 风格一致性 / 正式度（方案1） ----------
# 精致轻薄面料 + 功能性硬壳外套 = 材质语言打架（雪纺衬衫配冲锋衣）。
# 只看「上装/连衣裙」内层，围巾等配饰不算冲突。
_DELICATE_FABRICS = ("真丝", "桑蚕丝", "雪纺", "蕾丝", "缎", "丝", "醋酸")
_OUTDOOR_SHELL_KEYWORDS = ("冲锋衣", "防风衣", "滑雪服", "硬壳", "机能")

# 场合语境冲突：正式单品（FL≥4 的核心品类）与强休闲单品不能同框。
# 西裤配拖鞋/卫衣在任何场合都不成立——这是 S016 打分拦不住的硬冲突。
# T恤/运动鞋不在此列：T恤配西裤、运动鞋配西裤属于可接受的 smart casual。
_STRONG_CASUAL_SUBS = ("拖鞋", "凉鞋", "洞洞鞋", "卫衣", "运动裤", "短裤", "背心", "吊带", "家居服")
_FORMAL_ANCHOR_CATS = ("上装", "下装", "外套", "连衣裙", "套装", "鞋")

# 风格轴：clean 为百搭中性轴，不与任何轴冲突；其余按下表判互斥。
_STYLE_AXIS = {
    "基础款": "clean", "简约": "clean", "中性": "clean",
    "通勤": "formal", "休闲": "casual", "运动": "sport",
    "甜美": "soft", "设计款": "statement", "复古": "retro",
}
_STYLE_CONFLICTS = {
    frozenset(("formal", "sport")), frozenset(("formal", "soft")),
    frozenset(("sport", "soft")), frozenset(("sport", "retro")),
    frozenset(("sport", "statement")),
}

# 场合 → 目标正式度（1-5）。用于替代只看子品类黑名单的粗糙判断。
_FORMALITY_TARGET = {
    "面试": 5, "正式": 5, "通勤": 4, "出差": 4,
    "约会": 3, "聚会": 3,
    "日常": 2, "上课": 2, "出游": 2, "旅游": 2,
    "居家": 1, "运动": 1,
}

# ------------------------------------------------------- Top3 多样性（MMR）
# 风格/颜色/版型是主要差异来源，单品 ID 只作补充——只换一双鞋不该算
# 「明显不同的搭配」。缺失维度不进分母，按剩余权重归一化（沿用判重口径）。
_DIVERSITY_WEIGHTS = {
    "style": 0.30, "color": 0.25, "silhouette": 0.20,
    "formality": 0.15, "item": 0.10,
}
# 核心骨架（上装+下装/连衣裙）相同的两套，差异度直接记 0。
# 实测 76 件衣橱的 101811 个合法组合只落在 239 个骨架，单骨架最多派生
# 690 套。最初取 0.35 封顶，但 style/版型字段缺失时分母会缩到只剩正式度
# 与单品项，换鞋的 D≈0.2 根本触不到封顶，λ=0.7 下 10 分质量差就能压过
# 差异项——Top3 仍退化成同骨架换鞋。语义上「同一套上装下装换鞋」也就是
# 同一套搭配，差异记 0 更准。
_DIVERSITY_SAME_CORE_DIFF = 0.0
_MMR_LAMBDA = 0.7        # 质量权重；越大越重视分数，越小越重视差异
_MMR_POOL_LIMIT = 300    # 进入 MMR 的候选上限
_MMR_PER_CORE = 3        # 每个核心骨架最多入池几套，防止单一骨架霸榜

# ------------------------------------------------------- 视觉美感（只罚不奖）
# 2026-11-24 Vivian 拍板：V001/V002/V003 的分数只写进
# outfit['visual_scores'] 明细，排序时**只取负分做扣减、正分一律不奖**：
#   rank_score = score + min(0, 三个视觉子项 delta 之和)
# 理由：实测视觉分与主分相关系数仅 -0.12/-0.07/-0.036（正交、非重复计分），
# 保暖 S008 在所有权重下恒定满值 9.0；但对称并入（正分也奖）会把
# 「黑大衣+西装马甲+牛仔背带裤」这类视觉分高、单品语义荒谬的搭配抬进 Top3。
# 故只做减法：把视觉不协调的搭配压下去，不把视觉整齐的怪搭配抬上来。
# 主 score 字段保持原值（前端展示、理由文案照旧），只有排序键变。
# 三条子项的边界（拍板口径）：
#   V001 配色比例：用 color + 品类角色推面积（上装/下装/连衣裙/套装=大面积，
#     鞋/配饰=点缀），评「主色/辅助色/点缀是否成比例」，不评色相关系
#     （S004-S006 已评）、不评颜色数量（F005 已硬拦 >3）。面积是近似推算，
#     衣橱没有真实占比字段。
#   V002 版型协调：评上下松紧组合，但**跳过 S007 已认领的「上宽下窄」**那档，
#     只补它没覆盖的方向（上紧下松、上下同松、上下同紧）。
#   V003 材质协调：fabric 归一后用 fabric_family 判整套质感是否统一，
#     只处理「没到 F014 硬拦、但质感不搭」的软层；不评季节（S009 已评）。
_VISUAL_LARGE_CATS = ("上装", "下装", "连衣裙", "套装", "外套")
_VISUAL_ACCENT_CATS = ("鞋", "配饰")
# 中性色：与色轮里的黑白灰米卡其一致，作为主色的「安全底」不算花
_VISUAL_NEUTRALS = {"黑", "白", "灰", "米", "卡其"}


def _is_delicate(item: dict) -> bool:
    """是否是精致轻薄面料。有 fabric 字段时只信字段，避免「丝光棉」这类名字误杀。"""
    fabric = (item.get("fabric") or "").strip()
    if fabric:
        return any(kw in fabric for kw in _DELICATE_FABRICS)
    return any(kw in (item.get("name") or "") for kw in _DELICATE_FABRICS)


def _is_outdoor_shell(item: dict) -> bool:
    """是否是户外功能性硬壳外套。"""
    text = f"{item.get('name') or ''} {item.get('sub_category') or ''} {item.get('fabric') or ''}"
    return any(kw in text for kw in _OUTDOOR_SHELL_KEYWORDS)


def _formal_int(value: Any) -> int:
    """正式度健壮解析：缺失/异常回落到中性 3，不能当成最低档误杀。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 3


def _is_scarf(item: dict) -> bool:
    """按名字或子品类识别围巾（实测有围巾被误标成「包包」）。"""
    text = f"{item.get('name') or ''} {item.get('sub_category') or ''}"
    return any(kw in text for kw in _SCARF_KEYWORDS)


def _counts_for_warmth(item: dict) -> bool:
    """是否计入 26℃ 保暖总额：上装/外套/连衣裙 + 围巾。"""
    return normalize_category(item.get("category")) in ("上装", "外套", "连衣裙") or _is_scarf(item)


def _item_degrees(item: dict) -> float:
    """单品折算度数。围巾固定 3℃；其余按暖度档位映射。"""
    if _is_scarf(item):
        return _SCARF_DEGREES
    warmth = effective_warmth_named(item)
    return WARMTH_DEGREES.get(int(warmth), 4.0)


def _layer_of(item: dict) -> str | None:
    """洋葱/三明治分层：outer 防风 / middle 保暖 / inner 贴身透气。"""
    text = f"{item.get('name') or ''} {item.get('sub_category') or ''}"
    if any(kw in text for kw in _OUTER_KEYWORDS):
        return "outer"
    if any(kw in text for kw in _MIDDLE_KEYWORDS):
        return "middle"
    if any(kw in text for kw in _INNER_KEYWORDS):
        return "inner"
    return None


def _cat_rank(value: Any) -> int:
    """搭配展示排序：上装→下装→外套→连衣裙→鞋→配饰。"""
    c = normalize_category(value)
    return _CAT_ORDER.index(c) if c in _CAT_ORDER else 99


def _pref_int(value: Any) -> int:
    """健壮地把长期偏好分解析成整数，异常/缺失一律回中性 5。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 5
    return v


class RuleLoadError(Exception):
    """规则库 JSON 缺失或格式不对时抛出。"""


@dataclass
class RuleRepository:
    """规则库仓库：启动时一次性加载 4 份 JSON，进程内复用。"""

    rules_dir: Path = config.RULES_DIR

    filter_rules: list[dict[str, Any]] = field(default_factory=list)
    score_rules: list[dict[str, Any]] = field(default_factory=list)
    body_rules: dict[str, Any] = field(default_factory=dict)
    clothing_meta: dict[str, Any] = field(default_factory=dict)

    # 颜色关系表：用于色彩协调打分
    color_neighbors: dict[str, list[str]] = field(default_factory=dict)
    color_contrasts: dict[str, list[str]] = field(default_factory=dict)

    def load(self) -> "RuleRepository":
        self.filter_rules = self._load_file("filter_rules.json", need_key="rules")["rules"]
        self.score_rules = self._load_file("score_rules.json", need_key="rules")["rules"]
        body = self._load_file("body_rules.json", need_key="body_shapes")
        self.body_rules = body["body_shapes"]
        self.clothing_meta = self._load_file("clothing_meta.json")
        self._init_color_relations()
        return self

    def _load_file(self, name: str, need_key: str | None = None) -> dict[str, Any]:
        path = self.rules_dir / name
        if not path.exists():
            raise RuleLoadError(f"规则库文件缺失: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if need_key and need_key not in data:
            raise RuleLoadError(f"{name} 缺少字段: {need_key}")
        return data

    def _init_color_relations(self):
        """简单色轮：邻近色与对比色。"""
        families = self.clothing_meta.get("color_family", [])
        ring = [c for c in families if c not in ("黑", "白", "灰", "米", "卡其")]
        n = len(ring)
        for i, c in enumerate(ring):
            self.color_neighbors[c] = [ring[(i - 1) % n], ring[(i + 1) % n]]
            self.color_contrasts[c] = [ring[(i + n // 2) % n]]
        for neutral in ("黑", "白", "灰", "米", "卡其"):
            self.color_neighbors[neutral] = []
            self.color_contrasts[neutral] = []

    def rule_sources(self) -> list[dict[str, str]]:
        rows = []
        for r in self.filter_rules + self.score_rules:
            rows.append({"rule_id": r["rule_id"], "name": r["name"], "source": r["source"]})
        return rows


@dataclass
class RecommendContext:
    """一次推荐的输入。"""

    user_id: str
    occasion: str | None = None
    # None 表示没有拿到实时天气：此时不做天气硬过滤，也不给天气分，
    # 避免用一个编造的 22℃ 去误导穿搭。
    weather_temp: float | None = None
    weather_rain: str | None = None
    # 降雨概率（0-100）；None 表示未知。比「rain」文案更可靠
    weather_rain_prob: float | None = None
    temp_pref: dict | None = None
    constraints: dict | None = None
    shown_outfit_ids: list | None = None
    body_shape: str | None = None
    long_term_prefs: dict | None = None
    # 动态画像有效权重表 {"color": {"蓝": 2.4, ...}, ...}（utils.effective_profile）。
    # 有值时 S003 走真·逐值匹配；缺省时回落到旧五维标量口径，兼容老调用方。
    user_profile: dict | None = None
    # 被用户否定过的搭配核心签名（上装+下装/连衣裙的 item_id 组合）。
    # 「这些都不喜欢」时记录，之后整组排除，换鞋/配饰也一样不再推。
    rejected_cores: list | None = None


# 搭配「核心」= 上装+下装（或连衣裙）的 item_id 组合，用于负反馈整组排除。
# chat.py 直接 import 本函数，保证记录与过滤用同一套算法。
# 注意：只用 normalize_category 归一后的 category，不用 sub_category——
# 历史 response 里的 item 只有 item_id/category/name/image_url，无 sub_category，
# 两端字段不一致会导致签名对不上、排除失效（2026-11-17 实测踩坑）。
_CORE_CATS = {"上装", "下装", "连衣裙"}


def outfit_core_signature(outfit: dict) -> str:
    """返回搭配核心签名（排序后的 item_id 拼接）；无核心返回空串。"""
    ids = []
    for it in outfit.get("items", []):
        if normalize_category(it.get("category")) in _CORE_CATS:
            iid = it.get("item_id")
            if iid:
                ids.append(str(iid))
    return "_".join(sorted(ids))


@dataclass
class RecommendEngine:
    """推荐主流程。"""

    repo: RuleRepository

    def recommend(self, ctx: RecommendContext, items: list[dict]) -> dict[str, Any]:
        """按 7.6 执行顺序产出 Top 搭配 + 理由 + 剩余数量。"""
        ctx.constraints = ctx.constraints or {}
        ctx.shown_outfit_ids = ctx.shown_outfit_ids or []
        ctx.temp_pref = ctx.temp_pref or {}
        ctx.long_term_prefs = ctx.long_term_prefs or {}

        # 1. 枚举所有合法组合
        outfits = self._enumerate(items)

        # 2. 硬过滤
        outfits = [o for o in outfits if self._hard_filter(o, ctx)]

        # 3. 排除已展示（用 items 的 id 集合做指纹）
        shown_fingerprints = set(ctx.shown_outfit_ids)
        outfits = [o for o in outfits if o["fingerprint"] not in shown_fingerprints]

        # 3.5 排除被用户否定过的「核心组合」（上装+下装/连衣裙）。
        # 只排除整套指纹不够：换个鞋/配饰又会出同一套上装+下装，
        # 用户说「这些都不喜欢」后必须整组换掉。
        rejected = set(ctx.rejected_cores or [])
        if rejected:
            outfits = [o for o in outfits if outfit_core_signature(o) not in rejected]

        if not outfits:
            return {"outfits": [], "reason": "没有满足当前约束的搭配，建议放宽条件（如减少排除色、换场合）。"}

        # 4. 打分
        for o in outfits:
            o["score"], o["score_details"] = self._score(o, ctx)
            # 视觉分明细仍带 shadow 标记；排序只用其负向部分（只罚不奖）。
            o["visual_scores"] = self._visual_scores(o)

        # 5. 按「主分 + 视觉负分扣减」降序；主 score 本身不变。
        outfits.sort(key=self._rank_score, reverse=True)

        # 6. 差异化取 Top3
        top3 = self._diversify(outfits, top_k=3)

        # 7. 生成理由
        for o in top3:
            o["reason"] = self._generate_reason(o, ctx)
            o["outfit_id"] = o["fingerprint"]

        return {"outfits": [self._to_response(o) for o in top3]}

    def _enumerate(self, items: list[dict]) -> list[dict]:
        """枚举搭配组合：上装+下装+鞋 / 连衣裙+鞋，可选外套/配饰。"""
        by_cat: dict[str, list[dict]] = {}
        for it in items:
            # 品类归一：前端展示用「上衣/连衣装/套装」，引擎内部按「上装/连衣裙」枚举
            by_cat.setdefault(normalize_category(it.get("category")), []).append(it)

        tops = by_cat.get("上装", [])
        bottoms = by_cat.get("下装", [])
        shoes = by_cat.get("鞋", [])
        dresses = by_cat.get("连衣裙", [])
        outerwears = by_cat.get("外套", [])
        accessories = by_cat.get("配饰", [])
        suits = by_cat.get("套装", [])

        combos = []
        # 方案 A：上装 + 下装 + 鞋
        for t, b, s in product(tops, bottoms, shoes):
            core = [t, b, s]
            combos.append(core)
            for o in outerwears:
                combos.append([o] + core)
            for a in accessories:
                combos.append(core + [a])
            for o in outerwears:
                for a in accessories:
                    combos.append([o] + core + [a])

        # 方案 B：连衣裙 + 鞋（外套与配饰可同时叠加，与方案 A 对齐）
        for d, s in product(dresses, shoes):
            core = [d, s]
            combos.append(core)
            for o in outerwears:
                combos.append([o] + core)
            for a in accessories:
                combos.append(core + [a])
            for o in outerwears:
                for a in accessories:
                    combos.append([o] + core + [a])

        # 方案 C：套装 + 鞋（套装自带上下装，不再叠外套；配饰可选）
        for su, s in product(suits, shoes):
            core = [su, s]
            combos.append(core)
            for a in accessories:
                combos.append(core + [a])

        results = []
        for combo in combos:
            ids = sorted([it["item_id"] for it in combo])
            fp = "_".join(ids)
            results.append({
                "items": combo,
                "fingerprint": fp,
                "categories": {normalize_category(it["category"]) for it in combo},
                "sub_categories": {it["sub_category"] for it in combo},
                "colors": [it.get("color", "") for it in combo if it.get("color")],
            })
        return results

    def _hard_filter(self, outfit: dict, ctx: RecommendContext) -> bool:
        """执行 15 条硬过滤规则（F001-F015）。"""
        items = outfit["items"]
        weather_known = ctx.weather_temp is not None
        temp = ctx.weather_temp if weather_known else 22.0
        occasion = ctx.occasion or ""
        constraints = ctx.constraints or {}

        # F001 低温禁短装
        if weather_known and temp <= 5:
            forbid = {"短裤", "短袖", "吊带", "背心", "凉鞋"}
            if outfit["sub_categories"] & forbid:
                return False

        # F002 高温禁厚装
        if weather_known and temp >= 28:
            forbid = {"羽绒服", "棉服", "大衣", "厚毛衣"}
            if outfit["sub_categories"] & forbid:
                return False

        # F003 正式场合禁休闲
        if occasion in ("面试", "正式"):
            forbid = {"运动裤", "拖鞋", "背心", "短裤", "卫衣"}
            if outfit["sub_categories"] & forbid:
                return False

        # F004 运动场合禁正装
        if occasion == "运动":
            forbid = {"皮鞋", "西装外套", "衬衫"}
            if outfit["sub_categories"] & forbid:
                return False

        # F015 正式场合正式度下限：≤1 是运动/居家档，不该进面试或正式场合。
        # 比 F003 的子品类黑名单更通用——没列进黑名单的低正式度单品也能拦住。
        if occasion in ("面试", "正式") and any(_formal_int(it.get("formality_level")) <= 1 for it in items):
            return False

        # F005 三色原则（配饰不算）
        main_colors = [it.get("color", "") for it in items if it.get("category") != "配饰" and it.get("color")]
        if len(set(main_colors)) > 3:
            return False

        # F006 品类完整性
        cats = outfit["categories"]
        has_top_bottom = {"上装", "下装", "鞋"}.issubset(cats)
        has_dress = {"连衣裙", "鞋"}.issubset(cats)
        has_suit = {"套装", "鞋"}.issubset(cats)
        if not (has_top_bottom or has_dress or has_suit):
            return False

        # F007 同单品不重复
        ids = [it["item_id"] for it in items]
        if len(ids) != len(set(ids)):
            return False

        # F008 用户排除色
        exclude_colors = set(constraints.get("exclude_colors", []))
        if exclude_colors and set(outfit["colors"]) & exclude_colors:
            return False

        # F014 面料冲突：精致轻薄内搭 + 户外硬壳外套，材质语言互斥。
        # 只看上装/连衣裙内层；围巾/包等配饰不参与判定。
        inner = [it for it in items if normalize_category(it.get("category")) in ("上装", "连衣裙")]
        outer = [it for it in items if normalize_category(it.get("category")) == "外套"]
        if any(_is_delicate(it) for it in inner) and any(_is_outdoor_shell(it) for it in outer):
            return False

        # F016 场合语境冲突：正式单品 + 强休闲单品整组淘汰。
        # 西裤(FL4) + 拖鞋/卫衣(FL1) 属于语境互斥；打分扣 3 分仍可能入选，所以必须硬拦。
        has_formal_anchor = any(
            _formal_int(it.get("formality_level")) >= 4
            and normalize_category(it.get("category")) in _FORMAL_ANCHOR_CATS
            for it in items
        )
        has_strong_casual = any(
            (it.get("sub_category") or "") in _STRONG_CASUAL_SUBS for it in items
        )
        if has_formal_anchor and has_strong_casual:
            return False

        # 天气未知时不执行任何天气相关过滤
        if not weather_known:
            return True

        wear = [it for it in items if _counts_for_warmth(it)]
        degrees = sum(_item_degrees(it) for it in wear)
        need = self._warmth_need(temp)

        # F009 保暖总额上限：26℃ 穿衣法要求「气温 + 衣物度数 ≈ 26℃」，
        # 超出 4℃ 属于明显穿多（19.4℃ 穿羽绒服 9℃ 会超出一倍），直接淘汰。
        if degrees > need + 4.0:
            return False
        # F010 保暖总额下限：低于需求 5℃ 属于明显穿少，冬天不该出现。
        if degrees < need - 5.0:
            return False

        # F011 高温清凉夏装：>26℃ 时上身只能用薄款（折算 ≤2℃），
        # 任何针织衫/毛衣/大衣/羽绒服都不合规。
        if temp > COMFORT_TEMP and any(_item_degrees(it) > 2.0 for it in wear):
            return False

        # F012 单品级极端错配：单件折算度数超过总需求 +2 时，
        # 说明这一件本身就是过厚主力（如 19℃ 的羽绒服 9℃ vs 需求 6.6℃）。
        if any(_item_degrees(it) > need + 2.0 for it in wear):
            return False

        # F013 雨天禁凉鞋/拖鞋：湿滑且不合场合
        if self._is_rainy(ctx):
            wet_forbid = {"凉鞋", "拖鞋"}
            if outfit["sub_categories"] & wet_forbid:
                return False

        return True

    def _score(self, outfit: dict, ctx: RecommendContext) -> tuple[float, list[dict]]:
        """打分：基础分 60 + 各规则加权。"""
        items = outfit["items"]
        score = 60.0
        details = []

        # S001 临时偏好-颜色
        temp_color = ctx.temp_pref.get("color") if ctx.temp_pref else None
        if temp_color:
            brightnesses = [it.get("color_brightness", "") for it in items]
            if temp_color in brightnesses:
                score += 2.0
                details.append({"rule_id": "S001", "name": "临时偏好-颜色", "delta": 2.0})

        # S002 临时偏好-款式
        temp_style = ctx.temp_pref.get("style") if ctx.temp_pref else None
        if temp_style:
            styles = [it.get("style", "") for it in items]
            if temp_style in styles:
                score += 2.0
                details.append({"rule_id": "S002", "name": "临时偏好-款式", "delta": 2.0})

        # S003 长期偏好匹配
        # 2026-11-24 改为「真·逐值匹配」：画像存具体属性值权重（蓝 +3、黑 -2），
        # 直接看这套搭配里有没有用户喜欢/反感的值。旧实现只有 0-10 标量分，
        # 无法知道用户喜欢什么颜色，只能给所有搭配无差别加减，等于半失效。
        # 有 user_profile 时走逐值匹配；缺省回落旧标量口径，兼容老调用方。
        profile = ctx.user_profile or {}
        s003_delta = 0.0
        if profile:
            # 抽这套搭配的属性值；缺失字段不臆造，与画像写入同一归一口径
            from app import utils
            hit_attrs = utils.profile_attrs(outfit["items"])
            hit_attrs = {a for a in hit_attrs if a.split(":", 1)[0] in profile}
            for attr in hit_attrs:
                dim, _, val = attr.partition(":")
                w = (profile.get(dim) or {}).get(val)
                if w:
                    s003_delta += w * 0.4  # 单属性权重 2 → +0.8，命中多个累加
            s003_delta = max(-3.0, min(3.0, s003_delta))  # 画像不能锁死推荐
        else:
            prefs = ctx.long_term_prefs or {}
            for dim, key in (("color", "color"), ("style", "style"), ("comfort", "fit_snug")):
                val = _pref_int(prefs.get(dim))
                if val == 5:
                    continue
                s003_delta += (val - 5) * 0.5
        score += s003_delta
        if s003_delta:
            details.append({
                "rule_id": "S003",
                "name": "长期偏好匹配" if s003_delta > 0 else "长期偏好规避",
                "delta": round(s003_delta, 1),
            })

        # S004-S006 色彩协调
        colors = [c for c in outfit["colors"] if c]
        if len(colors) >= 2:
            same_family = len(set(colors)) < len(colors)
            adjacent = any(b in self.repo.color_neighbors.get(a, []) for a in colors for b in colors if a != b)
            contrast = any(b in self.repo.color_contrasts.get(a, []) for a in colors for b in colors if a != b)
            if same_family:
                score += 1.0
                details.append({"rule_id": "S004", "name": "色彩协调-同色系", "delta": 1.0})
            elif adjacent:
                score += 0.8
                details.append({"rule_id": "S005", "name": "色彩协调-邻近色", "delta": 0.8})
            elif contrast:
                score += 0.5
                details.append({"rule_id": "S006", "name": "色彩协调-对比色", "delta": 0.5})

        # S007 版型比例-上宽下窄
        top = next((it for it in items if normalize_category(it["category"]) == "上装"), None)
        bottom = next((it for it in items if normalize_category(it["category"]) == "下装"), None)
        if top and bottom:
            if top.get("fit_snug") in ("宽松", "Oversize") and bottom.get("fit_silhouette") in ("直筒", "紧身"):
                score += 1.0
                details.append({"rule_id": "S007", "name": "版型比例-上宽下窄", "delta": 1.0})

        # S008 26℃ 穿衣法：保暖总额与「26 - 气温」的契合度。
        # 与旧的「平均暖度」不同，求和模型下多叠一件厚衣会直接推高总额、
        # 拉开差距，从机制上消除「越穿越高分」的偏向。
        if ctx.weather_temp is not None:
            temp = float(ctx.weather_temp)
            need = self._warmth_need(temp)
            wear = [it for it in items if _counts_for_warmth(it)]
            degrees = sum(_item_degrees(it) for it in wear)
            gap = degrees - need  # 正=穿多，负=穿少
            abs_gap = abs(gap)

            if abs_gap <= 1.0:
                score += 9.0
                details.append({"rule_id": "S008", "name": "26℃穿衣法-保暖总额精准", "delta": 9.0})
            elif abs_gap <= 2.5:
                score += 5.0
                details.append({"rule_id": "S008", "name": "26℃穿衣法-保暖总额接近", "delta": 5.0})
            elif abs_gap <= 4.0:
                score += 1.0
                details.append({"rule_id": "S008", "name": "26℃穿衣法-保暖总额略偏", "delta": 1.0})
            else:
                score -= 5.0
                details.append({"rule_id": "S008", "name": "26℃穿衣法-保暖总额明显偏差", "delta": -5.0})

            # 穿多比穿少惩罚更重（用户明确反馈：宁可略凉也不要过度保暖）
            if gap > 2.0:
                score -= 4.0
                details.append({"rule_id": "S008", "name": "避免过度保暖", "delta": -4.0})

            # S013 洋葱式叠穿：外层+内层，或中层+内层，至少两层且组合合理
            layers = {_layer_of(it) for it in wear}
            layers.discard(None)
            if len(layers) >= 2:
                score += 2.0
                details.append({"rule_id": "S013", "name": "洋葱式分层叠穿", "delta": 2.0})
            # S014 三明治穿法：内/中/外三层齐全，湿冷天尤其合适
            if {"inner", "middle", "outer"}.issubset(layers):
                score += 1.5
                details.append({"rule_id": "S014", "name": "三明治穿法-三层齐全", "delta": 1.5})
            elif self._is_rainy(ctx) and {"inner", "outer"}.issubset(layers):
                score += 1.0
                details.append({"rule_id": "S014", "name": "雨天内外层防护", "delta": 1.0})

            # S010 雨天鞋履适配：有降雨时优先闭合式鞋
            if self._is_rainy(ctx):
                shoes = [it.get("sub_category", "") for it in items if it.get("category") == "鞋"]
                if shoes and not any(s in ("凉鞋", "拖鞋") for s in shoes):
                    score += 2.0
                    details.append({"rule_id": "S010", "name": "雨天鞋履适配", "delta": 2.0})

        # S009 面料季节适配
        temp = ctx.weather_temp if ctx.weather_temp is not None else None
        if temp is None:
            target_season = None
        elif temp < 10:
            target_season = "冬"
        elif temp >= 28:
            target_season = "夏"
        else:
            target_season = "春秋"
        if target_season is not None:
            for it in items:
                item_season = it.get("season", "四季")
                season_ok = (
                    item_season == "四季"
                    or item_season == target_season
                    or (target_season == "春秋" and item_season in ("春", "秋"))
                )
                if season_ok:
                    score += 0.5
                    details.append({"rule_id": "S009", "name": "面料季节适配", "delta": 0.5})
                    break

        # S015 风格一致性：上装/下装/外套/连衣裙的风格轴不能互斥。
        # clean（基础款/简约/中性）是百搭轴，与任何轴都不冲突。
        styled = [
            (it, _STYLE_AXIS.get(it.get("style", "")))
            for it in items
            if normalize_category(it.get("category")) in ("上装", "下装", "外套", "连衣裙", "套装", "鞋")
        ]
        axes = [(it, ax) for it, ax in styled if ax]
        if len(axes) >= 2:
            conflict = any(
                frozenset((axes[i][1], axes[j][1])) in _STYLE_CONFLICTS
                for i in range(len(axes)) for j in range(i + 1, len(axes))
            )
            if conflict:
                score -= 4.0
                details.append({"rule_id": "S015", "name": "风格互斥降权", "delta": -4.0})
            elif len({ax for _, ax in axes}) == 1:
                score += 3.0
                details.append({"rule_id": "S015", "name": "风格统一", "delta": 3.0})
            else:
                score += 1.5
                details.append({"rule_id": "S015", "name": "风格协调", "delta": 1.5})

        # S016 正式度一致性：整套平均正式度对齐场合目标。
        target_formality = _FORMALITY_TARGET.get(ctx.occasion or "")
        formality_vals = [
            _formal_int(it.get("formality_level")) for it in items
            if normalize_category(it.get("category")) in ("上装", "下装", "外套", "连衣裙", "套装")
        ]
        if target_formality is not None and formality_vals:
            gap = abs(sum(formality_vals) / len(formality_vals) - target_formality)
            if gap <= 0.75:
                score += 2.0
                details.append({"rule_id": "S016", "name": "正式度契合场合", "delta": 2.0})
            elif gap <= 1.5:
                score += 0.5
                details.append({"rule_id": "S016", "name": "正式度接近场合", "delta": 0.5})
            else:
                score -= 3.0
                details.append({"rule_id": "S016", "name": "正式度偏离场合", "delta": -3.0})

        # S011/S012 体型适配
        shape = ctx.body_shape
        if shape and shape in self.repo.body_rules:
            rule = self.repo.body_rules[shape]
            for it in items:
                sc = it.get("sub_category", "")
                if any(r in sc for r in rule.get("recommend", [])):
                    score += 1.0
                    details.append({"rule_id": "S011", "name": "体型适配-优势强化", "delta": 1.0})
                    break
            for it in items:
                sc = it.get("sub_category", "")
                if any(a in sc for a in rule.get("avoid", [])):
                    score -= 1.0
                    details.append({"rule_id": "S012", "name": "体型适配-避坑降权", "delta": -1.0})
                    break

        return round(max(0, min(100, score)), 1), details

    # ------------------------------------------------------ 视觉美感（影子分）
    # 三个子项各自返回 (delta, reason)；delta 仅进 visual_scores 明细，
    # 绝不并入 score。缺失维度返回 (0.0, "")，不臆造、不进比较。
    @staticmethod
    def _visual_v001(outfit: dict) -> tuple[float, str]:
        """V001 配色比例：主色/辅助色/点缀的面积配比（面积用品类角色近似）。

        与 S004-S006（色相关系）、F005（颜色数量）正交：这里评的是「谁占大面积、
        谁做点缀」。数据现实：F005 已把主色数限制在 ≤3，本项只看配比结构。
        """
        items = outfit["items"]
        large = [normalize_color(it.get("color", "")) for it in items
                 if normalize_category(it.get("category")) in _VISUAL_LARGE_CATS
                 and it.get("color")]
        accent = [normalize_color(it.get("color", "")) for it in items
                  if normalize_category(it.get("category")) in _VISUAL_ACCENT_CATS
                  and it.get("color")]
        if not large:
            return 0.0, ""
        main_colors = [c for c in large if c not in _VISUAL_NEUTRALS]
        neutral_count = len(large) - len(main_colors)
        delta = 0.0
        reasons = []
        # 主色：大面积里的非中性色，种类越少越聚焦
        uniq_main = set(main_colors)
        if len(uniq_main) == 1:
            delta += 1.0
            reasons.append(f"大面积以{next(iter(uniq_main))}为主色，配色聚焦")
        elif len(uniq_main) == 2:
            delta += 0.5
            reasons.append("大面积双主色，有层次")
        elif len(uniq_main) >= 3:
            delta -= 0.5
            reasons.append("大面积主色偏多，视觉略杂")
        # 中性色作为辅助底（有主色时才有意义）
        if neutral_count and uniq_main:
            delta += 0.5
            reasons.append("中性色作辅助底，主次分明")
        # 点缀色：小面积出现与主色不同的颜色
        accent_diff = [c for c in accent if c not in uniq_main and c not in _VISUAL_NEUTRALS]
        if accent_diff:
            delta += 0.5
            reasons.append("鞋/配饰有点睛色")
        return round(delta, 1), "；".join(reasons)

    @staticmethod
    def _visual_v002(outfit: dict) -> tuple[float, str]:
        """V002 版型协调：上下松紧组合，跳过 S007 已认领的「上宽下窄」。

        版型字段缺失严重（76 件里 fit_silhouette 仅 7 件有区分度），
        故优先取 fit_silhouette、缺失退 fit_snug，取不到就不评。
        """
        items = outfit["items"]
        top = next((it for it in items if normalize_category(it.get("category")) == "上装"), None)
        bottom = next((it for it in items if normalize_category(it.get("category")) == "下装"), None)
        if not (top and bottom):
            return 0.0, ""

        def looseness(it: dict) -> str | None:
            sil = str(it.get("fit_silhouette") or "").strip()
            snug = str(it.get("fit_snug") or "").strip()
            if sil in ("阔腿", "A字", "宽松"):
                return "loose"
            if sil in ("紧身", "修身"):
                return "tight"
            if snug in ("宽松", "Oversize"):
                return "loose"
            if snug == "修身":
                return "tight"
            return None  # 直筒/常规/合身/缺失：中性，不臆造

        tl, bl = looseness(top), looseness(bottom)
        if not tl or not bl:
            return 0.0, ""
        # S007 已认领「上宽松/oversize + 下直筒/紧身」：这里不重复给分。
        # 下紧身且上宽松的组合归 S007，V002 跳过。
        if tl == "loose" and bl == "tight":
            return 0.0, ""
        if tl == "tight" and bl == "loose":
            return 1.0, "上紧下松，比例利落"
        if tl == "loose" and bl == "loose":
            return -0.8, "上下同宽，轮廓偏拖沓"
        if tl == "tight" and bl == "tight":
            return -0.6, "上下同紧，略显局促"
        return 0.0, ""

    @staticmethod
    def _visual_v003(outfit: dict) -> tuple[float, str]:
        """V003 材质协调：整套面料质感是否统一（fabric_family）。

        只处理 S009（季节）、F014（精致内搭+户外硬壳硬拦）之外的软层：
        核心品类里两种面料都归一成功且不同家族 → 质感不搭，扣分；
        同家族 → +0.5。混纺与任何面料半匹配（沿用判重口径），不臆造。
        """
        items = outfit["items"]
        fabrics = [
            (it, normalize_fabric(it.get("fabric", "")))
            for it in items
            if normalize_category(it.get("category")) in _VISUAL_LARGE_CATS
            and it.get("fabric")
        ]
        named = [(it, f) for it, f in fabrics if f]
        if len(named) < 2:
            return 0.0, ""
        # 取前两件有明确面料的核心单品做质感配对（避免多件时组合爆炸）
        (_, f1), (_, f2) = named[0], named[1]
        if f1 == f2:
            return 0.5, f"整套以{f1}为主，质感统一"
        sim = fabric_similarity(f1, f2)
        if sim is None or sim >= 0.5:
            return 0.0, ""  # 混纺/同家族半匹配：不臆造优劣
        return -0.6, f"{f1}与{f2}质感差异较大"

    def _visual_scores(self, outfit: dict) -> list[dict]:
        """计算三条视觉子项分。返回明细列表，delta 恒不进主 score。"""
        rows = []
        for rid, name, fn in (
            ("V001", "配色比例", self._visual_v001),
            ("V002", "版型协调", self._visual_v002),
            ("V003", "材质协调", self._visual_v003),
        ):
            delta, reason = fn(outfit)
            rows.append({
                "rule_id": rid, "name": name, "delta": delta,
                "reason": reason, "shadow": True,
            })
        return rows

    @staticmethod
    def _rank_score(outfit: dict) -> float:
        """只罚不奖的排序键：主分 + min(0, 视觉分合计)。

        - 主 score 字段保持原值，前端展示与理由文案不受影响；
        - 没有 visual_scores 的调用方（如购前评估）自动按主分排序，
          行为与改造前完全一致。
        """
        visual = sum(v["delta"] for v in outfit.get("visual_scores", []))
        return outfit["score"] + min(0.0, visual)

    @staticmethod
    def _outfit_features(outfit: dict) -> dict:
        """把一套搭配压成多样性特征向量。缺失维度留空集，差异度里不进分母。

        - style：用现有 _STYLE_AXIS 把「简约/通勤/运动」映射到风格轴；
          未收录或空值不臆造，留空集。
        - silhouette：优先 fit_silhouette，缺失退 fit_snug（实测 76 件里
          前者仅 7 件有区分度，后者分布更全）。
        - formality：只取核心品类（与 S016 同口径），配饰不计。
        """
        items = outfit["items"]
        axes = {
            _STYLE_AXIS[style]
            for it in items
            if (style := str(it.get("style") or "").strip()) in _STYLE_AXIS
        }
        colors = {normalize_color(c) for c in outfit.get("colors", []) if c}
        silhouettes = {
            str(it.get("fit_silhouette") or it.get("fit_snug") or "").strip()
            for it in items
        }
        silhouettes.discard("")
        formality_vals = [
            _formal_int(it.get("formality_level")) for it in items
            if normalize_category(it.get("category")) in _FORMAL_ANCHOR_CATS
        ]
        return {
            "core": outfit_core_signature(outfit),
            "style": axes,
            "color": colors,
            "silhouette": silhouettes,
            "formality": (
                sum(formality_vals) / len(formality_vals) if formality_vals else 3.0
            ),
            "item_ids": frozenset(it["item_id"] for it in items),
        }

    @staticmethod
    def _outfit_difference(a: dict, b: dict) -> float:
        """两套搭配的加权差异度 0~1。缺失维度不进分母，避免空属性占便宜。

        核心骨架相同的两套差异记 0：只换鞋/外套/配饰不算有意义的差异。
        """
        parts: dict[str, float] = {}
        if a["style"] and b["style"]:
            parts["style"] = 1.0 - jaccard(a["style"], b["style"])
        if a["color"] and b["color"]:
            parts["color"] = 1.0 - jaccard(a["color"], b["color"])
        if a["silhouette"] and b["silhouette"]:
            parts["silhouette"] = 1.0 - jaccard(a["silhouette"], b["silhouette"])
        parts["formality"] = min(1.0, abs(a["formality"] - b["formality"]) / 3.0)
        parts["item"] = 1.0 - jaccard(a["item_ids"], b["item_ids"])

        den = sum(_DIVERSITY_WEIGHTS[k] for k in parts)
        diff = (
            sum(_DIVERSITY_WEIGHTS[k] * v for k, v in parts.items()) / den
            if den else 0.0
        )
        if a["core"] and a["core"] == b["core"]:
            diff = _DIVERSITY_SAME_CORE_DIFF
        return round(diff, 4)

    @staticmethod
    def _mmr_pool(outfits: list[dict], limit: int = _MMR_POOL_LIMIT,
                  per_core: int = _MMR_PER_CORE) -> list[dict]:
        """按分数降序建池，每个核心骨架最多取 per_core 套。

        实测最热骨架能派生 690 套；若直接截前 300，MMR 会全部在同一个
        骨架里打转，差异化形同虚设。
        """
        seen: dict[str, int] = {}
        pool: list[dict] = []
        for o in outfits:  # recommend 已按分数降序
            core = outfit_core_signature(o)
            if seen.get(core, 0) >= per_core:
                continue
            seen[core] = seen.get(core, 0) + 1
            pool.append(o)
            if len(pool) >= limit:
                break
        return pool

    def _diversify(self, outfits: list[dict], top_k: int = 3) -> list[dict]:
        """MMR 差异化取 TopK：第 1 套锁定最高分，后两套按质量-差异权衡。

        MMR(x) = λ·(rank/max_rank) − (1−λ)·max_sim(x, 已选)
        sim = 1 − 差异度。λ=0.7 偏重质量，不至于为了差异牺牲搭配合理性。
        质量项统一用 rank_score（只罚不奖），与 recommend 的排序同口径；
        没有 visual_scores 的调用方按主分计算，行为不变。
        """
        if len(outfits) <= top_k:
            return outfits

        pool = self._mmr_pool(outfits)
        if len(pool) <= top_k:
            return pool[:top_k]

        feats = [self._outfit_features(o) for o in pool]
        ranks = [self._rank_score(o) for o in pool]
        max_rank = max(ranks) or 1.0
        selected = [0]
        while len(selected) < top_k:
            best_i, best_key = None, None
            for i, o in enumerate(pool):
                if i in selected:
                    continue
                relevance = ranks[i] / max_rank
                similarity = max(
                    1.0 - self._outfit_difference(feats[i], feats[j])
                    for j in selected
                )
                # 并列时排序分高优先；再并列时保持原顺序（稳定输出）
                key = (
                    _MMR_LAMBDA * relevance - (1 - _MMR_LAMBDA) * similarity,
                    ranks[i], -i,
                )
                if best_key is None or key > best_key:
                    best_key, best_i = key, i
            selected.append(best_i)
        return [pool[i] for i in selected]

    def _generate_reason(self, outfit: dict, ctx: RecommendContext) -> dict[str, Any]:
        """生成可解释理由。"""
        items = outfit["items"]
        points = []

        # 天气：只在拿到真实气温时描述，绝不编造一个温度写进推荐理由
        temp = ctx.weather_temp
        wear = [it for it in items if _counts_for_warmth(it)]
        degrees = sum(_item_degrees(it) for it in wear)
        if temp is None:
            points.append({"type": "weather",
                           "text": "本次未获取到实时天气，方案按你的衣橱厚薄与场合匹配。",
                           "rule_id": "S008"})
        else:
            need = self._warmth_need(temp)
            total = round(float(temp) + degrees, 1)
            diff = degrees - need
            if temp > COMFORT_TEMP:
                head = f"气温 {temp}°C 高于 26°C，按清凉夏装标准挑选轻薄单品。"
            elif abs(diff) <= 1.0:
                head = f"气温 {temp}°C，衣物折算约 {degrees:g}°C，相加 {total:g}°C 贴近 26°C 舒适线。"
            elif diff > 0:
                head = f"气温 {temp}°C，衣物折算约 {degrees:g}°C，相加 {total:g}°C 略偏暖。"
            else:
                head = f"气温 {temp}°C，衣物折算约 {degrees:g}°C，相加 {total:g}°C 略偏凉。"
            points.append({"type": "weather", "text": head, "rule_id": "S008"})

            # 洋葱/三明治穿法说明：多套方案里最多点名两种，避免文案堆砌
            layers = {_layer_of(it) for it in wear}
            layers.discard(None)
            if {"inner", "middle", "outer"}.issubset(layers):
                points.append({"type": "layering", "text": "内-中-外三层齐全，按三明治穿法兼顾透气、保暖与防风。", "rule_id": "S014"})
            elif len(layers) >= 2:
                points.append({"type": "layering", "text": "上下分层叠穿，采用洋葱式穿法，可按体感随时增减。", "rule_id": "S013"})
            elif temp > COMFORT_TEMP:
                points.append({"type": "layering", "text": "单层清凉穿搭，透气散热，适合高温天气。", "rule_id": "S011"})

        # 体型
        shape = ctx.body_shape
        if shape and shape in self.repo.body_rules:
            rule = self.repo.body_rules[shape]
            rec_items = [it["name"] for it in items if any(r in it.get("sub_category", "") for r in rule.get("recommend", []))]
            avoid_items = [it["name"] for it in items if any(a in it.get("sub_category", "") for a in rule.get("avoid", []))]
            if rec_items:
                points.append({"type": "body", "text": f"{shape}身材适合{rec_items[0]}，能突出优势。", "rule_id": "S011"})
            if avoid_items:
                points.append({"type": "body", "text": f"注意{avoid_items[0]}对{shape}身材不够友好，已尽量降低优先级。", "rule_id": "S012"})

        # 场合
        occasion = ctx.occasion
        if occasion:
            points.append({"type": "occasion", "text": f"整体风格符合「{occasion}」场景的着装要求。", "rule_id": "F003"})

        # 颜色
        colors = [it.get("color", "") for it in items if it.get("color")]
        if len(set(colors)) <= 3:
            points.append({"type": "color", "text": f"主色控制在 {len(set(colors))} 种，符合三色原则。", "rule_id": "F005"})

        # 风格 / 正式度：只解释通过校验的方案，不把降权方案硬说成风格统一
        details = outfit.get("score_details", [])
        s015 = next((d for d in details if d.get("rule_id") == "S015" and d.get("delta", 0) > 0), None)
        if s015:
            text = ("上装、下装与外套风格统一，视觉语言一致。" if s015["delta"] >= 3
                    else "各单品风格协调，没有明显的混搭冲突。")
            points.append({"type": "style", "text": text, "rule_id": "S015"})
        s016 = next((d for d in details if d.get("rule_id") == "S016" and d.get("delta", 0) > 0), None)
        if s016 and len(points) < 6:
            points.append({"type": "occasion", "text": "单品正式度与场合要求一致。", "rule_id": "S016"})

        # 偏好
        temp_pref = ctx.temp_pref or {}
        if temp_pref.get("color"):
            points.append({"type": "preference", "text": f"照顾到你本次想要的「{temp_pref['color']}」倾向。", "rule_id": "S001"})
        if temp_pref.get("style"):
            points.append({"type": "preference", "text": f"照顾到你本次想要的「{temp_pref['style']}」风格。", "rule_id": "S002"})

        summary = f"这套搭配得分为 {outfit['score']} 分，"
        if occasion:
            summary += f"适合「{occasion}」"
        else:
            summary += "适合日常"
        if temp is not None:
            summary += f"，{temp}°C 穿着舒适。"
        else:
            summary += "。"

        return {"summary": summary, "points": points}

    @staticmethod
    def _is_rainy(ctx) -> bool:
        """是否在下雨。

        「rain」文案千奇百怪（晴 / 多云 / 小雨 / 中雨…），不能只靠黑名单，
        所以同时看降雨概率：概率 ≥ 50 直接判为有雨；文案明确含雨/雪也算。
        """
        prob = getattr(ctx, "weather_rain_prob", None)
        if prob is not None:
            try:
                if float(prob) >= 50:
                    return True
                if float(prob) < 20:
                    return False
            except (TypeError, ValueError):
                pass
        text = (getattr(ctx, "weather_rain", None) or "").strip()
        if not text:
            return False
        if text in ("无降雨", "未知", "晴", "多云", "阴"):
            return False
        return any(k in text for k in ("雨", "雪", "雷", "阵雨", "storm", "rain", "snow"))

    @staticmethod
    def _warmth_need(temp: float) -> float:
        """26℃ 穿衣法：所需衣物折算度数 = 26 - 气温，下限 1℃。

        气温高于 26℃ 时保留 1℃（一件薄衫），避免需求为 0/负数导致
        所有组合都得 0 分无法区分；清凉夏装由 F011 硬过滤保证。

        低温收敛（2026-11-22 实测修正）：26 - temp 是经验公式的线性外推，
        在 < 11℃ 时会要求 16~21℃ 以上的衣物折算；但本引擎按 26℃穿衣法
        的行业口径只把「上装/外套/连衣裙/围巾」计入折算（下装与鞋不计），
        单套可达上限约 15℃（厚上装 6 + 羽绒服 9）。两者叠加导致 F010
        下限（need - 5）在 5℃ 以下结构性不可达，整套推荐与购前评估全部
        归零 → 冬天买羽绒服反被判「不建议购买」。
        极寒区改按洋葱式叠穿的实际上限收敛，保留温和的温度区分，
        不再让线性外推制造不可能达标的需求。>= 11℃ 完全沿用原公式。
        """
        if temp is None:
            return 4.0
        t = float(temp)
        raw = max(1.0, round(COMFORT_TEMP - t, 1))
        if t >= LOW_TEMP_LINEAR_BOUNDARY:
            return raw
        # 11℃ 时 15.0；每低 1℃ 只再加 0.2℃，5℃ 约 16.2、-5℃ 约 18.2
        return round(LOW_TEMP_NEED_BASE + (LOW_TEMP_LINEAR_BOUNDARY - t) * LOW_TEMP_SLOPE, 1)

    @staticmethod
    def _target_warmth(temp: float) -> float:
        """兼容旧调用：返回旧的平均暖度目标（已不在推荐主链路使用）。"""
        if temp is None:
            return 3.0
        if temp <= 5:
            return 5.0
        if temp <= 12:
            return 4.2
        if temp <= 18:
            return 3.4
        if temp <= 24:
            return 2.8
        if temp <= 29:
            return 2.0
        return 1.4

    @staticmethod
    def _hydrate_items(base_items: list[dict], all_items: list[dict]) -> list[dict]:
        """把对话历史里的精简 item（只有 item_id/category/name/image_url）补全为
        引擎输入（warmth_level / season / sub_category 等），避免打分时全用默认值。
        找不到就保留原字典，不丢单品。
        """
        by_id = {it["item_id"]: it for it in all_items}
        return [by_id.get(it.get("item_id"), it) for it in base_items]

    def augment(self, base_items: list[dict], add_category: str,
                all_items: list[dict], ctx: RecommendContext) -> dict[str, Any]:
        """在锁定方案上加一件单品（对话式「加个配饰 / 添件外套」），返回单套。

        与 refine 的区别：refine 是替换，augment 是追加。两者都只动锁定套，
        不重新出三套方案。
        """
        base_items = self._hydrate_items(base_items, all_items)
        base_ids = {it["item_id"] for it in base_items}
        target = normalize_category(add_category)
        cands = [
            it for it in all_items
            if normalize_category(it.get("category")) == target
            and it["item_id"] not in base_ids
        ]
        if not cands:
            return {"outfit": None,
                    "reason": f"衣橱里没有可添加的「{add_category}」单品，先往衣橱补一件吧。"}

        exclude = set((ctx.constraints or {}).get("exclude_colors", []))
        if exclude:
            cands = [c for c in cands if c.get("color") not in exclude] or cands

        temp = ctx.weather_temp
        deg_before = sum(_item_degrees(it) for it in base_items if _counts_for_warmth(it))
        target_deg = self._warmth_need(temp) if temp is not None else deg_before
        base_colors = [it.get("color", "") for it in base_items if it.get("color")]

        def trial_score(cand: dict) -> float:
            s = 0.0
            # 新增件若计入保暖，纳入 26℃ 契合度；纯配饰不影响保暖
            if _counts_for_warmth(cand):
                new_deg = deg_before + _item_degrees(cand)
                s -= abs(new_deg - target_deg) * 3.0
                if new_deg - target_deg > 2.0:
                    s -= 2.0
            if cand.get("color") and cand["color"] in base_colors:
                s += 2.0
            main = {it.get("color") for it in base_items
                    if it.get("category") != "配饰" and it.get("color")}
            ccat = normalize_category(cand.get("category"))
            if cand.get("color") and ccat != "配饰":
                main.add(cand["color"])
            if len(main) <= 3:
                s += 2.0
            return s

        cands.sort(key=trial_score, reverse=True)
        best = cands[0]
        new_items = sorted(base_items + [best], key=lambda it: _cat_rank(it.get("category")))
        score = round(max(0.0, min(100.0, 70.0 + trial_score(best))), 1)

        points = [{"type": "augment",
                   "text": f"在锁定的这套里加入了「{best.get('name')}」（{add_category}），其余单品保持不变。",
                   "rule_id": "A001"}]
        if temp is not None:
            new_deg = sum(_item_degrees(it) for it in new_items if _counts_for_warmth(it))
            points.append({"type": "weather",
                           "text": f"气温 {temp}°C，加入后衣物折算约 {new_deg:g}°C。",
                           "rule_id": "S008"})

        return {
            "outfit": {
                "items": new_items,
                "score": score,
                "fingerprint": "augment_" + "_".join(sorted(it["item_id"] for it in new_items)),
                "categories": {normalize_category(it["category"]) for it in new_items},
                "sub_categories": {it.get("sub_category") for it in new_items},
                "colors": [it.get("color", "") for it in new_items if it.get("color")],
                "reason": {"summary": f"在锁定方案上加了「{best.get('name')}」，整体更完整。",
                           "points": points},
            },
            "added_name": best.get("name"),
            "points": points,
        }

    def refine(self, base_items: list[dict], target_category: str,
               all_items: list[dict], ctx: RecommendContext,
               sub_category: str | None = None) -> dict[str, Any]:
        """锁定一套方案，仅替换目标品类单品，返回单套修改版。

        sub_category：用户点名了具体单品（「换成裙子」）时，优先在同一子品类里找替代；
        衣橱里没有该子品类才回退到整个大类，避免换出「名为换裙、实为换裤」。
        """
        base_items = self._hydrate_items(base_items, all_items)
        base_ids = {it["item_id"] for it in base_items}
        target_cat = normalize_category(target_category)
        # 候选：同品类、可穿、且不在原方案中
        cands = [
            it for it in all_items
            if normalize_category(it.get("category")) == target_cat and it["item_id"] not in base_ids
        ]
        if not cands:
            return {"outfit": None, "reason": f"衣橱里没有可替换的「{target_category}」，建议先补充衣物。"}
        # 点名子品类时优先同子类（如「换成裙子」只在半身裙里挑）
        if sub_category:
            narrowed = [it for it in cands if it.get("sub_category") == sub_category]
            if narrowed:
                cands = narrowed

        # 26℃ 穿衣法：换装后按「当前气温 + 上身折算度数」贴近 26℃ 打分。
        # 天气未知时退回「保留原方案保暖量」，避免用假温度挑单品。
        temp = ctx.weather_temp
        base_degrees = sum(_item_degrees(it) for it in base_items if _counts_for_warmth(it))
        target = self._warmth_need(temp) if temp is not None else base_degrees
        exclude_colors = set((ctx.constraints or {}).get("exclude_colors", []))
        if exclude_colors:
            cands = [c for c in cands if c.get("color") not in exclude_colors] or cands

        kept = [it for it in base_items if normalize_category(it.get("category")) != target_cat]

        def trial_score(cand):
            trial_items = kept + [cand]
            deg = sum(_item_degrees(it) for it in trial_items if _counts_for_warmth(it))
            diff = deg - target
            s = -abs(diff) * 3.0
            # 穿多额外惩罚（与推荐主链路一致）
            if diff > 2.0:
                s -= 2.0
            # 与保留单品的色彩协调
            base_colors = [it.get("color", "") for it in kept if it.get("color")]
            c = cand.get("color", "")
            if c and c in base_colors:
                s += 2.0
            main_colors = set([it.get("color") for it in trial_items if it.get("category") != "配饰" and it.get("color")])
            if len(main_colors) <= 3:
                s += 2.0
            return s

        cands.sort(key=trial_score, reverse=True)
        best = cands[0]
        new_items = kept + [best]

        for it in new_items:
            it["score"] = round(60.0 + trial_score(best), 1)
        new_items.sort(key=lambda it: _cat_rank(it.get("category")))

        old_item = next((it for it in base_items if normalize_category(it.get("category")) == target_cat), None)
        new_deg = sum(_item_degrees(it) for it in new_items if _counts_for_warmth(it))
        if temp is not None:
            points = [{"type": "weather",
                       "text": f"气温 {temp}°C，替换后衣物折算 {new_deg:g}°C（目标 {target:g}°C），"
                               f"{'贴近 26℃ 舒适线' if abs(new_deg - target) <= 1 else '保暖量有偏差'}。",
                       "rule_id": "S008"}]
        else:
            points = [{"type": "weather",
                       "text": f"替换后衣物折算 {new_deg:g}°C，与原方案保暖量保持一致。",
                       "rule_id": "S008"}]
        if old_item:
            points.append({"type": "refine",
                           "text": f"保留原方案其余 {len(kept)} 件，把「{old_item.get('name')}」换成「{best.get('name')}」。",
                           "rule_id": "R001"})

        return {
            "outfit": {
                "items": new_items,
                "score": round(60.0 + trial_score(best), 1),
                "fingerprint": "refine_" + "_".join(sorted(it["item_id"] for it in new_items)),
                "categories": {normalize_category(it["category"]) for it in new_items},
                "sub_categories": {it.get("sub_category") for it in new_items},
                "colors": [it.get("color", "") for it in new_items if it.get("color")],
            },
            "kept_count": len(kept),
            "from_name": old_item.get("name") if old_item else "",
            "to_name": best.get("name"),
            "points": points,
        }

    def to_response(self, outfit: dict) -> dict[str, Any]:
        return self._to_response(outfit)

    def _to_response(self, outfit: dict) -> dict[str, Any]:
        items = sorted(outfit["items"], key=lambda it: _cat_rank(it.get("category")))
        return {
            "outfit_id": outfit["fingerprint"],
            "items": [
                {
                    "item_id": it["item_id"],
                    "category": it["category"],
                    "name": it["name"],
                    "image_url": it.get("image_url", ""),
                }
                for it in items
            ],
            "score": outfit["score"],
            "reason": outfit["reason"],
            "is_diff": True,
        }


# 进程级单例
rule_repo = RuleRepository()
