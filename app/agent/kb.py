# -*- coding: utf-8 -*-
"""知识库检索层 —— 对 S3 交付的 Markdown 文档做索引。

设计原则：
- 启动时自动扫描 data/ 下所有 .md，按二级/三级标题切片
- 优先使用 jieba + rank-bm25（BM25Okapi）；未安装时降级为简单词频匹配
- 检索返回 TopN 片段 + 来源标注，供 LLM 生成回答或降级展示
"""
import re
from collections import Counter
from pathlib import Path
from typing import Any

from app import config


# 同义词 / 上下位词表（2.1 混合检索用）。
# 纯词法检索的死穴：「大衣」和「外套」字面零重合就漏召回。
# 这里做单层双向扩展，只补召回、不改语义，扩展后仍由 BM25 打分。
_SYNONYMS: dict[str, tuple[str, ...]] = {
    # ---------- 品类：上下位词 ----------
    "外套": ("大衣", "风衣", "夹克", "棉服", "羽绒服", "皮衣", "西装", "开衫"),
    "大衣": ("外套", "呢子", "风衣", "羽绒服", "毛呢"),
    "风衣": ("外套", "大衣", "长外套", "trench"),
    "羽绒服": ("外套", "棉服", "保暖", "厚外套"),
    "棉服": ("外套", "羽绒服", "保暖"),
    "上装": ("上衣", "衬衫", "毛衣", "卫衣", "T恤", "针织衫", "打底"),
    "上衣": ("上装", "衬衫", "毛衣", "卫衣", "T恤", "针织衫"),
    "下装": ("裤子", "长裤", "短裤", "裙", "半身裙", "直筒裤", "牛仔裤"),
    "裤子": ("下装", "长裤", "直筒裤", "牛仔裤", "阔腿裤"),
    "裙子": ("半身裙", "连衣裙", "下装", "裙"),
    "连衣裙": ("裙子", "连衣", "一件式"),
    "鞋": ("鞋子", "皮鞋", "运动鞋", "靴子", "乐福鞋", "小白鞋"),
    "包": ("包包", "手提包", "托特包", "配饰"),
    # ---------- 季节与温度 ----------
    "冬天": ("冬季", "寒冬", "保暖", "低温", "冷天"),
    "冬季": ("冬天", "寒冬", "保暖", "低温"),
    "夏天": ("夏季", "炎热", "透气", "短袖", "清凉"),
    "夏季": ("夏天", "炎热", "透气", "短袖"),
    "春秋": ("春季", "秋季", "薄外套", "过渡季"),
    "秋天": ("秋季", "春秋", "薄外套"),
    "春天": ("春季", "春秋", "薄外套"),
    # ---------- 体型与塑形诉求（口语 → 术语，补召回重点） ----------
    "显瘦": ("收腰", "修身", "遮肉", "显高"),
    "显高": ("高腰", "短款", "显腿长"),
    "小个子": ("矮个", "娇小", "显高", "短款", "高腰", "压个子"),
    "微胖": ("遮肉", "显瘦", "宽松", "垂感"),
    "腿粗": ("遮肉", "显瘦", "阔腿裤", "直筒裤", "A型", "下装"),
    "胯宽": ("遮肉", "A型", "阔腿裤", "直筒裤", "下装"),
    "胳膊粗": ("遮肉", "泡泡袖", "五分袖", "上装"),
    "上半身壮": ("遮肉", "V领", "落肩", "上装", "Y型"),
    "肩宽": ("Y型", "落肩", "V领", "上装"),
    "肚子大": ("遮肉", "高腰", "A型", "宽松"),
    "五五分": ("高腰", "显腿长", "腰线", "显高"),
    "压个子": ("小个子", "显高", "短款", "高腰"),
    "显老": ("减龄", "活泼", "浅色", "设计款"),
    "显嫩": ("成熟", "沉稳", "深色", "基础款"),
    # ---------- 廓形与版型 ----------
    "廓形": ("H型", "X型", "A型", "O型", "Y型", "版型", "剪裁"),
    "修身": ("收腰", "合体", "X型", "显瘦"),
    "宽松": ("oversize", "O型", "直筒", "慵懒"),
    "直筒": ("H型", "直筒裤", "直线廓形"),
    "收腰": ("X型", "腰线", "修身", "高腰"),
    "腰线": ("收腰", "高腰", "腰带", "比例"),
    "挺括": ("硬挺", "有型", "结构感", "面料"),
    "垂感": ("垂坠", "柔软", "显瘦", "面料"),
    # ---------- 色彩维度（原表缺整个维度） ----------
    "配色": ("色彩", "三色原则", "主色", "辅色", "点缀色"),
    "显白": ("肤色", "冷暖皮", "低纯度", "显气色"),
    "显黑": ("肤色", "高纯度", "避免", "显气色"),
    "大地色": ("棕色", "驼色", "卡其", "米色", "暖色"),
    "莫兰迪": ("低纯度", "灰调", "高级灰", "柔和"),
    "雾霾蓝": ("低纯度", "灰调", "蓝色"),
    "高级灰": ("灰色", "无彩色", "中性色"),
    "黑白灰": ("无彩色", "中性色", "基础色"),
    "亮色": ("高纯度", "鲜艳", "点缀色"),
    "撞色": ("对比色", "冷暖对比", "配色"),
    "同色系": ("深浅", "单色搭配", "配色"),
    # ---------- 面料与体感（口语 → 术语） ----------
    "扎人": ("刺痒", "羊毛", "亲肤", "内层"),
    "起球": ("抗起毛起球", "耐磨", "面料"),
    "闷汗": ("透气", "吸湿排汗", "透湿"),
    "不透气": ("透气", "闷", "面料"),
    "皱": ("抗皱", "免烫", "易皱"),
    "防水": ("防风", "涂层", "降雨", "外层"),
    "保暖": ("冬天", "羽绒服", "棉服", "羊毛", "厚", "热阻"),
    "透气": ("夏天", "棉麻", "清凉", "短袖", "透湿"),
    # ---------- 场景与正式度 ----------
    "通勤": ("上班", "职场", "办公", "正式", "商务"),
    "上班": ("通勤", "职场", "办公", "商务"),
    "正式": ("通勤", "西装", "皮鞋", "职场", "商务正装"),
    "商务正装": ("正式", "西装", "套裙", "礼服"),
    "商务休闲": ("smart casual", "通勤", "休闲", "半正式"),
    "面试": ("正式", "通勤", "西装", "衬衫"),
    "婚礼": ("正装", "礼服", "西装", "正式"),
    "葬礼": ("深色", "素色", "正装", "庄重"),
    "晚宴": ("礼服", "正装", "宴会", "正式"),
    "宴会": ("礼服", "晚宴", "正装"),
    "约会": ("休闲", "浪漫", "裙", "甜", "半正式"),
    "居家": ("宅", "家居服", "舒适", "休闲"),
    "出差": ("通勤", "商务", "旅行", "多场景"),
    "旅行": ("出游", "休闲", "舒适", "运动鞋"),
    "运动": ("健身", "休闲", "运动鞋", "速干"),
}


