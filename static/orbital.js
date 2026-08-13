/**
 * Orbit Ghost console — Three.js Earth + residual trails + /orbital/* API.
 * Falls back to a 2D ECI projection when WebGL is unavailable.
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { Line2 } from "three/addons/lines/Line2.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";
import { LineGeometry } from "three/addons/lines/LineGeometry.js";
import {
  aimSatModel,
  cloneSatModel,
  loadSatModels,
  modelsReady,
  pickSatKind,
} from "/static/sat-models.js?v=1";

const EARTH_R = 1.0;
const KM_TO_SCENE = EARTH_R / 6378.137;
const LAND_URL = "https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/land-110m.json";
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const $ = (id) => document.getElementById(id);

// ── DOM ──────────────────────────────────────────────────────────────
const modeBanner = $("mode-banner");
const modeLabel = $("mode-label");
const modeDetail = $("mode-detail");
const modeAge = $("mode-age");
const objectSelect = $("object-select");
const groupSelect = $("group-select");
const dvSlider = $("dv-slider");
const dvValue = $("dv-value");
const nSamples = $("n-samples");
const btnClean = $("btn-clean");
const btnDetect = $("btn-detect");
const btnEval = $("btn-eval");
const btnDemo = $("btn-demo");
const btnField = $("btn-field");
const btnRefresh = $("btn-refresh");
const btnObserve = $("btn-observe");
const oemSource = $("oem-source");
const oemSamples = $("oem-samples");
const observeStatus = $("observe-status");
const frameDisclosure = $("frame-disclosure");
const detectStatus = $("detect-status");
const fieldStatus = $("field-status");
const flagsList = $("flags-list");
const flagsCount = $("flags-count");
const metricsCard = $("metrics-card");
const globeObject = $("globe-object");
const globeHint = $("globe-hint");
const globeBadge = $("globe-fallback-badge");
const chartBlock = $("chart-block");
const chartCanvas = $("residual-chart");
const canvas = $("globe-canvas");
const fallbackCanvas = $("fallback-canvas");
const pane = $("globe-pane");

let currentGroup = "stations";
let fieldObjects = []; // catalog scan results with orbits
let refreshTimer = null;
const REFRESH_MS = 60000;
let lastPipeline = "field"; // field | observe | synthetic

dvSlider.addEventListener("input", () => {
  dvValue.textContent = Number(dvSlider.value).toFixed(1);
});

// ── WebGL probe ──────────────────────────────────────────────────────
function createWebGLRenderer() {
  // Probe before constructing THREE.WebGLRenderer — failed probes log loudly in headless.
  try {
    const probe = document.createElement("canvas");
    const gl =
      probe.getContext("webgl2", { failIfMajorPerformanceCaveat: false }) ||
      probe.getContext("webgl", { failIfMajorPerformanceCaveat: false });
    if (!gl) return null;
  } catch {
    return null;
  }
  try {
    const r = new THREE.WebGLRenderer({
      canvas,
      antialias: true,
      alpha: true,
      powerPreference: "high-performance",
      failIfMajorPerformanceCaveat: false,
    });
    const gl = r.getContext();
    if (!gl) {
      r.dispose();
      return null;
    }
    r.setSize(4, 4, false);
    r.setClearColor(0x000000, 0);
    r.clear();
    return r;
  } catch {
    return null;
  }
}

let renderer = createWebGLRenderer();
let useFallback2D = !renderer;

if (useFallback2D) {
  canvas.style.display = "none";
  fallbackCanvas.hidden = false;
  globeBadge.hidden = false;
  globeHint.textContent = "2D projection · residual exaggerated for readability";
} else {
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 0);
  canvas.addEventListener(
    "webglcontextlost",
    (e) => {
      e.preventDefault();
      useFallback2D = true;
      try {
        renderer?.dispose();
      } catch {
        /* ignore */
      }
      renderer = null;
      canvas.style.display = "none";
      fallbackCanvas.hidden = false;
      globeBadge.hidden = false;
      globeHint.textContent = "2D projection · residual exaggerated for readability";
      drawFallback2D();
    },
    false
  );
}

// ── Three.js scene (only when WebGL ok) ──────────────────────────────
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
camera.position.set(0, 1.2, 3.2);

let controls = null;
if (renderer) {
  controls = new OrbitControls(camera, canvas);
  controls.enableDamping = true;
  controls.dampingFactor = 0.06;
  controls.minDistance = 1.4;
  controls.maxDistance = 8;
  controls.target.set(0, 0, 0);
}

const ambient = new THREE.AmbientLight(0x8899aa, 0.65);
scene.add(ambient);
const key = new THREE.DirectionalLight(0xfff2dd, 1.25);
key.position.set(4, 2, 3);
scene.add(key);

const earthGroup = new THREE.Group();
scene.add(earthGroup);

const earthMat = new THREE.MeshPhongMaterial({
  color: 0x1a2a3a,
  emissive: 0x050a12,
  specular: 0x223344,
  shininess: 12,
});
const earth = new THREE.Mesh(new THREE.SphereGeometry(EARTH_R, 64, 48), earthMat);
earthGroup.add(earth);

