# -*- coding: utf-8 -*-
"""CLI 入口: 交互式 REPL / 无头模式 / 定时任务调度(schedule 子命令组)

用法:
  python -m omni_agent                          # 交互式 REPL
  python -m omni_agent -p "写一个排序脚本"       # 无头模式
  python -m omni_agent --yolo -p "..."          # 跳过所有审批(危险)
  python -m omni_agent --deny-tools bash -p ... # 本次任务禁用指定工具(黑名单)
  python -m omni_agent --tool-policy auto -p ...# 按任务类型自动匹配工具范围
  python -m omni_agent web --port 3000          # 启动网页版 UI(功能与 CLI 一致)
  python -m omni_agent schedule init            # 生成调度配置示例
  python -m omni_agent schedule start           # 启动异步定时调度器
  python -m omni_agent schedule status          # 查看全部定时任务进度
  python -m omni_agent schedule status <task_id># 查看指定任务进度
  python -m omni_agent schedule kill <task_id>  # 终止正在执行的任务
"""
import argparse
import os
import sys
import time
import uuid

from .agent import Agent
from .config import (MEMORY_FILE, MODEL, SCHEDULE_FILE_DEFAULT, SETTINGS_DIR, PROJECTS_DIR, 
                     __version__)
from .envstore import EnvStore
from .logger import get_logger
from .tool_policy import ToolPolicy
from .scheduler import (kill_task, make_progress_writer, print_status,
                        run_scheduler, write_sample_config)
from .ui import C, cprint

log = get_logger("cli")

BANNER = f"""{C.CYAN}{C.B}
 ██   ██  █████  ██ ███████ ███    ██  █████  ██████
 ██   ██ ██   ██ ██ ██      ████   ██ ██   ██ ██   ██
 ███████ ███████ ██ ███████ ██ ██  ██ ███████ ██████
 ██   ██ ██   ██ ██      ██ ██  ██ ██ ██   ██ ██
 ██   ██ ██   ██ ██ ███████ ██   ████ ██   ██ ██
{C.R}
{C.GRAY}  omni-agent 本地智能体 v{__version__} · 北京海新智能 · 模型 {MODEL} · 输入 /help 查看命令{C.R}
"""

HELP = f"""{C.CYAN}会话命令:{C.R}
  /help              显示本帮助
  /clear             清空会话历史(保留系统提示与记忆)
  /compact           蒸馏压缩历史(原文归档可恢复, 失败记录被铭记)
  /cost              查看 token 消耗统计
  /memory            查看项目记忆 {MEMORY_FILE} 与跨会话全局记忆(lessons.jsonl)
  /tools             列出可用工具(内置 + MCP)
  /mode [checklist|auto|chat]   切换 计划模式 / 探索模式 / 对话模式
  /thinking [on|off] 开关 thinking 思考模式
  /sources           查看本会话登记的信源列表
  /env               查看用户级环境变量(.haisnap/env.json)
  /env set KEY VAL   设置用户级环境变量(同名覆盖, 即时生效, 持久化)
  /env del KEY       删除用户级环境变量
  /policy [allow=a,b] [deny=c] [auto|manual|off]  工具黑白名单策略(试算生效范围)
  /checkpoints                     列出项目快照(内容寻址去重存储)
  /checkpoint create [备注]        手动创建快照
  /checkpoint switch <ID> [ctx]    手动切换到指定快照(ctx=同时恢复会话上下文)
  /sessions          列出全部会话  |  /session <new|switch <ID>|info> 会话管理
  /schedule [task_id]        查看定时任务执行进度(等价 schedule status)
  /schedule kill <task_id>   终止正在执行的定时任务
  /exit 或 /quit     退出
  # <内容>           快速追加一条项目记忆到 {MEMORY_FILE}
{C.CYAN}定时调度(独立命令):{C.R} schedule init|start|status [task_id]|kill <task_id>
  配置文件(定时配置+任务prompt): {SCHEDULE_FILE_DEFAULT}
{C.CYAN}审批说明:{C.R} 敏感命令需 y/n 确认; 启动加 --yolo 跳过; 调度任务免审批一路畅通
{C.CYAN}Hooks:{C.R} 在 {SETTINGS_DIR}/settings.json 配置 pre_tool_use/post_tool_use 钩子
{C.CYAN}MCP:{C.R} settings.json 的 mcp_servers 配置第三方 MCP Server, 与内置工具同等调用
{C.CYAN}连接器:{C.R} settings.json 的 connectors 配置 webhook/feishu/wecom/email 推送交付物"""


