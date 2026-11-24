# -*- coding: utf-8 -*-
"""时尚资讯只读接口：给首页「时尚资讯」板块拉取最新灵感图。

数据来源是 scripts/fashion_ingest.py 月度采集写入的 fashion_inspo 表
（Vogue / ELLE 官方 RSS，只存标题、链接、本地缩略图与中文标签）。
这里只做只读列表，不写库、不联网，接口挂 /api/v1/fashion 前缀。
"""
from html import unescape

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import FashionInspo, get_db

router = APIRouter()


@router.get("/inspo")
def list_inspo(
    limit: int = Query(8, ge=1, le=24, description="返回条数"),
    db: Session = Depends(get_db),
):
    """最新时尚资讯列表，按采集时间倒序。"""
    rows = (db.query(FashionInspo)
              .order_by(FashionInspo.fetched_at.desc(), FashionInspo.inspo_id.desc())
              .limit(limit)
              .all())
    items = [{
        "inspo_id": r.inspo_id,
        "source": r.source or "",
        "title": unescape(r.title or ""),          # RSS 摘要里 H&amp;M 这类实体还原成原文
        "link": r.link or "",
        "image": r.image_path or "",   # 本地缩略图，/uploads 静态挂载
        "summary": unescape(r.summary or "")[:80],
        "tags": r.tags or [],
        "fetched_at": r.fetched_at.isoformat() if r.fetched_at else "",
    } for r in rows]
    return {"code": 0, "msg": "ok", "data": {"items": items, "total": len(items)}}
