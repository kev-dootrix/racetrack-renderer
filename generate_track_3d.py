from __future__ import annotations

import argparse
from datetime import datetime
import html
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".cache" / "fastf1"
CONFIG_DIR = ROOT / "track_configs"
DEFAULT_OUTPUT_ROOT = ROOT / "output"
THREE_JS_VERSION = "0.163.0"


@dataclass(frozen=True)
class LatLonPoint:
    lat: float
    lon: float


@dataclass(frozen=True)
class LocalPoint:
    x: float
    z: float


@dataclass(frozen=True)
class TrackEvent:
    season_year: int
    event_name: str
    official_event_name: str
    location: str
    country: str
    session_type: str

    @property
    def fields(self) -> list[str]:
        return [self.location, self.event_name, self.official_event_name, self.country]


@dataclass(frozen=True)
class GeometryResult:
    title: str
    source_label: str
    source_note: str
    source_urls: dict[str, str | None]
    geographic_points: list[LatLonPoint]
    local_points: list[LocalPoint]
    distances_m: list[float]
    projection_origin: LatLonPoint
    total_length_m: float
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ElevationResult:
    source_label: str
    source_note: str
    elevations_m: list[float]
    min_elevation_m: float
    max_elevation_m: float
    metadata: dict[str, Any]


@dataclass(frozen=True)
class RenderTheme:
    background: str = "#08111f"
    panel_background: str = "rgba(9, 13, 21, 0.74)"
    panel_border: str = "rgba(169, 205, 255, 0.16)"
    text: str = "#f4f7fb"
    muted_text: str = "rgba(244, 247, 251, 0.72)"
    track_fill: str = "#d7e0ee"
    track_side: str = "#5c6676"
    track_base: str = "#090d15"
    track_edge: str = "#0d1320"
    track_accent: str = "#77d1ff"
    sun_color: str = "#ffe7b7"
    fog: str = "#08111f"
    elevation_scale: float = 1.0
    track_width_m: float = 18.0
    track_depth: float = 18.0
    show_centerline: bool = True


class GeometryProvider(Protocol):
    def resolve(self, track_query: str, event: TrackEvent) -> GeometryResult: ...


class ElevationProvider(Protocol):
    def resolve(self, track_query: str, event: TrackEvent, geometry: GeometryResult) -> ElevationResult: ...


def ensure_fastf1():
    try:
        import fastf1  # type: ignore
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "fastf1"], check=True)
        import fastf1  # type: ignore
    return fastf1


def fetch_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": "TrackMaker/1.0"})
    with urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8")


def fetch_json(url: str) -> dict[str, Any]:
    return json.loads(fetch_text(url))


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "track"


def sanitize_dirname(value: str) -> str:
    value = "".join(ch for ch in value if ch not in '<>:"/\\|?*' and ord(ch) >= 32).strip()
    return value or "Track"


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def candidate_strings(*values: str) -> list[str]:
    out: list[str] = []
    seen = set()
    for value in values:
        compacted = compact(value)
        if compacted and compacted not in seen:
            out.append(compacted)
            seen.add(compacted)
    return out


def load_track_configs() -> dict[str, Any]:
    tracks: list[dict[str, Any]] = []
    if CONFIG_DIR.exists():
        for path in sorted(CONFIG_DIR.glob("*.json")):
            try:
                payload = json.loads(path.read_text())
            except Exception:
                continue
            if isinstance(payload, dict):
                tracks.append(payload)
    return {"tracks": tracks}


def find_track_config(query: str, event_fields: list[str], config: dict[str, Any]) -> dict[str, Any] | None:
    query_norm = normalize(query)
    event_norm = " ".join(normalize(value) for value in event_fields if value)
    best = None
    best_score = 0.0
    for item in config.get("tracks", []):
        terms = item.get("match_terms", []) + [item.get("id", ""), item.get("title", "")]
        score = 0.0
        for term in terms:
            term_norm = normalize(str(term))
            if not term_norm:
                continue
            if term_norm in query_norm or query_norm in term_norm:
                score = max(score, 1.0)
            if term_norm in event_norm:
                score = max(score, 0.98)
        if score > best_score:
            best_score = score
            best = item
    return best if best_score >= 0.72 else None


def dist(a: LocalPoint, b: LocalPoint) -> float:
    return math.hypot(b.x - a.x, b.z - a.z)


def cumulative_dist(points: list[LocalPoint]) -> list[float]:
    total = [0.0]
    for idx in range(1, len(points)):
        total.append(total[-1] + dist(points[idx - 1], points[idx]))
    return total


def interpolate_float(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def interpolate_point(a: LocalPoint, b: LocalPoint, t: float) -> LocalPoint:
    return LocalPoint(
        x=interpolate_float(a.x, b.x, t),
        z=interpolate_float(a.z, b.z, t),
    )


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


def project_latlon(lat: float, lon: float, lat0: float, lon0: float) -> LocalPoint:
    meters_per_deg_lat = 111132.0
    meters_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))
    return LocalPoint((lon - lon0) * meters_per_deg_lon, (lat - lat0) * meters_per_deg_lat)


