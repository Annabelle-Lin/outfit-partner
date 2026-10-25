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
    weather_temp: float | None = 22.0
    temp_pref: dict | None = None
    constraints: dict | None = None
    shown_outfit_ids: list | None = None
    body_shape: str | None = None
    long_term_prefs: dict | None = None


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

        if not outfits:
            return {"outfits": [], "reason": "没有满足当前约束的搭配，建议放宽条件（如减少排除色、换场合）。"}

        # 4. 打分
        for o in outfits:
            o["score"], o["score_details"] = self._score(o, ctx)

        # 5. 按分数降序
        outfits.sort(key=lambda x: x["score"], reverse=True)

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
            by_cat.setdefault(it["category"], []).append(it)

        tops = by_cat.get("上装", [])
        bottoms = by_cat.get("下装", [])
        shoes = by_cat.get("鞋", [])
        dresses = by_cat.get("连衣裙", [])
        outerwears = by_cat.get("外套", [])
        accessories = by_cat.get("配饰", [])

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

        # 方案 B：连衣裙 + 鞋
        for d, s in product(dresses, shoes):
            core = [d, s]
            combos.append(core)
            for o in outerwears:
                combos.append([o] + core)
            for a in accessories:
                combos.append(core + [a])

        results = []
        for combo in combos:
            ids = sorted([it["item_id"] for it in combo])
            fp = "_".join(ids)
            results.append({
                "items": combo,
                "fingerprint": fp,
                "categories": {it["category"] for it in combo},
                "sub_categories": {it["sub_category"] for it in combo},
                "colors": [it.get("color", "") for it in combo if it.get("color")],
            })
        return results

    def _hard_filter(self, outfit: dict, ctx: RecommendContext) -> bool:
        """执行 8 条硬过滤规则。"""
        items = outfit["items"]
        temp = ctx.weather_temp or 22.0
        occasion = ctx.occasion or ""
        constraints = ctx.constraints or {}

        # F001 低温禁短装
        if temp <= 5:
            forbid = {"短裤", "短袖", "吊带", "背心", "凉鞋"}
            if outfit["sub_categories"] & forbid:
                return False

        # F002 高温禁厚装
        if temp >= 28:
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

        # F005 三色原则（配饰不算）
        main_colors = [it.get("color", "") for it in items if it.get("category") != "配饰" and it.get("color")]
        if len(set(main_colors)) > 3:
            return False

        # F006 品类完整性
        cats = outfit["categories"]
        has_top_bottom = {"上装", "下装", "鞋"}.issubset(cats)
        has_dress = {"连衣裙", "鞋"}.issubset(cats)
        if not (has_top_bottom or has_dress):
            return False

        # F007 同单品不重复
        ids = [it["item_id"] for it in items]
        if len(ids) != len(set(ids)):
            return False

        # F008 用户排除色
        exclude_colors = set(constraints.get("exclude_colors", []))
        if exclude_colors and set(outfit["colors"]) & exclude_colors:
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
        prefs = ctx.long_term_prefs or {}
        # 简化：按颜色/风格/版型匹配次数给分，权重用偏好分映射到 -5~5
        s003_delta = 0.0
        for it in items:
            matches = 0
            if it.get("color") and prefs.get("color", 5) > 5:
                matches += 1
            if it.get("style") and prefs.get("style", 5) > 5:
                matches += 1
            if it.get("fit_snug") and prefs.get("comfort", 5) > 5:
                matches += 1
            if matches:
                s003_delta += matches * 0.5
        score += s003_delta
        if s003_delta > 0:
            details.append({"rule_id": "S003", "name": "长期偏好匹配", "delta": round(s003_delta, 1)})

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
        top = next((it for it in items if it["category"] == "上装"), None)
        bottom = next((it for it in items if it["category"] == "下装"), None)
        if top and bottom:
            if top.get("fit_snug") in ("宽松", "Oversize") and bottom.get("fit_silhouette") in ("直筒", "紧身"):
                score += 1.0
                details.append({"rule_id": "S007", "name": "版型比例-上宽下窄", "delta": 1.0})

        # S009 面料季节适配
        temp = ctx.weather_temp or 22.0
        if temp < 10:
            target_season = "冬"
        elif temp >= 28:
            target_season = "夏"
        else:
            target_season = "春秋"
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

    def _diversify(self, outfits: list[dict], top_k: int = 3) -> list[dict]:
        """从高分方案中取 TopK，要求彼此有一定差异（至少替换一件主单品）。"""
        if len(outfits) <= top_k:
            return outfits

        selected = [outfits[0]]
        for o in outfits[1:]:
            if len(selected) >= top_k:
                break
            fp_set = set(o["fingerprint"].split("_"))
            different = True
            for s in selected:
                overlap = len(fp_set & set(s["fingerprint"].split("_")))
                if overlap >= min(len(fp_set), 3):  # 至少替换一件核心单品
                    different = False
                    break
            if different:
                selected.append(o)
        return selected

    def _generate_reason(self, outfit: dict, ctx: RecommendContext) -> dict[str, Any]:
        """生成可解释理由。"""
        items = outfit["items"]
        points = []

        # 天气
        temp = ctx.weather_temp or 22.0
        warm_items = [it["name"] for it in items if it.get("warmth_level", 3) >= 4]
        light_items = [it["name"] for it in items if it.get("warmth_level", 3) <= 2]
        if temp <= 10 and warm_items:
            points.append({"type": "weather", "text": f"气温 {temp}°C，{warm_items[0]}等单品保暖性较好。", "rule_id": "S009"})
        elif temp >= 26 and light_items:
            points.append({"type": "weather", "text": f"气温 {temp}°C，{light_items[0]}等单品轻薄透气。", "rule_id": "S009"})
        else:
            points.append({"type": "weather", "text": f"气温 {temp}°C，整体厚度适中。", "rule_id": "S009"})

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
        if temp:
            summary += f"，{temp}°C 穿着舒适。"
        else:
            summary += "。"

        return {"summary": summary, "points": points}

    def _to_response(self, outfit: dict) -> dict[str, Any]:
        items = sorted(outfit["items"], key=lambda it: ("上装", "下装", "外套", "连衣裙", "鞋", "配饰").index(it["category"]) if it["category"] in ("上装", "下装", "外套", "连衣裙", "鞋", "配饰") else 99)
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