# 单个 chunk 正文字符上限。超过则二次切分，而不是在返回时硬截断。
# 背景：06 文档的 R01–R15 规则表整表位于「第一层」这一节内，长度约 1300 字，
# 旧实现只在返回时做 text[:800]，导致 R13 腰线规则 / R14 安全合规 /
# R15 号型兼容 三条永远进不了模型上下文。
_MAX_CHUNK_CHARS = 700


def _split_table(block: str) -> list[str]:
    """把超长 Markdown 表格按行分组切开，并在每个分组上重复表头。

    重复表头很关键：BM25 靠字面重合打分，丢掉表头的分片会同时丢失
    「规则号 / 规则内容」这类字段名，召回分数会显著偏低。
    """
    lines = [s.strip() for s in block.splitlines() if s.strip().startswith("|")]
    if len(lines) < 3:
        return [block]

    header = lines[:2]          # 表头行 + 分隔行
    rows = lines[2:]
    if not rows:
        return [block]

    hdr = "\n".join(header)
    hdr_len = len(hdr) + 1

    pieces: list[str] = []
    buf: list[str] = []
    cur = hdr_len
    for row in rows:
        if buf and cur + len(row) + 1 > _MAX_CHUNK_CHARS:
            pieces.append(hdr + "\n" + "\n".join(buf))
            buf = []
            cur = hdr_len
        buf.append(row)
        cur += len(row) + 1
    if buf:
        pieces.append(hdr + "\n" + "\n".join(buf))
    return pieces or [block]


