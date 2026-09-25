
from .logger import get_logger
log = get_logger("scheduler")
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid

from .config import (SCHEDULE_FILE_DEFAULT, SCHEDULER_DIR, PROJECTS_DIR,
                     SCHEDULER_HEARTBEAT_SEC, SCHEDULER_STALE_SEC)
from .ui import C, cprint

TASKS_DIR = os.path.join(SCHEDULER_DIR, "tasks")
HEARTBEAT_FILE = os.path.join(SCHEDULER_DIR, "heartbeat.json")
FINISHED_KEEP = 50   # 已结束任务状态文件最多保留数量


# ============================== 任务状态注册表 ==============================
class TaskRegistry:
    """任务状态以 JSON 文件持久化(跨进程可见): tasks/<task_id>.json
    结构: {task_id, job_id, name, prompt, pid, status, started, updated,
           progress:{turn,max_turns,tool,todos}, workdir, exit_code, rounds}
    status ∈ running | done | failed | killed"""

    _lock = threading.Lock()   # 任务状态文件并发读写锁(类级, 跨实例共享)

    def __init__(self):
        os.makedirs(TASKS_DIR, exist_ok=True)

    @staticmethod
    def _path(task_id):
        return os.path.join(TASKS_DIR, f"{task_id}.json")

    def write(self, meta):
        meta["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp = self._path(meta["task_id"]) + ".tmp"
        with self._lock:   # 并发锁 —— 防调度器/CLI/进度回调同时写坏状态文件
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path(meta["task_id"]))

    def read(self, task_id):
        p = self._path(task_id)
        if not os.path.isfile(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    _update_lock = threading.Lock()   # 读-改-写复合操作原子化

    def update(self, task_id, **fields):
        with self._update_lock:   # 防并发 update 丢失更新(读改写竞态)
            meta = self.read(task_id)
            if not meta:
                return None
            # progress 字段做浅合并, 其余直接覆盖
            prog = fields.pop("progress", None)
            if prog:
                meta.setdefault("progress", {}).update(prog)
            meta.update(fields)
            self.write(meta)
            return meta

    def list(self, status=None):
        out = []
        if not os.path.isdir(TASKS_DIR):
            return out
        for fn in sorted(os.listdir(TASKS_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(TASKS_DIR, fn), encoding="utf-8") as f:
                    meta = json.load(f)
            except (json.JSONDecodeError, OSError) as _e:
                log.warning("忽略异常(omni_agent/scheduler.py:99): %s: %s", type(_e).__name__, _e)
                continue
            if status and meta.get("status") != status:
                continue
            out.append(meta)
        return sorted(out, key=lambda m: m.get("started", ""), reverse=True)

    def refresh_liveness(self, meta):
        """校验 running 任务的进程是否真的存活(调度器崩溃后状态自愈)"""
        if meta.get("status") != "running":
            return meta
        pid = meta.get("pid")
        if pid and not _pid_alive(pid):
            meta["status"] = "failed"
            meta.setdefault("progress", {})["note"] = "进程已不存在(可能被系统终止)"
            self.write(meta)
        return meta

    def prune(self):
        """清理过多的已结束任务状态文件"""
        finished = [m for m in self.list() if m.get("status") != "running"]
        for meta in finished[FINISHED_KEEP:]:
            try:
                os.remove(self._path(meta["task_id"]))
            except OSError as _e:
                log.warning("忽略异常(omni_agent/scheduler.py:124): %s: %s", type(_e).__name__, _e)
                pass


def _pid_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True   # 进程存在但无权限发信号
    except (ProcessLookupError, OSError):
        return False


# ============================== cron 表达式(五段式标准实现) ==============================
def _parse_cron_field(field, lo, hi, dow=False):
    """解析单个 cron 字段 → 允许值集合。
    v4.8 标准 cron 语法完整支持:
    - 通配 *              → 全范围
    - 列表 1,3,5          → 离散值集合
    - 范围 1-5            → 连续区间
    - 步进 */5、1-30/5    → 步进基点为「范围起点」(修复原实现基于字段下界
      导致 5-59/15 错算为 15,30,45 而非标准的 5,20,35,50)
    - 组合 1,10-20/2,30   → 逗号分隔任意组合
    - 周字段 7 == 0(周日) → dow=True 时自动归一
    - 越界/非法值抛 ValueError(供配置校验阶段提前发现)"""
    vals = set()
    for part in str(field).split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"cron 字段含空列表项: {field!r}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                raise ValueError(f"cron 步进值非法: {step_s!r}")
            if step < 1:
                raise ValueError(f"cron 步进必须 >= 1: {step_s!r}")
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            try:
                start, end = int(a), int(b)
            except ValueError:
                raise ValueError(f"cron 范围值非法: {part!r}")
        else:
            try:
                start = end = int(part)
            except ValueError:
                raise ValueError(f"cron 值非法: {part!r}")
        if dow:   # 周字段: 7 归一为 0(周日), 兼容标准 cron 两种写法
            start = 0 if start == 7 else start
            end = 0 if end == 7 else end
            if start == 0 and end != 0:
                vals.add(0)      # 0-6 之类范围, 0 已含
        if not (lo <= start <= (hi + (1 if dow else 0)) and lo <= end <= (hi + (1 if dow else 0))):
            raise ValueError(f"cron 值越界 [{lo}-{hi}]: {part!r}")
        if start > end:
            raise ValueError(f"cron 范围起点大于终点: {part!r}")
        # 标准语义: 步进基点为范围起点
        vals.update(v for v in range(start, min(end, hi) + 1) if (v - start) % step == 0)
    if not vals:
        raise ValueError(f"cron 字段无有效值: {field!r}")
    return vals


def cron_match(expr, t=None):
    """判断时间 t(默认当前分钟) 是否命中 cron 表达式 '分 时 日 月 周'。
    完整支持范围(1-5)/步进(*/5)/列表(1,3,5)/组合/周日7 标准语法,
    并遵循标准 cron 的 日/周 OR 语义(两者均受限时任一命中即触发)。"""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron 表达式必须为五段式: {expr!r}")
    t = t or time.localtime()
    minute = _parse_cron_field(fields[0], 0, 59)
    hour = _parse_cron_field(fields[1], 0, 23)
    dom = _parse_cron_field(fields[2], 1, 31)
    month = _parse_cron_field(fields[3], 1, 12)
    dow = _parse_cron_field(fields[4], 0, 6, dow=True)
    # Python tm_wday 周一=0..周日=6; cron 周日=0..周六=6
    now_dow = (t.tm_wday + 1) % 7
    base = (t.tm_min in minute and t.tm_hour in hour and t.tm_mon in month)
    dom_restricted = fields[2].strip() != "*"
    dow_restricted = fields[4].strip() != "*"
    if dom_restricted and dow_restricted:
        # 标准 cron: 日与周均受限 → OR 语义
        return base and (t.tm_mday in dom or now_dow in dow)
    return base and t.tm_mday in dom and now_dow in dow


# ============================== 调度配置 ==============================
def load_schedule_config(path):
    """读取并校验调度配置文件, 返回 (jobs, errors)"""
    if not os.path.isfile(path):
        return [], [f"配置文件不存在: {path}(可用 'schedule init' 生成示例)"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return [], [f"配置文件 JSON 解析失败: {e}"]
    jobs, errors = [], []
    seen_ids = set()
    for i, job in enumerate(data.get("jobs", [])):
        jid = str(job.get("id") or f"job{i + 1}")
        if jid in seen_ids:
            errors.append(f"任务 id 重复: {jid}")
            continue
        seen_ids.add(jid)
        if not str(job.get("prompt", "")).strip():
            errors.append(f"任务 {jid}: 缺少 prompt")
            continue
        timers = [k for k in ("every", "at", "cron") if job.get(k)]
        if len(timers) != 1:
            errors.append(f"任务 {jid}: 定时字段 every/at/cron 必须且只能设置一个")
            continue
        if job.get("every") is not None:
            try:
                if int(job["every"]) < 5:
                    errors.append(f"任务 {jid}: every 间隔不得小于 5 秒")
                    continue
            except (TypeError, ValueError):
                errors.append(f"任务 {jid}: every 必须是整数秒")
                continue
        if job.get("at"):
            try:
                h, m = str(job["at"]).split(":")
                assert 0 <= int(h) <= 23 and 0 <= int(m) <= 59
            except (ValueError, AssertionError):
                errors.append(f"任务 {jid}: at 必须是 HH:MM 格式")
                continue
        if job.get("cron"):
            try:
                cron_match(str(job["cron"]))
            except (ValueError, TypeError) as e:
                errors.append(f"任务 {jid}: cron 无效 → {e}")
                continue
        job["id"] = jid
        jobs.append(job)
    return jobs, errors


def write_sample_config(path):
    sample = {
        "jobs": [
            {"id": "daily_patrol", "name": "每小时巡检", "enabled": True,
             "prompt": "检查项目目录中的代码与日志, 输出巡检摘要报告",
             "every": 3600, "project": "patrol_demo", "max_runs": 0},
            {"id": "morning_brief", "name": "每日晨报", "enabled": False,
             "prompt": "搜索行业最新动态并生成晨报", "at": "08:30"},
            {"id": "sync_10min", "name": "十分钟数据同步", "enabled": False,
             "prompt": "同步并校验数据文件", "cron": "*/10 * * * *"},
        ]
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sample, f, ensure_ascii=False, indent=2)
    return path


# ============================== 异步调度器 ==============================
class Scheduler:
    """asyncio 异步调度: 每个 job 一个协程, 到点后以子进程执行 Agent 任务。
    子进程执行的好处: 进度独立落盘、KILL 干净利落、任务互不影响。"""

    def __init__(self, config_path):
        self.config_path = os.path.abspath(config_path)
        self.registry = TaskRegistry()
        self._stop = asyncio.Event()

    # ---- 触发时间计算 ----
    @staticmethod
    def _next_delay(job):
        """返回距下次触发的秒数"""
        if job.get("every"):
            return int(job["every"])
        if job.get("at"):
            h, m = map(int, str(job["at"]).split(":"))
            now = time.localtime()
            target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, h, m, 0,
                                  0, 0, -1))
            if target <= time.time():
                target += 86400
            return max(1, target - time.time())
        # cron: 逐分钟对齐检查
        return 60 - time.time() % 60

    async def _run_job_once(self, job):
        """触发一次任务: 子进程执行 + 状态登记"""
        task_id = f"T{time.strftime('%m%d%H%M%S')}_{uuid.uuid4().hex[:4]}"
        meta = {"task_id": task_id, "job_id": job["id"],
                "name": job.get("name", job["id"]), "prompt": job["prompt"][:200],
                "status": "running", "pid": None,
                "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                "progress": {"turn": 0, "max_turns": 0, "tool": "", "todos": {}},
                "workdir": "", "exit_code": None}
        self.registry.write(meta)
        cmd = [sys.executable, "-m", "omni_agent", "-p", job["prompt"],
               "--task-id", task_id, "--yolo", "--no-thinking"]
        # v5.9 阻塞性BUG修复: CLI 已在 v3.3 移除 --project 参数, 原实现导致
        # 配置了 project 字段的调度任务子进程 argparse 报错立即退出(永远 failed)。
        # 现将 project 映射为独立工作目录(项目根目录/<project>), 语义等价。
        if job.get("workdir"):
            cmd += ["-w", job["workdir"]]
        elif job.get("project"):
            root = PROJECTS_DIR
            cmd += ["-w", os.path.join(root, str(job["project"]))]
        cprint(f"  ▶ [{job['id']}] 触发 task={task_id}", C.MAGENTA)
        try:
            # v4.2 Windows兼容: start_new_session 为 POSIX-only(Windows 传 True
            # 会抛 ValueError 导致调度器崩溃), Windows 改用新进程组 creationflags
            spawn_kw = {"stdout": asyncio.subprocess.DEVNULL,
                        "stderr": asyncio.subprocess.DEVNULL}
            if os.name == "nt":
                spawn_kw["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
            else:
                spawn_kw["start_new_session"] = True    # 独立进程组, 便于 KILL 整组
            proc = await asyncio.create_subprocess_exec(*cmd, **spawn_kw)
        except OSError as e:
            self.registry.update(task_id, status="failed",
                                 progress={"note": f"子进程启动失败: {e}"})
            return
        self.registry.update(task_id, pid=proc.pid)
        rc = await proc.wait()
        cur = self.registry.read(task_id) or {}
        if cur.get("status") == "killed":     # 被用户 KILL, 保留 killed 状态
            return
        self.registry.update(task_id, status="done" if rc == 0 else "failed",
                             exit_code=rc)
        cprint(f"  ■ [{job['id']}] task={task_id} 结束 exit={rc}", C.GRAY)
        self.registry.prune()

    async def _job_loop(self, job):
        """单个 job 的调度循环"""
        runs = 0
        max_runs = int(job.get("max_runs", 0) or 0)
        while not self._stop.is_set():
            delay = self._next_delay(job)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return  # 收到停止信号
            except asyncio.TimeoutError as _e:
                log.warning("忽略异常(omni_agent/scheduler.py:366): %s: %s", type(_e).__name__, _e)
                pass
            if job.get("cron") and not cron_match(job["cron"]):
                continue   # cron 逐分钟检查, 未命中则继续等待
            runs += 1
            cprint(f"\n═══ [{job['id']}] 第{runs}次触发 "
                   f"{time.strftime('%Y-%m-%d %H:%M:%S')} ═══", C.MAGENTA)
            await self._run_job_once(job)
            if max_runs and runs >= max_runs:
                cprint(f"  [{job['id']}] 已达最大执行次数({max_runs}), 停止调度", C.YELLOW)
                return

    async def _heartbeat(self):
        """调度器心跳: 供 status 命令判断调度器是否在线"""
        while not self._stop.is_set():
            try:
                tmp = HEARTBEAT_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"pid": os.getpid(), "ts": time.time(),
                               "config": self.config_path}, f)
                os.replace(tmp, HEARTBEAT_FILE)
            except OSError as _e:
                log.warning("忽略异常(omni_agent/scheduler.py:388): %s: %s", type(_e).__name__, _e)
                pass
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=SCHEDULER_HEARTBEAT_SEC)
            except asyncio.TimeoutError as _e:
                log.warning("忽略异常(omni_agent/scheduler.py:394): %s: %s", type(_e).__name__, _e)
                continue

    async def run(self):
        jobs, errors = load_schedule_config(self.config_path)
        for e in errors:
            cprint(f"  ! 配置错误: {e}", C.RED)
        active = [j for j in jobs if j.get("enabled", True)]
        if not active:
            cprint("   无可调度的任务(检查配置文件 jobs 且 enabled=true)", C.RED)
            return
        cprint(f"[Haisnap 定时调度器] 配置: {self.config_path} · "
               f"任务数: {len(active)}", C.MAGENTA)
        for j in active:
            timer = j.get("every") and f"every {j['every']}s" \
                or j.get("at") and f"daily {j['at']}" or f"cron '{j['cron']}'"
            cprint(f"  • {j['id']} [{timer}] {j.get('name', '')}: {j['prompt'][:50]}", C.CYAN)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, ValueError) as _e:
                log.warning("忽略异常(omni_agent/scheduler.py:416): %s: %s", type(_e).__name__, _e)
                pass
        job_tasks = [asyncio.create_task(self._job_loop(j)) for j in active]
        hb_task = asyncio.create_task(self._heartbeat())
        await asyncio.gather(*job_tasks, return_exceptions=True)  # 所有 job 循环结束
        self._stop.set()                                          # 通知心跳协程退出
        await asyncio.gather(hb_task, return_exceptions=True)
        cprint("\n调度器已停止", C.YELLOW)


