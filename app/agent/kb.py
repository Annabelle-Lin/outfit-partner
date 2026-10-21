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
            if not body or len(body) < 10:
                buffer.clear()
                return
            title = doc_title
            if current_h2:
                title += " / " + current_h2
            if current_h3:
                title += " / " + current_h3
            source_line = ""
            m = re.search(r"\[来源:[^\]]+\]", body)
            if m:
                source_line = m.group(0)
            self.chunks.append({
                "doc": md_path.name,
                "title": title,
                "source": source_line,
                "text": body,
            })
            buffer.clear()

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
        """检索，返回带分数的片段。"""
        if not self.corpus:
            return []

        if self.bm25:
            tokens = self._tokenize(query)
            scores = self.bm25.get_scores(tokens)
            indexed = list(enumerate(scores))
        else:
            # 降级：简单 TF 匹配
            query_tokens = set(self._tokenize(query))
            if not query_tokens:
                return []
            doc_freqs = [Counter(tokens) for tokens in self.corpus]
            total_tokens = [len(tokens) for tokens in self.corpus]
            scores = []
            for i, tokens in enumerate(self.corpus):
                match = sum(doc_freqs[i].get(t, 0) for t in query_tokens)
                # 归一化：匹配词频 / 文档总词数 * 1000
                score = (match / max(total_tokens[i], 1)) * 1000
                scores.append(score)
            indexed = list(enumerate(scores))

        indexed.sort(key=lambda x: x[1], reverse=True)
        results = []
        for idx, score in indexed[:top_k]:
            if score <= 0:
                continue
            chunk = self.chunks[idx]
            results.append({
                "doc": chunk["doc"],
                "title": chunk["title"],
                "source": chunk["source"],
                "text": chunk["text"][:800],
                "score": round(float(score), 4),
            })
        return results

    def stats(self) -> dict[str, Any]:
        return {
            "docs": len({c["doc"] for c in self.chunks}),
            "chunks": len(self.chunks),
            "ready": len(self.chunks) > 0,
            "bm25_enabled": self.bm25 is not None,
            "jieba_enabled": self._use_jieba,
        }
