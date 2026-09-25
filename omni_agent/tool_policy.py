# -*- coding: utf-8 -*-
"""工具策略引擎(v3.3 新增): run_task 级工具黑白名单 + 任务类型自动匹配

策略来源(优先级从高到低):
1. run_task(tool_policy=...) 显式传入(Web UI 每次任务可独立指定)
2. CLI --allow-tools / --deny-tools / --tool-policy auto (或 REPL /policy 命令)
3. settings.json 的 tool_policy 配置

规则:
- deny(黑名单) 优先级最高, 命中即拒绝(Schema 过滤 + 执行期双重拦截)
- allow(白名单) 非空时, 仅允许 白名单 ∪ 核心工具(规划/提问/交付不可或缺)
- auto 模式按任务类型(会话固化类型 > 意图关键词推断)自动匹配预设工具集
"""

# 核心工具: 规划/澄清/交付, 除非显式加入黑名单, 否则始终保留
CORE_TOOLS = {"todo_write", "ask_user_question", "send_user_msg"}

READONLY_FS = {"read_file", "glob_files", "grep_search"}
WEB_TOOLS = {"web_search", "web_fetch", "web_screenshot"}
FILE_WRITE = {"write_file", "edit_file", "multi_edit"}

# 任务类型 → 预设工具集(None = 全量不限制)
TASK_TYPE_PRESETS = {
    "research": READONLY_FS | WEB_TOOLS | FILE_WRITE | {"swarm_tasks", "vision",
                                                        "connector_push"},
    "writing":  READONLY_FS | WEB_TOOLS | FILE_WRITE | {"vision", "connector_push"},
    "data":     READONLY_FS | WEB_TOOLS | FILE_WRITE | {"bash", "swarm_tasks", "vision"},
    "coding":   None,
    "design":   READONLY_FS | FILE_WRITE | WEB_TOOLS | {"bash", "deploy", "vision"},
    "ops":      READONLY_FS | {"bash", "deploy", "checkpoint", "connector_push"},
    # v4.0.0: chat 类任务 —— 纯文本问答, 开放常用工具: bash/网络检索/网页阅读
    # 及只读文件(禁写文件); 与前端对话模式默认勾选(bash/web_search/web_fetch/
    # ask_user_question)保持一致
    "chat":     READONLY_FS | WEB_TOOLS | {"bash"},
    "general":  None,
}

# 意图关键词 → 任务类型(auto 模式下会话类型缺失时的降级推断)
_INTENT_HINTS = [
    ("research", ("调研", "检索", "报告", "情报", "市场", "对比分析", "research", "survey")),
    ("ops",      ("部署", "运维", "服务器", "监控", "上线", "deploy", "docker", "nginx")),
    ("writing",  ("写作", "文章", "文案", "公众号", "润色", "翻译", "演讲稿")),
    ("data",     ("数据分析", "excel", "csv", "爬取", "统计", "图表", "数据清洗")),
    ("design",   ("设计", "界面", "海报", "复刻", "原型", "ui稿")),
    ("coding",   ("代码", "开发", "脚本", "bug", "重构", "程序", "接口", "算法")),
    ("chat",     ("是什么", "为什么", "怎么理解", "解释一下", "翻译一下", "聊聊",
                  "问一下", "what is", "how to", "explain")),
]


class ToolPolicy:
    def __init__(self, allow=None, deny=None, auto=False, task_type=""):
        self.allow = {str(x).strip() for x in (allow or []) if str(x).strip()}
        self.deny = {str(x).strip() for x in (deny or []) if str(x).strip()}
        self.auto = bool(auto)
        self.task_type = (task_type or "").strip().lower()
        self.matched_type = ""    # auto 模式实际命中的任务类型
        self._resolved = None     # 生效白名单集合(None = 不限制)

    @classmethod
    def from_config(cls, cfg):
        """从 settings.json 的 tool_policy 节构造; 配置缺失/为空返回 None"""
        if not cfg or not isinstance(cfg, dict):
            return None
        p = cls(allow=cfg.get("allow"), deny=cfg.get("deny"),
                auto=(cfg.get("mode") == "auto"), task_type=cfg.get("task_type", ""))
        return None if p.is_noop() else p

    @staticmethod
    def infer_task_type(intent):
        low = (intent or "").lower()
        for ttype, kws in _INTENT_HINTS:
            if any(k in low for k in kws):
                return ttype
        return "general"

    def is_noop(self):
        return not (self.allow or self.deny or self.auto)

    def resolve(self, task_type="", intent=""):
        """计算生效白名单(在任务身份固化后调用); 返回 self 便于链式使用"""
        base = None
        if self.auto:
            self.matched_type = (self.task_type or task_type or "").lower() \
                or self.infer_task_type(intent)
            preset = TASK_TYPE_PRESETS.get(self.matched_type)
            if preset is not None:
                base = set(preset) | CORE_TOOLS
        if self.allow:
            allowset = self.allow | CORE_TOOLS
            base = allowset if base is None else (base & allowset) | CORE_TOOLS
        self._resolved = base
        return self

    def permits(self, name):
        """执行期拦截判定: 黑名单最高优先, 其次白名单(未 resolve 时仅黑名单生效)"""
        if name in self.deny:
            return False
        if self._resolved is None:
            return True
        return name in self._resolved

    def filter_schemas(self, schemas):
        """按策略过滤工具 Schema(模型侧不可见被禁工具, 从源头减少无效调用)"""
        return [t for t in schemas if self.permits(t["function"]["name"])]

    def describe(self):
        parts = []
        if self.auto:
            parts.append(f"auto(任务类型={self.matched_type or self.task_type or '待定'})")
        if self.allow:
            parts.append("白名单=" + ",".join(sorted(self.allow)))
        if self.deny:
            parts.append("黑名单=" + ",".join(sorted(self.deny)))
        if self._resolved is not None:
            parts.append(f"生效工具数={len(self._resolved)}")
        return " | ".join(parts) or "不限制(全量工具)"

    def to_dict(self):
        return {"allow": sorted(self.allow), "deny": sorted(self.deny),
                "mode": "auto" if self.auto else "manual",
                "task_type": self.task_type, "matched_type": self.matched_type}
