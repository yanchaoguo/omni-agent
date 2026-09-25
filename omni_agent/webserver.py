# -*- coding: utf-8 -*-
"""网页版 UI 服务器(v3.3 新增, 纯标准库实现, 功能与 CLI 一致)

创新点(对标 Codex CLI / 国内主流 Agent 工具):
- 实时驾驶舱   : SSE 推流 思考过程/回答/工具时间线/任务清单/Token成本, 全程透明可观测
- 远程人机协同 : 敏感命令审批与 ask_user_question 澄清提问桥接到网页, 倒计时自动兜底
- 任务级工具策略: 每次任务可独立指定黑白名单或 auto 按任务类型自动匹配工具范围
- 成果物中心   : 交付成功自动登记成果物, 网页端在线预览(HTML/图片/文本/PDF/音视频)与一键下载
- 全功能对齐   : 会话/记忆/快照回滚/上下文蒸馏/信源溯源 与 CLI 同一内核, 零功能阉割
"""
import json
import os
import queue
import re
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import base64

from .agent import Agent
from .config import MEMORY_FILE, MODEL, SETTINGS_DIR, SKIP_DIRS, __version__
from .envstore import EnvStore
from .logger import get_logger, new_tracker, set_tracker
from .tool_policy import TASK_TYPE_PRESETS, ToolPolicy
from .tools_schema import TOOL_TITLES
from .ui import C, cprint

wlog = get_logger("web")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
APPROVAL_TIMEOUT = 90     # 网页审批倒计时(秒), 超时按默认策略放行并标注
QUESTION_TIMEOUT = 120    # 网页澄清提问倒计时(秒), 超时自动采用第一个选项
MAX_SERVE_BYTES = 50 * 1024 * 1024   # 预览/下载单文件上限 50MB

# 在线预览 MIME 映射(inline); 未命中的按二进制处理(仅允许下载)
MIME_MAP = {
    ".html": "text/html; charset=utf-8", ".htm": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8", ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8", ".py": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8", ".log": "text/plain; charset=utf-8",
    ".tsv": "text/plain; charset=utf-8",
    ".xml": "text/plain; charset=utf-8", ".yml": "text/plain; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8", ".sh": "text/plain; charset=utf-8",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".ico": "image/x-icon", ".pdf": "application/pdf",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
}

# 大多数代码/配置文本文件均可在线预览(workspace_read 以文本读取),
# 修复此前仅按 MIME_MAP 判定导致 .ts/.go/.java/.sql 等被误判为不可预览
CODE_TEXT_EXT = {
    ".ts", ".tsx", ".jsx", ".vue", ".mjs", ".cjs", ".go", ".java", ".kt",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".cs", ".rs", ".rb", ".php", ".pl",
    ".swift", ".scala", ".lua", ".r", ".sql", ".toml", ".ini", ".cfg",
    ".conf", ".env", ".properties", ".gradle", ".bat", ".ps1", ".dockerfile",
    ".makefile", ".gitignore", ".editorconfig", ".tex", ".rst", ".diff",
    ".patch", ".proto", ".graphql", ".sass", ".scss", ".less", ".markdown",
}


# 表格类文件在线预览(csv/tsv 用 csv 模块, xlsx 用 zipfile+xml 纯标准库解析;
# .xls 旧二进制格式标准库无法解析, 仍仅支持下载)
TABLE_EXT = {".csv", ".tsv", ".xlsx"}
TABLE_MAX_ROWS = 500     # 表格预览最大行数
TABLE_MAX_COLS = 100     # 表格预览最大列数


# 直达预览链接 wd token 改用 16 位 md5 编码(替换原 base64):
# 更短更整洁且不泄露目录结构; token->wd 映射持久化到 web_state.json
_WD_TOKENS = {}
_WD_TOKENS_LOCK = threading.Lock()


def wd_token(wd):
    """工作目录 -> 16位 md5 token(注册进映射表供 /p/ 路由反查)"""
    import hashlib as _hl
    real = os.path.realpath(wd)
    tok = _hl.md5(real.encode("utf-8")).hexdigest()[:16]
    with _WD_TOKENS_LOCK:
        _WD_TOKENS[tok] = real
    return tok


def wd_from_token(tok):
    """token -> 工作目录; 兼容历史 base64 链接(md5 未命中时回退解码)"""
    with _WD_TOKENS_LOCK:
        wd = _WD_TOKENS.get(tok)
    if wd:
        return wd
    try:   # 历史 base64 token 兼容
        dec = base64.urlsafe_b64decode(
            tok + "=" * (-len(tok) % 4)).decode("utf-8")
        if os.path.isdir(dec):
            return dec
    except (ValueError, UnicodeDecodeError):
        pass
    return ""


def _previewable(ext):
    """在线预览能力判定: MIME 内置类型 + 常见代码/配置文本类型 + 表格类型"""
    e = (ext or "").lower()
    if not e.startswith("."):
        e = "." + e
    return e in MIME_MAP or e in CODE_TEXT_EXT or e in TABLE_EXT


def _col_to_idx(ref):
    """单元格引用转列索引: 'A1'->0, 'BC12'->54"""
    col = 0
    for ch in ref:
        if ch.isalpha():
            col = col * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return max(0, col - 1)


def _read_xlsx(fp, max_rows=TABLE_MAX_ROWS, max_cols=TABLE_MAX_COLS):
    """纯标准库解析 xlsx(zip+xml): 共享字符串/多工作表/inlineStr 均支持"""
    import zipfile
    import xml.etree.ElementTree as ET
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rns = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    try:
        with zipfile.ZipFile(fp) as z:
            names = set(z.namelist())
            shared = []
            if "xl/sharedStrings.xml" in names:
                for si in ET.fromstring(
                        z.read("xl/sharedStrings.xml")).iter(ns + "si"):
                    shared.append("".join(t.text or ""
                                          for t in si.iter(ns + "t")))
            metas = [{"name": sh.get("name", "Sheet"),
                      "rid": sh.get(rns + "id", "")}
                     for sh in ET.fromstring(
                         z.read("xl/workbook.xml")).iter(ns + "sheet")]
            rels = {}
            if "xl/_rels/workbook.xml.rels" in names:
                for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")):
                    rels[r.get("Id")] = r.get("Target", "")
            sheets, truncated = [], False
            for meta in metas[:5]:   # 最多解析前5个工作表
                target = rels.get(meta["rid"], "")
                if not target:
                    continue
                if not target.startswith("xl/"):
                    target = "xl/" + target.lstrip("/")
                if target not in names:
                    continue
                rows = []
                for row in ET.fromstring(z.read(target)).iter(ns + "row"):
                    if len(rows) >= max_rows:
                        truncated = True
                        break
                    cells = {}
                    for c in row.findall(ns + "c"):
                        idx = _col_to_idx(c.get("r", "")) if c.get("r") \
                            else len(cells)
                        if idx >= max_cols:
                            truncated = True
                            continue
                        t, v = c.get("t", ""), c.find(ns + "v")
                        if t == "s" and v is not None:
                            try:
                                val = shared[int(v.text)]
                            except (ValueError, IndexError, TypeError):
                                val = v.text or ""
                        elif t == "inlineStr":
                            el = c.find(ns + "is")
                            val = "".join(x.text or ""
                                          for x in el.iter(ns + "t")) \
                                if el is not None else ""
                        else:
                            val = v.text if v is not None else ""
                        cells[idx] = str(val or "")[:200]
                    width = (max(cells.keys()) + 1) if cells else 0
                    rows.append([cells.get(i, "") for i in range(width)])
                sheets.append({"name": meta["name"], "rows": rows})
            if not sheets:
                return {"ok": False, "error": "未找到有效工作表"}
            return {"ok": True, "kind": "table", "name": os.path.basename(fp),
                    "sheets": sheets, "truncated": truncated}
    except zipfile.BadZipFile:
        return {"ok": False, "error": "文件损坏或非有效的 xlsx 格式"}
    except Exception as e:
        return {"ok": False, "error": f"表格解析失败: {type(e).__name__}: {e}"}


def _read_table(fp, max_rows=TABLE_MAX_ROWS, max_cols=TABLE_MAX_COLS):
    """表格文件统一解析入口 -> {ok, kind:'table', sheets:[{name, rows}]}"""
    ext = os.path.splitext(fp)[1].lower()
    if ext == ".xlsx":
        return _read_xlsx(fp, max_rows, max_cols)
    if ext in (".csv", ".tsv"):
        import csv as _csv
        last_err = ""
        for enc in ("utf-8-sig", "gbk", "latin-1"):
            try:
                rows, truncated = [], False
                with open(fp, encoding=enc, newline="") as f:
                    reader = _csv.reader(
                        f, delimiter="\t" if ext == ".tsv" else ",")
                    for i, r in enumerate(reader):
                        if i >= max_rows:
                            truncated = True
                            break
                        rows.append([str(c)[:200] for c in r[:max_cols]])
                return {"ok": True, "kind": "table",
                        "name": os.path.basename(fp),
                        "sheets": [{"name": "Sheet1", "rows": rows}],
                        "truncated": truncated}
            except (UnicodeDecodeError, _csv.Error, OSError) as e:
                last_err = f"{type(e).__name__}: {e}"
                continue
        return {"ok": False, "error": f"表格读取失败: {last_err}"}
    return {"ok": False,
            "error": f"不支持的表格类型: {ext}(.xls 旧格式请转存为 .xlsx 后预览)"}


class EventBus:
    """事件总线: 追加式事件日志 + 多订阅者长轮询(SSE)。
    v4.9 阻塞性BUG修复: 事件列表有界(裁剪旧事件+偏移量寻址), 修复长时间
    运行下 events 无界增长导致的内存泄漏(最终OOM)。"""

    MAX_EVENTS = 5000   # 内存中保留的最大事件数(超出裁剪最旧)

    def __init__(self):
        self.events = []
        self.base = 0            # 已裁剪事件的绝对偏移量
        self.cond = threading.Condition()

    def push(self, etype, **payload):
        with self.cond:
            evt = {"i": self.base + len(self.events), "type": etype,
                   "ts": time.time(), **payload}
            self.events.append(evt)
            if len(self.events) > self.MAX_EVENTS:
                drop = len(self.events) - self.MAX_EVENTS
                self.events = self.events[drop:]
                self.base += drop
            self.cond.notify_all()
        return evt

    def wait_since(self, since, timeout=25):
        with self.cond:
            idx = max(0, since - self.base)
            if len(self.events) > idx:
                return self.events[idx:]
            self.cond.wait(timeout)
            idx = max(0, since - self.base)
            return self.events[idx:]