# ============================== 进度查询 / KILL(独立 CLI 进程调用) ==============================
def scheduler_online():
    """判断调度器是否在线(心跳文件新鲜度 + 进程存活)"""
    if not os.path.isfile(HEARTBEAT_FILE):
        return False, None
    try:
        with open(HEARTBEAT_FILE, encoding="utf-8") as f:
            hb = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False, None
    fresh = time.time() - hb.get("ts", 0) <= SCHEDULER_STALE_SEC
    alive = _pid_alive(hb.get("pid", -1))
    return fresh and alive, hb


def print_status(task_id=None):
    """CLI: 展示定时任务进度(全部或指定 task_id)"""
    reg = TaskRegistry()
    online, hb = scheduler_online()
    cprint(f"  调度器: {' 在线 (pid=' + str(hb['pid']) + ')' if online else '⚪ 离线'}",
           C.GREEN if online else C.GRAY)
    metas = [reg.read(task_id)] if task_id else reg.list()
    if task_id and metas[0] is None:
        cprint(f"   任务不存在: {task_id}", C.RED)
        return 1
    if not metas:
        cprint("  (暂无任务执行记录)", C.GRAY)
        return 0
    icons = {"running": "🔵", "done": "", "failed": "", "killed": "⚫"}
    for meta in metas:
        meta = reg.refresh_liveness(meta)
        p = meta.get("progress", {})
        st = meta.get("status", "?")
        line = (f"  {icons.get(st, '·')} {meta['task_id']}  [{meta.get('job_id', '')}] "
                f"{meta.get('name', '')}  {st}  started={meta.get('started', '')}")
        if st == "running":
            todos = p.get("todos") or {}
            line += (f"\n      进度: 第{p.get('turn', 0)}/{p.get('max_turns', '?')}轮"
                     f" · 当前工具: {p.get('tool') or '-'}")
            if todos.get("total"):
                line += f" · 清单 {todos.get('completed', 0)}/{todos['total']}"
                for it in (todos.get("items") or [])[:6]:
                    mark = {"completed": "●", "in_progress": "◐"}.get(it.get("status"), "○")
                    line += f"\n        {mark} {it.get('content', '')[:60]}"
            line += f"\n      pid={meta.get('pid')} · KILL: schedule kill {meta['task_id']}"
        elif p.get("summary"):
            line += f"\n      结果: {str(p['summary'])[:100]}"
        elif p.get("note"):
            line += f"\n      备注: {p['note']}"
        cprint(line, C.CYAN if st == "running" else C.GRAY)
    return 0


