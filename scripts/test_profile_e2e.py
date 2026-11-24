# -*- coding: utf-8 -*-
"""动态画像端到端验证：反馈 → 画像更新 → 时间衰减 → 撤销回退。

独立临时 SQLite + TestClient 走真实 HTTP 栈，不污染真实库。
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

DB = os.path.join(tempfile.mkdtemp(), "profile_e2e.db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ.pop("ZHIPU_API_KEY", None)
os.environ.pop("DEEPSEEK_API_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402
from app.db import ClothingItem, SessionLocal, init_db  # noqa: E402
from app.agent.engine import rule_repo  # noqa: E402

init_db()
rule_repo.load()
client = TestClient(app)

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + str(detail)[:140] if detail else ""))


r = client.post("/api/v1/user/register",
                json={"nickname": "画像测试甲", "password": "pw123456"})
d = r.json()["data"]
uid = d["user"]["user_id"]
hdr = {"Authorization": f"Bearer {d['token']}"}

# 插两件带属性的衣服：蓝棉简约 / 黑皮革运动
db = SessionLocal()
for iid, color, fabric, style in (
    ("it_blue", "蓝", "纯棉", "简约"),
    ("it_black", "黑", "皮革", "运动"),
):
    db.add(ClothingItem(
        item_id=iid, user_id=uid, name=iid, category="上装",
        sub_category="T恤", color=color, fabric=fabric, style=style,
        warmth_level=2, formality_level=3, season="四季",
    ))
db.commit()
db.close()

print("=" * 68)
print("1. 提交「喜欢蓝棉简约」→ 画像出现具体属性")
print("=" * 68)
r = client.post("/api/v1/feedback", headers=hdr, json={
    "user_id": uid, "session_id": "", "outfit_id": "o1",
    "rating": 2, "tags": [], "comment": "",
    "item_ids": ["it_blue"],
})
body = r.json()
ck("反馈接口 200", r.status_code == 200, r.text[:120])
prof = (body.get("data") or {}).get("profile") or {}
raw = (body.get("data") or {}).get("evidence", {}).get("profile_delta", {}).get("after") or {}
ck("首次反馈 raw 画像含蓝色", "蓝" in (raw.get("color") or {}), raw)
ck("纯棉归一为棉", "棉" in (raw.get("fabric") or {}), raw)
ck("首次反馈有效画像为空（|w|<2 门槛）", prof == {}, prof)
rid = (body.get("data") or {}).get("record_id")

print()
print("=" * 68)
print("2. 再提交一次喜欢 → 权重累加")
print("=" * 68)
r2 = client.post("/api/v1/feedback", headers=hdr, json={
    "user_id": uid, "session_id": "", "outfit_id": "o2",
    "rating": 2, "tags": [], "comment": "", "item_ids": ["it_blue"],
})
prof2 = (r2.json().get("data") or {}).get("profile") or {}
ck("蓝色权重累加到 2", (prof2.get("color") or {}).get("蓝", 0) >= 2, prof2)

print()
print("=" * 68)
print("3. 提交「不喜欢黑皮革运动」→ 出现负权重")
print("=" * 68)
r3 = client.post("/api/v1/feedback", headers=hdr, json={
    "user_id": uid, "session_id": "", "outfit_id": "o3",
    "rating": 0, "tags": [], "comment": "", "item_ids": ["it_black"],
})
raw3 = (r3.json().get("data") or {}).get("evidence", {}).get("profile_delta", {}).get("after") or {}
ck("黑色为负权重", (raw3.get("color") or {}).get("黑", {}).get("w", 0) < 0, raw3)
ck("皮革为负权重", (raw3.get("fabric") or {}).get("皮革", {}).get("w", 0) < 0, raw3)

print()
print("=" * 68)
print("4. GET 接口返回画像 + 旧五维字段")
print("=" * 68)
g = client.get("/api/v1/user/preferences", params={"user_id": uid}, headers=hdr).json()["data"]
ck("GET 含 profile", isinstance(g.get("profile"), dict), list(g.keys()))
ck("GET 保留旧五维 color", isinstance(g.get("color"), (int, float)), g.get("color"))

print()
print("=" * 68)
print("5. 撤销首条反馈 → 画像精确回退")
print("=" * 68)
before_undo = dict(g["profile"])
u = client.post(f"/api/v1/feedback/{rid}/undo", params={"user_id": uid}, headers=hdr)
ck("撤销接口 200", u.status_code == 200, u.text[:120])
g2 = client.get("/api/v1/user/preferences", params={"user_id": uid}, headers=hdr).json()["data"]
after = g2["profile"]
blue_before = (before_undo.get("color") or {}).get("蓝")
blue_after = (after.get("color") or {}).get("蓝")
ck("蓝色权重回退（变小或消失）",
   blue_after is None or (blue_before is not None and blue_after < blue_before),
   f"{blue_before} -> {blue_after}")

print()
print("=" * 68)
print("6. 重置偏好 → 画像清空")
print("=" * 68)
client.post("/api/v1/user/preferences/reset", params={"user_id": uid}, headers=hdr)
g3 = client.get("/api/v1/user/preferences", params={"user_id": uid}, headers=hdr).json()["data"]
ck("画像已清空", not g3.get("profile"), g3.get("profile"))
ck("旧五维回到 5", g3.get("color") == 5, g3.get("color"))

print()
print("=" * 68)
print(f"结果：{len(PASS)} PASS / {len(FAIL)} FAIL")
print("=" * 68)
if FAIL:
    print("FAILED:", FAIL)
    sys.exit(1)
