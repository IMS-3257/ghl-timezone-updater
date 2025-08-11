# app.py  — GHL Time Zone Updater (Google Geocoding + Time Zone API)
# - Auto-detects the "Time Zone" custom field ID by label (no manual ID needed)
# - Returns immediately to GHL and processes in the background (fixes webhook timeouts)

import os
import time
import requests
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

# ---- Environment (required) ----
GOOGLE = os.environ["GOOGLE_API_KEY"]        # Google Cloud API key (Geocoding + Time Zone APIs enabled; billing ON)
GHL_KEY = os.environ["GHL_API_KEY"]          # GoHighLevel Location API key
LOCATION_ID = os.environ["GHL_LOCATION_ID"]  # GoHighLevel Location (sub-account) ID

# ---- Optional envs ----
TZ_FIELD_ID_ENV = os.getenv("TZ_FIELD_ID")                 # If omitted, we'll find by label
TZ_FIELD_LABEL = os.getenv("TZ_FIELD_LABEL", "Time Zone")  # Label to search if no ID provided
TZ_NAME_FIELD_ID = os.getenv("TZ_NAME_FIELD_ID")           # Optional: field to store "Pacific Daylight Time", etc.

# ---- GHL API base/headers ----
GHL_BASE = "https://services.leadconnectorhq.com"
HEADERS = {
    "Authorization": f"Bearer {GHL_KEY}",
    "Version": "2021-07-28",
    "Content-Type": "application/json",
    "Location-Id": LOCATION_ID,
}

app = FastAPI()
_cache_field_ids = {"tz": TZ_FIELD_ID_ENV}  # prefer env TZ_FIELD_ID if provided


# ---------- Models ----------
class GHLHook(BaseModel):
    contact_id: str
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None


# ---------- Helpers ----------
def ensure_tz_field_id() -> str:
    """Return the Time Zone custom field ID; auto-lookup by label if not set."""
    if _cache_field_ids.get("tz"):
        return _cache_field_ids["tz"]

    # Fetch all custom fields for the location
    r = requests.get(
        f"{GHL_BASE}/custom-fields/",
        headers=HEADERS,
        params={"locationId": LOCATION_ID},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()

    # GHL may return under "customFields" or "fields"
    fields = data.get("customFields") or data.get("fields") or []
    for f in fields:
        label = (f.get("label") or f.get("name") or "").strip()
        if label.lower() == TZ_FIELD_LABEL.strip().lower():
            _cache_field_ids["tz"] = f["id"]
            return f["id"]

    raise HTTPException(404, f'Custom field not found by label: "{TZ_FIELD_LABEL}"')

def geocode(full_address: str) -> tuple[float, float]:
    r = requests.get(
        "https://maps.googleapis.com/maps/api/geocode/json",
        params={"address": full_address, "key": GOOGLE},
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    if not j.get("results"):
        raise HTTPException(422, f"Geocode failed for: {full_address}")
    loc = j["results"][0]["geometry"]["location"]
    return float(loc["lat"]), float(loc["lng"])

def tz_for(lat: float, lng: float) -> tuple[str, str | None]:
    r = requests.get(
        "https://maps.googleapis.com/maps/api/timezone/json",
        params={"location": f"{lat},{lng}", "timestamp": int(time.time()), "key": GOOGLE},
        timeout=20,
    )
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "OK":
        raise HTTPException(422, f"Timezone lookup failed: {j.get('status')}")
    return j["timeZoneId"], j.get("timeZoneName")

def update_ghl(contact_id: str, tz_id: str, tz_name: str | None = None) -> None:
    tz_field_id = ensure_tz_field_id()
    payload = {"id": contact_id, "customFields": [{"id": tz_field_id, "value": tz_id}]}
    if tz_name and TZ_NAME_FIELD_ID:
        payload["customFields"].append({"id": TZ_NAME_FIELD_ID, "value": tz_name})

    r = requests.put(f"{GHL_BASE}/contacts/", json=payload, headers=HEADERS, timeout=20)
    if r.status_code >= 300:
        raise HTTPException(r.status_code, f"GHL update failed: {r.text}")


# ---------- Routes ----------
@app.get("/health")
def health():
    return {"ok": True}

@app.post("/ghl/webhook")
def ghl_webhook(body: GHLHook, background: BackgroundTasks):
    # Build a single address string (ZIP-only works too)
    parts = [body.address or "", body.city or "", body.state or "", body.zip or ""]
    full = ", ".join([p.strip() for p in parts if p and p.strip()])
    if not full:
        # Do not error hard; return quickly so GHL step completes
        return {"ok": False, "error": "missing address/city/state/zip"}

    # Background job so GHL doesn't time out on free hosting
    def job():
        try:
            lat, lng = geocode(full)
            tz_id, tz_name = tz_for(lat, lng)
            update_ghl(body.contact_id, tz_id, tz_name)
            print(f"[TZ-UPDATER] contact={body.contact_id} -> {tz_id} ({tz_name})")
        except Exception as e:
            print(f"[TZ-UPDATER][ERROR] contact={body.contact_id} err={e}")

    background.add_task(job)
    # Immediate response so Webhook step completes fast
    return {"ok": True, "queued": True}
