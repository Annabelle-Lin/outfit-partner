# -*- coding: utf-8 -*-
"""把目标用户衣橱补齐到 80 件，保证 49 个最小品类各至少 1 件。

数据来源（均为已人工核对的干净单品图，非劣质爬虫结果）：
  - 本地 50 张电商图：衣橱示例数据/images/ 与 data/uploads/sample_*.jpg
  - Unsplash 23 张真实单品图：data/uploads/u*.jpg

写入方式：直接写 SQLite（与后端同库），无需鉴权；字段严格取自
rules/clothing_meta.json 枚举，避免 422 与规则 F006 品类完整性失败。

用法：
  C:/Python311/python.exe scripts/seed_wardrobe_80.py           # 执行写入
  C:/Python311/python.exe scripts/seed_wardrobe_80.py --dry-run # 只预览不写

关键：幂等。按 (user_id, name) 去重，重复运行不会重复写入。
"""
import json
import os
import sys
import sqlite3
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.expanduser("~/.wardrobe/wardrobe.db")
UID = "u_699853bb"  # root / Vivian，当前活跃账号
META = json.load(open(os.path.join(ROOT, "rules", "clothing_meta.json"), encoding="utf-8"))
DRY = "--dry-run" in sys.argv

# 本地示例图 URL（已核对图文一致）
S = "/uploads/sample_"


def sample(code):
    """返回 data/uploads 下 sample_Wxxx_*.jpg 的实际 URL。"""
    import glob
    hits = glob.glob(os.path.join(ROOT, "data", "uploads", "sample_W%s_*" % code))
    if not hits:
        raise RuntimeError("找不到 sample W%s" % code)
    return "/uploads/" + os.path.basename(hits[0])


U = "/uploads/u"

# 24 个缺口品类：每件 = (name, cat, sub, img, color, brightness, season, snug, silhouette, fabric, style, occasion, warmth, formality)
# 图片按「最贴近的真实单品」映射；无完全对应图的品类用同大类近邻干净图，字段仍严格按该品类填写。
MISSING = [
    ("米色针织开衫",   "上衣", "针织衫", sample("017"), "米", "亮色", "秋", "宽松", "直筒", "针织", "简约", ["通勤", "日常"], 4, 3),
    ("白色连帽卫衣",   "上衣", "卫衣",   U + "18_white_hoodie.jpg", "白", "亮色", "春秋", "宽松", "落肩", "棉", "休闲", ["日常", "上课"], 3, 1),
    ("白色棉质背心",   "上衣", "背心",   sample("001"), "白", "亮色", "夏", "合身", "直筒", "棉", "基础款", ["日常", "运动"], 1, 1),
    ("黑色细肩吊带",   "上衣", "吊带",   sample("002"), "黑", "暗色", "夏", "修身", "紧身", "丝", "简约", ["约会", "日常"], 1, 2),
    ("藏青西装马甲",   "上衣", "马甲",   sample("015"), "蓝", "暗色", "春秋", "修身", "直筒", "混纺", "通勤", ["面试", "通勤"], 3, 4),
    ("白色长袖打底衫", "上衣", "打底衫", sample("027"), "白", "亮色", "四季", "修身", "紧身", "棉", "基础款", ["日常", "通勤"], 2, 2),
    ("卡其休闲短裤",   "下装", "短裤",   sample("009"), "卡其", "亮色", "夏", "宽松", "直筒", "棉", "休闲", ["出游", "日常"], 1, 1),
    ("黑色弹力打底裤", "下装", "打底裤", sample("008"), "黑", "暗色", "四季", "修身", "紧身", "化纤", "基础款", ["运动", "日常"], 2, 1),
    ("蓝色牛仔背带裤", "下装", "背带裤", sample("007"), "蓝", "亮色", "春秋", "宽松", "直筒", "牛仔", "休闲", ["出游", "上课"], 3, 1),
    ("黑色连体裤",     "连衣装", "连体裤", sample("038"), "黑", "暗色", "春秋", "合身", "收腰", "混纺", "简约", ["通勤", "约会"], 3, 3),
    ("牛仔背带裙",     "连衣装", "背带裙", U + "21_denim_dress.jpg", "蓝", "亮色", "春秋", "宽松", "A字", "牛仔", "休闲", ["出游", "上课"], 3, 1),
    ("灰色西装套装",   "套装", "西装套装", sample("015"), "灰", "暗色", "春秋", "合身", "直筒", "混纺", "通勤", ["面试", "正式"], 3, 5),
    ("黄色运动套装",   "套装", "运动套装", U + "22_yellow_tracksuit.jpg", "黄", "亮色", "春秋", "宽松", "直筒", "化纤", "运动", ["运动", "日常"], 3, 1),
    ("米色休闲套装",   "套装", "休闲套装", sample("041"), "米", "亮色", "春秋", "宽松", "直筒", "棉", "休闲", ["日常", "出游"], 3, 1),
    ("黑色棉服",       "外套", "棉服",   sample("014"), "黑", "暗色", "冬", "宽松", "直筒", "化纤", "基础款", ["日常", "通勤"], 5, 2),
    ("棕色皮夹克",     "外套", "皮衣",   U + "25_leather_jacket_man.jpg", "棕", "暗色", "春秋", "合身", "直筒", "皮革", "复古", ["出游", "约会"], 4, 3),
    ("绿色皮鞋",       "鞋", "皮鞋",     U + "16_green_leather_shoe.jpg", "绿", "暗色", "四季", "", "", "皮革", "通勤", ["通勤", "正式"], 2, 4),
    ("米色单鞋",       "鞋", "单鞋",     sample("020"), "米", "亮色", "春秋", "", "", "皮革", "简约", ["通勤", "约会"], 2, 3),
    ("白橙休闲凉鞋",   "鞋", "凉鞋",     U + "06_white_orange_sneaker.jpg", "白", "亮色", "夏", "", "", "皮革", "休闲", ["出游", "日常"], 1, 1),
    ("灰色居家拖鞋",   "鞋", "拖鞋",     sample("021"), "灰", "暗色", "四季", "", "", "棉", "基础款", ["居家"], 1, 1),
    ("简约腕表",       "配饰", "首饰",   U + "12_smartwatch.jpg", "白", "亮色", "四季", "", "", "化纤", "简约", ["通勤", "日常"], 1, 3),
    ("黑色方框墨镜",   "配饰", "墨镜",   U + "23_sunglasses_woman.jpg", "黑", "暗色", "夏", "", "", "化纤", "设计款", ["出游", "约会"], 1, 3),
    ("米色针织手套",   "配饰", "手套",   sample("024"), "米", "亮色", "冬", "", "", "针织", "基础款", ["日常", "通勤"], 4, 2),
    ("白色中筒袜",     "配饰", "袜子",   sample("019"), "白", "亮色", "四季", "", "", "棉", "基础款", ["日常", "运动"], 1, 1),
]