class WebBridge:
    """Agent ↔ 浏览器 桥接: 流式输出、工具时间线、远程审批、澄清提问、成果物中心"""

    @staticmethod
    def _default_file_root():
        """探测系统下载目录(跨平台): Windows/macOS/Linux 均为 ~/Downloads,
        中文系统可能为 ~/下载; 均不存在时降级到用户主目录"""
        home = os.path.expanduser("~")
        for name in ("Downloads", "downloads", "下载"):
            d = os.path.join(home, name)
            if os.path.isdir(d):
                return os.path.realpath(d)
        return os.path.realpath(home)

    def __init__(self, workdir):
        self.bus = EventBus()
        self.pending = {}          # interaction_id -> queue.Queue (审批/提问回执)
        self.busy = threading.Lock()
        self.current_task = None
        self.artifacts = []        # 成果物登记表(交付成功后自动记录, 供预览/下载)
        # 会话级 Token 成本账本(sid -> 累计成本), 右侧栏仅显示当前会话数据
        self.session_costs = {}
        # 文件管理根目录(默认系统下载目录, 用户可在网页端修改)
        self.file_root = self._default_file_root()
        wlog.info("file manager root: %s", self.file_root)
        # 会话首个任务开始时间(sid -> ts), 变更文件列表统计起点
        self.session_t0 = {}
        # 恢复持久化的右侧栏数据(成果物/Token成本/变更统计起点),
        # 修复服务重启后成果物与成本账本丢失
        self._load_state()
        self.agent = Agent(workdir, headless=False, quiet=True,
                           progress_cb=self._on_progress)
        self.agent.llm.on_delta = self._on_delta
        self._patch(self.agent)
        # _rebind_workdir 会重建 ToolRunner, 需要重新打补丁
        orig_rebind = self.agent._rebind_workdir

        def rebind(new_wd):
            orig_rebind(new_wd)
            self._patch(self.agent)
            # 目录迁移后更新已登记交付物的 wd 字段(防预览404)
            new_wd_real = os.path.realpath(self.agent.wd)
            updated = False
            for a in self.artifacts:
                for f in (a.get("files") or []):
                    if f.get("wd") and f["wd"] != new_wd_real:
                        # v8.8 瘦身: 移除未使用的 old 中间变量
                        f["wd"] = new_wd_real
                        # 同步 direct_url 中的 md5 token
                        if f.get("direct_url", "").startswith("/p/"):
                            token = wd_token(new_wd_real)
                            parts_d = f["direct_url"].split("/", 3)
                            if len(parts_d) >= 4:
                                f["direct_url"] = "/p/" + token + "/" + parts_d[3]
                        updated = True
                # 同步 art 级 preview_url (direct_url)
                if a.get("preview_direct") and a.get("preview_url", "").startswith("/p/"):
                    token = wd_token(new_wd_real)
                    parts_p = a["preview_url"].split("/", 3)
                    if len(parts_p) >= 4:
                        a["preview_url"] = "/p/" + token + "/" + parts_p[3]
            if updated:
                self._save_state()
                wlog.info("artifacts wd updated after rebind: %s", new_wd_real)
        self.agent._rebind_workdir = rebind
        # 包装 _before_task —— 首个任务固化会话身份(生成任务名)后,
        # 立即推送 sessions 事件, 前端左侧历史对话列表实时显示当前任务名称
        orig_before = self.agent._before_task

        def before_task_wrapped(user_input):
            prev = dict(self.agent.session or {})
            orig_before(user_input)
            cur = self.agent.session or {}
            # 会话身份固化后迁移"待固化"任务起始时间(变更统计起点)
            if cur.get("session_id") and "_pending" in self.session_t0:
                self.session_t0.setdefault(cur["session_id"],
                                           self.session_t0.pop("_pending"))
                self._save_state()
            if (cur.get("session_id") != prev.get("session_id")
                    or cur.get("name_zh") != prev.get("name_zh")
                    or cur.get("workdir") != prev.get("workdir")):
                self.push_sessions()
                # 首次固化立即广播任务名与工作目录(前端顶栏/会话列表即时同步)
                if cur.get("name_zh") and not prev.get("name_zh"):
                    self.bus.push("sys", text=(
                        f"✓ 任务身份已固化: {cur.get('name_zh')}"
                        f"({cur.get('name_en', '')}) · 工作目录: {self.agent.wd}"))
        self.agent._before_task = before_task_wrapped
        # 重启后立即恢复当前会话上下文 —— 修复服务重启后右侧执行时间轴
        # (/api/history 依赖 agent.messages 重建)与任务清单数据丢失
        if self.agent.session and not self.agent._context_loaded:
            try:
                ctx = self.agent.sessions.load_context(self.agent.session)
                if ctx:
                    self.agent.messages = ctx
                    self.agent.repair_context()   # 修复中断损坏的消息链
                    self.agent._context_loaded = True
                    self._restore_todos(ctx)
                    wlog.info("context restored on startup: sid=%s msgs=%d",
                              self.agent.session.get("session_id"), len(ctx))
            except Exception as e:
                wlog.warning("context restore on startup failed: %s", e)

    # ---- 右侧栏数据持久化(成果物/Token成本/变更统计起点) ----
    def _state_path(self):
        from .config import GLOBAL_DIR
        return os.path.join(GLOBAL_DIR, "web_state.json")

    def _load_state(self):
        """服务启动时恢复成果物登记表/会话成本账本 —— 修复重启后右侧栏数据丢失"""
        p = self._state_path()
        if not os.path.isfile(p):
            return
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            self.artifacts = d.get("artifacts") or []
            self.session_costs = d.get("session_costs") or {}
            self.session_t0 = {k: v for k, v
                               in (d.get("session_t0") or {}).items()
                               if k != "_pending"}
            # 恢复 md5 token -> 工作目录映射(历史直达链接重启后仍可用)
            with _WD_TOKENS_LOCK:
                for k, v in (d.get("wd_tokens") or {}).items():
                    if isinstance(k, str) and isinstance(v, str):
                        _WD_TOKENS[k] = v
            wlog.info("web state restored: artifacts=%d costs=%d t0=%d",
                      len(self.artifacts), len(self.session_costs),
                      len(self.session_t0))
        except (json.JSONDecodeError, OSError, TypeError) as e:
            wlog.warning("web state load failed: %s", e)

    def _save_state(self):
        """成果物/成本账本变更后即时落盘(原子写), 服务重启后右侧栏数据可恢复"""
        try:
            p = self._state_path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                with _WD_TOKENS_LOCK:
                    _tokens = dict(_WD_TOKENS)
                json.dump({"artifacts": self.artifacts[-200:],
                           "session_costs": self.session_costs,
                           "session_t0": self.session_t0,
                           "wd_tokens": _tokens},
                          f, ensure_ascii=False, default=str)
            os.replace(tmp, p)
        except OSError as e:
            wlog.warning("web state save failed: %s", e)

    def push_sessions(self):
        """推送最新会话列表到前端(左侧历史对话面板自动刷新)"""
        self.bus.push("sessions",
                      sessions=self.agent.sessions.list()[:20],
                      current=(self.agent.session or {}).get("session_id", ""))

    # ---- Agent 侧钩子 ----
    def _on_delta(self, kind, text):
        # toolgen 事件携带 from_toolgen 标识, 前端据此关闭末尾
        # assistant 流并显示 loading(思考结束后→工具展示前的空窗期)
        if kind == "toolgen":
            self.bus.push("toolgen", text="")
        else:
            self.bus.push(kind, text=text)

    def _on_progress(self, event, payload):
        if event == "turn":
            self.bus.push("turn", **payload)
        elif event == "tool":
            # 附带中文 title(前端优先显示 title 而非方法名)
            payload = {**payload,
                       "title": TOOL_TITLES.get(payload.get("tool", ""),
                                                payload.get("tool", ""))}
            self.bus.push("tool_start", **payload)
        elif event == "tool_skip":
            # 旁路工具结果(中断占位/策略拒绝/重复拦截, 未经 run 包装器)
            # 统一补发 tool_end, 关闭前端"执行中"状态
            self.bus.push("tool_end", tool=payload.get("tool", ""),
                          dt=payload.get("dt", 0),
                          title=TOOL_TITLES.get(payload.get("tool", ""),
                                                payload.get("tool", "")),
                          result=payload.get("result", ""))
        elif event == "reflexion":   # 自愈反思卡片(归因过程透明可观测)
            self.bus.push("reflexion", **payload)
        elif event == "domain":      # 垂直领域异构路由徽章
            self.bus.push("domain", **payload)
        elif event == "steer_ack":   # 协同指令已被智能体吸收
            self.bus.push("steer_ack", **payload)
        elif event == "next_actions":   # 任务续航建议芯片(一键续跑)
            # 去重 —— 与上次推送的 next_actions 内容一致则跳过, 避免重复
            _new = tuple(payload.get("actions", []))
            if _new and _new != getattr(self, "_last_next_actions", None):
                self._last_next_actions = _new
                self.bus.push("next_actions", **payload)
            else:
                wlog.info("next_actions skipped (duplicate or empty)")
        elif event in ("done", "error"):
            self.bus.push("task_" + event, **payload)

    def _patch(self, agent):
        """包装 ToolRunner.run 上报工具结果; 桥接审批与提问到网页"""
        tools = agent.tools
        orig_run = tools.run

        def run_wrapped(name, args):
            t0 = time.time()
            result = orig_run(name, args)
            # web_search/web_fetch 富渲染 —— 放宽截断(避免批量结果被切断),
            # 并为 web_search 附带信源元数据(站点图标/来源名/发布时间/摘要)
            cap = 15000 if name in ("web_search", "web_fetch") else 1500
            extra = {}
            if name == "web_search":
                try:
                    extra["srcs"] = [
                        {"id": s.get("id"), "siteName": s.get("siteName", ""),
                         "siteIcon": s.get("siteIcon", ""), "date": s.get("date", ""),
                         "org": s.get("org", ""), "snippet": s.get("snippet", "")}
                        for s in tools.sources.sources[-60:]]
                except Exception:
                    pass
            self.bus.push("tool_end", tool=name, dt=round(time.time() - t0, 1),
                          title=TOOL_TITLES.get(name, name),
                          result=str(result)[:cap], **extra)
            if name == "todo_write":
                self.bus.push("todos", **tools.todo_progress())
            # 交付成功 -> 登记成果物并推送到网页(预览/下载入口)
            # [成功]与[部分成功]均登记交付卡片(部分文件缺失不影响有效交付物展示)
            if name == "send_user_msg" and str(result).startswith(("[成功]", "[部分成功]")):
                self._register_artifacts(args if isinstance(args, dict) else {})
            return result
        tools.run = run_wrapped
        # Swarm 子任务执行过程结构化上报(swarm_start/swarm_task_start/
        # swarm_tool/swarm_task_done/swarm_end) -> 前端树状渲染工具执行顺序与结果
        tools.swarm_cb = lambda event, payload: self.bus.push(event, **payload)
        agent.perm.ask = self._web_approve
        tools.t_ask_user_question = self._web_ask

    # ---- 成果物中心 ----
    def _register_artifacts(self, args):
        """交付成功后登记成果物: 校验文件真实存在, 记录大小与可预览性"""
        wd = os.path.realpath(self.agent.wd)
        # 交付自动快照隔离 —— 若本次交付已自动固化快照, 成果物登记到
        # 快照目录(wd 指向快照目录), 使预览/下载/部署均指向该快照目录内的文件,
        # 后续新交付物不会覆盖旧快照。
        snap = getattr(self.agent.tools, "delivery_snapshot", None)
        snap_dir = snap["dir"] if snap else None
        if snap_dir and os.path.isdir(snap_dir):
            wd = os.path.realpath(snap_dir)
        files = []
        for f in (args.get("files") or []):
            rel = (f.get("path") or "").strip()
            if not rel:
                continue
            fp = os.path.realpath(os.path.join(wd, rel))
            if not fp.startswith(wd + os.sep):
                continue
            # 目录类交付物(如 Mac 原生应用 .app / 构建产物目录)同样登记,
            # 确保所有存在实质性交付物的任务都能以卡片形式展示
            if os.path.isdir(fp):
                dir_size = 0
                fcount = 0
                for r_, _, fs_ in os.walk(fp):
                    for fn_ in fs_:
                        try:
                            dir_size += os.path.getsize(os.path.join(r_, fn_))
                            fcount += 1
                        except OSError as _e:
                            wlog.warning("忽略异常(omni_agent/webserver.py:182): %s: %s", type(_e).__name__, _e)
                            pass
                files.append({
                    "name": f.get("name") or os.path.basename(fp),
                    "path": os.path.relpath(fp, wd).replace(os.sep, "/"),
                    "wd": wd,
                    "file_type": f.get("file_type") or "dir",
                    "ext": "dir",
                    "size": dir_size,
                    "mtime": os.path.getmtime(fp),
                    "mtime_str": time.strftime("%Y-%m-%d %H:%M",
                                               time.localtime(os.path.getmtime(fp))),
                    "previewable": False,
                    "is_dir": True,
                    "file_count": fcount,
                    "category": "app",
                })
                continue
            if not os.path.isfile(fp):
                continue
            ext = os.path.splitext(fp)[1].lower()
            files.append({
                "name": f.get("name") or os.path.basename(fp),
                "path": os.path.relpath(fp, wd).replace(os.sep, "/"),
                "wd": wd,   # 记录归属工作目录, 会话切换后链接仍可用
                "file_type": f.get("file_type") or ext.lstrip(".") or "other",
                "ext": ext.lstrip("."),
                "size": os.path.getsize(fp),
                "mtime": os.path.getmtime(fp),
                "mtime_str": time.strftime("%Y-%m-%d %H:%M",
                                           time.localtime(os.path.getmtime(fp))),
                "previewable": _previewable(ext),
                "category": self._categorize(f, rel, ext),
            })
        art = {"id": "art_" + uuid.uuid4().hex[:8], "ts": time.time(),
               "ts_str": time.strftime("%Y-%m-%d %H:%M:%S"),   # 交付物更新时间
               # 记录归属会话, 切换任务时右侧栏仅显示当前会话的交付物
               "sid": (self.agent.session or {}).get("session_id", ""),
               "title": args.get("title", ""),
               "preview_url": (args.get("preview_url") or "").strip(),
               "snapshot_id": (snap.get("cid") if snap else None),
               "snapshot_dir": (snap.get("dir") if snap else None),
               "files": files,
               "suggestions": (args.get("suggestions") or [])[:3]}
        # web类型交付物预览直达 —— 无外部 preview_url 时, 自动以
        # /p/<b64工作目录>/<相对路径> 目录式路由生成完整直达链接(优先 index.html
        # 入口), 页面内相对引用的 CSS/JS 也能经该路由正确解析
        html_files = [f for f in files
                      if f.get("ext") in ("html", "htm") and not f.get("is_dir")]
        if html_files:
            token = wd_token(wd)   # 16位 md5 编码(替换 base64)
            for f in html_files:
                f["direct_url"] = "/p/" + token + "/" + urllib.parse.quote(f["path"])
            entry = next((f for f in html_files
                          if os.path.basename(f["path"]).lower() == "index.html"),
                         html_files[0])
            if not art["preview_url"]:
                art["preview_url"] = entry["direct_url"]
                art["preview_direct"] = True
        if art["preview_url"] or art["files"]:
            self.artifacts.append(art)
            self._save_state()   # 成果物即时落盘, 重启后可恢复
            self.bus.push("artifacts", **art)

    @staticmethod
    def _categorize(f, rel, ext):
        """v3.7 成果物分类: 图表/办公文件/图片/网页/媒体 -> 前端卡片分类徽标,
        支持同一任务混合交付 预览链接 + 办公文件 + Matplotlib图表"""
        name = ((f.get("name") or "") + " " + rel).lower()
        if ext in {".png", ".jpg", ".jpeg", ".svg", ".webp"}:
            if (f.get("file_type") == "chart"
                    or any(k in name for k in ("chart", "图表", "plot", "fig", "matplotlib"))):
                return "chart"
            return "image"
        if ext in {".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".pdf", ".csv"}:
            return "office"
        if ext in {".html", ".htm"}:
            return "web"
        if ext in {".mp3", ".mp4", ".wav", ".webm", ".ogg"}:
            return "media"
        return "file"

    def safe_path(self, rel, wd=""):
        """路径安全解析: 仅允许访问工作目录内真实存在的文件(防目录穿越)。
        支持显式 wd(成果物归属目录) —— 仅接受当前工作目录或会话根目录
        (~/haisnap_projects)内的目录, 修复切换会话后成果物链接 404。"""
        if not rel or "\x00" in rel:
            return None
        base = os.path.realpath(self.agent.wd)
        if wd:
            cand = os.path.realpath(wd)
            # 显式 wd 目录不存在(已被迁移) -> 回退当前工作目录
            if not os.path.isdir(cand):
                base = os.path.realpath(self.agent.wd)
            else:
                root = os.path.realpath(self.agent.sessions.root)
                known_wds = {os.path.realpath(f.get("wd", ""))
                             for a in self.artifacts for f in (a.get("files") or [])}
                # 文件管理根目录(默认系统下载目录)纳入白名单
                if cand == base or cand.startswith(root + os.sep) or cand in known_wds \
                        or cand == os.path.realpath(self.file_root):
                    base = cand
                else:
                    return None
        fp = os.path.realpath(os.path.join(base, rel))
        if not fp.startswith(base + os.sep):
            return None
        if os.path.isfile(fp):
            return fp
        # 文件不在显式 wd 目录(可能已被迁移) -> 尝试在当前工作目录查找
        cur = os.path.realpath(self.agent.wd)
        alt2 = os.path.realpath(os.path.join(cur, rel))
        if alt2.startswith(cur + os.sep) and os.path.isfile(alt2):
            return alt2
        # 归属目录内未找到(目录被任务身份固化重命名等) -> 回退到当前工作目录解析
        cur = os.path.realpath(self.agent.wd)
        alt = os.path.realpath(os.path.join(cur, rel))
        if alt.startswith(cur + os.sep) and os.path.isfile(alt):
            return alt
        return None

    # ---- 远程人机协同 ----
    def _wait_reply(self, iid, timeout):
        q = queue.Queue()
        self.pending[iid] = q
        try:
            return q.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self.pending.pop(iid, None)

    def _web_approve(self, tool, detail):
        # yolo 模式下直接放行(用户在审批卡片选择过 YOLO)
        if self.agent.perm.yolo:
            return True
        iid = "ap_" + uuid.uuid4().hex[:8]
        self.bus.push("approval", id=iid, tool=tool,
                      detail=str(detail)[:600], timeout=APPROVAL_TIMEOUT)
        ans = self._wait_reply(iid, APPROVAL_TIMEOUT)
        if ans is None:   # 超时: 默认放行(与 CLI 回车默认同意一致)并明确标注
            self.bus.push("approval_result", id=iid, result="timeout_allow")
            return True
        val = str(ans.get("value", "")).lower()
        if val == "yolo":   # YOLO — 本会话后续所有审批自动放行
            self.agent.perm.yolo = True
            self.bus.push("approval_result", id=iid, result="yolo")
            self.bus.push("sys", text="⚡ YOLO 模式已开启: 本会话后续审批请求将自动放行"
                          "(危险命令黑名单仍强制拦截)")
            return True
        ok = val in ("y", "yes", "true", "allow", "1")
        self.bus.push("approval_result", id=iid, result="allow" if ok else "deny")
        return ok

    def _web_ask(self, a):
        questions = a.get("questions")
        if not questions and a.get("question"):
            questions = [{"question": a["question"], "options": a.get("options") or []}]
        if not questions:
            return "[错误] questions 不能为空"
        if a.get("notify_only"):
            self.bus.push("notice", questions=questions)
            return "[通知已送达用户]"
        iid = "q_" + uuid.uuid4().hex[:8]
        self.bus.push("question", id=iid, questions=questions, timeout=QUESTION_TIMEOUT)
        ans = self._wait_reply(iid, QUESTION_TIMEOUT)
        answers = []
        for qi, q in enumerate(questions, 1):
            opts = q.get("options") or []
            default = opts[0] if opts else "(按最合理假设继续)"
            if ans is None:
                answers.append(f"Q{qi}: [超时自动选择] {default}")
            else:
                v = (ans.get("answers") or [])
                answers.append(f"Q{qi}: {v[qi - 1] if qi <= len(v) and v[qi - 1] else default}")
        self.bus.push("question_result", id=iid, answers=answers)
        return "[用户回答]\n" + "\n".join(answers)

    # ---- 任务执行 ----
    def submit_task(self, prompt, policy_cfg=None):
        if not self.busy.acquire(blocking=False):
            return {"ok": False, "error": "已有任务正在执行, 请等待完成"}
        policy = None
        if policy_cfg:
            policy = ToolPolicy(allow=policy_cfg.get("allow"),
                                deny=policy_cfg.get("deny"),
                                auto=(policy_cfg.get("mode") == "auto"))
            if policy.is_noop():
                policy = None

        # 复用 do_POST 入口创建的 tracker_id(从收到前端请求开始全链路一致),
        # 未经 HTTP 入口调用时降级新建
        from .logger import get_tracker
        tid = get_tracker()
        if not tid or tid == "-":
            tid = new_tracker("web")
        wlog.info("submit_task: tracker=%s prompt=%s", tid, prompt[:80])
        # 记录本会话首个任务开始时间(变更文件统计起点); 未固化暂记 _pending
        _sid0 = (self.agent.session or {}).get("session_id", "")
        self.session_t0.setdefault(_sid0 or "_pending", time.time())
        self._save_state()

        def worker():
            set_tracker(tid)       # 跨线程传递请求唯一性标记
            llm = self.agent.llm   # 任务前快照, 任务结束后差值归属当前会话
            base = (llm.calls, llm.total_prompt, llm.total_completion,
                    getattr(llm, "total_cached", 0))
            try:
                self.bus.push("task_start", prompt=prompt,
                              policy=policy.to_dict() if policy else None)
                if policy:
                    policy.resolve(
                        task_type=(self.agent.session or {}).get("task_type", ""),
                        intent=prompt)
                    self.bus.push("policy", desc=policy.describe(),
                                  detail=policy.to_dict())
                self.agent.run_task(prompt, tool_policy=policy)
            except Exception as e:
                # 断点续传支持 —— 异常退出前修复并持久化消息链,
                # 已完成的步骤不丢失; 前端据 resumable 标记提示"继续"续跑
                try:
                    self.agent.repair_context()
                    if self.agent.session:
                        self.agent.sessions.save_context(
                            self.agent.session, self.agent.messages,
                            last_task=prompt[:80])
                except Exception as _pe:
                    wlog.warning("异常中断后上下文保存失败: %s", _pe)
                self.bus.push("task_error", detail=f"{type(e).__name__}: {e}",
                              resumable=True)
            finally:
                self._accrue_cost(base)   # 本次任务成本记入当前会话账本
                # 任务结束同步持久化信源 —— 会话切换/重启后数据溯源可恢复
                try:
                    self.agent.sources.save(self.agent.wd)
                except Exception as _e:
                    wlog.warning("sources save failed: %s", _e)
                wlog.info("task finished: tracker=%s session=%s", tid,
                          (self.agent.session or {}).get("session_id", "-"))
                self.bus.push("cost", **self.cost())
                # 任务结束 last_task/updated 已更新, 同步刷新左侧会话列表
                self.push_sessions()
                self.bus.push("idle")
                self.busy.release()
        self.current_task = threading.Thread(target=worker, daemon=True)
        self.current_task.start()
        return {"ok": True}

    def stop_task(self):
        """手动终止当前任务: 置停止位并解除所有等待中的网页交互,
        避免任务卡在审批/提问上无法感知终止信号。"""
        if not self.busy.locked():
            return {"ok": False, "error": "当前没有正在执行的任务"}
        self.agent.request_stop()
        for iid in list(self.pending.keys()):
            q = self.pending.get(iid)
            if q:
                q.put({"value": "deny", "answers": [], "cancelled": True})
        self.bus.push("sys", text="⏹ 已收到终止请求: 当前步骤执行完毕后任务将停止")
        return {"ok": True}

    def reply(self, iid, payload):
        q = self.pending.get(iid)
        if not q:
            return {"ok": False, "error": "交互已过期或不存在"}
        q.put(payload)
        return {"ok": True}

    def md_save(self, rel, content, wd=""):
        """Markdown 双栏编辑保存 —— 覆盖写回原文件(仅 .md/.markdown/.txt,
        路径经 safe_path 白名单校验, 防目录穿越; 保存前自动备份到 .haisnap/md_bak)"""
        fp = self.safe_path(rel, wd=wd) or self.safe_path(rel) \
            or self.safe_path(rel, wd=self.file_root)
        if not fp:
            return {"ok": False, "error": "文件不存在或路径非法"}
        ext = os.path.splitext(fp)[1].lower()
        if ext not in (".md", ".markdown", ".txt"):
            return {"ok": False, "error": f"仅支持 Markdown/文本文件在线保存(当前 {ext})"}
        if not isinstance(content, str):
            return {"ok": False, "error": "内容格式非法"}
        if len(content) > 2 * 1024 * 1024:
            return {"ok": False, "error": "内容过大(>2MB), 请下载后本地编辑"}
        try:
            bak_dir = os.path.join(self.agent.wd, ".haisnap", "md_bak")
            os.makedirs(bak_dir, exist_ok=True)
            if os.path.isfile(fp):
                import shutil
                shutil.copy2(fp, os.path.join(
                    bak_dir, time.strftime("%Y%m%d_%H%M%S_")
                    + os.path.basename(fp)))
            with open(fp, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            wlog.info("md_save ok: %s (%d chars)", fp, len(content))
            return {"ok": True, "msg": "✓ 已保存并覆盖原文件(保存前已自动备份)",
                    "path": rel, "size": len(content.encode("utf-8"))}
        except OSError as e:
            wlog.warning("md_save failed: %s: %s", fp, e)
            return {"ok": False, "error": f"保存失败: {e}"}

    def steer(self, text):
        """人机协同实时共驾 —— 任务执行中用户注入协同指令"""
        if not self.busy.locked():
            return {"ok": False, "error": "当前没有正在执行的任务"}
        t = (text or "").strip()
        if not t:
            return {"ok": False, "error": "协同指令不能为空"}
        ok = self.agent.add_steer(t)
        self.bus.push("steer", text=t)
        return {"ok": ok}

    def rebuild_timeline(self, limit=120):
        """由 agent.messages 重建可视时间线(含工具调用与结果摘要),
        供前端切换会话/刷新页面后回放完整执行过程。"""
        items = []
        tool_results = {}
        tool_dts = {}   # 工具执行耗时(_dt 随 tool 消息持久化, 刷新后仍可显示)
        for m in (self.agent.messages or []):
            if m.get("role") == "tool":
                tool_results[m.get("tool_call_id", "")] = str(m.get("content", ""))
                if m.get("_dt") is not None:
                    tool_dts[m.get("tool_call_id", "")] = m.get("_dt")
        for m in (self.agent.messages or []):
            role = m.get("role")
            if role == "user":
                c = m.get("content")
                if isinstance(c, str) and c.startswith("[协同透传] "): 
                    items.append({"kind": "steer",
                                  "text": c[len("[协同透传] "):][:4000],
                                  "acked": True})
                elif isinstance(c, str) and c.strip() \
                        and not c.startswith(("[系统", "[工具", "[Hook", "[上下文已蒸馏",
                                              "[历史工具")): 
                    for _mark in ("\n[系统提示:", "\n【垂直领域异构执行引擎"):
                        _i = c.find(_mark)
                        if _i > 0:
                            c = c[:_i]
                    c = re.sub(r"\n\[系统提示:[^\]]*\]\s*$", "", c).rstrip()
                    items.append({"kind": "user", "text": c[:4000]})
            elif role == "assistant":
                # 回放思考过程(assistant._reasoning 随消息链持久化)
                rsn = m.get("_reasoning")
                if isinstance(rsn, str) and rsn.strip():
                    items.append({"kind": "reasoning", "text": rsn[:8000]})
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    items.append({"kind": "assistant", "text": c[:6000]})
                for tc in (m.get("tool_calls") or []):
                    fn = tc.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    brief = str(args.get("command") or args.get("path")
                                or args.get("pattern") or args.get("url")
                                or args.get("title") or "")[:120]
                    tname = fn.get("name", "?")
                    items.append({"kind": "tool", "tool": tname,
                                  "title": TOOL_TITLES.get(tname, tname),
                                  "brief": brief,
                                  "dt": tool_dts.get(tc.get("id", "")),
                                  "result": tool_results.get(tc.get("id", ""), "")[:1500]})
        return items[-limit:]

    def _accrue_cost(self, base):
        """任务结束后将本次消耗差值累加到当前会话的成本账本"""
        llm = self.agent.llm
        after = (llm.calls, llm.total_prompt, llm.total_completion,
                 getattr(llm, "total_cached", 0))
        delta = [max(0, a - b) for a, b in zip(after, base)]
        sid = (self.agent.session or {}).get("session_id") or "_pending"
        c = self.session_costs.setdefault(sid, {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0})
        c["calls"] += delta[0]
        c["prompt_tokens"] += delta[1]
        c["completion_tokens"] += delta[2]
        c["cached_tokens"] += delta[3]
        self._save_state()   # 成本账本即时落盘, 重启后可恢复

    def _restore_todos(self, messages):
        """会话切换后恢复任务清单 —— 从消息链中找最后一次 todo_write
        工具调用的参数, 回填到 ToolRunner.todos(右侧栏任务清单一一对应)"""
        todos = []
        for m in messages:
            if m.get("role") != "assistant":
                continue
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function", {})
                if fn.get("name") == "todo_write":
                    try:
                        todos = json.loads(fn.get("arguments") or "{}").get("todos", [])
                    except (json.JSONDecodeError, TypeError) as _e:
                        wlog.warning("忽略异常(omni_agent/webserver.py:472): %s: %s", type(_e).__name__, _e)
                        pass
        self.agent.tools.todos = todos
        self.bus.push("todos", **self.agent.tools.todo_progress())

    def cost(self):
        """返回当前会话的 Token 成本(任务切换时右侧栏一一对应)"""
        sid = (self.agent.session or {}).get("session_id") or "_pending"
        c = self.session_costs.get(sid)
        if c:
            return dict(c)
        return {"calls": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "cached_tokens": 0}

    def state(self):
        ag = self.agent
        tools = [{"name": t["function"]["name"],
                  "title": t.get("title") or TOOL_TITLES.get(
                      t["function"]["name"], t["function"]["name"]),
                  "description": t["function"]["description"][:120]}
                 for t in ag.all_tool_schemas()]
        # 右侧栏数据任务级隔离 —— 仅返回当前会话的成果物
        cur_sid = (ag.session or {}).get("session_id", "")
        my_arts = [a for a in self.artifacts if a.get("sid", "") == cur_sid]
        # 配置回显 —— 从 SettingsManager 四层链路统一读取生效值,
        # 确保通过 settings.json / 环境变量 / config.py 任意层配置的值均正确回显。
        # api_key / vision_api_key 为敏感信息: 仅返回是否已设置(bool), 不回显明文。
        from .config import VISION_MODEL
        from .settings import S
        _api_key, _ = S.resolve("model.api_key")
        _base_url, _ = S.resolve("model.base_url")
        _model, _ = S.resolve("model.model")
        _vmodel, _ = S.resolve("model.vision_model")
        _vkey, _ = S.resolve("model.vision_api_key")
        _vurl, _ = S.resolve("model.vision_base_url")
        _fbm, _ = S.resolve("model.fallback_model")
        _fbk, _ = S.resolve("model.fallback_api_key")
        _fbu, _ = S.resolve("model.fallback_base_url")
        return {
            "version": __version__,
            "model": _model or MODEL,
            "vision_model": _vmodel or VISION_MODEL,
            "api_key_set": bool(_api_key),
            "base_url": _base_url or "",
            "vision_key_set": bool(_vkey),
            "vision_base_url": _vurl or "",
            "fallback_model": (_fbm or "").strip(),
            "fallback_key_set": bool(_fbk),
            "fallback_base_url": _fbu or "",
            "fallback_hits": getattr(ag.llm, "fallback_hits", 0),
            # 模型路由配置与模型注册表(前端配置面板展示)
            "routing": __import__(
                "omni_agent.model_router", fromlist=["ModelRouter"]
            ).ModelRouter.describe_config(),
            "models": __import__(
                "omni_agent.model_router", fromlist=["ModelRouter"]
            ).ModelRouter.list_models(),
            # 模型池(模型管理)回显 —— api_key 敏感, 仅回传 key_set 布尔
            "pool": [{"name": m["name"], "base_url": m["base_url"],
                      "key_set": m["key_set"]} for m in S.get_pool()],
            "workdir": ag.wd, "mode": ag.mode, "thinking": ag.llm.thinking,
            "busy": self.busy.locked(),
            "session": ag.session, "brief": ag.project_brief(),
            "tools": tools, "cost": self.cost(),
            "todos": ag.tools.todo_progress(),
            "checkpoints": ag.ckpt.list()[-10:],
            "sessions": ag.sessions.list()[:20],   # list()新在前, 取前20个
            "sources": getattr(ag.sources, "sources", []),
            "artifacts": my_arts[-20:],
            "policy": (ag.tool_policy.to_dict()
                       if ag.tool_policy and not ag.tool_policy.is_noop() else None),
            "task_presets": {k: sorted(v) if v else None
                             for k, v in TASK_TYPE_PRESETS.items()},
            "event_count": len(self.bus.events),
            "reflexion": {"enabled": True,
                          "times": getattr(ag, "reflexion", None)
                          and ag.reflexion.reflections or 0},
        }

    # ---- 历史对话管理(重命名/置顶/删除/切换/新建) ----
    def session_op(self, action, sid="", value=""):
        wlog.info("session_op: action=%s sid=%s", action, sid or "-")
        ag, sm = self.agent, self.agent.sessions
        if action in ("switch", "new", "delete") and self.busy.locked():
            return {"ok": False, "error": "任务执行中, 请等待完成后再操作会话"}
        if action == "rename":
            if not (sid and str(value).strip()):
                return {"ok": False, "error": "缺少会话ID或新名称"}
            meta = sm.rename(sid, str(value).strip())
            # 重命名的是当前会话时同步内存副本, 顶部徽标/系统提示即时生效
            if meta and ag.session and ag.session.get("session_id") == sid:
                ag.session = meta
            return {"ok": bool(meta), "msg": "会话已重命名" if meta else "会话不存在"}
        if action == "pin":
            meta = sm.toggle_pin(sid)
            if not meta:
                return {"ok": False, "error": "会话不存在"}
            if ag.session and ag.session.get("session_id") == sid:
                ag.session = meta   # 同步内存副本
            return {"ok": True, "msg": "已置顶" if meta.get("pinned") else "已取消置顶"}
        if action == "delete":
            if ag.session and ag.session.get("session_id") == sid:
                return {"ok": False, "error": "不能删除当前会话, 请先切换到其他会话"}
            ok = sm.delete(sid, remove_files=bool(value))
            return {"ok": ok, "msg": "会话已删除" if ok else "会话不存在"}
        if action == "switch":
            target = sm.get(sid)
            if not target:
                return {"ok": False, "error": f"会话不存在: {sid}"}
            if ag.session and ag.session.get("session_id") == sid:
                return {"ok": True, "msg": "已处于该会话"}
            if ag.session:
                sm.save_context(ag.session, ag.messages)
                # 保存当前会话信源 —— 持久化到当前工作目录
                ag.sources.save(ag.wd)
            ag.session = target
            wd = os.path.abspath(target["workdir"])
            os.makedirs(wd, exist_ok=True)
            ag._rebind_workdir(wd)   # WebBridge 包装过: 自动重新打补丁
            ctx = sm.load_context(target)
            ag.messages = ctx or [{"role": "system", "content": ag._system()}]
            if ctx:
                ag.repair_context()   # 切换会话后校验消息链完整性
            ag._context_loaded = True
            # 从消息链回放最后一次 todo_write, 恢复该会话的任务清单
            self._restore_todos(ctx or [])
            # 恢复目标会话信源 —— 无持久化则自动清空(联动要求)
            ag.sources.load(wd)
            self.bus.push("sources", sources=ag.sources.sources)
            wlog.info("session switched: sid=%s msgs=%d sources=%d", sid, len(ctx or []), len(ag.sources.sources))
            self.bus.push("sys", text=f"已切换会话 [{sid}] {target.get('name_zh', '')}"
                          + (f" · 恢复 {len(ctx)} 条消息" if ctx else " · 全新上下文"))
            return {"ok": True, "msg": f"已切换到会话 {target.get('name_zh', sid)}"}
        if action == "new":
            if ag.session:
                sm.save_context(ag.session, ag.messages)
                # 保存当前会话信源
                ag.sources.save(ag.wd)
            d = os.path.join(sm.root, "session_" + time.strftime("%Y%m%d_%H%M%S")
                             + "_" + uuid.uuid4().hex[:4])
            os.makedirs(d, exist_ok=True)
            ag.session = None
            ag._rebind_workdir(d)
            ag.messages = [{"role": "system", "content": ag._system()}]
            ag._context_loaded = True
            # 新会话清空任务清单, 右侧栏与新会话一一对应
            ag.tools.todos = []
            self.bus.push("todos", **ag.tools.todo_progress())
            # 新会话信源清空，推送 sources 事件联动前端数据溯源列表
            ag.sources.reset()
            self.bus.push("sources", sources=[])
            self.bus.push("sys", text="已开启新会话(首个任务提交后自动固化身份)")
            return {"ok": True, "msg": "已开启新会话"}
        return {"ok": False, "error": f"未知会话操作: {action}"}

    # ---- 记忆查看(项目记忆 HAISNAP.md + 跨会话全局经验) ----
    def memory_view(self):
        ag = self.agent
        mp = os.path.join(ag.wd, MEMORY_FILE)
        project = ""
        if os.path.isfile(mp):
            with open(mp, encoding="utf-8", errors="replace") as f:
                project = f.read()[:8000]
        try:
            global_mem = ag.memory.load(4000)
        except Exception as e:
            global_mem = f"(全局记忆读取失败: {e})"
        return {"file": MEMORY_FILE, "project": project,
                "global": global_mem or "", "workdir": ag.wd}

    # ---- 技能管理(列表/加载/安装/删除) ----
    def skills_list(self):
        ag = self.agent
        proj_dir = os.path.realpath(ag.skills.dirs[0])
        out = []
        for sk in ag.skills.scan():
            out.append({"name": sk["name"], "brief": sk["brief"][:400],
                        "scope": "项目" if os.path.realpath(sk["path"]).startswith(
                            proj_dir + os.sep) else "全局",
                        "loaded": sk["name"] in ag.skills.loaded})
        return {"skills": out}

    def skill_op(self, action, name="", source=""):
        ag = self.agent
        if action == "view" and name:   # 查看完整 SKILL.md(前端 Markdown 渲染)
            body = ag.skills.content(name)
            if body is None:
                return {"ok": False, "error": f"技能不存在: {name}"}
            return {"ok": True, "name": name, "content": body}
        if action == "load" and name:
            r = ag.skills.load(name)
            ok = not r.startswith("[错误]")
            if ok:
                # 勾选即注入 system —— 刷新系统提示(_system 会把已勾选技能
                # 的完整规范追加到末尾), 下次发起对话自动生效
                if ag.messages and ag.messages[0].get("role") == "system":
                    ag.messages[0] = {"role": "system", "content": ag._system()}
                self.bus.push("sys", text=f"技能 '{name}' 已勾选启用: 规范已注入系统提示, 发起对话即生效")
            return {"ok": ok, "msg": r[:300]}
        if action == "unload" and name:   # 取消勾选 -> 从当前会话卸载
            r = ag.skills.unload(name)
            # 清理旧版本可能残留的会话内注入消息 + 刷新系统提示(移除已卸载技能规范)
            ag.messages = [m for m in ag.messages
                           if not (m.get("role") == "user" and isinstance(m.get("content"), str)
                                   and m["content"].startswith("[系统注入·技能规范]")
                                   and f"技能已加载: {name}" in m["content"])]
            if ag.messages and ag.messages[0].get("role") == "system":
                ag.messages[0] = {"role": "system", "content": ag._system()}
            self.bus.push("sys", text=f"技能 '{name}' 已从当前会话移除")
            return {"ok": r.startswith("[成功]"), "msg": r[:300]}
        if action == "install" and source:
            if self.busy.locked():
                return {"ok": False, "error": "任务执行中, 请稍后安装技能"}
            r = ag.skills.install(source.strip())
            return {"ok": r.startswith("[成功]"), "msg": r[:300]}
        if action == "install_zip":   # 上传 zip 包安装技能(base64)
            if self.busy.locked():
                return {"ok": False, "error": "任务执行中, 请稍后安装技能"}
            import tempfile
            fname = os.path.basename((name or "skill.zip").replace("\\", "/"))
            if not fname.lower().endswith(".zip"):
                return {"ok": False, "error": "仅支持 .zip 格式的技能包"}
            try:
                data = base64.b64decode(source or "", validate=True)
            except Exception:
                return {"ok": False, "error": "zip 文件内容解码失败"}
            if not data:
                return {"ok": False, "error": "zip 文件内容为空"}
            if len(data) > 50 * 1024 * 1024:
                return {"ok": False, "error": "zip 文件超过 50MB 上限"}
            tmp_path = ""
            try:
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False,
                                                 prefix=fname[:-4] + "_") as tf:
                    tf.write(data)
                    tmp_path = tf.name
                # 注意: _install_zip 的技能名降级方案取 zip 文件名(去后缀),
                # 临时文件名携带原始名前缀保证可读性
                r = ag.skills.install(tmp_path)
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError as _e:
                        wlog.warning("忽略异常(omni_agent/webserver.py:668): %s: %s", type(_e).__name__, _e)
                        pass
            return {"ok": r.startswith("[成功]"), "msg": r[:400]}
        if action == "remove" and name:
            r = ag.skills.remove(name)
            if r.startswith("[成功]") and ag.messages \
                    and ag.messages[0].get("role") == "system":
                ag.messages[0] = {"role": "system", "content": ag._system()}
            return {"ok": r.startswith("[成功]"), "msg": r[:300]}
        return {"ok": False, "error": f"未知技能操作: {action}"}

    # ---- 飞书连接器配置(网页端配置入口, 持久化到 settings.json) ----
    def feishu_config(self, action="", url="", app_id="", app_secret=""):
        """飞书配置管理: 读取/保存 webhook URL 与应用凭证到 settings.json"""
        from .settings import load_settings
        import json
        settings = load_settings(self.agent.wd)
        cfg_path = os.path.join(self.agent.wd, SETTINGS_DIR, "settings.json")
        connectors = settings.get("connectors", {})
        feishu = connectors.get("feishu", {"url": ""})
        if action == "save":
            feishu["url"] = url.strip()
            if app_id.strip():
                feishu["app_id"] = app_id.strip()
            if app_secret.strip():
                feishu["app_secret"] = app_secret.strip()
            connectors["feishu"] = feishu
            settings["connectors"] = connectors
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            # 运行时热更新
            self.agent.connectors.cfg = settings.get("connectors", {})
            self.bus.push("sys", text=f"🔧 飞书配置已保存: webhook={url[:40]}..."
                          + (f", app_id={app_id[:12]}..." if app_id else ""))
            return {"ok": True, "msg": "飞书配置已保存并即时生效",
                    "feishu": {"url": feishu.get("url", ""),
                               "app_id": feishu.get("app_id", ""),
                               "app_secret": feishu.get("app_secret", "")}}
        # 默认: 返回当前配置
        return {"ok": True,
                "feishu": {"url": feishu.get("url", ""),
                           "app_id": feishu.get("app_id", ""),
                           "app_secret": feishu.get("app_secret", "")}}

    # ---- MCP 服务配置(stdio + HTTP(Remote URL) 双协议管理与可用性验证) ----
    def mcp_config(self, action="", name="", command="", args=None, env=None,
                   transport="stdio", url="", headers=None):
        """MCP Server 配置管理: list/save/delete/test, 持久化到 settings.json。
        transport=http 时通过 Remote URL 连接云端 MCP(Streamable HTTP/SSE);
        save 成功后热重载 MCPManager, 新工具即时注册; test 仅做连接验证不落盘。"""
        import json as _json
        from .settings import load_settings
        from .mcp import MCPManager, create_server
        if action in ("save", "delete") and self.busy.locked():
            return {"ok": False, "error": "任务执行中, 请等待完成后再修改 MCP 配置"}
        # v5.3 修复[刷新后MCP列表为空]: 配置改为持久化到全局 ~/.haisnap/settings.json,
        # 会话切换/目录迁移/页面刷新后均可稳定读取(旧项目级配置由 load_settings 合并兼容)
        from .config import GLOBAL_DIR
        settings = load_settings(self.agent.wd)
        cfg_path = os.path.join(GLOBAL_DIR, "settings.json")
        servers = {k: v for k, v in settings.get("mcp_servers", {}).items()
                   if not k.startswith("_") and isinstance(v, dict)}
        transport = (transport or "stdio").strip().lower()
        is_remote = transport in ("http", "sse")   # SSE 与 HTTP 同为远程 URL 协议

        def _build_cfg():
            if is_remote:
                return {"transport": transport, "url": (url or "").strip(),
                        "headers": headers or {}}
            return {"command": (command or "").strip(), "args": args or [],
                    "env": env or {}}

        def _validate():
            if not (name or "").strip():
                return "name 不能为空"
            if is_remote:
                u = (url or "").strip()
                if not u.startswith(("http://", "https://")):
                    return f"{transport.upper()} 协议需填写合法的 Remote URL(http:// 或 https://)"
            elif not (command or "").strip():
                return "stdio 协议需填写启动命令 command"
            return None

        def _persist():
            """读取-合并-写回全局 settings.json, 不覆盖其他全局配置项"""
            g = {}
            if os.path.isfile(cfg_path):
                try:
                    with open(cfg_path, encoding="utf-8") as f:
                        g = _json.load(f)
                except (OSError, _json.JSONDecodeError):
                    g = {}
            g["mcp_servers"] = servers
            os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            tmp = cfg_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(g, f, ensure_ascii=False, indent=2)
            os.replace(tmp, cfg_path)
            wlog.info("mcp config persisted: %d servers -> %s", len(servers), cfg_path)

        def _reload():
            """热重载 MCPManager 并同步 ToolRunner 引用"""
            try:
                self.agent.mcp.close()
            except Exception as _e:
                wlog.warning("忽略异常(omni_agent/webserver.py:774): %s: %s", type(_e).__name__, _e)
                pass
            self.agent.mcp = MCPManager({"mcp_servers": servers})
            if hasattr(self.agent.tools, "mcp"):
                self.agent.tools.mcp = self.agent.mcp
            return {sn: sum(1 for sc in self.agent.mcp.schemas
                            if sc["function"]["name"].startswith(f"mcp__{sn}__"))
                    for sn in self.agent.mcp.servers}

        if action == "test":     # 可用性验证: 临时建立连接 initialize+list_tools 后关闭
            err = _validate()
            if err:
                return {"ok": False, "error": err}
            srv = None
            try:
                srv = create_server(name.strip(), _build_cfg())
                srv.initialize()
                tools = srv.list_tools()
                names = [t.get("name", "?") for t in tools][:20]
                wlog.info("mcp test ok: %s(%s) tools=%d", name, transport, len(tools))
                return {"ok": True, "transport": transport,
                        "msg": f"连接成功({transport}), 发现 {len(tools)} 个工具",
                        "tools": names}
            except FileNotFoundError:
                return {"ok": False, "error": f"命令不存在: {command}(请确认已安装)"}
            except Exception as e:
                wlog.warning("mcp test failed: %s: %s", name, e)
                return {"ok": False, "error": f"验证失败: {e}"}
            finally:
                if srv:
                    srv.close()
        if action == "save":
            err = _validate()
            if err:
                return {"ok": False, "error": err}
            servers[name.strip()] = _build_cfg()
            _persist()
            counts = _reload()
            connected = name.strip() in counts
            self.bus.push("sys", text=f"🔌 MCP Server '{name}'({transport}) 已保存"
                          + (f", 注册 {counts.get(name.strip(), 0)} 个工具"
                             if connected else "(连接失败, 请检查配置)"))
            return {"ok": True, "connected": connected,
                    "msg": ("已保存并连接成功, 注册 "
                            f"{counts.get(name.strip(), 0)} 个工具") if connected
                    else "已保存, 但连接失败(可稍后用「验证」排查)",
                    "servers": self._mcp_status(servers)}
        if action == "delete":
            if name not in servers:
                return {"ok": False, "error": f"MCP Server 不存在: {name}"}
            servers.pop(name)
            _persist()
            _reload()
            self.bus.push("sys", text=f"🔌 MCP Server '{name}' 已移除")
            return {"ok": True, "msg": f"已删除 {name}",
                    "servers": self._mcp_status(servers)}
        # 默认 list: 返回配置 + 运行时连接状态
        return {"ok": True, "servers": self._mcp_status(servers)}

    def _mcp_status(self, servers):
        """合并配置与运行时状态: connected / 已注册工具数"""
        live = getattr(self.agent.mcp, "servers", {})
        schemas = getattr(self.agent.mcp, "schemas", [])
        out = []
        for n, cfg in servers.items():
            cnt = sum(1 for sc in schemas
                      if sc["function"]["name"].startswith(f"mcp__{n}__"))
            tp = (cfg.get("transport") or "").lower()
            if tp not in ("http", "sse"):
                tp = "http" if (cfg.get("url") and not cfg.get("command")) else "stdio"
            out.append({"name": n, "transport": tp,
                        "command": cfg.get("command", ""),
                        "args": cfg.get("args", []), "env": cfg.get("env", {}),
                        "url": cfg.get("url", ""), "headers": cfg.get("headers", {}),
                        "connected": n in live, "tool_count": cnt})
        return out

    # ---- 用户级环境变量(同名覆盖, 即时生效, 持久化到 ~/.haisnap/env.json) ----
    def env_op(self, action, key="", value=""):
        if action == "list":
            return {"ok": True, "envs": EnvStore.list_masked()}
        if action == "set":
            ok, msg = EnvStore.set(key, value)
            if ok:
                wlog.info("env set: %s", key)
                self.bus.push("sys", text=f"🔧 {msg}")
            return {"ok": ok, "msg" if ok else "error": msg}
        if action in ("del", "unset", "remove"):
            ok, msg = EnvStore.unset(key)
            if ok:
                self.bus.push("sys", text=f"🔧 {msg}")
            return {"ok": ok, "msg" if ok else "error": msg}
        return {"ok": False, "error": f"未知环境变量操作: {action}"}

    # ---- 定时任务管理(Web端配置入口, 复用 scheduler.py 配置内核) ----
    def schedule_op(self, action, job=None, jid=""):
        from .config import SCHEDULE_FILE_DEFAULT
        from .scheduler import cron_match, scheduler_online

        def _read_cfg():
            try:
                with open(SCHEDULE_FILE_DEFAULT, encoding="utf-8") as f:
                    d = json.load(f)
                return d if isinstance(d, dict) else {"jobs": []}
            except (OSError, json.JSONDecodeError):
                return {"jobs": []}

        def _write_cfg(d):
            os.makedirs(os.path.dirname(SCHEDULE_FILE_DEFAULT) or ".",
                        exist_ok=True)
            tmp = SCHEDULE_FILE_DEFAULT + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SCHEDULE_FILE_DEFAULT)

        if action == "list":
            cfg = _read_cfg()
            online, _hb = scheduler_online()
            return {"ok": True, "jobs": cfg.get("jobs", []),
                    "scheduler_online": bool(online),
                    "config_path": SCHEDULE_FILE_DEFAULT,
                    "hint": ("调度器未运行: 需在服务器执行 "
                             "'python -m omni_agent schedule start' 启动"
                             if not online else "")}
        if action == "add":
            job = job or {}
            prompt = str(job.get("prompt", "")).strip()
            if not prompt:
                return {"ok": False, "error": "任务内容(prompt)不能为空"}
            timers = [k for k in ("every", "at", "cron") if job.get(k)]
            if len(timers) != 1:
                return {"ok": False,
                        "error": "定时方式 every/at/cron 必须且只能设置一个"}
            if job.get("every"):
                try:
                    if int(job["every"]) < 5:
                        return {"ok": False, "error": "间隔不得小于 5 秒"}
                except (TypeError, ValueError):
                    return {"ok": False, "error": "every 必须是整数秒"}
            if job.get("at"):
                try:
                    h, m = str(job["at"]).split(":")
                    assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
                except (ValueError, AssertionError):
                    return {"ok": False, "error": "at 必须是 HH:MM 格式"}
            if job.get("cron"):
                try:
                    cron_match(str(job["cron"]))
                except (ValueError, TypeError) as e:
                    return {"ok": False, "error": f"cron 表达式无效: {e}"}
            cfg = _read_cfg()
            jid_new = re.sub(r"[^a-zA-Z0-9_]+", "_",
                             str(job.get("id") or "")).strip("_")                 or "job_" + uuid.uuid4().hex[:6]
            if any(j.get("id") == jid_new for j in cfg.get("jobs", [])):
                return {"ok": False, "error": f"任务 id 已存在: {jid_new}"}
            entry = {"id": jid_new, "name": str(job.get("name") or jid_new)[:40],
                     "enabled": bool(job.get("enabled", True)),
                     "prompt": prompt[:2000],
                     "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            for k in ("every", "at", "cron"):
                if job.get(k):
                    entry[k] = int(job[k]) if k == "every" else str(job[k])
            if job.get("project"):
                entry["project"] = re.sub(r"[^a-zA-Z0-9_-]+", "_",
                                          str(job["project"]))[:40]
            if job.get("max_runs"):
                try:
                    entry["max_runs"] = max(0, int(job["max_runs"]))
                except (TypeError, ValueError):
                    pass
            cfg.setdefault("jobs", []).append(entry)
            _write_cfg(cfg)
            wlog.info("schedule job added: %s", jid_new)
            self.bus.push("sys", text=f"⏰ 定时任务已创建: {entry['name']}({jid_new})")
            return {"ok": True, "msg": f"定时任务已创建: {entry['name']}",
                    "job": entry}
        if action == "delete" and jid:
            cfg = _read_cfg()
            before = len(cfg.get("jobs", []))
            cfg["jobs"] = [j for j in cfg.get("jobs", []) if j.get("id") != jid]
            if len(cfg["jobs"]) == before:
                return {"ok": False, "error": f"任务不存在: {jid}"}
            _write_cfg(cfg)
            self.bus.push("sys", text=f"⏰ 定时任务已删除: {jid}")
            return {"ok": True, "msg": f"定时任务已删除: {jid}"}
        if action == "toggle" and jid:
            cfg = _read_cfg()
            for j in cfg.get("jobs", []):
                if j.get("id") == jid:
                    j["enabled"] = not j.get("enabled", True)
                    _write_cfg(cfg)
                    st = "启用" if j["enabled"] else "停用"
                    self.bus.push("sys", text=f"⏰ 定时任务已{st}: {jid}")
                    return {"ok": True, "msg": f"已{st}: {jid}",
                            "enabled": j["enabled"]}
            return {"ok": False, "error": f"任务不存在: {jid}"}
        return {"ok": False, "error": f"未知定时任务操作: {action}"}

    # ---- 附件上传(保存到工作区 uploads/, 供任务引用) ----
    MAX_UPLOAD = 20 * 1024 * 1024

    def upload(self, name, data_b64):
        fname = os.path.basename((name or "").strip().replace("\\", "/"))
        if not fname or fname.startswith("."):
            return {"ok": False, "error": "非法文件名"}
        try:
            data = base64.b64decode(data_b64 or "", validate=True)
        except Exception:
            return {"ok": False, "error": "文件内容解码失败"}
        if not data:
            return {"ok": False, "error": "文件内容为空"}
        if len(data) > self.MAX_UPLOAD:
            return {"ok": False, "error": "文件超过 20MB 上限"}
        d = os.path.join(self.agent.wd, "uploads")
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, fname)
        if os.path.exists(fp):   # 重名自动加序号, 不覆盖
            stem, ext = os.path.splitext(fname)
            fp = os.path.join(d, f"{stem}_{uuid.uuid4().hex[:4]}{ext}")
        with open(fp, "wb") as f:
            f.write(data)
        rel = os.path.relpath(fp, self.agent.wd).replace(os.sep, "/")
        self.bus.push("sys", text=f"附件已上传: {rel} ({len(data)} B)")
        return {"ok": True, "path": rel, "size": len(data),
                "name": os.path.basename(fp)}


    # ---- 工作空间(当前任务工作目录)文件管理 ----
    def workspace_list(self, rel=""):
        """浏览任务工作目录(单层列出子文件夹与文件, 防目录穿越)。
        首次对话前允许切换目录; 任务开始(会话固化)后锁定, 仅可查看浏览。"""
        wd = os.path.realpath(self.agent.wd)
        rel = (rel or "").strip().strip("/")
        target = os.path.realpath(os.path.join(wd, rel)) if rel else wd
        if not (target == wd or target.startswith(wd + os.sep)) \
                or not os.path.isdir(target):
            return {"ok": False, "error": "目录不存在或越权访问"}
        dirs, files = [], []
        try:
            names = sorted(os.listdir(target), key=str.lower)
        except OSError as e:
            return {"ok": False, "error": str(e)}
        for n in names:
            if n.startswith("."):
                continue
            fp = os.path.join(target, n)
            try:
                if os.path.isdir(fp):
                    if n in SKIP_DIRS:
                        continue
                    try:
                        cnt = len([x for x in os.listdir(fp)
                                   if not x.startswith(".")])
                    except OSError:
                        cnt = 0
                    dirs.append({"name": n, "count": cnt,
                                 "rel": os.path.relpath(fp, wd).replace(os.sep, "/")})
                else:
                    ext = os.path.splitext(n)[1].lower()
                    files.append({
                        "name": n,
                        "path": os.path.relpath(fp, wd).replace(os.sep, "/"),
                        "abs_path": os.path.abspath(fp),
                        "wd": wd, "ext": ext.lstrip("."),
                        "size": os.path.getsize(fp),
                        "mtime": os.path.getmtime(fp),
                        "mtime_str": time.strftime(
                            "%Y-%m-%d %H:%M",
                            time.localtime(os.path.getmtime(fp))),
                        "previewable": _previewable(ext),
                    })
            except OSError as _e:
                wlog.warning("workspace_list skip: %s: %s", type(_e).__name__, _e)
        locked = bool(self.agent.session) or self.busy.locked()
        return {"ok": True, "workdir": wd, "rel": rel, "dirs": dirs,
                "files": files, "locked": locked,
                "locked_reason": ("任务执行中" if self.busy.locked()
                                  else "任务已开始" if self.agent.session else "")}

    def make_dir(self, parent, name):
        """工作空间目录浏览器中创建新文件夹(支持命名)"""
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "文件夹名称不能为空"}
        if re.search(r'[\\/:*?"<>|]', name) or name in (".", ".."):
            return {"ok": False, "error": '名称含非法字符(\\ / : * ? " < > |)'}
        base = os.path.realpath(os.path.expanduser(
            (parent or "").strip() or self.agent.wd))
        if not os.path.isdir(base):
            return {"ok": False, "error": f"父目录不存在: {parent}"}
        np_ = os.path.join(base, name)
        if os.path.exists(np_):
            return {"ok": False, "error": f"同名文件/文件夹已存在: {name}"}
        try:
            os.makedirs(np_)
        except OSError as e:
            return {"ok": False, "error": f"创建失败: {e}"}
        wlog.info("mkdir: %s", np_)
        return {"ok": True, "path": np_, "msg": f"✓ 文件夹已创建: {np_}"}

    def dir_browse(self, path=""):
        """系统目录点选浏览器 —— 逐级点选导航选择工作目录(仅列目录)"""
        p = (path or "").strip().strip('"').strip("'")
        rp = os.path.realpath(os.path.expanduser(p)) if p \
            else os.path.realpath(self.agent.wd)
        if not os.path.isdir(rp):
            return {"ok": False, "error": f"目录不存在或不可访问: {p}"}
        dirs = []
        try:
            for n in sorted(os.listdir(rp), key=str.lower):
                if n.startswith("."):
                    continue
                fp = os.path.join(rp, n)
                try:
                    if os.path.isdir(fp):
                        dirs.append({"name": n, "path": fp})
                except OSError:
                    continue
        except PermissionError:
            return {"ok": False, "error": f"无权限访问该目录: {rp}"}
        except OSError as e:
            return {"ok": False, "error": str(e)}
        parent = os.path.dirname(rp)
        from .config import PROJECTS_DIR
        proot = os.path.realpath(PROJECTS_DIR)
        return {"ok": True, "path": rp,
                "parent": parent if parent and parent != rp else "",
                "dirs": dirs[:300],
                "home": os.path.expanduser("~"),
                "projects_root": proot if os.path.isdir(proot) else ""}

    def table_read(self, rel, wd=""):
        """表格文件在线预览(csv/tsv/xlsx) —— 全局统一入口(路径安全校验)"""
        fp = self.safe_path(rel, wd=wd)
        if not fp and self.file_root:
            fp = self.safe_path(rel, wd=self.file_root)
        if not fp:
            return {"ok": False, "error": "文件不存在或路径非法"}
        if os.path.getsize(fp) > MAX_SERVE_BYTES:
            return {"ok": False, "error": "文件过大, 请下载后本地查看"}
        return _read_table(fp)

    def set_workdir(self, path, force=False):
        """首次对话前手动切换任务工作目录; 任务开始后禁止修改
        目标目录非空时返回 confirm=True, 前端弹窗确认后携 force=True 重试执行"""
        if self.busy.locked():
            return {"ok": False, "error": "任务执行中, 禁止切换工作目录"}
        if self.agent.session:
            return {"ok": False,
                    "error": "当前任务已开始, 工作目录已锁定(仅可查看), 请新建会话后再切换"}
        p = (path or "").strip().strip('"').strip("'")
        if not p:
            return {"ok": False, "error": "目录不能为空"}
        rp = os.path.realpath(os.path.expanduser(p))
        if not os.path.isdir(rp):
            return {"ok": False, "error": f"目录不存在或不可访问: {p}"}
        if rp == os.path.realpath(self.agent.wd):
            return {"ok": True, "workdir": rp, "msg": "已处于该目录"}
        # 「选用此目录」前判断目录是否为空 —— 非空则要求前端弹窗确认
        if not force:
            try:
                entries = [n for n in os.listdir(rp) if not n.startswith(".")]
            except OSError as e:
                return {"ok": False, "error": f"目录不可读: {e}"}
            if entries:
                return {"ok": False, "confirm": True, "count": len(entries),
                        "workdir": rp,
                        "error": f"目标目录非空(含 {len(entries)} 项内容)"}
        self.agent._rebind_workdir(rp)   # WebBridge 已包装: 自动重新打补丁
        self.agent.messages = [{"role": "system", "content": self.agent._system()}]
        self.agent._context_loaded = True
        wlog.info("workspace dir changed: %s", rp)
        self.bus.push("sys", text=f"📂 任务工作目录已切换: {rp}")
        return {"ok": True, "workdir": rp, "msg": f"工作目录已切换: {rp}"}

    def workspace_read(self, rel):
        """读取工作目录内文件(工作空间/变更列表在线预览, 截断60000字符)"""
        fp = self.safe_path(rel)
        if not fp:
            return {"ok": False, "error": "文件不存在或路径非法"}
        ext = os.path.splitext(fp)[1].lower()
        size = os.path.getsize(fp)
        if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp"}:
            return {"ok": True, "kind": "image", "path": rel,
                    "name": os.path.basename(fp)}
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                content = f.read(60000)
            return {"ok": True, "kind": "text", "content": content,
                    "truncated": size > 60000, "path": rel,
                    "name": os.path.basename(fp), "ext": ext.lstrip(".")}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def changes(self, limit=300, art_id=""):
        """变更文件列表 —— 上一交付物至当前交付物时间段内发生更新或
        新增的所有类型文件(不含文件夹, 排除隐藏/系统目录):
        ≥2个交付物: 窗口=[上一交付物, 当前交付物]; 1个: [会话起点, 该交付物];
        无交付物: [会话起点, 现在]。每个文件附带完整路径(full_path)供前端悬浮显示。"""
        sid = (self.agent.session or {}).get("session_id", "")
        wd = os.path.realpath(self.agent.wd)
        t0 = self.session_t0.get(sid) or self.session_t0.get("_pending")
        arts = sorted((a for a in self.artifacts
                       if (a.get("sid") or "") == sid and a.get("ts")),
                      key=lambda a: a["ts"])
        # 精准时间窗 —— 按 art_id 定位当前交付物, 取其上一个交付物时间
        if art_id:
            idx = next((i for i, a in enumerate(arts) if a.get("id") == art_id), -1)
            if idx > 0:
                t_start, t_end = arts[idx - 1]["ts"], arts[idx]["ts"] + 2
            elif idx == 0:
                t_start, t_end = (t0 or 0), arts[0]["ts"] + 2
            else:
                t_start, t_end = (t0 or 0), time.time() + 1
        elif len(arts) >= 2:
            t_start, t_end = arts[-2]["ts"], arts[-1]["ts"] + 2
        elif len(arts) == 1:
            t_start, t_end = (t0 or 0), arts[-1]["ts"] + 2
        else:
            t_start, t_end = (t0 or 0), time.time() + 1
        art_map = {}   # rel路径 -> 交付物标题(徽标显示归属)
        for a in arts:
            for f in (a.get("files") or []):
                if f.get("path"):
                    art_map[f["path"]] = a.get("title") or ""
        # v8.3 修复: 按 art_id 查看历史交付物时, 后续任务修改过的文件 mtime
        # 已超出该交付物时间窗 → 旧交付物窗口内查不到任何文件(目录为空),
        # 而这些文件全部堆到最新交付物窗口。修复: 目标交付物登记时的文件清单
        # (含当时 mtime)必须纳入本窗口展示, 与 mtime 扫描结果合并去重。
        pinned = {}    # rel -> 登记时的文件元数据(优先展示登记时刻的状态)
        if art_id:
            cur_art = next((a for a in arts if a.get("id") == art_id), None)
            if cur_art:
                for f in (cur_art.get("files") or []):
                    if f.get("path"):
                        pinned[f["path"]] = f
        out = []
        for root, dirs, files_ in os.walk(wd):
            dirs[:] = [d for d in dirs
                       if not d.startswith(".") and d not in SKIP_DIRS]
            for fn in files_:
                if fn.startswith("."):
                    continue   # 隐藏文件不计入变更列表(与隐藏目录策略一致)
                fp = os.path.join(root, fn)
                try:
                    mt = os.path.getmtime(fp)
                    rel = os.path.relpath(fp, wd).replace(os.sep, "/")
                    if rel in pinned:
                        # 登记清单内文件: 用登记时刻 mtime 展示(不受后续修改影响)
                        mt = pinned[rel].get("mtime") or mt
                        pinned.pop(rel, None)
                    elif not (t_start <= mt <= t_end):
                        continue
                    ext = os.path.splitext(fn)[1].lower()
                    out.append({
                        "name": fn, "path": rel, "wd": wd,
                        "full_path": fp.replace(os.sep, "/"),
                        "ext": ext.lstrip("."),
                        "size": os.path.getsize(fp), "mtime": mt,
                        "mtime_str": time.strftime("%Y-%m-%d %H:%M:%S",
                                                   time.localtime(mt)),
                        "previewable": _previewable(ext),
                        "artifact": art_map.get(rel, ""),
                    })
                except OSError as _e:
                    wlog.warning("changes skip: %s: %s", type(_e).__name__, _e)
            if len(out) >= limit * 3:
                break

        def _fmt(t):
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
        out.sort(key=lambda x: x["mtime"], reverse=True)
        return {"ok": True, "files": out[:limit], "workdir": wd,
                "since": _fmt(t_start) if t_start else "",
                "until": _fmt(t_end) if arts else "",
                "scope": "delivery_window",
                "msg": "" if out else "该时间段内暂无更新或新增的文件"}

    def set_file_root(self, path):
        """修改文件管理根目录(适配 Windows 路径如 C:\\Users\\xx\\Desktop)"""
        p = (path or "").strip().strip('"').strip("'")
        if not p:
            self.file_root = self._default_file_root()
            wlog.info("file root reset to default: %s", self.file_root)
            return {"ok": True, "root": self.file_root,
                    "msg": f"已重置为默认下载目录: {self.file_root}"}
        rp = os.path.realpath(os.path.expanduser(p))
        if not os.path.isdir(rp):
            wlog.warning("set_file_root invalid dir: %s", p)
            return {"ok": False, "error": f"目录不存在或不可访问: {p}"}
        self.file_root = rp
        wlog.info("file root changed: %s", rp)
        return {"ok": True, "root": rp, "msg": f"文件管理根目录已切换: {rp}"}

    def file_list(self, query="", ftype="", limit=200):
        """本地操作系统文件管理 —— 默认展示/检索系统下载目录, 根目录可修改
        - query: 文件名模糊匹配(不区分大小写)
        - ftype: 类型过滤(all/image/doc/code/media/web/data/other)
        - 同名文件: 返回绝对路径供前端高亮标识; 导入对话框时使用绝对路径
        """
        wd = os.path.realpath(self.file_root)
        results = []
        name_count = {}   # 统计同名文件, >1 则前端高亮绝对路径

        TYPE_MAP = {
            "image": {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp"},
            "doc": {".md", ".txt", ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".csv"},
            "code": {".py", ".js", ".ts", ".json", ".css", ".html", ".htm", ".xml", ".yml", ".yaml", ".sh", ".sql", ".java", ".go", ".rs", ".c", ".cpp"},
            "media": {".mp3", ".wav", ".ogg", ".mp4", ".webm", ".avi", ".mov"},
            "web": {".html", ".htm", ".css", ".js"},
            "data": {".json", ".csv", ".xml", ".yml", ".yaml", ".sql", ".db", ".sqlite"},
        }
        ext_set = TYPE_MAP.get(ftype, None) if ftype and ftype != "all" else None
        q_lower = (query or "").lower().strip()

        for root, dirs, files in os.walk(wd):
            # 跳过隐藏目录与 node_modules 等
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS]
            for fname in files:
                if fname.startswith("."):
                    continue
                fp = os.path.join(root, fname)
                rel = os.path.relpath(fp, wd).replace(os.sep, "/")
                ext = os.path.splitext(fname)[1].lower()
                if ext_set and ext not in ext_set:
                    continue
                if q_lower and q_lower not in fname.lower():
                    continue
                try:
                    size = os.path.getsize(fp)
                    mtime = os.path.getmtime(fp)
                except OSError as _e:
                    wlog.warning("忽略异常(omni_agent/webserver.py:950): %s: %s", type(_e).__name__, _e)
                    continue
                base_name = fname.lower()
                name_count[base_name] = name_count.get(base_name, 0) + 1
                results.append({
                    "name": fname,
                    "path": rel,
                    "abs_path": os.path.abspath(fp),
                    "ext": ext.lstrip("."),
                    "size": size,
                    "mtime": mtime,
                    "mtime_str": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)),
                })
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break

        # 标记同名文件(需高亮绝对路径); 附带归属根目录供预览/下载链接构造
        for r in results:
            r["dup"] = name_count.get(r["name"].lower(), 1) > 1
            r["wd"] = wd

        # 按修改时间倒序
        results.sort(key=lambda x: x["mtime"], reverse=True)
        return {"files": results, "total": len(results), "workdir": wd,
                "root": wd, "default_root": self._default_file_root()}

    def file_read(self, rel):
        """读取文件内容供引用分析(文本类, 截断 60000 字符)。
        优先按文件管理根目录解析, 回退工作区解析(兼容旧调用)"""
        fp = self.safe_path(rel, wd=self.file_root) or self.safe_path(rel)
        if not fp:
            wlog.warning("file_read denied: %s", str(rel)[:120])
            return {"ok": False, "error": "文件不存在或路径非法"}
        ext = os.path.splitext(fp)[1].lower()
        size = os.path.getsize(fp)
        if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp"}:
            return {"ok": True, "kind": "image", "path": rel, "name": os.path.basename(fp)}
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                content = f.read(60000)
            return {"ok": True, "kind": "text", "content": content,
                    "truncated": size > 60000, "path": rel,
                    "name": os.path.basename(fp), "ext": ext.lstrip(".")}
        except OSError as e:
            return {"ok": False, "error": str(e)}

    def command(self, cmd, arg="", art_id=""):
        """网页版会话命令(与 CLI 斜杠命令一致); art_id: 快照与交付物绑定"""
        ag = self.agent
        if cmd == "clear":
            # v6.1阻塞性修复: 任务执行中清空消息链会破坏进行中的工具调用上下文
            if self.busy.locked():
                return {"ok": False, "error": "任务执行中, 请等待完成后再清空会话"}
            ag.messages = [ag.messages[0]]
            return {"ok": True, "msg": "会话已清空"}
        if cmd == "compact":
            return {"ok": False, "error": "上下文压缩功能已移除(ContextCompressor 已下线)"}
        if cmd == "mode" and arg in ("checklist", "auto", "chat"):
            ag.cmd_mode(arg)
            return {"ok": True, "msg": f"已切换到 {arg} 模式"}
        if cmd == "thinking" and arg in ("on", "off"):
            ag.llm.thinking = (arg == "on")
            # 参数黑名单机制已整体移除(400 仅本次请求临时降级),
            # thinking 开关即开即生效, 无需任何自愈清理
            # v8.3 修复: 思考开关只改运行时变量, 配置中心 HAISNAP_THINKING
            # 生效值不跟随变化(重启也会丢失) —— 同步写入用户级覆盖层并热重载
            try:
                EnvStore.set("HAISNAP_THINKING", arg)
                from .settings import S as _S
                _S.reload()
            except Exception as _e:
                wlog.warning("thinking 配置持久化失败: %s", _e)
            self.bus.push("sys", text=f"✻ 思考模式已{'开启' if arg == 'on' else '关闭'}"
                          "(已同步到配置中心, 重启后仍生效)")
            return {"ok": True, "msg": f"thinking 已{'开启' if arg == 'on' else '关闭'}"}
        if cmd == "memory_add" and arg:
            ag.add_memory(arg)
            return {"ok": True, "msg": "已写入项目记忆"}
        if cmd == "checkpoint_create":
            # v6.1阻塞性修复: 快照创建改为后台线程异步执行 —— 大工作区全量
            # 哈希可能耗时数十秒, 原同步实现会阻塞 HTTP 处理线程导致前端卡死;
            # 完成后经事件总线推送结果, 前端 sys 事件自动触发 refresh 刷新快照列表
            note = (arg or "网页手动快照")[:100]
            msgs_ref = ag.messages

            def _snap_worker():
                try:
                    cid, reused = ag.ckpt.create(note=note, messages=msgs_ref)
                    # 快照与交付物绑定 —— snapshot_id 回写登记表(页面刷新
                    # 后经 /api/state 仍可见), 并推送定向事件供前端将按钮替换为快照ID
                    if art_id:
                        for a in self.artifacts:
                            if a.get("id") == art_id:
                                a["snapshot_id"] = cid
                                break
                        self._save_state()   # 快照绑定信息落盘
                        self.bus.push("artifact_snapshot", art_id=art_id,
                                      cid=cid, ok=True)
                    self.bus.push("sys", text=f"⛃ 快照 {cid} "
                                  + ("(工作区未变化, 复用已有快照)" if reused else "已创建")
                                  + f" · {note}")
                except Exception as e:
                    wlog.error("checkpoint create failed: %s: %s",
                               type(e).__name__, e)
                    if art_id:   # 失败时通知前端恢复按钮可用
                        self.bus.push("artifact_snapshot", art_id=art_id,
                                      cid="", ok=False)
                    self.bus.push("sys", text=f" 快照创建失败: {type(e).__name__}: {e}")
            threading.Thread(target=_snap_worker, daemon=True,
                             name="ckpt-create").start()
            return {"ok": True, "msg": f" 快照创建中({note}), 完成后将在时间线通知"}
        if cmd == "checkpoint_rollback" and arg:
            if self.busy.locked():
                return {"ok": False, "error": "任务执行中, 禁止回滚工作区"}
            ctx, msg = ag.ckpt.rollback(arg, restore_context=False)
            self.bus.push("sys", text=msg)   # 广播结果, 时间线可见
            return {"ok": not msg.startswith("[错误]"), "msg": msg}
        # Web端斜杠命令 —— 纯展示类命令(与CLI一致), 返回文本经前端 sys 气泡渲染
        if cmd == "cost":
            c = self.cost()
            return {"ok": True, "msg": f" 模型调用 {c.get('calls', 0)} 次 | "
                    f"prompt {c.get('prompt_tokens', 0)} tok | "
                    f"completion {c.get('completion_tokens', 0)} tok | "
                    f"合计 {c.get('prompt_tokens', 0) + c.get('completion_tokens', 0)} tok"}
        if cmd == "sources":
            lines = ag.sources.render_list()
            return {"ok": True, "msg": lines if lines.strip() else "（暂无信源登记）"}
        if cmd == "sessions":
            items = ag.sessions.list()[:20]
            if not items:
                return {"ok": True, "msg": "（暂无历史会话）"}
            lines = ["📋 历史会话:"]
            for s in items:
                tag = " " if s.get("pinned") else "  "
                lines.append(f"{tag}[{s.get('session_id', '?')}] {s.get('name_zh', s.get('name', '(未命名)'))}")
            return {"ok": True, "msg": "\n".join(lines)}
        if cmd == "tools":
            tools = ag.all_tool_schemas()
            if not tools:
                return {"ok": True, "msg": "（无可用工具）"}
            lines = ["🔧 可用工具:"]
            for t in tools:
                f = t["function"]
                title = t.get("title") or TOOL_TITLES.get(f["name"], f["name"])
                lines.append(f"  {title} ({f['name']})")
            return {"ok": True, "msg": "\n".join(lines)}
        return {"ok": False, "error": f"未知命令: {cmd}"}


