# omni-agent

> **一个纯标准库实现的本地终端智能体框架** —— 零第三方依赖，自带 Web 驾驶舱、技能系统、MCP 接入与定时调度。

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)
[![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)]()

---

## 这是什么

`omni-agent` 是一个运行在你**本机终端**的编程与任务执行智能体。它把「大模型 + 工具调用」这套范式做成了一个可读、可改、可嵌入的完整实现：

- 模型负责规划与决策，**真实拥有** shell 与文件系统权限
- 支持多步任务拆解、计划模式、断点快照与失败自愈
- 可接入任意 OpenAI 兼容端点（通义千问 / DeepSeek / Claude / 本地 vLLM 均可）

**最大的特点：整个框架不依赖任何第三方 Python 包。** HTTP 请求走 `urllib`，Web 服务走 `http.server`，向量相似度自实现，全部基于 Python 标准库。这意味着你可以把它丢进任何一台有 Python 的机器，无需 `pip install` 就能跑起来。

---

## 快速开始

### 环境要求

- Python **3.10+**（推荐 3.12）
- 无需安装任何第三方依赖

```bash
# 1. 克隆
git clone https://github.com/yanchaoguo/omni-agent.git
cd omni-agent

# 2. 配置模型（必填）
export HAISNAP_API_KEY="sk-你的密钥"
export HAISNAP_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export HAISNAP_MODEL="qwen3-max"

# 3. 配置联网工具（可选，但强烈建议——否则 web_search 降级为直连抓取）
export HAISNAP_UNIFUNCS_BASE="https://api.unifuncs.com/api"
export HAISNAP_UNIFUNCS_KEY="your-unifuncs-key"

# 4. 启动交互式终端
python -m omni_agent
```

