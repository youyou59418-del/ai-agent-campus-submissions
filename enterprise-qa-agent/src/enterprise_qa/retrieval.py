"""Small-corpus BM25 over Chinese bigrams, with explicit synonym expansion."""
from collections import Counter
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re


def tokens(text):
    text=text.lower()
    result=re.findall(r"[a-z0-9]+", text)
    for word in re.findall(r"[\u4e00-\u9fff]+", text):
        result.extend(word[i:i+2] for i in range(len(word)-1))
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


class Corpus:
    def __init__(self, data_dir):
        self.data_dir=Path(data_dir)
        self.topics=json.loads((self.data_dir/"topics.json").read_text(encoding="utf-8"))["topics"]

    def load(self):
        raw=json.loads((self.data_dir/"documents.json").read_text(encoding="utf-8"))
        fragments=[]
        seen=set()
        for doc in raw["documents"]:
            if not isinstance(doc["doc_id"],str) or not doc["doc_id"].strip():
                raise ValueError("invalid document ID")
            date.fromisoformat(doc["version_date"])
            for fragment in doc["fragments"]:
                key=(doc["doc_id"],doc["version_date"],fragment["fragment_id"])
                if key in seen or not isinstance(fragment["text"],str) or not fragment["text"].strip():
                    raise ValueError("duplicate or empty fragment")
                if fragment["fact_key"] not in self.topics:
                    raise ValueError("unknown controlled fact key")
                seen.add(key)
                value={**{k:v for k,v in doc.items() if k!="fragments"},**fragment}
                value["source_hash"]=fingerprint(value)
                fragments.append(value)
        return fragments, fingerprint(raw)

    def intent(self, request):
        question=request.question
        direct=[]
        for key, topic in self.topics.items():
            if any(a in question for a in topic["anchors"]) and any(r in question for r in topic["relations"]):
                direct.append(key)
        if direct:
            return direct
        # History supplies an antecedent, never source evidence. Ignore assistant claims.
        relation_keys=[k for k,t in self.topics.items() if any(r in question for r in t["relations"])]
        for message in reversed(request.history):
            if message["role"]!="user":
                continue
            candidates=[k for k in relation_keys if any(a in message["content"] for a in self.topics[k]["anchors"])]
            if candidates:
                return candidates
        return []

    def needs_topic_clarification(self, request):
        return not self.intent(request) and (request.question.strip("？?。 ") in {"多久", "几天", "需要多久", "需要几天"} or any(w in request.question for w in ("那", "这个", "它", "上述")))

    def query_topics(self, query):
        return [key for key,topic in self.topics.items()
                if any(a in query for a in topic["anchors"]) and any(r in query for r in topic["relations"])]

    def search(self, query, strategy, request, limit=4):
        fragments, corpus_hash=self.load()
        intent=self.query_topics(query)
        if strategy=="expanded" and not intent:
            intent=self.intent(request)
        expanded=query
        if strategy=="expanded":
            expanded+=" "+" ".join(self.topics[k]["canonical_query"] for k in intent)
        query_tokens=Counter(tokens(expanded))
        texts=[tokens(f["title"]+" "+f["text"]) for f in fragments]
        total=len(texts)
        average=sum(map(len,texts))/max(1,total)
        ranked=[]
        for fragment,words in zip(fragments,texts):
            freq=Counter(words)
            score=0.0
            for word,count in query_tokens.items():
                df=sum(word in t for t in texts)
                if not df or not freq[word]:
                    continue
                idf=math.log(1+(total-df+0.5)/(df+0.5))
                score+=idf*freq[word]*2.2/(freq[word]+1.2*(0.25+0.75*len(words)/max(1,average)))
            if strategy=="expanded" and fragment["fact_key"] in intent:
                score+=10.0
            if score>0:
                ranked.append((score,fragment))
        ranked.sort(key=lambda pair:(-pair[0],pair[1]["doc_id"],pair[1]["version_date"],pair[1]["fragment_id"]))
        hits=[]
        for score,f in ranked[:limit]:
            hits.append({k:f[k] for k in ("doc_id","version_date","fragment_id","title","fact_key","scope","priority","kind")})
            hits[-1].update({"score":round(score,5),"candidate_only":True})
        return {"hits":hits,"total_matches":len(ranked),"more_available":len(ranked)>limit,
                "strategy":strategy,"query":query,"expanded_query":expanded,
                "corpus_hash":corpus_hash,"notice":"Search hits are locators, not read evidence. Dates do not imply policy precedence."}

    def read(self, doc_id, version_date, fragment_id):
        fragments,_=self.load()
        return next((f for f in fragments if (f["doc_id"],f["version_date"],f["fragment_id"])==(doc_id,version_date,fragment_id)),None)

    def peers(self, fact_key):
        fragments,_=self.load()
        return [f for f in fragments if f["fact_key"]==fact_key and f["kind"] in {"policy","faq"}]

    def claim(self, fragment):
        if self.topics[fragment["fact_key"]]["claim_type"]=="quantity":
            quantities=re.findall(r"(\d+)\s*(?:个)?\s*(工作日|天|小时)",fragment["text"])
            if len(quantities)==1:
                return tuple(quantities[0])
        # Conservative: text differences may require clarification. No semantic proof claimed.
        return re.sub(r"\s+","",fragment["text"])