# 额外 5 件：补充已有品类，让总数到 80（图片用已核对的干净图）
EXTRA = [
    ("浅蓝条纹衬衫",   "上衣", "衬衫",   sample("030"), "蓝", "亮色", "四季", "合身", "直筒", "棉", "通勤", ["通勤", "上课"], 3, 3),
    ("黑色直筒牛仔裤", "下装", "牛仔裤", sample("033"), "黑", "暗色", "四季", "合身", "直筒", "牛仔", "简约", ["日常", "出游"], 3, 2),
    ("米白高领毛衣",   "上衣", "毛衣",   sample("043"), "米", "亮色", "冬", "合身", "直筒", "针织", "简约", ["通勤", "日常"], 5, 3),
    ("黑色链条单肩包", "配饰", "包包",   U + "13_black_chainbag.jpg", "黑", "暗色", "四季", "", "", "皮革", "通勤", ["通勤", "约会"], 1, 3),
    ("棕色短靴",       "鞋", "靴子",     sample("022"), "棕", "暗色", "冬", "", "", "皮革", "复古", ["出游", "通勤"], 4, 3),
]


def validate(row):
    n, c, sc, img, col, br, se, snug, sil, fab, sty, oc, w, f = row
    assert c in META["category"], (n, "bad category", c)
    assert sc in META["category"][c], (n, "bad sub_category", c, sc)
    assert col in META["color_family"], (n, "bad color", col)
    assert br in META["color_brightness"], (n, "bad brightness", br)
    assert se in META["season"], (n, "bad season", se)
    if snug: assert snug in META["fit_snug"], (n, "bad snug", snug)
    if sil: assert sil in META["fit_silhouette"], (n, "bad silhouette", sil)
    assert fab in META["fabric"], (n, "bad fabric", fab)
    assert sty in META["style"], (n, "bad style", sty)
    for o in oc:
        assert o in META["occasion"], (n, "bad occasion", o)
    assert isinstance(w, int) and 1 <= w <= 5
    assert isinstance(f, int) and 1 <= f <= 5
    assert os.path.exists(os.path.join(ROOT, "data", img.lstrip("/"))), (n, "missing img", img)


def main():
    rows = MISSING + EXTRA
    for r in rows:
        validate(r)
    print("校验通过：%d 件待写入，其中缺口品类 %d 件" % (len(rows), len(MISSING)))

    if DRY:
        for r in rows:
            print("  ", r[0], r[1], r[2], r[3])
        return

    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    existing = {r[0] for r in cur.execute("select name from clothing_items where user_id=?", (UID,))}
    now = datetime.now().isoformat()
    added = 0
    for (n, c, sc, img, col, br, se, snug, sil, fab, sty, oc, w, f) in rows:
        if n in existing:
            print("跳过(已存在)", n)
            continue
        item_id = "i_auto_%s" % ("%03d" % (added + 1))
        # 图片为同大类近似款时如实标注，避免图文误导
        approx = n in {"白橙休闲凉鞋", "灰色居家拖鞋", "白色中筒袜", "米色针织手套",
                       "黑色弹力打底裤", "蓝色牛仔背带裤", "黑色连体裤", "灰色西装套装"}
        tag_note = ("图片为同大类近似款" if approx else "图片：电商示例图/Unsplash 已核对单品")
        cur.execute(
            """insert into clothing_items
               (item_id,user_id,name,category,sub_category,color,color_brightness,season,
                fit_snug,fit_silhouette,fabric,style,occasion,warmth_level,formality_level,
                image_url,tags,created_at,updated_at)
               values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, UID, n, c, sc, col, br, se, snug, sil, fab, sty,
             json.dumps(oc, ensure_ascii=False), w, f, img,
             json.dumps([tag_note], ensure_ascii=False),
             now, now))
        added += 1
    conn.commit()
    total = cur.execute("select count(*) from clothing_items where user_id=?", (UID,)).fetchone()[0]
    print("写入 %d 件，当前总数 %d 件" % (added, total))
    # 品类覆盖核验
    subs = {r[0] for r in cur.execute("select distinct sub_category from clothing_items where user_id=?", (UID,))}
    all_subs = {s for lst in META["category"].values() for s in lst}
    miss = sorted(all_subs - subs)
    print("最小品类覆盖 %d/%d，未覆盖：%s" % (len(subs & all_subs), len(all_subs), miss or "无"))
    conn.close()


if __name__ == "__main__":
    main()
