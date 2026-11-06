# -*- coding: utf-8 -*-
"""智谱 LLM 调用封装 —— 知识问答生成。

- 有 ZHIPU_API_KEY 时调用 GLM-4-Flash
- 无 key 或调用失败时返回 None，由上层降级为纯检索展示
- 优先使用 httpx；未安装时用 urllib 降级，不卡依赖
- 支持带入最近几轮对话（多轮上下文），超时与轮数由 config 统一配置
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


def _with_history(query: str, history: list[dict[str, Any]] | None) -> str:
    """把最近几轮对话拼到问题前，让模型能理解「那秋天呢」这类指代。

    只带纯文本、不带方案 JSON，避免 prompt 膨胀；单条超长截断。
    """
    if not history:
        return query
    lines: list[str] = []
    for m in history:
        txt = (m.get("content") or "").strip()
        if not txt:
            continue
        role = "用户" if m.get("role") == "user" else "助手"
        lines.append(f"{role}：{txt[:400]}")
    if not lines:
        return query
    return (
        "【上文对话】以下是同一会话最近几轮内容，仅用于理解本次问题中的指代与延续语境，不要复述：\n"
        + "\n".join(lines)
        + "\n【本次问题】"
        + query
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


def ask(query: str, passages: list[dict[str, Any]], timeout: float | None = None,
        history: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """调用智谱 GLM-4-Flash 生成回答。

    返回 {"answer": str, "sources": list}，失败返回 None。
    history 为最近几轮对话，用于延续语境（1.2 多轮上下文）。
    """
    if not config.ZHIPU_API_KEY:
        return None

    timeout = timeout or config.LLM_TIMEOUT

    messages = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": _build_user_prompt(_with_history(query, history), passages)},
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


# ============ 搭配方案的自然语言回复 ============
_REPLY_SYSTEM = (
    "你是一位专业、口语化的穿搭顾问，正在和用户对话。"
    "你会收到：用户的原始需求、当前天气、本轮生成的搭配方案（每套含单品清单与推荐理由）。\n"
    "要求：\n"
    "1. 用自然、有温度的口语回应，像朋友聊天，不要用「为你推荐 N 套搭配：」这类机械句式；\n"
    "2. 先简短回应/确认用户这次的要求（30 字内），再说这套方案的思路和亮点；\n"
    "3. 只使用给出的单品和理由，不要编造衣橱里没有的衣物；\n"
    "4. 总长度控制在 120 字以内，不要罗列编号清单，方案细节由界面卡片展示；\n"
    "5. 如方案不足 3 套或存在取舍，用一句话点明原因。"
)


def _build_reply_prompt(user_text: str, weather: dict, outfits: list[dict]) -> str:
    lines = [f"用户需求：{user_text or '未明确说明'}"]
    w = weather or {}
    if w.get("city") or w.get("temp") is not None:
        lines.append(
            f"当前天气：{w.get('city', '')} {w.get('temp', '')}°C "
            f"{w.get('rain', '') or ''}".strip()
        )

    if not outfits:
        lines.append("本轮未能生成有效搭配。")
    for idx, o in enumerate(outfits, start=1):
        items = "、".join(
            f"{it.get('color', '')}{it.get('name', '')}".strip()
            for it in (o.get("items") or [])
        )
        reason = (o.get("reason") or {})
        summary = reason.get("summary") if isinstance(reason, dict) else str(reason)
        points = reason.get("points") if isinstance(reason, dict) else None
        line = f"方案{idx}：{items}。理由：{summary or '无'}"
        if points:
            line += "；要点：" + "、".join(str(p) for p in points[:3])
        lines.append(line)

    lines.append("请用一两句自然的话回应用户。")
    return "\n".join(lines)


def ask_stream(query: str, passages: list[dict[str, Any]], timeout: float | None = None,
               history: list[dict[str, Any]] | None = None):
    """2.5 流式生成：逐 token yield，供 SSE 端点推送。

    无 KEY / 无 httpx 时返回 None，调用方退回「整段一次性发送」，
    而不是假装在流式——降级必须是可辨的。
    """
    if not config.ZHIPU_API_KEY or not _HAS_HTTPX:
        return None

    timeout = timeout or config.LLM_TIMEOUT
    payload = {
        "model": "glm-4-flash",
        "messages": [
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user", "content": _build_user_prompt(_with_history(query, history), passages)},
        ],
        "temperature": 0.3,
        "max_tokens": 1024,
        "stream": True,
    }

    def _gen():
        with httpx.stream("POST", f"{config.ZHIPU_BASE_URL}/chat/completions",
                          headers={
                              "Authorization": f"Bearer {config.ZHIPU_API_KEY}",
                              "Content-Type": "application/json",
                          },
                          json=payload, timeout=timeout) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line:
                    continue
                line = line.decode("utf-8") if isinstance(line, bytes) else line
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                    delta = obj["choices"][0].get("delta", {}).get("content")
                except Exception:
                    continue
                if delta:
                    yield delta

    return _gen()


def analyze_image(image_b64: str, mime: str = "image/jpeg") -> dict[str, Any] | None:
    """2.6 多模态入库：调用视觉模型识别衣物属性。

    无 KEY 时返回 None，调用方降级为本地启发式，并明确标注 degraded。
    """
    if not config.ZHIPU_API_KEY:
        return None

    prompt = (
        "你是服装识别助手。请识别图中这件衣物，只输出 JSON，不要任何解释文字。"
        '格式：{"category":"上装|下装|外套|连衣裙|鞋|配饰",'
        '"sub_category":"具体品类如衬衫/针织衫/直筒裤","color":"主色中文",'
        '"season":"春秋|夏|冬|四季","fabric":"棉|羊毛|牛仔|皮革|涤纶|未知",'
        '"style":"简约|休闲|正式|运动|甜美|复古"}'
    )
    payload = {
        "model": "glm-4v-flash",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
        "temperature": 0.1,
    }
    try:
        data = _post_json(
            f"{config.ZHIPU_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.ZHIPU_API_KEY}",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout=config.LLM_TIMEOUT,
        )
        text = data["choices"][0]["message"]["content"]
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def reply(user_text: str, weather: dict, outfits: list[dict], timeout: float | None = None,
          history: list[dict[str, Any]] | None = None) -> str | None:
    """让大模型针对本轮方案生成自然语言回复。失败或无 Key 时返回 None。

    history 只需带上一轮，用于「换成裙子」这类追问不丢失前文语境。
    """
    if not config.ZHIPU_API_KEY:
        return None

    timeout = timeout or config.LLM_TIMEOUT

    payload = {
        "model": "glm-4-flash",
        "messages": [
            {"role": "system", "content": _REPLY_SYSTEM},
            {"role": "user", "content": _build_reply_prompt(
                _with_history(user_text, history[-2:] if history else None), weather, outfits)},
        ],
        "temperature": 0.7,
        "max_tokens": 400,
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
        text = data["choices"][0]["message"]["content"]
        return text.strip() or None
    except Exception:
        return None
