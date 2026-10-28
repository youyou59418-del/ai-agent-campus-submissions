"""Live model uses actual tool calling. OfflineMock is only a workflow simulator."""

from dataclasses import dataclass
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .tools import TOOL_SCHEMAS


class ModelError(Exception):
    """Sanitized provider/configuration failure; never include response bodies or keys."""


@dataclass
class Action:
    name: str
    args: dict
    call_id: str

    def message(self):
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": self.call_id, "type": "function", "function": {
                "name": self.name, "arguments": json.dumps(self.args, ensure_ascii=False)}}]}


class ChatModel:
    mode = "live"

    def __init__(self, model_name=None):
        self.model = model_name or os.getenv("AGENT_MODEL", "qwen3.8-flash")
        self.base_url = os.getenv("AGENT_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        key_env = os.getenv("AGENT_KEY_ENV", "DASHSCOPE_API_KEY")
        self.key = os.getenv(key_env)
        parsed = urllib.parse.urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query:
            raise ModelError("configuration: AGENT_BASE_URL must be an HTTPS base URL without credentials/query")
        if not self.key:
            raise ModelError("configuration: API key environment variable is missing")
        self.is_dashscope = parsed.hostname in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"}
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "api_calls": 0}

    def choose(self, messages, history, request):
        payload = {"model": self.model, "messages": messages, "tools": TOOL_SCHEMAS,
                   "tool_choice": "required", "parallel_tool_calls": False,
                   "temperature": 0, "max_tokens": 600}
        if self.is_dashscope and self.model.startswith("qwen3"):
            # Non-streaming tool calls use the non-thinking mode on this provider.
            payload["enable_thinking"] = False
        req = urllib.request.Request(self.base_url + "/chat/completions",
                                     data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                raw = response.read(2_000_000)
            result = json.loads(raw)
            self.usage["api_calls"] += 1
            for field in ("prompt_tokens", "completion_tokens"):
                self.usage[field] += result.get("usage", {}).get(field, 0)
            calls = result["choices"][0]["message"].get("tool_calls", [])
            if len(calls) != 1:
                raise ModelError("protocol: expected exactly one tool call")
            call = calls[0]
            return Action(call["function"]["name"], json.loads(call["function"]["arguments"]), call["id"])
        except urllib.error.HTTPError as exc:
            # Allow bounded machine-readable tags; provider messages can echo secrets.
            detail = "unknown"
            parameter_tags = []
            try:
                error = json.loads(exc.read(100_000)).get("error", {})
                candidate = error.get("code") or error.get("type")
                if isinstance(candidate, str) and re.fullmatch(r"[a-zA-Z_][a-zA-Z_.]{0,63}", candidate):
                    detail = candidate
                if detail == "unknown":
                    text = str(error.get("message", "")).lower()
                    if "quota" in text or "billing" in text:
                        detail = "quota_or_billing"
                    elif "rate limit" in text or "too many" in text:
                        detail = "rate_limited"
                message = str(error.get("message", "")).lower()
                parameter_tags = [p for p in ("enable_thinking", "tool_choice", "parallel_tool_calls", "max_tokens", "temperature", "strict", "stream") if p in message]
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            parameter_note = "; parameter_tags=" + ",".join(parameter_tags) if parameter_tags else ""
            raise ModelError(f"provider_http_{exc.code}:{detail}{parameter_note}; no automatic mock fallback") from None
        except (OSError, TimeoutError):
            raise ModelError("provider_connection_or_timeout; no automatic mock fallback") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise ModelError("provider_invalid_response") from None


class QuotaStopGuard:
    """Share a stop signal across evaluation workers after a quota failure."""

    def __init__(self, model, stop_event):
        self._model, self._stop_event = model, stop_event

    def __getattr__(self, name):
        return getattr(self._model, name)

    def choose(self, *args):
        if self._stop_event.is_set():
            raise ModelError("evaluation_stopped_after_quota_failure")
        try:
            return self._model.choose(*args)
        except ModelError as exc:
            if any(tag in str(exc) for tag in ("AllocationQuota.FreeTierOnly", "credit_balance_exhausted", "insufficient_quota", "quota_or_billing", "all_configured_free_quotas_exhausted")):
                self._stop_event.set()
            raise


class LazyChatModel:
    """Missing-input checks need no credentials and perform no API calls."""
    mode = "live"

    def __init__(self):
        self.model = os.getenv("AGENT_MODEL", "qwen3.8-flash")
        self.base_url = os.getenv("AGENT_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self._client = None

    @property
    def usage(self):
        return self._client.usage if self._client else {"prompt_tokens": 0, "completion_tokens": 0, "api_calls": 0}

    def choose(self, *args):
        if self._client is None:
            self._client = ChatModel()
        return self._client.choose(*args)


class OfflineMock:
    """Handwritten test policy. Uses controlled-topic helpers, not model reasoning."""
    mode,model="mock","offline-handwritten-policy"

    def choose(self,messages,history,context):
        tools,request=context["tools"],context["request"]
        corpus=tools.corpus
        intent=corpus.intent(request)
        def act(name,reason,**args):
            return Action(name,{**args,"reason":reason},"mock_"+str(len(history)+1))
        def submit(mode,key="",citations=None,reason="根据已观察证据结束本次任务。"):
            return act("submit_answer",reason,mode=mode,fact_key=key,citations=citations or [])
        if len(intent)>1 or (not intent and corpus.needs_topic_clarification(request)):
            return submit("clarify",reason="问题缺少明确指代，先追问主题而不猜测制度。")
        if tools.failures:
            last=tools.failures[-1]
            count=sum(f["tool"]==last["tool"] for f in tools.failures)
            if count>=2:
                return submit("tool_failure",reason="同一文档工具持续失败，停止并明确资料不可用。")
        if history and history[-1]["observation"].get("code")=="STALE_EVIDENCE":
            previous=history[-1]["arguments"]["citations"][0]
            return act("read_fragment","原文已变化，重新读取而不是沿用旧引用。",**{k:previous[k] for k in ("doc_id","version_date","fragment_id")})
        if not tools.searches:
            return act("search_documents","先检索与当前问题相关的文档。",query=request.question,strategy="literal" if not tools.failures else "expanded")
        if not intent:
            if len(tools.searches)<2:
                return act("search_documents","首次检索不足，补充检索后再判断资料缺口。",query=request.question+" 企业制度 FAQ",strategy="expanded")
            return submit("not_found",reason="补查后仍无与问题直接相关的依据，说明资料缺口。")
        key=intent[0]
        peers=corpus.peers(key)
        if not peers:
            if len(tools.searches)<2:
                return act("search_documents","原检索无依据，使用规范主题补查。",query=corpus.topics[key]["canonical_query"],strategy="expanded")
            return submit("not_found",reason="知识库没有该主题的原文，停止而不补造答案。")
        if any((f["doc_id"],f["version_date"],f["fragment_id"]) not in tools.hit_refs for f in peers):
            return act("search_documents","同义改写或补查相关版本，不能只凭空搜索结果回答。",query=corpus.topics[key]["canonical_query"],strategy="expanded")
        for fragment in peers:
            locator=(fragment["doc_id"],fragment["version_date"],fragment["fragment_id"])
            if locator not in tools.reads:
                return act("read_fragment","读取检索命中的原文与版本，建立可引用证据。",**{k:fragment[k] for k in ("doc_id","version_date","fragment_id")})
        evidence=[tools.reads[(f["doc_id"],f["version_date"],f["fragment_id"])] for f in peers]
        conflict=len({corpus.claim(f) for f in evidence})>1
        if conflict and not any(s["strategy"]=="expanded" and any(w in s["query"] for w in ("优先级","适用","范围","冲突")) for s in tools.searches):
            return act("search_documents","原文规则不同，补查适用范围和权威优先级。",query=corpus.topics[key]["canonical_query"]+" 适用范围 优先级",strategy="expanded")
        selected=evidence if conflict else evidence[:1]
        citations=[{k:f[k] for k in ("doc_id","version_date","fragment_id")}|{"quote":f["text"]} for f in selected]
        return submit("conflict" if conflict else "answer",key,citations,
                      "冲突无法裁定，列出各原文并请求确认适用制度。" if conflict else "原文已核对且没有已知冲突，提交完整引用。")


