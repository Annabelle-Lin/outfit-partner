# ORM 映射表

> 本表说明 ORM 实体类、数据库表与字段之间的对应关系。实体类全部定义于 `app/db.py`，继承统一的 `Base`；请求与响应侧的 Pydantic 模型定义于 `app/schemas.py`，与持久化模型解耦。

## 一、实体类与表的总体映射

| ORM 实体类 | 物理表名 | 主键字段 | 关系（relationship） |
|---|---|---|---|
| User | users | user_id | 无显式 relationship |
| ClothingItem | clothing_items | item_id | 无 |
| ChatSession | chat_sessions | session_id | messages → ChatMessage，一对多 |
| ChatMessage | chat_messages | message_id | session → ChatSession，多对一 |
| FeedbackRecord | feedback_records | record_id | 无 |
| PurchaseEvaluation | purchase_evaluations | evaluation_id | 无 |
| OutfitRecord | outfit_records | outfit_id | 无 |

## 二、字段级映射

ORM 类属性名与物理列名在本项目中保持一致，下表列出类型映射关系，逐表对照。

### User → users

| 类属性 | 物理列 | ORM 列类型 |
|---|---|---|
| user_id | user_id | String，primary_key |
| nickname | nickname | String，nullable=False |
| avatar / username / password_hash | 同名 | String |
| body_shape / shoulder_width / waist / hip / city / gender | 同名 | String |
| height_cm / weight_kg | 同名 | Integer |
| styles | styles | JSON |
| pref_color / pref_style / pref_comfort / pref_occasion / pref_fabric | 同名 | Integer |
| created_at / updated_at | 同名 | DateTime |

### ClothingItem → clothing_items

| 类属性 | 物理列 | ORM 列类型 |
|---|---|---|
| item_id | item_id | String，primary_key |
| user_id / name / category / sub_category | 同名 | String |
| color / color_brightness / season / fit_snug / fit_silhouette / fabric / style | 同名 | String |
| occasion / tags | 同名 | JSON |
| warmth_level / formality_level | 同名 | Integer |
| image_url | image_url | String |
| created_at / updated_at | 同名 | DateTime |

### ChatSession → chat_sessions

| 类属性 | 物理列 | ORM 列类型 |
|---|---|---|
| session_id | session_id | String，primary_key |
| user_id / status / occasion / weather_city / weather_rain / weather_source | 同名 | String |
| weather_temp / weather_rain_prob | 同名 | Float |
| temp_pref_color / temp_pref_style | 同名 | String |
| constraints / shown_outfit_ids | 同名 | JSON |
| remaining_count | remaining_count | Integer |
| created_at / updated_at | 同名 | DateTime |

### ChatMessage → chat_messages

| 类属性 | 物理列 | ORM 列类型 |
|---|---|---|
| message_id | message_id | String，primary_key |
| session_id | session_id | String，ForeignKey("chat_sessions.session_id") |
| role / action / content | 同名 | String |
| outfits / payload | 同名 | JSON |
| created_at | created_at | DateTime |

### 其余三表

FeedbackRecord 映射 feedback_records，`record_id` 为主键，rating 为 Integer，tags、evidence 为 JSON，其余为 String。PurchaseEvaluation 映射 purchase_evaluations，`evaluation_id` 为主键，target_item 与 result 为 JSON。OutfitRecord 映射 outfit_records，`outfit_id` 为主键，score 为 Float，items、reason_points 为 JSON，is_diff 为 Integer，reason_summary 为 String。

## 三、表间关系的 ORM 实现

系统中唯一的显式 ORM 关系是会话与消息之间的一对多关系。`ChatSession.messages` 通过 `relationship("ChatMessage", back_populates="session", lazy="selectin")` 定义，加载会话时自动连带加载其全部消息并按创建时间排序；`ChatMessage.session` 通过 `back_populates` 反向指回会话。物理层由 `chat_messages.session_id` 外键约束保障引用完整性。

其余表间关联不使用数据库外键，而是通过约定字段实现逻辑关联：衣物、反馈、评估、方案均以 `user_id` 归属用户；消息、方案以 `session_id` 归属会话；反馈以 `outfit_id` 关联方案。这样设计是为了在 SQLite 轻量环境下降低删除与清空操作的耦合，归属完整性由应用层的鉴权中间件和服务逻辑保证。

## 四、ORM 与 Pydantic 模型的分工

`db.py` 中的 ORM 类负责持久化，直接对应物理表；`schemas.py` 中的 Pydantic 模型负责接口层的数据校验与序列化，二者不混用。例如创建衣物时，接口接收 `ClothingItemCreate` 模型完成字段校验，再由路由函数逐字段构造 `ClothingItem` 实体写入数据库；返回时再通过 `ClothingItemResponse` 序列化。响应模型通过 `from_attributes = True` 配置支持从 ORM 对象直接读取属性，避免手工转换。
