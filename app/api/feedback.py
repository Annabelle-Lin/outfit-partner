# -*- coding: utf-8 -*-
"""反馈模块（计划书 13.5）。

POST /feedback         提交反馈（喜欢/一般/不喜欢 -> 长期偏好分数更新）
GET  /api/v1/feedback/history 反馈历史
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app import schemas, security, utils
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

    # 2.3 记下这条反馈到底改了什么，撤销时才能反向回退
    delta = utils.preference_delta(req.rating, req.tags)
    record.evidence = {
        "delta": delta,
        "rating": req.rating,
        "tags": req.tags,
        "comment": req.comment,
    }
    db.flush()

    # 2.3 改为「按全部反馈重算」，保证幂等、可撤销
    prefs = _apply_recompute(db, user)

    db.commit()
    return ok(data={
        "record_id": record.record_id,
        "evidence": record.evidence,
        "preferences": prefs,
    })


def _apply_recompute(db: Session, user: User) -> dict:
    """按该用户全部反馈重算 5 维偏好并写回用户。"""
    rows = db.query(FeedbackRecord).filter(FeedbackRecord.user_id == user.user_id).all()
    records = [{"evidence": r.evidence or {}} for r in rows]
    prefs = utils.recompute_preferences(records)
    user.pref_color = prefs["color"]
    user.pref_style = prefs["style"]
    user.pref_comfort = prefs["comfort"]
    user.pref_occasion = prefs["occasion"]
    user.pref_fabric = prefs["fabric"]
    return prefs


def _current_prefs(user: User) -> dict:
    return {
        "color": user.pref_color,
        "style": user.pref_style,
        "comfort": user.pref_comfort,
        "occasion": user.pref_occasion,
        "fabric": user.pref_fabric,
    }


@router.get("/effects", response_model=schemas.APIResponse)
def feedback_effects(user_id: str, request: Request, limit: int = 10,
                     db: Session = Depends(get_db)):
    """2.3 「最近影响我的反馈」：每条反馈改了什么，可逐条撤销。"""
    security.assert_owner(user_id, request)

    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    rows = (db.query(FeedbackRecord)
              .filter(FeedbackRecord.user_id == user_id)
              .order_by(FeedbackRecord.created_at.desc())
              .limit(min(max(1, limit), 50))
              .all())

    return ok(data={
        "preferences": _current_prefs(user),
        "items": [{
            "record_id": r.record_id,
            "rating": r.rating,
            "tags": r.tags or [],
            "comment": r.comment or "",
            "delta": (r.evidence or {}).get("delta") or {},
            "undone": bool((r.evidence or {}).get("undone")),
            "undone_at": (r.evidence or {}).get("undone_at"),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "outfit_id": r.outfit_id or "",
        } for r in rows],
    })


@router.post("/{record_id}/undo", response_model=schemas.APIResponse)
def undo_feedback(record_id: str, user_id: str, request: Request,
                  db: Session = Depends(get_db)):
    """2.3 撤销一条反馈：删除记录 + 重算偏好，分数精确回退。"""
    security.assert_owner(user_id, request)

    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    rec = (db.query(FeedbackRecord)
             .filter(FeedbackRecord.record_id == record_id,
                     FeedbackRecord.user_id == user_id)
             .first())
    if not rec:
        raise HTTPException(status_code=404, detail="反馈记录不存在")

    before = _current_prefs(user)
    evidence = dict(rec.evidence or {})
    if evidence.get("undone"):
        raise HTTPException(status_code=404, detail="这条反馈已撤销，不能重复撤销")

    # 保留反馈和增量作为证据链；重算偏好时跳过已撤销记录。
    evidence["undone"] = True
    evidence["undone_at"] = utils.now_str()
    rec.evidence = evidence
    db.flush()
    after = _apply_recompute(db, user)
    db.commit()

    return ok(data={
        "undone": record_id,
        "preferences_before": before,
        "preferences": after,
        "restored": {k: after[k] - before[k] for k in utils.PREF_KEYS},
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
