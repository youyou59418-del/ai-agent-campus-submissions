from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
import urllib.error

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from content_planner.agent import run_agent
from content_planner.domain import Request,cents,units
from content_planner.models import Action,ChatModel,LazyChatModel,ModelError,OfflineMock,QuotaStopGuard
from content_planner.store import RequirementConflict,Store
from content_planner.tools import BusinessTools


def raw(budget=20,deadline="2026-11-03T09:30:00+08:00",**extra):
    return {"request_id":"test","topic":"秋日创作活动","budget":budget,"deadline":deadline,
            "start_time":"2026-11-03T09:00:00+08:00","initial_strategy":"premium",**extra}


class FixedModel:
    mode,model="test","fixed-action-adversary"
    def __init__(self,name,args): self.name,self.args=name,args
    def choose(self,*args): return Action(self.name,self.args,"fixed")


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.data=self.root/"data"
        shutil.copytree(ROOT/"data",self.data)
        self.store=Store(self.root/"state.sqlite")

    def request(self,**kwargs): return Request.parse(raw(**kwargs))

    def tools(self,request=None,faults=()):
        return BusinessTools(request or self.request(),self.store,self.data,faults)

    def run_mock(self,request=None,**kwargs):
        return run_agent(request or self.request(),self.store,self.data,OfflineMock(),**kwargs)

    def prepared(self,request=None,strategy="premium",faults=()):
        tools=self.tools(request,faults)
        tools.select_plan(tools.request.initial_strategy)
        tools.estimate_cost()
        if strategy != tools.request.initial_strategy:
            tools.select_plan(strategy)
            tools.estimate_cost()
        return tools

    def generated(self,request=None,strategy="premium",faults=()):
        tools=self.prepared(request,strategy,faults)
        tools.search_materials("primary")
        tools.generate_script()
        return tools

    def test_normal_actual_cost_and_deadline(self):
        result,_=self.run_mock()
        self.assertEqual(result["status"],"COMPLETED")
        self.assertEqual(result["actual_total_units"],"12.00")
        self.assertEqual(result["elapsed_minutes"],9)
        self.assertFalse(result["published"])

    def test_12_over_budget_changes_to_8(self):
        result,trace=self.run_mock(self.request(budget=10))
        estimates=[e["observation"]["estimate"]["estimated_total_units"] for e in trace["steps"] if e["tool"]=="estimate_cost"]
        self.assertLess(estimates.index("12.00"),estimates.index("8.00"))
        self.assertEqual(result["strategy"],"economy")
        self.assertEqual(result["actual_total_units"],"8.00")

    def test_no_feasible_budget_reports_gap_without_spending(self):
        result,_=self.run_mock(self.request(budget=5))
        self.assertEqual(result["status"],"INCOMPLETE")
        self.assertEqual(result["actual_total_units"],"0.00")
        self.assertIn("NO_FEASIBLE_PLAN",result["uncompleted_reasons"])
        self.assertEqual(min(cents(q["budget_gap_units"]) for q in result["feasibility"]),300)

    def test_deadline_replanning(self):
        result,_=self.run_mock(self.request(deadline="2026-11-03T09:08:00+08:00"))
        self.assertEqual(result["status"],"COMPLETED")
        self.assertEqual(result["strategy"],"economy")
        self.assertEqual(result["elapsed_minutes"],7)

    def test_impossible_deadline_no_spend(self):
        result,_=self.run_mock(self.request(deadline="2026-11-03T09:04:00+08:00"))
        self.assertEqual(result["status"],"INCOMPLETE")
        self.assertEqual(result["step_attempts"],[])
        self.assertEqual(min(q["deadline_gap_minutes"] for q in result["feasibility"]),3)

    def test_missing_budget_and_deadline_no_model_or_tool_calls(self):
        with patch.dict(os.environ,{},clear=True):
            result,trace=run_agent(self.request(budget=None,deadline=None),self.store,self.data,LazyChatModel())
        self.assertEqual(result["status"],"NEEDS_INPUT")
        self.assertEqual(result["missing_fields"],["budget","deadline"])
        self.assertEqual(trace["usage"]["api_calls"],0)
        self.assertEqual(result["step_attempts"],[])

    def test_single_missing_budget(self):
        result,_=self.run_mock(self.request(budget=None))
        self.assertEqual(result["missing_fields"],["budget"])

    def test_single_missing_deadline(self):
        result,_=self.run_mock(self.request(deadline=None))
        self.assertEqual(result["missing_fields"],["deadline"])

    def test_failed_generation_fee_is_not_forgotten(self):
        result,_=self.run_mock(faults=["generate_once"])
        self.assertEqual(result["actual_total_units"],"13.00")
        script=[s for s in result["step_attempts"] if s["step_key"]=="script:premium"]
        self.assertEqual([s["status"] for s in script],["FAILED","SUCCEEDED"])
        self.assertEqual([s["charged_units"] for s in script],["1.00","10.00"])

    def test_failure_changes_remaining_budget_and_replans(self):
        result,_=self.run_mock(self.request(budget=12),faults=["generate_once"])
        self.assertEqual(result["strategy"],"economy")
        self.assertEqual(result["actual_total_units"],"9.00")
        self.assertEqual(sum(s["step_key"]=="search:primary" for s in result["step_attempts"]),1)

    def test_partial_success_kept_partial_and_reused(self):
        result,_=self.run_mock(self.request(budget=12),faults=["search_partial"])
        self.assertEqual(result["status"],"COMPLETED")
        self.assertEqual(result["actual_total_units"],"7.00")
        self.assertEqual(result["step_attempts"][0]["status"],"PARTIAL")
        self.assertEqual(result["task_plan"][0]["status"],"PARTIAL")
        self.assertEqual(len(result["saved_plan"]["material_ids"]),1)

    def test_resume_preserves_prefix_and_does_not_reexecute_search(self):
        request=self.request(budget=12)
        first,_=self.run_mock(request,faults=["generate_once"],interrupt_after_failure=True)
        second,_=self.run_mock(request,faults=["generate_once"])
        self.assertEqual(first["termination"],"INTERRUPTED_AFTER_FAILURE")
        self.assertEqual(second["step_attempts"][:len(first["step_attempts"])],first["step_attempts"])
        self.assertEqual(second["actual_total_units"],"9.00")
        self.assertEqual(sum(s["step_key"]=="search:primary" for s in second["step_attempts"]),1)

    def test_repeat_execution_reuses_plan_id_and_all_steps(self):
        first,_=self.run_mock()
        second,trace=self.run_mock()
        self.assertEqual(first["saved_plan"],second["saved_plan"])
        self.assertEqual(first["step_attempts"],second["step_attempts"])
        self.assertEqual(len(trace["steps"]),1)

    def test_same_id_different_budget_rejected(self):
        self.run_mock()
        with self.assertRaises(RequirementConflict):
            self.run_mock(self.request(budget=10))

    def test_same_id_different_deadline_rejected(self):
        self.tools()
        with self.assertRaises(RequirementConflict):
            self.tools(self.request(deadline="2026-11-03T09:40:00+08:00"))

    def test_missing_input_can_be_completed_with_same_id(self):
        self.run_mock(self.request(budget=None))
        result,_=self.run_mock()
        self.assertEqual(result["status"],"COMPLETED")

    def test_repeated_successful_search_is_free(self):
        tools=self.prepared()
        first=tools.search_materials("primary")
        second=tools.search_materials("primary")
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])
        self.assertEqual(tools.state()["spent_units"],"2.00")

    def test_repeated_partial_result_is_not_charged_again(self):
        tools=self.prepared(faults=["search_partial"])
        tools.search_materials("primary")
        cached=tools.search_materials("primary")
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["step_status"],"PARTIAL")
        self.assertEqual(tools.state()["spent_units"],"1.00")

    def test_script_requires_plan(self):
        self.assertEqual(self.tools().generate_script()["code"],"PLAN_REQUIRED")

    def test_execution_requires_cost_estimate(self):
        tools=self.tools()
        tools.select_plan("premium")
        self.assertEqual(tools.search_materials("primary")["code"],"ESTIMATE_REQUIRED")

    def test_initial_draft_estimate_cannot_be_skipped(self):
        tools=self.tools(self.request(budget=10))
        self.assertEqual(tools.select_plan("economy")["code"],"INITIAL_PLAN_REQUIRED")
        tools.select_plan("premium")
        self.assertEqual(tools.select_plan("economy")["code"],"INITIAL_ESTIMATE_REQUIRED")
        self.assertEqual(tools.estimate_cost()["estimate"]["estimated_total_units"],"12.00")
        self.assertTrue(tools.select_plan("economy")["ok"])
        self.assertEqual(tools.estimate_cost()["estimate"]["estimated_total_units"],"8.00")

    def test_script_cannot_skip_material_dependency(self):
        self.assertEqual(self.prepared().generate_script()["code"],"DEPENDENCY_MISSING")

    def test_save_cannot_skip_successful_script(self):
        self.assertEqual(self.prepared().save_plan()["code"],"DEPENDENCY_MISSING")

    def test_save_requires_fresh_estimate_after_generation(self):
        tools=self.generated()
        self.assertEqual(tools.save_plan()["code"],"FRESH_ESTIMATE_REQUIRED")
        tools.estimate_cost()
        self.assertEqual(tools.save_plan()["code"],"SAVED")

    def test_initial_estimate_does_not_complete_post_script_review_node(self):
        tools=self.prepared()
        self.assertTrue(tools.state()["estimate_is_current"])
        self.assertEqual(tools.state()["task_plan"][2]["status"],"PLANNED")
        tools.search_materials("primary")
        tools.generate_script()
        self.assertEqual(tools.state()["task_plan"][2]["status"],"STALE")
        tools.estimate_cost()
        self.assertEqual(tools.state()["task_plan"][2]["status"],"SUCCEEDED")

    def test_infeasible_plan_cannot_execute_even_first_cheap_step(self):
        tools=self.prepared(self.request(budget=10))
        self.assertEqual(tools.search_materials("primary")["code"],"PLAN_INFEASIBLE")
        self.assertEqual(tools.state()["spent_units"],"0.00")

    def test_forged_budget_or_price_parameters_rejected(self):
        result=self.prepared().call("generate_script",{"reason":"run","budget":999,"cost":0})
        self.assertEqual(result["code"],"INVALID_ARGUMENTS")

    def test_unknown_publish_tool_rejected(self):
        self.assertEqual(self.tools().call("publish",{"reason":"publish"})["code"],"TOOL_NOT_ALLOWED")

    def test_extra_cross_request_parameter_rejected(self):
        result=self.tools().call("estimate_cost",{"reason":"estimate","request_id":"other"})
        self.assertEqual(result["code"],"INVALID_ARGUMENTS")

    def test_premature_finish_cannot_claim_completion(self):
        result,trace=run_agent(self.request(),self.store,self.data,FixedModel("finish",{"reason":"done"}),max_steps=2)
        self.assertEqual(result["status"],"INCOMPLETE")
        self.assertEqual(result["termination"],"STEP_LIMIT")
        self.assertTrue(all(s["observation"]["code"]=="FINISH_REJECTED" for s in trace["steps"]))

    def test_step_limit_retains_checkpoint(self):
        result,_=self.run_mock(max_steps=3)
        self.assertEqual(result["termination"],"STEP_LIMIT")
        self.assertEqual(result["actual_total_units"],"2.00")
        resumed,_=self.run_mock()
        self.assertEqual(resumed["status"],"COMPLETED")
        self.assertEqual(resumed["actual_total_units"],"12.00")

    def test_permanent_failure_never_saves_failed_script(self):
        result,_=self.run_mock(faults=["generate_always"])
        self.assertEqual(result["status"],"INCOMPLETE")
        self.assertIsNone(result["saved_plan"])
        self.assertTrue(all(s["status"]=="FAILED" for s in result["step_attempts"] if s["step_key"].startswith("script:")))

    def test_per_step_failure_retry_limit(self):
        tools=self.prepared(faults=["generate_always"])
        tools.search_materials("primary")
        tools.generate_script()
        tools.generate_script()
        self.assertEqual(tools.generate_script()["code"],"RETRY_LIMIT")
        self.assertEqual(tools.state()["spent_units"],"4.00")

    def test_save_failure_retains_successful_script(self):
        result,_=self.run_mock(faults=["save_always"])
        self.assertEqual(result["save_status"],"NOT_SAVED")
        self.assertTrue(any(s["step_key"]=="script:premium" and s["status"]=="SUCCEEDED" for s in result["step_attempts"]))

    def test_resume_failed_save_without_regenerating(self):
        first,_=self.run_mock(faults=["save_once"],interrupt_after_failure=True)
        second,_=self.run_mock(faults=["save_once"])
        self.assertEqual(second["status"],"COMPLETED")
        self.assertEqual(second["actual_total_units"],"12.00")
        self.assertEqual(sum(s["step_key"]=="script:premium" for s in second["step_attempts"]),1)
        self.assertEqual(second["step_attempts"][:len(first["step_attempts"])],first["step_attempts"])

    def test_concurrent_search_charged_only_once(self):
        tools=self.prepared()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:tools.search_materials("primary"),range(4)))
        self.assertEqual(sum(not r["cached"] for r in results),1)
        self.assertEqual(tools.state()["spent_units"],"2.00")

    def test_concurrent_script_charged_only_once(self):
        tools=self.prepared()
        tools.search_materials("primary")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:tools.generate_script(),range(4)))
        self.assertEqual(sum(not r["cached"] for r in results),1)
        self.assertEqual(tools.state()["spent_units"],"12.00")

    def test_concurrent_save_has_one_plan_and_one_minute(self):
        tools=self.generated()
        tools.estimate_cost()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:tools.save_plan(),range(4)))
        self.assertEqual(len({r["plan"]["plan_id"] for r in results}),1)
        self.assertEqual(tools.state()["elapsed_minutes"],9)
        self.assertEqual(sum(s["step_key"]=="save" for s in tools.state()["step_attempts"]),1)

    def test_sql_failure_rolls_back_cost_and_result(self):
        tools=self.prepared()
        tools.search_materials("primary")
        original=Store.add_attempt
        def fail(*args):
            original(*args)
            raise sqlite3.OperationalError("synthetic disk error")
        with patch.object(Store,"add_attempt",side_effect=fail):
            result=tools.call("generate_script",{"reason":"run"})
        self.assertEqual(result["code"],"STORAGE_FAILURE")
        self.assertEqual(tools.state()["spent_units"],"2.00")
        self.assertEqual(len(tools.state()["step_attempts"]),1)

    def test_saved_plan_cannot_be_replanned(self):
        self.run_mock()
        self.assertEqual(self.tools().select_plan("economy")["code"],"PLAN_ALREADY_SAVED")

    def test_actual_fees_equal_every_attempt_sum(self):
        result,_=self.run_mock(self.request(budget=12),faults=["generate_once"])
        self.assertEqual(sum(cents(a["charged_units"]) for a in result["step_attempts"]),cents(result["actual_total_units"]))

    def test_trace_records_observation_next_action_and_reason(self):
        _,trace=self.run_mock(self.request(budget=10))
        self.assertTrue(all(e["decision_basis"] and e["next_action"] and e["observation"] for e in trace["steps"]))

    def test_provider_failure_no_mock_fallback(self):
        class Failed(OfflineMock):
            mode="live"
            def choose(self,*args): raise ModelError("provider_http_429:limit_requests")
        result,trace=run_agent(self.request(),self.store,self.data,Failed())
        self.assertEqual(result["mode"],"live")
        self.assertEqual(result["termination"],"MODEL_FAILURE")
        self.assertEqual(trace["steps"],[])

    def test_more_expensive_source_cannot_overrun_actual_time(self):
        tools=self.prepared(self.request(deadline="2026-11-03T09:07:00+08:00"),strategy="economy")
        # Consume the allowed seven minutes only through guarded operations.
        tools.search_materials("backup")
        result=tools.generate_script()
        self.assertEqual(result["code"],"PLAN_INFEASIBLE")
        self.assertLessEqual(tools.state()["elapsed_minutes"],7)