def smooth_circular(values: list[float], radius: int = 5, passes: int = 1) -> list[float]:
    if not values:
        return []
    radius = max(0, int(radius))
    passes = max(1, int(passes))
    if radius == 0:
        return list(values)
    current = list(values)
    for _ in range(passes):
        out: list[float] = []
        n = len(current)
        for idx in range(n):
            segment = [current[(idx + offset) % n] for offset in range(-radius, radius + 1)]
            out.append(sum(segment) / len(segment))
        current = out
    return current


def interpolate_closed_series(distances: list[float], values: list[float], target: float, total: float) -> float:
    if not distances:
        return 0.0
    if total <= 0:
        return values[0]
    while target < 0:
        target += total
    while target > total:
        target -= total
    if target <= distances[0]:
        return values[0]
    if target >= distances[-1]:
        span = total - distances[-1]
        if span <= 0:
            return values[-1]
        wrapped_target = target - distances[-1]
        wrapped_total = span + distances[0]
        if wrapped_target <= span:
            return interpolate_float(values[-1], values[0], wrapped_target / span)
        return values[0]
    for idx in range(len(distances) - 1):
        if distances[idx] <= target <= distances[idx + 1]:
            seg = distances[idx + 1] - distances[idx]
            if seg <= 0:
                return values[idx]
            t = (target - distances[idx]) / seg
            return interpolate_float(values[idx], values[idx + 1], t)
    return values[-1]


