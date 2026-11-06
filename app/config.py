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


def _load_dotenv():
    """极简 .env 加载：把项目根目录 .env 的键值注入进程环境变量。

    不引入 python-dotenv 依赖；已在环境中的键不覆盖（系统环境变量优先）。
    没有这一步时，仅在 .env 里填 ZHIPU_API_KEY 不会被读到，LLM 永远降级。
    """
    p = BASE_DIR / ".env"
    if not p.exists():
        return
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_dotenv()

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


# ---------- 对话额度（1.3 额度语义显式化） ----------
# 会话初始额度与各类动作成本统一从配置读，避免 10 / 3 / 1 这些魔数散落在业务代码里。
CHAT_SESSION_QUOTA = int(os.environ.get("CHAT_SESSION_QUOTA", "15"))
CHAT_COST_RECOMMEND = int(os.environ.get("CHAT_COST_RECOMMEND", "3"))
CHAT_COST_REJECT = int(os.environ.get("CHAT_COST_REJECT", "1"))
CHAT_COST_KNOWLEDGE = int(os.environ.get("CHAT_COST_KNOWLEDGE", "0"))

# ---------- 知识库检索（2.1 混合检索） ----------
# 同义词扩展 + 双通道 RRF 融合；任一开关置 0 可退回纯 BM25 做对照实验。
KB_HYBRID_ENABLED = os.environ.get("KB_HYBRID_ENABLED", "1") != "0"
KB_SYNONYM_EXPAND = os.environ.get("KB_SYNONYM_EXPAND", "1") != "0"
# RRF 平滑常数：越大越平缓，60 是信息检索常用默认值
KB_RRF_K = int(os.environ.get("KB_RRF_K", "60"))


# ---------- LLM（1.2 多轮上下文 / 1.4 首字延迟可配） ----------
# 同步调用叠加过短超时是首字延迟的主因，默认放宽到 25s，可用环境变量覆盖。
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "25"))
# 带入 prompt 的历史轮数（1 轮 = 一问一答），超长自动裁旧。
LLM_HISTORY_TURNS = int(os.environ.get("LLM_HISTORY_TURNS", "6"))
