# -*- coding: utf-8 -*-
"""内置工具定义(OpenAI Function Schema) v4.4
变更:
- 每个工具增加 title 字段(前端显示用, 不传递给 LLM)
- glob_files 增加 root_dir 参数(跨平台文件查找, 可扫描指定目录)
- load_skills 增加 npx 安装方式(source 格式: npx skills add url/path/name)
"""
from .config import ASK_DEFAULT_TIMEOUT, BASH_TIMEOUT, VISION_MODEL

TOOLS_SCHEMA = [
    {"type": "function", "title": "执行Shell命令", "function": {
        "name": "bash",
        "description": "在本机 shell 中执行命令并返回 stdout/stderr。用于运行程序、安装依赖、git 操作、验证结果等。工作目录为项目根目录。设置 run_in_background=true 可后台运行长驻进程(如启动服务), 立即返回任务ID与日志路径。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明版（必要时包含风险提醒）"},
            "command": {"type": "string", "description": "要执行的 shell 命令"},
            "timeout": {"type": "integer", "description": f"超时秒数, 默认 {BASH_TIMEOUT}"},
            "run_in_background": {"type": "boolean", "description": "后台运行(默认 false)。耗时任务或离线任务请设置为 true"}},
            "required": ["command"]}}},
    {"type": "function", "title": "读取文件", "function": {
        "name": "read_file",
        "description": "读取文本文件内容, 返回带行号的文本。仅支持文本类型文件: 图片/音频/视频/压缩包/办公文档等二进制类型会被拒绝(图片改用 vision 分析, 压缩包先用 bash 解压)。支持批量: 传 paths 数组可一次读取多个文件(最多10个, 多线程并行执行后汇总返回)。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明"},
            "path": {"type": "string", "description": "单个文件路径(相对工作目录或绝对路径)。与 paths 二选一"},
            "paths": {"type": "array", "items": {"type": "string"},
                       "description": "批量读取: 文件路径数组(最多10个, 并行执行)"}},
            "required": []}}},
    {"type": "function", "title": "写入文件", "function": {
        "name": "write_file",
        "description": "创建或整体覆盖写入文本文件, 自动创建父目录。写入 .html/.md 报告文件时, 正文引用可用 [[信源ID]] 角标与 ==高亮== 语法, 系统自动渲染悬停式信源卡片(默认不在结尾追加信源列表, 除非用户明确要求)。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明"},
            "path": {"type": "string", "description": "文件路径"},
            "content": {"type": "string", "description": "完整文件内容"},
            "with_sources": {"type": "boolean", "description": "报告类文件设为 true: 自动渲染引用角标为悬停式信源卡片(默认不在结尾追加文献列表)"}},
            "required": ["path", "content"]}}},
    {"type": "function", "title": "编辑文件", "function": {
        "name": "edit_file",
        "description": "对已有文件做精准字符串替换(old_string 必须在文件中唯一出现)。适合小范围修改, 大改用 write_file。每次编辑前必须先 read_file 读取该文件(系统会校验读取版本, 编辑后版本自动失效, 同一文件多次编辑时每次都需重新读取)。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明"},
            "path": {"type": "string"},
            "old_string": {"type": "string", "description": "被替换的原文(需带足够上下文保证唯一)"},
            "new_string": {"type": "string", "description": "替换后的新文本"}},
            "required": ["path", "old_string", "new_string"]}}},
    {"type": "function", "title": "批量编辑文件", "function": {
        "name": "multi_edit",
        "description": "对同一文件执行多处精准替换(批量编辑)。edits 数组中每项含 old_string/new_string, 按序执行。old_string 必须唯一出现。每次编辑前必须先 read_file 读取该文件(系统校验读取版本, 编辑后自动失效)。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明"},
            "path": {"type": "string", "description": "文件路径"},
            "edits": {"type": "array", "items": {"type": "object", "properties": {
                "old_string": {"type": "string"},
                "new_string": {"type": "string"}},
                "required": ["old_string", "new_string"]},
                "description": "替换数组(按序执行)"}},
            "required": ["path", "edits"]}}},
    {"type": "function", "title": "查找文件", "function": {
        "name": "glob_files",
        "description": "按通配符模式列出匹配的文件路径, 如 **/*.py、src/*.js。默认扫描当前工作目录, 也可通过 root_dir 指定其它目录(跨平台兼容 Windows/Linux/macOS 路径)。无匹配时返回空字符串。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明"},
            "pattern": {"type": "string", "description": "glob 模式"},
            "path": {"type": "string", "description": "扫描目录(可为空 默认会使用当前工作目录；支持当前项目下相对路径和决定路径。注意跨平台兼容)"},
            "max_results": {"type": "integer", "description": "最多返回数量, 默认50"}},
            "required": ["pattern"]}}},
    {"type": "function", "title": "正则搜索文件内容", "function": {
        "name": "grep_search",
        "description": "在文件内容中做正则搜索, 返回 文件:行号:内容。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明"},
            "pattern": {"type": "string", "description": "正则表达式"},
            "glob": {"type": "string", "description": "限定文件范围的 glob, 默认 **/*"},
            "max_results": {"type": "integer", "description": "默认50"}},
            "required": ["pattern"]}}},
    {"type": "function", "title": "任务清单管理", "function": {
        "name": "todo_write",
        "description": "维护当前任务清单以规划与追踪进度。计划模式下开始复杂任务前必须先写计划, 每完成一步都要主动更新状态。",
        "parameters": {"type": "object", "properties": {
            "todos": {"type": "array", "items": {"type": "object", "properties": {
                "content": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}},
                "required": ["content", "status"]}}},
            "required": ["todos"]}}},
    {"type": "function", "title": "向用户提问", "function": {
        "name": "ask_user_question",
        "description": f"向用户发起一组提问(支持批量), 收集澄清信息。每个问题可附选项列表; 内置 {ASK_DEFAULT_TIMEOUT}s 倒计时确认——超时未确认自动采用第一个选项(请把最推荐的选项放首位)。定时任务/无头模式下直接返回默认选项。",
        "parameters": {"type": "object", "properties": {
            "questions": {"type": "array", "items": {"type": "object", "properties": {
                "question": {"type": "string", "description": "问题或通知内容"},
                "options": {"type": "array", "items": {"type": "string"},
                            "description": "可选项列表(第一项应为最推荐的默认项)"}},
                "required": ["question"]},
                "description": "问题数组(支持一次提出多个问题)"},
            "timeout": {"type": "integer", "description": f"倒计时秒数, 默认 {ASK_DEFAULT_TIMEOUT}s"},
            "notify_only": {"type": "boolean", "description": "true=仅通知不等待回答"}},
            "required": ["questions"]}}},
    {"type": "function", "title": "推送交付物", "function": {
        "name": "send_user_msg",
        "description": "任务完成后向用户推送交付物并返回交付物卡片。支持在一次交付中混合多种类型产物: 用户明确要求 N 种格式/文件(如'html+md两个文件'), files 必须逐一列全 N 个文件, 一个不少。多数情况下仅需提供 preview_url 或 成果物主文件即可。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "对成果物的简要说明"},
            "preview_url": {"type": "string", "description": "应用/交付物的直达预览地址(真实可达, 可为空)"},
            "files": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string", "description": "文件名称(含后缀)"},
                "path": {"type": "string", "description": "项目内相对路径"},
                "file_type": {"type": "string", "description": "image/chart(Matplotlib图表)/audio/video/text/word/excel/ppt/pdf/code/md/html/zip/other"}},
                "required": ["name", "path"]}, "description": "本地文件交付物列表。如无特殊要求仅交付当前任务的主结果文件(其它文件可打包交付)。"},
            },
            "required": ["title"]}}},
    {"type": "function", "title": "全网搜索", "function": {
        "name": "web_search",
        "description": "通过 UniFuncs 聚合搜索实时网页, 返回标题/链接/摘要, 结果自动登记到信源注册表(返回信源ID供报告引用 [[ID]])。支持多查询词并行检索。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "操作的简要说明"},
            "queries": {"type": "array", "items": {"type": "string"},
                        "description": "搜索关键词列表(支持并行批量)"},
            "max_results": {"type": "integer", "description": "每个查询返回条数, 默认5"}},
            "required": ["title", "queries"]}}},
    {"type": "function", "title": "网页阅读", "function": {
        "name": "web_fetch",
        "description": "批量阅读网页 URL。提取正文文本与发布日期，不支持PDF、Image等非文本类型文件读取(最多10个, 并行执行)。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明"},
            "url": {"type": "string", "description": "单个 URL。与 urls 二选一"},
            "urls": {"type": "array", "items": {"type": "string"},
                      "description": "批量抓取: URL 数组(最多10个, 并行执行)"},
            "prompt": {"type": "string", "description": "针对该内容的关注点(可选)"},
            "timeout": {"type": "integer",
                        "description": "抓取超时秒数(可选, 默认30, 范围5-120)"}},
            "required": []}}},
    {"type": "function", "title": "网页截图", "function": {
        "name": "web_screenshot",
        "description": "浏览器网页截图(Chrome CDP·三级降级): 优先启动用户侧独立空白浏览器实例(与主浏览器完全隔离·临时profile用完即焚·可见窗口支持按需登录), 失败自动降级无头CDP, 再失败降级普通截图。打开 URL 后可依序执行 click/input/scroll/wait 动作再截图, 支持整页截图(full_page)与截图后自动转交视觉模型分析(analyze)。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明"},
            "url": {"type": "string", "description": "要截图的网页 URL"},
            "actions": {"type": "array", "items": {"type": "object", "properties": {
                "type": {"type": "string", "enum": ["click", "input", "scroll", "wait"],
                         "description": "动作类型"},
                "selector": {"type": "string", "description": "CSS 选择器(click/input 定位元素)"},
                "text": {"type": "string", "description": "输入的文本(type=input)"},
                "x": {"type": "integer", "description": "点击横坐标(click 无 selector 时)"},
                "y": {"type": "integer", "description": "点击纵坐标 或 scroll 滚动像素(默认600)"},
                "seconds": {"type": "number", "description": "等待秒数(type=wait, 最大15)"}},
                "required": ["type"]},
                "description": "截图前依序执行的模拟用户动作序列(可选)"},
            "full_page": {"type": "boolean", "description": "true=整页截图(默认仅视口)"},
            "save_path": {"type": "string", "description": "截图保存路径(可选)"},
            "analyze": {"type": "string", "description": "截图后转交视觉模型分析的问题(可选)"}},
            "required": ["url"]}}},
    {"type": "function", "title": "部署服务", "function": {
        "name": "deploy",
        "description": "部署网站或临时暴露服务端口, 返回可访问的完整预览 URL。支持静态目录(内置HTTP服务)与自定义启动命令两种方式, 支持 restart 重启已有部署。url_path 为空时默认指向 index.html。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明"},
            "dir": {"type": "string", "description": "静态站点目录(相对项目根目录, 默认 '.')"},
            "command": {"type": "string", "description": "自定义启动命令(可选, 可用 ${PORT} 占位), 提供时忽略 dir"},
            "port": {"type": "integer", "description": "指定端口(可选, 默认自动分配)"},
            "url_path": {"type": "string", "description": "web应用的预览入口路径: 为空默认指向 index.html; 有值时返回拼接该路径的完整预览 URL, 如 pages/report.html"},
            "restart": {"type": "boolean", "description": "true=重启当前部署"}},
            "required": []}}},
    {"type": "function", "title": "技能管理", "function": {
        "name": "load_skills",
        "description": "技能一键安装与加载: action=list 列出; action=match 按任务语义匹配并自动加载; action=load 加载指定技能(支持批量 names); action=unload 从当前会话卸载技能; action=install 从 git 仓库、本地目录或 npx 安装(source 格式: npx skills add <url/path/name>)。如已加载，禁止重复注入。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明"},
            "action": {"type": "string", "enum": ["list", "match", "load", "unload", "install"]},
            "name": {"type": "string", "description": "技能名(action=load/unload 时, 单个)"},
            "names": {"type": "array", "items": {"type": "string"},
                       "description": "批量加载/卸载: 技能名数组(最多5个)"},
            "intent": {"type": "string", "description": "任务意图描述(action=match 时必填)"},
            "source": {"type": "string", "description": "git URL、本地目录 或 npx 安装命令(action=install 时必填, 如: npx skills add my-skill)"}},
            "required": ["action"]}}},
    {"type": "function", "title": "并行子任务", "function": {
        "name": "swarm_tasks",
        "description": "将同构任务拆给多个独立子智能体并行执行, 全部完成后合并返回。用于互不关联的任务(深度分析、市场调研、多维信息检索、平行文件生成等)。子任务执行过程以树状结构实时展示在网页端。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明"},
            "tasks": {"type": "array", "items": {"type": "string"},
                      "description": "互不依赖的子任务描述列表(2~20个)"},
            "shared_context": {"type": "string", "description": "所有子任务共享的背景信息(可选)"},
            "allow_tools": {"type": "boolean", "description": "子任务是否允许调用只读工具, 默认 true"},
            "max_workers": {"type": "integer", "description": "最大并行数, 默认5"}},
            "required": ["tasks"]}}},
    {"type": "function", "title": "视觉分析", "function": {
        "name": "vision",
        "description": f"基于 {VISION_MODEL} 的视觉能力: 理解图片内容(含OCR/布局检测/异常判断), 支持网页截图代码复刻(生成原生HTML)。支持批量: 传 images 数组可一次分析多张图片(最多5张, 并行执行)。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明"},
            "image": {"type": "string", "description": "单张图片来源: http(s) URL 或本地文件路径。与 images 二选一"},
            "images": {"type": "array", "items": {"type": "string"},
                        "description": "批量分析: 图片来源数组(最多5张, 并行执行)"},
            "prompt": {"type": "string", "description": "处理要求, 如: 识别文字 / 分析布局 / 生成HTML复刻此界面"},
            "mode": {"type": "string", "enum": ["understand", "replicate"],
                     "description": "understand=理解(默认); replicate=代码复刻"}},
            "required": []}}},
    {"type": "function", "title": "快照与回滚", "function": {
        "name": "checkpoint",
        "description": "项目版本管理及上下文-messages 快照与回滚。action=create 自动创建快照; action=list 列出快照; action=rollback 回滚到指定版本(回滚前自动备份)。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "当前操作的简要说明"},
            "action": {"type": "string", "enum": ["create", "list", "rollback"]},
            "note": {"type": "string", "description": "快照备注(action=create)"},
            "id": {"type": "string", "description": "快照ID(action=rollback 时必填)"},
            "restore_context": {"type": "boolean", "description": "回滚时是否同时恢复会话上下文, 默认 false（仅回滚项目版本）"}},
            "required": ["action"]}}},
    {"type": "function", "title": "连接器推送", "function": {
        "name": "connector_push",
        "description": "连接器: 通过 Webhook / 飞书(feishu) / 企业微信(wecom) / 邮件(email) 推送消息或交付物。飞书可在网页端顶栏'📘飞书'入口配置(webhook+应用凭证), 其它渠道在 .haisnap/settings.json 的 connectors 中配置。",
        "parameters": {"type": "object", "properties": {
            "channel": {"type": "string", "enum": ["webhook", "feishu", "wecom", "email"]},
            "title": {"type": "string", "description": "推送标题"},
            "content": {"type": "string", "description": "推送正文(交付说明/链接/摘要)"}},
            "required": ["channel", "title", "content"]}}},
    {"type": "function", "title": "定时任务", "function": {
        "name": "schedule_task",
        "description": "定时任务管理: action=add 创建定时任务(every秒/at HH:MM/cron五段式); action=list 列出; action=remove 删除; action=enable/disable 启停。任务写入全局调度配置，由 'haisnap schedule run' 调度器异步执行。",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string", "description": "操作的简要说明"},
            "action": {"type": "string", "enum": ["add", "list", "remove", "enable", "disable"],
                       "description": "操作类型"},
            "id": {"type": "string", "description": "任务ID(action=add时指定, 为空自动生成; remove/enable/disable时必填)"},
            "name": {"type": "string", "description": "任务名称(action=add)"},
            "prompt": {"type": "string", "description": "定时执行的指令描述(action=add必填)"},
            "every": {"type": "integer", "description": "固定间隔秒数(action=add时三选一)"},
            "at": {"type": "string", "description": "每日定时 HH:MM(action=add时三选一)"},
            "cron": {"type": "string", "description": "五段式cron表达式(action=add时三选一)"},
            "max_runs": {"type": "integer", "description": "最大执行次数(0=无限)"}},
            "required": ["action"]}}}
]

# 工具名称 -> 中文 title 映射(前端显示用, 不传递给 LLM)
TOOL_TITLES = {t["function"]["name"]: t.get("title", t["function"]["name"])
               for t in TOOLS_SCHEMA}