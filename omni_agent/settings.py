# -*- coding: utf-8 -*-
"""统一配置中心
加载优先级(高→低): 环境变量 > 项目 .haisnap/settings.json > 全局 ~/.haisnap/settings.json > config.py 内置默认

修复点:
- 深度合并: 原实现为 dict.update() 浅合并, 项目级配置若只写了 model.api_key,
  会把全局 model 节点整段覆盖丢失 base_url/model 等兄弟键; 现逐键递归合并。
- 环境变量最高优先: 原 load_settings 无 env 层, llm/tool_runner/compressor/
  permission 等模块各自只读 env+config.py 而跳过 settings.json;
  现统一由 Settings.get() 走完整四层链路, 运行时热读取(Web UI 改配置即刻生效)。
- 来源追踪: inspect() 返回每个配置项的最终生效值与来源层, 供「配置诊断」面板展示。
"""
import json
import os
import threading

from . import config as _cfg
from .config import GLOBAL_DIR, SETTINGS_DIR
from .ui import C, cprint

# 配置键 -> 环境变量名(env 层, 最高优先级)
ENV_MAP = {
    "model.api_key": "HAISNAP_API_KEY",
    "model.base_url": "HAISNAP_BASE_URL",
    "model.model": "HAISNAP_MODEL",
    "model.vision_model": "HAISNAP_VISION_MODEL",
    "model.vision_api_key": "HAISNAP_VISION_API_KEY",
    "model.vision_base_url": "HAISNAP_VISION_BASE_URL",
    "model.fallback_model": "HAISNAP_FALLBACK_MODEL",
    "model.fallback_api_key": "HAISNAP_FALLBACK_API_KEY",
    "model.fallback_base_url": "HAISNAP_FALLBACK_BASE_URL",
    # 多模智能路由配置(环境变量通道)
    "model.routing.enabled": "HAISNAP_ROUTING",
    "model.routing.light_model": "HAISNAP_ROUTING_LIGHT",
    "model.routing.code_model": "HAISNAP_ROUTING_CODE",
    "model.routing.long_model": "HAISNAP_ROUTING_LONG",
    "model.routing.long_context_tokens": "HAISNAP_ROUTING_LONG_TOKENS",
    "thinking": "HAISNAP_THINKING",
    "agent.max_turns": "HAISNAP_MAX_TURNS",
    "agent.bash_timeout": "HAISNAP_BASH_TIMEOUT",
    "agent.max_tool_output": "HAISNAP_MAX_TOOL_OUTPUT",
    "agent.max_file_read": "HAISNAP_MAX_FILE_READ",
    "agent.ask_default_timeout": "HAISNAP_ASK_TIMEOUT",
    "agent.web_fetch_timeout": "HAISNAP_WEB_FETCH_TIMEOUT",
    "unifuncs.api_key": "HAISNAP_UNIFUNCS_KEY",
    "unifuncs.base": "HAISNAP_UNIFUNCS_BASE",
    "image.batch_workers": "HAISNAP_IMAGE_WORKERS",
    "image.model": "HAISNAP_IMAGE_MODEL",
    "browser.user_agent": "HAISNAP_UA",
    "browser.headed_first": "HAISNAP_BROWSER_HEADED",
    "prompt_cache.mode": "HAISNAP_PROMPT_CACHE",
    "prompt_cache.ttl": "HAISNAP_PROMPT_CACHE_TTL",
    "reflexion.enabled": "HAISNAP_REFLEXION",
    "reflexion.error_threshold": "HAISNAP_REFLEXION_THRESHOLD",
}

# 配置键 -> config.py 内置默认(最低优先级兜底)
CONFIG_DEFAULTS = {
    "model.api_key": "",
    "model.base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model.model": "qwen3-max",   # 与 config.py 对齐(支持思考透传)
    "model.vision_model": "qwen-vl-max",
    "model.vision_api_key": "",
    "model.vision_base_url": "",
    "model.fallback_model": "",
    "model.fallback_api_key": "",
    "model.fallback_base_url": "",
    # 多模智能路由默认值(专用槽位留空=回退主模型, 路由零副作用)
    "model.routing.enabled": True,
    "model.routing.light_model": "",
    "model.routing.code_model": "",
    "model.routing.long_model": "",
    "model.routing.long_context_tokens": 30000,
    "thinking": True,
    "agent.max_turns": _cfg.MAX_AGENT_TURNS,
    "agent.bash_timeout": _cfg.BASH_TIMEOUT,
    "agent.max_tool_output": _cfg.MAX_TOOL_OUTPUT,
    "agent.max_file_read": _cfg.MAX_FILE_READ,
    "agent.ask_default_timeout": _cfg.ASK_DEFAULT_TIMEOUT,
    "agent.web_fetch_timeout": _cfg.WEB_FETCH_TIMEOUT,
    "unifuncs.api_key": _cfg.UNIFUNCS_API_KEY,
    "unifuncs.base": _cfg.UNIFUNCS_BASE,
    "image.batch_workers": _cfg.IMAGE_BATCH_WORKERS,
    "image.model": "qwen-image-plus",
    "browser.user_agent": _cfg.UA,
    "browser.headed_first": True,
    "prompt_cache.mode": _cfg.PROMPT_CACHE,
    "prompt_cache.ttl": _cfg.PROMPT_CACHE_TTL,
    "reflexion.enabled": True,
    "reflexion.error_threshold": 2,
}

