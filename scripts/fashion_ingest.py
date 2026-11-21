# -*- coding: utf-8 -*-
"""时尚资讯灵感图采集（月度批处理）。

数据源：Vogue / ELLE 官方 RSS（只取 RSS 自带的标题、链接、摘要与封面图，
不转载正文）。图片下载为本地缩略图，写入 data/uploads/inspo/ 与
fashion_inspo 表；前端时尚资讯回答以「灵感图 + 衣橱单品」双栏展示。

运行：
    PYTHONPATH=. C:/Python311/python.exe scripts/fashion_ingest.py
可选参数：
    --dry-run   只解析不写库不落盘
    --limit N   每个源最多取 N 条（默认 20）

失败隔离：单条图片下载失败只跳过该条；整体失败不影响已有数据。
历史批次图片默认保留最近 3 个月，超期清理。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None

try:
    from PIL import Image
except ImportError:
    Image = None

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import utils  # noqa: E402
from app.db import SessionLocal, FashionInspo, init_db  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
INSPO_DIR = BASE_DIR / "data" / "uploads" / "inspo"
BATCH = datetime.now().strftime("%Y-%m")

FEEDS = [
    ("vogue", "https://www.vogue.com/feed/rss"),
    ("elle", "https://www.elle.com/rss/fashion.xml"),  # 只用时尚分区，排除八卦/星座/美食
]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 英文标题 -> 中文标签的映射表。仅用于本地检索标签，不改写原文标题。
# 一个词命中就把对应中文标签附加到该条，便于「今年流行什么颜色」这类中文问题匹配。
TAG_RULES = [
    (("red", "burgundy", "cherry", "scarlet"), ["红色", "酒红"]),
    (("pink", "blush", "rose", "barbie"), ["粉色"]),
    (("orange", "tangerine", "peach"), ["橙色"]),
    (("yellow", "butter", "lemon"), ["黄色"]),
    (("green", "olive", "sage", "emerald", "forest"), ["绿色"]),
    (("blue", "denim", "navy", "cobalt", "sapphire"), ["蓝色", "牛仔"]),
    (("purple", "lilac", "lavender", "violet"), ["紫色"]),
    (("brown", "chocolate", "caramel", "cognac"), ["棕色", "美拉德"]),
    (("grey", "gray", "silver"), ["灰色"]),
    (("white", "cream", "ivory", "pearl", "oat"), ["白色", "奶油色"]),
    (("black",), ["黑色"]),
    (("beige", "tan", "neutr", "sand", "taupe"), ["米色", "中性色"]),
    (("metallic", "gold", "sequin", "glitter", "sparkle"), ["金属色", "亮片"]),
    (("monochrome", "all-black", " tonal"), ["同色系"]),
    (("knit", "sweater", "cardigan", "cashmere"), ["针织", "毛衣"]),
    (("denim", "jeans", "jacket"), ["牛仔"]),
    (("dress", "gown", "skirt"), ["连衣裙", "半裙"]),
    (("coat", "trench", "jacket", "outerwear", "blazer"), ["外套", "西装"]),
    (("shirt", "blouse", "top", "tee"), ["上装"]),
    (("trouser", "pants", "jeans"), ["下装"]),
    (("shoe", "boot", "sneaker", "heel", "loafer", "sandal"), ["鞋履"]),
    (("bag", "handbag", "purse", "tote"), ["包袋"]),
    (("jewel", "necklace", "earring", "ring", "bracelet"), ["配饰", "首饰"]),
    (("sunglass", "hat", "scarf", "belt", "cap"), ["配饰"]),
    (("suit", "tailoring", "tailored", "blazer"), ["西装", "通勤"]),
    (("lingerie", "lace", "sheer", "slip"), ["蕾丝", "透视"]),
    (("sport", "athletic", "activewear", "gym", "sneaker"), ["运动风"]),
    (("street", "streetwear", "grunge", "punk"), ["街头风"]),
    (("vintage", "retro", "90s", "70s", "archive"), ["复古风"]),
    (("minimalist", "minimalism", "quiet luxury", "minimal style"), ["极简风", "老钱风"]),
    (("romantic", "feminine", "floral", "ruffle", "bow"), ["浪漫风", "印花"]),
    (("dark", "goth", "noir", "moody"), ["暗黑风"]),
    (("boho", "bohemian", "festival", "crochet", "fringe"), ["波西米亚"]),
    (("resort", "beach", "summer", "vacation", "swim"), ["度假风"]),
    (("wedding", "bride", "bridal"), ["婚礼"]),
    (("fall", "autumn"), ["秋季"]),
    (("winter",), ["冬季"]),
    (("spring",), ["春季"]),
    (("summer",), ["夏季"]),
    (("holiday", "festive", "christmas", "new year"), ["节日"]),
    (("office", "workwear", "career", "commut"), ["通勤"]),
    (("celebrity", "red carpet", "gala", "met gala", "awards"), ["明星红毯"]),
    (("runway", "fashion week", "collection", "spring summer", "fall winter"), ["时装周"]),
    # 性别：问「男生穿搭」时据此召回男模图，避免错配女装图
    (("menswear", "men's", "for men", "gentleman", "tom ford", "dsquared2"), ["男装"]),
    (("womenswear", "women's", "for women", "bridal", "wedding dress"), ["女装"]),
]

# 标题级时尚信号：命中任一才入库，进一步剔除娱乐/生活/政治类文章
FASHION_SIGNAL = re.compile(
    r"fashion|style|wear|outfit|dress|coat|jacket|shoe|boot|sneaker|bag|jewel|"
    r"knit|sweater|denim|jean|skirt|suit|blazer|shirt|top|look|color|trend|"
    r"collection|campaign|runway|designer|brand|sale|shop|editorial|wardrobe|"
    r"glove|belt|hat|scarf|silhouette|clothing|clothes|beauty|makeup|manicure|"
    r"礼服|穿搭|时装|时尚|流行|color|style",
    re.I,
)

# 负向过滤：旅行/美食/图书/人事新闻等虽可能带 fashion 字样，但不是穿搭图
NOT_FASHION = re.compile(
    r"where to stay|hotel|ryokan|restaurant|recipe|food|horoscope|odyssey|"
    r"auction|book|prize|interview|podcast|guide to|best hotels|ceramic",
    re.I,
)

NS = {"m": "http://search.yahoo.com/mrss/"}


def _strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s[:300]


def _tags_from_text(text: str) -> list[str]:
    """英文标题/摘要 -> 中文标签。用词边界避免 cap 命中 capture、ring 命中 during。"""
    low = (text or "").lower()
    out: list[str] = []
    for needles, tags in TAG_RULES:
        if any(re.search(r"(?<![a-z])" + re.escape(n.strip()) + r"(?![a-z])", low)
               for n in needles):
            for t in tags:
                if t not in out:
                    out.append(t)
    return out[:12]


def _first_text(node, path: str) -> str:
    el = node.find(path)
    return (el.text or "").strip() if el is not None else ""


def _image_url(item) -> str:
    for el in item.findall("m:content", NS):
        u = el.get("url", "")
        if u.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            return u
    for el in item.findall("m:thumbnail", NS):
        u = el.get("url", "")
        if u:
            return u
    enc = item.find("enclosure")
    if enc is not None and enc.get("url", ""):
        return enc.get("url")
    # 兜底：description 里的第一张图
    desc = _first_text(item, "description")
    m = re.search(r'https?://[^"\' ]+\.(?:jpg|jpeg|png|webp)', desc, re.I)
    return m.group(0) if m else ""


def _thumb_url(url: str) -> str:
    """把原图 CDN 地址改写为小图，减少下载体积（各站规则不同，失败回退原图）。"""
    if "assets.vogue.com/photos/" in url and "/master/pass/" in url:
        return url.replace("/master/pass/", "/w_600,c_limit/")
    if "hips.hearstapps.com/hmg-prod/images/" in url and "?" not in url:
        return url + "?resize=480:*"
    return url


def _download_thumb(url: str) -> bytes | None:
    if httpx is None or Image is None:
        return None
    for candidate in (_thumb_url(url), url):
        try:
            with httpx.Client(timeout=12.0, follow_redirects=True, headers={"User-Agent": UA}) as c:
                r = c.get(candidate)
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
                continue
            im = Image.open(io.BytesIO(r.content))
            im.thumbnail((400, 520))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=78, optimize=True)
            return buf.getvalue()
        except Exception:
            continue
    return None


def ingest(limit: int = 20, dry_run: bool = False) -> dict:
    if httpx is None:
        raise RuntimeError("缺少 httpx，请先安装依赖")
    init_db()
    os_dir = INSPO_DIR
    if not dry_run:
        os_dir.mkdir(parents=True, exist_ok=True)

    db = SessionLocal()
    stat = {"fetched": 0, "saved": 0, "skipped": 0, "failed": 0, "by_source": {}}
    seen: set[str] = set()
    try:
        for source, feed_url in FEEDS:
            ok_src = 0
            try:
                with httpx.Client(timeout=15.0, follow_redirects=True, headers={"User-Agent": UA}) as c:
                    resp = c.get(feed_url)
                resp.raise_for_status()
                root = ET.fromstring(resp.content)
            except Exception as e:
                print(f"[{source}] RSS 抓取失败：{e}")
                stat["by_source"][source] = 0
                continue

            items = root.findall(".//item")[:limit]
            print(f"[{source}] RSS 条目 {len(items)} 条")
            for item in items:
                stat["fetched"] += 1
                title = _strip_html(_first_text(item, "title"))
                link = _first_text(item, "link")
                summary = _strip_html(_first_text(item, "description"))
                image_url = _image_url(item)
                if not title or not image_url or not link:
                    stat["skipped"] += 1
                    continue
                if not FASHION_SIGNAL.search(title + " " + summary) or NOT_FASHION.search(title):
                    stat["skipped"] += 1
                    continue
                # 按 CDN 原图地址去重（跨批次稳定的指纹）
                fp = hashlib.md5(image_url.split("?")[0].encode("utf-8")).hexdigest()[:12]
                if fp in seen:
                    stat["skipped"] += 1
                    continue
                exists = db.query(FashionInspo).filter(FashionInspo.inspo_id == fp).first()
                if exists:
                    stat["skipped"] += 1
                    continue
                seen.add(fp)

                tags = _tags_from_text(title + " " + summary)
                if dry_run:
                    print(f"  [dry] {source} | {title[:52]} | tags={tags}")
                    ok_src += 1
                    continue

                rel = f"/uploads/inspo/{BATCH}-{source}-{fp}.jpg"
                local = os_dir / rel.rsplit("/", 1)[-1]
                if local.exists() and local.stat().st_size > 1000:
                    data = local.read_bytes()  # 复用上次中断前已下载的图片
                else:
                    data = _download_thumb(image_url)
                if not data:
                    stat["failed"] += 1
                    print(f"  [skip] 图片下载失败：{title[:40]}")
                    continue
                local.write_bytes(data)
                db.add(FashionInspo(
                    inspo_id=fp, source=source, title=title, link=link,
                    image_path=rel, image_url=image_url.split("?")[0],
                    summary=summary, tags=tags, batch=BATCH,
                ))
                db.commit()  # 逐条提交：中途失败也保住已抓到的图
                ok_src += 1
                stat["saved"] += 1
            stat["by_source"][source] = ok_src

        if not dry_run:
            _prune_old(db)
        return stat
    finally:
        db.close()


def _prune_old(db, keep_batches: int = 3) -> None:
    """只保留最近 keep_batches 个月的批次，删除超期记录与图片文件。"""
    from sqlalchemy import func
    batches = [r[0] for r in db.query(func.distinct(FashionInspo.batch))
               .order_by(FashionInspo.batch.desc()).all() if r[0]]
    stale = batches[keep_batches:]
    for b in stale:
        rows = db.query(FashionInspo).filter(FashionInspo.batch == b).all()
        for r in rows:
            if r.image_path:
                p = INSPO_DIR / r.image_path.rsplit("/", 1)[-1]
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
        db.query(FashionInspo).filter(FashionInspo.batch == b).delete(synchronize_session=False)
        print(f"[prune] 清理旧批次 {b}：{len(rows)} 条")
    db.commit()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    stat = ingest(limit=max(1, args.limit), dry_run=args.dry_run)
    print("\n汇总：", stat)


if __name__ == "__main__":
    main()
