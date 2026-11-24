# -*- coding: utf-8 -*-
"""购前评估端到端复验（service 层：真实 SQLite + 真实推理引擎 + 真实 purchase.evaluate）。

跳过 HTTP 鉴权中间件（与本次修复无关，且不便在离线脚本里注入登录态），
其余链路与线上完全一致：建表 → 灌受控衣橱 → 构造 RecommendContext →
枚举/硬过滤/打分 → 判重 → 购买建议 → 落库。

运行：
    PYTHONPATH=. C:/Python311/python.exe scripts/test_purchase_e2e.py

覆盖场景（2026-11-22/23 事故 + 最后一处未复验红灯）：
  A 真同款（用户最初诉求）：与衣橱一模一样的大衣必须「高度重复/不建议购买」
  B 同名同色近似款、白 T 恤同款不得判「可新增」
  C 完全不同单品不得被误判为重复
  D 极端值温和归一、无效城市降级提示、历史同签名去重
  E【最后补的修复】28℃ 全冬装衣橱评估短裤：0 套时 wardrobe_gap 必须非空
  F 空衣橱首评文案兜底
"""
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(os.path.expanduser("~"), ".wardrobe", "e2e_final_verify.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)
os.environ["DATABASE_URL"] = "sqlite:///" + DB_PATH.replace("\\", "/")
sys.path.insert(0, ROOT)

from app import config, schemas  # noqa: E402
assert config.DATABASE_URL.endswith("e2e_final_verify.db"), config.DATABASE_URL

from app.agent.engine import RecommendEngine, rule_repo  # noqa: E402
from app.api import purchase  # noqa: E402
from app.db import ClothingItem, SessionLocal, User, init_db  # noqa: E402

rule_repo.load()
init_db()
ENGINE = RecommendEngine(rule_repo)
FAKE_REQ = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(engine=ENGINE)))
PASS = FAIL = 0


def chk(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}  {detail}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def add_user(uid):
    db = SessionLocal()
    db.add(User(user_id=uid, nickname="复验账号"))
    db.commit()
    db.close()


def add_clothes(uid, rows):
    db = SessionLocal()
    for i, r in enumerate(rows, 1):
        db.add(ClothingItem(
            item_id=f"{uid}_i{i:02d}", user_id=uid,
            name=r["name"], category=r["category"], sub_category=r["sub"],
            color=r.get("color", ""), color_brightness=r.get("br", ""),
            season=r.get("season", "四季"), fabric=r.get("fabric", ""),
            style=r.get("style", ""), occasion=r.get("occasion", ["日常"]),
            warmth_level=r.get("warmth", 3), formality_level=r.get("formality", 3),
        ))
    db.commit()
    db.close()


def evaluate(uid, item, full=False, **kw):
    req = schemas.PurchaseEvaluateRequest(
        user_id=uid, target_item=schemas.TargetItem(**item), **kw
    )
    db = SessionLocal()
    try:
        data = purchase.evaluate(req, FAKE_REQ, db)["data"]
        return data if full else data["result"]
    finally:
        db.close()


# ---------------------------------------------------------------- 衣橱 A
# 含「黑色长款大衣」与「白色基础T恤」，用于真同款 / 白T / 差异款对照
add_user("u_dup")
add_clothes("u_dup", [
    {"name": "黑色长款大衣", "category": "外套", "sub": "大衣", "color": "黑",
     "br": "暗色", "season": "冬", "fabric": "混纺", "warmth": 4},
    {"name": "米白高领毛衣", "category": "上衣", "sub": "毛衣", "color": "米",
     "br": "亮色", "season": "冬", "fabric": "针织", "warmth": 5},
    {"name": "深蓝直筒牛仔裤", "category": "下装", "sub": "牛仔裤", "color": "蓝",
     "br": "暗色", "season": "四季", "fabric": "牛仔", "warmth": 3},
    {"name": "棕色短靴", "category": "鞋", "sub": "靴子", "color": "棕",
     "br": "暗色", "season": "冬", "fabric": "皮革", "warmth": 4},
    {"name": "白色基础T恤", "category": "上衣", "sub": "T恤", "color": "白",
     "br": "亮色", "season": "夏", "fabric": "棉", "warmth": 2},
])

print("== A 真同款事故 case（用户最初诉求）==")
r = evaluate("u_dup", {
    "name": "黑色羊毛大衣", "category": "外套", "sub_category": "大衣",
    "color": "黑", "season": "冬季", "fabric": "羊毛", "warmth_level": 5,
}, season="冬", temperature=5)
chk("A1 真同款→高度重复", r["duplicate_score"] >= 0.7 and r["duplicate_level"] == "高度重复",
    f"score={r['duplicate_score']} level={r['duplicate_level']}")
chk("A1 nearest 命中真同款", (r["nearest_item"] or {}).get("name") == "黑色长款大衣",
    f"nearest={(r['nearest_item'] or {}).get('name')}")
chk("A1 结论=不建议购买", r["suggestion_level"] == "不建议购买",
    f"level={r['suggestion_level']} | {r['suggestion'][:40]}")

r2 = evaluate("u_dup", {
    "name": "黑色长款大衣", "category": "外套", "sub_category": "大衣",
    "color": "黑", "season": "冬", "fabric": "混纺", "warmth_level": 4,
}, season="冬", temperature=5)
chk("A2 完全同名同色→高度重复/不建议",
    r2["duplicate_score"] >= 0.7 and r2["suggestion_level"] == "不建议购买",
    f"score={r2['duplicate_score']} level={r2['suggestion_level']}")