_SENSITIVE_KEYS = ("api_key", "key", "token", "secret", "password")


def _deep_merge(base, override):
    """递归深合并: override 的嵌套 dict 逐键覆盖 base, 不整段替换"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_json(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception as e:
        cprint(f"  ! settings 解析失败 {path}: {e}", C.RED)
        return {}


def _dig(d, dotted):
    """按 'a.b.c' 取嵌套值, 缺失返回 _MISS"""
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISS
        cur = cur[part]
    return cur


class _Missing:
    def __repr__(self):
        return "<MISSING>"


_MISS = _Missing()


def _cast_like(raw, ref):
    """env 字符串按目标类型转换"""
    if isinstance(ref, bool):
        return str(raw).strip().lower() in ("1", "true", "on", "yes")
    if isinstance(ref, int) and not isinstance(ref, bool):
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return ref
    if isinstance(ref, float):
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            return ref
    return raw


class SettingsManager:
    """四层配置链单例: env > 项目 settings.json > 全局 settings.json > config.py"""

    def __init__(self):
        self._lock = threading.Lock()
        self._workdir = ""
        self._project = {}
        self._global = {}
        self._loaded = False

    # ---- 生命周期 ----
    def init(self, workdir):
        """Agent 创建/切换工作目录时调用, 绑定项目级配置文件"""
        with self._lock:
            self._workdir = os.path.abspath(workdir or "")
            self.reload_locked()
        return self

    def reload(self):
        with self._lock:
            self.reload_locked()

    def reload_locked(self):
        self._global = _read_json(os.path.join(GLOBAL_DIR, "settings.json"))
        self._project = _read_json(
            os.path.join(self._workdir, SETTINGS_DIR, "settings.json")) \
            if self._workdir else {}
        self._loaded = True

    def _ensure(self):
        if not self._loaded:
            with self._lock:
                if not self._loaded:
                    self.reload_locked()

    # ---- 取值(带来源) ----
    def resolve(self, key, default=None):
        """返回 (value, source): source ∈ env/project/global/config/default"""
        self._ensure()
        ref = CONFIG_DEFAULTS.get(key, default)
        env_name = ENV_MAP.get(key)
        if env_name:
            raw = os.environ.get(env_name, "")
            if raw != "":
                return _cast_like(raw, ref), "env"
        v = _dig(self._project, key)
        if v is not _MISS and v not in ("", None):
            return v, "project"
        v = _dig(self._global, key)
        if v is not _MISS and v not in ("", None):
            return v, "global"
        if key in CONFIG_DEFAULTS:
            return CONFIG_DEFAULTS[key], "config"
        return default, "default"

    def get(self, key, default=None):
        return self.resolve(key, default)[0]

    def get_int(self, key, default=0):
        v = self.get(key, default)
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def get_bool(self, key, default=False):
        v = self.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "on", "yes")

    def get_str(self, key, default=""):
        v = self.get(key, default)
        return str(v) if v is not None else default

    # ---- 诊断面板数据 ----
    # ---- 模型池(模型管理)持久化操作 ----
    def _pool_path(self):
        """模型池持久化路径: 全局 ~/.haisnap/model_pool.json"""
        return os.path.join(GLOBAL_DIR, "model_pool.json")

    def get_pool(self):
        """返回模型池列表 [{name, base_url, api_key, key_set}]"""
        self._ensure()
        # settings.json 中的 model.pool 为内置初始值; 用户通过 Web 登记的
        # 模型持久化在 model_pool.json, 两者合并去重(name 唯一, 用户登记优先)
        builtin = self.get("model.pool", [])
        if not isinstance(builtin, list):
            builtin = []
        user_pool = []
        try:
            with open(self._pool_path(), encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, list):
                user_pool = d
        except (OSError, json.JSONDecodeError):
            pass
        # 合并: user_pool 优先, builtin 补充不重名的
        seen = {m.get("name") for m in user_pool if isinstance(m, dict)}
        merged = list(user_pool)
        for m in builtin:
            if isinstance(m, dict) and m.get("name") and m["name"] not in seen:
                merged.append(m)
                seen.add(m["name"])
        # 过滤 _comment 占位项, 标准化输出
        out = []
        for m in merged:
            if not isinstance(m, dict) or not m.get("name"):
                continue
            name = str(m["name"]).strip()
            if not name:
                continue
            ak = str(m.get("api_key") or "").strip()
            out.append({
                "name": name,
                "base_url": str(m.get("base_url") or "").strip(),
                "api_key": ak,
                "key_set": bool(ak),
            })
        return out

    def pool_add(self, name, base_url="", api_key=""):
        """登记/更新模型到池中(name 唯一键, 存在则更新)"""
        name = (name or "").strip()
        if not name:
            return False, "模型名不能为空"
        pool = self._load_pool_raw()
        found = False
        for m in pool:
            if m.get("name") == name:
                if base_url:
                    m["base_url"] = base_url
                if api_key:
                    m["api_key"] = api_key
                found = True
                break
        if not found:
            pool.append({"name": name, "base_url": base_url, "api_key": api_key})
        self._save_pool(pool)
        # 热重载使 ModelRouter 端点覆盖即时生效
        self.reload()
        return True, f"模型「{name}」已{'更新' if found else '登记'}"

    def pool_del(self, name):
        """从用户模型池中移除指定模型"""
        name = (name or "").strip()
        pool = self._load_pool_raw()
        new_pool = [m for m in pool if m.get("name") != name]
        if len(new_pool) == len(pool):
            return False, f"模型「{name}」不在用户池中(可能是内置模型, 无法移除)"
        self._save_pool(new_pool)
        self.reload()
        return True, f"模型「{name}」已从模型管理中移除"

    def _load_pool_raw(self):
        try:
            with open(self._pool_path(), encoding="utf-8") as f:
                d = json.load(f)
            return [m for m in d if isinstance(m, dict)] if isinstance(d, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_pool(self, pool):
        path = self._pool_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pool, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    @staticmethod
    def _mask(key, value):
        if any(s in key.lower() for s in _SENSITIVE_KEYS) and value:
            v = str(value)
            return (v[:4] + "*" * min(len(v) - 6, 16) + v[-2:]) if len(v) > 8 \
                else "*" * len(v)
        v = str(value)
        return v if len(v) <= 80 else v[:77] + "..."

    def inspect(self):
        """返回全部已知配置项的 生效值+来源层, 敏感值打码(配置诊断面板)"""
        self._ensure()
        # 用户级 env.json 变量合并进诊断列表(用户覆盖层, 支持直接修改/新增)
        from .envstore import EnvStore
        user_env = EnvStore.load()
        known_envs = set(ENV_MAP.values())
        items = []
        for key in sorted(CONFIG_DEFAULTS):
            val, src = self.resolve(key)
            env_name = ENV_MAP.get(key, "")
            # 生效值来自用户级 env.json 同名覆盖 -> 标记 user 层(可删除恢复下层)
            if src == "env" and env_name in user_env:
                src = "user"
            items.append({
                "key": key,
                "env": env_name,
                "value": self._mask(key, val),
                "source": src,
                "sensitive": any(s in key.lower() for s in _SENSITIVE_KEYS),
            })
        for k in sorted(user_env):   # 非配置链映射的用户自定义变量
            if k not in known_envs:
                items.append({
                    "key": k, "env": k,
                    "value": EnvStore.mask(k, user_env[k]),
                    "source": "user", "custom": True,
                    "sensitive": any(s in k.lower() for s in _SENSITIVE_KEYS),
                })
        return {
            "items": items,
            "workdir": self._workdir,
            "project_file": os.path.join(self._workdir, SETTINGS_DIR,
                                         "settings.json") if self._workdir else "",
            "global_file": os.path.join(GLOBAL_DIR, "settings.json"),
            "project_exists": bool(self._project),
            "global_exists": bool(self._global),
            "priority": "用户覆盖(env.json) > 环境变量 > 项目 settings.json > 全局 settings.json > config.py 内置默认",
        }

    # ---- 兼容旧接口: 深合并后的完整 dict(项目覆盖全局) ----
    def merged(self):
        self._ensure()
        return _deep_merge(self._global, self._project)


S = SettingsManager()   # 进程级单例


def load_settings(workdir):
    """兼容旧调用: 返回 全局+项目 深合并后的 settings dict(项目优先)。
    v5.9 修复: 原 dict.update() 浅合并会让项目级嵌套节点整段覆盖全局同名节点。"""
    merged = {}
    for path in (os.path.join(GLOBAL_DIR, "settings.json"),
                 os.path.join(workdir, SETTINGS_DIR, "settings.json")):
        merged = _deep_merge(merged, _read_json(path))
    return merged