# -*- coding: utf-8 -*-
"""垂直领域异构执行引擎 

设计目标: 同一 Agent 内核在不同垂直场景下呈现"异构"的执行行为 ——
按用户当前意图将任务路由到垂直领域画像(Profile), 为每个领域注入差异化的
执行策略(工具偏好 / 验收标准 / 领域执行准则), 本地加权关键词评分(<1ms,
零 LLM 开销), 任务前置阶段完成路由并热注入系统提示词。

该能力取代原时间轴「继续」按钮: 从"手工指哪续跑"升级为"领域自适应执行"。
"""


def _p(pid, name, keywords, brief, rules):
    return {"id": pid, "name": name, "keywords": keywords,
            "strategy_brief": brief, "rules": rules}


# 关键词权重: 2=强特征词, 1=弱特征词(允许中英文混合命中)
PROFILES = [
    _p("data_analysis", "数据分析与建模",
       {"数据分析": 2, "统计": 2, "pandas": 2, "建模": 2, "excel": 1, "csv": 1,
        "图表": 1, "可视化": 1, "透视": 2, "回归": 2, "聚类": 2, "指标": 1,
        "报表": 1, "numpy": 2, "数据清洗": 2, "相关性": 2},
       "数据口径可溯源 · 先探查后建模 · 结论附样本量",
       ["工具偏好: run_shell(pandas/numpy) > read_file; 图表用 matplotlib 落盘 png 后交付",
        "验收标准: 数据口径可溯源、统计结论附样本量与置信度、异常值处理策略显式说明",
        "执行准则: 先探查数据结构(head/dtypes/缺失率)再分析建模; 严禁虚构数据填充空缺"]),
    _p("web_frontend", "前端与可视化开发",
       {"网页": 2, "前端": 2, "html": 2, "css": 1, "react": 2, "vue": 2,
        "页面": 1, "界面": 1, "组件": 1, "落地页": 2, "官网": 2, "大屏": 2,
        "tailwind": 2, "动画": 1, "响应式": 2, "ui": 1},
       "移动优先响应式 · 真实数据渲染 · 浏览器验证后交付",
       ["工具偏好: write_file 产出单入口 index.html; 完成后本地启动+页面日志验证",
        "验收标准: 无控制台报错、响应式断点完整、禁止占位图与死链接",
        "执行准则: 视觉基调先行(配色/字体/层次), 组件化组织代码, CDN 依赖须锁版本"]),
    _p("backend_api", "后端服务与API",
       {"接口": 2, "api": 2, "后端": 2, "服务": 1, "数据库": 2, "flask": 2,
        "fastapi": 2, "express": 2, "鉴权": 2, "路由": 1, "sql": 2,
        "中间件": 2, "微服务": 2, "websocket": 2},
       "契约先行 · 启动自检 · 错误码语义化",
       ["工具偏好: write_file 分层组织(路由/服务/存储); run_shell 启动后 curl 自测核心端点",
        "验收标准: 端点全部可达、错误码语义化、并发安全(锁/原子写)、无阻塞死循环",
        "执行准则: 先定义 API 契约再实现; 敏感配置走环境变量, 严禁硬编码密钥"]),
    _p("document", "文档与报告写作",
       {"报告": 2, "文档": 2, "word": 1, "ppt": 1, "总结": 1, "方案": 1,
        "markdown": 1, "论文": 2, "白皮书": 2, "手册": 2, "计划书": 2,
        "纪要": 2, "简历": 2, "合同": 1},
       "结构先行 · 数据必附引用 · 结论可验证",
       ["工具偏好: web_search 采集→sources 登记→write_file 产出(引用角标自动渲染)",
        "验收标准: 结构完整(摘要/正文/结论)、关键数据 100% 带引用序号、无未经证实断言",
        "执行准则: 先列大纲经确认再展开; 时间敏感数据优先 90 日内信源"]),
    _p("crawler", "数据采集与爬虫",
       {"爬虫": 2, "采集": 2, "抓取": 2, "爬取": 2, "监控": 1, "抓包": 2,
        "解析网页": 2, "批量下载": 2, "scrapy": 2, "beautifulsoup": 2,
        "selenium": 2, "playwright": 1},
       "合规限速 · 结构化落盘 · 断点可续",
       ["工具偏好: web_fetch/browser 优先; 批量任务并发受控并携带限速间隔",
        "验收标准: 数据结构化落盘(csv/json)、字段完整率标注、采集时间戳留痕",
        "执行准则: 遵守 robots 与法律边界; 失败重试≤2次后换通道, 禁止无限重试"]),
    _p("devops", "运维与自动化脚本",
       {"脚本": 2, "定时": 2, "部署": 2, "自动化": 2, "shell": 2, "cron": 2,
        "备份": 2, "日志": 1, "监控告警": 2, "docker": 2, "ci": 1, "流水线": 2,
        "批处理": 2},
       "幂等可重入 · 干跑先行 · 破坏性操作必审批",
       ["工具偏好: run_shell 小步执行逐段验证; 脚本落盘前先 dry-run 干跑",
        "验收标准: 脚本幂等可重入、异常路径有退出码、关键操作留审计日志",
        "执行准则: rm/覆盖/迁移等破坏性操作先备份并请求审批; 路径一律用绝对路径"]),
    _p("research", "调研与信息整合",
       {"调研": 2, "研究": 1, "对比": 1, "分析一下": 1, "了解": 1, "现状": 1,
        "趋势": 2, "竞品": 2, "行业": 1, "市场": 1, "评测": 2, "盘点": 2,
        "汇总": 1, "查一下": 2},
       "多源交叉验证 · 时效优先 · 观点与事实分离",
       ["工具偏好: web_search 多关键词并行→web_fetch 深读→sources 登记溯源",
        "验收标准: 关键结论≥2个独立信源交叉验证、明确标注数据时点、观点与事实分离",
        "执行准则: 优先 90 日内信源; 检索后仍不确定的事实明确告知用户, 严禁编造"]),
]

