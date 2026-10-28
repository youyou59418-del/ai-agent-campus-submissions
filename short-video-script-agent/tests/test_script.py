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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
sys.path.insert(0,str(ROOT))
from video_script.agent import run_agent
from video_script.cli import export_result
from video_script.content import Content,speech_units
from video_script.domain import Request
from video_script.models import Action,ChatModel,LazyChatModel,ModelError,OfflineMock,QuotaStopGuard,basic_spec
from video_script.tools import BusinessTools
from validate_script import validate_export


def raw(**extra):
    return {"request_id":"test","topic":"字幕功能介绍","audience":"新手创作者","platform":"抖音","target_seconds":30,"style":"自然",**extra}


class ScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.data=self.root/"data"
        shutil.copytree(ROOT/"data",self.data)

    def req(self,**kwargs): return Request.parse(raw(**kwargs))
    def tools(self,request=None,faults=()): return BusinessTools(request or self.req(),self.data,faults)
    def run_mock(self,request=None,**kwargs): return run_agent(request or self.req(),self.data,OfflineMock(),**kwargs)

    def edit(self,name,fn):
        path=self.data/name
        value=json.loads(path.read_text(encoding="utf-8"))
        fn(value)
        path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding="utf-8")

    def prepared(self,request=None,faults=(),holds=None):
        tools=self.tools(request,faults)
        fact=tools.search_materials("字幕","expanded")["hits"][0]
        draft=tools.write_draft(**basic_spec(tools.request,fact,holds))["draft"]
        return tools,draft["draft_id"]

    def checked(self,request=None,holds=None):
        tools,key=self.prepared(request,holds=holds)
        tools.estimate_duration(key)
        tools.check_rules(key)
        return tools,key

    def test_normal_script_has_fact_and_checked_scene_sum(self):
        result,_=self.run_mock()
        self.assertEqual(result["task_status"],"SCRIPT_READY")
        script=result["script"]
        self.assertEqual(script["estimated_total_seconds"],sum(s["estimated_seconds"] for s in script["scene_durations"]))
        self.assertEqual(script["citations"][0]["quote"],"支持字幕。")
        self.assertFalse(result["published"])

    def test_missing_audience_zero_model_calls(self):
        with patch.dict(os.environ,{},clear=True):
            result,trace=run_agent(self.req(audience=None),self.data,LazyChatModel())
        self.assertEqual(result["task_status"],"NEEDS_INPUT")
        self.assertEqual(trace["usage"]["api_calls"],0)
        self.assertEqual(result["gaps"],["audience"])

    def test_missing_duration_and_style(self):
        result,_=self.run_mock(self.req(target_seconds=None,style=None))
        self.assertEqual(result["task_status"],"NEEDS_INPUT")
        self.assertEqual(result["gaps"],["target_seconds","style"])

    def test_no_source_needs_supplement_and_no_script(self):
        self.edit("materials.json",lambda d:d.update(materials=[m for m in d["materials"] if m["source_id"]!="P01"]))
        result,trace=self.run_mock()
        self.assertEqual(result["task_status"],"NEEDS_MATERIALS")
        self.assertIsNone(result["script"])
        self.assertGreaterEqual(sum(e["tool"]=="search_materials" for e in trace["steps"]),2)

    def test_query_rewrite_finds_source_after_literal_miss(self):
        result,trace=self.run_mock(self.req(topic="给画面加字"))
        self.assertEqual(result["task_status"],"SCRIPT_READY")
        self.assertEqual(trace["steps"][0]["observation"]["hit_count"],0)
        self.assertEqual(trace["steps"][1]["arguments"]["strategy"],"expanded")

    def test_initial_38_is_measured_then_revised_to_28(self):
        seed=basic_spec(self.req(),{"source_id":"P01","fact_id":"F1","text":"支持字幕。"},[14,12,12])
        result,trace=self.run_mock(self.req(initial_draft=seed))
        self.assertEqual(result["duration_checks"][0]["total_seconds"],38)
        self.assertFalse(result["duration_checks"][0]["passed"])
        self.assertEqual(result["script"]["estimated_total_seconds"],28)
        self.assertNotEqual(result["script"]["draft_id"],"D0")

    def test_initial_assessment_cannot_be_skipped(self):
        seed=basic_spec(self.req(),{"source_id":"P01","fact_id":"F1","text":"支持字幕。"},[14,12,12])
        tools=self.tools(self.req(initial_draft=seed))
        self.assertEqual(tools.write_draft(**seed)["code"],"INITIAL_CHECKS_REQUIRED")

    def test_impossible_duration_reports_unfulfilled(self):
        result,_=self.run_mock(self.req(target_seconds=5))
        self.assertEqual(result["task_status"],"UNFULFILLED")
        self.assertGreater(result["minimum_estimated_seconds"],5)
        self.assertIsNone(result["script"])

    def test_shortening_hold_cannot_hide_spoken_duration(self):
        tools,key=self.prepared(holds=[0,0,0])
        estimate=tools.estimate_duration(key)["estimate"]
        self.assertEqual(estimate["total_seconds"],12)
        self.assertGreater(estimate["per_scene"][0]["speech_seconds"],0)

    def test_target_change_changes_compaction(self):
        a,_=self.run_mock()
        b,_=self.run_mock(self.req(target_seconds=20))
        self.assertEqual(a["script"]["estimated_total_seconds"],28)
        self.assertEqual(b["script"]["estimated_total_seconds"],18)

    def test_unknown_automatic_subtitle_claim_rejected(self):
        tools,key=self.prepared()
        spec=basic_spec(tools.request,next(iter(tools.retrieved.values())))
        spec["scenes"][1]["source_quote"]="支持自动生成字幕。"
        key=tools.write_draft(**spec)["draft"]["draft_id"]
        check=tools.check_rules(key)["check"]
        self.assertFalse(check["passed"])
        self.assertIn("UNSUPPORTED_FACT",{r["code"] for r in check["risks"]})

    def test_fabricated_80_percent_cannot_be_approved(self):
        tools,key=self.prepared()
        spec=basic_spec(tools.request,next(iter(tools.retrieved.values())))
        spec["scenes"][1]["source_quote"]="支持字幕，效率提升80%。"
        key=tools.write_draft(**spec)["draft"]["draft_id"]
        tools.estimate_duration(key)
        check=tools.check_rules(key)["check"]
        self.assertIn("UNSUPPORTED_FACT",{r["code"] for r in check["risks"]})
        self.assertIn("BRAND_FORBIDDEN",{r["code"] for r in check["risks"]})
        self.assertEqual(tools.submit_script(key)["code"],"RULES_NOT_PASSED")

    def test_user_required_unsupported_claim_is_not_silently_dropped(self):
        result,_=self.run_mock(self.req(required_claims=["效率提升80%"] ))
        self.assertEqual(result["task_status"],"UNFULFILLED")
        self.assertTrue(any(r["code"]=="REQUESTED_FACT_NOT_SUPPORTED" for r in result["risks"]))

    def test_brand_style_failure_is_corrected_and_rechecked(self):
        seed=basic_spec(self.req(),{"source_id":"P01","fact_id":"F1","text":"支持字幕。"},[6,4,6],style="专业")
        result,_=self.run_mock(self.req(initial_draft=seed))
        self.assertFalse(result["rule_checks"][0]["passed"])
        self.assertTrue(result["rule_checks"][-1]["passed"])
        self.assertEqual(result["script"]["title_id"],"T_NAT")

    def test_fact_must_have_been_retrieved(self):
        tools=self.tools()
        spec=basic_spec(tools.request,{"source_id":"P01","fact_id":"F1","text":"支持字幕。"})
        key=tools.write_draft(**spec)["draft"]["draft_id"]
        self.assertIn("SOURCE_NOT_RETRIEVED",{r["code"] for r in tools.check_rules(key)["check"]["risks"]})

    def test_cover_fact_cannot_substitute_for_subtitle(self):
        tools=self.tools()
        fact=tools.search_materials("封面","expanded")["hits"][0]
        key=tools.write_draft(**basic_spec(tools.request,fact))["draft"]["draft_id"]
        self.assertIn("UNRELATED_FACT",{r["code"] for r in tools.check_rules(key)["check"]["risks"]})

    def test_source_change_invalidates_retrieval(self):
        tools,key=self.prepared()
        self.edit("materials.json",lambda d:d["materials"][0]["facts"][0].update(text="支持字幕及文本样式。"))
        self.assertIn("SOURCE_CHANGED",{r["code"] for r in tools.check_rules(key)["check"]["risks"]})

    def test_old_duration_and_rule_checks_cannot_approve_new_revision(self):
        tools,key=self.checked(holds=[14,12,12])
        spec=basic_spec(tools.request,next(iter(tools.retrieved.values())))
        new=tools.write_draft(**spec)["draft"]["draft_id"]
        self.assertEqual(tools.submit_script(new)["code"],"CHECKS_REQUIRED")

    def test_feasible_checked_revision_blocks_unrequested_regeneration(self):
        tools,key=self.checked()
        before=len(tools.drafts)
        result=tools.write_draft(**tools.drafts[key]["spec"])
        self.assertEqual(result["code"],"FEASIBLE_DRAFT_AVAILABLE")
        self.assertEqual(len(tools.drafts),before)
        self.assertTrue(tools.submit_script(key)["ok"])

    def test_brand_rule_change_invalidates_pass_records(self):
        tools,key=self.checked()
        self.edit("brand_rules.json",lambda d:d["forbidden_phrases"].append("字幕"))
        self.assertEqual(tools.submit_script(key)["code"],"STALE_CHECKS")

    def test_rule_change_during_check_cannot_bind_old_result_to_new_snapshot(self):
        tools,key=self.prepared()
        original=tools.content.fact
        def change_during_read(source_id,fact_id):
            fact=original(source_id,fact_id)
            self.edit("brand_rules.json",lambda d:d["forbidden_phrases"].append("字幕"))
            return fact
        with patch.object(tools.content,"fact",side_effect=change_during_read):
            check=tools.check_rules(key)["check"]
        self.assertTrue(check["passed"])
        self.assertNotEqual(check["data_hash"],tools.content.snapshot_hash())
        tools.estimate_duration(key)
        self.assertEqual(tools.submit_script(key)["code"],"STALE_CHECKS")

    def test_source_change_after_approval_blocks_handoff(self):
        tools,key=self.checked()
        self.assertTrue(tools.submit_script(key)["ok"])
        self.edit("materials.json",lambda d:d["materials"][0]["facts"][0].update(text="支持字幕及文本样式。"))
        result=tools.report("SCRIPT_ACCEPTED")
        self.assertEqual(result["task_status"],"INCOMPLETE")
        self.assertIsNone(result["script"])

    def test_same_source_date_content_edit_is_detected(self):
        tools,key=self.checked()
        date=tools.checks[key]["citations"][0]["version_date"]
        self.edit("materials.json",lambda d:d["materials"][0]["facts"][0].update(text="支持字幕和字幕预览。"))
        self.assertEqual(tools.content.fact("P01","F1")["version_date"],date)
        self.assertEqual(tools.submit_script(key)["code"],"STALE_CHECKS")

    def test_expression_bank_change_requires_new_draft(self):
        tools,key=self.prepared()
        self.edit("expressions.json",lambda d:d["speech"]["H_NAT"].update(text="新的已审核开场表达。"))
        self.assertIn("EXPRESSION_LIBRARY_CHANGED",{r["code"] for r in tools.check_rules(key)["check"]["risks"]})

    def test_unknown_creative_text_cannot_smuggle_product_facts(self):
        tools,key=self.prepared()
        spec=tools.drafts[key]["spec"]
        spec["scenes"][0]["speech_id"]="自动生成字幕，效率提升80%"
        new=tools.write_draft(**spec)["draft"]["draft_id"]
        self.assertIn("UNKNOWN_EXPRESSION",{r["code"] for r in tools.check_rules(new)["check"]["risks"]})

    def test_unknown_visual_cannot_invent_product_ui(self):
        tools,key=self.prepared()
        spec=tools.drafts[key]["spec"]
        spec["scenes"][1]["visual_id"]="点击自动字幕按钮"
        new=tools.write_draft(**spec)["draft"]["draft_id"]
        self.assertIn("UNKNOWN_VISUAL",{r["code"] for r in tools.check_rules(new)["check"]["risks"]})

    def test_model_cannot_supply_arbitrary_narration_field(self):
        tools=self.tools()
        spec=basic_spec(tools.request,{"source_id":"P01","fact_id":"F1","text":"支持字幕。"})
        spec["scenes"][0]["narration"]="效率提升80%"
        self.assertEqual(tools.call("write_draft",{**spec,"reason":"write"})["code"],"INVALID_ARGUMENTS")

    def test_model_cannot_override_target_in_estimator(self):
        tools,key=self.prepared()
        self.assertEqual(tools.call("estimate_duration",{"draft_id":key,"target_seconds":999,"reason":"estimate"})["code"],"INVALID_ARGUMENTS")

    def test_negative_holds_rejected_by_tool_schema(self):
        tools=self.tools()
        spec=basic_spec(tools.request,{"source_id":"P01","fact_id":"F1","text":"支持字幕。"})
        spec["scenes"][0]["hold_seconds"]=-1
        self.assertEqual(tools.call("write_draft",{**spec,"reason":"write"})["code"],"INVALID_ARGUMENTS")

    def test_duplicate_scene_id_fails_rules(self):
        tools,key=self.prepared()
        spec=tools.drafts[key]["spec"]
        spec["scenes"][1]["scene_id"]=1
        key=tools.write_draft(**spec)["draft"]["draft_id"]
        self.assertIn("SCENE_ORDER",{r["code"] for r in tools.check_rules(key)["check"]["risks"]})

    def test_dropping_fact_to_shorten_script_fails(self):
        tools,key=self.prepared()
        spec=tools.drafts[key]["spec"]
        spec["scenes"][1].update(speech_id="B_NAT",source_id="",fact_id="",source_quote="")
        key=tools.write_draft(**spec)["draft"]["draft_id"]
        codes={r["code"] for r in tools.check_rules(key)["check"]["risks"]}
        self.assertIn("NO_SUPPORTED_FACT",codes)
        self.assertIn("MISSING_PART",codes)

    def test_two_scenes_cannot_cheat_brand_minimum(self):
        tools,key=self.prepared()
        spec=tools.drafts[key]["spec"]
        spec["scenes"]=spec["scenes"][:2]
        key=tools.write_draft(**spec)["draft"]["draft_id"]
        self.assertIn("SCENE_COUNT",{r["code"] for r in tools.check_rules(key)["check"]["risks"]})

    def test_cannot_smuggle_source_fields_into_creative_block(self):
        tools,key=self.prepared()
        spec=tools.drafts[key]["spec"]
        spec["scenes"][0]["source_quote"]="效率提升80%"
        key=tools.write_draft(**spec)["draft"]["draft_id"]
        self.assertIn("MISPLACED_SOURCE_FIELDS",{r["code"] for r in tools.check_rules(key)["check"]["risks"]})

    def test_unchecked_draft_cannot_be_submitted(self):
        tools,key=self.prepared()
        self.assertEqual(tools.submit_script(key)["code"],"CHECKS_REQUIRED")

    def test_overlong_checked_draft_cannot_be_submitted(self):
        tools,key=self.checked(holds=[14,12,12])
        self.assertEqual(tools.submit_script(key)["code"],"DURATION_EXCEEDED")

    def test_cannot_report_impossible_without_trying_correction(self):
        tools,key=self.checked(holds=[14,12,12])
        self.assertEqual(tools.stop("constraints")["code"],"REVISION_REQUIRED")

    def test_cannot_refuse_a_feasible_checked_draft(self):
        tools,key=self.checked()
        self.assertEqual(tools.stop("constraints")["code"],"FEASIBLE_DRAFT_AVAILABLE")

    def test_cannot_claim_materials_missing_after_one_query(self):
        tools=self.tools()
        tools.search_materials("nothing","literal")
        self.assertEqual(tools.stop("no_materials")["code"],"MORE_SEARCH_REQUIRED")

    def test_cannot_claim_missing_when_topic_source_exists(self):
        tools=self.tools()
        tools.search_materials("nothing","literal")
        tools.search_materials("missing","expanded")
        self.assertEqual(tools.stop("no_materials")["code"],"MATERIALS_AVAILABLE")

    def test_search_failure_once_recovers(self):
        result,trace=self.run_mock(faults=["search_once"])
        self.assertEqual(result["task_status"],"SCRIPT_READY")
        self.assertEqual(trace["steps"][0]["observation"]["code"],"SEARCH_UNAVAILABLE")

    def test_estimation_failure_once_recovers(self):
        result,trace=self.run_mock(faults=["estimate_once"])
        self.assertEqual(result["task_status"],"SCRIPT_READY")
        self.assertTrue(any(e["observation"].get("code")=="ESTIMATE_UNAVAILABLE" for e in trace["steps"]))

    def test_permanent_rules_failure_never_hands_off_script(self):
        result,_=self.run_mock(faults=["rules_always"])
        self.assertEqual(result["task_status"],"TOOL_FAILURE")
        self.assertIsNone(result["script"])

    def test_step_limit_keeps_unapproved_draft_separate(self):
        result,_=self.run_mock(max_steps=2)
        self.assertEqual(result["task_status"],"INCOMPLETE")
        self.assertIsNone(result["script"])
        self.assertTrue(result["drafts"])

    def test_unknown_publish_tool_is_rejected(self):
        self.assertEqual(self.tools().call("publish",{"reason":"publish"})["code"],"TOOL_NOT_ALLOWED")

    def test_revision_count_is_bounded(self):
        tools,key=self.prepared()
        spec=tools.drafts[key]["spec"]
        for _ in range(3): tools.write_draft(**spec)
        self.assertEqual(tools.write_draft(**spec)["code"],"REVISION_LIMIT")

    def test_draft_is_detached_from_callers_mutable_input(self):
        tools=self.tools()
        spec=basic_spec(tools.request,{"source_id":"P01","fact_id":"F1","text":"支持字幕。"})
        key=tools.write_draft(**spec)["draft"]["draft_id"]
        spec["scenes"][1]["source_quote"]="假的功能"
        self.assertEqual(tools.drafts[key]["spec"]["scenes"][1]["source_quote"],"支持字幕。")

    def test_valid_export_can_be_rechecked_without_a_model(self):
        result,_=self.run_mock()
        self.assertTrue(validate_export(self.req(),result["script"],self.data)["valid"])

    def test_manual_narration_edit_requires_review(self):
        result,_=self.run_mock()
        result["script"]["scenes"][1]["narration"]="效率提升80%"
        report=validate_export(self.req(),result["script"],self.data)
        self.assertFalse(report["valid"])
        self.assertIn("EDIT_OUTSIDE_VERIFIED_EXPRESSIONS_OR_SOURCE",report["risks"])

    def test_forged_export_duration_rejected(self):
        result,_=self.run_mock()
        result["script"]["estimated_total_seconds"]=1
        self.assertIn("DURATION_METADATA_MISMATCH",validate_export(self.req(),result["script"],self.data)["risks"])

    def test_failed_run_overwrites_stale_public_script_export(self):
        output=self.root/"output.json"
        result,_=self.run_mock()
        export_result(output,result)
        failure,_=self.run_mock(self.req(audience=None))
        export_result(output,failure)
        saved=json.loads((self.root/"output.script.json").read_text(encoding="utf-8"))
        self.assertIsNone(saved["script"])

    def test_api_failure_does_not_turn_into_mock_success(self):
        class Failed(OfflineMock):
            mode="live"
            def choose(self,*args): raise ModelError("provider_http_429:limit_requests")
        result,trace=run_agent(self.req(),self.data,Failed())
        self.assertEqual(result["termination"],"MODEL_FAILURE")
        self.assertEqual(trace["steps"],[])
        self.assertIsNone(result["script"])


