# -*- coding: utf-8 -*-
"""对话穿搭 Agent 模块（计划书 13.4，核心）。

POST /chat/session                创建对话会话
POST /chat/{session_id}/message   发送消息
GET  /chat/{session_id}           获取会话状态
POST /chat/{session_id}/end       结束会话
"""
import json
import re
from functools import lru_cache

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app import config, schemas, security, utils
from app.agent.engine import RecommendContext, outfit_core_signature
from app.api.weather import _fetch_weather, _fetch_forecast, forecast_summary, _CITY_COORDS, _geocode_city
from app.agent.kb import KnowledgeIndex
from app.agent.llm import (ask, classify_intent, check_domain, classify, weather_reply,
                           reply as llm_reply, trend as llm_trend)
from app.api import ok
from app.db import (ChatMessage, ChatSession, ClothingItem, FashionInspo,
                    OutfitRecord, User, get_db)

router = APIRouter()


def _message_outfits(m: ChatMessage) -> list:
    """还原消息里的完整方案快照，供历史会话回放。

    chat_messages.outfits 只存 outfit_id 字符串数组；完整对象在
    payload["outfits"]（以及 outfit_records 快照表）。前端渲染卡片需要
    items / reason / outfit_id，直接把 id 数组传过去只能渲染出空壳。
    """
    ids = m.outfits or []
    if not ids:
        return []
    if isinstance(ids[0], dict):  # 兼容历史上直接存对象的数据
        return ids
    full: list = []
    try:
        p = m.payload or {}
        if isinstance(p, dict):
            cand = p.get("outfits")
            if isinstance(cand, list) and cand and isinstance(cand[0], dict):
                full = cand
    except Exception:
        pass
    if not full:
        return [{"outfit_id": str(x)} for x in ids]
    by_id = {str(o.get("outfit_id")): o for o in full if isinstance(o, dict)}
    return [by_id.get(str(x), {"outfit_id": str(x)}) for x in ids]


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
                "outfits": _message_outfits(m),
                "images": (m.payload or {}).get("images", []) if m.action == "trend" else [],
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
                   "运动", "健身", "正式", "休闲", "度假", "逛街", "见家长",
                   "旅游", "旅行", "出游")
_CATEGORY_WORDS = ("上装", "下装", "外套", "连衣裙", "鞋", "配饰", "衬衫", "T恤",
                   "毛衣", "针织", "裤", "裙", "风衣", "大衣", "西装", "卫衣")
_COLOR_WORDS = ("黑", "白", "灰", "红", "蓝", "绿", "黄", "棕", "米", "粉", "紫", "卡其", "藏青")


_WEATHER_WORDS = ("度", "℃", "°", "冷", "热", "凉", "下雨", "雨", "晴", "雪", "暖和")

# 氛围模糊词：用户给的是跨维度的氛围描述，无法直接落位到颜色/材质/款式中的
# 任一单一维度。命中后应反问「你想的是哪种」，而不是硬猜成某个临时偏好。
# 例：「温柔」可能是颜色浅、材质柔、或换成裙子/外套 —— 三者的推荐方向完全不同。
_VIBE_WORDS = ("温柔", "柔和", "清爽", "可爱", "知性", "舒服", "慵懒", "文艺", "淑女", "仙气")


def _detect_vibe(text: str) -> str | None:
    """从文本里抽出氛围模糊词；命中返回该词，否则 None。"""
    t = (text or "").strip()
    return next((w for w in _VIBE_WORDS if w in t), None)


def _has_explicit_dimension(text: str) -> bool:
    """判断这句话里是否已经给了可落位的明确维度（颜色/品类/场合/温度）。

    有明确维度时，「温柔」就不再是唯一模糊点，不追问，避免啰嗦。
    """
    t = (text or "").strip()
    return bool(
        any(w in t for w in _COLOR_WORDS)
        or any(w in t for w in _CATEGORY_WORDS)
        or any(w in t for w in _OCCASION_WORDS)
        or any(w in t for w in _WEATHER_WORDS)
    )


def _occasion_confirmed(session: ChatSession) -> bool:
    """场合是否是「用户明确提供过」的。

    首页预览会往 session 里写默认场合（「日常」/旧版「通勤」），那是系统预设，
    不是用户说的。因此绝不能拿 session.occasion 是否有值来判断该不该反问——
    唯一可信的依据是这个由用户回答/自带场合词时才写入的标记。
    """
    return bool((session.constraints or {}).get("occasion_confirmed"))


def _clarify_needs(text: str, session: ChatSession) -> list[str]:
    """2.2 判断这次【对话推荐】前该不该先反问，并说明缺什么。

    场合：唯一标准是「用户有没有提供过场合信息」——
      · 用户在本对话里回答过场合 → occasion_confirmed=True，不再问；
      · 这句话自带场合词 → 交给回收段落位，不用问；
      · 除此之外一律反问，包括：会话残留首页预设场合、已经展示过方案、
        文本很长但通篇没提场合。系统绝不替用户凭空定场合。

    温度：保持原逻辑，天气未知且本句没给温度/城市时反问。
    首页预览走 payload.preview，不进本函数。
    """
    t = (text or "").strip()
    needs: list[str] = []
    if not _occasion_confirmed(session) and not any(w in t for w in _OCCASION_WORDS):
        needs.append("occasion")
    if session.weather_temp is None and not any(w in t for w in _WEATHER_WORDS):
        needs.append("weather")
    return needs


def _should_clarify(text: str, session: ChatSession) -> bool:
    return bool(_clarify_needs(text, session))


def _match_occasion(text: str) -> str | None:
    """从用户回答里抽出场合词（澄清选项即来自 _OCCASION_WORDS）。

    用户在澄清卡点「聚会」「休闲」后，前端把选项文字当作普通消息发来，
    必须把场合写回 session，否则下一轮又判缺场合、无限反问。
    """
    t = (text or "").strip()
    return next((w for w in _OCCASION_WORDS if w in t), None)


def _match_temperature(text: str) -> float | None:
    """从「18-25℃」「22度」这类回答里抽出可用于推荐的体感温度。"""
    import re
    t = (text or "").strip()
    # 区间优先，如 18-25℃ 取中值；否则单值正则会先抢走末尾的 25℃
    m = re.search(r"(\d+)\s*[-~到至]\s*(\d+)\s*(?:℃|°C|度)", t)
    if m:
        return (int(m.group(1)) + int(m.group(2))) / 2
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:℃|°C|度)", t)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def _clarify_response(session: ChatSession, needs: list[str] | None = None) -> dict:
    needs = needs or ["occasion", "weather"]
    options: dict = {}
    parts: list[str] = []
    if "occasion" in needs:
        options["occasion"] = ["通勤", "约会", "出差", "面试", "休闲", "聚会", "旅游"]
        parts.append("今天是什么场合")
    if "weather" in needs:
        options["temperature"] = ["10℃以下", "10-18℃", "18-25℃", "25℃以上"]
        options["weather_hint"] = "也可以直接说城市，我来取实时温度"
        parts.append("大概多少度或哪个城市")
    question = "想给你搭得准一点，先确认：" + "；".join(parts) + "？"
    # 记住这轮在等澄清：下一句即使不是枚举词（如「看演唱会」「朋友生日宴」），
    # 也要当作回答落位，否则选项没覆盖的场景会无限反问。
    _set_constraint(session, "clarify_pending", {"missing": needs})
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
    _set_constraint(session, "travel_pending", True)
    _set_constraint(session, "travel_days", days)
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


# ---------- 2.2b 氛围澄清 ----------
def _set_constraint(session: ChatSession, key: str, value) -> None:
    """写会话约束。JSON 字段无 MutableDict，原地改 dict 不会触发 SQLAlchemy
    变更跟踪、commit 会静默丢字段（vibe_pending 曾因此写不进 DB，导致澄清
    回收失效、死循环）。统一「读副本 → 改 → 整体赋值」规避。
    """
    d = dict(session.constraints or {})
    d[key] = value
    session.constraints = d


def _pop_constraint(session: ChatSession, key: str) -> None:
    d = dict(session.constraints or {})
    d.pop(key, None)
    session.constraints = d


# 用户给了「温柔/柔和」这类跨维度氛围词，无法确定他是要颜色浅、材质柔还是换款式，
# 反问落位，而不是硬猜成某个临时偏好（硬猜方向常错：温柔≠设计款）。
_VIBE_OPTIONS = ["颜色浅一点", "材质柔和一点", "款式换柔和些（如裙子/外套）", "都行，按整体氛围来"]


def _vibe_clarify_response(session: ChatSession, vibe_word: str) -> dict:
    question = (f"你说的「{vibe_word}」我更想先问清楚一点：你更偏向哪种方向？"
                f"这样我能搭得更贴你的意思。")
    return {
        "message_id": utils.gen_id("m"),
        "role": "agent",
        "action": "clarify",
        "content": question,
        "outfits": [],
        "remaining_count": session.remaining_count,
        "clarify": {
            "missing": ["vibe"],
            "vibe_word": vibe_word,
            "question": question,
            "options": {"vibe": _VIBE_OPTIONS},
        },
    }


