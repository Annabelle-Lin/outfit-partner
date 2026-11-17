"""一期改造专项验证：反馈标签 → 维度级偏好调整 → 自由文本解析。

与 smoke_test.py 同构：独立临时 SQLite + TestClient 走真实 HTTP 栈（含 AuthMiddleware）。
重点验证的不是"接口返回 200"，而是"偏好分数的变化方向对不对"。
"""
import os
import sys
import tempfile

DB = os.path.join(tempfile.mkdtemp(), "phase1.db")
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ.pop("ZHIPU_API_KEY", None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app                  # noqa: E402
from app.db import init_db                # noqa: E402
from app.agent.engine import rule_repo    # noqa: E402

init_db()
rule_repo.load()
client = TestClient(app)

PASS, FAIL = [], []


def ck(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("   " + str(detail)[:140] if detail else ""))


def mk_user(nick):
    """注册独立账号，返回 (user_id, headers)。"""
    r = client.post("/api/v1/user/register",
                    json={"nickname": nick, "password": "pw123456"})
    if r.status_code != 200:
        print("register failed:", r.status_code, r.text[:300])
        raise SystemExit(1)
    d = r.json()["data"]
    return d["user"]["user_id"], {"Authorization": f"Bearer {d['token']}"}


def prefs(uid, hdr):
    r = client.get("/api/v1/user/preferences", params={"user_id": uid}, headers=hdr)
    d = (r.json() or {}).get("data") or {}
    return {k: d.get(k) for k in ("color", "style", "comfort", "occasion", "fabric")}


def fb(uid, hdr, rating, tags=None, comment=""):
    return client.post("/api/v1/feedback", headers=hdr, json={
        "user_id": uid, "session_id": "", "outfit_id": "o_test",
        "rating": rating, "tags": tags or [], "comment": comment,
    })


print("=" * 68)
print("1. 标签只影响对应维度（核心断言）")
print("=" * 68)
a, A = mk_user("p1甲颜色")
b, B = mk_user("p1乙场合")
r1 = fb(a, A, 0, ["颜色不喜欢"])
r2 = fb(b, B, 0, ["不适合场合"])
ck("颜色反馈提交成功", r1.status_code == 200, r1.text[:80])
ck("场合反馈提交成功", r2.status_code == 200, r2.text[:80])

pa, pb = prefs(a, A), prefs(b, B)
print("   颜色标签用户偏好:", pa)
print("   场合标签用户偏好:", pb)
ck("颜色标签 → color 下降", pa["color"] < 5, pa["color"])
ck("颜色标签 → occasion 不变", pa["occasion"] == 5, pa["occasion"])
ck("场合标签 → occasion 下降", pb["occasion"] < 5, pb["occasion"])
ck("场合标签 → color 不变", pb["color"] == 5, pb["color"])

print()
print("=" * 68)
print("2. 无标签退回旧口径（五维同步下降）")
print("=" * 68)
c, C = mk_user("p1丙无标签")
fb(c, C, 0, [])
pc = prefs(c, C)
print("   无标签用户偏好:", pc)
ck("无标签 → 五维均下降",
   all(pc[k] == 4 for k in ("color", "style", "comfort", "occasion", "fabric")), pc)

print()
print("=" * 68)
print("3. 自由文本被解析成标签（不需要用户手点）")
print("=" * 68)
d, D = mk_user("p1丁文字")
r = fb(d, D, 0, [], "这件面料有点扎，穿着不舒服")
ck("纯文字反馈提交成功", r.status_code == 200, r.text[:80])
ev = ((r.json() or {}).get("data") or {}).get("evidence") or {}
print("   evidence.auto_tags =", ev.get("auto_tags"))
ck("文字被解析出标签", bool(ev.get("auto_tags")), ev.get("auto_tags"))
pd_ = prefs(d, D)
print("   偏好:", pd_)
ck("解析出的面料标签 → fabric 下降", pd_["fabric"] < 5, pd_["fabric"])
ck("解析出的面料标签 → color 不变", pd_["color"] == 5, pd_["color"])

print()
print("=" * 68)
print("4. 手选标签与文字解析结果合并去重")
print("=" * 68)
e, E = mk_user("p1戊合并")
r = fb(e, E, 0, ["版型不合适"], "颜色也太艳了")
ev = ((r.json() or {}).get("data") or {}).get("evidence") or {}
print("   manual_tags =", ev.get("manual_tags"))
print("   auto_tags   =", ev.get("auto_tags"))
print("   merged tags =", ev.get("tags"))
tags = ev.get("tags") or []
ck("手选标签保留", "版型不合适" in tags, tags)
ck("文字补充标签也在", "颜色不喜欢" in tags, tags)
pe = prefs(e, E)
print("   偏好:", pe)
ck("style 下降（手选）", pe["style"] < 5, pe["style"])
ck("color 下降（文字补充）", pe["color"] < 5, pe["color"])
ck("occasion 未受累", pe["occasion"] == 5, pe["occasion"])

print()
print("=" * 68)
print("5. 撤销精确回退（幂等）")
print("=" * 68)
p_before = prefs(a, A)
r = client.get("/api/v1/feedback/effects", params={"user_id": a, "limit": 5}, headers=A)
items = ((r.json() or {}).get("data") or {}).get("items") or []
ck("能读到反馈影响列表", len(items) >= 1, len(items))
rid = items[0]["record_id"] if items else ""
if rid:
    rr = client.post(f"/api/v1/feedback/{rid}/undo", params={"user_id": a}, headers=A)
    ck("撤销成功", rr.status_code == 200, rr.text[:80])
    p_after = prefs(a, A)
    print("   撤销前:", p_before, "→ 撤销后:", p_after)
    ck("撤销后精确回到中性值", all(p_after[k] == 5 for k in p_after), p_after)

print()
print("=" * 68)
print(f"结果：PASS={len(PASS)}  FAIL={len(FAIL)}")
print("=" * 68)
if FAIL:
    print("失败项：", FAIL)
    sys.exit(1)
