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
        self.model = model_name or os.getenv("AGENT_MODEL", "qwen3.8-27b")
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
        self.model = os.getenv("AGENT_MODEL", "qwen3.8-27b")
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
    """Handwritten state policy; proves workflow only, not model ReAct quality."""
    mode, model = "mock", "offline-handwritten-policy"

    def choose(self, messages, history, context):
        state, raw = context["state"], context["input"]
        def act(name, reason, **args):
            return Action(name, {**args, "reason": reason}, "mock_"+str(len(history)+1))
        if state["saved_plan"]:
            return act("finish", "已核对持久化方案，重复请求复用结果。")
        strategy = state["strategy"]
        if strategy is None:
            return act("select_plan", "从用户指定的初始方案开始规划。", strategy=raw["initial_strategy"])
        estimate = state["latest_estimate"]
        if not estimate or not state["estimate_is_current"]:
            return act("estimate_cost", "先核对已花费、剩余步骤费用与截止时间。")
        if not estimate["feasible"]:
            other = next((q["strategy"] for q in state["quotes"] if q["feasible"]), None)
            if other:
                return act("select_plan", "当前方案超出约束，改选可行方案并保留已有结果。", strategy=other)
            return act("finish", "没有同时满足预算和截止时间的方案，报告缺口。")
        latest = {}
        for step in state["step_attempts"]:
            latest[step["step_key"]] = step
        needed = estimate["min_materials"]
        if len(state["materials"]) < needed:
            source = "primary" if "search:primary" not in latest else "backup"
            if "search:backup" in latest and latest["search:backup"]["status"] == "FAILED":
                return act("finish", "主、备用检索均无素材，保留失败记录并停止。")
            return act("search_materials", "检索生成脚本所需素材，复用已有子结果。", source=source)
        script = latest.get("script:"+strategy)
        if not script or script["status"] != "SUCCEEDED":
            failures = sum(s["step_key"] == "script:"+strategy and s["status"] == "FAILED" for s in state["step_attempts"])
            if failures >= 2:
                alternative = next((q["strategy"] for q in state["quotes"] if q["strategy"] != strategy and q["feasible"] and sum(s["step_key"] == "script:"+q["strategy"] and s["status"] == "FAILED" for s in state["step_attempts"]) < 2), None)
                if alternative:
                    return act("select_plan", "当前生成工具持续失败，尝试替代方案。", strategy=alternative)
                return act("finish", "失败步骤已达重试上限，停止并保留已成功的素材。")
            return act("generate_script", "素材依赖已满足，执行或续跑未完成的脚本步骤。")
        if sum(s["step_key"] == "save" and s["status"] == "FAILED" for s in state["step_attempts"]) >= 2:
            return act("finish", "保存持续失败，保留脚本和素材供后续续跑。")
        return act("save_plan", "按最新费用与截止时间校验结果保存方案。")