r3 = evaluate("u_dup", {
    "name": "白色基础T恤", "category": "上衣", "sub_category": "T恤",
    "color": "白", "season": "夏", "fabric": "棉", "warmth_level": 2,
}, season="夏", temperature=28)
chk("A3 白T恤同款→高度重复/不建议",
    r3["duplicate_score"] >= 0.7 and r3["suggestion_level"] == "不建议购买",
    f"score={r3['duplicate_score']} level={r3['suggestion_level']}")

r4 = evaluate("u_dup", {
    "name": "卡其色风衣", "category": "外套", "sub_category": "风衣",
    "color": "卡其", "season": "春秋", "fabric": "棉", "warmth_level": 3,
}, season="春秋", temperature=18)
chk("A4 差异款不误判重复", r4["duplicate_score"] < 0.5 and r4["duplicate_level"] == "可新增",
    f"score={r4['duplicate_score']} level={r4['duplicate_level']}")

print("== D 稳健性：极端值 / 无效城市 / 历史去重 ==")
try:
    d5 = evaluate("u_dup", {
        "name": "极端值归一测试衫", "category": "上衣", "sub_category": "针织衫",
        "color": "白", "warmth_level": 99, "formality_level": -5,
    }, season="夏", temperature=50, full=True)
    r5, t5 = d5["result"], d5["target_item"]
    chk("D1 极端值温和归一且不 422：50℃回落 / 暖度·正式度收敛到 1-5",
        r5["temperature"] != 50 and not r5["temperature_source"].startswith("user")
        and 1 <= t5["warmth_level"] <= 5 and 1 <= t5["formality_level"] <= 5,
        f"temp={r5['temperature']} src={r5['temperature_source']} "
        f"warmth={t5['warmth_level']} formality={t5['formality_level']}")
except Exception as e:
    chk("D1 极端值温和归一且不 422", False, repr(e))

r6 = evaluate("u_dup", {
    "name": "无效城市测试衫", "category": "上衣", "sub_category": "衬衫",
    "color": "蓝", "season": "四季", "fabric": "棉", "warmth_level": 3,
}, season="春秋", city="火星城")
chk("D2 无效城市明确标注降级",
    bool(r6["temperature_note"]) and not r6["temperature_source"].startswith("weather:"),
    f"src={r6['temperature_source']} note={r6['temperature_note'][:30]}")

for _ in range(3):
    r7 = evaluate("u_dup", {
        "name": "去重测试专用针织衫", "category": "上衣", "sub_category": "针织衫",
        "color": "灰", "season": "春秋", "fabric": "针织", "warmth_level": 3,
    }, season="春秋", temperature=18)
chk("D3 同签名连评 3 次→eval_count=3", r7.get("eval_count") == 3,
    f"eval_count={r7.get('eval_count')}")

# ---------------------------------------------------------------- 衣橱 B
# 纯冬装、无任何夏季可穿单品：短裤在 28℃ 下应 0 套且给出季节品类缺口
add_user("u_gap")
add_clothes("u_gap", [
    {"name": "米白高领毛衣", "category": "上衣", "sub": "毛衣", "color": "米",
     "season": "冬", "fabric": "针织", "warmth": 5},
    {"name": "黑色加绒牛仔裤", "category": "下装", "sub": "牛仔裤", "color": "黑",
     "season": "冬", "fabric": "牛仔", "warmth": 4},
    {"name": "黑色长款大衣", "category": "外套", "sub": "大衣", "color": "黑",
     "season": "冬", "fabric": "混纺", "warmth": 4},
    {"name": "棕色短靴", "category": "鞋", "sub": "靴子", "color": "棕",
     "season": "冬", "fabric": "皮革", "warmth": 4},
])

print("== E【最后一处未复验红灯】28℃ 全冬装衣橱评估短裤 ==")
rg = evaluate("u_gap", {
    "name": "白色休闲短裤", "category": "下装", "sub_category": "短裤",
    "color": "白", "season": "夏", "fabric": "棉", "warmth_level": 1,
}, season="夏", temperature=28)
chk("E1 全冬装衣橱→0 套", rg["matchable_count"] == 0,
    f"matchable_count={rg['matchable_count']}")
chk("E2 wardrobe_gap 非空且指向应季缺口",
    len(rg["wardrobe_gap"]) > 0 and any("应季" in g or "夏季" in g for g in rg["wardrobe_gap"]),
    f"gap={rg['wardrobe_gap']}")
chk("E3 0 套但有缺口→降级为谨慎购买", rg["suggestion_level"] == "谨慎购买",
    f"level={rg['suggestion_level']} | {rg['suggestion'][:50]}")

# ---------------------------------------------------------------- 衣橱 C
add_user("u_empty")
print("== F 空衣橱首评兜底 ==")
re = evaluate("u_empty", {
    "name": "浅蓝条纹衬衫", "category": "上衣", "sub_category": "衬衫",
    "color": "蓝", "season": "四季", "fabric": "棉", "warmth_level": 3,
}, season="春秋", temperature=18)
chk("F1 空衣橱→专门文案 + 谨慎购买",
    "还没有衣物" in re["suggestion"] and re["suggestion_level"] == "谨慎购买",
    f"level={re['suggestion_level']} | {re['suggestion'][:40]}")

print(f"\n==== 汇总: PASS={PASS} FAIL={FAIL} ====")
sys.exit(0 if FAIL == 0 else 1)