def kill_task(task_id):
    """CLI: 终止指定 task_id 的执行进程(整个进程组), 状态标记为 killed"""
    reg = TaskRegistry()
    meta = reg.read(task_id)
    if not meta:
        cprint(f"   任务不存在: {task_id}", C.RED)
        return 1
    if meta.get("status") != "running":
        cprint(f"  ! 任务 {task_id} 当前状态为 {meta.get('status')}, 无需 KILL", C.YELLOW)
        return 1
    pid = meta.get("pid")
    if not pid or not _pid_alive(pid):
        reg.update(task_id, status="failed", progress={"note": "进程已不存在"})
        cprint(f"  ! 任务 {task_id} 进程已不存在, 状态已修正", C.YELLOW)
        return 1
    try:
        if os.name == "nt":
            # v4.2 Windows兼容: killpg/getpgid 为 POSIX-only, 改用 taskkill 递归终止进程树
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
        else:
            # 先 SIGTERM 整个进程组优雅退出, 2s 后仍存活则 SIGKILL
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            for _ in range(20):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
            if _pid_alive(pid):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError,
            subprocess.TimeoutExpired) as e:
        cprint(f"  ! KILL 过程异常: {e}", C.YELLOW)
    reg.update(task_id, status="killed",
               progress={"note": f"被用户 KILL @ {time.strftime('%H:%M:%S')}"})
    cprint(f"  ✓ 任务 {task_id} 已终止(pid={pid})", C.GREEN)
    return 0


