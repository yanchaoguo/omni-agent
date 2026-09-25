# -*- coding: utf-8 -*-
"""MCP 协议支持: stdio JSON-RPC + Streamable HTTP + SSE 三传输, 第三方工具与内置工具同等调用

新增 SSE(Server-Sent Events)传输协议 —— MCPHttpServer 的子类型,
通过 EventSource 长连接保持双向通信, 适配仅支持 SSE 端点的 MCP Server。
"""
from .logger import get_logger
log = get_logger("mcp")
import json
import os
import queue
import subprocess
import threading
import time
import urllib.error
import urllib.request

from .config import __version__
from .ui import C, cprint


class MCPServer:
    """单个 MCP Server 连接: 换行分隔 JSON-RPC over stdio"""

    transport = "stdio"

    def __init__(self, name, cfg):
        self.name = name
        cmd = [cfg["command"]] + cfg.get("args", [])
        env = os.environ.copy()
        env.update(cfg.get("env", {}))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, env=env, bufsize=1)
        self._id = 0
        self._q = queue.Queue()
        threading.Thread(target=self._reader, daemon=True,
                         name=f"mcp-reader-{self.name}").start()

    def _reader(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if line:
                    try:
                        self._q.put(json.loads(line))
                    except json.JSONDecodeError as _e:
                        log.warning("忽略异常(omni_agent/mcp.py:46): %s: %s", type(_e).__name__, _e)
                        continue
        except (ValueError, OSError) as _e:
            log.warning("忽略异常(omni_agent/mcp.py:49): %s: %s", type(_e).__name__, _e)
            pass

    def _send(self, obj):
        # v6.1阻塞性修复: 子进程崩溃后向已关闭管道写入会抛出未包装的
        # BrokenPipeError 直接冒泡; 先探活并包装为带上下文的 RuntimeError
        if self.proc.poll() is not None:
            raise RuntimeError(
                f"MCP[{self.name}] 子进程已退出(exit={self.proc.returncode})")
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise RuntimeError(f"MCP[{self.name}] stdin 写入失败: {e}") from e

    def _rpc(self, method, params=None, timeout=30):
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self._q.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"MCP[{self.name}] " + msg["error"].get("message", "error"))
                return msg.get("result", {})
        raise TimeoutError(f"MCP {self.name}.{method} 超时")

    def initialize(self):
        self._rpc("initialize", {"protocolVersion": "2025-03-26",
                                 "capabilities": {},
                                 "clientInfo": {"name": f"omni-agent/{self.name}",
                                                "version": __version__}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def list_tools(self):
        return self._rpc("tools/list").get("tools", [])

    def call(self, tool, args):
        result = self._rpc("tools/call", {"name": tool, "arguments": args}, timeout=60)
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        out = "\n".join(texts) or json.dumps(result, ensure_ascii=False)[:2000]
        if result.get("isError"):
            out = f"[错误] (server={self.name}) " + out
        return out

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)   # 限时等待, 避免僵尸进程阻塞退出
        except subprocess.TimeoutExpired:
            self.proc.kill()
        except OSError as _e:
            log.warning("忽略异常(omni_agent/mcp.py:97): %s: %s", type(_e).__name__, _e)
            pass


class MCPHttpServer:
    """云端 MCP Server 连接(Streamable HTTP 传输)。
    通过 Remote URL POST JSON-RPC, 兼容 application/json 与 text/event-stream(SSE) 两种响应,
    支持 Mcp-Session-Id 会话保持与自定义鉴权 Header。接口与 MCPServer 完全对齐。"""

    transport = "http"

    def __init__(self, name, cfg):
        self.name = name
        self.url = (cfg.get("url") or "").strip()
        if not self.url.startswith(("http://", "https://")):
            raise ValueError(f"非法 MCP Remote URL: {self.url or '(空)'}")
        self.headers = dict(cfg.get("headers") or {})
        self.session_id = None
        self._id = 0
        self._lock = threading.Lock()

    # ---- HTTP 传输层 ----
    def _post(self, obj, timeout=30):
        """POST 单条 JSON-RPC 消息, 返回解析后的响应消息(通知类 202/空体返回 None)"""
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "User-Agent": f"omni-agent-MCP/{__version__}"}
        headers.update(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(obj).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except OSError as _e:
                log.warning("忽略异常(omni_agent/mcp.py:136): %s: %s", type(_e).__name__, _e)
                pass
            raise RuntimeError(f"MCP[{self.name}] HTTP {e.code}: {detail or e.reason}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"MCP[{self.name}] 连接失败: {e.reason}")
        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            body = resp.read().decode("utf-8", "replace")
            if resp.status == 202 or not body.strip():
                return None    # 通知已受理, 无响应体
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/event-stream" in ctype:
                return self._parse_sse(body, obj.get("id"))
            return json.loads(body)

    @staticmethod
    def _parse_sse(body, rid):
        """解析 SSE 流: 提取 data: 行中与请求 id 匹配的 JSON-RPC 响应"""
        last = None
        for raw_event in body.replace("\r\n", "\n").split("\n\n"):
            data_lines = [ln[5:].lstrip() for ln in raw_event.split("\n")
                          if ln.startswith("data:")]
            if not data_lines:
                continue
            try:
                msg = json.loads("\n".join(data_lines))
            except json.JSONDecodeError as _e:
                log.warning("忽略异常(omni_agent/mcp.py:165): %s: %s", type(_e).__name__, _e)
                continue
            if isinstance(msg, dict) and ("result" in msg or "error" in msg):
                if rid is not None and msg.get("id") == rid:
                    return msg
                last = msg
        return last

    # ---- JSON-RPC 层(与 stdio 版语义一致) ----
    def _rpc(self, method, params=None, timeout=30):
        with self._lock:            # 串行化请求, 保证 session 语义
            self._id += 1
            rid = self._id
            msg = self._post({"jsonrpc": "2.0", "id": rid,
                              "method": method, "params": params or {}}, timeout=timeout)
        if msg is None:
            raise RuntimeError(f"MCP[{self.name}] {method} 无响应")
        if "error" in msg:
            raise RuntimeError(f"MCP[{self.name}] " + (msg["error"] or {}).get("message", "error"))
        return msg.get("result", {})

    def initialize(self):
        self._rpc("initialize", {"protocolVersion": "2025-03-26",
                                 "capabilities": {},
                                 "clientInfo": {"name": f"omni-agent/{self.name}",
                                                "version": __version__}})
        try:                        # initialized 通知(部分云端实现可能不支持, 容错)
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, timeout=10)
        except RuntimeError as _e:
            log.warning("忽略异常(omni_agent/mcp.py:194): %s: %s", type(_e).__name__, _e)
            pass

    def list_tools(self):
        return self._rpc("tools/list").get("tools", [])

    def call(self, tool, args):
        result = self._rpc("tools/call", {"name": tool, "arguments": args}, timeout=60)
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        out = "\n".join(texts) or json.dumps(result, ensure_ascii=False)[:2000]
        if result.get("isError"):
            out = f"[错误] (server={self.name}) " + out
        return out

    def close(self):
        """按规范尝试 DELETE 终止会话(服务端可不支持, 静默容错)"""
        if not self.session_id:
            return
        try:
            req = urllib.request.Request(
                self.url, headers={"Mcp-Session-Id": self.session_id}, method="DELETE")
            urllib.request.urlopen(req, timeout=5).close()
        except (urllib.error.URLError, OSError) as _e:
            log.warning("忽略异常(omni_agent/mcp.py:217): %s: %s", type(_e).__name__, _e)
            pass


class MCPSSEServer(MCPHttpServer):
    """SSE(Server-Sent Events)长连接传输 —— 持久连接双向通信。

    与 Streamable HTTP 的区别: SSE 保持长连接, 服务端可主动推送;
    客户端通过同一连接发送 JSON-RPC 消息(部分实现用 POST 发送, SSE 接收)。
    兼容模式: 请求仍 POST JSON-RPC, 但 Accept 仅接受 text/event-stream,
    强制走 SSE 流式响应; 支持 Mcp-Session-Id 与自定义 Header。
    """

    transport = "sse"

    def __init__(self, name, cfg):
        super().__init__(name, cfg)
        # SSE 模式: Accept 仅声明 event-stream, 强制流式响应
        self._sse_mode = True

    def _post(self, obj, timeout=30):
        """SSE 传输: POST JSON-RPC, 强制 Accept: text/event-stream"""
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream",
                   "User-Agent": f"omni-agent-MCP-SSE/{__version__}"}
        headers.update(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(obj).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except OSError as _e:
                log.warning("忽略异常(omni_agent/mcp.py:254): %s: %s", type(_e).__name__, _e)
                pass
            raise RuntimeError(f"MCP-SSE[{self.name}] HTTP {e.code}: {detail or e.reason}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"MCP-SSE[{self.name}] 连接失败: {e.reason}")
        with resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            body = resp.read().decode("utf-8", "replace")
            if resp.status == 202 or not body.strip():
                return None
            # SSE 模式: 响应一定是 event-stream
            return self._parse_sse(body, obj.get("id"))


def create_server(name, cfg):
    """传输工厂 —— transport=sse 走 SSE 长连接; transport=http 或配置了
    url 走 Streamable HTTP; 否则 stdio"""
    transport = (cfg.get("transport") or "").strip().lower()
    if transport == "sse":
        return MCPSSEServer(name, cfg)
    if transport == "http" or (cfg.get("url") and not cfg.get("command")):
        return MCPHttpServer(name, cfg)
    return MCPServer(name, cfg)


class MCPManager:
    """MCP 工具命名 mcp__<server>__<tool>, 调用受 Hooks 拦截"""

    def __init__(self, settings):
        self.servers = {}
        self.schemas = []
        for name, cfg in settings.get("mcp_servers", {}).items():
            if name.startswith("_") or not isinstance(cfg, dict):
                continue   # 跳过 _comment 等元数据键
            try:
                srv = create_server(name, cfg)
                srv.initialize()
                tools = srv.list_tools()
                for t in tools:
                    self.schemas.append({"type": "function", "function": {
                        "name": f"mcp__{srv.name}__{t['name']}",
                        "description": f"[MCP:{srv.name}] " + t.get("description", "")[:400],
                        "parameters": t.get("inputSchema", {"type": "object", "properties": {}})}})
                self.servers[srv.name] = srv
                cprint(f"  ✓ MCP Server '{srv.name}'({srv.transport}) 已连接, "
                       f"注册 {len(tools)} 个工具", C.GRAY)
            except Exception as e:
                cprint(f"  ! MCP Server '{name}' 连接失败: {e}", C.RED)

    def call(self, full_name, args):
        try:
            _, srv_name, tool = full_name.split("__", 2)
        except ValueError:
            return f"[错误] 非法 MCP 工具名: {full_name}"
        srv = self.servers.get(srv_name)
        if not srv:
            return f"[错误] MCP Server 未连接: {srv_name}"
        try:
            return srv.call(tool, args)
        except Exception as e:
            return f"[工具执行异常] MCP {full_name}: {e}"

    def close(self):
        for s in self.servers.values():
            s.close()
