# -*- coding: utf-8 -*-
"""动态用户画像专项验证（2026-11-24）。

验证点：
1. 属性抽取复用 garment_profile 归一口径，缺失不臆造
2. 权重可增可减，裁剪到 -5~+5，w=0 时删除属性
3. 时间衰减 w×0.5^(age/30)，30 天整 w4→2、w2→1
4. 阈值只在读取时过滤（写入 w=1 不丢，累积到 2 才生效）
5. S003 逐值匹配：命中喜欢值加分、反感值扣分、封顶 ±3
6. 无画像时回落旧五维标量口径，兼容老调用方
"""
import os
import sys
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("ZHIPU_API_KEY", None)

from app import utils  # noqa: E402
from app.agent.engine import (  # noqa: E402
    RecommendContext,
    RecommendEngine,
    rule_repo,
)

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + str(detail)[:120] if detail else ""))


print("=" * 68)
print("1. 属性抽取：归一 + 缺失不臆造")
print("=" * 68)
items = [
    {"category": "上装", "color": "藏青", "fabric": "纯棉", "style": "简约", "fit_snug": "修身"},
    {"category": "下装", "color": "黑", "fabric": "混纺", "style": "", "fit_snug": ""},
]
attrs = utils.profile_attrs(items)
ck("纯棉归一为棉", "fabric:棉" in attrs, sorted(attrs))
ck("藏青归入颜色属性", any(a.startswith("color:") for a in attrs))
ck("空 style 不臆造", not any(a.startswith("style:") and a.endswith(":") for a in attrs))
ck("非画像维度不入（warmth 等）", not any(a.startswith("warmth") for a in attrs))
ck("无单品 → 空集合", len(utils.profile_attrs([])) == 0)

print()
print("=" * 68)
print("2. 权重可增可减 + 裁剪 + 归零删除")
print("=" * 68)
p = utils.apply_profile_delta({}, {"color:蓝"}, 1)
ck("首次喜欢蓝 → w=1", p["color"]["蓝"]["w"] == 1, p)
p = utils.apply_profile_delta(p, {"color:蓝"}, 1)
p = utils.apply_profile_delta(p, {"color:蓝"}, 1)
p = utils.apply_profile_delta(p, {"color:蓝"}, 1)
p = utils.apply_profile_delta(p, {"color:蓝"}, 1)
ck("累积 +5 封顶", p["color"]["蓝"]["w"] == 5, p)
p = utils.apply_profile_delta(p, {"color:蓝"}, -1)
ck("不喜欢 → 减到 4", p["color"]["蓝"]["w"] == 4, p)
p = utils.apply_profile_delta(p, {"color:蓝"}, -1)
p = utils.apply_profile_delta(p, {"color:蓝"}, -1)
p = utils.apply_profile_delta(p, {"color:蓝"}, -1)
p = utils.apply_profile_delta(p, {"color:蓝"}, -1)
ck("减到 0 → 属性删除", "蓝" not in p.get("color", {}), p)
p2 = utils.apply_profile_delta({}, {"color:黑"}, -1)
for _ in range(4):
    p2 = utils.apply_profile_delta(p2, {"color:黑"}, -1)
ck("累积 -5 封底", p2["color"]["黑"]["w"] == -5, p2)

print()
print("=" * 68)
print("3. 时间衰减 + 读取阈值")
print("=" * 68)
now = datetime.utcnow()
old30 = (now - timedelta(days=30)).isoformat(timespec="seconds")
ck("30天前 w4 → 2.0", abs(utils.decay_weight(4, old30, now) - 2.0) < 1e-6)
ck("30天前 w2 → 1.0", abs(utils.decay_weight(2, old30, now) - 1.0) < 1e-6)
ck("今天 w3 → 3.0", abs(utils.decay_weight(3, now.isoformat(), now) - 3.0) < 1e-6)
ck("无 last → 不衰减", utils.decay_weight(3, None, now) == 3.0)
ck("坏 last → 不衰减", utils.decay_weight(3, "bad-date", now) == 3.0)
eff = utils.effective_profile({"color": {"蓝": {"w": 4, "last": old30}}}, now)
ck("w4 衰减后达标 → 保留", eff.get("color", {}).get("蓝") == 2.0, eff)
eff2 = utils.effective_profile({"color": {"蓝": {"w": 2, "last": old30}}}, now)
ck("w2 衰减后不达标 → 淘汰", "蓝" not in eff2.get("color", {}), eff2)

p3 = utils.apply_profile_delta({}, {"color:蓝"}, 1)
ck("写入 w1 不被阈值吃掉", p3["color"]["蓝"]["w"] == 1, p3)
ck("读取 w1 不达标", utils.effective_profile(p3) == {}, utils.effective_profile(p3))
p3 = utils.apply_profile_delta(p3, {"color:蓝"}, 1)
ck("累积 w2 后读取生效", utils.effective_profile(p3).get("color", {}).get("蓝") == 2.0)

print()
print("=" * 68)
print("4. 每维 top5 限制")
print("=" * 68)
many = {f"color:色{i}" for i in range(9)}
pl = utils.apply_profile_delta({}, many, 1)
ck("9 个颜色属性只留 5 个", len(pl["color"]) == 5, len(pl["color"]))

print()
print("=" * 68)
print("5. S003 真·逐值匹配")
print("=" * 68)
rule_repo.load()
eng = RecommendEngine(rule_repo)


def it(cat, **kw):
    base = {
        "item_id": "x", "category": cat, "name": "", "color": "",
        "fabric": "", "style": "", "fit_snug": "", "fit_silhouette": "",
        "warmth_level": 2, "sub_category": "", "season": "四季",
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


blue = outfit([it("上装", item_id="a", color="蓝", style="简约"),
               it("下装", item_id="b", color="黑", fabric="棉")])
black = outfit([it("上装", item_id="c", color="黑", style="运动"),
                it("下装", item_id="d", color="黑", fabric="棉")])
profile = {"color": {"蓝": 3.0, "黑": -2.0}, "style": {"简约": 2.5}}

ctx_new = RecommendContext(user_id="u", occasion="日常", weather_temp=22.0, user_profile=profile)
_, d_blue = eng._score(blue, ctx_new)
_, d_black = eng._score(black, ctx_new)
s003_blue = next((d["delta"] for d in d_blue if d["rule_id"] == "S003"), 0)
s003_black = next((d["delta"] for d in d_black if d["rule_id"] == "S003"), 0)
ck("蓝色搭配 S003 > 0", s003_blue > 0, s003_blue)
ck("黑色搭配 S003 < 0", s003_black < 0, s003_black)
ck("蓝搭配分 > 黑搭配分", s003_blue > s003_black, f"{s003_blue} vs {s003_black}")

many_profile = {"color": {f"色{i}": 5.0 for i in range(10)}}
ctx_many = RecommendContext(user_id="u", occasion="日常", user_profile=many_profile)
_, d_many = eng._score(blue, ctx_many)
s003_many = next((d["delta"] for d in d_many if d["rule_id"] == "S003"), 0)
ck("S003 封顶 ±3", s003_many <= 3.0, s003_many)

ctx_old = RecommendContext(
    user_id="u", occasion="日常",
    long_term_prefs={"color": 8, "style": 5, "comfort": 5},
)
_, d_old = eng._score(blue, ctx_old)
s003_old = next((d["delta"] for d in d_old if d["rule_id"] == "S003"), 0)
ck("无画像回落旧标量口径", abs(s003_old - 1.5) < 1e-6, s003_old)

print()
print("=" * 68)
print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
print("=" * 68)
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
