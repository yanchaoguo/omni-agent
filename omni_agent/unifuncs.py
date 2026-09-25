# -*- coding: utf-8 -*-
"""UniFuncs 聚合搜索/网页阅读客户端(v4.7, 纯标准库)
- web_search 通道: GET /api/web-search/search?query=..&apiKey=..
- web_fetch  通道: v4.7 依照官方文档改为 POST /api/web-reader/read
  (JSON 请求体 {url, format, readTimeout...}, API Key 经 Authorization: Bearer 头传递),
  失败时自动降级到旧版 GET /api/web-reader/{url} 通道保证可用性。
  文档: https://unifuncs.com/api/web-reader
- v4.1 实测校准: 搜索实际返回结构为 {code:0, message:"OK",
  data:{webPages:[{name,url,snippet,summary,datePublished},..], images:[..]}},
  解析器补齐 data.webPages 路径, 并保留防御式兼容路径。
- api_error(): code!=0 时透出服务端错误码与消息。
- API Key 优先级: 用户级环境变量 HAISNAP_UNIFUNCS_KEY > 内置默认;
  HAISNAP_UNIFUNCS=off 可整体关闭该通道。
"""
import json
import os
import urllib.parse
import urllib.request

from .config import UA, UNIFUNCS_API_KEY, UNIFUNCS_BASE
from .settings import S   # 统一配置链
from .logger import get_logger

log = get_logger("unifuncs")


