# -*- coding: utf-8 -*-
"""购前评估模块（计划书 13.6 + 知识库 K-ALG-05 / K-PREF-03）。

POST   /purchase/evaluate        新品评估（能否搭配 / 是否重复）
GET    /purchase/history         评估历史
DELETE /purchase/{evaluation_id} 删除一条评估记录

相对旧实现修正的缺陷：
1. 搭配必须包含目标单品。旧实现直接取引擎 Top3，拿到的是「整套衣橱搭配」，
   目标单品可能一套都没出现，却仍被计入 matchable_count。
   实测：衣橱全是夏装时评估重型羽绒服，返回的第 2 套完全不含羽绒服。
2. 硬过滤依赖暖度。旧实现衣柜单品暖度一律是默认 3，羽绒服与薄衬衫等价，
   F002/F014/F015 全部失效，出现「羽绒服 + 凉鞋」仍通过。
   现按子品类推断暖度（garment_profile），并保留用户显式录入值。
3. 气温不再写死 22℃：优先用请求里的 temperature，其次按 city 取实时天气，
   再次按季节默认值，并把实际来源如实返回。
4. 重复度改 Jaccard（K-ALG-05），并点名最相似单品与共同属性。
5. 购买建议按 K-PREF-03 阈值：<0.5 且 组合数≥3 才建议购买。
6. matchable_count 语义改为「含目标单品的合法搭配数」，
   另给 closet_only_count 作为衣橱原有搭配对照。
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app import schemas, utils
from app.agent.engine import RecommendContext
from app.agent.garment_profile import (
    duplicate_analysis,
    effective_season,
    effective_warmth,
    infer_season,
    infer_sub_category,
    infer_warmth,
    normalize_category,
    normalize_season,
)
from app.api import ok
from app.db import ClothingItem, PurchaseEvaluation, User, get_db

router = APIRouter()

TARGET_ID = "purchase_target"

# 季节默认气温：仅在用户既没给温度、又拿不到实时天气时使用，并如实标注
SEASON_DEFAULT_TEMP = {"春季": 18.0, "夏季": 28.0, "秋季": 18.0, "冬季": 5.0, "四季": 20.0}

# 引擎硬过滤里的品类禁配清单，用于目标单品自身校验（与 engine._hard_filter 保持一致）
FORMAL_FORBID = {"运动裤", "拖鞋", "背心", "短裤", "卫衣"}
SPORT_FORBID = {"皮鞋", "西装外套", "衬衫"}
COLD_FORBID = {"短裤", "短袖", "吊带", "背心", "凉鞋"}
HOT_FORBID = {"羽绒服", "棉服", "大衣", "厚毛衣"}


def _brightness(color: str | None) -> str:
    """粗略的明度归类，供引擎 S001（临时偏好-颜色）使用。"""
    text = str(color or "")
    if not text:
        return ""
    if any(k in text for k in ("黑", "深", "藏青", "咖", "棕")):
        return "深色"
    if any(k in text for k in ("白", "米", "浅", "杏", "奶")):
        return "浅色"
    return "中色"


def _target_warmth_for(temp: float | None) -> float:
    """与引擎 RecommendEngine._target_warmth 保持一致的理想暖度。"""
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


def _weather_from_city(city: str) -> tuple[float | None, str | None]:
    """按城市取实时气温；失败返回 (None, None)，不编造数据。"""
    city = (city or "").strip()
    if not city:
        return None, None
    try:
        from app.api import weather as weather_api
    except Exception:
        return None, None

    fetcher = getattr(weather_api, "_fetch_weather", None)
    if not callable(fetcher):
        return None, None
    try:
        data = fetcher(city)
    except Exception:
        return None, None
    if not isinstance(data, dict):
        return None, None

    temp = None
    for key in ("temp", "temperature", "temp_c", "temperature_c", "weather_temp"):
        value = data.get(key)
        if isinstance(value, (int, float)):
            temp = float(value)
            break
    if temp is None:
        for nest in ("current", "weather", "data"):
            sub = data.get(nest)
            if isinstance(sub, dict):
                for key in ("temp", "temperature", "temp_c"):
                    value = sub.get(key)
                    if isinstance(value, (int, float)):
                        temp = float(value)
                        break
            if temp is not None:
                break

    rain = None
    for key in ("rain", "rain_desc", "precip", "precipitation"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            rain = value.strip()
            break
    return temp, rain


def _resolve_temperature(req: schemas.PurchaseEvaluateRequest) -> tuple[float | None, str]:
    """返回 (气温, 来源)。来源用于前端如实标注，不能把估算值说成实时天气。"""
    if isinstance(req.temperature, (int, float)):
        return float(req.temperature), "user"
    if (req.city or "").strip():
        temp, _ = _weather_from_city(req.city)
        if temp is not None:
            return temp, f"weather:{req.city.strip()}"
    season = normalize_season(req.season)
    if season in SEASON_DEFAULT_TEMP:
        return SEASON_DEFAULT_TEMP[season], f"season_default:{season}"
    return SEASON_DEFAULT_TEMP["四季"], "season_default:四季"


def _build_target(raw: schemas.TargetItem) -> dict:
    """把请求里的拟购单品转成引擎可用的单品字典，并补齐推断属性。"""
    category = normalize_category(raw.category) or "上装"
    name = (raw.name or "").strip() or "待评估单品"
    sub_category = infer_sub_category(name, category, raw.sub_category)

    warmth_source = "user"
    warmth = raw.warmth_level
    if warmth is None or int(warmth) <= 0:
        warmth = infer_warmth(sub_category, category)
        warmth_source = "inferred"
    warmth = int(warmth)

    season_source = "user"
    season = normalize_season(raw.season)
    if not season or season in ("四季", "None"):
        season = infer_season(warmth)
        season_source = "inferred"

    formality = raw.formality_level
    if formality is None or int(formality) <= 0:
        formality = 3

    return {
        "item_id": TARGET_ID,
        "name": name,
        "category": category,
        "sub_category": sub_category,
        "color": (raw.color or "").strip(),
        "color_brightness": _brightness(raw.color),
        "season": season,
        "fit_snug": (raw.fit_snug or "").strip(),
        "fit_silhouette": (raw.fit_silhouette or "").strip(),
        "fabric": (raw.fabric or "").strip(),
        "style": (raw.style or "").strip(),
        "occasion": list(raw.occasion or []),
        "warmth_level": warmth,
        "formality_level": int(formality),
        "image_url": "",
        "tags": [],
        # 供前端展示字段来源，避免把推断值伪装成用户确认过的属性
        "_attr_source": {
            "sub_category": "inferred" if not (raw.sub_category or "").strip() else "user",
            "warmth_level": warmth_source,
            "season": season_source,
        },
    }


def _closet_item(row: ClothingItem) -> dict:
    """衣橱单品转引擎字典，并用推断暖度替换占位默认值。"""
    item = {
        "item_id": row.item_id,
        "name": row.name,
        "category": normalize_category(row.category),
        "sub_category": row.sub_category,
        "color": row.color or "",
        "color_brightness": row.color_brightness or _brightness(row.color),
        "season": row.season or "",
        "fit_snug": row.fit_snug or "",
        "fit_silhouette": row.fit_silhouette or "",
        "fabric": row.fabric or "",
        "style": row.style or "",
        "occasion": list(row.occasion or []),
        "warmth_level": row.warmth_level,
        "formality_level": row.formality_level,
        "image_url": row.image_url or "",
        "tags": list(row.tags or []),
    }
    item["warmth_level"] = effective_warmth(item)
    if not item["season"] or normalize_season(item["season"]) in ("", "四季", "None"):
        item["season"] = infer_season(item["warmth_level"])
    return item


def _season_conflict(target_season: str, season: str) -> str:
    """季节与拟购单品是否明显冲突；返回冲突说明，无冲突返回空串。"""
    target_season = normalize_season(target_season)
    season = normalize_season(season)
    if not season or season == "四季" or not target_season or target_season == "四季":
        return ""
    if season == "夏季" and target_season == "冬季":
        return "冬季单品不适合夏季穿着"
    if season == "冬季" and target_season == "夏季":
        return "夏季单品不适合冬季穿着"
    if season == "冬季" and target_season == "春秋":
        return "春秋款在冬季偏薄"
    return ""


def _target_self_check(target: dict, temp: float | None, occasion: str, season: str) -> dict:
    """目标单品自身的硬校验。

    只校验「这件单品本身是否合理」，不做搭配数量统计。
    任一条件不满足时，即使衣橱里能凑出组合也应明确告知原因，
    而不是用空搭配列表让用户猜。
    """
    violations: list[dict] = []
    warmth = int(target.get("warmth_level") or 3)
    sub = target.get("sub_category") or ""
    category = normalize_category(target.get("category"))

    # 暖度与气温
    if temp is not None:
        ideal = _target_warmth_for(temp)
        if category not in ("鞋", "配饰") and abs(warmth - ideal) >= 2.0:
            violations.append({
                "rule_id": "F014",
                "message": f"{sub} 暖度 {warmth} 与 {temp:g}℃ 不匹配（该气温宜 {ideal:g} 档）",
            })
        if temp <= 5 and sub in COLD_FORBID:
            violations.append({
                "rule_id": "F001",
                "message": f"{temp:g}℃ 不宜穿 {sub}",
            })
        if temp >= 28 and sub in HOT_FORBID:
            violations.append({
                "rule_id": "F002",
                "message": f"{temp:g}℃ 穿 {sub} 过热",
            })
        if temp > 15 and warmth >= 5:
            violations.append({
                "rule_id": "F015",
                "message": f"极厚单品（暖度 {warmth}）在 {temp:g}℃ 不合常理",
            })
        if temp < 20 and warmth <= 1 and category not in ("配饰",):
            violations.append({
                "rule_id": "F016",
                "message": f"极薄单品（暖度 {warmth}）在 {temp:g}℃ 偏冷",
            })

    # 季节
    conflict = _season_conflict(effective_season(target), season)
    if conflict:
        violations.append({"rule_id": "F-SEASON", "message": conflict})

    # 场合
    if occasion in ("面试", "正式") and sub in FORMAL_FORBID:
        violations.append({
            "rule_id": "F003",
            "message": f"正式场合不宜搭配 {sub}",
        })
    if occasion == "运动" and sub in SPORT_FORBID:
        violations.append({
            "rule_id": "F004",
            "message": f"运动场合不宜搭配 {sub}",
        })

    return {"passed": not violations, "violations": violations}


def _suggestion(duplicate: dict, matchable_count: int, partners: list[str],
                temp: float | None, nearest_name: str | None) -> tuple[str, str]:
    """按 K-PREF-03 给出建议与档位。

    建议购买：重复度 < 0.5 且 组合数 ≥ 3
    谨慎购买：重复度 0.5~0.7，或 组合数 1~2
    不建议：重复度 ≥ 0.7，或 组合数 = 0
    """
    score = duplicate.get("score") or 0.0
    level = duplicate.get("level")
    temp_text = f"{temp:g}℃" if temp is not None else "当前气温"
    partner_text = "、".join(partners[:3]) if partners else "（暂无）"

    if score >= 0.7:
        return (
            "不建议购买",
            f"与衣橱中「{nearest_name or '同类单品'}」重复度 {score:.2f}（{level}），"
            f"再买一件利用率会很低。",
        )
    if matchable_count == 0:
        return (
            "不建议购买",
            f"在 {temp_text} 下无法与现有衣橱组成合法搭配，买回去大概率闲置；"
            f"若确实需要，建议先补齐缺失的基础单品。",
        )
    if score >= 0.5 or matchable_count < 3:
        return (
            "谨慎购买",
            f"重复度 {score:.2f}（{level}），{temp_text} 下能组成 {matchable_count} 套搭配"
            + (f"，可与 {partner_text} 搭配" if partners else "")
            + "，但组合偏少或与已有单品接近，建议确认穿着场景后再决定。",
        )
    return (
        "建议购买",
        f"重复度 {score:.2f}（{level}），{temp_text} 下能组成 {matchable_count} 套搭配"
        + (f"，可与 {partner_text} 搭配" if partners else "")
        + "，利用率较高。",
    )


@router.post("/evaluate", response_model=schemas.APIResponse)
def evaluate(req: schemas.PurchaseEvaluateRequest, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == req.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    target = _build_target(req.target_item)
    rows = db.query(ClothingItem).filter(ClothingItem.user_id == req.user_id).all()
    closet = [_closet_item(row) for row in rows]

    temp, temp_source = _resolve_temperature(req)
    season = normalize_season(req.season) or infer_season(int(target["warmth_level"]))
    occasion = (target.get("occasion") or ["通勤"])[0] or "通勤"

    engine = request.app.state.engine
    ctx = RecommendContext(
        user_id=req.user_id,
        occasion=occasion,
        weather_temp=temp,
        weather_rain=None,
        temp_pref={},
        constraints={},
        shown_outfit_ids=[],
        body_shape=user.body_shape or "匀称",
        long_term_prefs={
            "color": user.pref_color,
            "style": user.pref_style,
            "comfort": user.pref_comfort,
            "occasion": user.pref_occasion,
            "fabric": user.pref_fabric,
        },
    )

    # 枚举 + 硬过滤（与推荐链路同一套规则），再按「是否包含目标单品」分离
    all_items = closet + [target]
    outfits = engine._enumerate(all_items)
    legal = [o for o in outfits if engine._hard_filter(o, ctx)]
    with_target = [
        o for o in legal if any(it.get("item_id") == TARGET_ID for it in o["items"])
    ]
    closet_only = [
        o for o in legal if not any(it.get("item_id") == TARGET_ID for it in o["items"])
    ]

    # 目标单品缺席校验：只有真正包含它的搭配才计入 matchable_count
    for outfit in with_target:
        outfit["score"], outfit["score_details"] = engine._score(outfit, ctx)
    with_target.sort(key=lambda x: x["score"], reverse=True)
    top = engine._diversify(with_target, top_k=4)
    for outfit in top:
        outfit["reason"] = engine._generate_reason(outfit, ctx)
        outfit["outfit_id"] = outfit["fingerprint"]

    best_matches = []
    for outfit in top:
        payload = engine._to_response(outfit)
        payload["includes_target"] = True
        payload["items"] = [
            {**it, "is_target": it.get("item_id") == TARGET_ID}
            for it in payload["items"]
        ]
        payload["partners"] = [
            {"item_id": it["item_id"], "name": it["name"], "category": it["category"]}
            for it in outfit["items"]
            if it.get("item_id") != TARGET_ID
        ]
        best_matches.append(payload)

    partner_names: list[str] = []
    for outfit in top:
        for it in outfit["items"]:
            if it.get("item_id") != TARGET_ID and it["name"] not in partner_names:
                partner_names.append(it["name"])

    duplicate = duplicate_analysis(target, closet)
    nearest = duplicate.get("nearest_item") or {}
    self_check = _target_self_check(target, temp, occasion, season)

    matchable_count = len(with_target)
    suggestion_level, suggestion = _suggestion(
        duplicate,
        matchable_count,
        partner_names,
        temp,
        nearest.get("name"),
    )

    # 目标单品自身不合理时，结论必须显式降级，不能只说「搭配 0 套」
    if not self_check["passed"]:
        suggestion_level = "不建议购买"
        reasons = "；".join(v["message"] for v in self_check["violations"])
        suggestion = f"这件单品在当前条件下本身不合适：{reasons}。"

    closet_categories = {normalize_category(i["category"]) for i in closet}
    wardrobe_gap = [
        category for category in ("上装", "下装", "鞋")
        if category not in closet_categories
    ]

    result_data = {
        "can_match": matchable_count > 0,
        "matchable_count": matchable_count,
        "closet_only_count": len(closet_only),
        "matchable_count_meaning": "含目标单品的合法搭配套数（不受 Top 截断影响）",
        "partner_count": len(partner_names),
        "partners": partner_names[:6],
        "duplicate_score": duplicate["score"],
        "duplicate_level": duplicate["level"],
        "nearest_item": nearest or None,
        "shared_attributes": duplicate["shared_attributes"][:6],
        "same_category_count": duplicate["same_category_count"],
        "suggestion_level": suggestion_level,
        "suggestion": suggestion,
        "temperature": temp,
        "temperature_source": temp_source,
        "season": season,
        "occasion": occasion,
        "target_check": self_check,
        "wardrobe_gap": wardrobe_gap,
        "best_matches": best_matches,
        "rules_basis": {
            "duplicate": "K-ALG-05 Jaccard；≥0.7 高度重复，0.5~0.7 部分重复，<0.5 可新增",
            "suggestion": "K-PREF-03；建议购买=重复度<0.5 且 组合数≥3",
        },
    }

    record = PurchaseEvaluation(
        evaluation_id=utils.gen_id("e"),
        user_id=req.user_id,
        target_item=target,
        result=result_data,
    )
    db.add(record)
    db.commit()

    return ok(data={
        "evaluation_id": record.evaluation_id,
        "target_item": target,
        "result": result_data,
        "created_at": record.created_at.isoformat(),
    })


@router.get("/history", response_model=schemas.APIResponse)
def evaluate_history(
    user_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    q = db.query(PurchaseEvaluation).filter(PurchaseEvaluation.user_id == user_id)
    total = q.count()
    records = q.order_by(PurchaseEvaluation.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return ok(data={
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "evaluation_id": r.evaluation_id,
                "target_item": r.target_item,
                "result": r.result,
                "created_at": r.created_at.isoformat(),
            }
            for r in records
        ],
    })


@router.delete("/{evaluation_id}", response_model=schemas.APIResponse)
def delete_evaluation(
    evaluation_id: str,
    user_id: str,
    db: Session = Depends(get_db),
):
    record = (
        db.query(PurchaseEvaluation)
        .filter(
            PurchaseEvaluation.evaluation_id == evaluation_id,
            PurchaseEvaluation.user_id == user_id,
        )
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="评估记录不存在")
    db.delete(record)
    db.commit()
    return ok(data={"deleted": evaluation_id})
