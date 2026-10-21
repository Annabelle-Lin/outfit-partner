# -*- coding: utf-8 -*-
"""全局配置：路径、环境变量读取。

约定：
- 敏感信息（API key）一律走环境变量 / .env，不写死在代码里；
- .env.example 提交到仓库，真实 .env 不提交。
"""
import os
from pathlib import Path

# 项目根目录（wardrobe-agent/）
BASE_DIR = Path(__file__).resolve().parent.parent

# 规则库目录与数据目录
RULES_DIR = BASE_DIR / "rules"
DATA_DIR = BASE_DIR / "data"

# 数据库：开发用 SQLite，放在用户主目录下避免项目目录无写权限
DB_PATH = Path.home() / ".wardrobe" / "wardrobe.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    f"sqlite:///{DB_PATH.as_posix()}"
)

# 智谱 LLM（免费 GLM-4-Flash），用于知识问答生成；为空则走降级逻辑
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"

# 和风天气（免费额度）；为空则天气接口走 mock 数据
QWEATHER_KEY = os.environ.get("QWEATHER_KEY", "")


def is_llm_enabled() -> bool:
    """是否启用了真实 LLM 调用。没配 key 时知识问答自动降级为纯检索展示。"""
    return bool(ZHIPU_API_KEY)


def is_weather_enabled() -> bool:
    """是否启用了真实天气 API。没配 key 时返回 mock 数据。"""
    return bool(QWEATHER_KEY)
