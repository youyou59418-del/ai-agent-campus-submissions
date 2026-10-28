from dataclasses import asdict,dataclass

PLATFORMS=("抖音","小红书","B站")
STYLES=("自然","轻松","专业")


@dataclass(frozen=True)
class Request:
    request_id:str
    topic:str|None
    audience:str|None
    platform:str|None
    target_seconds:int|None
    style:str|None
    required_claims:tuple[str,...]=()
    initial_draft:dict|None=None

    @classmethod
    def parse(cls,raw):
        allowed={"request_id","topic","audience","platform","target_seconds","style","required_claims","initial_draft"}
        if not isinstance(raw,dict) or set(raw)-allowed: raise ValueError("unknown input fields")
        rid=raw.get("request_id")
        if not isinstance(rid,str) or not rid.strip() or len(rid)>120: raise ValueError("request_id required")
        for key in ("topic","audience","platform","style"):
            if raw.get(key) is not None and (not isinstance(raw[key],str) or len(raw[key])>300):
                raise ValueError("invalid text field")
        seconds=raw.get("target_seconds")
        if seconds is not None and (type(seconds) is not int or not 1<=seconds<=180): raise ValueError("target_seconds must be 1..180 integer")
        if raw.get("platform") and raw["platform"] not in PLATFORMS: raise ValueError("unsupported platform")
        if raw.get("style") and raw["style"] not in STYLES: raise ValueError("unsupported style")
        claims=raw.get("required_claims",[])
        if not isinstance(claims,list) or len(claims)>4 or any(not isinstance(c,str) or not c.strip() or len(c)>200 for c in claims):
            raise ValueError("invalid required_claims")
        if raw.get("initial_draft") is not None and not isinstance(raw["initial_draft"],dict): raise ValueError("initial_draft must be an object")
        return cls(rid,raw.get("topic"),raw.get("audience"),raw.get("platform"),seconds,raw.get("style"),tuple(claims),raw.get("initial_draft"))

    @property
    def missing(self):
        return [k for k in ("topic","audience","platform","target_seconds","style") if getattr(self,k) is None or (isinstance(getattr(self,k),str) and not getattr(self,k).strip())]

    def as_dict(self):
        value=asdict(self)
        value["required_claims"]=list(self.required_claims)
        return value
