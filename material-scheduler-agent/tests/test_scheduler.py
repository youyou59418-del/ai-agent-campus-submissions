from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from material_scheduler.agent import run_agent
from material_scheduler.domain import Item, Request
from material_scheduler.models import Action, ModelError, OfflineMock, OpenAIModel, FreeQuotaRouter, QuotaStopGuard, build_live_model
from material_scheduler.store import Store
from material_scheduler.tools import BusinessTools

SLOT = "2026-11-02T17:00:00+08:00"
BUSY = "2026-11-02T18:00:00+08:00"
ALT = "2026-11-02T19:00:00+08:00"


class FixedModel:
    mode, model = "test", "adversarial-action-stub"

    def __init__(self, name, args):
        self.name, self.args = name, args

    def choose(self, *unused):
        return Action(self.name, self.args, "fixed")


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.data = self.path / "data"
        shutil.copytree(ROOT / "data", self.data)
        self.store = Store(self.path / "test.sqlite")

    def req(self, mid="M01", slot=SLOT, request_id="test", **kwargs):
        return Request(request_id, (Item(mid, slot, **kwargs),))

    def tools(self, req=None, **kwargs):
        return BusinessTools(req or self.req(), self.store, self.data, **kwargs)

    def run_mock(self, req=None, **kwargs):
        req = req or self.req()
        return run_agent(req, self.tools(req, **kwargs), OfflineMock())

    def prepared(self, req=None):
        tools = self.tools(req)
        mid = tools.request.items[0].material_id
        tools.read_material(mid, False)
        tools.check_rules(mid)
        tools.query_slots(mid, True)
        return tools

    def test_normal_is_ready_draft_never_published(self):
        result, _ = self.run_mock()
        item = result["items"][0]
        self.assertEqual(item["draft"]["status"], "READY_DRAFT")
        self.assertFalse(item["published"])
        self.assertEqual(self.store.count(), 1)

    def test_missing_metadata_queries_supplement_then_stops(self):
        result, trace = self.run_mock(self.req("M02"))
        self.assertEqual(result["items"][0]["outcome"], "NEEDS_INFO")
        self.assertEqual([e["tool"] for e in trace["steps"]].count("read_material"), 2)
        self.assertEqual(self.store.count(), 0)

    def test_supplement_can_change_next_decision(self):
        result, trace = self.run_mock(self.req("M07"))
        self.assertEqual(result["items"][0]["outcome"], "DRAFT_SAVED")
        reviews = [e["observation"]["review_status"] for e in trace["steps"] if e["tool"] == "check_rules"]
        self.assertEqual(reviews, ["NEEDS_INFO", "APPROVED"])

    def test_unknown_material_requests_information(self):
        result, _ = self.run_mock(self.req("NOT_FOUND"))
        self.assertEqual(result["items"][0]["outcome"], "NEEDS_INFO")
        self.assertEqual(self.store.count(), 0)

    def test_copyright_risk_blocks_save(self):
        result, _ = self.run_mock(self.req("M03"))
        self.assertEqual(result["items"][0]["outcome"], "BLOCKED")
        self.assertEqual(self.store.count(), 0)

    def test_uncertain_consent_requests_human_review(self):
        result, _ = self.run_mock(self.req("M04"))
        self.assertEqual(result["items"][0]["outcome"], "NEEDS_REVIEW")
        self.assertIn("consent_confirmed", result["items"][0]["pending_items"])

    def test_conflict_retains_hard_time_and_marks_pending(self):
        result, trace = self.run_mock(self.req(slot=BUSY))
        draft = result["items"][0]["draft"]
        self.assertEqual(draft["requested_slot"], BUSY)
        self.assertEqual(draft["suggested_slot"], ALT)
        self.assertEqual(draft["status"], "PENDING_CONFIRMATION")
        self.assertTrue(draft["requires_confirmation"])
        self.assertEqual([e["arguments"]["include_alternatives"] for e in trace["steps"] if e["tool"] == "query_slots"], [False, True])

    def test_flexible_request_authorizes_alternative(self):
        result, _ = self.run_mock(self.req(slot=BUSY, hard_requirement=False))
        self.assertFalse(result["items"][0]["draft"]["requires_confirmation"])

    def test_explicit_user_approval_accepts_only_exact_slot(self):
        result, _ = self.run_mock(self.req(slot=BUSY, approved_slot=ALT))
        draft = result["items"][0]["draft"]
        self.assertEqual(draft["approval_source"], "user_request")
        self.assertEqual(draft["status"], "READY_DRAFT")

    def test_different_approved_slot_does_not_approve_arbitrary_alternative(self):
        req = self.req(slot=BUSY, approved_slot="2026-11-02T21:00:00+08:00")
        result = self.prepared(req).save_draft("M01", ALT)
        self.assertTrue(result["draft"]["requires_confirmation"])

    def test_duplicate_submit_has_same_id_and_one_row(self):
        first, _ = self.run_mock(self.req(slot=BUSY))
        second, _ = self.run_mock(self.req(slot=BUSY))
        self.assertEqual(first["items"][0]["draft"], second["items"][0]["draft"])
        self.assertEqual(self.store.count(), 1)

    def test_confirmation_upgrades_same_draft(self):
        first, _ = self.run_mock(self.req(slot=BUSY))
        second, _ = self.run_mock(self.req(slot=BUSY, approved_slot=ALT))
        self.assertEqual(first["items"][0]["draft"]["draft_id"], second["items"][0]["draft"]["draft_id"])
        self.assertFalse(second["items"][0]["draft"]["requires_confirmation"])
        self.assertEqual(second["items"][0]["draft"]["revision"], 2)
        self.assertEqual(self.store.count(), 1)

    def test_old_request_does_not_erase_recorded_user_approval(self):
        self.run_mock(self.req(slot=BUSY, approved_slot=ALT))
        result, _ = self.run_mock(self.req(slot=BUSY))
        self.assertFalse(result["items"][0]["draft"]["requires_confirmation"])

    def test_changed_requirements_same_key_rejected(self):
        self.run_mock()
        tool = self.prepared(self.req(slot=ALT))
        self.assertEqual(tool.save_draft("M01", ALT)["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.store.count(), 1)

    def test_existing_valid_draft_not_moved_on_repeat(self):
        first = self.prepared().save_draft("M01", SLOT)
        req = self.req()
        tool = self.prepared(req)
        result = tool.save_draft("M01", ALT)
        self.assertEqual(result["code"], "EXISTING_DRAFT")
        self.assertEqual(result["draft"], first["draft"])

    def test_changed_occupancy_replans_same_draft_id(self):
        req = self.req(slot=BUSY)
        first, _ = self.run_mock(req)
        schedule = json.loads((self.data / "schedule.json").read_text())
        schedule["occupied"].append({"channel": "xiaoying", "slot": ALT, "reference": "new"})
        (self.data / "schedule.json").write_text(json.dumps(schedule))
        second, _ = self.run_mock(req)
        a, b = first["items"][0]["draft"], second["items"][0]["draft"]
        self.assertEqual(a["draft_id"], b["draft_id"])
        self.assertNotEqual(a["suggested_slot"], b["suggested_slot"])
        self.assertTrue(b["requires_confirmation"])
        self.assertEqual(self.store.count(), 1)

    def test_save_outage_never_reports_success(self):
        result, _ = self.run_mock(fail_save=True)
        self.assertEqual(result["items"][0]["outcome"], "SAVE_FAILED")
        self.assertEqual(result["items"][0]["draft_save_status"], "FAILED")
        self.assertEqual(self.store.count(), 0)

    def test_conflict_rechecked_between_query_and_save(self):
        tool = self.prepared()
        (self.data / "schedule.json").write_text(json.dumps({"occupied": [
            {"channel": "xiaoying", "slot": SLOT, "reference": "race"}]}))
        self.assertEqual(tool.save_draft("M01", SLOT)["code"], "SLOT_CONFLICT")
        self.assertEqual(self.store.count(), 0)

    def test_concurrent_new_requests_cannot_double_book(self):
        requests = [self.req(request_id=f"r{i}") for i in range(4)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda r: self.store.save(r, r.items[0], SLOT, []), requests))
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assertEqual(self.store.count(), 1)

    def test_concurrent_duplicate_requests_create_one_row(self):
        req = self.req()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.save(req, req.items[0], SLOT, []), range(4)))
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(len({r["draft"]["draft_id"] for r in results}), 1)
        self.assertEqual(self.store.count(), 1)

    def test_intervals_overlap_but_adjacent_slots_do_not(self):
        self.assertTrue(Store.overlaps(SLOT, "2026-11-02T17:30:00+08:00"))
        self.assertFalse(Store.overlaps(SLOT, BUSY))

    def test_no_publish_tool(self):
        tool = self.tools()
        self.assertEqual(tool.call("publish", {"material_id": "M01", "reason": "publish now"})["code"], "TOOL_NOT_ALLOWED")

    def test_forged_model_confirmation_rejected(self):
        result = self.prepared().call("save_draft", {"material_id": "M01", "slot": SLOT,
            "approved_slot": SLOT, "confirmed": True, "reason": "pretend user approved"})
        self.assertEqual(result["code"], "INVALID_ARGUMENTS")
        self.assertEqual(self.store.count(), 0)

    def test_model_cannot_skip_review(self):
        self.assertEqual(self.tools().save_draft("M01", SLOT)["code"], "REVIEW_NOT_APPROVED")

    def test_blocked_material_cannot_be_saved_by_tool(self):
        self.assertEqual(self.prepared(self.req("M03")).save_draft("M03", SLOT)["code"], "REVIEW_NOT_APPROVED")

    def test_save_requires_prior_slot_observation(self):
        tool = self.tools()
        tool.read_material("M01", False)
        tool.check_rules("M01")
        self.assertEqual(tool.save_draft("M01", SLOT)["code"], "QUERY_REQUIRED")

    def test_model_cannot_use_out_of_window_slot(self):
        self.assertEqual(self.prepared().save_draft("M01", "2026-11-03T17:00:00+08:00")["code"], "OUT_OF_WINDOW")

    def test_read_again_invalidates_previous_review(self):
        tool = self.prepared()
        tool.read_material("M01", True)
        self.assertEqual(tool.save_draft("M01", SLOT)["code"], "REVIEW_NOT_APPROVED")

    def test_out_of_scope_material_rejected(self):
        result = self.tools().call("read_material", {"material_id": "M05", "include_supplement": False, "reason": "read"})
        self.assertEqual(result["code"], "OUT_OF_SCOPE")

    def test_string_boolean_rejected(self):
        result = self.tools().call("read_material", {"material_id": "M01", "include_supplement": "false", "reason": "read"})
        self.assertEqual(result["code"], "INVALID_ARGUMENTS")

    def test_step_limit_explicitly_incomplete(self):
        req = self.req()
        result, _ = run_agent(req, self.tools(req), OfflineMock(), max_steps=1)
        self.assertEqual(result["termination"], "STEP_LIMIT")
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_premature_finish_cannot_fake_completion(self):
        result, _ = run_agent(self.req(), self.tools(), FixedModel("finish", {"reason": "all done"}))
        self.assertEqual(result["status"], "INCOMPLETE")

    def test_unknown_action_is_bounded(self):
        result, trace = run_agent(self.req(), self.tools(), FixedModel("publish", {}), max_steps=2)
        self.assertEqual(result["termination"], "STEP_LIMIT")
        self.assertEqual(len(trace["steps"]), 2)
        self.assertEqual(self.store.count(), 0)

    def test_provider_failure_has_no_mock_fallback(self):
        class Failed(OfflineMock):
            mode = "live"
            def choose(self, *args):
                raise ModelError("provider_http_429")
        result, trace = run_agent(self.req(), self.tools(), Failed())
        self.assertEqual(result["mode"], "live")
        self.assertEqual(result["termination"], "MODEL_FAILURE")
        self.assertEqual(trace["steps"], [])

    def test_trace_includes_observation_and_next_action(self):
        _, trace = self.run_mock(self.req(slot=BUSY))
        for event in trace["steps"]:
            self.assertIn("observation", event)
            self.assertIn("next_action", event)
            self.assertTrue(event["decision_basis"])

    def test_multi_material_drafts_have_distinct_slots(self):
        req = Request("multi", (Item("M01", BUSY), Item("M05", BUSY)))
        result, _ = run_agent(req, self.tools(req), OfflineMock(), max_steps=24)
        self.assertEqual(len({r["suggested_slot"] for r in result["items"]}), 2)
        self.assertEqual(self.store.count(), 2)

    def test_no_available_slot_requests_new_window(self):
        (self.data / "schedule.json").write_text(json.dumps({"occupied": [
            {"channel": "xiaoying", "slot": f"2026-11-02T{h}:00:00+08:00", "reference": str(h)} for h in range(18,22)]}))
        result, _ = self.run_mock(self.req(slot=BUSY))
        self.assertEqual(result["items"][0]["outcome"], "NO_AVAILABLE_SLOT")
        self.assertIn("provide_new_time_window", result["items"][0]["pending_items"])


