"""Versioned drafts, source-bound facts, duration estimates and brand gates."""
from collections import Counter
from datetime import datetime,timezone
import json

from .content import Content,digest,render,speech_units

FAULTS=("search_once","search_always","estimate_once","estimate_always","rules_once","rules_always")


class BusinessTools:
    def __init__(self,request,data_dir,faults=()):
        self.request=request
        self.content=Content(data_dir)
        self.faults=set(faults)
        if self.faults-set(FAULTS): raise ValueError("unknown fault")
        self.used=set()
        self.searches=[]
        self.retrieved={}
        self.drafts={}
        self.estimates={}
        self.checks={}
        self.failures=[]
        self.final=None
        self.initial_data_hash=self.content.snapshot_hash()
        if request.initial_draft is not None:
            if not valid(request.initial_draft,DRAFT_SCHEMA): raise ValueError("invalid initial draft")
            self._save_draft(request.initial_draft,"D0")

    def _save_draft(self,spec,draft_id):
        spec=json.loads(json.dumps(spec,ensure_ascii=False))
        _,_,expressions=self.content.load()
        expanded=render(spec,expressions)
        value={"draft_id":draft_id,"spec":spec,"rendered":expanded,"draft_hash":digest({"spec":spec,"rendered":expanded})}
        self.drafts[draft_id]=value
        return value

    def _fails(self,tool):
        once=tool+"_once"
        if tool+"_always" in self.faults or once in self.faults and once not in self.used:
            self.used.add(once)
            self.failures.append({"tool":tool,"code":tool.upper()+"_UNAVAILABLE"})
            return True
        return False

    def relevant_retrieved(self):
        keys=self.content.topic_keys(self.request.topic)
        return [f for f in self.retrieved.values() if f["topic_key"] in keys and f["approved"]]

    def search_materials(self,query,strategy):
        if self._fails("search"): return {"ok":False,"code":"SEARCH_UNAVAILABLE"}
        hits=self.content.search(query,strategy)
        for fact in hits: self.retrieved[(fact["source_id"],fact["fact_id"])]=fact
        self.searches.append({"query":query,"strategy":strategy,"hits":hits})
        return {"ok":True,"hits":hits,"hit_count":len(hits),"notice":"Only these complete approved facts may enter factual narration; no performance evidence is implied."}

    def write_draft(self,title_id,scenes):
        if "D0" in self.drafts and ("D0" not in self.estimates or "D0" not in self.checks):
            return {"ok":False,"code":"INITIAL_CHECKS_REQUIRED","message":"estimate and rule-check the supplied initial draft before revising it"}
        data_hash=self.content.snapshot_hash()
        ready=[key for key in self.drafts if self.estimates.get(key,{}).get("passed") and self.checks.get(key,{}).get("passed")
               and self.estimates[key]["data_hash"]==data_hash and self.checks[key]["data_hash"]==data_hash]
        if ready:
            return {"ok":False,"code":"FEASIBLE_DRAFT_AVAILABLE","draft_ids":ready,
                    "message":"a current checked draft already meets the duration ceiling and rules; submit it instead of unrequested extra rewrites"}
        if len(self.drafts)>=4:
            return {"ok":False,"code":"REVISION_LIMIT"}
        draft_id="D"+str(max([int(k[1:]) for k in self.drafts]+[0])+1)
        value=self._save_draft({"title_id":title_id,"scenes":scenes},draft_id)
        return {"ok":True,"draft":value,"notice":"Unverified draft only. It is not a deliverable until both checks pass for this exact revision."}

    def estimate_duration(self,draft_id):
        if draft_id not in self.drafts: return {"ok":False,"code":"DRAFT_NOT_FOUND"}
        if self._fails("estimate"): return {"ok":False,"code":"ESTIMATE_UNAVAILABLE"}
        material_snapshot,rules,expression_snapshot=self.content.load()
        checked_data_hash=digest((material_snapshot,rules,expression_snapshot))
        rows=[]
        for scene in self.drafts[draft_id]["rendered"]["scenes"]:
            units=speech_units(scene["narration"])
            speech_seconds=(units+rules["speech_units_per_second"]-1)//rules["speech_units_per_second"]
            seconds=max(speech_seconds,scene["hold_seconds"],rules["minimum_scene_seconds"])
            rows.append({"scene_id":scene["scene_id"],"speech_units":units,"speech_seconds":speech_seconds,
                         "hold_seconds":scene["hold_seconds"],"estimated_seconds":seconds})
        total=sum(s["estimated_seconds"] for s in rows)
        result={"draft_id":draft_id,"draft_hash":self.drafts[draft_id]["draft_hash"],"data_hash":checked_data_hash,
                "per_scene":rows,"total_seconds":total,"target_seconds":self.request.target_seconds,
                "passed":total<=self.request.target_seconds,"excess_seconds":max(0,total-self.request.target_seconds),
                "method":{"speech_units_per_second":rules["speech_units_per_second"],"minimum_scene_seconds":rules["minimum_scene_seconds"],
                          "formula":"sum(max(ceil(speech_units/rate), hold_seconds, minimum_scene_seconds))", "boundary":"Deterministic estimate, not measured TTS or video duration."}}
        self.estimates[draft_id]=result
        return {"ok":True,"estimate":result}

    def check_rules(self,draft_id):
        if draft_id not in self.drafts: return {"ok":False,"code":"DRAFT_NOT_FOUND"}
        if self._fails("rules"): return {"ok":False,"code":"RULES_UNAVAILABLE"}
        materials,rules,expressions=self.content.load()
        checked_data_hash=digest((materials,rules,expressions))
        draft=self.drafts[draft_id]
        spec=draft["spec"]
        expanded=draft["rendered"]
        risks=[]
        citations=[]
        def risk(code,detail,scene_id=None): risks.append({"code":code,"detail":detail,"scene_id":scene_id})
        if render(spec,expressions)!=expanded: risk("EXPRESSION_LIBRARY_CHANGED","Expression bank changed after this draft was created.")
        title=expressions["titles"].get(spec["title_id"])
        if not title: risk("UNKNOWN_TITLE","Title must use a reviewed expression ID.")
        elif title["style"]!=self.request.style: risk("STYLE_MISMATCH","Title style differs from the user's requested style.")
        scenes=expanded["scenes"]
        if not rules["minimum_scenes"]<=len(scenes)<=rules["maximum_scenes"]: risk("SCENE_COUNT","Scene count violates brand limits.")
        if [s["scene_id"] for s in scenes]!=list(range(1,len(scenes)+1)): risk("SCENE_ORDER","Scene IDs must be sequential and unique.")
        parts={s["part"] for s in scenes}
        for required in rules["required_parts"]:
            if required not in parts: risk("MISSING_PART","Missing required part: "+required)
        relevant_keys=self.content.topic_keys(self.request.topic)
        for scene in scenes:
            sid=scene["scene_id"]
            if scene["visual_id"] not in expressions["visuals"]: risk("UNKNOWN_VISUAL","Unknown visual template.",sid)
            if scene["speech_id"]=="FACT":
                key=(scene["source_id"],scene["fact_id"])
                retrieved=self.retrieved.get(key)
                current=self.content.fact(*key)
                if retrieved is None: risk("SOURCE_NOT_RETRIEVED","Fact must come from an actual material search.",sid)
                elif current is None or current["source_hash"]!=retrieved["source_hash"]: risk("SOURCE_CHANGED","Search the current source again.",sid)
                elif not current["approved"]: risk("SOURCE_NOT_APPROVED","Source fact is not approved.",sid)
                elif scene["source_quote"]!=current["text"]: risk("UNSUPPORTED_FACT","Factual narration must exactly match the complete retrieved fact.",sid)
                elif current["topic_key"] not in relevant_keys or not any(a in current["text"] for a in materials["topics"][current["topic_key"]]["aliases"]):
                    risk("UNRELATED_FACT","Retrieved fact does not support the requested topic.",sid)
                else:
                    citations.append({"scene_id":sid,"source_id":current["source_id"],"fact_id":current["fact_id"],
                                      "version_date":current["version_date"],"quote":current["text"],"source_hash":current["source_hash"]})
            else:
                phrase=expressions["speech"].get(scene["speech_id"])
                if not phrase: risk("UNKNOWN_EXPRESSION","Free-form product assertions are outside the reviewed expression contract.",sid)
                elif phrase["style"]!=self.request.style: risk("STYLE_MISMATCH","Speech style differs from the requested style.",sid)
                if scene["source_id"] or scene["fact_id"] or scene["source_quote"]: risk("MISPLACED_SOURCE_FIELDS","Only a FACT block may carry factual source fields.",sid)
            for forbidden in rules["forbidden_phrases"]:
                if forbidden in scene["narration"] or forbidden in scene["visual"]:
                    risk("BRAND_FORBIDDEN","Forbidden expression: "+forbidden,sid)
        for forbidden in rules["forbidden_phrases"]:
            if forbidden in expanded["title"]: risk("BRAND_FORBIDDEN","Forbidden title expression: "+forbidden)
        if rules["required_fact_citations"] and not citations: risk("NO_SUPPORTED_FACT","At least one supported factual block is required.")
        cited_texts={c["quote"].rstrip("。.!！ ") for c in citations}
        available_texts={f["text"].rstrip("。.!！ ") for f in self.relevant_retrieved()}
        for claim in self.request.required_claims:
            normalized=claim.rstrip("。.!！ ")
            if normalized not in cited_texts:
                risk("REQUIRED_CLAIM_MISSING" if normalized in available_texts else "REQUESTED_FACT_NOT_SUPPORTED",
                     "Required claim cannot be silently invented or omitted: "+claim)
        result={"draft_id":draft_id,"draft_hash":draft["draft_hash"],"data_hash":checked_data_hash,
                "rules_version":rules["version"],"passed":not risks,"risks":risks,"citations":citations}
        self.checks[draft_id]=result
        return {"ok":True,"check":result}

    def submit_script(self,draft_id):
        if draft_id not in self.drafts: return {"ok":False,"code":"DRAFT_NOT_FOUND"}
        estimate=self.estimates.get(draft_id)
        check=self.checks.get(draft_id)
        if not estimate or not check: return {"ok":False,"code":"CHECKS_REQUIRED"}
        current_hash=self.content.snapshot_hash()
        if estimate["data_hash"]!=current_hash or check["data_hash"]!=current_hash:
            return {"ok":False,"code":"STALE_CHECKS","message":"sources/rules changed; rerun checks for the current draft"}
        if estimate["draft_hash"]!=self.drafts[draft_id]["draft_hash"] or check["draft_hash"]!=self.drafts[draft_id]["draft_hash"]:
            return {"ok":False,"code":"STALE_CHECKS"}
        if not estimate["passed"]: return {"ok":False,"code":"DURATION_EXCEEDED","excess_seconds":estimate["excess_seconds"]}
        if not check["passed"]: return {"ok":False,"code":"RULES_NOT_PASSED","risks":check["risks"]}
        script={**self.drafts[draft_id]["rendered"],"verification":"APPROVED","draft_id":draft_id,
                "brief":{k:self.request.as_dict()[k] for k in ("topic","audience","platform","target_seconds","style")},
                "estimated_total_seconds":estimate["total_seconds"],"scene_durations":estimate["per_scene"],
                "citations":check["citations"],"data_hash":current_hash,"draft_hash":self.drafts[draft_id]["draft_hash"]}
        self.final={"run_status":"COMPLETED","task_status":"SCRIPT_READY","script":script,"risks":[],"gaps":[],"follow_up_question":None}
        return {"ok":True,"code":"SCRIPT_ACCEPTED","script":script}

    def minimum_seconds(self):
        _,rules,expressions=self.content.load()
        facts=self.relevant_retrieved()
        if not facts: return None
        def seconds(text): return max(rules["minimum_scene_seconds"],(speech_units(text)+rules["speech_units_per_second"]-1)//rules["speech_units_per_second"])
        values=[]
        for part in ("hook","cta"):
            options=[seconds(p["text"]) for p in expressions["speech"].values() if p["style"]==self.request.style and p["part"]==part]
            if not options: return None
            values.append(min(options))
        values.append(min(seconds(f["text"]) for f in facts))
        values.extend([rules["minimum_scene_seconds"]]*max(0,rules["minimum_scenes"]-3))
        return sum(values)

    def stop(self,reason_code):
        if reason_code=="no_materials":
            if len(self.searches)<2: return {"ok":False,"code":"MORE_SEARCH_REQUIRED"}
            if self.content.search(self.request.topic,"expanded"): return {"ok":False,"code":"MATERIALS_AVAILABLE","message":"try a topic-aligned expanded query"}
            status,gaps,question="NEEDS_MATERIALS",["与主题直接相关的已批准产品事实"],"请提供该主题的产品事实素材或更明确的检索信息。"
        elif reason_code=="tool_failure":
            if not self.failures or max(Counter(f["tool"] for f in self.failures).values())<2:
                return {"ok":False,"code":"RETRY_REQUIRED"}
            status,gaps,question="TOOL_FAILURE",["可用的素材检索、时长估算或规则检查工具"],"请稍后重试，或补充可核对的素材和检查条件。"
        else:
            feasible=[k for k in self.drafts if self.estimates.get(k,{}).get("passed") and self.checks.get(k,{}).get("passed")]
            if feasible: return {"ok":False,"code":"FEASIBLE_DRAFT_AVAILABLE","draft_ids":feasible}
            codes={r["code"] for c in self.checks.values() for r in c["risks"]}
            failed=any(not e["passed"] for e in self.estimates.values()) or any(not c["passed"] for c in self.checks.values())
            lower=self.minimum_seconds()
            if not failed: return {"ok":False,"code":"CHECK_FAILURE_REQUIRED"}
            if len(self.drafts)<2 and not (lower is not None and lower>self.request.target_seconds) and "REQUESTED_FACT_NOT_SUPPORTED" not in codes:
                return {"ok":False,"code":"REVISION_REQUIRED"}
            status,gaps,question="UNFULFILLED",["现有事实、表达库或品牌/时长约束无法同时满足"],"请调整时长或补充已验证事实、品牌允许的表达。"
        self.final={"run_status":"COMPLETED","task_status":status,"script":None,"risks":self.risks(),"gaps":gaps,"follow_up_question":question}
        return {"ok":True,"code":"STOPPED","task_status":status,"gaps":gaps,"minimum_estimated_seconds":self.minimum_safe()}

    def risks(self):
        result=[r for c in self.checks.values() for r in c["risks"]]
        for draft_id,estimate in self.estimates.items():
            if not estimate["passed"]:
                result.append({"code":"DURATION_EXCEEDED","draft_id":draft_id,"detail":f"exceeds target by {estimate['excess_seconds']} seconds"})
        return result

    def call(self,name,args):
        spec=next((t["function"] for t in TOOL_SCHEMAS if t["function"]["name"]==name),None)
        if spec is None: return {"ok":False,"code":"TOOL_NOT_ALLOWED"}
        if not valid(args,spec["parameters"]): return {"ok":False,"code":"INVALID_ARGUMENTS"}
        try:
            return getattr(self,name)(**{k:v for k,v in args.items() if k!="reason"})
        except (OSError,ValueError,KeyError,TypeError):
            self.failures.append({"tool":name,"code":"DATA_UNAVAILABLE"})
            return {"ok":False,"code":"DATA_UNAVAILABLE"}

    def report(self,termination):
        result=dict(self.final) if self.final else {"run_status":"INCOMPLETE","task_status":"INCOMPLETE","script":None,"risks":self.risks(),"gaps":[termination],"follow_up_question":None}
        if result["script"]:
            try:
                fresh=result["script"]["data_hash"]==self.content.snapshot_hash()
            except (OSError,ValueError,KeyError): fresh=False
            if not fresh:
                result={"run_status":"INCOMPLETE","task_status":"INCOMPLETE","script":None,"risks":[{"code":"SOURCE_CHANGED","detail":"Data changed before delivery."}],"gaps":["重新检索并检查当前素材"],"follow_up_question":None}
        result.update({"request_id":self.request.request_id,"drafts":list(self.drafts.values()),
                       "duration_checks":list(self.estimates.values()),"rule_checks":list(self.checks.values()),
                       "minimum_estimated_seconds":self.minimum_safe(),"published":False})
        return result

    def minimum_safe(self):
        try: return self.minimum_seconds()
        except (OSError,ValueError,KeyError,TypeError): return None


def valid(value,spec):
    kind=spec["type"]
    if kind=="string": return isinstance(value,str) and spec.get("minLength",0)<=len(value)<=1000 and ("enum" not in spec or value in spec["enum"])
    if kind=="integer": return type(value) is int and spec.get("minimum",0)<=value<=spec.get("maximum",10000)
    if kind=="array": return isinstance(value,list) and spec.get("minItems",0)<=len(value)<=spec.get("maxItems",8) and all(valid(v,spec["items"]) for v in value)
    if kind=="object": return isinstance(value,dict) and set(value)==set(spec["required"]) and all(valid(value[k],p) for k,p in spec["properties"].items())
    return False


TEXT={"type":"string"}
ID={"type":"string","minLength":1}
SCENE_PROPERTIES={"scene_id":{"type":"integer","minimum":1,"maximum":6},"visual_id":ID,"speech_id":ID,
                  "source_id":TEXT,"fact_id":TEXT,"source_quote":TEXT,"hold_seconds":{"type":"integer","minimum":0,"maximum":60}}
SCENE_SCHEMA={"type":"object","properties":SCENE_PROPERTIES,"required":list(SCENE_PROPERTIES),"additionalProperties":False}
DRAFT_PROPERTIES={"title_id":ID,"scenes":{"type":"array","items":SCENE_SCHEMA,"minItems":1,"maxItems":6}}
DRAFT_SCHEMA={"type":"object","properties":DRAFT_PROPERTIES,"required":list(DRAFT_PROPERTIES),"additionalProperties":False}


def schema(name,description,props):
    props={**props,"reason":{"type":"string","minLength":1,"description":"One short action rationale; no private chain of thought."}}
    return {"type":"function","function":{"name":name,"description":description,"strict":True,
            "parameters":{"type":"object","properties":props,"required":list(props),"additionalProperties":False}}}


TOOL_SCHEMAS=[
    schema("search_materials","Retrieve complete approved source facts. Empty results require query rewriting/expanded search or asking for material.",{"query":ID,"strategy":{"type":"string","enum":["literal","expanded"]}}),
    schema("write_draft","Model selects reviewed expression IDs, factual quotes, visual sequence and holds. Produces UNVERIFIED_DRAFT; never skips checks.",DRAFT_PROPERTIES),
    schema("estimate_duration","Estimate each scene and sum actual derived speech/hold time for this draft revision.",{"draft_id":ID}),
    schema("check_rules","Check source facts, exact quotes, relevance, requested style, required claims and synthetic brand rules for this revision.",{"draft_id":ID}),
    schema("submit_script","Accept only this exact revision with current passing duration and brand/fact checks.",{"draft_id":ID}),
    schema("stop","Ask for material, report repeated tool failure, or explain unmet constraints. Cannot replace a feasible checked draft with failure.",{"reason_code":{"type":"string","enum":["no_materials","tool_failure","constraints"]}}),
]
