
import base64
import concurrent.futures
import fnmatch
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid

from .browser import BrowserController
from .config import (ASK_DEFAULT_TIMEOUT, BASH_TIMEOUT, MAX_FILE_READ,
                     MAX_TOOL_OUTPUT, SETTINGS_DIR, SKIP_DIRS, VISION_MODEL,
                     WEB_FETCH_TIMEOUT)
from .settings import S   # 统一配置链(env > 项目 > 全局 > config)
from .logger import get_logger
from .sources import SourceRegistry
from .tools_schema import TOOLS_SCHEMA
from .ui import C, cprint, timed_input
from .unifuncs import UniFuncs

log = get_logger("tools")

# base64 / data-URI 匹配模式(用于过滤工具返回值, 防注入 LLM 上下文)
_BASE64_DATAURI_RE = re.compile(
    r"data:image/[a-z]+;base64,[A-Za-z0-9+/=\s]{200,}", re.I)
# 裸 base64 块(连续超 500 字符的 base64 字符, 含换行)
_BARE_BASE64_RE = re.compile(
    r"(?:^|\n)([A-Za-z0-9+/]{76,}\s*\n?){6,}", re.M)


def _sanitize_result(result_str):
    """过滤工具返回值中的 base64 / data-URI, 防注入 LLM 上下文。
    策略: 匹配到 data:image/...;base64,<长串> 则替换为占位提示;
    裸 base64 块(连续多行 base64) 同样替换。"""
    if not result_str:
        return result_str
    # 替换 data-URI
    result_str = _BASE64_DATAURI_RE.sub(
        "[base64图片数据已省略]", result_str)
    # 替换裸 base64 块
    result_str = _BARE_BASE64_RE.sub(
        "\n[base64数据已省略]", result_str)
    return result_str



