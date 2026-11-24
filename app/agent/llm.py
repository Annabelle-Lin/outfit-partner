# -*- coding: utf-8 -*-
"""LLM 调用封装 —— 文本对话走 DeepSeek，拍照识别走智谱视觉。

- 文本（意图识别 / 知识问答 / 流式问答 / 搭配文案）：DEEPSEEK_API_KEY + deepseek-flash
- 视觉（拍照识别衣物）：ZHIPU_API_KEY + glm-4v-flash（DeepSeek 无可用视觉模型）
- 无 key 或调用失败时返回 None，由上层降级为纯检索 / 本地启发式
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


def _chat_cfg() -> tuple[str, str, str]:
    """文本对话的 (key, base_url, model)。统一取自配置，便于一处切换供应商。"""
    return config.DEEPSEEK_API_KEY, config.DEEPSEEK_BASE_URL, config.DEEPSEEK_MODEL


# deepseek-flash 默认开启思考模式：token 消耗在 reasoning_content 上，
# content 为空（尤其小 max_tokens 的意图识别），导致调用看似失败而整体降级。
# 纯对话/分类/文案任务一律关闭思考，出词快且便宜。若换成不认此参数的供应商，删掉这个键即可。
_THINK_OFF = {"thinking": {"type": "disabled"}}


def _chat_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _chat_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/chat/completions"


def _build_system_prompt() -> str:
    return (
        "你是「今遂」智能穿搭助手，一位专业、耐心的专属穿搭顾问，正在和用户直接对话。\n"
        "【身份】你来自今遂，有一个很可爱的形象；你的名字固定为「遂遂子」，没有别的称呼。"
        "用户问「你叫什么/你是谁」时，要说明自己是专属穿搭顾问、来自今遂、形象可爱、"
        "名字就是遂遂子；用户喊其他名字时不要承认、不要复述或纠正那些名字、也不要记成新名字，"
        "按无关问题委婉拒答即可；回答时也不要提及或复述用户之前用过的其他称呼。"
        "用户单纯喊「遂遂子」时，要先回应「我在呢」，再问他想搭衣服、查天气还是聊流行趋势。\n"
        "【你能做的事】①按天气、场合和用户衣橱里的单品搭出可照着穿的整套方案；"
        "②讲清搭配原理与风格思路（三色原则、洋葱式叠穿、显高显瘦等）；"
        "③帮用户整理衣橱、检索衣橱里有没有某件衣服或相近的款、盘点可搭配组合、给购买前的建议；"
        "④解答近期的时尚资讯与潮流风向（流行色、热门风格、热门单品、搭配思路）；"
        "⑤查询天气（今天或未来几天的温度、降雨），会基于真实天气数据回答。\n"
        "【边界】除上述五类外的问题（如吃什么、星座运势、医疗、股票、作业题等），"
        "要委婉拒绝，并简要说明上面这些能力，不要硬答，也不要为了凑答案去引用穿搭知识库。\n"
        "回答要求：\n"
        "1. 先理解用户的真实意图。用户可能有错别字、语病、口语化表达或说一半，"
        "请按最合理的语义去理解，不要揪着字面错误不放，也不要反问「你是指什么」——"
        "除非真的无法判断，才用一句话澄清；\n"
        "2. 优先用参考资料（如有）和下方提供的用户上下文回答；资料没有覆盖时，"
        "大胆用通用穿搭常识补足，不要只回一句「资料里没有」；\n"
        "3. 回答要具体、可执行，给出明确的颜色/品类/搭配建议，而不是复述百科定义；\n"
        "4. 简洁，分 1-3 条核心结论；引用资料时用 [1][2] 标注，纯常识建议不必标注；\n"
        "5. 涉及「今天穿什么」这类结合当下的问题，必须结合提供的天气和用户的衣橱、"
        "偏好来答，不要给空泛的通用答案；\n"
        "6. 你能完整查看用户衣橱里的每一件单品，具体清单在「用户上下文」里逐件列出。"
        "用户问「我衣橱里有没有某件衣服」「有没有类似的」时，必须逐件检索这份清单："
        "明确对应就说「有」并指出是哪几件；没有完全同款但有同品类/相近颜色/相近款式的，"
        "说「有相近的/疑似有」并列出依据；确实没有才说没有并给替代建议。"
        "绝对不要回答「看不到具体单品名称」「只能看到分类总数」「无法查看衣橱」这类推脱话；\n"
        "7. 回答衣橱检索类问题时，不要罗列知识库长文，直接给检索结论（有/疑似有/没有+具体单品）；\n"
        "8. 排版要清爽易读：按要点分成 2-4 个条目，每个条目的关键词/小标题用 **加粗** 包裹，"
        "条目之间空一行；不要把整段回答挤成一坨，也不要输出 Markdown 表格。"
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
    "- weather：查询天气本身，如「今天天气怎么样」「厦门多少度」「明天会下雨吗」"
    "「未来三天温度」——用户要的是天气情况，系统会用真实天气数据回答。"
    "注意：「今天穿什么」是要搭配方案，判 recommend，不是 weather\n"
    "- off_topic：和穿搭/时尚/天气完全无关的问题，如「今天吃什么」「白羊座运势如何」"
    "「帮我写作业」「最近股票怎么买」——用户要的是别的领域答案，系统会给委婉拒答。"
    "注意：问助手自己「你叫什么/你是谁/你能做什么」不是 off_topic，判 knowledge\n"
    "核心规则：问「今天/现在/出门穿什么、配什么、搭什么」= recommend；"
    "问「X是什么/为什么/有哪些/能不能」这类要知道答案的 = knowledge；"
    "提到「去某城市旅游/出差/玩」+ 要穿搭 = travel。"
    "有语病、口语、错别字时先理解真实意图再归类。"
)


def classify_intent(text: str, timeout: float | None = None) -> str | None:
    """用模型判断意图。无 key / 失败返回 None，由上层退回关键词规则。

    只跑一次极短调用， temperature=0，输出被收敛到固定标签集合。
    """
    key, base_url, model = _chat_cfg()
    if not key or not text:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _INTENT_SYSTEM},
            {"role": "user", "content": text[:200]},
        ],
        "temperature": 0,
        "max_tokens": 16,
        **_THINK_OFF,
    }
    try:
        data = _post_json(
            _chat_url(base_url),
            headers=_chat_headers(key),
            payload=payload,
            timeout=timeout or min(config.LLM_TIMEOUT, 8),
        )
        label = data["choices"][0]["message"]["content"].strip().lower()
        allowed = {"knowledge", "recommend", "compare", "refine", "free_text",
                   "accept", "reject", "travel", "off_topic", "weather"}
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
    """调用 DeepSeek 生成回答。

    返回 {"answer": str, "sources": list}，失败返回 None。
    history 为最近几轮对话，用于延续语境（1.2 多轮上下文）。
    context 为天气/衣橱/画像等结构化上下文，即使 passages 为空也能回答。
    """
    key, base_url, model = _chat_cfg()
    if not key:
        return None

    timeout = timeout or config.LLM_TIMEOUT

    messages = [
        {"role": "system", "content": _build_system_prompt()},
        {"role": "user", "content": _build_user_prompt(
            _with_history(query, history), passages, context)},
    ]

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 1024,
        **_THINK_OFF,
    }

    try:
        data = _post_json(
            _chat_url(base_url),
            headers=_chat_headers(key),
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


# ============ 统一裁决：一次调用同时判「在不在域」+「什么意图」============
# 背景：原架构本地关键词先短路、LLM 只兜底，导致「奥特曼喜欢穿什么」「怎么搭车」
# 「搭一套煎饼」被裸词放行（2026-11-18 三张截图）。改为 LLM 主裁决：
# 一次调用输出 domain + intent，本地词表降级为后置窄修正，不再前置放行。
_CLASSIFY_SYSTEM = (
    "你是智能穿搭助手的请求路由器。对用户每句话，先判断是否属于助手能力范围，再判断意图。\n"
    "只输出一个 JSON，不要解释、不要 markdown：{\"domain\":\"in|out\",\"intent\":\"标签\"}\n"
    "domain=out 时 intent 固定为 off_topic。\n\n"
    "【属于 in 的五类能力】\n"
    "1. 要穿搭方案：今天/出门/上班穿什么、帮我搭一套、推荐几套、换一批、对比方案、改某单品\n"
    "2. 穿搭知识原理：颜色搭配、风格、体型适配、面料、保养收纳、三色原则\n"
    "3. 衣橱管理：整理衣橱、查衣橱里有没有某件衣服或类似的款、某件衣服值不值得买\n"
    "4. 时尚资讯：流行色、热门风格、趋势风向\n"
    "5. 查询天气：今天多少度、下不下雨、未来几天天气\n"
    "另外：问助手自己「你叫什么/你是谁/能做什么」、简单问候，都算 in。\n\n"
    "【必须判 out 的情况】美食点菜、星座运势、医疗、股票基金、作业论文、写诗写故事、"
    "电影小说游戏推荐、旅行目的地/景点推荐、搭车搭桥搭煎饼等非衣物语境。\n"
    "关键：同一个词在不同语境含义不同——\n"
    "- 「今天穿什么」「帮我搭一套衣服」是在要穿搭方案 → in/recommend\n"
    "- 「奥特曼喜欢穿什么」「怎么搭车」「给我搭一套煎饼」不是在问穿搭 → out/off_topic\n"
    "- 「给我推荐一本书」「推荐旅行目的地」不是在要衣服 → out/off_topic\n"
    "- 「今天吃什么」是美食 → out；「吃完饭穿什么」是穿搭 → in/recommend\n"
    "判断标准是【这句话真实想做的事】，不是有没有出现穿搭相关的字。\n\n"
    "【intent 标签（domain=in 时）】\n"
    "- recommend：要可直接照穿的搭配方案（今天穿什么/帮我搭一套/换一批）\n"
    "- knowledge：要一段解释（配饰有哪些种类/三色原则是什么/你叫什么）\n"
    "- trend：时尚流行资讯（今年流行什么颜色）\n"
    "- weather：查天气本身（今天多少度/明天会下雨吗）\n"
    "- travel：去某城市旅游出差问穿什么（周末去厦门玩3天穿什么）\n"
    "- compare：对比两套或「换成X会怎样」\n"
    "- refine：改已有方案的单品（第二套把鞋换成靴子）\n"
    "- reject：不满意、都不喜欢\n"
    "- accept：满意、就要这套\n"
    "- free_text：模糊偏好（亮一点、基础款）\n"
    "有语病、口语、错别字时先理解真实意图再归类。"
)

_CLASSIFY_ALLOWED = {"knowledge", "recommend", "compare", "refine", "free_text",
                     "accept", "reject", "travel", "off_topic", "weather", "trend"}


def classify(text: str, timeout: float | None = None) -> dict[str, str] | None:
    """一次 LLM 调用判「域 + 意图」。返回 {"domain": "in|out", "intent": 标签}。

    无 key / 失败返回 None，由上层退回本地规则。这是主裁决，优先级高于本地词表。
    """
    key, base_url, model = _chat_cfg()
    if not key or not text:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _CLASSIFY_SYSTEM},
            {"role": "user", "content": text[:300]},
        ],
        "temperature": 0,
        "max_tokens": 64,
        **_THINK_OFF,
    }
    try:
        data = _post_json(
            _chat_url(base_url), headers=_chat_headers(key),
            payload=payload, timeout=timeout or min(config.LLM_TIMEOUT, 8),
        )
        raw = (data["choices"][0]["message"].get("content") or "").strip()
        # 容错：去掉 markdown 代码围栏
        if raw.startswith("```"):
            raw = raw.strip("`").strip()
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        obj = json.loads(raw)
        domain = str(obj.get("domain", "")).strip().lower()
        intent = str(obj.get("intent", "")).strip().lower()
        if intent not in _CLASSIFY_ALLOWED:
            return None
        if domain == "out":
            intent = "off_topic"
        elif domain != "in":
            return None
        return {"domain": domain, "intent": intent}
    except Exception:
        return None


# ============ 领域门禁：判断问题是否属于助手能力范围 ============
_DOMAIN_SYSTEM = (
    "你在判断用户的问题是否属于一个「智能穿搭助手」的服务范围。"
    "只输出 in 或 out，不要解释、不要标点。\n"
    "服务范围（输出 in）：\n"
    "1. 按天气/场合/衣橱搭配衣服、推荐搭配、换一批、对比两套、改某个单品\n"
    "2. 穿搭知识与原理：颜色搭配、风格、体型适配、面料、保养、收纳\n"
    "3. 衣橱管理：整理衣橱、单品去留、买不买某件衣服\n"
    "4. 时尚资讯：流行趋势、热门色、风格风向\n"
    "5. 查询天气：今天/某地多少度、冷不冷、热不热、下不下雨、未来几天天气\n"
    "6. 问助手自己叫什么、能做什么，以及简单问候（你好、在吗）\n"
    "超出以上范围的（美食、星座运势、医疗、股票、作业、写诗、游戏等）输出 out。\n"
    "注意：只要最终诉求是穿衣搭配，即使句子里提到天气或场合，也输出 in。"
)


def check_domain(text: str, timeout: float | None = None) -> str | None:
    """能力白名单门禁。返回 in / out，调用失败返回 None（由上层保守放行）。"""
    key, base_url, model = _chat_cfg()
    if not key or not text:
        return None
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _DOMAIN_SYSTEM},
            {"role": "user", "content": text[:300]},
        ],
        "temperature": 0,
        "max_tokens": 4,
        **_THINK_OFF,
    }
    try:
        data = _post_json(
            _chat_url(base_url), headers=_chat_headers(key),
            payload=payload, timeout=timeout or min(config.LLM_TIMEOUT, 8),
        )
        label = (data["choices"][0]["message"].get("content") or "").strip().lower()
        return label if label in ("in", "out") else None
    except Exception:
        return None


# ============ 天气询问：基于真实天气数据回答，禁止编造 ============
_WEATHER_SYSTEM = (
    "你是穿搭顾问，正在回答用户关于天气的问题。\n"
    "回答要求：\n"
    "1. 只能使用下面给出的天气数据，绝对不要编造没给出的温度、降雨或地点；\n"
    "2. 用自然口语，2-3 句：先说清天气情况，再顺带一句贴合的穿搭提示；\n"
    "3. 问的是未来天气时，说明数据覆盖的日期区间与最高/最低温；\n"
    "4. 结尾可以问一句要不要按这个天气搭配；不要罗列编号清单，不要引用文献。"
)


def weather_reply(query: str, weather_info: dict, history: list[dict[str, Any]] | None = None,
                  timeout: float | None = None) -> str | None:
    """基于给定天气数据生成口语化回答。数据不足时上层会提示，不会编造。"""
    key, base_url, model = _chat_cfg()
    if not key:
        return None
    lines = [f"用户的问题：{query}"]
    for k, v in (weather_info or {}).items():
        if v is not None and v != "":
            lines.append(f"{k}：{v}")
    messages = [{"role": "system", "content": _WEATHER_SYSTEM}]
    messages.append({"role": "user", "content": _with_history(
        "\n".join(lines), history) if history else "\n".join(lines)})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.5,
        "max_tokens": 300,
        **_THINK_OFF,
    }
    try:
        data = _post_json(_chat_url(base_url), headers=_chat_headers(key),
                          payload=payload, timeout=timeout or config.LLM_TIMEOUT)
        text = (data["choices"][0]["message"].get("content") or "").strip()
        return text or None
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
    key, base_url, model = _chat_cfg()
    if not key or not _HAS_HTTPX:
        return None

    timeout = timeout or config.LLM_TIMEOUT
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user", "content": _build_user_prompt(
                _with_history(query, history), passages, context)},
        ],
        "temperature": 0.3,
        "max_tokens": 1024,
        "stream": True,
        **_THINK_OFF,
    }

    def _gen():
        with httpx.stream("POST", _chat_url(base_url),
                          headers=_chat_headers(key),
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
    """2.6 多模态入库：调用智谱视觉模型识别衣物属性。

    注意：DeepSeek 无可用视觉模型，此处固定走智谱 GLM-4V。
    无 KEY 时返回 None，调用方降级为本地启发式，并明确标注 degraded。
    """
    if not config.is_vision_enabled():
        return None

    prompt = (
        "你是服装识别助手。请识别图中这件衣物，只输出 JSON，不要任何解释文字。"
        '格式：{"category":"上装|下装|外套|连衣裙|鞋|配饰",'
        '"sub_category":"具体品类如衬衫/针织衫/直筒裤",'
        '"color":"主色中文",'
        '"season":"春|夏|秋|冬|春秋|四季",'
        '"fabric":"棉|麻|丝|毛|化纤|混纺|牛仔|皮革|针织|雪纺|灯芯绒|未知",'
        '"style":"基础款|设计款|复古|简约|休闲|通勤|运动|甜美|中性"}'
        "。season 和 fabric 只能从给定枚举中选一个字面值，不得自造（如「冬季」「羊毛」均不合法）。"
    )
    payload = {
        "model": config.ZHIPU_VISION_MODEL,
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
            _chat_url(config.ZHIPU_BASE_URL),
            headers=_chat_headers(config.ZHIPU_API_KEY),
            payload=payload,
            timeout=config.LLM_TIMEOUT,
        )
        text = data["choices"][0]["message"]["content"]
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# ============ 时尚资讯（流行趋势 / 热门色 / 风格）============
_TREND_SYSTEM = (
    "你是一位时尚趋势观察员，正在给用户讲最近的流行趋势和热门穿搭思路。"
    "回答要求：\n"
    "1. 重点是当下/近期的时尚资讯：流行色、热门风格、热门单品、搭配思路，"
    "给出具体可感知的方向（如「低饱和奶油色」「松弛感通勤」「老钱风」），不要空泛；\n"
    "2. 这是通用资讯，不要假设用户在问自己的衣橱，也不要编造『根据你的衣橱』——"
    "除非用户明确说要结合衣橱；\n"
    "3. 你的知识有截止时间，无法确认此刻的实时热搜。若涉及时效性很强的内容"
    "（如『这周』『今天热搜』），坦诚说明无法获取实时榜单，给出你知识范围内的近期趋势；\n"
    "4. 分 2-4 个小标题或条目，每点一句话讲清「是什么 + 怎么穿」，简洁有力；\n"
    "5. 用中文，不要输出 Markdown 表格，不要罗列文献来源；\n"
    "6. 排版必须清爽：每个小标题单独一行、用 **加粗** 包裹关键词；"
    "标题与正文之间、条目之间用空行分隔；不要把所有内容挤成一整段。"
    "示例格式：\n**松弛感通勤**\n\n软西装配直筒裤，内搭薄针织……\n\n**老钱风**\n\n"
    "高领针织加乐福鞋，颜色选黑灰驼……"
)


def trend(query: str, history: list[dict[str, Any]] | None = None,
          timeout: float | None = None) -> str | None:
    """生成时尚资讯/趋势解读。失败或无 Key 返回 None，由上层降级。"""
    key, base_url, model = _chat_cfg()
    if not key:
        return None
    messages = [{"role": "system", "content": _TREND_SYSTEM}]
    hist = _with_history(query, history) if history else query
    messages.append({"role": "user", "content": hist})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.6,
        "max_tokens": 700,
        **_THINK_OFF,
    }
    try:
        data = _post_json(_chat_url(base_url), headers=_chat_headers(key),
                          payload=payload, timeout=timeout or config.LLM_TIMEOUT)
        text = (data["choices"][0]["message"].get("content") or "").strip()
        return text or None
    except Exception:
        return None


def reply(user_text: str, weather: dict, outfits: list[dict], timeout: float | None = None,
          history: list[dict[str, Any]] | None = None,
          accessories: list[str] | None = None) -> str | None:
    """让大模型针对本轮方案生成自然语言回复。失败或无 Key 时返回 None。

    history 只需带上一轮，用于「换成裙子」这类追问不丢失前文语境。
    accessories 为衣橱可用的配饰单品名，用于在回答「配饰怎么搭」时给具体建议。
    """
    key, base_url, model = _chat_cfg()
    if not key:
        return None

    timeout = timeout or config.LLM_TIMEOUT

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _REPLY_SYSTEM},
            {"role": "user", "content": _build_reply_prompt(
                _with_history(user_text, history[-2:] if history else None), weather, outfits,
                accessories)},
        ],
        "temperature": 0.7,
        "max_tokens": 400,
        **_THINK_OFF,
    }

    try:
        data = _post_json(
            _chat_url(base_url),
            headers=_chat_headers(key),
            payload=payload,
            timeout=timeout,
        )
        text = data["choices"][0]["message"]["content"]
        return text.strip() or None
    except Exception:
        return None
