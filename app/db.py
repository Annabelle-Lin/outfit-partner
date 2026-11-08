# -*- coding: utf-8 -*-
"""数据库层：SQLite + SQLAlchemy。

- 7 张表对应计划书 8.2 节数据模型
- 启动时自动建表
- JSON 字段统一用 JSON 字符串存储，Pydantic 模型负责校验
"""
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, Float, ForeignKey, Integer, String, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker

from app import config

Base = declarative_base()
engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {},
    echo=False,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    """FastAPI Depends 用生成器。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class User(Base):
    """用户表。"""

    __tablename__ = "users"

    user_id = Column(String, primary_key=True, index=True)
    nickname = Column(String, nullable=False)
    avatar = Column(String, default="")

    # 登录凭证：username 唯一（应用层保证），password_hash 为不可逆摘要
    username = Column(String, index=True, default="")
    password_hash = Column(String, default="")

    # 体型
    body_shape = Column(String, default="")
    height_cm = Column(Integer, default=0)
    weight_kg = Column(Integer, default=0)
    shoulder_width = Column(String, default="")
    waist = Column(String, default="")
    hip = Column(String, default="")

    # 资料扩展：常用城市 / 性别 / 常用风格（多选，JSON 数组）
    city = Column(String, default="")
    gender = Column(String, default="")
    styles = Column(JSON, default=list)

    # 长期偏好 1-10
    pref_color = Column(Integer, default=5)
    pref_style = Column(Integer, default=5)
    pref_comfort = Column(Integer, default=5)
    pref_occasion = Column(Integer, default=5)
    pref_fabric = Column(Integer, default=5)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ClothingItem(Base):
    """衣物单品表。"""

    __tablename__ = "clothing_items"

    item_id = Column(String, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=False)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)  # 上装/下装/外套/鞋/连衣裙/配饰
    sub_category = Column(String, nullable=False)

    color = Column(String, default="")
    color_brightness = Column(String, default="")
    season = Column(String, default="四季")
    fit_snug = Column(String, default="")
    fit_silhouette = Column(String, default="")
    fabric = Column(String, default="")
    style = Column(String, default="")
    occasion = Column(JSON, default=list)  # ["通勤", "上课"]

    warmth_level = Column(Integer, default=3)  # 1-5
    formality_level = Column(Integer, default=3)  # 1-5

    image_url = Column(String, default="")
    tags = Column(JSON, default=list)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatSession(Base):
    """对话会话表。"""

    __tablename__ = "chat_sessions"

    session_id = Column(String, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=False)
    status = Column(String, default="active")  # active / ended

    occasion = Column(String, default="")
    weather_city = Column(String, default="")
    weather_temp = Column(Float, default=22.0)
    weather_rain = Column(String, default="未知")
    # 降雨概率（0-100）：比天气文案更可靠，用于判断是否需要避雨鞋履
    weather_rain_prob = Column(Float, nullable=True)
    weather_source = Column(String, default="")  # passed / realtime / unavailable

    temp_pref_color = Column(String, default="")  # 亮色/暗色
    temp_pref_style = Column(String, default="")  # 基础款/设计款

    constraints = Column(JSON, default=dict)  # {exclude_colors: [], replace: []}
    shown_outfit_ids = Column(JSON, default=list)
    remaining_count = Column(Integer, default=10)

    messages = relationship("ChatMessage", back_populates="session", lazy="selectin", order_by="ChatMessage.created_at")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ChatMessage(Base):
    """对话消息表。"""

    __tablename__ = "chat_messages"

    message_id = Column(String, primary_key=True, index=True)
    session_id = Column(String, ForeignKey("chat_sessions.session_id"), index=True, nullable=False)
    role = Column(String, nullable=False)  # user / agent
    action = Column(String, default="free_text")
    content = Column(String, default="")
    outfits = Column(JSON, default=list)  # [outfit_id, ...]
    payload = Column(JSON, default=dict)  # 原始请求/响应载荷

    session = relationship("ChatSession", back_populates="messages")

    created_at = Column(DateTime, default=datetime.utcnow)


class FeedbackRecord(Base):
    """反馈记录表。"""

    __tablename__ = "feedback_records"

    record_id = Column(String, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=False)
    session_id = Column(String, index=True, default="")
    outfit_id = Column(String, default="")
    rating = Column(Integer, default=1)  # 2=喜欢 1=一般 0=不喜欢
    tags = Column(JSON, default=list)
    comment = Column(String, default="")
    # 2.3 偏好可撤销：记录这条反馈对 5 维偏好造成的增量，撤销时可精确回退
    evidence = Column(JSON, default=dict)

    created_at = Column(DateTime, default=datetime.utcnow)


class PurchaseEvaluation(Base):
    """购前评估记录表。"""

    __tablename__ = "purchase_evaluations"

    evaluation_id = Column(String, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=False)
    target_item = Column(JSON, default=dict)
    result = Column(JSON, default=dict)

    created_at = Column(DateTime, default=datetime.utcnow)


class OutfitRecord(Base):
    """搭配方案记录表（用于会话内去重与历史回溯）。"""

    __tablename__ = "outfit_records"

    outfit_id = Column(String, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=False)
    session_id = Column(String, index=True, nullable=False)
    items = Column(JSON, default=list)  # [{item_id, category, name, image_url}]
    score = Column(Float, default=0.0)
    reason_summary = Column(String, default="")
    reason_points = Column(JSON, default=list)
    is_diff = Column(Integer, default=1)  # 0/1

    created_at = Column(DateTime, default=datetime.utcnow)


def _ensure_user_columns():
    """轻量迁移：给已存在的 users 表补齐新增列（SQLite 不支持 ADD COLUMN IF NOT EXISTS）。"""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("users")}
    wanted = {
        "city": "VARCHAR",
        "gender": "VARCHAR",
        "styles": "JSON",
        "username": "VARCHAR",
        "password_hash": "VARCHAR",
    }
    with engine.begin() as conn:
        for name, coltype in wanted.items():
            if name not in existing:
                conn.execute(text(f'ALTER TABLE users ADD COLUMN {name} {coltype}'))


def _ensure_session_columns():
    """轻量迁移：给已存在的 chat_sessions 表补齐新增列。"""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "chat_sessions" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("chat_sessions")}
    wanted = {
        "weather_rain_prob": "FLOAT",
    }
    with engine.begin() as conn:
        for name, coltype in wanted.items():
            if name not in existing:
                conn.execute(text(f'ALTER TABLE chat_sessions ADD COLUMN {name} {coltype}'))


def init_db():
    """启动时建表，并补齐历史库缺失的新列。"""
    Base.metadata.create_all(bind=engine)
    _ensure_user_columns()
    _ensure_session_columns()

# ---------- 轻量 schema 迁移（2.3 新增 evidence 列） ----------
def _ensure_columns() -> None:
    """老库补列。create_all 不会给已存在的表加列，这里显式 ALTER 一次。"""
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            rows = [r for r in conn.execute(text("PRAGMA table_info(feedback_records)"))]
            cols = [r[1] for r in rows]
            if cols and "evidence" not in cols:
                conn.execute(text("ALTER TABLE feedback_records ADD COLUMN evidence JSON"))
                conn.commit()
            rows = [r for r in conn.execute(text("PRAGMA table_info(chat_sessions)"))]
            scols = [r[1] for r in rows]
            if scols and "weather_source" not in scols:
                conn.execute(text("ALTER TABLE chat_sessions ADD COLUMN weather_source VARCHAR DEFAULT ''"))
                conn.commit()
    except Exception:
        pass


_ensure_columns()
