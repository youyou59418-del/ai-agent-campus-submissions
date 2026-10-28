"""Curated synthetic-query retrieval check, not held-out enterprise evaluation."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/"src"))
from enterprise_qa.domain import Request
from enterprise_qa.retrieval import Corpus
from enterprise_qa.cli import write_json

QUERIES=[
    ("报销期限多久？","expense.deadline"),
    ("我的票据最迟啥时候交？","expense.deadline"),
    ("年假申请需要提前几天？","leave.notice"),
    ("休假需提前多久？","leave.notice"),
    ("产品支持离线导出吗？","product.export"),
    ("没网时还能导报告吗？","product.export"),
    ("企业密码怎么重置？","it.reset"),
    ("口令忘了咋处理？","it.reset"),
]


def main():
    corpus=Corpus(ROOT/"data")
    rows=[]
    for i,(question,key) in enumerate(QUERIES):
        request=Request.parse({"request_id":f"retrieval-{i}","question":question})
        relevant={f["doc_id"] for f in corpus.peers(key)}
        row={"question":question,"expected_fact_key":key,"relevant_docs":sorted(relevant)}
        for strategy in ("literal","expanded"):
            hits=corpus.search(question,strategy,request)["hits"]
            got={h["doc_id"] for h in hits}
            row[strategy]={"hit_at_1":bool(hits and hits[0]["doc_id"] in relevant),
                           "recall_at_4":len(got & relevant)/len(relevant),"returned_docs":[h["doc_id"] for h in hits]}
        rows.append(row)
    result={"query_count":len(rows),"queries":rows,
            "summary":{s:{"hit_at_1":sum(r[s]["hit_at_1"] for r in rows)/len(rows),
                          "mean_recall_at_4":sum(r[s]["recall_at_4"] for r in rows)/len(rows)} for s in ("literal","expanded")},
            "boundary":"Curated synthetic queries and manually configured synonyms; not an independent held-out dataset, not answer accuracy."}
    write_json(ROOT/"examples/verification/retrieval-benchmark.json",result)
    print(json.dumps(result["summary"],indent=2))
    return 0


if __name__=="__main__": raise SystemExit(main())
