# -*- coding: utf-8 -*-
"""权限审批门控 v3(参考 Codex CLI shell_environment_policy 设计):
- shlex.split() 解析命令, 按 && || ; | & 拆分为独立管道段, 每段独立权限检查
- 检测 && || ; | $() ` 等元字符, 命令替换($()/反引号)内容递归检查
- 环境变量注入白名单管控: LD_PRELOAD/BASH_ENV 等注入载体直接拒绝, 白名单外需确认
- 分级放行: 工作区内文件写入/删除直接通过; 工作区外需确认; 危险命令直接拒绝
- v3.7 新增「脚本内容纵深防御」: 恶意代码写入 .sh/.py 等文件再执行时,
  ① write_file/edit_file 写入阶段扫描内容, 命中危险模式直接拒绝;
  ② bash/python/sh 执行脚本文件前读取文件内容二次扫描, 防绕过。
"""
import os
import re
import shlex

from .ui import C, cprint

# ---- 命令黑名单(命中即拒绝, yolo 模式亦不放行) ----
DENY_PATTERNS = [
    r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(/|~)(\s|$|/)",  # rm -rf / 或 ~
    r"\bmkfs(\.\w+)?\b", r"\bdd\s+if=.*of=/dev/", r":\(\)\s*\{.*\}\s*;\s*:",   # 格式化/写盘/fork炸弹
    r"\bshutdown\b", r"\breboot\b", r"\bhalt\b", r"\bpoweroff\b",
    r">\s*/dev/sd", r"\bchmod\s+(-[a-zA-Z]+\s+)*0?777\s+/(\s|$)",
    r"\b(curl|wget)\b[^|;]*\|\s*(ba|z|da)?sh\b",                               # 管道直灌 shell
    # v4.1 Windows 破坏性命令
    r"(?i)\bformat\s+[a-z]:",                                   # 格式化盘符
    r"(?i)\b(del|rd|rmdir)\s+(/[sqfa]\s+)*[a-z]:\\\\?(\s|$)",   # 删除盘符根目录
    r"(?i)\bdiskpart\b", r"(?i)\bcipher\s+/w",                  # 磁盘分区/擦除
    r"(?i)\bvssadmin\s+delete\b",                               # 删除卷影副本
]

