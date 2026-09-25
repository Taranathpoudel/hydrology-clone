"""
FastAPI backend for hydrology.gov.np (DHM Nepal).

Aggregates:
  - station catalog         : GET /gss/api/station
  - river water levels      : socket.io poll -> event "river_test"
  - rainfall observations   : socket.io poll -> event "rainfall_watch"

Computes per station:
  - warning_level_diff  (WL - warning)
  - danger_level_diff   (WL - danger)
  - trend (RISING / FALLING / STEADY)
  - reporting delay in minutes (for 10-min telemetry watch)

Exposes clean JSON for a thin Flask front end.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
UPSTREAM = "https://hydrology.gov.np"
REFRESH_SECONDS = 60          # background refresh cadence
USER_AGENT = "DHM-Dashboard/1.0 (public-data viewer)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dhm")


# ----------------------------------------------------------------------------
# Upstream client
# ----------------------------------------------------------------------------
class DHMClient:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- public API ----------------------------------------------------------
    async def catalog(self) -> list[dict]:
        try:
            r = await self._http.get(f"{UPSTREAM}/gss/api/station")
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except httpx.HTTPError as e:
            log.warning("catalog fetch failed: %s", e)
            return []

    async def telemetry(self) -> dict[str, list]:
        sid = await self._handshake()
        if sid is None:
            return {"river": [], "rainfall": []}

        await asyncio.gather(
            self._emit(sid, "river_test"),
            self._emit(sid, "rainfall_watch"),
        )
        await asyncio.sleep(1.2)

        raw = await self._poll(sid)
        events = self._parse_packets(raw)

        river: list = []
        rainfall: list = []
        for name, payload in events:
            if name in ("river_test", "river_watch") and isinstance(payload, list):
                river = payload
            elif name == "rainfall_watch" and isinstance(payload, list):
                rainfall = payload
        return {"river": river, "rainfall": rainfall}

    # -- low level -----------------------------------------------------------
    async def _handshake(self) -> Optional[str]:
        url = f"{UPSTREAM}/gss/socket.io/?EIO=3&transport=polling&t={int(time.time() * 1000)}"
        try:
            r = await self._http.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("handshake failed: %s", e)
            return None
        m = re.search(r'"sid"\s*:\s*"([^"]+)"', r.text)
        return m.group(1) if m else None

    async def _emit(self, sid: str, event: str) -> None:
        payload = f'42["client_request","{event}"]'
        body = f"{len(payload)}:{payload}"
        url = f"{UPSTREAM}/gss/socket.io/?EIO=3&transport=polling&sid={sid}&t={int(time.time() * 1000)}"
        try:
            await self._http.post(
                url, content=body,
                headers={"Content-Type": "text/plain;charset=UTF-8"},
            )
        except httpx.HTTPError as e:
            log.warning("emit %s failed: %s", event, e)

    async def _poll(self, sid: str) -> str:
        url = f"{UPSTREAM}/gss/socket.io/?EIO=3&transport=polling&sid={sid}&t={int(time.time() * 1000)}"
        try:
            r = await self._http.get(url)
            r.raise_for_status()
            return r.text
        except httpx.HTTPError as e:
            log.warning("poll failed: %s", e)
            return ""

    @staticmethod
    def _parse_packets(raw: str) -> list[tuple[str, Any]]:
        """Engine.IO v3 length-prefixed packet parser."""
        out: list[tuple[str, Any]] = []
        i = 0
        while i < len(raw):
            colon = raw.find(":", i)
            if colon == -1:
                break
            try:
                length = int(raw[i:colon])
            except ValueError:
                break
            payload = raw[colon + 1: colon + 1 + length]
            if payload.startswith("42["):
                try:
                    parsed = json.loads(payload[2:])
                    if isinstance(parsed, list) and parsed:
                        out.append((parsed[0], parsed[1] if len(parsed) > 1 else None))
                except json.JSONDecodeError:
                    pass
            i = colon + 1 + length
        return out


# ----------------------------------------------------------------------------
# Processing helpers
# ----------------------------------------------------------------------------
def _to_float(v: Any) -> Optional[float]:
    try:
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


def _parse_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _delay_minutes(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    delta = datetime.now(timezone.utc) - dt
    return max(0, int(delta.total_seconds() // 60))


def _level_diff(water: Optional[float], threshold: Optional[float]) -> Optional[float]:
    if water is None or threshold is None or threshold <= 0:
        return None
    return round(water - threshold, 2)


def _index_catalog(catalog: list[dict]) -> dict[int, dict]:
    indexed: dict[int, dict] = {}
    for st in catalog:
        try:
            sid = int(st.get("id"))
        except (TypeError, ValueError):
            continue
        meta = st.get("meta_data") or []
        basin = district = station_index = nepali = ""
        for m in meta:
            name = m.get("name")
            val = (m.get("value") or "").strip()
            if name == "Basin":
                basin = val
            elif name == "District":
                district = val
            elif name == "Station Index":
                station_index = val
            elif name == "Nepali Name":
                nepali = val
        indexed[sid] = {
            "id": sid,
            "name": st.get("name") or f"Station {sid}",
            "nepali_name": nepali,
            "station_index": station_index or st.get("description") or "",
            "basin": basin or st.get("folder_name") or "Other",
            "district": district or "Unknown",
            "latitude": st.get("latitude"),
            "longitude": st.get("longitude"),
            "elevation": st.get("elevation"),
            "water_level": None,
            "warning_level": None,
            "danger_level": None,
            "rainfall": None,
            "water_level_time": None,
            "rainfall_time": None,
            "trend": "",
            "status": "",
        }
    return indexed


def build_stations(catalog: list[dict], telemetry: dict[str, list]) -> list[dict]:
    stations = _index_catalog(catalog)

    # -- merge river entries -------------------------------------------------
    for r in telemetry.get("river", []):
        try:
            sid = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        st = stations.get(sid)
        if st is None:
            st = {
                "id": sid,
                "name": r.get("name") or f"Station {sid}",
                "nepali_name": "",
                "station_index": r.get("stationIndex") or "",
                "basin": r.get("basin") or "Other",
                "district": r.get("district") or "Unknown",
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "elevation": r.get("elevation"),
                "water_level": None, "warning_level": None, "danger_level": None,
                "rainfall": None, "water_level_time": None, "rainfall_time": None,
                "trend": "", "status": "",
            }
            stations[sid] = st

        wl = r.get("waterLevel") or {}
        st["water_level"] = _to_float(wl.get("value"))
        st["water_level_time"] = wl.get("datetime")
        st["warning_level"] = _to_float(r.get("warning_level"))
        st["danger_level"] = _to_float(r.get("danger_level"))
        st["trend"] = (r.get("steady") or "").upper()
        st["status"] = (r.get("status") or "").upper()

    # -- merge rainfall entries ---------------------------------------------
    for rf in telemetry.get("rainfall", []):
        try:
            sid = int(rf.get("id"))
        except (TypeError, ValueError):
            continue
        st = stations.get(sid)
        if st is None:
            st = {
                "id": sid,
                "name": rf.get("name") or f"Station {sid}",
                "nepali_name": "",
                "station_index": rf.get("stationIndex") or "",
                "basin": rf.get("basin") or "Other",
                "district": rf.get("district") or "Unknown",
                "latitude": rf.get("latitude"),
                "longitude": rf.get("longitude"),
                "elevation": rf.get("elevation"),
                "water_level": None, "warning_level": None, "danger_level": None,
                "rainfall": None, "water_level_time": None, "rainfall_time": None,
                "trend": "", "status": "",
            }
            stations[sid] = st

        lo = rf.get("latest_observation") or {}
        st["rainfall"] = _to_float(lo.get("value"))
        st["rainfall_time"] = lo.get("datetime")

    # -- enrich --------------------------------------------------------------
    out: list[dict] = []
    for st in stations.values():
        if "-DELETE" in st["name"] or "_delete" in st["name"]:
            continue

        wl_dt = _parse_dt(st["water_level_time"])
        rf_dt = _parse_dt(st["rainfall_time"])
        candidates = [d for d in (wl_dt, rf_dt) if d is not None]
        latest = max(candidates) if candidates else None

        delay = _delay_minutes(latest)

        diff_warning = _level_diff(st["water_level"], st["warning_level"])
        diff_danger = _level_diff(st["water_level"], st["danger_level"])

        st["water_level_diff_warning"] = diff_warning
        st["water_level_diff_danger"] = diff_danger
        st["above_warning"] = bool(diff_warning is not None and diff_warning >= 0)
        st["above_danger"] = bool(diff_danger is not None and diff_danger >= 0)
        st["delay_minutes"] = delay
        st["latest_time"] = latest.isoformat() if latest else None

        if delay is None:
            st["telemetry_state"] = "offline"
        elif delay <= 10:
            st["telemetry_state"] = "on_time"
        elif delay <= 60:
            st["telemetry_state"] = "delayed"
        elif delay <= 1440:
            st["telemetry_state"] = "critical"
        else:
            st["telemetry_state"] = "offline"

        out.append(st)

    out.sort(key=lambda s: (s["delay_minutes"] is None, -(s["delay_minutes"] or 0)))
    return out


def classify_hazards(stations: list[dict]) -> dict[str, list[dict]]:
    danger, warning = [], []
    for st in stations:
        if st["water_level"] is None:
            continue
        if st["above_danger"] or "DANGER" in st["status"]:
            danger.append(st)
        elif st["above_warning"] or ("WARNING" in st["status"] and "BELOW" not in st["status"]):
            warning.append(st)
    danger.sort(key=lambda s: (s["water_level_diff_danger"] or 0), reverse=True)
    warning.sort(key=lambda s: (s["water_level_diff_warning"] or 0), reverse=True)
    return {"danger": danger, "warning": warning}


def stats(stations: list[dict]) -> dict:
    def count(pred):
        return sum(1 for s in stations if pred(s))

    hz = classify_hazards(stations)
    return {
        "total": len(stations),
        "delayed": count(lambda s: (s["delay_minutes"] or 0) > 10),
        "critical": count(lambda s: 60 < (s["delay_minutes"] or 0) < 10**9),
        "offline": count(lambda s: s["delay_minutes"] is None or (s["delay_minutes"] or 0) > 1440),
        "on_time": count(lambda s: s["delay_minutes"] is not None and s["delay_minutes"] <= 10),
        "rising": count(lambda s: s["trend"] == "RISING"),
        "falling": count(lambda s: s["trend"] == "FALLING"),
        "steady": count(lambda s: s["trend"] == "STEADY"),
        "danger_count": len(hz["danger"]),
        "warning_count": len(hz["warning"]),
    }


# ----------------------------------------------------------------------------
# Cache + background refresh
# ----------------------------------------------------------------------------
class Cache:
    def __init__(self) -> None:
        self.stations: list[dict] = []
        self.updated_at: Optional[datetime] = None
        self._lock = asyncio.Lock()

    async def refresh(self, client: DHMClient) -> None:
        async with self._lock:
            catalog, telemetry = await asyncio.gather(
                client.catalog(), client.telemetry()
            )
            self.stations = build_stations(catalog, telemetry)
            self.updated_at = datetime.now(timezone.utc)
            log.info("cache refreshed: %d stations", len(self.stations))


cache = Cache()
client: Optional[DHMClient] = None
_refresh_task: Optional[asyncio.Task] = None


async def _refresh_loop() -> None:
    assert client is not None
    while True:
        try:
            await cache.refresh(client)
        except Exception as e:                       # keep loop alive
            log.exception("refresh loop error: %s", e)
        await asyncio.sleep(REFRESH_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global client, _refresh_task
    client = DHMClient()
    _refresh_task = asyncio.create_task(_refresh_loop())
    yield
    _refresh_task.cancel()
    try:
        await _refresh_task
    except asyncio.CancelledError:
        pass
    if client:
        await client.aclose()


# ----------------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------------
app = FastAPI(title="DHM Hydrology API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5000",
        "http://127.0.0.1:5000",
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "stations": len(cache.stations),
        "updated_at": cache.updated_at.isoformat() if cache.updated_at else None,
    }


@app.get("/api/stats")
async def api_stats():
    return {"stats": stats(cache.stations),
            "updated_at": cache.updated_at.isoformat() if cache.updated_at else None}


@app.get("/api/stations")
async def api_stations(
    state: str = Query("all", description="all|delayed|critical|offline|on_time|rising|falling|steady"),
    basin: str = Query("all"),
    district: str = Query("all"),
    q: str = Query(""),
    sort: str = Query("delay_desc",
                      description="delay_desc|delay_asc|diff_warning_desc|diff_warning_asc|"
                                  "water_desc|water_asc|name_asc|basin_asc|district_asc"),
    limit: int = Query(2000, ge=1, le=5000),
):
    rows = cache.stations

    if state == "delayed":
        rows = [s for s in rows if (s["delay_minutes"] or 0) > 10]
    elif state == "critical":
        rows = [s for s in rows if 60 < (s["delay_minutes"] or 0) < 10**9]
    elif state == "offline":
        rows = [s for s in rows if s["delay_minutes"] is None or (s["delay_minutes"] or 0) > 1440]
    elif state == "on_time":
        rows = [s for s in rows if s["delay_minutes"] is not None and s["delay_minutes"] <= 10]
    elif state in ("rising", "falling", "steady"):
        rows = [s for s in rows if s["trend"] == state.upper()]

    if basin != "all":
        rows = [s for s in rows if s["basin"].lower() == basin.lower()]
    if district != "all":
        rows = [s for s in rows if s["district"].lower() == district.lower()]
    if q:
        needle = q.lower()
        rows = [s for s in rows if needle in " ".join([
            s["name"].lower(), s["nepali_name"].lower(),
            s["station_index"].lower(), s["basin"].lower(),
            s["district"].lower(), s["trend"].lower(),
        ])]

    def key(s: dict):
        if sort == "delay_desc":
            return (s["delay_minutes"] is None, -(s["delay_minutes"] or 0))
        if sort == "delay_asc":
            return (s["delay_minutes"] is None, s["delay_minutes"] or 0)
        if sort == "diff_warning_desc":
            v = s["water_level_diff_warning"]
            return (v is None, -(v if v is not None else 0))
        if sort == "diff_warning_asc":
            v = s["water_level_diff_warning"]
            return (v is None, v if v is not None else 0)
        if sort == "water_desc":
            v = s["water_level"]
            return (v is None, -(v if v is not None else 0))
        if sort == "water_asc":
            v = s["water_level"]
            return (v is None, v if v is not None else 0)
        if sort == "name_asc":
            return s["name"].lower()
        if sort == "basin_asc":
            return (s["basin"].lower(), s["name"].lower())
        if sort == "district_asc":
            return (s["district"].lower(), s["name"].lower())
        return (s["delay_minutes"] is None, -(s["delay_minutes"] or 0))

    rows = sorted(rows, key=key)[:limit]
    return {
        "count": len(rows),
        "updated_at": cache.updated_at.isoformat() if cache.updated_at else None,
        "stations": rows,
    }


@app.get("/api/river-watch")
async def api_river_watch():
    river = [s for s in cache.stations if s["water_level"] is not None]
    return {
        "count": len(river),
        "updated_at": cache.updated_at.isoformat() if cache.updated_at else None,
        "stations": river,
    }


@app.get("/api/hazard")
async def api_hazard():
    return {
        "updated_at": cache.updated_at.isoformat() if cache.updated_at else None,
        **classify_hazards(cache.stations),
    }


@app.get("/api/compare")
async def api_compare(
    near: int = Query(5, ge=1, le=50),
    rising: int = Query(5, ge=1, le=50),
):
    near_rows = [s for s in cache.stations if s["water_level_diff_warning"] is not None]
    near_rows.sort(key=lambda s: s["water_level_diff_warning"], reverse=True)

    rising_rows = [s for s in cache.stations if s["trend"] == "RISING"]
    rising_rows.sort(
        key=lambda s: (s["water_level_diff_warning"] is None,
                       -(s["water_level_diff_warning"] or -999)),
    )

    return {
        "near_warning": near_rows[:near],
        "rising": rising_rows[:rising],
        "updated_at": cache.updated_at.isoformat() if cache.updated_at else None,
    }