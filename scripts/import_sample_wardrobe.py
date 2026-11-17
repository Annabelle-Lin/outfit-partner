# -*- coding: utf-8 -*-
"""把「衣橱示例数据」导入本地电子衣橱（直接写 SQLite，不依赖后端服务）。

为什么要单独写脚本而不是直接用 Excel 导入接口：
- 示例表是中文列名，且图片是独立文件，接口只收 image_url；
- 备注里有逗号、顿号、斜杠，Excel 的 tags/occasion 会按这些符号切分，备注会被切碎；
- 示例品类较粗（风衣/裤子/裙子），需要逐件映射到 rules/clothing_meta.json 的两级品类。

安全约定：
- 默认只预演：解析、校验、打印映射结果，不写库、不复制图片；
- 加 --apply 才真正写入；重复执行按 tag「示例编号：Wxxx」跳过已导入项，不产生重复；
- 只新增衣物，不修改、不删除你已有的任何记录；
- 断点文件只记 item_id / image_url，不保存密码。

用法（在项目根目录执行）：
    python scripts/import_sample_wardrobe.py                     # 预演，看映射
    python scripts/import_sample_wardrobe.py --apply             # 真正导入到默认数据库
    python scripts/import_sample_wardrobe.py --apply --limit 2   # 先试导 2 件
    python scripts/import_sample_wardrobe.py --apply --user-id u_xxxx
    python scripts/import_sample_wardrobe.py --apply --create-user 示例衣橱 --init-db

数据库默认取 app.config.DB_PATH（~/.wardrobe/wardrobe.db），可用 --db-path 覆盖。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
# 让脚本无论从哪个目录调用，都能导入项目自身的 app 包
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
ID_TAG_PREFIX = "示例编号："
NOTE_PREFIX = "备注："
STATUS_TAGS = {"清洗中", "暂不可穿"}

# ---------------------------------------------------------------- 解析源文件

@dataclass
class SampleItem:
    sample_id: str
    name: str
    image: str
    raw_category: str
    raw_color: str
    color_hex: str
    season: str
    raw_fit: str
    raw_fabric: str
    occasions: list[str] = field(default_factory=list)
    status: str = "可穿"
    notes: str = ""


def _split_occasions(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[/、,，]", value) if part.strip()]


def parse_source(sample_dir: Path) -> tuple[list[SampleItem], dict[str, dict[str, str]]]:
    source = sample_dir / "衣物标签表.md"
    if not source.is_file():
        raise SystemExit(f"找不到标签文件：{source}")
    text = source.read_text(encoding="utf-8")

    # 逐件明细：备注 + 带 #RRGGBB 的主颜色
    details: dict[str, dict[str, str]] = {}
    blocks = re.split(r"(?m)^### (W\d{3})[ \t]+(.+?)[ \t]*$", text)
    for index in range(1, len(blocks), 3):
        sample_id, body = blocks[index], blocks[index + 2]

        def field(label: str) -> str:
            match = re.search(rf"(?m)^- \*\*{label}\*\*：(.+?)[ \t]*$", body)
            return match.group(1).strip() if match else ""

        details[sample_id] = {
            "notes": field("备注"),
            "color": field("主颜色"),
            "image": field("图片文件名").strip("`"),
        }

    # 标签总表
    items: list[SampleItem] = []
    row_pattern = re.compile(r"(?m)^\| (W\d{3}) \|.*$")
    for line in row_pattern.findall(text):
        pass
    for match in re.finditer(r"(?m)^\| (W\d{3}) \|(.+)\|[ \t]*$", text):
        sample_id = match.group(1)
        cells = [cell.strip() for cell in match.group(2).split("|")]
        if len(cells) < 9:
            continue
        image_cell, name, raw_category, raw_color, season, raw_fit, raw_fabric, occasions, status = cells[:9]
        image_match = re.search(r"\(([^)]+)\)", image_cell)
        detail = details.get(sample_id, {})
        color_with_hex = detail.get("color") or raw_color
        hex_match = re.search(r"#([0-9A-Fa-f]{6})", color_with_hex)
        items.append(SampleItem(
            sample_id=sample_id,
            name=name,
            image=Path(image_match.group(1)).name if image_match else detail.get("image", ""),
            raw_category=raw_category,
            raw_color=raw_color,
            color_hex=hex_match.group(1) if hex_match else "",
            season=season,
            raw_fit=raw_fit,
            raw_fabric=raw_fabric,
            occasions=_split_occasions(occasions),
            status=status,
            notes=detail.get("notes", ""),
        ))
    return sorted(items, key=lambda item: item.sample_id), details


# ---------------------------------------------------------------- 字段映射
# 顺序敏感：先匹配更具体的词，避免「牛仔衬衫」被判成牛仔裤、「运动裤」被判成长裤。
_CATEGORY_RULES: tuple[tuple[tuple[str, ...], tuple[str, str]], ...] = (
    (("连衣裙",), ("连衣装", "连衣裙")),
    (("羽绒服",), ("外套", "羽绒服")),
    (("西装外套",), ("外套", "西装外套")),
    (("风衣",), ("外套", "风衣")),
    (("大衣",), ("外套", "大衣")),
    (("开衫",), ("外套", "开衫")),
    (("夹克", "工装外套"), ("外套", "夹克")),
    (("Polo", "polo"), ("上衣", "Polo衫")),
    (("衬衫",), ("上衣", "衬衫")),
    (("卫衣",), ("上衣", "卫衣")),
    (("毛衣",), ("上衣", "毛衣")),
    (("T恤", "t恤"), ("上衣", "T恤")),
    (("牛仔裤",), ("下装", "牛仔裤")),
    (("西装裤",), ("下装", "西裤")),
    (("运动裤",), ("下装", "运动裤")),
    (("阔腿裤", "休闲裤"), ("下装", "长裤")),
    (("半身裙", "百褶裙", "裙"), ("下装", "半身裙")),
    (("帆布鞋",), ("鞋", "帆布鞋")),
    (("乐福鞋",), ("鞋", "乐福鞋")),
    (("运动鞋",), ("鞋", "运动鞋")),
    (("靴",), ("鞋", "靴子")),
    (("包",), ("配饰", "包包")),
    (("围巾",), ("配饰", "围巾")),
    (("腰带", "皮带"), ("配饰", "腰带")),
    (("帽",), ("配饰", "帽子")),
)

_COLOR_RULES: tuple[tuple[str, str], ...] = (
    ("藏青", "蓝"), ("牛仔蓝", "蓝"), ("浅蓝", "蓝"), ("深蓝", "蓝"), ("蓝", "蓝"),
    ("米白", "米"), ("米色", "米"), ("燕麦", "米"),
    ("卡其", "卡其"), ("白", "白"), ("黑", "黑"), ("灰", "灰"), ("棕", "棕"),
    ("红", "红"), ("黄", "黄"), ("绿", "绿"), ("粉", "粉"), ("紫", "紫"), ("橙", "橙"),
)

# 顺序敏感：混纺需先于纯棉，牛仔需最先。
_FABRIC_RULES: tuple[tuple[str, str], ...] = (
    ("牛仔", "牛仔"), ("皮革", "皮革"), ("雪纺", "雪纺"), ("网面", "化纤"),
    ("混纺", "混纺"), ("亚麻", "麻"), ("帆布", "棉"), ("法兰绒", "棉"),
    ("纯棉", "棉"), ("聚酯", "化纤"), ("羊毛", "毛"), ("毛", "毛"), ("棉", "棉"),
)

_FIT_RULES: tuple[tuple[str, str], ...] = (
    ("Oversize", "Oversize"), ("宽松", "宽松"), ("廓形", "Oversize"),
    ("修身", "修身"), ("常规", "合身"), ("合身", "合身"),
)

_WARMTH_BY_SUBCATEGORY = {
    "T恤": 1, "衬衫": 2, "Polo衫": 2, "卫衣": 3, "毛衣": 4, "打底衫": 2,
    "西裤": 3, "牛仔裤": 3, "长裤": 3, "运动裤": 3, "半身裙": 2,
    "连衣裙": 3, "风衣": 3, "羽绒服": 5, "西装外套": 3, "开衫": 3,
    "大衣": 4, "夹克": 3, "帆布鞋": 2, "乐福鞋": 2, "运动鞋": 2,
    "靴子": 3, "包包": 1, "围巾": 4, "腰带": 1, "帽子": 2,
}


def _match(rules, value: str, field_name: str, sample_id: str):
    for keys, target in rules:
        if isinstance(keys, str):
            keys = (keys,)
        if any(key in value for key in keys):
            return target
    raise SystemExit(f"{sample_id} 的{field_name}无法映射：{value!r}（请补充 rules）")


def map_category(item: SampleItem) -> tuple[str, str]:
    return _match(_CATEGORY_RULES, item.name, "品类", item.sample_id)


def map_color(item: SampleItem) -> str:
    return _match(_COLOR_RULES, item.raw_color, "主颜色", item.sample_id)


def map_brightness(item: SampleItem) -> str:
    if not item.color_hex:
        raise SystemExit(f"{item.sample_id} 缺少 #RRGGBB 色值，无法判断亮/暗色")
    red, green, blue = (int(item.color_hex[i:i + 2], 16) for i in (0, 2, 4))
    return "亮色" if (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255 >= 0.65 else "暗色"


def map_fabric(item: SampleItem) -> str:
    return _match(_FABRIC_RULES, item.raw_fabric, "面料", item.sample_id)


def map_fit_snug(item: SampleItem) -> str:
    return _match(_FIT_RULES, item.raw_fit, "版型", item.sample_id)


def map_fit_silhouette(item: SampleItem, sub_category: str) -> str:
    for keyword, value in (("A字", "A字"), ("收腰", "A字"), ("包臀", "紧身"),
                           ("阔腿", "阔腿"), ("直筒", "直筒"), ("百褶", "A字")):
        if keyword in item.name:
            return value
    if sub_category in {"连衣裙", "毛衣", "T恤", "衬衫", "卫衣", "Polo衫"}:
        return "直筒"
    return "直筒"


def map_warmth(item: SampleItem, sub_category: str) -> int:
    level = _WARMTH_BY_SUBCATEGORY.get(sub_category, 3)
    if item.season == "冬" and sub_category in {"西裤", "牛仔裤", "长裤", "运动裤", "连衣裙", "半身裙"}:
        level = min(5, level + 1)
    if item.season == "夏" and sub_category in {"连衣裙", "半身裙", "长裤"}:
        level = max(1, level - 1)
    if item.name == "黑色毛线帽":
        level = 4
    return level


def map_formality(item: SampleItem) -> int:
    if "正式" in item.occasions:
        return 4
    if "通勤" in item.occasions:
        return 3
    if "运动" in item.occasions:
        return 1
    return 2


def build_item(item: SampleItem) -> dict[str, Any]:
    category, sub_category = map_category(item)
    tags = [f"{ID_TAG_PREFIX}{item.sample_id}"]
    if item.notes:
        tags.append(f"{NOTE_PREFIX}{item.notes}")
    if item.status != "可穿":
        tags.append(item.status)
    return {
        "name": item.name,
        "category": category,
        "sub_category": sub_category,
        "color": map_color(item),
        "color_brightness": map_brightness(item),
        "season": item.season,
        "fit_snug": map_fit_snug(item),
        "fit_silhouette": map_fit_silhouette(item, sub_category),
        "fabric": map_fabric(item),
        "style": "",
        "occasion": list(item.occasions),
        "warmth_level": map_warmth(item, sub_category),
        "formality_level": map_formality(item),
        "tags": tags,
    }


def validate(payload: dict[str, Any], meta: dict[str, Any], sample_id: str) -> None:
    categories = meta.get("category", {})
    if categories:
        if payload["category"] not in categories:
            raise SystemExit(f"{sample_id} 品类 {payload['category']} 不在 rules/clothing_meta.json")
        if payload["sub_category"] not in categories[payload["category"]]:
            raise SystemExit(
                f"{sample_id} 二级品类无效：{payload['category']}/{payload['sub_category']}"
            )
    checks = (
        ("color", "color_family"), ("color_brightness", "color_brightness"),
        ("season", "season"), ("fit_snug", "fit_snug"),
        ("fit_silhouette", "fit_silhouette"), ("fabric", "fabric"),
    )
    for field_name, meta_key in checks:
        allowed = meta.get(meta_key) or []
        if allowed and payload[field_name] not in allowed:
            raise SystemExit(f"{sample_id} {field_name}={payload[field_name]} 不在项目枚举 {allowed}")
    allowed_occasions = set(meta.get("occasion") or [])
    unknown = [value for value in payload["occasion"] if allowed_occasions and value not in allowed_occasions]
    if unknown:
        raise SystemExit(f"{sample_id} 场合不在项目枚举：{unknown}")


def load_items(sample_dir: Path) -> list[SampleItem]:
    items, _details = parse_source(sample_dir)
    expected = [f"W{number:03d}" for number in range(1, 51)]
    ids = [item.sample_id for item in items]
    if len(items) != 50 or ids != expected:
        missing = sorted(set(expected) - set(ids))
        extra = sorted(set(ids) - set(expected))
        raise SystemExit(f"预期 W001–W050 共 50 件，实际 {len(items)} 件；缺失={missing}，额外={extra}")
    for item in items:
        if not item.image or Path(item.image).name != item.image:
            raise SystemExit(f"{item.sample_id} 图片文件名异常：{item.image!r}")
        image_path = sample_dir / "images" / item.image
        if not image_path.is_file():
            raise SystemExit(f"{item.sample_id} 图片缺失：{image_path}")
        if not item.occasions:
            raise SystemExit(f"{item.sample_id} 适用场合为空")
        if item.status not in {"可穿", *STATUS_TAGS}:
            raise SystemExit(f"{item.sample_id} 未知状态：{item.status}")
        if not item.notes:
            raise SystemExit(f"{item.sample_id} 缺少备注")
    return items


# ---------------------------------------------------------------- 数据写入

def _load_project_db_module(db_path: Path):
    """在导入 app.db 前设定 DATABASE_URL，使模型绑定到目标库。"""
    from sqlalchemy import create_engine  # 尽早暴露依赖缺失
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))
    from app import db as db_module  # noqa: PLC0415
    return db_module


def run(args: argparse.Namespace) -> int:
    sample_dir = (args.sample_dir or (BASE_DIR / "衣橱示例数据")).expanduser()
    items = load_items(sample_dir)
    meta_path = BASE_DIR / "rules" / "clothing_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    payloads = {item.sample_id: build_item(item) for item in items}
    for item in items:
        validate(payloads[item.sample_id], meta, item.sample_id)

    selected = items[:args.limit] if args.limit else items
    print(f"已校验源数据 {len(items)} 件（W001–W050，图片齐全）。本次范围 {len(selected)} 件。\n")
    print(f"{'编号':<5}{'名称':<20}{'品类':<14}{'颜色':<4}{'亮暗':<4}{'季节':<5}{'版型':<5}{'面料':<5}")
    print("-" * 82)
    for item in selected:
        payload = payloads[item.sample_id]
        print(f"{item.sample_id:<6}{item.name[:18]:<20}"
              f"{payload['category'] + '/' + payload['sub_category']:<15}"
              f"{payload['color']:<5}{payload['color_brightness']:<5}{payload['season']:<6}"
              f"{payload['fit_snug']:<6}{payload['fabric']:<5}")
    print("\n大品类分布：" + "，".join(
        f"{key} {value}" for key, value in sorted(Counter(p["category"] for p in payloads.values()).items())))
    print("状态分布：" + "，".join(
        f"{key} {value}" for key, value in sorted(Counter(i.status for i in items).items())))
    print("说明：暖度 1–5、正式度 1–5 由季节/场合规则推断（源数据未给数值），导入后可在衣橱里继续调整；\n"
          "      备注完整保存为 tag「备注：…」，示例编号保存为 tag「示例编号：Wxxx」。")

    if not args.apply:
        print("\n预演结束：未写数据库、未复制图片。确认无误后加 --apply 正式导入。")
        return 0

    if args.db_path:
        db_path = args.db_path.expanduser()
    else:
        from app import config as app_config  # noqa: PLC0415
        db_path = Path(app_config.DB_PATH)
    db_module = _load_project_db_module(db_path)
    # 安全校验：项目数据层必须真的绑定到目标库，否则拒绝写入，避免误写其它数据库
    actual_db = str(getattr(db_module.engine.url, "database", "") or "")
    if actual_db and Path(actual_db).resolve() != db_path.resolve():
        raise SystemExit(
            f"错误：项目 app/db.py 未采用 DATABASE_URL，实际绑定 {actual_db}，"
            f"与指定 {db_path} 不一致；已停止，不写入任何数据。"
        )
    db = db_module.SessionLocal()
    try:
        db_module.Base.metadata.create_all(bind=db_module.engine)
        user_id = args.user_id
        if not user_id and args.create_user:
            user = (db.query(db_module.User)
                    .filter(db_module.User.username == args.create_user)
                    .first())
            if user is None:
                user = db_module.User(
                    user_id=f"u_{uuid.uuid4().hex[:10]}",
                    nickname=args.create_user,
                    username=args.create_user,
                    password_hash="",
                )
                db.add(user)
                db.commit()
                db.refresh(user)
                print(f"\n已创建本地账号：{user.nickname}（{user.user_id}）；未设置密码，仅供数据导入/演示。")
            user_id = user.user_id
        if not user_id:
            users = db.query(db_module.User).all()
            if not users:
                print("\n数据库里还没有用户。请加 --create-user 演示衣橱 创建一个，或用 --user-id 指定。",
                      file=sys.stderr)
                return 2
            print("\n数据库现有用户，请用 --user-id 指定要导入哪个衣橱：")
            for candidate in users:
                count = (db.query(db_module.ClothingItem)
                         .filter(db_module.ClothingItem.user_id == candidate.user_id).count())
                print(f"  {candidate.user_id}  {candidate.nickname or candidate.username or '(未命名)'}  "
                      f"用户名={candidate.username or '(无)'}  现有衣物 {count} 件")
            return 2
        user = db.query(db_module.User).filter(db_module.User.user_id == user_id).first()
        if user is None:
            print(f"错误：user_id={user_id} 不存在", file=sys.stderr)
            return 2
        print(f"\n目标账号：{user.nickname or user.username}（{user.user_id}），数据库：{db_module.engine.url}")

        uploads_dir = (args.uploads_dir or (BASE_DIR / "data" / "uploads")).expanduser()
        uploads_dir.mkdir(parents=True, exist_ok=True)

        existing: dict[str, Any] = {}
        for row in db.query(db_module.ClothingItem).filter(db_module.ClothingItem.user_id == user_id):
            for tag in (row.tags or []):
                if isinstance(tag, str) and tag.startswith(ID_TAG_PREFIX):
                    existing[tag[len(ID_TAG_PREFIX):].strip()] = row

        created = skipped = 0
        failures: list[str] = []
        for item in selected:
            current = existing.get(item.sample_id)
            if current is not None:
                skipped += 1
                print(f"[跳过] {item.sample_id} {item.name}（已存在 item_id={current.item_id}）")
                continue
            source_image = sample_dir / "images" / item.image
            target_name = f"sample_{item.sample_id}_{uuid.uuid4().hex[:8]}{source_image.suffix.lower()}"
            target_path = uploads_dir / target_name
            try:
                shutil.copyfile(source_image, target_path)
                payload = dict(payloads[item.sample_id])
                payload["image_url"] = f"/uploads/{target_name}"
                now = datetime.utcnow()
                row = db_module.ClothingItem(
                    item_id=f"i_{uuid.uuid4().hex[:10]}",
                    user_id=user_id,
                    created_at=now,
                    updated_at=now,
                    **payload,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                created += 1
                print(f"[已导入] {item.sample_id} {item.name} → {payload['category']}/{payload['sub_category']}")
            except Exception as exc:  # 单件失败不影响其余，并回报原因
                db.rollback()
                if target_path.exists():
                    target_path.unlink(missing_ok=True)
                failures.append(f"{item.sample_id}: {exc}")
                print(f"[失败] {item.sample_id} {item.name}：{exc}", file=sys.stderr)

        # 回读校验
        rows = (db.query(db_module.ClothingItem)
                .filter(db_module.ClothingItem.user_id == user_id).all())
        by_id: dict[str, Any] = {}
        for row in rows:
            for tag in (row.tags or []):
                if isinstance(tag, str) and tag.startswith(ID_TAG_PREFIX):
                    by_id[tag[len(ID_TAG_PREFIX):].strip()] = row
        missing = [item.sample_id for item in items if item.sample_id not in by_id]
        no_image = [item.sample_id for item in items
                    if item.sample_id in by_id and not by_id[item.sample_id].image_url]
        image_broken = [item.sample_id for item in items
                        if item.sample_id in by_id
                        and by_id[item.sample_id].image_url
                        and not (BASE_DIR / "data" / by_id[item.sample_id].image_url.lstrip("/")).is_file()]
        status_bad = [item.sample_id for item in items
                      if item.status != "可穿" and item.sample_id in by_id
                      and item.status not in (by_id[item.sample_id].tags or [])]

        print("\n========== 导入结果 ==========")
        print(f"本次新增 {created}；跳过 {skipped}；失败 {len(failures)}")
        print(f"该衣柜当前示例衣物合计 {len(by_id)}/50 件")
        if missing:
            print("缺失编号：" + "，".join(missing))
        if no_image:
            print("无图片地址：" + "，".join(no_image))
        if image_broken:
            print("图片文件缺失：" + "，".join(image_broken))
        if status_bad:
            print("不可穿状态缺失：" + "，".join(status_bad))
        if failures:
            print("失败明细：")
            for line in failures:
                print("  " + line)
        ok = not missing and not no_image and not image_broken and not status_bad and not failures
        print("结论：" + ("50 件全部入库并通过回读校验（含图片与清洗中/暂不可穿状态）。"
                        if ok else "仍有缺口，见上方明细；修正后可重复执行，脚本会自动跳过已存在项。"))
        return 0 if ok else 1
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="导入衣橱示例数据（默认预演，--apply 才写库）")
    parser.add_argument("--apply", action="store_true", help="真正写入数据库并复制图片")
    parser.add_argument("--limit", type=int, help="只处理前 N 件（W001 起），用于试导 1–2 件")
    parser.add_argument("--sample-dir", type=Path, help="示例数据目录（默认：项目根/衣橱示例数据）")
    parser.add_argument("--db-path", type=Path, help="SQLite 文件路径（默认：app.config.DB_PATH）")
    parser.add_argument("--uploads-dir", type=Path, help="图片目录（默认：项目根/data/uploads）")
    parser.add_argument("--user-id", help="导入到哪个 user_id")
    parser.add_argument("--create-user", metavar="昵称", help="不存在则创建本地演示账号（无密码）")
    parser.add_argument("--init-db", action="store_true", help="库不存在时也允许建表（默认也会建表）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and not 1 <= args.limit <= 50:
        print("错误：--limit 需在 1–50 之间", file=sys.stderr)
        return 2
    try:
        return run(args)
    except SystemExit as exc:
        if str(exc.code).startswith("预期") or "无法映射" in str(exc.code) or "缺失" in str(exc.code):
            print(f"校验失败：{exc.code}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
