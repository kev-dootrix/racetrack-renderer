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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".cache" / "fastf1"
CONFIG_PATH = ROOT / "track_configs.json"

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


def load_config() -> dict[str, Any]:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text())
    return {"tracks": []}


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n")


def fetch_json(url: str) -> dict[str, Any]:
    return json.loads(fetch_text(url))


def fetch_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": "TrackMaker/1.0"})
    with urlopen(req, timeout=20) as response:
        return response.read().decode("utf-8")


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
    parser.add_argument("--output-root", default=str(ROOT), help="Root directory for generated track folders")
    args = parser.parse_args()

    fastf1 = ensure_fastf1()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fastf1.Cache.enable_cache(str(CACHE_DIR))

    config = load_config()
    if not CONFIG_PATH.exists():
        save_config(config)
    track_config = find_track_config(args.track, [args.track], config) or {}
    title = track_config.get("title") or args.track.strip()
    folder_name = sanitize_dirname(title)
    output_root = Path(args.output_root).resolve()
    track_root = output_root / folder_name
    track_root.mkdir(parents=True, exist_ok=True)
    svg_name = f"{slugify(title)}.svg"
    rotation_degrees = float(track_config.get("rotation_degrees", 0.0))

    geometry_source = str(track_config.get("geometry_source", "fastf1")).lower()

    if geometry_source != "fastf1":
        raceline_url = str(track_config.get("raceline_url", "")).strip()
        centerline_url = str(track_config.get("centerline_url", "")).strip()
        if not raceline_url:
            raise ValueError(f"Track config for '{title}' is missing raceline_url.")

        raceline_points, _, _, curvatures = load_raceline_points(raceline_url)
        if not raceline_points:
            raise ValueError(f"Could not load raceline points for '{title}'.")

        track_points = raceline_points
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
        turns = build_turns_from_raceline(track_points[:-1], curvatures[:-1], int(track_config.get("turn_count", 0) or len(track_config.get("corner_labels", [])) or 9))
        turn_lookup = {turn.key: turn for turn in turns}

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

        raceline_text = fetch_text(raceline_url)
        (track_root / Path(raceline_url).name).write_text(raceline_text)
        if centerline_url:
            (track_root / Path(centerline_url).name).write_text(fetch_text(centerline_url))
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
            "event": None,
            "fastest_lap": None,
            "sources": [
                "F1TENTH racetrack repository centerline/raceline data",
                "Original F1TENTH racetrack database derived from OpenStreetMap GPS center lines",
            ],
            "data_urls": {
                "raceline": raceline_url,
                "centerline": centerline_url or None,
            },
            "sector_splits": [
                {
                    "sector": 1,
                    "method": "equal thirds of raceline distance",
                    "distance_along_trace": round(split_1, 3),
                    "point": {"x": round(split_positions[0].x, 3), "y": round(split_positions[0].y, 3)},
                },
                {
                    "sector": 2,
                    "method": "equal thirds of raceline distance",
                    "distance_along_trace": round(split_2, 3),
                    "point": {"x": round(split_positions[1].x, 3), "y": round(split_positions[1].y, 3)},
                },
            ],
            "config_overrides_used": {
                "track_config_id": track_config.get("id"),
                "rotation_degrees": rotation_degrees,
                "corner_labels": bool(track_config.get("corner_labels")),
                "marker_spread_hints": bool(track_config.get("marker_spread_hints")),
            },
        }
        (track_root / "source_metadata.json").write_text(json.dumps(source_metadata, indent=2))
    else:
        fastf1 = ensure_fastf1()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        fastf1.Cache.enable_cache(str(CACHE_DIR))

        season_year, event = best_event_match(fastf1, args.track, args.year)

        event_fields = [
            str(event.get("Location", "")),
            str(event.get("EventName", "")),
            str(event.get("OfficialEventName", "")),
        ]
        track_config = find_track_config(args.track, event_fields, config) or {}

        title = track_config.get("title") or str(event.get("Location") or args.track).strip()
        folder_name = sanitize_dirname(title)
        output_root = Path(args.output_root).resolve()
        track_root = output_root / folder_name
        track_root.mkdir(parents=True, exist_ok=True)
        svg_name = f"{slugify(title)}.svg"
        rotation_degrees = float(track_config.get("rotation_degrees", 0.0))

        session_type = track_config.get("session_type", args.session)
        session = fastf1.get_session(season_year, str(event.get("EventName")), session_type)
        session.load(weather=False, messages=False)

        fastest = session.laps.pick_fastest()
        pos_df = fastest.get_pos_data()[["SessionTime", "X", "Y"]].copy()
        circuit_info = session.get_circuit_info()
        corners_df = circuit_info.corners.copy()

        track_points, times, cum = dedupe_pos_points(pos_df)
        if track_points:
            min_x = min(point.x for point in track_points)
            max_x = max(point.x for point in track_points)
            min_y = min(point.y for point in track_points)
            max_y = max(point.y for point in track_points)
            rotation_origin = Point((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
        else:
            rotation_origin = Point(0.0, 0.0)

        track_points = rotate_points(track_points, rotation_origin, rotation_degrees)
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
            track_config = autogenerate_track_config(args.track, event, turns, turn_lookup)
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

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS_W}" height="{CANVAS_H}" viewBox="0 0 {CANVAS_W} {CANVAS_H}" fill="none">',
        "  <style>",
        "    .title { font: 700 34px Inter, Arial, sans-serif; fill: #111111; }",
        "    .track { fill: none; stroke-linejoin: round; stroke-linecap: round; }",
        "    .sector { fill: none; stroke-linejoin: round; stroke-linecap: butt; }",
        "    .marker { fill: #2e3448; stroke: #2e3448; stroke-width: 1.5; }",
        "    .marker-text { font: 700 15px Inter, Arial, sans-serif; fill: #ffffff; text-anchor: middle; dominant-baseline: middle; }",
        "    .label { font: 600 18px Inter, Arial, sans-serif; fill: #111111; paint-order: stroke; stroke: #ffffff; stroke-width: 5px; stroke-linejoin: round; }",
        "    .start-line-outline { stroke: #121528; stroke-width: 8; stroke-linecap: round; }",
        "    .start-line-inner { stroke: #ffffff; stroke-width: 4; stroke-linecap: round; }",
        "    .arrow-shaft { stroke: #121528; stroke-width: 4; stroke-linecap: round; }",
        "    .arrow-head { fill: #121528; }",
        "  </style>",
        f'  <rect width="{CANVAS_W}" height="{CANVAS_H}" fill="{COLORS["bg"]}"/>',
        f'  <text class="title" x="{PADDING_X}" y="62">{title}</text>',
        f'  <path class="track" d="{to_svg_path(full_svg_points)}" stroke="{COLORS["outer"]}" stroke-width="{OUTER_W}"/>',
        f'  <path class="track" d="{to_svg_path(full_svg_points)}" stroke="{COLORS["inner"]}" stroke-width="{INNER_W}"/>',
        f'  <path class="sector" d="{to_svg_path(sector_svg_points[0])}" stroke="{COLORS["s1"]}" stroke-width="{SECTOR_W}"/>',
        f'  <path class="sector" d="{to_svg_path(sector_svg_points[1])}" stroke="{COLORS["s2"]}" stroke-width="{SECTOR_W}"/>',
        f'  <path class="sector" d="{to_svg_path(sector_svg_points[2])}" stroke="{COLORS["s3"]}" stroke-width="{SECTOR_W}"/>',
        f'  <line class="start-line-outline" x1="{start_line["x1"]:.2f}" y1="{start_line["y1"]:.2f}" x2="{start_line["x2"]:.2f}" y2="{start_line["y2"]:.2f}"/>',
        f'  <line class="start-line-inner" x1="{start_line["x1"]:.2f}" y1="{start_line["y1"]:.2f}" x2="{start_line["x2"]:.2f}" y2="{start_line["y2"]:.2f}"/>',
        f'  <line class="arrow-shaft" x1="{arrow_base_x:.2f}" y1="{arrow_base_y:.2f}" x2="{arrow_shaft_end_x:.2f}" y2="{arrow_shaft_end_y:.2f}"/>',
        f'  <polygon class="arrow-head" points="{arrow_tip_x:.2f},{arrow_tip_y:.2f} {arrow_left_x:.2f},{arrow_left_y:.2f} {arrow_right_x:.2f},{arrow_right_y:.2f}"/>',
    ]

    for turn in turns:
        x, y = marker_positions[turn.key]
        svg_parts.append(f'  <circle class="marker" cx="{x:.2f}" cy="{y:.2f}" r="{MARKER_R}"/>')
        svg_parts.append(f'  <text class="marker-text" x="{x:.2f}" y="{y + 0.5:.2f}">{turn.key}</text>')

    for label in label_positions:
        svg_parts.append(f'  <text class="label" x="{label["x"]:.2f}" y="{label["y"]:.2f}">{label["name"]}</text>')

    svg_parts.append("</svg>")
    svg_path = track_root / svg_name
    svg_path.write_text("\n".join(svg_parts) + "\n")

    print(str(svg_path))


if __name__ == "__main__":
    main()
