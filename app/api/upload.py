# -*- coding: utf-8 -*-
"""图片上传模块。

POST /upload/image   上传衣物 / 新品参考图，返回可访问的图片地址

POST /upload/analyze 识别衣物属性，供新增表单预填（2.6 多模态入库）

识别策略分三段，且每段都显式标注来源，不让用户误以为「模型看清了」：
1. 配置了视觉模型 -> 调用模型识别（source="llm"）
2. 无模型 -> 用图片主色做本地启发式（source="heuristic"，degraded=true）
3. 都失败 -> 只回文件名推断，其余留空等用户填

文件落到 data/uploads/，由 main.py 挂载 /uploads 静态目录对外提供。
"""
import base64
import colorsys
import io
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from app import config
from app.agent import llm
from app.api import ok

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent.parent
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif", ".bmp"}
MAX_SIZE = 8 * 1024 * 1024  # 8MB，手机直出照片足够


@router.post("/image", response_model=None)
async def upload_image(file: UploadFile = File(...)):
    """上传单张图片，返回 image_url（相对路径，前端直接拼到当前域名下）。

    失败约定：
    - 400 未选择文件 / 格式不支持 / 文件过大
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="未选择图片文件")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的图片格式 {ext or '(无后缀)'}，请上传 jpg / png / webp",
        )

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="图片内容为空")
    if len(raw) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="图片超过 8MB，请压缩后再上传")

    name = f"{int(time.time())}_{uuid.uuid4().hex[:10]}{ext}"
    path = UPLOAD_DIR / name
    path.write_bytes(raw)

    return ok(data={
        "image_url": f"/uploads/{name}",
        "filename": name,
        "size": len(raw),
    })

# ---------- 2.6 属性识别 ----------
_COLOR_BUCKETS = (
    ("黑", (0, 0, 0)), ("白", (255, 255, 255)), ("灰", (128, 128, 128)),
    ("红", (200, 40, 40)), ("橙", (230, 120, 30)), ("黄", (235, 205, 60)),
    ("绿", (60, 150, 80)), ("蓝", (50, 100, 190)), ("紫", (140, 80, 170)),
    ("粉", (240, 160, 180)), ("棕", (120, 80, 50)), ("卡其", (195, 175, 140)),
    ("藏青", (35, 55, 95)),
)


def _dominant_color(raw: bytes) -> tuple[str, tuple[int, int, int]] | None:
    """取图片主色：裁掉近白/近黑背景后按面积加权平均。

    只取中心区域，四角多为背景。没有 PIL 时返回 None，不猜。
    """
    try:
        from PIL import Image
    except ImportError:
        return None

    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((96, 96))
    except Exception:
        return None

    w, h = img.size
    if not w or not h:
        return None

    cx, cy = w / 2, h / 2
    center = []
    for y in range(h):
        for x in range(w):
            if abs(x - cx) < w * 0.32 and abs(y - cy) < h * 0.32:
                center.append(img.getpixel((x, y)))
    use = center or list(img.getdata())
    if not use:
        return None

    filtered = [p for p in use if not (p[0] > 244 and p[1] > 244 and p[2] > 244)]
    filtered = [p for p in filtered if not (p[0] < 22 and p[1] < 22 and p[2] < 22)]
    use = filtered or use

    r = sum(p[0] for p in use) // len(use)
    g = sum(p[1] for p in use) // len(use)
    b = sum(p[2] for p in use) // len(use)

    best, dist = "白", 1e18
    for name, (cr, cg, cb) in _COLOR_BUCKETS:
        d = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if d < dist:
            best, dist = name, d
    return best, (r, g, b)


def _guess_category(filename: str) -> dict:
    """文件名兜底：用户常按 shalizi.jpg / coat.png 命名。

    只填能确定命中的字段，其余留空，避免猜错污染衣橱。
    """
    low = (filename or "").lower()
    table = (
        (("衬衫", "shirt", "chenshan"), {"category": "上装", "sub_category": "衬衫"}),
        (("t恤", "tshirt", "txu"), {"category": "上装", "sub_category": "T恤"}),
        (("毛衣", "针织", "knit", "sweater"), {"category": "上装", "sub_category": "针织衫"}),
        (("卫衣", "hoodie"), {"category": "上装", "sub_category": "卫衣"}),
        (("风衣", "trench", "coat", "大衣", "外套", "jacket"),
         {"category": "外套", "sub_category": "风衣"}),
        (("牛仔", "jeans", "denim"), {"category": "下装", "sub_category": "牛仔裤"}),
        (("裤", "pants", "trouser"), {"category": "下装", "sub_category": "长裤"}),
        (("裙", "skirt", "dress"), {"category": "下装", "sub_category": "半身裙"}),
        (("鞋", "shoe", "sneaker", "boot", "皮鞋"), {"category": "鞋", "sub_category": "皮鞋"}),
        (("包", "bag"), {"category": "配饰", "sub_category": "包"}),
    )
    for keys, attrs in table:
        if any(k in low for k in keys):
            return dict(attrs)
    return {}


def _season_from_color(rgb: tuple[int, int, int]) -> str:
    """按明度/饱和度给季节倾向，仅作预填建议，用户可改。"""
    r, g, b = rgb
    _h, l, sat = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    if l < 0.32:
        return "秋冬"
    if l > 0.72 and sat < 0.2:
        return "春秋"
    if sat > 0.45:
        return "夏"
    return "四季"


@router.post("/analyze", response_model=None)
async def analyze_image(file: UploadFile = File(...)):
    """2.6 上传即识别，供新增表单预填。

    三段式，且每段都标注识别来源，不假装模型看清了：
    1) 有视觉模型 -> 调模型（source=llm）
    2) 无模型 -> 本地主色 + 文件名启发式（source=heuristic, degraded=true）
    3) 都拿不到 -> 返回缺失字段清单，让用户自己填
    """
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail="未选择图片文件")

    ext = Path(file.filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=f"不支持的图片格式 {ext or '(无后缀)'}")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="图片内容为空")
    if len(raw) > MAX_SIZE:
        raise HTTPException(status_code=400, detail="图片超过 8MB，请压缩后再上传")

    # 先落盘：表单提交时直接带这个地址，避免二次上传
    name = f"{int(time.time())}_{uuid.uuid4().hex[:10]}{ext}"
    (UPLOAD_DIR / name).write_bytes(raw)

    mime = {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")

    attrs: dict = {}
    source = "none"
    degraded = True
    reason = ""

    # 第 1 段：视觉模型
    if config.is_llm_enabled():
        try:
            got = llm.analyze_image(base64.b64encode(raw).decode(), mime)
        except Exception:
            got = None
        if got:
            attrs = {k: v for k, v in got.items() if isinstance(v, str) and v.strip()}
            source = "llm"
            degraded = False
        else:
            reason = "model_failed"
    else:
        reason = "no_key"

    # 第 2 段：本地启发式
    if not attrs:
        dom = _dominant_color(raw)
        guess = _guess_category(file.filename)
        if dom:
            color, rgb = dom
            attrs["color"] = color
            attrs["season"] = _season_from_color(rgb)
            source = "heuristic"
        attrs.update(guess)
        if not attrs:
            reason = "no_signal"

    missing = [k for k in ("category", "sub_category", "color", "season", "fabric", "style")
               if not attrs.get(k)]

    return ok(data={
        "image_url": f"/uploads/{name}",
        "filename": name,
        "attrs": attrs,
        "missing": missing,
        "source": source,
        "degraded": degraded,
        "degraded_reason": reason,
        "confidence": "high" if source == "llm" else ("medium" if source == "heuristic" else "low"),
    })
