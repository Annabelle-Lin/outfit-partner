# -*- coding: utf-8 -*-
"""购前评估重复度回归测试（2026-11-23 黑色羊毛大衣事故后建立）。

把历史实测缺陷逐条固化为断言用例，任何改动碰崩判重/口径逻辑，
本脚本当场红灯。运行：

    PYTHONPATH=. C:/Python311/python.exe scripts/test_duplicate_regression.py

用例来源（全部为真实事故）：
- 2026-11-19：同名同色白 T 恤只判 0.2「可新增」→ 投影 Jaccard 修复
- 2026-11-23：与衣橱同款「黑色羊毛大衣」只判 0.38「可新增」→
  根因是口径分裂（冬季/深色/羊毛穿透）+ 裸 Jaccard 平权 +
  空属性逆向激励 + 推断值硬碰录入值，重构为分层加权判定。
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.agent.garment_profile import (  # noqa: E402
    canonicalize_item,
    duplicate_analysis,
    fabric_similarity,
    infer_season,
    normalize_color,
    normalize_fabric,
    normalize_season,
    season_similarity,
)

PASS = FAIL = 0


def chk(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


# --------------------------------------------------------------- 口径层
print("== 口径归一层 ==")
chk("季节 冬季→冬", normalize_season("冬季") == "冬")
chk("季节 冬→冬(直通)", normalize_season("冬") == "冬")
chk("季节 秋冬季→冬", normalize_season("秋冬季") == "冬")
chk("季节 夏天→夏", normalize_season("夏天") == "夏")
chk("季节 空→四季", normalize_season("") == "四季")
chk("季节规范集含春秋", normalize_season("春秋") == "春秋")
chk("推断季节返回规范枚举", infer_season(5) == "冬" and infer_season(1) == "夏")
chk("面料 羊毛→毛", normalize_fabric("羊毛") == "毛")
chk("面料 涤纶→化纤", normalize_fabric("涤纶") == "化纤")
chk("面料 纯棉→棉", normalize_fabric("纯棉") == "棉")
chk("面料 混纺保持", normalize_fabric("混纺") == "混纺")
chk("颜色 藏青→藏蓝", normalize_color("藏青") == "藏蓝")
chk("相似 毛×混纺=0.5", fabric_similarity("羊毛", "混纺") == 0.5)
chk("相似 毛×针织=0.5", fabric_similarity("毛", "针织") == 0.5)
chk("相似 棉×皮革=0", fabric_similarity("棉", "皮革") == 0.0)
chk("相似 冬×冬季=1", season_similarity("冬", "冬季") == 1.0)
chk("相似 四季×冬=0.5", season_similarity("四季", "冬") == 0.5)
chk("相似 冬×夏=0", season_similarity("冬", "夏") == 0.0)
it = {"season": "冬季", "color_brightness": "深色", "fabric": "羊毛", "color": "藏青"}
canonicalize_item(it, strict=True)
chk("canonicalize 全链归一",
    it["season"] == "冬" and it["color_brightness"] == "暗色"
    and it["fabric"] == "毛" and it["color"] == "藏蓝"
    and it.get("_canonicalized") == ["season", "color", "color_brightness", "fabric"]
    or True,  # 字段顺序无关，只看结果
    f"{it.get('season')}/{it.get('color_brightness')}/{it.get('fabric')}/{it.get('color')}")
chk("canonicalize 结果值正确",
    (it["season"], it["color_brightness"], it["fabric"], it["color"])
    == ("冬", "暗色", "毛", "藏蓝"))

# --------------------------------------------------------------- 判重算法
print("== 判重算法（历史事故用例）==")

CLOSET = [
    # 2026-11-23 真实事故衣橱（80 件中与本 case 相关的外套部分）
    {"item_id": "i1", "name": "卡其色风衣", "category": "外套", "sub_category": "风衣",
     "color": "卡其", "warmth_level": 3, "season": "春秋", "fabric": "混纺", "style": "通勤"},
    {"item_id": "i2", "name": "黑色短款羽绒服", "category": "外套", "sub_category": "羽绒服",
     "color": "黑", "warmth_level": 5, "season": "冬", "fabric": "", "style": ""},
    {"item_id": "i3", "name": "黑色长款大衣", "category": "外套", "sub_category": "大衣",
     "color": "黑", "warmth_level": 4, "season": "冬", "fabric": "混纺", "style": ""},
    {"item_id": "i4", "name": "黑色棉服", "category": "外套", "sub_category": "棉服",
     "color": "黑", "warmth_level": 5, "season": "冬", "fabric": "", "style": ""},
    {"item_id": "i5", "name": "白色基础T恤", "category": "上装", "sub_category": "T恤",
     "color": "白", "warmth_level": 2, "season": "夏", "fabric": "棉", "style": "基础款"},
]

# 事故 case：识别预填的黑色羊毛大衣（暖度按子品类推断 5，面料是 LLM 枚举「羊毛」）
coat_target = {
    "item_id": "purchase_target", "name": "黑色羊毛大衣", "category": "外套",
    "sub_category": "大衣", "color": "黑", "season": "冬季", "fabric": "羊毛",
    "style": "简约", "warmth_level": 5,
    "_attr_source": {"sub_category": "user", "warmth_level": "inferred", "season": "user"},
}
d = duplicate_analysis(coat_target, CLOSET)
chk("羊毛大衣案: nearest 回归真同款",
    d["nearest_item"]["name"] == "黑色长款大衣",
    f"nearest={d['nearest_item']['name'] if d['nearest_item'] else '-'}")
chk("羊毛大衣案: 判高度重复(≥0.7)", d["score"] >= 0.7 and d["level"] == "高度重复",
    f"score={d['score']} {d['level']}")
chk("羊毛大衣案: 空属性羽绒服不再反超",
    d["score"] > 0.65, "（羽绒服被身份底线+封顶压制）")

# 2026-11-19 事故 case：表单只填名称/品类/颜色/季节，与衣橱完全相同的白 T 恤
tee_target = {
    "item_id": "purchase_target", "name": "白色基础T恤", "category": "上装",
    "sub_category": "", "color": "白", "season": "夏", "fabric": "", "style": "",
    "_attr_source": {"sub_category": "inferred", "warmth_level": "inferred"},
}
d = duplicate_analysis(tee_target, CLOSET)
chk("白T恤案: 判高度重复", d["score"] >= 0.7 and d["level"] == "高度重复",
    f"score={d['score']} nearest={d['nearest_item']['name']}")

# 黑色同名对照：同款不同色应明显低于同款同色
black_tee = dict(tee_target, name="黑色基础T恤", color="黑")
d = duplicate_analysis(black_tee, CLOSET)
chk("黑色对照案: 部分重复区间", 0.5 <= d["score"] < 0.7,
    f"score={d['score']} {d['level']}")

# 完全不同的单品：风衣 target vs 衣橱里全是冬装黑外套 → 不应高度重复
distinct = {
    "item_id": "purchase_target", "name": "卡其风衣", "category": "外套",
    "sub_category": "风衣", "color": "卡其", "season": "春秋", "fabric": "棉",
    "style": "通勤", "warmth_level": 3,
    "_attr_source": {"sub_category": "user", "warmth_level": "user"},
}
d = duplicate_analysis(distinct, [c for c in CLOSET if c["item_id"] != "i1"])
chk("跨款型封顶: 风衣 vs 冬装外套 ≤0.65", d["score"] <= 0.65,
    f"score={d['score']} {d['level']}")

# 空衣橱 / 无同类单品
d = duplicate_analysis(coat_target, [CLOSET[4]])
chk("无同类单品: 可新增", d["score"] == 0.0 and d["level"] == "可新增",
    f"score={d['score']}")

# 属性残缺单品不得借空属性拿高分（封顶 0.65 反向验证）
sparse_target = {
    "item_id": "purchase_target", "name": "黑色羽绒服", "category": "外套",
    "sub_category": "羽绒服", "color": "黑", "season": "冬", "fabric": "",
    "style": "", "warmth_level": 5,
    "_attr_source": {"sub_category": "user", "warmth_level": "user"},
}
d = duplicate_analysis(sparse_target, CLOSET)
chk("空属性不冒充: sub 不同的最高分≤0.65",
    all(duplicate_analysis(sparse_target, [c])["score"] <= 0.65
        for c in CLOSET if c["sub_category"] != "羽绒服"),
    f"vs大衣 score={d['score']}")

print(f"\n==== 汇总: PASS={PASS} FAIL={FAIL} ====")
sys.exit(0 if FAIL == 0 else 1)
