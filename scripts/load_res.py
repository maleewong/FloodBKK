import json,sys
def load(path):
    raw=json.load(open(path)); t=raw[0]['text']
    if '\n\n(captured' in t: t=t[:t.rindex('\n\n(captured')]
    s=json.loads(t) if not t.endswith(']') or True else None
    while isinstance(s,str):
        s,_=json.JSONDecoder().raw_decode(s)
    return s
