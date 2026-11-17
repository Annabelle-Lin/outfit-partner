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
        "你是一位专业、耐心的穿搭顾问，正在和用户直接对话。\n"
        "回答要求：\n"
        "1. 先理解用户的真实意图。用户可能有错别字、语病、口语化表达或说一半，"
        "请按最合理的语义去理解，不要揪着字面错误不放，也不要反问「你是指什么」——"
        "除非真的无法判断，才用一句话澄清；\n"
        "2. 优先用参考资料（如有）和下方提供的用户上下文回答；资料没有覆盖时，"
        "大胆用通用穿搭常识补足，不要只回一句「资料里没有」；\n"
        "3. 回答要具体、可执行，给出明确的颜色/品类/搭配建议，而不是复述百科定义；\n"
        "4. 简洁，分 1-3 条核心结论；引用资料时用 [1][2] 标注，纯常识建议不必标注；\n"
        "5. 涉及「今天穿什么」这类结合当下的问题，必须结合提供的天气和用户的衣橱、"
        "偏好来答，不要给空泛的通用答案。"
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


def _build_user_prompt(query: str, passages: list[dict[str, Any]],
                       context: str = "") -> str:
    parts: list[str] = [f"问题：{query}"]
    if context:
        parts.append(f"用户上下文（务必结合）：\n{context}")
    if passages:
        ref = "\n\n".join(
            f"[{i+1}] 来源：{p['doc']} - {p['title']}\n{p['text']}\n出处：{p['source']}"
            for i, p in enumerate(passages)
        )
        parts.append(f"参考资料（优先使用，不足处用常识补足）：\n{ref}")
    else:
        parts.append("参考资料：本次检索未命中，请基于用户上下文和通用穿搭常识回答。")
    parts.append("请直接回答问题。")
    return "\n\n".join(parts)


# ============ 意图识别（替代前端硬编码关键词） ============
_INTENT_SYSTEM = (
    "你是穿搭助手的意图分类器。判断用户这句话想做什么，只输出一个英文标签，不要解释、不要标点。\n"
    "可选标签：\n"
    "- recommend：想马上看到穿什么/搭什么的具体方案。包括「今天穿什么」「该穿啥」「我穿什么好」"
    "「有什么配饰可以搭配」「帮我搭一套」「推荐几套」「来一套」「换一批」——"
    "只要用户是想让助手给出可直接照着穿的搭配，就判 recommend（系统会自动出搭配卡片+文字讲解）\n"
    "- knowledge：纯知识/原理/方法类提问，用户要的是一段解释而不是搭配方案，"
    "如「配饰有哪些种类」「为什么冬天穿深色」「衬衫和牛仔裤能一起穿吗」「三色原则是什么」\n"
    "- travel：要去另一个城市旅游/出差/游玩并问穿什么，如「周末去厦门玩3天穿什么」"
    "「下周去北京出差怎么穿」「去哈尔滨滑雪带什么衣服」——"
    "只要涉及目的地城市且要穿搭建议，就判 travel（系统会查目的地未来天气再推荐）\n"
    "- compare：要求对比两个方案或「如果换成X会怎样」\n"
    "- refine：锁定已有方案只改一个单品，如「第二套把鞋换成靴子」\n"
    "- free_text：表达模糊的临时偏好，如「亮一点」「基础款」（没有明确动作动词）\n"
    "- accept：满意、就要这套\n"
    "- reject：不满意、再来一套、这些都不喜欢、换一批\n"
    "核心规则：问「今天/现在/出门穿什么、配什么、搭什么」= recommend；"
    "问「X是什么/为什么/有哪些/能不能」这类要知道答案的 = knowledge；"
    "提到「去某城市旅游/出差/玩」+ 要穿搭 = travel。"
    "有语病、口语、错别字时先理解真实意图再归类。"
)


