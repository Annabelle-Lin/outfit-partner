# -*- coding: utf-8 -*-
"""
wardrobe-agent · 演示衣橱数据填充脚本（可选）

为什么需要它：
  前端已改为读取后端真实数据。全新用户的后端衣橱是空的，
  推荐引擎会返回「衣橱还是空的」，于是首页看不到搭配方案。
  跑一次本脚本即可灌入 10 件符合规则库枚举的示例衣物。

使用前提：
  1. 后端已启动（默认 http://127.0.0.1:8300，与 README 的 uvicorn --port 8300 一致）
     若你用了别的端口，把地址作为参数传进来即可
  2. 先打开一次网页，让它自动注册用户（或手动调用 /api/v1/user/register）

用法：
  python seed_demo.py                              # 自动选第一个已有用户，连 8300
  python seed_demo.py u_xxxxxxxx                   # 指定 user_id，连 8300
  python seed_demo.py http://127.0.0.1:8000        # 只改端口
  python seed_demo.py u_xxxxxxxx http://127.0.0.1:8000   # 两个都指定
  （user_id 与 URL 的顺序可以任意互换）

重要（踩坑提示）：
  category / sub_category / color / color_brightness / season / fit_* /
  fabric / style / occasion 都必须严格取自 rules/clothing_meta.json 的枚举值。
  例如一级分类只能是 上装/下装/外套/鞋/连衣裙/配饰（不是「鞋履」），
  color_brightness 只能填「亮色」或「暗色」（不是「亮」「中」）。
  否则规则 F006「品类完整性」校验失败，推荐结果会变成 0 套。
"""
import json
import sys
import urllib.request

# 参数顺序随意：URL 放第 1 个或第 2 个都能识别，剩余的非 URL 参数当作 user_id
_args = [a for a in sys.argv[1:] if not a.startswith("http")]
_urls = [a for a in sys.argv[1:] if a.startswith("http")]
BASE = (_urls[0] if _urls else "http://127.0.0.1:8300").rstrip("/")
API = BASE + "/api/v1"

DEMO = [
    ("雾霾蓝衬衫", "上装", "衬衫", "蓝", "暗色", "四季", "合身", "直筒", "棉", "通勤", ["通勤", "正式"], 3, 4),
    ("白色廓形衬衫", "上装", "衬衫", "白", "亮色", "四季", "宽松", "A字", "棉", "简约", ["通勤", "约会"], 3, 4),
    ("白色基础T恤", "上装", "短袖", "白", "亮色", "夏", "宽松", "直筒", "棉", "基础款", ["居家", "出游"], 2, 1),
    ("燕麦色针织开衫", "上装", "针织衫", "米", "亮色", "秋", "宽松", "直筒", "针织", "甜美", ["约会", "出游"], 4, 2),
    ("黑色阔腿西裤", "下装", "阔腿裤", "黑", "暗色", "四季", "宽松", "阔腿", "混纺", "通勤", ["通勤", "正式"], 3, 4),
    ("浅蓝直筒牛仔裤", "下装", "牛仔裤", "蓝", "亮色", "四季", "合身", "直筒", "牛仔", "休闲", ["出游", "上课"], 3, 2),
    ("黑色半身裙", "下装", "半身裙", "黑", "暗色", "秋", "合身", "A字", "混纺", "简约", ["约会", "通勤"], 3, 3),
    ("卡其色风衣", "外套", "风衣", "卡其", "亮色", "秋", "宽松", "直筒", "混纺", "通勤", ["通勤", "正式"], 4, 4),
    ("黑色乐福鞋", "鞋", "皮鞋", "黑", "暗色", "四季", "", "", "皮革", "通勤", ["通勤", "正式"], 2, 4),
    ("小白鞋", "鞋", "帆布鞋", "白", "亮色", "四季", "", "", "棉", "休闲", ["出游", "上课"], 2, 1),
]


def call(path, body=None, method="GET"):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API + path, data=data,
        headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def pick_user():
    if _args:
        return _args[0]
    r = call("/user/register", {"nickname": "林晓"}, "POST")
    return r["data"]["user_id"]


def main():
    uid = pick_user()
    print("user_id =", uid)

    existing = call("/clothing?user_id=%s&page_size=100" % uid)["data"]
    if existing.get("total", 0) > 0:
        print("衣橱已有 %d 件衣物，为免重复不再写入。" % existing["total"])
        print("如需重灌，请先在页面里清空衣橱。")
        return

    ok = 0
    for (n, c, sc, col, br, se, fs, fsh, fb, st, oc, w, f) in DEMO:
        body = {
            "name": n, "category": c, "sub_category": sc,
            "color": col, "color_brightness": br, "season": se,
            "fit_snug": fs, "fit_silhouette": fsh, "fabric": fb,
            "style": st, "occasion": oc, "warmth_level": w,
            "formality_level": f, "tags": [],
        }
        try:
            r = call("/clothing?user_id=%s" % uid, body, "POST")
            if r.get("code") == 0:
                ok += 1
            else:
                print("跳过", n, r.get("msg"))
        except Exception as e:
            print("失败", n, e)

    print("写入 %d / %d 件" % (ok, len(DEMO)))
    print("当前衣橱总数：", call("/clothing?user_id=%s&page_size=100" % uid)["data"]["total"])
    print("打开或刷新页面即可看到真实搭配方案。")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("\n[连接失败]", e)
        print("当前目标地址：", API)
        print("请确认：")
        print("  1) 后端已启动，且端口与上面的地址一致")
        print("     启动命令：uvicorn app.main:app --reload --port 8300")
        print("  2) 先访问一次 http://127.0.0.1:8300/ 让页面注册用户")
        print("  3) 端口不同就改地址，例如：python seed_demo.py http://127.0.0.1:8000")
        sys.exit(1)
