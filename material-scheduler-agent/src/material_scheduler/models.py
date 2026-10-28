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


class OpenAIModel:
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


class FreeQuotaRouter:
    """Fail over only on explicit free-quota exhaustion, never disable billing protection."""

    mode = "live"

    def __init__(self, models, *, protection_confirmed=False):
        if not protection_confirmed:
            raise ModelError("configuration: confirm console free-quota-only protection with AGENT_FREE_ONLY_CONFIRMED=1")
        if not models or len(models) > 10 or not all(m.is_dashscope for m in models):
            raise ModelError("configuration: free-quota router requires 1..10 Dashscope models")
        self.models, self.index, self.switches = models, 0, []
        self.attempts = 0

    @property
    def model(self):
        return self.models[min(self.index, len(self.models)-1)].model

    @property
    def base_url(self):
        return self.models[0].base_url

    @property
    def usage(self):
        return {"api_calls": sum(m.usage["api_calls"] for m in self.models),
                "prompt_tokens": sum(m.usage["prompt_tokens"] for m in self.models),
                "completion_tokens": sum(m.usage["completion_tokens"] for m in self.models),
                "attempted_calls": self.attempts,
                "by_model": {m.model: dict(m.usage) for m in self.models}}

    def choose(self, messages, history, request):
        while self.index < len(self.models):
            if self.attempts >= 100:
                raise ModelError("model_call_budget_exhausted")
            current = self.models[self.index]
            self.attempts += 1
            try:
                return current.choose(messages, history, request)
            except ModelError as exc:
                if "AllocationQuota.FreeTierOnly" not in str(exc):
                    # Authentication, rate limits, malformed requests and other errors
                    # do not establish free-quota exhaustion. Stop and report them.
                    raise
                self.switches.append({"from": current.model,
                                      "to": self.models[self.index+1].model if self.index+1 < len(self.models) else None,
                                      "reason": "AllocationQuota.FreeTierOnly",
                                      "after_tool_steps": len(history)})
                self.index += 1
        raise ModelError("all_configured_free_quotas_exhausted; stopped without paid fallback")


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


def build_live_model():
    if os.getenv("AGENT_QUOTA_POLICY") == "user_managed":
        # Explicit caller policy: user selects the model and handles quota errors.
        # This does not assert that the provider console has free-only protection.
        model = OpenAIModel()
        model.quota_control = "user-selected model; stop on quota error; console free-only state unverified"
        return model
    # No runtime switch is capable of enabling the provider's console protection.
    # This flag records the caller's declaration, not an API-verified billing state.
    if os.getenv("AGENT_FREE_ONLY_CONFIRMED") != "1":
        raise ModelError("configuration: set console protection first, then AGENT_FREE_ONLY_CONFIRMED=1")
    names = [s.strip() for s in os.getenv("AGENT_MODELS", os.getenv("AGENT_MODEL", "qwen3.8-27b")).split(",") if s.strip()]
    names = list(dict.fromkeys(names))
    return FreeQuotaRouter([OpenAIModel(name) for name in names], protection_confirmed=True)


class OfflineMock:
    """Handwritten result-dependent policy. It does NOT prove model-driven ReAct."""

    mode = "mock"
    model = "offline-handwritten-policy"

    def choose(self, messages, history, request):
        def action(name, reason, **args):
            return Action(name, {**args, "reason": reason}, "mock_" + str(len(history) + 1))

        for item in request.items:
            mid = item.material_id
            events = [e for e in history if e["arguments"].get("material_id") == mid]
            reads = [e for e in events if e["tool"] == "read_material"]
            if not reads:
                return action("read_material", "先读取素材，避免凭空审核。", material_id=mid, include_supplement=False)
            last_read = reads[-1]
            if not last_read["observation"]["ok"]:
                continue
            checks = [e for e in events if e["tool"] == "check_rules" and e["step"] > last_read["step"]]
            if not checks:
                return action("check_rules", "依据已读取信息执行规则审核。", material_id=mid)
            review = checks[-1]["observation"].get("review_status")
            if review == "NEEDS_INFO" and not last_read["arguments"]["include_supplement"]:
                return action("read_material", "发现缺失字段，补查资料后重新审核。", material_id=mid, include_supplement=True)
            if review != "APPROVED":
                continue
            saves = [e for e in events if e["tool"] == "save_draft"]
            if saves and (saves[-1]["observation"]["ok"] or saves[-1]["observation"].get("code") != "SLOT_CONFLICT"):
                continue
            queries = [e for e in events if e["tool"] == "query_slots"]
            if not queries:
                return action("query_slots", "审核通过，先检查用户原定时段。", material_id=mid, include_alternatives=False)
            if saves and saves[-1]["step"] > queries[-1]["step"]:
                return action("query_slots", "保存时检测到新冲突，重新查询。", material_id=mid, include_alternatives=True)
            observation = queries[-1]["observation"]
            free = [r["slot"] for r in observation.get("slots", []) if r["available"]]
            existing = observation.get("existing_draft")
            preferred = [existing["suggested_slot"] if existing else None, item.approved_slot, item.requested_slot]
            slot = next((p for p in preferred if p in free), free[0] if free else None)
            if slot:
                return action("save_draft", "仅保存已查询的可用时段；确认状态由工具根据用户原始输入判定。", material_id=mid, slot=slot)
            if not queries[-1]["arguments"]["include_alternatives"]:
                return action("query_slots", "原时段冲突，查询替代时段并保留硬性要求。", material_id=mid, include_alternatives=True)
        return action("finish", "各素材已形成草稿或明确列出缺资料、风险、无可用时段或保存失败。")
