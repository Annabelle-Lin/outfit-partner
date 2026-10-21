# -*- coding: utf-8 -*-
"""对话穿搭 Agent 模块（计划书 13.4，核心）。

POST /chat/session                创建对话会话
POST /chat/{session_id}/message   发送消息
GET  /chat/{session_id}           获取会话状态
POST /chat/{session_id}/end       结束会话
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import config, schemas, utils
from app.agent.engine import RecommendContext
from app.agent.kb import KnowledgeIndex
from app.agent.llm import ask
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
                   deduct: int = 3) -> tuple[list, str]:
    """推荐主链路：拉衣橱 -> 引擎推荐 -> 更新会话 -> 持久化方案快照。

    返回 (outfits, content)。deduct 为本次消耗的剩余次数（recommend 为 3，
    reject 已在调用方扣减，传 0）。
    """
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

    if outfits:
        content = f"为你推荐 {len(outfits)} 套搭配："
    else:
        content = result.get("reason", "暂时没有满足条件的搭配，建议放宽约束。")
    return outfits, content


@router.post("/session", response_model=schemas.APIResponse)
def create_session(req: schemas.ChatSessionCreate, request: Request, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.user_id == req.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    weather = req.weather
    session = ChatSession(
        session_id=utils.gen_id("s"),
        user_id=req.user_id,
        status="active",
        occasion=req.occasion or "",
        weather_city=weather.city if weather else "福州",
        weather_temp=weather.temp if weather else 22.0,
        weather_rain=weather.rain if weather else "无降雨",
        temp_pref_color=req.temp_pref.color if req.temp_pref else "",
        temp_pref_style=req.temp_pref.style if req.temp_pref else "",
        constraints={},
        shown_outfit_ids=[],
        remaining_count=10,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return ok(data=_session_to_response(session))


@router.post("/{session_id}/message", response_model=schemas.APIResponse)
def send_message(session_id: str, req: schemas.ChatMessagePayload, request: Request, db: Session = Depends(get_db)):
    session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.status == "ended":
        raise HTTPException(status_code=400, detail="会话已结束")

    user = db.query(User).filter(User.user_id == session.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    action = req.action
    payload = req.payload or {}

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

    # 处理自由文本解析
    if action == "free_text":
        parsed_action, parsed_payload = _parse_free_text(payload.get("text", ""))
        if parsed_action != "free_text":
            action = parsed_action
            payload = parsed_payload

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

        answer = None
        if passages and config.is_llm_enabled():
            answer = ask(query, passages)

        content = ""
        if answer:
            content = answer["answer"]
        elif passages:
            content = "我找到以下参考资料：\n" + "\n\n".join(
                f"[{i+1}] {p['title']}\n{p['text'][:300]}...\n出处：{p['source']}"
                for i, p in enumerate(passages[:3])
            )
        else:
            content = "知识库里暂时没有相关资料，建议换个问法或补充领域资料。"

        agent_msg = ChatMessage(
            message_id=utils.gen_id("m"),
            session_id=session_id,
            role="agent",
            action="knowledge",
            content=content,
            payload={"query": query, "passages": passages[:5], "llm_answer": answer},
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
        # 换一套：按契约扣 1 次，并实际重新推荐返回新方案
        session.remaining_count = max(0, session.remaining_count - 1)
        outfits, content = _run_recommend(db, request, session, user, deduct=0)
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

    outfits, content = _run_recommend(db, request, session, user)

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


@router.get("/{session_id}", response_model=schemas.APIResponse)
def get_session(session_id: str, db: Session = Depends(get_db)):
    session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    return ok(data=_session_to_response(session))


@router.post("/{session_id}/end", response_model=schemas.APIResponse)
def end_session(session_id: str, db: Session = Depends(get_db)):
    session = db.query(ChatSession).filter(ChatSession.session_id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    session.status = "ended"
    db.commit()
    return ok(data={"session_id": session_id, "status": "ended"})
