from __future__ import annotations

import argparse
import csv
import difflib
import io
import json
import math
import re
import subprocess
import sys
from functools import lru_cache
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".cache" / "fastf1"
CONFIG_DIR = ROOT / "track_configs"
STYLE_DIR = ROOT / "track_styles"
LEGACY_CONFIG_PATH = ROOT / "track_configs.json"

CANVAS_W = 1240
CANVAS_H = 860
PADDING_X = 96
PADDING_Y = 120

OUTER_W = 34
INNER_W = 24
SECTOR_W = 10
MARKER_R = 18
DEFAULT_MARKER_OFFSET = 37.5
START_LINE_HALF = 22
ARROW_OFFSET = 34
ARROW_LENGTH = 24
ARROW_HEAD = 8

COLORS = {
    "bg": "#ffffff",
    "outer": "#121528",
    "inner": "#2e3448",
    "s1": "#fd0000",
    "s2": "#02a5d5",
    "s3": "#eecc03",
}

DEFAULT_STYLE_NAME = "default"
STYLE_DEFAULTS: dict[str, Any] = {
    "name": DEFAULT_STYLE_NAME,
    "bg": COLORS["bg"],
    "title_fill": "#111111",
    "title_size": 34,
    "title_weight": 700,
    "title_font": "Inter, Arial, sans-serif",
    "outer": COLORS["outer"],
    "inner": COLORS["inner"],
    "s1": COLORS["s1"],
    "s2": COLORS["s2"],
    "s3": COLORS["s3"],
    "outer_w": OUTER_W,
    "inner_w": INNER_W,
    "sector_w": SECTOR_W,
    "track_join": "round",
    "track_cap": "round",
    "sector_join": "round",
    "sector_cap": "butt",
    "marker_fill": "#2e3448",
    "marker_stroke": "#2e3448",
    "marker_text": "#ffffff",
    "marker_text_size": 15,
    "marker_text_weight": 700,
    "label_fill": "#111111",
    "label_stroke": "#ffffff",
    "label_stroke_w": 5,
    "label_size": 18,
    "label_weight": 600,
    "label_font": "Inter, Arial, sans-serif",
    "sector_labels": False,
    "sector_label_fill_mode": "sector",
    "sector_label_stroke": "#ffffff",
    "sector_label_stroke_w": 5,
    "sector_label_size": 20,
    "sector_label_weight": 800,
    "sector_label_font": "Inter, Arial, sans-serif",
    "sector_label_letter_spacing": 0.0,
    "start_line_outer": "#121528",
    "start_line_inner": "#ffffff",
    "arrow_color": "#121528",
    "comparison_centerline_color": "#eecc03",
    "debug_centerline_color": "#000000",
}


def ensure_fastf1():
    try:
        import fastf1  # type: ignore
    except ImportError:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "fastf1"],
            check=True,
        )
        import fastf1  # type: ignore
    return fastf1


@dataclass
class Point:
    x: float
    y: float


@dataclass
class Turn:
    key: str
    number: int
    letter: str
    point: Point
    angle_deg: float
    track_index: int


@dataclass
class GeometrySpec:
    kind: str
    title: str
    centerline_url: str | None = None
    raceline_url: str | None = None
    source_repo: str | None = None
    source_note: str | None = None
    source_label: str | None = None
    raw_candidates: list[str] | None = None
    points: list[Point] | None = None
    raw_data: dict[str, Any] | None = None
    data_file_name: str | None = None


def dist(a: Point, b: Point) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)


def cumulative_dist(points: list[Point]) -> list[float]:
    total = [0.0]
    for idx in range(1, len(points)):
        total.append(total[-1] + dist(points[idx - 1], points[idx]))
    return total


def interpolate_point(a: Point, b: Point, t: float) -> Point:
    return Point(
        x=a.x + (b.x - a.x) * t,
        y=a.y + (b.y - a.y) * t,
    )


def rotate_point(point: Point, origin: Point, degrees: float) -> Point:
    if degrees % 360 == 0:
        return point
    radians = math.radians(degrees)
    cos_a = math.cos(radians)
    sin_a = math.sin(radians)
    dx = point.x - origin.x
    dy = point.y - origin.y
    return Point(
        x=origin.x + dx * cos_a - dy * sin_a,
        y=origin.y + dx * sin_a + dy * cos_a,
    )


def rotate_points(points: list[Point], origin: Point, degrees: float) -> list[Point]:
    return [rotate_point(point, origin, degrees) for point in points]


def interpolate_track_point(points: list[Point], cum: list[float], target: float) -> Point:
    if target <= 0:
        return points[0]
    if target >= cum[-1]:
        return points[-1]
    for idx in range(len(cum) - 1):
        if cum[idx] <= target <= cum[idx + 1]:
            seg_len = cum[idx + 1] - cum[idx]
            if seg_len == 0:
                return points[idx]
            t = (target - cum[idx]) / seg_len
            return interpolate_point(points[idx], points[idx + 1], t)
    return points[-1]


def slice_path(points: list[Point], cum: list[float], start_d: float, end_d: float) -> list[Point]:
    out = [interpolate_track_point(points, cum, start_d)]
    for idx, value in enumerate(cum):
        if start_d < value < end_d:
            out.append(points[idx])
    out.append(interpolate_track_point(points, cum, end_d))
    return out


def to_svg_path(points: list[tuple[float, float]]) -> str:
    if not points:
        return ""
    start = f"M {points[0][0]:.2f} {points[0][1]:.2f}"
    segments = [f"L {x:.2f} {y:.2f}" for x, y in points[1:]]
    return " ".join([start, *segments])


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


def config_file_path(track: dict[str, Any]) -> Path:
    name = str(track.get("id") or track.get("title") or "track").strip()
    return CONFIG_DIR / f"{slugify(name)}.json"


def load_config() -> dict[str, Any]:
    tracks: list[dict[str, Any]] = []
    if CONFIG_DIR.exists():
        for path in sorted(CONFIG_DIR.glob("*.json")):
            try:
                tracks.append(json.loads(path.read_text()))
            except Exception:
                continue
        return {"tracks": tracks}
    if LEGACY_CONFIG_PATH.exists():
        payload = json.loads(LEGACY_CONFIG_PATH.read_text())
        if isinstance(payload, dict) and isinstance(payload.get("tracks"), list):
            return payload
    return {"tracks": []}


def save_config(config: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    desired_paths: set[Path] = set()
    for track in config.get("tracks", []):
        path = config_file_path(track)
        desired_paths.add(path)
        path.write_text(json.dumps(track, indent=2) + "\n")
    for path in CONFIG_DIR.glob("*.json"):
        if path not in desired_paths:
            path.unlink()


def compact_key(value: str) -> str:
    return compact(value)


def load_styles() -> dict[str, dict[str, Any]]:
    styles: dict[str, dict[str, Any]] = {compact_key(DEFAULT_STYLE_NAME): dict(STYLE_DEFAULTS)}
    if STYLE_DIR.exists():
        for path in sorted(STYLE_DIR.glob("*.json")):
            try:
                payload = json.loads(path.read_text())
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            name = str(payload.get("name") or path.stem).strip() or path.stem
            merged = dict(STYLE_DEFAULTS)
            merged.update(payload)
            merged["name"] = name
            styles[compact_key(name)] = merged
    return styles


def resolve_style(style_name: str | None, styles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    key = compact_key(style_name or DEFAULT_STYLE_NAME)
    return styles.get(key, styles[compact_key(DEFAULT_STYLE_NAME)])


def fetch_json(url: str) -> dict[str, Any]:
    return json.loads(fetch_text(url))


def fetch_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": "TrackMaker/1.0"})
    with urlopen(req, timeout=20) as response:
        return response.read().decode("utf-8")


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
    text = fetch_text(url)
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter) if row]


def load_raceline_points(url: str) -> tuple[list[Point], list[float], list[float], list[float]]:
    points: list[Point] = []
    s_values: list[float] = []
    headings: list[float] = []
    curvatures: list[float] = []
    for row in fetch_csv_rows(url, delimiter=";"):
        if row[0].startswith("#"):
            continue
        s, x, y, psi, kappa, *_rest = map(float, row)
        s_values.append(s)
        points.append(Point(x, y))
        headings.append(psi)
        curvatures.append(kappa)
    if points and points[0] != points[-1]:
        points.append(points[0])
        s_values.append(s_values[-1] + dist(points[-2], points[-1]))
        headings.append(headings[0])
        curvatures.append(curvatures[0])
    return points, s_values, headings, curvatures


def load_centerline_points(url: str) -> list[Point]:
    return load_xy_points_from_text(fetch_text(url), delimiter=",")


def load_xy_points(csv_path: Path) -> list[Point]:
    points: list[Point] = []
    with csv_path.open(newline="") as fh:
        reader = csv.reader(fh)
        for row in reader:
            if not row:
                continue
            if row[0].startswith("#"):
                continue
            if len(row) < 2:
                continue
            x = float(row[0])
            y = float(row[1])
            points.append(Point(x, y))
    return points


def load_xy_points_from_text(text: str, delimiter: str = ",") -> list[Point]:
    points: list[Point] = []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    for row in reader:
        if not row:
            continue
        if row[0].startswith("#"):
            continue
        if len(row) < 2:
            continue
        try:
            x = float(row[0])
            y = float(row[1])
        except ValueError:
            continue
        points.append(Point(x, y))
    return points


def download_url_text(url: str, destination: Path) -> None:
    destination.write_text(fetch_text(url))


@dataclass
class SimilarityTransform:
    source_center: Point
    target_center: Point
    scale: float
    rotation_deg: float


def sample_closed_points(points: list[Point], sample_count: int) -> list[Point]:
    if not points:
        return []
    if points[0] != points[-1]:
        points = [*points, points[0]]
    cum = cumulative_dist(points)
    total = cum[-1]
    if total <= 0:
        return [points[0] for _ in range(sample_count)]
    return [interpolate_track_point(points, cum, total * idx / sample_count) for idx in range(sample_count)]


