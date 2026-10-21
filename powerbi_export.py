import os, time, uuid, hashlib, hmac, base64, csv, io, json
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from mongo_query import count_by_field, sum_by_field
from agent import SCHEMA_CATALOG, _fetch_records, _flatten_record, _extract_array_object_fields

BASE_URL= os.environ["POWERBI_FEED_BASE_URL"].rstrip("/")
SECRET= os.environ.get("POWERBI_FEED_SECRET", "").encode()
TTL_SECONDS= int(os.environ.get("POWERBI_TOKEN_TTL_DAYS", "30"))*86400
OUTPUT_DIR= os.path.join(os.path.dirname(__file__), "exports")


def _sign(raw):
    return hmac.new(SECRET, raw, hashlib.sha256).hexdigest()[:32]

def sign_spec(spec):
    if not SECRET:
        raise RuntimeError("POWERBI_FEED_SECRET is not set")
    payload= {"spec": spec, "exp": int(time.time()) + TTL_SECONDS}
    raw= base64.urlsafe_b64encode(json.dumps(payload, default=str).encode()).rstrip(b'=')
    return f"{raw.decode()}.{_sign(raw)}"

def verify_token(token):
    try:
        raw, sig= token.rsplit(".",1)
        if not hmac.compare_digest(sig, _sign(raw.encode())):
            raise ValueError
        payload= json.loads(base64.urlsafe_b64decode(raw + '='* (-len(raw)%4)))
        if payload["exp"] < time.time():
            raise ValueError
        return payload['spec']
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired feed token")

def fetch_rows(spec):
    kind = spec.get("kind", "records")
    coll = spec["collection_name"]
    dates = {k: spec.get(k) for k in ("date_field", "date_from", "date_to")}
 
    if kind == "records":
        records, err = _fetch_records(spec)
        if err:
            raise HTTPException(status_code=400, detail=err)
        rows = []
        for rec in records:
            row = _flatten_record(rec)
            # _flatten_record skips lists of objects; a CSV has one table, so keep them as JSON text
            for name, items in _extract_array_object_fields(rec).items():
                row[name] = json.dumps(items, default=str)
            rows.append(row)
        return rows
 
    if kind == "count":
        res = count_by_field(SCHEMA_CATALOG, coll, spec["group_by"], filters=spec.get("filters"),
                             date_bucket=spec.get("date_bucket", "auto"), **dates)
        return [{spec["group_by"]: c["group"], "count": c["count"]} for c in res.get("counts", [])]
 
    if kind == "sum":
        res = sum_by_field(SCHEMA_CATALOG, coll, spec["group_by"], spec["sum_fields"],
                           filters=spec.get("filters"), date_bucket=spec.get("date_bucket", "month"), **dates)
        return [
            {spec["group_by"]: g["group"], "records": g["count"],
             **{f: g["sums"].get(f, 0) for f in spec["sum_fields"]}}
            for g in res.get("groups", [])
        ]
 
    raise HTTPException(status_code=400, detail=f"Unknown kind: {kind}")
 
 
def rows_to_csv(rows):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()
 
 
def register_feed(app):
    """Call once at startup: register_feed(app)"""
    @app.get("/powerbi/feed.csv")
    def powerbi_feed(t: str):
        return Response(content=rows_to_csv(fetch_rows(verify_token(t))), media_type="text/csv")
 
 
def create_powerbi_file(spec, label=None):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    feed_url = f"{BASE_URL}/powerbi/feed.csv?t={sign_spec(spec)}"
    pbids = {"version": "0.1", "connections": [{
        "details": {"protocol": "http", "address": {"url": feed_url}},
        "options": {}, "mode": "Import",
    }]}
    safe = "".join(c for c in (label or spec["collection_name"]) if c.isalnum() or c in "-_") or "report"
    path = os.path.join(OUTPUT_DIR, f"powerbi_{safe}_{uuid.uuid4().hex[:6]}.pbids")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(pbids, f, indent=2)
 
    message = f"Power BI file created: {path}. Open it in Power BI Desktop (choose Anonymous if asked)."
    return {"message": message}, {"type": "powerbi_file", "path": path}
