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

# 2. 配置模型（任选其一）
export HAISNAP_API_KEY="sk-你的密钥"
export HAISNAP_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export HAISNAP_MODEL="qwen3-max"

# 3. 启动交互式终端
python -m omni_agent
```

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

## 配置

配置按 **环境变量 > 项目 `.haisnap/settings.json` > 全局 `~/.haisnap/settings.json` > 代码内置默认值** 的顺序逐层回退，任何一层都可以省略。

### 常用环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HAISNAP_API_KEY` | *(空)* | 模型 API 密钥 |
| `HAISNAP_BASE_URL` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | OpenAI 兼容端点 |
| `HAISNAP_MODEL` | `qwen3-max` | 主模型 |
| `HAISNAP_VISION_MODEL` | `qwen-vl-max` | 视觉模型（留空继承主模型） |
| `HAISNAP_THINKING` | `on` | 是否开启思考模式 |
| `HAISNAP_MAX_TURNS` | `500` | 单任务最大工具调用轮数 |
| `HAISNAP_BASH_TIMEOUT` | `120` | 命令执行超时（秒） |
| `HAISNAP_HOME` | `~/.haisnap` | 全局配置与项目根目录 |

完整变量清单见 `omni_agent/config.py`。

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
