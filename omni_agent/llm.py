# -*- coding: utf-8 -*-
"""LLM 客户端(纯标准库, 支持流式SSE, 失败自动回退非流式)
v3.7 新增: Claude Prompt Caching(cache_control 参数)支持
- 断点策略: system 提示(最大稳定前缀) + 最后一条 user 消息(多轮增量滚动命中)
- 兼容性: 仅在字符串/文本块 content 上打标, 图片块与工具消息不改写,
  非 Claude 模型(auto 模式)自动跳过, 不影响 OpenAI 兼容端点。
"""
import io
import json
import time
import urllib.error
import urllib.request

from .config import API_KEY, BASE_URL, MODEL, PROMPT_CACHE, PROMPT_CACHE_TTL
from .logger import get_logger
from .settings import S   # 统一配置链 env > 项目 > 全局 > config.py
from .ui import C, cprint

log = get_logger("llm")


class _ThinkTagSplitter:
    """增量拆分 content 流中内联的 <think>...</think> 思考段。
    部分 OpenAI 兼容网关(claude/R1 适配层)不走 reasoning_content 字段,
    而是把思考直接混入 content —— 本拆分器跨 chunk 边界安全还原。"""
    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.in_think = False

    def _tail_guard(self, tag):
        """缓冲区尾部可能是 tag 的前缀 → 保留待下一 chunk 拼接后再判定"""
        for k in range(min(len(tag) - 1, len(self.buf)), 0, -1):
            if tag.startswith(self.buf[-k:]):
                return k
        return 0

    def feed(self, chunk):
        """输入增量 chunk, 返回 (思考增量, 正文增量)"""
        self.buf += chunk
        reason, content = [], []
        while True:
            tag = self.CLOSE if self.in_think else self.OPEN
            i = self.buf.find(tag)
            if i >= 0:
                (reason if self.in_think else content).append(self.buf[:i])
                self.buf = self.buf[i + len(tag):]
                self.in_think = not self.in_think
                continue
            keep = self._tail_guard(tag)
            out = self.buf[:len(self.buf) - keep] if keep else self.buf
            (reason if self.in_think else content).append(out)
            self.buf = self.buf[len(self.buf) - keep:] if keep else ""
            break
        return "".join(reason), "".join(content)

    def flush(self):
        rest, self.buf = self.buf, ""
        return rest