def _apply_vibe_answer(session: ChatSession, text: str) -> str | None:
    """把用户对氛围追问的回答映射到临时偏好/约束；返回命中的落位方向，未命中 None。

    - 颜色浅一点    -> temp_pref_color = 亮色（现成槽位，立即生效）
    - 材质柔和一点  -> constraints.vibe_note（带进 LLM 上下文，引擎无面料偏好槽位）
    - 款式换柔和些  -> constraints.vibe_note（同上）
    - 都行          -> 不落位，返回 "overall"，把「温柔」留给 LLM 自由发挥
    """
    t = (text or "").strip()
    if any(w in t for w in ("颜色", "浅", "亮", "色")):
        session.temp_pref_color = "亮色"
        return "颜色浅一点"
    if any(w in t for w in ("材质", "面料", "料子")):
        _set_constraint(session, "vibe_note", "材质要柔和（棉、麻、丝、针织等亲肤面料优先）")
        return "材质柔和一点"
    if any(w in t for w in ("款式", "裙子", "裙", "外套", "大衣", "换")):
        _set_constraint(session, "vibe_note", "往柔和款式方向改（如裙子、开衫、垂坠感单品）")
        return "款式换柔和些"
    if any(w in t for w in ("都行", "整体", "氛围", "随便", "都行", "随意")):
        return "overall"
    return None


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
    """动作 -> 额度成本（1.3 语义显式化，统一读配置）。

    额度总开关关闭时恒为 0：同一会话无限对话，拦截与扣减自然失效。
    """
    if not config.CHAT_QUOTA_ENABLED:
        return 0
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
# 要「可照着穿的方案」的强信号。这类问法必须判 recommend 出卡片，
# 不能被问号/「什么」截成知识问答（知识问答只有文字+文献，没有卡片）。
# 含前端 _looksLikeKnowledgeQuestion 的全部推荐词，两边口径一致。
_RECOMMEND_WORDS = (
    "推荐", "搭一套", "搭配一下", "帮我搭", "来一套", "换一批", "配一套",
    "穿什么", "穿啥", "穿哪", "配什么", "配啥", "搭什么", "搭啥",
    "怎么穿", "怎么搭", "配饰可以搭", "配饰怎么搭", "穿点什么", "穿点啥",
    "穿的好看", "穿得好看", "穿的合适", "穿得合适",
)
# 「推荐」单独出现时可能指向任意领域（推荐电影/餐厅/股票），
# 必须有穿搭语境才当强推荐信号，否则交给白名单门禁判域外。
_NON_CLOTHING_RECOMMEND = (
    "电影", "电视剧", "小说", "书", "餐厅", "饭店", "歌", "音乐", "游戏",
    "股票", "基金", "景点", "旅游地", "城市", "专业", "学校", "工作", "软件",
)


def _has_recommend_signal(text: str) -> bool:
    """是否是要穿搭方案的信号（排除「推荐电影」这类域外语境）。"""
    t = (text or "").strip()
    if not t:
        return False
    if any(w in t for w in _NON_CLOTHING_RECOMMEND):
        return False
    return any(w in t for w in _RECOMMEND_WORDS)


# 关键区分：裸「推荐/搭配一下/喜欢」是泛化动作词，单独出现不足以证明在域——
# 「推荐一个旅行目的地」「搭配今天的菜谱」「喜欢穿铠甲」都会误命中（2026-11-18 实测）。
# 只有带明确衣物实体/穿搭方案短语/套号，才算「强在域信号」，可由本地安全锁定、不必等 LLM；
# 其余一律交给 LLM 做开放集领域判定（实测 check_domain/classify_intent 对这些域外句全判 out）。
_STRONG_RECOMMEND_PHRASES = (
    "穿什么", "穿啥", "穿哪", "配什么", "配啥", "搭什么", "搭啥",
    "怎么穿", "怎么搭", "来一套", "换一批", "配一套", "搭一套",
    "帮我搭一套", "帮我搭一套衣服",  # 裸「帮我搭」是泛化词（帮我搭配菜谱/搭桥），必须带「一套」
    "穿点什么", "穿点啥", "穿的好看", "穿得好看", "穿的合适", "穿得合适",
    "配饰可以搭", "配饰怎么搭",
)
_STRONG_DOMAIN_ENTITIES = (
    "衣服", "服装", "上衣", "下装", "外套", "裤子", "裙子", "鞋子", "配饰", "围巾",
    "衬衫", "牛仔裤", "T恤", "t恤", "风衣", "毛衣", "针织衫", "卫衣", "连衣裙", "西装",
    "夹克", "羽绒服", "运动鞋", "靴子", "衣橱", "衣柜", "穿搭",
)


def _has_strong_domain_signal(text: str) -> bool:
    """本地可安全锁定为「穿搭在域」的强信号：衣物实体 / 方案短语 / 套号。

    与 _has_recommend_signal 的区别：后者含「推荐」「搭配一下」等泛化词，
    会把「推荐旅行目的地」「搭配菜谱」误判在域，不能作为免 LLM 短路的依据。
    """
    t = (text or "").strip()
    if not t:
        return False
    if any(w in t for w in _STRONG_RECOMMEND_PHRASES):
        return True
    if any(w in t for w in _STRONG_DOMAIN_ENTITIES):
        return True
    if _extract_outfit_index(t) is not None:
        return True
    return False

# ---------- 与穿搭无关的问题：委婉拒答 ----------
# 只列明显属于其他领域的词，避免误伤「吃完饭穿什么」这类仍要出方案的话。
_OFF_TOPIC_WORDS = (
    # 美食 / 餐饮
    "吃什么", "吃啥", "今天吃", "午餐", "晚餐", "早餐", "夜宵", "菜单", "菜谱",
    "食谱", "美食", "餐厅", "外卖", "做饭", "做菜", "卡路里",
    # 星座 / 命理
    "星座", "运势", "塔罗", "算命", "八字", "风水", "周公解梦", "生肖",
    "白羊座", "金牛座", "双子座", "巨蟹座", "狮子座", "处女座", "天秤座",
    "天蝎座", "射手座", "摩羯座", "水瓶座", "双鱼座",
    # 其他典型无关领域
    "股票", "基金", "炒股", "数学题", "作业", "论文", "考试", "翻译", "写代码",
    "感冒", "发烧", "吃药", "看病",
)
# 即使命中上面的词，只要落点仍是穿搭，就不算无关问题。
_OFF_TOPIC_FASHION_GOAL = (
    "穿什么", "穿啥", "配什么", "搭什么", "怎么穿", "怎么搭", "搭配", "衣橱",
    "穿搭", "衣服", "配饰", "上装", "下装", "外套", "鞋子",
)

_OFF_TOPIC_REPLY = (
    "这个问题我就不硬答啦～我是今遂的专属穿搭顾问遂遂子，平时能帮你："
    "按天气、场合和衣橱里的单品搭出可直接照穿的整套方案，讲清搭配原理和风格思路，"
    "帮你整理衣橱、给购买前的建议，聊最近的流行色、热门风格和时尚资讯，"
    "还能告诉你今天或未来几天的天气。换个我擅长的问题，我马上帮你安排～"
)


def _detect_off_topic(text: str) -> bool:
    """是否是与穿搭/时尚完全无关的问题（美食、星座、医疗、股票等）。

    反向白名单必须用「强在域信号」——裸「搭配」会豁免「搭配菜谱」这类域外句，
    只有带衣物实体/方案短语（吃完饭穿什么）才算真落点穿搭。
    """
    t = (text or "").strip()
    if not t:
        return False
    if _has_strong_domain_signal(t) or any(w in t for w in _OFF_TOPIC_FASHION_GOAL
                                           if w not in ("搭配", "怎么搭")):
        return False
    return any(w in t for w in _OFF_TOPIC_WORDS)


# ---------- 人设问答：身份与能力介绍 ----------
_PERSONA_INTRO = (
    "我是你的专属穿搭顾问，我来自今遂，我有个很可爱的形象哦~我的开发者喜欢叫我遂遂子。"
    "我平时可以帮你按天气、场合和衣橱里的单品来搭衣服——像今天厦门21.4℃这种不冷不热的天，"
    "帮你定「松弛感通勤」那套白T+深蓝直筒牛仔裤+卡其风衣，就是我的活儿。"
    "如果你想了解最近流行的穿搭思路和时尚趋势，我也可以帮你解答。"
    "我的名字固定就是遂遂子，没有别的称呼；你喊「遂遂子」我就答应。"
)
_PERSONA_CAPABILITY = (
    "我能帮你做的，主要围绕「穿搭」这件事："
    "① 每日穿搭推荐——结合当天天气、场合和你衣橱里的单品，直接搭出可照着穿的成套方案，"
    "并说明为什么这么配，像「松弛感通勤」那套白T+深蓝直筒牛仔裤+卡其风衣，就是我的活儿；"
    "② 时尚资讯与潮流风向——最近流行什么颜色、哪些风格当红（奶油色系、松弛感通勤、老钱风、"
    "运动休闲混搭）、哪些单品值得关注，我帮你梳理思路，再落到你自己的衣橱上；"
    "③ 风格解读与搭配原理——三色原则、洋葱式叠穿、显高显瘦这些方法，讲清楚怎么用在你身上；"
    "④ 衣橱管理与购买前评估——盘点能搭出多少组合、哪些单品重复，想买新衣服时按可搭配度"
    "给「建议买/不建议买」的结论。简单说：告诉我天气、场合、想要的风格，或者直接问"
    "「今年流行什么」「这件外套值不值得买」，我就能给出具体到颜色和单品的方案。"
)
_PERSONA_TRIGGERS = (
    (("你叫什么", "叫什么名字", "你的名字", "你是谁", "怎么称呼你", "你叫什么呀"),
     _PERSONA_INTRO),
    (("你可以做什么", "你可以做些", "你能做什么", "你能做些", "你能干什么",
      "你可以干什么", "你能干嘛", "可以做些什么", "能做些什么", "会些什么",
      "你有什么功能", "你会什么", "你能帮我做什么", "介绍一下你自己", "介绍下你自己"),
     _PERSONA_CAPABILITY),
)


