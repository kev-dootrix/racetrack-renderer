from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path

import fastf1


ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT.parent / ".cache" / "ff1cache"
SESSION_YEAR = 2024
SESSION_NAME = "Emilia Romagna Grand Prix"
SESSION_TYPE = "Q"
TITLE = "Imola"

CANVAS_W = 1240
CANVAS_H = 860
PADDING_X = 96
PADDING_Y = 120

OUTER_W = 34
INNER_W = 24
SECTOR_W = 10
MARKER_R = 18
MARKER_OFFSET = 37.5
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
    "text": "#111111",
    "marker_text": "#ffffff",
}

# FIA circuit map references used for sector boundaries.
SECTOR_SPLIT_RULES = {
    1: {"before_turn": 7, "distance_m": 115},
    2: {"before_turn": 14, "distance_m": 190},
}


@dataclass
class Point:
    x: float
    y: float


def dist(a: Point, b: Point) -> float:
    return math.hypot(b.x - a.x, b.y - a.y)


def cumulative_dist(points: list[Point]) -> list[float]:
    total = [0.0]
    for idx in range(1, len(points)):
        total.append(total[-1] + dist(points[idx - 1], points[idx]))
    return total


def nearest_index(points: list[Point], target: Point) -> int:
    best_idx = 0
    best_dist = float("inf")
    for idx, point in enumerate(points):
        d = (point.x - target.x) ** 2 + (point.y - target.y) ** 2
        if d < best_dist:
            best_dist = d
            best_idx = idx
    return best_idx


def interpolate(points: list[Point], cum: list[float], target: float) -> Point:
    if target <= 0:
        return points[0]
    if target >= cum[-1]:
        return points[-1]
    lo = 0
    hi = len(cum) - 1
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if cum[mid] <= target:
            lo = mid
        else:
            hi = mid
    start = points[lo]
    end = points[lo + 1]
    seg_len = cum[lo + 1] - cum[lo]
    if seg_len == 0:
        return start
    t = (target - cum[lo]) / seg_len
    return Point(
        x=start.x + (end.x - start.x) * t,
        y=start.y + (end.y - start.y) * t,
    )


def slice_path(points: list[Point], cum: list[float], start_d: float, end_d: float) -> list[Point]:
    out = [interpolate(points, cum, start_d)]
    for idx, d in enumerate(cum):
        if start_d < d < end_d:
            out.append(points[idx])
    out.append(interpolate(points, cum, end_d))
    return out


