# -*- coding: utf-8 -*-
"""意图判定回归集：守住「穿搭在域放行 / 域外拒答」的判定边界。

背景：2026-11-18 连续三轮出现域外句被本地关键词短路放行
（吃什么/星座 → 旅行目的地/菜谱 → 奥特曼穿什么/怎么搭车/搭煎饼）。
本脚本把这些 case 固化，任何改动判定逻辑后都必须全绿。

用法（项目根目录）：
    PYTHONPATH=. C:/Python311/python.exe scripts/intent_regression.py

需要真实 DEEPSEEK_API_KEY；无 key 时只跑本地兜底路径并标注跳过 LLM 校验。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config
from app.api.chat import _classify_intent, _domain_allows
from app.agent.llm import classify as llm_classify

# (句子, 期望意图, 期望是否在域)
# 期望意图用 None 表示只校验在域与否、不约束具体标签
CASES = [
    # ---------- 截图证据：域外必须拒（四句来自 2026-11-18 三轮反馈）----------
    ("今天适合吃什么？", "off_topic", False),
    ("今天白羊座的运势如何", "off_topic", False),
    ("给我推荐，一个旅行目的地", "off_topic", False),
    ("帮我搭配一下今天的菜谱", "off_topic", False),
    ("我喜欢穿铠甲，你喜欢吗？", "off_topic", False),
    ("奥特曼喜欢穿什么？", "off_topic", False),
    ("怎么搭车？", "off_topic", False),
    ("给我搭一套煎饼", "off_topic", False),
    ("给我推荐一本书", "off_topic", False),
    ("帮我写首诗", "off_topic", False),
    ("最近油价多少", "off_topic", False),
    ("猫咪为什么打呼噜", "off_topic", False),
    ("推荐一部电影", "off_topic", False),
    ("帮我写作业", "off_topic", False),
    # ---------- 在域：穿搭方案（含域外词但落点仍是穿搭，不能误伤）----------
    ("今天穿什么", "recommend", True),
    ("今天穿什么?", "recommend", True),
    ("帮我搭一套衣服", "recommend", True),
    ("给我推荐一套通勤穿搭", "recommend", True),
    ("吃完饭穿什么", "recommend", True),
    ("有啥配饰可以搭", "recommend", True),
    ("穿点什么好", "recommend", True),
    # ---------- 在域：否定 / 结构动作 ----------
    ("这些搭配我都不喜欢", "reject", True),
    ("换一批", "reject", True),
    ("不满意，换一批", "reject", True),
    ("第二套把鞋换成靴子", "refine", True),
    ("第2套我挺喜欢的", "lock", True),
    ("加个配饰", "augment", True),
    ("如果换成黑色会怎样", "compare", True),
    # ---------- 在域：知识 / 人设 / 时尚 / 天气 / 旅游 / 模糊偏好 ----------
    ("三色原则是什么", "knowledge", True),
    ("配饰有哪些种类", "knowledge", True),
    ("为什么冬天穿深色", "knowledge", True),
    ("你是谁", "knowledge", True),
    ("你能做什么", "knowledge", True),
    ("今年流行什么颜色", "trend", True),
    ("今天厦门天气怎么样", "weather", True),
    ("厦门多少度", "weather", True),
    ("明天会下雨吗", "weather", True),
    ("周末去厦门玩3天 穿什么好呢", "travel", True),
    ("亮一点", "free_text", True),
]


def main() -> int:
    llm_on = bool(config.DEEPSEEK_API_KEY)
    print("=" * 68)
    print(f"意图回归：{len(CASES)} 例 | LLM 主裁决: {'ON' if llm_on else 'OFF(本地兜底)'}")
    print("=" * 68)

    fail_intent = 0
    fail_domain = 0
    for text, want_intent, want_domain in CASES:
        got_intent = _classify_intent(text)
        allows = _domain_allows(text)
        got_domain = allows if allows is not None else (got_intent != "off_topic")

        i_ok = want_intent is None or got_intent == want_intent
        d_ok = bool(got_domain) == want_domain
        if not i_ok:
            fail_intent += 1
        if not d_ok:
            fail_domain += 1
        flag = "OK  " if (i_ok and d_ok) else "FAIL"
        print(f"  {flag} [{text}] intent={got_intent}(want {want_intent}) domain={got_domain}")

    # LLM 单独校验：确认主裁决对每条的 domain 判断也与期望一致
    if llm_on:
        print("-" * 68)
        print("LLM 主裁决单独校验（classify 一次调用）：")
        llm_fail = 0
        for text, _want_intent, want_domain in CASES:
            verdict = llm_classify(text)
            if verdict is None:
                print(f"  SKIP [{text}] LLM 调用失败")
                continue
            got = verdict["domain"] == "in"
            if got != want_domain:
                llm_fail += 1
                print(f"  FAIL [{text}] LLM domain={verdict['domain']} want_domain={want_domain}")
        print(f"  LLM domain 不一致: {llm_fail}")

    print("=" * 68)
    print(f"结果：intent FAIL={fail_intent}  domain FAIL={fail_domain}  共 {len(CASES)} 例")
    print("=" * 68)
    return 1 if (fail_intent or fail_domain) else 0


if __name__ == "__main__":
    sys.exit(main())
