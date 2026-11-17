# -*- coding: utf-8 -*-
"""对话穿搭 Agent 模块（计划书 13.4，核心）。

POST /chat/session                创建对话会话
POST /chat/{session_id}/message   发送消息
GET  /chat/{session_id}           获取会话状态
POST /chat/{session_id}/end       结束会话
"""
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import config, schemas, security, utils
from app.agent.engine import RecommendContext
from app.api.weather import _fetch_weather, _fetch_forecast, forecast_summary, _CITY_COORDS, _geocode_city
from app.agent.kb import KnowledgeIndex
from app.agent.llm import ask, classify_intent, reply as llm_reply
from app.api import ok
from app.db import ChatMessage, ChatSession, ClothingItem, OutfitRecord, User, get_db

router = APIRouter()


def _session_to_response(session: ChatSession) -> dict:
    return {
        "session_id": session.session_id,
        "user_id": session.user_id,
        "status": session.status,
        "context": {
            "occasion": session.occasion,
            "weather": {
                "city": session.weather_city,
                "temp": session.weather_temp,
                "rain": session.weather_rain,
            },
            "temp_pref": {
                "color": session.temp_pref_color,
                "style": session.temp_pref_style,
            },
            "constraints": session.constraints,
            "shown_outfit_ids": session.shown_outfit_ids,
        },
        "messages": [
            {
                "role": m.role,
                "action": m.action,
                "content": m.content,
                "outfits": m.outfits,
                "timestamp": m.created_at.isoformat(),
            }
            for m in session.messages
        ],
        "remaining_count": session.remaining_count,
        "created_at": session.created_at.isoformat(),
    }


