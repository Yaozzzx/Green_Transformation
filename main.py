from __future__ import annotations

import math
import time
from typing import Dict, List, Optional, Any

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Street Tree Planting Assistant (MVP)")

# If you serve frontend from same FastAPI, CORS isn't needed, but harmless.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# Config (Gowanus default bbox)
# ----------------------------
CENTER_LAT = 40.676
CENTER_LON = -73.991
# ~700m-ish bbox
DLAT = 0.0063
DLON = 0.0083

DEFAULT_BBOX = {
    "minLat": CENTER_LAT - DLAT,
    "maxLat": CENTER_LAT + DLAT,
    "minLon": CENTER_LON - DLON,
    "maxLon": CENTER_LON + DLON,
}

NYC_TREES_ENDPOINT = "https://data.cityofnewyork.us/resource/uvpi-gqnh.json"


# ----------------------------
# Species library (placeholder)
# Replace coefficients with USDA/i-Tree calibrated values later.
# ----------------------------
SPECIES_DB = [
    {
        "species_id": "red_maple",
        "common": "Red Maple",
        "latin": "Acer rubrum",
        # DBH growth inches/year (simple)
        "dbh_growth_in_per_year": 0.35,
        # canopy radius at maturity ~ k * DBH(in) => meters (rough proxy)
        "canopy_k_m_per_in": 0.45,
        # benefit coefficients per year (rough demo scalars)
        "carbon_kg_per_in_dbh": 3.0,
        "stormwater_l_per_m2_canopy": 45.0,
        "pollution_g_per_m2_canopy": 12.0,
    },
    {
        "species_id": "london_plane",
        "common": "London Plane",
        "latin": "Platanus × acerifolia",
        "dbh_growth_in_per_year": 0.50,
        "canopy_k_m_per_in": 0.55,
        "carbon_kg_per_in_dbh": 4.2,
        "stormwater_l_per_m2_canopy": 55.0,
        "pollution_g_per_m2_canopy": 14.0,
    },
    {
        "species_id": "ginkgo",
        "common": "Ginkgo",
        "latin": "Ginkgo biloba",
        "dbh_growth_in_per_year": 0.25,
        "canopy_k_m_per_in": 0.35,
        "carbon_kg_per_in_dbh": 2.2,
        "stormwater_l_per_m2_canopy": 35.0,
        "pollution_g_per_m2_canopy": 9.0,
    },
]

SPECIES_BY_ID = {s["species_id"]: s for s in SPECIES_DB}


# ----------------------------
# In-memory scenario state (MVP)
# For production: PostGIS tables.
# ----------------------------
SCENARIO = {
    "id": "default",
    "created_at": time.time(),
    "planted_trees": {},  # tree_id -> dict
}
REMOVED_EXISTING_IDS = set()  # ids removed client-side for existing census trees


# ----------------------------
# Utility / modeling (simple)
# ----------------------------
def canopy_radius_m(species_id: str, dbh_in: float) -> float:
    s = SPECIES_BY_ID[species_id]
    # Very rough proxy: radius proportional to DBH
    return max(0.5, s["canopy_k_m_per_in"] * max(0.0, dbh_in))

def canopy_area_m2(radius_m: float) -> float:
    return math.pi * radius_m * radius_m

def annual_benefits(species_id: str, dbh_in: float) -> Dict[str, float]:
    s = SPECIES_BY_ID[species_id]
    r = canopy_radius_m(species_id, dbh_in)
    a = canopy_area_m2(r)
    # Demo models:
    carbon = s["carbon_kg_per_in_dbh"] * max(0.0, dbh_in)               # kg/year
    stormwater = s["stormwater_l_per_m2_canopy"] * a                    # liters/year
    pollution = s["pollution_g_per_m2_canopy"] * a                      # grams/year
    return {
        "canopy_radius_m": r,
        "canopy_area_m2": a,
        "carbon_kg_per_year": carbon,
        "stormwater_l_per_year": stormwater,
        "pollution_g_per_year": pollution,
    }

def project_tree(species_id: str, dbh_in: float, years: int) -> Dict[str, float]:
    s = SPECIES_BY_ID[species_id]
    dbh_t = dbh_in + s["dbh_growth_in_per_year"] * years
    return annual_benefits(species_id, dbh_t)

def make_feature(lon: float, lat: float, props: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "properties": props,
    }


# ----------------------------
# Routes
# ----------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    # Serve the frontend HTML from the backend so you avoid CORS + file:// issues.
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/api/species")
def list_species():
    return {"species": SPECIES_DB}

