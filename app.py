# app.py — Resilient GHL Time Zone updater (Google Geocoding + Time Zone API)
# - Auto-detects the "Time Zone" custom field by label (tries multiple endpoints)
# - Returns immediately to GHL (background task) to avoid webhook timeouts
# - Uses POST variants to update the contact (works where PUT/PATCH 404)

import os, time, requests
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel

# ===== Required env vars =====
GOOGLE = os.environ["GOOGLE_API_KEY"]        # Geocoding + Time Zone APIs enabled; billing ON
GHL_KEY = os.environ["GHL_API_KEY"]          # GoHighLevel Location API key
LOCATION_ID = os.environ["GHL_LOCATION_ID"]  # GoHighLevel Location (sub-account) ID

# ===== Optional env vars =====
TZ_FIELD_ID_ENV = os.getenv("TZ_FIELD_ID")                 # If omitted, we’ll find it by label
TZ_FIELD_LABEL = os.getenv("TZ_FIELD_LABEL", "Time Zone")  # Label used in GHL
TZ_NAME_FIELD_ID = os.getenv("TZ_NAME_FIELD_ID")           # Optional: store human-readable TZ name

# ===== GHL base/headers =====
GHL_BASE = "https://services.leadconnectorhq.com"
HEADERS = {
    "Authorization": f"Bearer {GHL_KEY}",
    "Version": "2021-07-28",
    "Content-Type": "application/json",
    "Location-Id": LOCATION_ID,
}

app = FastAPI()
_cache_field_ids = {"tz": TZ_FIELD_ID_ENV}  # prefer explicit env if provided


# ---------- Models ----------
class GHLHook(BaseModel):
    contact_id: str | None = None
    id: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    zip: str | None = None


# ---------- Helpers ----------
def ensure_tz_field_id():
    """Return custom field ID for Time Zone, trying several endpoints; None if not found."""
    if _cache_field_ids.get("tz"):
        return _cache_field_ids["tz"]

    candidates = [
        (f"{GHL_BASE}/custom-fields", {"locationId": LOCATION_ID}),      # kebab
        (f"{GHL_BASE}/customFields", {"locationId": LOCATION_ID}),       # camel
        (f"{GHL_BASE}/locations/{LOCATION_ID}/customFields", None),      # scoped
    ]
    for url, params in candidates:
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=20)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            j = r.json()
            fields = j.get("customFields") or j.get("fields") or j.get("data") or []
            for f in fields:
                label = (f.get("label") or f.get("name") or "").strip()
                if label.lower() == TZ_FIELD_LABEL.strip().lower():
                    _cache_field_ids["tz"] = f["id"]
                    return f["id"]
        except Exception:
            continue
    return None  # fine if missing


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


def update_ghl(contact_id: str, tz_id: str, tz_name: str | None):
    """Update the system timeZone and optional custom fields using POST variants."""
    cf_id = ensure_tz_field_id()
    custom_fields = []
    if cf_id:
        custom_fields.append({"id": cf_id, "value": tz_id})
    if tz_name and TZ_NAME_FIELD_ID:
        custom_fields.append({"id": TZ_NAME_FIELD_ID, "value": tz_name})

    payload = {"id": contact_id, "timeZone": tz_id}
    if custom_fields:
        payload["customFields"] = custom_fields

    headers = HEADERS | {"Accept": "application/json"}

    variants = [
        ("POST", f"{GHL_BASE}/contacts",         payload),
        ("POST", f"{GHL_BASE}/contacts/",        payload),
        ("POST", f"{GHL_BASE}/contacts/upsert",  payload),
        ("POST", f"{GHL_BASE}/contacts/upsert/", payload),
    ]

    for method, url, body in variants:
        try:
            r = requests.post(url, json=body, headers=headers, timeout=20)
            if r.status_code < 300:
                print(f"[TZ-UPDATER] Updated via {url}")
                return
            print(f"[TZ-UPDATER][UPDATE-ERR] {method} {url} {r.status_code} {r.text}")
        except Exception as e:
            print(f"[TZ-UPDATER][UPDATE-ERR] {method} {url} EXC {e}")

    raise HTTPException(502, "All contact POST variants failed")


# ---------- Routes ----------
@app.get("/health")
def health():
    return {"ok": True}


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
