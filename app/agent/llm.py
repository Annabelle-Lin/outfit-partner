# -*- coding: utf-8 -*-
"""智谱 LLM 调用封装 —— 知识问答生成。

- 有 ZHIPU_API_KEY 时调用 GLM-4-Flash
- 无 key 或调用失败时返回 None，由上层降级为纯检索展示
- 优先使用 httpx；未安装时用 urllib 降级，保证不卡依赖
"""
import json
import urllib.request
from typing import Any

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

from app import config


def _build_system_prompt() -> str:
    return (
        "你是一位专业的穿搭顾问，基于提供的参考资料回答用户问题。"
        "回答要求：\n"
        "1. 只使用参考资料中的事实，不要编造；\n"
        "2. 回答简洁、有结构化，分 1-3 条核心结论；\n"
        "3. 每条结论尽量引用参考来源标注；\n"
        "4. 如果参考资料无法回答问题，明确说明。"
    )


def _build_user_prompt(query: str, passages: list[dict[str, Any]]) -> str:
    context = "\n\n".join(
        f"[{i+1}] 来源：{p['doc']} - {p['title']}\n{p['text']}\n出处：{p['source']}"
        for i, p in enumerate(passages)
    )
    return f"问题：{query}\n\n参考资料：\n{context}\n\n请根据参考资料回答问题。"


def _post_json(url: str, headers: dict, payload: dict, timeout: float) -> dict:
    """兼容 httpx 与 urllib 的 POST JSON。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if _HAS_HTTPX:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, headers=headers, content=data)
            resp.raise_for_status()
            return resp.json()

    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def ask(query: str, passages: list[dict[str, Any]], timeout: float = 15.0) -> dict[str, Any] | None:
    """调用智谱 GLM-4-Flash 生成回答。

    返回 {"answer": str, "sources": list}，失败返回 None。
    """
    if not config.ZHIPU_API_KEY:
        return None

    messages = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": _build_user_prompt(query, passages)},
    ]

    payload = {
        "model": "glm-4-flash",
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1024,
    }

    try:
        data = _post_json(
            f"{config.ZHIPU_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.ZHIPU_API_KEY}",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout=timeout,
        )
        answer = data["choices"][0]["message"]["content"]
        return {
            "answer": answer.strip(),
            "sources": [
                {"doc": p["doc"], "title": p["title"], "source": p["source"], "score": p["score"]}
                for p in passages
            ],
        }
    except Exception:
        return None
