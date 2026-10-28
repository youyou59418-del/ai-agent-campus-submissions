from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Request:
    request_id: str
    question: str
    history: tuple[dict, ...] = ()

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, dict) or set(raw)-{"request_id", "question", "history"}:
            raise ValueError("invalid request fields")
        rid, question = raw.get("request_id"), raw.get("question")
        if not isinstance(rid, str) or not rid.strip() or len(rid)>120:
            raise ValueError("request_id required")
        if not isinstance(question, str) or not question.strip() or len(question)>1000:
            raise ValueError("question required (1..1000 characters)")
        history=raw.get("history", [])
        if not isinstance(history, list) or len(history)>8:
            raise ValueError("history must contain at most 8 messages")
        for message in history:
            if not isinstance(message, dict) or set(message)!={"role", "content"}:
                raise ValueError("history messages need role/content")
            if message["role"] not in {"user", "assistant"} or not isinstance(message["content"], str) or not message["content"].strip() or len(message["content"])>1000:
                raise ValueError("invalid history message")
        return cls(rid, question, tuple(dict(m) for m in history))

    def as_dict(self):
        value=asdict(self)
        value["history"]=list(self.history)
        return value
