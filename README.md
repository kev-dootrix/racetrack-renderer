# TrackMaker

This project generates presentation-ready SVG circuit maps for motorsport tracks.
It supports multiple geometry sources, per-track layout overrides, and two modes of operation:

- F1-based tracks, where FastF1 provides event/session context and marker placement
- geometry-only tracks, where the circuit is built from a non-F1 centerline source

The current generator is [`generate_track_svg.py`](./generate_track_svg.py).

## What the script does

At a high level, the script:

1. Resolves the track name to a track config entry in [`track_configs/`](./track_configs).
2. Chooses the best available geometry source for that track.
3. Loads or builds the centerline.
4. Places turn markers and corner labels.
5. Adds the start/finish line and direction arrow.
6. Writes the final SVG into a per-track folder.

## Geometry source order

The generator now supports several geometry paths. The exact path depends on the track config.

### 1. `fastf1`

Use the FastF1 fastest-lap position trace as the circuit geometry.

This is the fallback of last resort. It is useful when no better source exists, but it is not ideal for "what the track looks like" because it reflects a racing lap, not a surveyed centerline.

### 2. `track_database`

Use a CSV from the TUMFTM `racetrack-database` repo.

This is the preferred source when the track exists there.

### 3. `f1tenth_racetrack`

Use the centerline/raceline CSVs from the F1TENTH racetrack repo.

### 4. `osm_raceway`

Use OpenStreetMap `highway=raceway` geometry.

This is the preferred fallback when a dedicated racetrack CSV is not available.

### Geometry selection rules

The script currently follows this logic:

- If `geometry_source` is `fastf1`, use FastF1 geometry only.
- If `geometry_source` starts with `osm`, try OpenStreetMap first, then fall back to the dedicated track databases if possible.
- Otherwise, try `track_database` first, then `f1tenth_racetrack`, then OpenStreetMap.
- If no F1 event exists for the requested track, the script switches to geometry-only mode and uses the geometry source without trying to load FastF1 session data.

## What gets used when

### F1-based mode

When the track corresponds to a real F1 event, FastF1 is still used for:

- event and session lookup
- fastest-lap timing data
- sector timing data
- `circuit_info.corners` for turn marker placement

The geometry itself may still come from TUMFTM, F1TENTH, or OSM.
In that case, the FastF1 lap trace is used to transform the corner metadata into the geometry space.

### Geometry-only mode

When no suitable F1 event exists, the generator uses the selected centerline source directly.

In this mode:

- turn markers are inferred from the geometry itself
- labels still come from the track config
- sector splits fall back to equal thirds of the circuit length

This is the mode used for tracks such as Brands Hatch.

## Per-track config

All track-specific behavior lives in [`track_configs/`](./track_configs), with one JSON file per track.

Useful fields include:

- `style`: selects the visual preset to render with
- `geometry_source`: chooses the source family
- `centerline_url` / `raceline_url`: explicit URLs when a track is not auto-discovered
- `rotation_degrees`: rotates the whole layout for better presentation
- `marker_offset`: controls how far turn markers sit from the track edge
- `marker_spread_hints`: per-turn nudges for crowded sections
- `corner_labels`: named corner groupings and label offsets
- `turn_detection_min_sep`: tightens or relaxes geometry-based turn selection
- `comparison_centerline_csv`: optional debug overlay for comparing against another centerline
- `debug_centerline`: draws the main centerline as a 2 px debug overlay

If a track does not already have a config entry and the track maps to an F1 event, the script can create a draft entry.
That draft may use Wikipedia text to seed corner names.
The generated config is meant to be edited by hand and then treated as the source of truth on reruns.

## Track styles

Track appearance lives in [`track_styles/`](./track_styles), with one JSON file per style preset.

The current presets are:

- `default`: the original white-background presentation style
- `broadcast_dark`: the dark broadcast-style render with a black background, colored sector centerline, and curved sector labels

Style resolution works in this order:

1. A `--style` CLI flag, if you pass one.
2. The track config's `style` field.
3. The `default` style preset.

That means you can keep one track config and render it in a different visual treatment just by overriding the style on the command line.

## What gets stored in each track folder

Each generated track gets its own folder under the output root, named after the circuit title.

Example folders:

- `Imola/`
- `Monza/`
- `Brands Hatch/`

### Always written

- `*.svg`: the final rendered circuit map
- `source_metadata.json`: the data sources, event context, and render settings used
- `track_turns.json`: the final numbered turn positions used for the render
- a local copy of the geometry source, when the source comes from a downloadable file or JSON payload

### Written for F1-based tracks

- `fastf1_fastest_lap_pos.csv`: the fastest-lap position trace used for FastF1 geometry or mapping
- `fastf1_corners.csv`: the FastF1 corner metadata used for marker placement

### Written for geometry-only tracks

- no `fastf1_fastest_lap_pos.csv`
- no `fastf1_corners.csv`
- the geometry source file or JSON payload still gets saved locally

## How the SVG is assembled

The final SVG is built in layers:

1. White canvas background.
2. Track title.
3. Outer track ribbon in dark navy.
4. Inner running surface in dark slate.
5. Sector overlays in red, cyan-blue, and yellow.
6. Optional debug/comparison centerline overlays.
7. Circular numbered turn markers.
8. Corner name labels.
9. Start/finish line.
10. Small direction arrow.

The track is scaled to fit the canvas while preserving aspect ratio.
Rounded joins and caps are used for the main ribbon, and labels are drawn last so they sit above the markers.

## Start/finish line and direction arrow

When an F1 session is available, the start/finish marker is anchored to the FastF1 lap-start reference.
The arrow direction follows the lap direction.

In geometry-only mode, the start/finish marker is derived from the geometry layout itself.

## Sector boundaries

Sector boundaries are chosen differently depending on mode:

- F1-based mode: sector 1 and sector 2 are mapped from FastF1 sector timing onto the lap trace.
- Geometry-only mode: sector splits default to equal thirds of the centerline length.

## Example commands

Generate an F1 track:

```bash
python3 generate_track_svg.py Imola --year 2024 --output-root /Users/kevinsmith/Develop/TrackMaker
```

Generate a non-F1 geometry-only track:

```bash
python3 generate_track_svg.py "Brands Hatch" --year 2024 --output-root /Users/kevinsmith/Develop/TrackMaker
```

## Notes

- The `.cache/fastf1/` directory is used for FastF1 session caching and is intentionally not checked in.
- The generated SVGs are intended to be publication-ready, not rough previews.
- If you edit a file in `track_configs/`, rerun the script to regenerate the affected track.
