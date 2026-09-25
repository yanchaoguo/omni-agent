# -*- coding: utf-8 -*-
"""统一记忆(lessons.jsonl): 全局记忆与错误学习单文件存储, 归并/LRU清理/错误铭记
蒸馏提示词优化 —— 面向多任务共性学习 + 最短执行路径提效 + 关键配置复用
"""
import json
import os
import threading
import time
import copy
from .config import GLOBAL_DIR, LESSON_MERGE_SIM, LESSON_TTL_DAYS
from .logger import get_logger
from .similarity import TextSimilarity
from .prompts import LESSON_DISTILL_PROMPT

log = get_logger("lessons")


class LessonStore:
    """记录结构: {ts, type(note|lesson|error), task, text, count, last_used}
    - 同类信息归并: 相似度超阈值时合并(count+1, 刷新时间戳)
    - LRU 清理    : 长期未用且低频的 note/lesson 自动清理
    - 错误铭记    : type=error 永不清理"""

    def __init__(self):
        self.dir = os.path.join(GLOBAL_DIR, "memory")
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, "lessons.jsonl")
        self._lock = threading.Lock()

    def _read_all(self):
        items = []
        if os.path.isfile(self.path):
            with open(self.path, encoding="utf-8", errors="replace") as f:
                for ln in f:
                    try:
                        items.append(json.loads(ln))
                    except json.JSONDecodeError as _e:
                        log.warning("忽略异常(omni_agent/lessons.py:37): %s: %s", type(_e).__name__, _e)
                        continue
        return items

    def _write_all(self, items):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    def add(self, text, kind="lesson", task="", count=1):
        """写入一条知识; 与既有同类型知识相似度超阈值时归并而非新增"""
        text = text.strip()[:300]
        if not text:
            return "empty"
        now = time.strftime("%Y-%m-%d %H:%M")
        with self._lock:
            items = self._read_all()
            for it in items:
                if it.get("type") == kind and \
                        TextSimilarity.similar(it.get("text", ""), text) >= LESSON_MERGE_SIM:
                    it["count"] = it.get("count", 1) + 1
                    it["last_used"] = now
                    if len(text) > len(it.get("text", "")):
                        it["text"] = text   # 保留信息量更大的表述
                    self._write_all(items)
                    return "merged"
            items.append({"ts": now, "type": kind, "task": task[:80],
                          "text": text, "count": count, "last_used": now})
            self._write_all(items)
        return "added"

    def prune(self, max_items=100):
        """LRU + TTL 清理, error 永不清理"""
        with self._lock:
            items = self._read_all()
            cutoff = time.time() - LESSON_TTL_DAYS * 86400
            kept = []
            for it in items:
                if it.get("type") == "error":
                    kept.append(it)
                    continue
                try:
                    last = time.mktime(time.strptime(it.get("last_used", it.get("ts", "")),
                                                     "%Y-%m-%d %H:%M"))
                except (ValueError, OverflowError):
                    last = time.time()
                if last >= cutoff or it.get("count", 1) >= 3:
                    kept.append(it)
            if len(kept) > max_items:
                kept.sort(key=lambda x: (x.get("type") == "error",
                                         x.get("count", 1), x.get("last_used", "")),
                          reverse=True)
                kept = kept[:max_items]
            removed = len(items) - len(kept)
            if removed:
                self._write_all(kept)
        return removed

    def load(self, limit=6000):
        """供系统提示注入, 同时刷新 last_used 实现 LRU。
        按使用频次排序, 优先展示高频复用经验(最短路径/关键配置/共性法则)"""
        with self._lock:
            items = self._read_all()
            if not items:
                return "(暂无跨会话记忆, 首个任务完成后将自动蒸馏经验)"
            now = time.strftime("%Y-%m-%d %H:%M")
            errors = [it for it in items if it.get("type") == "error"]
            others = sorted((it for it in items if it.get("type") != "error"),
                            key=lambda x: (x.get("count", 1), x.get("last_used", "")),
                            reverse=True)
            for it in errors[-10:] + others[:15]:
                it["last_used"] = now   # 被注入即视为被使用 → LRU 刷新
            self._write_all(items)
        parts = []
        if errors:
            parts.append("##  必须铭记的历史错误(永不遗忘, 规划方案时必须主动绕开)\n" + "\n".join(
                f"- [{e.get('ts', '')}] {e.get('text', '')}" for e in errors[-10:]))
        if others:
            parts.append("## 跨任务复用经验(按使用频次排序: 最短路径/关键配置/共性法则)\n" + "\n".join(
                f"- ({it.get('count', 1)}次) {it.get('text', '')}" for it in others[:10]))
        return "\n\n".join(parts)[:limit]

    def learn_async(self, llm, messages, task_brief, error_memory=None):
        t = threading.Thread(target=self._learn,
                             args=(llm, copy.deepcopy(messages), task_brief, list(error_memory or [])),
                             daemon=True)
        t.start()
        return t

    def _learn(self, llm, messages, task_brief, error_memory):
        try:
            for e in error_memory:  # 失败记录直接持久化为 error 知识
                self.add(f"{e.get('tool', '')}: {e.get('brief', '')[:200]}",
                         kind="error", task=task_brief)
            # 使用优化的蒸馏提示词(共性学习/最短路径/关键配置复用)
            if messages and messages[0].get("role") == "system":
                messages[0] = {"role": "system", "content": LESSON_DISTILL_PROMPT}
            else:
                messages.insert(0, {"role": "system", "content": LESSON_DISTILL_PROMPT})
            messages.append({"role": "user", "content": f"""当前任务: {task_brief}
---
##以下是完整对话过程(含工具调用与结果), 请从中蒸馏跨任务可复用的经验:"""})
            r = llm.chat(messages, stream=False, internal=True)
            lesson = (r["content"] or "").strip()
            if lesson and lesson.upper() != "NONE" and len(lesson) >= 8:
                st = self.add(lesson, kind="lesson", task=task_brief)
                log.info("lesson distilled(%s): %s", st, lesson[:100])
            else:
                log.info("lesson distill: no reusable knowledge (task=%s)",
                         task_brief[:50])
            # 重新排位并清理
            self.prune()
        except Exception as e:   # 后台学习失败不影响主流程, 但必须留痕
            log.warning("lesson learn failed: %s: %s", type(e).__name__, e)

    def append(self, note):
        """用户手动追加全局记忆"""
        return self.add(note, kind="note", task="(用户手动记录)", count=999)