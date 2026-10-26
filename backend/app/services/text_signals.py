"""查询侧词法信号：实词抽取 + 实词命中率。

**为什么需要这一层**（P0-05④ 的根因）：
`_grade` 原来只看 `top_score`。但融合分数由三部分组成，其中两路对「问句主题是否真的
出现在课本里」几乎没有判别力：

1. 哈希向量余弦对任意两段中文文本都有 0.05~0.15 的非零基线（bigram 碰撞），
   `VECTOR_WEIGHT=0.45` 直接把它放大成 0.02~0.07 的「白送分」；
2. BM25 归一化的分母是 `max(实际最高分, Σidf)`，而 Σidf 只累加**语料里真实存在**的
   查询词。于是「21 个查询词只命中 3 个单字」时，分母被压得很小，归一化分数反而逼近 1.0，
   实测「怎么用 Rust 写一个 WebAssembly 模块」拿到 0.4016 —— 比真正相关的
   「Malayan Union 对马来西亚宪制发展的长期影响」（0.3605）还高。**分数本身没有排序语义**。

所以判定「课本是否覆盖」必须依赖一个**语义信号**而不是分数：问句的实词有多少真的
出现在课本切片里。本模块只做两件事，都是纯函数、无状态、无随机：

- `content_words()`：抽「实词」。中文按 **bigram** 而不是单字 —— 单字会把「三」「分」
  「色」这类偶然字面重合当成语义命中；bigram 再剔掉含虚词/疑问词字的位置，
  留下的才是「量子」「苏丹」「天空」这类真正的主题词。
- `content_hit_ratio()`：这些实词在给定文本集合里的命中率。

为什么不用 jieba 之类的分词器：本项目零外部模型依赖（P0-12 要求清空 Key 后全链路可跑），
而 bigram + 虚词表在这个「中文教材问句」场景下的区分度已经足够（实测无关问句 ≤0.33、
相关问句 ≥0.50，见 `tests/test_regression_grader_coverage.py`）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

# 显式章节号：4.1 / 4.1.2；以及 ≥3 位的年份/数字（1946）。两位以内裸数字是量词（3 道题），
# 不当实词用，否则「出 3 道题」会把「3」当成主题词。
_NUMBER_RE = re.compile(r"\d{1,2}(?:\.\d{1,2}){1,2}|\d{3,}")
_LATIN_RE = re.compile(r"[a-z]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

# 虚词 / 疑问词 / 指示词单字。凡是 bigram 里出现这些字，说明该 bigram 跨了词边界或本身
# 就是语法成分，一律不算实词。宁可少留几个实词，也不能让「什么」「怎么」「这里」混进分子。
_CJK_STOP_CHARS = frozenset(
    # 结构助词、介词、连词、副词
    "的了是在和与及或也就都而极其个之对为以于把被从向并且但很更最再还又只将让给"
    # 疑问词
    "什么怎哪谁何几多少吗呢吧啊呀么嘛"
    # 人称与指示
    "我你他她它们这那"
    # 泛动词/量词
    "有无会能要想说做用些第"
)

# 双字以上的疑问/套话。整段剔除，避免「如何」「一个」被拆成两个 bigram 各拿半分。
_STOP_PHRASES = frozenset(
    {
        "如何", "怎么", "怎样", "什么", "为何", "哪些", "哪里", "哪儿", "多少", "多久",
        "几点", "是否", "可以", "应该", "一个", "这个", "那个", "我们", "你们", "他们",
        "它们", "请问", "帮我", "一下", "一些", "以及", "还有", "关于", "能否", "是否",
    }
)

# 英文功能词。教材正文里的英文术语（Malayan / Union / Sejarah）必须留下。
_LATIN_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "how", "why", "what", "who", "when", "where",
        "which", "is", "are", "was", "were", "can", "could", "should", "would",
        "please", "explain", "about", "from", "that", "this", "does", "did", "not",
        "you", "your", "our", "its", "has", "have", "will", "may", "but", "any",
    }
)


@dataclass(frozen=True)
class ContentCoverage:
    """实词命中情况。`ratio` 是命中率，`matched` 便于日志与测试定位误判来源。"""

    ratio: float
    matched: tuple[str, ...]
    total: int

    @property
    def is_empty(self) -> bool:
        """问句里一个实词都没抽出来（纯套话，如「讲一下」）。"""
        return self.total == 0


def content_words(text: str) -> list[str]:
    """抽取问句里的实词（保序去重）。

    顺序固定、纯函数：同一问句永远得到同一列表，测试与日志可复现。
    """
    lowered = text.lower()
    words: list[str] = []

    for match in _NUMBER_RE.finditer(lowered):
        words.append(match.group())
    for match in _LATIN_RE.finditer(lowered):
        word = match.group()
        if word not in _LATIN_STOPWORDS:
            words.append(word)
    for match in _CJK_RUN_RE.finditer(lowered):
        run = match.group()
        for index in range(len(run) - 1):
            bigram = run[index : index + 2]
            if bigram in _STOP_PHRASES:
                continue
            if bigram[0] in _CJK_STOP_CHARS or bigram[1] in _CJK_STOP_CHARS:
                continue
            words.append(bigram)

    return list(dict.fromkeys(words))


def content_hit_ratio(words: Sequence[str], texts: Iterable[str]) -> ContentCoverage:
    """实词在 `texts` 里的命中率。

    用「子串出现」而不是 token 集合相等：`words` 本身就是子串形式（bigram / 英文词 /
    数字），与 `tokenize` 的 bigram 语义一致，但对英文大小写与标点更宽容。
    """
    if not words:
        return ContentCoverage(ratio=0.0, matched=(), total=0)
    haystack = "\n".join(text.lower() for text in texts)
    matched = tuple(word for word in words if word in haystack)
    return ContentCoverage(ratio=len(matched) / len(words), matched=matched, total=len(words))


__all__ = [
    "ContentCoverage",
    "content_hit_ratio",
    "content_words",
]