def fit_similarity_transform(source_points: list[Point], target_points: list[Point], sample_count: int = 64) -> SimilarityTransform:
    source_samples = sample_closed_points(source_points, sample_count)
    target_samples = sample_closed_points(target_points, sample_count)
    if not source_samples or not target_samples:
        return SimilarityTransform(Point(0.0, 0.0), Point(0.0, 0.0), 1.0, 0.0)

    best_transform = SimilarityTransform(Point(0.0, 0.0), Point(0.0, 0.0), 1.0, 0.0)
    best_error = float("inf")

    for shift in range(sample_count):
        paired_target = target_samples[shift:] + target_samples[:shift]
        sx = sum(point.x for point in source_samples) / sample_count
        sy = sum(point.y for point in source_samples) / sample_count
        tx = sum(point.x for point in paired_target) / sample_count
        ty = sum(point.y for point in paired_target) / sample_count
        source_center = Point(sx, sy)
        target_center = Point(tx, ty)

        a = 0.0
        b = 0.0
        denom = 0.0
        for sp, tp in zip(source_samples, paired_target):
            spx = sp.x - sx
            spy = sp.y - sy
            tpx = tp.x - tx
            tpy = tp.y - ty
            a += spx * tpx + spy * tpy
            b += spx * tpy - spy * tpx
            denom += spx * spx + spy * spy

        if denom <= 0:
            continue
        rotation = math.atan2(b, a)
        cos_a = math.cos(rotation)
        sin_a = math.sin(rotation)
        num = 0.0
        for sp, tp in zip(source_samples, paired_target):
            spx = sp.x - sx
            spy = sp.y - sy
            rx = cos_a * spx - sin_a * spy
            ry = sin_a * spx + cos_a * spy
            num += tp.x * rx + tp.y * ry
        scale = num / denom
        if scale == 0:
            continue

        transform = SimilarityTransform(
            source_center=source_center,
            target_center=target_center,
            scale=scale,
            rotation_deg=math.degrees(rotation),
        )
        errors = []
        for sp, tp in zip(source_samples, paired_target):
            transformed = apply_similarity_transform(sp, transform)
            errors.append(math.hypot(transformed.x - tp.x, transformed.y - tp.y))
        rms = sum(e * e for e in errors) / len(errors)
        if rms < best_error:
            best_error = rms
            best_transform = transform

    return best_transform


def apply_similarity_transform(point: Point, transform: SimilarityTransform) -> Point:
    radians = math.radians(transform.rotation_deg)
    cos_a = math.cos(radians)
    sin_a = math.sin(radians)
    dx = point.x - transform.source_center.x
    dy = point.y - transform.source_center.y
    return Point(
        x=transform.target_center.x + transform.scale * (dx * cos_a - dy * sin_a),
        y=transform.target_center.y + transform.scale * (dx * sin_a + dy * cos_a),
    )


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.asin(min(1.0, math.sqrt(a)))


def project_latlon(lat: float, lon: float, lat0: float, lon0: float) -> Point:
    meters_per_deg_lat = 111132.0
    meters_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))
    return Point((lon - lon0) * meters_per_deg_lon, (lat - lat0) * meters_per_deg_lat)


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


@dataclass
class RacewaySegment:
    way_id: int
    points: list[Point]
    name: str
    length_m: float

    @property
    def start_key(self) -> tuple[float, float]:
        return (round(self.points[0].x, 6), round(self.points[0].y, 6))

    @property
    def end_key(self) -> tuple[float, float]:
        return (round(self.points[-1].x, 6), round(self.points[-1].y, 6))

    @property
    def is_named(self) -> bool:
        return bool(self.name.strip())


def segment_heading(points: list[Point], at_start: bool) -> float:
    if len(points) < 2:
        return 0.0
    if at_start:
        a, b = points[0], points[1]
    else:
        a, b = points[-1], points[-2]
    return math.degrees(math.atan2(b.x - a.x, b.y - a.y))


def build_osm_raceway_loop(segments: list[RacewaySegment]) -> list[Point]:
    if not segments:
        return []

    by_endpoint: dict[tuple[float, float], list[RacewaySegment]] = {}
    for seg in segments:
        by_endpoint.setdefault(seg.start_key, []).append(seg)
        by_endpoint.setdefault(seg.end_key, []).append(seg)

    def priority(seg: RacewaySegment) -> tuple[int, int, float]:
        return (1 if seg.is_named else 0, len(seg.points), seg.length_m)

    start = max(segments, key=priority)
    used = {start.way_id}
    path = list(start.points)
    current_heading = segment_heading(path, at_start=False)
    current_key = start.end_key
    start_key = start.start_key

    while True:
        candidates = [seg for seg in by_endpoint.get(current_key, []) if seg.way_id not in used]
        if not candidates:
            break
        scored: list[tuple[float, int, RacewaySegment, bool]] = []
        for seg in candidates:
            if seg.start_key == current_key:
                heading = segment_heading(seg.points, at_start=True)
                reverse = False
                oriented_len = len(seg.points)
            else:
                heading = segment_heading(seg.points, at_start=False)
                reverse = True
                oriented_len = len(seg.points)
            diff = abs(((heading - current_heading + 180.0) % 360.0) - 180.0)
            scored.append((diff, -oriented_len, seg, reverse))
        scored.sort(key=lambda item: (item[0], item[1]))
        _, _, chosen, reverse = scored[0]
        used.add(chosen.way_id)
        oriented = list(reversed(chosen.points)) if reverse else chosen.points
        path.extend(oriented[1:])
        current_heading = segment_heading(oriented, at_start=False)
        current_key = chosen.start_key if reverse else chosen.end_key
        if current_key == start_key:
            break

    if path and path[0] != path[-1]:
        path.append(path[0])
    return path


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