# ---- 脚本文件内容危险模式(写入时 + 执行前 双重扫描) ----
# 覆盖 shell 与 python 两类载体: 破坏性删除/磁盘覆写/fork炸弹/反弹shell/编码执行注入
SCRIPT_CONTENT_DENY = [
    (r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r)\s+(/|~|\$HOME)(\s|$|/|['\"])",
     "破坏性删除根目录/家目录"),
    (r"\bmkfs(\.\w+)?\b", "格式化磁盘"),
    (r"\bdd\s+if=.*of=/dev/", "覆写磁盘设备"),
    (r":\(\)\s*\{.*\}\s*;\s*:", "shell fork 炸弹"),
    (r"\b(shutdown|reboot|poweroff)\b\s", "关机/重启指令"),
    (r">\s*/dev/sd[a-z]", "重定向写盘设备"),
    (r"\b(curl|wget)\b[^|;\n]*\|\s*(ba|z|da)?sh\b", "远程脚本管道直灌 shell"),
    (r"\bshutil\.rmtree\s*\(\s*['\"](/|/etc|/usr|/var|/home|/root|/bin|/lib)['\"/]",
     "python 递归删除系统目录"),
    (r"os\.system\s*\(\s*['\"][^'\"]*rm\s+-[rf]{2}\s+/", "python 调用 rm -rf /"),
    (r"subprocess\.(run|call|Popen|check_output)\s*\([^)]*rm\s+-[rf]{2}\s+/",
     "python subprocess 调用 rm -rf /"),
    # v3.7.1: 列表参数形式 subprocess.run(['rm', '-rf', '/']) 绕过防御
    (r"subprocess\.(run|call|Popen|check_output)\s*\(\s*\[[^\]]*['\"]rm['\"]"
     r"[^\]]*['\"]-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*['\"][^\]]*['\"](/|~)[^'\"]*['\"]",
     "python subprocess 列表参数调用 rm -rf 系统路径"),
    (r"os\.exec[lv]p?e?\s*\(\s*['\"](rm|mkfs|dd)['\"]", "python os.exec 族调用破坏性命令"),
    (r"while\s+(True|1)\s*:\s*(\n\s*)?os\.fork\s*\(", "python fork 炸弹"),
    (r"\b(bash|sh)\s+-i\s+[^\n]*(/dev/tcp/|>&\s*/dev/tcp)", "反弹 shell(/dev/tcp)"),
    (r"\bnc(at)?\s+(-[a-z]*e[a-z]*\s+)(/bin/)?(ba)?sh\b", "nc 反弹 shell"),
    (r"pty\.spawn\s*\(\s*['\"]/bin/(ba)?sh", "python pty 反弹 shell"),
    (r"socket\.socket\([^)]*\)[\s\S]{0,200}(connect\s*\()[\s\S]{0,200}"
     r"(os\.dup2|subprocess\.call\s*\(\s*\[?['\"]/bin/)", "python socket 反弹 shell"),
    (r"(eval|exec)\s*\(\s*(base64\.b64decode|compile\s*\(|__import__\s*\()",
     "编码/动态导入执行注入"),
    (r"__import__\s*\(\s*['\"]os['\"]\s*\)\s*\.system", "混淆式 os.system 调用"),
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*0?777\s+/(\s|$|['\"])", "全局提权系统根目录"),
    (r"echo\s+[^\n]*>>?\s*/etc/(passwd|shadow|sudoers|crontab)", "篡改系统认证/计划任务文件"),
    (r"\bcrontab\s+-r\b", "清空计划任务"),
    (r"\bhistory\s+-c\b[^\n]*&&[^\n]*rm\b", "清痕迹+删除组合"),
]

# 需做内容扫描的可执行脚本扩展名
SCRIPT_EXTS = {".sh", ".bash", ".zsh", ".py", ".pl", ".rb", ".js", ".mjs", ".cjs"}

# v3.7.1: 扩展名 -> 语言载体标识(拒绝理由中标注, 便于审计定位)
_LANG_BY_EXT = {
    ".py": "python", ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".pl": "perl", ".rb": "ruby",
}

# ---- 命令分级(按管道段首命令分类) ----
READ_ONLY_CMDS = {
    "ls", "cat", "head", "tail", "pwd", "echo", "printf", "grep", "egrep", "fgrep",
    "find", "wc", "which", "whoami", "date", "df", "du", "ps", "env", "printenv",
    "uname", "stat", "file", "diff", "sort", "uniq", "tree", "basename", "dirname",
    "realpath", "cut", "tr", "awk", "md5sum", "sha1sum", "sha256sum", "hostname",
    "id", "uptime", "type", "test", "true", "false", "xargs", "less", "more", "sleep",
    # v4.1 Windows cmd.exe 内置/自带只读命令(文件查找 findstr/where 等)
    "dir", "findstr", "where", "ver", "vol", "fc", "comp", "tasklist", "systeminfo",
    "ipconfig", "chcp", "timeout", "whereis", "certutil",
}
WRITE_FS_CMDS = {  # 文件系统写入/删除: 路径全部在工作区内 → 直接放行
    "touch", "mkdir", "rmdir", "cp", "mv", "rm", "ln", "sed", "tee", "truncate",
    "patch", "unzip", "zip", "tar", "gzip", "gunzip", "chmod", "chown", "install", "split",
    # v4.1 Windows 文件系统命令
    "copy", "move", "del", "ren", "rename", "md", "rd", "xcopy", "robocopy", "attrib",
    "expand", "makecab",
}
NETWORK_CMDS = {  # 网络访问: 直接放行(管道直灌 shell 已被黑名单拦截)
    "curl", "wget", "ping", "git", "pip", "pip3", "npm", "npx", "yarn", "pnpm",
}
INTERP_CMDS = {"python", "python3", "node", "make"}     # 开发解释器: 脚本内容扫描后放行
SHELL_CMDS = {"bash", "sh", "zsh", "dash"}              # 嵌套 shell: -c 递归检查/脚本扫描
CONFIRM_ALWAYS = {  # 高敏感命令: 永远需要人工确认
    "sudo", "su", "docker", "systemctl", "service", "apt", "apt-get", "yum", "dnf",
    "brew", "kill", "pkill", "killall", "crontab", "ssh", "scp", "nc", "ncat",
    "telnet", "mount", "umount", "chroot", "eval", "exec", "source",
    # v4.1 Windows 高敏感命令
    "powershell", "pwsh", "wmic", "reg", "regedit", "sc", "net", "netsh",
    "taskkill", "schtasks", "runas", "bcdedit", "cipher", "icacls", "takeown",
}

# ---- 环境变量注入策略(参考 Codex CLI shell_environment_policy) ----
ENV_DENY = {  # 代码注入载体 → 直接拒绝
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH", "BASH_ENV", "ENV", "IFS", "PROMPT_COMMAND",
    "PYTHONSTARTUP", "GIT_SSH_COMMAND", "GIT_PAGER", "PERL5OPT", "NODE_OPTIONS",
}
ENV_ALLOW_EXACT = {  # 白名单(精确): 直接放行
    "LANG", "TZ", "TERM", "TMPDIR", "NO_COLOR", "FORCE_COLOR", "PORT", "DEBUG",
    "COLUMNS", "LINES", "PAGER", "EDITOR", "CI",
}
ENV_ALLOW_PREFIX = (  # 白名单(前缀): 直接放行(ENV_DENY 优先级更高)
    "LC_", "PYTHON", "NODE_ENV", "NPM_CONFIG_", "HAISNAP_", "GIT_AUTHOR_",
    "GIT_COMMITTER_", "FLASK_", "DJANGO_",
)

_CONTROL_OPS = {"&&", "||", ";", "|", "&", ";;", "|&"}
_REDIRECT_OPS = {">", ">>", "<", "<<", "2>", "&>", ">&"}
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CMD_SUBST = re.compile(r"\$\(([^()]{0,500})\)|`([^`]{0,500})`")
_RANK = {"allow": 0, "confirm": 1, "deny": 2}
MAX_SCRIPT_SCAN_BYTES = 512 * 1024   # 脚本内容扫描上限(512KB, 防大文件拖慢)


class Permission:
    """三级审批: allow(自动放行) / confirm(询问) / deny(拒绝)"""

    def __init__(self, yolo=False, headless=False, scheduled=False):
        self.yolo = yolo or scheduled   # 定时任务模式: 一路畅通直至交付
        self.headless = headless
        self.scheduled = scheduled
        self.session_allow = set()      # 本次会话已授权的命令头

    # ---- 主入口: bash 命令审批 ----
    def check_bash(self, cmd, workdir=None):
        """返回 (verdict, reason): deny 拒绝 / confirm 需确认 / allow 放行"""
        c = (cmd or "").strip()
        if not c:
            return "allow", ""
        wd = os.path.realpath(workdir or os.getcwd())
        verdict, reason = self._check_cmdline(c, wd, depth=0)
        if verdict == "deny":
            return "deny", reason or "命中危险命令黑名单, 已强制拦截"
        if self.yolo:
            return "allow", ""
        return verdict, reason

    # ---- v3.7 主入口: 文件写入审批(write_file/edit_file/multi_edit 统一走这里) ----
    def check_file_write(self, path, content, workdir=None):
        """脚本文件写入的纵深防御 + 工作区分级:
        ① 目标为可执行脚本(.sh/.py 等)且内容命中危险模式 → deny(yolo 亦不放行);
        ② 目标路径在工作区内 → allow(直接通过, 无需人工审批);
        ③ 工作区外写入 → confirm(需人工确认)。
        返回 (verdict, reason)"""
        wd = os.path.realpath(workdir or os.getcwd())
        ext = os.path.splitext(path or "")[1].lower()
        if ext in SCRIPT_EXTS:
            v, r = self.scan_script_content(content or "", os.path.basename(path))
            if v == "deny":
                return "deny", r
        rp = os.path.realpath(path if os.path.isabs(path)
                              else os.path.join(wd, path or ""))
        if rp == wd or rp.startswith(wd + os.sep):
            return "allow", ""            # 工作区内文件系统写入: 直接通过
        if self.yolo:
            return "allow", ""
        return "confirm", f"写入路径超出工作区, 需人工确认: {path}"

    # ---- 脚本内容危险模式扫描 ----
    @staticmethod
    def scan_script_content(content, filename=""):
        """对脚本文本做危险模式匹配, 命中即 deny。用于:
        写入 .sh/.py 时的前置拦截 + bash/python 执行脚本文件前的二次扫描。"""
        body = (content or "")[:MAX_SCRIPT_SCAN_BYTES]
        lang = _LANG_BY_EXT.get(os.path.splitext(filename or "")[1].lower(), "script")
        for pat, desc in SCRIPT_CONTENT_DENY:
            if re.search(pat, body):
                return "deny", (f"脚本 {filename or '(内容)'} ({lang} 载体) "
                                f"命中危险模式[{desc}], 已强制拦截")
        return "allow", ""

    def _scan_script_file(self, path, wd):
        """执行前扫描脚本文件内容(存在才扫, 读取失败按需确认处理)"""
        fp = os.path.realpath(path if os.path.isabs(path)
                              else os.path.join(wd, os.path.expanduser(path)))
        if not os.path.isfile(fp):
            return "allow", ""   # 文件不存在: 交由执行期报错, 不阻塞
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                body = f.read(MAX_SCRIPT_SCAN_BYTES)
        except OSError:
            return "confirm", f"脚本文件不可读, 需人工确认: {path}"
        v, r = self.scan_script_content(body, os.path.basename(fp))
        if v == "deny":
            return "deny", r
        if not (fp == wd or fp.startswith(wd + os.sep)):
            return "confirm", f"执行工作区外脚本需确认: {path}"
        return "allow", ""

    # ---- 黑名单双重扫描(原始字符串 + shlex 规范化token) ----
    @staticmethod
    def _deny_scan(text):
        """对文本做危险命令黑名单匹配, 命中返回 True"""
        for pat in DENY_PATTERNS:
            if re.search(pat, text):
                return True
        return False

    # ---- 命令行整体检查(可递归: 命令替换 / bash -c) ----
    def _check_cmdline(self, cmd, wd, depth):
        if depth > 3:
            return "confirm", "命令嵌套层级过深"
        # ① 原始字符串黑名单(先于分段, 防止管道拆分绕过)
        if self._deny_scan(cmd):
            return "deny", "命中危险命令黑名单, 已强制拦截"
        worst, why = "allow", ""
        # ② 命令替换 $() / `` 元字符: 提取内部命令递归检查
        for m in _CMD_SUBST.finditer(cmd):
            inner = (m.group(1) or m.group(2) or "").strip()
            if inner:
                v, r = self._check_cmdline(inner, wd, depth + 1)
                if _RANK[v] > _RANK[worst]:
                    worst, why = v, f"命令替换段[{inner[:40]}]: {r or '需确认'}"
                if worst == "deny":
                    return worst, why
        # ③ shlex 解析 + 控制符(&& || ; | &)分段
        segments, redirects, parse_ok = self._split_segments(cmd)
        if not parse_ok:
            return self._merge(worst, "confirm", why, "命令解析失败(引号未闭合?), 需人工确认")
        # ④ 重定向目标写入位置检查(> >> 视为文件写入)
        for target in redirects:
            if not self._in_workspace(target, wd):
                worst, why = self._merge(worst, "confirm", why,
                                         f"重定向写入超出工作区: {target}")
        # ⑤ 每个管道段独立权限检查
        for seg in segments:
            v, r = self._check_segment(seg, wd, depth)
            worst, why = self._merge(worst, v, why, r)
            if worst == "deny":
                return worst, why
        return worst, why

    @staticmethod
    def _merge(v1, v2, r1, r2):
        return (v1, r1) if _RANK[v1] >= _RANK[v2] else (v2, r2)

    # ---- shlex 解析 + 分段 ----
    @staticmethod
    def _split_segments(cmd):
        """返回 (管道段列表[token列表], 重定向目标列表, 是否解析成功)"""
        try:
            lex = shlex.shlex(cmd, posix=True, punctuation_chars="();|&<>")
            lex.whitespace_split = True
            tokens = list(lex)
        except ValueError:
            return [], [], False
        segments, cur, redirects = [], [], []
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t in _CONTROL_OPS or (t and set(t) <= set("();|&") and t not in _REDIRECT_OPS):
                if cur:
                    segments.append(cur)
                    cur = []
            elif t in _REDIRECT_OPS or re.fullmatch(r"\d?>{1,2}|\d?>&\d?", t or ""):
                if i + 1 < len(tokens) and tokens[i + 1] not in _CONTROL_OPS:
                    redirects.append(tokens[i + 1])
                    i += 1
            else:
                cur.append(t)
            i += 1
        if cur:
            segments.append(cur)
        return segments, redirects, True

    @staticmethod
    def _first_script_arg(tokens):
        """从解释器参数中定位首个疑似脚本文件的参数(跳过选项与 -m 模块)"""
        skip_next = False
        for t in tokens:
            if skip_next:
                skip_next = False
                continue
            if t in ("-m", "-c", "--", "-e"):
                skip_next = (t in ("-m", "-c", "-e"))
                if t == "-c":
                    return None   # -c 内联代码由上层递归逻辑处理
                continue
            if t.startswith("-"):
                continue
            return t
        return None

    # ---- 单个管道段检查 ----
    def _check_segment(self, tokens, wd, depth):
        verdict, reason = "allow", ""
        # shlex 已剥离引号 → 规范化重组后二次黑名单扫描,
        # 拦截 rm "-rf" '/'、rm $'-rf' / 等引号混淆绕过
        normalized = " ".join(tokens)
        if self._deny_scan(normalized):
            return "deny", "命中危险命令黑名单(shlex 规范化后), 已强制拦截"
        # 剥离前置环境变量赋值 NAME=VALUE, 逐一做白名单管控
        while tokens and _ENV_ASSIGN.match(tokens[0]):
            name = tokens[0].split("=", 1)[0]
            v, r = self._check_env(name)
            if v == "deny":
                return "deny", r
            verdict, reason = self._merge(verdict, v, reason, r)
            tokens = tokens[1:]
        if not tokens:
            return verdict, reason
        head = os.path.basename(tokens[0])
        # v4.9-fix: cd/pushd/popd 目录切换 — 目标在工作区内(或无参数)直接放行,
        # 修复 `cd "<workdir>" && ls -laR` 等无害组合命令被误判为"未知命令需确认"
        if head in ("cd", "pushd", "popd"):
            target = next((t for t in tokens[1:] if not t.startswith("-")), "")
            if not target or target in ("-", "~") or self._in_workspace(target, wd):
                return verdict, reason
            return self._merge(verdict, "confirm", reason,
                               f"目录切换超出工作区, 需确认: {head} {target}")
        # eval/exec 拼接参数后递归深检(shlex 解析处理 eval "rm -rf /" 等)
        if head in ("eval", "exec") and len(tokens) > 1:
            inner = " ".join(tokens[1:])
            v, r = self._check_cmdline(inner, wd, depth + 1)
            if v == "deny":
                return "deny", r or f"{head} 内部命令命中黑名单"
            return self._merge(verdict, "confirm", reason,
                               f"高敏感命令需人工确认: {head}({inner[:40]})")
        # 包装命令(xargs/nohup/nice/stdbuf/setsid)穿透检查真实命令,
        # 拦截 `echo / | xargs rm -rf` 类绕过
        if head in ("xargs", "nohup", "nice", "stdbuf", "setsid") and len(tokens) > 1:
            inner = tokens[1:]
            inner_head = next((os.path.basename(t) for t in inner
                               if not t.startswith("-")), "")
            # xargs: 实际参数来自 stdin, 无法静态审计路径 →
            # 包装破坏性删除(rm 递归强制)/格式化/写盘 直接拒绝
            if head == "xargs":
                flags = " ".join(inner)
                if inner_head == "rm" and re.search(
                        r"-[a-zA-Z]*r[a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*r", flags):
                    return "deny", "xargs 包装 rm -rf(参数来自stdin不可审计), 已强制拦截"
                if inner_head in ("mkfs", "dd", "shred"):
                    return "deny", f"xargs 包装破坏性命令 {inner_head}, 已强制拦截"
                if inner_head in WRITE_FS_CMDS:
                    return self._merge(verdict, "confirm", reason,
                                       f"xargs 包装文件写入命令(路径来自stdin): {inner_head}")
            rest = [t for t in inner if not t.startswith("-")]
            if rest:
                v, r = self._check_segment(rest, wd, depth + 1)
                return self._merge(verdict, v, reason,
                                   r or f"{head} 包装命令: {rest[0]}")
        if head in CONFIRM_ALWAYS:
            return self._merge(verdict, "confirm", reason, f"高敏感命令需人工确认: {head}")
        if head in SHELL_CMDS:  # bash -c "..." → 递归检查内部命令
            if "-c" in tokens:
                idx = tokens.index("-c")
                inner = tokens[idx + 1] if idx + 1 < len(tokens) else ""
                v, r = self._check_cmdline(inner, wd, depth + 1)
                return self._merge(verdict, v, reason, r or f"嵌套 shell: {inner[:40]}")
            # bash script.sh → 执行前扫描脚本文件内容(防恶意代码落盘绕过)
            script = self._first_script_arg(tokens[1:])
            if script:
                v, r = self._scan_script_file(script, wd)
                return self._merge(verdict, v, reason, r)
            return self._merge(verdict, "confirm", reason, f"交互式嵌套 shell 需确认: {head}")
        if head in INTERP_CMDS:
            # python evil.py / node evil.js → 执行前扫描脚本文件内容
            if "-c" in tokens or "-e" in tokens:   # 内联代码: 整体做内容扫描
                flag = "-c" if "-c" in tokens else "-e"
                idx = tokens.index(flag)
                inline = tokens[idx + 1] if idx + 1 < len(tokens) else ""
                # 内联代码同时过危险命令黑名单(python -c "import os;os.system('rm -rf /')")
                if self._deny_scan(inline):
                    return "deny", f"{head} {flag} 内联代码命中危险命令黑名单, 已强制拦截"
                v, r = self.scan_script_content(inline, f"{head} {flag} 内联代码")
                return self._merge(verdict, v, reason, r)
            script = self._first_script_arg(tokens[1:])
            if script and os.path.splitext(script)[1].lower() in SCRIPT_EXTS:
                v, r = self._scan_script_file(script, wd)
                return self._merge(verdict, v, reason, r)
            return verdict, reason
        if head in READ_ONLY_CMDS or head in NETWORK_CMDS:
            return verdict, reason   # 只读 / 网络访问: 直接放行
        if head in WRITE_FS_CMDS:    # 文件写入/删除: 路径都在工作区内 → 放行
            for t in tokens[1:]:
                if t.startswith("-"):
                    continue
                if not self._in_workspace(t, wd):
                    return self._merge(verdict, "confirm", reason,
                                       f"文件操作路径超出工作区: {t}")
            return verdict, reason
        # 直接执行脚本(./evil.sh 或 /path/x.py) → 内容扫描
        if "/" in tokens[0] and os.path.splitext(tokens[0])[1].lower() in SCRIPT_EXTS:
            v, r = self._scan_script_file(tokens[0], wd)
            if v != "allow":
                return self._merge(verdict, v, reason, r)
            return self._merge(verdict, "allow", reason, "")
        if head in self.session_allow:
            return verdict, reason   # 用户本会话已授权的命令头
        return self._merge(verdict, "confirm", reason, f"未知命令需确认: {head}")

    # ---- 环境变量白名单管控 ----
    @staticmethod
    def _check_env(name):
        up = name.upper()
        if up in ENV_DENY:
            return "deny", f"危险环境变量注入已拦截: {name}"
        if up in ENV_ALLOW_EXACT or any(up.startswith(p) for p in ENV_ALLOW_PREFIX):
            return "allow", ""
        return "confirm", f"环境变量 {name} 不在白名单, 需确认"

    # ---- 路径是否位于工作区内 ----
    @staticmethod
    def _in_workspace(p, wd):
        if not p or p.startswith("-"):
            return True
        # v4.1 Windows: /s /q /y 等单字母斜杠开关不按路径处理(避免误判绝对路径)
        if os.name == "nt" and re.fullmatch(r"/[A-Za-z]{1,4}(:\S*)?", p):
            return True
        if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", p):
            return True   # URL 不按文件路径处理
        rp = os.path.realpath(os.path.join(wd, os.path.expanduser(p)))
        return rp == wd or rp.startswith(wd + os.sep)

    def ask(self, tool, detail):
        """交互式审批; 无头模式下默认放行写文件类、拒绝需确认的 bash"""
        if self.yolo:
            return True
        if self.headless:
            return tool != "bash"  # 无头非yolo: 文件操作放行, 敏感bash拒绝
        cprint("\n  ┌─ 审批请求 ─────────────────────────", C.YELLOW)
        print(f"{C.YELLOW}  │ 工具: {C.RED}{tool}{C.R}", flush=True)
        for line in str(detail).splitlines()[:8]:
            cprint(f"  │ {line}", C.YELLOW)
        cprint("  └─ 允许执行? [y]是 / [n]否 / [a]本会话始终允许 / [o]YOLO模式: ", C.YELLOW, end="")
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if ans == "a":
            head = str(detail).split()[0] if str(detail).split() else ""
            if head:
                self.session_allow.add(head)
            return True
        if ans in ("o", "yolo"):   # YOLO — 本会话后续审批全部自动放行
            self.yolo = True
            cprint("  ⚡ YOLO 模式已开启: 后续审批自动放行(危险命令黑名单仍强制拦截)", C.YELLOW)
            return True
        return ans in ("y", "yes", "")