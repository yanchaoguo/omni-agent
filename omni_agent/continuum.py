# -*- coding: utf-8 -*-
"""任务续航引擎(Next-Best-Action) —— 行动型用户粘性策略
任务完成后基于本次任务上下文, 由 LLM 异步蒸馏 2~3 条"下一步最佳行动"建议,
以可点击芯片呈现在任务完成卡片下方, 一键点击即提交续跑, 形成任务链闭环。
设计依据: 相比被动展示型粘性(徽章/足迹), 行动链直接缩短"想法→下一个任务"
的路径, 把每次任务终点变为下一次任务起点(对标 Notion AI / 飞书智能伙伴的
next-step 推荐范式) —— 上下文感知的个性化推荐(创新性) + 直接驱动续用复访
(重要性)。异步执行, LLM 失败/超时不影响主流程。
"""
import json
import re
import threading

from .logger import get_logger

log = get_logger("continuum")

_PROMPT = (
    "你是任务续航推荐器。基于用户刚完成的任务, 提出2~3条用户下一步最可能"
    "需要、且本智能体能直接执行的具体行动建议。\n要求:\n"
    "- 每条为一句可直接发送执行的任务指令(祈使句, ≤40字), 具体到对象与动作\n"
    "- 与刚完成的任务强相关(深化/延伸/验证/复用成果), 禁止泛泛而谈\n"
    "- 仅输出 JSON 字符串数组, 如 [\"...\", \"...\"], 不要输出其他任何文字")


class ContinuumEngine:
    """任务续航引擎: 后台线程蒸馏下一步行动, 经 emit 推送前端芯片"""

    def __init__(self, llm, emit=None):
        self.llm = llm
        self.emit = emit

    def suggest_async(self, user_input, summary=""):
        t = threading.Thread(target=self._suggest,
                             args=(user_input or "", summary or ""),
                             daemon=True)
        t.start()
        return t

    def _suggest(self, user_input, summary):
        try:
            msgs = [{"role": "system", "content": _PROMPT},
                    {"role": "user", "content":
                     f"刚完成的任务: {user_input[:600]}\n"
                     f"完成结果摘要: {summary[:600]}"}]
            r = self.llm.chat(msgs, stream=False, internal=True)
            acts = self._parse((r.get("content") or "") if isinstance(r, dict) else "")
            if acts and self.emit:
                self.emit("next_actions", {"actions": acts})
        except Exception as e:   # 兜底: 推荐失败绝不影响任务收尾
            log.warning("续航建议生成失败(不影响任务): %s", e)

    @staticmethod
    def _parse(text):
        """解析 LLM 输出的 JSON 数组(容忍代码块包裹/前后杂文)"""
        m = re.search(r"\[.*\]", text, re.S)
        if not m:
            return []
        try:
            arr = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(arr, list):
            return []
        return [str(a).strip()[:80] for a in arr
                if isinstance(a, str) and str(a).strip()][:3]