def _persona_answer(text: str) -> str | None:
    """身份/能力介绍类问题的标准答案；其他问题返回 None 走正常链路。"""
    t = (text or "").strip()
    if not t:
        return None
    for words, answer in _PERSONA_TRIGGERS:
        if any(w in t for w in words):
            return answer
    return None


# 固定称呼：只允许喊「遂遂子」。其他昵称（小米/小冻/李教授…）一律不认、不记忆。
_NAME_CALL_REPLY = (
    "我在呢～遂遂子收到！我是你的专属穿搭顾问，来自今遂。"
    "想让我帮你搭一套、查今天的天气，还是聊聊最近的流行色和热门风格？直接说就行。"
)


def _detect_name_call(text: str) -> bool:
    """是否只是呼唤固定名字。允许「遂遂子」「遂遂子你好」「你好遂遂子」「遂遂子在吗」。"""
    t = (text or "").strip()
    if not t:
        return False
    s = re.sub(r"[\s，,。！!？?~～、：:；;]", "", t)
    s = re.sub(r"^(你好呀|你好|哈喽|嗨|hi|hello)+", "", s, flags=re.I)
    s = re.sub(r"(你好呀|你好|在吗|在不在|在么|呀|啊|呢|哦|哈)+$", "", s)
    return s == "遂遂子"


_OTHER_NAME_SAFE = {"你好", "您好", "在吗", "在不在", "嗨", "哈喽", "hi", "hello",
                    "谢谢", "好的", "收到", "是的", "嗯嗯"}

# 多轮微调短指令：用户在对上一套方案下指令，不是"喊别的助手名字"。
# "换成裙子" / "不要黑色" / "更休闲" 都只有 3-4 字且不含穿搭关键词，
# 旧规则会把它们误判成 name-call，短路成 off_topic 直接拒答（Q17/Q18）。
_TWEAK_SIGNALS = (
    "换", "改成", "改为", "改掉", "不要", "别要", "别用", "去掉", "删掉", "不想要",
    "加", "添", "配", "来个", "来件", "来条", "来双", "来套", "来一",
    "更", "再", "太", "有点", "稍", "一点", "一些", "点儿",
    "还是", "这", "那",
)


def _detect_other_name_call(text: str) -> bool:
    """像在喊别的名字（小米/小冻/李教授…）：不承认、不复述，走通用拒答。

    注意：多轮微调常是 3-4 字的短指令（"换成裙子""不要黑色""更休闲"），
    语义是在改上一套方案，绝不能当成 name-call 拒答。
    """
    s = re.sub(r"[\s，,。！!？?~～、：:；;]", "", (text or "").strip())
    if not s or len(s) > 4 or s in _OTHER_NAME_SAFE:
        return False
    if _preset_answer(s):
        return False
    if any(w in s for w in ("穿", "搭", "衣", "天气", "流行", "颜色", "风格")):
        return False
    if any(w in s for w in _TWEAK_SIGNALS):
        return False
    return True


def _preset_answer(text: str) -> str | None:
    """人设问答与固定名字呼唤：都不检索、不调 LLM。"""
    return _persona_answer(text) or (_NAME_CALL_REPLY if _detect_name_call(text) else None)


# ---------- 天气询问（第五项能力）：问天气本身，不是问穿搭 ----------
_WEATHER_QUERY_WORDS = (
    "天气", "气温", "温度", "多少度", "几度", "几℃", "多少℃", "度左右",
    "冷不冷", "热不热", "冷吗", "热吗", "凉不凉", "暖和吗", "冻不冻",
    "下雨", "下雨吗", "会下雨", "要下雨", "有雨", "无雨", "晴天", "雨天", "阴天",
    "刮风", "风大", "降温", "回暖", "预报", "紫外线", "空气湿度",
)
_WEATHER_FUTURE_WORDS = ("明天", "后天", "未来", "这几天", "这周", "下周", "接下来", "两天", "三天", "一周")


def _detect_weather_query(text: str) -> bool:
    """是否是「问天气本身」。要穿搭方案的（今天穿什么）不算，由 recommend 优先拿走。"""
    t = (text or "").strip()
    if not t:
        return False
    if _has_recommend_signal(t):
        return False
    return any(w in t for w in _WEATHER_QUERY_WORDS)


# ---------- 能力白名单门禁：本地明确信号 + LLM 开放集兜底 ----------
# 本地在域信号用具体穿搭词，不用「怎么/什么/为什么」这类过宽词（否则「吃什么」会被放行）。
_DOMAIN_KNOWLEDGE_WORDS = (
    "穿搭", "搭配", "衣服", "服装", "上衣", "下装", "外套", "裤子", "裙子", "鞋子",
    "配饰", "围巾", "颜色", "深色", "浅色", "亮色", "素色", "色彩", "风格", "款式",
    "面料", "材质", "体型", "身材", "显瘦", "显高", "三色原则", "洋葱", "叠穿",
    "保养", "清洗", "收纳", "护理", "衣橱", "衣柜", "值不值得买", "该不该买",
    "整理衣橱", "买衣服", "买外套", "穿搭思路",
)


def _domain_allows(text: str, use_llm: bool = True) -> bool | None:
    """问题是否属于助手能力范围（白名单思路）。

    返回 True=明确在域 / False=明确域外 / None=模糊。
    同步路径复用意图分类的 off_topic 标签（use_llm=False 只跑本地），
    SSE 路径在模糊时才额外调一次轻量门禁（4 token）。
    """
    t = (text or "").strip()
    if not t:
        return True
    # 1) LLM 主裁决（与 _classify_intent 共用同一次调用，靠 _judge 缓存）。
    #    use_llm=False 仅供离线回归用，跳过 LLM。
    if use_llm:
        verdict = _judge(t)
        if verdict["domain"] == "out":
            return False
        if verdict["domain"] == "in":
            return True
    # 2) LLM 不可用/被跳过 → 本地保守兜底：只认黑名单与强在域实体，不裸词放行。
    if _detect_off_topic(t):
        return False
    if (_detect_weather_query(t) or _detect_trend(t) or _preset_answer(t)
            or _has_strong_domain_signal(t)
            or any(w in t for w in _DOMAIN_KNOWLEDGE_WORDS)):
        return True
    return None


def _weather_answer_text(query: str, session, user, passed_w=None,
                         history=None, db=None) -> str:
    """基于真实天气数据生成天气回答。取不到数据就如实说，绝不编造温度。"""
    city = ""
    if isinstance(passed_w, dict):
        city = (passed_w.get("city") or "").strip()
    city = city or (getattr(session, "weather_city", "") or "").strip()
    city = city or (getattr(user, "city", "") or "").strip() or "福州"

    future = any(w in query for w in _WEATHER_FUTURE_WORDS)
    info: dict = {"城市": city}

    if future:
        days = 3
        m = re.search(r"([1-9一二三四五六七两])\s*天", query)
        if m:
            days = _ORDINAL.get(m.group(1), days) or days
        forecast = _fetch_forecast(city, days)
        summary = forecast_summary(forecast, days) if forecast else None
        if summary:
            info.update({
                "日期区间": f"{summary['start_date']} 至 {summary['end_date']}",
                "代表温度": f"{summary['temp']}°C",
                "最低温": f"{summary['temp_min']}°C",
                "最高温": f"{summary['temp_max']}°C",
                "天气": summary["rain"],
                "降雨概率": summary["rain_probability"],
                "数据来源": summary["source"],
            })
        else:
            info["预报状态"] = "暂时取不到未来天气预报"

    # 当前天气：优先前端传值 > 会话缓存 > 后端实时抓取
    if not future or not info.get("代表温度"):
        temp = getattr(session, "weather_temp", None)
        rain = getattr(session, "weather_rain", None)
        prob = getattr(session, "weather_rain_prob", None)
        source = getattr(session, "weather_source", None)
        if isinstance(passed_w, dict) and passed_w.get("temp") is not None:
            temp = passed_w.get("temp")
            rain = passed_w.get("rain") or rain
            prob = passed_w.get("rain_probability")
            source = "passed"
        if temp is None:
            try:
                data = _fetch_weather(city)
                if data and data.get("temp") is not None:
                    temp = data.get("temp")
                    rain = data.get("rain") or rain
                    prob = data.get("rain_probability")
                    source = data.get("source") or "open-meteo"
            except Exception:
                pass
        if temp is not None:
            info.update({
                "当前温度": f"{temp}°C", "天气": rain or "未知",
                "降雨概率": prob, "数据来源": source or "实时",
            })
        else:
            info["当前状态"] = "暂时取不到实时天气"

    has_data = any(k in info for k in ("当前温度", "代表温度"))
    content = weather_reply(query, info, history=history) if has_data else None
    if content:
        return content

    # 本地兜底模板（LLM 未启用/失败），只用真实数据
    if not has_data:
        return (f"我这边暂时没拿到{city}的实时天气，不想瞎编一个温度误导你。"
                f"你可以告诉我大概多少度、下不下雨，或允许页面取天气，我就能按 26℃ 穿衣法给你建议。")
    if "代表温度" in info:
        return (f"{city}{info['日期区间']} 的天气预报：代表温度 {info['代表温度']}，"
                f"最低 {info['最低温']}、最高 {info['最高温']}，{info['天气']}。"
                f"要我按这个温度帮你搭一套吗？")
    t = float(str(info["当前温度"]).replace("°C", ""))
    hint = ("厚外套加毛衣" if t < 10 else "薄外套或针织衫" if t < 16 else
            "长袖加件薄外套" if t < 22 else "单件长袖或 T 恤" if t < 26 else "清凉夏装")
    return (f"{city}现在 {info['当前温度']}，{info['天气']}"
            + (f"，降雨概率 {info['降雨概率']}%" if info.get("降雨概率") is not None else "")
            + f"。这个温度{hint}就舒服。要我按这个天气帮你搭一套吗？")

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


