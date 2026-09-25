# -*- coding: utf-8 -*-
"""会话管理(多会话 · 每个会话即一个任务): 索引登记 + 上下文持久化"""
from .logger import get_logger
log = get_logger("session")
import json
import os
import shutil
import threading
import time
import uuid

from .config import SETTINGS_DIR, PROJECTS_DIR


class SessionManager:
    """- 会话索引 : <root>/sessions.json 统一登记会话元数据
    - 上下文   : <workdir>/.haisnap/context.json 持久化对话消息"""

    def __init__(self, root=None):
        self.root = root or PROJECTS_DIR
        os.makedirs(self.root, exist_ok=True)
        self.index_path = os.path.join(self.root, "sessions.json")
        self._lock = threading.Lock()       # sessions.json 索引读写锁
        self._ctx_lock = threading.Lock()   # context.json 并发读写锁

    def _read(self):
        if os.path.isfile(self.index_path):
            try:
                with open(self.index_path, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _write(self, items):
        tmp = self.index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.index_path)

    def list(self):
        """置顶会话优先, 其余按更新时间倒序(供网页版历史对话面板)"""
        items = self._read()
        # updated 精度为秒, 同一秒内创建的多个会话时间戳相同导致排序不稳定
        # (新会话反排末尾); 以 session_id 自增序号作次级排序键保证新会话恒在前
        items.sort(key=lambda x: (x.get("updated", ""), x.get("session_id", "")),
                   reverse=True)
        items.sort(key=lambda x: not x.get("pinned", False))
        return items

    def rename(self, sid, name_zh):
        return self.update(sid, name_zh=name_zh[:60])

    def toggle_pin(self, sid):
        cur = self.get(sid)
        if not cur:
            return None
        return self.update(sid, pinned=not cur.get("pinned", False))

    def delete(self, sid, remove_files=False):
        """从索引移除会话; remove_files=True 时同步删除会话目录
        (安全约束: 仅允许删除位于会话根目录内的目录, 防误删)"""
        with self._lock:
            items = self._read()
            target = next((x for x in items if x.get("session_id") == sid), None)
            if not target:
                return False
            self._write([x for x in items if x.get("session_id") != sid])
        if remove_files:
            wd = os.path.realpath(target.get("workdir", ""))
            root = os.path.realpath(self.root)
            if wd.startswith(root + os.sep) and os.path.isdir(wd):
                shutil.rmtree(wd, ignore_errors=True)
        return True

    def get(self, sid):
        return next((s for s in self._read() if s.get("session_id") == sid), None)

    def find_by_workdir(self, workdir):
        wd = os.path.abspath(workdir)
        return next((s for s in self._read() if s.get("workdir") == wd), None)

    def create(self, name_zh, name_en, task_type, workdir, last_task=""):
        """注册新会话, 返回会话元数据"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            items = self._read()
            sid = f"S{len(items) + 1:03d}_{uuid.uuid4().hex[:6]}"
            meta = {"session_id": sid, "name_zh": name_zh, "name_en": name_en,
                    "task_type": task_type, "workdir": os.path.abspath(workdir),
                    "last_task": last_task[:80], "created": now, "updated": now}
            items.append(meta)
            self._write(items)
        return meta

    def update(self, sid, **fields):
        with self._lock:
            items = self._read()
            for it in items:
                if it.get("session_id") == sid:
                    it.update(fields)
                    it["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    self._write(items)
                    return it
        return None

    def save_context(self, session, messages, last_task=None):
        # 并发锁保护 —— Web多线程/后台学习线程同时写同一会话上下文的竞态
        d = os.path.join(session.get("workdir", ""), SETTINGS_DIR)
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, "context.json.tmp")
        with self._ctx_lock:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, default=str)
            os.replace(tmp, os.path.join(d, "context.json"))
        if last_task:
            self.update(session.get("session_id"), last_task=last_task[:80])

    def load_context(self, session):
        # 并发锁保护 —— 读取时防止与写操作交错读到半截文件
        p = os.path.join(session.get("workdir", ""), SETTINGS_DIR, "context.json")
        if os.path.isfile(p):
            try:
                with self._ctx_lock:
                    with open(p, encoding="utf-8") as f:
                        ctx = json.load(f)
                if isinstance(ctx, list) and ctx:
                    return ctx
            except (json.JSONDecodeError, OSError) as _e:
                log.warning("忽略异常(omni_agent/session.py:124): %s: %s", type(_e).__name__, _e)
                pass
        return None
