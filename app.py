# app.py
import os, requests
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel

GOOGLE = os.environ["GOOGLE_API_KEY"]
GHL_KEY = os.environ["GHL_API_KEY"]
LOCATION_ID = os.environ["GHL_LOCATION_ID"]
TZ_FIELD = os.environ["TZ_FIELD_ID"]
TZ_NAME_FIELD = os.getenv("TZ_NAME_FIELD_ID")

app = FastAPI()

class GHLHook(BaseModel):
    contact_id: str
    address: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None

def geocode(addr):
    url = "https://maps.googleapis.com/maps/api/geocode/json"
    r = requests.get(url, params={"address": addr, "key": GOOGLE}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if not j.get("results"):
        raise HTTPException(422, "Geocode failed")
    loc = j["results"][0]["geometry"]["location"]
    return loc["lat"], loc["lng"]

def tz_for(lat, lng):
    import time
    url = "https://maps.googleapis.com/maps/api/timezone/json"
    r = requests.get(url, params={"location": f"{lat},{lng}",
                                  "timestamp": int(time.time()),
                                  "key": GOOGLE}, timeout=20)
    r.raise_for_status()
    j = r.json()
    if j.get("status") != "OK":
        raise HTTPException(422, "Timezone lookup failed")
    return j["timeZoneId"], j["timeZoneName"]

def update_ghl(contact_id, tz_id, tz_name=None):
    url = "https://services.leadconnectorhq.com/contacts/"
    headers = {
        "Authorization": f"Bearer {GHL_KEY}",
        "Version": "2021-07-28",
        "Content-Type": "application/json",
        "Location-Id": LOCATION_ID,
    }
    payload = {
        "id": contact_id,
        "customFields": [
            {"id": TZ_FIELD, "value": tz_id}
        ]
    }
    if tz_name and TZ_NAME_FIELD:
        payload["customFields"].append({"id": TZ_NAME_FIELD, "value": tz_name})
    r = requests.put(url, json=payload, headers=headers, timeout=20)
    if r.status_code >= 300:
        raise HTTPException(r.status_code, f"GHL update failed: {r.text}")
    return True

@app.post("/ghl/webhook")
async def ghl_webhook(body: GHLHook):
    # Build address string (minimal: zip only still works for geocode)
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
