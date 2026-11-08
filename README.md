# wardrobe-agent 对话式穿搭推理 Agent · 后端

课程小组项目，S1 后端与 Agent 集成。技术路线：**规则引擎（推荐主链路）+ BM25 知识问答（RAG 降级友好）**，FastAPI + SQLite，全免费。

## 目录结构

```
wardrobe-agent/
├── app/
│   ├── main.py            # FastAPI 入口，挂载全部路由
│   ├── config.py          # 配置与环境变量
│   ├── db.py              # SQLite + SQLAlchemy，7 张表
│   ├── schemas.py         # Pydantic 请求/响应模型
│   ├── utils.py           # ID 生成、偏好更新等工具
│   ├── api/               # 六大模块路由（用户/衣物/天气/对话/反馈/购前评估）
│   └── agent/
│       └── engine.py      # Agent 推理层：规则库加载 + 推荐主流程
├── rules/                 # 规则库（JSON，可改可答辩追溯）
│   ├── filter_rules.json  # 硬过滤规则 F001-F008
│   ├── score_rules.json   # 打分排序规则 S001-S012
│   ├── body_rules.json    # 体型适配规则（5 种体型）
│   └── clothing_meta.json # 衣物标签枚举（前后端共用）
├── data/                  # 知识库文档（S3 产出的领域资料，切片后供知识问答检索）
├── API_CONTRACT.md        # 接口契约 v0.2（前端可据此联调）
├── requirements.txt
└── .env.example
```

## 本地运行

```bash
# 1. 装依赖
pip install -r requirements.txt

# 2. 启动（开发模式，自动重载）
uvicorn app.main:app --reload --port 8300

# 3. 验证
curl http://127.0.0.1:8300/health
curl http://127.0.0.1:8300/api/v1/clothing/meta
# 交互式 API 文档
# http://127.0.0.1:8300/docs
```

## 一条完整的本地体验命令链

```bash
BASE=http://127.0.0.1:8300/api/v1

# 1. 注册（输入昵称即可）
U=$(curl -s -X POST $BASE/user/register -H "Content-Type: application/json" -d '{"nickname":"小明"}' | python -c "import sys,json; print(json.load(sys.stdin)['data']['user_id'])")

# 2. 添加几件衣物（以白衬衫+牛仔裤+帆布鞋为例）
curl -s -X POST "$BASE/clothing?user_id=$U" -H "Content-Type: application/json" -d '{"name":"白衬衫","category":"上装","sub_category":"衬衫","color":"白","style":"简约","warmth_level":2,"formality_level":4}'
curl -s -X POST "$BASE/clothing?user_id=$U" -H "Content-Type: application/json" -d '{"name":"蓝色牛仔裤","category":"下装","sub_category":"牛仔裤","color":"蓝","style":"简约","warmth_level":2,"formality_level":3}'
curl -s -X POST "$BASE/clothing?user_id=$U" -H "Content-Type: application/json" -d '{"name":"帆布鞋","category":"鞋","sub_category":"帆布鞋","color":"白","style":"休闲","warmth_level":1,"formality_level":2}'

# 3. 更新体型
 curl -s -X PUT $BASE/user/body -H "Content-Type: application/json" -d "{\"user_id\":\"$U\",\"body\":{\"shape\":\"梨形\"}}"

# 4. 创建会话并推荐
S=$(curl -s -X POST $BASE/chat/session -H "Content-Type: application/json" -d "{\"user_id\":\"$U\",\"occasion\":\"通勤\"}" | python -c "import sys,json; print(json.load(sys.stdin)['data']['session_id'])")
curl -s -X POST "$BASE/chat/$S/message" -H "Content-Type: application/json" -d '{"action":"recommend","payload":{}}' | python -m json.tool

# 5. 多轮调整：排除黑色
# curl -s -X POST "$BASE/chat/$S/message" -H "Content-Type: application/json" -d '{"action":"free_text","payload":{"text":"不要黑色"}}'
```

## 统一响应格式

```json
{ "code": 0, "msg": "ok", "data": ... }
```

- `code=0` 正常；`code=501` 接口为桩（增量二实现）；其余非 0 为业务错误
- 所有接口前缀 `/api/v1`

## 开发增量（S1）

| 增量 | 内容 | 状态 |
|---|---|---|
| 一 | 项目骨架 + 规则库 JSON + 全部路由桩 + 标签枚举/天气/健康检查可用 | ✅ |
| 二 | 数据库 7 表 + 用户/衣物 CRUD + 推荐引擎（组合枚举→硬过滤→打分→差异化→理由模板）+ 反馈/购前评估 | ✅ 当前 |
| 三 | 天气真实接入 + Excel 导入/模板 + 知识问答（BM25 + 智谱 GLM-4-Flash，免 key 降级） | ⏳ |
| 四 | 对比演示素材（3 个冷门问题）+ Demo 稳定化（边界/超时/降级） | ⏳ |

## 规则可追溯

每条规则带 `source` 字段（色彩三色原则 / 服装材料学 / 场合着装礼仪规范等），答辩时用 `/health` 与规则库文件展示。
