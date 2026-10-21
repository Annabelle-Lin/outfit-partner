# -*- coding: utf-8 -*-
"""购前评估模块（计划书 13.6）。

POST /purchase/evaluate 新品评估（能否搭配 / 是否重复）
GET  /purchase/history  评估历史
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app import schemas, utils
from app.agent.engine import RecommendContext
from app.api import ok
from app.db import ClothingItem, PurchaseEvaluation, User, get_db

router = APIRouter()


@router.post("/evaluate", response_model=schemas.APIResponse)
def evaluate(req: schemas.PurchaseEvaluateRequest, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == req.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 拉取现有衣橱，把目标单品加入虚拟衣橱
    items = db.query(ClothingItem).filter(ClothingItem.user_id == req.user_id).all()
    target = req.target_item.model_dump()
    target["item_id"] = "target_item"
    item_dicts = [{
        "item_id": i.item_id,
        "name": i.name,
        "category": i.category,
        "sub_category": i.sub_category,
        "color": i.color,
        "color_brightness": i.color_brightness,
        "season": i.season,
        "fit_snug": i.fit_snug,
        "fit_silhouette": i.fit_silhouette,
        "fabric": i.fabric,
        "style": i.style,
        "occasion": i.occasion or [],
        "warmth_level": i.warmth_level,
        "formality_level": i.formality_level,
        "image_url": i.image_url,
        "tags": i.tags or [],
    } for i in items]
    item_dicts.append(target)

    # 计算重复度：颜色/品类/风格相似
    same_sub = sum(1 for i in items if i.sub_category == target["sub_category"])
    same_color = sum(1 for i in items if i.color == target["color"] and i.category == target["category"])
    duplicate_score = min(1.0, (same_sub * 0.3 + same_color * 0.2))

    # 用推荐引擎评估可搭配数
    engine = request.app.state.engine
    ctx = RecommendContext(
        user_id=req.user_id,
        occasion=target.get("occasion", ["通勤"])[0] if target.get("occasion") else "通勤",
        weather_temp=22.0,
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
    result = engine.recommend(ctx, item_dicts)
    outfits = result.get("outfits", [])

    suggestion = ""
    if outfits:
        if duplicate_score < 0.3:
            suggestion = f"可与衣橱中 {len(outfits)} 件单品组成 {len(outfits)} 套搭配，重复度低，建议购买。"
        elif duplicate_score < 0.7:
            suggestion = f"可与衣橱中 {len(outfits)} 件单品组成 {len(outfits)} 套搭配，但同类单品较多，建议考虑后再决定。"
        else:
            suggestion = "衣橱中已有类似单品，重复度较高，不建议重复购买。"
    else:
        suggestion = "当前衣橱难以与这件单品组成完整搭配，建议先补充基础单品。"

    result_data = {
        "can_match": bool(outfits),
        "matchable_count": len(outfits),
        "duplicate_score": round(duplicate_score, 2),
        "best_matches": outfits[:3],
        "suggestion": suggestion,
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
