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

    # 一期：把用户手选的标签与「自由文本解析出的标签」合并。
    # 手选优先、去重；解析结果单独留在 evidence.auto_tags 里，便于回溯是哪来的。
    manual_tags = [t for t in (req.tags or []) if isinstance(t, str) and t.strip()]
    auto_tags = [t for t in utils.parse_comment_to_tags(req.comment) if t not in manual_tags]
    tags = manual_tags + auto_tags

    record = FeedbackRecord(
        record_id=utils.gen_id("r"),
        user_id=req.user_id,
        session_id=req.session_id,
        outfit_id=req.outfit_id,
        rating=req.rating,
        tags=tags,
        comment=req.comment,
    )
    db.add(record)

    # 2.3 记下这条反馈到底改了什么，撤销时才能反向回退
    # 注意：delta 必须在 tags 合并之后计算，否则标签信息进不了偏好。
    delta = utils.preference_delta(req.rating, tags)

    # 动态画像：从被评价搭配的单品抽具体属性值，按喜欢/不喜欢增减权重。
    # 前端没带 item_ids 时跳过（老前端兼容），属性为空时也不写空壳。
    from app.db import ClothingItem
    profile_attrs: set[str] = set()
    if req.item_ids:
        rows = (db.query(ClothingItem)
                .filter(ClothingItem.item_id.in_(req.item_ids),
                        ClothingItem.user_id == req.user_id).all())
        profile_attrs = utils.profile_attrs([{
            "category": r.category, "sub_category": r.sub_category,
            "color": r.color, "fabric": r.fabric, "style": r.style,
            "fit_snug": r.fit_snug, "fit_silhouette": r.fit_silhouette,
            "warmth_level": r.warmth_level,
        } for r in rows])
    # rating 2=喜欢→+1，0=不喜欢→−1，1=一般→不动画像
    profile_step = {2: 1, 0: -1}.get(req.rating, 0)
    profile_delta = None
    if profile_attrs and profile_step:
        before = dict(user.profile_prefs or {})
        after = utils.apply_profile_delta(before, profile_attrs, profile_step)
        user.profile_prefs = after
        profile_delta = {
            "attrs": sorted(profile_attrs), "step": profile_step,
            "before": before, "after": after,
        }

    record.evidence = {
        "delta": delta,
        "rating": req.rating,
        "tags": tags,
        "manual_tags": manual_tags,
        "auto_tags": auto_tags,
        "comment": req.comment,
        "profile_delta": profile_delta,
    }
    db.flush()

    # 2.3 改为「按全部反馈重算」，保证幂等、可撤销
    prefs = _apply_recompute(db, user)

    db.commit()
    return ok(data={
        "record_id": record.record_id,
        "evidence": record.evidence,
        "preferences": prefs,
        "profile": utils.effective_profile(user.profile_prefs or {}),
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
                     include_undone: bool = True,
                     db: Session = Depends(get_db)):
    """2.3 「最近影响我的反馈」：每条反馈改了什么，可逐条撤销。

    历史记录页传 include_undone=false 只列仍生效的评价；个人资料的证据链
    默认保留已撤销记录，便于回看当初改了什么。
    """
    security.assert_owner(user_id, request)

    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    rows = (db.query(FeedbackRecord)
              .filter(FeedbackRecord.user_id == user_id)
              .order_by(FeedbackRecord.created_at.desc())
              .all())
    if not include_undone:
        rows = [r for r in rows if not (r.evidence or {}).get("undone")]
    rows = rows[:min(max(1, limit), 50)]

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
    # 动态画像回退：用证据里存的 before 快照精确还原，
    # 不用反向扣 step（反向扣会被后续其它反馈的累积干扰）。
    pd = evidence.get("profile_delta")
    if pd and pd.get("before") is not None:
        user.profile_prefs = pd["before"]
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