class ToolRunner:
    def __init__(self, workdir, perm, hooks, sources, connectors, ckpt, skills,
                 mcp, llm, session):
        self.wd = workdir
        self.perm = perm
        self.hooks = hooks
        self.sources = sources
        self.connectors = connectors
        self.ckpt = ckpt
        self.skills = skills
        self.mcp = mcp
        self.llm = llm
        self.todos = []
        self.bg_jobs = {}          # 后台 bash 任务: id -> {proc, log, cmd}
        self.deployment = None     # 当前部署: {proc, port, url, command, dir}
        self.delivered = set()     # 已交付产物指纹(杜绝重复交付)
        self.delivery_snapshot = None  # 最近一次交付的自动快照 {cid, dir}
        self.restored_context = None  # checkpoint rollback 恢复的上下文(主循环取走)
        self.session = session or {}
        self.swarm_cb = None       # Swarm 子任务结构化事件回调(网页端树状展示)
        # 文件读取版本跟踪 {path: (mtime, size)} —— 编辑前校验上下文一致性
        self._read_versions = {}
        # 本轮已提示"无需重复读取"的文件集合(第二次重读放行, 防死锁)
        self._read_skipped = set()

    def _abs(self, p):
        return p if os.path.isabs(p) else os.path.join(self.wd, p)

    def _track_read(self, path):
        """记录文件读取时的版本(mtime, size)"""
        p = self._abs(path)
        try:
            st = os.stat(p)
            self._read_versions[path] = (st.st_mtime, st.st_size)
        except OSError as _e:
            log.warning("忽略异常(omni_agent/tool_runner.py:94): %s: %s", type(_e).__name__, _e)
            pass

    def _check_read_fresh(self, path):
        """检查文件自上次读取后是否被修改(版本一致性校验)
        返回: True=版本一致(可以安全编辑), False=需重新读取"""
        p = self._abs(path)
        try:
            st = os.stat(p)
        except OSError:
            return False
        ver = self._read_versions.get(path)
        if ver is None:
            return False  # 从未读取过
        return ver == (st.st_mtime, st.st_size)

    def run(self, name, args):
        """对外统一入口(WebBridge 会包装本方法上报工具时间线)"""
        return self._dispatch(name, args)

    def _dispatch(self, name, args):
        """核心分发: hooks + 执行 + base64过滤 + 截断。
        统一对所有工具返回值进行 base64/data-URI 过滤,
        确保图片生成/编辑等工具的 base64 编码不注入 LLM 上下文。"""
        ok, reason = self.hooks.fire("pre_tool_use", name, args)
        if not ok:
            return f"[被 PreToolUse hook 阻止] {reason}"
        if name.startswith("mcp__"):
            result = self.mcp.call(name, args)   # MCP 工具与内置工具同等调用
        else:
            try:
                result = getattr(self, "t_" + name)(args)
            except AttributeError:
                result = f"[错误] 未知工具: {name}"
            except Exception as e:
                result = f"[工具执行异常] {type(e).__name__}: {e}"
        # 统一过滤 base64 / data-URI, 防注入 LLM 上下文
        result = _sanitize_result(str(result))
        self.hooks.fire("post_tool_use", name, {"args": args, "result": result[:2000]})
        # r = str(result)
        # max_out = S.get_int("agent.max_tool_output", MAX_TOOL_OUTPUT)   # 配置链
        # if len(r) > max_out:
        # r = r[:max_out] + f"\n...[输出已截断, 共 {len(r)} 字符]"
        return result

    def _swarm_emit(self, event, **payload):
        """Swarm 结构化事件上报(网页端树状渲染); CLI 模式无回调则静默"""
        if self.swarm_cb:
            try:
                self.swarm_cb(event, payload)
            except Exception as _e:
                log.warning("忽略异常(omni_agent/tool_runner.py:144): %s: %s", type(_e).__name__, _e)
                pass

    # ---- bash(支持 run_in_background) ----
    def t_bash(self, a):
        cmd = a.get("command", "")
        verdict, why = self.perm.check_bash(cmd, workdir=self.wd)
        if verdict == "deny":
            return f"[已拒绝] {why}"
        if verdict == "confirm" and not self.perm.ask("bash", cmd):
            return "[用户拒绝了本次命令执行]"
        if a.get("run_in_background"):
            job_id = "bg_" + uuid.uuid4().hex[:8]
            log_dir = os.path.join(self.wd, SETTINGS_DIR, "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_f = os.path.join(log_dir, f"{job_id}.log")
            lf = open(log_f, "w", encoding="utf-8")
            try:
                popen_kw = dict(shell=True, stdout=lf, stderr=subprocess.STDOUT,
                                cwd=self.wd)
                if os.name == "nt":
                    popen_kw["creationflags"] = 0x00000008  # CREATE_NEW_PROCESS_GROUP (Windows)
                else:
                    popen_kw["start_new_session"] = True    # POSIX: setsid 脱离终端
                proc = subprocess.Popen(cmd, **popen_kw)
            finally:
                lf.close()   # 子进程已继承 fd, 关闭父进程句柄避免泄漏
            self.bg_jobs[job_id] = {"proc": proc, "log": log_f, "cmd": cmd}
            time.sleep(1.0)  # 给早期崩溃留出暴露窗口
            alive = proc.poll() is None
            with open(log_f, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-800:]
            return (f"[后台任务已启动] id={job_id} pid={proc.pid} "
                    f"状态={'运行中' if alive else f'已退出(exit={proc.returncode})'}\n"
                    f"日志: {os.path.relpath(log_f, self.wd)}\n早期输出:\n{tail or '(暂无)'}")
        t = min(int(a.get("timeout", S.get_int("agent.bash_timeout", BASH_TIMEOUT))), 600)
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                               timeout=t, cwd=self.wd, errors="replace")
            out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
            return f"[exit={r.returncode}]\n{out.strip() or '(无输出)'}"
        except subprocess.TimeoutExpired:
            return f"[超时] 命令超过 {t}s 未完成(长驻进程请用 run_in_background=true)"

    # ---- 文件读写(read_file 批量并行 + 版本跟踪) ----
    # read_file 工具能力边界 —— 仅支持文本类型, 二进制类型明确拒绝并导引
    _BINARY_EXT = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff",
        ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".mp4", ".webm",
        ".mov", ".avi", ".mkv", ".zip", ".rar", ".7z", ".gz", ".tar", ".bz2",
        ".xz", ".exe", ".dll", ".so", ".dylib", ".bin", ".pdf", ".docx",
        ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".woff", ".woff2", ".ttf",
        ".otf", ".eot", ".pyc", ".class", ".jar", ".db", ".sqlite", ".iso",
        ".img", ".dmg", ".apk", ".wasm"}

    def _read_one(self, path, start_line=None, end_line=None):
        p = self._abs(path)
        if not os.path.isfile(p):
            return f"[错误] 文件不存在: {path}"
        # 提示词策略优化 —— 本轮对话中已读过且文件未变化(版本一致),
        # 全量重读时提示 LLM 直接使用上下文中的已有内容, 减少重复加载步骤;
        # 防御: 若 LLM 坚持再次重读(可能因上下文压缩丢失原文), 第二次放行真实读取
        if start_line is None and end_line is None \
                and self._check_read_fresh(path) \
                and path not in self._read_skipped:
            self._read_skipped.add(path)
            log.info("read skipped (context fresh): %s", path)
            return (f"[无需重复读取] 文件 {path} 自上次读取后未发生任何变化, "
                    f"当前上下文中的版本即为最新版本。请直接基于已有内容继续任务"
                    f"(如需编辑可直接 edit_file/multi_edit), 无需重复加载。"
                    f"(若上下文中确实无该文件内容, 再次调用 read_file 将正常返回全文)")
        self._read_skipped.discard(path)
        ext = os.path.splitext(p)[1].lower()
        if ext in self._BINARY_EXT:
            return (f"[已拒绝] read_file 仅支持文本类型文件, {path} 为二进制类型"
                    f"({ext})。图片请改用 vision 工具分析; 压缩包请先用 bash 解压;"
                    f" 音视频/办公文档请使用相应命令或工具处理。")
        try:
            with open(p, "rb") as fb:
                if b"\x00" in fb.read(4096):
                    return (f"[已拒绝] 检测到二进制内容(含NUL字节): {path}, "
                            f"read_file 仅支持文本类型文件。")
        except OSError as e:
            return f"[错误] 读取 {path}: {e}"
        with open(p, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        # 记录读取版本(用于编辑前一致性校验)
        self._track_read(path)
        s = max(1, int(start_line or 1))
        e = min(len(lines), int(end_line or len(lines)))
        body = "\n".join(f"{i:>5}│{lines[i - 1]}" for i in range(s, e + 1))
        max_read = S.get_int("agent.max_file_read", MAX_FILE_READ)   # 配置链
        log.info("显示头部数据: %s", body[:max_read])
        return f"文件 {path} (共{len(lines)}行) :\n{body}"

    def t_read_file(self, a):
        paths = a.get("paths") or ([a["path"]] if a.get("path") else [])
        if not paths:
            return "[错误] path/paths 不能为空"
        if len(paths) == 1:   # 单文件保留行号范围能力
            return self._read_one(paths[0])
        # 批量读取改用线程池并行执行
        paths = paths[:10]
        blocks = [None] * len(paths)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(paths))) as ex:
            futs = {ex.submit(self._read_one, p): i for i, p in enumerate(paths)}
            for fut in concurrent.futures.as_completed(futs):
                idx = futs[fut]
                try:
                    blocks[idx] = fut.result()
                except Exception as e:
                    blocks[idx] = f"[错误] 读取 {paths[idx]}: {e}"
        return f"[批量读取 {len(blocks)} 个文件]\n\n" + "\n\n".join(blocks)

    def t_write_file(self, a):
        p = self._abs(a["path"])
        content = a["content"]
        
        auto_cite = (p.endswith((".html", ".htm", ".md", ".markdown"))
                     and bool(re.search(
                         r"\[\[\d+\]\]|\[\^\d+\]|"
                         r"href=[\"\']#(?:fn|footnote|note|ref|cite)|"
                         r"class=[\"\'][^\"\']*footnote|"
                         r"<sup[^>]*>\s*(?:<a[^>]*>\s*)?\[?\d+\]?\s*(?:</a>\s*)?</sup>",
                         content or "")))
        if a.get("with_sources") or auto_cite:
            if p.endswith((".html", ".htm")):
                content = self.sources.decorate_html(content)
            elif p.endswith((".md", ".markdown")):
                content = self.sources.decorate_md(content)
        verdict, why = self.perm.check_file_write(a["path"], content, workdir=self.wd)
        if verdict == "deny":
            return f"[已拒绝] {why}"
        if verdict == "confirm":
            preview = content[:300] + ("..." if len(content) > 300 else "")
            if not self.perm.ask("write_file", f"{why}\n{a['path']}\n{preview}"):
                return "[用户拒绝了写入]"
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        # 写入后更新版本跟踪
        self._track_read(a["path"])
        extra = (" (已渲染引用角标悬浮卡片)"
                 if (a.get("with_sources") or auto_cite) else "")
        return f"[成功] 已写入 {a['path']} ({len(content)} 字符, {content.count(chr(10)) + 1} 行){extra}"

    def t_edit_file(self, a):
        p = self._abs(a["path"])
        if not os.path.isfile(p):
            return f"[错误] 文件不存在: {a['path']}"
        # 编辑前上下文校验 —— 检查是否已读取且版本一致
         
        with open(p, encoding="utf-8", errors="replace") as f:
            text = f.read()
        n = text.count(a["old_string"])
        if n == 0:
            return "[错误] old_string 未在文件中找到, 请先 read_file 核对原文"
        if n > 1:
            return f"[错误] old_string 出现 {n} 次，内容不唯一, 请增加上下文"
        new_text = text.replace(a["old_string"], a["new_string"], 1)
        verdict, why = self.perm.check_file_write(a["path"], new_text, workdir=self.wd)
        if verdict == "deny":
            return f"[已拒绝] {why}"
        if verdict == "confirm":
            if not self.perm.ask("edit_file",
                                 f"{why}\n{a['path']}\n- {a['old_string'][:120]}\n+ {a['new_string'][:120]}"):
                return "[用户拒绝了编辑]"
        with open(p, "w", encoding="utf-8") as f:
            f.write(new_text)
        # 编辑后主动失效版本 —— 同一会话对同一文件的下一次编辑
        # 必须重新 read_file(强制每次编辑前先读取, 防上下文遗忘)
        self._read_versions.pop(a["path"], None)
        return f"[成功] 已编辑 {a['path']} " 

    def t_multi_edit(self, a):
        p = self._abs(a["path"])
        if not os.path.isfile(p):
            return f"[错误] 文件不存在: {a['path']}"
        
        with open(p, encoding="utf-8", errors="replace") as f:
            text = f.read()
        edits = a.get("edits") or []
        if not edits:
            return "[错误] edits 不能为空"
        for i, e in enumerate(edits):
            old_s = e.get("old_string", "")
            new_s = e.get("new_string", "")
            n = text.count(old_s)
            if n == 0:
                return f"[错误] 第{i + 1}处替换: old_string 未在文件中找到"
            if n > 1:
                return f"[错误] 第{i + 1}处替换: old_string 出现 {n} 次不唯一"
            text = text.replace(old_s, new_s, 1)
        verdict, why = self.perm.check_file_write(a["path"], text, workdir=self.wd)
        if verdict == "deny":
            return f"[已拒绝] {why}"
        if verdict == "confirm":
            preview = text[:300] + ("..." if len(text) > 300 else "")
            if not self.perm.ask("multi_edit", f"{why}\n{a['path']} ({len(edits)}处替换)\n预览:\n{preview}"):
                return "[用户拒绝了批量编辑]"
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        # 编辑后主动失效版本, 强制下一次编辑前重新 read_file
        self._read_versions.pop(a["path"], None)
        return (f"[成功] 已批量编辑 {a['path']} ({len(edits)}处替换) "
                f"(提示: 再次编辑本文件前需先 read_file 确认最新内容)")

    # ---- 文件检索 ----
    def t_glob_files(self, a):
        pat = a["pattern"]
        limit = int(a.get("max_results", 50))
        root_dir = (a.get("path") or "").strip()
        base = os.path.realpath(os.path.expanduser(root_dir)) if root_dir else self.wd
        if root_dir and not os.path.isdir(base):
            log.warning("glob_files: root_dir 不存在: %s", root_dir)
            return f"[错误] 扫描目录 不存在或不可访问: {root_dir}"
        log.info("glob_files: pattern=%s base=%s", pat, base)
        pat_norm = pat.replace("\\", "/")
        pats = [pat_norm]
        if pat_norm.startswith("**/"):
            pats.append(pat_norm[3:])
        hits = []
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), base).replace(os.sep, "/")
                if any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(f, p) for p in pats):
                    hits.append(rel if base == self.wd else os.path.join(base, rel))
                    if len(hits) >= limit:
                        return "\n".join(hits) + "\n...[达到上限]"
        if not hits:
            log.info("glob_files: 无匹配 pattern=%s base=%s", pat, base)
            return (f"(无匹配文件: pattern={pat} · 扫描目录={base})\n"
                    "提示: 可调整通配符(如 **/*.py 或 *.py), 或传 root_dir 扫描其它目录")
        return "\n".join(hits)

    def t_grep_search(self, a):
        try:
            rx = re.compile(a["pattern"])
        except re.error as e:
            return f"[错误] 正则无效: {e}"
        gpat = a.get("glob", "**/*")
        limit = int(a.get("max_results", 50))
        hits = []
        for root, dirs, files in os.walk(self.wd):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), self.wd).replace(os.sep, "/")
                if not (fnmatch.fnmatch(rel, gpat) or fnmatch.fnmatch(f, gpat)):
                    continue
                try:
                    with open(os.path.join(root, f), encoding="utf-8", errors="replace") as fp:
                        for i, line in enumerate(fp, 1):
                            if rx.search(line):
                                hits.append(f"{rel}:{i}:{line.rstrip()[:200]}")
                                if len(hits) >= limit:
                                    return "\n".join(hits) + "\n...[达到上限]"
                except (OSError, UnicodeError) as _e:
                    log.warning("忽略异常(omni_agent/tool_runner.py:364): %s: %s", type(_e).__name__, _e)
                    continue
        return "\n".join(hits) or "(无匹配内容)"

    # ---- todo ----
    def t_todo_write(self, a):
        self.todos = a.get("todos", [])
        icons = {"pending": "○", "in_progress": "◐", "completed": "●"}
        colors = {"pending": C.GRAY, "in_progress": C.CYAN, "completed": C.GREEN}
        cprint("  ┌─ 任务清单 ──────────", C.MAGENTA)
        for t in self.todos:
            st = t.get("status", "pending")
            cprint(f"  │ {icons.get(st, '○')} {t.get('content', '')}", colors.get(st, C.GRAY))
        cprint("  └────────────────────", C.MAGENTA)
        done = sum(1 for t in self.todos if t.get("status") == "completed")
        return f"[任务清单已更新] {done}/{len(self.todos)} 已完成"

    def todo_progress(self):
        if not self.todos:
            return {"total": 0, "completed": 0, "items": []}
        return {"total": len(self.todos),
                "completed": sum(1 for t in self.todos if t.get("status") == "completed"),
                "items": [{"content": t.get("content", ""), "status": t.get("status", "")}
                          for t in self.todos]}

    # ---- ask_user_question ----
    @staticmethod
    def _input_with_countdown(prompt, timeout):
        last = {"n": -1}

        def _tick(remain):
            if remain != last["n"] and (remain <= 30 or remain % 10 == 0):
                last["n"] = remain
                print(f"\r{C.GRAY}  (剩余 {remain:>3}s, 超时自动选择第一项) "
                      f"{C.R}{C.GREEN}> {C.R}", end="", flush=True)
        cprint(prompt, C.GREEN, end="")
        return timed_input("", timeout, tick_cb=_tick)

    def t_ask_user_question(self, a):
        questions = a.get("questions")
        if not questions and a.get("question"):
            questions = [{"question": a["question"], "options": a.get("options") or []}]
        if not questions:
            return "[错误] questions 不能为空"
        timeout = min(int(a.get("timeout", S.get_int("agent.ask_default_timeout",
                       ASK_DEFAULT_TIMEOUT))), 600)
        notify = bool(a.get("notify_only"))

        cprint("\n  ┌─ 来自智能体的" + ("通知" if notify else "提问") + " ─────────", C.MAGENTA)
        for qi, q in enumerate(questions, 1):
            q_lines = str(q.get("question", "")).splitlines()[:10]
            for li, line in enumerate(q_lines):
                prefix = f"  │ Q{qi}. " if li == 0 else "  │     "
                cprint(prefix + line, C.MAGENTA)
            for oi, o in enumerate(q.get("options") or [], 1):
                mark = " (默认)" if oi == 1 else ""
                cprint(f"  │     {oi}. {o}{mark}", C.CYAN)
        cprint("  └──────────────────────────", C.MAGENTA)
        if notify:
            return "[通知已送达用户]"

        answers = []
        for qi, q in enumerate(questions, 1):
            opts = q.get("options") or []
            default = opts[0] if opts else "(按最合理假设继续, 并在交付说明中标注)"
            if self.perm.headless or self.perm.scheduled:
                answers.append(f"Q{qi}: [无人值守自动确认] {default}")
                continue
            ans = self._input_with_countdown(
                f"  Q{qi} 你的回答(数字选项或文字, {timeout}s 内确认): ", timeout)
            if ans is None:
                print()
                cprint(f"  ⏲ Q{qi} 超时未确认, 已自动选择第一项: {default}", C.YELLOW)
                answers.append(f"Q{qi}: [超时自动选择] {default}")
                continue
            if opts and ans.isdigit() and 1 <= int(ans) <= len(opts):
                ans = opts[int(ans) - 1]
            answers.append(f"Q{qi}: {ans or default}")
        return "[用户回答]\n" + "\n".join(answers)

    # ---- send_user_msg(部分文件缺失不整体失败) ----
    def t_send_user_msg(self, a):
        # 指纹加入文件内容特征(mtime+size) —— 修复多轮对话中文件已
        # 更新但同路径同标题的新版交付被误判"重复交付"拒绝, 导致用户要求
        # 的多个交付物只出现部分卡片的问题; 文件未变化时仍严格防重复
        def _sig(x):
            try:
                st = os.stat(self._abs(x.get("path", "")))
                return f"{x.get('path')}@{int(st.st_mtime)}:{st.st_size}"
            except OSError:
                return str(x.get("path"))
        fp = json.dumps({"t": a.get("title"), "u": a.get("preview_url"),
                         "f": [_sig(x) for x in a.get("files") or []]},
                        ensure_ascii=False, sort_keys=True)
        if fp in self.delivered:
            return ("[已拒绝] 该产物已交付过且文件内容无变化, 严禁重复交付"
                    "(若已更新文件内容, 重新交付会自动放行)")
        self.delivered.add(fp)
        title = a.get("title", "")
        if self.session.get("name_zh"):
            title = f"【{self.session['name_zh']}】{title}"
        cprint("\n  ╔═ 交付物卡片 ═══════════════════════════╗", C.GREEN)
        cprint(f"  ║  {title}", C.GREEN)
        # 交付卡片添加完成时间
        finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
        cprint(f"  ║ 🕒 完成时间: {finished_at}", C.GRAY)
        if a.get("preview_url"):
            cprint(f"  ║ 🔗 预览: {a['preview_url']}", C.CYAN)
        missing = []
        present = []
        for f in a.get("files") or []:
            p = self._abs(f.get("path", ""))
            if os.path.isfile(p):
                size = os.path.getsize(p)
                cprint(f"  ║ 📄 {f.get('name')}  ({f.get('file_type', 'file')}, {size} B)"
                       f"  → {f.get('path')}", C.R)
                present.append(f)
            elif os.path.isdir(p):
                # 目录类交付物(如 Mac 原生应用 .app / 构建产物目录)同样有效
                cprint(f"  ║ 📂 {f.get('name')}  (目录交付物)  → {f.get('path')}", C.R)
                present.append(f)
            else:
                missing.append(f.get("path"))
                cprint(f"  ║  文件不存在: {f.get('path')}", C.RED)
        for s in (a.get("suggestions") or [])[:3]:
            cprint(f"  ║  {s}", C.GRAY)
        cprint("  ╚════════════════════════════════════════╝", C.GREEN)
        # 即使部分文件缺失, 只要存在有效交付物(preview_url或至少1个文件)
        # 就视为交付成功, 缺失文件在返回值中标注但不阻断
        has_preview = bool(a.get("preview_url"))
        if missing and not has_preview and not present:
            return f"[失败] 所有文件均不存在, 禁止虚报交付: {missing}"
        if missing:
            return (f"[部分成功] 交付物卡片已推送(完成时间: {finished_at}), "
                    f"但以下文件不存在: {missing}。")
        # 交付即自动快照固话 —— 创建快照并物化到独立目录, 使部署/预览/
        # 变更/下载均指向该快照目录内的文件, 后续新交付物不会覆盖旧快照。
        # 快照目录位于 .haisnap/snapshots/<cid>, 与工作区隔离, webserver 登记时接管。
        try:
            cid, _reused = self.ckpt.create(
                note="交付自动快照: " + (title or "")[:60],
                messages=getattr(self, "messages", None))
            snap_dir = os.path.join(self.wd, SETTINGS_DIR, "snapshots", cid)
            _missing_cnt, mat_dir = self.ckpt.materialize(cid, snap_dir)
            self.delivery_snapshot = {"cid": cid, "dir": mat_dir}
            log.info("snapshot auto-created for delivery: cid=%s", cid)
        except Exception as _e:
            log.warning("忽略异常(omni_agent/tool_runner.py:send_msg): "
                        "自动快照失败: %s: %s", type(_e).__name__, _e)
            self.delivery_snapshot = None
        return (f"[成功] 交付物卡片已推送(完成时间: {finished_at}), "
                f"已自动固化快照: "
                f"{self.delivery_snapshot['cid'] if self.delivery_snapshot else '无'}。")

    # ---- web_search(支持多查询词并行) ----
    def _search_one(self, q, limit):
        if not UniFuncs.enabled():
            raise RuntimeError("UniFuncs 搜索通道未启用: 请配置 HAISNAP_UNIFUNCS_KEY "
                               "环境变量(或检查 HAISNAP_UNIFUNCS 开关)")
        items = []
        for title, href, snippet, siteName, siteIcon in UniFuncs.search(q, limit):
            date = SourceRegistry.extract_date(snippet)
            # 补传 snippet(此前漏传导致悬浮卡片摘要恒为空); 剥离 "(YYYY-MM-DD) " 前缀
            clean_snip = re.sub(r"^\(20\d{2}-\d{2}-\d{2}\)\s*", "", snippet).strip()
            sid = self.sources.add(title, href, date=date, snippet=clean_snip,
                                   siteName=siteName, siteIcon=siteIcon)
            items.append(f"  [[{sid}]] {title}\n      {href}\n      {snippet}")
        items.append("  (搜索源: UniFuncs 聚合搜索)")
        return items

    def t_web_search(self, a):
        queries = a.get("queries") or []
        if not queries:
            return "[错误] queries 不能为空"
        limit = int(a.get("max_results", 5))
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(queries))) as ex:
            futs = {ex.submit(self._search_one, q, limit): q for q in queries}
            for fut in concurrent.futures.as_completed(futs):
                q = futs[fut]
                try:
                    items = fut.result()
                    results[q] = f"◈ 查询: {q}\n" + ("\n".join(items) or "  (无结果)")
                except Exception as e:
                    results[q] = f"◈ 查询: {q}\n  [失败] UniFuncs: {e}"
        blocks = [results[q] for q in queries if q in results]
        return ("\n\n".join(blocks) +
                "\n\n提示: 报告正文中引用数据时使用 [[信源ID]] 角标 + ==关键数据高亮== 语法, "
                "并对报告文件 write_file 时设置 with_sources=true。")

    # ---- web_fetch(POST 方式请求 UniFuncs 阅读器, 支持批量并行) ----
    def _fetch_one(self, url, prompt, timeout):
        if re.search(r"\.(png|jpe?g|gif|webp|bmp)(\?|$)", url, re.I):
            return self.t_vision({"image": url, "prompt": prompt or "描述这张图片的内容",
                                  "mode": "understand"})
        if not UniFuncs.enabled():
            return ("[失败] UniFuncs 阅读器通道未启用: 请配置 HAISNAP_UNIFUNCS_KEY "
                    "环境变量(或检查 HAISNAP_UNIFUNCS 开关)")
        try:
            # UniFuncs.fetch 内部已改为 POST 首选 + GET 降级
            text = UniFuncs.fetch(url, timeout)
        except Exception as e:
            return f"[失败] 抓取 {url}: UniFuncs阅读器(POST): {e}(超时阈值 {timeout}s)"
        lines = [ln for ln in text.strip().splitlines() if ln.strip()]
        title = (lines[0].lstrip("# ").strip()[:120] if lines else "") or url
        date = SourceRegistry.extract_date(text[:3000])
        # 从正文首段提炼摘要, 供引用悬浮卡片展示
        body_lines = [ln.strip() for ln in lines[1:] if len(ln.strip()) > 20]
        summary = (body_lines[0] if body_lines else "")[:200]
        sid = self.sources.add(title, url, date=date, snippet=summary)
        focus = f"\n(关注点: {prompt})" if prompt else ""
        return (f"◈ {title} [[{sid}]]{focus}\nURL: {url}  \n\n{text}")

    def t_web_fetch(self, a):
        urls = a.get("urls") or ([a["url"]] if a.get("url") else [])
        if not urls:
            return "[错误] url/urls 不能为空"
        try:
            timeout = int(a.get("timeout") or S.get_int("agent.web_fetch_timeout",
                       WEB_FETCH_TIMEOUT))
        except (TypeError, ValueError):
            timeout = S.get_int("agent.web_fetch_timeout", WEB_FETCH_TIMEOUT)
        timeout = max(5, min(timeout, 120))
        prompt = a.get("prompt")
        if len(urls) == 1:
            return self._fetch_one(urls[0], prompt, timeout)
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(5, len(urls))) as ex:
            futs = {ex.submit(self._fetch_one, u, prompt, timeout): u for u in urls[:10]}
            for fut in concurrent.futures.as_completed(futs):
                u = futs[fut]
                try:
                    results[u] = fut.result()
                except Exception as e:
                    results[u] = f"[失败] 抓取 {u}: {e}"
        blocks = [results[u] for u in urls if u in results]
        return f"[批量抓取 {len(blocks)} 个URL]\n\n" + "\n\n━━━━━━━━\n\n".join(blocks)

    # ---- web_screenshot ----
    def t_web_screenshot(self, a):
        url = a.get("url", "")
        if not re.match(r"^https?://", url):
            return f"[错误] 无效 URL: {url}"
        actions = a.get("actions") or []
        shot_dir = os.path.join(self.wd, SETTINGS_DIR, "shots")
        os.makedirs(shot_dir, exist_ok=True)
        path = self._abs(a.get("save_path") or os.path.join(
            shot_dir, f"shot_{time.strftime('%H%M%S')}_{uuid.uuid4().hex[:4]}.png"))
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        logs = []
        try:
            browser = BrowserController()
            try:
                browser.goto(url)
                logs.append(f"打开页面: {url} ✓")
                for act in actions[:20]:
                    logs.append(browser.act(act))
                size = browser.screenshot(path, full_page=bool(a.get("full_page")))
                logs.append(f"截图完成: {os.path.relpath(path, self.wd)} ({size} B, "
                            f"{'整页' if a.get('full_page') else '视口'})")
            finally:
                browser.close()
        except Exception as e:
            binpath = BrowserController.find_browser()
            if not binpath:
                return f"[失败] 未找到 Chrome/Chromium, 无法截图: {e}"
            note = "(CDP 不可用, 已降级普通截图, 交互动作未执行)" if actions else "(降级普通截图)"
            try:
                subprocess.run([binpath, "--headless=new", "--disable-gpu", "--no-sandbox",
                                f"--screenshot={path}", "--window-size=1280,900", url],
                               capture_output=True, timeout=60)
            except subprocess.TimeoutExpired:
                return f"[超时] 降级截图超过 60s: {url}"
            if not os.path.isfile(path):
                return f"[失败] 截图未生成: {e}"
            logs.append(f"降级截图完成 {note}: {os.path.relpath(path, self.wd)}")
        # sid = self.sources.add(f"网页截图: {url}", url, date=time.strftime("%Y-%m-%d"))
        out = "\n\n".join(logs)
        if a.get("analyze"):
            out += "\n\n" + self.t_vision({"image": path, "prompt": a["analyze"],
                                           "mode": "understand"})
        return out

    # ---- deploy ----
    def t_deploy(self, a):
        if a.get("restart") and self.deployment:
            self.deployment["proc"].terminate()
            time.sleep(0.5)
            old = self.deployment
            self.deployment = None
            a = {**a, "command": old["command"], "port": old["port"], "restart": False} \
                if old["command"] else \
                {**a, "dir": old.get("dir", "."), "port": old["port"], "restart": False}
        if self.deployment and self.deployment["proc"].poll() is None:
            # 已运行时也计算 url_path 并返回完整预览路径
            url_path = (a.get("url_path") or "").strip("/")
            if not url_path:
                url_path = "index.html"
            full_url = self.deployment['url'].rstrip("/") + "/" + url_path
            return f"[提示] 服务已在运行: {full_url}(如需更新请 restart=true)"
        port = int(a.get("port") or 0)
        if not port:
            with socket.socket() as s:
                s.bind(("", 0))
                port = s.getsockname()[1]
        log_dir = os.path.join(self.wd, SETTINGS_DIR, "logs")
        os.makedirs(log_dir, exist_ok=True)
        lf = open(os.path.join(log_dir, "deploy.log"), "w", encoding="utf-8")
        try:
            if a.get("command"):
                cmd = a["command"].replace("${PORT}", str(port))
                popen_kw3 = dict(shell=True, stdout=lf, stderr=subprocess.STDOUT,
                                cwd=self.wd, env={**os.environ, "PORT": str(port)})
                if os.name == "nt":
                    popen_kw3["creationflags"] = 0x00000008
                else:
                    popen_kw3["start_new_session"] = True
                proc = subprocess.Popen(cmd, **popen_kw3)
                kind = f"命令 {cmd}"
            else:
                # 部署读取快照目录 —— 若最近一次交付已自动固化快照, 静态
                # 目录部署指向该快照目录内的文件, 保证预览/变更对应快照版本, 新
                # 交付物不会覆盖已部署内容。
                base = self.wd
                if self.delivery_snapshot and os.path.isdir(self.delivery_snapshot["dir"]):
                    base = self.delivery_snapshot["dir"]
                rel = a.get("dir", ".") or "."
                serve_dir = base if rel == "." else os.path.join(base, rel)
                if not os.path.isdir(serve_dir):
                    serve_dir = self._abs(rel)
                popen_kw2 = dict(stdout=lf, stderr=subprocess.STDOUT)
                if os.name == "nt":
                    popen_kw2["creationflags"] = 0x00000008
                else:
                    popen_kw2["start_new_session"] = True
                proc = subprocess.Popen([sys.executable, "-m", "http.server", str(port),
                                         "--bind", "0.0.0.0", "-d", serve_dir],
                                        **popen_kw2)
                kind = f"静态目录 {a.get('dir', '.')}"
        finally:
            lf.close()
        time.sleep(1.2)
        if proc.poll() is not None:
            return f"[失败] 服务启动即退出(exit={proc.returncode}), 查看 {SETTINGS_DIR}/logs/deploy.log"
        url = f"http://localhost:{port}"
        self.deployment = {"proc": proc, "port": port, "url": url,
                           "command": a.get("command"), "dir": a.get("dir", ".")}
        # url_path 参数 —— 为空指向 index.html, 有值返回完整预览路径
        url_path = (a.get("url_path") or "").strip("/")
        if not url_path:
            url_path = "index.html"
        full_url = url.rstrip("/") + "/" + url_path
        proj = (f" · 项目: {self.session.get('name_zh', '')}({self.session.get('name_en', '')})"
                if self.session.get("name_zh") else "")
        return (f"[成功] 已部署({kind}){proj}\n可访问 URL: {full_url}\n"
                f"重启方式: deploy(restart=true)")

    # ---- load_skills(批量 load/unload 改线程池并行) ----
    def t_load_skills(self, a):
        act = a.get("action")
        if act == "list":
            sk = self.skills.scan()
            return "\n".join(f"  • {s['name']}: {s['brief'][:80]}..." for s in sk) or "(暂无已安装技能)"
        if act == "install":
            return self.skills.install(a.get("source", ""))
        if act == "load":
            names = a.get("names") or ([a["name"]] if a.get("name") else [])
            if not names:
                return "[错误] name/names 不能为空"
            names = names[:5]
            # 批量加载改线程池并行
            if len(names) == 1:
                return self.skills.load(names[0])
            results = [None] * len(names)
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(names))) as ex:
                futs = {ex.submit(self.skills.load, n): i for i, n in enumerate(names)}
                for fut in concurrent.futures.as_completed(futs):
                    idx = futs[fut]
                    try:
                        results[idx] = fut.result()
                    except Exception as e:
                        results[idx] = f"[错误] 加载 {names[idx]}: {e}"
            return "\n\n".join(results)
        if act == "unload":
            names = a.get("names") or ([a["name"]] if a.get("name") else [])
            if not names:
                return "[错误] name/names 不能为空"
            names = names[:5]
            # 批量卸载改线程池并行
            if len(names) == 1:
                return self.skills.unload(names[0])
            results = [None] * len(names)
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(names))) as ex:
                futs = {ex.submit(self.skills.unload, n): i for i, n in enumerate(names)}
                for fut in concurrent.futures.as_completed(futs):
                    idx = futs[fut]
                    try:
                        results[idx] = fut.result()
                    except Exception as e:
                        results[idx] = f"[错误] 卸载 {names[idx]}: {e}"
            return "\n".join(results)
        if act == "match":
            matched = self.skills.match(a.get("intent", ""))
            if not matched:
                return "(无语义匹配的技能, 按通用流程执行)"
            return self.skills.load(matched[0]["name"]) + \
                (f"\n(其他候选: {[m['name'] for m in matched[1:3]]})" if len(matched) > 1 else "")
        return f"[错误] 未知 action: {act}"

    # ---- swarm_tasks ----
    def _swarm_one(self, idx, task, shared, allow_tools, sid):
        sub_tools = None
        if allow_tools:
            ro = {"web_search", "web_fetch", "web_screenshot", "read_file",
                  "glob_files", "grep_search"}
            sub_tools = [t for t in TOOLS_SCHEMA if t["function"]["name"] in ro]
        msgs = [{"role": "system", "content":
                 "你是并行子任务执行器, 独立完成指定任务后输出简洁的结构化结论。只关注自己的任务。"},
                {"role": "user", "content": (f"共享背景:\n{shared}\n\n" if shared else "")
                 + f"子任务: {task}"}]
        # 子任务工具白名单集合(实际执行前强制校验, 防模型调用白名单外工具;
        # hooks 拦截: 子任务工具统一走 _dispatch → pre/post_tool_use hooks 全覆盖)
        allowed_names = {t["function"]["name"] for t in (sub_tools or [])}
        cprint(f"   子任务:\n {task}", C.CYAN)
        self._swarm_emit("swarm_task_start", sid=sid, idx=idx, task=task)
        for _ in range(6):
            r = self.llm.chat(msgs, tools=sub_tools, stream=False, internal=True)
            msgs.append({"role": "assistant", "content": r["content"] or "",
                         **({"tool_calls": r["tool_calls"]} if r["tool_calls"] else {})})
            if not r["tool_calls"]:
                self._swarm_emit("swarm_task_done", sid=sid, idx=idx,
                                 output=(r["content"] or "")[:2000])
                return idx, task, r["content"]
            for tc in r["tool_calls"]:
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                name = tc["function"]["name"]
                brief = str(args.get("command") or args.get("path")
                            or args.get("pattern") or args.get("url")
                            or (args.get("queries") or [""])[0]
                            or (args.get("urls") or [""])[0]
                            or (args.get("paths") or [""])[0] or "")[:120]
                t0 = time.time()
                if name not in allowed_names:
                    # 白名单外工具直接拒绝(子任务仅允许只读工具集合)
                    out = (f"[已拒绝] 子任务仅允许只读工具"
                           f"{sorted(allowed_names) if allowed_names else '(无)'}, "
                           f"工具 {name} 不在允许范围")
                    log.warning("swarm subtask blocked tool: %s (allowed=%s)",
                                name, sorted(allowed_names))
                else:
                    out = self._dispatch(name, args)
                self._swarm_emit("swarm_tool", sid=sid, idx=idx, tool=name,
                                 brief=brief, dt=round(time.time() - t0, 1),
                                 result=str(out)[:1200])
                msgs.append({"role": "tool", "tool_call_id": tc["id"],
                             "content": str(out)[:6000]})
        partial = "[子任务超出最大轮数, 以下为部分结论]\n" + (msgs[-1].get("content") or "")
        self._swarm_emit("swarm_task_done", sid=sid, idx=idx, output=partial[:2000],
                         partial=True)
        return idx, task, partial

    def t_swarm_tasks(self, a):
        tasks = a.get("tasks") or []
        if not tasks:
            return "[错误] tasks 不能为空"
        shared = a.get("shared_context", "")
        allow_tools = a.get("allow_tools", True)
        workers = max(1, min(int(a.get("max_workers", 5)), 10, len(tasks)))
        sid = "sw_" + uuid.uuid4().hex[:8]
        cprint(f"  ⚡ Swarm: {len(tasks)} 个子任务并行执行(并发={workers})...", C.MAGENTA)
        self._swarm_emit("swarm_start", sid=sid, tasks=tasks, workers=workers)
        results = [None] * len(tasks)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(self._swarm_one, i, t, shared, allow_tools, sid): i
                    for i, t in enumerate(tasks)}
            for fut in concurrent.futures.as_completed(futs):
                i = futs[fut]
                try:
                    _, task, out = fut.result()
                    results[i] = f"═══ 子任务{i + 1}: {task[:60]} ═══\n{out}"
                    cprint(f"  ⚡ 子任务{i + 1} 完成", C.GRAY)
                except Exception as e:
                    results[i] = f"[子任务{i + 1}失败] {e}"
                    self._swarm_emit("swarm_task_done", sid=sid, idx=i,
                                     output=f"[失败] {e}", failed=True)
        self._swarm_emit("swarm_end", sid=sid)
        return "[Swarm 全部完成, 合并结果如下]\n\n" + "\n\n".join(r or "(空)" for r in results)

    # ---- vision(支持批量 images 并行分析) ----
    def _vision_one(self, image, prompt, mode):
        if mode == "replicate":
            prompt = ("请生成原生 HTML(含内联样式)尽可能像素级复刻图中界面, 只输出完整 HTML 代码。"
                      + "\n补充要求: " + prompt)
        if re.match(r"^https?://", image):
            img_content = {"type": "image_url", "image_url": {"url": image}}
        else:
            p = self._abs(image)
            if not os.path.isfile(p):
                return f"[错误] 图片不存在: {image}"
            ext = os.path.splitext(p)[1].lstrip(".").lower() or "png"
            # MIME 规范化, jpg->jpeg 等, 避免严格网关对非标 MIME 报 400
            ext = {"jpg": "jpeg", "jfif": "jpeg", "tif": "tiff",
                   "svg": "svg+xml"}.get(ext, ext)
            with open(p, "rb") as f:
                data = base64.b64encode(f.read()).decode()  # 仅传输编码
            img_content = {"type": "image_url",
                           "image_url": {"url": f"data:image/{ext};base64,{data}"}}
        vmdl = S.get_str("model.vision_model") or VISION_MODEL   # 配置链
        msgs = [{"role": "user", "content": [
            img_content, {"type": "text", "text": prompt}]}]
        try:
            r = self.llm.chat(msgs, stream=False, model=vmdl,
                              internal=True, vision=True)
            return f"[视觉分析结果 · {mode}]\n{r['content']}"
        except Exception as e:
            # 配置的视觉模型不支持图片输入(HTTP 400, 如误配纯文本模型)时,
            # 自动回退默认视觉模型 qwen-vl-max 重试一次
            emsg = str(e)
            if "400" in emsg and vmdl != "qwen-vl-max":
                log.warning("视觉模型 %s 调用 400, 自动回退 qwen-vl-max: %s",
                            vmdl, emsg[:200])
                try:
                    r = self.llm.chat(msgs, stream=False, model="qwen-vl-max",
                                      internal=True, vision=True)
                    return (f"[视觉分析结果 · {mode}] (模型 {vmdl} 不可用, "
                            f"已自动回退 qwen-vl-max)\n{r['content']}")
                except Exception as e2:
                    log.warning("回退模型仍失败(omni_agent/tool_runner.py): %s: %s",
                                type(e2).__name__, e2)
                    return f"[失败] 视觉模型调用异常(含回退): {e2}"
            return f"[失败] 视觉模型调用异常: {e}"

    def t_vision(self, a):
        images = a.get("images") or ([a["image"]] if a.get("image") else [])
        if not images:
            return "[错误] image/images 不能为空"
        mode = a.get("mode", "understand")
        prompt = a.get("prompt", "")
        if len(images) == 1:
            return self._vision_one(images[0], prompt, mode)
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(3, len(images))) as ex:
            futs = {ex.submit(self._vision_one, img, prompt, mode): img
                    for img in images[:5]}
            for fut in concurrent.futures.as_completed(futs):
                img = futs[fut]
                try:
                    results[img] = fut.result()
                except Exception as e:
                    results[img] = f"[失败] 视觉分析 {img}: {e}"
        blocks = [f"◈ 图片{i + 1}: {img}\n{results[img]}"
                  for i, img in enumerate(images) if img in results]
        return f"[批量视觉分析 {len(blocks)} 张图片]\n\n" + "\n\n".join(blocks)

    # ---- checkpoint ----
    def t_checkpoint(self, a):
        act = a.get("action")
        if act == "create":
            cid, reused = self.ckpt.create(note=a.get("note", "自动快照 - by agent"))
            return (f"[成功] 当前状态与快照 {cid} 一致, 已复用(零冗余)" if reused
                    else f"[成功] 已创建快照 {cid}")
        if act == "list":
            items = self.ckpt.list()
            if not items:
                return "(暂无快照)"
            return "\n".join(f"  {m['id']}  {m['created']}  "
                             f"{'[自动]' if m.get('auto') else '[手动]'} "
                             f"files={m.get('files', '?')} {m['note']}" for m in items)
        if act == "rollback":
            ctx, msg = self.ckpt.rollback(a.get("id", ""), a.get("restore_context", False))
            if ctx:
                self.restored_context = ctx
                msg += " (会话上下文已同步恢复)"
            return msg
        return f"[错误] 未知 action: {act}"
 

    # ---- connector_push ----
    def t_connector_push(self, a):
        return self.connectors.push(a.get("channel", ""), a.get("title", ""),
                                    a.get("content", ""))
 
    def t_schedule_task(self, a):
        """LLM 可直接创建/列出/启停/删除定时任务(写入全局 schedule.json,
        由 haisnap schedule run 调度器执行); 支持 every/at/cron 三种定时方式"""
        from .config import SCHEDULE_FILE_DEFAULT
        from .scheduler import cron_match, scheduler_online
        act = a.get("action")
        path = SCHEDULE_FILE_DEFAULT
        def _load():
            if os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError):
                    return {"jobs": []}
            return {"jobs": []}
        def _save(data):
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        data = _load()
        jobs = data.setdefault("jobs", [])
        if act == "list":
            online, hb = scheduler_online()
            head = f"调度器: {' 在线' if online else '⚪ 离线(启动: haisnap schedule run)'}"
            if not jobs:
                return head + "\n(暂无定时任务)"
            lines = [head]
            for j in jobs:
                timer = (j.get("every") and f"every {j['every']}s") \
                    or (j.get("at") and f"daily {j['at']}") \
                    or (j.get("cron") and f"cron '{j['cron']}'") or "?"
                lines.append(f"  • {j['id']} [{timer}] "
                             f"{'✓启用' if j.get('enabled', True) else '✕停用'} "
                             f"{j.get('name', '')}: {str(j.get('prompt', ''))[:60]}")
            return "\n".join(lines)
        if act == "add":
            prompt = (a.get("prompt") or "").strip()
            if not prompt:
                return "[错误] prompt 不能为空"
            timers = [k for k in ("every", "at", "cron") if a.get(k)]
            if len(timers) != 1:
                return "[错误] every(秒)/at(HH:MM)/cron(五段式) 必须且只能提供一个"
            if a.get("every") is not None:
                try:
                    if int(a["every"]) < 5:
                        return "[错误] every 间隔不得小于 5 秒"
                except (TypeError, ValueError):
                    return "[错误] every 必须是整数秒"
            if a.get("at"):
                try:
                    h, m = str(a["at"]).split(":")
                    assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
                except (ValueError, AssertionError):
                    return "[错误] at 必须是 HH:MM 格式"
            if a.get("cron"):
                try:
                    cron_match(str(a["cron"]))
                except (ValueError, TypeError) as e:
                    return f"[错误] cron 表达式无效: {e}"
            jid = re.sub(r"[^a-zA-Z0-9_]+", "_", a.get("id") or "") \
                or "job_" + uuid.uuid4().hex[:6]
            if any(j.get("id") == jid for j in jobs):
                return f"[错误] 任务 id 已存在: {jid}"
            job = {"id": jid, "name": (a.get("name") or jid)[:40],
                   "enabled": True, "prompt": prompt[:500],
                   "workdir": self.wd,
                   "max_runs": int(a.get("max_runs") or 0)}
            job[timers[0]] = a[timers[0]]
            jobs.append(job)
            _save(data)
            online, _ = scheduler_online()
            tip = "" if online else "\n 调度器当前离线, 需运行 'haisnap schedule run' 后生效"
            return f"[成功] 定时任务已创建: {jid}({timers[0]}={a[timers[0]]}){tip}"
        if act in ("remove", "enable", "disable"):
            jid = a.get("id") or ""
            job = next((j for j in jobs if j.get("id") == jid), None)
            if not job:
                return f"[错误] 任务不存在: {jid}(可用 action=list 查看)"
            if act == "remove":
                data["jobs"] = [j for j in jobs if j.get("id") != jid]
                _save(data)
                return f"[成功] 定时任务已删除: {jid}"
            job["enabled"] = (act == "enable")
            _save(data)
            return f"[成功] 任务 {jid} 已{'启用' if act == 'enable' else '停用'}"
        return f"[错误] 未知 action: {act}(支持 add/list/remove/enable/disable)"

    def shutdown(self):
        """清理后台进程(部署服务/后台任务): terminate 后限时等待回收, 避免僵尸进程。"""
        procs = []
        if self.deployment and self.deployment["proc"].poll() is None:
            procs.append(self.deployment["proc"])
        procs += [j["proc"] for j in self.bg_jobs.values() if j["proc"].poll() is None]
        for proc in procs:
            proc.terminate()
        for proc in procs:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()