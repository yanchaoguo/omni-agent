# -*- coding: utf-8 -*-
"""通用文本相似度工具类(LessonStore 知识归并与 SkillsManager 技能匹配共用)"""
import re


class TextSimilarity:
    """- tokens() : 混合分词——英文/数字按词切, 中文按二元切分(bigram)
    - similar(): 基于词元集合的 Jaccard 相似度"""

    @staticmethod
    def tokens(text):
        toks = set(re.findall(r"[a-z0-9_]{2,}", text.lower()))
        for seg in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            toks.update(seg[i:i + 2] for i in range(len(seg) - 1))
        return toks

    @classmethod
    def similar(cls, a, b):
        """Jaccard 相似度: |A∩B| / |A∪B|, 返回 [0, 1]"""
        ta, tb = cls.tokens(a), cls.tokens(b)
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    @classmethod
    def overlap(cls, query, doc):
        """非对称覆盖率: query 词元出现在 doc 中的比例, 适合 意图→技能描述 匹配"""
        tq, td = cls.tokens(query), cls.tokens(doc)
        if not tq or not td:
            return 0.0
        return len(tq & td) / len(tq)