def to_svg_path(points: list[tuple[float, float]], close: bool = False) -> str:
    if not points:
        return ""
    start = f"M {points[0][0]:.2f} {points[0][1]:.2f}"
    segments = [f"L {x:.2f} {y:.2f}" for x, y in points[1:]]
    if close:
        segments.append("Z")
    return " ".join([start, *segments])


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    fastf1.Cache.enable_cache(str(CACHE_DIR))
    session = fastf1.get_session(SESSION_YEAR, SESSION_NAME, SESSION_TYPE)
    session.load(weather=False, messages=False)

    fastest = session.laps.pick_fastest()
    pos_df = fastest.get_pos_data()[["X", "Y"]].copy()
    corners_df = session.get_circuit_info().corners.copy()

    pos_points_track: list[Point] = []
    last = None
    for row in pos_df.itertuples(index=False):
        pt = Point(float(row.X), float(row.Y))
        if last is None or pt.x != last.x or pt.y != last.y:
            pos_points_track.append(pt)
            last = pt
    if pos_points_track[0] != pos_points_track[-1]:
        pos_points_track.append(pos_points_track[0])

    cum = cumulative_dist(pos_points_track)

    corner_points = {
        int(row.Number): Point(float(row.X), float(row.Y))
        for row in corners_df.itertuples(index=False)
    }
    corner_angles = {
        int(row.Number): float(row.Angle)
        for row in corners_df.itertuples(index=False)
    }

    split_distances = []
    split_positions = []
    for sector_no in (1, 2):
        rule = SECTOR_SPLIT_RULES[sector_no]
        turn_index = nearest_index(pos_points_track, corner_points[rule["before_turn"]])
        target = max(0.0, cum[turn_index] - rule["distance_m"])
        split_distances.append(target)
        split_positions.append(interpolate(pos_points_track, cum, target))

    sector_tracks = [
        slice_path(pos_points_track, cum, 0.0, split_distances[0]),
        slice_path(pos_points_track, cum, split_distances[0], split_distances[1]),
        slice_path(pos_points_track, cum, split_distances[1], cum[-1]),
    ]

    with (ROOT / "fastf1_fastest_lap_pos.csv").open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["x", "y"])
        for point in pos_points_track[:-1]:
            writer.writerow([f"{point.x:.3f}", f"{point.y:.3f}"])

    corners_df.to_csv(ROOT / "fastf1_corners.csv", index=False)

    source_metadata = {
        "title": TITLE,
        "session": {
            "year": SESSION_YEAR,
            "grand_prix": SESSION_NAME,
            "session_type": SESSION_TYPE,
            "fastest_lap_driver": fastest["Driver"],
            "fastest_lap_time": str(fastest["LapTime"]),
        },
        "sources": [
            "FastF1 session positioning data for real circuit trace",
            "FastF1 circuit_info corner coordinates and angles for official turn placement",
            "FIA Emilia Romagna Grand Prix circuit map notes for sector split distances",
        ],
        "sector_splits": [
            {
                "sector": 1,
                "split_rule": "115 m before Turn 7",
                "distance_along_trace": round(split_distances[0], 3),
                "point": {
                    "x": round(split_positions[0].x, 3),
                    "y": round(split_positions[0].y, 3),
                },
            },
            {
                "sector": 2,
                "split_rule": "190 m before Turn 14",
                "distance_along_trace": round(split_distances[1], 3),
                "point": {
                    "x": round(split_positions[1].x, 3),
                    "y": round(split_positions[1].y, 3),
                },
            },
        ],
        "named_labels": [
            "Tamburello",
            "Villeneuve",
            "Tosa",
            "Piratella",
            "Acque Minerali",
            "Variante Alta",
            "Rivazza",
        ],
    }
    (ROOT / "source_metadata.json").write_text(json.dumps(source_metadata, indent=2))

    all_track_points = pos_points_track[:-1]
    min_x = min(point.x for point in all_track_points)
    max_x = max(point.x for point in all_track_points)
    min_y = min(point.y for point in all_track_points)
    max_y = max(point.y for point in all_track_points)

    usable_w = CANVAS_W - 2 * PADDING_X
    usable_h = CANVAS_H - 2 * PADDING_Y
    scale = min(usable_w / (max_x - min_x), usable_h / (max_y - min_y))
    extra_left = 12
    extra_top = 6

    def sx(point: Point) -> float:
        return (point.x - min_x) * scale + PADDING_X + extra_left

    def sy(point: Point) -> float:
        return (max_y - point.y) * scale + PADDING_Y + extra_top

    full_svg_points = [(sx(point), sy(point)) for point in pos_points_track]
    sector_svg_points = [
        [(sx(point), sy(point)) for point in section]
        for section in sector_tracks
    ]

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

    marker_spread_hints = {
        1: (0, -4),
        2: (0, 0),
        3: (2, 4),
        4: (-2, 0),
        5: (-4, -2),
        6: (10, 4),
        7: (6, 2),
        8: (-12, -6),
        9: (-10, -2),
        10: (-2, -2),
        11: (-6, 2),
        12: (-8, -2),
        13: (-4, -8),
        14: (-2, -4),
        15: (8, -4),
        16: (2, -4),
        17: (-12, 0),
        18: (-8, -4),
        19: (6, -4),
    }

    marker_positions: dict[int, tuple[float, float]] = {}
    for turn, point in corner_points.items():
        angle = math.radians(corner_angles[turn])
        radial_x = math.cos(angle)
        radial_y = -math.sin(angle)
        tangent_x = math.sin(angle)
        tangent_y = math.cos(angle)
        hint_x, hint_y = marker_spread_hints.get(turn, (0, 0))
        tangent_shift = hint_x * tangent_x + hint_y * tangent_y
        mx = sx(point) + MARKER_OFFSET * radial_x + tangent_shift * tangent_x
        my = sy(point) + MARKER_OFFSET * radial_y + tangent_shift * tangent_y
        marker_positions[turn] = (mx, my)

    label_specs = {
        "Tamburello": {"turns": [1, 2, 3], "dx": 8, "dy": -62},
        "Villeneuve": {"turns": [4, 5, 6], "dx": -72, "dy": -10},
        "Tosa": {"turns": [7], "dx": -18, "dy": 62},
        "Piratella": {"turns": [8, 9], "dx": -52, "dy": -60},
        "Acque Minerali": {"turns": [10, 11, 12, 13], "dx": 82, "dy": 16},
        "Variante Alta": {"turns": [14, 15], "dx": 22, "dy": -66},
        "Rivazza": {"turns": [17, 18], "dx": -116, "dy": -4},
    }

    label_positions = []
    for name, spec in label_specs.items():
        xs = [sx(corner_points[turn]) for turn in spec["turns"]]
        ys = [sy(corner_points[turn]) for turn in spec["turns"]]
        label_positions.append(
            {
                "name": name,
                "x": sum(xs) / len(xs) + spec["dx"],
                "y": sum(ys) / len(ys) + spec["dy"],
            }
        )

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
        f'  <text class="title" x="{PADDING_X}" y="62">{TITLE}</text>',
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

    for label in label_positions:
        svg_parts.append(
            f'  <text class="label" x="{label["x"]:.2f}" y="{label["y"]:.2f}">{label["name"]}</text>'
        )

    for turn in sorted(marker_positions):
        x, y = marker_positions[turn]
        svg_parts.append(f'  <circle class="marker" cx="{x:.2f}" cy="{y:.2f}" r="{MARKER_R}"/>')
        svg_parts.append(f'  <text class="marker-text" x="{x:.2f}" y="{y + 0.5:.2f}">{turn:02d}</text>')

    svg_parts.append("</svg>")
    svg = "\n".join(svg_parts) + "\n"
    (ROOT / "imola.svg").write_text(svg)


if __name__ == "__main__":
    main()