def resolve_project_dir(args):
    """每个项目独立且唯一的目录: 行动前检查并创建, 返回目录绝对路径。
    自动命名目录将在首个任务后由 LLM 英文名重建(移除 --project 传参,
    项目身份统一由任务固化机制管理, 避免与自动命名双轨冲突)。"""
    if args.workdir != ".":
        d = os.path.abspath(args.workdir)
    else:
        root = PROJECTS_DIR
        d = os.path.join(root, "project_" + time.strftime("%Y%m%d_%H%M%S"))
        if os.path.exists(d):   # 极端并发下保证唯一
            d += "_" + uuid.uuid4().hex[:4]
    os.makedirs(d, exist_ok=True)
    return d


def build_parser():
    ap = argparse.ArgumentParser(
        prog="omni_agent",
        description="omni-agent - omni-agent 本地智能体(北京海新智能)")
    ap.add_argument("-p", "--prompt", help="无头模式: 执行单个任务后退出")
    ap.add_argument("-w", "--workdir", default=".", help="工作目录(默认自动创建独立项目目录)")
    ap.add_argument("--yolo", action="store_true", help="跳过所有审批(危险)")
    ap.add_argument("--allow-tools", default="",
                    help="工具白名单(逗号分隔): 仅允许指定工具+核心工具(todo/提问/交付)")
    ap.add_argument("--deny-tools", default="",
                    help="工具黑名单(逗号分隔): 命中即拒绝, 优先级最高")
    ap.add_argument("--tool-policy", choices=["manual", "auto"], default="manual",
                    help="auto=按任务类型(coding/research/writing/...)自动匹配工具范围")
    ap.add_argument("--mode", choices=["checklist", "auto", "chat"], default="checklist",
                    help="checklist=计划模式(默认) / auto=探索模式 / chat=对话模式")
    ap.add_argument("--no-thinking", action="store_true", help="关闭 thinking 思考模式")
    ap.add_argument("--task-id", help="(调度器内部使用)以指定任务ID上报执行进度")
    ap.add_argument("--version", action="version", version=f"omni-agent v{__version__}")

    sub = ap.add_subparsers(dest="subcmd")
    sp = sub.add_parser("schedule", help="定时任务调度(基于配置文件的异步调度)")
    sp.add_argument("action", choices=["init", "start", "status", "kill"],
                    help="init=生成示例配置 / start=启动调度器 / status=进度查询 / kill=终止任务")
    sp.add_argument("task_id", nargs="?", help="任务ID(status 可选 / kill 必填)")
    sp.add_argument("-c", "--config", default=SCHEDULE_FILE_DEFAULT,
                    help=f"调度配置文件路径(默认 {SCHEDULE_FILE_DEFAULT})")
    wp = sub.add_parser("web", help="启动网页版 UI(功能与 CLI 一致)")
    wp.add_argument("-w", "--workdir", default=".",
                    help="工作目录(默认自动创建独立项目目录; 子命令级参数, 覆盖全局 -w)")
    wp.add_argument("--host", default="0.0.0.0", help="监听地址(默认 0.0.0.0)")
    wp.add_argument("--port", type=int, default=int(os.environ.get("PORT", 3000)),
                    help="监听端口(默认 $PORT 或 3000)")
    return ap


def handle_schedule(args):
    """schedule 子命令组: init / start / status / kill"""
    if args.action == "init":
        path = write_sample_config(args.config)
        cprint(f"  ✓ 已生成调度配置示例: {path}", C.GREEN)
        cprint("  编辑 jobs 后执行: python -m omni_agent schedule start", C.GRAY)
        return 0
    if args.action == "start":
        run_scheduler(args.config)
        return 0
    if args.action == "status":
        return print_status(args.task_id)
    if args.action == "kill":
        if not args.task_id:
            cprint("   用法: schedule kill <task_id>(先 schedule status 查看)", C.RED)
            return 1
        return kill_task(args.task_id)
    return 1