def _detect_reject(text: str) -> bool:
    """本地检测否定/换一批意图（2026-11-17 补回：此前实现未落盘）。

    「这些都不喜欢/不满意/换一批/不要这些」= reject，要换掉上一批方案；
    「第二套把鞋换成靴子」是 refine，不能混进来（refine 有 _detect_refine 独立判定，
    且本函数不含「换成」这类单改一个件的词）。
    """
    t = (text or "").strip()
    if not t:
        return False
    dislike = (
        "不喜欢", "不满意", "不想要", "不行", "不好看", "太丑", "不要这些",
        "都不喜欢", "都不满意", "都不行", "这几套都不", "这些都不", "换一批",
        "重新推荐", "重新搭", "重来", "再换", "换别的", "换个别的", "别推这些",
    )
    return any(w in t for w in dislike)


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
    # 对话式锁定/追加：用户说「第1套不错」「加个配饰」时不该重新出三套
    if _detect_add(t):
        return "augment"
    if _detect_lock(t) and (_extract_outfit_index(t) is not None or _has_strong_domain_signal(t)):
        return "lock"
    # 旅游先于推荐：「去厦门玩3天穿什么」含「穿什么」，必须先被 travel 拿走
    if _detect_travel(t):
        return "travel"
    if _detect_reject(t):
        return "reject"
    if _has_recommend_signal(t) and _has_strong_domain_signal(t):
        return "recommend"
    if _detect_trend(t):
        return "trend"
    if _detect_weather_query(t):
        return "weather"
    if _detect_off_topic(t):
        return "off_topic"
    if t.endswith(("?", "？", "吗", "呢", "吧")):
        return "knowledge"
    # 纯知识问法：问概念/能不能/原理，要的是解释而非搭配方案
    if any(h in t for h in ("有哪些种类", "有哪些类型", "什么种类", "能一起穿",
                            "能不能一起", "可以一起穿", "三色原则", "是什么", "什么意思")):
        return "knowledge"
    if t.startswith(_KNOWLEDGE_HEADS) or any(h in t for h in ("怎么选", "如何选", "为什么")):
        return "knowledge"
    return "recommend"


@lru_cache(maxsize=512)
def _judge(text: str) -> dict:
    """统一裁决（LLM 主）：一次调用同时判「域 + 意图」，进程内缓存避免重复调用。

    返回 {"domain": "in"|"out"|"unknown", "intent": 标签|None}。
    LLM 不可用（无 key / 失败）时 domain=unknown，由调用方退回本地规则兜底。
    这是现在唯一的「主裁决」——本地词表无权再前置放行，只能做事后窄修正。
    """
    t = (text or "").strip()
    if not t:
        return {"domain": "in", "intent": "free_text"}
    verdict = classify(t)
    if verdict is None:
        return {"domain": "unknown", "intent": None}
    return verdict


def _classify_intent(text: str) -> str:
    """意图识别：LLM 主裁决 + 本地后置窄修正 + 关键词兜底。

    架构原则（2026-11-18 重构）：
    - LLM 先判域，out 就是 off_topic，本地词表无权翻案——根治「奥特曼喜欢穿什么」
      「怎么搭车」「搭一套煎饼」被裸词短路放行的 bug。
    - 本地规则只在 LLM 判 in 后做「窄修正」：补 LLM 的已知弱点（结构化动作槽位、
      「今天穿什么?」被误判 knowledge），且只在有明确证据时改，不再靠裸词放行。
    - LLM 不可用时退回关键词规则，保证离线/超时仍可用。
    """
    t = (text or "").strip()
    if not t:
        return "free_text"

    verdict = _judge(t)
    domain, intent = verdict["domain"], verdict["intent"]

    # 1) LLM 明确判域外 → 拒答。最高优先级，本地任何词表都不再能放行。
    if domain == "out":
        return "off_topic"
    # 2) LLM 不可用 → 退回本地关键词规则（离线降级）。
    if domain == "unknown":
        return _classify_intent_rules(t)

    # 3) domain=in：本地只做「后置窄修正」，且必须本地检测确认（不是裸词命中就放行）。
    #    结构化动作：LLM 判得对也保留本地抽槽位，判错则用本地结果纠正。
    if _detect_refine(t):
        return "refine"
    if _detect_add(t):
        return "augment"
    if _detect_lock(t) and (_extract_outfit_index(t) is not None or _has_strong_domain_signal(t)):
        return "lock"
    if _detect_travel(t) and _extract_dest_city(t) is not None:
        return "travel"
    if _detect_reject(t):
        return "reject"
    if _detect_weather_query(t):
        return "weather"
    if intent == "compare" or any(w in t for w in ("会怎样", "对比", "比较一下", "如果换")):
        return "compare"

    # 历史已知偏差：LLM 偶尔把「今天穿什么?」判成 knowledge（出文献不出卡片）。
    # 只在明确穿搭请求信号存在时才纠，且这是「LLM 判 in 之后」的窄修正。
    if intent == "knowledge" and _has_recommend_signal(t) and _has_strong_domain_signal(t):
        return "recommend"

    # LLM 判的结构化动作若本地抽不到必需槽位，不采信，退回本地规则。
    if intent in ("refine", "reject") and not (_detect_refine(t) if intent == "refine" else _detect_reject(t)):
        return _classify_intent_rules(t)
    if intent == "travel" and _extract_dest_city(t) is None:
        return _classify_intent_rules(t)

    return intent if intent else _classify_intent_rules(t)


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
    if "设计" in text or "特别" in text:
        return "temp_pref_style", {"style": "设计款"}
    # 「温柔/柔和」等氛围词不再落到这里（已改为走 2.2b 氛围澄清），
    # 落回 free_text 由上层 _detect_vibe 接管，避免被误判成「设计款」。

    return "free_text", {"text": text}


def _last_recommended_cores(db: Session, session_id: str, exclude_message_id: str | None = None) -> list[str]:
    """取最近一轮推荐的搭配核心签名（上装+下装/连衣裙组合）。

    用户说「这些都不喜欢」时，把上一轮卡片里的核心组合记入 rejected_cores，
    之后整组排除——只排除整套指纹不够，换个鞋又会出同样的上装+下装。
    从历史 response 的 outfits 反算签名，与引擎过滤端共用 outfit_core_signature
    （response 的 item 无 sub_category，签名函数只用归一化 category）。
    """
    q = (db.query(ChatMessage)
         .filter(ChatMessage.session_id == session_id,
                 ChatMessage.role == "agent",
                 ChatMessage.action == "recommend")
         .order_by(ChatMessage.created_at.desc()))
    rows = q.limit(3).all()
    cores: list[str] = []
    for msg in rows:
        if exclude_message_id and msg.message_id == exclude_message_id:
            continue
        for o in (msg.payload or {}).get("outfits") or []:
            c = outfit_core_signature(o)
            if c:
                cores.append(c)
        if cores:
            break  # 只取最近一轮（有方案的那条）
    return cores


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
        rejected_cores=(session.constraints or {}).get("rejected_cores") or [],
        body_shape=user.body_shape or "匀称",
        long_term_prefs={
            "color": user.pref_color,
            "style": user.pref_style,
            "comfort": user.pref_comfort,
            "occasion": user.pref_occasion,
            "fabric": user.pref_fabric,
        },
        user_profile=utils.effective_profile(user.profile_prefs or {}),
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
    ("鞋", "鞋"), ("靴", "鞋"), ("板鞋", "鞋"), ("皮鞋", "鞋"), ("运动鞋", "鞋"),
    ("配饰", "配饰"), ("围巾", "配饰"), ("帽子", "配饰"), ("包", "配饰"), ("腰带", "配饰"),
    ("首饰", "配饰"), ("项链", "配饰"), ("手表", "配饰"), ("墨镜", "配饰"), ("手套", "配饰"),
    ("上衣", "上装"), ("上装", "上装"), ("T恤", "上装"), ("衬衫", "上装"), ("毛衣", "上装"),
    ("针织", "上装"), ("卫衣", "上装"),
]
# 细化到子品类：用户点名「换成裙子/短靴/风衣」时，替代也限定在同一子品类，
# 否则「换成裙子」会在「下装」里按保暖配色挑到另一条牛仔裤（Q17）。
_SUB_WORDS = (
    ("连衣裙", "连衣裙"),
    ("半身裙", "半身裙"), ("短裙", "半身裙"), ("长裙", "半身裙"), ("裙子", "半身裙"),
    ("阔腿裤", "阔腿裤"), ("直筒裤", "直筒裤"), ("工装裤", "工装裤"),
    ("短靴", "短靴"), ("帆布鞋", "帆布鞋"), ("板鞋", "板鞋"), ("运动鞋", "运动鞋"),
    ("风衣", "风衣"), ("羽绒服", "羽绒服"), ("皮衣", "皮衣"),
    ("帽子", "帽子"), ("围巾", "围巾"), ("项链", "项链"), ("墨镜", "墨镜"),
)
# 吐槽某单品 = 换掉它：「这帽子不太搭」「鞋太丑了」不必明说「换」也算单品级微调。
# 必须是**单数/具体单品**的否定；「这些都不喜欢/换一批」由 _detect_reject 先行拦截。
_ITEM_DISLIKE = ("不搭", "不太搭", "不好看", "不好看", "不协调", "突兀", "太丑", "难看",
                 "不喜欢这个", "不喜欢这", "不满意这个", "怪怪的", "违和", "不顺眼")


