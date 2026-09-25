# -*- coding: utf-8 -*-
"""浏览器控制器(Chrome CDP · 纯标准库) v7.0
三级启动链(自动降级):
  ① 用户侧独立可见实例 —— 全新空白临时 profile, 与用户日常主浏览器完全隔离,
     可见窗口支持按需手动登录, 会话结束即焚毁 profile(不留任何登录态/缓存)。
  ② 无头 CDP 实例 —— 同样使用隔离临时 profile, 适用于无 GUI 环境。
  ③ --screenshot 普通截图 —— CDP 不可用时由上层(tool_runner)兜底。
"""
from .logger import get_logger
log = get_logger("browser")
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

from .config import UA
from .settings import S


class MiniWebSocket:
    """极简 WebSocket 客户端, 仅用于连接本机 Chrome DevTools Protocol"""

    def __init__(self, url, timeout=30):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        handshake = (f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
                     f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                     f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(handshake.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP WebSocket 握手失败")
            resp += chunk
        if b"101" not in resp.split(b"\r\n", 1)[0]:
            raise ConnectionError("CDP WebSocket 握手被拒绝")

    def send_text(self, text):
        payload = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text frame
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("CDP 连接中断")
            buf += chunk
        return buf

    def recv_text(self):
        """接收一条完整文本消息(自动处理分片/忽略 ping-pong)"""
        message = b""
        while True:
            b1, b2 = self._recv_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            masked, ln = b2 & 0x80, b2 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._recv_exact(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            data = self._recv_exact(ln)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:      # ping → pong
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode == 0xA:      # pong 忽略
                continue
            if opcode == 0x8:      # close
                raise ConnectionError("CDP 连接已关闭")
            message += data
            if fin:
                return message.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError as _e:
            log.warning("忽略异常(browser.MiniWebSocket.close): %s: %s",
                        type(_e).__name__, _e)


class BrowserController:
    """三级降级浏览器控制器: 独立可见实例(隔离profile·用完即焚) → 无头CDP → 上层普通截图。
    打开页面 → 依序执行交互动作(click/input/scroll/wait) → 全页截图。"""

    BROWSERS = ("chromium-browser", "chromium", "google-chrome",
                "google-chrome-stable", "chrome")
    WIN_PATHS = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    ]
    MAC_PATHS = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]

    # 隔离基线参数: 全新空白实例, 与用户主浏览器 profile/扩展/同步/登录态完全隔离
    ISOLATION_FLAGS = [
        "--no-first-run", "--no-default-browser-check", "--disable-sync",
        "--disable-extensions", "--disable-background-networking",
        "--no-service-autorun", "--password-store=basic",
        "--disable-blink-features=AutomationControlled",
    ]

    @classmethod
    def find_browser(cls):
        for b in cls.BROWSERS:                       # 1. PATH 中查找
            path = shutil.which(b)
            if path:
                return path
        if os.name == "nt":                          # 2. Windows 常见安装路径
            for p in cls.WIN_PATHS:
                if p and os.path.isfile(p):
                    return p
        elif sys.platform == "darwin":               # 3. macOS 常见安装路径
            for p in cls.MAC_PATHS:
                if os.path.isfile(p):
                    return p
        return None

    @staticmethod
    def _gui_available():
        """判断当前环境能否弹出可见浏览器窗口(用户侧独立实例的前提)"""
        if os.name == "nt" or sys.platform == "darwin":
            return True
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    def __init__(self, width=1280, height=900, headed=None):
        self.bin = self.find_browser()
        if not self.bin:
            raise RuntimeError("未找到可用的 Chrome/Chromium 浏览器")
        self.proc = None
        self.ws = None
        self.profile = ""
        self._id = 0
        self.headed = False
        # headed=None(默认): 优先尝试用户侧独立可见实例(需 GUI 环境且配置未关闭);
        # headed=True: 强制可见(按需登录场景); headed=False: 直接无头
        try_headed = (headed if headed is not None
                      else S.get_bool("browser.headed_first", True) and self._gui_available())
        if try_headed:
            try:
                self._launch(width, height, headed=True)
                self._connect()
                self.headed = True
                log.info("已启动用户侧独立浏览器实例(隔离profile: %s)", self.profile)
                return
            except Exception as e:
                log.warning("独立可见实例启动失败, 自动降级无头模式: %s: %s",
                            type(e).__name__, e)
                self._burn()   # 失败也焚毁残留 profile
        self._launch(width, height, headed=False)
        self._connect()

    def _launch(self, width, height, headed):
        """启动一个全新隔离实例: 独立临时 profile + 独立调试端口(用完即焚)"""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.profile = os.path.join(tempfile.gettempdir(),
                                    f"hs_cdp_{uuid.uuid4().hex[:8]}")
        args = [self.bin] + list(self.ISOLATION_FLAGS) + [
            f"--user-agent={S.get_str('browser.user_agent', UA)}",
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile}",
            f"--window-size={width},{height}", "--disable-gpu"]
        if not headed:
            args.insert(1, "--headless=new")
            args.append("--no-sandbox")
        args.append("about:blank")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)

    def _connect(self, retries=20):
        last = None
        for _ in range(retries):
            time.sleep(0.4)
            if self.proc and self.proc.poll() is not None:   # 进程早退, 立即失败
                raise RuntimeError(f"浏览器进程退出(exit={self.proc.returncode})")
            try:
                raw = urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/list", timeout=3).read()
                pages = [t for t in json.loads(raw) if t.get("type") == "page"]
                if pages:
                    self.ws = MiniWebSocket(pages[0]["webSocketDebuggerUrl"])
                    self.cmd("Page.enable")
                    self.cmd("Runtime.enable")
                    return
            except Exception as e:
                last = e
        raise RuntimeError(f"CDP 连接失败: {last}")

    def cmd(self, method, params=None, timeout=30):
        """发送 CDP 命令并等待对应响应(事件消息跳过)"""
        self._id += 1
        rid = self._id
        self.ws.send_text(json.dumps({"id": rid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv_text())
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error'].get('message')}")
                return msg.get("result", {})
        raise TimeoutError(f"CDP {method} 超时")

    def goto(self, url, wait=3.0):
        self.cmd("Page.navigate", {"url": url})
        time.sleep(max(1.0, wait))   # 简化处理: 固定等待加载(含JS渲染)

    def eval_js(self, expr):
        r = self.cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        return r.get("result", {}).get("value")

    def get_html(self):
        """获取 JS 渲染后的完整页面源码(供 web_fetch 反爬降级链使用)"""
        return self.eval_js("document.documentElement.outerHTML") or ""

    def act(self, action):
        """执行单个交互动作: {type: click|input|scroll|wait, selector?, text?, x?, y?, seconds?}"""
        kind = action.get("type", "")
        sel = json.dumps(action.get("selector", ""))
        if kind == "click":
            if action.get("selector"):
                ok = self.eval_js(
                    f"(function(){{var el=document.querySelector({sel});"
                    f"if(!el)return false;el.scrollIntoView({{block:'center'}});"
                    f"el.click();return true;}})()")
                return f"click {action.get('selector')}: {'✓' if ok else ' 未找到元素'}"
            x, y = int(action.get("x", 0)), int(action.get("y", 0))
            for t in ("mousePressed", "mouseReleased"):
                self.cmd("Input.dispatchMouseEvent",
                         {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1})
            return f"click 坐标({x},{y}): ✓"
        if kind == "input":
            text = action.get("text", "")
            ok = self.eval_js(
                f"(function(){{var el=document.querySelector({sel});if(!el)return false;"
                f"el.focus();el.value={json.dumps(text)};"
                f"el.dispatchEvent(new Event('input',{{bubbles:true}}));"
                f"el.dispatchEvent(new Event('change',{{bubbles:true}}));return true;}})()")
            return f"input '{text[:30]}' → {action.get('selector')}: {'✓' if ok else ' 未找到元素'}"
        if kind == "scroll":
            dy = int(action.get("y", 600))
            self.eval_js(f"window.scrollBy(0,{dy})")
            time.sleep(0.3)
            return f"scroll 纵向 {dy}px: ✓"
        if kind == "wait":
            # 可见实例支持长等待(用户手动登录窗口), 无头上限 15s
            cap = 300 if self.headed else 15
            sec = min(float(action.get("seconds", 1)), cap)
            time.sleep(sec)
            return f"wait {sec}s: ✓"
        return f"[跳过] 未知动作类型: {kind}"

    def screenshot(self, path, full_page=False):
        params = {"format": "png"}
        if full_page:
            m = self.cmd("Page.getLayoutMetrics")
            cs = m.get("cssContentSize") or m.get("contentSize", {})
            w = min(int(cs.get("width", 1280)), 1920)
            h = min(int(cs.get("height", 900)), 8000)
            self.cmd("Emulation.setDeviceMetricsOverride",
                     {"width": w, "height": h, "deviceScaleFactor": 1, "mobile": False})
            params["captureBeyondViewport"] = True
        data = self.cmd("Page.captureScreenshot", params, timeout=60).get("data", "")
        with open(path, "wb") as f:
            f.write(base64.b64decode(data))  # 仅传输解码, 不操作图像内容
        return os.path.getsize(path)

    def _burn(self):
        """用完即焚: 终止进程并彻底删除临时 profile(登录态/缓存/历史一并销毁)"""
        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                except OSError:
                    pass
            self.proc = None
        if self.profile:
            shutil.rmtree(self.profile, ignore_errors=True)

    def close(self):
        if self.ws:
            self.ws.close()
            self.ws = None
        self._burn()