def classify_intent(text: str, timeout: float | None = None) -> str | None:
    """用模型判断意图。无 key / 失败返回 None，由上层退回关键词规则。

    只跑一次极短调用， temperature=0，输出被收敛到固定标签集合。
    """
    if not config.ZHIPU_API_KEY or not text:
        return None
    payload = {
        "model": "glm-4-flash",
        "messages": [
            {"role": "system", "content": _INTENT_SYSTEM},
            {"role": "user", "content": text[:200]},
        ],
        "temperature": 0,
        "max_tokens": 16,
    }
    try:
        data = _post_json(
            f"{config.ZHIPU_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {config.ZHIPU_API_KEY}",
                "Content-Type": "application/json",
            },
            payload=payload,
            timeout=timeout or min(config.LLM_TIMEOUT, 8),
        )
        label = data["choices"][0]["message"]["content"].strip().lower()
        allowed = {"knowledge", "recommend", "compare", "refine", "free_text",
                   "accept", "reject", "travel"}
        return label if label in allowed else None
    except Exception:
        return None


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
        history: list[dict[str, Any]] | None = None,
        context: str = "") -> dict[str, Any] | None:
    """调用智谱 GLM-4-Flash 生成回答。

    返回 {"answer": str, "sources": list}，失败返回 None。
    history 为最近几轮对话，用于延续语境（1.2 多轮上下文）。
    context 为天气/衣橱/画像等结构化上下文，即使 passages 为空也能回答。
    """
    if not config.ZHIPU_API_KEY:
        return None

    timeout = timeout or config.LLM_TIMEOUT

    messages = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": _build_user_prompt(
            _with_history(query, history), passages, context)},
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
    "你会收到：用户的原始问题、当前天气（含降雨）、衣橱里可用的配饰、"
    "本轮生成的搭配方案（每套含单品清单与推荐理由）。\n"
    "要求：\n"
    "1. 必须正面回答用户的问题，不要答非所问，也不要用「为你推荐 N 套搭配：」这类机械开场白；\n"
    "2. 结合天气给出判断和理由，例如「今天 22℃ 有点凉，建议薄针织+牛仔裤」，"
    "让用户即使不看卡片也知道今天该怎么穿、为什么这么穿；\n"
    "3. 如果用户在问配饰，主动用「衣橱可用配饰」里的具体单品给出搭配建议，"
    "说明配哪套、为什么配；没给配饰清单时不要凭空编造具体单品；\n"
    "4. 只使用给出的单品，不要编造衣橱里没有的衣物；\n"
    "5. 用自然、有温度的口语，分 2-4 句，总长 150 字以内；不要罗列编号清单，"
    "具体单品清单由界面卡片展示，文字讲清「思路+理由+配饰建议」。"
)


def _build_reply_prompt(user_text: str, weather: dict, outfits: list[dict],
                        accessories: list[str] | None = None) -> str:
    lines = [f"用户的问题：{user_text or '未明确说明（用户想看今天的穿搭方案）'}"]
    w = weather or {}
    if w.get("travel"):
        tv = w["travel"]
        span = (f"{tv.get('temp_min')}~{tv.get('temp_max')}°C"
                if tv.get("temp_min") is not None and tv.get("temp_max") is not None
                else f"{tv.get('temp')}°C")
        lines.append(
            f"用户要去 {tv.get('city', '')} 旅游 {tv.get('day_count', 3)} 天，"
            f"当地这段时间 {span}（代表温度 {tv.get('temp')}°C，"
            f"按 26℃ 穿衣法搭配）。请在回复里点明目的地和温度区间。"
        )
    elif w.get("city") or w.get("temp") is not None:
        wl = f"当前天气：{w.get('city', '')} {w.get('temp', '')}°C"
        if w.get("rain") and w.get("rain") != "未知":
            wl += f" {w['rain']}"
        if w.get("rain_probability") is not None:
            wl += f"（降雨概率 {w['rain_probability']}%）"
        lines.append(wl.strip())

    if accessories:
        lines.append("衣橱里可用的配饰：" + "、".join(accessories))

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
               history: list[dict[str, Any]] | None = None, context: str = ""):
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
            {"role": "user", "content": _build_user_prompt(
                _with_history(query, history), passages, context)},
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
          history: list[dict[str, Any]] | None = None,
          accessories: list[str] | None = None) -> str | None:
    """让大模型针对本轮方案生成自然语言回复。失败或无 Key 时返回 None。

    history 只需带上一轮，用于「换成裙子」这类追问不丢失前文语境。
    accessories 为衣橱可用的配饰单品名，用于在回答「配饰怎么搭」时给具体建议。
    """
    if not config.ZHIPU_API_KEY:
        return None

    timeout = timeout or config.LLM_TIMEOUT

    payload = {
        "model": "glm-4-flash",
        "messages": [
            {"role": "system", "content": _REPLY_SYSTEM},
            {"role": "user", "content": _build_reply_prompt(
                _with_history(user_text, history[-2:] if history else None), weather, outfits,
                accessories)},
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
