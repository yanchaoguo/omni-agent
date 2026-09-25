# -*- coding: utf-8 -*-
"""快照回滚(Checkpoint · 内容寻址去重存储, 类 git 对象库)"""
from .logger import get_logger
log = get_logger("checkpoint")
import hashlib
import json
import os
import shutil
import threading
import time
import uuid

from .config import SETTINGS_DIR, SKIP_DIRS


class CheckpointManager:
    """- blobs/  : 按文件内容 sha256 存储, 相同内容全局只存一份
    - <cid>/manifest.json : 相对路径 → blob哈希 的映射
    - 快照级去重: 工作区状态哈希相同的快照直接复用, 来回切换版本零冗余"""

    def __init__(self, workdir):
        self.wd = workdir
        self.dir = os.path.join(workdir, SETTINGS_DIR, "checkpoints")
        self.blob_dir = os.path.join(self.dir, "blobs")
        os.makedirs(self.blob_dir, exist_ok=True)
        self._lock = threading.RLock()  # 快照创建/回滚并发保护(可重入: rollback 内调 create)

    def _walk_files(self):
        for root, dirs, files in os.walk(self.wd):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for f in files:
                p = os.path.join(root, f)
                rel = os.path.relpath(p, self.wd).replace(os.sep, "/")
                yield rel, p

    def _store_blob(self, path):
        h = hashlib.sha256()
        with open(path, "rb") as fp:
            for chunk in iter(lambda: fp.read(65536), b""):
                h.update(chunk)
        digest = h.hexdigest()
        dst = os.path.join(self.blob_dir, digest)
        if not os.path.exists(dst):
            shutil.copy2(path, dst)
        return digest

    def _snapshot_manifest(self):
        manifest = {}
        for rel, p in sorted(self._walk_files()):
            try:
                manifest[rel] = self._store_blob(p)
            except OSError as _e:
                log.warning("忽略异常(omni_agent/checkpoint.py:50): %s: %s", type(_e).__name__, _e)
                continue
        state = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
        return manifest, state

    def _find_by_state(self, state):
        for meta in self.list():
            if meta.get("state") == state:
                return meta["id"]
        return None

    def current_state(self):
        """返回当前工作区的状态哈希(不创建快照), 供 CLI 标记当前快照"""
        _, state = self._snapshot_manifest()
        return state

    def create(self, note="", messages=None, auto=False):
        """创建快照; 工作区状态与既有快照一致则直接复用。
        并发锁保护 —— 防多线程同时创建同一状态快照导致 blob 竞态写。"""
        with self._lock:
            manifest, state = self._snapshot_manifest()
            existing_cid = self._find_by_state(state)
            if existing_cid:
                return existing_cid, True
            cid = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
            dst = os.path.join(self.dir, cid)
            os.makedirs(dst, exist_ok=True)
            with open(os.path.join(dst, "manifest.json"), "w", encoding="utf-8") as f:
                json.dump(manifest, f)
            meta = {"id": cid, "note": note[:100], "auto": auto, "state": state,
                    "files": len(manifest), "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            with open(os.path.join(dst, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            if messages:
                with open(os.path.join(dst, "context.json"), "w", encoding="utf-8") as f:
                    json.dump(messages, f, ensure_ascii=False, default=str)
            self._prune()
            return cid, False

    def _prune(self, keep=20):
        """清理最旧快照, 并回收不再被引用的孤儿 blob"""
        ids = sorted(d for d in os.listdir(self.dir) if d != "blobs")
        for old in ids[:-keep]:
            shutil.rmtree(os.path.join(self.dir, old), ignore_errors=True)
        referenced = set()
        for cid in os.listdir(self.dir):
            mp = os.path.join(self.dir, cid, "manifest.json")
            if os.path.isfile(mp):
                try:
                    with open(mp, encoding="utf-8") as f:
                        referenced.update(json.load(f).values())
                except (json.JSONDecodeError, OSError) as _e:
                    log.warning("忽略异常(omni_agent/checkpoint.py:100): %s: %s", type(_e).__name__, _e)
                    continue
        for blob in os.listdir(self.blob_dir):
            if blob not in referenced:
                try:
                    os.remove(os.path.join(self.blob_dir, blob))
                except OSError as _e:
                    log.warning("忽略异常(omni_agent/checkpoint.py:107): %s: %s", type(_e).__name__, _e)
                    pass

    def materialize(self, cid, dest_dir):
        """快照物化 —— 将指定快照的文件还原到独立目录 dest_dir。
        用于交付物隔离: 部署/预览/变更均指向该快照目录内的文件, 新交付物
        不会覆盖旧快照。返回 (缺失文件数, 目录)。"""
        mp = os.path.join(self.dir, cid, "manifest.json")
        if not os.path.isfile(mp):
            return -1, f"[错误] 快照不存在: {cid}"
        if os.path.isdir(dest_dir):
            shutil.rmtree(dest_dir, ignore_errors=True)
        os.makedirs(dest_dir, exist_ok=True)
        with open(mp, encoding="utf-8") as f:
            manifest = json.load(f)
        missing = 0
        for rel, digest in manifest.items():
            src = os.path.join(self.blob_dir, digest)
            if not os.path.isfile(src):
                missing += 1
                continue
            dst = os.path.join(dest_dir, rel)
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            shutil.copy2(src, dst)
        return missing, dest_dir

    def list(self):
        out = []
        for cid in sorted(d for d in os.listdir(self.dir) if d != "blobs"):
            mp = os.path.join(self.dir, cid, "meta.json")
            if os.path.isfile(mp):
                try:
                    with open(mp, encoding="utf-8") as f:
                        out.append(json.load(f))
                except (json.JSONDecodeError, OSError) as _e:
                    log.warning("忽略异常(omni_agent/checkpoint.py:119): %s: %s", type(_e).__name__, _e)
                    continue
        return out

    def rollback(self, cid, restore_context=False):
        """回滚到指定快照; 回滚前自动快照当前状态(内容寻址去重)。
        并发锁保护 —— 回滚期间其他线程不得同时创建快照/修改工作区。"""
        with self._lock:
            return self._rollback_locked(cid, restore_context)

    def _rollback_locked(self, cid, restore_context=False):
        """回滚到指定快照; 回滚前自动快照当前状态(内容寻址去重)"""
        mp = os.path.join(self.dir, cid, "manifest.json")
        if not os.path.isfile(mp):
            return None, f"[错误] 快照不存在: {cid}"
        backup, reused = self.create(note=f"rollback前自动备份(目标:{cid})", auto=True)
        if backup == cid:
            return None, f"[提示] 当前工作区状态与快照 {cid} 完全一致, 无需回滚"
        with open(mp, encoding="utf-8") as f:
            manifest = json.load(f)
        for name in os.listdir(self.wd):
            if name == SETTINGS_DIR or name in SKIP_DIRS or name.startswith("."):
                continue
            p = os.path.join(self.wd, name)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except OSError as _e:
                    log.warning("忽略异常(omni_agent/checkpoint.py:149): %s: %s", type(_e).__name__, _e)
                    pass
        missing = []
        for rel, digest in manifest.items():
            src = os.path.join(self.blob_dir, digest)
            if not os.path.isfile(src):
                missing.append(rel)
                continue
            dst = os.path.join(self.wd, rel)
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            shutil.copy2(src, dst)
        ctx = None
        cp = os.path.join(self.dir, cid, "context.json")
        if restore_context and os.path.isfile(cp):
            with open(cp, encoding="utf-8") as f:
                ctx = json.load(f)
        note = "(复用已有快照, 零冗余)" if reused else "(新建快照)"
        msg = f"[成功] 已回滚到快照 {cid}; 回滚前状态已备份为 {backup} {note}"
        if missing:
            msg += f";  {len(missing)} 个文件的 blob 缺失未还原: {missing[:5]}"
        return ctx, msg
