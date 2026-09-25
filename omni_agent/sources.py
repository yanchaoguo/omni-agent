# -*- coding: utf-8 -*-
"""数据溯源(信源注册表): web_search / web_fetch / web_screenshot 自动登记信源
HTML 报告默认以"引用下标 + 悬停式信源卡片"呈现引用, 不再在结尾追加
"信源列表"章节(仅当显式传 with_list=True 时追加, 对应用户明确要求列表的场景)。
"""
import html as html_mod
import re
import urllib.parse


class SourceRegistry:
    """信源字段: 引用序号(id) / 标题(title) / 链接(url) / 来源机构(org) / 发布日期(date)。
    正文引用写法: ==高亮文本==[[n]]; 输出时渲染 <mark> + 悬停信源卡片角标。"""

    _ORG_MAP = {
        "gov.cn": "中国政府网", "xinhuanet.com": "新华社", "people.com.cn": "人民网",
        "cctv.com": "央视网", "chinanews.com": "中国新闻网", "thepaper.cn": "澎湃新闻",
        "36kr.com": "36氪", "zhihu.com": "知乎", "csdn.net": "CSDN",
        "github.com": "GitHub", "stackoverflow.com": "Stack Overflow",
        "cnblogs.com": "博客园", "juejin.cn": "掘金", "baidu.com": "百度",
        "bing.com": "必应", "weixin.qq.com": "微信公众平台", "sina.com.cn": "新浪",
        "163.com": "网易", "sohu.com": "搜狐", "qq.com": "腾讯网", "ifeng.com": "凤凰网",
    }

    def __init__(self):
        self.sources = []   # [{id,title,url,org,date}]
        self._seen = {}     # url -> id (去重, 保证同一链接序号唯一)

    # ---- 会话级持久化 —— 信源跟随会话切换联动(保存/恢复/清空) ----
    def reset(self):
        """清空信源登记表(新会话/目标会话无持久化信源时)"""
        self.sources = []
        self._seen = {}

    def save(self, workdir, settings_dir=".haisnap"):
        """将当前信源列表持久化到 <workdir>/<settings_dir>/sources.json"""
        import json as _json
        import os as _os
        try:
            d = _os.path.join(workdir, settings_dir)
            _os.makedirs(d, exist_ok=True)
            tmp = _os.path.join(d, "sources.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(self.sources, f, ensure_ascii=False)
            _os.replace(tmp, _os.path.join(d, "sources.json"))
        except OSError:
            pass

    def load(self, workdir, settings_dir=".haisnap"):
        """从会话目录恢复信源列表; 文件不存在/损坏则清空(联动要求: 无则清空)"""
        import json as _json
        import os as _os
        self.reset()
        p = _os.path.join(workdir, settings_dir, "sources.json")
        if _os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    data = _json.load(f)
                if isinstance(data, list):
                    self.sources = [s for s in data if isinstance(s, dict) and s.get("url")]
                    self.sources.sort(key=lambda s: s.get("id") or 0)   # 恢复即正序
                    self._seen = {s["url"]: s.get("id") for s in self.sources}
            except (_json.JSONDecodeError, OSError):
                self.reset()

    @classmethod
    def _guess_org(cls, url):
        host = urllib.parse.urlparse(url).netloc.lower()
        for key, org in cls._ORG_MAP.items():
            if host == key or host.endswith("." + key):
                return org
        return host or "未知来源"

    @staticmethod
    def extract_date(text):
        """从网页文本中提取发布日期(支持 2026-07-17 / 2026年7月17日 / 2026/07/17)"""
        m = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?", text[:5000])
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
        return ""

    def add(self, title, url, org="", date="", snippet="", siteName="", siteIcon=""):
        """登记信源, 返回引用序号(同一 URL 复用同一序号)。
        兼容 UniFuncs 返回字段 —— 持久化 siteName/siteIcon/snippet,
        悬浮卡片优先展示站点名与站点图标, 底部以 snippet 摘要替代裸链接。"""
        if url in self._seen:
            sid = self._seen[url]
            src = self.get(sid)
            if src:  # 后续补充到的机构/日期/站点/摘要信息回填(不改变序号)
                if org and (not src["org"] or src["org"] == self._guess_org(url)):
                    src["org"] = org
                if date and not src["date"]:
                    src["date"] = date
                if siteName and not src.get("siteName"):
                    src["siteName"] = siteName[:60]
                if siteIcon and not src.get("siteIcon"):
                    src["siteIcon"] = siteIcon
                if snippet and not src.get("snippet"):
                    src["snippet"] = snippet[:220]
            return sid
        sid = len(self.sources) + 1
        self.sources.append({"id": sid, "title": (title or url)[:120], "url": url,
                             "org": org or siteName or self._guess_org(url),
                             "date": date or "", "snippet": (snippet or "")[:220],
                             "siteName": (siteName or "")[:60],
                             "siteIcon": siteIcon or ""})
        self._seen[url] = sid
        return sid

    def get(self, sid):
        return next((s for s in self.sources if s["id"] == sid), None)

    @staticmethod
    def _cited_ids(text):
        """统一引用序号标准 —— 同时识别 [[n]] 与 markdown 脚注 [^n] 两种写法"""
        ids = {int(n) for n in re.findall(r"\[\[(\d+)\]\]", text)}
        ids |= {int(n) for n in re.findall(r"\[\^(\d+)\](?!:)", text)}
        return ids

    _CSS = ("<style>.hs-cite{position:relative;color:#1a73e8;cursor:pointer;font-size:.75em;"
            "font-weight:600}.hs-cite .hs-card{display:none;position:absolute;bottom:1.6em;left:0;"
            "z-index:99;min-width:260px;max-width:340px;background:#fff;border:1px solid #dbe1ea;"
            "border-radius:10px;box-shadow:0 8px 24px rgba(20,40,80,.15);padding:10px 12px;"
            "font-size:12px;font-weight:400;color:#333;line-height:1.6;text-align:left}"
            ".hs-cite:hover .hs-card{display:block}"
            ".hs-card .hs-title{display:block;color:#1a73e8;font-weight:600;font-size:12.5px;"
            "text-decoration:none;line-height:1.5;word-break:break-word}"
            ".hs-card .hs-title:hover{text-decoration:underline}"
            ".hs-card .hs-site{margin-top:6px;color:#5f6b7c;font-size:11px;display:flex;"
            "align-items:center;gap:5px}"
            ".hs-card .hs-favicon{width:14px;height:14px;border-radius:3px;flex:none;"
            "object-fit:contain}"
            ".hs-card .hs-meta{display:block;margin-top:5px;padding-top:5px;"
            "border-top:1px solid #f0f2f6;color:#8a93a3;font-size:11px}"
            ".hs-card .hs-snippet{display:block;margin-top:2px;color:#5f6b7c;font-size:11.5px;"
            "line-height:1.55;max-height:5.2em;overflow:hidden}"
            "mark.hs-hl{background:#fff3bf;border-radius:3px;padding:0 2px}</style>\n")

    @staticmethod
    def _extract_doc_defs(text):
        """提取文档内嵌的脚注文献定义([^n]: [标题](url) — 机构 · 日期),
        兼容纯文本行与 <a> 链接两种形态; 作为信源注册表之外的兜底数据源,
        修复 md 报告转 html 后引用角标悬浮卡片失效的问题。"""
        defs = {}
        for m in re.finditer(
                r"\[\^(\d+)\]:\s*\[([^\]]*)\]\(([^)\s]+)\)\s*(?:[—–-]\s*)?([^\n<]*)",
                text):
            defs[int(m.group(1))] = {"title": (m.group(2) or m.group(3)).strip(),
                                     "url": m.group(3),
                                     "meta": m.group(4).strip()}
        # LLM 把定义行的 [^n]: 直译为 <sup>[n]</sup>: 的形态(附件实测):
        # <sup>[1]</sup>: [标题](url) — 站点 · 日期
        for m in re.finditer(
                r"<sup[^>]*>\s*\[?(\d+)\]?\s*</sup>\s*[:：]\s*"
                r"\[([^\]]*)\]\(([^)\s]+)\)\s*(?:[—–-]\s*)?([^\n<]*)",
                text):
            defs.setdefault(int(m.group(1)), {
                "title": (m.group(2) or m.group(3)).strip(),
                "url": m.group(3), "meta": m.group(4).strip()})
        # 同形态 + <a> 链接: <sup>[1]</sup>: <a href="url">标题</a> — meta
        for m in re.finditer(
                r"<sup[^>]*>\s*\[?(\d+)\]?\s*</sup>\s*[:：]\s*"
                r"<a[^>]*href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>"
                r"\s*(?:[—–-]\s*)?([^\n<]*)", text, re.S):
            defs.setdefault(int(m.group(1)), {
                "title": re.sub(r"<[^>]+>", "", m.group(3)).strip() or m.group(2),
                "url": m.group(2), "meta": m.group(4).strip()})
        for m in re.finditer(
                r"\[\^(\d+)\]:\s*<a[^>]*href=[\"\']([^\"\']+)[\"\'][^>]*>(.*?)</a>"
                r"\s*(?:[—–-]\s*)?([^\n<]*)", text, re.S):
            defs.setdefault(int(m.group(1)), {
                "title": re.sub(r"<[^>]+>", "", m.group(3)).strip() or m.group(2),
                "url": m.group(2), "meta": m.group(4).strip()})
        # html 渲染器的脚注区 <li id="fn1">...<a href="url">标题</a>...</li>
        for m in re.finditer(
                r"<li[^>]*id=[\"\']fn:?(\d+)[\"\'][^>]*>(.*?)</li>",
                text, re.S | re.I):
            n = int(m.group(1))
            if n in defs:
                continue
            seg = m.group(2)
            am = re.search(
                r"<a[^>]*href=[\"\'](https?://[^\"\']+)[\"\'][^>]*>(.*?)</a>",
                seg, re.S)
            if am:
                tail = re.sub(r"<[^>]+>", "", seg.split("</a>")[-1])
                defs[n] = {"title": re.sub(r"<[^>]+>", "", am.group(2)).strip()
                           or am.group(1),
                           "url": am.group(1),
                           "meta": tail.strip(" \t\n—–-·↩")[:80]}
        # LLM 把 [^n] 直译为 <sup>[n]</sup>, 脚注定义转为 <ol>/<ul> 中的
        # <li> 但无 fn id —— 用序号(从1递增)匹配正文中 <sup>[n]</sup> 的 n 值;
        # 同时也兼容 <li value="n"> 显式序号写法
        for m in re.finditer(
                r"<li[^>]*value=[\"\'](\d+)[\"\'][^>]*>(.*?)</li>",
                text, re.S | re.I):
            n = int(m.group(1))
            if n in defs:
                continue
            seg = m.group(2)
            am = re.search(
                r"<a[^>]*href=[\"\'](https?://[^\"\']+)[\"\'][^>]*>(.*?)</a>",
                seg, re.S)
            if am:
                tail = re.sub(r"<[^>]+>", "", seg.split("</a>")[-1])
                defs.setdefault(n, {
                    "title": re.sub(r"<[^>]+>", "", am.group(2)).strip()
                    or am.group(1),
                    "url": am.group(1),
                    "meta": tail.strip(" \t\n—–-·↩")[:80]})
        # 无 id 无 value 的 <ol>/<ul><li> 列表(按位置从1递增序号) ——
        # 仅提取"信源列表/参考文献"类标题后紧跟的列表, 避免把正文中的普通
        # 链接列表误当作信源定义
        for m in re.finditer(
                r"<h[1-6][^>]*>\s*(?:信源列表|参考文献|参考资料|引用来源|"
                r"References|Sources)\s*</h[1-6]>\s*<(?:ol|ul)[^>]*>(.*?)"
                r"</(?:ol|ul)>",
                text, re.S | re.I):
            block = m.group(1)
            for idx, li_m in enumerate(
                    re.finditer(r"<li[^>]*>(.*?)</li>", block, re.S | re.I),
                    start=1):
                if idx in defs:
                    continue
                seg = li_m.group(1)
                am = re.search(
                    r"<a[^>]*href=[\"\'](https?://[^\"\']+)[\"\'][^>]*>(.*?)</a>",
                    seg, re.S)
                if am:
                    tail = re.sub(r"<[^>]+>", "", seg.split("</a>")[-1])
                    defs.setdefault(idx, {
                        "title": re.sub(r"<[^>]+>", "", am.group(2)).strip()
                        or am.group(1),
                        "url": am.group(1),
                        "meta": tail.strip(" \t\n—–-·↩")[:80]})
        return defs

    @staticmethod
    def _strip_doc_defs(text):
        """剥离文档内的脚注定义(纯文本行 + <li>/<p> 包裹形态)
        同时剥离 <ol>/<ul> 中仅含链接的 <li>(LLM 把 [^n]: 定义转为
        列表项, 装饰后由 _extract_doc_defs 提取并重建悬浮卡片, 原列表冗余)"""
        text = re.sub(r"<li[^>]*>\s*\[\^\d+\]:.*?</li>\s*", "", text, flags=re.S)
        text = re.sub(r"<li[^>]*id=[\"\']fn:?\d+[\"\'][^>]*>.*?</li>\s*", "",
                      text, flags=re.S | re.I)
        text = re.sub(r"<p[^>]*>\s*\[\^\d+\]:.*?</p>\s*", "", text, flags=re.S)
        text = re.sub(r"(?m)^\s*\[\^\d+\]:[^\n]*\n?", "", text)
        # 剥离 <sup>[n]</sup>: 形态定义行(整行, 含尾部 <br>)
        text = re.sub(
            r"(?m)^\s*<sup[^>]*>\s*\[?\d+\]?\s*</sup>\s*[:：][^\n]*\n?", "", text)
        # 剥离 <p>/<li> 包裹的 <sup>[n]</sup>: 定义行 —— LLM 直译 HTML
        # 报告时定义行常被段落标签包裹, 行首正则匹配不到, 残留后被 _plain_num
        # 兜底误渲染为第二个悬浮卡片(实测双卡片根因)
        # 改为行内有界匹配([^\n]*) —— 修复 HTML5 未闭合 <p> 时
        # 旧正则 .*?</p> 跨段吞噬后续正文的回归; 同时支持全角冒号定义行
        text = re.sub(
            r"<(p|li)[^>]*>\s*<sup[^>]*>\s*\[?\d+\]?\s*</sup>\s*[:：]"
            r"[^\n]*?(?:</\1>)?[ \t]*\n?",
            "", text, flags=re.I)
        # 仅剥离"信源列表/参考文献/References"标题后紧跟的链接列表
        # (避免误删正文中的普通链接列表); 标题保留由装饰器重建带卡片的列表
        text = re.sub(
            r"(<h[1-6][^>]*>\s*(?:信源列表|参考文献|参考资料|引用来源|"
            r"References|Sources)\s*</h[1-6]>\s*)<(?:ol|ul)[^>]*>.*?"
            r"</(?:ol|ul)>\s*", r"\1", text, flags=re.S | re.I)
        return text

    def decorate_html(self, html_text, with_list=False):
        """替换 [[n]] 为悬停信源卡片角标, ==文本== 为高亮。
        默认不追加"信源列表"章节(with_list=False) —— 引用信息完整呈现在
        悬停卡片中(标题/机构/日期/可点击链接); 仅当用户明确要求列表时传 True。"""
        doc_defs = self._extract_doc_defs(html_text)   # 文档内嵌文献定义兜底
        if not self.sources and not doc_defs:
            # 无任何信源数据时(跨进程恢复后注册表丢失且文档无内嵌定义),
            # 不能原样返回 —— 否则 [[n]]/==高亮== 标记裸露在正文; 清理后返回
            cleaned = re.sub(r"==([^=\n]{1,120})==",
                             r'<mark class="hs-hl">\1</mark>', html_text)
            cleaned = re.sub(r"\[\[\d+\]\]|\[\^\d+\](?!:)", "", cleaned)
            if cleaned != html_text and "mark.hs-hl" not in cleaned:
                # 高亮样式依赖 CSS, 按新的安全位置注入
                if "</body>" in cleaned:
                    cleaned = cleaned.replace("</body>", self._CSS + "</body>", 1)
                elif "</head>" in cleaned:
                    cleaned = cleaned.replace("</head>", self._CSS + "</head>", 1)
                else:
                    cleaned = cleaned + self._CSS
            return cleaned
        html_text = re.sub(r"==([^=\n]{1,120})==", r'<mark class="hs-hl">\1</mark>', html_text)
        # 引用序号统一标准化 —— markdown 转 html 场景兼容脚注写法:
        # ① 剥离脚注定义(纯文本行 + <p>/<li> 包裹形态) ② 行内 [^n] 归一化为 [[n]]
        html_text = self._strip_doc_defs(html_text)
        # md->html 渲染器输出的 HTML 脚注角标归一化 —— 修复"先建 md 报告
        # 再转 html"后引用角标失去悬浮卡片的问题(pandoc/marked/LLM 直译均覆盖):
        # <sup><a href="#fn1">1</a></sup> / <a href="#fn1"><sup>1</sup></a>
        # / <sup class="footnote-ref">[1]</sup> 统一还原为 [[n]] 再渲染卡片
        # 锚点前缀放宽 —— marked.js/showdown 等渲染器输出 #footnote-1
        # /#note1/#ref1/#cite1 等形态, 原实现仅匹配 #fn 导致角标悬浮卡片丢失
        _anchor = r"#(?:fn|footnote|note|ref|cite)[^\"\']*"
        html_text = re.sub(
            r"<sup[^>]*>\s*<a[^>]*href=[\"\']" + _anchor
            + r"[\"\'][^>]*>\s*\[?(\d+)\]?\s*</a>\s*</sup>",
            r"[[\1]]", html_text, flags=re.I)
        html_text = re.sub(
            r"<a[^>]*href=[\"\']" + _anchor
            + r"[\"\'][^>]*>\s*<sup[^>]*>\s*\[?(\d+)\]?\s*</sup>\s*</a>",
            r"[[\1]]", html_text, flags=re.I)
        html_text = re.sub(
            r"<sup[^>]*class=[\"\'][^\"\']*footnote[^\"\']*[\"\'][^>]*>\s*\[?(\d+)\]?\s*</sup>",
            r"[[\1]]", html_text, flags=re.I)
        # 裸 <sup>[1]</sup> / <sup>1</sup>(LLM 直译常见形态, 无 class 无链接)
        # 仅当序号对应有效信源时归一化, 避免误伤数学上标
        _known = {s["id"] for s in self.sources} | set(doc_defs)

        def _bare_sup(m):
            n = int(m.group(1))
            # 排除脚注定义行(<sup>[n]</sup>: 形态, 后跟冒号) ——
            # 定义行中的角标不应归一化为正文引用, 否则 _strip_doc_defs
            # 剥离定义行后仍残留 <sup> 导致同一序号出现两个悬浮卡片
            return f"[[{n}]]" if n in _known else m.group(0)
        # 正则增加 (?!\s*:) 排除定义行; 排除已被 hs-cite 包裹的角标
        # (?!\s*:) 收窄为仅排除"定义行签名"(冒号后紧跟 <a>/markdown
        # 链接/裸URL) —— 修复正文角标后跟普通冒号(如 "数据如下<sup>[1]</sup>:")
        # 被误判为定义行导致引用角标丢失的回归
        html_text = re.sub(
            r"<sup(?![^>]*hs-cite)[^>]*>\s*\[?(\d+)\]?\s*</sup>"
            r"(?!\s*[:：]\s*(?:<a[\s>]|\[[^\]]*\]\(|https?://))",
            _bare_sup, html_text, flags=re.I)
        html_text = re.sub(r"\[\^(\d+)\](?!:)", r"[[\1]]", html_text)
        # 纯文本 [n] 兜底(md->html 渲染器把 [^n] 直译为 [n] 的场景):
        # 仅在文档尚未含 hs-cite 卡片时执行(防二次装饰误伤), 且序号必须命中
        # 有效信源、后面不跟 (/[/: (排除 markdown 链接/定义/数组下标形态)
        if 'class="hs-cite"' not in html_text:
            def _plain_num(m):
                n = int(m.group(1))
                return f"[[{n}]]" if n in _known else m.group(0)
            # 排除脚注定义行 [^n]: / 行尾冒号 / 仍在 <sup>…</sup>: 定义
            # 结构内的数字(后跟 </sup>: 视为定义行残留, 不渲染卡片)
            html_text = re.sub(
                r"(?<![A-Za-z0-9_\[\^])\[(\d{1,3})\]"
                r"(?![\]\(:=])(?!\[(?!\d))"
                r"(?!\s*</sup>\s*[:：]\s*(?:<a[\s>]|\[[^\]]*\]\(|https?://))",
                _plain_num, html_text)
        # v7.5 bug修复: cited 必须在 [[n]] 被替换为 <sup> 前提取, 否则 with_list
        # 模式下所有信源都会被误标为"未在正文引用"
        # 同时识别已渲染的 hs-cite 卡片角标(二次装饰场景) —— 否则重复
        # 装饰时 cited 为空, with_list 列表被剥离后无法重建
        cited = self._cited_ids(html_text)
        cited |= {int(n) for n in re.findall(
            r'<sup class="hs-cite">\[(\d+)\]', html_text)}

        def _sup(m):
            n = int(m.group(1))
            s = self.get(n)
            if not s and n in doc_defs:
                # 信源注册表未命中(跨进程恢复后为空/序号缺失)时,
                # 回退使用文档内嵌的文献定义渲染悬浮卡片
                d = doc_defs[n]
                parts = [x.strip() for x in (d.get("meta") or "").split("·")]
                s = {"id": n, "title": d.get("title") or d.get("url") or f"参考文献[{n}]",
                     "url": d.get("url") or "", "snippet": "",
                     "siteName": "", "siteIcon": "",
                     "org": (parts[0] if parts and parts[0] else "参考来源"),
                     "date": (parts[1] if len(parts) > 1 else "")}
            if not s:
                return ""   # 无效引用序号直接剔除
            # 正文引用仅保留下标 [n], 域名/日期/摘要等一律收进悬停卡片;
            # 修复旧版 </a> 在 hs-card 内提前闭合导致浏览器重排、站点行裸露在正文的缺陷。
            url = html_mod.escape(s["url"], quote=True)
            title = html_mod.escape(s["title"])
            site = html_mod.escape(s.get("siteName") or s["org"] or "未知来源")
            icon = (f'<img class="hs-favicon" src="{html_mod.escape(s["siteIcon"], quote=True)}" '
                    f'alt="" loading="lazy" onerror="this.style.display=\'none\'">'
                    if s.get("siteIcon") else "")
            date = html_mod.escape(s["date"] or "日期未注明")
            snippet = html_mod.escape((s.get("snippet") or "").strip())
            # 卡片布局: ① 标题(可点击) ② icon+来源 ③ 下方时间+摘要(无摘要时仅时间, 不再回退域名)
            meta = f'<span class="hs-meta">🕒 {date}</span>'
            if snippet:
                meta += f'<span class="hs-snippet">{snippet}</span>'
            return (f'<sup class="hs-cite">[{s["id"]}]'
                    f'<span class="hs-card">'
                    f'<a class="hs-title" href="{url}" target="_blank" rel="noopener">{title}</a>'
                    f'<span class="hs-site">{icon}{site}</span>'
                    f'{meta}</span></sup>')
        html_text = re.sub(r"\[\[(\d+)\]\]", _sup, html_text)

        # CSS 注入幂等 —— 已装饰过的文档(如二次 write_file)不重复注入
        # 修复固定角标样式丢失 —— 无 </body> 时 CSS 不得拼接在文档头部
        # (DOCTYPE 前), 否则浏览器 quirks 模式导致 position/box-shadow 全部失效;
        # 优先注入 </head> 前, 其次 </body> 前, 再次 <body> 后, 最后才尾部追加
        _css = "" if ".hs-cite{" in html_text else self._CSS
        if not with_list:   # 默认: 仅注入悬停卡片样式, 不追加结尾列表
            if not _css:
                return html_text
            if "</body>" in html_text:
                return html_text.replace("</body>", _css + "</body>", 1)
            if "</head>" in html_text:
                return html_text.replace("</head>", _css + "</head>", 1)
            if "<body" in html_text.lower():
                # 有 <body> 标签但无闭合 → 紧跟 <body...> 后插入
                return re.sub(r"(<body[^>]*>)", r"\1" + _css,
                              html_text, count=1, flags=re.I)
            # 无 body 标签的 HTML 片段 → 尾部追加(不破坏 DOCTYPE)
            return html_text + _css

        # 信源列表仅收录"正文实际引用"的条目(升序) —— 与当前文档内容
        # 相关的信源才进入列表; 同时剥离旧列表 section 保证重复装饰幂等
        html_text = re.sub(r'<section class="hs-sources".*?</section>\s*', "",
                           html_text, flags=re.S)
        ordered = [s for s in sorted(self.sources, key=lambda x: x["id"])
                   if s["id"] in cited]

        def _li(s):
            date = s["date"] or "日期未注明"
            return (f'<li id="hs-src-{s["id"]}" value="{s["id"]}">'
                    f'<a href="{s["url"]}" target="_blank">{s["title"]}</a> '
                    f'<span class="hs-site">— {s["org"]} · {date}</span></li>')
        items = "".join(_li(s) for s in ordered)
        block = (f'\n<section class="hs-sources" style="margin-top:40px;border-top:1px solid #e5e9f0;'
                 f'padding-top:16px"><h3>信源列表</h3><ol style="font-size:13px;color:#445">{items}'
                 f'</ol></section>\n') if items else ""
        if "</body>" in html_text:
            return html_text.replace("</body>", _css + block + "</body>", 1)
        if "</head>" in html_text:
            return html_text.replace("</head>", _css + block + "</head>", 1)
        if "<body" in html_text.lower():
            return re.sub(r"(<body[^>]*>)", r"\1" + _css,
                          html_text, count=1, flags=re.I) + block
        return html_text + _css + block

    def decorate_md(self, md_text):
        """Markdown: [[n]] → [^n] 脚注(脚注定义为 Markdown 语法必需, 保留在文末)
        v8.4 修复: ① 仅追加正文中实际引用到的信源脚注定义 —— 修复多轮对话
        (每轮均有检索)后, 最后一个 md 交付物被追加会话内全部信源列表的问题;
        ② 追加前先剥离文档内已有的脚注定义(幂等) —— 修复同一文件被多次
        write_file 装饰时脚注列表反复叠加。"""
        if not self.sources:
            return md_text
        # 先提取文档内嵌旧定义(可能含本注册表没有的序号, 保留其数据),
        # 再剥离旧定义行, 保证重复装饰不叠加
        doc_defs = self._extract_doc_defs(md_text)
        md_text = self._strip_doc_defs(md_text).rstrip() + "\n"
        valid = {s["id"] for s in self.sources} | set(doc_defs)
        cited = self._cited_ids(md_text)   # 先于脚注替换提取, 保证一一对应
        md_text = re.sub(r"\[\[(\d+)\]\]",
                         lambda m: f"[^{m.group(1)}]" if int(m.group(1)) in valid else "",
                         md_text)
        # 仅输出"正文实际引用"的信源, 按引用序号升序;
        # 未在正文引用的信源不再追加(与当前文档内容无关)
        used = [s for s in sorted(self.sources, key=lambda x: x["id"])
                if s["id"] in cited]
        # 注册表未命中但正文引用了的序号 → 回退文档内嵌旧定义(跨进程恢复场景)
        reg_ids = {s["id"] for s in used}
        extra = [(n, doc_defs[n]) for n in sorted(cited)
                 if n not in reg_ids and n in doc_defs]
        if not used and not extra:
            return md_text

        def _line(s):
            date = s["date"] or "日期未注明"
            return f"[^{s['id']}]: [{s['title']}]({s['url']}) — {s['org']} · {date}"
        lines = [_line(s) for s in used]
        for n, d in extra:
            meta = (" — " + d["meta"]) if d.get("meta") else ""
            lines.append(f"[^{n}]: [{d['title']}]({d['url']}){meta}")
        lines.sort(key=lambda x: int(re.match(r"\[\^(\d+)\]", x).group(1)))
        return md_text + "\n" + "\n".join(lines)

    def render_list(self):
        if not self.sources:
            return "(本会话暂无登记信源)"
        return "\n".join(f"  [{s['id']}] [{s['title']}]({s['url']})  "
                         f"— {s['org']} · {s['date'] or '日期未注明'}"
                         for s in sorted(self.sources, key=lambda x: x["id"]))