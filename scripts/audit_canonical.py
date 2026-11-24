# -*- coding: utf-8 -*-
"""存量衣物数据口径审计（只读，不改任何数据）。

2026-11-23 黑色羊毛大衣事故复盘：系统内曾并存三套词汇表，历史录入
与早期 seed 灌入可能残留非规范枚举值。本脚本扫描全库并输出审计报告；
修复迁移必须先跑本脚本、经确认后再执行。

    PYTHONPATH=. C:/Python311/python.exe scripts/audit_canonical.py
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.agent.garment_profile import (  # noqa: E402
    _meta_seasons,
    normalize_color,
    normalize_fabric,
    normalize_season,
)
from app.config import RULES_DIR  # noqa: E402

DB_PATH = os.path.join(os.path.expanduser("~"), ".wardrobe", "wardrobe.db")

meta = json.load(open(RULES_DIR / "clothing_meta.json", encoding="utf-8"))
META_FABRIC = {str(x) for x in meta.get("fabric", [])}
META_SUBS = {str(x) for x in meta.get("sub_category_alias", {}).values()}
CAT_MAP = meta.get("category", {})


def main() -> int:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT item_id, user_id, name, category, sub_category, color, "
        "color_brightness, season, fabric FROM clothing_items"
    ).fetchall()
    con.close()

    issues = {"season": [], "fabric": [], "color_brightness": [],
              "color": [], "sub_category": [], "category": []}
    for r in rows:
        label = f"[{r['user_id']}] {r['name']}(id={r['item_id'][:10]})"

        if r["season"] and r["season"] not in _meta_seasons():
            issues["season"].append(f"{label}: {r['season']!r} -> {normalize_season(r['season'])!r}")
        if r["fabric"] and r["fabric"] not in META_FABRIC:
            issues["fabric"].append(f"{label}: {r['fabric']!r} -> {normalize_fabric(r['fabric'])!r}")
        if r["color_brightness"] and r["color_brightness"] not in ("亮色", "暗色"):
            issues["color_brightness"].append(f"{label}: {r['color_brightness']!r}")
        if r["color"] and normalize_color(r["color"]) != r["color"]:
            issues["color"].append(f"{label}: {r['color']!r} -> {normalize_color(r['color'])!r}")
        if r["sub_category"] and r["category"] in CAT_MAP:
            allowed = CAT_MAP[r["category"]] or []
            norm = meta.get("sub_category_alias", {}).get(r["sub_category"], r["sub_category"])
            if allowed and norm not in allowed:
                issues["sub_category"].append(
                    f"{label}: {r['category']}/{r['sub_category']!r}（不在品类白名单）")
        if r["category"] and r["category"] not in CAT_MAP:
            issues["category"].append(f"{label}: {r['category']!r}（不在品类白名单）")

    total = sum(len(v) for v in issues.values())
    print(f"审计对象：{len(rows)} 件衣物（全部账号）")
    print(f"发现问题：{total} 条\n")
    for field, items in issues.items():
        print(f"== {field}（{len(items)} 条）==")
        for line in items[:30]:
            print("   ", line)
        if len(items) > 30:
            print(f"    …… 其余 {len(items) - 30} 条略")
    print(f"\n结论：{'✅ 存量数据全部合规，无需迁移' if total == 0 else '⚠️ 存在口径脏值，建议执行迁移（待确认）'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