def resample_closed_profile(distances: list[float], values: list[float], sample_count: int) -> list[float]:
    if not distances or not values or sample_count <= 0:
        return []
    if len(distances) != len(values):
        raise ValueError("Distance and value arrays must have the same length.")
    total = distances[-1]
    if total <= 0:
        return [values[0] for _ in range(sample_count)]
    samples = []
    for idx in range(sample_count):
        target = total * idx / sample_count
        samples.append(interpolate_closed_series(distances, values, target, total))
    return smooth_circular(samples, radius=max(3, min(10, sample_count // 28 or 3)), passes=2)


def segment_heading(points: list[LocalPoint], at_start: bool) -> float:
    if len(points) < 2:
        return 0.0
    a, b = (points[0], points[1]) if at_start else (points[-1], points[-2])
    return math.degrees(math.atan2(b.x - a.x, b.z - a.z))


def build_osm_raceway_loop(segments: list[tuple[int, list[LocalPoint], str, float]]) -> list[LocalPoint]:
    if not segments:
        return []

    def key_for(point: LocalPoint) -> tuple[float, float]:
        return (round(point.x, 6), round(point.z, 6))

    by_endpoint: dict[tuple[float, float], list[tuple[int, list[LocalPoint], str, float]]] = {}
    for seg in segments:
        start_key = key_for(seg[1][0])
        end_key = key_for(seg[1][-1])
        by_endpoint.setdefault(start_key, []).append(seg)
        by_endpoint.setdefault(end_key, []).append(seg)

    def priority(seg: tuple[int, list[LocalPoint], str, float]) -> tuple[int, int, float]:
        return (1 if seg[2].strip() else 0, len(seg[1]), seg[3])

    start = max(segments, key=priority)
    used = {start[0]}
    path = list(start[1])
    current_heading = segment_heading(path, at_start=False)
    current_key = key_for(path[-1])
    start_key = key_for(path[0])

    while True:
        candidates = [seg for seg in by_endpoint.get(current_key, []) if seg[0] not in used]
        if not candidates:
            break
        scored: list[tuple[float, int, tuple[int, list[LocalPoint], str, float], bool]] = []
        for seg in candidates:
            points = seg[1]
            if key_for(points[0]) == current_key:
                heading = segment_heading(points, at_start=True)
                reverse = False
            else:
                heading = segment_heading(points, at_start=False)
                reverse = True
            diff = abs(((heading - current_heading + 180.0) % 360.0) - 180.0)
            scored.append((diff, -len(points), seg, reverse))
        scored.sort(key=lambda item: (item[0], item[1]))
        _, _, chosen, reverse = scored[0]
        used.add(chosen[0])
        oriented = list(reversed(chosen[1])) if reverse else chosen[1]
        path.extend(oriented[1:])
        current_heading = segment_heading(oriented, at_start=False)
        current_key = key_for(oriented[-1])
        if current_key == start_key:
            break

    if path and path[0] != path[-1]:
        path.append(path[0])
    return path


def nominatim_search(query: str) -> dict[str, Any] | None:
    url = (
        "https://nominatim.openstreetmap.org/search?"
        + f"q={quote(query)}&format=jsonv2&limit=1&polygon_geojson=1&addressdetails=1&extratags=1"
    )
    try:
        req = Request(url, headers={"User-Agent": "TrackMaker/1.0"})
        with urlopen(req, timeout=20) as response:
            data = json.loads(response.read().decode("utf-8"))
        return data[0] if data else None
    except Exception:
        return None


def overpass_request(query: str) -> dict[str, Any] | None:
    endpoints = [
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass-api.de/api/interpreter",
        "https://lz4.overpass-api.de/api/interpreter",
    ]
    for url in endpoints:
        try:
            req = Request(url, data=query.encode("utf-8"), headers={"User-Agent": "TrackMaker/1.0"})
            with urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            continue
    return None


def best_event_match(fastf1, track_query: str, year: int | None) -> tuple[int, Any]:
    current_year = datetime.now().year
    years = [year] if year else list(range(current_year, current_year - 5, -1))
    query_norm = normalize(track_query)
    best: tuple[float, int, Any] | None = None

    for candidate_year in years:
        schedule = fastf1.get_event_schedule(candidate_year)
        for _, row in schedule.iterrows():
            if int(row["RoundNumber"]) == 0:
                continue
            fields = [
                str(row.get("EventName", "")),
                str(row.get("OfficialEventName", "")),
                str(row.get("Location", "")),
                str(row.get("Country", "")),
            ]
            field_norms = [normalize(value) for value in fields if value]
            score = 0.0
            for field_norm in field_norms:
                if not field_norm:
                    continue
                if query_norm in field_norm or field_norm in query_norm:
                    score = max(score, 0.99)
                else:
                    score = max(score, len(set(query_norm.split()) & set(field_norm.split())) / max(len(query_norm.split()), 1))
            if best is None or score > best[0]:
                best = (score, candidate_year, row)

    if best is None or best[0] < 0.35:
        raise ValueError(f"Could not find a FastF1 event matching '{track_query}'.")
    return best[1], best[2]


class OSMGeometryProvider:
    def __init__(self, track_config: dict[str, Any] | None = None):
        self.track_config = track_config or {}

    def resolve(self, track_query: str, event: TrackEvent) -> GeometryResult:
        candidates = candidate_strings(track_query, *event.fields)
        config_terms = sorted(
            {str(term).strip() for term in self.track_config.get("match_terms", []) if str(term).strip()},
            key=lambda value: (-len(value), value.lower()),
        )
        if not candidates:
            raise RuntimeError("Cannot build OSM geometry without a usable track query.")

        search_queries = [
            *config_terms,
            track_query,
            f"{track_query} circuit",
            f"{track_query} raceway",
            event.location,
            event.event_name,
            event.official_event_name,
            f"{event.location} circuit",
            f"{event.location} raceway",
        ]

        nominatim_result = None
        chosen_query = None
        for query in search_queries:
            query = query.strip()
            if not query:
                continue
            result = nominatim_search(query)
            if result:
                nominatim_result = result
                chosen_query = query
                break
        if not nominatim_result:
            raise RuntimeError(
                "OSM geometry lookup failed. Try a more specific circuit name or a track that exists in OpenStreetMap."
            )

        try:
            lat0 = float(nominatim_result["lat"])
            lon0 = float(nominatim_result["lon"])
        except Exception as exc:
            raise RuntimeError("OSM geometry lookup returned an invalid Nominatim result.") from exc

        bbox = nominatim_result.get("boundingbox") or []
        radius = 1500.0
        if len(bbox) == 4:
            south, north, west, east = map(float, bbox)
            radius = max(1200.0, haversine_m(south, west, north, east) / 2.0 + 250.0)

        overpass_query = f"""[out:json][timeout:25];
way["highway"="raceway"](around:{int(radius)},{lat0},{lon0});
out tags geom qt;"""
        raw_overpass = overpass_request(overpass_query)
        if not raw_overpass:
            raise RuntimeError(
                f"OpenStreetMap lookup found '{chosen_query}', but no raceway geometry was returned from Overpass."
            )

        segments: list[tuple[int, list[LocalPoint], str, float]] = []
        for element in raw_overpass.get("elements", []):
            if element.get("type") != "way":
                continue
            tags = element.get("tags", {})
            if tags.get("highway") != "raceway":
                continue
            name = str(tags.get("name", "")).strip()
            if "pit lane" in name.lower():
                continue
            geometry = element.get("geometry", [])
            if len(geometry) < 2:
                continue
            points = [project_latlon(float(node["lat"]), float(node["lon"]), lat0, lon0) for node in geometry]
            length_m = sum(dist(a, b) for a, b in zip(points, points[1:]))
            segments.append((int(element["id"]), points, name, length_m))

        loop_points = build_osm_raceway_loop(segments)
        if not loop_points:
            raise RuntimeError(
                f"OpenStreetMap geometry search for '{chosen_query}' returned ways, but they could not be ordered into a closed loop."
            )

        if loop_points[0] == loop_points[-1]:
            open_loop = loop_points[:-1]
        else:
            open_loop = loop_points

        geographic_points: list[LatLonPoint] = []
        # Reconstruct approximate geographic coordinates from the projected local points.
        # The projection origin is retained in metadata so future providers can swap projection schemes.
        for local in open_loop:
            lat = lat0 + local.z / 111132.0
            lon = lon0 + local.x / (111320.0 * math.cos(math.radians(lat0)))
            geographic_points.append(LatLonPoint(lat=lat, lon=lon))
        geographic_points.append(geographic_points[0])

        local_points = [*open_loop, open_loop[0]]
        distances_m = cumulative_dist(local_points)
        title = str(nominatim_result.get("name") or event.location or track_query).strip()

        return GeometryResult(
            title=title,
            source_label="OpenStreetMap raceway geometry",
            source_note="OSM highway=raceway geometry via Nominatim + Overpass",
            source_urls={"nominatim": "https://nominatim.openstreetmap.org", "overpass": "https://overpass-api.de"},
            geographic_points=geographic_points,
            local_points=local_points,
            distances_m=distances_m,
            projection_origin=LatLonPoint(lat=lat0, lon=lon0),
            total_length_m=distances_m[-1] if distances_m else 0.0,
            metadata={
                "track_query": track_query,
                "config_terms": config_terms,
                "nominatim_query": chosen_query,
                "nominatim_name": str(nominatim_result.get("name", "")),
                "nominatim_display_name": str(nominatim_result.get("display_name", "")),
                "overpass_query": overpass_query,
                "segment_count": len(segments),
                "radius_m": radius,
            },
        )


class FastF1ElevationProvider:
    def __init__(self, elevation_scale: float):
        self.elevation_scale = float(elevation_scale)

    def resolve(self, track_query: str, event: TrackEvent, geometry: GeometryResult) -> ElevationResult:
        fastf1 = ensure_fastf1()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fastf1.Cache.enable_cache(str(CACHE_DIR))

        try:
            session = fastf1.get_session(event.season_year, str(event.event_name), event.session_type)
            session.load(weather=False, messages=False)
        except Exception as exc:
            raise RuntimeError(
                f"FastF1 could not load session '{event.session_type}' for {event.event_name} {event.season_year}."
            ) from exc

        fastest = session.laps.pick_fastest()
        telemetry = fastest.get_telemetry()

        def find_column(name: str) -> str | None:
            for column in telemetry.columns:
                if str(column).lower() == name.lower():
                    return str(column)
            return None

        distance_col = find_column("Distance")
        z_col = find_column("Z")
        if not distance_col or not z_col:
            raise RuntimeError(
                "FastF1 telemetry did not expose usable Distance and Z columns for the selected session."
            )

        cleaned: list[tuple[float, float]] = []
        for row in telemetry.itertuples(index=False):
            try:
                distance = float(getattr(row, distance_col))
                z_value = float(getattr(row, z_col)) / 10.0
            except Exception:
                continue
            if math.isnan(distance) or math.isnan(z_value):
                continue
            cleaned.append((distance, z_value))

        if len(cleaned) < 3:
            raise RuntimeError(
                "FastF1 telemetry did not expose enough position samples to build an elevation profile."
            )

        cleaned.sort(key=lambda item: item[0])
        deduped: list[tuple[float, float]] = [cleaned[0]]
        for distance, z_value in cleaned[1:]:
            if abs(distance - deduped[-1][0]) < 1e-6:
                deduped[-1] = (distance, z_value)
            else:
                deduped.append((distance, z_value))

        start_distance = deduped[0][0]
        adjusted = [(distance - start_distance, z_value) for distance, z_value in deduped]
        total_distance = adjusted[-1][0]
        if total_distance <= 0:
            raise RuntimeError("FastF1 telemetry distance trace was not usable.")

        distances = [distance for distance, _ in adjusted]
        elevations = [z_value for _, z_value in adjusted]
        if distances[0] != 0.0:
            distances = [distance - distances[0] for distance in distances]
            total_distance = distances[-1]

        elevations = smooth_circular(elevations, radius=max(2, min(7, len(elevations) // 80 or 2)), passes=1)

        sample_count = max(len(geometry.local_points) - 1, 3)
        sampled = resample_closed_profile(distances, elevations, sample_count)
        if not sampled:
            raise RuntimeError("FastF1 telemetry could not be resampled into an elevation profile.")

        min_elevation = min(sampled)
        shifted = [value - min_elevation for value in sampled]
        max_elevation = max(shifted)

        return ElevationResult(
            source_label="FastF1 telemetry elevation",
            source_note="FastF1 telemetry Z values aligned by normalized lap distance",
            elevations_m=shifted,
            min_elevation_m=0.0,
            max_elevation_m=max_elevation,
            metadata={
                "track_query": track_query,
                "session_type": event.session_type,
                "event_name": event.event_name,
                "season_year": event.season_year,
                "fastest_lap_driver": str(fastest["Driver"]),
                "fastest_lap_time": str(fastest["LapTime"]),
                "raw_sample_count": len(cleaned),
                "resampled_count": len(shifted),
                "distance_trace_m": total_distance,
                "elevation_scale": self.elevation_scale,
            },
        )


def resolve_event(track_query: str, year: int | None, session_type: str) -> TrackEvent:
    fastf1 = ensure_fastf1()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(CACHE_DIR))
    season_year, event = best_event_match(fastf1, track_query, year)
    return TrackEvent(
        season_year=season_year,
        event_name=str(event.get("EventName")),
        official_event_name=str(event.get("OfficialEventName")),
        location=str(event.get("Location")),
        country=str(event.get("Country")),
        session_type=session_type,
    )


def build_html_document(
    geometry: GeometryResult,
    elevation: ElevationResult,
    event: TrackEvent,
    theme: RenderTheme,
) -> str:
    data = {
        "title": geometry.title,
        "event": {
            "season_year": event.season_year,
            "event_name": event.event_name,
            "official_event_name": event.official_event_name,
            "location": event.location,
            "country": event.country,
            "session_type": event.session_type,
        },
        "geometry": {
            "source_label": geometry.source_label,
            "source_note": geometry.source_note,
            "projection_origin": {
                "lat": geometry.projection_origin.lat,
                "lon": geometry.projection_origin.lon,
            },
            "points": [
                {
                    "lat": point.lat,
                    "lon": point.lon,
                    "x": local.x,
                    "z": local.z,
                    "distance_m": distance,
                }
                for point, local, distance in zip(geometry.geographic_points, geometry.local_points, geometry.distances_m)
            ],
            "total_length_m": geometry.total_length_m,
            "metadata": geometry.metadata,
        },
        "elevation": {
            "source_label": elevation.source_label,
            "source_note": elevation.source_note,
            "values_m": elevation.elevations_m,
            "min_m": elevation.min_elevation_m,
            "max_m": elevation.max_elevation_m,
            "metadata": elevation.metadata,
        },
        "theme": {
            "background": theme.background,
            "panelBackground": theme.panel_background,
            "panelBorder": theme.panel_border,
            "text": theme.text,
            "mutedText": theme.muted_text,
            "trackFill": theme.track_fill,
            "trackSide": theme.track_side,
            "trackBase": theme.track_base,
            "trackEdge": theme.track_edge,
            "trackAccent": theme.track_accent,
            "sunColor": theme.sun_color,
            "fog": theme.fog,
            "elevationScale": theme.elevation_scale,
            "trackWidthM": theme.track_width_m,
            "trackDepth": theme.track_depth,
            "showCenterline": theme.show_centerline,
        },
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    title_escaped = html.escape(geometry.title)
    info_line = html.escape(f"{geometry.source_label} • {elevation.source_label}")
    event_line = html.escape(f"{event.location} • {event.session_type} {event.season_year}")
    track_length_km = geometry.total_length_m / 1000.0
    elevation_span_m = elevation.max_elevation_m - elevation.min_elevation_m

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title_escaped} 3D Track</title>
  <style>
    :root {{
      color-scheme: dark;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    html, body {{
      margin: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: {theme.background};
    }}
    body {{
      background:
        radial-gradient(circle at 20% 15%, rgba(86, 126, 179, 0.18), transparent 34%),
        radial-gradient(circle at 80% 10%, rgba(226, 191, 123, 0.12), transparent 26%),
        linear-gradient(180deg, rgba(10, 16, 28, 0.3), rgba(6, 10, 18, 0.78));
    }}
    #app {{
      position: relative;
      width: 100vw;
      height: 100vh;
    }}
    canvas {{
      display: block;
      width: 100%;
      height: 100%;
    }}
    .hud {{
      position: absolute;
      top: 24px;
      left: 24px;
      max-width: min(520px, calc(100vw - 48px));
      padding: 18px 20px 16px;
      border: 1px solid {theme.panel_border};
      border-radius: 18px;
      background: {theme.panel_background};
      backdrop-filter: blur(18px);
      -webkit-backdrop-filter: blur(18px);
      box-shadow: 0 18px 52px rgba(0, 0, 0, 0.28);
    }}
    .eyebrow {{
      margin: 0 0 8px;
      color: {theme.muted_text};
      font-size: 12px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
    }}
    h1 {{
      margin: 0;
      color: {theme.text};
      font-size: clamp(28px, 4vw, 48px);
      line-height: 0.98;
      letter-spacing: -0.04em;
    }}
    .meta {{
      margin: 12px 0 0;
      color: {theme.muted_text};
      font-size: 14px;
      line-height: 1.5;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 14px;
    }}
    .stat {{
      padding-top: 10px;
      border-top: 1px solid rgba(255, 255, 255, 0.08);
    }}
    .stat-label {{
      display: block;
      color: {theme.muted_text};
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      margin-bottom: 4px;
    }}
    .stat-value {{
      color: {theme.text};
      font-size: 14px;
      line-height: 1.35;
    }}
    .hint {{
      position: absolute;
      right: 24px;
      bottom: 24px;
      color: {theme.muted_text};
      font-size: 12px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid rgba(255, 255, 255, 0.1);
      background: rgba(5, 8, 14, 0.42);
    }}
    @media (max-width: 720px) {{
      .hud {{
        top: 16px;
        left: 16px;
        right: 16px;
        max-width: none;
      }}
      .stats {{
        grid-template-columns: 1fr;
      }}
      .hint {{
        right: 16px;
        bottom: 16px;
      }}
    }}
  </style>
</head>
<body>
  <div id="app">
    <canvas id="scene"></canvas>
    <div class="hud">
      <p class="eyebrow">TrackMaker 3D ribbon</p>
      <h1>{title_escaped}</h1>
      <p class="meta">{event_line}<br />{info_line}</p>
      <div class="stats">
        <div class="stat">
          <span class="stat-label">Track length</span>
          <span class="stat-value">{track_length_km:.2f} km</span>
        </div>
        <div class="stat">
          <span class="stat-label">Elevation span</span>
          <span class="stat-value">{elevation_span_m:.1f} m</span>
        </div>
        <div class="stat">
          <span class="stat-label">Geometry source</span>
          <span class="stat-value">{html.escape(geometry.source_label)}</span>
        </div>
        <div class="stat">
          <span class="stat-label">Elevation source</span>
          <span class="stat-value">{html.escape(elevation.source_label)}</span>
        </div>
      </div>
    </div>
    <div class="hint">Drag to orbit • scroll to zoom • shift-drag to pan</div>
  </div>
  <script>
    window.__TRACK_DATA__ = {payload};
  </script>
  <script type="importmap">
    {{
      "imports": {{
        "three": "https://cdn.jsdelivr.net/npm/three@{THREE_JS_VERSION}/build/three.module.js",
        "three/addons/": "https://cdn.jsdelivr.net/npm/three@{THREE_JS_VERSION}/examples/jsm/"
      }}
    }}
  </script>
  <script type="module">
    import * as THREE from "three";
    import {{ OrbitControls }} from "three/addons/controls/OrbitControls.js";

    const data = window.__TRACK_DATA__;
    const theme = data.theme;
    const rawPoints = data.geometry.points;
    const centerline = rawPoints.slice(0, -1).map((point, index) => {{
      const elevation = data.elevation.values_m[index] ?? 0;
      return {{
        x: point.x,
        y: elevation * theme.elevationScale,
        z: point.z,
        distance: point.distance_m,
      }};
    }});

    const canvas = document.getElementById("scene");
    const renderer = new THREE.WebGLRenderer({{ canvas, antialias: true, alpha: false }});
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.18;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(theme.background);
    scene.fog = new THREE.Fog(theme.fog, 3000, 11000);

    const camera = new THREE.PerspectiveCamera(42, window.innerWidth / window.innerHeight, 0.1, 50000);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.06;
    controls.rotateSpeed = 0.55;
    controls.zoomSpeed = 0.75;
    controls.panSpeed = 0.6;
    controls.maxPolarAngle = Math.PI * 0.495;
    controls.minDistance = 300;
    controls.maxDistance = 22000;

    const ambient = new THREE.AmbientLight(0xffffff, 1.9);
    scene.add(ambient);

    const hemi = new THREE.HemisphereLight(0xbcd7ff, 0x10131a, 1.1);
    scene.add(hemi);

    const sun = new THREE.DirectionalLight(new THREE.Color(theme.sunColor), 3.2);
    sun.position.set(2200, 3600, 1800);
    scene.add(sun);

    const backLight = new THREE.DirectionalLight(0x9bb8ff, 0.9);
    backLight.position.set(-1800, 1200, -2400);
    scene.add(backLight);

    function buildSurfaceGeometry(points, widthMeters, yOverride = null) {{
      const count = points.length;
      if (count < 3) {{
        throw new Error("Not enough centerline points to build a track surface.");
      }}
      const halfWidth = widthMeters / 2;
      const positions = new Float32Array(count * 2 * 3);
      const indices = [];

      for (let i = 0; i < count; i++) {{
        const prev = points[(i - 1 + count) % count];
        const curr = points[i];
        const next = points[(i + 1) % count];
        const y = yOverride === null ? curr.y : yOverride;

        const tangentX = next.x - prev.x;
        const tangentZ = next.z - prev.z;
        const tangentLength = Math.hypot(tangentX, tangentZ) || 1;
        const ux = tangentX / tangentLength;
        const uz = tangentZ / tangentLength;
        const normalX = -uz;
        const normalZ = ux;
        const leftX = curr.x + normalX * halfWidth;
        const leftZ = curr.z + normalZ * halfWidth;
        const rightX = curr.x - normalX * halfWidth;
        const rightZ = curr.z - normalZ * halfWidth;

        positions.set([leftX, y, leftZ], i * 6);
        positions.set([rightX, y, rightZ], i * 6 + 3);
      }}

      for (let i = 0; i < count; i++) {{
        const next = (i + 1) % count;
        const leftCurrent = i * 2;
        const rightCurrent = i * 2 + 1;
        const leftNext = next * 2;
        const rightNext = next * 2 + 1;
        indices.push(leftCurrent, rightCurrent, rightNext);
        indices.push(leftCurrent, rightNext, leftNext);
      }}

      const geometry = new THREE.BufferGeometry();
      geometry.setIndex(indices);
      geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      geometry.computeVertexNormals();
      return geometry;
    }}

    function buildSideWallGeometry(points, widthMeters, baseY, side) {{
      const count = points.length;
      const halfWidth = widthMeters / 2;
      const positions = new Float32Array(count * 4 * 3);
      const indices = [];

      for (let i = 0; i < count; i++) {{
        const next = (i + 1) % count;
        const curr = points[i];
        const nextPoint = points[next];
        const prev = points[(i - 1 + count) % count];
        const nextNext = points[(i + 2) % count];

        const currTangentX = nextPoint.x - prev.x;
        const currTangentZ = nextPoint.z - prev.z;
        const currTangentLength = Math.hypot(currTangentX, currTangentZ) || 1;
        const currNormalX = -currTangentZ / currTangentLength;
        const currNormalZ = currTangentX / currTangentLength;

        const nextTangentX = nextNext.x - curr.x;
        const nextTangentZ = nextNext.z - curr.z;
        const nextTangentLength = Math.hypot(nextTangentX, nextTangentZ) || 1;
        const nextNormalX = -nextTangentZ / nextTangentLength;
        const nextNormalZ = nextTangentX / nextTangentLength;

        const currTopX = side === "left" ? curr.x + currNormalX * halfWidth : curr.x - currNormalX * halfWidth;
        const currTopZ = side === "left" ? curr.z + currNormalZ * halfWidth : curr.z - currNormalZ * halfWidth;
        const nextTopX = side === "left" ? nextPoint.x + nextNormalX * halfWidth : nextPoint.x - nextNormalX * halfWidth;
        const nextTopZ = side === "left" ? nextPoint.z + nextNormalZ * halfWidth : nextPoint.z - nextNormalZ * halfWidth;

        const baseIndex = i * 12;
        positions.set([
          currTopX, curr.y, currTopZ,
          nextTopX, nextPoint.y, nextTopZ,
          nextTopX, baseY, nextTopZ,
          currTopX, baseY, currTopZ,
        ], baseIndex);
        indices.push(i * 4, i * 4 + 1, i * 4 + 2);
        indices.push(i * 4, i * 4 + 2, i * 4 + 3);
      }}

      const geometry = new THREE.BufferGeometry();
      geometry.setIndex(indices);
      geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      geometry.computeVertexNormals();
      return geometry;
    }}

    function buildCenterline(points, offsetY = 0.15) {{
      const geometry = new THREE.BufferGeometry();
      const positions = [];
      for (const point of points) {{
        positions.push(point.x, point.y + offsetY, point.z);
      }}
      positions.push(points[0].x, points[0].y + offsetY, points[0].z);
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
      return geometry;
    }}

    const minTopY = Math.min(...centerline.map((point) => point.y));
    const baseY = minTopY - theme.trackDepth;

    const trackGroup = new THREE.Group();

    const topGeometry = buildSurfaceGeometry(centerline, theme.trackWidthM, null);
    const topMaterial = new THREE.MeshStandardMaterial({{
      color: theme.trackFill,
      roughness: 0.62,
      metalness: 0.08,
      emissive: new THREE.Color(theme.trackAccent),
      emissiveIntensity: 0.12,
      side: THREE.DoubleSide,
    }});
    const topMesh = new THREE.Mesh(topGeometry, topMaterial);
    trackGroup.add(topMesh);

    const bottomGeometry = buildSurfaceGeometry(centerline, theme.trackWidthM, baseY);
    const bottomMaterial = new THREE.MeshStandardMaterial({{
      color: theme.trackBase,
      roughness: 0.98,
      metalness: 0.0,
      emissive: new THREE.Color(theme.trackBase),
      emissiveIntensity: 0.02,
      side: THREE.DoubleSide,
    }});
    const bottomMesh = new THREE.Mesh(bottomGeometry, bottomMaterial);
    trackGroup.add(bottomMesh);

    const leftWallGeometry = buildSideWallGeometry(centerline, theme.trackWidthM, baseY, "left");
    const rightWallGeometry = buildSideWallGeometry(centerline, theme.trackWidthM, baseY, "right");
    const wallMaterial = new THREE.MeshStandardMaterial({{
      color: theme.trackSide,
      roughness: 0.88,
      metalness: 0.01,
      side: THREE.DoubleSide,
    }});
    trackGroup.add(new THREE.Mesh(leftWallGeometry, wallMaterial));
    trackGroup.add(new THREE.Mesh(rightWallGeometry, wallMaterial));

    let centerlineLine = null;
    if (theme.showCenterline) {{
      const lineGeometry = buildCenterline(centerline);
      const lineMaterial = new THREE.LineBasicMaterial({{
        color: theme.trackAccent,
        transparent: true,
        opacity: 1.0,
      }});
      centerlineLine = new THREE.Line(lineGeometry, lineMaterial);
      trackGroup.add(centerlineLine);
    }}

    const bounds = {{
      minX: Math.min(...centerline.map((point) => point.x)),
      maxX: Math.max(...centerline.map((point) => point.x)),
      minY: Math.min(...centerline.map((point) => point.y), baseY),
      maxY: Math.max(...centerline.map((point) => point.y)),
      minZ: Math.min(...centerline.map((point) => point.z)),
      maxZ: Math.max(...centerline.map((point) => point.z)),
    }};
    const centerX = (bounds.minX + bounds.maxX) / 2;
    const centerY = (bounds.minY + bounds.maxY) / 2;
    const centerZ = (bounds.minZ + bounds.maxZ) / 2;
    trackGroup.position.set(-centerX, -centerY, -centerZ);
    scene.add(trackGroup);

    const spanX = bounds.maxX - bounds.minX;
    const spanY = bounds.maxY - bounds.minY;
    const spanZ = bounds.maxZ - bounds.minZ;
    const radius = Math.max(spanX + theme.trackWidthM, spanY * 1.4, spanZ + theme.trackWidthM);
    camera.position.set(radius * 1.15, radius * 0.58 + spanY * 0.18, radius * 1.18);
    controls.target.set(0, 0, 0);
    controls.update();

    function animate() {{
      requestAnimationFrame(animate);
      controls.update();
      renderer.render(scene, camera);
    }}
    animate();

    function resize() {{
      const width = window.innerWidth;
      const height = window.innerHeight;
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height);
    }}
    window.addEventListener("resize", resize, {{ passive: true }});
  </script>
</body>
</html>
"""


def load_geometry_from_html(html_path: Path) -> GeometryResult | None:
    if not html_path.exists():
        return None
    try:
        text = html_path.read_text()
        marker = "window.__TRACK_DATA__ = "
        start = text.index(marker) + len(marker)
        end = text.index(";</script>", start)
        payload = json.loads(text[start:end])
        return geometry_payload_to_result(payload["geometry"], default_title=html_path.stem.replace("-3d", ""))
    except Exception:
        return None


def geometry_result_to_payload(geometry: GeometryResult) -> dict[str, Any]:
    return {
        "title": geometry.title,
        "source_label": geometry.source_label,
        "source_note": geometry.source_note,
        "source_urls": geometry.source_urls,
        "projection_origin": {
            "lat": geometry.projection_origin.lat,
            "lon": geometry.projection_origin.lon,
        },
        "points": [
            {
                "lat": point.lat,
                "lon": point.lon,
                "x": local.x,
                "z": local.z,
                "distance_m": distance,
            }
            for point, local, distance in zip(geometry.geographic_points, geometry.local_points, geometry.distances_m)
        ],
        "total_length_m": geometry.total_length_m,
        "metadata": geometry.metadata,
    }


def geometry_payload_to_result(payload: dict[str, Any], default_title: str = "Track") -> GeometryResult:
    projection_origin = payload["projection_origin"]
    points = payload["points"]
    source_urls = payload.get("source_urls") or {}
    return GeometryResult(
        title=str(payload.get("title") or default_title).strip(),
        source_label=str(payload.get("source_label") or "OpenStreetMap raceway geometry"),
        source_note=str(payload.get("source_note") or "Cached geometry from output folder"),
        source_urls={str(key): value for key, value in dict(source_urls).items()},
        geographic_points=[
            LatLonPoint(lat=float(point["lat"]), lon=float(point["lon"])) for point in points
        ],
        local_points=[
            LocalPoint(x=float(point["x"]), z=float(point["z"])) for point in points
        ],
        distances_m=[float(point["distance_m"]) for point in points],
        projection_origin=LatLonPoint(lat=float(projection_origin["lat"]), lon=float(projection_origin["lon"])),
        total_length_m=float(payload["total_length_m"]),
        metadata=dict(payload.get("metadata") or {}),
    )


def load_geometry_from_cache(cache_path: Path) -> GeometryResult | None:
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text())
        return geometry_payload_to_result(payload, default_title=cache_path.stem.split("_osm_", 1)[0])
    except Exception:
        return None


def save_geometry_cache(cache_path: Path, geometry: GeometryResult) -> None:
    cache_path.write_text(json.dumps(geometry_result_to_payload(geometry), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a 3D HTML track ribbon from OSM geometry and FastF1 elevation.")
    parser.add_argument("track", help="Track or event name, e.g. Imola, Monza, Suzuka")
    parser.add_argument("--year", type=int, help="Season year to use")
    parser.add_argument("--session", default="Q", help="FastF1 session code to use, defaults to Q")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Root output directory")
    parser.add_argument("--track-width-m", type=float, default=18.0, help="Track width in meters")
    parser.add_argument("--elevation-scale", type=float, default=1.0, help="Elevation exaggeration multiplier")
    parser.add_argument("--track-depth", type=float, default=18.0, help="Solid track depth in scene units")
    args = parser.parse_args()

    event = resolve_event(args.track, args.year, args.session)
    config = load_track_configs()
    track_config = find_track_config(args.track, event.fields, config) or {}
    title = str(track_config.get("title") or event.location or args.track).strip() or "Track"
    output_root = Path(args.output_root).resolve()
    track_root = output_root / sanitize_dirname(title)
    cache_html = track_root / f"{slugify(title)}-3d.html"
    geometry_cache = track_root / f"{slugify(title)}_osm_raceway.json"
    track_root.mkdir(parents=True, exist_ok=True)
    geometry_provider = OSMGeometryProvider(track_config)
    geometry = load_geometry_from_cache(geometry_cache)
    if geometry is not None:
        print(f"Using cached geometry from {geometry_cache}", file=sys.stderr)
    else:
        try:
            geometry = geometry_provider.resolve(args.track, event)
        except RuntimeError:
            cached_geometry = load_geometry_from_html(cache_html)
            if cached_geometry is None:
                raise
            print(f"Using cached 3D geometry from {cache_html}", file=sys.stderr)
            geometry = cached_geometry
        save_geometry_cache(geometry_cache, geometry)
    elevation_provider = FastF1ElevationProvider(args.elevation_scale)
    elevation = elevation_provider.resolve(args.track, event, geometry)

    theme = RenderTheme(
        track_width_m=args.track_width_m,
        elevation_scale=args.elevation_scale,
        track_depth=args.track_depth,
    )
    html_path = track_root / f"{slugify(title)}-3d.html"
    html_path.write_text(build_html_document(geometry, elevation, event, theme))
    print(str(html_path))


if __name__ == "__main__":
    main()
