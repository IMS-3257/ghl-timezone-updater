# app.py
import os, time, requests
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

GOOGLE = os.environ["GOOGLE_API_KEY"]
GHL_KEY = os.environ["GHL_API_KEY"]
LOCATION_ID = os.environ["GHL_LOCATION_ID"]
TZ_FIELD_ID_ENV = os.getenv("TZ_FIELD_ID")  # optional now
TZ_FIELD_LABEL = os.getenv("TZ_FIELD_LABEL", "Time Zone")
TZ_NAME_FIELD_ID = os.getenv("TZ_NAME_FIELD_ID")  # optional

GHL_BASE = "https://services.leadconnectorhq.com"
HEADERS = {
    "Authorization": f"Bearer {GHL_KEY}",
    "Version": "2021-07-28",
    "Content-Type": "application/json",
    "Location-Id": LOCATION_ID,
}

app = FastAPI()
_cache_field_id = {"tz": TZ_FIELD_ID_ENV}  # prefer env if provided

class GHLHook(BaseModel):
    contact_id: str
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None

def ensure_tz_field_id() -> str:
    """Return Time Zone custom field ID; auto-lookup by label if not set."""
    if _cache_field_id.get("tz"):
        return _cache_field_id["tz"]
    # fetch all custom fields for location
    r = requests.get(f"{GHL_BASE}/custom-fields/", headers=HEADERS, params={"locationId": LOCATION_ID}, timeout=20)
    r.raise_for_status()
    data = r.json()
    for f in data.get("customFields", data.get("fields", [])):
        label = f.get("label") or f.get("name")
        if label and label.strip().lower() == TZ_FIELD_LABEL.strip().lower():
            _cache_field_id["tz"] = f["id"]
            return f["id"]
    raise HTTPException(404, f"Custom field not found by label: {TZ_FIELD_LABEL}")

def geocode(addr: str):
    r = requests.get("https://maps.googleapis.com/maps/api/geocode/json",
                     params={"address": addr, "key": GOOGLE}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if not j.get("results"):
        raise HTTPException(422, "Geocode failed")
    loc = j["results"][0]["geometry"]["location"]
    return loc["lat"], loc["lng"]

def tz_for(lat: float, lng: float):
    r = requests.get("https://maps.googleapis.com/maps/api/timezone/json",
                     params={"location": f"{lat},{lng}", "timestamp": int(time.time()), "key": GOOGLE}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "OK":
        raise HTTPException(422, f"Timezone lookup failed: {j.get('status')}")
    return j["timeZoneId"], j.get("timeZoneName")

def update_ghl(contact_id: str, tz_id: str, tz_name: str | None = None):
    tz_field_id = ensure_tz_field_id()
    payload = {"id": contact_id, "customFields": [{"id": tz_field_id, "value": tz_id}]}
    if tz_name and TZ_NAME_FIELD_ID:
        payload["customFields"].append({"id": TZ_NAME_FIELD_ID, "value": tz_name})
    r = requests.put(f"{GHL_BASE}/contacts/", json=payload, headers=HEADERS, timeout=20)
    if r.status_code >= 300:
        raise HTTPException(r.status_code, f"GHL update failed: {r.text}")

@app.post("/ghl/webhook")
def ghl_webhook(body: GHLHook):
    parts = [body.address or "", body.city or "", body.state or "", body.zip or ""]
    full = ", ".join([p.strip() for p in parts if p and p.strip()])
    if not full:
        raise HTTPException(400, "No address/city/state/zip provided")
    lat, lng = geocode(full)
    tz_id, tz_name = tz_for(lat, lng)
    update_ghl(body.contact_id, tz_id, tz_name)
    return {"ok": True, "contact_id": body.contact_id, "timeZoneId": tz_id, "timeZoneName": tz_name}

@app.get("/health")
def health():
    return {"ok": True}
