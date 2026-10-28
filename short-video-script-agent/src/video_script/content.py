from datetime import date
import hashlib
import json
from pathlib import Path
import re


def digest(value): return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def speech_units(text):
    return sum(1 if re.fullmatch(r"[\u4e00-\u9fff]",t) else 2 for t in re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9]+",text))


class Content:
    def __init__(self,data_dir): self.path=Path(data_dir)

    def load(self):
        materials=json.loads((self.path/"materials.json").read_text(encoding="utf-8"))
        rules=json.loads((self.path/"brand_rules.json").read_text(encoding="utf-8"))
        expressions=json.loads((self.path/"expressions.json").read_text(encoding="utf-8"))
        seen=set()
        for source in materials["materials"]:
            date.fromisoformat(source["version_date"])
            for fact in source["facts"]:
                key=(source["source_id"],fact["fact_id"])
                if key in seen or not isinstance(fact["text"],str) or not fact["text"].strip() or type(fact["approved"]) is not bool:
                    raise ValueError("invalid or duplicate source fact")
                seen.add(key)
        for key in ("speech_units_per_second","minimum_scene_seconds","minimum_scenes","maximum_scenes"):
            if type(rules[key]) is not int or rules[key]<1: raise ValueError("invalid timing/brand rule")
        return materials,rules,expressions

    def snapshot_hash(self): return digest(self.load())

    def topic_keys(self,query):
        materials,_,_=self.load()
        return [k for k,t in materials["topics"].items() if any(a in query for a in t["aliases"])]

    def search(self,query,strategy):
        materials,_,_=self.load()
        keys=self.topic_keys(query) if strategy=="expanded" else []
        hits=[]
        for source in materials["materials"]:
            canonical=materials["topics"][source["topic_key"]]["canonical"]
            literal=canonical in query or query in source["title"]
            if not literal and source["topic_key"] not in keys: continue
            for fact in source["facts"]:
                if not fact["approved"]: continue
                value={k:source[k] for k in ("source_id","version_date","topic_key","title")}|dict(fact)
                value["source_hash"]=digest(value)
                hits.append(value)
        return hits

    def fact(self,source_id,fact_id):
        materials,_,_=self.load()
        for source in materials["materials"]:
            for fact in source["facts"]:
                if (source["source_id"],fact["fact_id"])==(source_id,fact_id):
                    value={k:source[k] for k in ("source_id","version_date","topic_key","title")}|dict(fact)
                    value["source_hash"]=digest(value)
                    return value
        return None


def render(spec,expressions):
    title=expressions["titles"].get(spec["title_id"])
    scenes=[]
    for raw in spec["scenes"]:
        phrase=expressions["speech"].get(raw["speech_id"])
        text=raw["source_quote"] if raw["speech_id"]=="FACT" else phrase["text"] if phrase else "[未知表达模板]"
        scenes.append({**raw,"visual":expressions["visuals"].get(raw["visual_id"],"[未知视觉模板]"),
                       "narration":text,"subtitle":text,"part":"fact" if raw["speech_id"]=="FACT" else phrase["part"] if phrase else "unknown"})
    return {"title":title["text"] if title else "[未知标题模板]","title_id":spec["title_id"],"scenes":scenes,
            "verification":"UNVERIFIED_DRAFT"}