def handle_checkpoint(agent, arg):
    """checkpoint 手动管理: list / create [note] / switch <id> [ctx]"""
    parts = arg.split()
    sub = parts[0] if parts else "list"
    if sub == "list":
        items = agent.ckpt.list()
        if not items:
            cprint("  (暂无快照: /checkpoint create [备注] 手动创建)", C.GRAY)
            return
        cur_state = agent.ckpt.current_state()
        for m in items:
            mark = "▶" if m.get("state") == cur_state else " "
            cprint(f"  {mark} {m['id']}  {m['created']}  "
                   f"{'[自动]' if m.get('auto') else '[手动]'} "
                   f"files={m.get('files', '?')} {m['note']}", C.CYAN)
        cprint("  用法: /checkpoint create [备注] | /checkpoint switch <ID> [ctx]", C.GRAY)
        return
    if sub == "create":
        note = " ".join(parts[1:]) or "手动快照"
        try:
            cid, reused = agent.ckpt.create(note=note, messages=agent.messages)
            cprint(f"  ✓ 快照 {cid}" + (" (状态未变, 复用零冗余)" if reused else " 已创建"),
                   C.GREEN)
        except OSError as e:
            cprint(f"   快照创建失败: {e}", C.RED)
        return
    if sub == "switch":
        if len(parts) < 2:
            cprint("  用法: /checkpoint switch <快照ID> [ctx](先 /checkpoints 查看)", C.YELLOW)
            return
        restore_ctx = len(parts) > 2 and parts[2].lower() in ("ctx", "context", "--ctx")
        ctx, msg = agent.ckpt.rollback(parts[1], restore_context=restore_ctx)
        cprint(f"  {msg}", C.GREEN if msg.startswith("[成功]") else C.YELLOW)
        if ctx:
            agent.messages = ctx
            cprint(f"  ⛃ 会话上下文已同步恢复({len(ctx)} 条消息)", C.MAGENTA)
        return
    cprint("  用法: /checkpoints | /checkpoint create [备注] | /checkpoint switch <ID> [ctx]",
           C.YELLOW)