def _detect_refine(text: str):
    """识别「对某套满意，只想改某个单品」的意图。返回 {"index", "category"} 或 None。

    两条路径：
    1. 明确换的动词 + 品类词：「第二套把鞋换成靴子」「换个帽子」
    2. 否定某具体单品（含锁定时）：「这个帽子好像不太搭」= 换掉帽子
    """
    t = (text or "").strip()
    if not t:
        return None
    cat = None
    for word, mapped in _CAT_WORDS:
        if word in t:
            cat = mapped
            break
    if not cat:
        return None

    has_verb = any(v in t for v in _REFINE_VERB)
    has_dislike = any(v in t for v in _ITEM_DISLIKE)
    # 整批否定（都不喜欢/换一批）不是单品微调，交给 reject。
    # 注意不能用 _detect_reject——它含「不好看/太丑」，那些在单品语境下正是 refine。
    _BATCH_REJECT = ("都不喜欢", "都不满意", "都不行", "这几套都不", "这些都不",
                     "换一批", "重新推荐", "重新搭", "重来", "不要这些", "别推这些")
    if any(v in t for v in _BATCH_REJECT):
        return None
    if not (has_verb or has_dislike):
        return None

    idx = None
    # 方案 2 / 第 2 套 / LOOK 2 / 第二套
    m = re.search(r"(?:方案|第)\s*([1-3一二三])\s*(?:套|个)?", t) or re.search(r"look\s*([1-3])", t, re.I)
    if m:
        key = m.group(1)
        idx = _ORDINAL.get(key)
    sub = next((mapped for word, mapped in _SUB_WORDS if word in t), None)
    return {"index": idx, "category": cat, "sub_category": sub}


# ---------- 对话式锁定某套 + 追加单品 ----------
# 用户不点卡片上的按钮，而是在对话里说「第1套不错 / 就选这套 / 我挺喜欢第二套」，
# 这类话要识别为锁定，把那一套单独调出来，后续微调都基于它。
_LOCK_WORDS = ("喜欢", "满意", "中意", "看好", "选中", "就选", "就要", "定了",
               "就用这", "就这套", "就它", "这套不错", "这套好", "这个不错", "这个好",
               "套不错", "套好", "可以就要", "就定", "选这个", "要这个")
_ADD_VERBS = ("加个", "加一件", "加一双", "加条", "加一顶", "加一块", "加上",
              "添个", "添一件", "添条", "添件", "配个", "配一件", "配条",
              "来个", "搭个", "来一件", "搭一件")

# 时尚资讯：用户想了解趋势/热门色/风格，重点不是搭配自己的衣橱。
_TREND_WORDS = ("流行", "趋势", "时尚", "热门穿搭", "热门色", "流行色", "今年穿",
                "这季", "本季", "时装周", "什么风格火", "穿搭风格", "穿搭资讯",
                "最近火", "现在流行", "潮流")


def _detect_trend(text: str) -> bool:
    """识别「想了解时尚资讯」话语。要搭配自己的衣橱不算（走 recommend）。"""
    t = (text or "").strip()
    if not t:
        return False
    # 明确要方案的词优先排除：「今天穿什么」「帮我搭」即使带「流行」也要出卡片
    if _has_recommend_signal(t):
        return False
    return any(w in t for w in _TREND_WORDS)


# 中文问题 -> 标签同义词扩展。用于把「今年流行什么颜色」映射到采集时打的英文来源标签。
_TREND_TAG_SYNONYMS: dict[str, tuple[str, ...]] = {
    # 泛问：问「流行什么颜色/风格/单品」时展开为整个类别，而不是落空取最新
    "什么颜色": ("红色", "酒红", "粉色", "橙色", "黄色", "绿色", "蓝色",
                 "紫色", "棕色", "灰色", "白色", "奶油色", "米色", "黑色", "中性色"),
    "哪个颜色": ("红色", "酒红", "粉色", "橙色", "黄色", "绿色", "蓝色",
                 "紫色", "棕色", "灰色", "白色", "奶油色", "米色", "黑色", "中性色"),
    "流行色": ("红色", "酒红", "粉色", "橙色", "黄色", "绿色", "蓝色",
               "紫色", "棕色", "灰色", "白色", "奶油色", "米色", "黑色", "中性色"),
    "什么风格": ("极简风", "老钱风", "复古风", "街头风", "浪漫风", "运动风", "波西米亚", "暗黑风"),
    "哪种风格": ("极简风", "老钱风", "复古风", "街头风", "浪漫风", "运动风", "波西米亚", "暗黑风"),
    "什么单品": ("针织", "毛衣", "牛仔", "外套", "西装", "连衣裙", "半裙", "鞋履", "包袋", "配饰"),
    "哪些单品": ("针织", "毛衣", "牛仔", "外套", "西装", "连衣裙", "半裙", "鞋履", "包袋", "配饰"),
    "红": ("红色", "酒红"), "粉": ("粉色",), "橙": ("橙色",), "黄": ("黄色",),
    "绿": ("绿色",), "蓝": ("蓝色", "牛仔"), "紫": ("紫色",), "棕": ("棕色", "美拉德"),
    "灰": ("灰色",), "白": ("白色", "奶油色"), "黑": ("黑色",), "米": ("米色", "中性色"),
    "奶油": ("奶油色", "米色"), "美拉德": ("棕色", "美拉德"), "多巴胺": ("黄色", "粉色", "橙色"),
    "针织": ("针织", "毛衣"), "牛仔": ("牛仔", "蓝色"), "西装": ("西装", "通勤"),
    "通勤": ("通勤", "西装"), "外套": ("外套", "西装"), "裙": ("连衣裙", "半裙"),
    "鞋": ("鞋履",), "包": ("包袋",), "首饰": ("首饰", "配饰"), "配饰": ("配饰",),
    "复古": ("复古风",), "极简": ("极简风", "老钱风"), "老钱": ("老钱风", "极简风"),
    "运动": ("运动风",), "街头": ("街头风",), "浪漫": ("浪漫风", "印花"),
    "度假": ("度假风",), "婚礼": ("婚礼",), "红毯": ("明星红毯",),
    "秋": ("秋季",), "冬": ("冬季",), "春": ("春季",), "夏": ("夏季",),
}


def _trend_query_tags(text: str) -> list[str]:
    """从中文问题里抽出用于匹配灵感图的中文标签。"""
    t = (text or "")
    tags: list[str] = []
    for key, syn in _TREND_TAG_SYNONYMS.items():
        if key in t:
            for s in syn:
                if s not in tags:
                    tags.append(s)
    # 用户直接说出完整标签（如「想看针织」）
    for known in {s for group in _TREND_TAG_SYNONYMS.values() for s in group}:
        if known in t and known not in tags:
            tags.append(known)
    return tags


def _query_gender(text: str) -> str | None:
    """从问题里抽性别指向：male / female / None（中性不筛）。

    只在明确说出男/女时过滤，避免「男生穿搭」错配女模特图这类图文不符。
    """
    t = (text or "").lower()
    if any(w in t for w in ("男生", "男士", "男人", "男装", "男性", "男士穿搭", "男孩子", "men's", "menswear", "for men")):
        return "male"
    if any(w in t for w in ("女生", "女士", "女人", "女装", "女性", "女孩子", "women's", "womenswear", "for women")):
        return "female"
    return None


# 性别判定：标题/摘要/标签里出现强性别词即归类，冲突时女装词优先（采集源以女装为主，宁缺勿错配）
_MALE_HINT = re.compile(
    r"menswear|men'?s|men\b|male|gentleman|^\bhis\b|\bhis\b|guy|boys|tailoring"
    r"|tom ford|dsquared2|suit", re.I)
_FEMALE_HINT = re.compile(
    r"women|woman|female|ladies|lady|bride|bridal|wedding"
    r"|dress|skirt|manicure|makeup|lipstick|heels|feminine|her\b|she\b|girls", re.I)


def _inspo_gender(r) -> str | None:
    """单条灵感图的性别：male / female / None（不确定）。"""
    text = " ".join(str(x or "") for x in (r.title, r.summary, " ".join(r.tags or [])))
    male = bool(_MALE_HINT.search(text))
    female = bool(_FEMALE_HINT.search(text))
    if female and not male:
        return "female"
    if male and not female:
        return "male"
    return None