class HaisnapClient:
    def __init__(self, thinking=True, quiet=False, on_delta=None):
        self.total_prompt = 0
        self.total_completion = 0
        self.total_cached = 0      # 缓存命中 token 累计(cache read)
        self.calls = 0
        self.thinking = thinking   # 可配置关闭 thinking 模式
        self._blank_think_notified = False   # "网关不透传思考"提示只发一次
        self._no_think_models = set()   # 已提示"无思考输出"的模型名(每模型一次)
        self._loop_no_think = set()    # 工具循环中思考衰减已提示的模型名
        self.quiet = quiet         # 静默模式(调度器后台执行时不刷屏)
        self.on_delta = on_delta   # 流式增量回调 fn(kind, text): Web UI 打字机推流
        self.fallback_hits = 0     # 备用模型兜底触发次数
        

    # ---- Claude cache_control 支持 ----
    @staticmethod
    def cache_enabled(model):
        """auto=仅 claude 系模型启用; on=强制启用; off=关闭"""
        mode = str(S.get("prompt_cache.mode", PROMPT_CACHE)).lower()   # 配置链
        if mode == "off":
            return False
        if mode == "on":
            return True
        return "claude" in (model or "").lower()

    @staticmethod
    def _mark_cache(msg, ttl):
        """在消息 content 上打 cache_control 断点(仅文本, 保守改写):
        - str content -> 转为单文本块并打标
        - list content -> 在最后一个 text 块上打标(图片块跳过)
        - 其余情况原样返回, 不破坏消息结构"""
        cc = {"type": "ephemeral"}
        if ttl and ttl != "5m":
            cc["ttl"] = ttl        # Anthropic 扩展 TTL(如 1h), 默认 5m 无需显式传
        c = msg.get("content")
        if isinstance(c, str) and c:
            return {**msg, "content": [{"type": "text", "text": c,
                                        "cache_control": cc}]}
        if isinstance(c, list) and c:
            blocks = [dict(b) if isinstance(b, dict) else b for b in c]
            for j in range(len(blocks) - 1, -1, -1):
                if isinstance(blocks[j], dict) and blocks[j].get("type") == "text":
                    blocks[j]["cache_control"] = cc
                    return {**msg, "content": blocks}
        return msg

    def apply_cache_control(self, messages, model): 
        if not self.cache_enabled(model) or not messages:
            return messages
        ttl = S.get_str("prompt_cache.ttl", PROMPT_CACHE_TTL)   # 配置链
        out = list(messages)
        if out[0].get("role") == "system":
            out[0] = self._mark_cache(out[0], ttl)
        last = out[-1]
        role = last.get("role")
        if role == "user":
            out[-1] = self._mark_cache(last, ttl)
        elif role == "tool":
            cc = {"type": "ephemeral"}
            if ttl and ttl != "5m":
                cc["ttl"] = ttl
            out[-1] = {**last, "cache_control": cc}   # 顶层打标, 不入 content
        return out

    def _request(self, payload, vision=False, fallback=False):
        # 走统一配置链(env > 项目settings.json > 全局settings.json > config.py)
        # fallback=True 时切换到备用语言模型端点(留空字段继承主模型)
        api_key = S.get_str("model.api_key") or API_KEY
        base_url = S.get_str("model.base_url") or BASE_URL
        if fallback:
            api_key = S.get_str("model.fallback_api_key") or api_key
            base_url = S.get_str("model.fallback_base_url") or base_url
        elif vision:
            # 视觉模型独立端点(未配置则继承基础语言模型的 Key/URL)
            api_key = S.get_str("model.vision_api_key") or api_key
            base_url = S.get_str("model.vision_base_url") or base_url
        # 模型池端点覆盖 —— 路由器选择的模型可能有独立 Key/URL
        # 视觉/备用请求同样按模型名查池覆盖(模型管理统一登记端点,
        # 主/备/视觉直选模型后无需二次填写 Key/URL)
        mdl_req = payload.get("model", "")
        from .model_router import ModelRouter
        _ov = ModelRouter.endpoint_override(mdl_req)
        if _ov:
            if _ov.get("api_key"):
                api_key = _ov["api_key"]
            if _ov.get("base_url"):
                base_url = _ov["base_url"]
            log.info("model_endpoint_override: model=%s base_url=%s",
                      mdl_req, base_url[:60])
        if not api_key:
            raise RuntimeError("API_KEY 未配置! 请设置环境变量 HAISNAP_API_KEY "
                               "(或 HAISNAP_BASE_URL / HAISNAP_MODEL 修改模型配置)")
        url = base_url.rstrip("/") + "/chat/completions"
        mdl = payload.get("model", "")
        # HTTP 400 自适应参数降级 —— 服务商拒绝某个采样/思考参数时
        # (如 Claude 4.5 系 `temperature is deprecated`), 自动剔除该参数重试,
        # 并记入黑名单避免同模型后续请求重复触发 400
        for _ in range(4):
            send_payload = {k: v for k, v in payload.items()
                            if not str(k).startswith("_")}
            req = urllib.request.Request(
                url, data=json.dumps(send_payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                method="POST")
            try:
                return urllib.request.urlopen(req, timeout=300)
            except urllib.error.HTTPError as e:
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                if e.code == 400:
                    # thinking 相关容量类 400(budget_tokens/max_tokens
                    # 约束不满足) —— 自适应调高 max_tokens 重试, 不拉黑参数
                    low400 = (body or "").lower()
                    if payload.get("thinking") and not payload.get("_tk_adj") \
                            and ("budget" in low400 or "max_tokens" in low400
                                 or "max tokens" in low400):
                        payload["max_tokens"] = 32000
                        payload["thinking"] = {"type": "enabled",
                                               "budget_tokens": 4096}
                        payload["_tk_adj"] = True
                        log.warning("thinking 容量类 400, 已调整 max_tokens 重试: %s",
                                    body[:200])
                        continue
                    # thinking 功能开关被端点明确拒绝 → 仅本次请求临时
                    # 剔除重试(不入黑名单) —— 换回支持思考的模型后立即恢复
                    _rej = ("unknown" in low400 or "unsupported" in low400
                            or "unexpected" in low400 or "not support" in low400
                            or "invalid" in low400 or "不支持" in low400)
                    if _rej and (payload.get("thinking")
                                 or payload.get("enable_thinking")) \
                            and ("thinking" in low400):
                        payload.pop("thinking", None)
                        payload.pop("enable_thinking", None)
                        log.warning("端点不支持思考参数(HTTP 400), 本次请求临时"
                                    "关闭思考(不拉黑, 切回支持的模型自动恢复): %s",
                                    body[:200])
                        continue
                    bad = self._find_bad_param(payload, body)
                    if bad:
                        payload.pop(bad, None)
                        log.warning("端点拒绝参数 `%s`(HTTP 400), 已临时剔除重试"
                                    "(仅本次请求生效, 无黑名单记忆): %s",
                                    bad, body[:200])
                        continue
                    # HTTP 400 `function.arguments must be in JSON format`
                    # —— 历史助手消息中 tool_calls 的 arguments 字段不是合法 JSON
                    # 字符串(可能为 dict、空串、或非法 JSON 文本)被严格校验的
                    # OpenAI 兼容端点拒绝。此处对全部历史 tool_calls 做一次性
                    # 规范化: dict→json.dumps, 非法/空串→重编码或兜底 "{}",
                    # 保证重发始终为合法 JSON 字符串。
                    if "function.arguments" in low400 \
                            or "arguments must be" in low400 \
                            or ("arguments" in low400 and "json" in low400):
                        fixed = 0
                        for m in payload.get("messages", []):
                            for tc in (m.get("tool_calls") or []):
                                fn = tc.get("function")
                                if not fn:
                                    continue
                                a = fn.get("arguments")
                                if isinstance(a, dict):
                                    fn["arguments"] = json.dumps(
                                        a, ensure_ascii=False)
                                    fixed += 1
                                elif isinstance(a, str):
                                    try:
                                        json.loads(a)
                                    except Exception:
                                        # 空串/裸文本 → 尝试包装为 JSON 字符串值,
                                        # 仍失败则兜底空对象, 保证合法
                                        fn["arguments"] = json.dumps(
                                            a, ensure_ascii=False)
                                        fixed += 1
                                else:
                                    fn["arguments"] = "{}"
                                    fixed += 1
                        if fixed:
                            log.warning("tool_calls.arguments 非合法 JSON, 已规范化"
                                        " %d 处(HTTP 400) 重试: %s",
                                        fixed, body[:200])
                            continue
                # 无法降级: 重建 HTTPError(保留响应体供上层读取错误细节)
                raise urllib.error.HTTPError(
                    url, e.code, e.reason, e.headers,
                    io.BytesIO(body.encode("utf-8", errors="replace")))
        raise RuntimeError(f"LLM 请求参数降级重试耗尽(model={mdl})")

    @staticmethod
    def _find_bad_param(payload, body):
        """从 400 错误报文中识别被服务商拒绝的可降级参数(采样/思考/
        格式类, 不含 tools 等语义必需字段), 返回参数名或 None"""
        low = (body or "").lower()
        # 仅当报错体明确表示"参数不被支持/无效"时才拉黑 —— 修复原实现
        # 只要报文含参数名即拉黑的误杀(如 thinking 缺 max_tokens 的 400 报文
        # 同样含 "thinking" 字样, 导致思考模式被永久静默关闭)
        reject_kws = ("unknown", "unsupported", "unexpected", "not support",
                      "invalid", "deprecated", "unrecognized", "extra_forbidden",
                      "not allowed", "禁止", "不支持", "无效")
        if not any(k in low for k in reject_kws):
            return None
        # thinking / enable_thinking 不参与拉黑 —— 这些是功能开关而非
        # 可选采样参数; 新模型不支持思考时 400 报文含 "thinking" 字样,
        # 原实现会将其拉黑, 导致切回支持思考的模型后参数仍被 pop → 永久关闭
        for p in ("temperature", "top_p",
                  "stream_options", "response_format",
                  "presence_penalty", "frequency_penalty"):
            if p in payload and p in low:
                return p
        return None

    @staticmethod
    def _enforce_tool_pairing(msgs):
        """发送前强制校验 tool_use/tool_result 严格相邻配对(Anthropic 系
        网关要求 tool_result 必须紧跟在含对应 tool_use 的 assistant 消息之后):
        ① 孤立/错位/重复 tool 消息 → 降级为 user 文本(内容保留);
        ② assistant.tool_calls 缺失的响应 → 紧随其后就地补占位 tool 消息;
        ③ 同一批 tool 响应保持连续, 中间不允许插入其他角色消息。"""
        out = []
        i, n = 0, len(msgs)
        while i < n:
            m = msgs[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                out.append(m)
                want = [tc.get("id") for tc in m["tool_calls"] if tc.get("id")]
                pending = set(want)
                j = i + 1
                got = {}
                # 收集紧随其后的 tool 响应(允许中间穿插的非 tool 消息延后)
                deferred = []
                while j < n and (msgs[j].get("role") == "tool"
                                 or (pending and msgs[j].get("role") != "assistant")):
                    mj = msgs[j]
                    if mj.get("role") == "tool":
                        tcid = mj.get("tool_call_id")
                        if tcid in pending:
                            got[tcid] = mj
                            pending.discard(tcid)
                        else:   # 重复/不属于本批 → 降级保留
                            deferred.append({"role": "user", "content":
                                             "[历史工具执行结果(配对修复保留)]\n"
                                             + str(mj.get("content", ""))[:4000]})
                    else:
                        deferred.append(mj)
                    j += 1
                    if not pending and (j >= n or msgs[j].get("role") != "tool"):
                        break
                for tcid in want:   # 按 tool_calls 顺序连续输出, 缺失补占位
                    out.append(got.get(tcid) or {
                        "role": "tool", "tool_call_id": tcid,
                        "content": "[已中断] 该工具调用未返回结果(上下文修复占位)"})
                out.extend(deferred)
                i = j
            elif m.get("role") == "tool":
                # 孤立 tool 消息(前面没有 assistant.tool_calls) → 降级为 user
                out.append({"role": "user", "content":
                            "[历史工具执行结果(配对修复保留)]\n"
                            + str(m.get("content", ""))[:4000]})
                i += 1
            else:
                out.append(m)
                i += 1
        return out

    def chat(self, messages, tools=None, stream=True, json_format=False, model=None,
             internal=False, vision=False): 
        try:
            fb_model = S.get_str("model.fallback_model")
            if len(messages) % 4 == 1 and fb_model:
                model = fb_model.strip()
            return self._chat_once(messages, tools, stream, json_format,
                                   model, internal, vision)
        except Exception as e:
            fb_model = S.get_str("model.fallback_model").strip()
            if vision or not fb_model:
                raise
            primary = model or S.get_str("model.model") or MODEL
            fb_key = S.get_str("model.fallback_api_key")
            fb_url = S.get_str("model.fallback_base_url")
            if fb_model == primary and not (fb_key or fb_url):
                raise   # 备用与主配置完全相同, 兜底无意义
            self.fallback_hits += 1
            # 关键决策日志 —— 主备故障转移(模型变更原因完整记录)
            log.warning("model_switch: primary=%s -> fallback=%s (hit#%d) "
                       "reason=%s: %s",
                       primary, fb_model, self.fallback_hits,
                       type(e).__name__, e)
            if not self.quiet:
                cprint(f"\n  ⚡ 主模型请求失败, 自动切换备用模型 {fb_model} 兜底重试...",
                       C.YELLOW)
            if self.on_delta and not internal:
                # 通知前端废弃主模型半途推送的流式内容 —— 防止备用模型
                # 重新生成时与残留内容叠加(表现为"重复执行了之前的步骤")
                self.on_delta("discard", "")
                self.on_delta("sys", f"⚡ 主模型请求失败({str(e)[:100]}), "
                              f"已自动切换备用模型 {fb_model} 兜底")
            return self._chat_once(messages, tools, stream, json_format,
                                   model=fb_model, internal=internal,
                                   vision=False, fallback=True)

    def _chat_once(self, messages, tools=None, stream=True, json_format=False,
                   model=None, internal=False, vision=False, fallback=False): 
        if not model and not vision:
            from .model_router import ModelRouter
            model, reason = ModelRouter().route(
                messages, internal=internal,
                task_type=str(getattr(self, '_current_task_type', '') or ''),
                domain=str(getattr(self, '_current_domain', '') or ''))
            log.info("model_route: %s | %s", model, reason)
        mdl = model or S.get_str("model.model") or MODEL   # 配置链
        if internal:
            stream = False
        # 剥离内部元数据字段(如 tool 消息的 _dt 执行耗时), 不发送给模型
        clean = [{k: v for k, v in m.items() if not str(k).startswith("_")}
                 for m in messages] 
        clean = self._enforce_tool_pairing(clean)
        payload = {"model": mdl,
                   "messages": self.apply_cache_control(clean, mdl),
                   "temperature": 1.0 if mdl.startswith("kimi") else 0.3,
                   "enable_thinking": self.thinking}
        if "claude" in mdl.lower(): 
            payload.pop("temperature", None)
            payload.pop("enable_thinking", None)
            if self.thinking: 
                payload["thinking"] = {"type": "enabled", "budget_tokens": 4096}
                payload.setdefault("max_tokens", 16000)
        if tools:
            # 工具 Schema 顶层 title 字段仅供前端显示, 发送前剥离,
            # 防止严格校验的 OpenAI 兼容端点报 unknown field 错误
            payload["tools"] = [{"type": t.get("type", "function"),
                                 "function": t["function"]} for t in tools]
        if stream and (not self.quiet or self.on_delta):
            payload["stream"] = True
            payload["stream_options"] = {"include_usage": True}
            try:
                return self._chat_stream(payload, fallback)
            except Exception as e:
                log.warning("stream failed, fallback non-stream: %s: %s",
                            type(e).__name__, e)
                cprint(f"\n  ! 流式请求失败({e}), 回退非流式...", C.GRAY)
        if json_format:
            payload["response_format"] = {"type": "json_object"}
        payload.pop("stream", None)
        payload.pop("stream_options", None)
        # 非流式请求保留思考(仅内部调用关闭)—— v4.2 原逻辑在流式回退
        # 路径中强制关闭 thinking, 导致回退后思考静默消失; 改为仅 internal=True
        # 时关闭(内部调用如任务命名/蒸馏等无需思考), 非流式回退时保留用户设置
        if "claude" in mdl.lower():
            payload.pop("thinking", None)
            payload.pop("enable_thinking", None)
        elif internal:
            payload["enable_thinking"] = False
        for attempt in range(3):
            try:
                with self._request(payload, vision=vision,
                                   fallback=fallback) as resp:   # v3.8.1: with 释放连接
                    data = json.loads(resp.read().decode("utf-8"))
                msg = data["choices"][0]["message"]
                u = data.get("usage", {})
                self._count(u)
                content = msg.get("content") or ""
                reasoning_ns = (msg.get("reasoning_content")
                                or msg.get("reasoning") or "")
                # 非流式兜底 —— 网关把思考以 <think> 标签混入 content
                if not reasoning_ns and content.lstrip().startswith("<think>") \
                        and "</think>" in content:
                    _head, _, _tail = content.partition("</think>")
                    reasoning_ns = _head.lstrip()[len("<think>"):].strip()
                    content = _tail.lstrip()
                # 非流式回退路径若网关仍返回了思考内容, 一并推送到
                # Web 前端(先思考后正文, 保证思考显示链路无死角)
                if reasoning_ns and self.thinking and self.on_delta \
                        and not internal:
                    self.on_delta("reasoning", reasoning_ns)
                if content and not self.quiet and not internal:
                    cprint(content, C.R)
                if content and self.on_delta and not internal:
                    self.on_delta("content", content)
                # 非流式路径也同步 toolgen 信号(loading 贯穿一致性)
                if (msg.get("tool_calls") and self.on_delta and not internal):
                    self.on_delta("toolgen", "")
                return {"content": content,
                        "reasoning": reasoning_ns,
                        "tool_calls": msg.get("tool_calls") or [], "usage": u}
            except urllib.error.HTTPError as e:
                log.error(payload)
                try:
                    body = e.read().decode("utf-8", errors="replace")[:600]
                except Exception as _e:
                    log.warning("读取HTTPError响应体失败(omni_agent/llm.py): %s: %s",
                                type(_e).__name__, _e)
                    body = ""
                detail = f"HTTP {e.code} {e.reason}" + (f" | {body}" if body else "")
                log.warning("llm http error (attempt %d/3): %s", attempt + 1, detail)
                # 4xx 客户端错误(429限流除外)重试必然复现, 立即失败并带上细节
                if 400 <= e.code < 500 and e.code != 429:
                    log.error("llm client error, no retry: %s", detail)
                    raise RuntimeError(detail) from e
                if attempt == 2:
                    log.error("llm request failed after 3 attempts: %s", detail)
                    raise RuntimeError(detail) from e
                cprint(f"  ! 请求失败重试 {attempt + 1}/3: {detail}", C.GRAY)
                time.sleep(2 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                log.warning("llm request retry %d/3: %s: %s",
                            attempt + 1, type(e).__name__, e)
                if attempt == 2:
                    log.error("llm request failed after 3 attempts: %s", e)
                    raise
                cprint(f"  ! 请求失败重试 {attempt + 1}/3: {e}", C.GRAY)
                time.sleep(2 * (attempt + 1))

    def _chat_stream(self, payload, fallback=False):
        resp = self._request(payload, fallback=fallback)
        try:
            return self._consume_stream(resp, model=payload.get("model", ""))
        finally:
            resp.close()   # v3.8.1: 异常/提前 break 时也确保释放连接

    def _consume_stream(self, resp, model=""):
        content, reasoning = [], []
        tool_calls = {}  # index -> {id, name, arguments}
        usage = {}
        # 部分 OpenAI 兼容网关(如 claude 适配层)把思考以 <think> 标签
        # 混入 content 流 —— 增量拆分器跨 chunk 边界安全地还原为 reasoning
        splitter = _ThinkTagSplitter() if self.thinking else None
        thinking_shown = answering = False
        toolgen_sent = False   # 首个工具调用增量到达时通知前端(loading贯穿)
        blank_think_seen = False   # 网关思考流仅回空白(claude 兼容网关
        # 不透传思考正文, 只回 "\n" 占位) —— 曾被静默丢弃, 用户误以为
        # "切换模型后思考模式坏了"; 现记录状态, 流结束后推送一次性说明
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError as _e:
                log.warning("忽略异常(omni_agent/llm.py:193): %s: %s", type(_e).__name__, _e)
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            if not chunk.get("choices"):
                continue
            delta = chunk["choices"][0].get("delta", {})
            # 字段名兜底 —— 部分 OpenAI 兼容网关将思考流放在 reasoning
            rc = delta.get("reasoning_content") or delta.get("reasoning") 
            if rc and not reasoning and not rc.strip():
                blank_think_seen = True
                rc = None
            if rc and self.thinking:
                if self.on_delta:
                    self.on_delta("reasoning", rc)
                reasoning.append(rc)
                if not self.quiet:
                    if not thinking_shown:
                        cprint("  ✻ 思考中: ", C.GRAY, end="")
                    total_len = sum(len(x) for x in reasoning)
                    if total_len <= 600:   # 思考过程灰色简略输出, 避免刷屏
                        print(f"{C.DIM}{rc}{C.R}", end="", flush=True)
                    elif total_len - len(rc) < 600:
                        print(f"{C.DIM}...{C.R}", end="", flush=True)
                thinking_shown = True
            c = delta.get("content")
            if c and splitter:
                r2, c = splitter.feed(c)
                if r2 and r2.strip() or (r2 and reasoning):
                    if self.on_delta:
                        self.on_delta("reasoning", r2)
                    reasoning.append(r2)
                    thinking_shown = True
            if c:
                if self.on_delta:
                    self.on_delta("content", c)
                if not self.quiet:
                    if thinking_shown and not answering:
                        print()
                    print(c, end="", flush=True)
                answering = True
                content.append(c)
            for tc in delta.get("tool_calls") or []:
                if not toolgen_sent and self.on_delta:
                    # 回答流已结束, 模型开始生成工具调用参数(可能耗时较长),
                    # 通知前端展示“智能体正在处理”loading(修复思考结束后到工具
                    # 展示前的无指示空窗)
                    self.on_delta("toolgen", "")
                    toolgen_sent = True
                idx = tc.get("index", 0)
                slot = tool_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function", {})
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
        if splitter:
            rest = splitter.flush()
            if rest:
                if splitter.in_think:
                    reasoning.append(rest)
                    if self.on_delta:
                        self.on_delta("reasoning", rest)
                else:
                    content.append(rest)
                    if self.on_delta:
                        self.on_delta("content", rest)
        if (answering or thinking_shown) and not self.quiet:
            print() 
        if (self.thinking and blank_think_seen and not reasoning
                and self.on_delta and not self._blank_think_notified):
            self._blank_think_notified = True
            # self.on_delta("sys", "💭 当前模型端点未透传思考正文(思考模式已开启, "
            # "模型内部推理正常, 但该网关不输出思考过程)。如需查看"
            # "思考内容, 可切换到支持思考透传的模型(如 qwen3-max / "
            # "qwen-plus / deepseek-r系列)") 
        if (self.thinking and not reasoning and not blank_think_seen
                and answering and self.on_delta
                and model and model not in self._no_think_models):
            self._no_think_models.add(model)
            # self.on_delta("sys", f"💭 模型 {model} 不输出思考过程"
            # "(思考模式已开启但该模型无思考内容返回)。如需查看"
            # "思考过程, 请切换到支持思考的模型(如 qwen3-max / "
            # "qwen-plus / deepseek-r系列 / kimi-k2思考版)") 
        _has_tc = bool(tool_calls)
        if (self.thinking and not reasoning and not blank_think_seen
                and not answering and _has_tc and self.on_delta
                and model and model not in self._no_think_models
                and model not in self._loop_no_think):
            self._loop_no_think.add(model)
            # self.on_delta("sys", f"💭 工具循环中 {model} 跳过了思考"
            # "(模型在已有工具调用历史时自动加速执行, 思考在最终"
            # "总结轮恢复)。如需每轮都看思考, 可用 qwen-plus / "
            # "deepseek-r系列(工具循环中稳定输出思考)。")
        self._count(usage)
        tcs = [{"id": v["id"] or f"call_{i}", "type": "function",
                "function": {"name": v["name"], "arguments": v["arguments"]}}
               for i, v in sorted(tool_calls.items())]
        return {"content": "".join(content), "reasoning": "".join(reasoning),
                "tool_calls": tcs, "usage": usage}

    def _count(self, u):
        self.calls += 1
        self.total_prompt += u.get("prompt_tokens", 0)
        self.total_completion += u.get("completion_tokens", 0)
        # 缓存命中统计(兼容 OpenAI prompt_tokens_details 与 Anthropic 字段)
        d = u.get("prompt_tokens_details") or {}
        self.total_cached += (d.get("cached_tokens") or 0) \
            or (u.get("cache_read_input_tokens") or 0)