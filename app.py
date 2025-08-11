# app.py — resilient GHL Time Zone updater
import os, time, requests
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

GOOGLE = os.environ["GOOGLE_API_KEY"]
GHL_KEY = os.environ["GHL_API_KEY"]
LOCATION_ID = os.environ["GHL_LOCATION_ID"]

TZ_FIELD_ID_ENV = os.getenv("TZ_FIELD_ID")
TZ_FIELD_LABEL = os.getenv("TZ_FIELD_LABEL", "Time Zone")
TZ_NAME_FIELD_ID = os.getenv("TZ_NAME_FIELD_ID")

GHL_BASE = "https://services.leadconnectorhq.com"
HEADERS = {
    "Authorization": f"Bearer {GHL_KEY}",
    "Version": "2021-07-28",
    "Content-Type": "application/json",
    "Location-Id": LOCATION_ID,
}

app = FastAPI()
_cache_field_ids = {"tz": TZ_FIELD_ID_ENV}

class GHLHook(BaseModel):
    contact_id: str | None = None
    id: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    zip: str | None = None

def ensure_tz_field_id():
    if _cache_field_ids.get("tz"):
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
            data = r.json()
            fields = data.get("customFields") or data.get("fields") or data.get("data") or []
            for f in fields:
                label = (f.get("label") or f.get("name") or "").strip()
                if label.lower() == TZ_FIELD_LABEL.strip().lower():
                    _cache_field_ids["tz"] = f["id"]; return f["id"]
        except Exception:
            pass
    return None

def geocode(addr: str):
    r = requests.get("https://maps.googleapis.com/maps/api/geocode/json",
                     params={"address": addr, "key": GOOGLE}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if not j.get("results"):
        raise HTTPException(422, "Geocode failed")
    loc = j["results"][0]["geometry"]["location"]
    return float(loc["lat"]), float(loc["lng"])

def tz_for(lat: float, lng: float):
    r = requests.get("https://maps.googleapis.com/maps/api/timezone/json",
                     params={"location": f"{lat},{lng}", "timestamp": int(time.time()), "key": GOOGLE}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "OK":
        raise HTTPException(422, f"Timezone lookup failed: {j.get('status')}")
    return j["timeZoneId"], j.get("timeZoneName")

def try_update(url, method, payload):
    if method == "PUT":
        r = requests.put(url, json=payload, headers=HEADERS, timeout=20)
    else:
        r = requests.patch(url, json=payload, headers=HEADERS, timeout=20)
    if r.status_code < 300:
        return True
    print(f"[TZ-UPDATER][UPDATE-ERR] {method} {url} {r.status_code} {r.text}")
    return False

def update_ghl(contact_id: str, tz_id: str, tz_name: str | None):
    cf_id = ensure_tz_field_id()

    # Build variants (system field + optional custom fields)
    base_cf = []
    if cf_id: base_cf.append({"id": cf_id, "value": tz_id})
    if tz_name and TZ_NAME_FIELD_ID: base_cf.append({"id": TZ_NAME_FIELD_ID, "value": tz_name})

    variants = [
        ("PUT",  f"{GHL_BASE}/contacts/",                      {"id": contact_id, "timeZone": tz_id, "customFields": base_cf or None}),
        ("PUT",  f"{GHL_BASE}/contacts/",                      {"id": contact_id, "timezone": tz_id, "customFields": base_cf or None}),
        ("PATCH",f"{GHL_BASE}/contacts/{contact_id}",          {"timeZone": tz_id, "customFields": base_cf or None}),
        ("PATCH",f"{GHL_BASE}/contacts/{contact_id}",          {"timezone": tz_id, "customFields": base_cf or None}),
    ]
    # Clean None values
    variants = [(m,u,{k:v for k,v in p.items() if v is not None}) for (m,u,p) in variants]

    for m,u,p in variants:
        if try_update(u, m, p): return
    raise HTTPException(502, "All contact update variants failed")

@app.get("/health")
def health(): return {"ok": True}

@app.post("/ghl/webhook")
def ghl_webhook(body: GHLHook, background: BackgroundTasks):
    contact_id = body.contact_id or body.id
    zip_code = body.zip or body.postal_code or ""
    parts = [body.address or "", body.city or "", body.state or "", zip_code]
    full = ", ".join([p.strip() for p in parts if p and p.strip()])
    if not contact_id or not full:
        return {"ok": False, "error": "missing contact_id or address parts"}

    def job():
        try:
            lat, lng = geocode(full)
            tz_id, tz_name = tz_for(lat, lng)
            update_ghl(contact_id, tz_id, tz_name)
            print(f"[TZ-UPDATER] contact={contact_id} -> {tz_id} ({tz_name})")
        except Exception as e:
            print(f"[TZ-UPDATER][ERROR] contact={contact_id} err={e}")

    background.add_task(job)
    return {"ok": True, "queued": True}
