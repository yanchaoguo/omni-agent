# -*- coding: utf-8 -*-
"""终端样式与彩色输出(含 Windows CLI 交互适配)
- ANSI 能力探测: Windows 10+ 通过 SetConsoleMode 开启; 旧控制台/重定向自动降级纯文本
- 编码兜底    : GBK 控制台遇到 emoji 等无法编码字符自动替换, 不再抛 UnicodeEncodeError
- 超时输入    : timed_input() 跨平台实现(POSIX=select / Windows=msvcrt 逐键轮询),
                取代"后台线程读 input()"的旧方案, 根治超时后孤儿线程吞输入的交互假死问题
"""
import os
import sys
import time

IS_WINDOWS = (os.name == "nt")


def _enable_windows_ansi():
    """Windows 10+ 通过 SetConsoleMode 开启 ANSI 虚拟终端; 失败返回 False 以禁用彩色"""
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def supports_color():
    """彩色输出可用性: NO_COLOR 约定 > 非TTY(输出重定向) > Windows ANSI 探测"""
    if os.environ.get("NO_COLOR"):
        return False
    if not (hasattr(sys.stdout, "isatty") and sys.stdout.isatty()):
        return False
    return _enable_windows_ansi() if IS_WINDOWS else True


_COLOR_ON = supports_color()


class C:
    R = "\033[0m"; B = "\033[1m"; DIM = "\033[2m"
    CYAN = "\033[96m"; GREEN = "\033[92m"; YELLOW = "\033[93m"
    RED = "\033[91m"; MAGENTA = "\033[95m"; BLUE = "\033[94m"; GRAY = "\033[90m"


if not _COLOR_ON:   # 不支持 ANSI 的终端(旧版 cmd / 输出重定向)自动降级为纯文本
    for _name in ("R", "B", "DIM", "CYAN", "GREEN", "YELLOW",
                  "RED", "MAGENTA", "BLUE", "GRAY"):
        setattr(C, _name, "")


def cprint(text, color=C.R, end="\n", flush=True):
    """彩色打印; Windows GBK 控制台遇到无法编码的字符(emoji等)自动替换, 不抛异常"""
    s = f"{color}{text}{C.R}"
    try:
        print(s, end=end, flush=flush)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        print(s.encode(enc, errors="replace").decode(enc, errors="replace"),
              end=end, flush=flush)


def timed_input(prompt, timeout, tick_cb=None):
    """跨平台带超时输入, 超时返回 None(tick_cb 每秒回调剩余秒数用于倒计时展示)。
    - POSIX  : select 监听 stdin, 不产生任何后台线程
    - Windows: msvcrt 逐键轮询(支持退格/回车/Ctrl+C), 适配 cmd/PowerShell/Windows Terminal
    """
    if prompt:
        print(prompt, end="", flush=True)
    deadline = time.time() + timeout
    if IS_WINDOWS:
        try:
            import msvcrt
        except ImportError:          # 极端环境兜底: 退化为普通阻塞输入
            try:
                return input().strip()
            except (EOFError, KeyboardInterrupt):
                return ""
        buf = []
        while time.time() < deadline:
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\r", "\n"):
                    print()
                    return "".join(buf).strip()
                if ch == "\x03":     # Ctrl+C
                    raise KeyboardInterrupt
                if ch == "\x08":     # Backspace
                    if buf:
                        buf.pop()
                        print("\b \b", end="", flush=True)
                    continue
                buf.append(ch)
                print(ch, end="", flush=True)
            if tick_cb:
                tick_cb(int(deadline - time.time()))
            time.sleep(0.05)
        return None
    import select
    while True:
        remain = deadline - time.time()
        if remain <= 0:
            return None
        if tick_cb:
            tick_cb(int(remain))
        ready, _, _ = select.select([sys.stdin], [], [], min(1.0, remain))
        if ready:
            line = sys.stdin.readline()
            return line.strip() if line else ""
