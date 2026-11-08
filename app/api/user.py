# -*- coding: utf-8 -*-
"""用户模块（计划书 13.1）—— 真实的注册 / 登录 / 资料。

POST /user/register            注册新账号（账号+密码），返回登录 token
POST /user/login               老用户登录，返回登录 token 与自己的全部资料
GET  /user/username_available  注册前检查账号是否可用
GET  /user/profile             获取用户信息
PUT  /user/profile             更新资料
PUT  /user/body                更新体型数据
GET  /user/preferences         获取长期偏好

登录态：除 register/login/username_available 外，全部接口都需要
`Authorization: Bearer <token>`，token 由 app.security 签发与校验。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import schemas, security, utils
from app.api import ok
from app.db import (
    ChatMessage,
    ChatSession,
    ClothingItem,
    FeedbackRecord,
    OutfitRecord,
    PurchaseEvaluation,
    User,
    get_db,
)

router = APIRouter()


def _user_to_response(user: User) -> schemas.UserResponse:
    return schemas.UserResponse(
        user_id=user.user_id,
        nickname=user.nickname,
        avatar=user.avatar or "",
        city=user.city or "",
        gender=user.gender or "",
        styles=list(user.styles or []),
        body=schemas.BodyInfo(
            shape=user.body_shape or "",
            height_cm=user.height_cm or 0,
            weight_kg=user.weight_kg or 0,
            shoulder_width=user.shoulder_width or "",
            waist=user.waist or "",
            hip=user.hip or "",
        ),
        preferences=schemas.Preferences(
            color=user.pref_color or 5,
            style=user.pref_style or 5,
            comfort=user.pref_comfort or 5,
            occasion=user.pref_occasion or 5,
            fabric=user.pref_fabric or 5,
        ),
    )


@router.post("/register", response_model=schemas.APIResponse)
def register(req: schemas.UserRegisterRequest, db: Session = Depends(get_db)):
    """注册新账号：账号唯一，密码加盐摘要存储，注册成功即返回登录凭证。

    账号规则：显式传 username 用它，否则用昵称作为登录账号。
    """
    username = (req.username or req.nickname or "").strip()
    if len(username) < 2:
        raise HTTPException(status_code=400, detail="账号至少 2 个字符")
    if len(req.password or "") < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")

    # 账号唯一性校验（用户名+密码是登录凭据，不允许重复）
    dup = db.query(User).filter(User.username == username).first()
    if dup:
        raise HTTPException(status_code=409, detail="该账号已被注册，请更换账号或直接登录")

    user = User(
        user_id=utils.gen_id("u"),
        nickname=req.nickname,
        avatar=req.avatar or "",
        username=username,
        password_hash=security.hash_password(req.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return ok(data={
        "token": security.issue_token(user.user_id),
        "user": _user_to_response(user).model_dump(),
        "is_new": True,
    })


@router.post("/login", response_model=schemas.APIResponse)
def login(req: schemas.UserLoginRequest, db: Session = Depends(get_db)):
    """老用户登录：校验账号密码，成功后下发 token 并带出该账号的全部内容。"""
    username = (req.username or "").strip()
    user = db.query(User).filter(User.username == username).first()
    if not user or not security.verify_password(req.password, user.password_hash or ""):
        # 统一提示，避免暴露账号是否存在
        raise HTTPException(status_code=401, detail="账号或密码错误")

    clothing_count = db.query(ClothingItem).filter(ClothingItem.user_id == user.user_id).count()
    feedback_count = db.query(FeedbackRecord).filter(FeedbackRecord.user_id == user.user_id).count()
    eval_count = db.query(PurchaseEvaluation).filter(PurchaseEvaluation.user_id == user.user_id).count()
    session_count = db.query(ChatSession).filter(ChatSession.user_id == user.user_id).count()

    return ok(data={
        "token": security.issue_token(user.user_id),
        "user": _user_to_response(user).model_dump(),
        "is_new": False,
        "stats": {
            "clothing": clothing_count,
            "feedback": feedback_count,
            "evaluation": eval_count,
            "session": session_count,
        },
    })


@router.get("/username_available", response_model=schemas.APIResponse)
def username_available(username: str, db: Session = Depends(get_db)):
    """注册前检查账号是否可用。"""
    name = (username or "").strip()
    if len(name) < 2:
        return ok(data={"username": name, "available": False, "reason": "账号至少 2 个字符"})
    exists = db.query(User).filter(User.username == name).first() is not None
    return ok(data={
        "username": name,
        "available": not exists,
        "reason": "" if not exists else "该账号已被注册",
    })


@router.get("/profile", response_model=schemas.APIResponse)
def get_profile(user_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return ok(data=_user_to_response(user))


@router.put("/profile", response_model=schemas.APIResponse)
def update_profile(req: schemas.UserProfileUpdate, db: Session = Depends(get_db)):
    """更新用户资料：昵称 / 头像 / 城市 / 性别 / 常用风格 / 体型 / 长期偏好。

    所有字段可选，只写入本次传入的字段；返回更新后的完整用户信息。
    """
    user = db.query(User).filter(User.user_id == req.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    if req.nickname is not None:
        user.nickname = req.nickname
    if req.avatar is not None:
        user.avatar = req.avatar
    if req.city is not None:
        user.city = req.city
    if req.gender is not None:
        user.gender = req.gender
    if req.styles is not None:
        user.styles = list(req.styles)

    if req.body is not None:
        b = req.body
        user.body_shape = b.shape
        user.height_cm = b.height_cm
        user.weight_kg = b.weight_kg
        user.shoulder_width = b.shoulder_width
        user.waist = b.waist
        user.hip = b.hip

    if req.preferences is not None:
        p = req.preferences
        if p.color is not None:
            user.pref_color = p.color
        if p.style is not None:
            user.pref_style = p.style
        if p.comfort is not None:
            user.pref_comfort = p.comfort
        if p.occasion is not None:
            user.pref_occasion = p.occasion
        if p.fabric is not None:
            user.pref_fabric = p.fabric

    db.commit()
    db.refresh(user)
    return ok(data=_user_to_response(user))


@router.put("/body", response_model=schemas.APIResponse)
def update_body(req: schemas.UpdateBodyRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == req.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    body = req.body
    user.body_shape = body.shape
    user.height_cm = body.height_cm
    user.weight_kg = body.weight_kg
    user.shoulder_width = body.shoulder_width
    user.waist = body.waist
    user.hip = body.hip
    db.commit()
    db.refresh(user)
    return ok(data=_user_to_response(user))


@router.post("/preferences/reset", response_model=schemas.APIResponse)
def reset_preferences(user_id: str, db: Session = Depends(get_db)):
    """把五个长期偏好维度重置为默认中值 5（用户画像回到中性）。

    注意：只重置偏好，不删除衣物和反馈历史。
    """
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    user.pref_color = 5
    user.pref_style = 5
    user.pref_comfort = 5
    user.pref_occasion = 5
    user.pref_fabric = 5
    db.commit()
    db.refresh(user)
    return ok(data=schemas.Preferences(
        color=user.pref_color,
        style=user.pref_style,
        comfort=user.pref_comfort,
        occasion=user.pref_occasion,
        fabric=user.pref_fabric,
    ).model_dump())


@router.delete("/data", response_model=schemas.APIResponse)
def delete_user_data(user_id: str, scope: str = "all", db: Session = Depends(get_db)):
    """清空当前用户的数据。

    scope:
      - all        全部（衣物、反馈、评估、方案、会话与消息），保留账号与偏好
      - clothing   仅衣橱
      - history    仅反馈 / 评估 / 会话记录
    """
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    if scope not in ("all", "clothing", "history"):
        raise HTTPException(status_code=400, detail="scope 只能是 all / clothing / history")

    removed = {}

    def _count_and_delete(model, **filters):
        q = db.query(model).filter_by(**filters)
        n = q.count()
        q.delete(synchronize_session=False)
        return n

    if scope in ("all", "clothing"):
        removed["clothing"] = _count_and_delete(ClothingItem, user_id=user_id)

    if scope in ("all", "history"):
        # 会话消息依附于会话，先删消息再删会话
        session_ids = [s.session_id for s in db.query(ChatSession).filter_by(user_id=user_id).all()]
        msg_n = 0
        if session_ids:
            msg_n = db.query(ChatMessage).filter(ChatMessage.session_id.in_(session_ids)).count()
            db.query(ChatMessage).filter(ChatMessage.session_id.in_(session_ids)).delete(synchronize_session=False)
        removed["chat_messages"] = msg_n
        removed["chat_sessions"] = _count_and_delete(ChatSession, user_id=user_id)
        removed["feedback"] = _count_and_delete(FeedbackRecord, user_id=user_id)
        removed["evaluations"] = _count_and_delete(PurchaseEvaluation, user_id=user_id)
        removed["outfits"] = _count_and_delete(OutfitRecord, user_id=user_id)

    db.commit()
    return ok(data={"scope": scope, "removed": removed})


@router.get("/preferences", response_model=schemas.APIResponse)
def get_preferences(user_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    return ok(data=schemas.Preferences(
        color=user.pref_color,
        style=user.pref_style,
        comfort=user.pref_comfort,
        occasion=user.pref_occasion,
        fabric=user.pref_fabric,
    ).model_dump())
