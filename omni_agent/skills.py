# -*- coding: utf-8 -*-
"""技能系统(Skills): 技能=目录+SKILL.md, 语义匹配自动加载, 支持一键安装
新增启动目录(launcher 模式) skills/ 扫描
安装支持 npx 方式
安装支持 zip 上传、GitHub owner/repo 简写、ModelScope 技能 API 下载;
      npx 正则兼容 --skill 参数(多技能指定)与 -g/-y/-a 等标志位
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile

from .config import GLOBAL_DIR, SETTINGS_DIR
from .logger import get_logger
from .similarity import TextSimilarity

log = get_logger("skills")

SKILL_MATCH_SIM = 0.4   # 意图词元覆盖率阈值(修复: 原引用了未定义常量导致 NameError)

# 进程启动时刻的工作目录(launcher 目录)。默认模式下 Agent 的 workdir 是
# 自动新建的独立项目目录, 与启动目录不同, 导致 os.path.join(workdir, "skills")
# 永远为空目录 —— 此处固化启动目录, 其下 skills/ 一并纳入扫描。
LAUNCH_DIR = os.path.realpath(os.getcwd())

# npx 安装命令解析(兼容 npx --yes / npx -y 前缀, 以及 --skill name 参数)
# 匹配: npx skills add <target> [--skill xxx] [--skill yyy] [-g] [-y] [-a agent]
# target 可以是: owner/repo, URL, 本地路径, 或纯技能名
NPX_RE = re.compile(
    r"^npx\s+(?:--yes\s+|-y\s+)?skills\s+add\s+(.+?)(?:\s+--(\w+)[=\s]+(\S+))?\s*$",
    re.I)


class SkillsManager:
    """loaded 集合用于防止同一技能在一次会话中被重复加载注入上下文"""

    def __init__(self, workdir, loaded=None):
        cand = [os.path.join(workdir, SETTINGS_DIR, "skills"),
                os.path.join(workdir, "skills"),
                os.path.join(LAUNCH_DIR, "skills"),   # launcher 目录技能
                os.path.join(GLOBAL_DIR, "skills")]
        # realpath 去重(workdir 可能恰好就是 LAUNCH_DIR)
        seen = set()
        self.dirs = []
        for d in cand:
            rp = os.path.realpath(d)
            if rp not in seen:
                seen.add(rp)
                self.dirs.append(d)
        for d in (self.dirs[0], self.dirs[-1]):
            os.makedirs(d, exist_ok=True)
        # 支持继承已启用技能集合(修复 _rebind_workdir 后勾选技能失效)
        self.loaded = set(loaded or ())

    def scan(self):
        skills = []
        seen = set()
        for base in self.dirs:
            if not os.path.isdir(base):
                continue
            for name in sorted(os.listdir(base)):
                if name in seen:
                    continue
                sp = os.path.join(base, name, "SKILL.md")
                if os.path.isfile(sp):
                    with open(sp, encoding="utf-8", errors="replace") as f:
                        head = f.read(1200)
                    skills.append({"name": name, "path": sp, "brief": head})
                    seen.add(name)
        return skills

    def match(self, intent):
        """任务语义与技能描述匹配打分(复用通用工具类 TextSimilarity,
        与 LessonStore 知识归并同源算法), 按意图词元覆盖率排序返回最相关技能"""
        scored = []
        for s in self.scan():
            doc = s["name"].lower() + " " + s["brief"].lower()
            score = TextSimilarity.overlap(intent, doc)
            if score >= SKILL_MATCH_SIM:
                scored.append((score, s))
        return [s for _, s in sorted(scored, key=lambda x: -x[0])]

    def install(self, source):
        """一键安装: git URL / GitHub owner/repo / 本地目录 / zip 文件 /
        npx 命令 / ModelScope 技能 URL
        → 全局技能库, 安装成功后自动出现在技能列表(scan 即时可见)"""
        source = (source or "").strip()
        log.info("skill install: source=%s", source[:150])
        if not source:
            return "[错误] 安装来源不能为空"

        # npx 命令解析(兼容 --skill 参数)
        m = NPX_RE.match(source)
        if m:
            return self._install_npx(source)

        dst_root = self.dirs[-1]

        # ModelScope 技能 URL → API 下载
        ms_m = re.match(
            r"^https?://(?:www\.)?modelscope\.cn/skills/@([\w.-]+)/(\S+)$",
            source)
        if ms_m:
            return self._install_modelscope(ms_m.group(1), ms_m.group(2))

        # GitHub owner/repo 简写 (不含 / 也不含 : 的纯名称不匹配)
        gh_shorthand = re.match(r"^([\w.-]+)/([\w.-]+)(?:/(\S+))?$", source)
        if gh_shorthand and not os.path.exists(source):
            owner, repo = gh_shorthand.group(1), gh_shorthand.group(2)
            subpath = gh_shorthand.group(3) or ""
            return self._install_github(owner, repo, subpath, dst_root)

        # git URL
        if re.match(r"^(https?://|git@)", source):
            name = re.sub(r"\.git$", "", source.rstrip("/").rsplit("/", 1)[-1])
            dst = os.path.join(dst_root, name)
            r = subprocess.run(["git", "clone", "--depth", "1", source, dst],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                return f"[失败] git clone: {r.stderr[:300]}"
            # 如果仓库根目录无 SKILL.md, 尝试在 skills/ 子目录中安装
            return self._finalize_repo_install(dst, name, dst_root)

        # zip 文件安装
        if source.lower().endswith(".zip") and os.path.isfile(source):
            return self._install_zip(source, dst_root)

        # 本地目录
        if os.path.isdir(source):
            name = os.path.basename(source.rstrip("/").rstrip("\\"))
            shutil.copytree(source, os.path.join(dst_root, name), dirs_exist_ok=True)
            return f"[成功] 技能 '{name}' 已安装"

        return (f"[错误] 无效技能来源: {source}(支持 git URL / GitHub owner/repo / "
                f"本地目录 / zip 文件 / npx skills add <url|path|name> / "
                f"ModelScope 技能 URL)")

    def _finalize_repo_install(self, repo_dir, repo_name, dst_root):
        """仓库克隆后: 如果根目录有 SKILL.md 直接用; 否则在 skills/ 子目录中
        逐个安装。适用于 owner/repo 格式(如 anthropics/skills 含多个技能)。"""
        if os.path.isfile(os.path.join(repo_dir, "SKILL.md")):
            # 根目录就是技能目录 → 移动到技能库
            final = os.path.join(dst_root, repo_name)
            if os.path.abspath(repo_dir) != os.path.abspath(final):
                shutil.move(repo_dir, final)
            return f"[成功] 技能 '{repo_name}' 已安装到 {final}"
        # 在 skills/ 子目录中查找技能
        skills_subdir = os.path.join(repo_dir, "skills")
        search_dirs = [skills_subdir, repo_dir]
        installed = []
        for sd in search_dirs:
            if not os.path.isdir(sd):
                continue
            for entry in sorted(os.listdir(sd)):
                entry_path = os.path.join(sd, entry)
                if os.path.isfile(os.path.join(entry_path, "SKILL.md")):
                    final = os.path.join(dst_root, entry)
                    if os.path.exists(final):
                        shutil.rmtree(final, ignore_errors=True)
                    shutil.copytree(entry_path, final, dirs_exist_ok=True)
                    installed.append(entry)
        # 清理临时仓库
        shutil.rmtree(repo_dir, ignore_errors=True)
        if installed:
            return f"[成功] 已安装 {len(installed)} 个技能: {', '.join(installed)}"
        return (f"[错误] 仓库 '{repo_name}' 中未找到有效的 SKILL.md 文件"
                f"(技能需含 name 和 description 字段)")

    def _install_github(self, owner, repo, subpath, dst_root):
        """GitHub owner/repo 简写安装 → 克隆后自动提取技能目录"""
        git_url = f"https://github.com/{owner}/{repo}.git"
        with tempfile.TemporaryDirectory(prefix="hs_skill_") as tmp:
            dst = os.path.join(tmp, repo)
            r = subprocess.run(["git", "clone", "--depth", "1", git_url, dst],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                return f"[失败] git clone {git_url}: {r.stderr[:300]}"
            # 如果指定了子路径, 定位到该子目录
            if subpath:
                target = os.path.join(dst, subpath)
                if os.path.isfile(os.path.join(target, "SKILL.md")):
                    name = os.path.basename(target.rstrip("/"))
                    final = os.path.join(dst_root, name)
                    shutil.copytree(target, final, dirs_exist_ok=True)
                    return f"[成功] 技能 '{name}' 已安装到 {final}"
            return self._finalize_repo_install(dst, repo, dst_root)

    def _install_modelscope(self, owner, skill_name):
        """ModelScope 技能 API 下载安装。
        ModelScope 技能不是 git 仓库, 需通过 REST API 递归下载文件。
        API 端点:
        - 列目录: /api/v1/skills/@{owner}/{skill}/repo/files?Revision=master[&Root={path}]
        - 下载文件: /skills/@{owner}/{skill}/resolve/master/{path}
        """
        api_base = f"https://www.modelscope.cn/api/v1/skills/@{owner}/{skill_name}/repo"
        resolve_base = (f"https://www.modelscope.cn/skills/@{owner}/{skill_name}"
                        "/resolve/master")
        dst_root = self.dirs[-1]
        dst = os.path.join(dst_root, skill_name)
        if os.path.exists(dst):
            shutil.rmtree(dst, ignore_errors=True)
        os.makedirs(dst, exist_ok=True)

        def _download_tree(root_path=""):
            url = f"{api_base}/files?Revision=master"
            if root_path:
                url += f"&Root={root_path}"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "omni-agent/4.5"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                return f"[失败] ModelScope API 请求失败: {e}"
            if data.get("Code") != 200 or not data.get("Data"):
                return f"[失败] ModelScope 技能不存在或无权限: @{owner}/{skill_name}"
            for entry in data["Data"]["Files"]:
                etype = entry.get("Type", "")
                epath = entry.get("Path", entry.get("Name", ""))
                if etype == "tree":
                    # 递归下载子目录
                    sub_result = _download_tree(epath)
                    if sub_result and sub_result.startswith("[失败]"):
                        return sub_result
                else:
                    # 下载文件
                    file_url = f"{resolve_base}/{epath}"
                    local_path = os.path.join(dst, epath)
                    os.makedirs(os.path.dirname(local_path), exist_ok=True)
                    try:
                        req = urllib.request.Request(
                            file_url, headers={"User-Agent": "omni-agent/4.5"})
                        with urllib.request.urlopen(req, timeout=60) as resp:
                            with open(local_path, "wb") as f:
                                f.write(resp.read())
                    except Exception as e:
                        log.warning("modelscope file download failed: %s: %s",
                                    epath, e)
            return None

        result = _download_tree()
        if result and result.startswith("[失败]"):
            shutil.rmtree(dst, ignore_errors=True)
            return result
        # 验证 SKILL.md 存在
        if not os.path.isfile(os.path.join(dst, "SKILL.md")):
            shutil.rmtree(dst, ignore_errors=True)
            return (f"[失败] ModelScope 技能 @{owner}/{skill_name} 缺少 SKILL.md"
                    f"(技能需含 name 和 description 字段)")
        return f"[成功] 技能 '{skill_name}' 已通过 ModelScope 安装到 {dst}"

    def _install_zip(self, zip_path, dst_root):
        """从 zip 文件安装技能。
        ZIP 包目录结构要求:
        ┌── my-skill/ ← 技能目录(名即技能名)
        │   ├── SKILL.md       ← 必须存在, 含 YAML frontmatter(name, description)
        │   └── references/ ← 可选: 辅助参考文件
        └── (其它内容忽略)

        也支持 zip 根目录直接含 SKILL.md 的扁平结构:
        ┌── SKILL.md
        └── references/

        多技能 zip 包(每个子目录各含 SKILL.md)也支持:
        ┌── skill-a/
        │   └── SKILL.md
        └── skill-b/
            └── SKILL.md
        """
        if not os.path.isfile(zip_path):
            return f"[错误] zip 文件不存在: {zip_path}"
        with tempfile.TemporaryDirectory(prefix="hs_zip_") as tmp:
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(tmp)
            except (zipfile.BadZipFile, OSError) as e:
                return f"[失败] zip 文件解压失败: {e}"
            # 分析解压后的目录结构
            entries = [e for e in os.listdir(tmp)
                       if not e.startswith(".") and not e.startswith("__")]
            installed = []
            # 情况1: 扁平结构 —— 根目录直接含 SKILL.md
            if os.path.isfile(os.path.join(tmp, "SKILL.md")):
                skill_name = os.path.splitext(
                    os.path.basename(zip_path))[0]
                final = os.path.join(dst_root, skill_name)
                if os.path.exists(final):
                    shutil.rmtree(final, ignore_errors=True)
                shutil.copytree(tmp, final, dirs_exist_ok=True)
                installed.append(skill_name)
            else:
                # 情况2: 每个子目录含 SKILL.md
                for entry in entries:
                    entry_path = os.path.join(tmp, entry)
                    if os.path.isdir(entry_path) and \
                            os.path.isfile(os.path.join(entry_path, "SKILL.md")):
                        final = os.path.join(dst_root, entry)
                        if os.path.exists(final):
                            shutil.rmtree(final, ignore_errors=True)
                        shutil.copytree(entry_path, final, dirs_exist_ok=True)
                        installed.append(entry)
            if not installed:
                return ("[错误] zip 包中未找到有效的技能目录(需含 SKILL.md)"
                        "\n期望目录结构:\n"
                        "  my-skill/\n"
                        "    ├── SKILL.md  (必须, 含 name/description)\n"
                        "    └── references/  (可选)\n"
                        "或扁平结构:\n"
                        "  ├── SKILL.md\n"
                        "  └── references/")
            return f"[成功] 已从 zip 安装 {len(installed)} 个技能: {', '.join(installed)}"

    def _install_npx(self, raw_cmd):
        """npx 方式安装技能(完整命令, 含 --skill 等参数)。
        解析策略:
        1. 提取 target(第一个非选项参数)
        2. 提取 --skill 参数列表(可选, 用于从含多技能的仓库中只安装指定技能)
        3. target 为 URL/owner-repo/ModelScope → 用内置逻辑安装(无需 Node 环境)
        4. target 为纯技能名 → 调用真实 npx skills CLI
        """
        # 解析 target 和 --skill 参数
        parts = raw_cmd.split()
        # 跳过 npx --yes/-y skills add
        i = 0
        while i < len(parts) and parts[i] not in ("add",):
            i += 1
        i += 1  # 跳过 "add"
        target = None
        skill_filters = []
        while i < len(parts):
            p = parts[i]
            if p == "--skill" and i + 1 < len(parts):
                skill_filters.append(parts[i + 1])
                i += 2
            elif p.startswith("--skill="):
                skill_filters.append(p.split("=", 1)[1])
                i += 1
            elif p.startswith("-"):
                i += 1  # 跳过其他标志位
            else:
                if target is None:
                    target = p
                i += 1

        if not target:
            return "[错误] npx skills add 命令缺少技能来源(target)"

        log.info("npx install: target=%s skills=%s", target, skill_filters)

        # target 为 URL/git → 内置安装逻辑
        if re.match(r"^(https?://|git@)", target):
            return self.install(target)

        # target 为 ModelScope 技能 URL
        ms_m = re.match(
            r"^https?://(?:www\.)?modelscope\.cn/skills/@([\w.-]+)/(\S+)$",
            target)
        if ms_m:
            return self._install_modelscope(ms_m.group(1), ms_m.group(2))

        # target 为 GitHub owner/repo 简写(含 /)
        gh_m = re.match(r"^([\w.-]+)/([\w.-]+)(?:/(\S+))?$", target)
        if gh_m and not os.path.exists(target):
            owner, repo = gh_m.group(1), gh_m.group(2)
            subpath = gh_m.group(3) or ""
            dst_root = self.dirs[-1]
            # 如果有 --skill 过滤, 在 subpath 上追加
            if skill_filters:
                # owner/repo 仓库中的 skills/ 子目录格式
                for sf in skill_filters:
                    # v8.8 瘦身: 移除未使用的 sp 中间变量
                    # 先克隆仓库, 再从 skills/ 子目录中提取指定技能
                    return self._install_github_skill_filter(
                        owner, repo, sf, dst_root)
            return self._install_github(owner, repo, subpath, dst_root)

        # target 为本地目录或 zip 文件 → 内置安装逻辑
        if os.path.exists(target):
            return self.install(target)

        # target 为纯技能名 → 调用真实 npx skills CLI
        dst_root = self.dirs[-1]
        before = {s["name"] for s in self.scan()}
        cmd_parts = ["npx", "--yes", "skills", "add", target]
        # 追加 --skill 参数
        for sf in skill_filters:
            cmd_parts.extend(["--skill", sf])
        # 追加全局安装 + 自动确认 + 指定 agent 为通用格式
        cmd_parts.extend(["-g", "-y", "-a", "opencode"])
        try:
            if os.name == "nt":
                r = subprocess.run(" ".join(f'"{p}"' for p in cmd_parts),
                                   shell=True, capture_output=True, text=True,
                                   timeout=180, cwd=dst_root)
            else:
                r = subprocess.run(cmd_parts, capture_output=True, text=True,
                                   timeout=180, cwd=dst_root)
        except FileNotFoundError:
            return ("[失败] 未检测到 npx(需安装 Node.js 环境); "
                    "可改用 git URL、GitHub owner/repo 或本地目录方式安装")
        except subprocess.TimeoutExpired:
            return "[失败] npx 安装超时(180s), 请检查网络后重试"

        log.info("npx skills add %s: exit=%s stdout=%s stderr=%s", target,
                 r.returncode, (r.stdout or "")[:150], (r.stderr or "")[:150])

        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()[:300]
            # npx skills CLI 对某些仓库格式报"No valid skills found"
            # 这通常是因为仓库根目录没有 SKILL.md(技能在 skills/ 子目录中),
            # npx CLI 不自动递归查找。此时降级为内置安装逻辑。
            if "No valid skills" in err or "No skills found" in err:
                log.info("npx reported no skills found, "
                         "falling back to internal installer")
                # 如果 target 像 owner/repo 格式
                if gh_m:
                    owner, repo = gh_m.group(1), gh_m.group(2)
                    subpath = gh_m.group(3) or ""
                    dst_root = self.dirs[-1]
                    if skill_filters:
                        return self._install_github_skill_filter(
                            owner, repo, skill_filters[0], dst_root)
                    return self._install_github(owner, repo, subpath, dst_root)
                return (f"[失败] npx skills add {target}: {err}。"
                        f"建议改用 git URL 或 GitHub owner/repo 格式安装")
            return f"[失败] npx skills add {target}: {err or '未知错误'}"

        # 安装后重新扫描: 确认技能已出现在技能列表
        added = [s["name"] for s in self.scan() if s["name"] not in before]
        if target in ([s["name"] for s in self.scan()]):
            return f"[成功] 技能 '{target}' 已通过 npx 安装并加入技能列表"
        if added:
            return (f"[成功] 已通过 npx 安装技能: {', '.join(added)}"
                    f"(已加入技能列表)")
        out = (r.stdout or "").strip()[:200]
        return (f"[成功] npx 执行完成: {out or '(无输出)'}"
                f"(如未见新技能请刷新技能列表)")

    def _install_github_skill_filter(self, owner, repo, skill_name, dst_root):
        """从 GitHub 仓库中安装指定技能(owner/repo --skill name)。
        克隆仓库后在 skills/{skill_name} 子目录中查找 SKILL.md。"""
        git_url = f"https://github.com/{owner}/{repo}.git"
        with tempfile.TemporaryDirectory(prefix="hs_skill_") as tmp:
            dst = os.path.join(tmp, repo)
            r = subprocess.run(["git", "clone", "--depth", "1", git_url, dst],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                return f"[失败] git clone {git_url}: {r.stderr[:300]}"
            # 在多个候选路径中查找 SKILL.md
            candidates = [
                os.path.join(dst, "skills", skill_name),
                os.path.join(dst, skill_name),
                os.path.join(dst, "skills", skill_name, "SKILL.md"),
                os.path.join(dst, skill_name, "SKILL.md"),
            ]
            for cand in candidates:
                if os.path.isdir(cand) and \
                        os.path.isfile(os.path.join(cand, "SKILL.md")):
                    final = os.path.join(dst_root, skill_name)
                    if os.path.exists(final):
                        shutil.rmtree(final, ignore_errors=True)
                    shutil.copytree(cand, final, dirs_exist_ok=True)
                    return f"[成功] 技能 '{skill_name}' 已安装到 {final}"
            # 没找到指定技能, 全量安装
            return self._finalize_repo_install(dst, repo, dst_root)

    def remove(self, name):
        """删除技能目录(供网页版技能管理; 仅限含 SKILL.md 的合法技能目录)"""
        for base in self.dirs:
            d = os.path.join(base, name)
            if os.path.isdir(d) and os.path.isfile(os.path.join(d, "SKILL.md")):
                shutil.rmtree(d, ignore_errors=True)
                self.loaded.discard(name)
                return f"[成功] 技能 '{name}' 已删除"
        return f"[错误] 技能不存在: {name}"

    def unload(self, name):
        """从当前会话卸载技能(仅移除加载标记, 不删除文件)"""
        if name in self.loaded:
            self.loaded.discard(name)
            return f"[成功] 技能 '{name}' 已从当前会话卸载"
        return f"[提示] 技能 '{name}' 未在当前会话加载"

    def load(self, name):
        if name in self.loaded:
            return f"[提示] 技能 '{name}' 本会话已加载过, 无需重复注入"
        for s in self.scan():
            if s["name"] == name:
                with open(s["path"], encoding="utf-8", errors="replace") as f:
                    content = f.read()[:8000]
                self.loaded.add(name)
                return f"=== 技能已加载: {name} ===\n{content}"
        return f"[错误] 技能不存在: {name}(可用: {[x['name'] for x in self.scan()] or '无'})"

    def content(self, name, limit=20000):
        """返回指定技能完整 SKILL.md 正文(供网页端 Markdown 渲染查看)"""
        for s in self.scan():
            if s["name"] == name:
                with open(s["path"], encoding="utf-8", errors="replace") as f:
                    return f.read()[:limit]
        return None

    def loaded_content(self, limit_each=6000):
        """拼装当前会话已勾选(loaded)技能的完整规范文本。
        发起对话时由 Agent._system() 自动追加到系统提示末尾,
        实现"选择即注入 system"的技能启用语义。"""
        if not self.loaded:
            return ""
        blocks = []
        for s in self.scan():
            if s["name"] in self.loaded:
                try:
                    with open(s["path"], encoding="utf-8", errors="replace") as f:
                        body = f.read()[:limit_each]
                    blocks.append(f"## 技能规范: {s['name']}\n{body}")
                except OSError as _e:
                    log.warning("忽略异常(omni_agent/skills.py:519): %s: %s", type(_e).__name__, _e)
                    continue
        if not blocks:
            return ""
        return ("\n\n# 已启用技能(用户在技能管理中勾选, 本会话内必须遵循其规范执行)\n"
                + "\n\n".join(blocks))
