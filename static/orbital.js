/**
 * Orbit Ghost console — Three.js Earth + residual trails + /orbital/* API.
 */
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";

const EARTH_R = 1.0; // scene units ≈ Earth radius
const KM_TO_SCENE = EARTH_R / 6378.137;

const $ = (id) => document.getElementById(id);

// ── DOM ──────────────────────────────────────────────────────────────
const modeBanner = $("mode-banner");
const modeLabel = $("mode-label");
const modeDetail = $("mode-detail");
const modeAge = $("mode-age");
const dvSlider = $("dv-slider");
const dvValue = $("dv-value");
const nSamples = $("n-samples");
const btnClean = $("btn-clean");
const btnDetect = $("btn-detect");
const btnEval = $("btn-eval");
const detectStatus = $("detect-status");
const flagsList = $("flags-list");
const metricsCard = $("metrics-card");

dvSlider.addEventListener("input", () => {
  dvValue.textContent = Number(dvSlider.value).toFixed(1);
});

// ── Three.js scene ───────────────────────────────────────────────────
const canvas = $("globe-canvas");
const pane = $("globe-pane");
const renderer = new THREE.WebGLRenderer({
  canvas,
  antialias: true,
  alpha: true,
});
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setClearColor(0x000000, 0);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
camera.position.set(0, 1.2, 3.2);

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.06;
controls.minDistance = 1.4;
controls.maxDistance = 8;
controls.target.set(0, 0, 0);

const ambient = new THREE.AmbientLight(0x8899aa, 0.55);
scene.add(ambient);
const key = new THREE.DirectionalLight(0xfff2dd, 1.1);
key.position.set(4, 2, 3);
scene.add(key);

// Earth
const earthGroup = new THREE.Group();
scene.add(earthGroup);

const earthGeo = new THREE.SphereGeometry(EARTH_R, 64, 48);
const earthMat = new THREE.MeshPhongMaterial({
  color: 0x1a2a3a,
  emissive: 0x050a12,
  specular: 0x223344,
  shininess: 12,
  flatShading: false,
});
const earth = new THREE.Mesh(earthGeo, earthMat);
earthGroup.add(earth);

// Atmosphere shell
const atmo = new THREE.Mesh(
  new THREE.SphereGeometry(EARTH_R * 1.018, 48, 32),
  new THREE.MeshBasicMaterial({
    color: 0x4a90c8,
    transparent: true,
    opacity: 0.08,
    side: THREE.BackSide,
  })
);
earthGroup.add(atmo);

// Lat/lon grid
const grid = new THREE.Group();
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

// Orbit trails
let refLine = null;
let obsLine = null;
let residualRibbons = [];
let satMarker = null;
let trailData = [];
let playIndex = 0;

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

function clearTrails() {
  if (refLine) {
    earthGroup.remove(refLine);
    refLine.geometry.dispose();
    refLine.material.dispose();
    refLine = null;
  }
  if (obsLine) {
    earthGroup.remove(obsLine);
    obsLine.geometry.dispose();
    obsLine.material.dispose();
    obsLine = null;
  }
  for (const g of residualRibbons) {
    earthGroup.remove(g);
    g.geometry?.dispose();
    g.material?.dispose();
  }
  residualRibbons = [];
  if (satMarker) {
    earthGroup.remove(satMarker);
    satMarker.geometry.dispose();
    satMarker.material.dispose();
    satMarker = null;
  }
}

function setTrails(trail) {
  clearTrails();
  trailData = trail || [];
  playIndex = 0;
  if (!trailData.length) return;

  const refPts = trailData.map((p) => rKmToVec3(p.ref.r_km));
  const obsPts = trailData.map((p) => rKmToVec3(p.obs.r_km));

  refLine = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(refPts),
    new THREE.LineBasicMaterial({ color: 0x6ab0e0, linewidth: true, opacity: 0.85 })
  );
  earthGroup.add(refLine);

  obsLine = new THREE.Line(
    new THREE.BufferGeometry().setFromPoints(obsPts),
    new THREE.LineBasicMaterial({ color: 0xe0b050, linewidth: true, opacity: 0.95 })
  );
  earthGroup.add(obsLine);

  // Residual connectors every N samples (visual offset between ref and obs)
  const maxRes = Math.max(...trailData.map((p) => p.residual_km), 1e-9);
  for (let i = 0; i < trailData.length; i += 3) {
    const p = trailData[i];
    if (p.residual_km < 1e-6) continue;
    const a = rKmToVec3(p.ref.r_km);
    const b = rKmToVec3(p.obs.r_km);
    const t = Math.min(1, p.residual_km / Math.max(maxRes, 0.5));
    const col = new THREE.Color().setHSL(0.08 - t * 0.08, 0.85, 0.45 + t * 0.15);
    const line = new THREE.Line(
      new THREE.BufferGeometry().setFromPoints([a, b]),
      new THREE.LineBasicMaterial({ color: col, transparent: true, opacity: 0.35 + t * 0.5 })
    );
    residualRibbons.push(line);
    earthGroup.add(line);
  }

  // Peak residual marker
  let peak = trailData[0];
  for (const p of trailData) {
    if (p.residual_km > peak.residual_km) peak = p;
  }
  if (peak.residual_km > 0.01) {
    const m = new THREE.Mesh(
      new THREE.SphereGeometry(0.018, 12, 12),
      new THREE.MeshBasicMaterial({ color: 0xe05040 })
    );
    m.position.copy(rKmToVec3(peak.obs.r_km));
    residualRibbons.push(m);
    earthGroup.add(m);
  }

  satMarker = new THREE.Mesh(
    new THREE.SphereGeometry(0.022, 12, 12),
    new THREE.MeshBasicMaterial({ color: 0xffd070 })
  );
  satMarker.position.copy(obsPts[0]);
  earthGroup.add(satMarker);
}

