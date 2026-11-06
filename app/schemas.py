# -*- coding: utf-8 -*-
"""Pydantic 数据模型：请求体验证 + 响应序列化。

所有 API 出入结构都在这里定义，与数据库模型解耦。
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ---------- 通用 ----------
class APIResponse(BaseModel):
    code: int = 0
    msg: str = "ok"
    data: Any = None


# ---------- 用户 ----------
class BodyInfo(BaseModel):
    shape: str = ""
    height_cm: int = 0
    weight_kg: int = 0
    shoulder_width: str = ""
    waist: str = ""
    hip: str = ""


class Preferences(BaseModel):
    color: int = 5
    style: int = 5
    comfort: int = 5
    occasion: int = 5
    fabric: int = 5


class UserRegisterRequest(BaseModel):
    nickname: str = Field(..., min_length=2, max_length=20)
    avatar: str = ""
    username: str | None = Field(default=None, max_length=32, description="登录账号，留空则用昵称")
    password: str = Field(..., min_length=6, max_length=64, description="登录密码，至少 6 位")


class UserLoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=32)
    password: str = Field(..., min_length=1, max_length=64)


class UserResponse(BaseModel):
    user_id: str
    nickname: str
    avatar: str = ""
    city: str = ""
    gender: str = ""
    styles: list[str] = Field(default_factory=list)
    body: BodyInfo
    preferences: Preferences


class UpdateBodyRequest(BaseModel):
    user_id: str
    body: BodyInfo


class PreferencesUpdate(BaseModel):
    """长期偏好（1-10），只传需要改的项。"""

    color: int | None = Field(default=None, ge=1, le=10)
    style: int | None = Field(default=None, ge=1, le=10)
    comfort: int | None = Field(default=None, ge=1, le=10)
    occasion: int | None = Field(default=None, ge=1, le=10)
    fabric: int | None = Field(default=None, ge=1, le=10)


class UserProfileUpdate(BaseModel):
    """资料更新：全部字段可选，只更新传入的字段。

    PUT /user/profile 的请求体。前端「编辑资料」弹窗提交这里。
    """

    user_id: str
    nickname: str | None = Field(default=None, min_length=2, max_length=20)
    avatar: str | None = None
    city: str | None = Field(default=None, max_length=20)
    gender: str | None = Field(default=None, max_length=10)
    styles: list[str] | None = None
    body: BodyInfo | None = None
    preferences: PreferencesUpdate | None = None


# ---------- 衣物 ----------
class ClothingItemCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=50)
    category: str
    sub_category: str
    color: str = ""
    color_brightness: str = ""
    season: str = "四季"
    fit_snug: str = ""
    fit_silhouette: str = ""
    fabric: str = ""
    style: str = ""
    occasion: list[str] = Field(default_factory=list)
    warmth_level: int = 3
    formality_level: int = 3
    image_url: str = ""
    tags: list[str] = Field(default_factory=list)


class ClothingItemUpdate(BaseModel):
    name: str | None = None
    category: str | None = None
    sub_category: str | None = None
    color: str | None = None
    color_brightness: str | None = None
    season: str | None = None
    fit_snug: str | None = None
    fit_silhouette: str | None = None
    fabric: str | None = None
    style: str | None = None
    occasion: list[str] | None = None
    warmth_level: int | None = None
    formality_level: int | None = None
    image_url: str | None = None
    tags: list[str] | None = None


class ClothingItemResponse(ClothingItemCreate):
    item_id: str
    user_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ClothingListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[ClothingItemResponse]


# ---------- 天气 ----------
class WeatherInfo(BaseModel):
    city: str
    # 允许为空：前端拿不到真实天气时不传天气，由后端按常用城市自行获取
    temp: float | None = None
    rain: str = "未知"
    # 降雨概率（0-100），来自天气接口，用于判断是否需要避雨鞋履
    rain_probability: float | None = None
    source: str = "mock"


# ---------- 对话 ----------
class TempPref(BaseModel):
    color: str = ""  # 亮色 / 暗色
    style: str = ""  # 基础款 / 设计款


class ChatSessionCreate(BaseModel):
    user_id: str
    occasion: str = ""
    weather: WeatherInfo | None = None
    temp_pref: TempPref | None = None


class ReasonPoint(BaseModel):
    type: str
    text: str
    rule_id: str


class OutfitSummary(BaseModel):
    item_id: str
    category: str
    name: str
    image_url: str = ""


class OutfitResponse(BaseModel):
    outfit_id: str
    items: list[OutfitSummary]
    score: float
    reason: dict[str, Any]
    is_diff: bool


class ChatMessagePayload(BaseModel):
    action: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ChatMessageResponse(BaseModel):
    message_id: str
    role: str
    action: str
    content: str
    outfits: list[OutfitResponse] = Field(default_factory=list)
    remaining_count: int = 10
    constraints: dict[str, Any] | None = None


class ChatSessionResponse(BaseModel):
    session_id: str
    user_id: str
    status: str
    context: dict[str, Any]
    messages: list[dict[str, Any]]
    remaining_count: int
    created_at: datetime


# ---------- 反馈 ----------
class FeedbackSubmit(BaseModel):
    user_id: str
    session_id: str = ""
    outfit_id: str = ""
    rating: int = Field(..., ge=0, le=2)
    tags: list[str] = Field(default_factory=list)
    comment: str = ""


# ---------- 购前评估 ----------
class TargetItem(BaseModel):
    """拟购单品。

    补齐 fabric/fit_snug/fit_silhouette/warmth_level/formality_level/season：
    硬过滤（暖度、季节、场合、三色）需要这些判断依据，缺字段时
    引擎只能拿默认值，规则会静默失效。
    """

    name: str
    category: str
    sub_category: str = ""
    color: str = ""
    style: str = ""
    occasion: list[str] = Field(default_factory=list)
    fabric: str = ""
    fit_snug: str = ""
    fit_silhouette: str = ""
    # 1 最薄 - 5 最厚；留空时按子品类推断，并在结果里标注来源
    warmth_level: int | None = None
    formality_level: int | None = None
    season: str = ""


class PurchaseEvaluateRequest(BaseModel):
    user_id: str
    target_item: TargetItem
    # 评估所处的季节与气温：不传则按季节默认值估算，并如实标注来源
    season: str = ""
    temperature: float | None = None
    city: str = ""
