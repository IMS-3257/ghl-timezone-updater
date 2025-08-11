# app.py — GHL Time Zone Updater with /diag auth check
# Env vars used: GOOGLE_API_KEY, GHL_API_KEY, GHL_LOCATION_ID
# ZIP -> city+state -> state-only fallback. Background processing.
# Contact update via POST variants. /diag tests JWT from the server.

import os, time, requests
from fastapi import FastAPI, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel

# ===== Required env =====
GOOGLE = os.environ["GOOGLE_API_KEY"]
GHL_API_KEY = os.environ["GHL_API_KEY"]
LOCATION_ID = os.environ["GHL_LOCATION_ID"]

# ===== Optional env =====
TZ_FIELD_LABEL = os.getenv("TZ_FIELD_LABEL", "Time Zone")
TZ_FIELD_ID_ENV = os.getenv("TZ_FIELD_ID")
TZ_NAME_FIELD_ID = os.getenv("TZ_NAME_FIELD_ID")

# ===== GHL API =====
GHL_BASE = "https://services.leadconnectorhq.com"
HEADERS = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Version": "2021-07-28",
    "Location-Id": LOCATION_ID,
    "Content-Type": "application/json",
    "Accept": "application/json",
}

app = FastAPI()
_cache_field_ids = {"tz": TZ_FIELD_ID_ENV}

STATE_TZ = {
    "AL":"America/Chicago","AK":"America/Anchorage","AZ":"America/Phoenix","AR":"America/Chicago",
    "CA":"America/Los_Angeles","CO":"America/Denver","CT":"America/New_York","DE":"America/New_York",
    "DC":"America/New_York","FL":"America/New_York","GA":"America/New_York","HI":"Pacific/Honolulu",
    "ID":"America/Boise","IL":"America/Chicago","IN":"America/Indiana/Indianapolis","IA":"America/Chicago",
    "KS":"America/Chicago","KY":"America/New_York","LA":"America/Chicago","ME":"America/New_York",
    "MD":"America/New_York","MA":"America/New_York","MI":"America/Detroit","MN":"America/Chicago",
    "MS":"America/Chicago","MO":"America/Chicago","MT":"America/Denver","NE":"America/Chicago",
    "NV":"America/Los_Angeles","NH":"America/New_York","NJ":"America/New_York","NM":"America/Denver",
    "NY":"America/New_York","NC":"America/New_York","ND":"America/Chicago","OH":"America/New_York",
    "OK":"America/Chicago","OR":"America/Los_Angeles","PA":"America/New_York","RI":"America/New_York",
    "SC":"America/New_York","SD":"America/Chicago","TN":"America/Chicago","TX":"America/Chicago",
    "UT":"America/Denver","VT":"America/New_York","VA":"America/New_York","WA":"America/Los_Angeles",
    "WV":"America/New_York","WI":"America/Chicago","WY":"America/Denver",
}

class GHLHook(BaseModel):
    contact_id: str | None = None
    id: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    zip: str | None = None
    contact: dict | None = None

def get_first(payload: dict, keys: list[str]) -> str | None:
    for k in keys:
        v = payload.get(k)
        if v: return str(v)
    c = payload.get("contact")
    if isinstance(c, dict):
        for k in keys:
            v = c.get(k)
            if v: return str(v)
    return None

def ensure_tz_field_id() -> str | None:
    if "tz" in _cache_field_ids:
        return _cache_field_ids["tz"]
    for url, params in [
        (f"{GHL_BASE}/custom-fields", {"locationId": LOCATION_ID}),
        (f"{GHL_BASE}/customFields", {"locationId": LOCATION_ID}),
        (f"{GHL_BASE}/locations/{LOCATION_ID}/customFields", None),
    ]:
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=20)
            if r.status_code == 404: continue
            r.raise_for_status()
            j = r.json()
            fields = j.get("customFields") or j.get("fields") or j.get("data") or []
            for f in fields:
                label = (f.get("label") or f.get("name") or "").strip()
                if label.lower() == TZ_FIELD_LABEL.strip().lower():
                    _cache_field_ids["tz"] = f["id"]; return f["id"]
        except Exception:
            continue
    _cache_field_ids["tz"] = None
    return None