function resize() {
  const w = pane.clientWidth;
  const h = pane.clientHeight;
  if (w < 1 || h < 1) return;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

window.addEventListener("resize", resize);
resize();

let last = performance.now();
function animate(now) {
  requestAnimationFrame(animate);
  const dt = (now - last) / 1000;
  last = now;
  earthGroup.rotation.y += dt * 0.02;
  if (satMarker && trailData.length) {
    playIndex = (playIndex + dt * 12) % trailData.length;
    const i = Math.floor(playIndex);
    const p = trailData[i];
    if (p) satMarker.position.copy(rKmToVec3(p.obs.r_km));
  }
  controls.update();
  renderer.render(scene, camera);
}
requestAnimationFrame(animate);

// ── API ──────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`${r.status}: ${t.slice(0, 200)}`);
  }
  return r.json();
}

function setMode(mode, detail) {
  modeBanner.className = "";
  const map = {
    FIXTURE: "mode-synthetic",
    LIVE_CELESTRAK: "mode-live",
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

function renderFlags(flags) {
  if (!flags || !flags.length) {
    flagsList.innerHTML = '<div class="empty-state">No flags · track clean or below threshold</div>';
    return;
  }
  flagsList.innerHTML = flags
    .map(
      (f) => `
    <article class="flag-card">
      <div><strong>${f.flag_type}</strong> · sev ${(f.severity * 100).toFixed(0)}%</div>
      <div class="meta">|r|=${f.residual_magnitude_km.toFixed(3)} km · CUSUM ${f.cusum_score.toFixed(2)}</div>
      <div class="action">${f.recommended_action}</div>
      <div class="meta">${f.time}</div>
    </article>`
    )
    .join("");
}

function renderMetrics(m) {
  metricsCard.classList.remove("metrics-empty");
  $("m-f1").textContent = m.f1.toFixed(3);
  $("m-p").textContent = m.precision.toFixed(3);
  $("m-r").textContent = m.recall.toFixed(3);
  $("m-bound").textContent = `${m.dv_boundary_m_s} m/s`;
  $("m-disclosure").textContent = m.disclosure || "";
}

async function refreshHealth() {
  try {
    const h = await api("/orbital/health");
    setMode(h.mode, `${h.backend}${h.detail ? " · " + h.detail : ""}`);
    if (h.cache_age_seconds != null) {
      modeAge.textContent = `cache ${Math.round(h.cache_age_seconds)}s`;
    } else {
      modeAge.textContent = "offline-ok";
    }
  } catch (e) {
    setMode("DEGRADED", String(e.message || e));
  }
}

async function refreshCatalog() {
  try {
    const c = await api("/orbital/catalog");
    $("stat-catalog").textContent = String(c.count);
  } catch {
    $("stat-catalog").textContent = "—";
  }
}

async function runDetect(dv) {
  btnDetect.disabled = true;
  btnClean.disabled = true;
  setStatus("Running detect…");
  try {
    const body = {
      dv_m_s: dv,
      n_samples: Number(nSamples.value) || 120,
    };
    const res = await api("/orbital/detect", {
      method: "POST",
      body: JSON.stringify(body),
    });
    setTrails(res.trail || []);
    renderFlags(res.flags);
    $("stat-flagged").textContent = String(res.flag_count);
    $("stat-maxres").textContent = res.residual_summary.max_magnitude_km.toFixed(2);
    setStatus(
      res.flagged
        ? `FLAGGED · ${res.name} · Δv=${dv} m/s · ${res.flag_count} flag(s)`
        : `CLEAN · ${res.name} · max |r|=${res.residual_summary.max_magnitude_km.toFixed(3)} km`,
      res.flagged ? "warn" : "ok"
    );
  } catch (e) {
    setStatus(String(e.message || e), "err");
  } finally {
    btnDetect.disabled = false;
    btnClean.disabled = false;
  }
}

async function runEval() {
  btnEval.disabled = true;
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
    setStatus(String(e.message || e), "err");
  } finally {
    btnEval.disabled = false;
  }
}

btnClean.addEventListener("click", () => {
  dvSlider.value = "0";
  dvValue.textContent = "0.0";
  runDetect(0);
});
btnDetect.addEventListener("click", () => runDetect(Number(dvSlider.value)));
btnEval.addEventListener("click", runEval);

// boot
(async () => {
  setMode("CONNECTING", "orbit ghost");
  await refreshHealth();
  await refreshCatalog();
  await runDetect(0);
})();
