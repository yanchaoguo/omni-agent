# -*- coding: utf-8 -*-
"""全局配置常量(环境变量优先级高于内置默认值)
加载顺序: 用户级环境变量(~/.haisnap/env.json, 同名覆盖) → os.environ → 内置默认"""
import os

from .envstore import EnvStore

EnvStore.apply()   # 用户级环境变量最先注入(同名覆盖进程变量)

__version__ = "4.1"

# ---- 基础语言模型接入 ----
API_KEY = os.environ.get("HAISNAP_API_KEY", "")
BASE_URL = os.environ.get("HAISNAP_BASE_URL",
                          "https://dashscope.aliyuncs.com/compatible-mode/v1")
MODEL = os.environ.get("HAISNAP_MODEL", "qwen3-max")  # qwen-max 不输出思考(reasoning恒空), 默认改用支持思考透传的 qwen3-max

# ---- 视觉模型独立接入(独立 Key/URL, 未配置时继承基础模型) ----
VISION_MODEL = os.environ.get("HAISNAP_VISION_MODEL", "qwen-vl-max")
VISION_API_KEY = os.environ.get("HAISNAP_VISION_API_KEY", "")   # 留空=继承基础模型
VISION_BASE_URL = os.environ.get("HAISNAP_VISION_BASE_URL", "")  # 留空=继承基础模型

# ---- UniFuncs 聚合搜索/网页阅读(web_search/web_fetch 首选通道) ----
UNIFUNCS_BASE = os.environ.get("HAISNAP_UNIFUNCS_BASE",
                               "https://api.unifuncs.com/api")
UNIFUNCS_API_KEY = os.environ.get(
    "HAISNAP_UNIFUNCS_KEY",
    "")

# ---- Prompt Caching (Claude cache_control 参数支持) ----
# auto = 仅当模型名含 claude 时自动启用; on = 强制启用; off = 关闭
PROMPT_CACHE = os.environ.get("HAISNAP_PROMPT_CACHE", "auto")
PROMPT_CACHE_TTL = os.environ.get("HAISNAP_PROMPT_CACHE_TTL", "5m")  # 5m 或 1h

# ---- Agent 行为 ----
MAX_AGENT_TURNS = 500           # 单个任务最大工具调用轮数(从40提升至100)
BASH_TIMEOUT = 120              # 命令执行超时(秒)
WEB_FETCH_TIMEOUT = 30          # web_fetch 默认抓取超时(秒), 工具调用可传 timeout 覆盖
MAX_TOOL_OUTPUT = 12000         # 工具输出截断长度
MAX_FILE_READ = 60000           # 单文件读取截断长度
ASK_DEFAULT_TIMEOUT = 120       # ask_user_question 倒计时确认默认秒数

# ---- 图片生成并行度(批量任务并行执行) ----
IMAGE_BATCH_WORKERS = 6         # 批量图片生成最大并行数

# ---- 目录与文件 ----
MEMORY_FILE = "HAISNAP.md"      # 项目记忆文件
SETTINGS_DIR = ".haisnap"       # 项目配置目录(hooks/mcp/connectors/快照/归档)
# v6.0阻塞性修复: 原 expanduser(".haisnap") 不以 ~ 开头无法展开,
# 实际落在 CWD 相对目录 —— 换目录启动后全局配置/MCP/环境变量全部"丢失";
# 现修正为文档声明的用户主目录 ~/.haisnap(可用 HAISNAP_HOME 环境变量覆盖)
GLOBAL_DIR = os.environ.get(
    "HAISNAP_HOME",
    os.path.join(os.path.expanduser("~"), ".haisnap"))  # 全局目录
PROJECTS_DIR = os.environ.get(
            "HAISNAP_PROJECTS_ROOT",
            os.path.join(GLOBAL_DIR, "haisnap_projects"))
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             ".haisnap", "dist", "build"}

# ---- 上下文压缩与记忆 ----
LESSON_MERGE_SIM = 0.6           # lessons.jsonl 同类知识归并相似度阈值
LESSON_TTL_DAYS = 30             # 非 error 知识长期不用的清理周期(天)
ERROR_MARKERS = ("[错误]", "[失败]", "[超时]", "[工具执行异常]", "[已拒绝]",
                 "[被 PreToolUse hook 阻止]", "Traceback (most recent call last)")

# ---- 定时任务调度 ----
SCHEDULE_FILE_DEFAULT = os.path.join(GLOBAL_DIR, "schedule.json")  # 默认调度配置
SCHEDULER_DIR = os.path.join(GLOBAL_DIR, "scheduler")              # 调度器状态目录
SCHEDULER_HEARTBEAT_SEC = 5      # 调度器心跳写入间隔(秒)
SCHEDULER_STALE_SEC = 30         # 心跳超过该秒数视为调度器离线

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")  # 去除 Agent 标识, 修复知乎/CSDN 等站点 403