def geocode(address_str: str):
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/geocode/json",
                         params={"address": address_str, "key": GOOGLE}, timeout=20)
        r.raise_for_status()
        j = r.json()
        if not j.get("results"): return None
        loc = j["results"][0]["geometry"]["location"]
        return float(loc["lat"]), float(loc["lng"])
    except Exception:
        return None

def tz_for(lat: float, lng: float):
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/timezone/json",
                         params={"location": f"{lat},{lng}", "timestamp": int(time.time()), "key": GOOGLE},
                         timeout=20)
        r.raise_for_status()
        j = r.json()
        if j.get("status") != "OK": return None
        return j["timeZoneId"], j.get("timeZoneName")
    except Exception:
        return None

def update_contact(contact_id: str, tz_id: str, tz_name: str | None):
    cf_id = ensure_tz_field_id()
    payload = {"id": contact_id, "timeZone": tz_id}
    cf = []
    if cf_id: cf.append({"id": cf_id, "value": tz_id})
    if tz_name and TZ_NAME_FIELD_ID: cf.append({"id": TZ_NAME_FIELD_ID, "value": tz_name})
    if cf: payload["customFields"] = cf

    for url in [f"{GHL_BASE}/contacts", f"{GHL_BASE}/contacts/", f"{GHL_BASE}/contacts/upsert", f"{GHL_BASE}/contacts/upsert/"]:
        try:
            r = requests.post(url, json=payload, headers=HEADERS, timeout=20)
            if r.status_code < 300:
                print(f"[TZ-UPDATER] Updated {contact_id} -> {tz_id}"); return
            print(f"[TZ-UPDATER][UPDATE-ERR] POST {url} {r.status_code} {r.text}")
        except Exception as e:
            print(f"[TZ-UPDATER][UPDATE-ERR] POST {url} EXC {e}")
    raise HTTPException(502, "All contact POST variants failed")

@app.get("/health")
def health(): return {"ok": True}

@app.get("/diag")
def diag():
    """Server-side JWT sanity check."""
    try:
        r = requests.get(f"{GHL_BASE}/users/me", headers=HEADERS, timeout=15)
        return {"status": r.status_code, "ok": r.status_code < 300, "body": r.json(), "loc": LOCATION_ID,
                "token_len": len(GHL_API_KEY)}
    except Exception as e:
        return {"status": "error", "error": str(e), "loc": LOCATION_ID, "token_len": len(GHL_API_KEY)}

@app.post("/ghl/webhook")
async def ghl_webhook(req: Request, background: BackgroundTasks):
    body = await req.json()
    contact_id = get_first(body, ["contact_id","id"])
    if not contact_id: return {"ok": False, "error": "missing contact_id"}

    zip_code = (get_first(body, ["postal_code","zip"]) or "").strip()
    city = (get_first(body, ["city"]) or "").strip()
    state = (get_first(body, ["state"]) or "").strip().upper()
    address = (get_first(body, ["address"]) or "").strip()

    candidates = []
    if zip_code: candidates.append(f"{zip_code}, USA")
    if city and state: candidates.append(f"{city}, {state}, USA")
    if address and (city or state): candidates.insert(0, f"{address}, {city}, {state}, USA")
    if state: candidates.append(f"{state}, USA")

    def job():
        try:
            tz_id = None; tz_name = None
            for a in candidates:
                if not a: continue
                coords = geocode(a)
                if coords:
                    tz = tz_for(*coords)
                    if tz: tz_id, tz_name = tz; break
            if not tz_id and state in STATE_TZ:
                tz_id = STATE_TZ[state]
            if not tz_id:
                print(f"[TZ-UPDATER][WARN] No TZ derived. body={body}"); return
            update_contact(contact_id, tz_id, tz_name)
            print(f"[TZ-UPDATER] contact={contact_id} -> {tz_id} ({tz_name})")
        except Exception as e:
            print(f"[TZ-UPDATER][ERROR] contact={contact_id} err={e}")

    background.add_task(job)
    return {"ok": True, "queued": True}