class InputValidationTest(unittest.TestCase):
    def raw(self, **extra):
        return {"request_id": "r", "items": [{"material_id": "M01", "requested_slot": SLOT, **extra}]}

    def test_default_hard_requirement(self):
        self.assertTrue(Request.parse(self.raw()).items[0].hard_requirement)

    def test_reject_naive_time(self):
        with self.assertRaises(ValueError):
            Request.parse(self.raw(requested_slot="2026-11-02T17:00:00"))

    def test_reject_non_hourly_time(self):
        with self.assertRaises(ValueError):
            Request.parse(self.raw(requested_slot="2026-11-02T17:30:00+08:00"))

    def test_reject_string_hard_requirement(self):
        with self.assertRaises(ValueError):
            Request.parse(self.raw(hard_requirement="false"))

    def test_reject_duplicate_materials(self):
        raw = self.raw()
        raw["items"] *= 2
        with self.assertRaises(ValueError):
            Request.parse(raw)

    def test_reject_empty_materials(self):
        with self.assertRaises(ValueError):
            Request.parse({"request_id": "r", "items": []})

    def test_equivalent_iso_input_is_canonicalized(self):
        req = Request.parse(self.raw(requested_slot="2026-11-02 17:00:00+08:00"))
        self.assertEqual(req.items[0].requested_slot, SLOT)


