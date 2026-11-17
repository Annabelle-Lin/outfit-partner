-- ============================================================
-- 对话式穿搭推理 Agent · SQLite 建表脚本
-- 依据 app/db.py 的 SQLAlchemy 模型反推生成
-- 说明：SQLite 为动态类型，VARCHAR/TEXT 等价存储文本；
--       JSON 列以 TEXT 存储，由 Pydantic 校验结构；
--       DateTime 由 SQLAlchemy 以 ISO8601 文本写入。
-- 运行：sqlite3 wardrobe.db < wardrobe_schema.sql
-- ============================================================

PRAGMA foreign_keys = ON;

-- ---------- 1. 用户表 ----------
CREATE TABLE IF NOT EXISTS users (
    user_id        VARCHAR PRIMARY KEY,
    nickname       VARCHAR NOT NULL,
    avatar         VARCHAR DEFAULT '',
    username       VARCHAR DEFAULT '',
    password_hash  VARCHAR DEFAULT '',
    body_shape     VARCHAR DEFAULT '',
    height_cm      INTEGER DEFAULT 0,
    weight_kg      INTEGER DEFAULT 0,
    shoulder_width VARCHAR DEFAULT '',
    waist          VARCHAR DEFAULT '',
    hip            VARCHAR DEFAULT '',
    city           VARCHAR DEFAULT '',
    gender         VARCHAR DEFAULT '',
    styles         TEXT DEFAULT '[]',
    pref_color     INTEGER DEFAULT 5,
    pref_style     INTEGER DEFAULT 5,
    pref_comfort   INTEGER DEFAULT 5,
    pref_occasion  INTEGER DEFAULT 5,
    pref_fabric    INTEGER DEFAULT 5,
    created_at     TEXT,
    updated_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_users_user_id ON users (user_id);
CREATE INDEX IF NOT EXISTS ix_users_username ON users (username);

-- ---------- 2. 衣物单品表 ----------
CREATE TABLE IF NOT EXISTS clothing_items (
    item_id          VARCHAR PRIMARY KEY,
    user_id          VARCHAR NOT NULL,
    name             VARCHAR NOT NULL,
    category         VARCHAR NOT NULL,
    sub_category     VARCHAR NOT NULL,
    color            VARCHAR DEFAULT '',
    color_brightness VARCHAR DEFAULT '',
    season           VARCHAR DEFAULT '四季',
    fit_snug         VARCHAR DEFAULT '',
    fit_silhouette   VARCHAR DEFAULT '',
    fabric           VARCHAR DEFAULT '',
    style            VARCHAR DEFAULT '',
    occasion         TEXT DEFAULT '[]',
    warmth_level     INTEGER DEFAULT 3,
    formality_level  INTEGER DEFAULT 3,
    image_url        VARCHAR DEFAULT '',
    tags             TEXT DEFAULT '[]',
    created_at       TEXT,
    updated_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_clothing_items_item_id ON clothing_items (item_id);
CREATE INDEX IF NOT EXISTS ix_clothing_items_user_id ON clothing_items (user_id);

-- ---------- 3. 对话会话表 ----------
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id        VARCHAR PRIMARY KEY,
    user_id           VARCHAR NOT NULL,
    status            VARCHAR DEFAULT 'active',
    occasion          VARCHAR DEFAULT '',
    weather_city      VARCHAR DEFAULT '',
    weather_temp      REAL DEFAULT 22.0,
    weather_rain      VARCHAR DEFAULT '未知',
    weather_rain_prob REAL,
    weather_source    VARCHAR DEFAULT '',
    temp_pref_color   VARCHAR DEFAULT '',
    temp_pref_style   VARCHAR DEFAULT '',
    constraints       TEXT DEFAULT '{}',
    shown_outfit_ids  TEXT DEFAULT '[]',
    remaining_count   INTEGER DEFAULT 15,
    created_at        TEXT,
    updated_at        TEXT
);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_session_id ON chat_sessions (session_id);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_user_id ON chat_sessions (user_id);

-- ---------- 4. 对话消息表（含唯一显式外键） ----------
CREATE TABLE IF NOT EXISTS chat_messages (
    message_id VARCHAR PRIMARY KEY,
    session_id VARCHAR NOT NULL,
    role       VARCHAR NOT NULL,
    action     VARCHAR DEFAULT 'free_text',
    content    VARCHAR DEFAULT '',
    outfits    TEXT DEFAULT '[]',
    payload    TEXT DEFAULT '{}',
    created_at TEXT,
    FOREIGN KEY (session_id) REFERENCES chat_sessions (session_id)
);
CREATE INDEX IF NOT EXISTS ix_chat_messages_message_id ON chat_messages (message_id);
CREATE INDEX IF NOT EXISTS ix_chat_messages_session_id ON chat_messages (session_id);

-- ---------- 5. 反馈记录表 ----------
CREATE TABLE IF NOT EXISTS feedback_records (
    record_id  VARCHAR PRIMARY KEY,
    user_id    VARCHAR NOT NULL,
    session_id VARCHAR DEFAULT '',
    outfit_id  VARCHAR DEFAULT '',
    rating     INTEGER DEFAULT 1,
    tags       TEXT DEFAULT '[]',
    comment    VARCHAR DEFAULT '',
    evidence   TEXT DEFAULT '{}',
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_feedback_records_record_id ON feedback_records (record_id);
CREATE INDEX IF NOT EXISTS ix_feedback_records_user_id ON feedback_records (user_id);

-- ---------- 6. 购前评估表 ----------
CREATE TABLE IF NOT EXISTS purchase_evaluations (
    evaluation_id VARCHAR PRIMARY KEY,
    user_id       VARCHAR NOT NULL,
    target_item   TEXT DEFAULT '{}',
    result        TEXT DEFAULT '{}',
    created_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_purchase_evaluations_evaluation_id ON purchase_evaluations (evaluation_id);
CREATE INDEX IF NOT EXISTS ix_purchase_evaluations_user_id ON purchase_evaluations (user_id);

-- ---------- 7. 搭配方案记录表 ----------
CREATE TABLE IF NOT EXISTS outfit_records (
    outfit_id      VARCHAR PRIMARY KEY,
    user_id        VARCHAR NOT NULL,
    session_id     VARCHAR NOT NULL,
    items          TEXT DEFAULT '[]',
    score          REAL DEFAULT 0.0,
    reason_summary VARCHAR DEFAULT '',
    reason_points  TEXT DEFAULT '[]',
    is_diff        INTEGER DEFAULT 1,
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_outfit_records_outfit_id ON outfit_records (outfit_id);
CREATE INDEX IF NOT EXISTS ix_outfit_records_user_id ON outfit_records (user_id);
CREATE INDEX IF NOT EXISTS ix_outfit_records_session_id ON outfit_records (session_id);