class UniFuncs:

    @staticmethod
    def api_key():
        # env > 项目settings.json > 全局settings.json > config.py
        return S.get_str("unifuncs.api_key", UNIFUNCS_API_KEY)

    @staticmethod
    def base():
        return S.get_str("unifuncs.base", UNIFUNCS_BASE).rstrip("/")

    @classmethod
    def enabled(cls):
        if os.environ.get("HAISNAP_UNIFUNCS", "on").lower() in ("off", "0", "false"):
            return False
        return bool(cls.api_key())

    @staticmethod
    def _get(url, timeout=20):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:   # with 释放连接
            return resp.read().decode("utf-8", "replace")

    @classmethod
    def _post(cls, url, payload, timeout=30):
        """POST JSON 请求(Authorization: Bearer 头传 API Key)"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cls.api_key()}",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    @staticmethod
    def api_error(body):
        """提取 API 层错误(code!=0)信息; 无法解析或无错误时返回空串"""
        try:
            data = json.loads(body or "")
        except (json.JSONDecodeError, TypeError):
            return ""
        if isinstance(data, dict):
            code = data.get("code")
            if code not in (None, 0, 200, "0", "200"):
                msg = str(data.get("message") or data.get("msg") or "")[:200]
                return f"UniFuncs API 错误 code={code}: {msg or '(无错误描述)'}"
        return ""

    # ---- 搜索 ----
    @classmethod
    def search_url(cls, query):
        return (f"{cls.base()}/web-search/search?"
                f"query={urllib.parse.quote(query)}&apiKey={cls.api_key()}")

    @classmethod
    def search(cls, query, limit=5, timeout=20):
        """返回 [(title, url, snippet), ...], 失败抛异常(错误信息含服务端错误码)"""
        body = cls._get(cls.search_url(query), timeout)
        rows = cls.parse_search(body, limit)
        if not rows:
            err = cls.api_error(body)
            raise RuntimeError(err or "UniFuncs 搜索结果为空或格式无法解析")
        log.info("unifuncs search ok: query=%s hits=%d", query[:50], len(rows))
        return rows

    @staticmethod
    def parse_search(body, limit=5):
        """ # unifuncs search返回字段
{
    "code": 0,
    "message": "OK",
    "data": {
        "webPages": [
            {
                "name": "UniFuncs",
                "url": "https://unifuncs.com/",
                "displayUrl": "https://unifuncs.com/",
                "snippet": "UniFuncs推出的专业深度研究模型，擅长研究推理，能够从收集材料中分析和提出专业见解。 立即接入 · S3. 深度搜索. UniFuncs ...",
                "summary": "UniFuncs推出的专业深度研究模型，擅长研究推理，能够从收集材料中分析和提出专业见解。 立即接入 · S3. 深度搜索. UniFuncs ...",
                "siteName": "unifuncs.com",
                "siteIcon": "https://ga-1.unifuncs.com/siteicon/756e6966756e63732e636f6d7c3238366237313933",
                "datePublished": null
            },
        ],
        "images": []
    },
    "requestId": "b69b4510-a74d-47d3-8eef-dca355dc9984"
}"""
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return []
        items = None
        candidates = (("data", "webPages"), ("data", "results"), ("data", "items"),
                      ("data", "list"), ("webPages", "value"), ("webPages",),
                      ("results",), ("items",), ("list",), ("data",))
        for path in candidates:
            cur = data
            ok = True
            for k in path:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, list):
                items = cur
                break
        if items is None and isinstance(data, list):
            items = data
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                continue
            title = str(it.get("title") or it.get("name") or "").strip()
            href = str(it.get("url") or it.get("link") or it.get("href") or "").strip()
            snippet = str(it.get("snippet") or it.get("description")
                          or it.get("summary") or it.get("content") or "").strip()[:200]
            date = str(it.get("datePublished") or it.get("date") or "").strip()
            siteName = str(it.get("siteName") or "").strip()
            siteIcon = str(it.get("siteIcon") or "").strip()
            if date and date.lower() not in ("none", "null"):
                snippet = f"({date[:10]}) {snippet}"[:220]
            if title and href.startswith("http"):
                out.append((title, href, snippet, siteName, siteIcon))
            if len(out) >= limit:
                break
        return out

    # ---- 网页阅读(POST 首选, GET 降级) ----
    READER_POST_URL_SUFFIX = "/web-reader/read"

    @classmethod
    def reader_post_url(cls):
        return f"{cls.base()}{cls.READER_POST_URL_SUFFIX}"

    @classmethod
    def fetch_url(cls, url):
        """旧版 GET 通道 URL(仅作 POST 失败时的降级路径)"""
        return (f"{cls.base()}/web-reader/"
                f"{urllib.parse.quote(url, safe=':/')}?apiKey={cls.api_key()}")

    @classmethod
    def fetch(cls, url, timeout=30):
        """返回正文文本(markdown/纯文本)。
        按官方文档改用 POST /api/web-reader/read —— JSON 体传参
        (url/format/readTimeout), API Key 经 Authorization: Bearer 头传递;
        POST 异常时自动降级旧版 GET 通道, 保证兼容性。
        阅读器返回纯 markdown(Title:/URL Source:/Markdown Content: 头部)时
        parse_reader 原样透传; JSON 包装结构走字段提取; code!=0 透出服务端错误。"""
        payload = {
            "url": url,
            "format": "md",
            # readTimeout 单位毫秒; 预留 5s 网络往返余量, 不低于 5000ms
            "readTimeout": max(5, timeout - 5) * 1000,
        }
        text = ""
        post_err = None
        try:
            text = cls._post(cls.reader_post_url(), payload, timeout)
        except Exception as e:   # POST 通道异常 -> 降级 GET
            post_err = e
            log.warning("unifuncs fetch POST failed(%s), fallback GET: url=%s",
                        e, url[:80])
        if not (text or "").strip():
            try:
                text = cls._get(cls.fetch_url(url), timeout)
            except Exception as e:
                raise RuntimeError(
                    f"UniFuncs 阅读器 POST/GET 双通道均失败: POST={post_err}; GET={e}")
        body = cls.parse_reader(text)
        if not body.strip():
            err = cls.api_error(text)
            raise RuntimeError(err or "UniFuncs 阅读器返回内容为空或格式无法解析")
        if len(body.strip()) < 80:   # 短正文: 可能是错误页, 记录告警但不阻断
            log.warning("unifuncs fetch short: url=%s chars=%d", url[:80], len(body))
        log.info("unifuncs fetch ok(POST): url=%s chars=%d", url[:80], len(body))
        return body

    @staticmethod
    def parse_reader(text):
        """兼容 JSON 包装({data:{content}}等)与纯 markdown 两种返回"""
        t = (text or "").strip()
        if t.startswith(("{", "[")):
            try:
                data = json.loads(t)
            except json.JSONDecodeError:
                return t
            if isinstance(data, dict):
                for path in (("data", "content"), ("data", "markdown"),
                             ("data", "text"), ("content",), ("markdown",),
                             ("text",), ("data",)):
                    cur = data
                    ok = True
                    for k in path:
                        if isinstance(cur, dict) and k in cur:
                            cur = cur[k]
                        else:
                            ok = False
                            break
                    if ok and isinstance(cur, str) and cur.strip():
                        return cur
                return ""   # JSON 但无正文字段: 视为失败(fetch 会透出 api_error)
        return t
