# -*- coding: utf-8 -*-
"""用户模块（计划书 13.1）—— 极简注册/登录。

POST /user/register    输入昵称即注册/登录
GET  /user/profile     获取用户信息
PUT  /user/body        更新体型数据
GET  /user/preferences 获取长期偏好
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import schemas, utils
from app.api import ok
from app.db import User, get_db

router = APIRouter()


def _user_to_response(user: User) -> schemas.UserResponse:
    return schemas.UserResponse(
        user_id=user.user_id,
        nickname=user.nickname,
        avatar=user.avatar or "",
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
    """极简注册：昵称已存在则登录，不存在则新建用户。"""
    existing = db.query(User).filter(User.nickname == req.nickname).first()
    if existing:
        return ok(data=_user_to_response(existing))

    user = User(
        user_id=utils.gen_id("u"),
        nickname=req.nickname,
        avatar=req.avatar or "",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return ok(data=_user_to_response(user))


@router.get("/profile", response_model=schemas.APIResponse)
def get_profile(user_id: str, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
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
