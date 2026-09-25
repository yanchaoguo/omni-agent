# -*- coding: utf-8 -*-
"""Reflexion 自愈反思引擎(v5.9 创新功能, 对标 Reflexion/Voyager 论文思想)

竞争力定位(Agent 领域):
- 传统 Agent 遇到连续工具失败时会盲目重试或原地打转, 消耗大量轮次与 token;
- 本引擎在「连续失败 ≥ 阈值」时自动暂停执行流, 调用 LLM 对失败轨迹做
  结构化归因(错误模式 → 根因假设 → 纠偏策略), 并将反思结论作为高优先级
  指令注入上下文, 使模型在下一轮立即切换策略;
- 反思结论同时落盘到跨会话经验库(lessons), 同类错误跨任务免疫;
- 全程事件化上报, 网页端时间线以「自愈反思卡片」透明展示归因过程。

触发条件与开关(走统一配置链 env > 项目 > 全局 > config 默认):
- reflexion.enabled          (HAISNAP_REFLEXION, 默认 on)
- reflexion.error_threshold  (HAISNAP_REFLEXION_THRESHOLD, 默认 2 次连续失败)
"""
import time

from .config import ERROR_MARKERS
from .logger import get_logger
from .settings import S

log = get_logger("reflexion")

REFLECT_PROMPT = """你是智能体的失败归因与自愈策略专家。以下是刚刚连续失败的工具调用轨迹。
请输出简洁的结构化反思(不超过200字), 格式:
【错误模式】一句话概括失败共性
【根因假设】最可能的原因(路径错误/参数错误/依赖缺失/权限/网络/方案本身不可行等)
【纠偏策略】下一步应该改用的具体做法(可执行, 禁止建议原样重试)
只输出以上三段, 不要客套话。"""


class ReflexionEngine:
    """连续失败监测 → LLM 归因反思 → 纠偏策略注入 + 经验落盘"""

    def __init__(self, llm, memory=None, emit=None):
        self.llm = llm
        self.memory = memory          # LessonStore(可选): 反思结论跨会话落盘
        self.emit = emit              # fn(event, payload): 事件上报(Web 卡片)
        self.consecutive = 0          # 连续失败计数
        self.trail = []               # 最近失败轨迹 [{tool, brief}]
        self.reflections = 0          # 本会话累计反思次数
        self.last_strategy = ""

    # ---- 配置(运行时热读取) ----
    @staticmethod
    def enabled():
        return S.get_bool("reflexion.enabled", True)

    @staticmethod
    def threshold():
        return max(1, S.get_int("reflexion.error_threshold", 2))

    # ---- 观测每次工具结果 ----
    def observe(self, tool, result):
        """记录工具执行结果; 返回 True 表示达到反思触发条件"""
        failed = any(m in str(result) for m in ERROR_MARKERS)
        if failed:
            self.consecutive += 1
            self.trail.append({"tool": tool, "brief": str(result)[:300]})
            self.trail = self.trail[-6:]
        else:
            self.consecutive = 0
            self.trail = []
        return (self.enabled() and self.consecutive >= self.threshold())

    def reset(self):
        self.consecutive = 0
        self.trail = []

    # ---- 归因反思 ----
    def reflect(self, task_brief=""):
        """调用 LLM 对失败轨迹做归因, 返回注入上下文的纠偏指令(失败时返回空)"""
        if not self.trail:
            return ""
        trail_txt = "\n".join(
            f"{i + 1}. 工具 {t['tool']} → {t['brief']}"
            for i, t in enumerate(self.trail))
        try:
            r = self.llm.chat(
                [{"role": "system", "content": REFLECT_PROMPT},
                 {"role": "user", "content":
                  f"当前任务: {task_brief[:200]}\n连续失败轨迹:\n{trail_txt}"}],
                stream=False, internal=True)
            strategy = (r.get("content") or "").strip()
        except Exception as e:
            log.warning("reflexion llm failed: %s: %s", type(e).__name__, e)
            return ""
        if not strategy:
            return ""
        self.reflections += 1
        self.last_strategy = strategy
        # 跨会话经验落盘: 同类错误后续任务免疫
        if self.memory:
            try:
                self.memory.add(strategy[:400], kind="error",
                                task=f"[Reflexion自愈] {task_brief[:60]}")
            except Exception as _e:
                log.warning("reflexion lesson save failed: %s", _e)
        if self.emit:
            try:
                self.emit("reflexion", {
                    "strategy": strategy,
                    "errors": [t["brief"][:120] for t in self.trail],
                    "count": self.consecutive,
                    "ts": time.strftime("%H:%M:%S")})
            except Exception as _e:
                log.warning("reflexion emit failed: %s", _e)
        log.info("reflexion triggered: consecutive=%d strategy=%s",
                 self.consecutive, strategy[:100])
        self.reset()   # 反思后复位, 给纠偏策略一个观察窗口
        return ("[Reflexion 自愈反思 · 系统自动注入]\n"
                "检测到连续工具失败, 已完成失败归因, 你必须按以下纠偏策略调整方案, "
                "严禁按原方案重试:\n" + strategy)