def make_handler(bridge):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _read_body(self):
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except json.JSONDecodeError:
                return {}

        def _serve_file(self, rel, as_download, wd="", from_p_route=False):
            """成果物在线预览(inline)与下载(attachment), 带路径安全校验"""
            fp = bridge.safe_path(rel, wd=wd)
            if not fp:
                self._json({"error": "非法路径或文件不存在"}, 404)
                return
            # v5.10-fix: HTML 预览请求 302 重定向到目录式路由 /p/<b64wd>/<rel>,
            # 使页面内相对引用的 CSS/JS/图片可被正确解析(修复静态资源404)
            ext0 = os.path.splitext(fp)[1].lower()
            if (not as_download and not from_p_route and ext0 in (".html", ".htm")):
                # 归属目录: safe_path 可能命中显式 wd 或回退到当前工作目录
                base_wd = os.path.realpath(wd) if wd else os.path.realpath(bridge.agent.wd)
                if not fp.startswith(base_wd + os.sep):
                    base_wd = os.path.realpath(bridge.agent.wd)
                rel_fixed = os.path.relpath(fp, base_wd).replace(os.sep, "/")
                token = wd_token(base_wd)   # md5 直达 token
                self.send_response(302)
                self.send_header("Location",
                                 f"/p/{token}/" + urllib.parse.quote(rel_fixed))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")   # HTTP/1.1 长连接必须显式声明
                self.end_headers()
                return
            size = os.path.getsize(fp)
            if size > MAX_SERVE_BYTES:
                self._json({"error": f"文件超过 {MAX_SERVE_BYTES // 1048576}MB, 请到服务器获取"}, 413)
                return
            ext = os.path.splitext(fp)[1].lower()
            if as_download:
                ctype, disp = "application/octet-stream", "attachment"
            else:
                ctype, disp = MIME_MAP.get(ext, "application/octet-stream"), "inline"
            with open(fp, "rb") as f:
                body = f.read()
            fname = urllib.parse.quote(os.path.basename(fp))
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f"{disp}; filename*=UTF-8''{fname}")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            # GET 关键端点注入 tracker_id(排除高频轮询与静态页, 防日志刷屏)
            if path.startswith("/api/") and path not in ("/api/events", "/api/state",
                                                         "/api/stream"):
                tid = new_tracker("req")
                wlog.info("http request: tracker=%s GET %s", tid, path)
            if path in ("/", "/index.html"):
                fp = os.path.join(WEB_DIR, "index.html")
                with open(fp, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/state":
                self._json(bridge.state())
                return
            if path == "/api/stream":   # SSE 单连接推流(修复平台403限频)
                qs = urllib.parse.parse_qs(parsed.query)
                since = int(qs.get("since", ["0"])[0])
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    while True:
                        events = bridge.bus.wait_since(since, timeout=15)
                        if events:
                            since = events[-1]["i"] + 1
                            data = json.dumps({"events": events, "next": since},
                                              ensure_ascii=False, default=str)
                            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                        else:
                            self.wfile.write(b": ping\n\n")   # 心跳保活
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError) as _e:
                    wlog.info("sse client disconnected: %s", type(_e).__name__)
                return
            if path == "/api/events":   # 长轮询: since 之后的事件(SSE 的降级兼容方案)
                qs = urllib.parse.parse_qs(parsed.query)
                since = int(qs.get("since", ["0"])[0])
                wait = min(float(qs.get("timeout", ["10"])[0]), 25.0)  # 上限25s降低轮询频率
                events = bridge.bus.wait_since(since, timeout=wait)
                # 事件列表有界裁剪后, next 必须用事件绝对序号计算
                nxt = (events[-1]["i"] + 1) if events else since
                self._json({"events": events, "next": nxt})
                return
            if path == "/api/artifacts":   # 成果物登记表(历史可追溯)
                self._json({"artifacts": bridge.artifacts})
                return
            if path == "/api/history":
                # 从持久化消息链重建完整时间线(用户/回答/工具执行过程),
                # 修复历史对话来回切换后执行过程数据丢失的问题
                self._json({"items": bridge.rebuild_timeline()})
                return
            if path == "/api/sessions":     # 历史对话列表(置顶优先)
                cur = (bridge.agent.session or {}).get("session_id")
                self._json({"sessions": bridge.agent.sessions.list(),
                            "current": cur})
                return
            if path == "/api/memory":       # 记忆查看(项目+全局)
                self._json(bridge.memory_view())
                return
            if path == "/api/workspace":     # 工作空间 —— 浏览任务工作目录
                qs = urllib.parse.parse_qs(parsed.query)
                self._json(bridge.workspace_list(qs.get("dir", [""])[0]))
                return
            if path == "/api/dir_browse":    # 目录点选浏览器(切换工作目录)
                qs = urllib.parse.parse_qs(parsed.query)
                self._json(bridge.dir_browse(qs.get("path", [""])[0]))
                return
            if path == "/api/changes":       # 交付物文件变更列表(按 art_id 精准时间窗)
                qs_ch = urllib.parse.parse_qs(parsed.query)
                self._json(bridge.changes(art_id=qs_ch.get("art_id", [""])[0]))
                return
            if path == "/api/files":          # 文件管理 —— 浏览/检索工作区文件
                qs = urllib.parse.parse_qs(parsed.query)
                self._json(bridge.file_list(
                    qs.get("q", [""])[0],
                    qs.get("type", ["all"])[0],
                    int(qs.get("limit", ["500"])[0])))
                return
            if path == "/api/skills":       # 技能列表
                self._json(bridge.skills_list())
                return
            if path == "/api/env":          # 用户级环境变量列表(敏感值打码)
                self._json(bridge.env_op("list"))
                return
            if path == "/api/settings":     # 配置诊断 —— 四层配置链生效值与来源
                from .settings import S
                S.reload()   # 热读取: 文件修改后无需重启
                self._json(S.inspect())
                return
            if path == "/api/mcp":          # MCP Server 配置列表+连接状态
                self._json(bridge.mcp_config("list"))
                return
            if path == "/api/schedule":     # 定时任务列表(含调度器在线状态)
                self._json(bridge.schedule_op("list"))
                return
            if path == "/api/models":       # 模型管理 —— 注册表+路由配置
                from .model_router import ModelRouter
                self._json({"ok": True,
                            "models": ModelRouter.list_models(),
                            "routing": ModelRouter.describe_config()})
                return
            if path.startswith("/p/"):
                # v5.10-fix[静态资源404]: 目录式预览路由 /p/<b64wd>/<relpath> ——
                # HTML 成果物经此路由预览时, 页面内相对引用(css/style.css、js/app.js)
                # 会被浏览器解析为 /p/<b64wd>/css/style.css, 从而正确命中本路由,
                # 修复多文件 HTML 应用预览时 CSS/JS 返回 404 的问题。
                parts = path[len("/p/"):].split("/", 1)
                if len(parts) != 2 or not parts[1]:
                    self._json({"error": "非法预览路径"}, 400)
                    return
                # 优先 md5 token 映射表反查, 未命中回退 base64 历史兼容
                wd_dec = wd_from_token(parts[0])
                if not wd_dec:
                    self._json({"error": "非法或已失效的预览token"}, 400)
                    return
                rel = urllib.parse.unquote(parts[1])
                self._serve_file(rel, as_download=False, wd=wd_dec,
                                 from_p_route=True)
                return
            if path in ("/api/preview", "/api/download"):
                qs = urllib.parse.parse_qs(parsed.query)
                rel = qs.get("path", [""])[0]
                self._serve_file(rel, as_download=(path == "/api/download"),
                                 wd=qs.get("wd", [""])[0])
                return
            if path == "/health":
                self._json({"status": "ok", "version": __version__})
                return
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            body = self._read_body()
            # 从收到前端请求开始创建 tracker_id, 注入本请求链路所有日志
            tid = new_tracker("req")
            wlog.info("http request: tracker=%s POST %s", tid, path)
            if path == "/api/task":
                prompt = (body.get("prompt") or "").strip()
                if not prompt:
                    self._json({"ok": False, "error": "prompt 不能为空"}, 400)
                    return
                self._json(bridge.submit_task(prompt, body.get("policy")))
                return
            if path == "/api/stop":     # 手动终止当前任务
                self._json(bridge.stop_task())
                return
            if path == "/api/reply":
                self._json(bridge.reply(body.get("id", ""), body))
                return
            if path == "/api/steer":       # 人机协同实时共驾
                self._json(bridge.steer(body.get("text", "")))
                return
            if path == "/api/md_save":     # Markdown 双栏编辑保存
                self._json(bridge.md_save(body.get("path", ""),
                                          body.get("content", ""),
                                          body.get("wd", "")))
                return
            if path == "/api/command":
                self._json(bridge.command(body.get("cmd", ""), body.get("arg", ""),
                                          art_id=body.get("art_id", "")))
                return
            if path == "/api/session":      # 会话管理: rename/pin/delete/switch/new
                self._json(bridge.session_op(body.get("action", ""),
                                             body.get("sid", ""), body.get("value", "")))
                return
            if path == "/api/skill":        # 技能管理: load/unload/view/install/remove
                self._json(bridge.skill_op(body.get("action", ""),
                                           body.get("name", ""), body.get("source", "")))
                return
            if path == "/api/file_read":     # 读取文件内容供引用分析
                self._json(bridge.file_read(body.get("path", "")))
                return
            if path == "/api/workspace_root":   # 首次对话前切换工作目录
                self._json(bridge.set_workdir(body.get("path", ""),
                                              bool(body.get("force"))))
                return
            if path == "/api/mkdir":            # 目录浏览器中创建文件夹
                self._json(bridge.make_dir(body.get("parent", ""),
                                           body.get("name", "")))
                return
            if path == "/api/workspace_read":   # 工作空间文件在线预览
                self._json(bridge.workspace_read(body.get("path", "")))
                return
            if path == "/api/table_read":       # 表格文件在线预览(csv/tsv/xlsx)
                self._json(bridge.table_read(body.get("path", ""),
                                             body.get("wd", "")))
                return
            if path == "/api/files_root":    # 修改文件管理根目录(适配Windows)
                self._json(bridge.set_file_root(body.get("path", "")))
                return
            if path == "/api/upload":       # 附件上传(base64 JSON)
                self._json(bridge.upload(body.get("name", ""), body.get("data", "")))
                return
            if path == "/api/feishu":      # 飞书配置 读取/保存
                act = body.get("action", "get")
                self._json(bridge.feishu_config(
                    act, body.get("url", ""),
                    body.get("app_id", ""), body.get("app_secret", "")))
                return
            if path == "/api/mcp":          # MCP 配置 save/delete/test
                self._json(bridge.mcp_config(
                    body.get("action", "list"), body.get("name", ""),
                    body.get("command", ""), body.get("args"),
                    body.get("env"), body.get("transport", "stdio"),
                    body.get("url", ""), body.get("headers")))
                return
            if path == "/api/env":          # 环境变量 set/del(同名覆盖)
                self._json(bridge.env_op(body.get("action", ""),
                                         body.get("key", ""), body.get("value", "")))
                return
            if path == "/api/schedule":     # 定时任务 add/delete/toggle
                self._json(bridge.schedule_op(body.get("action", ""),
                                              job=body.get("job"),
                                              jid=body.get("id", "")))
                return
            if path == "/api/models":       # 模型管理 —— 登记/移除模型
                from .settings import S
                action = body.get("action", "")
                if action == "add":
                    ok, msg = S.pool_add(body.get("name", ""),
                                         body.get("base_url", ""),
                                         body.get("api_key", ""))
                elif action == "del":
                    ok, msg = S.pool_del(body.get("name", ""))
                else:
                    self._json({"ok": False, "error": f"未知操作: {action}"}, 400)
                    return
                wlog.info("model pool op: action=%s name=%s ok=%s",
                          action, body.get("name", ""), ok)
                self._json({"ok": ok, "msg": msg} if ok
                           else {"ok": False, "error": msg})
                return
            if path == "/api/config":
                # 基础语言模型与视觉模型双栏独立配置——视觉模型支持
                # 独立 Base URL / API Key(留空则继承基础语言模型配置)
                # 保存后 SettingsManager 热重载, 配置回显与生效值实时一致
                from .settings import S
                if body.get("vision_reset"):
                    # 恢复继承: 清除视觉独立端点, 重新回到基础模型 Key/URL
                    os.environ.pop("HAISNAP_VISION_API_KEY", None)
                    os.environ.pop("HAISNAP_VISION_BASE_URL", None)
                    # 同步清除持久化存储, 避免重启后被 env.json 重新注入
                    EnvStore.unset("HAISNAP_VISION_API_KEY")
                    EnvStore.unset("HAISNAP_VISION_BASE_URL")
                    wlog.info("vision endpoint reset: inherit base model config")
                if body.get("fallback_reset"):
                    # 清除备用语言模型配置(停用失败兜底策略)
                    for k in ("HAISNAP_FALLBACK_MODEL",
                              "HAISNAP_FALLBACK_API_KEY",
                              "HAISNAP_FALLBACK_BASE_URL"):
                        os.environ.pop(k, None)
                        EnvStore.unset(k)
                    wlog.info("fallback model config cleared")
                # persist=true 时同步持久化到 ~/.haisnap/env.json,
                # 重启服务后配置仍然生效, 配置弹窗可稳定回显
                persist = bool(body.get("persist"))
                # 模型路由配置(开关+专用模型槽位), 留空不覆盖,
                # 显式传 routing_enabled 则持久化开关状态
                if "routing_enabled" in body:
                    _rv = "on" if body.get("routing_enabled") else "off"
                    os.environ["HAISNAP_ROUTING"] = _rv
                    if bool(body.get("persist")):
                        EnvStore.set("HAISNAP_ROUTING", _rv)
                    wlog.info("routing config: enabled=%s", _rv)
                for env_key, body_key in (("HAISNAP_API_KEY", "api_key"),
                                          ("HAISNAP_BASE_URL", "base_url"),
                                          ("HAISNAP_MODEL", "model"),
                                          ("HAISNAP_VISION_MODEL", "vision_model"),
                                          ("HAISNAP_VISION_API_KEY", "vision_api_key"),
                                          ("HAISNAP_VISION_BASE_URL", "vision_base_url"),
                                          ("HAISNAP_FALLBACK_MODEL", "fallback_model"),
                                          ("HAISNAP_FALLBACK_API_KEY", "fallback_api_key"),
                                          ("HAISNAP_FALLBACK_BASE_URL", "fallback_base_url"),
                                          ("HAISNAP_ROUTING_LIGHT", "routing_light_model"),
                                          ("HAISNAP_ROUTING_CODE", "routing_code_model"),
                                          ("HAISNAP_ROUTING_LONG", "routing_long_model"),
                                          ("HAISNAP_ROUTING_LONG_TOKENS", "routing_long_tokens")):
                    v = (body.get(body_key) or "").strip()
                    if v:
                        os.environ[env_key] = v
                        if persist:
                            EnvStore.set(env_key, v)
                wlog.info("model config updated: model=%s vision=%s "
                          "vision_endpoint=%s",
                          os.environ.get("HAISNAP_MODEL", "(默认)"),
                          os.environ.get("HAISNAP_VISION_MODEL", "(默认qwen-vl-max)"),
                          "独立" if os.environ.get("HAISNAP_VISION_API_KEY")
                          or os.environ.get("HAISNAP_VISION_BASE_URL") else "继承基础模型")
                # 热重载 SettingsManager, 使后续 state() 调用返回最新配置
                S.reload()
                # 切换模型后重置"网关不透传思考"一次性提示标志 ——
                # 新端点思考透传能力未知, 允许重新提示; 同时若思考模式开启,
                # 主动推送当前思考配置状态(用户切换模型后常误以为思考被关闭)
                try:
                    bridge.agent.llm._blank_think_notified = False
                    # 新端点的思考能力未知, 清空"无思考模型"记录, 允许重新提示
                    bridge.agent.llm._no_think_models.clear()
                    bridge.agent.llm._loop_no_think.clear()  # v8.8
                except Exception:
                    pass
                # 参数黑名单机制已整体移除, 更换模型端点无需清理
                # api_key_set 统一走 SettingsManager 四层链路(S.resolve)
                # 校验, 与 state() 回显逻辑保持一致 —— 修复仅查环境变量导致
                # settings.json 中配置的 key 被误判为未设置的逻辑漏洞
                _ak, _ = S.resolve("model.api_key")
                _vk, _ = S.resolve("model.vision_api_key")
                _fk, _ = S.resolve("model.fallback_api_key")
                self._json({"ok": True,
                            "api_key_set": bool(_ak),
                            "base_url": S.get_str("model.base_url"),
                            "model": S.get_str("model.model"),
                            "vision_key_set": bool(_vk),
                            "vision_base_url": S.get_str("model.vision_base_url"),
                            "vision_model": S.get_str("model.vision_model"),
                            "fallback_model": S.get_str("model.fallback_model"),
                            "fallback_key_set": bool(_fk),
                            "fallback_base_url":
                                S.get_str("model.fallback_base_url")})
                return
            self._json({"error": "not found"}, 404)
    return Handler


def serve(host="0.0.0.0", port=3000, workdir="."):
    bridge = WebBridge(workdir)
    httpd = ThreadingHTTPServer((host, port), make_handler(bridge))
    cprint(f"\n  ✦ omni-agent 网页版已启动: http://{host}:{port}", C.GREEN)
    cprint(f"  项目目录: {bridge.agent.wd}", C.GRAY)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        cprint("\n已停止网页服务", C.YELLOW)
    finally:
        httpd.server_close()   # v3.8.1: 释放监听 socket, 修复资源泄漏
        bridge.agent.close()