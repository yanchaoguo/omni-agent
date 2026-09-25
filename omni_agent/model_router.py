# -*- coding: utf-8 -*-
"""模型管理与多模智能路由策略 (Model Registry + Smart Routing)

设计目标:
- 模型注册表: 统一管理可用模型池(名称/端点/能力标签), 支持 settings.json /
  环境变量 / Web API 三通道配置, 敏感信息不回显明文;
- 多模智能路由: 按请求特征(内部调用/上下文长度/任务类型/领域)在模型池中
  自动选择最优模型, 本地规则评估零 LLM 开销;
- 决策透明: 每次路由决策输出关键日志(模型变更/路由原因均可追溯)。

路由优先级(高→低):
1. 调用方显式指定 model 参数 —— 路由器不介入(强制直通);
2. internal 内部调用(任务命名/上下文蒸馏/经验学习/续航建议) → light_model;
3. 上下文估算 token 超过 long_context_tokens 阈值 → long_model;
4. 任务类型/领域命中代码特征(coding/backend_api/web_frontend) → code_model;
5. 默认 → 主模型(model.model)。

专用槽位留空时自动回退主模型, 路由行为完全无副作用。
"""
import threading

from .logger import get_logger
from .settings import S

log = get_logger("router")

# 代码类特征: LLM 固化的 task_type 与领域路由 id
_CODE_TASK_TYPES = {"coding", "code", "ops"}
_CODE_DOMAINS = {"backend_api", "web_frontend", "devops"}


class ModelRouter:
    """多模智能路由器: route() 返回 (模型名, 路由原因)"""

    # 类级端点覆盖注册表: 模型名 -> {"api_key": "", "base_url": ""}
    # (模型池中的模型可声明独立端点, 请求层按模型名查表覆盖)
    _endpoint_overrides = {}
    _ov_lock = threading.Lock()
    _last_decision = {"model": "", "reason": "", "ts": 0}

    def __init__(self):
        self._reload_pool()

    # ---- 配置读取(热读取: 每次路由实时走配置链, Web 修改即刻生效) ----
    @staticmethod
    def enabled():
        return S.get_bool("model.routing.enabled", True)

    @staticmethod
    def _cfg():
        return {
            "enabled": S.get_bool("model.routing.enabled", True),
            "light_model": S.get_str("model.routing.light_model").strip(),
            "code_model": S.get_str("model.routing.code_model").strip(),
            "long_model": S.get_str("model.routing.long_model").strip(),
            "long_context_tokens": S.get_int(
                "model.routing.long_context_tokens", 30000),
        }

    def _reload_pool(self):
        """端点覆盖改为 endpoint_override() 热读取, 此处仅保留兼容占位"""
        return

    # ---- 端点覆盖查询(llm._request 按模型名调用) ----
    @classmethod
    def endpoint_override(cls, model):
        """热读取模型池(settings.json 内置 + Web 登记合并),
        Web 端登记/修改端点后无需重启即时生效"""
        name = (model or "").strip()
        if not name:
            return None
        for m in S.get_pool():
            if m.get("name") == name:
                ov = {"api_key": m.get("api_key") or "",
                      "base_url": m.get("base_url") or ""}
                return ov if (ov["api_key"] or ov["base_url"]) else None
        return None

    # ---- 上下文长度估算(与 compressor 同口径: 字符数/2.6 近似) ----
    @staticmethod
    def estimate_tokens(messages):
        total = 0
        for m in messages or []:
            c = m.get("content")
            if isinstance(c, str):
                total += len(c)
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and isinstance(b.get("text"), str):
                        total += len(b["text"])
            for tc in m.get("tool_calls") or []:
                try:
                    total += len(tc.get("function", {}).get("arguments") or "")
                except AttributeError:
                    pass
        return int(total / 2.6)

    # ---- 路由决策 ----
    def route(self, messages, internal=False, task_type="", domain=""):
        """返回 (model, reason)。路由关闭或专用槽位为空时回退主模型。"""
        # 主模回退值与 config.py/settings.py 默认 "qwen3-max" 对齐,
        # 修复原 "qwen3.7-max" 与主模型默认不一致导致路由槽位空时回退到不存在的模型
        primary = S.get_str("model.model") or "qwen3-max"
        cfg = self._cfg()
        if not cfg["enabled"]:
            return primary, "路由已关闭→主模型"
        # ① 内部轻量调用(命名/蒸馏/摘要/续航建议)
        if internal and cfg["light_model"]:
            return self._decide(cfg["light_model"], "内部调用→轻量模型")
        # ② 长上下文
        est = self.estimate_tokens(messages)
        if cfg["long_model"] and est >= cfg["long_context_tokens"]:
            return self._decide(
                cfg["long_model"],
                f"上下文约{est}tok≥阈值{cfg['long_context_tokens']}→长上下文模型")
        # ③ 代码类任务/领域
        tt, dm = (task_type or "").lower(), (domain or "").lower()
        if cfg["code_model"] and (tt in _CODE_TASK_TYPES or dm in _CODE_DOMAINS):
            return self._decide(
                cfg["code_model"], f"代码类任务(task_type={tt or '-'}/"
                                   f"domain={dm or '-'})→代码强化模型")
        # ④ 默认主模型
        return self._decide(primary, "默认→主模型")

    @classmethod
    def _decide(cls, model, reason):
        import time as _t
        cls._last_decision = {"model": model, "reason": reason,
                              "ts": int(_t.time())}
        return model, reason

    # ---- 模型注册表(Web API /api/models 数据源) ----
    @staticmethod
    def list_models():
        """汇总所有已配置模型(主/备用/视觉/路由槽位/模型池), 去重保序"""
        cfg = ModelRouter._cfg()
        seen, out = set(), []

        def _add(name, role):
            n = (name or "").strip()
            if not n or n in seen:
                return
            seen.add(n)
            ov = ModelRouter.endpoint_override(n)
            out.append({"name": n, "role": role,
                        "endpoint": "独立端点" if ov else "主端点"})
        _add(S.get_str("model.model"), "主模型")
        _add(S.get_str("model.fallback_model"), "备用兜底")
        _add(S.get_str("model.vision_model"), "视觉理解")
        _add(cfg["code_model"], "路由·代码强化")
        _add(cfg["long_model"], "路由·长上下文")
        _add(cfg["light_model"], "路由·内部轻量")
        for m in S.get_pool():
            _add(m.get("name"), "模型池")
        return out

    @classmethod
    def describe_config(cls):
        """路由配置概览(供 /api/state 回显与前端配置面板展示)"""
        cfg = cls._cfg()
        cfg["last_decision"] = dict(cls._last_decision)
        return cfg
