# -*- coding: utf-8 -*-
"""Hooks 系统: PreToolUse / PostToolUse 钩子(内置工具与 MCP 工具均被拦截)
v4.8 安全加固:
- hook 命令通过环境变量接收工具名与参数, 原实现参数值未经 shell 转义,
  hook 脚本内无引号引用 $HAISNAP_PAYLOAD 时存在命令注入风险。
  现提供 HAISNAP_TOOL_Q / HAISNAP_PAYLOAD_Q 经 shlex.quote 转义的安全版本,
  且原始值中的控制字符统一清洗; 工具名做白名单字符校验。
"""
import fnmatch
import json
import os
import re
import shlex
import subprocess

from .logger import get_logger
from .ui import C, cprint

log = get_logger("hooks")

_TOOL_NAME_RX = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")
_CTRL_RX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize(value, limit=4000):
    """清洗控制字符并截断, 防换行/终端转义序列注入 hook 脚本"""
    return _CTRL_RX.sub("", str(value))[:limit]


class Hooks:
    def __init__(self, workdir, settings):
        self.workdir = workdir
        self.cfg = {"pre_tool_use": [], "post_tool_use": []}
        for k in self.cfg:
            self.cfg[k] = settings.get("hooks", {}).get(k, [])
        if any(self.cfg.values()):
            cprint(f"  ✓ 已加载 Hooks 配置 (pre={len(self.cfg['pre_tool_use'])}, "
                   f"post={len(self.cfg['post_tool_use'])})", C.GRAY)

    def fire(self, event, tool_name, payload):
        for hook in self.cfg.get(event, []):
            matcher = hook.get("matcher", "*")
            # 支持通配: * 匹配所有; mcp__* 可匹配全部 MCP 工具
            if matcher != "*" and matcher != tool_name and not fnmatch.fnmatch(tool_name, matcher):
                continue
            cmd = hook.get("command", "")
            if not cmd:
                continue
            # 工具名白名单字符校验(防伪造工具名注入)
            safe_tool = tool_name if _TOOL_NAME_RX.match(tool_name or "") else "invalid_tool"
            if safe_tool != tool_name:
                log.warning("hook: 工具名含非法字符已替换: %r", tool_name)
            raw_payload = _sanitize(
                json.dumps(payload, ensure_ascii=False, default=str))
            env = os.environ.copy()
            env["HAISNAP_TOOL"] = safe_tool
            env["HAISNAP_EVENT"] = event
            env["HAISNAP_PAYLOAD"] = raw_payload
            # shlex.quote 转义版本 —— hook 脚本中可直接安全内插
            env["HAISNAP_TOOL_Q"] = shlex.quote(safe_tool)
            env["HAISNAP_PAYLOAD_Q"] = shlex.quote(raw_payload)
            try:
                r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                                   timeout=20, env=env, cwd=self.workdir)
                cprint(f"  ⚓ hook[{event}:{matcher}] exit={r.returncode}", C.GRAY)
                log.info("hook fired: event=%s matcher=%s tool=%s exit=%s",
                         event, matcher, safe_tool, r.returncode)
                if event == "pre_tool_use" and r.returncode == 2:
                    return False, (r.stderr or r.stdout or "hook 阻止了本次调用").strip()
            except Exception as e:
                cprint(f"  ⚓ hook 执行异常: {e}", C.GRAY)
                log.error("hook 执行异常: event=%s matcher=%s tool=%s err=%s: %s",
                          event, matcher, safe_tool, type(e).__name__, e)
        return True, ""
