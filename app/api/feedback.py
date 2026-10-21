# -*- coding: utf-8 -*-
"""反馈模块（计划书 13.5）。

POST /feedback         提交反馈（喜欢/一般/不喜欢 -> 长期偏好分数更新）
GET  /api/v1/feedback/history 反馈历史
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import schemas, utils
from app.api import ok
from app.db import FeedbackRecord, User, get_db

router = APIRouter()


@router.post("", response_model=schemas.APIResponse)
def submit_feedback(req: schemas.FeedbackSubmit, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == req.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    record = FeedbackRecord(
        record_id=utils.gen_id("r"),
        user_id=req.user_id,
        session_id=req.session_id,
        outfit_id=req.outfit_id,
        rating=req.rating,
        tags=req.tags,
        comment=req.comment,
    )
    db.add(record)

    # 更新长期偏好
    prefs = {
        "color": user.pref_color,
        "style": user.pref_style,
        "comfort": user.pref_comfort,
        "occasion": user.pref_occasion,
        "fabric": user.pref_fabric,
    }
    prefs = utils.update_preferences(prefs, req.rating, req.tags)
    user.pref_color = prefs["color"]
    user.pref_style = prefs["style"]
    user.pref_comfort = prefs["comfort"]
    user.pref_occasion = prefs["occasion"]
    user.pref_fabric = prefs["fabric"]

    db.commit()
    return ok(data={
        "record_id": record.record_id,
        "preferences": prefs,
    })


@router.get("/history", response_model=schemas.APIResponse)
def feedback_history(
    user_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    q = db.query(FeedbackRecord).filter(FeedbackRecord.user_id == user_id)
    total = q.count()
    records = q.order_by(FeedbackRecord.created_at.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return ok(data={
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "record_id": r.record_id,
                "session_id": r.session_id,
                "outfit_id": r.outfit_id,
                "rating": r.rating,
                "tags": r.tags,
                "comment": r.comment,
                "created_at": r.created_at.isoformat(),
            }
            for r in records
        ],
    })