class DomainTest(unittest.TestCase):
    def test_bool_duration_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(target_seconds=True))
    def test_negative_duration_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(target_seconds=-1))
    def test_unknown_platform_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(platform="unknown"))
    def test_unknown_style_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(style="unknown"))
    def test_unknown_input_field_rejected(self):
        with self.assertRaises(ValueError): Request.parse(raw(force_approve=True))
    def test_empty_text_is_missing_not_invented(self): self.assertIn("topic",Request.parse(raw(topic=" ")).missing)
    def test_speech_estimator_counts_words_and_chinese(self): self.assertEqual(speech_units("字幕 hello 123"),6)


class AdapterTest(unittest.TestCase):
    def test_flash_model_supports_structured_draft_response_budget(self):
        response={"choices":[{"message":{"tool_calls":[{"id":"id","function":{"name":"stop","arguments":'{"reason_code":"tool_failure","reason":"failure"}'}}]}}],"usage":{"prompt_tokens":10,"completion_tokens":5}}
        class Response(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self,*args): self.close()
        with patch.dict(os.environ,{"DASHSCOPE_API_KEY":"synthetic-test-key"},clear=True):
            with patch("urllib.request.urlopen",return_value=Response(json.dumps(response).encode())) as mocked:
                model=ChatModel()
                model.choose([],[],None)
        payload=json.loads(mocked.call_args.args[0].data)
        self.assertEqual(payload["model"],"qwen3.8-flash")
        self.assertEqual(payload["max_tokens"],1400)
        self.assertFalse(payload["enable_thinking"])

    def test_provider_error_message_does_not_echo_key(self):
        body=json.dumps({"error":{"code":"AllocationQuota.FreeTierOnly","message":"synthetic-test-key"}}).encode()
        error=urllib.error.HTTPError("url",403,"forbidden",{},io.BytesIO(body))
        with patch.dict(os.environ,{"DASHSCOPE_API_KEY":"synthetic-test-key"},clear=True):
            with patch("urllib.request.urlopen",side_effect=error):
                with self.assertRaises(ModelError) as caught: ChatModel().choose([],[],None)
        self.assertNotIn("synthetic-test-key",str(caught.exception))

    def test_quota_error_stops_other_workers(self):
        class Failed(OfflineMock):
            def choose(self,*args): raise ModelError("provider_http_403:AllocationQuota.FreeTierOnly")
        event=Event()
        with self.assertRaises(ModelError): QuotaStopGuard(Failed(),event).choose([],[],None)
        self.assertTrue(event.is_set())


if __name__=="__main__": unittest.main()