def _split_body(body: str) -> list[str]:
    """把超长正文按「段落 → 表格行组」两级切分，尽量不切断单条规则。"""
    if len(body) <= _MAX_CHUNK_CHARS:
        return [body]

    blocks = [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]
    if not blocks:
        return [body]

    pieces: list[str] = []
    buf: list[str] = []
    buf_len = 0

    def push() -> None:
        nonlocal buf, buf_len
        if buf:
            pieces.append("\n\n".join(buf).strip())
            buf = []
            buf_len = 0

    for block in blocks:
        # 单个表格块本身就超长（如 R01–R15 整表）：交给表格专用切分
        if len(block) > _MAX_CHUNK_CHARS and "|" in block:
            push()
            pieces.extend(_split_table(block))
            continue
        if buf and buf_len + len(block) + 2 > _MAX_CHUNK_CHARS:
            push()
        buf.append(block)
        buf_len += len(block) + 2
    push()

    return [p for p in pieces if p] or [body]


class KnowledgeIndex:
    """知识索引。优先 BM25，无依赖时用简单词频匹配。"""

    def __init__(self, data_dir: Path = config.DATA_DIR):
        self.data_dir = data_dir
        self.chunks: list[dict[str, Any]] = []
        self.corpus: list[list[str]] = []
        self.bm25 = None
        self._use_bm25 = False
        self._use_jieba = False

    def _tokenize(self, text: str) -> list[str]:
        """分词：有 jieba 用 jieba，否则按字符/英文单词切分。"""
        if self._use_jieba:
            import jieba
            tokens = list(jieba.cut_for_search(text))
        else:
            # 简单分词：中文逐字 + 英文/数字按非词字符切分
            tokens = []
            for token in re.findall(r"[\u4e00-\u9fa5]|[a-zA-Z0-9]+", text):
                if re.match(r"^[a-zA-Z0-9]+$", token):
                    tokens.append(token.lower())
                else:
                    tokens.extend(list(token))
        return [t for t in tokens if t.strip() and not re.match(r"^[\s\|\-\*:>#]+$", t)]

    def load(self) -> "KnowledgeIndex":
        """扫描 .md 文件，切片，建索引。可重复调用以刷新。"""
        self.chunks = []
        self.corpus = []
        self.bm25 = None
        self._use_bm25 = False
        self._use_jieba = False

        # 探测可选依赖
        try:
            import jieba  # noqa: F401
            self._use_jieba = True
        except ImportError:
            pass

        try:
            from rank_bm25 import BM25Okapi
            self._use_bm25 = True
        except ImportError:
            pass

        md_files = sorted(self.data_dir.rglob("*.md"))
        if not md_files:
            return self

        for md_path in md_files:
            if md_path.name == "README.md" and md_path.parent == self.data_dir:
                continue
            self._slice_file(md_path)

        if not self.chunks:
            return self

        # 统一对所有 chunk 分词，避免多文件重复累积
        self.corpus = [self._tokenize(c["text"]) for c in self.chunks]

        if self._use_bm25:
            from rank_bm25 import BM25Okapi
            self.bm25 = BM25Okapi(self.corpus)
        return self

    def _slice_file(self, md_path: Path) -> None:
        """按标题把 markdown 切成若干 chunk。"""
        text = md_path.read_text(encoding="utf-8")
        lines = text.splitlines()

        doc_title = ""
        current_h2 = ""
        current_h3 = ""
        buffer: list[str] = []

        def flush():
            if not buffer:
                return
            body = "\n".join(buffer).strip()
            buffer.clear()
            if not body or len(body) < 10:
                return
            title = doc_title
            if current_h2:
                title += " / " + current_h2
            if current_h3:
                title += " / " + current_h3

            # 按长度二次切分：每片都带完整标题路径，后续在返回阶段不再截断正文
            for i, piece in enumerate(_split_body(body)):
                piece_title = title if i == 0 else "{}（续{}）".format(title, i + 1)
                source_line = ""
                m = re.search(r"\[来源:[^\]]+\]", piece)
                if m:
                    source_line = m.group(0)
                self.chunks.append({
                    "doc": md_path.name,
                    "title": piece_title,
                    "source": source_line,
                    "text": piece,
                })

        for line in lines:
            if line.strip() and not line.startswith("#"):
                doc_title = line.strip().lstrip("# ")
                break
            if line.startswith("# "):
                doc_title = line.lstrip("# ").strip()
                break

        for line in lines:
            s = line.rstrip()
            if s.startswith("## "):
                flush()
                current_h2 = s.lstrip("# ").strip()
                current_h3 = ""
            elif s.startswith("### "):
                flush()
                current_h3 = s.lstrip("# ").strip()
            else:
                if s.strip():
                    buffer.append(s)

        flush()

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """检索，返回带分数的片段。

        2.1 混合检索：原词通道 + 同义词扩展通道，各自用 BM25 打分后按 RRF 融合。
        单通道打分尺度不同（原词命中长句分数会被拉低），RRF 只看排名不看绝对分，
        能稳定把「两个通道都靠前」的片段顶上来，比直接相加分数更耐噪。
        """
        if not self.corpus:
            return []

        channels: list[list[str]] = [self._tokenize(query)]
        if config.KB_HYBRID_ENABLED and config.KB_SYNONYM_EXPAND:
            expanded = self._expand_synonyms(query)
            if expanded:
                channels.append(self._tokenize(expanded))

        fused: dict[int, float] = {}
        best_raw: dict[int, float] = {}
        k = max(1, config.KB_RRF_K)

        for tokens in channels:
            scores = self._score_channel(tokens)
            order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
            for rank, idx in enumerate(order):
                if scores[idx] <= 0:
                    continue
                # RRF：1 / (k + rank)，排名越靠前贡献越大
                fused[idx] = fused.get(idx, 0.0) + 1.0 / (k + rank + 1)
            for i, sc in enumerate(scores):
                if sc > best_raw.get(i, 0.0):
                    best_raw[i] = sc

        if not fused:
            return []

        ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
        results: list[dict[str, Any]] = []
        for idx, rrf in ordered[:top_k]:
            chunk = self.chunks[idx]
            results.append({
                "doc": chunk["doc"],
                "title": chunk["title"],
                "source": chunk["source"],
                # 切片阶段已保证单片不超过 _MAX_CHUNK_CHARS，
                # 这里只做一个远高于上限的保护性封顶，避免二次截断丢失规则条目。
                "text": chunk["text"][:2000],
                # score 保留 RRF 融合分（越小越可比），raw_score 保留原始 BM25 分便于排查
                "score": round(float(rrf), 6),
                "raw_score": round(float(best_raw.get(idx, 0.0)), 4),
            })
        return results

    def _score_channel(self, tokens: list[str]) -> list[float]:
        """单通道打分：有 BM25 用 BM25，否则退化为归一化词频。"""
        if not tokens:
            return [0.0] * len(self.corpus)
        if self.bm25:
            return [float(x) for x in self.bm25.get_scores(tokens)]
        query_tokens = set(tokens)
        doc_freqs = [Counter(t) for t in self.corpus]
        return [
            (sum(doc_freqs[i].get(t, 0) for t in query_tokens)
             / max(len(self.corpus[i]), 1)) * 1000
            for i in range(len(self.corpus))
        ]

    def _expand_synonyms(self, text: str) -> str:
        """同义词扩展：命中词表就并入等价词，只做单层扩展避免查询膨胀。"""
        extra: list[str] = []
        for key, alts in _SYNONYMS.items():
            if key not in text:
                continue
            for alt in alts:
                alt = alt.strip()
                if alt and alt not in text and alt not in extra:
                    extra.append(alt)
        if not extra:
            return ""
        return text + " " + " ".join(extra)

    def stats(self) -> dict[str, Any]:
        return {
            "docs": len({c["doc"] for c in self.chunks}),
            "chunks": len(self.chunks),
            "ready": len(self.chunks) > 0,
            "bm25_enabled": self.bm25 is not None,
            "jieba_enabled": self._use_jieba,
            "hybrid_enabled": bool(config.KB_HYBRID_ENABLED),
        }