class DomainTest(unittest.TestCase):
    def test_money_uses_exact_decimal(self): self.assertEqual(cents(0.1)+cents(0.2),30)
    def test_units_always_two_decimals(self): self.assertEqual(units(123),"1.23")
    def test_negative_budget_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(budget=-1))
    def test_nan_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(budget="NaN"))
    def test_infinite_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(budget="Infinity"))
    def test_boolean_budget_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(budget=True))
    def test_fraction_beyond_two_places_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(budget="0.001"))
    def test_naive_deadline_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(deadline="2026-11-03T09:30:00"))
    def test_wrong_timezone_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(deadline="2026-11-03T09:30:00+00:00"))
    def test_empty_topic_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(topic=" "))
    def test_unknown_input_field_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(approve_overspend=True))
    def test_invalid_initial_strategy_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(initial_strategy="made-up"))


class ProviderTest(unittest.TestCase):
    def test_quota_stops_other_workers(self):
        class Failed(OfflineMock):
            def choose(self,*args): raise ModelError("provider_http_403:AllocationQuota.FreeTierOnly")
        stop=Event()
        with self.assertRaises(ModelError): QuotaStopGuard(Failed(),stop).choose([],[],None)
        self.assertTrue(stop.is_set())
        with self.assertRaisesRegex(ModelError,"stopped_after_quota_failure"):
            QuotaStopGuard(OfflineMock(),stop).choose([],[],None)

    def test_parameter_payload_and_tool_response(self):
        response={"choices":[{"message":{"tool_calls":[{"id":"id1","function":{"name":"finish","arguments":'{"reason":"done"}'}}]}}],"usage":{"prompt_tokens":10,"completion_tokens":5}}
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self,*args): self.close()
        with patch.dict(os.environ,{"DASHSCOPE_API_KEY":"synthetic-test-key"},clear=True):
            with patch("urllib.request.urlopen",return_value=Response(json.dumps(response).encode())) as mocked:
                model=ChatModel()
                action=model.choose([],[],None)
        payload=json.loads(mocked.call_args.args[0].data)
        self.assertFalse(payload["enable_thinking"])
        self.assertEqual(payload["tool_choice"],"required")
        self.assertEqual(action.name,"finish")

    def test_provider_error_does_not_echo_key(self):
        body=json.dumps({"error":{"code":"AllocationQuota.FreeTierOnly","message":"synthetic-test-key"}}).encode()
        error=urllib.error.HTTPError("url",403,"forbidden",{},io.BytesIO(body))
        with patch.dict(os.environ,{"DASHSCOPE_API_KEY":"synthetic-test-key"},clear=True):
            with patch("urllib.request.urlopen",side_effect=error):
                with self.assertRaises(ModelError) as caught: ChatModel().choose([],[],None)
        self.assertIn("AllocationQuota.FreeTierOnly",str(caught.exception))
        self.assertNotIn("synthetic-test-key",str(caught.exception))


if __name__=="__main__": unittest.main()
