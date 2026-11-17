#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前端交付物契约校验（v2）。

美化外包交回 index.html 后先跑这个脚本：它不判断"好不好看"，
只判断"功能锚点有没有被改坏"，把"页面漂亮但按钮点不动"提前拦下来。

用法：
    python3 scripts/check_frontend_contract.py [新的index.html路径]

退出码：0 = 通过可合并，1 = 触碰红线
"""
import io
import os
import re
import sys
import json
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, "docs", "frontend-contract", "index-anchors.json")
TARGET = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "frontend", "index.html")

OK, BAD, WARN = [], [], []


def ok(m):
    OK.append(m)
    print("  PASS  " + m)


def bad(m):
    BAD.append(m)
    print("  FAIL  " + m)


def warn(m):
    WARN.append(m)
    print("  WARN  " + m)


def extract(html):
    """提取全部功能锚点。"""
    styles = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S)
    scripts = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", html, re.S)
    css = "\n".join(styles)
    js = "\n".join(scripts)

    fn_set = set(re.findall(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", js, re.M))
    fn_set |= set(re.findall(r"^\s*([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function", js, re.M))

    root = re.search(r":root\s*\{(.*?)\}", css, re.S)
    tokens = {}
    if root:
        for line in root.group(1).split(";"):
            line = line.strip()
            if line.startswith("--") and ":" in line:
                k, v = line.split(":", 1)
                tokens[k.strip()] = v.strip()

    onclick = re.findall(r'onclick="([A-Za-z_$][\w$.]*)\(', html)
    api = re.findall(r"_api\(\s*'([^']+)'", js)

    return {
        "views": set(re.findall(r'<section[^>]*id="(view-[^"]+)"', html)),
        "html_ids": set(re.findall(r'\sid="([^"]+)"', html)),
        "js_ids": set(re.findall(r"getElementById\(\s*'([^']+)'", js))
        | set(re.findall(r"\$\(\s*'#([^']+)'", js)),
        "onclick_counts": dict(Counter(onclick)),
        "api_counts": dict(Counter(api)),
        "js_functions": fn_set,
        "storage_keys": set(re.findall(
            r"localStorage\.(?:getItem|setItem|removeItem)\(\s*'([^']+)'", js)),
        "css_tokens": tokens,
    }


def main():
    print("=" * 70)
    print("前端契约校验  v2")
    print("  基准:", BASELINE)
    print("  目标:", TARGET)
    print("=" * 70)

    for p in (BASELINE, TARGET):
        if not os.path.exists(p):
            print("缺文件:", p)
            return 1

    with io.open(BASELINE, encoding="utf-8") as f:
        base = json.load(f)
    with io.open(TARGET, encoding="utf-8") as f:
        html = f.read()

    got = extract(html)

    # 1. 页面视图
    print("\n[页面视图 view-*]  基准 %d 个" % len(base["views"]))
    miss = sorted(set(base["views"]) - got["views"])
    if miss:
        bad("视图缺失: " + ", ".join(miss))
    else:
        ok("%d 个视图全部保留" % len(base["views"]))

    # 2. HTML 静态 id —— 改名/删除都会在这里暴露
    print("\n[HTML 元素 id]  基准 %d 个" % len(base["html_static_ids"]))
    miss = sorted(set(base["html_static_ids"]) - got["html_ids"])
    if miss:
        bad("%d 个 id 被改名或删除: %s%s" % (
            len(miss), ", ".join(miss[:8]), " ..." if len(miss) > 8 else ""))
    else:
        ok("%d 个 id 全部保留" % len(base["html_static_ids"]))

    # 3. JS 抓取的 id
    print("\n[JS 抓取的 id]  基准 %d 个" % len(base["js_ids"]))
    miss = sorted(set(base["js_ids"]) - got["js_ids"])
    if miss:
        bad("JS 不再抓取: " + ", ".join(miss[:8]))
    else:
        ok("JS 抓取目标全部保留")

    # 4. 内联 onclick —— 按出现次数比对，改一个也算
    print("\n[内联 onclick 绑定]  基准 %d 次调用 / %d 个函数"
          % (sum(base["onclick_counts"].values()), len(base["onclick_counts"])))
    shrink = []
    for fn, n in base["onclick_counts"].items():
        m = got["onclick_counts"].get(fn, 0)
        if m < n:
            shrink.append("%s(%d→%d)" % (fn, n, m))
    if shrink:
        bad("%d 个事件绑定减少或消失: %s" % (len(shrink), ", ".join(shrink[:6])))
    else:
        ok("全部 onclick 绑定保留")

    # 5. API 调用点 —— 路径一改，后端直接 404/422
    print("\n[API 调用]  基准 %d 次 / %d 个路径"
          % (sum(base["api_counts"].values()), len(base["api_counts"])))
    shrink = []
    for pth, n in base["api_counts"].items():
        m = got["api_counts"].get(pth, 0)
        if m < n:
            shrink.append("%s(%d→%d)" % (pth, n, m))
    if shrink:
        bad("%d 个接口调用减少或改路径: %s" % (len(shrink), ", ".join(shrink[:6])))
    else:
        ok("全部 API 调用保留")

    # 6. JS 函数
    print("\n[JS 函数]  基准 %d 个" % len(base["js_functions"]))
    miss = sorted(set(base["js_functions"]) - got["js_functions"])
    if miss:
        bad("%d 个函数被删或改名: %s" % (len(miss), ", ".join(miss[:8])))
    else:
        ok("%d 个函数全部保留" % len(base["js_functions"]))

    # 7. localStorage 键 —— 改了就要重新登录
    print("\n[localStorage 键]  基准 %d 个" % len(base["storage_keys"]))
    miss = sorted(set(base["storage_keys"]) - got["storage_keys"])
    if miss:
        bad("键被改名/删除(会导致登录态丢失): " + ", ".join(miss))
    else:
        ok("全部保留")

    # 8. CSS 变量 —— 只查名，值随便改
    print("\n[CSS 设计变量]  基准 %d 个" % len(base["css_tokens"]))
    miss = sorted(set(base["css_tokens"]) - set(got["css_tokens"]))
    if miss:
        bad("变量被改名/删除: " + ", ".join(miss[:8]))
    else:
        changed = sum(1 for k, v in base["css_tokens"].items()
                      if got["css_tokens"].get(k) != v)
        ok("变量名全部保留（其中 %d 个的值已调整）" % changed)

    # 9. 构建链
    print("\n[交付形态]")
    if "package.json" in html or re.search(r'<script[^>]+type="module"', html):
        warn("疑似引入构建依赖，确认不需要 npm build")
    else:
        ok("未引入构建链，仍是纯静态")
    ext = re.findall(r'<script[^>]+src="(https?://[^"]+)"', html)
    if ext:
        warn("引用外部 JS: " + ", ".join(ext[:3]))
    else:
        ok("无外部 JS 依赖")

    print("\n" + "=" * 70)
    print("结果：PASS=%d  FAIL=%d  WARN=%d" % (len(OK), len(BAD), len(WARN)))
    print("=" * 70)
    if BAD:
        print("\n以下红线被触碰，请不要合并：")
        for b in BAD:
            print("  - " + b)
        return 1
    print("\n契约通过，可以进入功能回归与视觉验收。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