def _trend_images(db: Session, query: str, user_id: str) -> dict:
    """装配双栏图片：左栏=采集到的时尚资讯灵感图，右栏=衣橱里真实可穿的单品。

    问「男生/女士穿搭」时左栏只取对应性别的图；库里没有对应性别图时
    宁可留空，也不塞相反性别的模特图（图文不符比没图更糟）。
    两栏都失败/为空时返回空列表，前端只显示文字，不影响主回答。
    """
    result: dict[str, list[dict]] = {"inspo": [], "wardrobe": []}
    tags = _trend_query_tags(query)
    gender = _query_gender(query)

    try:
        rows = (db.query(FashionInspo)
                  .order_by(FashionInspo.fetched_at.desc())
                  .limit(120).all())
        if gender:
            # 强过滤：只保留同性别 + 不确定的中性图；明确相反性别的一律排除
            opposite = "female" if gender == "male" else "male"
            rows = [r for r in rows if _inspo_gender(r) != opposite]
        scored: list[tuple[int, int, FashionInspo]] = []
        for i, r in enumerate(rows):
            hit = sum(1 for t in tags if t in (r.tags or []))
            # 同分时新的优先；没命中标签时也带一批最新图兜底
            scored.append((hit, -i, r))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        # 命中的优先；命中不足 2 张时只补 1~2 张近期图，并标记「近期」避免图文脱节
        matched = [x for x in scored if x[0] > 0][:4]
        if len(matched) < 2:
            recent = [x for x in scored if x[0] == 0][:2 - len(matched)]
            matched = matched + recent
        for hit, _, r in matched:
            result["inspo"].append({
                "title": r.title, "image": r.image_path, "link": r.link,
                "source": "Vogue" if r.source == "vogue" else "ELLE",
                "tags": (r.tags or [])[:4],
                "match": bool(hit > 0),
            })
    except Exception:
        pass

    try:
        items = (db.query(ClothingItem)
                   .filter(ClothingItem.user_id == user_id)
                   .filter(ClothingItem.image_url != "")
                   .all())
        scored_items: list[tuple[int, ClothingItem]] = []
        for it in items:
            hay = " ".join(str(x or "") for x in (
                it.color, it.style, it.category, it.sub_category,
                it.name, " ".join(it.tags or []),
            ))
            hit = sum(1 for t in tags if t in hay)
            scored_items.append((hit, it))
        scored_items.sort(key=lambda x: x[0], reverse=True)
        for _, it in scored_items[:4]:
            result["wardrobe"].append({
                "name": it.name, "image": it.image_url,
                "color": it.color or "", "category": it.category or "",
            })
    except Exception:
        pass

    return result


def _extract_outfit_index(text: str) -> int | None:
    """从「第2套 / 方案一 / look 3」抽套号，无号返回 None。"""
    m = re.search(r"(?:第|方案|look|LOOK)\s*([1-3一二三])\s*(?:套|个)?", text)
    return _ORDINAL.get(m.group(1)) if m else None


def _detect_lock(text: str) -> dict | None:
    """识别「锁定某套」话语，返回 {"index": n} 或 None。

    必须有明确锁定词；单独一句「不错」不算，避免误伤。
    「都不喜欢」由 _detect_reject 先行拦截，不会进这里。
    """
    t = (text or "").strip()
    if not t or _detect_reject(t):
        return None
    if not any(w in t for w in _LOCK_WORDS):
        return None
    # 指代「这套/这个/第一套」：有套号取套号，没有默认第 1 套（对话里最常指首套）
    idx = _extract_outfit_index(t)
    return {"index": idx or 1}


def _detect_add(text: str) -> dict | None:
    """识别「在锁定套上加一件某品类」，返回 {"category": ...} 或 None。"""
    t = (text or "").strip()
    if not t or not any(v in t for v in _ADD_VERBS):
        return None
    for word, mapped in _CAT_WORDS:
        if word in t:
            return {"category": mapped}
    return None


