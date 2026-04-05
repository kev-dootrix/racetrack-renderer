const SVG_NS = "http://www.w3.org/2000/svg";
const DEFAULT_LABEL_SIZE = 18;
const DEFAULT_MARKER_OFFSET = 37.5;
const MARKER_RADIUS = 18;
const IGNORED_DIRECTORIES = new Set([".git", ".cache", "node_modules", "__pycache__"]);

const state = {
  rootHandle: null,
  tracks: [],
  current: null,
  view: {
    scale: 1,
    panX: 0,
    panY: 0,
  },
  interaction: null,
  selection: null,
};

const elements = {
  connectButton: document.getElementById("connectButton"),
  saveButton: document.getElementById("saveButton"),
  fitButton: document.getElementById("fitButton"),
  selectTitleButton: document.getElementById("selectTitleButton"),
  trackList: document.getElementById("trackList"),
  repoLabel: document.getElementById("repoLabel"),
  trackMeta: document.getElementById("trackMeta"),
  canvasViewport: document.getElementById("canvasViewport"),
  canvasStage: document.getElementById("canvasStage"),
  statusMessage: document.getElementById("statusMessage"),
  selectionLabel: document.getElementById("selectionLabel"),
  zoomLabel: document.getElementById("zoomLabel"),
  emptyInspector: document.getElementById("emptyInspector"),
  markerInspector: document.getElementById("markerInspector"),
  titleInspector: document.getElementById("titleInspector"),
  labelInspector: document.getElementById("labelInspector"),
  markerTurn: document.getElementById("markerTurn"),
  markerX: document.getElementById("markerX"),
  markerY: document.getElementById("markerY"),
  titleVisible: document.getElementById("titleVisible"),
  titleX: document.getElementById("titleX"),
  titleY: document.getElementById("titleY"),
  titleFontFamily: document.getElementById("titleFontFamily"),
  titleFontSize: document.getElementById("titleFontSize"),
  labelName: document.getElementById("labelName"),
  labelAnchor: document.getElementById("labelAnchor"),
  labelX: document.getElementById("labelX"),
  labelY: document.getElementById("labelY"),
  labelFontFamily: document.getElementById("labelFontFamily"),
  labelFontSize: document.getElementById("labelFontSize"),
  resetTitleButton: document.getElementById("resetTitleButton"),
  resetMarkerButton: document.getElementById("resetMarkerButton"),
  resetLabelButton: document.getElementById("resetLabelButton"),
};

function normalizeValue(value) {
  return String(value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "");
}

