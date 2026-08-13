/**
 * Official NASA 3D Resources spacecraft (GLB), loaded once and cloned.
 * Public NASA media — see static/models/nasa/ATTRIBUTION.md
 */
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";

const SPECS = {
  iss: { url: "/static/models/nasa/iss.glb", size: 0.09 },
  hubble: { url: "/static/models/nasa/hubble.glb", size: 0.055 },
  landsat: { url: "/static/models/nasa/landsat.glb", size: 0.05 },
  cloudsat: { url: "/static/models/nasa/cloudsat.glb", size: 0.046 },
  cubesat: { url: "/static/models/nasa/cubesat.glb", size: 0.03 },
};

const templates = {};
let loadPromise = null;
let ready = false;

function fitToSize(wrap, target) {
  const inner = wrap.children[0] || wrap;
  const box = new THREE.Box3().setFromObject(inner);
  const size = box.getSize(new THREE.Vector3());
  const max = Math.max(size.x, size.y, size.z) || 1;
  inner.position.sub(box.getCenter(new THREE.Vector3()));
  wrap.scale.setScalar(target / max);
}

function prepTemplate(gltf, size) {
  const inner = gltf.scene;
  inner.traverse((o) => {
    if (!o.isMesh) return;
    o.castShadow = false;
    o.receiveShadow = false;
    if (o.material) {
      o.material = o.material.clone();
      o.material.side = THREE.DoubleSide;
      o.material.metalness = Math.min(0.55, o.material.metalness ?? 0.25);
      o.material.roughness = Math.max(0.35, o.material.roughness ?? 0.55);
    }
  });
  const wrap = new THREE.Group();
  wrap.add(inner);
  fitToSize(wrap, size);
  return wrap;
}

export function modelsReady() {
  return ready;
}

export function loadSatModels() {
  if (loadPromise) return loadPromise;
  const loader = new GLTFLoader();
  loadPromise = Promise.all(
    Object.entries(SPECS).map(
      ([kind, spec]) =>
        new Promise((resolve) => {
          loader.load(
            spec.url,
            (gltf) => {
              templates[kind] = prepTemplate(gltf, spec.size);
              resolve();
            },
            undefined,
            (err) => {
              console.warn("sat model failed", kind, err);
              resolve();
            }
          );
        })
    )
  ).then(() => {
    ready = Object.keys(templates).length > 0;
    return ready;
  });
  return loadPromise;
}

export function pickSatKind(name, noradId) {
  const u = String(name || "").toUpperCase();
  if (noradId === 25544 || /\bISS\b|ZARYA|TIANHE|\bCSS\b|TIANGONG/.test(u)) return "iss";
  if (u.includes("HUBBLE") || /\bHST\b/.test(u)) return "hubble";
  if (
    /LANDSAT|TERRA|AQUA|AURA|NPP|NOAA|GOES|SENTINEL|METOP|SUOMI|JPSS|ENVISAT/.test(
      u
    )
  ) {
    return "landsat";
  }
  if (/STARLINK|ONEWEB|IRIDIUM|GLOBALSTAR|ORBCOMM/.test(u)) return "cubesat";
  return "cloudsat";
}

export function cloneSatModel(kind, { tintHex = null, scale = 1 } = {}) {
  const src = templates[kind] || templates.cloudsat || templates.cubesat;
  if (!src) return null;
  const obj = src.clone(true);
  if (scale !== 1) obj.scale.multiplyScalar(scale);
  if (tintHex != null) {
    const tint = new THREE.Color(tintHex);
    obj.traverse((o) => {
      if (!o.isMesh || !o.material) return;
      o.material = o.material.clone();
      o.material.emissive = tint;
      o.material.emissiveIntensity = 0.18;
    });
  }
  obj.userData.kind = kind;
  obj.userData.isSatModel = true;
  return obj;
}

const _up = new THREE.Vector3(0, 1, 0);

export function aimSatModel(obj, pos, ahead) {
  obj.position.copy(pos);
  if (!ahead) return;
  const dir = ahead.clone().sub(pos);
  if (dir.lengthSq() < 1e-12) return;
  dir.normalize();
  obj.up.copy(_up);
  obj.lookAt(pos.clone().add(dir));
}
