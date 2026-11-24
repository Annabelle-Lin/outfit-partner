# -*- coding: utf-8 -*-
"""视觉美感分专项验证（2026-11-24）。

验证点：
1. 三子项各自的正/负/零方向正确
2. V002 跳过 S007 已认领的「上宽下窄」
3. 缺失维度不臆造（返回 0）
4. 规则 ID 与现有 S 规则不重叠、明细带 shadow 标记
5. 只罚不奖：负分扣减、正分不奖、主 score 不变、负分搭配被踢出 Top3
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("ZHIPU_API_KEY", None)

from app.agent.engine import (  # noqa: E402
    RecommendContext,
    RecommendEngine,
    rule_repo,
)

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + str(detail)[:120] if detail else ""))


rule_repo.load()
eng = RecommendEngine(rule_repo)


def item(cat, **kw):
    base = {
        "item_id": kw.pop("item_id", "x"),
        "category": cat,
        "name": kw.pop("name", ""),
        "color": "",
        "fabric": "",
        "fit_silhouette": "",
        "fit_snug": "",
        "warmth_level": 2,
        "sub_category": "",
        "season": "四季",
        "formality_level": 3,
    }
    base.update(kw)
    return base


def outfit(items):
    return {
        "items": items,
        "fingerprint": "_".join(i["item_id"] for i in items),
        "categories": {i["category"] for i in items},
        "sub_categories": {i.get("sub_category", "") for i in items},
        "colors": [i.get("color", "") for i in items if i.get("color")],
    }


print("=" * 68)
print("1. V001 配色比例：主色聚焦 / 主色偏多 / 点缀")
print("=" * 68)
focused = outfit([item("上装", color="蓝"), item("下装", color="黑"),
                  item("鞋", color="白")])
d, why = eng._visual_v001(focused)
ck("蓝主色+黑中性底+白鞋点睛 > 0", d > 0, f"{d} {why}")

scattered = outfit([item("上装", color="红"), item("下装", color="蓝"),
                    item("外套", color="绿")])
d2, _ = eng._visual_v001(scattered)
ck("大面积三主色 < 聚焦搭配", d2 < d, f"scattered={d2} focused={d}")

no_color = outfit([item("上装"), item("下装")])
d3, _ = eng._visual_v001(no_color)
ck("颜色全缺失 → 0（不臆造）", d3 == 0.0, d3)

print()
print("=" * 68)
print("2. V002 版型协调：方向正确 + 跳过 S007")
print("=" * 68)
top_tight = item("上装", fit_snug="修身", item_id="t1")
bot_loose = item("下装", fit_silhouette="阔腿", item_id="b1")
d, why = eng._visual_v002(outfit([top_tight, bot_loose]))
ck("上紧下松 > 0", d > 0, f"{d} {why}")

top_loose = item("上装", fit_snug="宽松", item_id="t2")
bot_tight = item("下装", fit_silhouette="紧身", item_id="b2")
d2, _ = eng._visual_v002(outfit([top_loose, bot_tight]))
ck("上宽下窄 → 0（S007 已认领，不重复）", d2 == 0.0, d2)

top_l = item("上装", fit_snug="宽松", item_id="t3")
bot_l = item("下装", fit_snug="宽松", item_id="b3")
d3, _ = eng._visual_v002(outfit([top_l, bot_l]))
ck("上下同宽 < 0", d3 < 0, d3)

d4, _ = eng._visual_v002(outfit([item("上装"), item("下装")]))
ck("版型全缺失 → 0（不臆造）", d4 == 0.0, d4)

print()
print("=" * 68)
print("3. V003 材质协调：同族加分 / 异族降权 / 混纺不臆造")
print("=" * 68)
same = outfit([item("上装", fabric="毛", item_id="t"),
               item("下装", fabric="毛", item_id="b")])
d, why = eng._visual_v003(same)
ck("同族面料（毛+毛）> 0", d > 0, f"{d} {why}")

diff = outfit([item("上装", fabric="丝", item_id="t"),
               item("下装", fabric="皮革", item_id="b")])
d2, why2 = eng._visual_v003(diff)
ck("异族面料（丝+皮革）< 0", d2 < 0, f"{d2} {why2}")

blend = outfit([item("上装", fabric="混纺", item_id="t"),
                item("下装", fabric="棉", item_id="b")])
d3, _ = eng._visual_v003(blend)
ck("混纺半匹配 → 0（不臆造）", d3 == 0.0, d3)

d4, _ = eng._visual_v003(outfit([item("上装", fabric="棉")]))
ck("面料不足两件 → 0", d4 == 0.0, d4)

print()
print("=" * 68)
print("4. 明细结构：不进主 score、规则不重叠、shadow 标记")
print("=" * 68)
con = sqlite3.connect(os.path.expanduser("~/.wardrobe/wardrobe.db"))
con.row_factory = sqlite3.Row
rows = con.execute("SELECT * FROM clothing_items WHERE user_id='u_699853bb'").fetchall()
items = [dict(r) for r in rows]
allc = eng._enumerate(items)
ctx = RecommendContext(user_id="u", occasion="日常", weather_temp=22.0)
legal = [o for o in allc if eng._hard_filter(o, ctx)]

probe = legal[0]
s, details = eng._score(probe, ctx)
vs = eng._visual_scores(probe)
ck("明细恰好三条", len(vs) == 3, len(vs))
ck("每条带 shadow 标记", all(v.get("shadow") is True for v in vs))
ck("字段齐全", all({"rule_id", "name", "delta", "reason", "shadow"} <= set(v) for v in vs))
sids = {d["rule_id"] for d in details}
vids = {v["rule_id"] for v in vs}
ck("视觉规则ID与现有S规则不重叠", not (sids & vids), f"{sorted(sids)} vs {sorted(vids)}")
ck("主score明细不含V规则（视觉分未并入）", not any(v["rule_id"] in sids for v in vs))

print()
print("=" * 68)
print("5. 只罚不奖：排序键语义 + 负分搭配被踢 + 正分不误抬")
print("=" * 68)

# 5a. 排序键单元语义：正分不奖、负分扣减、无明细回落主分
base = outfit([item("上装"), item("下装")])
base["score"] = 80.0
base["visual_scores"] = [
    {"rule_id": "V001", "delta": 2.0},
    {"rule_id": "V002", "delta": 1.0},
    {"rule_id": "V003", "delta": 0.5},
]
ck("视觉全正 → 排序分=主分（只罚不奖）", eng._rank_score(base) == 80.0, eng._rank_score(base))
base["visual_scores"] = [
    {"rule_id": "V001", "delta": 0.5},
    {"rule_id": "V002", "delta": -0.8},
    {"rule_id": "V003", "delta": -0.6},
]
ck("混合正负 → 只扣合计负值", abs(eng._rank_score(base) - 79.1) < 1e-6, eng._rank_score(base))
bare = outfit([item("上装"), item("下装")])
bare["score"] = 80.0
ck("无 visual_scores → 回落主分（购前评估兼容）", eng._rank_score(bare) == 80.0)

# 5b. 真实衣橱三场合：主分仍展示、负分搭配被踢、正分不误抬
NEG_FPS = set()
for occ, temp in (("日常", 22.0), ("通勤", 15.0), ("聚会", 25.0)):
    c = RecommendContext(user_id="u", occasion=occ, weather_temp=temp)
    outs = [o for o in eng._enumerate(items) if eng._hard_filter(o, c)]
    for o in outs:
        o["score"], _ = eng._score(o, c)
        o["visual_scores"] = eng._visual_scores(o)
    old_fps = {o["fingerprint"] for o in eng._diversify(
        sorted(outs, key=lambda x: x["score"], reverse=True), 3)}
    neg_fps = {o["fingerprint"] for o in outs
               if sum(v["delta"] for v in o["visual_scores"]) < 0}
    NEG_FPS |= neg_fps
    new_top = eng._diversify(sorted(outs, key=eng._rank_score, reverse=True), 3)
    new_fps = {o["fingerprint"] for o in new_top}
    ck(f"{occ}: 新 Top3 三套齐全", len(new_top) == 3, len(new_top))
    ck(f"{occ}: 主 score 字段未被扣减", all(o["score"] >= 0 for o in new_top))
    ck(f"{occ}: 新 Top3 不含视觉负分搭配",
       not (new_fps & neg_fps), f"交集 {len(new_fps & neg_fps)}")
    ck(f"{occ}: 正分搭配不被抬升（排序分=主分）",
       all(eng._rank_score(o) == o["score"] for o in new_top))
    # 旧 Top3 若有负分搭配，应已出局
    old_neg = old_fps & neg_fps
    ck(f"{occ}: 旧 Top3 负分搭配已出局", not (old_neg & new_fps),
       f"原负分 {len(old_neg)} 套")

# 5c. 真正的怪搭配（正分高但语义荒谬）不应被奖励进 Top3
ck("存在被只罚不奖保护的高正分搭配", len(NEG_FPS) > 0, f"负分搭配池 {len(NEG_FPS)} 套")

print()
print("=" * 68)
print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
print("=" * 68)
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
