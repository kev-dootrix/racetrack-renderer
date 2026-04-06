from __future__ import annotations

import argparse
import csv
from datetime import datetime
import io
import html
import hashlib
import json
import math
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from functools import lru_cache
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
    def resolve(
        self,
        track_query: str,
        event: TrackEvent | None,
        geometry: GeometryResult,
        track_root: Path,
    ) -> ElevationResult: ...


def ensure_fastf1():
    try:
        import fastf1  # type: ignore
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "--user", "fastf1"], check=True)
        import fastf1  # type: ignore
    return fastf1


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


def geometry_signature(geometry: GeometryResult) -> str:
    payload = {
        "projection_origin": {
            "lat": round(geometry.projection_origin.lat, 7),
            "lon": round(geometry.projection_origin.lon, 7),
        },
        "points": [
            {
                "lat": round(point.lat, 7),
                "lon": round(point.lon, 7),
                "x": round(local.x, 4),
                "z": round(local.z, 4),
            }
            for point, local in zip(geometry.geographic_points, geometry.local_points)
        ],
        "total_length_m": round(geometry.total_length_m, 3),
    }
    return hashlib.sha1(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def target_track_length_m(track_config: dict[str, Any]) -> float | None:
    for key in ("track_length_m", "expected_length_m", "lap_length_m"):
        value = track_config.get(key)
        try:
            if value is not None:
                parsed = float(value)
                if parsed > 0:
                    return parsed
        except Exception:
            continue
    return None


def scale_local_points_to_length(points: list[LocalPoint], target_length_m: float | None) -> list[LocalPoint]:
    if not points or target_length_m is None or target_length_m <= 0:
        return list(points)
    raw_length = closed_loop_length(points)
    if raw_length <= 0:
        return list(points)
    scale = target_length_m / raw_length
    return [LocalPoint(x=point.x * scale, z=point.z * scale) for point in points]


def geometry_length_scale_factor(points: list[LocalPoint], target_length_m: float | None) -> float | None:
    if not points or target_length_m is None or target_length_m <= 0:
        return None
    raw_length = closed_loop_length(points)
    if raw_length <= 0:
        return None
    return target_length_m / raw_length


def closed_loop_length(points: list[LocalPoint]) -> float:
    if len(points) < 2:
        return 0.0
    closed_points = list(points)
    if closed_points[0] != closed_points[-1]:
        closed_points.append(closed_points[0])
    return cumulative_dist(closed_points)[-1]


def interpolate_closed_local_point(
    points: list[LocalPoint],
    distances: list[float],
    target: float,
) -> LocalPoint:
    if not points or not distances:
        return LocalPoint(0.0, 0.0)
    if len(points) != len(distances):
        raise ValueError("Point and distance arrays must have the same length.")

    total = distances[-1]
    if total <= 0:
        return points[0]

    while target < 0:
        target += total
    while target > total:
        target -= total

    if target <= distances[0]:
        return points[0]

    for idx in range(len(distances) - 1):
        start_distance = distances[idx]
        end_distance = distances[idx + 1]
        if start_distance <= target <= end_distance:
            span = end_distance - start_distance
            if span <= 0:
                return points[idx]
            t = (target - start_distance) / span
            return LocalPoint(
                x=interpolate_float(points[idx].x, points[idx + 1].x, t),
                z=interpolate_float(points[idx].z, points[idx + 1].z, t),
            )
    return points[-1]


def fill_missing_circular(values: list[float | None]) -> list[float]:
    if not values:
        return []

    known_indices = [index for index, value in enumerate(values) if value is not None]
    if not known_indices:
        raise RuntimeError("DEM lookup returned no usable elevation samples.")
    if len(known_indices) == 1:
        return [float(values[known_indices[0]])] * len(values)

    filled = [float(value) if value is not None else 0.0 for value in values]
    circular_indices = known_indices + [known_indices[0] + len(values)]

    for start_index, end_index in zip(circular_indices, circular_indices[1:]):
        start_value = filled[start_index % len(values)]
        end_value = filled[end_index % len(values)]
        gap = end_index - start_index
        for offset in range(1, gap):
            position = (start_index + offset) % len(values)
            filled[position] = interpolate_float(start_value, end_value, offset / gap)

    return filled


def candidate_strings(*values: str) -> list[str]:
    out: list[str] = []
    seen = set()
    for value in values:
        compacted = compact(value)
        if compacted and compacted not in seen:
            out.append(compacted)
            seen.add(compacted)
    return out


def match_compact_name(candidates: list[str], available: dict[str, str]) -> str | None:
    for candidate in candidates:
        if candidate in available:
            return available[candidate]
    for candidate in candidates:
        for key, value in available.items():
            if candidate and (candidate in key or key in candidate):
                return value
    return None


@lru_cache(maxsize=8)
def github_contents(repo: str, path: str = "") -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{repo}/contents"
    if path:
        url += f"/{quote(path)}"
    req = Request(url, headers={"User-Agent": "TrackMaker/1.0", "Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict):
        return [payload]
    return list(payload)


def fetch_csv_rows(url: str, delimiter: str = ",") -> list[list[str]]:
    req = Request(url, headers={"User-Agent": "TrackMaker/1.0"})
    with urlopen(req, timeout=20) as response:
        text = response.read().decode("utf-8")
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if row]


def load_centerline_points(url: str) -> list[LocalPoint]:
    points: list[LocalPoint] = []
    for row in fetch_csv_rows(url, delimiter=","):
        if not row or row[0].startswith("#") or len(row) < 2:
            continue
        try:
            points.append(LocalPoint(x=float(row[0]), z=float(row[1])))
        except ValueError:
            continue
    return points


def load_raceline_points(url: str) -> tuple[list[LocalPoint], list[float], list[float], list[float]]:
    points: list[LocalPoint] = []
    s_values: list[float] = []
    headings: list[float] = []
    curvatures: list[float] = []
    for row in fetch_csv_rows(url, delimiter=";"):
        if not row or row[0].startswith("#"):
            continue
        if len(row) < 5:
            continue
        try:
            s, x, y, psi, kappa, *_rest = map(float, row)
        except ValueError:
            continue
        s_values.append(s)
        points.append(LocalPoint(x=x, z=y))
        headings.append(psi)
        curvatures.append(kappa)
    if points and points[0] != points[-1]:
        points.append(points[0])
        s_values.append(s_values[-1] + dist(points[-2], points[-1]))
        headings.append(headings[0])
        curvatures.append(curvatures[0])
    return points, s_values, headings, curvatures


def resolve_geometry_anchor(title: str, event_fields: list[str], track_config: dict[str, Any]) -> LatLonPoint | None:
    config_terms = sorted(
        {str(term).strip() for term in track_config.get("match_terms", []) if str(term).strip()},
        key=lambda value: (-len(value), value.lower()),
    )
    search_queries = [
        *config_terms,
        title,
        *event_fields,
        f"{title} circuit",
        f"{title} raceway",
        f"{title} motorsport circuit",
    ]
    for query in search_queries:
        query = query.strip()
        if not query:
            continue
        result = nominatim_search(query)
        if not result:
            continue
        try:
            return LatLonPoint(lat=float(result["lat"]), lon=float(result["lon"]))
        except Exception:
            continue
    return None


def elevation_cache_path(track_root: Path, title: str) -> Path:
    return track_root / f"{slugify(title)}_elevation_profile.json"


def load_elevation_cache(cache_path: Path, expected_signature: str, expected_count: int) -> ElevationResult | None:
    if not cache_path.exists():
        return None
    try:
        payload = json.loads(cache_path.read_text())
        if payload.get("geometry_signature") != expected_signature:
            return None
        if int(payload.get("sample_count", -1)) != expected_count:
            return None
        return ElevationResult(
            source_label=str(payload.get("source_label") or "OpenTopoData DEM profile"),
            source_note=str(payload.get("source_note") or "OpenTopoData DEM samples cached locally"),
            elevations_m=[float(value) for value in payload.get("elevations_m", [])],
            min_elevation_m=float(payload.get("min_elevation_m", 0.0)),
            max_elevation_m=float(payload.get("max_elevation_m", 0.0)),
            metadata=dict(payload.get("metadata") or {}),
        )
    except Exception:
        return None


def save_elevation_cache(
    cache_path: Path,
    geometry_signature_value: str,
    elevation: ElevationResult,
    sample_count: int,
) -> None:
    payload = {
        "geometry_signature": geometry_signature_value,
        "sample_count": sample_count,
        "source_label": elevation.source_label,
        "source_note": elevation.source_note,
        "elevations_m": elevation.elevations_m,
        "min_elevation_m": elevation.min_elevation_m,
        "max_elevation_m": elevation.max_elevation_m,
        "metadata": elevation.metadata,
    }
    cache_path.write_text(json.dumps(payload, indent=2))


def geometry_result_from_local_points(
    title: str,
    points: list[LocalPoint],
    source_label: str,
    source_note: str,
    source_urls: dict[str, str | None],
    metadata: dict[str, Any],
    projection_origin: LatLonPoint | None = None,
) -> GeometryResult:
    if not points:
        raise ValueError("Cannot build geometry from an empty point list.")
    local_points = list(points)
    if local_points[0] != local_points[-1]:
        local_points.append(local_points[0])
    projection_origin = projection_origin or LatLonPoint(lat=0.0, lon=0.0)
    meters_per_deg_lon = 111320.0 * math.cos(math.radians(projection_origin.lat))
    meters_per_deg_lon = meters_per_deg_lon if abs(meters_per_deg_lon) > 1e-9 else 1e-9
    geographic_points = [
        LatLonPoint(
            lat=projection_origin.lat + point.z / 111132.0,
            lon=projection_origin.lon + point.x / meters_per_deg_lon,
        )
        for point in local_points
    ]
    distances_m = cumulative_dist(local_points)
    return GeometryResult(
        title=title,
        source_label=source_label,
        source_note=source_note,
        source_urls=source_urls,
        geographic_points=geographic_points,
        local_points=local_points,
        distances_m=distances_m,
        projection_origin=projection_origin,
        total_length_m=distances_m[-1] if distances_m else 0.0,
        metadata=metadata,
    )


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


def rotate_series(values: list[float], offset: int) -> list[float]:
    if not values:
        return []
    offset %= len(values)
    if offset == 0:
        return list(values)
    return values[offset:] + values[:offset]


def center_points(points: list[LocalPoint]) -> list[LocalPoint]:
    if not points:
        return []
    mean_x = sum(point.x for point in points) / len(points)
    mean_z = sum(point.z for point in points) / len(points)
    return [LocalPoint(x=point.x - mean_x, z=point.z - mean_z) for point in points]


def best_circular_alignment_metrics(
    reference_points: list[LocalPoint],
    candidate_points: list[LocalPoint],
) -> tuple[int, float]:
    if not reference_points or not candidate_points or len(reference_points) != len(candidate_points):
        return 0, float("inf")

    ref = center_points(reference_points)
    cand = center_points(candidate_points)
    n = len(ref)
    best_shift = 0
    best_error = float("inf")

    for shift in range(n):
        shifted = cand[shift:] + cand[:shift]

        sum_a = 0.0
        sum_b = 0.0
        sum_c = 0.0
        for ref_point, cand_point in zip(ref, shifted):
            sum_a += cand_point.x * ref_point.x + cand_point.z * ref_point.z
            sum_b += cand_point.x * ref_point.z - cand_point.z * ref_point.x
            sum_c += cand_point.x * cand_point.x + cand_point.z * cand_point.z

        if sum_c <= 0:
            continue

        magnitude = math.hypot(sum_a, sum_b)
        if magnitude <= 0:
            continue

        scale = magnitude / sum_c
        cos_theta = sum_a / magnitude
        sin_theta = sum_b / magnitude

        error = 0.0
        for ref_point, cand_point in zip(ref, shifted):
            rotated_x = scale * (cand_point.x * cos_theta - cand_point.z * sin_theta)
            rotated_z = scale * (cand_point.x * sin_theta + cand_point.z * cos_theta)
            dx = rotated_x - ref_point.x
            dz = rotated_z - ref_point.z
            error += dx * dx + dz * dz

        if error < best_error:
            best_error = error
            best_shift = shift

    if best_error == float("inf"):
        return 0, float("inf")
    return best_shift, math.sqrt(best_error / n)


def best_circular_alignment_shift(reference_points: list[LocalPoint], candidate_points: list[LocalPoint]) -> int:
    shift, _ = best_circular_alignment_metrics(reference_points, candidate_points)
    return shift


def segment_heading(points: list[LocalPoint], at_start: bool) -> float:
    if len(points) < 2:
        return 0.0
    a, b = (points[0], points[1]) if at_start else (points[-1], points[-2])
    return math.degrees(math.atan2(b.x - a.x, b.z - a.z))


def segment_name_bias(
    name: str,
    preferred_terms: list[str] | None = None,
    avoid_terms: list[str] | None = None,
) -> float:
    normalized_name = normalize(name)
    if not normalized_name:
        return 0.0

    bias = 0.0
    preferred_terms = [normalize(term) for term in (preferred_terms or []) if normalize(term)]
    avoid_terms = [normalize(term) for term in (avoid_terms or []) if normalize(term)]

    if any(term in normalized_name for term in avoid_terms):
        bias += 500.0
    if any(term in normalized_name for term in preferred_terms):
        bias -= 120.0
    return bias


def path_quality(points: list[LocalPoint]) -> tuple[float, float]:
    if len(points) < 2:
        return 0.0, 0.0

    working_points = points[:-1] if len(points) > 2 and points[0] == points[-1] else points
    steps = [dist(a, b) for a, b in zip(working_points, working_points[1:])]
    if not steps:
        return 0.0, 0.0

    max_step = max(steps)
    discontinuity_penalty = sum(max(0.0, step - 90.0) for step in steps)
    return max_step, discontinuity_penalty


def build_osm_raceway_loop(
    segments: list[tuple[int, list[LocalPoint], str, float]],
    preferred_terms: list[str] | None = None,
    avoid_terms: list[str] | None = None,
    shape_reference: list[LocalPoint] | None = None,
) -> list[LocalPoint]:
    if not segments:
        return []

    def key_for(point: LocalPoint) -> tuple[float, float]:
        return (round(point.x, 6), round(point.z, 6))

    def priority(seg: tuple[int, list[LocalPoint], str, float]) -> tuple[int, int, float]:
        return (
            1 if seg[2].strip() else 0,
            int(segment_name_bias(seg[2], preferred_terms, avoid_terms)),
            len(seg[1]),
            seg[3],
        )

    by_endpoint: dict[tuple[float, float], list[tuple[int, list[LocalPoint], str, float]]] = {}
    for seg in segments:
        start_key = key_for(seg[1][0])
        end_key = key_for(seg[1][-1])
        by_endpoint.setdefault(start_key, []).append(seg)
        by_endpoint.setdefault(end_key, []).append(seg)

    def trace_from_start(start: tuple[int, list[LocalPoint], str, float]) -> list[LocalPoint]:
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
                scored.append((segment_name_bias(seg[2], preferred_terms, avoid_terms), diff, -len(points), seg, reverse))
            scored.sort(key=lambda item: (item[0], item[1], item[2]))
            _, _, _, chosen, reverse = scored[0]
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

    best_path: list[LocalPoint] = []
    best_length = 0.0
    best_score = float("-inf")
    for start in sorted(segments, key=priority, reverse=True):
        candidate_path = trace_from_start(start)
        if len(candidate_path) < 4:
            continue
        if candidate_path[0] != candidate_path[-1]:
            continue
        candidate_length = sum(dist(a, b) for a, b in zip(candidate_path, candidate_path[1:]))
        max_step, discontinuity_penalty = path_quality(candidate_path)
        if max_step > 240.0:
            continue
        candidate_score = candidate_length - discontinuity_penalty * 6.0 - max(0.0, max_step - 120.0) * 10.0
        if shape_reference and len(shape_reference) >= 3:
            candidate_count = len(shape_reference)
            candidate_distances = cumulative_dist(candidate_path)
            if candidate_distances[-1] > 0:
                candidate_x = [point.x for point in candidate_path[:-1]]
                candidate_z = [point.z for point in candidate_path[:-1]]
                sampled_x = resample_closed_profile(candidate_distances[:-1], candidate_x, candidate_count)
                sampled_z = resample_closed_profile(candidate_distances[:-1], candidate_z, candidate_count)
                if len(sampled_x) == candidate_count and len(sampled_z) == candidate_count:
                    candidate_reference = [LocalPoint(x=x, z=z) for x, z in zip(sampled_x, sampled_z)]
                    _, shape_rmse = best_circular_alignment_metrics(shape_reference, candidate_reference)
                    if math.isfinite(shape_rmse):
                        candidate_score -= shape_rmse * 25.0
        if candidate_score > best_score or (candidate_score == best_score and candidate_length > best_length):
            best_score = candidate_score
            best_length = candidate_length
            best_path = candidate_path

    if best_path:
        return best_path

    fallback = trace_from_start(max(segments, key=priority))
    if fallback and fallback[0] != fallback[-1]:
        fallback.append(fallback[0])
    return fallback


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


def fetch_opentopodata_elevations(
    locations: list[LatLonPoint],
    datasets: tuple[str, ...],
    batch_size: int = 100,
) -> list[float | None]:
    if not locations:
        return []

    dataset_path = ",".join(datasets)
    results: list[float | None] = []
    for batch_index, start in enumerate(range(0, len(locations), batch_size)):
        batch = locations[start : start + batch_size]
        if batch_index:
            time.sleep(1.1)

        location_arg = "|".join(f"{point.lat:.6f},{point.lon:.6f}" for point in batch)
        url = f"https://api.opentopodata.org/v1/{dataset_path}?locations={quote(location_arg, safe='|,.-')}"
        req = Request(url, headers={"User-Agent": "TrackMaker/1.0"})
        with urlopen(req, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))

        status = str(payload.get("status", "")).upper()
        if status not in {"OK", "PARTIAL"}:
            raise RuntimeError(f"OpenTopoData returned status '{status or 'UNKNOWN'}'.")

        batch_results = payload.get("results", [])
        if len(batch_results) != len(batch):
            raise RuntimeError("OpenTopoData returned an unexpected number of elevation samples.")

        for item in batch_results:
            elevation = item.get("elevation")
            results.append(None if elevation is None else float(elevation))

    return results


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


def resolve_tumftm_geometry(
    title: str,
    event_fields: list[str],
    track_config: dict[str, Any],
    event: TrackEvent,
) -> GeometryResult | None:
    explicit_centerline = str(track_config.get("centerline_url", "")).strip()
    explicit_raceline = str(track_config.get("raceline_url", "")).strip()
    lap_length_m = fastf1_lap_distance_m(event)

    if explicit_centerline or explicit_raceline:
        source_label = "configured geometry"
        source_note = "track config override"
        geometry_source = str(track_config.get("geometry_source", "auto")).lower()
        if geometry_source == "track_database":
            source_label = "TUMFTM track centerline"
            source_note = "track_database config override"
        elif geometry_source == "f1tenth_racetrack":
            source_label = "F1TENTH track centerline"
            source_note = "f1tenth config override"

        if explicit_centerline:
            points = load_centerline_points(explicit_centerline)
            if not points:
                return None
            raw_length = cumulative_dist([*points, points[0]])[-1] if points[0] != points[-1] else cumulative_dist(points)[-1]
            scale = lap_length_m / raw_length if lap_length_m and raw_length > 0 else 1.0
            points = [LocalPoint(x=point.x * scale, z=point.z * scale) for point in points]
            return geometry_result_from_local_points(
                title=title,
                points=points,
                source_label=source_label,
                source_note=source_note,
                source_urls={"centerline": explicit_centerline, "raceline": None},
                metadata={
                    "track_query": title,
                    "config_terms": track_config.get("match_terms", []),
                    "geometry_source": geometry_source if geometry_source != "auto" else "track_database",
                    "source_repo": None,
                },
            )

        points, _, _, _ = load_raceline_points(explicit_raceline)
        if not points:
            return None
        raw_length = cumulative_dist([*points, points[0]])[-1] if points[0] != points[-1] else cumulative_dist(points)[-1]
        scale = lap_length_m / raw_length if lap_length_m and raw_length > 0 else 1.0
        points = [LocalPoint(x=point.x * scale, z=point.z * scale) for point in points]
        return geometry_result_from_local_points(
            title=title,
            points=points,
            source_label=source_label,
            source_note=source_note,
            source_urls={"centerline": None, "raceline": explicit_raceline},
            metadata={
                "track_query": title,
                "config_terms": track_config.get("match_terms", []),
                "geometry_source": geometry_source if geometry_source != "auto" else "f1tenth_racetrack",
                "source_repo": None,
            },
        )

    candidates = candidate_strings(title, *event_fields)
    if not candidates:
        return None

    try:
        tumftm_tracks = github_contents("TUMFTM/racetrack-database", "tracks")
    except Exception:
        tumftm_tracks = []

    tumftm_available = {
        compact(item.get("name", "")): item.get("download_url", "")
        for item in tumftm_tracks
        if item.get("type") == "file" and str(item.get("name", "")).lower().endswith(".csv")
    }
    tumftm_url = match_compact_name(candidates, tumftm_available)
    if tumftm_url:
        points = load_centerline_points(tumftm_url)
        if points:
            raw_length = cumulative_dist([*points, points[0]])[-1] if points[0] != points[-1] else cumulative_dist(points)[-1]
            scale = lap_length_m / raw_length if lap_length_m and raw_length > 0 else 1.0
            points = [LocalPoint(x=point.x * scale, z=point.z * scale) for point in points]
            return geometry_result_from_local_points(
                title=title,
                points=points,
                source_label="TUMFTM track centerline",
                source_note="TUMFTM centerline database",
                source_urls={"centerline": tumftm_url, "raceline": None},
                metadata={
                    "track_query": title,
                    "config_terms": track_config.get("match_terms", []),
                    "geometry_source": "track_database",
                    "source_repo": "TUMFTM/racetrack-database",
                },
            )

    try:
        f1tenth_roots = github_contents("f1tenth/f1tenth_racetracks")
    except Exception:
        f1tenth_roots = []

    directory_map = {
        compact(item.get("name", "")): item.get("path", item.get("name", ""))
        for item in f1tenth_roots
        if item.get("type") == "dir"
    }
    match_path = match_compact_name(candidates, directory_map)
    if match_path:
        try:
            directory_items = github_contents("f1tenth/f1tenth_racetracks", match_path)
        except Exception:
            directory_items = []
        centerline_url = None
        raceline_url = None
        for item in directory_items:
            if item.get("type") != "file":
                continue
            name = str(item.get("name", ""))
            if name.endswith("_centerline.csv"):
                centerline_url = item.get("download_url", centerline_url)
            elif name.endswith("_raceline.csv"):
                raceline_url = item.get("download_url", raceline_url)
        if centerline_url or raceline_url:
            if centerline_url:
                points = load_centerline_points(centerline_url)
                source_label = "F1TENTH track centerline"
                source_note = "F1TENTH track database"
                source_urls = {"centerline": centerline_url, "raceline": raceline_url}
            else:
                points, _, _, _ = load_raceline_points(raceline_url or "")
                source_label = "F1TENTH track centerline"
                source_note = "F1TENTH track database"
                source_urls = {"centerline": centerline_url, "raceline": raceline_url}
            if points:
                raw_length = cumulative_dist([*points, points[0]])[-1] if points[0] != points[-1] else cumulative_dist(points)[-1]
                scale = lap_length_m / raw_length if lap_length_m and raw_length > 0 else 1.0
                points = [LocalPoint(x=point.x * scale, z=point.z * scale) for point in points]
                return geometry_result_from_local_points(
                    title=title,
                    points=points,
                    source_label=source_label,
                    source_note=source_note,
                    source_urls=source_urls,
                    metadata={
                        "track_query": title,
                        "config_terms": track_config.get("match_terms", []),
                        "geometry_source": "f1tenth_racetrack",
                        "source_repo": "f1tenth/f1tenth_racetracks",
                    },
                )

    return None


def load_fastf1_shape_hint(event: TrackEvent, sample_count: int = 256) -> list[LocalPoint]:
    fastf1 = ensure_fastf1()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(CACHE_DIR))

    try:
        session = fastf1.get_session(event.season_year, str(event.event_name), event.session_type)
        session.load(weather=False, messages=False)
    except Exception:
        return []

    fastest = session.laps.pick_fastest()
    telemetry = fastest.get_telemetry()

    def find_column(name: str) -> str | None:
        for column in telemetry.columns:
            if str(column).lower() == name.lower():
                return str(column)
        return None

    distance_col = find_column("Distance")
    x_col = find_column("X")
    y_col = find_column("Y")
    if not distance_col or not x_col or not y_col:
        return []

    cleaned_xy: list[tuple[float, float, float]] = []
    for row in telemetry.itertuples(index=False):
        try:
            distance = float(getattr(row, distance_col))
            x_value = float(getattr(row, x_col))
            y_value = float(getattr(row, y_col))
        except Exception:
            continue
        if math.isnan(distance) or math.isnan(x_value) or math.isnan(y_value):
            continue
        cleaned_xy.append((distance, x_value, y_value))

    if len(cleaned_xy) < 3:
        return []

    cleaned_xy.sort(key=lambda item: item[0])
    deduped_xy: list[tuple[float, float, float]] = [cleaned_xy[0]]
    for distance, x_value, y_value in cleaned_xy[1:]:
        if abs(distance - deduped_xy[-1][0]) < 1e-6:
            deduped_xy[-1] = (distance, x_value, y_value)
        else:
            deduped_xy.append((distance, x_value, y_value))

    start_distance = deduped_xy[0][0]
    adjusted = [(distance - start_distance, x_value, y_value) for distance, x_value, y_value in deduped_xy]
    total_distance = adjusted[-1][0]
    if total_distance <= 0:
        return []

    distances = [distance for distance, _, _ in adjusted]
    xs = [x_value for _, x_value, _ in adjusted]
    ys = [y_value for _, _, y_value in adjusted]
    xs = smooth_circular(xs, radius=max(2, min(7, len(xs) // 80 or 2)), passes=1)
    ys = smooth_circular(ys, radius=max(2, min(7, len(ys) // 80 or 2)), passes=1)

    sampled_x = resample_closed_profile(distances, xs, sample_count)
    sampled_y = resample_closed_profile(distances, ys, sample_count)
    if len(sampled_x) != sample_count or len(sampled_y) != sample_count:
        return []
    return [LocalPoint(x=x_value, z=y_value) for x_value, y_value in zip(sampled_x, sampled_y)]


def fastf1_lap_distance_m(event: TrackEvent) -> float | None:
    fastf1 = ensure_fastf1()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(CACHE_DIR))

    try:
        session = fastf1.get_session(event.season_year, str(event.event_name), event.session_type)
        session.load(weather=False, messages=False)
    except Exception:
        return None

    fastest = session.laps.pick_fastest()
    telemetry = fastest.get_telemetry()

    distance_col = None
    for column in telemetry.columns:
        if str(column).lower() == "distance":
            distance_col = str(column)
            break
    if not distance_col:
        return None

    distance_values: list[float] = []
    for row in telemetry.itertuples(index=False):
        try:
            value = float(getattr(row, distance_col))
        except Exception:
            continue
        if math.isnan(value):
            continue
        distance_values.append(value)

    if not distance_values:
        return None
    return max(distance_values)


class OSMGeometryProvider:
    def __init__(self, track_config: dict[str, Any] | None = None):
        self.track_config = track_config or {}

    def resolve(
        self,
        track_query: str,
        event_fields: list[str],
        shape_reference: list[LocalPoint] | None = None,
    ) -> GeometryResult:
        candidates = candidate_strings(track_query, *event_fields)
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
            *event_fields,
            *[f"{field} circuit" for field in event_fields],
            *[f"{field} raceway" for field in event_fields],
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

        preferred_route_terms = [
            str(term).strip()
            for term in self.track_config.get("preferred_route_terms", [])
            if str(term).strip()
        ]
        avoid_route_terms = [
            str(term).strip()
            for term in self.track_config.get("avoid_route_terms", [])
            if str(term).strip()
        ]

        radius_candidates = [
            max(900.0, radius * 0.7),
            max(900.0, radius * 0.8),
            max(900.0, radius * 0.9),
            radius,
        ]

        raw_overpass = None
        overpass_query = ""
        radius_used = radius
        for candidate_radius in radius_candidates:
            overpass_query = f"""[out:json][timeout:25];
way["highway"="raceway"](around:{int(candidate_radius)},{lat0},{lon0});
out tags geom qt;"""
            raw_overpass = overpass_request(overpass_query)
            if raw_overpass and raw_overpass.get("elements"):
                radius_used = candidate_radius
                break
        if not raw_overpass or not raw_overpass.get("elements"):
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

        loop_points = build_osm_raceway_loop(segments, preferred_route_terms, avoid_route_terms, shape_reference)
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
        title_source = event_fields[0] if event_fields else track_query
        title = str(nominatim_result.get("name") or title_source or track_query).strip()

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
                "radius_m": radius_used,
            },
        )


class FastF1ElevationProvider:
    def __init__(self, elevation_scale: float):
        self.elevation_scale = float(elevation_scale)

    def resolve(
        self,
        track_query: str,
        event: TrackEvent | None,
        geometry: GeometryResult,
        track_root: Path,
    ) -> ElevationResult:
        if event is None:
            raise RuntimeError("FastF1 elevation requires a FastF1 event.")
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
        x_col = find_column("X")
        y_col = find_column("Y")
        z_col = find_column("Z")
        if not distance_col or not z_col:
            raise RuntimeError(
                "FastF1 telemetry did not expose usable Distance and Z columns for the selected session."
            )

        cleaned: list[tuple[float, float]] = []
        cleaned_xy: list[tuple[float, float, float]] = []
        for row in telemetry.itertuples(index=False):
            try:
                distance = float(getattr(row, distance_col))
                z_value = float(getattr(row, z_col)) / 10.0
                x_value = float(getattr(row, x_col)) if x_col else 0.0
                y_value = float(getattr(row, y_col)) if y_col else 0.0
            except Exception:
                continue
            if math.isnan(distance) or math.isnan(z_value):
                continue
            cleaned.append((distance, z_value))
            if x_col and y_col and not (math.isnan(x_value) or math.isnan(y_value)):
                cleaned_xy.append((distance, x_value, y_value))

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
        alignment_shift = 0
        if len(cleaned_xy) >= 3:
            xy_pairs = sorted(cleaned_xy, key=lambda item: item[0])
            xy_deduped: list[tuple[float, float, float]] = [xy_pairs[0]]
            for distance, x_value, y_value in xy_pairs[1:]:
                if abs(distance - xy_deduped[-1][0]) < 1e-6:
                    xy_deduped[-1] = (distance, x_value, y_value)
                else:
                    xy_deduped.append((distance, x_value, y_value))
            xy_distances = [distance - xy_deduped[0][0] for distance, _, _ in xy_deduped]
            xy_xs = [x_value for _, x_value, _ in xy_deduped]
            xy_ys = [y_value for _, _, y_value in xy_deduped]
            xy_x_profile = resample_closed_profile(xy_distances, xy_xs, sample_count)
            xy_y_profile = resample_closed_profile(xy_distances, xy_ys, sample_count)
            if len(xy_x_profile) == sample_count and len(xy_y_profile) == sample_count:
                candidate_points = [LocalPoint(x=x, z=y) for x, y in zip(xy_x_profile, xy_y_profile)]
                alignment_shift = best_circular_alignment_shift(geometry.local_points[:-1], candidate_points)

        sampled = resample_closed_profile(distances, elevations, sample_count)
        if not sampled:
            raise RuntimeError("FastF1 telemetry could not be resampled into an elevation profile.")
        if alignment_shift:
            sampled = rotate_series(sampled, alignment_shift)

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
                "alignment_shift": alignment_shift,
                "distance_trace_m": total_distance,
                "elevation_scale": self.elevation_scale,
            },
        )


class OpenTopoDataElevationProvider:
    def __init__(self, elevation_scale: float, datasets: tuple[str, ...] = ("eudem25m", "srtm30m", "aster30m")):
        self.elevation_scale = float(elevation_scale)
        self.datasets = datasets

    def resolve(
        self,
        track_query: str,
        event: TrackEvent | None,
        geometry: GeometryResult,
        track_root: Path,
    ) -> ElevationResult:
        cache_path = elevation_cache_path(track_root, geometry.title)
        expected_signature = geometry_signature(geometry)
        expected_count = max(len(geometry.local_points) - 1, 1)
        cached = load_elevation_cache(cache_path, expected_signature, expected_count)
        if cached is not None:
            return cached

        locations = geometry.geographic_points[:-1] if len(geometry.geographic_points) > 1 else geometry.geographic_points
        raw_samples = fetch_opentopodata_elevations(locations, self.datasets)
        if len(raw_samples) != expected_count:
            raise RuntimeError("OpenTopoData returned an unexpected number of samples for the track geometry.")

        smoothed = fill_missing_circular(raw_samples)
        smoothed = smooth_circular(smoothed, radius=max(2, min(7, len(smoothed) // 80 or 2)), passes=1)
        min_elevation = min(smoothed)
        shifted = [value - min_elevation for value in smoothed]
        max_elevation = max(shifted)

        result = ElevationResult(
            source_label="OpenTopoData DEM profile",
            source_note=f"OpenTopoData samples from {', '.join(self.datasets)} cached in the track folder",
            elevations_m=shifted,
            min_elevation_m=0.0,
            max_elevation_m=max_elevation,
            metadata={
                "track_query": track_query,
                "geometry_title": geometry.title,
                "provider": "OpenTopoData",
                "datasets": list(self.datasets),
                "request_url": f"https://api.opentopodata.org/v1/{','.join(self.datasets)}",
                "sample_count": len(shifted),
                "geometry_signature": expected_signature,
                "track_root": str(track_root),
                "elevation_scale": self.elevation_scale,
                "cache_file": cache_path.name,
                "event": {
                    "season_year": getattr(event, "season_year", None),
                    "event_name": getattr(event, "event_name", None),
                    "official_event_name": getattr(event, "official_event_name", None),
                    "location": getattr(event, "location", None),
                    "country": getattr(event, "country", None),
                    "session_type": getattr(event, "session_type", None),
                },
            },
        )
        save_elevation_cache(cache_path, expected_signature, result, len(shifted))
        return result


def resolve_event(track_query: str, year: int | None, session_type: str) -> TrackEvent | None:
    fastf1 = ensure_fastf1()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(CACHE_DIR))
    try:
        season_year, event = best_event_match(fastf1, track_query, year)
    except ValueError:
        return None
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
    event: TrackEvent | None,
    theme: RenderTheme,
    display_title: str,
) -> str:
    data = {
        "title": display_title,
        "resolved_title": geometry.title,
        "event": None
        if event is None
        else {
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
    title_escaped = html.escape(display_title)
    info_line = html.escape(f"{geometry.source_label} • {elevation.source_label}")
    if event is None:
        event_line = html.escape("Geometry-only • OpenTopoData DEM")
    else:
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
    function toScenePoint(point, elevation) {{
      return {{
        x: point.x,
        y: elevation * theme.elevationScale,
        z: -point.z,
        distance: point.distance_m,
      }};
    }}

    const centerline = rawPoints.slice(0, -1).map((point, index) => {{
      const elevation = data.elevation.values_m[index] ?? 0;
      return toScenePoint(point, elevation);
    }});

    const canvas = document.getElementById("scene");
    const renderer = new THREE.WebGLRenderer({{ canvas, antialias: true, alpha: false, logarithmicDepthBuffer: true }});
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(window.innerWidth, window.innerHeight);
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.18;

    const scene = new THREE.Scene();
    scene.background = new THREE.Color(theme.background);
    scene.fog = new THREE.Fog(theme.fog, 3000, 11000);

    const camera = new THREE.PerspectiveCamera(42, window.innerWidth / window.innerHeight, 5, 50000);

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

    function normalize2D(x, z) {{
      const length = Math.hypot(x, z) || 1;
      return {{ x: x / length, z: z / length }};
    }}

    function intersectOffsetLines(prevPoint, prevDir, nextPoint, nextDir) {{
      const denom = prevDir.x * nextDir.z - prevDir.z * nextDir.x;
      if (Math.abs(denom) < 1e-6) {{
        return null;
      }}
      const deltaX = nextPoint.x - prevPoint.x;
      const deltaZ = nextPoint.z - prevPoint.z;
      const t = (deltaX * nextDir.z - deltaZ * nextDir.x) / denom;
      return {{
        x: prevPoint.x + prevDir.x * t,
        z: prevPoint.z + prevDir.z * t,
      }};
    }}

    function buildTrackBoundaries(points, widthMeters) {{
      const count = points.length;
      const halfWidth = widthMeters / 2;
      const left = new Array(count);
      const right = new Array(count);
      const maxMiterLength = halfWidth * 2.5;

      for (let i = 0; i < count; i++) {{
        const prev = points[(i - 1 + count) % count];
        const curr = points[i];
        const next = points[(i + 1) % count];

        const prevDir = normalize2D(curr.x - prev.x, curr.z - prev.z);
        const nextDir = normalize2D(next.x - curr.x, next.z - curr.z);

        for (const side of ["left", "right"]) {{
          const prevNormal = side === "left"
            ? {{ x: -prevDir.z, z: prevDir.x }}
            : {{ x: prevDir.z, z: -prevDir.x }};
          const nextNormal = side === "left"
            ? {{ x: -nextDir.z, z: nextDir.x }}
            : {{ x: nextDir.z, z: -nextDir.x }};
          const prevPoint = {{
            x: curr.x + prevNormal.x * halfWidth,
            z: curr.z + prevNormal.z * halfWidth,
          }};
          const nextPoint = {{
            x: curr.x + nextNormal.x * halfWidth,
            z: curr.z + nextNormal.z * halfWidth,
          }};

          let join = intersectOffsetLines(prevPoint, prevDir, nextPoint, nextDir);
          if (!join) {{
            join = nextPoint;
          }}

          const miterX = join.x - curr.x;
          const miterZ = join.z - curr.z;
          const miterLength = Math.hypot(miterX, miterZ);
          if (miterLength > maxMiterLength) {{
            const scale = maxMiterLength / (miterLength || 1);
            join = {{
              x: curr.x + miterX * scale,
              z: curr.z + miterZ * scale,
            }};
          }}

          if (side === "left") {{
            left[i] = join;
          }} else {{
            right[i] = join;
          }}
        }}
      }}

      return {{ left, right }};
    }}

    function buildSurfaceGeometry(points, boundaries, yOverride = null) {{
      const count = points.length;
      if (count < 3) {{
        throw new Error("Not enough centerline points to build a track surface.");
      }}
      const positions = new Float32Array(count * 2 * 3);
      const indices = [];

      for (let i = 0; i < count; i++) {{
        const curr = points[i];
        const y = yOverride === null ? curr.y : yOverride;
        const leftPoint = boundaries.left[i];
        const rightPoint = boundaries.right[i];

        positions.set([leftPoint.x, y, leftPoint.z], i * 6);
        positions.set([rightPoint.x, y, rightPoint.z], i * 6 + 3);
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

    function buildSideWallGeometry(points, boundaries, baseY, side) {{
      const count = points.length;
      const positions = new Float32Array(count * 4 * 3);
      const indices = [];

      for (let i = 0; i < count; i++) {{
        const next = (i + 1) % count;
        const curr = points[i];
        const nextPoint = points[next];
        const currTop = side === "left" ? boundaries.left[i] : boundaries.right[i];
        const nextTop = side === "left" ? boundaries.left[next] : boundaries.right[next];

        const baseIndex = i * 12;
        positions.set([
          currTop.x, curr.y, currTop.z,
          nextTop.x, nextPoint.y, nextTop.z,
          nextTop.x, baseY, nextTop.z,
          currTop.x, baseY, currTop.z,
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
    const boundaries = buildTrackBoundaries(centerline, theme.trackWidthM);

    const trackGroup = new THREE.Group();

    const topGeometry = buildSurfaceGeometry(centerline, boundaries, null);
    const topMaterial = new THREE.MeshStandardMaterial({{
      color: theme.trackFill,
      roughness: 0.62,
      metalness: 0.08,
      emissive: new THREE.Color(theme.trackAccent),
      emissiveIntensity: 0.12,
      side: THREE.DoubleSide,
    }});
    topMaterial.polygonOffset = true;
    topMaterial.polygonOffsetFactor = -1;
    topMaterial.polygonOffsetUnits = -1;
    const topMesh = new THREE.Mesh(topGeometry, topMaterial);
    trackGroup.add(topMesh);

    const bottomGeometry = buildSurfaceGeometry(centerline, boundaries, baseY);
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

    const leftWallGeometry = buildSideWallGeometry(centerline, boundaries, baseY, "left");
    const rightWallGeometry = buildSideWallGeometry(centerline, boundaries, baseY, "right");
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
        default_title = cache_path.stem
        for suffix in ("_geometry", "_osm_raceway"):
            if default_title.endswith(suffix):
                default_title = default_title[: -len(suffix)]
                break
        return geometry_payload_to_result(payload, default_title=default_title)
    except Exception:
        return None


def save_geometry_cache(cache_path: Path, geometry: GeometryResult) -> None:
    cache_path.write_text(json.dumps(geometry_result_to_payload(geometry), indent=2))


def resolve_geometry(track_query: str, event: TrackEvent, track_config: dict[str, Any]) -> GeometryResult:
    geometry = resolve_tumftm_geometry(track_query, event.fields, track_config, event)
    if geometry is not None:
        return geometry

    geometry_provider = OSMGeometryProvider(track_config)
    return geometry_provider.resolve(track_query, event.fields, load_fastf1_shape_hint(event))


def resolve_trusted_geometry_geometry_only(
    track_query: str,
    event_fields: list[str],
    track_config: dict[str, Any],
    projection_origin: LatLonPoint | None,
) -> GeometryResult | None:
    target_length_m = target_track_length_m(track_config)

    explicit_centerline = str(track_config.get("centerline_url", "")).strip()
    explicit_raceline = str(track_config.get("raceline_url", "")).strip()

    if explicit_centerline or explicit_raceline:
        source_label = "configured geometry"
        source_note = "track config override"
        geometry_source = str(track_config.get("geometry_source", "auto")).lower()
        if geometry_source == "track_database":
            source_label = "TUMFTM track centerline"
            source_note = "track_database config override"
        elif geometry_source == "f1tenth_racetrack":
            source_label = "F1TENTH track centerline"
            source_note = "f1tenth config override"

        if explicit_centerline:
            raw_points = load_centerline_points(explicit_centerline)
            if not raw_points:
                return None
            scale_factor = geometry_length_scale_factor(raw_points, target_length_m)
            points = scale_local_points_to_length(raw_points, target_length_m)
            return geometry_result_from_local_points(
                title=track_query,
                points=points,
                source_label=source_label,
                source_note=source_note,
                source_urls={"centerline": explicit_centerline, "raceline": None},
                metadata={
                    "track_query": track_query,
                    "config_terms": track_config.get("match_terms", []),
                    "geometry_source": geometry_source if geometry_source != "auto" else "track_database",
                    "source_repo": None,
                    "target_track_length_m": target_length_m,
                    "geometry_length_scale_factor": scale_factor,
                },
                projection_origin=projection_origin,
            )

        raw_points, _, _, _ = load_raceline_points(explicit_raceline)
        if not raw_points:
            return None
        scale_factor = geometry_length_scale_factor(raw_points, target_length_m)
        points = scale_local_points_to_length(raw_points, target_length_m)
        return geometry_result_from_local_points(
            title=track_query,
            points=points,
            source_label=source_label,
            source_note=source_note,
            source_urls={"centerline": None, "raceline": explicit_raceline},
            metadata={
                "track_query": track_query,
                "config_terms": track_config.get("match_terms", []),
                "geometry_source": geometry_source if geometry_source != "auto" else "f1tenth_racetrack",
                "source_repo": None,
                "target_track_length_m": target_length_m,
                "geometry_length_scale_factor": scale_factor,
            },
            projection_origin=projection_origin,
        )

    candidates = candidate_strings(track_query, *event_fields)
    if not candidates:
        return None

    try:
        tumftm_tracks = github_contents("TUMFTM/racetrack-database", "tracks")
    except Exception:
        tumftm_tracks = []

    tumftm_available = {
        compact(item.get("name", "")): item.get("download_url", "")
        for item in tumftm_tracks
        if item.get("type") == "file" and str(item.get("name", "")).lower().endswith(".csv")
    }
    tumftm_url = match_compact_name(candidates, tumftm_available)
    if tumftm_url:
        raw_points = load_centerline_points(tumftm_url)
        if raw_points:
            scale_factor = geometry_length_scale_factor(raw_points, target_length_m)
            points = scale_local_points_to_length(raw_points, target_length_m)
            return geometry_result_from_local_points(
                title=track_query,
                points=points,
                source_label="TUMFTM track centerline",
                source_note="TUMFTM centerline database",
                source_urls={"centerline": tumftm_url, "raceline": None},
                metadata={
                    "track_query": track_query,
                    "config_terms": track_config.get("match_terms", []),
                    "geometry_source": "track_database",
                    "source_repo": "TUMFTM/racetrack-database",
                    "target_track_length_m": target_length_m,
                    "geometry_length_scale_factor": scale_factor,
                },
                projection_origin=projection_origin,
            )

    try:
        f1tenth_roots = github_contents("f1tenth/f1tenth_racetracks")
    except Exception:
        f1tenth_roots = []

    directory_map = {
        compact(item.get("name", "")): item.get("path", item.get("name", ""))
        for item in f1tenth_roots
        if item.get("type") == "dir"
    }
    match_path = match_compact_name(candidates, directory_map)
    if match_path:
        try:
            directory_items = github_contents("f1tenth/f1tenth_racetracks", match_path)
        except Exception:
            directory_items = []
        centerline_url = None
        raceline_url = None
        for item in directory_items:
            if item.get("type") != "file":
                continue
            name = str(item.get("name", ""))
            if name.endswith("_centerline.csv"):
                centerline_url = item.get("download_url", centerline_url)
            elif name.endswith("_raceline.csv"):
                raceline_url = item.get("download_url", raceline_url)
        if centerline_url or raceline_url:
            if centerline_url:
                raw_points = load_centerline_points(centerline_url)
                source_label = "F1TENTH track centerline"
                source_note = "F1TENTH track database"
                source_urls = {"centerline": centerline_url, "raceline": raceline_url}
            else:
                raw_points, _, _, _ = load_raceline_points(raceline_url or "")
                source_label = "F1TENTH track centerline"
                source_note = "F1TENTH track database"
                source_urls = {"centerline": centerline_url, "raceline": raceline_url}
            if raw_points:
                scale_factor = geometry_length_scale_factor(raw_points, target_length_m)
                points = scale_local_points_to_length(raw_points, target_length_m)
                return geometry_result_from_local_points(
                    title=track_query,
                    points=points,
                    source_label=source_label,
                    source_note=source_note,
                    source_urls=source_urls,
                    metadata={
                        "track_query": track_query,
                        "config_terms": track_config.get("match_terms", []),
                        "geometry_source": "f1tenth_racetrack",
                        "source_repo": "f1tenth/f1tenth_racetracks",
                        "target_track_length_m": target_length_m,
                        "geometry_length_scale_factor": scale_factor,
                    },
                    projection_origin=projection_origin,
                )

    return None


def resolve_geometry_geometry_only(track_query: str, track_config: dict[str, Any], title: str) -> GeometryResult:
    event_fields = [track_query, title, *track_config.get("match_terms", [])]
    projection_origin = resolve_geometry_anchor(track_query, [str(field) for field in event_fields if str(field).strip()], track_config)
    trusted_geometry = resolve_trusted_geometry_geometry_only(
        track_query=track_query,
        event_fields=[str(field) for field in event_fields if str(field).strip()],
        track_config=track_config,
        projection_origin=projection_origin,
    )
    if trusted_geometry is not None:
        return trusted_geometry

    geometry_provider = OSMGeometryProvider(track_config)
    return geometry_provider.resolve(track_query, [str(field) for field in event_fields if str(field).strip()], None)


def render_track_3d(
    track: str,
    year: int | None,
    session: str,
    output_root_value: str,
    track_width_m: float,
    elevation_scale: float,
    track_depth: float,
) -> Path:
    event = resolve_event(track, year, session)
    config = load_track_configs()
    track_fields = event.fields if event is not None else [track]
    track_config = find_track_config(track, track_fields, config) or {}
    title = str(track_config.get("title") or (event.location if event else track) or track).strip() or "Track"
    output_root = Path(output_root_value).resolve()
    track_root = output_root / sanitize_dirname(title)
    cache_html = track_root / f"{slugify(title)}-3d.html"
    geometry_cache = track_root / f"{slugify(title)}_geometry_v3.json"
    track_root.mkdir(parents=True, exist_ok=True)
    geometry = load_geometry_from_cache(geometry_cache)
    if geometry is not None:
        print(f"Using cached geometry from {geometry_cache}", file=sys.stderr)
    else:
        try:
            if event is not None:
                geometry = resolve_geometry(track, event, track_config)
            else:
                geometry = resolve_geometry_geometry_only(track, track_config, title)
        except RuntimeError:
            cached_geometry = load_geometry_from_html(cache_html)
            if cached_geometry is None:
                raise
            print(f"Using cached 3D geometry from {cache_html}", file=sys.stderr)
            geometry = cached_geometry
        save_geometry_cache(geometry_cache, geometry)
    if event is None:
        elevation_provider: ElevationProvider = OpenTopoDataElevationProvider(elevation_scale)
        elevation = elevation_provider.resolve(track, event, geometry, track_root)
    else:
        elevation_provider = FastF1ElevationProvider(elevation_scale)
        try:
            elevation = elevation_provider.resolve(track, event, geometry, track_root)
        except RuntimeError:
            if str(geometry.metadata.get("geometry_source")) == "osm_raceway":
                print("FastF1 elevation unavailable, falling back to OpenTopoData DEM.", file=sys.stderr)
                elevation = OpenTopoDataElevationProvider(elevation_scale).resolve(track, event, geometry, track_root)
            else:
                raise

    theme = RenderTheme(
        track_width_m=track_width_m,
        elevation_scale=elevation_scale,
        track_depth=track_depth,
    )
    html_path = track_root / f"{slugify(title)}-3d.html"
    html_path.write_text(build_html_document(geometry, elevation, event, theme, display_title=title))

    if event is None:
        source_metadata = {
            "title": title,
            "event": {
                "year": None,
                "event_name": title,
                "official_event_name": title,
                "location": title,
                "country": "",
                "session_type": None,
            },
            "fastest_lap": None,
            "sources": [
                geometry.source_label,
                elevation.source_label,
                "Track-config corner labels and geometry-derived marker placement",
                "OpenTopoData DEM samples cached in the track folder",
            ],
            "data_urls": {
                **geometry.source_urls,
                "elevation": elevation.metadata.get("request_url"),
            },
            "data_files": {
                "geometry": geometry_cache.name,
                "elevation_profile": elevation.metadata.get("cache_file"),
            },
            "sector_splits": [],
            "config_overrides_used": {
                "track_config_id": track_config.get("id"),
                "rotation_degrees": float(track_config.get("rotation_degrees", 0.0)),
                "corner_labels": bool(track_config.get("corner_labels")),
                "marker_spread_hints": bool(track_config.get("marker_spread_hints")),
                "geometry_source": geometry.metadata.get("geometry_source", geometry.source_label),
                "geometry_only": True,
                "target_track_length_m": geometry.metadata.get("target_track_length_m"),
                "geometry_length_scale_factor": geometry.metadata.get("geometry_length_scale_factor"),
            },
        }
        cum = geometry.distances_m
        split_1 = cum[-1] / 3.0 if cum else 0.0
        split_2 = cum[-1] * 2.0 / 3.0 if cum else 0.0
        if geometry.local_points and geometry.distances_m:
            point_1 = interpolate_closed_local_point(geometry.local_points, geometry.distances_m, split_1)
            point_2 = interpolate_closed_local_point(geometry.local_points, geometry.distances_m, split_2)
            source_metadata["sector_splits"] = [
                {
                    "sector": 1,
                    "method": "equal thirds of centerline distance",
                    "distance_along_trace": round(split_1, 3),
                    "point": {"x": round(point_1.x, 3), "y": round(point_1.z, 3)},
                },
                {
                    "sector": 2,
                    "method": "equal thirds of centerline distance",
                    "distance_along_trace": round(split_2, 3),
                    "point": {"x": round(point_2.x, 3), "y": round(point_2.z, 3)},
                },
            ]
        (track_root / "source_metadata.json").write_text(json.dumps(source_metadata, indent=2))

    print(str(html_path))
    return html_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a 3D HTML track ribbon from FastF1 or DEM-backed elevation.")
    parser.add_argument("track", help="Track or event name, e.g. Imola, Monza, Suzuka")
    parser.add_argument("--year", type=int, help="Season year to use")
    parser.add_argument("--session", default="Q", help="FastF1 session code to use, defaults to Q")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Root output directory")
    parser.add_argument("--track-width-m", type=float, default=18.0, help="Track width in meters")
    parser.add_argument("--elevation-scale", type=float, default=1.0, help="Elevation exaggeration multiplier")
    parser.add_argument("--track-depth", type=float, default=18.0, help="Solid track depth in scene units")
    args = parser.parse_args()

    render_track_3d(
        track=args.track,
        year=args.year,
        session=args.session,
        output_root_value=args.output_root,
        track_width_m=args.track_width_m,
        elevation_scale=args.elevation_scale,
        track_depth=args.track_depth,
    )


if __name__ == "__main__":
    main()
