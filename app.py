import os
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

app = FastAPI()

# Environment variables
GHL_API_KEY = os.getenv("GHL_API_KEY")
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID")
GOOGLE_MAPS_KEY = os.getenv("GOOGLE_MAPS_KEY")
TZ_FIELD_LABEL = os.getenv("TZ_FIELD_LABEL", "Time Zone")
TZ_NAME_FIELD_ID = os.getenv("TZ_NAME_FIELD_ID")

GHL_BASE = "https://services.leadconnectorhq.com"
HEADERS = {
    "Authorization": f"Bearer {GHL_API_KEY}",
    "Version": "2021-07-28",
    "Location-Id": GHL_LOCATION_ID,
    "Content-Type": "application/json"
}

_cache_field_ids = {}


def ensure_tz_field_id():
    """Return custom field ID for Time Zone, trying several endpoints; None if not found."""
    # Avoid repeated network calls if we've already looked up (or failed to look up)
    if "tz" in _cache_field_ids:
        return _cache_field_ids["tz"]

    candidates = [
        (f"{GHL_BASE}/custom-fields", {"locationId": GHL_LOCATION_ID}),      # kebab
        (f"{GHL_BASE}/customFields", {"locationId": GHL_LOCATION_ID}),       # camel
        (f"{GHL_BASE}/locations/{GHL_LOCATION_ID}/customFields", None),      # scoped
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
    # Remember that no field was found so we don't try again
    _cache_field_ids["tz"] = None
    return None


def update_ghl(contact_id: str, tz_id: str, tz_name: str | None):
    cf_id = ensure_tz_field_id()
    custom_fields = []
    if cf_id:
        custom_fields.append({"id": cf_id, "value": tz_id})
    if tz_name and TZ_NAME_FIELD_ID:
        custom_fields.append({"id": TZ_NAME_FIELD_ID, "value": tz_name})

    payload_common = {"id": contact_id, "timeZone": tz_id}
    if custom_fields:
        payload_common["customFields"] = custom_fields

    headers = HEADERS | {"Accept": "application/json"}

    # Try POST variants
    variants = [
        ("POST", f"{GHL_BASE}/contacts", payload_common),
        ("POST", f"{GHL_BASE}/contacts/", payload_common),
        ("POST", f"{GHL_BASE}/contacts/upsert", payload_common),
        ("POST", f"{GHL_BASE}/contacts/upsert/", payload_common),
    ]

    for method, url, payload in variants:
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=20)
            if r.status_code < 300:
                print(f"[TZ-UPDATER] Updated contact {contact_id} to {tz_id}")
                return
            print(f"[TZ-UPDATER][UPDATE-ERR] {method} {url} {r.status_code} {r.text}")
        except Exception as e:
            print(f"[TZ-UPDATER][UPDATE-ERR] {method} {url} EXC {e}")

    raise HTTPException(502, "All contact POST variants failed")


def get_timezone_from_address(address, city, state, zip_code):
    # Build address string
    addr_parts = [address, city, state, zip_code]
    full_address = ", ".join([p for p in addr_parts if p])
    print(f"[TZ-UPDATER] Geocoding: {full_address}")

    # Step 1: Geocode
    geo_url = "https://maps.googleapis.com/maps/api/geocode/json"
    geo_params = {"address": full_address, "key": GOOGLE_MAPS_KEY}
    geo_resp = requests.get(geo_url, params=geo_params, timeout=20)
    geo_resp.raise_for_status()
    geo_data = geo_resp.json()

    if not geo_data.get("results"):
        raise HTTPException(400, f"No geocoding results for: {full_address}")

    loc = geo_data["results"][0]["geometry"]["location"]
    lat, lng = loc["lat"], loc["lng"]

    # Step 2: Timezone
    tz_url = "https://maps.googleapis.com/maps/api/timezone/json"
    tz_params = {"location": f"{lat},{lng}", "timestamp": 0, "key": GOOGLE_MAPS_KEY}
    tz_resp = requests.get(tz_url, params=tz_params, timeout=20)
    tz_resp.raise_for_status()
    tz_data = tz_resp.json()

    if tz_data.get("status") != "OK":
        raise HTTPException(400, f"Timezone lookup failed: {tz_data}")

    return tz_data.get("timeZoneId"), tz_data.get("timeZoneName")


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/ghl/webhook")
async def ghl_webhook(req: Request):
    body = await req.json()
    contact_id = body.get("contact_id") or body.get("id")
    if not contact_id:
        raise HTTPException(400, "Missing contact_id in webhook payload")

    address = body.get("address") or ""
    city = body.get("city") or ""
    state = body.get("state") or ""
    zip_code = body.get("postal_code") or ""

    try:
        tz_id, tz_name = get_timezone_from_address(address, city, state, zip_code)
        update_ghl(contact_id, tz_id, tz_name)
        return JSONResponse({"status": "success", "contact_id": contact_id, "tz_id": tz_id})
    except Exception as e:
        print(f"[TZ-UPDATER][ERROR] contact={contact_id} err={e}")
        raise
