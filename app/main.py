# -*- coding: utf-8 -*-
"""穿搭 Agent 后端入口。

- 挂载全部 API 路由（统一前缀 /api/v1）
- 启动时加载规则库 JSON（失败直接退出，让问题在启动期暴露）
- /health 健康检查，给部署（S5）做存活探测
- 挂载前端静态页面（frontend/），实现前后端一体化调试
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.agent.engine import RecommendEngine, rule_repo
from app.agent.kb import KnowledgeIndex
from app.api import chat, clothing, feedback, purchase, user, weather
from app.db import init_db

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：加载规则库 + 初始化数据库表 + 加载知识库 BM25 索引
    rule_repo.load()
    init_db()
    app.state.engine = RecommendEngine(rule_repo)
    app.state.kb = KnowledgeIndex().load()
    yield
    # 关闭：暂无清理逻辑


app = FastAPI(
    title="对话式穿搭推理 Agent",
    version="0.1.0",
    description="盘活已有衣橱的穿搭推理服务（课程项目 · S1 后端）",
    lifespan=lifespan,
)

# 开发期允许跨域：前端可能用 file:// 直接打开或跑在别的端口
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(user.router, prefix="/api/v1/user", tags=["用户模块"])
app.include_router(clothing.router, prefix="/api/v1/clothing", tags=["衣物模块"])
app.include_router(weather.router, prefix="/api/v1/weather", tags=["天气模块"])
app.include_router(chat.router, prefix="/api/v1/chat", tags=["对话 Agent（核心）"])
app.include_router(feedback.router, prefix="/api/v1/feedback", tags=["反馈模块"])
app.include_router(purchase.router, prefix="/api/v1/purchase", tags=["购前评估模块"])


@app.get("/health", tags=["运维"])
def health(request: Request):
    """存活探测：同时返回规则库与知识库加载状态。"""
    kb = getattr(request.app.state, "kb", None)
    kb_stats = kb.stats() if kb else {"docs": 0, "chunks": 0, "ready": False}
    return {
        "status": "ok",
        "rules_loaded": {
            "filter_rules": len(rule_repo.filter_rules),
            "score_rules": len(rule_repo.score_rules),
            "body_shapes": len(rule_repo.body_rules),
        },
        "knowledge_base": kb_stats,
    }


# 前端静态页面：必须放在所有 API 路由之后，避免覆盖 /api 前缀
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