# ============================== 子进程侧: 进度写入器 ==============================
def make_progress_writer(task_id):
    """构造 Agent progress_cb: 将执行进度写入任务状态文件(供 status 查询)"""
    reg = TaskRegistry()
    if not reg.read(task_id):   # 直接以 --task-id 启动(非调度器拉起)时自动登记
        reg.write({"task_id": task_id, "job_id": "(manual)", "name": "手动任务",
                   "prompt": "", "status": "running", "pid": os.getpid(),
                   "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "progress": {}, "workdir": os.getcwd(), "exit_code": None})

    def _cb(event, payload):
        if event == "turn":
            reg.update(task_id, progress={"turn": payload.get("turn"),
                                          "max_turns": payload.get("max_turns"),
                                          "todos": payload.get("todos")})
        elif event == "tool":
            reg.update(task_id, progress={"tool": f"{payload.get('tool')}"
                                          f"({payload.get('brief', '')[:60]})"})
        elif event == "done":
            reg.update(task_id, progress={"tool": "", "summary": payload.get("summary", "")})
        elif event == "error":
            reg.update(task_id, progress={"note": payload.get("detail", "")})
    return _cb


def run_scheduler(config_path=None):
    """入口: 启动异步调度器"""
    path = config_path or SCHEDULE_FILE_DEFAULT
    try:
        asyncio.run(Scheduler(path).run())
    except KeyboardInterrupt:
        cprint("\n调度器已停止", C.YELLOW)
