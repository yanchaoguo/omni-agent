# -*- coding: utf-8 -*-
"""用户级环境变量存储: ~/.haisnap/env.json
- 同名覆盖策略: 启动时(config 模块加载最前) apply() 注入 os.environ,
  用户级配置覆盖进程同名环境变量; set/unset 即时生效并持久化。
- CLI 入口: /env 命令; Web 入口: 顶栏「环境变量」弹窗(/api/env)。
- 敏感值(KEY/TOKEN/SECRET/PASSWORD等)展示时自动打码, 永不明文回显。
"""
import json
import os
import re
import threading

_KEY_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_RX = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)


class EnvStore:
    # v6.0阻塞性修复: 原 expanduser(".haisnap") 无法展开(落在CWD相对目录),
    # 换目录启动后用户级环境变量全部丢失; 现修正为真正的 ~/.haisnap/env.json
    # (envstore 被 config 模块最先导入, 不能反向依赖 config.GLOBAL_DIR, 故独立计算)
    FILE = os.path.join(
        os.environ.get("HAISNAP_HOME",
                       os.path.join(os.path.expanduser("~"), ".haisnap")),
        "env.json")
    _lock = threading.Lock()

    # ---- 持久化 ----
    @classmethod
    def load(cls):
        try:
            with open(cls.FILE, encoding="utf-8") as f:
                d = json.load(f)
            return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    @classmethod
    def _save(cls, d):
        os.makedirs(os.path.dirname(cls.FILE), exist_ok=True)
        tmp = cls.FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, cls.FILE)   # 原子写, 防并发写坏配置

    # ---- 同名覆盖注入 ----
    @classmethod
    def apply(cls):
        """启动时调用: 用户级环境变量注入 os.environ(同名覆盖进程变量)。
        返回注入的变量数。"""
        d = cls.load()
        for k, v in d.items():
            os.environ[k] = v
        return len(d)

    # ---- 增删查 ----
    @classmethod
    def set(cls, key, value):
        key = (key or "").strip()
        if not _KEY_RX.match(key):
            return False, f"非法变量名: {key or '(空)'}(仅允许字母/数字/下划线, 不以数字开头)"
        if value is None:
            return False, "变量值不能为 None"
        with cls._lock:
            d = cls.load()
            existed = key in d
            d[key] = str(value)
            cls._save(d)
        os.environ[key] = str(value)   # 即时生效(同名覆盖)
        return True, f"已{'更新' if existed else '新增'}用户级环境变量 {key}(同名覆盖, 即时生效)"

    @classmethod
    def unset(cls, key):
        key = (key or "").strip()
        with cls._lock:
            d = cls.load()
            if key not in d:
                return False, f"变量不存在: {key}"
            d.pop(key)
            cls._save(d)
        os.environ.pop(key, None)
        return True, f"已删除用户级环境变量 {key}"

    # ---- 展示(敏感值打码) ----
    @staticmethod
    def mask(key, value):
        v = str(value)
        if _SENSITIVE_RX.search(key or ""):
            if len(v) <= 8:
                return "*" * len(v)
            return v[:4] + "*" * min(len(v) - 6, 20) + v[-2:]
        return v if len(v) <= 60 else v[:57] + "..."

    @classmethod
    def list_masked(cls):
        return [{"key": k, "value": cls.mask(k, v),
                 "sensitive": bool(_SENSITIVE_RX.search(k))}
                for k, v in sorted(cls.load().items())]