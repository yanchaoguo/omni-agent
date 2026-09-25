# -*- coding: utf-8 -*-
"""Agent 核心: 基于 Function Calling 的多轮自主工具调用循环"""
import json
import os
import re
import sys
import threading
import time
import uuid

from .checkpoint import CheckpointManager
from .config import MAX_AGENT_TURNS, MEMORY_FILE, SETTINGS_DIR, GLOBAL_DIR
from .settings import S   # 统一配置链
from .connectors import Connectors
from .hooks import Hooks
from .lessons import LessonStore
from .llm import HaisnapClient
from .logger import get_logger, new_tracker
from .mcp import MCPManager
from .permission import Permission
from .prompts import MODE_PROMPTS, NAMER_PROMPT, SYSTEM_PROMPT
from .session import SessionManager
from .settings import load_settings
from .skills import SkillsManager
from .sources import SourceRegistry
from .tool_policy import ToolPolicy
from .tool_runner import ToolRunner
from .tools_schema import TOOLS_SCHEMA
from .continuum import ContinuumEngine    # 任务续航引擎(行动型粘性)
from .reflexion import ReflexionEngine   # 自愈反思引擎
from .domain_router import DomainRouter  # 垂直领域异构执行引擎
from .ui import C, cprint


class Agent:
    def __init__(self, workdir, yolo=False, headless=False, scheduled=False,
                 mode="checklist", thinking=True, quiet=False,
                 progress_cb=None, tool_policy=None):
        # 项目隔离: 行动前检查并创建独立且唯一的目录
        self.wd = os.path.abspath(workdir)
        os.makedirs(self.wd, exist_ok=True)
        os.makedirs(os.path.join(self.wd, SETTINGS_DIR), exist_ok=True)
        os.makedirs(GLOBAL_DIR, exist_ok=True)

        settings = load_settings(self.wd)
        S.init(self.wd)   # 绑定四层配置链单例到当前工作目录
        # thinking 开关优先级: 环境变量 > 项目settings.json > 全局settings.json > CLI
        if os.environ.get("HAISNAP_THINKING", "").lower() in ("off", "false", "0"):
            thinking = False
        elif S.resolve("thinking")[1] != "default":
            thinking = S.get_bool("thinking", True)

        self.log = get_logger("agent")   # 核心模块统一日志(tracker_id 链路标记)
        self.mode = mode          # checklist=计划模式 / auto=探索模式
        self.progress_cb = progress_cb   # 调度器进度回调: fn(event, payload)
        # 工具策略优先级: 显式传入(CLI/Web) > settings.json 的 tool_policy 配置
        self.tool_policy = tool_policy or ToolPolicy.from_config(
            settings.get("tool_policy"))
        self.perm = Permission(yolo, headless, scheduled)
        self.hooks = Hooks(self.wd, settings)
        self.sources = SourceRegistry()
        self.connectors = Connectors(settings)
        self.ckpt = CheckpointManager(self.wd)
        self.memory = LessonStore()
        self.skills = SkillsManager(self.wd)
        self.llm = HaisnapClient(thinking=thinking, quiet=quiet)
        self.mcp = MCPManager(settings)
        self.sessions = SessionManager()
        self.session = self.sessions.find_by_workdir(self.wd)
        self._context_loaded = False    # 首次任务前尝试恢复持久化上下文
        self.tools = ToolRunner(self.wd, self.perm, self.hooks, self.sources,
                                self.connectors, self.ckpt, self.skills, self.mcp,
                                self.llm, self.session)
        # Reflexion 自愈反思引擎 —— 连续工具失败时 LLM 归因并注入纠偏策略
        self.reflexion = ReflexionEngine(
            self.llm, memory=self.memory,
            emit=lambda ev, pl: self._emit(ev, pl))
        # 任务续航引擎 —— 任务完成后异步蒸馏"下一步最佳行动"建议,
        # 以可点击芯片推送给用户一键续跑, 形成任务链闭环(行动型粘性)
        self.continuum = ContinuumEngine(
            self.llm, emit=lambda ev, pl: self._emit(ev, pl))
        self.messages = [{"role": "system", "content": self._system()}]
        # 人机协同·实时共驾(Co-Pilot Steering) —— 任务执行中用户可随时
        # 注入协同指令, 智能体在下一轮开始前吸收(对标 WorkBuddy「项目」人机协作范式)
        self.steer_queue = []
        self._steer_lock = threading.Lock()
        self._bg_threads = []
        self._stop_event = threading.Event()   # 手动终止任务信号(线程安全)
        # 连续相同工具调用去重(修复模型循环重复执行同一工具的问题)
        self._last_tool_fp = None      # 上一次工具调用指纹 (name, args_json)
        self._dup_tool_count = 0       # 连续重复次数
        self._ensure_gitignore()

    # ---- 系统提示与项目记忆 ----
    def _system(self):
        mem = f"(暂无。可创建 {MEMORY_FILE} 记录项目约定, 将自动注入上下文)"
        mp = os.path.join(self.wd, MEMORY_FILE)
        if os.path.isfile(mp):
            with open(mp, encoding="utf-8", errors="replace") as f:
                mem = f.read()[:8000]
        # 注入运行平台信息(Windows/Linux/macOS), LLM 据此选择正确的命令语法
        platform = ("Windows" if os.name == "nt"
                    else "macOS" if sys.platform == "darwin"
                    else "Linux/Unix")
        # v4.0.0: 按规划模式动态加载对应系统提示词(checklist/auto/chat)
        tpl = MODE_PROMPTS.get(self.mode, SYSTEM_PROMPT)
        # chat 模式提示词缺少 global_memory / skills_index 占位, 需用 SafePlaceholder 兜底
        class _SafeDict(dict):
            def __missing__(self, key):
                return f"{{{key}}}"
        base = tpl.format_map(_SafeDict(
            workdir=self.wd, memory=mem,
            memory_file=MEMORY_FILE,
            project_identity=(self.session or {}).get("name_zh", ""),
            global_memory=self.memory.load(),
            skills_index=self._skills_index(),
            platform=platform))
         
        try:
            base += self.skills.loaded_content()
        except Exception as _e:
            self.log.warning("忽略异常(omni_agent/agent.py:100): %s: %s", type(_e).__name__, _e)
            pass
        self.log.warning("系统提示词: %s", base)
        return base

    def _skills_index(self):
        """启动/每次任务前自动扫描技能列表(项目 skills/、.haisnap/skills/
        与全局技能库), 注入系统提示供模型按用户意图挑选并 load_skills 加载。"""
        try:
            sks = self.skills.scan()
        except Exception:
            sks = []
        if not sks:
            return "(当前无可用技能, 按通用流程执行)"
        lines = []
        for s in sks:
            brief = " ".join(s.get("brief", "").split())[:110]
            lines.append(f"- {s['name']}: {brief}")
        return "\n".join(lines)

    def _ensure_gitignore(self):
        gi = os.path.join(self.wd, ".gitignore")
        if not os.path.isfile(gi):
            content = ("# omni-agent auto-generated .gitignore\n"
                       f"{SETTINGS_DIR}/\n*.pyc\n__pycache__/\n.venv/\nvenv/\n"
                       "node_modules/\ndist/\nbuild/\n.env\n*.log\n")
            try:
                with open(gi, "w", encoding="utf-8") as f:
                    f.write(content)
            except OSError as _e:
                self.log.warning("忽略异常(omni_agent/agent.py:129): %s: %s", type(_e).__name__, _e)
                pass

    def project_brief(self):
        if not self.session:
            return "(新会话, 提交首个任务后由 LLM 固化身份)"
        name_zh = self.session.get("name_zh", "")
        if name_zh:
            return (f"{name_zh}({self.session.get('name_en', '')}) · "
                    f"{self.session.get('task_type', '')} · {self.session.get('session_id', '')}")
        return f"会话 {self.session.get('session_id', '')} (身份待固化)"

    def all_tool_schemas(self, policy=None):
        schemas = TOOLS_SCHEMA + self.mcp.schemas
        if policy and not policy.is_noop():
            schemas = policy.filter_schemas(schemas)
        return schemas

    # ---- 人机协同实时共驾 ----
    def add_steer(self, text):
        """任务执行中注入用户协同指令(线程安全), 下一轮 LLM 调用前生效"""
        text = (text or "").strip()
        if not text:
            return False
        with self._steer_lock:
            self.steer_queue.append(text)
        self.log.info("steer queued: %s", text[:80])
        return True

    def _drain_steer(self):
        """取出待处理的协同指令(FIFO), 返回列表"""
        with self._steer_lock:
            out, self.steer_queue = self.steer_queue, []
        return out

    # ---- 手动终止 ----
    def request_stop(self):
        """请求终止当前任务(线程安全, 可跨线程调用)。
        当前步骤执行完毕后停止, 并保证消息链完整可继续对话。"""
        self._stop_event.set()

    def repair_context(self):
        """修复被中断/损坏的消息链(阻塞性BUG修复), 全链单遍扫描:
        ① assistant.tool_calls 缺失配对 tool 响应 → 就地补齐占位结果;
        ② 孤立 tool 消息(无前置 assistant tool_call 配对) → 不再删除(原多轮 while 删除逻辑会误删合法 tool 结果导致 LLM 上下文不完整),
           改为降级转换成 user 角色的上下文消息, 内容完整保留。"""
        if not self.messages:
            return
        repaired, pending_ids = [], set()
        fixed_orphan = fixed_missing = 0
        for m in self.messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                # 上一组 tool_calls 仍有未配对响应 → 先补齐占位
                for tcid in sorted(pending_ids):
                    repaired.append({"role": "tool", "tool_call_id": tcid,
                                     "content": "[已终止] 用户中断了任务执行"})
                    fixed_missing += 1
                pending_ids = {tc.get("id") for tc in m["tool_calls"] if tc.get("id")}
                repaired.append(m)
            elif role == "tool":
                tcid = m.get("tool_call_id")
                if tcid in pending_ids:
                    pending_ids.discard(tcid)
                    repaired.append(m)
                else:
                    # 孤立 tool 消息: 保留内容, 降级为 user 上下文(不丢信息)
                    repaired.append({"role": "user", "content":
                                     "[历史工具执行结果(上下文修复保留)]\n"
                                     + str(m.get("content", ""))[:4000]})
                    fixed_orphan += 1
            else:
                for tcid in sorted(pending_ids):
                    repaired.append({"role": "tool", "tool_call_id": tcid,
                                     "content": "[已终止] 用户中断了任务执行"})
                    fixed_missing += 1
                pending_ids = set()
                repaired.append(m)
        for tcid in sorted(pending_ids):   # 尾部未配对的 tool_calls
            repaired.append({"role": "tool", "tool_call_id": tcid,
                             "content": "[已终止] 用户中断了任务执行"})
            fixed_missing += 1
        self.messages = repaired
        if fixed_orphan or fixed_missing:
            self.log.warning("repair_context: 补齐缺失tool响应=%d, "
                             "孤立tool消息降级保留=%d", fixed_missing, fixed_orphan)

    def _emit(self, event, payload):
        """向调度器上报进度(不影响交互模式)"""
        if self.progress_cb:
            try:
                self.progress_cb(event, payload)
            except Exception as _e:
                self.log.warning("忽略异常(omni_agent/agent.py:206): %s: %s", type(_e).__name__, _e)
                pass

    # ---- 任务主循环 ----
    def run_task(self, user_input, tool_policy=None):
        """执行单个任务。tool_policy: 本次任务级工具黑白名单(优先于会话级策略);
        auto 模式在任务身份固化后按 task_type 自动匹配工具范围。"""
        # 复用上游(Web 请求入口)已绑定的 tracker_id, 仅 CLI 直接调用时新建,
        # 保证 前端请求→任务提交→LLM请求→工具执行→交付 全链路同一标记
        from .logger import get_tracker
        tid = get_tracker()
        if not tid or tid == "-":
            tid = new_tracker("task")
        self.log.info("run_task start: tracker=%s input=%s", tid, user_input[:80])
        self._before_task(user_input)
        self._stop_event.clear()   # 新任务开始: 复位手动终止信号
        self._last_tool_fp = None  # 复位工具重复调用检测状态
        self._dup_tool_count = 0
        # 交付保障状态 —— 跟踪本任务是否产出文件/是否已推送交付卡片
        deliver_nudged = False
        produced_file = False
        delivered_n0 = len(self.tools.delivered)
        policy = tool_policy or self.tool_policy
        if policy and not policy.is_noop():
            policy.resolve(task_type=(self.session or {}).get("task_type", ""),
                           intent=user_input)
            # 关键决策日志 —— 工具策略变更
            self.log.info("tool_policy: %s", policy.describe())
            cprint(f"  ⛨ 工具策略: {policy.describe()}", C.MAGENTA)
        else:
            policy = None
         
        matched = self.skills.match(user_input)
        hint = (f"\n[系统提示: 检测到语义匹配技能 {[m['name'] for m in matched[:2]]}, "
                f"可 load_skills(action=load) 加载]" if matched else "")
        # 垂直领域异构执行引擎 —— 按当前意图路由领域画像, 注入差异化
        # 执行策略(工具偏好/验收标准/领域准则), 本地加权评分零 LLM 开销
        self.domain = DomainRouter.classify(
            user_input, (self.session or {}).get("task_type", ""))
        # 关键决策日志 —— 领域路由变更
        self.log.info("domain_route: id=%s name=%s confidence=%s strategy=%s",
                      self.domain.get("id"), self.domain.get("name"),
                      self.domain.get("confidence"),
                      self.domain.get("strategy_brief", "")[:80])
        # 设置 LLM 客户端的路由上下文(domain id + task_type), 供多模智能路由决策
        self.llm._current_domain = self.domain.get("id", "")
        self.llm._current_task_type = (self.session or {}).get("task_type", "")
        if self.domain["id"] != "general":
            hint += "\n" + self.domain["injection"]
            cprint(f"   领域路由: {self.domain['name']}"
                   f" (置信 {self.domain['confidence']}%) · "
                   f"{self.domain['strategy_brief']}", C.MAGENTA)
        self._emit("domain", self.domain)
        # 系统级提示(技能匹配/领域策略)不再拼接进用户消息 —— 改为独立
        # 注入消息([系统提示] 开头, 时间线回放/前端气泡/恢复摘要均自动过滤),
        # 修复领域引擎策略文本追加显示在用户消息气泡内的问题
        self.messages.append({"role": "user", "content": user_input})
        if hint.strip():
            self.messages.append({"role": "user", "content":
                                  "[系统提示] 本任务执行策略参考(对用户不可见):"
                                  + hint})

        max_turns = S.get_int("agent.max_turns", MAX_AGENT_TURNS)
        self.log.info(f"============ 上下文消息数量：{len(self.messages)} ============ ")
        for turn in range(max_turns):
            if self._stop_event.is_set():   # 手动终止检查点(轮次级)
                cprint("\n  ⏹ 任务已被用户手动终止", C.YELLOW)
                self._after_task(user_input)
                self._emit("done", {"summary": "任务已被用户手动终止", "stopped": True,
                                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
                return True
            # 人机协同共驾 —— 每轮开始前吸收用户实时协同指令(插入位置
            # 安全: 位于轮次边界, 不会破坏 tool_use/tool_result 相邻配对)
            for _steer in self._drain_steer():
                # 人机协同提示词后端化 —— 注入消息以内部标记开头,
                # rebuild_timeline 与前端气泡均自动过滤, 用户不可见
                self.messages.append({"role": "user", "content":
                    "[协同透传] " + _steer})
                self.log.info("steer injected (invisible to UI): %s", _steer[:120])
                cprint(f"\n  🤝 已吸收协同指令: {_steer[:80]}", C.CYAN)
                self._emit("steer_ack", {"text": _steer})
            self._emit("turn", {"turn": turn + 1, "max_turns": max_turns,
                                "todos": self.tools.todo_progress()})
            try:
                r = self.llm.chat(self.messages, tools=self.all_tool_schemas(policy))
            except Exception as e:
                cprint(f"\n 模型请求失败: {e}", C.RED)
                # 关键决策日志 —— 异常报错完整堆栈
                import traceback as _tb
                self.log.error("llm request failed: %s: %s\n%s",
                               type(e).__name__, e, _tb.format_exc())
                
                # 不直接弹出用户消息(若弹出, 下次"继续"时模型丢失上下文);
                # 改为修复消息链(补占位/降级孤立 tool), 保留全部已完成步骤
                self.repair_context()
                # 失败退出前持久化上下文 —— 已完成步骤(工具结果)不丢失,
                # 重试/备用模型接管后不再重复执行之前的步骤
                if self.session:
                    try:
                        self.sessions.save_context(self.session, self.messages,
                                                   last_task=user_input[:80])
                    except OSError as _e:
                        self.log.warning("失败退出时上下文持久化失败: %s", _e)
                # 携带 resumable 标记 —— 前端提示"发送'继续'在断点处续跑"
                self._emit("error", {"detail": f"模型请求失败: {e}",
                                     "resumable": True})
                return False
            assistant = {"role": "assistant", "content": r["content"] or ""}
            if r["tool_calls"]:
                assistant["tool_calls"] = r["tool_calls"]
            # 思考内容随消息链持久化(内部 _ 前缀字段, 发送 LLM 前自动剥离),
            # 刷新页面/切换会话后时间线可回放思考过程
            if r.get("reasoning"):
                assistant["_reasoning"] = str(r["reasoning"])[:20000]
            self.messages.append(assistant)
            if not r["tool_calls"]:
                # 交付保障 —— 本任务产出过文件但从未推送交付卡片时,
                # 注入一次性提醒让模型补交付(或确认无需交付), 防止有实质
                # 产物却静默结束
                if (produced_file and not deliver_nudged and self.mode != "chat"
                        and len(self.tools.delivered) == delivered_n0):
                    deliver_nudged = True
                    self.messages.append({"role": "user", "content":
                        "[系统提示] 检测到本任务已产出文件但尚未推送交付物。"
                        "若存在面向用户的实质交付物, 请立即调用 send_user_msg "
                        "推送交付卡片(仅限真实存在的文件/可达链接, 严禁虚报); "
                        "若产出仅为中间过程文件、确无需交付, 直接简要总结结束。"})
                    cprint("\n  ⚑ 交付保障: 检测到产物未推送, 已提醒模型补交付",
                           C.MAGENTA)
                    continue
                # 已移除"交付后强制终止"引导 —— 任务是否结束完全由
                # 模型根据用户需求完成度自主判断(交付非终点, 剩余需求继续执行)
                self.log.info("run_task done: turns=%d", turn + 1)
                cprint(f"\n  ✓ 任务完成 · {time.strftime('%Y-%m-%d %H:%M:%S')}", C.GREEN)
                self._after_task(user_input)
                self._last_summary = (r["content"] or "")[:500]   # 供续航建议
                self._emit("done", {"summary": self._last_summary,
                                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
                return True  # 任务结束
            # v8.2 阻塞性修复(HTTP 400 tool_use/tool_result 配对): 同一 assistant
            # 消息的多个 tool_calls 响应必须连续相邻 —— Reflexion 纠偏等注入消息
            # 一律缓冲到本批工具全部落链后再追加, 禁止插入 tool 响应之间
            deferred_injections = []
            restored_break = False
            for tc in r["tool_calls"]:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                    # 关键决策日志 —— 工具参数(长文本截断至200字符)
                    _arg_brief = {k: (str(v)[:200] + '...' if len(str(v)) > 200 else v)
                                  for k, v in args.items()}
                    self.log.info("tool args: %s(%s)", name, _arg_brief)
                except json.JSONDecodeError:
                    args = {}
                    self.log.warning("tool args parse failed: %s", name)
                brief = (args.get("name") or args.get("path") or args.get("pattern")
                         or args.get("command") or args.get("url") or "")
                if not brief and isinstance(args.get("questions"), list) and args["questions"]:
                    brief = args["questions"][0].get("question", "")
                 
                if not brief and isinstance(args.get("tasks"), list) and args["tasks"]:
                    t0_ = args["tasks"][0]
                    brief = t0_.get("prompt", "") if isinstance(t0_, dict) else str(t0_)

                if args.get("title") :
                    brief = f"【{args.get('title')}】 {brief}"
                cprint(f"\n  ⏺ {name}({str(brief)[:120]})", C.BLUE)
                self.log.info("tool call: %s(%s)", name, str(brief)[:100])
                self._emit("tool", {"tool": name, "brief": str(brief)[:120]})
                t0 = time.time() 
                fp = (name, json.dumps(args, sort_keys=True, ensure_ascii=False))
                is_dup = (fp == self._last_tool_fp)
                if is_dup:
                    self._dup_tool_count += 1
                else:
                    self._dup_tool_count = 0
                self._last_tool_fp = fp
                _bypass = True   # 旁路标记(未经 tools.run 包装器执行)
                if self._stop_event.is_set():   # 终止后跳过执行但保留响应占位
                    result = "[已终止] 用户手动终止了当前任务, 停止一切后续操作"
                elif policy and not policy.permits(name):
                    result = (f"[已拒绝] 工具 {name} 被当前任务的工具策略禁用"
                              f"({policy.describe()}), 请改用允许范围内的工具完成任务")
                elif is_dup and self._dup_tool_count >= 2 \
                        and name not in ("ask_user_question", "bash",
                                         "web_fetch", "read_file"):
                    # 拦截阈值放宽 —— 首次重复即拦截过于激进(bash 轮询
                    # 服务状态、web_fetch 重试、read_file 复查文件均属合理重复),
                    # 现连续第3次完全相同参数才拦截, 且轮询/读取类工具豁免
                    result = (f"[重复调用已拦截] 工具 {name} 与上一次调用的参数完全"
                              f"相同(连续第 {self._dup_tool_count + 1} 次), 已跳过执行。"
                              f"上一次的执行结果仍然有效, 请直接使用该结果继续任务; "
                              f"若上次结果不满足需求, 请调整参数或改用其他工具/策略, "
                              f"禁止原样重试。")
                    self.log.warning("duplicate tool call blocked: %s (count=%d)",
                                     name, self._dup_tool_count + 1)
                else:
                    result = self.tools.run(name, args)
                    _bypass = False
                if _bypass:
                     
                    self._emit("tool_skip", {"tool": name,
                                             "result": str(result)[:1000],
                                             "dt": round(time.time() - t0, 1)})
                dt = time.time() - t0
                # 交付保障跟踪 —— 记录成功的文件产出与交付动作
                if name == "write_file" and not str(result).startswith(
                        ("[失败]", "[已拒绝]", "[错误]")):
                    produced_file = True
                first = str(result).splitlines()[0][:200] if str(result) else ""
                cprint(f"  ⎿ {first}  {C.GRAY}({dt:.1f}s){C.R}", C.GREEN)
                self.log.info("tool done: %s dt=%.1fs result=%s", name, dt, first[:80])
                 
                self.messages.append({"role": "tool", "tool_call_id": tc["id"],
                                      "content": str(result), "_dt": round(dt, 1)})
                # Reflexion 自愈反思 —— 连续失败监测
                if self.reflexion.observe(name, result):
                    _n_fail = self.reflexion.consecutive
                    injection = self.reflexion.reflect(user_input[:200])
                    if injection:
                        deferred_injections.append(injection)
                        cprint(f"\n  ♻ Reflexion 自愈反思已触发(连续 {_n_fail} 次失败, 纠偏策略将在本批工具后注入)", C.MAGENTA)
                if self.tools.restored_context:   # checkpoint rollback 恢复上下文
                    self.messages = self.tools.restored_context
                    self.tools.restored_context = None 
                    restored_break = True
                    cprint("  ⛃ 会话上下文已回滚恢复(本批剩余工具已跳过)", C.MAGENTA)
                    break
            if restored_break:
                self.repair_context()
            for injection in deferred_injections:
                self.messages.append({"role": "user", "content": injection})
        
        cprint(f"\n 已达单任务最大轮次({max_turns}), 任务未完成已暂停。"
               f"可发送新消息(如‘继续’)接着执行剩余步骤。", C.YELLOW)
        self.log.warning("run_task max turns reached: %d", max_turns)
        self.messages.append({"role": "user", "content":
                              f"[系统提示] 已达单任务最大轮次({max_turns}), "
                              f"任务被中断。请在下一次对话中优先完成剩余步骤。"})
        self._after_task(user_input)
        self._emit("done", {"summary": f" 已达单任务最大轮次({max_turns}), "
                                       f"任务未完成已暂停, 可发送‘继续’接着执行",
                            "max_turns_reached": True,
                            "max_turns": max_turns,
                            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        return True

    def _after_task(self, user_input):
        """任务收尾: 持久化上下文 + 异步蒸馏经验 + 失败记录持久化"""
        # 项目记忆自动沉淀 —— 任务完成自动追加任务足迹到 HAISNAP.md
        # (修复项目记忆长期为空: 此前仅依赖用户手动 '# 备注' 写入, 无自动通道)
        try:
            brief = " ".join((user_input or "").split())[:80]
            if brief and not brief.startswith(("[系统", "[Hook")):
                mp = os.path.join(self.wd, MEMORY_FILE)
                existing = ""
                if os.path.isfile(mp):
                    with open(mp, encoding="utf-8", errors="replace") as f:
                        existing = f.read()
                if brief not in existing:
                    header = ("" if existing
                              else "# 项目记忆 (omni-agent 自动加载)\n\n")
                    with open(mp, "a", encoding="utf-8") as f:
                        f.write(header + f"- [{time.strftime('%Y-%m-%d %H:%M')}]"
                                         f" 已完成任务: {brief}\n")
        except OSError as e:
            self.log.warning("项目记忆自动写入失败: %s", e)
        if self.session:
            try:
                self.sessions.save_context(self.session, self.messages,
                                           last_task=user_input[:80])
            except OSError as e:
                cprint(f"  ! 上下文持久化失败: {e}", C.GRAY)
        # 任务续航 —— 异步蒸馏下一步行动建议(非阻塞)
        try:
            self.continuum.suggest_async(
                user_input, summary=getattr(self, '_last_summary', ''))
        except Exception as e:
            self.log.warning("续航建议启动失败(不影响任务): %s", e)
        t = self.memory.learn_async(self.llm, self.messages, user_input)
        self._bg_threads.append(t)

    # ---- 任务前置: 加载上下文 + 首次执行时固化任务身份 ----
    def _before_task(self, user_input):
        if not self._context_loaded:
            self._context_loaded = True
            if self.session:
                ctx = self.sessions.load_context(self.session)
                if ctx:
                    self.messages = ctx
                    # 恢复的持久化上下文可能含中断损坏的消息链(孤立
                    # tool_result / 未配对 tool_calls), 直接发送必触发
                    # Anthropic 系网关 HTTP 400, 恢复后立即修复
                    self.repair_context()
                    cprint(f"  ⛃ 已恢复会话 [{self.session['session_id']}] "
                           f"上下文({len(ctx)} 条消息, 消息链已校验)", C.GRAY)
                    self._inject_resume_digest(ctx)   # v4.0.0: 会话恢复摘要

        meta = None
        if not self.session:
            meta = self._gen_task_identity(user_input)
            if meta.get("name_en"):
                self._rename_workdir(meta["name_en"])
            self.session = self.sessions.create(meta["name_zh"], meta["name_en"],
                                                meta["task_type"], self.wd, user_input)
            self.tools.session = self.session
        elif not self.session.get("name_en"):
            meta = self._gen_task_identity(user_input)
            self.session = self.sessions.update(
                self.session["session_id"], name_zh=meta["name_zh"],
                name_en=meta["name_en"], task_type=meta["task_type"],
                workdir=self.wd) or self.session
            self.tools.session = self.session
            if not self.session.get("_dir_renamed") \
                    and self.session.get("name_en"):
                if self._rename_workdir(self.session["name_en"]):
                    self.sessions.update(self.session["session_id"],
                                         workdir=self.wd, _dir_renamed=True)
        # v4.0.0: 任务类型→规划模式自动路由(chat 类任务自动启用轻量对话模式,
        # 仅在用户未显式切换模式时生效, 尊重手动选择的 auto/chat 模式)
        if meta and meta.get("task_type") == "chat" and self.mode == "checklist":
            self.mode = "chat"
            cprint("  ✓ 检测到 chat 类型任务, 已自动切换 [chat] 对话模式"
                   "(直接文本回复, 必要时检索网络)", C.GREEN)
        # 每次对话都会先更新系统提示词
        self.messages[0] = {"role": "system", "content": self._system()}
         
        sp = self.messages[0]
        if sp.get("role") != "system" or "omni-agent" not in str(sp.get("content", "")):
            self.messages[0] = {"role": "system", "content": self._system()}
            self.log.warning("system prompt 校验失败, 已强制重建")
        dup_sys = [i for i, m in enumerate(self.messages[1:], 1)
                   if m.get("role") == "system"]
        if dup_sys:
            self.messages = [self.messages[0]] + [
                m for i, m in enumerate(self.messages[1:], 1) if i not in dup_sys]
            self.log.warning("移除 %d 条冗余 system 消息", len(dup_sys))
         
        self.tools._read_versions.clear()
        self.tools._read_skipped.clear()   # 新任务复位"免重读"提示状态
        if meta:
            cprint(f"  ✓ 任务身份已固化: {meta['name_zh']}({meta['name_en']}) "
                   f"· 类型: {meta['task_type']} · 会话: {self.session['session_id']}", C.GREEN)

    def _inject_resume_digest(self, ctx):
        """v4.0.0 新增: 会话恢复摘要 —— 长会话跨进程恢复时自动提炼近期任务脉络,
        以轻量上下文消息注入, 避免模型冷启动丢失任务连续性(纯本地提取, 零LLM开销)。"""
        if len(ctx) < 12:
            return
        tasks, last_reply = [], ""
        for m in ctx:
            role, content = m.get("role"), str(m.get("content", ""))
            if role == "user" and content.strip() and not content.startswith(
                    ("[历史工具", "[系统提示]", "[会话恢复摘要]")):
                t = " ".join(content.split())[:80]
                if t:
                    tasks.append(t)
            elif role == "assistant" and str(content).strip():
                last_reply = " ".join(str(content).split())[:200]
        if not tasks:
            return
        digest = ("[会话恢复摘要] 本会话已跨进程恢复, 近期任务脉络: "
                  + " → ".join(tasks[-5:])
                  + (f" | 最近结论: {last_reply}" if last_reply else "")
                  + " | 请基于以上上下文无缝衔接后续任务, 禁止重复已完成的工作。")
        self.messages.append({"role": "user", "content": digest})
        cprint("  ⛃ 已注入会话恢复摘要(近期任务脉络, v4.0.0)", C.GRAY)

    def _rename_workdir(self, name_en):
        """自动命名的目录以英文名重建独立且唯一的工作目录
        os.rename 失败(Windows 下进程 CWD/文件句柄占用目录)时依次降级:
        ① 将进程 CWD 挪出目标目录后重试 ② shutil.move 复制迁移;
        修复首次对话后项目工作目录未按 LLM 英文名重建的问题"""
        parent = os.path.dirname(self.wd) or self.sessions.root
        target = os.path.join(parent, name_en)
        if os.path.abspath(target) == self.wd:
            return False
        if os.path.exists(target):
            target += "_" + uuid.uuid4().hex[:4]
        src = self.wd
        try:
            os.rename(src, target)
        except OSError as e1:
            self.log.warning("os.rename 迁移失败, 启动降级路径: %s", e1)
            try:
                cwd = os.path.realpath(os.getcwd())
                rsrc = os.path.realpath(src)
                if cwd == rsrc or cwd.startswith(rsrc + os.sep):
                    os.chdir(parent)   # Windows: 进程 CWD 占用导致重命名失败
                os.rename(src, target)
            except OSError:
                try:
                    import shutil
                    shutil.move(src, target)
                except (OSError, shutil.Error) as e3:
                    cprint(f"  ! 工作目录迁移失败, 沿用原目录: {e3}", C.YELLOW)
                    self.log.warning("工作目录迁移最终失败(沿用原目录): %s", e3)
                    return False
        self._rebind_workdir(target)
        cprint(f"  ✓ 已创建任务工作目录: {self.wd}", C.GREEN)
        return True

    def _gen_task_identity(self, user_input):
        """通过 LLM 生成任务中文名/英文名/任务类型(失败降级时间戳命名)
        ① json_format 不被端点支持(HTTP 400 不重试)时自动降级普通文本重试;
        ② 严格校验 LLM 返回有效性(空名视为失败进入重试);
        修复 Web 端首次对话偶发未获取到 LLM 生成任务名的问题"""
        fallback = {"name_zh": "任务" + time.strftime("%m%d%H%M"),
                    "name_en": "task_" + time.strftime("%Y%m%d_%H%M%S"),
                    "task_type": "general"}
        last_err = None
        for json_fmt in (True, False):
            try:
                r = self.llm.chat([{"role": "system", "content": NAMER_PROMPT},
                                   {"role": "user", "content": user_input[:500]}],
                                  stream=False, json_format=json_fmt, internal=True)
                m = re.search(r"\{[^{}]*\}", r["content"] or "", re.S)
                meta = json.loads(m.group(0)) if m else {}
                name_en = re.sub(r"[^a-z0-9_]+", "_",
                                 str(meta.get("name_en", "")).lower()).strip("_")[:24]
                name_zh = str(meta.get("name_zh", "")).strip()[:20]
                if not (name_zh or name_en):
                    raise ValueError("LLM 未返回有效任务名: "
                                     + str(r.get("content", ""))[:120])
                return {"name_zh": name_zh or fallback["name_zh"],
                        "name_en": name_en or fallback["name_en"],
                        "task_type": (str(meta.get("task_type", "")).strip()
                                      or "general")[:16]}
            except Exception as e:
                last_err = e
                self.log.warning("任务命名尝试失败(json_format=%s): %s: %s",
                                 json_fmt, type(e).__name__, e)
        cprint(f"  ! LLM 任务命名失败, 降级默认命名: {last_err}", C.GRAY)
        return fallback

    def _rebind_workdir(self, new_wd):
        """切换/迁移工作目录后, 重建所有目录绑定组件"""
        self.wd = os.path.abspath(new_wd)
        os.makedirs(os.path.join(self.wd, SETTINGS_DIR), exist_ok=True)
        settings = load_settings(self.wd)
        S.init(self.wd)   # 配置链跟随工作目录切换重新绑定项目级配置
        self.hooks = Hooks(self.wd, settings)
        self.ckpt = CheckpointManager(self.wd)
        # 继承已勾选技能集合, 修复切换/迁移目录后已启用技能被静默重置
        self.skills = SkillsManager(self.wd, loaded=self.skills.loaded)
        self.tools = ToolRunner(self.wd, self.perm, self.hooks, self.sources,
                                self.connectors, self.ckpt, self.skills, self.mcp,
                                self.llm, self.session)

    # ---- 会话管理命令(/sessions /session new|switch|info) ----
    def cmd_session(self, arg):
        parts = arg.split()
        sub = parts[0] if parts else "list"
        if sub == "list":
            items = self.sessions.list()
            if not items:
                cprint("  (暂无会话记录: 提交首个任务后自动注册)", C.GRAY)
            cur = self.session["session_id"] if self.session else None
            for it in items:
                mark = "▶" if it["session_id"] == cur else " "
                cprint(f"  {mark} {it['session_id']}  {it.get('name_zh', '')}"
                       f"({it.get('name_en', '')})  [{it.get('task_type', '')}]  "
                       f"{it.get('updated', '')}  {it.get('last_task', '')[:36]}", C.CYAN)
            cprint("  用法: /session new | /session switch <会话ID> | /session info", C.GRAY)
            return
        if sub == "new":
            if self.session:
                self.sessions.save_context(self.session, self.messages)
            d = os.path.join(self.sessions.root, "session_" + time.strftime("%Y%m%d_%H%M%S")
                             + "_" + uuid.uuid4().hex[:4])
            os.makedirs(d, exist_ok=True)
            self.session = None
            self._rebind_workdir(d)
            self.messages = [{"role": "system", "content": self._system()}]
            self._context_loaded = True
            cprint(f"  ✓ 已开启新会话(首个任务提交后由 LLM 固化身份) · 目录: {self.wd}", C.GREEN)
            return
        if sub == "switch":
            if len(parts) < 2:
                cprint("  用法: /session switch <会话ID>(先 /sessions 查看)", C.YELLOW)
                return
            target = self.sessions.get(parts[1])
            if not target:
                cprint(f"   会话不存在: {parts[1]}", C.RED)
                return
            if self.session and target["session_id"] == self.session["session_id"]:
                cprint("  已处于该会话, 无需切换", C.GRAY)
                return
            if self.session:
                self.sessions.save_context(self.session, self.messages)
            self.session = target
            self.wd = os.path.abspath(target["workdir"])
            os.makedirs(self.wd, exist_ok=True)
            self._rebind_workdir(self.wd)
            ctx = self.sessions.load_context(target)
            self.messages = ctx or [{"role": "system", "content": self._system()}]
            if ctx:
                self.repair_context()   # 切换会话后校验消息链完整性
            self._context_loaded = True
            cprint(f"  ✓ 已切换会话 [{target['session_id']}] {target.get('name_zh', '')}"
                   f" · 目录: {self.wd}"
                   + (f" · 已恢复 {len(ctx)} 条消息" if ctx else " · 全新上下文"), C.GREEN)
            return
        if sub == "info":
            if not self.session:
                cprint("  当前为新会话(尚未注册, 提交首个任务后固化)", C.GRAY)
                return
            cprint(json.dumps(self.session, ensure_ascii=False, indent=2), C.CYAN)
            return
        cprint("  用法: /sessions | /session new | /session switch <ID> | /session info", C.YELLOW)

    # ---- 其他会话命令 ----
    def cmd_cost(self):
        cprint(f"  模型调用 {self.llm.calls} 次 | prompt {self.llm.total_prompt} tok"
               f" | completion {self.llm.total_completion} tok"
               f" | 合计 {self.llm.total_prompt + self.llm.total_completion} tok", C.CYAN)

    def cmd_compact(self):
        cprint("  上下文压缩功能已移除; 项目文件备份采用自动快照机制(交付时固化, 部署/预览指向快照目录)", C.GRAY)

    def cmd_memory(self):
        mp = os.path.join(self.wd, MEMORY_FILE)
        if os.path.isfile(mp):
            cprint("── 项目记忆 ──", C.CYAN)
            with open(mp, encoding="utf-8") as f:
                cprint(f.read()[:2000], C.CYAN)
        else:
            cprint(f"  暂无 {MEMORY_FILE}。用 '# 备注内容' 快速追加记忆。", C.GRAY)
        cprint("── 跨会话全局记忆(lessons.jsonl) ──", C.CYAN)
        cprint(self.memory.load(2000), C.GRAY)

    _MODE_DESC = {"checklist": "计划模式: 复杂任务先列计划逐步执行",
                  "auto": "探索模式: 自由决定执行路径直至交付",
                  "chat": "对话模式: 直接文本回复, 必要时检索网络, 无需交付文件"}

    def cmd_mode(self, arg):
        if arg in self._MODE_DESC:
            self.mode = arg
            self.messages[0] = {"role": "system", "content": self._system()}
            cprint(f"  ✓ 已切换到 [{arg}] {self._MODE_DESC[arg]}", C.GREEN)
        else:
            cprint(f"  当前模式: {self.mode} "
                   f"(计划模式=checklist | 探索模式=auto | 对话模式=chat)", C.CYAN)

    def cmd_thinking(self, arg):
        if arg in ("on", "off"):
            self.llm.thinking = (arg == "on")
            cprint(f"  ✓ thinking 模式已{'开启' if arg == 'on' else '关闭'}", C.GREEN)
        else:
            cprint(f"  当前 thinking: {'on' if self.llm.thinking else 'off'} "
                   f"(用法: /thinking on|off)", C.CYAN)

    def cmd_policy(self, arg):
        """/policy 命令: 查看/设置会话级工具黑白名单策略
        用法: /policy | /policy allow=web_search,read_file deny=bash auto | /policy off"""
        if not arg:
            if self.tool_policy and not self.tool_policy.is_noop():
                cprint(f"  当前工具策略: {self.tool_policy.describe()}", C.CYAN)
            else:
                cprint("  当前无工具策略限制(全量工具可用)", C.GRAY)
            cprint("  用法: /policy allow=a,b deny=c,d auto|manual | /policy off", C.GRAY)
            return
        if arg.strip().lower() in ("off", "clear", "none"):
            self.tool_policy = None
            cprint("  ✓ 工具策略已清除(恢复全量工具)", C.GREEN)
            return
        allow, deny, auto = [], [], False
        for part in arg.split():
            if part.startswith("allow="):
                allow = [x for x in part[6:].split(",") if x.strip()]
            elif part.startswith("deny="):
                deny = [x for x in part[5:].split(",") if x.strip()]
            elif part == "auto":
                auto = True
        p = ToolPolicy(allow=allow, deny=deny, auto=auto,
                       task_type=(self.session or {}).get("task_type", ""))
        if p.is_noop():
            cprint("   未解析到有效策略(示例: /policy allow=web_search deny=bash auto)",
                   C.YELLOW)
            return
        self.tool_policy = p
        preview = p.resolve(task_type=(self.session or {}).get("task_type", ""))
        n = len(self.all_tool_schemas(preview))
        cprint(f"  ✓ 工具策略已生效: {p.describe()} · 可用工具 {n} 个", C.GREEN)

    def add_memory(self, note):
        mp = os.path.join(self.wd, MEMORY_FILE)
        header = "" if os.path.isfile(mp) else "# 项目记忆 (omni-agent 自动加载)\n\n"
        with open(mp, "a", encoding="utf-8") as f:
            f.write(header + f"- {note}\n")
        self.messages[0] = {"role": "system", "content": self._system()}
        cprint(f"  ✓ 已写入 {MEMORY_FILE} 并刷新上下文", C.GREEN)

    def close(self):
        if self.session:   # 退出前持久化当前会话上下文
            try:
                self.sessions.save_context(self.session, self.messages)
            except OSError as _e:
                self.log.warning("忽略异常(omni_agent/agent.py:618): %s: %s", type(_e).__name__, _e)
                pass
        self.mcp.close()
        self.tools.shutdown()
        for t in self._bg_threads:
            t.join(timeout=3)   # 等待后台学习线程收尾