def repl(agent):
    """交互式 REPL 主循环"""
    print(BANNER)
    cprint(f"  项目目录(独立唯一): {agent.wd}", C.GRAY)
    cprint(f"  任务身份: {agent.project_brief()}", C.GRAY)
    cprint(f"  模式: {agent.mode} | thinking: {'on' if agent.llm.thinking else 'off'}"
           f" | 连接器: {agent.connectors.available() or '未配置'}"
           f" | MCP工具: {len(agent.mcp.schemas)}", C.GRAY)
    if agent.perm.yolo:
        cprint("   YOLO 模式: 所有操作免审批!", C.YELLOW)
    while True:
        try:
            cprint(f"\n{C.B}{C.GREEN}❯ {C.R}", end="")
            user = input().strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user:
            continue
        if user in ("/exit", "/quit"):
            break
        if user == "/help":
            print(HELP)
        elif user == "/clear":
            agent.messages = [agent.messages[0]]
            cprint("  ✓ 会话已清空", C.GREEN)
        elif user == "/compact":
            agent.cmd_compact()
        elif user == "/cost":
            agent.cmd_cost()
        elif user == "/memory":
            agent.cmd_memory()
        elif user == "/sources":
            cprint(agent.sources.render_list(), C.CYAN)
        elif user == "/sessions":
            agent.cmd_session("list")
        elif user.startswith("/session"):
            agent.cmd_session(user.split(maxsplit=1)[1].strip() if " " in user else "")
        elif user == "/checkpoints":
            handle_checkpoint(agent, "list")
        elif user.startswith("/checkpoint"):
            handle_checkpoint(agent, user.split(maxsplit=1)[1].strip()
                              if " " in user else "list")
        elif user.startswith("/schedule"):
            rest = user.split(maxsplit=1)[1].strip().split() if " " in user else []
            if rest and rest[0] == "kill":
                if len(rest) > 1:
                    kill_task(rest[1])
                else:
                    cprint("  用法: /schedule kill <task_id>", C.YELLOW)
            else:
                print_status(rest[0] if rest else None)
        elif user.startswith("/mode"):
            agent.cmd_mode(user.split(maxsplit=1)[1].strip() if " " in user else "")
        elif user.startswith("/thinking"):
            agent.cmd_thinking(user.split(maxsplit=1)[1].strip() if " " in user else "")
        elif user.startswith("/policy"):
            agent.cmd_policy(user.split(maxsplit=1)[1].strip() if " " in user else "")
        elif user == "/tools":
            for t in agent.all_tool_schemas():
                f = t["function"]
                cprint(f"  {f['name']:<22} {f['description'][:58]}", C.CYAN)
        elif user.startswith("/env"):
            parts = user.split(None, 3)
            sub = parts[1] if len(parts) > 1 else "list"
            if sub == "list":
                items = EnvStore.list_masked()
                if not items:
                    cprint("  (暂无用户级环境变量, 用 /env set KEY VAL 添加)", C.GRAY)
                for it in items:
                    cprint(f"  {it['key']:28s} = {it['value']}"
                           + ("  " if it["sensitive"] else ""), C.CYAN)
            elif sub == "set" and len(parts) >= 4:
                ok, msg = EnvStore.set(parts[2], parts[3])
                cprint(f"  {'✓' if ok else ''} {msg}", C.GREEN if ok else C.RED)
            elif sub in ("del", "delete", "remove") and len(parts) >= 3:
                ok, msg = EnvStore.unset(parts[2])
                cprint(f"  {'✓' if ok else ''} {msg}", C.GREEN if ok else C.RED)
            else:
                cprint("  用法: /env | /env set KEY VALUE | /env del KEY", C.YELLOW)
        elif user.startswith("# "):
            agent.add_memory(user[2:])
        else:
            try:
                agent.run_task(user)
            except KeyboardInterrupt:
                # 修复阻塞性BUG: 中断可能发生在 assistant.tool_calls 已入队、
                # tool 响应未写入之间, 需补齐消息链, 否则后续请求全部失败
                agent.repair_context()
                cprint("\n   当前任务已被中断(Ctrl+C), 消息链已修复, "
                       "发送新的需求可继续对话。", C.YELLOW)
    agent.cmd_cost()
    agent.close()
    cprint("再见! 👋", C.CYAN)


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)

    # ---- schedule 子命令: 不创建 Agent, 直接处理 ----
    if args.subcmd == "schedule":
        sys.exit(handle_schedule(args))

    # ---- web 子命令: 启动网页版 UI ----
    if args.subcmd == "web":
        from .webserver import serve
        serve(host=args.host, port=args.port, workdir=resolve_project_dir(args))
        return

    workdir = resolve_project_dir(args)
    headless = bool(args.prompt)
    scheduled = bool(args.task_id)   # 调度器拉起的子进程: 免审批 + 进度上报
    progress_cb = make_progress_writer(args.task_id) if args.task_id else None
    cli_policy = ToolPolicy(
        allow=[x for x in args.allow_tools.split(",") if x.strip()],
        deny=[x for x in args.deny_tools.split(",") if x.strip()],
        auto=(args.tool_policy == "auto"))
    agent = Agent(workdir, yolo=args.yolo, headless=headless, scheduled=scheduled,
                  mode=args.mode, thinking=not args.no_thinking,
                  quiet=scheduled, progress_cb=progress_cb,
                  tool_policy=None if cli_policy.is_noop() else cli_policy)

    if headless:
        label = f"[omni-agent 定时任务 task={args.task_id}]" if scheduled \
            else "[omni-agent 无头模式]"
        cprint(f"{label} 项目目录: {agent.wd}", C.GRAY)
        try:
            ok = agent.run_task(args.prompt)
        finally:
            agent.cmd_cost()
            agent.close()
        sys.exit(0 if ok else 1)

    repl(agent)


def entry():
    """setup.py console_scripts 入口(带顶层异常兜底)"""
    try:
        main()
    except KeyboardInterrupt:
        cprint("\n已中断退出", C.YELLOW)
    except Exception as e:
        cprint(f"\n 致命错误: {type(e).__name__}: {e}", C.RED)
        import traceback
        traceback.print_exc()
        sys.exit(1)