_GENERAL = {"id": "general", "name": "通用执行", "strategy_brief":
            "标准执行流程 · 小步验证 · 交付前自检",
            "rules": ["按标准流程执行: 理解意图→规划→小步实施→验证→交付",
                      "关键产出交付前运行/预览自检一次"]}

# 任务类型(LLM 固化的 task_type)→ 领域加成
_TASK_TYPE_BONUS = {
    "data": "data_analysis", "analysis": "data_analysis",
    "web": "web_frontend", "frontend": "web_frontend", "design": "web_frontend",
    "api": "backend_api", "backend": "backend_api", "code": "backend_api",
    "doc": "document", "report": "document", "writing": "document",
    "crawler": "crawler", "spider": "crawler",
    "ops": "devops", "script": "devops", "automation": "devops",
    "research": "research", "search": "research",
}


class DomainRouter:
    """本地加权评分路由器: classify() 返回领域画像(含注入用策略文本)"""

    @staticmethod
    def classify(text, task_type=""):
        low = (text or "").lower()
        scores = {}
        for prof in PROFILES:
            s = sum(w for kw, w in prof["keywords"].items() if kw in low)
            if s:
                scores[prof["id"]] = s
        # task_type 加成(LLM 固化的任务类型作为领域先验)
        tt = (task_type or "").lower()
        for hint, pid in _TASK_TYPE_BONUS.items():
            if hint in tt:
                scores[pid] = scores.get(pid, 0) + 2
                break
        if not scores:
            return DomainRouter._pack(_GENERAL, 30)
        best_id = max(scores, key=lambda k: scores[k])
        best = next(p for p in PROFILES if p["id"] == best_id)
        # 置信度: 基线40 + 每分8, 封顶96; 与次高分差距小则降档
        ranked = sorted(scores.values(), reverse=True)
        conf = min(96, 40 + ranked[0] * 8)
        if len(ranked) > 1 and ranked[0] - ranked[1] <= 1:
            conf = max(35, conf - 15)
        return DomainRouter._pack(best, conf)

    @staticmethod
    def _pack(prof, confidence):
        inj = ("【垂直领域异构执行引擎 v8.0 · 当前领域: %s (置信 %d%%)】\n%s"
               % (prof["name"], confidence,
                  "\n".join("- " + r for r in prof["rules"])))
        return {"id": prof["id"], "name": prof["name"],
                "confidence": confidence,
                "strategy_brief": prof.get("strategy_brief", ""),
                "injection": inj}