class ProviderAdapterTest(unittest.TestCase):
    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-key", "AGENT_MODEL": "test-model", "AGENT_BASE_URL": "https://api.openai.com/v1", "AGENT_KEY_ENV": "OPENAI_API_KEY"})
    def test_actual_tool_response_is_parsed(self):
        response = {"choices": [{"message": {"tool_calls": [{"id": "abc", "function": {
            "name": "finish", "arguments": json.dumps({"reason": "done"})}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        with patch("urllib.request.urlopen", return_value=Response(json.dumps(response).encode())) as mocked:
            model = OpenAIModel()
            action = model.choose([], [], None)
            sent = json.loads(mocked.call_args.args[0].data)
            self.assertEqual(sent["tool_choice"], "required")
            self.assertFalse(sent["parallel_tool_calls"])
            self.assertEqual(action.name, "finish")
            self.assertEqual(model.usage["api_calls"], 1)

    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-key", "AGENT_BASE_URL": "https://api.openai.com/v1", "AGENT_KEY_ENV": "OPENAI_API_KEY"})
    def test_http_error_does_not_leak_response_body(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError("url", 401, "secret-error-body", {}, None)):
            with self.assertRaisesRegex(ModelError, "provider_http_401") as caught:
                OpenAIModel().choose([], [], None)
        self.assertNotIn("secret-error-body", str(caught.exception))

    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-key", "AGENT_BASE_URL": "https://api.openai.com/v1", "AGENT_KEY_ENV": "OPENAI_API_KEY"})
    def test_provider_quota_tag_is_preserved_without_raw_message(self):
        body = json.dumps({"error": {"code": "AllocationQuota.FreeTierOnly", "message": "synthetic-test-key must stay private"}}).encode()
        error = urllib.error.HTTPError("url", 403, "forbidden", {}, io.BytesIO(body))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ModelError) as caught:
                OpenAIModel().choose([], [], None)
        self.assertIn("AllocationQuota.FreeTierOnly", str(caught.exception))
        self.assertNotIn("synthetic-test-key", str(caught.exception))


class FreeQuotaRouterTest(unittest.TestCase):
    class Provider:
        is_dashscope = True
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        def __init__(self, name, error=None):
            self.model, self.error, self.calls = name, error, 0
            self.usage = {"api_calls": 0, "prompt_tokens": 0, "completion_tokens": 0}
        def choose(self, *args):
            self.calls += 1
            if self.error:
                raise ModelError(self.error)
            self.usage["api_calls"] += 1
            return Action("finish", {"reason": "done"}, "synthetic")

    def test_no_calls_without_console_protection_declaration(self):
        provider = self.Provider("a")
        with self.assertRaises(ModelError):
            FreeQuotaRouter([provider], protection_confirmed=False)
        self.assertEqual(provider.calls, 0)

    def test_quota_exhaustion_switches_with_same_context(self):
        a = self.Provider("a", "provider_http_403:AllocationQuota.FreeTierOnly")
        b = self.Provider("b")
        router = FreeQuotaRouter([a, b], protection_confirmed=True)
        action = router.choose([], [], None)
        self.assertEqual(action.name, "finish")
        self.assertEqual(router.model, "b")
        self.assertEqual(router.switches[0]["from"], "a")
        self.assertEqual(router.usage["attempted_calls"], 2)

    def test_exhausted_models_are_not_retried_or_paid_fallback(self):
        a = self.Provider("a", "provider_http_403:AllocationQuota.FreeTierOnly")
        router = FreeQuotaRouter([a], protection_confirmed=True)
        for _ in range(2):
            with self.assertRaisesRegex(ModelError, "all_configured_free_quotas_exhausted"):
                router.choose([], [], None)
        self.assertEqual(a.calls, 1)

    def test_rate_limit_is_not_misclassified_as_quota_exhaustion(self):
        a, b = self.Provider("a", "provider_http_429:limit_requests"), self.Provider("b")
        router = FreeQuotaRouter([a, b], protection_confirmed=True)
        with self.assertRaisesRegex(ModelError, "limit_requests"):
            router.choose([], [], None)
        self.assertEqual(b.calls, 0)

    def test_non_dashscope_provider_rejected_in_free_only_route(self):
        a = self.Provider("a")
        a.is_dashscope = False
        with self.assertRaises(ModelError):
            FreeQuotaRouter([a], protection_confirmed=True)

    def test_live_factory_requires_explicit_protection_declaration(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ModelError, "console protection"):
                build_live_model()

    def test_quota_failure_stops_other_workers_before_their_next_call(self):
        stop = Event()
        a = self.Provider("a", "provider_http_403:AllocationQuota.FreeTierOnly")
        b = self.Provider("b")
        with self.assertRaises(ModelError):
            QuotaStopGuard(a, stop).choose([], [], None)
        with self.assertRaisesRegex(ModelError, "stopped_after_quota_failure"):
            QuotaStopGuard(b, stop).choose([], [], None)
        self.assertEqual(b.calls, 0)

    def test_non_quota_error_does_not_disable_unrelated_cases(self):
        stop = Event()
        a = self.Provider("a", "provider_http_400:invalid_parameter_error")
        with self.assertRaises(ModelError):
            QuotaStopGuard(a, stop).choose([], [], None)
        self.assertFalse(stop.is_set())

    def test_user_managed_policy_does_not_claim_billing_protection(self):
        with patch.dict(os.environ, {"AGENT_QUOTA_POLICY": "user_managed", "DASHSCOPE_API_KEY": "synthetic-test-key"}, clear=True):
            model = build_live_model()
        self.assertIsInstance(model, OpenAIModel)
        self.assertIn("unverified", model.quota_control)

    def test_qwen_non_streaming_payload_disables_thinking(self):
        response = {"choices": [{"message": {"tool_calls": [{"id": "a", "function": {
            "name": "finish", "arguments": '{"reason":"done"}'}}]}}]}
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "synthetic-test-key"}, clear=True):
            with patch("urllib.request.urlopen", return_value=Response(json.dumps(response).encode())) as mocked:
                OpenAIModel().choose([], [], None)
        self.assertFalse(json.loads(mocked.call_args.args[0].data)["enable_thinking"])

    @patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-test-key", "AGENT_BASE_URL": "https://api.openai.com/v1", "AGENT_KEY_ENV": "OPENAI_API_KEY"})
    def test_non_machine_error_tag_cannot_echo_key(self):
        body = json.dumps({"error": {"code": "synthetic-test-key", "message": "synthetic-test-key"}}).encode()
        error = urllib.error.HTTPError("url", 401, "forbidden", {}, io.BytesIO(body))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(ModelError) as caught:
                OpenAIModel().choose([], [], None)
        self.assertNotIn("synthetic-test-key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
