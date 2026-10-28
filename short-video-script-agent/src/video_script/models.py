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
                   "temperature": 0, "max_tokens": 1400}
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


def basic_spec(request,fact,holds=None,style=None):
    style=style or request.style
    suffix={"自然":"NAT","轻松":"LIGHT","专业":"PRO"}[style]
    if holds is None:
        budget=max(0,request.target_seconds-2)
        holds=[budget//3,budget//3,budget-2*(budget//3)]
    def scene(sid,visual,speech,hold):
        factual=speech=="FACT"
        return {"scene_id":sid,"visual_id":visual,"speech_id":speech,"source_id":fact["source_id"] if factual else "",
                "fact_id":fact["fact_id"] if factual else "","source_quote":fact["text"] if factual else "","hold_seconds":hold}
    return {"title_id":"T_"+suffix,"scenes":[scene(1,"V_HOOK","H_"+suffix,holds[0]),
            scene(2,"V_FACT","FACT",holds[1]),scene(3,"V_CTA","C_"+suffix,holds[2])]}


class OfflineMock:
    """Handwritten workflow policy, not proof of real model script decisions."""
    mode,model="mock","offline-handwritten-policy"

    def choose(self,messages,history,context):
        tools,request=context["tools"],context["request"]
        def act(name,reason,**args): return Action(name,{**args,"reason":reason},"mock_"+str(len(history)+1))
        if tools.failures:
            counts={name:sum(f["tool"]==name for f in tools.failures) for name in {f["tool"] for f in tools.failures}}
            if max(counts.values())>=2:
                return act("stop","同一检查工具持续失败，不能批准未检查的脚本。",reason_code="tool_failure")
        if not tools.searches:
            return act("search_materials","先检索与主题直接相关的产品事实。",query=request.topic,strategy="literal" if not tools.failures else "expanded")
        facts=tools.relevant_retrieved()
        if not facts:
            if len(tools.searches)<2:
                return act("search_materials","首次没有相关素材，改用扩展检索。",query=request.topic,strategy="expanded")
            return act("stop","补查后仍无产品事实，先请求素材而不编造功能。",reason_code="no_materials")
        if not tools.drafts:
            return act("write_draft","用已确认事实与表达库编排初稿。",**basic_spec(request,facts[0]))
        latest=list(tools.drafts)[-1]
        if latest not in tools.estimates:
            return act("estimate_duration","逐分镜估算当前版本，不依赖模型猜测时长。",draft_id=latest)
        if latest not in tools.checks:
            return act("check_rules","核对来源、事实、风格和品牌规则。",draft_id=latest)
        estimate,check=tools.estimates[latest],tools.checks[latest]
        if estimate["passed"] and check["passed"]:
            return act("submit_script","当前版本时长与事实/品牌检查均通过，交付可编辑脚本。",draft_id=latest)
        codes={r["code"] for r in check["risks"]}
        lower=tools.minimum_seconds()
        if "REQUESTED_FACT_NOT_SUPPORTED" in codes or lower is not None and lower>request.target_seconds or len(tools.drafts)>=2:
            return act("stop","现有素材或最短可行估算不能满足硬性要求，明确说明缺口。",reason_code="constraints")
        return act("write_draft","根据检查结果压缩停留、调整表达并保留同一产品事实。",**basic_spec(request,facts[0]))