def _session_brief(s: ChatSession) -> dict:
    """列表用轻量 DTO：不返回 messages 全文，避免 20 条会话把响应拉爆。"""
    last = s.messages[-1] if s.messages else None
    return {
        "session_id": s.session_id,
        "status": s.status,
        "occasion": s.occasion,
        "weather": {"city": s.weather_city, "temp": s.weather_temp, "rain": s.weather_rain},
        "remaining_count": s.remaining_count,
        "message_count": len(s.messages),
        "last_message": ((last.content or "")[:60] if last else ""),
        "last_role": (last.role if last else ""),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


def _recent_history(db: Session, session_id: str, turns: int | None = None,
                    exclude_message_id: str | None = None) -> list[dict]:
    """取最近若干轮对话（纯文本），供大模型延续语境。

    exclude_message_id 用于排除本轮刚落库的用户消息，避免同一句被重复带入。
    """
    turns = max(1, int(turns or config.LLM_HISTORY_TURNS))
    rows = (db.query(ChatMessage)
              .filter(ChatMessage.session_id == session_id)
              .order_by(ChatMessage.created_at.desc(), ChatMessage.message_id.desc())
              .limit(turns * 2 + 4)
              .all())
    out: list[dict] = []
    for m in reversed(rows):
        if exclude_message_id and m.message_id == exclude_message_id:
            continue
        txt = (m.content or "").strip()
        if not txt:
            continue
        out.append({"role": m.role, "content": txt})
    return out[-(turns * 2):]


# ---------- 2.2 主动澄清 ----------
_OCCASION_WORDS = ("通勤", "上班", "职场", "约会", "面试", "婚礼", "出差", "聚会",
                   "运动", "健身", "正式", "休闲", "度假", "逛街", "见家长")
_CATEGORY_WORDS = ("上装", "下装", "外套", "连衣裙", "鞋", "配饰", "衬衫", "T恤",
                   "毛衣", "针织", "裤", "裙", "风衣", "大衣", "西装", "卫衣")
_COLOR_WORDS = ("黑", "白", "灰", "红", "蓝", "绿", "黄", "棕", "米", "粉", "紫", "卡其", "藏青")


_WEATHER_WORDS = ("度", "℃", "°", "冷", "热", "凉", "下雨", "雨", "晴", "雪", "暖和")


def _clarify_needs(text: str, session: ChatSession) -> list[str]:
    """2.2 判断这次推荐前该不该先反问，并说明缺什么。

    真正的「信息不足」只有两种：缺场合、缺温度。
    天气能自动取到实时值时温度不算缺，但场合没给过、这句话里也没线索时仍该问一句，
    否则就是替用户凭空假设「通勤」。
    """
    if session.shown_outfit_ids:
        return []
    t = (text or "").strip()
    if len(t) >= 24:
        return []

    needs: list[str] = []
    if not (session.occasion or "").strip() and not any(w in t for w in _OCCASION_WORDS):
        needs.append("occasion")
    if session.weather_temp is None and not any(w in t for w in _WEATHER_WORDS):
        needs.append("weather")
    return needs


def _should_clarify(text: str, session: ChatSession) -> bool:
    return bool(_clarify_needs(text, session))


def _clarify_response(session: ChatSession, needs: list[str] | None = None) -> dict:
    needs = needs or ["occasion", "weather"]
    options: dict = {}
    parts: list[str] = []
    if "occasion" in needs:
        options["occasion"] = ["通勤", "约会", "出差", "面试", "休闲", "聚会"]
        parts.append("今天是什么场合")
    if "weather" in needs:
        options["temperature"] = ["10℃以下", "10-18℃", "18-25℃", "25℃以上"]
        options["weather_hint"] = "也可以直接说城市，我来取实时温度"
        parts.append("大概多少度或哪个城市")
    question = "想给你搭得准一点，先确认：" + "；".join(parts) + "？"
    return {
        "message_id": utils.gen_id("m"),
        "role": "agent",
        "action": "clarify",
        "content": question,
        "outfits": [],
        "remaining_count": session.remaining_count,
        "clarify": {
            "missing": needs,
            "question": question,
            "options": options,
        },
    }


def _travel_clarify_response(session: ChatSession, text: str = "") -> dict:
    """旅游场景缺目的地时反问：去哪个城市、玩几天。"""
    days = _extract_travel_days(text)
    session.constraints = session.constraints or {}
    session.constraints["travel_pending"] = True
    session.constraints["travel_days"] = days
    question = "你要去哪个城市玩？告诉我城市名（比如 厦门、北京），我按当地未来几天的天气给你搭配。"
    return {
        "message_id": utils.gen_id("m"),
        "role": "agent",
        "action": "clarify",
        "content": question,
        "outfits": [],
        "remaining_count": session.remaining_count,
        "clarify": {
            "missing": ["destination"],
            "question": question,
            "options": {"cities": ["厦门", "北京", "上海", "成都", "哈尔滨", "昆明", "三亚", "西安"]},
        },
    }


# ---------- 2.4 反事实对比 ----------
def _pick_base_outfit(db: Session, session: ChatSession) -> dict | None:
    """取本会话最近一套方案，作为对比基准。"""
    rows = (db.query(ChatMessage)
              .filter(ChatMessage.session_id == session.session_id,
                      ChatMessage.role == "agent")
              .order_by(ChatMessage.created_at.desc(), ChatMessage.message_id.desc())
              .limit(10)
              .all())
    for m in rows:
        outfits = m.outfits or []
        if outfits:
            return outfits[0]
    return None


def _outfit_score(outfit: dict) -> float:
    for k in ("score", "total_score"):
        v = outfit.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def _compare_summary(before: dict, after: dict) -> dict:
    """把两套方案压成可对比的字段，附分数变化。"""
    def _items(o):
        return [{
            "item_id": it.get("item_id", ""),
            "name": it.get("name", ""),
            "color": it.get("color", ""),
            "category": it.get("category", ""),
        } for it in (o.get("items") or [])]

    b_items, a_items = _items(before), _items(after)
    b_ids = {x["item_id"] for x in b_items}
    a_ids = {x["item_id"] for x in a_items}
    return {
        "before": {"score": _outfit_score(before), "items": b_items},
        "after": {"score": _outfit_score(after), "items": a_items},
        "score_delta": round(_outfit_score(after) - _outfit_score(before), 2),
        "added": [x for x in a_items if x["item_id"] not in b_ids],
        "removed": [x for x in b_items if x["item_id"] not in a_ids],
    }


# ---------- 2.4 反事实对比：解析「换成黑色」「把长裤换掉」这类诉求 ----------
_CATEGORY_ALIAS = (
    ("连衣裙", "连衣裙"), ("裙", "下装"), ("裤", "下装"), ("鞋", "鞋"),
    ("风衣", "外套"), ("大衣", "外套"), ("外套", "外套"), ("夹克", "外套"),
    ("衬衫", "上装"), ("T恤", "上装"), ("毛衣", "上装"), ("针织", "上装"),
    ("卫衣", "上装"), ("西装", "上装"), ("上衣", "上装"),
)


def _extract_color(text: str) -> str | None:
    for c in _COLOR_WORDS:
        if c in (text or ""):
            return c
    return None


def _extract_category(text: str) -> str | None:
    for word, cat in _CATEGORY_ALIAS:
        if word in (text or ""):
            return cat
    return None


def _as_engine_outfit(outfit: dict) -> dict:
    """引擎打分要求 items/colors/categories/sub_categories 齐全，
    对比时临时拼的方案只有 items，缺派生字段会让 _score 抛 KeyError。
    """
    items = [dict(x) for x in (outfit.get("items") or [])]
    return {
        "items": items,
        "colors": [it.get("color", "") for it in items if it.get("color")],
        "categories": {it.get("category") for it in items if it.get("category")},
        "sub_categories": {it.get("sub_category") for it in items if it.get("sub_category")},
        "fingerprint": outfit.get("fingerprint") or outfit.get("outfit_id") or "",
        "score": outfit.get("score"),
        "reason": outfit.get("reason"),
    }


def _score_outfit(engine, outfit: dict, ctx) -> float | None:
    """尽量复用引擎自己的打分函数；签名不匹配就返回 None，绝不编一个分。"""
    if engine is None:
        return None
    payload = _as_engine_outfit(outfit)
    for name in ("score_outfit", "_score_outfit", "_score"):
        fn = getattr(engine, name, None)
        if not callable(fn):
            continue
        try:
            r = fn(payload, ctx)
        except Exception:
            continue
        if isinstance(r, tuple) and r and isinstance(r[0], (int, float)):
            return float(r[0])
        if isinstance(r, (int, float)):
            return float(r)
        if isinstance(r, dict) and isinstance(r.get("score"), (int, float)):
            return float(r["score"])
    return None


def _get_engine(request):
    eng = getattr(getattr(request, "app", None), "state", None)
    eng = getattr(eng, "engine", None) if eng is not None else None
    if eng is not None:
        return eng
    try:
        from app.agent.engine import RecommendEngine, RuleRepository
        from app import config as _cfg
        return RecommendEngine(repo=RuleRepository(_cfg.RULES_DIR))
    except Exception:
        return None


def _clone_outfit(outfit: dict) -> dict:
    return {
        "items": [dict(x) for x in (outfit.get("items") or [])],
        "score": outfit.get("score"),
        "reason": outfit.get("reason"),
    }


def _run_compare(db: Session, request: Request, session: ChatSession, user: User,
                 payload: dict) -> dict:
    """2.4 反事实对比：拿本会话最近一套方案，替换指定品类/颜色后重算分数。"""
    base = _load_base_outfit(db, session, 1)
    if not base:
        return {
            "action": "compare",
            "content": "还没有可以对比的方案，先让我给你推荐一套吧。",
            "compare": None,
        }

    text = payload.get("text", "") or ""
    color = _extract_color(text)
    category = _extract_category(text)

    wardrobe = _item_dicts(db, session.user_id)
    base_items = base.get("items") or []
    base_cats = {it.get("category") for it in base_items}

    # 没有明确品类时，优先对比「外套」，否则取方案里第一个可替换品类
    if not category:
        category = "外套" if "外套" in base_cats else (next(iter(base_cats), None))

    candidates = [
        w for w in wardrobe
        if w.get("category") == category
        and (color is None or color in (w.get("color") or ""))
        and w.get("item_id") not in {it.get("item_id") for it in base_items}
    ]

    if not candidates:
        hint = "%s%s" % (color or "", category or "")
        return {
            "action": "compare",
            "content": ("衣橱里没有可替换的%s，所以这组对比暂时做不出来。" % hint)
                       + "可以先加一件再看差异，或者换个颜色/品类试试。",
            "compare": None,
        }

    # 取暖度最接近的一件，减少「换了但完全不适应天气」的噪声
    ctx = _make_ctx(session, user)
    engine = _get_engine(request)

    before_score = _score_outfit(engine, _clone_outfit(base), ctx)
    if before_score is None:
        before_score = _outfit_score(base)

    results = []
    for cand in candidates[:3]:
        after = _clone_outfit(base)
        replaced = False
        # 必须保留候选单品的完整属性（warmth_level/sub_category/style/fabric…），
        # 只留 4 个字段会让评分函数取不到保暖度，分数直接变成 None。
        full = dict(cand)
        for idx, it in enumerate(after["items"]):
            if it.get("category") == category:
                after["items"][idx] = full
                replaced = True
                break
        if not replaced:
            after["items"].append(full)
        sc = _score_outfit(engine, after, ctx)
        if sc is None:
            # 拿不到真实分就只报单品差异，score 置空，不编数
            results.append({"candidate": cand, "score": None, "delta": None, "outfit": after})
        else:
            results.append({
                "candidate": cand, "score": round(sc, 2),
                "delta": round(sc - before_score, 2), "outfit": after,
            })

    scored = [r for r in results if r["score"] is not None]
    best = max(scored, key=lambda r: r["delta"]) if scored else results[0]

    summary = _compare_summary(_clone_outfit(base), best["outfit"])
    summary["before"]["score"] = round(before_score, 2) if before_score is not None else None
    summary["after"]["score"] = best["score"]
    summary["score_delta"] = best["delta"]
    summary["options"] = [{
        "item_id": r["candidate"]["item_id"],
        "name": r["candidate"]["name"],
        "color": r["candidate"]["color"],
        "score": r["score"],
        "delta": r["delta"],
    } for r in results]

    if best["delta"] is not None:
        trend = "更高" if best["delta"] > 0 else ("更低" if best["delta"] < 0 else "持平")
        content = ("把%s换成「%s%s」后，这套的匹配分%s %.2f（%.2f → %.2f）。"
                   % (category or "单品", best["candidate"]["color"],
                      best["candidate"]["name"], trend, abs(best["delta"]),
                      before_score, best["score"]))
    else:
        content = ("把%s换成「%s%s」后，单品组合已重新计算；"
                   "当前环境没拿到评分数值，只给出单品差异。"
                   % (category or "单品", best["candidate"]["color"],
                      best["candidate"]["name"]))

    return {"action": "compare", "content": content, "compare": summary,
            "outfits": [best["outfit"]]}


def _quota_cost(action: str) -> int:
    """动作 -> 额度成本（1.3 语义显式化，统一读配置）。"""
    if action == "recommend":
        return config.CHAT_COST_RECOMMEND
    if action == "reject":
        return config.CHAT_COST_REJECT
    if action == "knowledge":
        return config.CHAT_COST_KNOWLEDGE
    return 0


_KNOWLEDGE_HEADS = (
    "为什么", "为什么", "怎么", "怎样", "如何", "什么", "哪些", "哪种", "哪个",
    "是不是", "是否", "要不要", "该不该", "适不适合", "有什么区别", "区别",
    "原则", "技巧", "讲究", "注意什么",
)
_RECOMMEND_WORDS = ("推荐", "搭一套", "搭配一下", "帮我搭", "来一套", "换一批", "配一套")

# ---------- 旅游目的地场景 ----------
# 旅游语义词：出现这些且带「穿搭诉求」时判 travel，走目的地未来天气推荐。
_TRAVEL_WORDS = ("旅游", "旅行", "出游", "去玩", "出去玩", "游玩", "出差", "度假",
                 "滑雪", "爬山", "徒步", "露营", "海边", "玩几天", "玩三天", "去...玩")
# 目的地引导词：用于从句子里定位城市（「去厦门」「到北京」「下周在上海」）。
_DEST_MARKERS = ("去", "到", "往", "飞", "抵达", "下一站", "目的地是")


def _extract_dest_city(text: str) -> str | None:
    """从用户话里抽取旅游目的地城市名。

    优先匹配内置城市表（覆盖产品常见城市），再尝试「去/到+2~4字」并通过
    Geocoding 验证，避免把「去吃饭」误当城市。
    """
    t = (text or "").strip()
    if not t:
        return None
    # 1) 内置城市表： longest-first 避免「西安」被「西」类误匹配
    for city in sorted(_CITY_COORDS, key=len, reverse=True):
        if city in t:
            return city
    # 2) 引导词后的候选词 -> Geocoding 验证
    for marker in _DEST_MARKERS:
        for m in re.finditer(re.escape(marker) + r"([一-龥]{2,6})", t):
            cand = m.group(1)
            # 去掉常见的非城市后缀
            cand = re.sub(r"(玩|旅游|出差|度假|游玩|逛逛|几天|三天|两天|一周|市|玩儿).*$", "", cand)
            cand = cand.strip()
            if len(cand) >= 2 and _geocode_city(cand):
                return cand
    return None


def _extract_travel_days(text: str) -> int:
    """从话里抽旅行天数；用于决定取未来几天预报，默认 3 天。"""
    t = (text or "")
    m = re.search(r"([1-9]|一|二|三|四|五|六|七)\s*天", t)
    if m:
        return _ORDINAL.get(m.group(1), 3)
    if "周末" in t:
        return 2
    if "一周" in t or "七天" in t:
        return 7
    return 3


def _detect_travel(text: str) -> bool:
    """本地检测旅游意图（无 LLM 时也能生效）。需同时有旅游词与穿搭诉求。"""
    t = (text or "").strip()
    if not t:
        return False
    has_travel = any(w in t for w in _TRAVEL_WORDS if "..." not in w) or bool(
        re.search(r"去[一-龥]{2,6}(玩|旅游|出差|度假|逛)", t))
    if not has_travel:
        return False
    # 穿搭诉求：穿/搭/带什么衣服等；或本来就点到城市名
    has_outfit = any(w in t for w in ("穿", "搭", "衣服", "带什么", "着装", "什么好"))
    return has_outfit or _extract_dest_city(t) is not None


def _classify_intent_rules(text: str) -> str:
    """关键词兜底规则（无 LLM / 模型失败时使用）。"""
    t = (text or "").strip()
    if not t:
        return "free_text"
    # 2.4 反事实对比：先于 refine 判定，否则「换成黑色会怎样」会被当成换单品
    if any(w in t for w in ("会怎样", "怎么样", "对比", "比较一下", "如果换", "换成")):
        if any(w in t for w in ("会怎样", "怎么样", "对比", "比较一下", "如果换")):
            return "compare"
    if _detect_refine(t):
        return "refine"
    if any(w in t for w in _RECOMMEND_WORDS):
        return "recommend"
    if _detect_travel(t):
        return "travel"
    if t.endswith(("?", "？", "吗", "呢", "吧")):
        return "knowledge"
    # 纯知识问法：问概念/能不能/原理，要的是解释而非搭配方案
    if any(h in t for h in ("有哪些种类", "有哪些类型", "什么种类", "能一起穿",
                            "能不能一起", "可以一起穿", "三色原则", "是什么", "什么意思")):
        return "knowledge"
    if t.startswith(_KNOWLEDGE_HEADS) or any(h in t for h in ("怎么选", "怎么搭", "如何选", "为什么")):
        return "knowledge"
    return "recommend"


def _classify_intent(text: str) -> str:
    """后端统一意图识别（1.5）：LLM 优先，关键词规则兜底。

    LLM 能理解「今天穿什么」「有啥配饰可以搭」这类口语/语病问法，
    不再一律按关键词落到 recommend。无 key / 模型失败自动回退规则，
    保证离线部署行为与升级前一致。
    """
    t = (text or "").strip()
    if not t:
        return "free_text"

    # 这几个是结构化动作，必须先由本地规则锁定，避免 LLM 误判导致 payload 缺字段。
    if any(w in t for w in ("会怎样", "怎么样", "对比", "比较一下", "如果换")):
        return "compare"
    if _detect_refine(t):
        return "refine"
    # 旅游：本地检测优先，保证「去厦门玩3天穿什么」不被 LLM 误判成普通 recommend。
    if _detect_travel(t):
        return "travel"

    predicted = classify_intent(t)
    # LLM 判成 travel 但本地没检测到时，只要有目的地城市也采信（口语/语病场景）
    if predicted == "travel" and _extract_dest_city(t) is None:
        predicted = None
    # refine 必须带出「改第几套的哪个品类」，模型判了但本地解析不到时不能采用。
    if predicted == "refine" and not _detect_refine(t):
        predicted = None
    return predicted if predicted else _classify_intent_rules(t)


def _parse_free_text(text: str) -> tuple[str, dict]:
    """关键词+槽位解析自由文本。"""
    text = text.strip()
    lower = text.lower()

    # 排除颜色：不要黑 / 排除红色
    color_families = ["黑", "白", "灰", "红", "蓝", "绿", "黄", "棕", "米", "粉", "紫", "卡其"]
    if "不要" in text or "排除" in text or "别" in text:
        colors = [c for c in color_families if c in text]
        if colors:
            return "exclude_color", {"colors": colors}

    # 替换品类：换条裙子 / 把长裤换成半身裙
    categories = ["上装", "下装", "外套", "鞋", "连衣裙", "配饰"]
    sub_cats = ["长裤", "半身裙", "A字裙", "包臀裙", "短裤", "直筒裤", "阔腿裤", "衬衫", "针织衫", "卫衣"]
    if "换" in text:
        to_sub = [s for s in sub_cats if s in text]
        if to_sub:
            return "replace_category", {"from": "下装", "to": to_sub[0]}

    # 临时偏好：亮色 / 暗色 / 基础款 / 设计款
    if "亮" in text or "浅" in text:
        return "temp_pref_color", {"color": "亮色"}
    if "暗" in text or "深" in text:
        return "temp_pref_color", {"color": "暗色"}
    if "基础" in text or "简单" in text:
        return "temp_pref_style", {"style": "基础款"}
    if "设计" in text or "特别" in text or "温柔" in text:
        return "temp_pref_style", {"style": "设计款"}

    return "free_text", {"text": text}


def _run_recommend(db: Session, request: Request, session: ChatSession, user: User,
                   deduct: int | None = None, user_text: str = "") -> tuple[list, str]:
    """推荐主链路：拉衣橱 -> 引擎推荐 -> 更新会话 -> 持久化方案快照。

    返回 (outfits, content)。deduct 为本次消耗的剩余次数，统一由本函数扣减
    （1.3-E：调用方只传 cost，避免 reject 扣减散落在调用方导致漏扣）。
    """
    if deduct is None:
        deduct = config.CHAT_COST_RECOMMEND
    items = db.query(ClothingItem).filter(ClothingItem.user_id == session.user_id).all()
    if not items:
        return [], "衣橱还是空的，先去「衣物管理」添加几件单品吧。"

    item_dicts = [{
        "item_id": i.item_id,
        "name": i.name,
        "category": i.category,
        "sub_category": i.sub_category,
        "color": i.color,
        "color_brightness": i.color_brightness,
        "season": i.season,
        "fit_snug": i.fit_snug,
        "fit_silhouette": i.fit_silhouette,
        "fabric": i.fabric,
        "style": i.style,
        "occasion": i.occasion or [],
        "warmth_level": i.warmth_level,
        "formality_level": i.formality_level,
        "image_url": i.image_url,
        "tags": i.tags or [],
    } for i in items]

    engine = request.app.state.engine
    ctx = RecommendContext(
        user_id=session.user_id,
        occasion=session.occasion,
        weather_temp=session.weather_temp,
        weather_rain=session.weather_rain,
        weather_rain_prob=getattr(session, "weather_rain_prob", None),
        temp_pref={"color": session.temp_pref_color, "style": session.temp_pref_style}
        if (session.temp_pref_color or session.temp_pref_style) else {},
        constraints=session.constraints or {},
        shown_outfit_ids=session.shown_outfit_ids or [],
        body_shape=user.body_shape or "匀称",
        long_term_prefs={
            "color": user.pref_color,
            "style": user.pref_style,
            "comfort": user.pref_comfort,
            "occasion": user.pref_occasion,
            "fabric": user.pref_fabric,
        },
    )
    result = engine.recommend(ctx, item_dicts)
    outfits = result.get("outfits", [])

    # 更新会话：扣减剩余次数、记录已展示
    session.remaining_count = max(0, session.remaining_count - deduct)
    shown = session.shown_outfit_ids or []
    shown.extend([o["outfit_id"] for o in outfits])
    session.shown_outfit_ids = list(dict.fromkeys(shown))

    # 持久化方案快照：衣物后续修改/删除不影响历史回溯
    for o in outfits:
        db.merge(OutfitRecord(
            outfit_id=o["outfit_id"],
            user_id=session.user_id,
            session_id=session.session_id,
            items=o["items"],
            score=o["score"],
            reason_summary=o["reason"]["summary"],
            reason_points=o["reason"]["points"],
            is_diff=1,
        ))

    # 回复文案：优先由大模型结合本轮需求与方案生成，避免机械模板句
    weather_info = {
        "city": session.weather_city,
        "temp": session.weather_temp,
        "rain": session.weather_rain,
        "rain_probability": getattr(session, "weather_rain_prob", None),
    }
    # 旅游场景：把目的地与温度区间带进文案，明确这是按当地未来天气搭的
    _travel = (session.constraints or {}).get("travel")
    if _travel:
        weather_info["travel"] = _travel
    # 衣橱可用配饰：回答「有什么配饰可以搭配」等问题时给具体建议用
    accessories = [
        (i.name or "").strip() for i in items
        if (i.category == "配饰" or i.sub_category in ("帽子", "围巾", "腰带", "包包", "首饰"))
        and (i.name or "").strip()
    ]
    # 1.2：把上一轮对话带给模型，避免「换成裙子」时丢失前文语境
    history = _recent_history(db, session.session_id)
    content = llm_reply(user_text, weather_info, outfits, history=history,
                        accessories=accessories) if outfits else None

    if not content:
        if outfits:
            if _travel:
                tmin, tmax = _travel.get("temp_min"), _travel.get("temp_max")
                span = (f"{tmin}~{tmax}°C" if tmin is not None and tmax is not None
                        else f"{_travel.get('temp')}°C")
                content = (f"按 {_travel.get('city')} 未来 {_travel.get('day_count', 3)} 天天气"
                           f"（{span}，代表温度 {_travel.get('temp')}°C，26℃ 穿衣法）"
                           f"给你搭了 {len(outfits)} 套：")
            else:
                content = f"为你推荐 {len(outfits)} 套搭配："
        else:
            content = result.get("reason", "暂时没有满足条件的搭配，建议放宽约束。")
    return outfits, content


def _resolve_weather(req_weather, user):
    """确定本次会话使用的天气。

    优先级：前端显式传入 > 用户常用城市的实时天气 > 空（不编造温度）。
    返回 (weather, source)；source 为 realtime / passed / unavailable。
    """
    if req_weather is not None:
        return req_weather, "passed"

    city = (getattr(user, "city", "") or "").strip()
    if not city:
        return None, "unavailable"

    try:
        data = _fetch_weather(city)
    except Exception:
        data = None

    if not data or data.get("temp") is None:
        # 取不到实时天气时，明确标记不可用，而不是硬编码一个假温度
        class _Empty:
            def __init__(self, c):
                self.city = c
                self.temp = None
                self.rain = "未知"
                self.rain_probability = None
                self.source = "unavailable"

        return _Empty(city), "unavailable"

    class _W:
        def __init__(self, d):
            self.city = d.get("city") or city
            self.temp = d.get("temp")
            self.rain = d.get("rain") or "未知"
            self.temp_max = d.get("temp_max")
            self.temp_min = d.get("temp_min")
            self.rain_probability = d.get("rain_probability")
            self.source = d.get("source") or "open-meteo"

    return _W(data), "realtime"


# 出方案前实时天气刷新缓存：session_id -> (抓取时间, city, temp, rain, rain_prob, source)
# 用内存缓存而非数据库字段，避免改 schema；服务重启后缓存清空，下次自然重取。
_WEATHER_REFRESH_CACHE: dict[str, tuple] = {}


def _refresh_session_weather(db: Session, session: ChatSession, user: User) -> None:
    """出方案前刷新会话天气，避免复用旧会话时沿用建会话那一刻的过期温度。

    前端会话存在 localStorage 里会被长期复用，而页面天气是实时的；若不刷新，
    引擎会拿旧 temp（或 None→默认 22℃）推荐，出现「页面 19℃、卡片按 22℃ 搭」的错位。
    同一会话 10 分钟内只打一次天气 API，兼顾实时性与延迟。
    """
    from datetime import datetime, timedelta
    try:
        city = (getattr(user, "city", "") or getattr(session, "weather_city", "") or "").strip()
        if not city:
            return
        now = datetime.now()
        hit = _WEATHER_REFRESH_CACHE.get(session.session_id)
        if hit and now - hit[0] < timedelta(minutes=10) and hit[1] == city:
            _, _, temp, rain, prob, source = hit
            session.weather_temp = temp
            session.weather_rain = rain or "未知"
            session.weather_rain_prob = prob
            session.weather_source = source
            return

        data = _fetch_weather(city)
        if data and data.get("temp") is not None:
            temp = data.get("temp")
            rain = data.get("rain") or "未知"
            prob = data.get("rain_probability")
            source = data.get("source") or "realtime"
            session.weather_city = data.get("city") or city
            session.weather_temp = temp
            session.weather_rain = rain
            session.weather_rain_prob = prob
            session.weather_source = source
            _WEATHER_REFRESH_CACHE[session.session_id] = (now, city, temp, rain, prob, source)
    except Exception:
        # 天气刷新失败不影响主链路：保留会话原有天气，引擎按已知/默认处理
        pass


@router.post("/session", response_model=schemas.APIResponse)
def create_session(req: schemas.ChatSessionCreate, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == req.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    weather, weather_source = _resolve_weather(req.weather, user)
    session = ChatSession(
        session_id=utils.gen_id("s"),
        user_id=req.user_id,
        status="active",
        occasion=req.occasion or "",
        weather_city=weather.city if weather else (user.city or "福州"),
        weather_temp=weather.temp if weather and weather.temp is not None else None,
        # 天气未知时写「未知」，不要伪装成「无降雨」影响雨天鞋履判断
        weather_rain=weather.rain if (weather and weather.rain) else "未知",
        weather_rain_prob=getattr(weather, "rain_probability", None) if weather else None,
        weather_source=weather_source,
        temp_pref_color=req.temp_pref.color if req.temp_pref else "",
        temp_pref_style=req.temp_pref.style if req.temp_pref else "",
        constraints={},
        shown_outfit_ids=[],
        remaining_count=config.CHAT_SESSION_QUOTA,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return ok(data=_session_to_response(session))


# ---------- 锁定方案 + 单品级改良 ----------
_REFINE_VERB = ("换成", "改成", "换一件", "换条", "换个", "换双", "替换", "不要这个", "换掉")
_ORDINAL = {"一": 1, "二": 2, "三": 3, "1": 1, "2": 2, "3": 3}

_CAT_WORDS = [
    ("连衣裙", "连衣裙"), ("裤子", "下装"), ("长裤", "下装"), ("短裤", "下装"), ("裙", "下装"),
    ("外套", "外套"), ("风衣", "外套"), ("大衣", "外套"), ("羽绒服", "外套"), ("夹克", "外套"),
    ("鞋", "鞋"), ("靴", "鞋"), ("板鞋", "鞋"), ("皮鞋", "鞋"),
    ("配饰", "配饰"), ("围巾", "配饰"), ("包", "配饰"),
    ("上衣", "上装"), ("上装", "上装"), ("T恤", "上装"), ("衬衫", "上装"), ("毛衣", "上装"),
    ("针织", "上装"), ("卫衣", "上装"),
]


def _detect_refine(text: str):
    """识别「对某套满意，只想改某个单品」的意图。返回 (idx, category) 或 None。"""
    t = (text or "").strip()
    if not t:
        return None
    has_verb = any(v in t for v in _REFINE_VERB)
    cat = None
    for word, mapped in _CAT_WORDS:
        if word in t:
            cat = mapped
            break
    if not has_verb or not cat:
        return None

    idx = None
    # 方案 2 / 第 2 套 / LOOK 2 / 第二套
    m = re.search(r"(?:方案|第)\s*([1-3一二三])\s*(?:套|个)?", t) or _re.search(r"look\s*([1-3])", t, _re.I)
    if m:
        key = m.group(1)
        idx = _ORDINAL.get(key)
    return {"index": idx, "category": cat}


def _load_base_outfit(db: Session, session, index):
    """取出会话中最近一轮推荐的第 index 套（默认第 1 套）。"""
    msgs = db.query(ChatMessage).filter(
        ChatMessage.session_id == session.session_id,
        ChatMessage.action.in_(["recommend", "refine"]),
    ).order_by(ChatMessage.created_at.desc()).all()
    for m in msgs:
        outfits = (m.payload or {}).get("outfits") or []
        if not outfits:
            continue
        i = max(0, min((index or 1) - 1, len(outfits) - 1))
        return outfits[i]
    return None


def _item_dicts(db: Session, user_id: str) -> list[dict]:
    """衣橱单品 -> 引擎输入结构。"""
    rows = db.query(ClothingItem).filter(ClothingItem.user_id == user_id).all()
    return [{
        "item_id": i.item_id, "name": i.name, "category": i.category,
        "sub_category": i.sub_category, "color": i.color,
        "color_brightness": i.color_brightness, "season": i.season,
        "fit_snug": i.fit_snug, "fit_silhouette": i.fit_silhouette,
        "fabric": i.fabric, "style": i.style, "occasion": i.occasion or [],
        "warmth_level": i.warmth_level, "formality_level": i.formality_level,
        "image_url": i.image_url, "tags": i.tags or [],
    } for i in rows]


def _make_ctx(session, user) -> RecommendContext:
    """按会话状态构造推荐上下文（与 _run_recommend 保持一致）。"""
    return RecommendContext(
        user_id=session.user_id,
        occasion=session.occasion,
        weather_temp=session.weather_temp,
        weather_rain=session.weather_rain,
        weather_rain_prob=getattr(session, "weather_rain_prob", None),
        temp_pref={"color": session.temp_pref_color, "style": session.temp_pref_style}
        if (session.temp_pref_color or session.temp_pref_style) else {},
        constraints=session.constraints or {},
        shown_outfit_ids=session.shown_outfit_ids or [],
        body_shape=user.body_shape or "匀称",
        long_term_prefs={
            "color": user.pref_color, "style": user.pref_style,
            "comfort": user.pref_comfort, "occasion": user.pref_occasion,
            "fabric": user.pref_fabric,
        },
    )


@router.post("/{session_id}/message", response_model=schemas.APIResponse)
def send_message(session_id: str, req: schemas.ChatMessagePayload, request: Request, db: Session = Depends(get_db)):
    session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    security.assert_owner(session.user_id, request)
    if session.status == "ended":
        raise HTTPException(status_code=400, detail="会话已结束")

    user = db.query(User).filter(User.user_id == session.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    action = req.action
    payload = req.payload or {}

    # 1.5 意图分流收到后端单点：前端只传原文（action="auto"）时由后端统一判定
    if action == "auto":
        action = _classify_intent(payload.get("text", ""))
        if action == "refine":
            payload = {**payload, "refine": _detect_refine(payload.get("text", ""))}

    # 旅游场景：会话级「等待目的地」状态。用户先说「想去旅游」但没给城市时反问，
    # 下一句补上城市名后，即使被判成别的意图也要回到旅游流程。
    constraints = session.constraints or {}
    travel_pending = bool(constraints.get("travel_pending"))
    text_now = payload.get("text", "")
    if travel_pending and action in ("recommend", "free_text", "knowledge", "travel"):
        action = "travel"

    # 记录用户消息
    msg = ChatMessage(
        message_id=utils.gen_id("m"),
        session_id=session_id,
        role="user",
        action=action,
        content=payload.get("text", ""),
        payload=payload,
    )
    db.add(msg)

    # 旅游场景：目的地未来天气驱动推荐。
    # 用户去的是另一座城市，绝不能用当前城市的实时天气，也不用前端传来的天气。
    travel_info: dict | None = None
    if action == "travel":
        dest = _extract_dest_city(text_now)
        if not dest:
            db.commit()
            return ok(data=_travel_clarify_response(session, text_now))
        days = _extract_travel_days(text_now) or int(constraints.get("travel_days", 3))
        forecast = _fetch_forecast(dest, days)
        summary = forecast_summary(forecast, days) if forecast else None
        if not summary:
            # 抓不到未来天气时给明确提示，不伪造温度、也不回落到当前城市（会误导）
            db.commit()
            return ok(data={
                "message_id": utils.gen_id("m"), "role": "agent", "action": "echo",
                "content": (f"暂时取不到 {dest} 未来 {days} 天的天气，怕给你搭错，"
                            f"请稍会儿再试或换个城市。"),
                "outfits": [], "remaining_count": session.remaining_count,
            })
        # 用目的地代表温度覆盖会话天气（26℃ 法按体感均温计算）
        session.weather_city = summary["city"]
        session.weather_temp = summary["temp"]
        session.weather_rain = summary["rain"]
        session.weather_rain_prob = summary["rain_probability"]
        session.weather_source = "travel-forecast"
        session.occasion = session.occasion or "旅游"
        travel_info = summary
        # 清掉等待标记，记录旅游上下文供回复文案标注
        constraints["travel_pending"] = False
        constraints["travel_days"] = days
        constraints["travel"] = summary
        session.constraints = constraints
        action = "recommend"  # 转入既有推荐链路，ctx/过滤/打分/文案全部复用

    # 出方案类动作：先确定本次会话天气，再判断澄清与推荐。
    # 优先级：前端传来的真实天气（浏览器能联网取到）> 后端自取实时天气 > 会话旧值。
    # 避免「页面 19℃、卡片按 mock 22℃ 搭」的错位。
    # 注意：旅游场景已用目的地天气覆盖，必须跳过这里的当前城市天气覆盖。
    if action in ("recommend", "refine", "compare", "reject") and not travel_info:
        passed_w = payload.get("weather")
        if (isinstance(passed_w, dict) and passed_w.get("temp") is not None
                and str(passed_w.get("source", "")).startswith("open-meteo")):
            session.weather_city = passed_w.get("city") or session.weather_city
            session.weather_temp = passed_w.get("temp")
            session.weather_rain = passed_w.get("rain") or "未知"
            session.weather_rain_prob = passed_w.get("rain_probability")
            session.weather_source = "realtime"
        else:
            _refresh_session_weather(db, session, user)

    # 先识别「锁定某套 + 只改一个单品」的改良意图
    if action == "free_text":
        _rf = _detect_refine(payload.get("text", ""))
        if _rf:
            action = "refine"
            payload = {**payload, "refine": _rf}

    # 处理自由文本解析
    if action == "free_text":
        parsed_action, parsed_payload = _parse_free_text(payload.get("text", ""))
        if parsed_action != "free_text":
            action = parsed_action
            payload = parsed_payload

    # 2.2 主动澄清：首次推荐且场合/天气都缺时先反问，不凭空猜一套还照样扣额度
    _needs = _clarify_needs(payload.get("text", ""), session) if action == "recommend" else []
    if _needs:
        db.commit()
        return ok(data=_clarify_response(session, _needs))

    # 1.3 额度入口拦截：先校验后扣减，不再「扣到 0 还继续服务」
    cost = _quota_cost(action)
    if cost and session.remaining_count < cost:
        db.commit()
        return ok(data={
            "message_id": utils.gen_id("m"),
            "role": "agent",
            "action": "quota_exhausted",
            "content": (f"本次会话的推荐额度已用完（剩余 {session.remaining_count} 次，"
                        f"本次需要 {cost} 次）。点击「新对话」即可重置额度继续使用。"),
            "outfits": [],
            "remaining_count": session.remaining_count,
            "quota": {"denied": True, "required": cost, "remaining": session.remaining_count},
        })

    # 2.4 反事实对比：纯重算，不调模型，不扣额度
    if action == "compare":
        res = _run_compare(db, request, session, user, payload)
        db.commit()
        return ok(data={
            "message_id": utils.gen_id("m"),
            "role": "agent",
            "action": "compare",
            "content": res["content"],
            "compare": res.get("compare"),
            "outfits": res.get("outfits") or [],
            "remaining_count": session.remaining_count,
        })

    # 处理约束更新类动作
    if action == "exclude_color":
        colors = payload.get("colors", [])
        session.constraints = session.constraints or {}
        session.constraints.setdefault("exclude_colors", [])
        session.constraints["exclude_colors"] = list(set(session.constraints["exclude_colors"]) | set(colors))
        db.commit()
        return ok(data={
            "message_id": utils.gen_id("m"),
            "role": "agent",
            "action": "constraint_updated",
            "content": f"已排除颜色：{', '.join(colors)}，重新推荐中...",
            "outfits": [],
            "remaining_count": session.remaining_count,
            "constraints": session.constraints,
        })

    if action == "replace_category":
        session.constraints = session.constraints or {}
        session.constraints.setdefault("replace", [])
        session.constraints["replace"].append({"from": payload.get("from", ""), "to": payload.get("to", "")})
        db.commit()
        return ok(data={
            "message_id": utils.gen_id("m"),
            "role": "agent",
            "action": "constraint_updated",
            "content": f"已将「{payload.get('from')}」替换为「{payload.get('to')}」倾向，重新推荐中...",
            "outfits": [],
            "remaining_count": session.remaining_count,
            "constraints": session.constraints,
        })

    if action == "temp_pref_color":
        session.temp_pref_color = payload.get("color", "")
        db.commit()
        return ok(data={
            "message_id": utils.gen_id("m"),
            "role": "agent",
            "action": "constraint_updated",
            "content": f"已记录你偏好「{session.temp_pref_color}」，重新推荐中...",
            "outfits": [],
            "remaining_count": session.remaining_count,
        })

    if action == "temp_pref_style":
        session.temp_pref_style = payload.get("style", "")
        db.commit()
        return ok(data={
            "message_id": utils.gen_id("m"),
            "role": "agent",
            "action": "constraint_updated",
            "content": f"已记录你偏好「{session.temp_pref_style}」风格，重新推荐中...",
            "outfits": [],
            "remaining_count": session.remaining_count,
        })

    # knowledge：知识问答（BM25 + LLM）
    if action == "knowledge":
        # 构造用户上下文：天气 + 长期偏好 + 衣橱概况，让无检索命中时也能具体作答
        _ctx_lines: list[str] = []
        if session.weather_city or session.weather_temp is not None:
            _ctx_lines.append(
                f"当前天气：{session.weather_city or '未知城市'} "
                f"{session.weather_temp if session.weather_temp is not None else '温度未知'}°C，"
                f"降雨：{session.weather_rain or '未知'}（来源：{getattr(session, 'weather_source', '未知')}）"
            )
        if session.occasion:
            _ctx_lines.append(f"本次场合：{session.occasion}")
        _prefs = [
            f"{k}={getattr(user, 'pref_' + k, '')}"
            for k in ("color", "style", "comfort", "occasion", "fabric")
            if getattr(user, "pref_" + k, "")
        ]
        if _prefs:
            _ctx_lines.append("用户长期偏好：" + "、".join(_prefs))
        _items = _item_dicts(db, session.user_id)
        if _items:
            _cats: dict[str, int] = {}
            for _it in _items:
                _cats[_it["category"]] = _cats.get(_it["category"], 0) + 1
            _ctx_lines.append(
                f"用户衣橱共 {len(_items)} 件：" +
                "、".join(f"{k}{v}件" for k, v in _cats.items())
            )
        _closet_context = "\n".join(_ctx_lines)
        query = payload.get("text", "")
        if not query:
            db.commit()
            return ok(data={
                "message_id": utils.gen_id("m"),
                "role": "agent",
                "action": "knowledge",
                "content": "请输入你想了解的穿搭问题。",
                "passages": [],
                "llm_enabled": config.is_llm_enabled(),
            })

        kb: KnowledgeIndex = request.app.state.kb
        passages = kb.search(query, top_k=5)

        # 1.2 多轮上下文：把最近几轮对话带给模型，支持「那秋天呢」这类追问
        history = _recent_history(db, session_id, exclude_message_id=msg.message_id)

        # 检索是增强项而非开关：有模型就调，片段为空时让模型结合上下文与常识作答。
        llm_on = config.is_llm_enabled()
        answer = None
        if llm_on:
            answer = ask(query, passages, history=history, context=_closet_context)
            if answer is None and passages:
                answer = ask(query, passages, history=history, context=_closet_context,
                             timeout=config.LLM_TIMEOUT * 1.5)

        content = ""
        if answer:
            content = answer["answer"]
        elif passages:
            content = "我找到以下参考资料：\n" + "\n\n".join(
                f"[{i+1}] {p['title']}\n{p['text'][:300]}...\n出处：{p['source']}"
                for i, p in enumerate(passages[:3])
            )
        elif _closet_context:
            content = ("根据你目前的天气、偏好和衣橱情况，我暂时没法给出可靠建议。"
                       "可以换个更具体的问法，比如「这种天气通勤怎么穿」或「配饰怎么搭」。")
        else:
            content = "可以换个更具体的问法，比如「今天通勤怎么穿」或「配饰怎么搭」。"

        # 1.4 显式四态：模型生成 / 原文摘录 / 降级简答 / 无资料，并给出降级原因
        if answer:
            answer_source, degraded, degraded_reason = "llm", False, ""
        elif passages and not llm_on:
            answer_source, degraded, degraded_reason = "passages", True, "no_key"
        elif passages:
            answer_source, degraded, degraded_reason = "passages", True, "llm_failed"
        elif llm_on:
            answer_source, degraded, degraded_reason = "none", True, "llm_failed_no_kb"
        else:
            answer_source, degraded, degraded_reason = "none", True, "no_key_no_kb"

        agent_msg = ChatMessage(
            message_id=utils.gen_id("m"),
            session_id=session_id,
            role="agent",
            action="knowledge",
            content=content,
            payload={
                "query": query,
                "passages": passages[:5],
                "llm_answer": answer,
                "answer_source": answer_source,
                "degraded": degraded,
                "degraded_reason": degraded_reason,
            },
        )
        db.add(agent_msg)
        db.commit()
        return ok(data={
            "message_id": agent_msg.message_id,
            "role": "agent",
            "action": "knowledge",
            "content": content,
            "passages": passages[:5],
            "llm_enabled": config.is_llm_enabled(),
            "llm_answer": answer,
            "answer_source": answer_source,
            "degraded": degraded,
            "degraded_reason": degraded_reason,
            "remaining_count": session.remaining_count,
        })

    # accept / reject 简单响应
    if action == "accept":
        db.commit()
        return ok(data={
            "message_id": utils.gen_id("m"),
            "role": "agent",
            "action": "accept",
            "content": "已记录你的选择，祝你穿出好心情！",
            "outfits": [],
            "remaining_count": session.remaining_count,
        })

    if action == "reject":
        # 换一套：成本统一由 _run_recommend 扣减，调用方只传 cost（1.3-E）
        outfits, content = _run_recommend(db, request, session, user,
                                          deduct=config.CHAT_COST_REJECT,
                                          user_text=payload.get("text", ""))
        agent_msg = ChatMessage(
            message_id=utils.gen_id("m"),
            session_id=session_id,
            role="agent",
            action="recommend",
            content=content,
            outfits=[o["outfit_id"] for o in outfits],
            payload={"outfits": outfits},
        )
        db.add(agent_msg)
        db.commit()
        db.refresh(session)
        return ok(data={
            "message_id": agent_msg.message_id,
            "role": "agent",
            "action": "recommend",
            "content": agent_msg.content,
            "outfits": outfits,
            "remaining_count": session.remaining_count,
        })

    if action == "refine":
        info = payload.get("refine") or {}
        base = _load_base_outfit(db, session, info.get("index"))
        if not base:
            db.commit()
            return ok(data={
                "message_id": utils.gen_id("m"), "role": "agent", "action": "echo",
                "content": "还没找到可以沿用的方案，先让我给你推荐几套吧。",
                "outfits": [], "remaining_count": session.remaining_count,
            })

        all_items = _item_dicts(db, session.user_id)
        ctx = _make_ctx(session, user)
        eng = request.app.state.engine
        res = eng.refine(base.get("items", []), info.get("category"), all_items, ctx)

        if not res.get("outfit"):
            db.commit()
            return ok(data={
                "message_id": utils.gen_id("m"), "role": "agent", "action": "refine",
                "content": res.get("reason", "暂时没有可替换的单品。"),
                "outfits": [], "remaining_count": session.remaining_count,
            })

        outfit = res["outfit"]
        outfit["reason"] = {"summary": f"在「{base.get('outfit_id', '')[:6]}」这套基础上，"
                                       f"把「{res['from_name']}」换成了「{res['to_name']}」。",
                            "points": res.get("points", [])}
        resp = eng.to_response(outfit)

        agent_msg = ChatMessage(
            message_id=utils.gen_id("m"), session_id=session_id, role="agent",
            action="refine",
            content=f"保留其余 {res['kept_count']} 件，只替换了「{res['to_name']}」。",
            outfits=[resp["outfit_id"]], payload={"outfits": [resp]},
        )
        db.add(agent_msg)
        db.commit()
        db.refresh(session)
        return ok(data={
            "message_id": agent_msg.message_id, "role": "agent", "action": "refine",
            "content": agent_msg.content, "outfits": [resp],
            "remaining_count": session.remaining_count,
        })

    # recommend：核心推荐链路
    if action != "recommend":
        db.commit()
        return ok(data={
            "message_id": utils.gen_id("m"),
            "role": "agent",
            "action": "echo",
            "content": "已收到，请输入「推荐」或点击推荐按钮获取搭配。",
            "outfits": [],
            "remaining_count": session.remaining_count,
        })

    outfits, content = _run_recommend(db, request, session, user,
                                     user_text=payload.get("intent") or payload.get("text", ""))

    # 记录 Agent 消息
    agent_msg = ChatMessage(
        message_id=utils.gen_id("m"),
        session_id=session_id,
        role="agent",
        action="recommend",
        content=content,
        outfits=[o["outfit_id"] for o in outfits],
        payload={"outfits": outfits},
    )
    db.add(agent_msg)
    db.commit()
    db.refresh(session)

    return ok(data={
        "message_id": agent_msg.message_id,
        "role": "agent",
        "action": "recommend",
        "content": agent_msg.content,
        "outfits": outfits,
        "remaining_count": session.remaining_count,
    })


@router.get("/sessions", response_model=schemas.APIResponse)
def list_sessions(user_id: str, request: Request, limit: int = 20, offset: int = 0,
                  db: Session = Depends(get_db)):
    """按用户列出历史会话（倒序）。归属校验复用统一的 assert_owner 写法。"""
    security.assert_owner(user_id, request)

    base = db.query(ChatSession).filter(ChatSession.user_id == user_id)
    total = base.count()

    rows = (base.order_by(ChatSession.updated_at.desc())
                .offset(max(0, offset))
                .limit(min(max(1, limit), 50))
                .all())

    return ok(data={
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [_session_brief(s) for s in rows],
    })


@router.get("/{session_id}", response_model=schemas.APIResponse)
def get_session(session_id: str, request: Request, db: Session = Depends(get_db)):
    session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 只能查看自己的会话，防止按 session_id 越权读取他人对话
    security.assert_owner(session.user_id, request)
    return ok(data=_session_to_response(session))


@router.post("/{session_id}/end", response_model=schemas.APIResponse)
def end_session(session_id: str, request: Request, db: Session = Depends(get_db)):
    session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 只能结束自己的会话，防止越权终止他人咨询
    security.assert_owner(session.user_id, request)
    session.status = "ended"
    db.commit()
    return ok(data={"session_id": session_id, "status": "ended"})

# ============ 2.5 流式问答（SSE） ============
def _sse(obj: dict) -> str:
    """SSE 单帧：必须是 `data: {...}\n\n`，缺换行会让客户端一直等行结束。"""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@router.post("/{session_id}/message/stream")
def send_message_stream(session_id: str, user_id: str,
                        req: schemas.ChatMessagePayload, request: Request):
    """知识问答的流式版本。

    与同步版共用同一套三态语义：模型可用则逐字推送，不可用则一次性推送原文摘录
    并带上 degraded 标记，前端据此显示「原文摘录（模型未启用）」。
    落库放在流结束之后，避免 SSE 过程中持有写事务。
    """
    security.assert_owner(user_id, request)

    text = ((req.payload or {}).get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="内容不能为空")

    from app.db import SessionLocal

    db = SessionLocal()
    try:
        session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="会话不存在")
        if session.user_id != user_id:
            raise HTTPException(status_code=403, detail="无权访问该会话")
        user = db.query(User).filter(User.user_id == session.user_id).first()

        kb: KnowledgeIndex = request.app.state.kb
        passages = kb.search(text, top_k=5)
        history = _recent_history(db, session_id)
        llm_on = config.is_llm_enabled()

        # 与同步版一致：构造天气/画像/衣橱上下文，检索未命中也能具体回答。
        _ctx_lines: list[str] = []
        if session.weather_city or session.weather_temp is not None:
            _ctx_lines.append(
                f"当前天气：{session.weather_city or '未知城市'} "
                f"{session.weather_temp if session.weather_temp is not None else '温度未知'}°C，"
                f"降雨：{session.weather_rain or '未知'}"
            )
        if session.occasion:
            _ctx_lines.append(f"本次场合：{session.occasion}")
        _prefs = [
            f"{k}={getattr(user, 'pref_' + k, '')}"
            for k in ("color", "style", "comfort", "occasion", "fabric")
            if getattr(user, "pref_" + k, "")
        ]
        if _prefs:
            _ctx_lines.append("用户长期偏好：" + "、".join(_prefs))
        _items = db.query(ClothingItem).filter(ClothingItem.user_id == session.user_id).all()
        if _items:
            _cats: dict[str, int] = {}
            for _it in _items:
                _cats[_it.category] = _cats.get(_it.category, 0) + 1
            _ctx_lines.append(
                f"用户衣橱共 {len(_items)} 件：" +
                "、".join(f"{k}{v}件" for k, v in _cats.items())
            )
        _closet_context = "\n".join(_ctx_lines)

        user_msg = ChatMessage(
            message_id=utils.gen_id("m"), session_id=session_id,
            role="user", action="knowledge", content=text,
        )
        db.add(user_msg)
        db.commit()
        uid_for_log = session.user_id
    finally:
        db.close()

    def _gen():
        from app.agent import llm as _llm

        src_stream = None
        if llm_on:
            src_stream = _llm.ask_stream(text, passages, history=history,
                                         context=_closet_context)

        if src_stream is not None:
            answer_source, degraded, reason = "llm", False, ""
        elif passages and not llm_on:
            answer_source, degraded, reason = "passages", True, "no_key"
        elif passages:
            answer_source, degraded, reason = "passages", True, "llm_failed"
        elif llm_on:
            answer_source, degraded, reason = "none", True, "llm_failed_no_kb"
        else:
            answer_source, degraded, reason = "none", True, "no_key_no_kb"

        yield _sse({"type": "meta", "answer_source": answer_source,
                    "degraded": degraded, "degraded_reason": reason,
                    "passages": [{"title": p["title"], "doc": p["doc"],
                                  "source": p["source"]} for p in passages[:5]]})

        collected: list[str] = []
        if src_stream is not None:
            try:
                for delta in src_stream:
                    collected.append(delta)
                    yield _sse({"type": "delta", "text": delta})
            except Exception:
                answer_source, degraded, reason = "passages", True, "stream_failed"

        content = "".join(collected).strip()
        if not content:
            if passages:
                content = "\n\n".join(
                    "%s（%s）\n%s" % (p["title"], p["source"], p["text"][:400])
                    for p in passages[:3]
                )
                if answer_source == "llm":
                    answer_source, degraded, reason = "passages", True, "llm_failed"
            else:
                content = ("可以换个更具体的问法，比如「今天通勤怎么穿」或「配饰怎么搭」，"
                           "我会结合天气和你的衣橱来答。")
                answer_source, degraded, reason = "none", True, "no_answer"
            yield _sse({"type": "delta", "text": content})

        message_id = utils.gen_id("m")
        db2 = SessionLocal()
        try:
            db2.add(ChatMessage(
                message_id=message_id, session_id=session_id, role="agent",
                action="knowledge", content=content,
                payload={"answer_source": answer_source, "degraded": degraded,
                         "degraded_reason": reason,
                         "passages": passages[:5], "llm_answer": None},
            ))
            sess = db2.query(ChatSession).filter(ChatSession.session_id == session_id).first()
            remaining = sess.remaining_count if sess else None
            db2.commit()
        finally:
            db2.close()

        yield _sse({"type": "done", "message_id": message_id,
                    "answer_source": answer_source, "degraded": degraded,
                    "degraded_reason": reason, "remaining_count": remaining})

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})