def resolve_tumftm_geometry(title: str, event_fields: list[str], track_config: dict[str, Any]) -> GeometrySpec | None:
    explicit_centerline = str(track_config.get("centerline_url", "")).strip()
    explicit_raceline = str(track_config.get("raceline_url", "")).strip()
    geometry_source = str(track_config.get("geometry_source", "auto")).lower()
    if geometry_source == "fastf1":
        return None
    if geometry_source.startswith("osm"):
        return None
    if geometry_source == "f1tenth_racetrack" and not explicit_centerline and not explicit_raceline:
        return None

    if explicit_centerline or explicit_raceline:
        source_label = "configured geometry"
        source_note = "track config override"
        if geometry_source == "track_database":
            source_label = "TUMFTM track centerline"
            source_note = "track_database config override"
        elif geometry_source == "f1tenth_racetrack":
            source_label = "F1TENTH track centerline"
            source_note = "f1tenth config override"
        return GeometrySpec(
            kind=geometry_source if geometry_source != "auto" else "track_database",
            title=title,
            centerline_url=explicit_centerline or None,
            raceline_url=explicit_raceline or None,
            source_repo=None,
            source_note=source_note,
            source_label=source_label,
            raw_candidates=candidate_strings(title, *event_fields),
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
        return GeometrySpec(
            kind="track_database",
            title=title,
            centerline_url=tumftm_url,
            source_repo="TUMFTM/racetrack-database",
            source_note="TUMFTM centerline database",
            source_label="TUMFTM track centerline",
            raw_candidates=candidates,
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
            return GeometrySpec(
                kind="f1tenth_racetrack",
                title=title,
                centerline_url=centerline_url,
                raceline_url=raceline_url,
                source_repo="f1tenth/f1tenth_racetracks",
                source_note="F1TENTH track database",
                source_label="F1TENTH track centerline",
                raw_candidates=candidates,
            )

    return None


def resolve_osm_geometry(title: str, event_fields: list[str], track_config: dict[str, Any]) -> GeometrySpec | None:
    if str(track_config.get("geometry_source", "")).lower() == "fastf1":
        return None

    candidates = candidate_strings(title, *event_fields)
    if not candidates:
        return None

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
    nominatim_result = None
    for query in search_queries:
        query = query.strip()
        if not query:
            continue
        nominatim_result = nominatim_search(query)
        if nominatim_result:
            break
    if not nominatim_result:
        return None

    try:
        lat0 = float(nominatim_result["lat"])
        lon0 = float(nominatim_result["lon"])
    except Exception:
        return None

    bbox = nominatim_result.get("boundingbox") or []
    radius = 1500.0
    if len(bbox) == 4:
        south, north, west, east = map(float, bbox)
        radius = max(1200.0, haversine_m(south, west, north, east) / 2.0 + 250.0)

    overpass_query_text = f'''[out:json][timeout:25];
way["highway"="raceway"](around:{int(radius)},{lat0},{lon0});
out tags geom qt;'''
    raw_overpass = overpass_request(overpass_query_text)
    if not raw_overpass:
        return None

    segments: list[RacewaySegment] = []
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
        segments.append(RacewaySegment(int(element["id"]), points, name, length_m))

    if not segments:
        return None

    loop_points = build_osm_raceway_loop(segments)
    if not loop_points:
        return None

    return GeometrySpec(
        kind="osm_raceway",
        title=title,
        source_repo="OpenStreetMap",
        source_note="OSM highway=raceway geometry via Overpass",
        source_label="OpenStreetMap raceway geometry",
        raw_candidates=candidates,
        points=loop_points,
        raw_data={
            "nominatim_query": search_queries,
            "nominatim_result": nominatim_result,
            "overpass_query": overpass_query_text,
            "overpass_result": raw_overpass,
        },
        data_file_name=f"{slugify(title)}_osm_raceway.json",
    )


def unwrap_angles(values: list[float]) -> list[float]:
    if not values:
        return []
    out = [values[0]]
    for value in values[1:]:
        adjusted = value
        while adjusted - out[-1] > math.pi:
            adjusted -= 2 * math.pi
        while adjusted - out[-1] < -math.pi:
            adjusted += 2 * math.pi
        out.append(adjusted)
    return out


def smooth_series(values: list[float], window: int = 7) -> list[float]:
    if not values:
        return []
    out: list[float] = []
    for idx in range(len(values)):
        start = max(0, idx - window)
        end = min(len(values), idx + window + 1)
        segment = values[start:end]
        out.append(sum(segment) / len(segment))
    return out


def local_maxima(values: list[float]) -> list[int]:
    peaks: list[int] = []
    for idx in range(1, len(values) - 1):
        if values[idx] >= values[idx - 1] and values[idx] >= values[idx + 1]:
            peaks.append(idx)
    return peaks


def select_spaced_peaks(peaks: list[tuple[float, int]], target_count: int, min_sep: int) -> list[int]:
    selected: list[tuple[float, int]] = []
    for score, idx in sorted(peaks, reverse=True):
        if all(abs(idx - other_idx) >= min_sep for _, other_idx in selected):
            selected.append((score, idx))
        if len(selected) >= target_count:
            break
    selected.sort(key=lambda item: item[1])
    return [idx for _, idx in selected]


def heading_from_points(points: list[Point], index: int, window: int = 3) -> float:
    if not points:
        return 0.0
    left = points[(index - window) % len(points)]
    right = points[(index + window) % len(points)]
    dx = right.x - left.x
    dy = right.y - left.y
    return (math.degrees(math.atan2(dx, dy)) + 360.0) % 360.0


def build_turns_from_raceline(points: list[Point], curvatures: list[float], target_count: int) -> list[Turn]:
    if not points:
        return []
    abs_curvature = smooth_series([abs(value) for value in curvatures], window=7)
    peaks = local_maxima(abs_curvature)
    if not peaks:
        return []
    min_sep = max(60, len(points) // max(target_count * 2, 1))
    scored = [(abs_curvature[idx], idx) for idx in peaks]
    if len(scored) < target_count:
        scored = [(abs_curvature[idx], idx) for idx in range(len(points))]
    selected = select_spaced_peaks(scored, target_count, min_sep)
    turns: list[Turn] = []
    for idx, point_index in enumerate(selected, start=1):
        curvature = curvatures[point_index]
        tangent_angle = heading_from_points(points, point_index, window=4)
        angle_deg = tangent_angle if curvature >= 0 else (tangent_angle + 180.0) % 360.0
        turns.append(
            Turn(
                key=turn_label(idx, ""),
                number=idx,
                letter="",
                point=points[point_index],
                angle_deg=angle_deg,
                track_index=point_index,
            )
        )
    return turns


def compute_signed_curvatures(points: list[Point]) -> list[float]:
    if len(points) < 3:
        return [0.0 for _ in points]
    curvatures = [0.0] * len(points)
    for idx in range(1, len(points) - 1):
        prev_pt = points[idx - 1]
        point = points[idx]
        next_pt = points[idx + 1]
        ax = point.x - prev_pt.x
        ay = point.y - prev_pt.y
        bx = next_pt.x - point.x
        by = next_pt.y - point.y
        norm_a = math.hypot(ax, ay)
        norm_b = math.hypot(bx, by)
        if norm_a == 0 or norm_b == 0:
            continue
        cross = ax * by - ay * bx
        dot = ax * bx + ay * by
        angle = math.atan2(cross, dot)
        arc = (norm_a + norm_b) / 2.0
        curvatures[idx] = angle / arc if arc else 0.0
    curvatures[0] = curvatures[1]
    curvatures[-1] = curvatures[-2]
    return smooth_series(curvatures, window=3)


def build_turns_from_geometry(
    points: list[Point],
    target_count: int,
    min_sep: int | None = None,
    curvature_window: int = 3,
) -> list[Turn]:
    if not points:
        return []
    curvatures = compute_signed_curvatures(points)
    peaks = local_maxima([abs(value) for value in curvatures])
    if not peaks:
        return []
    if min_sep is None:
        min_sep = max(60, len(points) // max(target_count * 2, 1))
    scored = [(abs(curvatures[idx]), idx) for idx in peaks]
    if len(scored) < target_count:
        scored = [(abs(curvatures[idx]), idx) for idx in range(len(points))]
    selected = select_spaced_peaks(scored, target_count, min_sep)
    turns: list[Turn] = []
    for idx, point_index in enumerate(selected, start=1):
        curvature = curvatures[point_index]
        tangent_angle = heading_from_points(points, point_index, window=4)
        angle_deg = tangent_angle if curvature >= 0 else (tangent_angle + 180.0) % 360.0
        turns.append(
            Turn(
                key=turn_label(idx, ""),
                number=idx,
                letter="",
                point=points[point_index],
                angle_deg=angle_deg,
                track_index=point_index,
            )
        )
    return turns


def remap_turn_numbers(turns: list[Turn], remap: dict[str, str]) -> list[Turn]:
    if not remap:
        return turns
    remapped: list[Turn] = []
    for turn in turns:
        new_key = remap.get(turn.key, turn.key)
        try:
            new_number = int(re.match(r"\d{1,2}", new_key).group(0)) if re.match(r"\d{1,2}", new_key) else turn.number
        except Exception:
            new_number = turn.number
        remapped.append(
            Turn(
                key=new_key,
                number=new_number,
                letter=turn.letter,
                point=turn.point,
                angle_deg=turn.angle_deg,
                track_index=turn.track_index,
            )
        )
    remapped.sort(key=lambda item: (item.track_index, item.number, item.letter))
    return remapped


def wikipedia_search_titles(queries: list[str], limit: int = 5) -> list[str]:
    titles: list[str] = []
    seen = set()
    for query in queries:
        if not query.strip():
            continue
        url = (
            "https://en.wikipedia.org/w/api.php?action=query&list=search"
            f"&srsearch={quote(query)}&utf8=1&format=json&srlimit={limit}"
        )
        try:
            payload = fetch_json(url)
        except Exception:
            continue
        for item in payload.get("query", {}).get("search", []):
            title = item.get("title")
            if title and title not in seen:
                titles.append(title)
                seen.add(title)
    return titles


def wikipedia_extract(title: str) -> str:
    url = (
        "https://en.wikipedia.org/w/api.php?action=query&prop=extracts"
        f"&titles={quote(title)}&explaintext=1&exsectionformat=plain&redirects=1&format=json"
    )
    payload = fetch_json(url)
    pages = payload.get("query", {}).get("pages", {})
    for page in pages.values():
        extract = page.get("extract")
        if extract:
            return str(extract)
    return ""


def parse_turn_reference(value: str) -> list[int]:
    cleaned = value.replace("&", ",").replace("and", ",").replace("to", "-")
    out: list[int] = []
    for part in re.split(r"[,/]", cleaned):
        token = part.strip()
        if not token:
            continue
        match = re.fullmatch(r"(\d{1,2})\s*-\s*(\d{1,2})", token)
        if match:
            start = int(match.group(1))
            end = int(match.group(2))
            if start <= end:
                out.extend(range(start, end + 1))
            continue
        match = re.search(r"\d{1,2}", token)
        if match:
            out.append(int(match.group(0)))
    seen = set()
    deduped = []
    for number in out:
        if number not in seen:
            deduped.append(number)
            seen.add(number)
    return deduped


def clean_corner_name(name: str) -> str:
    parts = re.split(r"[.;:]", name)
    if parts:
        name = parts[-1]
    name = re.sub(r"\s+", " ", name).strip(" -:,.;")
    name = re.sub(r"^(the)\s+", "", name, flags=re.IGNORECASE)
    if name.lower() in {"the", "turn", "corner", "curve", "chicane", "bend", "hairpin"}:
        return ""
    return name


def extract_explicit_corner_labels(extract: str, turn_lookup: dict[str, Turn]) -> list[dict[str, Any]]:
    patterns = [
        re.compile(
            r"([A-Z][A-Za-z0-9À-ÿ'’().-]+(?:\s+[A-Z0-9][A-Za-z0-9À-ÿ'’().-]+){0,5})\s*\((?:Turns?|turns?)\s*([0-9A-Za-z,\-/ &]+)\)"
        ),
        re.compile(
            r"(?:Turns?|turns?)\s*([0-9A-Za-z,\-/ &]+)\s+(?:are|is|form|forms|known as|called)\s+(?:the\s+)?([A-Z][A-Za-z0-9À-ÿ'’().-]+(?:\s+[A-Z0-9][A-Za-z0-9À-ÿ'’().-]+){0,5})"
        ),
    ]
    labels: list[dict[str, Any]] = []
    seen = set()
    for pattern in patterns:
        for match in pattern.finditer(extract):
            if pattern.pattern.startswith("("):
                name = clean_corner_name(match.group(1))
                numbers = parse_turn_reference(match.group(2))
            else:
                numbers = parse_turn_reference(match.group(1))
                name = clean_corner_name(match.group(2))
            turn_keys = [key for key, turn in turn_lookup.items() if turn.number in numbers]
            if not name or not turn_keys:
                continue
            key = (name, tuple(turn_keys))
            if key in seen:
                continue
            labels.append({"name": name, "turns": turn_keys, "dx": 0, "dy": 0})
            seen.add(key)
    return labels


def extract_candidate_corner_names(extract: str) -> list[str]:
    candidates: list[str] = []
    seen = set()
    patterns = [
        re.compile(
            r"([A-Z][A-Za-z0-9À-ÿ'’().-]+(?:\s+[A-Z0-9][A-Za-z0-9À-ÿ'’().-]+){0,5})\s+(?:corner|corners|curve|curves|chicane|bend|hairpin|esses)"
        ),
        re.compile(
            r"(?:corner|corners|curve|curves|chicane|bend|hairpin)\s+(?:called|named)\s+([A-Z][A-Za-z0-9À-ÿ'’().-]+(?:\s+[A-Z0-9][A-Za-z0-9À-ÿ'’().-]+){0,5})"
        ),
    ]
    for pattern in patterns:
        for match in pattern.finditer(extract):
            name = clean_corner_name(match.group(1))
            if len(name) < 3:
                continue
            lowered = name.lower()
            if lowered in seen:
                continue
            if lowered.startswith("turn "):
                continue
            candidates.append(name)
            seen.add(lowered)
    return candidates


def infer_turn_groups(turns: list[Turn]) -> list[list[str]]:
    if not turns:
        return []
    groups: list[list[str]] = [[turns[0].key]]
    gaps = []
    for first, second in zip(turns, turns[1:]):
        gaps.append(abs(second.track_index - first.track_index))
    if not gaps:
        return groups
    median_gap = sorted(gaps)[len(gaps) // 2]
    threshold = median_gap * 0.72
    for gap, turn in zip(gaps, turns[1:]):
        if gap <= threshold and len(groups[-1]) < 4:
            groups[-1].append(turn.key)
        else:
            groups.append([turn.key])
    return groups


def autogenerate_track_config(
    track_query: str,
    event: Any,
    turns: list[Turn],
    turn_lookup: dict[str, Turn],
    geometry_spec: GeometrySpec | None = None,
    style_name: str = DEFAULT_STYLE_NAME,
) -> dict[str, Any]:
    search_queries = [
        f"{event.get('Location', '')} circuit",
        f"{event.get('EventName', '')} circuit",
        f"{track_query} circuit",
        f"{event.get('OfficialEventName', '')} circuit",
    ]
    source_title = None
    source_extract = ""
    for title in wikipedia_search_titles(search_queries):
        extract = wikipedia_extract(title)
        if not extract:
            continue
        source_title = title
        source_extract = extract
        break

    explicit_labels = extract_explicit_corner_labels(source_extract, turn_lookup) if source_extract else []
    candidate_names = extract_candidate_corner_names(source_extract) if source_extract else []
    guessed_labels: list[dict[str, Any]] = []
    strategy = "none"

    if explicit_labels:
        guessed_labels = explicit_labels
        strategy = "explicit_turn_references"
    elif candidate_names:
        groups = infer_turn_groups(turns) if len(candidate_names) < len(turns) else [[turn.key] for turn in turns]
        for name, group in zip(candidate_names, groups):
            guessed_labels.append({"name": name, "turns": group, "dx": 0, "dy": 0})
        strategy = "ordered_name_guess"

    return {
        "id": slugify(str(event.get("Location") or track_query)),
        "title": str(event.get("Location") or track_query).strip(),
        "match_terms": [
            normalize(track_query),
            normalize(str(event.get("Location", ""))),
            normalize(str(event.get("EventName", ""))),
        ],
        "geometry_source": geometry_spec.kind if geometry_spec else "fastf1",
        "raceline_url": geometry_spec.raceline_url if geometry_spec else "",
        "centerline_url": geometry_spec.centerline_url if geometry_spec else "",
        "style": style_name,
        "rotation_degrees": 0.0,
        "marker_offset": DEFAULT_MARKER_OFFSET,
        "marker_spread_hints": {},
        "corner_labels": guessed_labels,
        "generated_corner_name_candidates": candidate_names,
        "generated_from": {
            "source": "wikipedia",
            "page_title": source_title,
            "strategy": strategy,
        },
        "generated_geometry": {
            "source_repo": geometry_spec.source_repo if geometry_spec else None,
            "source_note": geometry_spec.source_note if geometry_spec else None,
            "source_label": geometry_spec.source_label if geometry_spec else None,
        },
    }


def find_track_config(query: str, event_fields: list[str], config: dict[str, Any]) -> dict[str, Any] | None:
    query_norm = normalize(query)
    event_norm = " ".join(normalize(v) for v in event_fields if v)
    best = None
    best_score = 0.0
    for item in config.get("tracks", []):
        terms = item.get("match_terms", []) + [item.get("id", ""), item.get("title", "")]
        score = 0.0
        for term in terms:
            term_norm = normalize(term)
            if not term_norm:
                continue
            score = max(
                score,
                difflib.SequenceMatcher(None, query_norm, term_norm).ratio(),
                difflib.SequenceMatcher(None, event_norm, term_norm).ratio(),
            )
            if term_norm in query_norm or query_norm in term_norm:
                score = max(score, 1.0)
            if term_norm and term_norm in event_norm:
                score = max(score, 0.98)
        if score > best_score:
            best_score = score
            best = item
    return best if best_score >= 0.72 else None


def best_event_match(fastf1, track_query: str, year: int | None) -> tuple[int, Any]:
    years = [year] if year else list(range(datetime.now().year, datetime.now().year - 5, -1))
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
                score = max(score, difflib.SequenceMatcher(None, query_norm, field_norm).ratio())
                if query_norm in field_norm:
                    score = max(score, 0.99)
                if field_norm and field_norm in query_norm:
                    score = max(score, 0.95)
            if best is None or score > best[0]:
                best = (score, candidate_year, row)

    if best is None or best[0] < 0.45:
        raise ValueError(f"Could not find an F1 event matching '{track_query}'.")
    return best[1], best[2]


def turn_label(number: int, letter: str) -> str:
    return f"{number:02d}{letter}" if letter else f"{number:02d}"


def dedupe_pos_points(pos_df) -> tuple[list[Point], list[float], list[float]]:
    points: list[Point] = []
    times: list[float] = []
    last_xy = None
    for row in pos_df.itertuples(index=False):
        pt = Point(float(row.X), float(row.Y))
        t = row.SessionTime.total_seconds()
        xy = (pt.x, pt.y)
        if xy != last_xy:
            points.append(pt)
            times.append(t)
            last_xy = xy
    if points[0] != points[-1]:
        points.append(points[0])
        lap_end = pos_df["SessionTime"].iloc[-1].total_seconds() + 1e-6
        times.append(lap_end)
    return points, times, cumulative_dist(points)


def interpolate_distance_by_time(times: list[float], cum: list[float], target_time: float) -> float:
    if target_time <= times[0]:
        return cum[0]
    if target_time >= times[-1]:
        return cum[-1]
    for idx in range(len(times) - 1):
        if times[idx] <= target_time <= times[idx + 1]:
            span = times[idx + 1] - times[idx]
            if span <= 0:
                return cum[idx]
            t = (target_time - times[idx]) / span
            return cum[idx] + (cum[idx + 1] - cum[idx]) * t
    return cum[-1]


def build_turns(corners_df, track_points: list[Point]) -> list[Turn]:
    turns = []
    for row in corners_df.itertuples(index=False):
        number = int(row.Number)
        letter = str(row.Letter or "").strip()
        key = turn_label(number, letter)
        point = Point(float(row.X), float(row.Y))
        track_index = nearest_track_index(track_points, point)
        turns.append(
            Turn(
                key=key,
                number=number,
                letter=letter,
                point=point,
                angle_deg=float(row.Angle),
                track_index=track_index,
            )
        )
    turns.sort(key=lambda item: (item.track_index, item.number, item.letter))
    return turns


def nearest_track_index(points: list[Point], target: Point) -> int:
    best_idx = 0
    best_dist = float("inf")
    for idx, point in enumerate(points):
        d = (point.x - target.x) ** 2 + (point.y - target.y) ** 2
        if d < best_dist:
            best_dist = d
            best_idx = idx
    return best_idx


def project_point_to_segment(a: Point, b: Point, target: Point) -> tuple[Point, float]:
    dx = b.x - a.x
    dy = b.y - a.y
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq <= 0:
        return a, (a.x - target.x) ** 2 + (a.y - target.y) ** 2
    t = ((target.x - a.x) * dx + (target.y - a.y) * dy) / seg_len_sq
    t = max(0.0, min(1.0, t))
    proj = Point(a.x + dx * t, a.y + dy * t)
    return proj, (proj.x - target.x) ** 2 + (proj.y - target.y) ** 2


def reanchor_closed_loop(points: list[Point], anchor: Point) -> list[Point]:
    if not points:
        return []
    closed = list(points)
    if closed[0] != closed[-1]:
        closed.append(closed[0])

    best_idx = 0
    best_point = closed[0]
    best_dist = float("inf")
    for idx in range(len(closed) - 1):
        projected, dist_sq = project_point_to_segment(closed[idx], closed[idx + 1], anchor)
        if dist_sq < best_dist:
            best_dist = dist_sq
            best_idx = idx
            best_point = projected

    reordered = [best_point, *closed[best_idx + 1 : -1], *closed[: best_idx + 1], best_point]
    deduped: list[Point] = [reordered[0]]
    for point in reordered[1:]:
        if point != deduped[-1]:
            deduped.append(point)
    if deduped[0] != deduped[-1]:
        deduped.append(deduped[0])
    return deduped


def render_geometry_only_track(
    title: str,
    track_root: Path,
    track_points: list[Point],
    turns: list[Turn],
    turn_lookup: dict[str, Turn],
    track_config: dict[str, Any],
    style: dict[str, Any],
    rotation_degrees: float,
    source_label: str,
    source_urls: dict[str, str | None] | None = None,
    source_data_file_name: str | None = None,
) -> None:
    if source_data_file_name:
        pass

    cum = cumulative_dist(track_points)
    times = cum[:]
    split_1 = cum[-1] / 3.0
    split_2 = cum[-1] * 2.0 / 3.0
    split_positions = [
        interpolate_track_point(track_points, cum, split_1),
        interpolate_track_point(track_points, cum, split_2),
    ]
    sector_tracks = [
        slice_path(track_points, cum, 0.0, split_1),
        slice_path(track_points, cum, split_1, split_2),
        slice_path(track_points, cum, split_2, cum[-1]),
    ]

    turns_export = [
        {
            "turn": turn.key,
            "x": round(turn.point.x, 3),
            "y": round(turn.point.y, 3),
            "angle_deg": round(turn.angle_deg, 3),
        }
        for turn in turns
    ]
    (track_root / "track_turns.json").write_text(json.dumps(turns_export, indent=2))

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
            source_label,
            "Track-config corner labels and geometry-derived marker placement",
            "Equal-thirds sector splits derived from the centerline geometry",
        ],
        "data_urls": source_urls or {},
        "data_files": {
            "geometry": source_data_file_name,
        },
        "sector_splits": [
            {
                "sector": 1,
                "method": "equal thirds of centerline distance",
                "distance_along_trace": round(split_1, 3),
                "point": {"x": round(split_positions[0].x, 3), "y": round(split_positions[0].y, 3)},
            },
            {
                "sector": 2,
                "method": "equal thirds of centerline distance",
                "distance_along_trace": round(split_2, 3),
                "point": {"x": round(split_positions[1].x, 3), "y": round(split_positions[1].y, 3)},
            },
        ],
        "config_overrides_used": {
            "track_config_id": track_config.get("id"),
            "style": style.get("name", style_name),
            "rotation_degrees": rotation_degrees,
            "corner_labels": bool(track_config.get("corner_labels")),
            "marker_spread_hints": bool(track_config.get("marker_spread_hints")),
            "geometry_source": source_label,
            "geometry_only": True,
        },
    }
    (track_root / "source_metadata.json").write_text(json.dumps(source_metadata, indent=2))

    all_track_points = track_points[:-1]
    min_x = min(point.x for point in all_track_points)
    max_x = max(point.x for point in all_track_points)
    min_y = min(point.y for point in all_track_points)
    max_y = max(point.y for point in all_track_points)
    usable_w = CANVAS_W - 2 * PADDING_X
    usable_h = CANVAS_H - 2 * PADDING_Y
    scale = min(usable_w / (max_x - min_x), usable_h / (max_y - min_y))

    def sx(point: Point) -> float:
        return (point.x - min_x) * scale + PADDING_X

    def sy(point: Point) -> float:
        return (max_y - point.y) * scale + PADDING_Y

    full_svg_points = [(sx(point), sy(point)) for point in track_points]
    sector_svg_points = [[(sx(point), sy(point)) for point in section] for section in sector_tracks]

    start_x, start_y = full_svg_points[0]
    next_x, next_y = full_svg_points[1]
    tangent_dx = next_x - start_x
    tangent_dy = next_y - start_y
    tangent_len = math.hypot(tangent_dx, tangent_dy)
    tangent_ux = tangent_dx / tangent_len
    tangent_uy = tangent_dy / tangent_len
    normal_ux = -tangent_uy
    normal_uy = tangent_ux

    start_line = {
        "x1": start_x - normal_ux * START_LINE_HALF,
        "y1": start_y - normal_uy * START_LINE_HALF,
        "x2": start_x + normal_ux * START_LINE_HALF,
        "y2": start_y + normal_uy * START_LINE_HALF,
    }
    arrow_base_x = start_x + normal_ux * ARROW_OFFSET
    arrow_base_y = start_y + normal_uy * ARROW_OFFSET
    arrow_tip_x = arrow_base_x + tangent_ux * ARROW_LENGTH
    arrow_tip_y = arrow_base_y + tangent_uy * ARROW_LENGTH
    arrow_shaft_end_x = arrow_tip_x - tangent_ux * (ARROW_HEAD * 0.95)
    arrow_shaft_end_y = arrow_tip_y - tangent_uy * (ARROW_HEAD * 0.95)
    arrow_left_x = arrow_tip_x - tangent_ux * ARROW_HEAD + normal_ux * (ARROW_HEAD * 0.8)
    arrow_left_y = arrow_tip_y - tangent_uy * ARROW_HEAD + normal_uy * (ARROW_HEAD * 0.8)
    arrow_right_x = arrow_tip_x - tangent_ux * ARROW_HEAD - normal_ux * (ARROW_HEAD * 0.8)
    arrow_right_y = arrow_tip_y - tangent_uy * ARROW_HEAD - normal_uy * (ARROW_HEAD * 0.8)

    marker_offset = float(track_config.get("marker_offset", DEFAULT_MARKER_OFFSET))
    spread_hints = {
        key: (float(value[0]), float(value[1]))
        for key, value in track_config.get("marker_spread_hints", {}).items()
    }
    marker_positions = marker_positions_for_turns(turns, sx, sy, marker_offset, spread_hints)
    label_positions = build_label_positions(track_config.get("corner_labels", []), turn_lookup, sx, sy)
    label_positions = resolve_label_collisions(label_positions, marker_positions)

    comparison_centerline_points: list[tuple[float, float]] = []
    comparison_centerline_csv = str(track_config.get("comparison_centerline_csv", "")).strip()
    if comparison_centerline_csv:
        comparison_path = (ROOT / comparison_centerline_csv).resolve()
        if comparison_path.exists():
            comparison_points = load_xy_points(comparison_path)
            if comparison_points:
                if comparison_points[0] == comparison_points[-1]:
                    comparison_points = comparison_points[:-1]
                comparison_origin = Point(
                    (min(point.x for point in comparison_points) + max(point.x for point in comparison_points)) / 2.0,
                    (min(point.y for point in comparison_points) + max(point.y for point in comparison_points)) / 2.0,
                )
                rotated_comparison = rotate_points(comparison_points, comparison_origin, rotation_degrees)
                cmp_min_x = min(point.x for point in rotated_comparison)
                cmp_max_x = max(point.x for point in rotated_comparison)
                cmp_min_y = min(point.y for point in rotated_comparison)
                cmp_max_y = max(point.y for point in rotated_comparison)
                cmp_usable_w = CANVAS_W - 2 * PADDING_X
                cmp_usable_h = CANVAS_H - 2 * PADDING_Y
                cmp_scale = min(cmp_usable_w / (cmp_max_x - cmp_min_x), cmp_usable_h / (cmp_max_y - cmp_min_y))

                def cmp_sx(point: Point) -> float:
                    return (point.x - cmp_min_x) * cmp_scale + PADDING_X

                def cmp_sy(point: Point) -> float:
                    return (cmp_max_y - point.y) * cmp_scale + PADDING_Y

                comparison_centerline_points = [(cmp_sx(point), cmp_sy(point)) for point in rotated_comparison]

    sector_paths = ["sector-path-1", "sector-path-2", "sector-path-3"]
    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS_W}" height="{CANVAS_H}" viewBox="0 0 {CANVAS_W} {CANVAS_H}" fill="none">',
        "  <style>",
        f'    .title {{ font: {style["title_weight"]} {style["title_size"]}px {style["title_font"]}; fill: {style["title_fill"]}; }}',
        f'    .track {{ fill: none; stroke-linejoin: {style["track_join"]}; stroke-linecap: {style["track_cap"]}; }}',
        f'    .sector {{ fill: none; stroke-linejoin: {style["sector_join"]}; stroke-linecap: {style["sector_cap"]}; }}',
        f'    .marker {{ fill: {style["marker_fill"]}; stroke: {style["marker_stroke"]}; stroke-width: 1.5; }}',
        f'    .marker-text {{ font: {style["marker_text_weight"]} {style["marker_text_size"]}px {style["label_font"]}; fill: {style["marker_text"]}; text-anchor: middle; dominant-baseline: middle; }}',
        f'    .label {{ font: {style["label_weight"]} {style["label_size"]}px {style["label_font"]}; fill: {style["label_fill"]}; paint-order: stroke; stroke: {style["label_stroke"]}; stroke-width: {style["label_stroke_w"]}px; stroke-linejoin: round; }}',
        f'    .sector-label {{ font: {style["sector_label_weight"]} {style["sector_label_size"]}px {style["sector_label_font"]}; paint-order: stroke fill; stroke-linejoin: round; stroke-linecap: round; letter-spacing: {style["sector_label_letter_spacing"]}px; }}',
        f'    .start-line-outline {{ stroke: {style["start_line_outer"]}; stroke-width: 8; stroke-linecap: round; }}',
        f'    .start-line-inner {{ stroke: {style["start_line_inner"]}; stroke-width: 4; stroke-linecap: round; }}',
        f'    .arrow-shaft {{ stroke: {style["arrow_color"]}; stroke-width: 4; stroke-linecap: round; }}',
        f'    .arrow-head {{ fill: {style["arrow_color"]}; }}',
        "  </style>",
        f'  <rect width="{CANVAS_W}" height="{CANVAS_H}" fill="{style["bg"]}"/>',
        f'  <text class="title" x="{PADDING_X}" y="62">{title}</text>',
        f'  <path class="track" d="{to_svg_path(full_svg_points)}" stroke="{style["outer"]}" stroke-width="{style["outer_w"]}"/>',
        f'  <path class="track" d="{to_svg_path(full_svg_points)}" stroke="{style["inner"]}" stroke-width="{style["inner_w"]}"/>',
        f'  <path id="{sector_paths[0]}" class="sector" d="{to_svg_path(sector_svg_points[0])}" stroke="{style["s1"]}" stroke-width="{style["sector_w"]}"/>',
        f'  <path id="{sector_paths[1]}" class="sector" d="{to_svg_path(sector_svg_points[1])}" stroke="{style["s2"]}" stroke-width="{style["sector_w"]}"/>',
        f'  <path id="{sector_paths[2]}" class="sector" d="{to_svg_path(sector_svg_points[2])}" stroke="{style["s3"]}" stroke-width="{style["sector_w"]}"/>',
    ]

    if bool(style.get("sector_labels")):
        sector_label_fill_mode = str(style.get("sector_label_fill_mode", "sector")).lower()
        sector_label_stroke = str(style.get("sector_label_stroke", style["bg"]))
        sector_label_stroke_w = float(style.get("sector_label_stroke_w", 5))
        for idx, sector_id in enumerate(sector_paths, start=1):
            fill = style[f"s{idx}"] if sector_label_fill_mode != "fixed" else str(style.get("sector_label_fill", style[f"s{idx}"]))
            svg_parts.append(
                f'  <text class="sector-label" fill="{fill}" stroke="{sector_label_stroke}" stroke-width="{sector_label_stroke_w:g}">'
                f'<textPath href="#{sector_id}" startOffset="50%" text-anchor="middle">SECTOR {idx}</textPath></text>'
            )

    if comparison_centerline_points:
        svg_parts.append(
            f'  <polyline points="{" ".join(f"{x:.2f},{y:.2f}" for x, y in comparison_centerline_points)}" fill="none" stroke="{style["comparison_centerline_color"]}" stroke-width="2"/>'
        )

    for turn in turns:
        x, y = marker_positions[turn.key]
        svg_parts.append(f'  <circle class="marker" cx="{x:.2f}" cy="{y:.2f}" r="{MARKER_R}"/>')
        svg_parts.append(
            f'  <text class="marker-text" x="{x:.2f}" y="{y + 0.5:.2f}">{turn.key}</text>'
        )

    for item in label_positions:
        svg_parts.append(
            f'  <text class="label" x="{item["x"]:.2f}" y="{item["y"]:.2f}" text-anchor="{item.get("anchor", "middle")}">{item["name"]}</text>'
        )

    svg_parts.extend(
        [
            f'  <line class="start-line-outline" x1="{start_line["x1"]:.2f}" y1="{start_line["y1"]:.2f}" x2="{start_line["x2"]:.2f}" y2="{start_line["y2"]:.2f}"/>',
            f'  <line class="start-line-inner" x1="{start_line["x1"]:.2f}" y1="{start_line["y1"]:.2f}" x2="{start_line["x2"]:.2f}" y2="{start_line["y2"]:.2f}"/>',
            f'  <line class="arrow-shaft" x1="{arrow_base_x:.2f}" y1="{arrow_base_y:.2f}" x2="{arrow_shaft_end_x:.2f}" y2="{arrow_shaft_end_y:.2f}"/>',
            f'  <polygon class="arrow-head" points="{arrow_tip_x:.2f},{arrow_tip_y:.2f} {arrow_left_x:.2f},{arrow_left_y:.2f} {arrow_right_x:.2f},{arrow_right_y:.2f}"/>',
            "</svg>",
        ]
    )

    svg_text = "\n".join(svg_parts) + "\n"
    svg_path = track_root / f"{slugify(title)}.svg"
    svg_path.write_text(svg_text)


def marker_positions_for_turns(
    turns: list[Turn],
    sx,
    sy,
    marker_offset: float,
    spread_hints: dict[str, tuple[float, float]],
) -> dict[str, tuple[float, float]]:
    positions: dict[str, list[float]] = {}
    tangents: dict[str, tuple[float, float]] = {}

    for turn in turns:
        angle = math.radians(turn.angle_deg)
        radial_x = math.cos(angle)
        radial_y = -math.sin(angle)
        tangent_x = math.sin(angle)
        tangent_y = math.cos(angle)
        tangents[turn.key] = (tangent_x, tangent_y)
        hint_x, hint_y = spread_hints.get(turn.key, (0.0, 0.0))
        tangent_shift = hint_x * tangent_x + hint_y * tangent_y
        positions[turn.key] = [
            sx(turn.point) + marker_offset * radial_x + tangent_shift * tangent_x,
            sy(turn.point) + marker_offset * radial_y + tangent_shift * tangent_y,
        ]

    min_sep = MARKER_R * 2 + 6
    for _ in range(80):
        moved = False
        for idx, first in enumerate(turns):
            for second in turns[idx + 1 :]:
                p1 = positions[first.key]
                p2 = positions[second.key]
                dx = p2[0] - p1[0]
                dy = p2[1] - p1[1]
                gap = math.hypot(dx, dy)
                if gap >= min_sep:
                    continue
                overlap = min_sep - (gap if gap else 0.01)
                close_on_track = abs(first.track_index - second.track_index) < max(8, len(turns) // 2)
                if close_on_track:
                    t1x, t1y = tangents[first.key]
                    t2x, t2y = tangents[second.key]
                    p1[0] -= t1x * overlap * 0.35
                    p1[1] -= t1y * overlap * 0.35
                    p2[0] += t2x * overlap * 0.35
                    p2[1] += t2y * overlap * 0.35
                else:
                    ux = dx / gap if gap else 1.0
                    uy = dy / gap if gap else 0.0
                    p1[0] -= ux * overlap * 0.5
                    p1[1] -= uy * overlap * 0.5
                    p2[0] += ux * overlap * 0.5
                    p2[1] += uy * overlap * 0.5
                moved = True
        if not moved:
            break

    return {key: (value[0], value[1]) for key, value in positions.items()}


def build_label_positions(label_specs: list[dict[str, Any]], turn_lookup: dict[str, Turn], sx, sy) -> list[dict[str, float | str]]:
    labels = []
    for spec in label_specs:
        selected_turns = [turn_lookup[key] for key in spec.get("turns", []) if key in turn_lookup]
        if not selected_turns:
            continue
        xs = [sx(turn.point) for turn in selected_turns]
        ys = [sy(turn.point) for turn in selected_turns]
        labels.append(
            {
                "name": spec["name"],
                "x": sum(xs) / len(xs) + float(spec.get("dx", 0)),
                "y": sum(ys) / len(ys) + float(spec.get("dy", 0)),
            }
        )
    return labels


def count_label_turns(label_specs: list[dict[str, Any]]) -> int:
    seen = set()
    for spec in label_specs:
        for key in spec.get("turns", []):
            seen.add(str(key))
    return len(seen)


def rect_circle_intersects(
    rect_x: float,
    rect_y: float,
    half_w: float,
    half_h: float,
    circle_x: float,
    circle_y: float,
    radius: float,
) -> bool:
    dx = abs(circle_x - rect_x)
    dy = abs(circle_y - rect_y)
    if dx > half_w + radius or dy > half_h + radius:
        return False
    if dx <= half_w or dy <= half_h:
        return True
    corner_dx = dx - half_w
    corner_dy = dy - half_h
    return corner_dx * corner_dx + corner_dy * corner_dy <= radius * radius


def resolve_label_collisions(
    labels: list[dict[str, float | str]],
    marker_positions: dict[str, tuple[float, float]],
) -> list[dict[str, float | str]]:
    resolved: list[dict[str, float | str]] = []
    marker_items = list(marker_positions.items())
    for label in labels:
        x = float(label["x"])
        y = float(label["y"])
        name = str(label["name"])
        width = max(54.0, len(name) * 8.4 + 12.0)
        half_w = width / 2.0
        half_h = 10.0
        for _ in range(36):
            colliding = None
            best_dist = None
            for key, (mx, my) in marker_items:
                if not rect_circle_intersects(x, y, half_w, half_h, mx, my, MARKER_R + 5):
                    continue
                d = math.hypot(mx - x, my - y)
                if best_dist is None or d < best_dist:
                    best_dist = d
                    colliding = (mx, my)
            if colliding is None:
                break
            mx, my = colliding
            vx = x - mx
            vy = y - my
            length = math.hypot(vx, vy)
            if length < 0.001:
                vx, vy = 0.0, -1.0
                length = 1.0
            target_clearance = MARKER_R + max(half_w, half_h) + 8.0
            push = max(2.0, target_clearance - length)
            x += (vx / length) * push
            y += (vy / length) * push
        resolved.append({"name": name, "x": x, "y": y})
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a broadcast-style F1 circuit SVG from FastF1 data.")
    parser.add_argument("track", help="Track or event name, e.g. Imola, Suzuka, Monza")
    parser.add_argument("--year", type=int, help="Season year to use")
    parser.add_argument("--session", default="Q", help="Session code to use, defaults to Q")
    parser.add_argument("--style", help="Style preset to use, overriding the track config")
    parser.add_argument("--output-root", default=str(ROOT), help="Root directory for generated track folders")
    args = parser.parse_args()

    fastf1 = ensure_fastf1()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(CACHE_DIR))

    config = load_config()
    styles = load_styles()
    if not CONFIG_DIR.exists() or not any(CONFIG_DIR.glob("*.json")):
        save_config(config)
    event_lookup_failed = False
    try:
        season_year, event = best_event_match(fastf1, args.track, args.year)
        event_fields = [
            str(event.get("Location", "")),
            str(event.get("EventName", "")),
            str(event.get("OfficialEventName", "")),
        ]
    except ValueError:
        event_lookup_failed = True
        season_year = args.year or datetime.now().year
        event = {
            "Location": str(args.track),
            "EventName": str(args.track),
            "OfficialEventName": str(args.track),
            "Country": "",
            "RoundNumber": 0,
        }
        event_fields = [str(args.track)]
    track_config = find_track_config(args.track, event_fields, config) or {}
    style_name = str(args.style or track_config.get("style") or DEFAULT_STYLE_NAME).strip() or DEFAULT_STYLE_NAME
    style = resolve_style(style_name, styles)
    title = track_config.get("title") or str(event.get("Location") or args.track).strip()
    folder_name = sanitize_dirname(title)
    output_root = Path(args.output_root).resolve()
    track_root = output_root / folder_name
    track_root.mkdir(parents=True, exist_ok=True)
    svg_name = f"{slugify(title)}.svg"
    rotation_degrees = float(track_config.get("rotation_degrees", 0.0))
    geometry_source = str(track_config.get("geometry_source", "auto")).lower()
    geometry_spec: GeometrySpec | None
    if geometry_source.startswith("osm"):
        geometry_spec = resolve_osm_geometry(title, event_fields, track_config) or resolve_tumftm_geometry(title, event_fields, track_config)
    elif geometry_source == "fastf1":
        geometry_spec = None
    else:
        geometry_spec = resolve_tumftm_geometry(title, event_fields, track_config) or resolve_osm_geometry(title, event_fields, track_config)
    if geometry_spec is not None and not str(track_config.get("geometry_source", "")).strip():
        track_config["geometry_source"] = geometry_spec.kind
        if geometry_spec.centerline_url and not str(track_config.get("centerline_url", "")).strip():
            track_config["centerline_url"] = geometry_spec.centerline_url
        if geometry_spec.raceline_url and not str(track_config.get("raceline_url", "")).strip():
            track_config["raceline_url"] = geometry_spec.raceline_url
        save_config(config)

    if geometry_spec is not None:
        if event_lookup_failed:
            if geometry_spec.centerline_url:
                raw_geometry_points = load_centerline_points(geometry_spec.centerline_url)
                source_text_url = geometry_spec.centerline_url
            elif geometry_spec.raceline_url:
                raw_geometry_points, _, _, _ = load_raceline_points(geometry_spec.raceline_url)
                source_text_url = geometry_spec.raceline_url
            elif geometry_spec.points is not None:
                raw_geometry_points = list(geometry_spec.points)
                source_text_url = None
                if geometry_spec.raw_data is not None:
                    data_file_name = geometry_spec.data_file_name or f"{slugify(title)}_{geometry_spec.kind}.json"
                    (track_root / data_file_name).write_text(json.dumps(geometry_spec.raw_data, indent=2))
            else:
                raise ValueError(f"Track config for '{title}' did not resolve a usable geometry file.")
            if not raw_geometry_points:
                raise ValueError(f"Could not load geometry points for '{title}'.")

            track_points = raw_geometry_points
            if track_points[0] != track_points[-1]:
                track_points = [*track_points, track_points[0]]

            if track_points:
                min_x = min(point.x for point in track_points[:-1])
                max_x = max(point.x for point in track_points[:-1])
                min_y = min(point.y for point in track_points[:-1])
                max_y = max(point.y for point in track_points[:-1])
                rotation_origin = Point((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
            else:
                rotation_origin = Point(0.0, 0.0)

            track_points = rotate_points(track_points, rotation_origin, rotation_degrees)
            target_count = int(track_config.get("turn_count") or max(len(track_config.get("corner_labels", [])), 1) or 1)
            min_sep_override = track_config.get("turn_detection_min_sep")
            turns = build_turns_from_geometry(
                track_points,
                target_count,
                int(min_sep_override) if min_sep_override is not None else None,
            )
            turns = remap_turn_numbers(turns, {str(k): str(v) for k, v in track_config.get("turn_number_remap", {}).items()})
            turn_lookup = {turn.key: turn for turn in turns}

            if source_text_url:
                download_url_text(source_text_url, track_root / Path(source_text_url).name)

            render_geometry_only_track(
                title=title,
                track_root=track_root,
                track_points=track_points,
                turns=turns,
                turn_lookup=turn_lookup,
                track_config=track_config,
                style=style,
                rotation_degrees=rotation_degrees,
                source_label=geometry_spec.source_label or geometry_spec.source_note or geometry_spec.kind,
                source_urls={
                    "centerline": geometry_spec.centerline_url,
                    "raceline": geometry_spec.raceline_url,
                },
                source_data_file_name=geometry_spec.data_file_name,
            )
            print(track_root / f"{slugify(title)}.svg")
            return

        session_type = track_config.get("session_type", args.session)
        session = fastf1.get_session(season_year, str(event.get("EventName")), session_type)
        session.load(weather=False, messages=False)

        fastest = session.laps.pick_fastest()
        pos_df = fastest.get_pos_data()[["SessionTime", "X", "Y"]].copy()
        fastf1_track_points, fastf1_times, fastf1_cum = dedupe_pos_points(pos_df)
        circuit_info = session.get_circuit_info()
        corners_df = circuit_info.corners.copy()
        start_reference_points = list(fastf1_track_points)

        if geometry_spec.centerline_url:
            raw_geometry_points = load_centerline_points(geometry_spec.centerline_url)
            source_text_url = geometry_spec.centerline_url
        elif geometry_spec.raceline_url:
            raw_geometry_points, _, _, _ = load_raceline_points(geometry_spec.raceline_url)
            source_text_url = geometry_spec.raceline_url
        elif geometry_spec.points is not None:
            raw_geometry_points = list(geometry_spec.points)
            source_text_url = None
            if geometry_spec.raw_data is not None:
                data_file_name = geometry_spec.data_file_name or f"{slugify(title)}_{geometry_spec.kind}.json"
                (track_root / data_file_name).write_text(json.dumps(geometry_spec.raw_data, indent=2))
        else:
            raise ValueError(f"Track config for '{title}' did not resolve a usable geometry file.")
        if not raw_geometry_points:
            raise ValueError(f"Could not load geometry points for '{title}'.")

        track_points = raw_geometry_points
        if track_points[0] != track_points[-1]:
            track_points = [*track_points, track_points[0]]

        if fastf1_track_points:
            transform = fit_similarity_transform(fastf1_track_points[:-1], track_points[:-1])
        else:
            transform = SimilarityTransform(Point(0.0, 0.0), Point(0.0, 0.0), 1.0, 0.0)
        if fastf1_track_points:
            start_reference_points = [apply_similarity_transform(point, transform) for point in fastf1_track_points]

        transformed_x = []
        transformed_y = []
        for row in corners_df.itertuples(index=False):
            transformed = apply_similarity_transform(Point(float(row.X), float(row.Y)), transform)
            transformed_x.append(transformed.x)
            transformed_y.append(transformed.y)
        corners_df["X"] = transformed_x
        corners_df["Y"] = transformed_y
        corners_df["Angle"] = corners_df["Angle"].astype(float) + transform.rotation_deg

        cum = cumulative_dist(track_points)
        times = cum[:]
        if track_points:
            min_x = min(point.x for point in track_points[:-1])
            max_x = max(point.x for point in track_points[:-1])
            min_y = min(point.y for point in track_points[:-1])
            max_y = max(point.y for point in track_points[:-1])
            rotation_origin = Point((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
        else:
            rotation_origin = Point(0.0, 0.0)

        track_points = rotate_points(track_points, rotation_origin, rotation_degrees)
        if start_reference_points:
            start_reference_points = rotate_points(start_reference_points, rotation_origin, rotation_degrees)
            track_points = reanchor_closed_loop(track_points, start_reference_points[0])
        if rotation_degrees % 360 != 0:
            rotated_x = []
            rotated_y = []
            for row in corners_df.itertuples(index=False):
                rotated = rotate_point(Point(float(row.X), float(row.Y)), rotation_origin, rotation_degrees)
                rotated_x.append(rotated.x)
                rotated_y.append(rotated.y)
            corners_df["X"] = rotated_x
            corners_df["Y"] = rotated_y
        corners_df["Angle"] = corners_df["Angle"].astype(float) + rotation_degrees

        turns = build_turns(corners_df, track_points)
        turn_lookup = {turn.key: turn for turn in turns}

        if not track_config:
            track_config = autogenerate_track_config(args.track, event, turns, turn_lookup, geometry_spec, style_name)
            config.setdefault("tracks", []).append(track_config)
            save_config(config)

        sector1_time = fastest["Sector1SessionTime"].total_seconds()
        sector2_time = fastest["Sector2SessionTime"].total_seconds()
        split_1 = interpolate_distance_by_time(fastf1_times, fastf1_cum, sector1_time)
        split_2 = interpolate_distance_by_time(fastf1_times, fastf1_cum, sector2_time)
        if fastf1_cum and fastf1_cum[-1] > 0 and cum[-1] > 0:
            split_1 = split_1 * (cum[-1] / fastf1_cum[-1])
            split_2 = split_2 * (cum[-1] / fastf1_cum[-1])
        split_positions = [
            interpolate_track_point(track_points, cum, split_1),
            interpolate_track_point(track_points, cum, split_2),
        ]
        sector_tracks = [
            slice_path(track_points, cum, 0.0, split_1),
            slice_path(track_points, cum, split_1, split_2),
            slice_path(track_points, cum, split_2, cum[-1]),
        ]

        if source_text_url:
            download_url_text(source_text_url, track_root / Path(source_text_url).name)

        turns_export = [
            {
                "turn": turn.key,
                "x": round(turn.point.x, 3),
                "y": round(turn.point.y, 3),
                "angle_deg": round(turn.angle_deg, 3),
            }
            for turn in turns
        ]
        (track_root / "track_turns.json").write_text(json.dumps(turns_export, indent=2))
        source_metadata = {
            "title": title,
            "event": {
                "year": season_year,
                "event_name": str(event.get("EventName")),
                "official_event_name": str(event.get("OfficialEventName")),
                "location": str(event.get("Location")),
                "country": str(event.get("Country")),
                "session_type": session_type,
            },
            "fastest_lap": {
                "driver": fastest["Driver"],
                "lap_time": str(fastest["LapTime"]),
                "sector_1_time": str(fastest["Sector1Time"]),
                "sector_2_time": str(fastest["Sector2Time"]),
                "sector_3_time": str(fastest["Sector3Time"]),
            },
            "sources": [
                geometry_spec.source_label or geometry_spec.source_note or geometry_spec.kind,
                "FastF1 circuit_info corner coordinates and angles for marker placement",
                "Equal-thirds sector splits derived from the centerline geometry",
            ],
            "data_urls": {
                "centerline": geometry_spec.centerline_url,
                "raceline": geometry_spec.raceline_url,
            },
            "data_files": {
                "geometry": geometry_spec.data_file_name,
            },
            "sector_splits": [
                {
                    "sector": 1,
                    "method": "mapped from Sector1SessionTime onto position trace",
                    "distance_along_trace": round(split_1, 3),
                    "point": {"x": round(split_positions[0].x, 3), "y": round(split_positions[0].y, 3)},
                },
                {
                    "sector": 2,
                    "method": "mapped from Sector2SessionTime onto position trace",
                    "distance_along_trace": round(split_2, 3),
                    "point": {"x": round(split_positions[1].x, 3), "y": round(split_positions[1].y, 3)},
                },
            ],
            "config_overrides_used": {
                "track_config_id": track_config.get("id"),
                "style": style.get("name", style_name),
                "rotation_degrees": rotation_degrees,
                "corner_labels": bool(track_config.get("corner_labels")),
                "marker_spread_hints": bool(track_config.get("marker_spread_hints")),
                "geometry_source": geometry_spec.kind,
            },
        }
        (track_root / "source_metadata.json").write_text(json.dumps(source_metadata, indent=2))
    else:
        fastf1 = ensure_fastf1()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fastf1.Cache.enable_cache(str(CACHE_DIR))

        session_type = track_config.get("session_type", args.session)
        session = fastf1.get_session(season_year, str(event.get("EventName")), session_type)
        session.load(weather=False, messages=False)

        fastest = session.laps.pick_fastest()
        pos_df = fastest.get_pos_data()[["SessionTime", "X", "Y"]].copy()
        circuit_info = session.get_circuit_info()
        corners_df = circuit_info.corners.copy()

        track_points, times, cum = dedupe_pos_points(pos_df)
        start_reference_points = list(track_points)
        if track_points:
            min_x = min(point.x for point in track_points)
            max_x = max(point.x for point in track_points)
            min_y = min(point.y for point in track_points)
            max_y = max(point.y for point in track_points)
            rotation_origin = Point((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
        else:
            rotation_origin = Point(0.0, 0.0)

        track_points = rotate_points(track_points, rotation_origin, rotation_degrees)
        start_reference_points = track_points
        corners_df = corners_df.copy()
        if rotation_degrees % 360 != 0:
            rotated_x = []
            rotated_y = []
            for row in corners_df.itertuples(index=False):
                rotated = rotate_point(Point(float(row.X), float(row.Y)), rotation_origin, rotation_degrees)
                rotated_x.append(rotated.x)
                rotated_y.append(rotated.y)
            corners_df["X"] = rotated_x
            corners_df["Y"] = rotated_y
        corners_df["Angle"] = corners_df["Angle"].astype(float) + rotation_degrees

        turns = build_turns(corners_df, track_points)
        turn_lookup = {turn.key: turn for turn in turns}

        if not track_config:
            track_config = autogenerate_track_config(args.track, event, turns, turn_lookup, None, style_name)
            config.setdefault("tracks", []).append(track_config)
            save_config(config)

        sector1_time = fastest["Sector1SessionTime"].total_seconds()
        sector2_time = fastest["Sector2SessionTime"].total_seconds()
        split_1 = interpolate_distance_by_time(times, cum, sector1_time)
        split_2 = interpolate_distance_by_time(times, cum, sector2_time)
        split_positions = [
            interpolate_track_point(track_points, cum, split_1),
            interpolate_track_point(track_points, cum, split_2),
        ]
        sector_tracks = [
            slice_path(track_points, cum, 0.0, split_1),
            slice_path(track_points, cum, split_1, split_2),
            slice_path(track_points, cum, split_2, cum[-1]),
        ]

        with (track_root / "fastf1_fastest_lap_pos.csv").open("w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["session_time_seconds", "x", "y"])
            for point, time_value in zip(track_points[:-1], times[:-1]):
                writer.writerow([f"{time_value:.3f}", f"{point.x:.3f}", f"{point.y:.3f}"])

        corners_df.to_csv(track_root / "fastf1_corners.csv", index=False)

        source_metadata = {
            "title": title,
            "event": {
                "year": season_year,
                "event_name": str(event.get("EventName")),
                "official_event_name": str(event.get("OfficialEventName")),
                "location": str(event.get("Location")),
                "country": str(event.get("Country")),
                "session_type": session_type,
            },
            "fastest_lap": {
                "driver": fastest["Driver"],
                "lap_time": str(fastest["LapTime"]),
                "sector_1_time": str(fastest["Sector1Time"]),
                "sector_2_time": str(fastest["Sector2Time"]),
                "sector_3_time": str(fastest["Sector3Time"]),
            },
            "sources": [
                "FastF1 event schedule resolution",
                "FastF1 fastest-lap position data for circuit geometry",
                "FastF1 circuit_info corner coordinates and angles for marker placement",
                "FastF1 fastest-lap sector times mapped onto the lap trace for timing-sector boundaries",
            ],
            "sector_splits": [
                {
                    "sector": 1,
                    "method": "mapped from Sector1SessionTime onto position trace",
                    "distance_along_trace": round(split_1, 3),
                    "point": {"x": round(split_positions[0].x, 3), "y": round(split_positions[0].y, 3)},
                },
                {
                    "sector": 2,
                    "method": "mapped from Sector2SessionTime onto position trace",
                    "distance_along_trace": round(split_2, 3),
                    "point": {"x": round(split_positions[1].x, 3), "y": round(split_positions[1].y, 3)},
                },
            ],
            "config_overrides_used": {
                "track_config_id": track_config.get("id"),
                "style": style.get("name", style_name),
                "rotation_degrees": rotation_degrees,
                "corner_labels": bool(track_config.get("corner_labels")),
                "marker_spread_hints": bool(track_config.get("marker_spread_hints")),
                "generated_corner_name_candidates": bool(track_config.get("generated_corner_name_candidates")),
            },
        }
        (track_root / "source_metadata.json").write_text(json.dumps(source_metadata, indent=2))

    all_track_points = track_points[:-1]
    min_x = min(point.x for point in all_track_points)
    max_x = max(point.x for point in all_track_points)
    min_y = min(point.y for point in all_track_points)
    max_y = max(point.y for point in all_track_points)
    usable_w = CANVAS_W - 2 * PADDING_X
    usable_h = CANVAS_H - 2 * PADDING_Y
    scale = min(usable_w / (max_x - min_x), usable_h / (max_y - min_y))

    def sx(point: Point) -> float:
        return (point.x - min_x) * scale + PADDING_X

    def sy(point: Point) -> float:
        return (max_y - point.y) * scale + PADDING_Y

    full_svg_points = [(sx(point), sy(point)) for point in track_points]
    sector_svg_points = [[(sx(point), sy(point)) for point in section] for section in sector_tracks]
    start_svg_points = [(sx(point), sy(point)) for point in start_reference_points] if start_reference_points else full_svg_points
    if len(start_svg_points) < 2:
        start_svg_points = full_svg_points

    start_x, start_y = start_svg_points[0]
    next_x, next_y = start_svg_points[1]
    tangent_dx = next_x - start_x
    tangent_dy = next_y - start_y
    tangent_len = math.hypot(tangent_dx, tangent_dy)
    tangent_ux = tangent_dx / tangent_len
    tangent_uy = tangent_dy / tangent_len
    normal_ux = -tangent_uy
    normal_uy = tangent_ux

    start_line = {
        "x1": start_x - normal_ux * START_LINE_HALF,
        "y1": start_y - normal_uy * START_LINE_HALF,
        "x2": start_x + normal_ux * START_LINE_HALF,
        "y2": start_y + normal_uy * START_LINE_HALF,
    }
    arrow_base_x = start_x + normal_ux * ARROW_OFFSET
    arrow_base_y = start_y + normal_uy * ARROW_OFFSET
    arrow_tip_x = arrow_base_x + tangent_ux * ARROW_LENGTH
    arrow_tip_y = arrow_base_y + tangent_uy * ARROW_LENGTH
    arrow_shaft_end_x = arrow_tip_x - tangent_ux * (ARROW_HEAD * 0.95)
    arrow_shaft_end_y = arrow_tip_y - tangent_uy * (ARROW_HEAD * 0.95)
    arrow_left_x = arrow_tip_x - tangent_ux * ARROW_HEAD + normal_ux * (ARROW_HEAD * 0.8)
    arrow_left_y = arrow_tip_y - tangent_uy * ARROW_HEAD + normal_uy * (ARROW_HEAD * 0.8)
    arrow_right_x = arrow_tip_x - tangent_ux * ARROW_HEAD - normal_ux * (ARROW_HEAD * 0.8)
    arrow_right_y = arrow_tip_y - tangent_uy * ARROW_HEAD - normal_uy * (ARROW_HEAD * 0.8)

    marker_offset = float(track_config.get("marker_offset", DEFAULT_MARKER_OFFSET))
    spread_hints = {
        key: (float(value[0]), float(value[1]))
        for key, value in track_config.get("marker_spread_hints", {}).items()
    }
    marker_positions = marker_positions_for_turns(turns, sx, sy, marker_offset, spread_hints)
    label_positions = build_label_positions(track_config.get("corner_labels", []), turn_lookup, sx, sy)
    label_positions = resolve_label_collisions(label_positions, marker_positions)
    debug_centerline = bool(track_config.get("debug_centerline"))
    debug_centerline_width = float(track_config.get("debug_centerline_width", 2.0))
    comparison_centerline_csv = str(track_config.get("comparison_centerline_csv", "")).strip()
    comparison_centerline_points: list[tuple[float, float]] = []
    if comparison_centerline_csv:
        comparison_path = (ROOT / comparison_centerline_csv).resolve()
        if comparison_path.exists():
            comparison_points = load_xy_points(comparison_path)
            if comparison_points:
                if comparison_points[0] == comparison_points[-1]:
                    comparison_points = comparison_points[:-1]
                comparison_origin = Point(
                    (min(point.x for point in comparison_points) + max(point.x for point in comparison_points)) / 2.0,
                    (min(point.y for point in comparison_points) + max(point.y for point in comparison_points)) / 2.0,
                )
                rotated_comparison = rotate_points(comparison_points, comparison_origin, rotation_degrees)
                cmp_min_x = min(point.x for point in rotated_comparison)
                cmp_max_x = max(point.x for point in rotated_comparison)
                cmp_min_y = min(point.y for point in rotated_comparison)
                cmp_max_y = max(point.y for point in rotated_comparison)
                cmp_usable_w = CANVAS_W - 2 * PADDING_X
                cmp_usable_h = CANVAS_H - 2 * PADDING_Y
                cmp_scale = min(cmp_usable_w / (cmp_max_x - cmp_min_x), cmp_usable_h / (cmp_max_y - cmp_min_y))

                def cmp_sx(point: Point) -> float:
                    return (point.x - cmp_min_x) * cmp_scale + PADDING_X

                def cmp_sy(point: Point) -> float:
                    return (cmp_max_y - point.y) * cmp_scale + PADDING_Y

                comparison_centerline_points = [(cmp_sx(point), cmp_sy(point)) for point in rotated_comparison]

    sector_paths = ["sector-path-1", "sector-path-2", "sector-path-3"]
    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS_W}" height="{CANVAS_H}" viewBox="0 0 {CANVAS_W} {CANVAS_H}" fill="none">',
        "  <style>",
        f'    .title {{ font: {style["title_weight"]} {style["title_size"]}px {style["title_font"]}; fill: {style["title_fill"]}; }}',
        f'    .track {{ fill: none; stroke-linejoin: {style["track_join"]}; stroke-linecap: {style["track_cap"]}; }}',
        f'    .sector {{ fill: none; stroke-linejoin: {style["sector_join"]}; stroke-linecap: {style["sector_cap"]}; }}',
        f'    .marker {{ fill: {style["marker_fill"]}; stroke: {style["marker_stroke"]}; stroke-width: 1.5; }}',
        f'    .marker-text {{ font: {style["marker_text_weight"]} {style["marker_text_size"]}px {style["label_font"]}; fill: {style["marker_text"]}; text-anchor: middle; dominant-baseline: middle; }}',
        f'    .label {{ font: {style["label_weight"]} {style["label_size"]}px {style["label_font"]}; fill: {style["label_fill"]}; paint-order: stroke; stroke: {style["label_stroke"]}; stroke-width: {style["label_stroke_w"]}px; stroke-linejoin: round; }}',
        f'    .sector-label {{ font: {style["sector_label_weight"]} {style["sector_label_size"]}px {style["sector_label_font"]}; paint-order: stroke fill; stroke-linejoin: round; stroke-linecap: round; letter-spacing: {style["sector_label_letter_spacing"]}px; }}',
        f'    .start-line-outline {{ stroke: {style["start_line_outer"]}; stroke-width: 8; stroke-linecap: round; }}',
        f'    .start-line-inner {{ stroke: {style["start_line_inner"]}; stroke-width: 4; stroke-linecap: round; }}',
        f'    .arrow-shaft {{ stroke: {style["arrow_color"]}; stroke-width: 4; stroke-linecap: round; }}',
        f'    .arrow-head {{ fill: {style["arrow_color"]}; }}',
        "  </style>",
        f'  <rect width="{CANVAS_W}" height="{CANVAS_H}" fill="{style["bg"]}"/>',
        f'  <text class="title" x="{PADDING_X}" y="62">{title}</text>',
        f'  <path class="track" d="{to_svg_path(full_svg_points)}" stroke="{style["outer"]}" stroke-width="{style["outer_w"]}"/>',
        f'  <path class="track" d="{to_svg_path(full_svg_points)}" stroke="{style["inner"]}" stroke-width="{style["inner_w"]}"/>',
        f'  <path id="{sector_paths[0]}" class="sector" d="{to_svg_path(sector_svg_points[0])}" stroke="{style["s1"]}" stroke-width="{style["sector_w"]}"/>',
        f'  <path id="{sector_paths[1]}" class="sector" d="{to_svg_path(sector_svg_points[1])}" stroke="{style["s2"]}" stroke-width="{style["sector_w"]}"/>',
        f'  <path id="{sector_paths[2]}" class="sector" d="{to_svg_path(sector_svg_points[2])}" stroke="{style["s3"]}" stroke-width="{style["sector_w"]}"/>',
    ]

    if bool(style.get("sector_labels")):
        sector_label_fill_mode = str(style.get("sector_label_fill_mode", "sector")).lower()
        sector_label_stroke = str(style.get("sector_label_stroke", style["bg"]))
        sector_label_stroke_w = float(style.get("sector_label_stroke_w", 5))
        for idx, sector_id in enumerate(sector_paths, start=1):
            fill = style[f"s{idx}"] if sector_label_fill_mode != "fixed" else str(style.get("sector_label_fill", style[f"s{idx}"]))
            svg_parts.append(
                f'  <text class="sector-label" fill="{fill}" stroke="{sector_label_stroke}" stroke-width="{sector_label_stroke_w:g}">'
                f'<textPath href="#{sector_id}" startOffset="50%" text-anchor="middle">SECTOR {idx}</textPath></text>'
            )

    svg_parts.extend(
        [
            f'  <line class="start-line-outline" x1="{start_line["x1"]:.2f}" y1="{start_line["y1"]:.2f}" x2="{start_line["x2"]:.2f}" y2="{start_line["y2"]:.2f}"/>',
            f'  <line class="start-line-inner" x1="{start_line["x1"]:.2f}" y1="{start_line["y1"]:.2f}" x2="{start_line["x2"]:.2f}" y2="{start_line["y2"]:.2f}"/>',
            f'  <line class="arrow-shaft" x1="{arrow_base_x:.2f}" y1="{arrow_base_y:.2f}" x2="{arrow_shaft_end_x:.2f}" y2="{arrow_shaft_end_y:.2f}"/>',
            f'  <polygon class="arrow-head" points="{arrow_tip_x:.2f},{arrow_tip_y:.2f} {arrow_left_x:.2f},{arrow_left_y:.2f} {arrow_right_x:.2f},{arrow_right_y:.2f}"/>',
        ]
    )

    for turn in turns:
        x, y = marker_positions[turn.key]
        svg_parts.append(f'  <circle class="marker" cx="{x:.2f}" cy="{y:.2f}" r="{MARKER_R}"/>')
        svg_parts.append(f'  <text class="marker-text" x="{x:.2f}" y="{y + 0.5:.2f}">{turn.key}</text>')

    for label in label_positions:
        svg_parts.append(f'  <text class="label" x="{label["x"]:.2f}" y="{label["y"]:.2f}">{label["name"]}</text>')

    if debug_centerline:
        svg_parts.append(
            f'  <path d="{to_svg_path(full_svg_points)}" fill="none" stroke="{style["debug_centerline_color"]}" stroke-width="{debug_centerline_width:g}" stroke-linecap="round" stroke-linejoin="round"/>'
        )
    if comparison_centerline_points:
        svg_parts.append(
            f'  <path d="{to_svg_path(comparison_centerline_points)}" fill="none" stroke="{style["comparison_centerline_color"]}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
        )

    svg_parts.append("</svg>")
    svg_path = track_root / svg_name
    svg_path.write_text("\n".join(svg_parts) + "\n")

    print(str(svg_path))


if __name__ == "__main__":
    main()
