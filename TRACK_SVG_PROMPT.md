# Formula 1 Circuit SVG Prompt

You are a data visualisation and motorsport graphics expert.

Your task is to generate a high-quality SVG rendering of the Formula 1 circuit at `{track}`, using authoritative circuit geometry and the visual language of the supplied reference image.

The result must be clean, precise, presentation-ready, and suitable for embedding in a professional article or product UI.

The reference image is for art direction and annotation placement behaviour, not for track geometry. Preserve the real circuit shape and proportions.

---

## Inputs

- `{track}` = the circuit name
- `{reference_image}` = a supplied styling reference image
- `{project_root}` = the working directory where files should be saved

Create a new folder at:

- `{project_root}/{track}`

Store in that folder:

- the final SVG
- the source data actually used to construct it
- any small generator script used to produce the SVG
- a concise metadata file documenting sources, assumptions, and sector split interpretation

The final SVG must be written to a file in that folder. Do not print the SVG markup to the chat unless explicitly asked.

---

## Reference Alignment Workflow

- First, derive the circuit from authoritative geometry, not from the reference image.
- If the reference image orientation does not match the authoritative circuit orientation, mentally rotate or align the reference image until it matches.
- Only after alignment, apply the reference image’s:
  - colour scheme
  - track ribbon styling
  - corner marker styling
  - marker placement logic
- Do not distort the real circuit shape to imitate the reference image.

---

## Data & Interpretation

- Use authoritative circuit geometry where available, such as FastF1 circuit info, OpenF1, known GeoJSON track layouts, FIA circuit maps, or other primary motorsport sources.
- Prefer FastF1 positioning data plus FastF1 circuit corner metadata where available.
- Represent the full circuit as a continuous closed polyline or path based on real coordinates.
- Identify:
  - all official turn numbers
  - named corners, grouped where appropriate
  - sector boundaries for 3 sectors
- If exact turn or corner metadata is unavailable, infer conservatively from:
  - track curvature
  - known F1 track maps
  - standard Formula 1 naming conventions
- Record the exact data sources used in a metadata file saved alongside the SVG.

---

## Visual Style

Match the supplied reference image after orientation alignment, but apply the following palette and surface treatment.

### Canvas

- Pure white background: `#ffffff`
- Balanced composition with generous padding
- Maintain the correct aspect ratio of the real circuit
- Include title: `{track}`

### Track Rendering

- Draw the circuit as a smooth continuous line
- Use a layered ribbon treatment:
  - outer underlay: dark navy, for example `#121528`
  - inner track fill or central ribbon: `#2e3448`
  - sector overlay strokes centred on the track
- Use rounded joins and rounded end caps for the main track ribbon
- Sector overlay strokes should be thinner than the base track
- The inner visible running surface of the track must read as `#2e3448` against the white canvas

### Sector Colours

- Sector 1: `#fd0000`
- Sector 2: `#02a5d5`
- Sector 3: `#eecc03`
- Sector transitions must align to real timing sectors
- The sector overlay should sit cleanly on top of the `#2e3448` inner track and remain clearly legible

### Start/Finish And Direction

- Include a start/finish line marker at the real start timing location
- Render it as a short line crossing the track, perpendicular to the local track direction
- Keep the treatment clean and understated so it reads as part of the broadcast-style graphic
- Include a small direction arrow near the start/finish area showing lap direction
- Align the arrow to the local track tangent
- Place the arrow just outside the ribbon so it does not clutter the racing surface
- Make the arrowhead clean and proportionate
- Ensure the arrow shaft terminates under the arrowhead and does not poke through beyond the tip
- Do not add extra icons or labels unless explicitly requested

---

## Turn Markers

For every official turn:

- Render a circular marker positioned directly near the relevant corner
- Fill: `#2e3448`
- Stroke: `#2e3448`, or a very subtle darker outline if needed
- Text should be:
  - white
  - bold
  - centred
  - sans-serif
- Use two-digit turn labels where appropriate, for example `01`, `02`, `03`

### Marker Placement Rules

- Do not use call-out lines or leader lines
- Do not use detached annotation stems of any kind
- Position each turn marker directly outside the relevant corner, aligned to the outside edge of the bend
- The marker should visually hug the outer radius of the corner, as in broadcast-style circuit maps
- Use local track direction and curvature to determine the outside of the corner
- In dense clusters such as chicanes, apply slight local spreading while keeping each marker clearly associated with its correct corner
- Markers must not overlap:
  - the track
  - other markers
  - corner names
  - the title
- Use a consistent radial spacing rule for all turn markers once a good marker distance has been established
- Only use small tangential adjustments where necessary to separate clustered markers without changing the apparent gap from the track

### Important

- Prefer marker placement that feels spatially correct and visually deliberate over mechanically uniform offsets.

---

## Corner Name Labels

- Add named corner labels only when they improve clarity
- Group consecutive turns under one name where appropriate
- Use a clean sans-serif font
- Use black or near-black text, for example `#111111`
- Add a subtle white halo only if needed for readability across coloured sector overlays
- Position labels near the relevant corner cluster, without crossing the track or colliding with turn markers
- If a corner name creates clutter, omit it rather than forcing it in

---

## Typography

- Use Inter, Arial, or a similar clean sans-serif
- Title:
  - black
  - bold
  - clean and understated
- Turn numbers:
  - white
  - bold
  - centred inside `#2e3448` circles
- Corner names:
  - slightly smaller than the title
  - high contrast
  - unobtrusive

---

## Composition Rules

- The final image should feel manually art-directed
- It should resemble a professional motorsport broadcast or telemetry graphic
- Avoid unnecessary decoration
- Do NOT include:
  - legends
  - DRS zones
  - speed traps
  - telemetry icons
  - extra symbols
- Unless explicitly requested

---

## SVG Output Requirements

- Output valid SVG only into the output file
- Keep the SVG structure clean and readable
- Use:
  - `<style>` for shared styling
  - `<polyline>` or `<path>` for the track
  - `<circle>` and `<text>` for turn markers and labels
- Save the final SVG as:
  - `{project_root}/{track}/{track_slug}.svg`

---

## Working Requirements

- Build the geometry from data first, then style it
- Save the intermediate source data that materially affects the output
- Prefer a small reproducible script if the SVG is generated programmatically
- Do a final collision and spacing pass before finishing
- If you need to preview locally, keep that as a temporary workflow step and do not treat screenshots as final outputs
- The final response should briefly confirm which files were created, not dump the SVG markup

---

## Final Quality Checks

Before finishing, verify that:

- the circuit geometry is based on real coordinates
- the background is white
- the inner part of the track is `#2e3448`
- the circular numbered turn markers are filled `#2e3448`
- the turn marker numbers are white
- the colour scheme otherwise matches the intended reference style
- sector colours are red, cyan-blue, and yellow
- the start/finish marker is present and placed at the real start location
- the direction arrow reads clearly and follows lap direction
- the arrow shaft does not protrude beyond the arrowhead
- every turn marker sits on the outside edge of its corner
- turn marker spacing is visually consistent around the track
- there are no call-out lines anywhere
- labels do not overlap each other or the track
- the output files are saved into `{project_root}/{track}`
- the overall result feels deliberate, polished, and presentation-ready

If any placement is ambiguous, prefer clarity over strict local geometric accuracy.

---

## Input Variables

- `{track}` = the circuit name to render
- `{reference_image}` = the art direction reference
- `{project_root}` = the root folder where the track folder should be created