function median(values) {
  if (!values.length) {
    return 0;
  }
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function setStatus(message) {
  elements.statusMessage.textContent = message;
}

function updateZoomLabel() {
  elements.zoomLabel.textContent = `${Math.round(state.view.scale * 100)}%`;
}

function applyStageTransform() {
  elements.canvasStage.style.transform = `translate(${state.view.panX}px, ${state.view.panY}px) scale(${state.view.scale})`;
  updateZoomLabel();
}

function getSvgCoordinates(clientX, clientY) {
  const rect = elements.canvasViewport.getBoundingClientRect();
  return {
    x: (clientX - rect.left - state.view.panX) / state.view.scale,
    y: (clientY - rect.top - state.view.panY) / state.view.scale,
  };
}

function fitCurrentTrack() {
  if (!state.current) {
    return;
  }
  const { width, height } = state.current.viewBox;
  const viewportWidth = elements.canvasViewport.clientWidth;
  const viewportHeight = elements.canvasViewport.clientHeight;
  const scale = Math.min((viewportWidth - 40) / width, (viewportHeight - 40) / height, 1.8);
  state.view.scale = scale;
  state.view.panX = (viewportWidth - width * scale) / 2;
  state.view.panY = (viewportHeight - height * scale) / 2;
  applyStageTransform();
}

function ensureMarkerOverrideMap() {
  if (!state.current.config.marker_position_overrides || typeof state.current.config.marker_position_overrides !== "object") {
    state.current.config.marker_position_overrides = {};
  }
  return state.current.config.marker_position_overrides;
}

function ensureTitleSettings() {
  if (!state.current.config.title_settings || typeof state.current.config.title_settings !== "object") {
    state.current.config.title_settings = {};
  }
  return state.current.config.title_settings;
}

function ensureLabelSettings() {
  if (!state.current.config.label_settings || typeof state.current.config.label_settings !== "object") {
    state.current.config.label_settings = {};
  }
  return state.current.config.label_settings;
}

function getCurrentMarkers() {
  return state.current.derived.markers;
}

function getCurrentLabels() {
  return state.current.derived.labels;
}

function getCurrentTitle() {
  return state.current.derived.title;
}

function getLegacyLabelSetting(key) {
  const labels = state.current?.config?.corner_labels || [];
  for (const label of labels) {
    if (label && label[key] != null && String(label[key]).trim() !== "") {
      return label[key];
    }
  }
  return null;
}

function getGlobalLabelStyle() {
  const labelSettings = state.current?.config?.label_settings || {};
  const legacyFontFamily = getLegacyLabelSetting("font_family");
  const legacyFontSize = getLegacyLabelSetting("font_size");
  return {
    fontFamily: String(labelSettings.font_family || legacyFontFamily || state.current?.defaultLabelStyle?.fontFamily || ""),
    fontSize: parseFontSizeValue(
      labelSettings.font_size ?? legacyFontSize,
      parseFontSizeValue(state.current?.defaultLabelStyle?.fontSize, state.current?.labelFontSize || DEFAULT_LABEL_SIZE),
    ),
  };
}

function getSelectedMarker() {
  return getCurrentMarkers().find((item) => item.turn === state.selection?.key) || null;
}

function getSelectedLabel() {
  return getCurrentLabels().find((item) => item.index === state.selection?.index) || null;
}

function estimateLabelBounds(name, anchor, fontSize) {
  const lines = String(name || "").split("\n");
  const longest = Math.max(...lines.map((line) => line.length), 0);
  const width = Math.max(54, longest * 8.4 + 12);
  const height = Math.max(20, ((lines.length - 1) * fontSize * 1.05 + fontSize));
  let centerOffsetX = 0;
  if (anchor === "start") {
    centerOffsetX = width / 2;
  } else if (anchor === "end") {
    centerOffsetX = -width / 2;
  }
  return { width, height, centerOffsetX };
}

function parseFontSizeValue(value, fallback) {
  const parsed = Number.parseFloat(String(value || ""));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function rectCircleIntersects(rectX, rectY, halfW, halfH, circleX, circleY, radius) {
  const dx = Math.abs(circleX - rectX);
  const dy = Math.abs(circleY - rectY);
  if (dx > halfW + radius || dy > halfH + radius) {
    return false;
  }
  if (dx <= halfW || dy <= halfH) {
    return true;
  }
  const cornerDx = dx - halfW;
  const cornerDy = dy - halfH;
  return cornerDx * cornerDx + cornerDy * cornerDy <= radius * radius;
}

function buildTrackTransform(trackTurns, svgMarkerPositions) {
  const pairs = trackTurns
    .map((turn) => ({ turn, svg: svgMarkerPositions.get(turn.turn) }))
    .filter((pair) => pair.svg);
  if (pairs.length < 2) {
    return {
      scale: 1,
      tx: 0,
      ty: 0,
    };
  }

  const scaleCandidates = [];
  for (let idx = 0; idx < pairs.length; idx += 1) {
    for (let jdx = idx + 1; jdx < pairs.length; jdx += 1) {
      const first = pairs[idx];
      const second = pairs[jdx];
      const rawDx = second.turn.x - first.turn.x;
      const rawDy = second.turn.y - first.turn.y;
      const svgDx = second.svg.x - first.svg.x;
      const svgDy = second.svg.y - first.svg.y;
      if (Math.abs(rawDx) > 1e-6 && Math.abs(svgDx) > 1e-6) {
        scaleCandidates.push(Math.abs(svgDx / rawDx));
      }
      if (Math.abs(rawDy) > 1e-6 && Math.abs(svgDy) > 1e-6) {
        scaleCandidates.push(Math.abs(-svgDy / rawDy));
      }
    }
  }

  const scale = median(scaleCandidates.filter((value) => Number.isFinite(value) && value > 0)) || 1;
  const tx = median(pairs.map((pair) => pair.svg.x - scale * pair.turn.x));
  const ty = median(pairs.map((pair) => pair.svg.y + scale * pair.turn.y));

  return { scale, tx, ty };
}

function projectTurnToSvg(transform, turn) {
  return {
    x: transform.scale * turn.x + transform.tx,
    y: -transform.scale * turn.y + transform.ty,
  };
}

function buildAutoMarkerPositions(trackTurns, config, transform) {
  const markerOffset = Number(config.marker_offset ?? DEFAULT_MARKER_OFFSET);
  const spreadHints = config.marker_spread_hints || {};
  const positions = new Map();
  const tangents = new Map();

  trackTurns.forEach((turn, index) => {
    const angle = (Number(turn.angle_deg) * Math.PI) / 180;
    const radialX = Math.cos(angle);
    const radialY = -Math.sin(angle);
    const tangentX = Math.sin(angle);
    const tangentY = Math.cos(angle);
    tangents.set(turn.turn, { tangentX, tangentY, index });
    const hint = spreadHints[turn.turn] || [0, 0];
    const hintX = Number(hint[0] || 0);
    const hintY = Number(hint[1] || 0);
    const tangentShift = hintX * tangentX + hintY * tangentY;
    const projected = projectTurnToSvg(transform, turn);
    positions.set(turn.turn, {
      x: projected.x + markerOffset * radialX + tangentShift * tangentX,
      y: projected.y + markerOffset * radialY + tangentShift * tangentY,
    });
  });

  const minSeparation = MARKER_RADIUS * 2 + 6;
  for (let pass = 0; pass < 80; pass += 1) {
    let moved = false;
    for (let idx = 0; idx < trackTurns.length; idx += 1) {
      for (let jdx = idx + 1; jdx < trackTurns.length; jdx += 1) {
        const first = trackTurns[idx];
        const second = trackTurns[jdx];
        const p1 = positions.get(first.turn);
        const p2 = positions.get(second.turn);
        const dx = p2.x - p1.x;
        const dy = p2.y - p1.y;
        const gap = Math.hypot(dx, dy);
        if (gap >= minSeparation) {
          continue;
        }
        const overlap = minSeparation - (gap || 0.01);
        const closeOnTrack = Math.abs(idx - jdx) < Math.max(8, Math.floor(trackTurns.length / 2));
        if (closeOnTrack) {
          const t1 = tangents.get(first.turn);
          const t2 = tangents.get(second.turn);
          p1.x -= t1.tangentX * overlap * 0.35;
          p1.y -= t1.tangentY * overlap * 0.35;
          p2.x += t2.tangentX * overlap * 0.35;
          p2.y += t2.tangentY * overlap * 0.35;
        } else {
          const unitX = gap ? dx / gap : 1;
          const unitY = gap ? dy / gap : 0;
          p1.x -= unitX * overlap * 0.5;
          p1.y -= unitY * overlap * 0.5;
          p2.x += unitX * overlap * 0.5;
          p2.y += unitY * overlap * 0.5;
        }
        moved = true;
      }
    }
    if (!moved) {
      break;
    }
  }

  return positions;
}

function buildDerivedState() {
  const markerOverrides = state.current.config.marker_position_overrides || {};
  const autoMarkerPositions = buildAutoMarkerPositions(state.current.trackTurns, state.current.config, state.current.transform);
  const markers = state.current.trackTurns.map((turn) => {
    const override = markerOverrides[turn.turn];
    const base = autoMarkerPositions.get(turn.turn);
    return {
      turn: turn.turn,
      x: override?.x != null ? Number(override.x) : base.x,
      y: override?.y != null ? Number(override.y) : base.y,
      autoX: base.x,
      autoY: base.y,
    };
  });
  const markerPositionMap = new Map(markers.map((item) => [item.turn, { x: item.x, y: item.y }]));
  const turnLookup = new Map(state.current.trackTurns.map((turn) => [turn.turn, turn]));
  const labels = [];
  const fontSize = state.current.labelFontSize || DEFAULT_LABEL_SIZE;
  const globalLabelStyle = getGlobalLabelStyle();

  (state.current.config.corner_labels || []).forEach((spec, index) => {
    const selectedTurns = (spec.turns || []).map((key) => turnLookup.get(key)).filter(Boolean);
    if (!selectedTurns.length) {
      return;
    }
    const labelStyle = state.current.labelStyleByIndex.get(index) || state.current.defaultLabelStyle;
    const projected = selectedTurns.map((turn) => projectTurnToSvg(state.current.transform, turn));
    const averageX = projected.reduce((sum, point) => sum + point.x, 0) / projected.length;
    const averageY = projected.reduce((sum, point) => sum + point.y, 0) / projected.length;
    const manualPosition = spec.x != null && spec.y != null;
    labels.push({
      index,
      turns: [...(spec.turns || [])],
      name: String(spec.name ?? ""),
      anchor: String(spec.anchor || "middle"),
      fontFamily: globalLabelStyle.fontFamily || String(labelStyle.fontFamily || ""),
      fontWeight: String(spec.font_weight || labelStyle.fontWeight || ""),
      fontSize: globalLabelStyle.fontSize || fontSize,
      x: manualPosition ? Number(spec.x) : averageX + Number(spec.dx || 0),
      y: manualPosition ? Number(spec.y) : averageY + Number(spec.dy || 0),
      manualPosition,
    });
  });

  const resolvedLabels = labels.map((label) => ({ ...label }));
  resolvedLabels.forEach((label) => {
    if (label.manualPosition) {
      return;
    }
    const effectiveFontSize = parseFontSizeValue(label.fontSize, fontSize);
    const { width, height, centerOffsetX } = estimateLabelBounds(label.name, label.anchor, effectiveFontSize);
    const halfW = width / 2;
    const halfH = height / 2;
    for (let pass = 0; pass < 36; pass += 1) {
      let collision = null;
      let bestDistance = Infinity;
      markerPositionMap.forEach((marker) => {
        const rectX = label.x + centerOffsetX;
        if (!rectCircleIntersects(rectX, label.y, halfW, halfH, marker.x, marker.y, MARKER_RADIUS + 5)) {
          return;
        }
        const distance = Math.hypot(marker.x - rectX, marker.y - label.y);
        if (distance < bestDistance) {
          bestDistance = distance;
          collision = marker;
        }
      });
      if (!collision) {
        break;
      }
      let vectorX = label.x - collision.x;
      let vectorY = label.y - collision.y;
      let length = Math.hypot(vectorX, vectorY);
      if (length < 0.001) {
        vectorX = 0;
        vectorY = -1;
        length = 1;
      }
      const clearance = MARKER_RADIUS + Math.max(halfW, halfH) + 8;
      const push = Math.max(2, clearance - length);
      label.x += (vectorX / length) * push;
      label.y += (vectorY / length) * push;
    }
  });

  const titleSettings = state.current.config.title_settings || {};
  const titleDefaults = state.current.titleDefaults;
  const title = {
    text: titleDefaults.text,
    x: titleSettings.x != null ? Number(titleSettings.x) : titleDefaults.x,
    y: titleSettings.y != null ? Number(titleSettings.y) : titleDefaults.y,
    hidden: Boolean(titleSettings.hidden),
    fontFamily: String(titleSettings.font_family || titleDefaults.fontFamily || ""),
    fontWeight: String(titleSettings.font_weight || titleDefaults.fontWeight || ""),
    fontSize: parseFontSizeValue(titleSettings.font_size, parseFontSizeValue(titleDefaults.fontSize, 34)),
  };

  state.current.derived = { markers, labels: resolvedLabels, title };
}

function buildMultilineText(parent, x, y, text) {
  const lines = String(text || "").split("\n");
  if (lines.length <= 1) {
    parent.textContent = lines[0] || "";
    return;
  }
  const fontSize = parseFontSizeValue(parent.style.fontSize, state.current.labelFontSize || DEFAULT_LABEL_SIZE);
  const lineHeight = fontSize * 1.05;
  lines.forEach((line, index) => {
    const tspan = document.createElementNS(SVG_NS, "tspan");
    tspan.setAttribute("x", x.toFixed(2));
    const lineY = y + (index - (lines.length - 1) / 2) * lineHeight;
    tspan.setAttribute("y", lineY.toFixed(2));
    tspan.textContent = line;
    parent.appendChild(tspan);
  });
}

function renderSvg() {
  if (!state.current) {
    return;
  }
  buildDerivedState();

  state.current.titleLayer.replaceChildren();
  state.current.markerLayer.replaceChildren();
  state.current.labelLayer.replaceChildren();

  const title = getCurrentTitle();
  if (!title.hidden) {
    const group = document.createElementNS(SVG_NS, "g");
    group.classList.add("editor-title");
    group.dataset.title = "true";
    if (state.selection?.type === "title") {
      group.classList.add("is-selected");
    }

    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("class", "title");
    text.setAttribute("data-title", "true");
    text.setAttribute("x", title.x.toFixed(2));
    text.setAttribute("y", title.y.toFixed(2));
    if (title.fontFamily) {
      text.style.fontFamily = title.fontFamily;
    }
    if (title.fontWeight) {
      text.style.fontWeight = title.fontWeight;
    }
    if (title.fontSize) {
      text.style.fontSize = `${title.fontSize}px`;
    }
    text.textContent = title.text;
    group.appendChild(text);
    state.current.titleLayer.appendChild(group);
  }

  getCurrentMarkers().forEach((marker) => {
    const group = document.createElementNS(SVG_NS, "g");
    group.classList.add("editor-marker");
    group.dataset.turn = marker.turn;
    if (state.selection?.type === "marker" && state.selection.key === marker.turn) {
      group.classList.add("is-selected");
    }

    const circle = document.createElementNS(SVG_NS, "circle");
    circle.setAttribute("class", "marker");
    circle.setAttribute("data-turn", marker.turn);
    circle.setAttribute("cx", marker.x.toFixed(2));
    circle.setAttribute("cy", marker.y.toFixed(2));
    circle.setAttribute("r", String(MARKER_RADIUS));

    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("class", "marker-text");
    text.setAttribute("data-turn", marker.turn);
    text.setAttribute("x", marker.x.toFixed(2));
    text.setAttribute("y", (marker.y + 0.5).toFixed(2));
    text.textContent = marker.turn;

    group.append(circle, text);
    state.current.markerLayer.appendChild(group);
  });

  getCurrentLabels().forEach((label) => {
    const group = document.createElementNS(SVG_NS, "g");
    group.classList.add("editor-label");
    group.dataset.labelIndex = String(label.index);
    if (state.selection?.type === "label" && state.selection.index === label.index) {
      group.classList.add("is-selected");
    }

    const text = document.createElementNS(SVG_NS, "text");
    text.setAttribute("class", "label");
    text.setAttribute("data-label-index", String(label.index));
    text.setAttribute("x", label.x.toFixed(2));
    text.setAttribute("y", label.y.toFixed(2));
    text.setAttribute("text-anchor", label.anchor);
    if (label.fontFamily) {
      text.style.fontFamily = label.fontFamily;
    }
    if (label.fontWeight) {
      text.style.fontWeight = label.fontWeight;
    }
    if (label.fontSize) {
      text.style.fontSize = `${label.fontSize}px`;
    }
    buildMultilineText(text, label.x, label.y, label.name);

    group.appendChild(text);
    state.current.labelLayer.appendChild(group);
  });

  refreshInspector();
}

function setSelection(selection) {
  state.selection = selection;
  renderSvg();
}

function updateTrackButtons() {
  elements.trackList.innerHTML = "";
  if (!state.tracks.length) {
    elements.trackList.className = "track-list empty-state";
    elements.trackList.textContent = "No generated SVG tracks were found under this repo.";
    return;
  }

  elements.trackList.className = "track-list";
  state.tracks.forEach((track) => {
    const button = document.createElement("button");
    button.className = "track-button";
    if (state.current?.track.id === track.id) {
      button.classList.add("is-active");
    }
    button.type = "button";
    button.innerHTML = `<span class="track-name">${track.displayName}</span><span class="track-path">${track.folderPath}/${track.svgHandle.name}</span>`;
    button.addEventListener("click", () => loadTrack(track));
    elements.trackList.appendChild(button);
  });
}

function refreshInspector() {
  const marker = state.selection?.type === "marker" ? getSelectedMarker() : null;
  const title = state.selection?.type === "title" ? getCurrentTitle() : null;
  const label = state.selection?.type === "label" ? getSelectedLabel() : null;
  const globalLabelStyle = state.current ? getGlobalLabelStyle() : { fontFamily: "", fontSize: "" };

  elements.emptyInspector.classList.toggle("hidden", Boolean(marker || label || title));
  elements.markerInspector.classList.toggle("hidden", !marker);
  elements.titleInspector.classList.toggle("hidden", !title);
  elements.labelInspector.classList.toggle("hidden", !label);

  if (marker) {
    elements.selectionLabel.textContent = `Turn ${marker.turn}`;
    elements.markerTurn.value = marker.turn;
    elements.markerX.value = marker.x.toFixed(2);
    elements.markerY.value = marker.y.toFixed(2);
  } else if (title) {
    elements.selectionLabel.textContent = title.hidden ? "Title (hidden)" : "Track title";
    elements.titleVisible.checked = !title.hidden;
    elements.titleX.value = title.x.toFixed(2);
    elements.titleY.value = title.y.toFixed(2);
    elements.titleFontFamily.value = title.fontFamily;
    elements.titleFontSize.value = title.fontSize ? String(title.fontSize) : "";
  } else if (label) {
    elements.selectionLabel.textContent = `Label ${label.index + 1}`;
    elements.labelName.value = label.name;
    elements.labelAnchor.value = label.anchor;
    elements.labelX.value = label.x.toFixed(2);
    elements.labelY.value = label.y.toFixed(2);
    elements.labelFontFamily.value = globalLabelStyle.fontFamily || "";
    elements.labelFontSize.value = globalLabelStyle.fontSize ? String(globalLabelStyle.fontSize) : "";
  } else {
    elements.selectionLabel.textContent = "Nothing selected";
  }
}

function applyMarkerCoordinates(turnKey, x, y) {
  const overrides = ensureMarkerOverrideMap();
  overrides[turnKey] = {
    x: Number(x.toFixed(2)),
    y: Number(y.toFixed(2)),
  };
  renderSvg();
}

function applyTitleCoordinates(x, y) {
  const titleSettings = ensureTitleSettings();
  titleSettings.x = Number(x.toFixed(2));
  titleSettings.y = Number(y.toFixed(2));
  renderSvg();
}

function applyLabelCoordinates(index, x, y) {
  const spec = state.current.config.corner_labels[index];
  spec.x = Number(x.toFixed(2));
  spec.y = Number(y.toFixed(2));
  renderSvg();
}

function setOptionalTextOverride(target, key, value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) {
    delete target[key];
  } else if (key === "font_weight") {
    const parsed = Number(trimmed);
    if (Number.isFinite(parsed)) {
      target[key] = parsed;
    } else {
      delete target[key];
    }
  } else if (key === "font_size") {
    const parsed = Number(trimmed);
    if (Number.isFinite(parsed) && parsed > 0) {
      target[key] = parsed;
    } else {
      delete target[key];
    }
  } else {
    target[key] = trimmed;
  }
}

function resetTitleSelection() {
  const titleSettings = ensureTitleSettings();
  delete titleSettings.x;
  delete titleSettings.y;
  delete titleSettings.hidden;
  delete titleSettings.font_family;
  delete titleSettings.font_weight;
  delete titleSettings.font_size;
  renderSvg();
}

function resetMarkerSelection() {
  if (state.selection?.type !== "marker") {
    return;
  }
  const overrides = ensureMarkerOverrideMap();
  delete overrides[state.selection.key];
  renderSvg();
}

function resetLabelSelection() {
  if (state.selection?.type !== "label") {
    return;
  }
  const spec = state.current.config.corner_labels[state.selection.index];
  delete spec.x;
  delete spec.y;
  renderSvg();
}

function extractMarkerPositions(svgRoot, trackTurns) {
  const circles = Array.from(svgRoot.querySelectorAll("circle.marker"));
  const markerTexts = Array.from(svgRoot.querySelectorAll("text.marker-text"));
  const positions = new Map();

  circles.forEach((circle, index) => {
    const textNode = markerTexts[index];
    const key =
      circle.dataset.turn ||
      textNode?.dataset.turn ||
      textNode?.textContent?.trim() ||
      trackTurns[index]?.turn;
    if (!key) {
      return;
    }
    positions.set(key, {
      x: Number(circle.getAttribute("cx")),
      y: Number(circle.getAttribute("cy")),
    });
  });

  return positions;
}

function removeEditableNodes(svgRoot) {
  svgRoot
    .querySelectorAll('[data-editor-layer], g.editor-marker, g.editor-label, g.editor-title, circle.marker, text.marker-text, text.label, text.title')
    .forEach((node) => node.remove());
}

function createLayer(svgRoot, layerId) {
  const layer = document.createElementNS(SVG_NS, "g");
  layer.setAttribute("data-editor-layer", layerId);
  svgRoot.appendChild(layer);
  return layer;
}

function parseViewBox(svgRoot) {
  const value = svgRoot.getAttribute("viewBox");
  const parts = value ? value.trim().split(/\s+/).map(Number) : [0, 0, 1240, 860];
  return {
    width: parts[2] || 1240,
    height: parts[3] || 860,
  };
}

function detectTextStyle(node, fallbackFontFamily = "", fallbackFontWeight = "", fallbackFontSize = "") {
  if (!node) {
    return {
      fontFamily: fallbackFontFamily,
      fontWeight: fallbackFontWeight,
      fontSize: fallbackFontSize,
    };
  }
  const computed = window.getComputedStyle(node);
  return {
    fontFamily: computed.fontFamily || fallbackFontFamily,
    fontWeight: computed.fontWeight || fallbackFontWeight,
    fontSize: computed.fontSize || fallbackFontSize,
  };
}

function detectLabelFontSize(svgRoot) {
  const label = svgRoot.querySelector("text.label");
  if (!label) {
    return DEFAULT_LABEL_SIZE;
  }
  const computed = window.getComputedStyle(label);
  const parsed = Number.parseFloat(computed.fontSize);
  return Number.isFinite(parsed) ? parsed : DEFAULT_LABEL_SIZE;
}

function extractTitleState(svgRoot, fallbackTitle) {
  const titleNode = svgRoot.querySelector("text.title");
  const textStyle = detectTextStyle(titleNode, "Inter, Arial, sans-serif", "700", "34px");
  return {
    text: titleNode?.textContent?.trim() || fallbackTitle,
    x: titleNode ? Number(titleNode.getAttribute("x")) : 96,
    y: titleNode ? Number(titleNode.getAttribute("y")) : 62,
    fontFamily: textStyle.fontFamily,
    fontWeight: textStyle.fontWeight,
    fontSize: textStyle.fontSize,
    hidden: false,
  };
}

function extractLabelStyleMap(svgRoot) {
  const styles = new Map();
  const labelNodes = Array.from(svgRoot.querySelectorAll("text.label"));
  labelNodes.forEach((node, index) => {
    const style = detectTextStyle(node, "Inter, Arial, sans-serif", "600", "18px");
    const labelIndex = Number(node.dataset.labelIndex ?? index);
    styles.set(labelIndex, style);
  });
  return styles;
}

async function readJsonFile(handle) {
  const file = await handle.getFile();
  return JSON.parse(await file.text());
}

async function findConfigHandle(rootHandle, track) {
  let configDirectory = null;
  for await (const [name, handle] of rootHandle.entries()) {
    if (name === "track_configs" && handle.kind === "directory") {
      configDirectory = handle;
      break;
    }
  }
  if (!configDirectory) {
    return null;
  }

  const configs = [];
  for await (const [name, handle] of configDirectory.entries()) {
    if (handle.kind !== "file" || !name.endsWith(".json")) {
      continue;
    }
    try {
      const payload = await readJsonFile(handle);
      configs.push({ handle, payload });
    } catch (error) {
      console.warn("Failed to parse config", name, error);
    }
  }

  const trackKeys = new Set([
    normalizeValue(track.displayName),
    normalizeValue(track.folderName),
    normalizeValue(track.svgHandle.name.replace(/\.svg$/i, "")),
  ]);

  let bestMatch = null;
  let bestScore = -1;
  configs.forEach((config) => {
    const payload = config.payload;
    const candidates = [
      payload.title,
      payload.id,
      ...(Array.isArray(payload.match_terms) ? payload.match_terms : []),
    ].map(normalizeValue);
    let score = 0;
    candidates.forEach((candidate) => {
      if (trackKeys.has(candidate)) {
        score += candidate === normalizeValue(payload.title) ? 4 : 2;
      }
    });
    if (score > bestScore) {
      bestScore = score;
      bestMatch = config;
    }
  });

  return bestScore > 0 ? bestMatch : null;
}

async function walkTracks(directoryHandle, pathParts = []) {
  const results = [];
  const entries = [];
  for await (const [name, handle] of directoryHandle.entries()) {
    entries.push([name, handle]);
  }

  const svgFiles = entries
    .filter(([name, handle]) => handle.kind === "file" && name.endsWith(".svg"))
    .map(([, handle]) => handle);
  const turnsHandle = entries.find(([name, handle]) => handle.kind === "file" && name === "track_turns.json")?.[1] || null;

  if (svgFiles.length && turnsHandle) {
    svgFiles.forEach((svgHandle) => {
      const folderPath = pathParts.join("/") || ".";
      const folderName = pathParts[pathParts.length - 1] || svgHandle.name.replace(/\.svg$/i, "");
      const displayName = folderName;
      results.push({
        id: `${folderPath}/${svgHandle.name}`,
        folderName,
        folderPath,
        displayName,
        svgHandle,
        turnsHandle,
        directoryHandle,
      });
    });
  }

  for (const [name, handle] of entries) {
    if (handle.kind !== "directory" || IGNORED_DIRECTORIES.has(name)) {
      continue;
    }
    results.push(...(await walkTracks(handle, [...pathParts, name])));
  }

  return results;
}

async function loadTrack(track) {
  const svgFile = await track.svgHandle.getFile();
  const svgText = await svgFile.text();
  const trackTurns = await readJsonFile(track.turnsHandle);
  const configMatch = await findConfigHandle(state.rootHandle, track);
  if (!configMatch) {
    setStatus(`No matching config JSON was found for ${track.displayName}.`);
    return;
  }

  const parser = new DOMParser();
  const svgDocument = parser.parseFromString(svgText, "image/svg+xml");
  const svgRoot = svgDocument.documentElement;
  elements.canvasStage.replaceChildren(svgRoot);
  const titleDefaults = extractTitleState(svgRoot, configMatch.payload.title || track.displayName);
  const labelFontSize = detectLabelFontSize(svgRoot);
  const defaultLabelStyle = detectTextStyle(svgRoot.querySelector("text.label"), "Inter, Arial, sans-serif", "600");
  const labelStyleByIndex = extractLabelStyleMap(svgRoot);
  const rawMarkerPositions = extractMarkerPositions(svgRoot, trackTurns);
  const transform = buildTrackTransform(trackTurns, rawMarkerPositions);
  const viewBox = parseViewBox(svgRoot);
  svgRoot.setAttribute("width", String(viewBox.width));
  svgRoot.setAttribute("height", String(viewBox.height));

  removeEditableNodes(svgRoot);
  const titleLayer = createLayer(svgRoot, "title");
  const markerLayer = createLayer(svgRoot, "markers");
  const labelLayer = createLayer(svgRoot, "labels");
  elements.canvasStage.style.width = `${viewBox.width}px`;
  elements.canvasStage.style.height = `${viewBox.height}px`;

  state.current = {
    track,
    svgRoot,
    titleLayer,
    markerLayer,
    labelLayer,
    viewBox,
    trackTurns,
    transform,
    titleDefaults,
    labelFontSize,
    defaultLabelStyle,
    labelStyleByIndex,
    configHandle: configMatch.handle,
    config: structuredClone(configMatch.payload),
    derived: {
      title: null,
      markers: [],
      labels: [],
    },
  };

  elements.trackMeta.textContent = `${track.displayName} matched to ${configMatch.handle.name}`;
  state.selection = null;
  updateTrackButtons();
  renderSvg();
  fitCurrentTrack();
  elements.saveButton.disabled = false;
  elements.fitButton.disabled = false;
  elements.selectTitleButton.disabled = false;
  setStatus(`Loaded ${track.displayName}. Drag a marker or label to start tweaking.`);
}

async function loadRepository() {
  if (!window.showDirectoryPicker) {
    setStatus("This browser does not support the File System Access API. Use Chrome or Edge on localhost.");
    return;
  }
  try {
    const rootHandle = await window.showDirectoryPicker({ mode: "readwrite" });
    state.rootHandle = rootHandle;
    state.tracks = await walkTracks(rootHandle);
    state.tracks.sort((first, second) => first.displayName.localeCompare(second.displayName));
    elements.repoLabel.textContent = `Repo: ${rootHandle.name}`;
    updateTrackButtons();
    setStatus(`Found ${state.tracks.length} generated track SVGs. Pick one from the sidebar.`);
  } catch (error) {
    if (error?.name !== "AbortError") {
      console.error(error);
      setStatus("Could not open the repo. Please try again.");
    }
  }
}

function serializeSvg() {
  return `${new XMLSerializer().serializeToString(state.current.svgRoot)}\n`;
}

async function saveCurrentTrack() {
  if (!state.current) {
    return;
  }

  const configWritable = await state.current.configHandle.createWritable();
  await configWritable.write(`${JSON.stringify(state.current.config, null, 2)}\n`);
  await configWritable.close();

  const svgWritable = await state.current.track.svgHandle.createWritable();
  await svgWritable.write(serializeSvg());
  await svgWritable.close();

  setStatus(`Saved ${state.current.track.displayName} config and SVG.`);
}

function handlePointerDown(event) {
  if (!state.current) {
    return;
  }

  const titleNode = event.target.closest("[data-title]");
  const markerNode = event.target.closest("[data-turn]");
  const labelNode = event.target.closest("[data-label-index]");
  if (titleNode) {
    setSelection({ type: "title" });
    state.interaction = {
      type: "title",
      pointerId: event.pointerId,
    };
    elements.canvasViewport.setPointerCapture(event.pointerId);
    return;
  }
  if (markerNode) {
    const turnKey = markerNode.dataset.turn;
    setSelection({ type: "marker", key: turnKey });
    state.interaction = {
      type: "marker",
      key: turnKey,
      pointerId: event.pointerId,
    };
    elements.canvasViewport.setPointerCapture(event.pointerId);
    return;
  }
  if (labelNode) {
    const labelIndex = Number(labelNode.dataset.labelIndex);
    setSelection({ type: "label", index: labelIndex });
    state.interaction = {
      type: "label",
      index: labelIndex,
      pointerId: event.pointerId,
    };
    elements.canvasViewport.setPointerCapture(event.pointerId);
    return;
  }

  state.interaction = {
    type: "pan",
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    startPanX: state.view.panX,
    startPanY: state.view.panY,
  };
  elements.canvasViewport.classList.add("is-panning");
  elements.canvasViewport.setPointerCapture(event.pointerId);
}

function handlePointerMove(event) {
  if (!state.current || !state.interaction) {
    return;
  }
  if (state.interaction.pointerId !== event.pointerId) {
    return;
  }
  if (state.interaction.type === "pan") {
    state.view.panX = state.interaction.startPanX + (event.clientX - state.interaction.startX);
    state.view.panY = state.interaction.startPanY + (event.clientY - state.interaction.startY);
    applyStageTransform();
    return;
  }

  const position = getSvgCoordinates(event.clientX, event.clientY);
  if (state.interaction.type === "title") {
    applyTitleCoordinates(position.x, position.y);
  } else if (state.interaction.type === "marker") {
    applyMarkerCoordinates(state.interaction.key, position.x, position.y);
  } else if (state.interaction.type === "label") {
    applyLabelCoordinates(state.interaction.index, position.x, position.y);
  }
}

function clearInteraction(event) {
  if (state.interaction && state.interaction.pointerId === event.pointerId) {
    state.interaction = null;
    elements.canvasViewport.classList.remove("is-panning");
    elements.canvasViewport.releasePointerCapture(event.pointerId);
  }
}

function handleWheel(event) {
  if (!state.current) {
    return;
  }
  event.preventDefault();
  const rect = elements.canvasViewport.getBoundingClientRect();
  const pointerX = event.clientX - rect.left;
  const pointerY = event.clientY - rect.top;
  const zoomFactor = event.deltaY < 0 ? 1.08 : 0.92;
  const nextScale = clamp(state.view.scale * zoomFactor, 0.3, 8);
  const worldX = (pointerX - state.view.panX) / state.view.scale;
  const worldY = (pointerY - state.view.panY) / state.view.scale;
  state.view.scale = nextScale;
  state.view.panX = pointerX - worldX * nextScale;
  state.view.panY = pointerY - worldY * nextScale;
  applyStageTransform();
}

function nudgeSelection(dx, dy) {
  if (!state.current || !state.selection) {
    return;
  }
  if (state.selection.type === "title") {
    const title = getCurrentTitle();
    if (title) {
      applyTitleCoordinates(title.x + dx, title.y + dy);
    }
  }
  if (state.selection.type === "marker") {
    const marker = getSelectedMarker();
    if (marker) {
      applyMarkerCoordinates(marker.turn, marker.x + dx, marker.y + dy);
    }
  }
  if (state.selection.type === "label") {
    const label = getSelectedLabel();
    if (label) {
      applyLabelCoordinates(label.index, label.x + dx, label.y + dy);
    }
  }
}

function handleKeyDown(event) {
  if (!state.current || !state.selection) {
    return;
  }
  const activeTag = document.activeElement?.tagName;
  if (activeTag === "INPUT" || activeTag === "TEXTAREA" || activeTag === "SELECT") {
    return;
  }

  const step = event.shiftKey ? 10 : 1;
  if (event.key === "ArrowLeft") {
    event.preventDefault();
    nudgeSelection(-step, 0);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    nudgeSelection(step, 0);
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    nudgeSelection(0, -step);
  } else if (event.key === "ArrowDown") {
    event.preventDefault();
    nudgeSelection(0, step);
  } else if (event.key === "Escape") {
    setSelection(null);
  }
}

elements.connectButton.addEventListener("click", loadRepository);
elements.saveButton.addEventListener("click", saveCurrentTrack);
elements.fitButton.addEventListener("click", fitCurrentTrack);
elements.selectTitleButton.addEventListener("click", () => {
  if (state.current) {
    setSelection({ type: "title" });
  }
});
elements.resetTitleButton.addEventListener("click", resetTitleSelection);
elements.resetMarkerButton.addEventListener("click", resetMarkerSelection);
elements.resetLabelButton.addEventListener("click", resetLabelSelection);

elements.markerX.addEventListener("change", () => {
  if (state.selection?.type !== "marker") {
    return;
  }
  const marker = getSelectedMarker();
  applyMarkerCoordinates(marker.turn, Number(elements.markerX.value), marker.y);
});

elements.markerY.addEventListener("change", () => {
  if (state.selection?.type !== "marker") {
    return;
  }
  const marker = getSelectedMarker();
  applyMarkerCoordinates(marker.turn, marker.x, Number(elements.markerY.value));
});

elements.titleVisible.addEventListener("change", () => {
  if (!state.current) {
    return;
  }
  const titleSettings = ensureTitleSettings();
  titleSettings.hidden = !elements.titleVisible.checked;
  renderSvg();
});

elements.titleX.addEventListener("change", () => {
  const title = getCurrentTitle();
  if (!title) {
    return;
  }
  applyTitleCoordinates(Number(elements.titleX.value), title.y);
});

elements.titleY.addEventListener("change", () => {
  const title = getCurrentTitle();
  if (!title) {
    return;
  }
  applyTitleCoordinates(title.x, Number(elements.titleY.value));
});

elements.titleFontFamily.addEventListener("change", () => {
  if (!state.current) {
    return;
  }
  const titleSettings = ensureTitleSettings();
  setOptionalTextOverride(titleSettings, "font_family", elements.titleFontFamily.value);
  renderSvg();
});

elements.titleFontSize.addEventListener("input", () => {
  if (!state.current) {
    return;
  }
  const titleSettings = ensureTitleSettings();
  setOptionalTextOverride(titleSettings, "font_size", elements.titleFontSize.value);
  renderSvg();
});

elements.labelName.addEventListener("input", () => {
  if (state.selection?.type !== "label") {
    return;
  }
  state.current.config.corner_labels[state.selection.index].name = elements.labelName.value;
  renderSvg();
});

elements.labelAnchor.addEventListener("change", () => {
  if (state.selection?.type !== "label") {
    return;
  }
  state.current.config.corner_labels[state.selection.index].anchor = elements.labelAnchor.value;
  renderSvg();
});

elements.labelX.addEventListener("change", () => {
  if (state.selection?.type !== "label") {
    return;
  }
  const label = getSelectedLabel();
  applyLabelCoordinates(label.index, Number(elements.labelX.value), label.y);
});

elements.labelY.addEventListener("change", () => {
  if (state.selection?.type !== "label") {
    return;
  }
  const label = getSelectedLabel();
  applyLabelCoordinates(label.index, label.x, Number(elements.labelY.value));
});

elements.labelFontFamily.addEventListener("input", () => {
  if (!state.current || state.selection?.type !== "label") {
    return;
  }
  const labelSettings = ensureLabelSettings();
  setOptionalTextOverride(labelSettings, "font_family", elements.labelFontFamily.value);
  renderSvg();
});

elements.labelFontSize.addEventListener("input", () => {
  if (!state.current || state.selection?.type !== "label") {
    return;
  }
  const labelSettings = ensureLabelSettings();
  setOptionalTextOverride(labelSettings, "font_size", elements.labelFontSize.value);
  renderSvg();
});

elements.canvasViewport.addEventListener("pointerdown", handlePointerDown);
elements.canvasViewport.addEventListener("pointermove", handlePointerMove);
elements.canvasViewport.addEventListener("pointerup", clearInteraction);
elements.canvasViewport.addEventListener("pointercancel", clearInteraction);
elements.canvasViewport.addEventListener("wheel", handleWheel, { passive: false });
window.addEventListener("keydown", handleKeyDown);
window.addEventListener("resize", () => {
  if (state.current) {
    fitCurrentTrack();
  }
});