@app.get("/api/existing-trees")
def existing_trees(
    minLat: float = DEFAULT_BBOX["minLat"],
    maxLat: float = DEFAULT_BBOX["maxLat"],
    minLon: float = DEFAULT_BBOX["minLon"],
    maxLon: float = DEFAULT_BBOX["maxLon"],
    limit: int = 2000,
):
    # Bounding-box query against NYC Open Data 2015 census
    # Keep fields needed for popup + filtering
    soql = (
        f"?$select=tree_id,spc_common,spc_latin,health,status,tree_dbh,steward,curb_loc,"
        f"zipcode,boroname,nta,latitude,longitude,created_at"
        f"&$where=latitude between {minLat} and {maxLat} and longitude between {minLon} and {maxLon}"
        f"&$limit={min(limit, 50000)}"
    )

    url = NYC_TREES_ENDPOINT + soql
    resp = requests.get(url, headers={"Accept": "application/json"}, timeout=30)
    if not resp.ok:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    rows = resp.json()
    features = []
    for r in rows:
        tid = r.get("tree_id")
        if tid is not None and str(tid) in REMOVED_EXISTING_IDS:
            continue

        lat = r.get("latitude")
        lon = r.get("longitude")
        try:
            lat = float(lat)
            lon = float(lon)
        except Exception:
            continue

        props = {
            "source": "nyc_tree_census_2015",
            "tree_id": r.get("tree_id"),
            "spc_common": r.get("spc_common"),
            "spc_latin": r.get("spc_latin"),
            "health": r.get("health"),
            "status": r.get("status"),
            "tree_dbh": r.get("tree_dbh"),
            "steward": r.get("steward"),
            "curb_loc": r.get("curb_loc"),
            "zipcode": r.get("zipcode"),
            "boroname": r.get("boroname"),
            "nta": r.get("nta"),
        }
        features.append(make_feature(lon, lat, props))

    return {"type": "FeatureCollection", "features": features}

@app.post("/api/existing-trees/{tree_id}/remove")
def remove_existing_tree(tree_id: str):
    REMOVED_EXISTING_IDS.add(str(tree_id))
    return {"ok": True, "removed_tree_id": str(tree_id)}

@app.get("/api/scenario")
def get_scenario():
    planted = list(SCENARIO["planted_trees"].values())
    return {
        "scenario_id": SCENARIO["id"],
        "planted_trees": planted,
    }

@app.post("/api/scenario/trees")
def add_planted_tree(payload: Dict[str, Any]):
    # payload: { lon, lat, species_id, dbh_in, planting_year }
    try:
        lon = float(payload["lon"])
        lat = float(payload["lat"])
        species_id = str(payload.get("species_id", "red_maple"))
        dbh_in = float(payload.get("dbh_in", 2.0))
        planting_year = int(payload.get("planting_year", 2026))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Bad payload: {e}")

    if species_id not in SPECIES_BY_ID:
        raise HTTPException(status_code=400, detail="Unknown species_id")

    # Simple id
    tid = f"planted_{int(time.time()*1000)}_{len(SCENARIO['planted_trees'])+1}"
    benefits = annual_benefits(species_id, dbh_in)

    tree = {
        "id": tid,
        "lon": lon,
        "lat": lat,
        "species_id": species_id,
        "dbh_in": dbh_in,
        "planting_year": planting_year,
        "benefits": benefits,
    }
    SCENARIO["planted_trees"][tid] = tree
    return {"ok": True, "tree": tree}

@app.patch("/api/scenario/trees/{tree_id}")
def update_planted_tree(tree_id: str, payload: Dict[str, Any]):
    if tree_id not in SCENARIO["planted_trees"]:
        raise HTTPException(status_code=404, detail="Tree not found")

    t = SCENARIO["planted_trees"][tree_id]

    if "lon" in payload: t["lon"] = float(payload["lon"])
    if "lat" in payload: t["lat"] = float(payload["lat"])
    if "dbh_in" in payload: t["dbh_in"] = float(payload["dbh_in"])
    if "planting_year" in payload: t["planting_year"] = int(payload["planting_year"])
    if "species_id" in payload:
        sid = str(payload["species_id"])
        if sid not in SPECIES_BY_ID:
            raise HTTPException(status_code=400, detail="Unknown species_id")
        t["species_id"] = sid

    t["benefits"] = annual_benefits(t["species_id"], t["dbh_in"])
    return {"ok": True, "tree": t}

@app.delete("/api/scenario/trees/{tree_id}")
def delete_planted_tree(tree_id: str):
    if tree_id in SCENARIO["planted_trees"]:
        del SCENARIO["planted_trees"][tree_id]
    return {"ok": True}

@app.get("/api/scenario/projection")
def scenario_projection(horizon: int = 10):
    if horizon not in (5, 10, 15, 20):
        raise HTTPException(status_code=400, detail="horizon must be one of 5,10,15,20")

    total = {
        "carbon_kg_per_year": 0.0,
        "stormwater_l_per_year": 0.0,
        "pollution_g_per_year": 0.0,
        "canopy_area_m2": 0.0,
    }

    per_tree = []
    for t in SCENARIO["planted_trees"].values():
        proj = project_tree(t["species_id"], float(t["dbh_in"]), horizon)
        per_tree.append({"id": t["id"], "projection": proj})
        total["carbon_kg_per_year"] += proj["carbon_kg_per_year"]
        total["stormwater_l_per_year"] += proj["stormwater_l_per_year"]
        total["pollution_g_per_year"] += proj["pollution_g_per_year"]
        total["canopy_area_m2"] += proj["canopy_area_m2"]

    return {
        "horizon_years": horizon,
        "total": total,
        "per_tree": per_tree,
    }