const atmo = new THREE.Mesh(
  new THREE.SphereGeometry(EARTH_R * 1.06, 48, 32),
  new THREE.ShaderMaterial({
    uniforms: {
      glowColor: { value: new THREE.Color(0x4a90c8) },
      intensity: { value: 0.5 },
    },
    vertexShader: `
      varying vec3 vNormal;
      varying vec3 vView;
      void main() {
        vNormal = normalize(normalMatrix * normal);
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        vView = normalize(-mv.xyz);
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: `
      uniform vec3 glowColor;
      uniform float intensity;
      varying vec3 vNormal;
      varying vec3 vView;
      void main() {
        float rim = pow(1.0 - abs(dot(vNormal, vView)), 3.0);
        gl_FragColor = vec4(glowColor, rim * intensity);
      }
    `,
    transparent: true,
    side: THREE.BackSide,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  })
);
earthGroup.add(atmo);

let grid = null;
function buildGrid() {
  if (grid) return;
  grid = new THREE.Group();
  const gridMat = new THREE.LineBasicMaterial({
    color: 0x3a4a5a,
    transparent: true,
    opacity: 0.35,
  });
  for (let lat = -60; lat <= 60; lat += 30) {
    const pts = [];
    for (let lon = 0; lon <= 360; lon += 4) {
      pts.push(latLonToVec3(lat, lon, EARTH_R * 1.002));
    }
    grid.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), gridMat));
  }
  for (let lon = 0; lon < 360; lon += 30) {
    const pts = [];
    for (let lat = -90; lat <= 90; lat += 4) {
      pts.push(latLonToVec3(lat, lon, EARTH_R * 1.002));
    }
    grid.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), gridMat));
  }
  earthGroup.add(grid);
}
if (renderer) buildGrid();

if (renderer) {
  (function makeStars() {
    const n = 1400;
    const pos = new Float32Array(n * 3);
    const col = new Float32Array(n * 3);
    const rng = (() => {
      let s = 42;
      return () => (s = (s * 16807) % 2147483647) / 2147483647;
    })();
    for (let i = 0; i < n; i++) {
      const u = rng() * 2 - 1;
      const th = rng() * Math.PI * 2;
      const r = 38 + rng() * 18;
      const sq = Math.sqrt(1 - u * u);
      pos[i * 3] = r * sq * Math.cos(th);
      pos[i * 3 + 1] = r * u;
      pos[i * 3 + 2] = r * sq * Math.sin(th);
      const lum = 0.25 + rng() * 0.5;
      const warm = rng() < 0.12;
      col[i * 3] = lum * (warm ? 1.0 : 0.82);
      col[i * 3 + 1] = lum * 0.86;
      col[i * 3 + 2] = lum * (warm ? 0.78 : 1.0);
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    geo.setAttribute("color", new THREE.BufferAttribute(col, 3));
    scene.add(
      new THREE.Points(
        geo,
        new THREE.PointsMaterial({
          size: 0.09,
          sizeAttenuation: true,
          vertexColors: true,
          transparent: true,
          opacity: 0.8,
          depthWrite: false,
        })
      )
    );
  })();
}

// Orbit trails (selected object residual lab)
let refLine = null;
let obsLine = null;
let residualRibbons = [];
let satMarker = null;
let refSatMarker = null;
let fieldIconGroup = null;

let peakRing = null;
let trailData = [];
let obsVisPts = [];
let playIndex = 0;
let visGain = 1;
const lineMats = [];

// Multi-track field (all catalog objects)
let fieldLineGroup = null;
const fieldOrbitPts = []; // for 2D fallback: [{r_km[], color, flagged}]

function latLonToVec3(lat, lon, r) {
  const phi = THREE.MathUtils.degToRad(90 - lat);
  const theta = THREE.MathUtils.degToRad(lon + 90);
  return new THREE.Vector3(
    -r * Math.sin(phi) * Math.cos(theta),
    r * Math.cos(phi),
    r * Math.sin(phi) * Math.sin(theta)
  );
}

function rKmToVec3(rKm) {
  return new THREE.Vector3(rKm[0], rKm[2], rKm[1]).multiplyScalar(KM_TO_SCENE);
}

function makeLine2(flatPts, color, widthPx, opacity) {
  const geo = new LineGeometry();
  geo.setPositions(flatPts);
  const mat = new LineMaterial({
    color,
    linewidth: widthPx,
    transparent: true,
    opacity,
    worldUnits: false,
  });
  mat.resolution.set(pane.clientWidth || 1, pane.clientHeight || 1);
  lineMats.push(mat);
  return new Line2(geo, mat);
}

const iconTexCache = new Map();

function canvasTex(draw, size = 128) {
  const c = document.createElement("canvas");
  c.width = size;
  c.height = size;
  const ctx = c.getContext("2d");
  draw(ctx, size);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.needsUpdate = true;
  return tex;
}

/** ISS pictogram: core + long arrays. Same language as the public Three.js trackers. */
function drawIssIcon(ctx, s, fill, stroke) {
  const cx = s / 2;
  const cy = s / 2;
  ctx.clearRect(0, 0, s, s);
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  // arrays
  ctx.fillStyle = fill;
  ctx.strokeStyle = stroke;
  ctx.lineWidth = s * 0.035;
  const wingW = s * 0.42;
  const wingH = s * 0.11;
  ctx.fillRect(cx - wingW, cy - wingH * 2.15, wingW * 0.78, wingH);
  ctx.fillRect(cx - wingW, cy + wingH * 1.15, wingW * 0.78, wingH);
  ctx.fillRect(cx + wingW * 0.22, cy - wingH * 2.15, wingW * 0.78, wingH);
  ctx.fillRect(cx + wingW * 0.22, cy + wingH * 1.15, wingW * 0.78, wingH);
  ctx.strokeRect(cx - wingW, cy - wingH * 2.15, wingW * 0.78, wingH);
  ctx.strokeRect(cx - wingW, cy + wingH * 1.15, wingW * 0.78, wingH);
  ctx.strokeRect(cx + wingW * 0.22, cy - wingH * 2.15, wingW * 0.78, wingH);
  ctx.strokeRect(cx + wingW * 0.22, cy + wingH * 1.15, wingW * 0.78, wingH);
  // truss
  ctx.fillRect(cx - s * 0.42, cy - s * 0.025, s * 0.84, s * 0.05);
  // modules
  ctx.fillRect(cx - s * 0.08, cy - s * 0.16, s * 0.16, s * 0.32);
  ctx.strokeRect(cx - s * 0.08, cy - s * 0.16, s * 0.16, s * 0.32);
}

/** Classic box + solar wings used by most OSS satellite trackers. */
function drawSatIcon(ctx, s, fill, stroke) {
  const cx = s / 2;
  const cy = s / 2;
  ctx.clearRect(0, 0, s, s);
  ctx.lineJoin = "round";
  ctx.strokeStyle = stroke;
  ctx.fillStyle = fill;
  ctx.lineWidth = s * 0.045;
  const body = s * 0.18;
  ctx.fillRect(cx - body / 2, cy - body / 2, body, body);
  ctx.strokeRect(cx - body / 2, cy - body / 2, body, body);
  const w = s * 0.28;
  const h = s * 0.14;
  ctx.fillRect(cx - body / 2 - w - 2, cy - h / 2, w, h);
  ctx.strokeRect(cx - body / 2 - w - 2, cy - h / 2, w, h);
  ctx.fillRect(cx + body / 2 + 2, cy - h / 2, w, h);
  ctx.strokeRect(cx + body / 2 + 2, cy - h / 2, w, h);
}

function iconTexture(kind, tone) {
  const key = `${kind}:${tone}`;
  if (iconTexCache.has(key)) return iconTexCache.get(key);
  const pal = {
    nasa: { fill: "rgba(232, 196, 96, 0.95)", stroke: "rgba(40, 32, 12, 0.95)" },
    catalog: { fill: "rgba(130, 178, 214, 0.95)", stroke: "rgba(16, 28, 40, 0.95)" },
    field: { fill: "rgba(168, 184, 196, 0.92)", stroke: "rgba(20, 28, 36, 0.95)" },
    stale: { fill: "rgba(196, 120, 88, 0.95)", stroke: "rgba(40, 18, 12, 0.95)" },
    select: { fill: "rgba(150, 200, 230, 0.96)", stroke: "rgba(12, 24, 36, 0.95)" },
  }[tone] || { fill: "#ccc", stroke: "#111" };
  const tex = canvasTex((ctx, s) => {
    if (kind === "iss") drawIssIcon(ctx, s, pal.fill, pal.stroke);
    else drawSatIcon(ctx, s, pal.fill, pal.stroke);
  }, 128);
  iconTexCache.set(key, tex);
  return tex;
}

function makeSatSprite(kind, tone, scale) {
  const spr = new THREE.Sprite(
    new THREE.SpriteMaterial({
      map: iconTexture(kind, tone),
      transparent: true,
      depthTest: true,
      depthWrite: false,
      sizeAttenuation: true,
    })
  );
  spr.scale.setScalar(scale);
  spr.userData.kind = kind;
  spr.userData.keepMap = true;
  return spr;
}

function ringSpriteTexture() {
  const s = 64;
  const c = document.createElement("canvas");
  c.width = s;
  c.height = s;
  const ctx = c.getContext("2d");
  ctx.strokeStyle = "rgba(224, 80, 64, 0.95)";
  ctx.lineWidth = 4;
  ctx.beginPath();
  ctx.arc(s / 2, s / 2, 20, 0, Math.PI * 2);
  ctx.stroke();
  ctx.fillStyle = "rgba(224, 80, 64, 0.9)";
  ctx.beginPath();
  ctx.arc(s / 2, s / 2, 3.5, 0, Math.PI * 2);
  ctx.fill();
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function dropObject3d(obj) {
  if (!obj) return;
  earthGroup.remove(obj);
  obj.traverse((o) => {
    o.geometry?.dispose();
    const mats = o.material
      ? Array.isArray(o.material)
        ? o.material
        : [o.material]
      : [];
    for (const m of mats) {
      if (!o.userData?.keepMap && !obj.userData?.isSatModel) m.map?.dispose();
      m.dispose?.();
    }
  });
}

function clearTrails() {
  dropObject3d(refLine);
  refLine = null;
  dropObject3d(obsLine);
  obsLine = null;
  for (const g of residualRibbons) dropObject3d(g);
  residualRibbons = [];
  dropObject3d(satMarker);
  satMarker = null;
  dropObject3d(refSatMarker);
  refSatMarker = null;
  dropObject3d(peakRing);
  peakRing = null;
  // keep field lines; residual lab is overlay
  lineMats.length = 0;
  obsVisPts = [];
}

function clearFieldLines() {
  if (fieldLineGroup) {
    earthGroup.remove(fieldLineGroup);
    fieldLineGroup.traverse((o) => {
      o.geometry?.dispose();
      if (o.material) {
        if (Array.isArray(o.material)) o.material.forEach((m) => m.dispose());
        else {
          if (!o.userData?.keepMap) o.material.map?.dispose();
          o.material.dispose();
        }
      }
    });
    fieldLineGroup = null;
  }
  fieldOrbitPts.length = 0;
}

function setFieldOrbits(objects) {
  clearFieldLines();
  fieldObjects = objects || [];
  if (!fieldObjects.length) {
    if (useFallback2D) drawFallback2D();
    return;
  }

  fieldLineGroup = new THREE.Group();
  const selectedId = objectSelect.value ? Number(objectSelect.value) : null;

  for (const o of fieldObjects) {
    const orbit = o.orbit;
    if (!orbit || orbit.length < 2) continue;
    const pts = [];
    const scenePts = [];
    for (const p of orbit) {
      if (!p.r_km) continue;
      const v = rKmToVec3(p.r_km);
      pts.push(v.x, v.y, v.z);
      scenePts.push(v);
    }
    if (pts.length < 6) continue;

    const isSel = selectedId != null && o.norad_id === selectedId;
    const flagged = !!o.flagged;
    let color = 0x3a5568;
    let opacity = 0.35;
    let width = 1.0;
    if (flagged) {
      color = o.custody_tier === "stale" ? 0xc06050 : 0xc0a050;
      opacity = 0.75;
      width = 1.4;
    }
    if (isSel) {
      color = 0x7fb8e0;
      opacity = 0.9;
      width = 2.0;
    }

    fieldOrbitPts.push({
      pts: scenePts,
      color,
      flagged,
      selected: isSel,
      name: o.name,
    });

    if (renderer && !useFallback2D) {
      // Simple Line for field (cheaper than Line2 at N=300)
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
      const mat = new THREE.LineBasicMaterial({
        color,
        transparent: true,
        opacity,
        depthWrite: false,
      });
      fieldLineGroup.add(new THREE.Line(geo, mat));

      const kind = pickSatKind(o.name, o.norad_id);
      const isIss = kind === "iss";
      const tint = isSel ? 0x7fb8e0 : o.flagged ? 0xc47858 : null;
      const mesh =
        cloneSatModel(kind, {
          tintHex: tint,
          scale: isIss ? 0.95 : isSel ? 0.85 : 0.7,
        }) ||
        makeSatSprite(
          isIss ? "iss" : "sat",
          isSel ? "select" : o.flagged ? "stale" : isIss ? "nasa" : "field",
          isIss ? 0.078 : 0.042
        );
      const ahead = scenePts[1] || scenePts[0];
      aimSatModel(mesh, scenePts[0], ahead);
      fieldLineGroup.add(mesh);
    }
  }

  if (renderer && !useFallback2D && fieldLineGroup) {
    earthGroup.add(fieldLineGroup);
  }
  if (useFallback2D) drawFallback2D();
  setHint();
}

function setHint() {
  const n = fieldObjects.length;
  const base = n ? `${n} tracks` : "no field";
  const lg = $("leg-gain");
  if (lg) {
    lg.textContent =
      visGain > 1.5
        ? `gap drawn ${Math.round(visGain)}× so you can see it`
        : "";
  }
  if (useFallback2D) {
    globeHint.textContent =
      visGain > 1.5
        ? `2D · gold NASA / blue catalog · gap ×${Math.round(visGain)}`
        : `2D · gold NASA / blue catalog`;
    return;
  }
  if (!trailData.length) {
    globeHint.textContent = `Drag Earth · gold is NASA, blue is the catalog`;
    return;
  }
  globeHint.textContent =
    visGain > 1.5
      ? `Drag Earth · gold NASA / blue catalog · gap drawn ${Math.round(visGain)}×`
      : `Drag Earth · gold NASA / blue catalog`;
}

function computeVisGain(trail) {
  const maxRes = trail.reduce((m, p) => Math.max(m, p.residual_km), 0);
  const targetOffset = 0.14;
  return maxRes > 1e-9 ? Math.max(1, targetOffset / (maxRes * KM_TO_SCENE)) : 1;
}

function setTrails(trail) {
  clearTrails();
  trailData = trail || [];
  playIndex = 0;
  if (!trailData.length) {
    visGain = 1;
    setHint();
    if (useFallback2D) drawFallback2D();
    return;
  }

  visGain = computeVisGain(trailData);

  const refPts = [];
  const obsPts = [];
  for (const p of trailData) {
    const a = rKmToVec3(p.ref.r_km);
    const b = rKmToVec3(p.obs.r_km);
    const bo = a.clone().lerp(b, visGain);
    refPts.push(a.x, a.y, a.z);
    obsPts.push(bo.x, bo.y, bo.z);
    obsVisPts.push(bo);
  }

  if (renderer && !useFallback2D) {
    refLine = makeLine2(refPts, 0x7fb8e0, 1.4, 0.75);
    earthGroup.add(refLine);
    obsLine = makeLine2(obsPts, 0xe0b050, 2.2, 0.95);
    earthGroup.add(obsLine);

    const maxRes = trailData.reduce((m, p) => Math.max(m, p.residual_km), 0);
    const step = Math.max(1, Math.ceil(trailData.length / 32));
    for (let i = 0; i < trailData.length; i += step) {
      const p = trailData[i];
      if (p.residual_km < 1e-4) continue;
      const a = rKmToVec3(p.ref.r_km);
      const b = obsVisPts[i];
      const t = Math.min(1, p.residual_km / Math.max(maxRes, 0.5));
      const col = new THREE.Color().setHSL(0.08 - t * 0.08, 0.85, 0.45 + t * 0.15);
      const line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([a, b]),
        new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: 0.3 + t * 0.5 })
      );
      residualRibbons.push(line);
      earthGroup.add(line);
    }

    let peakIdx = 0;
    for (let i = 0; i < trailData.length; i++) {
      if (trailData[i].residual_km > trailData[peakIdx].residual_km) peakIdx = i;
    }
    if (trailData[peakIdx].residual_km > 0.01) {
      peakRing = new THREE.Sprite(
        new THREE.SpriteMaterial({ map: ringSpriteTexture(), transparent: true, depthTest: true })
      );
      peakRing.scale.setScalar(0.1);
      peakRing.position.copy(obsVisPts[peakIdx]);
      earthGroup.add(peakRing);
    }

    const ahead =
      obsVisPts.length > 1 ? obsVisPts[1] : obsVisPts[0].clone().add(new THREE.Vector3(0.01, 0, 0));
    satMarker =
      cloneSatModel("iss", { scale: 1.15 }) || makeSatSprite("iss", "nasa", 0.095);
    aimSatModel(satMarker, obsVisPts[0], ahead);
    earthGroup.add(satMarker);

    const ref0 = rKmToVec3(trailData[0].ref.r_km);
    const ref1 =
      trailData.length > 1 ? rKmToVec3(trailData[1].ref.r_km) : ref0.clone().add(new THREE.Vector3(0.01, 0, 0));
    refSatMarker =
      cloneSatModel("iss", { tintHex: 0x4a90c8, scale: 0.78 }) ||
      makeSatSprite("sat", "catalog", 0.062);
    aimSatModel(refSatMarker, ref0, ref1);
    earthGroup.add(refSatMarker);
  }

  setHint();
  if (useFallback2D) drawFallback2D();
}

// ── 2D fallback (ECI XZ orthographic) ────────────────────────────────
function drawFallback2D() {
  if (!useFallback2D) return;
  const w = pane.clientWidth || 1;
  const h = pane.clientHeight || 1;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  fallbackCanvas.width = w * dpr;
  fallbackCanvas.height = h * dpr;
  const ctx = fallbackCanvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  ctx.fillStyle = "#0f141c";
  ctx.fillRect(0, 0, w, h);

  // soft vignette via flat rings (no CSS gradient rule; canvas ok)
  const cx = w * 0.48;
  const cy = h * 0.5;
  const earthPx = Math.min(w, h) * 0.28;

  // stars
  let s = 99;
  const rnd = () => (s = (s * 16807) % 2147483647) / 2147483647;
  for (let i = 0; i < 180; i++) {
    const sx = rnd() * w;
    const sy = rnd() * h;
    const a = 0.15 + rnd() * 0.45;
    ctx.fillStyle = `rgba(200, 210, 230, ${a})`;
    ctx.fillRect(sx, sy, 1 + (rnd() > 0.85 ? 1 : 0), 1);
  }

  // Earth disc
  ctx.beginPath();
  ctx.arc(cx, cy, earthPx, 0, Math.PI * 2);
  ctx.fillStyle = "#1a2836";
  ctx.fill();
  ctx.strokeStyle = "rgba(90, 130, 160, 0.45)";
  ctx.lineWidth = 1.5;
  ctx.stroke();

  // lat bands
  ctx.strokeStyle = "rgba(90, 110, 130, 0.22)";
  ctx.lineWidth = 1;
  for (const f of [-0.6, -0.3, 0, 0.3, 0.6]) {
    const ry = earthPx * Math.sqrt(Math.max(0, 1 - f * f));
    ctx.beginPath();
    ctx.ellipse(cx, cy + earthPx * f * 0.15, ry, earthPx * 0.12, 0, 0, Math.PI * 2);
    ctx.stroke();
  }

  // limb ring
  ctx.beginPath();
  ctx.arc(cx, cy, earthPx * 1.04, 0, Math.PI * 2);
  ctx.strokeStyle = "rgba(74, 144, 200, 0.28)";
  ctx.lineWidth = 6;
  ctx.stroke();

  // Project ECI scene units: X horizontal, Z vertical (Y depth ignored)
  let maxAbs = EARTH_R * 1.4;
  for (const fo of fieldOrbitPts) {
    for (const v of fo.pts) {
      maxAbs = Math.max(maxAbs, Math.hypot(v.x, v.z));
    }
  }
  for (const p of trailData) {
    const a = rKmToVec3(p.ref.r_km);
    maxAbs = Math.max(maxAbs, Math.hypot(a.x, a.z));
  }
  for (const p of obsVisPts) {
    maxAbs = Math.max(maxAbs, Math.hypot(p.x, p.z));
  }
  const scale = (Math.min(w, h) * 0.38) / Math.max(maxAbs, 1e-6);
  const toXY = (v) => [cx + v.x * scale, cy - v.z * scale];

  // Field tracks (behind residual lab)
  for (const fo of fieldOrbitPts) {
    if (fo.pts.length < 2) continue;
    const c = fo.color;
    const r = (c >> 16) & 255;
    const g = (c >> 8) & 255;
    const b = c & 255;
    ctx.strokeStyle = `rgba(${r},${g},${b},${fo.selected ? 0.9 : fo.flagged ? 0.65 : 0.28})`;
    ctx.lineWidth = fo.selected ? 2 : fo.flagged ? 1.4 : 0.9;
    ctx.beginPath();
    fo.pts.forEach((v, i) => {
      const [x, y] = toXY(v);
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  if (!trailData.length || !obsVisPts.length) return;

  // residual connectors
  const maxRes = trailData.reduce((m, p) => Math.max(m, p.residual_km), 0);
  const step = Math.max(1, Math.ceil(trailData.length / 28));
  for (let i = 0; i < trailData.length; i += step) {
    const p = trailData[i];
    if (p.residual_km < 1e-4) continue;
    const a = rKmToVec3(p.ref.r_km);
    const b = obsVisPts[i];
    const [x0, y0] = toXY(a);
    const [x1, y1] = toXY(b);
    const t = Math.min(1, p.residual_km / Math.max(maxRes, 0.5));
    ctx.strokeStyle = `rgba(220, ${Math.round(100 + t * 40)}, 60, ${0.25 + t * 0.45})`;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x0, y0);
    ctx.lineTo(x1, y1);
    ctx.stroke();
  }

  // reference trail
  ctx.strokeStyle = "rgba(127, 184, 224, 0.8)";
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  trailData.forEach((p, i) => {
    const [x, y] = toXY(rKmToVec3(p.ref.r_km));
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // observed trail
  ctx.strokeStyle = "rgba(224, 176, 80, 0.95)";
  ctx.lineWidth = 2.2;
  ctx.beginPath();
  obsVisPts.forEach((p, i) => {
    const [x, y] = toXY(p);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // peak
  let peakIdx = 0;
  for (let i = 0; i < trailData.length; i++) {
    if (trailData[i].residual_km > trailData[peakIdx].residual_km) peakIdx = i;
  }
  if (trailData[peakIdx].residual_km > 0.01) {
    const [px, py] = toXY(obsVisPts[peakIdx]);
    ctx.strokeStyle = "rgba(224, 80, 64, 0.95)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(px, py, 8, 0, Math.PI * 2);
    ctx.stroke();
    ctx.fillStyle = "rgba(224, 80, 64, 0.9)";
    ctx.beginPath();
    ctx.arc(px, py, 2.5, 0, Math.PI * 2);
    ctx.fill();
  }

  // field satellite icons
  for (const fo of fieldOrbitPts) {
    if (!fo.pts.length) continue;
    const [fx, fy] = toXY(fo.pts[0]);
    drawSatIcon2d(ctx, fx, fy, 9, fo.selected ? "#9ec8e4" : fo.flagged ? "#c47858" : "#a8b8c4");
  }

  // NASA ISS + catalog icons
  const idx = Math.floor(playIndex) % obsVisPts.length;
  if (trailData[idx]) {
    const [rx, ry] = toXY(rKmToVec3(trailData[idx].ref.r_km));
    drawSatIcon2d(ctx, rx, ry, 11, "#82b2d6");
    const [sx, sy] = toXY(obsVisPts[idx]);
    drawIssIcon2d(ctx, sx, sy, 16, "#e8c460");
  }
}

function drawIssIcon2d(ctx, x, y, s, fill) {
  ctx.save();
  ctx.translate(x, y);
  ctx.fillStyle = fill;
  ctx.strokeStyle = "rgba(20,16,8,0.9)";
  ctx.lineWidth = 1;
  ctx.fillRect(-s, -s * 0.22, s * 0.72, s * 0.18);
  ctx.fillRect(-s, s * 0.04, s * 0.72, s * 0.18);
  ctx.fillRect(s * 0.28, -s * 0.22, s * 0.72, s * 0.18);
  ctx.fillRect(s * 0.28, s * 0.04, s * 0.72, s * 0.18);
  ctx.fillRect(-s * 0.9, -s * 0.05, s * 1.8, s * 0.1);
  ctx.fillRect(-s * 0.16, -s * 0.28, s * 0.32, s * 0.56);
  ctx.restore();
}

function drawSatIcon2d(ctx, x, y, s, fill) {
  ctx.save();
  ctx.translate(x, y);
  ctx.fillStyle = fill;
  ctx.strokeStyle = "rgba(12,18,24,0.9)";
  ctx.lineWidth = 1;
  ctx.fillRect(-s * 0.18, -s * 0.18, s * 0.36, s * 0.36);
  ctx.strokeRect(-s * 0.18, -s * 0.18, s * 0.36, s * 0.36);
  ctx.fillRect(-s * 0.72, -s * 0.12, s * 0.46, s * 0.24);
  ctx.fillRect(s * 0.26, -s * 0.12, s * 0.46, s * 0.24);
  ctx.restore();
}

// Procedural earth texture from world-atlas land polygons
async function loadEarthTexture() {
  const res = await fetch(LAND_URL);
  if (!res.ok) throw new Error(`land fetch ${res.status}`);
  const topo = await res.json();
  const { feature } = await import("topojson-client");
  const land = feature(topo, topo.objects.land);

  const W = 2048;
  const H = 1024;
  const cnv = document.createElement("canvas");
  cnv.width = W;
  cnv.height = H;
  const ctx = cnv.getContext("2d");

  ctx.fillStyle = "#1a3d55";
  ctx.fillRect(0, 0, W, H);

  const px = (lon) => ((lon + 180) / 360) * W;
  const py = (lat) => ((90 - lat) / 180) * H;
  ctx.fillStyle = "#4d6a52";
  ctx.strokeStyle = "rgba(190, 210, 180, 0.35)";
  ctx.lineWidth = 1.2;
  ctx.lineJoin = "round";
  const polys =
    land.geometry.type === "Polygon"
      ? [land.geometry.coordinates]
      : land.geometry.coordinates;
  for (const poly of polys) {
    for (const ring of poly) {
      ctx.beginPath();
      ring.forEach(([lon, lat], i) => {
        const x = px(lon);
        const y = py(lat);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
    }
  }

  ctx.strokeStyle = "rgba(90, 110, 128, 0.14)";
  ctx.lineWidth = 1;
  for (let lat = -60; lat <= 60; lat += 30) {
    ctx.beginPath();
    ctx.moveTo(0, py(lat));
    ctx.lineTo(W, py(lat));
    ctx.stroke();
  }
  for (let lon = -180; lon < 180; lon += 30) {
    ctx.beginPath();
    ctx.moveTo(px(lon), 0);
    ctx.lineTo(px(lon), H);
    ctx.stroke();
  }

  const tex = new THREE.CanvasTexture(cnv);
  tex.colorSpace = THREE.SRGBColorSpace;
  if (renderer) tex.anisotropy = Math.min(4, renderer.capabilities.getMaxAnisotropy());
  tex.offset.x = 0.25;
  tex.wrapS = THREE.RepeatWrapping;
  return tex;
}

if (renderer) {
  loadEarthTexture()
    .then((tex) => {
      earthMat.map = tex;
      earthMat.color = new THREE.Color(0xffffff);
      earthMat.emissive = new THREE.Color(0x122018);
      earthMat.specular = new THREE.Color(0x2a3a40);
      earthMat.shininess = 9;
      earthMat.needsUpdate = true;
      if (grid) {
        earthGroup.remove(grid);
        grid.traverse((o) => o.geometry?.dispose());
        grid = null;
      }
    })
    .catch(() => {
      /* offline / CDN blocked — keep grid-sphere fallback */
    });
}

// Chart state hoisted so resize() can redraw safely during boot.
let lastChart = null;

function resize() {
  const w = pane.clientWidth;
  const h = pane.clientHeight;
  if (w < 1 || h < 1) return;
  if (renderer && !useFallback2D) {
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
    for (const m of lineMats) m.resolution.set(w, h);
  } else if (useFallback2D) {
    drawFallback2D();
  }
  if (lastChart) renderChart(lastChart.series, lastChart.flags, lastChart.h);
}

window.addEventListener("resize", resize);
resize();

let last = performance.now();
function animate(now) {
  requestAnimationFrame(animate);
  const dt = (now - last) / 1000;
  last = now;

  if (trailData.length && !reduceMotion) {
    playIndex = (playIndex + dt * 12) % trailData.length;
  }

  if (renderer && !useFallback2D) {
    if (!reduceMotion) earthGroup.rotation.y += dt * 0.02;
    if (satMarker && obsVisPts.length && !reduceMotion) {
      const p = obsVisPts[Math.floor(playIndex) % obsVisPts.length];
      if (p) {
        const i = Math.floor(playIndex) % trailData.length;
        const nxt = obsVisPts[(i + 1) % obsVisPts.length];
        aimSatModel(satMarker, p, nxt);
        if (refSatMarker && trailData[i]) {
          const r0 = rKmToVec3(trailData[i].ref.r_km);
          const r1 = rKmToVec3(trailData[(i + 1) % trailData.length].ref.r_km);
          aimSatModel(refSatMarker, r0, r1);
        }
      }
    }
    controls?.update();
    renderer.render(scene, camera);
  } else if (useFallback2D && trailData.length && !reduceMotion) {
    drawFallback2D();
  }
}
requestAnimationFrame(animate);

// ── Residual / CUSUM chart (dual scale) ──────────────────────────────
const cssVar = (name, fallback) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;

function fmtUTCHM(iso) {
  return iso.slice(11, 16) + "Z";
}

function niceMax(v) {
  if (v <= 0) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / exp;
  const nice = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return nice * exp;
}

function fmtAxis(v) {
  if (v === 0) return "0";
  if (v >= 10) return v.toFixed(0);
  if (v >= 1) return v.toFixed(1);
  if (v >= 0.1) return v.toFixed(2);
  return v.toFixed(3);
}

let chartAnim = 0;

function revealChart(chart, flags, hThreshold) {
  lastChart = { series: chart, flags, h: hThreshold };
  if (!chart || !chart.length) {
    if (chartBlock) {
      chartBlock.hidden = true;
      chartBlock.classList.remove("is-ready");
    }
    return;
  }
  if (chartBlock) {
    chartBlock.hidden = false;
    chartBlock.classList.add("is-ready");
  }
  if (reduceMotion) {
    renderChart(chart, flags, hThreshold, 1);
    return;
  }
  const t0 = performance.now();
  const dur = 220;
  cancelAnimationFrame(chartAnim);
  const tick = (now) => {
    const p = Math.min(1, (now - t0) / dur);
    const eased = 1 - (1 - p) ** 3;
    renderChart(chart, flags, hThreshold, eased);
    if (p < 1) chartAnim = requestAnimationFrame(tick);
  };
  chartAnim = requestAnimationFrame(tick);
}

function renderChart(chart, flags, hThreshold, drawProgress = 1) {
  lastChart = { series: chart, flags, h: hThreshold };
  if (!chart || !chart.length) {
    if (chartBlock) {
      chartBlock.hidden = true;
      chartBlock.classList.remove("is-ready");
    }
    return;
  }
  if (chartBlock) {
    chartBlock.hidden = false;
    chartBlock.classList.add("is-ready");
  }
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = chartCanvas.clientWidth || 300;
  const h = 140;
  chartCanvas.width = w * dpr;
  chartCanvas.height = h * dpr;
  const ctx = chartCanvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const padL = 36;
  const padR = 36;
  const padT = 12;
  const padB = 18;
  const iw = w - padL - padR;
  const ih = h - padT - padB;

  const mags = chart.map((p) => {
    const along = Array.isArray(p.rtn_km) ? p.rtn_km[1] : null;
    return along != null && Number.isFinite(along) ? Math.abs(along) : p.mag_km ?? 0;
  });
  const cusums = chart.map((p) => p.cusum ?? 0);
  const maxMag = niceMax(Math.max(...mags, 1e-6) * 1.08);
  const maxC = niceMax(Math.max(...cusums, hThreshold || 0, 1e-6) * 1.08);

  const x = (i) => padL + (i / Math.max(1, chart.length - 1)) * iw;
  const yMag = (v) => padT + ih - (Math.max(0, v) / maxMag) * ih;
  const yC = (v) => padT + ih - (Math.max(0, v) / maxC) * ih;

  ctx.clearRect(0, 0, w, h);

  // plot background is the canvas CSS bg — draw grid only
  ctx.strokeStyle = "rgba(120, 135, 150, 0.12)";
  ctx.lineWidth = 1;
  for (const f of [0.25, 0.5, 0.75]) {
    ctx.beginPath();
    ctx.moveTo(padL, padT + ih * f);
    ctx.lineTo(w - padR, padT + ih * f);
    ctx.stroke();
  }

  // h threshold on CUSUM scale
  if (hThreshold > 0 && hThreshold <= maxC) {
    const yh = yC(hThreshold);
    ctx.strokeStyle = "rgba(224, 80, 64, 0.55)";
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(padL, yh);
    ctx.lineTo(w - padR, yh);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(224, 80, 64, 0.85)";
    ctx.font = "9px 'IBM Plex Mono', monospace";
    ctx.textAlign = "right";
    ctx.fillText(`h=${fmtAxis(hThreshold)}`, w - padR + 32, yh - 3);
    ctx.textAlign = "left";
  }

  ctx.save();
  ctx.beginPath();
  ctx.rect(padL, padT, iw * Math.max(0, Math.min(1, drawProgress)), ih);
  ctx.clip();

  // CUSUM (right scale)
  ctx.strokeStyle = cssVar("--text-secondary", "#8a97a5");
  ctx.lineWidth = 1.3;
  ctx.globalAlpha = 0.75;
  ctx.beginPath();
  cusums.forEach((v, i) => (i ? ctx.lineTo(x(i), yC(v)) : ctx.moveTo(x(i), yC(v))));
  ctx.stroke();
  ctx.globalAlpha = 1;

  // |r| magnitude (left scale)
  ctx.strokeStyle = cssVar("--accent", "#d9a94e");
  ctx.lineWidth = 1.7;
  ctx.beginPath();
  mags.forEach((v, i) => (i ? ctx.lineTo(x(i), yMag(v)) : ctx.moveTo(x(i), yMag(v))));
  ctx.stroke();
  ctx.restore();

  // flag ticks
  if (flags && flags.length) {
    const t0 = Date.parse(chart[0].t);
    const t1 = Date.parse(chart[chart.length - 1].t);
    ctx.fillStyle = cssVar("--red", "#e05040");
    for (const f of flags) {
      const ft = Date.parse(f.time);
      if (Number.isNaN(ft) || t1 <= t0) continue;
      const fx = padL + ((ft - t0) / (t1 - t0)) * iw;
      ctx.fillRect(fx - 1, padT - 3, 2, 5);
    }
  }

  // axis labels
  ctx.fillStyle = cssVar("--text-muted", "#6b7683");
  ctx.font = "9px 'IBM Plex Mono', monospace";
  ctx.textAlign = "left";
  ctx.fillStyle = cssVar("--accent", "#d9a94e");
  ctx.fillText(`${fmtAxis(maxMag)}`, 4, padT + 8);
  ctx.fillStyle = cssVar("--text-muted", "#6b7683");
  ctx.fillText("along", 4, padT + 18);
  ctx.textAlign = "right";
  ctx.fillStyle = cssVar("--text-secondary", "#8a97a5");
  ctx.fillText(fmtAxis(maxC), w - 4, padT + 8);
  ctx.fillStyle = cssVar("--text-muted", "#6b7683");
  ctx.fillText("C", w - 4, padT + 18);
  ctx.textAlign = "left";
  ctx.fillText(fmtUTCHM(chart[0].t), padL, h - 4);
  const lastLabel = fmtUTCHM(chart[chart.length - 1].t);
  ctx.textAlign = "right";
  ctx.fillText(lastLabel, w - padR, h - 4);
  ctx.textAlign = "left";
}

// ── API ──────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!r.ok) {
    let t = "";
    try {
      t = await r.text();
    } catch {
      /* ignore */
    }
    throw new Error(`${r.status}: ${t.slice(0, 200)}`);
  }
  return r.json();
}

function setMode(mode, detail) {
  modeBanner.className = "";
  const map = {
    FIXTURE: "mode-synthetic",
    LIVE_CELESTRAK: "mode-live",
    LIVE_NASA_OEM: "mode-nasa",
    NASA_OEM_VS_SGP4: "mode-nasa",
    DEGRADED: "mode-degraded",
    CONNECTING: "mode-connecting",
  };
  modeBanner.classList.add(map[mode] || "mode-connecting");
  modeLabel.textContent = mode;
  modeDetail.textContent = detail || "";
}

function setStatus(msg, kind = "") {
  detectStatus.textContent = msg;
  detectStatus.className = "status-line" + (kind ? ` ${kind}` : "");
}

function setBusy(busy, ...btns) {
  for (const b of btns) {
    if (!b) continue;
    b.disabled = busy;
    b.classList.toggle("is-busy", busy);
  }
}

const ACTIONS = {
  reobserve_priority: "REOBSERVE · PRIORITY",
  hold_custody_and_reobserve: "HOLD CUSTODY · REOBSERVE",
  monitor: "MONITOR",
};

function sevClass(sev) {
  if (sev >= 0.7) return "sev-hi";
  if (sev >= 0.4) return "sev-med";
  return "sev-lo";
}

function fmtRtn(rtn) {
  const f = (v) => (v == null || Number.isNaN(v) ? 0 : v).toFixed(2);
  return `R ${f(rtn[0])} · T ${f(rtn[1])} · C ${f(rtn[2])}`;
}

function renderFlags(flags) {
  if (!flags || !flags.length) {
    flagsList.innerHTML =
      '<div class="empty-state">No jump. The catalog is offset by a steady amount, not a burn.</div>';
    flagsCount.hidden = true;
    return;
  }
  flagsCount.hidden = false;
  flagsCount.textContent = `${flags.length} event${flags.length === 1 ? "" : "s"}`;
  // Cap list for scroll performance; show highest severity first
  const sorted = [...flags].sort((a, b) => b.severity - a.severity);
  const shown = sorted.slice(0, 24);
  flagsList.innerHTML =
    shown
      .map((f) => {
        const sevPct = (f.severity * 100).toFixed(0);
        const time = f.time.slice(11, 19) + "Z";
        return `
    <article class="flag-card ${sevClass(f.severity)}">
      <div class="flag-top">
        <span class="sev-badge">SEV ${sevPct}</span>
        <span class="flag-time">${time}</span>
      </div>
      <div class="flag-action">${ACTIONS[f.recommended_action] || f.recommended_action}</div>
      <div class="meta">|r| ${f.residual_magnitude_km.toFixed(3)} km · CUSUM ${f.cusum_score.toFixed(2)}</div>
      <div class="meta">${fmtRtn(f.residual_rtn_km)} km</div>
    </article>`;
      })
      .join("") +
    (flags.length > shown.length
      ? `<div class="empty-state">+${flags.length - shown.length} more (top severity shown)</div>`
      : "");
}

function clearMetricsLoading() {
  metricsCard.classList.remove("metrics-loading");
  metricsCard.setAttribute("aria-busy", "false");
}

function setMetricsLoading() {
  metricsCard.classList.add("metrics-loading");
  metricsCard.setAttribute("aria-busy", "true");
  $("m-f1").textContent = "…";
  if ($("m-p")) $("m-p").textContent = "…";
  if ($("m-r")) $("m-r").textContent = "…";
  if ($("m-bound")) $("m-bound").textContent = "…";
  if ($("m-sep")) $("m-sep").textContent = "…";
  if ($("m-n")) $("m-n").textContent = "…";
  $("m-disclosure").textContent = "Running labeled suite…";
}

function renderMetrics(m) {
  clearMetricsLoading();
  metricsCard.classList.remove("metrics-empty");
  $("m-f1").textContent = m.f1.toFixed(3);
  $("m-p").textContent = m.precision.toFixed(3);
  $("m-r").textContent = m.recall.toFixed(3);
  $("m-bound").textContent = `${m.dv_boundary_m_s} m/s`;
  $("m-sep").textContent =
    m.separation_ratio > 0
      ? m.separation_ratio >= 1000
        ? `${m.separation_ratio.toExponential(1)}×`
        : `${m.separation_ratio.toFixed(1)}×`
      : "—";
  $("m-n").textContent = String(m.n_clean + m.n_anomalous);
  $("m-disclosure").textContent = m.disclosure || "";
  $("footer-metrics").textContent =
    `suite F1 ${m.f1.toFixed(3)} · Δv95 ${m.dv_boundary_m_s} m/s · P ${m.precision.toFixed(3)} · R ${m.recall.toFixed(3)}`;
}

function setFieldStatus(msg, kind = "") {
  if (!fieldStatus) return;
  fieldStatus.textContent = msg;
  fieldStatus.className = "status-line" + (kind ? ` ${kind}` : "");
}

function setObserveStatus(msg, kind = "") {
  if (!observeStatus) return;
  observeStatus.textContent = msg;
  observeStatus.className = "status-line" + (kind ? ` ${kind}` : "");
}

function fmtKm(v) {
  if (v == null || Number.isNaN(Number(v))) return "—";
  return Number(v).toFixed(2);
}

function setText(id, v) {
  const el = $(id);
  if (el) el.textContent = v;
}

function setRtnHud(rtn, timingS) {
  const rEl = $("rtn-r");
  const tEl = $("rtn-t");
  const nEl = $("rtn-n");
  const sEl = $("rtn-sec");
  if (!rEl) return;
  const r = rtn?.radial?.median_km;
  const t = rtn?.along_track?.median_km;
  const n = rtn?.cross_track?.median_km;
  rEl.textContent = `${fmtKm(r)} km`;
  tEl.textContent = `${fmtKm(t)} km`;
  nEl.textContent = `${fmtKm(n)} km`;
  if (sEl) {
    sEl.textContent =
      timingS == null || Number.isNaN(Number(timingS))
        ? "—"
        : `${Number(timingS).toFixed(1)} s`;
  }
  tEl.classList.toggle("is-hot", Number.isFinite(Number(t)) && Math.abs(Number(t)) > 5);
}

function applyObserveResult(res) {
  lastPipeline = "observe";
  setTrails(res.trail || []);
  renderFlags(res.flags);
  revealChart(res.chart, res.flags, res.h ?? 12);
  setText("stat-flagged", String(res.flag_count));
  const maxR = res.residual_summary?.max_magnitude_km ?? 0;
  setText("stat-maxres", Number(maxR).toFixed(2));
  globeObject.hidden = false;
  const obsMode = res.meta?.observation_mode || "OEM";
  const rtn = res.residual_summary?.rtn || res.meta?.rtn || {};
  const timing = res.residual_summary?.timing_error_s ?? res.meta?.timing_error_s;
  setRtnHud(rtn, timing);
  const tKm = rtn.along_track?.median_km;
  const tAbs = Number.isFinite(Number(tKm)) ? Math.abs(Number(tKm)) : null;
  const sec =
    timing != null && Number.isFinite(Number(timing))
      ? Math.abs(Number(timing)).toFixed(1)
      : tAbs != null
        ? (tAbs / 7.66).toFixed(1)
        : "—";
  const dir =
    Number(tKm) < 0 ? "behind" : Number(tKm) > 0 ? "ahead of" : "even with";
  const verdict = $("verdict");
  if (verdict) {
    verdict.className = "verdict " + (res.flagged ? "is-warn" : "is-ok");
    verdict.textContent = res.flagged
      ? `The catalog jumped relative to NASA. ${res.flag_count} change flag(s).`
      : `NASA’s ISS file and the public catalog disagree by about ${fmtKm(tAbs)} km along-track (${sec} seconds ${dir} NASA). Height matches. That is a catalog timing error, not a burn.`;
  }
  globeObject.innerHTML = `<span class="${res.flagged ? "go-flagged" : "go-clean"}">${
    res.flagged ? "CHANGE" : "STEADY"
  }</span> ${res.name}`;
  const now = res.now_obs || res.meta?.now_obs;
  const hudNow = $("hud-now");
  if (hudNow && now) {
    const lat = Number(now.lat).toFixed(1);
    const lon = Number(now.lon).toFixed(1);
    const alt = Number(now.alt_km).toFixed(0);
    hudNow.textContent = `${lat}°, ${lon}° · ${alt} km up · ${String(now.t).slice(11, 16)}Z`;
  }
  const src = res.sources;
  const srcList = $("sources-list");
  const srcCard = $("sources-card");
  if (src && srcList && srcCard) {
    const o = src.observed || {};
    const r = src.reference || {};
    const win = o.window || {};
    srcList.innerHTML = `
      <li><strong>NASA ISS file</strong> — ${o.operator || "JSC TOPO"} · ${o.mode || "live"} · ${o.n_states ?? "?"} states · window ${String(win.start || "").slice(11, 16)}–${String(win.stop || "").slice(11, 16)}Z · created ${String(o.created || "").slice(11, 19)}Z${o.url ? ` · <a href="${o.url}" target="_blank" rel="noopener">raw OEM</a>` : ""}</li>
      <li><strong>Public catalog</strong> — ${r.source || "CelesTrak"} TLE → SGP4 · epoch ${String(r.tle_epoch || "").slice(0, 16).replace("T", " ")}Z</li>
    `;
    srcCard.hidden = false;
  }
  const origin = res.meta?.observation_originator || "NASA/JSC TOPO";
  setMode("NASA vs catalog", `${origin} · ${obsMode}`);
  if (modeAge) {
    const tle = (res.meta?.reference_tle_epoch || "").slice(5, 16).replace("T", " ");
    modeAge.textContent = `catalog TLE ${tle || "—"}`;
  }
  if (frameDisclosure) {
    const disc =
      res.meta?.residual_floor_note ||
      res.frame_disclosure ||
      res.meta?.frame_disclosure ||
      "";
    frameDisclosure.hidden = !disc;
    frameDisclosure.textContent = disc;
  }
  const ev = res.oem_events || res.meta?.oem_events || [];
  setObserveStatus(
    res.flagged
      ? `NASA file vs catalog · ${res.flag_count} change flag(s)`
      : ev.length
        ? `NASA file vs catalog · ${ev.length} published burn(s)`
        : "NASA file vs catalog · no published burns in this OEM",
    res.flagged ? "warn" : "ok"
  );
}

async function runObserve() {
  if (!btnObserve) return;
  setBusy(true, btnObserve);
  setObserveStatus("Fetching NASA ISS OEM + propagating SGP4…");
  try {
    const res = await api("/orbital/observe/iss", {
      method: "POST",
      body: JSON.stringify({
        n_samples: Number(oemSamples?.value) || 90,
        oem_source: oemSource?.value || "auto",
        norad_id: 25544,
        group: currentGroup || "stations",
      }),
    });
    applyObserveResult(res);
  } catch (e) {
    setObserveStatus(String(e.message || e), "err");
  } finally {
    setBusy(false, btnObserve);
  }
}

async function refreshHealth() {
  try {
    const g = groupSelect?.value || currentGroup;
    const h = await api(`/orbital/health?group=${encodeURIComponent(g)}`);
    setMode(h.mode, `${h.backend}${h.detail ? " · " + h.detail : ""}`);
    if (h.cache_age_seconds != null) {
      modeAge.textContent = `cache ${Math.round(h.cache_age_seconds)}s · n=${h.catalog_count ?? "—"}`;
    } else {
      modeAge.textContent = h.mode === "FIXTURE" ? "fixture" : "live";
    }
    if (h.catalog_count != null) setText("stat-catalog", String(h.catalog_count));
  } catch (e) {
    setMode("DEGRADED", String(e.message || e));
  }
}

function fillObjectSelect(objects, preferNorad) {
  const prev = preferNorad != null ? String(preferNorad) : objectSelect.value;
  objectSelect.innerHTML = "";
  if (!objects.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "— empty catalog —";
    objectSelect.appendChild(opt);
    return;
  }
  for (const o of objects) {
    const opt = document.createElement("option");
    opt.value = String(o.norad_id);
    const tier = o.custody_tier ? ` · ${o.custody_tier}` : "";
    opt.textContent = `${o.name} · ${o.norad_id}${tier}`;
    objectSelect.appendChild(opt);
  }
  if (prev && [...objectSelect.options].some((o) => o.value === prev)) {
    objectSelect.value = prev;
  }
}

async function loadField({ silent = false } = {}) {
  currentGroup = groupSelect?.value || "stations";
  if (!silent) setFieldStatus("Fetching catalog + orbits…");
  setBusy(true, btnField, btnRefresh);
  try {
    const res = await api("/orbital/field/scan", {
      method: "POST",
      body: JSON.stringify({
        group: currentGroup,
        orbit_samples: currentGroup === "stations" ? 64 : 40,
        include_orbits: true,
      }),
    });
    fillObjectSelect(res.objects);
    setFieldOrbits(res.objects);
    setText("stat-catalog", String(res.n_scanned));
    setText("stat-flagged", String(res.n_flagged));
    const stale = res.objects.filter((o) => o.custody_tier === "stale").length;
    setText("stat-stale", String(stale));

    // Field flags list (custody / prop errors)
    const fieldFlags = res.objects
      .filter((o) => o.flagged)
      .sort((a, b) => b.severity - a.severity);
    if (fieldFlags.length) {
      flagsCount.hidden = false;
      flagsCount.textContent = `${fieldFlags.length} field`;
      flagsList.innerHTML = fieldFlags
        .slice(0, 40)
        .map((o) => {
          const sev = o.severity >= 0.7 ? "sev-hi" : o.severity >= 0.35 ? "sev-med" : "sev-lo";
          return `<article class="flag-card ${sev}">
            <div class="flag-top">
              <span class="sev-badge">${(o.custody_tier || "flag").toUpperCase()}</span>
              <span class="flag-time">${o.norad_id}</span>
            </div>
            <div class="flag-action">${o.name}</div>
            <div class="meta">age ${o.custody_age_hours?.toFixed?.(1) ?? "—"}h · ${o.reason}</div>
          </article>`;
        })
        .join("");
    } else {
      flagsCount.hidden = true;
      flagsList.innerHTML =
        '<div class="empty-state">No field flags · TLEs fresh · NASA residual is the default path</div>';
    }

    setFieldStatus(
      `${res.mode} · ${res.n_scanned} objects · ${res.n_flagged} flagged · ${res.duration_ms}ms`,
      res.mode === "DEGRADED" ? "warn" : "ok"
    );
    setMode(
      res.mode,
      `${res.backend} · group=${res.group} · n=${res.n_scanned}`
    );
    if (res.cache_age_seconds != null) {
      modeAge.textContent = `cache ${Math.round(res.cache_age_seconds)}s`;
    }

    // Prefer real NASA residual when ISS is in the field; else synthetic clean
    const hasIss = res.objects.some(
      (o) => o.norad_id === 25544 || /ISS/i.test(o.name || "")
    );
    if (hasIss) {
      const iss = res.objects.find(
        (o) => o.norad_id === 25544 || /ISS/i.test(o.name || "")
      );
      if (iss) objectSelect.value = String(iss.norad_id);
      await runObserve();
    } else if (objectSelect.value) {
      await runDetect(0);
    }
  } catch (e) {
    setFieldStatus(String(e.message || e), "err");
  } finally {
    setBusy(false, btnField, btnRefresh);
  }
}

function startAutoRefresh() {
  if (refreshTimer) clearInterval(refreshTimer);
  if (reduceMotion) return;
  refreshTimer = setInterval(() => {
    if (document.hidden) return;
    loadField({ silent: true });
  }, REFRESH_MS);
}

async function runDetect(dv) {
  setBusy(true, btnDetect, btnClean, btnDemo);
  lastPipeline = "synthetic";
  setStatus("Propagating SGP4 (synthetic Δv lab)…");
  if (frameDisclosure) {
    frameDisclosure.hidden = false;
    frameDisclosure.textContent =
      "Synthetic Δv path — residual labels known by construction. Use NASA ISS OEM for real dual-source residual.";
  }
  try {
    const body = {
      dv_m_s: dv,
      n_samples: Number(nSamples.value) || 120,
    };
    if (objectSelect.value) body.norad_id = Number(objectSelect.value);
    body.group = currentGroup;
    const res = await api("/orbital/detect", {
      method: "POST",
      body: JSON.stringify(body),
    });
    setTrails(res.trail || []);
    renderFlags(res.flags);
    revealChart(res.chart, res.flags, res.h ?? 0.5);
    setText("stat-flagged", String(res.flag_count));
    setText("stat-maxres", res.residual_summary.max_magnitude_km.toFixed(2));
    globeObject.hidden = false;
    globeObject.innerHTML = `<span class="${res.flagged ? "go-flagged" : "go-clean"}">${
      res.flagged ? "FLAGGED" : "CLEAN"
    }</span> ${res.name} · synthetic Δv ${dv.toFixed(1)} m/s`;
    setStatus(
      res.flagged
        ? `FLAGGED · ${res.name} · synthetic Δv=${dv} m/s · ${res.flag_count} flag(s)`
        : `CLEAN · ${res.name} · synthetic · max |r|=${res.residual_summary.max_magnitude_km.toFixed(3)} km`,
      res.flagged ? "warn" : "ok"
    );
  } catch (e) {
    setStatus(String(e.message || e), "err");
  } finally {
    setBusy(false, btnDetect, btnClean, btnDemo);
  }
}

async function runEval() {
  setBusy(true, btnEval);
  setMetricsLoading();
  setStatus("Running eval suite…");
  try {
    const m = await api("/orbital/eval/run", {
      method: "POST",
      body: JSON.stringify({
        n_clean: 12,
        n_anomalous: 12,
        dv_m_s: 2.0,
        n_samples: 100,
        seed: 42,
      }),
    });
    renderMetrics(m);
    setStatus(`Eval F1=${m.f1.toFixed(3)} · boundary ${m.dv_boundary_m_s} m/s`, "ok");
  } catch (e) {
    clearMetricsLoading();
    $("m-disclosure").textContent = String(e.message || e);
    setStatus(String(e.message || e), "err");
  } finally {
    setBusy(false, btnEval);
  }
}

async function runDemo() {
  setBusy(true, btnDetect, btnClean, btnDemo);
  setStatus("Demo: clean track…");
  dvSlider.value = "0";
  dvValue.textContent = "0.0";
  await runDetect(0);
  if (reduceMotion) {
    dvSlider.value = "0.5";
    dvValue.textContent = "0.5";
    await runDetect(0.5);
    return;
  }
  // Animate slider 0 → 0.5 then detect
  setStatus("Demo: inject Δv…");
  const target = 0.5;
  const steps = 10;
  for (let i = 1; i <= steps; i++) {
    const v = (target * i) / steps;
    dvSlider.value = String(v);
    dvValue.textContent = v.toFixed(1);
    await new Promise((r) => setTimeout(r, 40));
  }
  await runDetect(target);
}

btnClean.addEventListener("click", () => {
  dvSlider.value = "0";
  dvValue.textContent = "0.0";
  runDetect(0);
});
btnDetect.addEventListener("click", () => runDetect(Number(dvSlider.value)));
btnEval.addEventListener("click", runEval);
btnDemo.addEventListener("click", runDemo);
btnObserve?.addEventListener("click", () => runObserve());
btnField?.addEventListener("click", () => loadField());
btnRefresh?.addEventListener("click", () => loadField());
groupSelect?.addEventListener("change", () => loadField());
objectSelect.addEventListener("change", () => {
  // recolor field selection
  if (fieldObjects.length) setFieldOrbits(fieldObjects);
  const norad = Number(objectSelect.value);
  if (norad === 25544) {
    runObserve();
  } else {
    runDetect(Number(dvSlider.value) || 0);
  }
});

// boot — NASA 3D models, then field + ISS residual
(async () => {
  setMode("CONNECTING", "orbit ghost");
  await refreshHealth();
  await loadSatModels();
  await loadField();
  startAutoRefresh();
})();