def _load_base_outfit(db: Session, session, index):
    """取出会话中最近一轮推荐的第 index 套（默认第 1 套）。

    若会话已锁定某套（constraints.locked_outfit），优先返回锁定的那套——
    用户说「就这套，加个配饰」时必须接着这一套改，而不是最近一轮的第 1 套。
    """
    cons = session.constraints or {}
    locked = cons.get("locked_outfit")
    # 有锁定套且用户没指定别的套号（或指定的就是第1套=默认锁定套）时沿用锁定态。
    # 用户明确说「第二套」时仍走历史，避免把锁定的第1套硬塞给第二套。
    if locked and (index is None or index == 1):
        return locked
    msgs = db.query(ChatMessage).filter(
        ChatMessage.session_id == session.session_id,
        ChatMessage.action.in_(["recommend", "refine", "augment", "lock"]),
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


def _closet_inventory_context(items: list[dict]) -> list[str]:
    """构造给 LLM 的衣橱上下文：分类统计 + 逐件清单 + 检索判断规则。

    旧实现只把「上衣18件、下装10件」这类统计塞进上下文，模型因此回答
    「看不到每件单品的具体名称」。用户问「衣橱里有没有某件衣服」时必须能
    检索到具体单品，故逐件列出名称/品类/子品类/颜色/风格/季节。
    """
    if not items:
        return []
    cats: dict[str, int] = {}
    for it in items:
        cat = it.get("category") or "其他"
        cats[cat] = cats.get(cat, 0) + 1
    lines = [
        f"用户衣橱共 {len(items)} 件："
        + "、".join(f"{k}{v}件" for k, v in cats.items()),
        "【衣橱单品清单：你能完整查看用户衣橱；用户问有没有某件衣服时，必须逐件检索本清单】",
    ]
    for idx, it in enumerate(items[:80], start=1):
        name = str(it.get("name") or "").strip()
        if not name:
            name = str(it.get("color") or "") + str(it.get("sub_category") or "单品")
        meta: list[str] = []
        cat, sub = it.get("category"), it.get("sub_category")
        if cat or sub:
            meta.append("/".join(str(x) for x in (cat, sub) if x))
        for key in ("color", "style", "season", "fabric"):
            val = it.get(key)
            if val and str(val) not in ("未知", "四季"):
                meta.append(str(val))
        line = f"{idx}. {name}"
        if meta:
            line += "｜" + "｜".join(meta)
        lines.append(line)
    if len(items) > 80:
        lines.append(f"（以上仅列出前 80 件，衣橱共 {len(items)} 件）")
    lines.append(
        "检索判断规则：名称/品类/子品类/颜色明确对应就说「有」并列出是哪几件；"
        "只有同品类或相近颜色/款式、记录缺少领型等细节时说「有相近的/疑似有」并说明依据；"
        "确实没有才说没有。不要回答「看不到具体单品」「只能看到总数」。"
    )
    return lines


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
        rejected_cores=(session.constraints or {}).get("rejected_cores") or [],
        body_shape=user.body_shape or "匀称",
        long_term_prefs={
            "color": user.pref_color, "style": user.pref_style,
            "comfort": user.pref_comfort, "occasion": user.pref_occasion,
            "fabric": user.pref_fabric,
        },
        user_profile=utils.effective_profile(user.profile_prefs or {}),
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

    # 上一轮刚问过澄清：这一句就是回答，不能被意图分流抢先判成域外拒答。
    # 「旅游」「看演唱会」都不含穿搭词，裸分类必挂，必须先放行给回收段。
    _awaiting_clarify = bool((session.constraints or {}).get("clarify_pending"))

    # 1.5 意图分流收到后端单点：前端只传原文（action="auto"）时由后端统一判定
    # 固定名字呼唤 / 人设问答走预设答案，不参与关键词或 LLM 分类。
    preset_answer = _preset_answer(payload.get("text", ""))
    other_name_call = _detect_other_name_call(payload.get("text", ""))
    if _awaiting_clarify and action == "auto":
        # 正在等澄清回答：跳过预设/呼唤/域外三类短路，原文一律先当回答处理。
        # 「看演唱会」会被 _detect_other_name_call 误判成 name-call 直接拒答。
        action = "free_text"
    elif preset_answer:
        action = "persona"
    elif other_name_call:
        action = "off_topic"
    elif action == "auto":
        action = _classify_intent(payload.get("text", ""))
        if _awaiting_clarify and action in ("off_topic", "knowledge", "free_text", "echo"):
            action = "free_text"
        # 能力白名单门禁：本地没明确在域/域外的模糊问题，让 LLM 按五项能力判一次。
        # 只拦「准备走知识问答/echo」这类会硬答的分支，结构化动作已由本地规则锁定。
        if (action in ("knowledge", "free_text") and not _awaiting_clarify
                and _domain_allows(payload.get("text", "")) is False):
            action = "off_topic"
        if action == "refine":
            payload = {**payload, "refine": _detect_refine(payload.get("text", ""))}
        if action == "lock":
            payload = {**payload, "lock": _detect_lock(payload.get("text", ""))}
        if action == "augment":
            payload = {**payload, "augment": _detect_add(payload.get("text", ""))}

    # 澄清回答回收：上一轮问了「什么场合/多少度」，用户答「聚会」「18-25℃」这类词
    # 常被判成 free_text。这里把场合/温度写回 session，避免下一轮又缺信息无限反问。
    # 只在尚未展示过方案时回收，避免把推荐流程里的随口一提误当成回答。
    text_now = payload.get("text", "")
    _pending = (session.constraints or {}).get("clarify_pending")
    # 有一轮展示过方案后本来不做回收；但「帮我搭一套」会强制重问场合，
    # 此时必须允许回收，否则用户答了场合却没人接，直接死循环。
    if not session.shown_outfit_ids or _pending:
        _missing = _pending.get("missing", []) if isinstance(_pending, dict) else []
        if _pending:
            _pop_constraint(session, "clarify_pending")
        # 澄清选项里的「旅游」不能当普通场合用当前城市天气硬搭：
        # 转旅游流程，按目的地未来天气推荐（下一步追问城市）。
        if _pending and any(w in text_now for w in ("旅游", "旅行", "出游")):
            action = "travel"
        else:
            # 场合落位：只要用户还没确认过场合，本句里的场合词/自定义场景都写回 session，
            # 并打上 occasion_confirmed，之后不再重复反问。
            # 不再看 session.occasion 是否有值——首页预设的「日常」不是用户提供的，
            # 用户答「聚会」时必须能覆盖它。
            _occ_in_text = any(w in text_now for w in _OCCASION_WORDS)
            if not _occasion_confirmed(session) or (_pending and "occasion" in _missing):
                _occ = _match_occasion(text_now)
                if not _occ and _pending and "occasion" in _missing:
                    # 选项没覆盖的自定义场景：整句落位为场合。先剥掉温度片段，
                    # 避免「生日宴 25℃」把温度写进 occasion。
                    _custom = re.sub(r"\s*-?\d+(?:\s*[-~到至]\s*\d+)?\s*(?:℃|°C|度)\s*", " ", text_now)
                    _occ = _custom.strip(" ，,。！!？?、；;：:")[:30] or None
                if _occ and (_pending or _occ_in_text
                             or _classify_intent(text_now) in ("free_text", "echo")):
                    session.occasion = _occ
                    _set_constraint(session, "occasion_confirmed", True)
                    if action in ("free_text", "echo", "recommend", "weather"):
                        action = "recommend"
            if session.weather_temp is None or (_pending and "weather" in _missing):
                _temp = _match_temperature(text_now)
                if _temp is not None and (_pending or _classify_intent(text_now) in ("free_text", "echo")):
                    session.weather_temp = _temp
                    session.weather_source = session.weather_source or "clarify"
                    if action in ("free_text", "echo", "recommend", "weather"):
                        action = "recommend"

    # 氛围澄清回收：上一轮问了「温柔想的是哪种」，用户答「颜色浅一点/材质柔和/换裙子」。
    # 把落位写回临时偏好或约束，并转成推荐继续；未命中则清掉 pending 标记，不再追问。
    _vibe_pending = bool((session.constraints or {}).get("vibe_pending"))
    _vibe_landed = False
    if _vibe_pending and not session.shown_outfit_ids:
        _landed = _apply_vibe_answer(session, text_now)
        _pop_constraint(session, "vibe_pending")
        if _landed:
            _vibe_landed = True
            action = "recommend"

    # 旅游场景：会话级「等待目的地」状态。用户先说「想去旅游」但没给城市时反问，
    # 下一句补上城市名后，即使被判成别的意图也要回到旅游流程。
    constraints = session.constraints or {}
    travel_pending = bool(constraints.get("travel_pending"))

    if travel_pending and action in ("recommend", "free_text", "knowledge", "travel", "off_topic", "echo"):
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

    # 人设/呼唤：固定答案，不检索、不调 LLM、不走推荐流程。
    if action == "persona":
        content = preset_answer or _NAME_CALL_REPLY
        agent_msg = ChatMessage(
            message_id=utils.gen_id("m"), session_id=session_id, role="agent",
            action="persona", content=content, payload={"preset": True},
        )
        db.add(agent_msg)
        db.commit()
        return ok(data={
            "message_id": agent_msg.message_id, "role": "agent", "action": "persona",
            "content": content, "outfits": [], "passages": [],
            "llm_enabled": config.is_llm_enabled(), "answer_source": "preset",
            "degraded": False, "degraded_reason": "",
            "remaining_count": session.remaining_count,
        })

    # 旅游场景：目的地未来天气驱动推荐。
    # 用户去的是另一座城市，绝不能用当前城市的实时天气，也不用前端传来的天气。
    travel_info: dict | None = None
    if action == "travel":
        dest = _extract_dest_city(text_now)
        if not dest:
            # 与上面的澄清卡同理：先写 travel_pending，再提交，否则下一句补城市时收不到状态
            _resp = _travel_clarify_response(session, text_now)
            db.commit()
            return ok(data=_resp)
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

    # 本地强约束兜底：LLM 偶尔把「不要黑色」判成普通 recommend，绕过约束解析，
    # 导致排除色写不进 session、照样推出黑色单品（Q18 实测踩坑）。
    # 只锁定「不要/排除/别 + 颜色」这类明确约束；「温柔/深色」等宽泛偏好
    # 仍交给 LLM，避免把「推荐一套温柔的穿搭」误当成偏好设置。
    if action in ("recommend", "free_text", "echo"):
        _pa, _pp = _parse_free_text(text_now)
        if _pa == "exclude_color":
            action = _pa
            payload = {**_pp, "text": text_now}

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
            # 保留原文：约束更新后要直接重算推荐，recommend 分支需要它生成文案
            payload = {**parsed_payload, "text": payload.get("text", "")}

    # 2.2 主动澄清：对话推荐里只要用户没提供过场合，一律先反问——
    # 会话残留首页预设场合、已展示过方案、文本很长，都不能成为跳过的理由。
    # 首页预览（payload.preview=true）是泛化展示，不反问，否则首页会空白。
    _needs = (_clarify_needs(payload.get("text", ""), session)
              if action == "recommend" and not payload.get("preview") else [])
    if _needs:
        # 先生成响应（内部会写 clarify_pending），再 commit。
        # 反过来写会因参数求值顺序把标记漏在事务外，下一轮收不到澄清回答。
        _resp = _clarify_response(session, _needs)
        db.commit()
        return ok(data=_resp)

    # 2.2b 氛围澄清：用户给「温柔/柔和」等模糊氛围词、且没给其他可落位的明确维度时，
    # 反问他想的是颜色/材质/款式中的哪种，而不是硬猜成某个临时偏好。
    # _vibe_landed：本轮刚回收完氛围回答（如「材质柔和一点」本身含「柔和」），
    # 不再二次追问，否则死循环。
    _vibe_word = _detect_vibe(payload.get("text", "")) if action in ("recommend", "free_text") else None
    if _vibe_word and not _vibe_landed and not _has_explicit_dimension(payload.get("text", "")):
        _set_constraint(session, "vibe_pending", True)
        db.commit()
        return ok(data=_vibe_clarify_response(session, _vibe_word))

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

    # 处理约束更新类动作。
    # 前端对 constraint_updated 没有二次请求逻辑——旧实现只回一句「重新推荐中...」
    # 就返回空卡，用户永远等不到新方案（Q18）。改为写入约束后直接转入 recommend
    # 重算并出卡，文案作为约束说明拼在推荐结果前面。
    constraint_note = ""
    if action == "exclude_color":
        colors = payload.get("colors", [])
        _cur = list((session.constraints or {}).get("exclude_colors") or [])
        _set_constraint(session, "exclude_colors", list(set(_cur) | set(colors)))
        constraint_note = f"已按你的要求排除「{', '.join(colors)}」，重新搭了一套：\n"
        action = "recommend"

    if action == "replace_category":
        _cur = list((session.constraints or {}).get("replace") or [])
        _cur.append({"from": payload.get("from", ""), "to": payload.get("to", "")})
        _set_constraint(session, "replace", _cur)
        constraint_note = f"已把「{payload.get('from')}」换成「{payload.get('to')}」倾向，重新搭了一套：\n"
        action = "recommend"

    if action == "temp_pref_color":
        session.temp_pref_color = payload.get("color", "")
        constraint_note = f"已记下你偏好「{session.temp_pref_color}」，重新搭了一套：\n"
        action = "recommend"

    if action == "temp_pref_style":
        session.temp_pref_style = payload.get("style", "")
        constraint_note = f"已记下你偏好「{session.temp_pref_style}」风格，重新搭了一套：\n"
        action = "recommend"

    # weather：第五项能力，回答天气本身。用真实天气数据，不检索知识库、不硬答常识。
    if action == "weather":
        query = payload.get("text", "")
        history = _recent_history(db, session_id, exclude_message_id=msg.message_id)
        content = _weather_answer_text(query, session, user,
                                       passed_w=payload.get("weather"),
                                       history=history, db=db)
        agent_msg = ChatMessage(
            message_id=utils.gen_id("m"), session_id=session_id, role="agent",
            action="weather", content=content,
            payload={"query": query, "llm_enabled": config.is_llm_enabled()},
        )
        db.add(agent_msg)
        db.commit()
        return ok(data={
            "message_id": agent_msg.message_id, "role": "agent", "action": "weather",
            "content": content, "outfits": [],
            "llm_enabled": config.is_llm_enabled(),
            "remaining_count": session.remaining_count,
        })

    # trend：时尚资讯（流行趋势/热门色/风格）。重点是通用资讯，不绑衣橱、
    # 不检索本地知识库——知识库是穿搭原理，给不了「最近流行什么」。
    if action == "trend":
        query = payload.get("text", "")
        images = _trend_images(db, query, session.user_id)
        history = _recent_history(db, session_id, exclude_message_id=msg.message_id)
        content = None
        if config.is_llm_enabled():
            content = llm_trend(query, history=history)
        if not content:
            content = (
                "我暂时没法获取实时时尚资讯（模型未启用或调用失败）。"
                "可以换个具体一点的问题，比如「今年流行什么颜色」或「通勤怎么穿显高级」，"
                "我会基于穿搭知识为你分析。"
            )
        agent_msg = ChatMessage(
            message_id=utils.gen_id("m"), session_id=session_id, role="agent",
            action="trend", content=content,
            payload={"query": query, "llm_enabled": config.is_llm_enabled(),
                     "images": images},
        )
        db.add(agent_msg)
        db.commit()
        return ok(data={
            "message_id": agent_msg.message_id, "role": "agent", "action": "trend",
            "content": content, "outfits": [], "images": images,
            "llm_enabled": config.is_llm_enabled(),
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
        _vibe_note = (session.constraints or {}).get("vibe_note")
        if _vibe_note:
            _ctx_lines.append(f"用户对氛围的落位要求：{_vibe_note}")
        _ctx_lines.extend(_closet_inventory_context(_item_dicts(db, session.user_id)))
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

        # 人设问答（你叫什么 / 你能做什么）：固定标准答案，不检索、不编造
        persona = _persona_answer(query)
        if persona:
            db.commit()
            return ok(data={
                "message_id": utils.gen_id("m"),
                "role": "agent",
                "action": "knowledge",
                "content": persona,
                "passages": [],
                "llm_enabled": config.is_llm_enabled(),
                "answer_source": "llm",
                "degraded": False,
                "degraded_reason": "",
                "remaining_count": session.remaining_count,
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
        # 负反馈：把最近一轮搭配的核心组合（上装+下装/连衣裙）记入排除名单。
        # 只排除整套指纹不够——换个鞋/配饰又会出同样的上装+下装，用户会感觉没换。
        cores = _last_recommended_cores(db, session_id, exclude_message_id=msg.message_id)
        if cores:
            cons = dict(session.constraints or {})
            rejected = list(cons.get("rejected_cores") or [])
            for c in cores:
                if c and c not in rejected:
                    rejected.append(c)
            cons["rejected_cores"] = rejected
            cons.pop("locked_outfit", None)  # 换一批=放弃锁定
            session.constraints = cons
            db.commit()
        else:
            # 即便没取到核心签名，也要清掉锁定态
            cons = dict(session.constraints or {})
            if cons.pop("locked_outfit", None) is not None:
                session.constraints = cons
                db.commit()

        # 换一套：成本统一由 _run_recommend 扣减，调用方只传 cost（1.3-E）
        outfits, content = _run_recommend(db, request, session, user,
                                          deduct=config.CHAT_COST_REJECT,
                                          user_text=payload.get("text", ""))
        if cores:
            content = "已换掉刚才那几套的核心搭配（上装+下装不再重复）。\n" + (content or "")
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

    # lock：用户在对话里表达「喜欢/选中第X套」——把那套单独调出来，记入会话锁定态。
    # 只回这一套卡片，不再铺三套；后续 refine/augment 都基于它。
    if action == "lock":
        info = payload.get("lock") or {}
        base = _load_base_outfit(db, session, info.get("index"))
        if not base:
            db.commit()
            return ok(data={
                "message_id": utils.gen_id("m"), "role": "agent", "action": "echo",
                "content": "还没找到可以锁定的方案，先让我给你推荐几套吧。",
                "outfits": [], "remaining_count": session.remaining_count,
            })
        cons = dict(session.constraints or {})
        cons["locked_outfit"] = base
        session.constraints = cons
        agent_msg = ChatMessage(
            message_id=utils.gen_id("m"), session_id=session_id, role="agent",
            action="lock", content="已锁定这一套。接下来可以直接说「加个配饰」「把鞋换成靴子」，我在这套上帮你改。",
            outfits=[base.get("outfit_id", "")], payload={"outfits": [base]},
        )
        db.add(agent_msg)
        db.commit()
        return ok(data={
            "message_id": agent_msg.message_id, "role": "agent", "action": "lock",
            "content": agent_msg.content, "outfits": [base],
            "remaining_count": session.remaining_count,
        })

    # augment：用户在锁定套上说「加个配饰 / 添件外套」——只追加一件，回单套卡片。
    if action == "augment":
        info = payload.get("augment") or {}
        base = _load_base_outfit(db, session, info.get("index"))
        if not base:
            db.commit()
            return ok(data={
                "message_id": utils.gen_id("m"), "role": "agent", "action": "echo",
                "content": "还没找到可以修改的方案，先让我给你推荐几套吧。",
                "outfits": [], "remaining_count": session.remaining_count,
            })
        all_items = _item_dicts(db, session.user_id)
        ctx = _make_ctx(session, user)
        eng = request.app.state.engine
        res = eng.augment(base.get("items", []), info.get("category", "配饰"), all_items, ctx)
        if not res.get("outfit"):
            db.commit()
            return ok(data={
                "message_id": utils.gen_id("m"), "role": "agent", "action": "augment",
                "content": res.get("reason", "暂时没有可添加的单品。"),
                "outfits": [], "remaining_count": session.remaining_count,
            })
        resp = eng.to_response(res["outfit"])
        # 同步锁定态到修改后的这套，支持连续多轮微调
        cons = dict(session.constraints or {})
        cons["locked_outfit"] = resp
        session.constraints = cons
        agent_msg = ChatMessage(
            message_id=utils.gen_id("m"), session_id=session_id, role="agent",
            action="augment", content=f"已在原来基础上加了「{res['added_name']}」。",
            outfits=[resp["outfit_id"]], payload={"outfits": [resp]},
        )
        db.add(agent_msg)
        db.commit()
        return ok(data={
            "message_id": agent_msg.message_id, "role": "agent", "action": "augment",
            "content": agent_msg.content, "outfits": [resp],
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
        res = eng.refine(base.get("items", []), info.get("category"), all_items, ctx,
                         sub_category=info.get("sub_category"))

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
        # 同步锁定态到替换后的这套，支持「换鞋→再加个包」连续微调
        cons = dict(session.constraints or {})
        cons["locked_outfit"] = resp
        session.constraints = cons

        agent_msg = ChatMessage(
            message_id=utils.gen_id("m"), session_id=session_id, role="agent",
            action="refine",
            content=f"保留其余 {res['kept_count']} 件，把「{res['from_name']}」换成了「{res['to_name']}」。",
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

    # off_topic：与穿搭无关的问题，委婉拒答并说明我能做什么
    if action == "off_topic":
        db.commit()
        return ok(data={
            "message_id": utils.gen_id("m"),
            "role": "agent",
            "action": "off_topic",
            "content": _OFF_TOPIC_REPLY,
            "outfits": [],
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
    if constraint_note:
        content = constraint_note + content

    # 重新推荐（含「换一批」）= 放弃锁定，清掉锁定态，避免之后「加个配饰」还接着旧套改
    cons = dict(session.constraints or {})
    if cons.pop("locked_outfit", None) is not None:
        session.constraints = cons

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


def _purge_sessions(db: Session, session_ids: list[str]) -> None:
    """物理删除会话及其消息，并清掉会话级天气刷新缓存。"""
    ids = list(dict.fromkeys(session_ids))
    if not ids:
        return
    db.query(ChatMessage).filter(ChatMessage.session_id.in_(ids)).delete(
        synchronize_session=False)
    db.query(ChatSession).filter(ChatSession.session_id.in_(ids)).delete(
        synchronize_session=False)
    for sid in ids:
        _WEATHER_REFRESH_CACHE.pop(sid, None)
    db.commit()


@router.delete("/{session_id}", response_model=schemas.APIResponse)
def delete_session(session_id: str, request: Request, db: Session = Depends(get_db)):
    """删除单条历史对话（连同其中的消息）。只能删自己的会话。"""
    session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    security.assert_owner(session.user_id, request)
    _purge_sessions(db, [session_id])
    return ok(data={"deleted": [session_id], "deleted_count": 1})


@router.post("/sessions/batch-delete", response_model=schemas.APIResponse)
def batch_delete_sessions(req: schemas.SessionBatchDelete, request: Request,
                          db: Session = Depends(get_db)):
    """批量删除 / 清空历史对话。越权 id 直接忽略，不做任何删除。"""
    security.assert_owner(req.user_id, request)
    if req.clear_all:
        ids = [s.session_id for s in db.query(ChatSession.session_id)
               .filter(ChatSession.user_id == req.user_id).all()]
    else:
        ids = list(dict.fromkeys(x for x in req.session_ids if x))
        if len(ids) > 200:
            raise HTTPException(status_code=400, detail="单次最多删除 200 条对话")
    if not ids:
        return ok(data={"deleted": [], "deleted_count": 0, "requested_count": 0})
    rows = (db.query(ChatSession.session_id)
            .filter(ChatSession.user_id == req.user_id,
                    ChatSession.session_id.in_(ids)).all())
    found = [r.session_id for r in rows]
    _purge_sessions(db, found)
    return ok(data={"deleted": found, "deleted_count": len(found),
                    "requested_count": len(ids)})


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

    # 拦截要在检索之前：无关问题不该借知识库片段硬答，人设问答也不该被检索带偏。
    # 用能力白名单门禁（本地快判 + LLM 开放集兜底）替换纯黑名单，弥补 SSE 不过意图分类的盲区。
    persona_answer = _preset_answer(text)
    other_name_call = _detect_other_name_call(text)
    domain_hit = _domain_allows(text) if persona_answer is None else True
    off_topic_hit = persona_answer is None and (other_name_call or domain_hit is False)

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
        passages = [] if (persona_answer or off_topic_hit) else kb.search(text, top_k=5)
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
        _vibe_note = (session.constraints or {}).get("vibe_note")
        if _vibe_note:
            _ctx_lines.append(f"用户对氛围的落位要求：{_vibe_note}")
        _ctx_lines.extend(_closet_inventory_context(_item_dicts(db, session.user_id)))
        _closet_context = "\n".join(_ctx_lines)

        user_msg = ChatMessage(
            message_id=utils.gen_id("m"), session_id=session_id,
            role="user",
            action="persona" if persona_answer else ("off_topic" if off_topic_hit else "knowledge"),
            content=text,
        )
        db.add(user_msg)
        db.commit()
        uid_for_log = session.user_id
    finally:
        db.close()

    def _gen():
        from app.agent import llm as _llm

        src_stream = None
        canned = persona_answer or (_OFF_TOPIC_REPLY if off_topic_hit else None)
        if not canned and llm_on:
            src_stream = _llm.ask_stream(text, passages, history=history,
                                         context=_closet_context)

        if canned or src_stream is not None:
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
                    "preset": bool(canned),
                    "passages": [{"title": p["title"], "doc": p["doc"],
                                  "source": p["source"]} for p in passages[:5]]})

        collected: list[str] = []
        if canned:
            collected.append(canned)
            yield _sse({"type": "delta", "text": canned})
        elif src_stream is not None:
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
                action="off_topic" if off_topic_hit else "knowledge", content=content,
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
