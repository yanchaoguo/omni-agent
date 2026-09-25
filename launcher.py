#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""omni-agent Windows 启动器(v3.8, PyInstaller 打包入口)
进入后由用户确认当前对话窗口模式:
  [1] CLI  —— 终端交互式 REPL
  [2] WEB  —— 本机启动网页版驾驶舱, 自动弹出系统默认浏览器
打包方式(在 Windows 环境执行): build_windows.bat  →  dist/omni-agent.exe
"""
import os
import socket
import sys
import threading
import time
import webbrowser


def resolve_mode(ans):
    """解析用户模式选择: 1/c/cli -> cli; 空/2/w/web -> web; 其余返回空重问"""
    a = (ans or "").strip().lower()
    if a in ("1", "c", "cli"):
        return "cli"
    if a in ("", "2", "w", "web"):
        return "web"
    return ""


def free_port(preferred=3000):
    """优先使用 3000 端口, 被占用时自动分配空闲端口"""
    try:
        with socket.socket() as s:
            s.bind(("127.0.0.1", preferred))
        return preferred
    except OSError:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


def default_workdir():
    # 与 config.GLOBAL_DIR 修复保持一致 —— 默认落在 ~/.haisnap 下,
    # 避免 CWD 相对路径导致换目录启动后历史项目"丢失"
    root = os.environ.get(
            "HAISNAP_PROJECTS_ROOT",
            os.path.join(os.environ.get(
                "HAISNAP_HOME",
                os.path.join(os.path.expanduser("~"), ".haisnap")),
                "haisnap_projects"))
    d = os.path.join(root, "project_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(d, exist_ok=True)
    return d


def _pyinstaller_import_anchor():
    """PyInstaller 静态分析锚点(永不实际调用): 显式列出全部惰性导入的子模块,
    确保打包时被完整收集, 修复 exe 运行到图片生成才报 ModuleNotFoundError 的问题"""
    import omni_agent.browser        # noqa: F401
    import omni_agent.scheduler      # noqa: F401
    import omni_agent.checkpoint     # noqa: F401
    import omni_agent.skills         # noqa: F401
    import omni_agent.mcp            # noqa: F401
    import omni_agent.connectors     # noqa: F401
    import omni_agent.lessons        # noqa: F401
    import omni_agent.similarity     # noqa: F401
    import omni_agent.unifuncs       # noqa: F401
    import omni_agent.sources        # noqa: F401


PREFLIGHT_MODULES = (
    "browser", "scheduler", "checkpoint",
    "skills", "mcp", "connectors", "lessons", "similarity",
    "unifuncs", "sources", "tool_runner", "webserver", "agent")


def preflight_check(log=None):
    """启动自检: 逐个验证核心子模块可导入, 返回缺失列表 [(模块名, 异常)]。
    在进入交互前提前暴露打包遗漏, 而非等到工具执行时才报错"""
    import importlib
    missing = []
    for m in PREFLIGHT_MODULES:
        try:
            importlib.import_module("omni_agent." + m)
        except Exception as e:
            missing.append((m, f"{type(e).__name__}: {e}"))
            if log:
                log.error("模块预检失败 omni_agent.%s: %s", m, e)
    return missing


def main():
    # 用户级环境变量最先注入(同名覆盖), 再加载其余配置
    from omni_agent.envstore import EnvStore
    n = EnvStore.apply()
    from omni_agent.config import __version__
    from omni_agent.logger import get_logger, new_tracker
    log = get_logger("launcher")
    new_tracker("launch")
    _missing = preflight_check(log)
    if _missing:
        print("  !! 模块完整性预检失败, 以下子模块缺失(打包遗漏或文件损坏):")
        for _m, _err in _missing:
            print(f"     - omni_agent.{_m}  ({_err})")
        print("  !! 请使用项目内 omni-agent.spec 重新打包, 或还原缺失文件后重试\n")
    print("\n  ================================================")
    print(f"   omni-agent v{__version__} · 北京海新智能")
    print(f"   用户级环境变量已注入 {n} 项 (~/.haisnap/env.json)")
    print("  ================================================\n")

    mode = ""
    while not mode:
        try:
            ans = input("  请选择对话窗口模式  [1] CLI 终端  [2] WEB 网页(默认): ")
        except (EOFError, KeyboardInterrupt):
            print("\n  已取消启动")
            return
        mode = resolve_mode(ans)
        if not mode:
            print("  ! 无效输入, 请输入 1 或 2")
    log.info("launcher mode=%s", mode)
    workdir = default_workdir()

    if mode == "cli":
        from omni_agent.cli import main as cli_main
        try:
            cli_main(["-w", workdir])
        except KeyboardInterrupt:
            print("\n  已退出")
        return

    # WEB 模式: 本机监听 + 延时弹出系统默认浏览器
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    print(f"\n  ✦ 网页版驾驶舱启动中: {url}")
    print("  ✦ 即将自动打开默认浏览器(Ctrl+C 停止服务)\n")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    from omni_agent.webserver import serve
    serve(host="127.0.0.1", port=port, workdir=workdir)


if __name__ == "__main__":
    if getattr(sys, "frozen", False):   # PyInstaller 冻结环境: 保证工作目录可写
        os.chdir(os.path.expanduser("~"))
    main()