> 全部 40 个环境变量见下方[配置](#配置)章节。除 `HAISNAP_API_KEY` 外均有默认值，最小配置即可跑通。

### 三种运行方式

```bash
# ① 交互式 REPL（默认）
python -m omni_agent

# ② 无头模式：执行单个任务后退出
python -m omni_agent -p "统计当前目录下各类型文件的数量"

# ③ Web 驾驶舱：浏览器里操作，功能与 CLI 一致
python -m omni_agent web --port 3000
```

启动 Web 版后打开 `http://localhost:3000`，可以看到对话流、工具调用树、任务清单与快照管理界面。

---

## 核心能力

### 内置工具（20 个）

| 类别 | 工具 | 说明 |
|---|---|---|
| **执行** | `bash` | 执行 shell 命令，支持后台长驻进程 |
| **文件** | `read_file` `write_file` `edit_file` `multi_edit` | 读写与精准编辑，支持批量 |
| **检索** | `glob_files` `grep_search` | 跨平台文件查找与正则搜索 |
| **任务** | `todo_write` `ask_user_question` | 计划清单维护、向用户澄清确认 |
| **网络** | `web_search` `web_fetch` `web_screenshot` | 联网检索、网页阅读、页面截图 |
| **交付** | `send_user_msg` `deploy` `connector_push` | 成果推送、服务部署、多渠道通知 |
| **扩展** | `load_skills` `swarm_tasks` `vision` | 技能加载、子智能体并行、图像理解 |
| **运维** | `checkpoint` `schedule_task` | 项目快照回滚、定时任务管理 |

### 计划模式

面对复杂任务，智能体会先用 `todo_write` 拆出可追踪的步骤清单，执行中逐步更新状态，避免长任务跑偏。

### 失败自愈（Reflexion）

连续工具失败达到阈值时，自动触发归因分析并注入纠偏策略，而不是盲目重试同一个方案。错误经验会沉淀为长期记忆，后续任务自动规避。

### 技能系统

以 `SKILL.md` 描述能力，放在 `skills/` 目录即可被自动发现与加载：

```
skills/
└── code-review/
    └── SKILL.md
```

也可以通过 `npx skills add <仓库地址>` 从社区安装技能。

### MCP 接入

在 `settings.json` 的 `mcp_servers` 中声明 MCP Server，其工具会自动注册为 `mcp__<server>__<tool>`，与内置工具同等调用：

```json
{
  "mcp_servers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@anthropic/mcp-filesystem", "/tmp"]
    }
  }
}
```

### 定时任务

```bash
python -m omni_agent schedule init      # 生成配置示例
python -m omni_agent schedule start     # 启动异步调度器
python -m omni_agent schedule status    # 查看全部任务进度
```

---

## 用它能做什么

下面是不同人群的真实使用场景。**每条都标注了依赖条件**——凡是需要额外装东西的都会写明，不会让你照着做却跑不通。

### 全栈开发：从写代码到上线一条龙

```
> 帮我用 FastAPI 写一个待办清单 API，带 SQLite 存储和测试用例，写完跑一遍测试
```

智能体会自己建目录、写代码、装依赖、跑测试、修报错，最后把服务跑起来给你一个可访问地址（`deploy` 工具会返回预览 URL）。

**真实能力边界**：它能写代码、跑测试、部署服务，但**不能替你做架构决策**。适合的是「需求明确、你不想手敲」的场景。

---

### 排查问题：把报错日志丢给它

```
> 服务起不来了，这是日志，帮我定位原因
> [粘贴 200 行报错]
```

它会读日志、查代码、复现问题、验证假设。配合 `grep_search` / `glob_files` 在大型项目里定位也很快。

**为什么适合排错**：它拥有**真实的 shell 权限**，能实际跑命令验证猜想，而不是只根据你贴的片段猜。遇到不确定的地方会主动问你，而不是硬猜。

---

### 数据抓取与整理

```
> 抓取这个榜单页面的全部条目，整理成 Excel，按评分排序
```

流程是：`web_fetch` 取页面 → 解析 → `write_file` 存成 CSV/Excel → `send_user_msg` 推给你。

**依赖条件**：解析 HTML 若需要 `beautifulsoup4` 等库，它会用 `bash` 自己装。**当前环境若 pip 源不通会失败**，这时它会退回正则解析或提示你配置镜像源。

**边界**：目标是 JS 动态渲染的页面时，`web_fetch` 拿不到内容——需改用 `web_screenshot`（真实浏览器渲染后截图 + 视觉模型识别）。这是两条不同路径，它会自己判断。

---

### 网页报告与数据可视化

```
> 把这份数据做成一份带图表的 HTML 报告，我要能直接发给同事看
```

产出单文件 HTML，图表用内联 SVG 或 Chart.js，双击就能在浏览器打开。

**这类任务它特别擅长**，因为 `write_file` 写 `.html`/`.md` 时会自动渲染引用角标与高亮，`deploy` 还能直接生成可分享的预览链接。

---

### 写论文与做研究

```
> 帮我查一下 2025 年固态电池的最新进展，整理成带引用的综述，每个数据都要有来源
```

`web_search` 检索 → `web_fetch` 读原文 → 整理成带信源标注的文档。

**重要提醒**：它会标注数据来源，但**你需要自己复核引用**。这是所有 AI 工具的共性局限，不是这个框架特有的问题。

---

### 做 PPT 与演示文稿

三条可行路径，按你的环境选：

| 方案 | 依赖 | 适用 |
|---|---|---|
| **单文件 HTML 幻灯片** | **零依赖** | 推荐。键盘翻页，浏览器直接演示，可离线 |
| `.pptx` 文件 | 需 `pip install python-pptx` | 需要交付可编辑的 Office 文件时 |
| Markdown → 任意工具 | 零依赖 | 你已有转换工具链时 |

```
> 把这份报告做成 10 页的 HTML 幻灯片，深色主题
```

**边界**：框架**没有内置图片生成能力**。需要配图时它会用 CSS/SVG 绘制示意图，或提示你自己准备图片素材。

---

### 操作本地系统软件

它有真实 shell 权限，macOS 下可以驱动系统命令与应用：

```
> 把系统音量调到 50%，然后打开访达的下载目录
> 每天早上 9 点帮我截屏一次桌面存到指定目录
```

macOS 可用 `osascript`（AppleScript 控制任意应用）、`open`、`pmset`、`screencapture`、`say` 等。Windows 下走 PowerShell / `cmd`。

**安全提示**：默认开启权限门控，`rm`、`sudo` 等敏感操作会先问你。涉及系统级修改时请仔细看确认提示。

---

### 编写屏保、桌宠与小工具

```
> 写一个桌面宠物程序，小猫在屏幕上随机走动，点击会跳一下
```

**依赖条件**：图形界面需要 `tkinter`。

```bash
# macOS: 系统自带 Python 常缺 tkinter，需补装
brew install python-tk

# Windows: 官方 Python 安装包默认已含
```

补上后可用 `tkinter` 的 `overrideredirect(True)` + `-topmost` 实现无边框置顶的桌宠窗口。

**替代方案**：不想装 tkinter 也可以做成**网页版**——一个 HTML 文件 + `deploy` 就能跑，效果一样。

---

### 安装与调试浏览器插件

这里需要**说清楚**，避免你误以为它能直接操作你日常浏览器的插件：

**它做不到的**：`web_screenshot` 启动的浏览器实例**硬编码禁用了扩展**（`--disable-extensions`），这是为了与你日常浏览器完全隔离——临时 profile、用完即焚、不留登录态。所以它**不能**在你日常 Chrome 里装插件。

**它能做的**：

| 场景 | 可行路径 |
|---|---|
| 写一个浏览器插件 | 直接写 `manifest.json` + JS，产出可加载的扩展目录 |
| 调试插件逻辑 | 用 `web_screenshot` 截图验证目标页面的 DOM 结构，据此写选择器 |
| 需要带插件的自动化 | 通过 **MCP** 挂载 Playwright 类服务（MCP 支持自定义 `command`/`args`），在其参数里加 `--load-extension` |

```
> 帮我写一个 Chrome 插件，在京东商品页显示历史价格
```

它会生成完整扩展目录 + 加载说明（`chrome://extensions` → 开发者模式 → 加载已解压的扩展程序）。

---

### 定时任务与自动化

```
> 每天早上 8 点抓取某网站数据，汇总后推送到飞书
```

`schedule_task` 支持固定间隔、每日定时、cron 三种模式，配合 `connector_push` 推送到飞书/企业微信/邮件/Webhook。

---

### 一句话总结适用边界

**它擅长**：有明确目标、可通过命令或文件操作完成、结果可验证的任务。

**它不擅长**：需要审美判断的设计、需要领域专家决策的问题、以及任何你无法验收结果的任务。

**核心优势**是真实执行——不是给你一段代码让你自己跑，而是真的在你机器上跑通了再交付。

---

## 配置

配置按 **环境变量 > 项目 `.haisnap/settings.json` > 全局 `~/.haisnap/settings.json` > 代码内置默认值** 的顺序逐层回退，任何一层都可以省略。

所有配置项都有内置默认值，**最小可用配置只需 `HAISNAP_API_KEY`**（或通过 Web 界面 / `settings.json` 填入）。

### 模型接入

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HAISNAP_API_KEY` | *(空)* | 主模型 API 密钥，**唯一必填项** |
| `HAISNAP_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容端点 |
| `HAISNAP_MODEL` | `qwen3-max` | 主模型名称 |
| `HAISNAP_VISION_MODEL` | `qwen-vl-max` | 视觉模型（用于 `vision` 工具） |
| `HAISNAP_VISION_API_KEY` | *(空)* | 视觉模型独立密钥，留空继承主模型 |
| `HAISNAP_VISION_BASE_URL` | *(空)* | 视觉模型独立端点，留空继承主模型 |

### 联网工具（搜索与网页阅读）

`web_search` / `web_fetch` 走 UniFuncs 聚合通道。**不配置也能用**——此时自动降级为直连抓取，但搜索质量与反爬能力会下降。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HAISNAP_UNIFUNCS_BASE` | `https://api.unifuncs.com/api` | 联网工具服务端点 |
| `HAISNAP_UNIFUNCS_KEY` | *(空)* | 联网工具 API 密钥，留空则降级为直连模式 |
| `HAISNAP_WEB_FETCH_TIMEOUT` | `30` | 网页抓取超时（秒），单次调用可覆盖 |
| `HAISNAP_UA` | Chrome 131 UA | 请求 User-Agent（默认去除 Agent 标识以规避站点 403） |

```bash
# 启用完整联网能力
export HAISNAP_UNIFUNCS_BASE="https://api.unifuncs.com/api"
export HAISNAP_UNIFUNCS_KEY="your-unifuncs-key"
```

### 备用模型与智能路由

主模型不可用时自动故障转移；路由按请求特征选择专用模型（槽位留空即回退主模型，无副作用）。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HAISNAP_FALLBACK_MODEL` | *(空)* | 备用模型，非空即启用主备转移 |
| `HAISNAP_FALLBACK_API_KEY` | *(空)* | 备用模型密钥，留空继承主模型 |
| `HAISNAP_FALLBACK_BASE_URL` | *(空)* | 备用模型端点，留空继承主模型 |
| `HAISNAP_ROUTING` | `true` | 是否启用多模型智能路由 |
| `HAISNAP_ROUTING_LIGHT` | *(空)* | 轻量任务模型（内部调用、简单改写） |
| `HAISNAP_ROUTING_CODE` | *(空)* | 代码任务模型 |
| `HAISNAP_ROUTING_LONG` | *(空)* | 超长上下文任务模型 |
| `HAISNAP_ROUTING_LONG_TOKENS` | `30000` | 触发长上下文路由的 token 阈值 |

### Agent 行为

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HAISNAP_THINKING` | `on` | 思考模式。设 `off`/`false`/`0` 可关闭 |
| `HAISNAP_MAX_TURNS` | `500` | 单任务最大工具调用轮数 |
| `HAISNAP_BASH_TIMEOUT` | `120` | shell 命令执行超时（秒） |
| `HAISNAP_MAX_TOOL_OUTPUT` | `12000` | 工具输出截断长度（字符） |
| `HAISNAP_MAX_FILE_READ` | `60000` | 单文件读取截断长度（字符） |
| `HAISNAP_ASK_TIMEOUT` | `120` | `ask_user_question` 倒计时确认（秒） |
| `HAISNAP_PROMPT_CACHE` | `auto` | Prompt 缓存：`auto`（仅 Claude 启用）/ `on` / `off` |
| `HAISNAP_PROMPT_CACHE_TTL` | `5m` | 缓存有效期，`5m` 或 `1h` |

### 失败自愈

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HAISNAP_REFLEXION` | `true` | 是否启用 Reflexion 自愈引擎 |
| `HAISNAP_REFLEXION_THRESHOLD` | `2` | 连续失败达到该次数即触发归因 |

### 浏览器与图像

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HAISNAP_BROWSER_HEADED` | `true` | 优先使用可见浏览器实例（便于登录）；`false` 直接走无头 |
| `HAISNAP_IMAGE_MODEL` | `qwen-image-plus` | 图像生成模型 |
| `HAISNAP_IMAGE_WORKERS` | `6` | 批量图像生成并行度 |

### 目录与日志

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HAISNAP_HOME` | `~/.haisnap` | 全局配置目录（含 `env.json`、快照、调度状态） |
| `HAISNAP_PROJECTS_ROOT` | `~/.haisnap/haisnap_projects` | 自动创建的项目工作目录根 |
| `HAISNAP_LOG_LEVEL` | `INFO` | 日志级别 |
| `HAISNAP_LOG_CONSOLE` | *(空)* | 设 `on`/`1`/`true` 时日志同时输出到控制台 |

### 用户级环境变量持久化

除 `export` 外，还可以把变量写入 `~/.haisnap/env.json`，启动时自动加载（**同名覆盖进程环境变量**）：

```json
{
  "HAISNAP_API_KEY": "sk-xxx",
  "HAISNAP_MODEL": "qwen3-max",
  "HAISNAP_UNIFUNCS_KEY": "your-unifuncs-key"
}
```

Web 驾驶舱的配置面板也支持修改，改动会持久化到该文件，重启后仍生效。

### Hooks（工具调用钩子）

`settings.json` 的 `hooks` 段可在工具调用前后执行自定义命令，通过环境变量读取上下文：`HAISNAP_TOOL`（工具名）、`HAISNAP_EVENT`（事件类型）、`HAISNAP_PAYLOAD` / `HAISNAP_PAYLOAD_Q`（调用参数）。钩子返回码 `2` 会阻止该次工具调用。

### 安全门控

`permission` 段可配置自动放行、询问确认与直接拒绝三类策略，并内置危险命令模式拦截（如 `rm -rf /`、`mkfs`、`dd of=/dev/*`）。默认情况下 `rm`、`sudo`、`git push` 等敏感操作都会先征求确认。

---

## 项目结构

```
omni-agent/
├── omni_agent/              # 核心包
│   ├── agent.py             # 智能体主循环：规划 → 工具调用 → 观察 → 收敛
│   ├── cli.py               # 命令行入口与参数解析
│   ├── webserver.py         # Web 驾驶舱服务端
│   ├── llm.py               # 模型接入与流式响应
│   ├── tools_schema.py      # 20 个内置工具的定义
│   ├── tool_runner.py       # 工具执行与异常隔离
│   ├── permission.py        # 权限门控与危险命令拦截
│   ├── checkpoint.py        # 项目快照与回滚
│   ├── reflexion.py         # 失败归因与自愈
│   ├── lessons.py           # 长期经验沉淀
│   ├── skills.py            # 技能发现与加载
│   ├── mcp.py               # MCP 协议接入
│   ├── scheduler.py         # 定时任务调度
│   ├── tool_policy.py       # 按任务类型自动匹配工具集
│   ├── model_router.py      # 多模型智能路由（轻量/代码/长上下文）
│   ├── browser.py           # 网页截图（CDP 三级降级）
│   ├── similarity.py        # 文本相似度（自实现，无 numpy）
│   ├── settings.py          # 四层配置回退
│   ├── envstore.py          # 用户级环境变量持久化
│   └── web/index.html       # Web 前端（单文件）
├── skills/code-review/      # 内置技能示例
├── launcher.py              # 启动器（CLI / Web 二选一）
├── omni_agent_win.spec         # PyInstaller 打包配置
└── omni_agent/settings.json # 全量默认配置示例
```

> 注：`swarm_tasks`（子智能体并行）与 `vision`（图像理解）作为工具在 `tool_runner.py` 中实现，未拆分为独立模块。

---

## 打包为独立可执行文件

```bash
pip install pyinstaller
pyinstaller omni_agent_win.spec
# 产物: dist/omni-agent.exe（单文件，含 Web 前端资源）
```

---

## 设计取舍

**为什么不用 `requests`？** 框架定位是"能在任何环境直接跑"。`urllib` 虽笨重，但省掉了依赖安装这一步，也让部署到隔离环境变得简单。

**为什么自实现向量相似度？** 避免为了一个余弦相似度引入 `numpy` 这种重依赖。数据规模在万级时纯 Python 完全够用。

**为什么 Web 前端是单文件？** 不引入构建工具链。`index.html` 内联全部样式与逻辑，改完直接刷新即可，无需 `npm run build`。

---

## 许可

[MIT License](LICENSE) —— 可自由用于商业项目，欢迎 Fork 与二次开发。
