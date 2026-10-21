# 对话式穿搭推理 Agent · 接口契约 v0.4.0

> 状态：**已冻结，前端可据此联调**  
> 后端实现状态：全部 22 个接口已实现，无桩接口。  
> 更新日期：2026-10-21  
> 变更：`POST /clothing/import`（Excel 批量导入）与 `GET /clothing/template`（模板下载）由桩接口转为正式实现；`action=reject` 现在会实际重新推荐并返回新搭配；推荐方案快照持久化到 OutfitRecord 表供历史回溯

---

## 1. 基础约定

### 1.1 Base URL

```text
开发环境：http://127.0.0.1:8300
线上环境：由 S5 部署后提供（统一前缀 /api/v1）
```

### 1.2 请求格式

- 所有请求均为 HTTP/1.1
- `POST/PUT` 请求体统一为 `Content-Type: application/json`
- 路径参数用 `{name}` 表示
- 布尔、枚举值均按 JSON 原生类型传递

### 1.3 统一响应格式

```json
{
  "code": 0,
  "msg": "ok",
  "data": { ... }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | int | `0` 成功；非 `0` 为业务错误，详见第 7 节错误码 |
| `msg` | string | 可读说明，前端可直接 Toast/展示 |
| `data` | any | 业务数据，`code!=0` 时通常为 `null` |

HTTP 状态码：成功统一返回 `200 OK`，异常返回 `4xx/5xx`（FastAPI 自动生成）。

---

## 2. 通用数据模型

### 2.1 User（用户）

```json
{
  "user_id": "u_123456",
  "nickname": "小明",
  "avatar": "https://example.com/avatar.png",
  "body": {
    "shape": "梨形",
    "height_cm": 165,
    "weight_kg": 55,
    "shoulder_width": "正常",
    "waist": "细",
    "hip": "宽"
  },
  "preferences": {
    "color": 7,
    "style": 6,
    "comfort": 8,
    "occasion": 5,
    "fabric": 6
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | string | 用户唯一标识 |
| `nickname` | string | 昵称 |
| `avatar` | string | 头像 URL，可为空 |
| `body.shape` | string | 体型：梨形 / 苹果形 / 沙漏形 / 矩形 / 倒三角 |
| `body.height_cm` | int | 身高 cm |
| `body.weight_kg` | int | 体重 kg |
| `body.shoulder_width` | string | 肩宽：窄 / 正常 / 宽 |
| `body.waist` | string | 腰：细 / 正常 / 粗 |
| `body.hip` | string | 臀：窄 / 正常 / 宽 |
| `preferences.*` | int | 长期偏好分，1-10，由反馈模块自动更新 |

### 2.2 ClothingItem（衣物单品）

```json
{
  "item_id": "i_abcdef",
  "user_id": "u_123456",
  "name": "米色针织开衫",
  "category": "上装",
  "sub_category": "针织衫",
  "color": "米",
  "color_family": "米",
  "color_brightness": "亮色",
  "season": "秋",
  "fit_snug": "合身",
  "fit_silhouette": "直筒",
  "fabric": "针织",
  "style": "简约",
  "occasion": ["通勤", "上课", "约会"],
  "warmth_level": 2,
  "formality_level": 3,
  "image_url": "https://example.com/img.jpg",
  "tags": ["基础款", "百搭"],
  "created_at": "2026-10-15T14:30:00",
  "updated_at": "2026-10-15T14:30:00"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `item_id` | string | 后端生成 | 单品唯一标识 |
| `user_id` | string | 是 | 所属用户 |
| `name` | string | 是 | 用户自定义名称 |
| `category` | string | 是 | 一级品类：上装 / 下装 / 外套 / 鞋 / 连衣裙 / 配饰 |
| `sub_category` | string | 是 | 二级品类，见 `/clothing/meta` |
| `color` | string | 是 | 具体颜色，见 `/clothing/meta` 的 `color_family` |
| `color_brightness` | string | 否 | 亮色 / 暗色 |
| `season` | string | 否 | 春 / 夏 / 秋 / 冬 / 四季 |
| `fit_snug` | string | 否 | 修身 / 合身 / 宽松 / Oversize |
| `fit_silhouette` | string | 否 | 直筒 / 收腰 / A字 / 阔腿 / 紧身 / 落肩 |
| `fabric` | string | 否 | 棉 / 麻 / 丝 / 毛 / 化纤 / 混纺 / 牛仔 / 皮革 / 针织 / 雪纺 / 灯芯绒 |
| `style` | string | 否 | 基础款 / 设计款 / 复古 / 简约 / 休闲 / 通勤 / 运动 / 甜美 / 中性 |
| `occasion` | string[] | 否 | 适用场合，可多选 |
| `warmth_level` | int | 否 | 保暖等级 1-5，Agent 根据温度选用 |
| `formality_level` | int | 否 | 正式度 1-5，Agent 根据场合选用 |
| `image_url` | string | 否 | 单品照片 URL |
| `tags` | string[] | 否 | 用户自定义标签 |

### 2.3 Outfit（搭配方案）

```json
{
  "outfit_id": "o_123abc",
  "items": [
    { "item_id": "i_xxx", "category": "上装", "name": "白色衬衫", "image_url": "..." },
    { "item_id": "i_yyy", "category": "下装", "name": "蓝色牛仔裤", "image_url": "..." },
    { "item_id": "i_zzz", "category": "鞋", "name": "帆布鞋", "image_url": "..." }
  ],
  "score": 82.5,
  "reason": {
    "summary": "这套适合今日 22°C 通勤，浅色上装提亮肤色，直筒裤修饰梨形身材。",
    "points": [
      { "type": "weather", "text": "气温 22°C，针织开衫保暖适中。", "rule_id": "S001" },
      { "type": "body", "text": "梨形身材适合直筒裤，避免包臀裙凸显臀围。", "rule_id": "B003" },
      { "type": "occasion", "text": "衬衫+牛仔裤符合通勤正式度。", "rule_id": "S005" },
      { "type": "preference", "text": "符合你对简约风格的长期偏好。", "rule_id": "S009" }
    ]
  },
  "is_diff": true
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `outfit_id` | string | 搭配唯一标识（同一次会话内去重用） |
| `items` | object[] | 组成单品，按 category 排序：上装→下装→外套→连衣裙→鞋→配饰 |
| `items[].item_id` | string | 单品 ID |
| `items[].category` | string | 品类 |
| `items[].name` | string | 名称 |
| `items[].image_url` | string | 图片 URL，可能为空 |
| `score` | float | 综合得分 0-100 |
| `reason.summary` | string | 一句话总理由 |
| `reason.points` | object[] | 分点理由，每条可溯源到规则 ID |
| `reason.points[].type` | string | 理由类型：weather / body / occasion / preference / color / fabric |
| `reason.points[].text` | string | 可读解释 |
| `reason.points[].rule_id` | string | 规则编号，如 F001 / S001 / B003，答辩展示可追溯 |
| `is_diff` | bool | 是否与已展示搭配有足够差异（差异化输出） |

### 2.4 ChatSession（对话会话）

```json
{
  "session_id": "s_abc123",
  "user_id": "u_123456",
  "status": "active",
  "context": {
    "occasion": "通勤",
    "weather": { "city": "福州", "temp": 22, "rain": "无降雨" },
    "temp_pref": { "color": "亮色", "style": "基础款" },
    "constraints": { "exclude_colors": ["黑"], "replace": [] },
    "shown_outfit_ids": ["o_123abc"]
  },
  "messages": [
    { "role": "user", "action": "recommend", "content": "今天上班穿什么", "timestamp": "2026-10-15T14:30:00" },
    { "role": "agent", "action": "recommend", "content": "推荐 3 套搭配", "outfits": ["o_123abc", "o_456def", "o_789ghi"], "timestamp": "2026-10-15T14:30:01" }
  ],
  "remaining_count": 7,
  "created_at": "2026-10-15T14:30:00"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | string | 会话 ID |
| `status` | string | `active` / `ended` |
| `context` | object | 当前会话的场合、天气、约束、已展示 |
| `messages` | object[] | 对话消息历史 |
| `messages[].role` | string | `user` / `agent` |
| `messages[].action` | string | 动作类型，见 6.2 节 |
| `messages[].content` | string | 文本内容 |
| `messages[].outfits` | string[] | Agent 消息中返回的搭配 ID 列表 |
| `remaining_count` | int | 本次会话还可推荐的套数（默认 10，每次推荐 -3，换一套 -1） |

### 2.5 FeedbackRecord（反馈记录）

```json
{
  "record_id": "r_abc123",
  "user_id": "u_123456",
  "session_id": "s_abc123",
  "outfit_id": "o_123abc",
  "rating": 2,
  "tags": ["颜色好看", "太正式"],
  "comment": "",
  "created_at": "2026-10-15T14:35:00"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `rating` | int | `2`=喜欢 / `1`=一般 / `0`=不喜欢 |
| `tags` | string[] | 快捷标签 |
| `comment` | string | 用户补充说明 |

### 2.6 PurchaseEvaluation（购前评估）

```json
{
  "evaluation_id": "e_abc123",
  "user_id": "u_123456",
  "target_item": {
    "name": "黑色西装外套",
    "category": "外套",
    "sub_category": "西装外套",
    "color": "黑",
    "style": "通勤",
    "occasion": ["面试", "正式", "通勤"]
  },
  "result": {
    "can_match": true,
    "matchable_count": 5,
    "duplicate_score": 0.2,
    "best_matches": [
      { "outfit_id": "o_x1", "score": 88.0, "items": [...] }
    ],
    "suggestion": "可与衣橱中 5 件单品组成 5 套搭配，重复度低，建议购买。"
  },
  "created_at": "2026-10-15T14:40:00"
}
```

---

## 3. 接口清单（共 21 个）

| # | 模块 | 方法 | 路径 | 状态 | 说明 |
|---|------|------|------|------|------|
| 1 | 运维 | GET | `/health` | ✅ 已实现 | 存活探测 |
| 2 | 用户 | POST | `/api/v1/user/register` | ✅ 已实现 | 注册/登录 |
| 3 | 用户 | GET | `/api/v1/user/profile` | ✅ 已实现 | 用户信息 |
| 4 | 用户 | PUT | `/api/v1/user/body` | ✅ 已实现 | 更新体型 |
| 5 | 用户 | GET | `/api/v1/user/preferences` | ✅ 已实现 | 长期偏好 |
| 6 | 衣物 | GET | `/api/v1/clothing/meta` | ✅ 已实现 | 标签枚举 |
| 7 | 衣物 | POST | `/api/v1/clothing` | ✅ 已实现 | 新增衣物 |
| 8 | 衣物 | GET | `/api/v1/clothing` | ✅ 已实现 | 衣橱列表 |
| 9 | 衣物 | GET | `/api/v1/clothing/{item_id}` | ✅ 已实现 | 单件详情 |
| 10 | 衣物 | PUT | `/api/v1/clothing/{item_id}` | ✅ 已实现 | 修改衣物 |
| 11 | 衣物 | DELETE | `/api/v1/clothing/{item_id}` | ✅ 已实现 | 删除衣物 |
| 12 | 衣物 | POST | `/api/v1/clothing/import` | ✅ 已实现 | Excel 导入 |
| 13 | 衣物 | GET | `/api/v1/clothing/template` | ✅ 已实现 | 导入模板 |
| 14 | 天气 | GET | `/api/v1/weather` | ✅ 已实现（Open-Meteo） | 当前天气，支持 `?city=` |
| 15 | 对话 | POST | `/api/v1/chat/session` | ✅ 已实现 | 创建会话 |
| 16 | 对话 | POST | `/api/v1/chat/{session_id}/message` | ✅ 已实现 | 发送消息 |
| 17 | 对话 | GET | `/api/v1/chat/{session_id}` | ✅ 已实现 | 会话状态 |
| 18 | 对话 | POST | `/api/v1/chat/{session_id}/end` | ✅ 已实现 | 结束会话 |
| 19 | 反馈 | POST | `/api/v1/feedback` | ✅ 已实现 | 提交反馈 |
| 20 | 反馈 | GET | `/api/v1/feedback/history` | ✅ 已实现 | 反馈历史 |
| 21 | 购前 | POST | `/api/v1/purchase/evaluate` | ✅ 已实现 | 新品评估 |
| 22 | 购前 | GET | `/api/v1/purchase/history` | ✅ 已实现 | 评估历史 |

---

## 4. 接口详细说明

### 4.1 运维接口

#### GET `/health`

**用途**：S5 部署时做存活探测；开发时验证后端与规则库是否启动正常。

**响应示例**：

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "status": "ok",
    "rules_loaded": {
      "filter_rules": 8,
      "score_rules": 12,
      "body_shapes": 5
    }
  }
}
```

---

### 4.2 用户模块（`/api/v1/user`）

#### POST `/user/register`

**用途**：极简注册/登录——输入昵称即可使用，无需密码、手机号或第三方授权。若昵称已存在则视为登录并返回已有资料。

**请求体**：

```json
{
  "nickname": "小明",
  "avatar": "https://..."
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `nickname` | string | 是 | 用户昵称，2-20 字符，不可为空 |
| `avatar` | string | 否 | 头像 URL |

**响应**：返回完整 `User` 对象（2.1）。首次注册时 `preferences` 为默认中间分（各维度 5 分），`body` 各字段为空或默认值。

#### GET `/user/profile`

**查询参数**：`user_id`

**响应**：`User` 对象。

#### PUT `/user/body`

**用途**：更新体型数据。支持客观围度，也支持主观描述（后端用关键词+槽位解析，LLM 可选）。

**请求体**：

```json
{
  "user_id": "u_123456",
  "body": {
    "shape": "梨形",
    "height_cm": 165,
    "weight_kg": 55,
    "shoulder_width": "正常",
    "waist": "细",
    "hip": "宽"
  }
}
```

**响应**：更新后的 `User`。

#### GET `/user/preferences`

**查询参数**：`user_id`

**响应**：

```json
{
  "code": 0,
  "data": {
    "color": 7,
    "style": 6,
    "comfort": 8,
    "occasion": 5,
    "fabric": 6
  }
}
```

---

### 4.3 衣物模块（`/api/v1/clothing`）

#### GET `/clothing/meta`

**用途**：获取全部标签枚举。前端录入表单、Excel 校验、Agent 推理都以此为唯一事实源。

**响应**：见 `rules/clothing_meta.json`，结构如下：

```json
{
  "code": 0,
  "data": {
    "version": "1.0",
    "category": { "上装": [...], "下装": [...], ... },
    "color_family": ["黑", "白", ...],
    "color_brightness": ["亮色", "暗色"],
    "season": ["春", "夏", "秋", "冬", "四季"],
    "fit_snug": ["修身", "合身", "宽松", "Oversize"],
    "fit_silhouette": ["直筒", "收腰", "A字", "阔腿", "紧身", "落肩"],
    "fabric": [...],
    "occasion": [...],
    "style": [...]
  }
}
```

#### POST `/clothing`

**用途**：新增单品。

**查询参数**：`user_id`（必填）

**请求体**：`ClothingItem` 对象（不含 `item_id`、`user_id`、`created_at`、`updated_at`）。

**响应**：创建后的完整 `ClothingItem`。

#### GET `/clothing`

**查询参数**：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_id` | string | 是 | 用户 ID |
| `category` | string | 否 | 按一级品类筛选 |
| `page` | int | 否 | 页码，默认 1 |
| `page_size` | int | 否 | 默认 20 |

**响应**：

```json
{
  "code": 0,
  "data": {
    "total": 42,
    "page": 1,
    "page_size": 20,
    "items": [ { ...ClothingItem... } ]
  }
}
```

#### GET `/clothing/{item_id}`

**响应**：单个 `ClothingItem`。

#### PUT `/clothing/{item_id}`

**请求体**：`ClothingItem` 部分字段（传什么改什么）。

**响应**：更新后的 `ClothingItem`。

#### DELETE `/clothing/{item_id}`

**响应**：

```json
{ "code": 0, "data": { "deleted": true } }
```

#### POST `/clothing/import`

**用途**：按 `/clothing/template` 模板批量导入 Excel。

**请求**：`multipart/form-data`，字段名 `file`。

**响应**：

```json
{
  "code": 0,
  "data": {
    "success": 12,
    "failed": 1,
    "errors": [
      { "row": 5, "reason": "品类 '上衣' 不存在，应为 '上装'" }
    ]
  }
}
```

#### GET `/clothing/template`

**用途**：下载 Excel 导入模板。

**响应**：`Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`，文件名为 `wardrobe_import_template.xlsx`。

---

### 4.4 天气模块（`/api/v1/weather`）

#### GET `/weather`

**用途**：获取当前天气。已接入 Open-Meteo（https://open-meteo.com/，零密钥、非商用免费、CC BY 4.0），无需配置任何密钥。

**查询参数**：`city`（可选，默认「福州」）。常用城市走内置坐标表，未收录城市自动调用 Open-Meteo Geocoding API 解析。

**行为约定**：
- 正常时 `source` 为 `open-meteo`，返回实时温度与降雨描述；
- 结果缓存 30 分钟，命中缓存时响应带 `cached: true`；
- 外部服务不可用或超时（8 秒）时降级返回 mock 数据，`source` 为 `mock(fallback)`，前端可据此提示「本次未使用实时天气」；
- 页面公开展示天气数据时需署名 "Weather data by Open-Meteo.com"；商用需切换 Open-Meteo 付费方案。

**响应**：

```json
{
  "code": 0,
  "data": {
    "city": "上海",
    "temp": 20.9,
    "rain": "无降雨",
    "rain_probability": 5,
    "source": "open-meteo"
  }
}
```

---

### 4.5 对话模块（`/api/v1/chat`，核心）

#### POST `/chat/session`

**用途**：创建一次穿搭咨询会话。

**请求体**：

```json
{
  "user_id": "u_123456",
  "occasion": "通勤",
  "weather": { "city": "福州", "temp": 22, "rain": "无降雨" },
  "temp_pref": { "color": "亮色", "style": "基础款" }
}
```

| 字段 | 必填 | 说明 |
|------|------|------|
| `user_id` | 是 | |
| `occasion` | 否 | 默认由用户偏好推断 |
| `weather` | 否 | 不传则后端自动调用天气模块 |
| `temp_pref` | 否 | 本次临时偏好 |

**响应**：`ChatSession` 对象（`status=active`，`remaining_count=10`）。

#### POST `/chat/{session_id}/message`

**用途**：发送用户意图，Agent 返回搭配或确认调整。

**请求体**：

```json
{
  "action": "recommend",
  "payload": {}
}
```

**action 枚举与 payload**：

| action | payload | 说明 |
|--------|---------|------|
| `recommend` | `{}` | 请求推荐 3 套搭配 |
| `accept` | `{ "outfit_id": "o_123abc" }` | 接受某套 |
| `reject` | `{ "outfit_id": "o_123abc" }` | 不喜欢，换下一套 |
| `exclude_color` | `{ "colors": ["黑"] }` | 排除颜色 |
| `replace_category` | `{ "from": "长裤", "to": "半身裙" }` | 替换某品类 |
| `free_text` | `{ "text": "不要黑色，想要温柔一点" }` | 自由文本 |
| `knowledge` | `{ "text": "梨形身材面试能穿包臀裙吗" }` | 知识问答（BM25+LLM） |

**响应示例**（`action=recommend`）：

```json
{
  "code": 0,
  "data": {
    "message_id": "m_abc",
    "role": "agent",
    "action": "recommend",
    "content": "根据今天 22°C 通勤场景，为你推荐 3 套搭配：",
    "outfits": [ { ...Outfit... }, { ...Outfit... }, { ...Outfit... } ],
    "remaining_count": 7
  }
}
```

**响应示例**（`action=free_text` 解析为排除颜色）：

```json
{
  "code": 0,
  "data": {
    "role": "agent",
    "action": "constraint_updated",
    "content": "已排除黑色，重新推荐中...",
    "outfits": [ { ...Outfit... }, ... ],
    "constraints": { "exclude_colors": ["黑"] }
  }
}
```

**响应示例**（`action=knowledge`）：

```json
{
  "code": 0,
  "data": {
    "message_id": "m_xyz",
    "role": "agent",
    "action": "knowledge",
    "content": "梨形身材面试不建议穿包臀裙。包臀裙会紧贴胯部和大腿，强化下半身的量感；建议选择 A 字裙、直筒裙或阔腿裤来平衡视觉重心。",
    "passages": [
      {
        "doc": "02_服装造型与版型体型适配知识库.md",
        "title": "服装造型与版型体型适配知识库 / 体型与廓形 / 梨形身材",
        "source": "[来源: S02｜陈彬《服装造型设计》东华大学出版社 2015，ISBN 9787566908265，第4章]",
        "text": "梨形身材...",
        "score": 12.34
      }
    ],
    "llm_enabled": true,
    "llm_answer": { "answer": "...", "sources": [...] }
  }
}
```

#### GET `/chat/{session_id}`

**响应**：完整 `ChatSession`。

#### POST `/chat/{session_id}/end`

**用途**：结束会话，清理内存中的临时约束。

**响应**：

```json
{ "code": 0, "data": { "session_id": "s_abc123", "status": "ended" } }
```

---

### 4.6 反馈模块（`/api/v1/feedback`）

#### POST `/feedback`

**用途**：用户对某套搭配打分，后端映射为偏好分数 ±2 并持久化。

**请求体**：

```json
{
  "user_id": "u_123456",
  "session_id": "s_abc123",
  "outfit_id": "o_123abc",
  "rating": 2,
  "tags": ["颜色好看"],
  "comment": ""
}
```

**响应**：更新后的 `User.preferences`。

#### GET `/feedback/history`

**查询参数**：`user_id`, `page`, `page_size`

**响应**：`FeedbackRecord[]` 分页列表。

---

### 4.7 购前评估模块（`/api/v1/purchase`）

#### POST `/purchase/evaluate`

**用途**：输入一件想买的衣服，评估能否与现有衣橱搭配、是否重复。

**请求体**：

```json
{
  "user_id": "u_123456",
  "target_item": {
    "name": "黑色西装外套",
    "category": "外套",
    "sub_category": "西装外套",
    "color": "黑",
    "style": "通勤",
    "occasion": ["面试", "正式", "通勤"]
  }
}
```

**响应**：`PurchaseEvaluation`。

#### GET `/purchase/history`

**查询参数**：`user_id`, `page`, `page_size`

**响应**：`PurchaseEvaluation[]` 分页列表。

---

## 5. 前端联调指南

### 5.1 快速开始

1. 启动后端：`python -m uvicorn app.main:app --host 127.0.0.1 --port 8300 --reload`
2. 浏览器打开 Swagger：`http://127.0.0.1:8300/docs`
3. 先用 `POST /user/register` 拿一个 `user_id`
4. 用 `GET /clothing/meta` 拉枚举渲染表单
5. 调 `POST /chat/session` 创建会话，再调 `POST /chat/{session_id}/message`

### 5.2 当前桩接口处理

增量二之前，所有 `code=501` 的接口会返回：

```json
{ "code": 501, "msg": "接口开发中（增量二实现）", "data": null }
```

前端建议统一封装：

```javascript
async function api(url, options = {}) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options });
  const json = await res.json();
  if (json.code !== 0) {
    if (json.code === 501) {
      console.warn('接口开发中:', url);
      // 可展示“即将上线”占位
    }
    throw new Error(json.msg);
  }
  return json.data;
}
```

### 5.3 Mock 数据建议

前端可先用以下静态数据渲染界面，等接口就绪后替换：

```javascript
const mockUser = {
  user_id: 'u_demo',
  nickname: '演示用户',
  body: { shape: '梨形', height_cm: 165, weight_kg: 55 },
  preferences: { color: 7, style: 6, comfort: 8, occasion: 5, fabric: 6 }
};

const mockOutfit = {
  outfit_id: 'o_demo_1',
  items: [
    { item_id: 'i1', category: '上装', name: '白色衬衫', image_url: '' },
    { item_id: 'i2', category: '下装', name: '蓝色直筒牛仔裤', image_url: '' },
    { item_id: 'i3', category: '鞋', name: '帆布鞋', image_url: '' }
  ],
  score: 85.0,
  reason: {
    summary: '这套适合今日 22°C 通勤，浅色上装提亮肤色，直筒裤修饰梨形身材。',
    points: [
      { type: 'weather', text: '气温 22°C，衬衫单穿舒适。', rule_id: 'S001' },
      { type: 'body', text: '梨形身材适合直筒裤，避免包臀裙凸显臀围。', rule_id: 'B003' },
      { type: 'occasion', text: '衬衫+牛仔裤符合通勤正式度。', rule_id: 'S005' }
    ]
  },
  is_diff: true
};
```

---

## 6. 附录

### 6.1 错误码对照表

| code | 含义 | 处理建议 |
|------|------|----------|
| `0` | 成功 | 正常展示 data |
| `400` | 请求参数错误 | 检查字段类型/必填 |
| `401` | 未登录/用户不存在 | 跳注册/登录 |
| `404` | 资源不存在 | 检查 item_id/session_id |
| `429` | 请求过于频繁 | 限流，稍后重试 |
| `500` | 服务端内部错误 | 提示用户稍后再试 |
| `501` | 接口开发中 | 前端展示占位或忽略 |

### 6.2 action 枚举汇总

| 值 | 使用场景 |
|----|---------|
| `recommend` | 请求/返回推荐 |
| `accept` | 接受搭配 |
| `reject` | 换一套 |
| `exclude_color` | 排除颜色 |
| `replace_category` | 替换品类 |
| `free_text` | 自由文本 |
| `constraint_updated` | Agent 解析自由文本后更新约束 |

### 6.3 与计划书章节对照

| 接口模块 | 计划书章节 |
|----------|-----------|
| 用户模块 | 13.1 用户管理接口 |
| 衣物模块 | 13.2 衣物管理接口 |
| 天气模块 | 13.3 天气接口 |
| 对话模块 | 13.4 穿搭推荐对话接口 |
| 反馈模块 | 13.5 反馈与偏好学习接口 |
| 购前模块 | 13.6 购前评估接口 |
