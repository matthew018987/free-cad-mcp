// SPDX-License-Identifier: Apache-2.0
// The viewer page's code. server/viewer.py bundles it with three.js into the page each build writes
// (build/viewer/<name>.html), together with that build's status and model.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

const $ = (id) => document.getElementById(id);
const POLL_MS = 1000;

// ------------------------------------------------------------------ renderer, cameras, lights
const canvas = $('c');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, stencil: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.localClippingEnabled = true;
renderer.autoClear = false;
const scene = new THREE.Scene();
const persp = new THREE.PerspectiveCamera(35, 1, 0.1, 1e6);
const ortho = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.01, 1e6);
for (const c of [persp, ortho]) { c.up.set(0, 0, 1); c.position.set(100, -100, 100); }
let camera = persp;
let orthoHalf = 50;                                  // half-height of the ortho view at zoom 1 (mm)
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.15;
controls.screenSpacePanning = true;
scene.add(new THREE.HemisphereLight(0xffffff, 0x7a8394, 1.6));
const head = new THREE.DirectionalLight(0xffffff, 2.2);
scene.add(head, head.target);

function cssColour(name) {
  return new THREE.Color(getComputedStyle(document.documentElement).getPropertyValue(name).trim() || '#eeeeee');
}
function applyTheme() { scene.background = cssColour('--canvas'); dirty = true; }
let dirty = true;
applyTheme();
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyTheme);
controls.addEventListener('change', () => { dirty = true; });

// ------------------------------------------------------------------ the model
// glTF from FreeCAD is in metres with Y up; bring it back to the model's mm with Z up.
const FIX = new THREE.Matrix4().makeRotationX(Math.PI / 2).multiply(new THREE.Matrix4().makeScale(1000, 1000, 1000));
const loader = new GLTFLoader();
const model = new THREE.Group();
scene.add(model);
let parts = [];            // {label, ref, colour, group, mesh, edges, back, front, cap, visible, xray}
let bounds = new THREE.Box3(new THREE.Vector3(-50, -50, -50), new THREE.Vector3(50, 50, 50));
let radius = 87;
let modelVersion = null, needFit = true;
const hiddenLabels = new Set(), xrayLabels = new Set();    // kept across rebuilds, by label

const stencilBase = { depthWrite: false, depthTest: false, colorWrite: false, stencilWrite: true,
                      stencilFunc: THREE.AlwaysStencilFunc };

function clearModel() {
  for (const p of parts) {
    p.group.traverse((o) => {
      if (o.geometry) o.geometry.dispose();
      if (o.material) o.material.dispose();
    });
  }
  model.clear();
  parts = [];
}

function cleanGeometry(g) {
  for (const k of Object.keys(g.attributes)) if (k !== 'position' && k !== 'normal') g.deleteAttribute(k);
  if (!g.attributes.normal) g.computeVertexNormals();
  return g;
}

function buildPart(info, geoms, k) {
  geoms = geoms.map(cleanGeometry);
  // one CAD face per glTF primitive: its open boundary is the face's edge loop, and a cylinder's
  // seam closes up (positions are matched), so EdgesGeometry draws the real CAD edges.
  const chunks = geoms.map((g) => { const e = new THREE.EdgesGeometry(g, 40); const a = e.attributes.position.array; e.dispose(); return a; });
  const ep = new Float32Array(chunks.reduce((n, a) => n + a.length, 0));
  chunks.reduce((o, a) => { ep.set(a, o); return o + a.length; }, 0);
  const edgeGeom = new THREE.BufferGeometry();
  edgeGeom.setAttribute('position', new THREE.BufferAttribute(ep, 3));

  const mixed = geoms.some((g) => g.index) && !geoms.every((g) => g.index);
  const list = mixed ? geoms.map((g) => (g.index ? g.toNonIndexed() : g)) : geoms;
  const geom = list.length === 1 ? list[0] : mergeGeometries(list, false);
  if (list.length > 1) list.forEach((g) => g.dispose());
  geom.computeBoundingBox();
  geom.computeBoundingSphere();

  const colour = new THREE.Color().setRGB(info.colour[0], info.colour[1], info.colour[2], THREE.SRGBColorSpace);
  const mesh = new THREE.Mesh(geom, new THREE.MeshStandardMaterial({
    color: colour, roughness: 0.55, metalness: 0.05, side: THREE.DoubleSide,
    polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 }));
  mesh.renderOrder = 1000;
  const edges = new THREE.LineSegments(edgeGeom, new THREE.LineBasicMaterial({
    color: info.ref ? 0x8a909b : 0x1d2129, transparent: true, opacity: info.ref ? 0.7 : 0.9 }));
  edges.renderOrder = 1001;
  // section caps: count the cut part's crossings in the stencil buffer, then paint the plane where it is non-zero
  const back = new THREE.Mesh(geom, new THREE.MeshBasicMaterial({ ...stencilBase, side: THREE.BackSide,
    stencilFail: THREE.IncrementWrapStencilOp, stencilZFail: THREE.IncrementWrapStencilOp, stencilZPass: THREE.IncrementWrapStencilOp }));
  const front = new THREE.Mesh(geom, new THREE.MeshBasicMaterial({ ...stencilBase, side: THREE.FrontSide,
    stencilFail: THREE.DecrementWrapStencilOp, stencilZFail: THREE.DecrementWrapStencilOp, stencilZPass: THREE.DecrementWrapStencilOp }));
  back.renderOrder = front.renderOrder = 2 * k + 1;
  const cap = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshStandardMaterial({
    color: colour.clone().multiplyScalar(0.72), roughness: 0.85, metalness: 0, side: THREE.DoubleSide,
    stencilWrite: true, stencilRef: 0, stencilFunc: THREE.NotEqualStencilFunc,
    stencilFail: THREE.ReplaceStencilOp, stencilZFail: THREE.ReplaceStencilOp, stencilZPass: THREE.ReplaceStencilOp }));
  cap.renderOrder = 2 * k + 1.5;
  cap.onAfterRender = (r) => r.clearStencil();
  for (const o of [back, front, cap]) { o.visible = false; o.raycast = () => {}; }
  edges.raycast = () => {};
  const group = new THREE.Group();
  group.add(mesh, edges, back, front, cap);
  model.add(group);
  return { label: info.label, ref: info.ref, colour, group, mesh, edges, back, front, cap,
           visible: !hiddenLabels.has(info.label), xray: xrayLabels.has(info.label) };
}

function base64Bytes(s) {
  const bin = atob(s.trim());
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

async function loadModel(info, b64) {
  const gltf = await loader.parseAsync(base64Bytes(b64), '');
  gltf.scene.updateMatrixWorld(true);
  const wanted = new Map(info.parts.map((p) => [p.node, p]));
  const found = new Map();
  (function walk(o) {
    if (wanted.has(o.name) && !found.has(o.name)) {
      const geoms = [];
      o.traverse((m) => {
        if (m.isMesh) {
          const g = m.geometry.clone();
          g.applyMatrix4(new THREE.Matrix4().multiplyMatrices(FIX, m.matrixWorld));
          geoms.push(g);
        }
      });
      found.set(o.name, geoms);
      return;
    }
    for (const c of o.children) walk(c);
  })(gltf.scene);
  gltf.scene.traverse((o) => { if (o.geometry) o.geometry.dispose(); });

  clearModel();
  info.parts.forEach((p, k) => {
    const geoms = found.get(p.node);
    if (geoms && geoms.length) parts.push(buildPart(p, geoms, k));
  });
  bounds = new THREE.Box3();
  for (const p of parts) bounds.expandByObject(p.mesh);
  if (bounds.isEmpty()) bounds.set(new THREE.Vector3(-50, -50, -50), new THREE.Vector3(50, 50, 50));
  radius = Math.max(bounds.getSize(new THREE.Vector3()).length() / 2, 1);
  applyParts();
  updateSectionRange(false);
  applySection();
  clearPick();
  if (needFit) { setView('iso'); needFit = false; }
  renderParts();
  dirty = true;
}

// ------------------------------------------------------------------ parts panel
function applyParts() {
  for (const p of parts) {
    p.group.visible = p.visible;
    const m = p.mesh.material;
    m.transparent = p.xray;
    m.opacity = p.xray ? 0.18 : 1;
    m.depthWrite = !p.xray;
    m.needsUpdate = true;
    p.edges.visible = $('edges').checked;
    p.edges.material.opacity = p.xray ? 0.35 : (p.ref ? 0.7 : 0.9);
  }
  applySection();
  dirty = true;
}

function renderParts() {
  const box = $('parts');
  if (!parts.length) { box.textContent = '—'; box.className = 'muted'; return; }
  box.className = '';
  box.replaceChildren(...parts.map((p) => {
    const row = document.createElement('div');
    row.className = 'part' + (p.visible ? '' : ' off');
    row.title = 'Double-click to show only this part';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = p.visible; cb.setAttribute('aria-label', 'Show ' + p.label);
    cb.onchange = () => { p.visible = cb.checked; (p.visible ? hiddenLabels.delete(p.label) : hiddenLabels.add(p.label)); applyParts(); renderParts(); };
    const sw = document.createElement('span'); sw.className = 'sw'; sw.style.background = '#' + p.colour.getHexString(THREE.SRGBColorSpace);
    const lb = document.createElement('span'); lb.className = 'lb'; lb.textContent = p.label;
    row.append(cb, sw, lb);
    if (p.ref) { const t = document.createElement('span'); t.className = 'tag'; t.textContent = 'ref'; row.append(t); }
    const xr = document.createElement('button');
    xr.className = 'btn small' + (p.xray ? ' on' : ''); xr.textContent = 'X-ray'; xr.title = 'See through this part';
    xr.onclick = () => { p.xray = !p.xray; (p.xray ? xrayLabels.add(p.label) : xrayLabels.delete(p.label)); applyParts(); renderParts(); };
    row.append(xr);
    row.ondblclick = (e) => {
      if (e.target === cb || e.target === xr) return;
      for (const q of parts) { q.visible = q === p; (q.visible ? hiddenLabels.delete(q.label) : hiddenLabels.add(q.label)); }
      applyParts(); renderParts();
    };
    return row;
  }));
}
$('showAll').onclick = () => { for (const p of parts) p.visible = true; hiddenLabels.clear(); applyParts(); renderParts(); };
$('edges').onchange = applyParts;

// ------------------------------------------------------------------ section
const section = { axis: '', flip: false, value: 0 };
const plane = new THREE.Plane(new THREE.Vector3(-1, 0, 0), 0);
const AXES = { x: new THREE.Vector3(1, 0, 0), y: new THREE.Vector3(0, 1, 0), z: new THREE.Vector3(0, 0, 1) };

function applySection() {
  const on = !!section.axis;
  if (on) {
    // like render(section=…): remove the side toward the standard viewer (+X, −Y, +Z); Flip removes the other side
    let s = section.axis === 'y' ? -1 : 1;
    if (section.flip) s = -s;
    plane.normal.copy(AXES[section.axis]).multiplyScalar(-s);
    plane.constant = s * section.value;
  }
  const planes = on ? [plane] : [];
  const centre = bounds.getCenter(new THREE.Vector3());
  for (const p of parts) {
    for (const o of [p.mesh, p.edges, p.back, p.front]) o.material.clippingPlanes = planes;
    const capOn = on && $('caps').checked && p.visible && !p.xray;
    p.back.visible = p.front.visible = p.cap.visible = capOn;
    if (capOn) {
      p.cap.position.copy(centre).addScaledVector(plane.normal, -plane.distanceToPoint(centre));
      p.cap.quaternion.setFromUnitVectors(AXES.z, plane.normal);
      p.cap.scale.set(radius * 4, radius * 4, 1);
    }
  }
  $('cutVal').textContent = on ? section.axis.toUpperCase() + ' ' + section.value.toFixed(2) : '';
  dirty = true;
}

function updateSectionRange(reset) {
  if (!section.axis) return;
  const i = { x: 'x', y: 'y', z: 'z' }[section.axis];
  const lo = bounds.min[i], hi = bounds.max[i];
  const r = $('cut');
  r.min = lo; r.max = hi; r.step = Math.max((hi - lo) / 1000, 0.001);
  if (reset || section.value < lo || section.value > hi) section.value = (lo + hi) / 2;
  r.value = section.value;
}

document.querySelectorAll('[data-axis]').forEach((b) => b.addEventListener('click', () => {
  section.axis = b.dataset.axis;
  document.querySelectorAll('[data-axis]').forEach((x) => x.classList.toggle('on', x === b));
  $('sliderRow').hidden = !section.axis;
  updateSectionRange(true);
  applySection();
}));
$('flip').onclick = () => { section.flip = !section.flip; $('flip').classList.toggle('on', section.flip); applySection(); };
$('cut').oninput = () => { section.value = parseFloat($('cut').value); applySection(); };
$('caps').onchange = applySection;

// ------------------------------------------------------------------ views
// Same presets as the server's renders: the eye sits along (sin az cos el, −cos az cos el, sin el), so Front looks toward +Y.
const VIEWS = { iso: [45, 35.264], front: [0, 0], back: [180, 0], right: [90, 0], left: [-90, 0], top: [0, 89.99], bottom: [180, -89.99] };
function viewDir(az, el) {
  const a = THREE.MathUtils.degToRad(az), e = THREE.MathUtils.degToRad(el);
  return new THREE.Vector3(Math.sin(a) * Math.cos(e), -Math.cos(a) * Math.cos(e), Math.sin(e));
}

function visibleBox() {
  const b = new THREE.Box3();
  for (const p of parts) if (p.visible) b.expandByObject(p.mesh);
  return b.isEmpty() ? bounds.clone() : b;
}

function fit(dir) {
  const b = visibleBox();
  const c = b.getCenter(new THREE.Vector3());
  const r = Math.max(b.getSize(new THREE.Vector3()).length() / 2, 1);
  dir = (dir || camera.position.clone().sub(controls.target)).normalize();
  const dist = (r / Math.sin(THREE.MathUtils.degToRad(persp.fov / 2))) * 1.05;
  for (const cam of [persp, ortho]) cam.position.copy(c).addScaledVector(dir, dist);
  persp.near = dist / 200; persp.far = dist * 50;
  ortho.near = 0.01; ortho.far = dist + 10 * r;
  orthoHalf = r * 1.08; ortho.zoom = 1;
  controls.target.copy(c);
  resize();
  controls.update();
  dirty = true;
}
function setView(name) { fit(viewDir(...VIEWS[name])); }
document.querySelectorAll('[data-view]').forEach((b) => b.addEventListener('click', () => setView(b.dataset.view)));
$('fit').onclick = () => fit();

$('ortho').onchange = () => {
  const to = $('ortho').checked ? ortho : persp, from = camera;
  if (to === from) return;
  const t = controls.target, d = from.position.distanceTo(t), dir = from.position.clone().sub(t).normalize();
  const tan = Math.tan(THREE.MathUtils.degToRad(persp.fov / 2));
  if (to === ortho) { orthoHalf = d * tan; ortho.zoom = 1; ortho.position.copy(from.position); ortho.far = d + 10 * radius; }
  else { const h = orthoHalf / ortho.zoom; persp.position.copy(t).addScaledVector(dir, h / tan); }
  to.quaternion.copy(from.quaternion);
  camera = to;
  controls.object = to;
  resize();
  controls.update();
  dirty = true;
};

function resize() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  renderer.setSize(w, h, false);
  const a = w / h;
  persp.aspect = a; persp.updateProjectionMatrix();
  ortho.left = -orthoHalf * a; ortho.right = orthoHalf * a; ortho.top = orthoHalf; ortho.bottom = -orthoHalf;
  ortho.updateProjectionMatrix();
  dirty = true;
}
new ResizeObserver(resize).observe(canvas);

// ------------------------------------------------------------------ axis triad (bottom left)
const triScene = new THREE.Scene();
const triCam = new THREE.OrthographicCamera(-1.6, 1.6, 1.6, -1.6, 0.1, 10);
function label(text, colour) {
  const c = document.createElement('canvas'); c.width = c.height = 64;
  const g = c.getContext('2d'); g.fillStyle = colour; g.font = 'bold 44px sans-serif'; g.textAlign = 'center'; g.textBaseline = 'middle';
  g.fillText(text, 32, 34);
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(c), depthTest: false }));
  s.scale.set(0.55, 0.55, 1);
  return s;
}
for (const [axis, colour] of [['x', '#d9473b'], ['y', '#3aa655'], ['z', '#3b72d9']]) {
  const d = AXES[axis];
  triScene.add(new THREE.ArrowHelper(d, new THREE.Vector3(), 1, colour, 0.28, 0.16));
  const s = label(axis.toUpperCase(), colour); s.position.copy(d).multiplyScalar(1.3); triScene.add(s);
}
function drawTriad() {
  const size = 92;
  triCam.position.copy(camera.position).sub(controls.target).normalize().multiplyScalar(5);
  triCam.quaternion.copy(camera.quaternion);
  renderer.clearDepth();
  renderer.setViewport(4, 4, size, size);
  renderer.render(triScene, triCam);
  renderer.setViewport(0, 0, canvas.clientWidth, canvas.clientHeight);
}

// ------------------------------------------------------------------ pick a point, measure between two
const raycaster = new THREE.Raycaster();
const markers = new THREE.Group(); scene.add(markers);
let pickA = null, pickB = null, downAt = null;
function clearPick() { pickA = pickB = null; markers.clear(); $('info').hidden = true; dirty = true; }
function fmt(v) { return '(' + [v.x, v.y, v.z].map((x) => x.toFixed(2)).join(', ') + ') mm'; }
function marker(p, colour) {
  const m = new THREE.Mesh(new THREE.SphereGeometry(radius * 0.008, 16, 12), new THREE.MeshBasicMaterial({ color: colour, depthTest: false }));
  m.position.copy(p); m.renderOrder = 3000; return m;
}
function showPick() {
  markers.clear();
  const lines = [];
  if (pickA) { markers.add(marker(pickA.point, 0xff5a1f)); lines.push(pickA.label + '  ' + fmt(pickA.point)); }
  if (pickB) {
    markers.add(marker(pickB.point, 0xff5a1f));
    const g = new THREE.BufferGeometry().setFromPoints([pickA.point, pickB.point]);
    const l = new THREE.Line(g, new THREE.LineBasicMaterial({ color: 0xff5a1f, depthTest: false })); l.renderOrder = 3000; markers.add(l);
    const d = pickB.point.clone().sub(pickA.point);
    lines.push(pickB.label + '  ' + fmt(pickB.point));
    lines.push('Distance ' + d.length().toFixed(3) + ' mm   Δx ' + d.x.toFixed(3) + '  Δy ' + d.y.toFixed(3) + '  Δz ' + d.z.toFixed(3));
  }
  $('info').hidden = !lines.length;
  $('info').textContent = lines.join('\n');
  $('info').style.whiteSpace = 'pre';
  dirty = true;
}
canvas.addEventListener('pointerdown', (e) => { downAt = [e.clientX, e.clientY]; });
canvas.addEventListener('pointerup', (e) => {
  if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 4 || e.button !== 0) return;
  const r = canvas.getBoundingClientRect();
  raycaster.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), camera);
  const meshes = parts.filter((p) => p.visible).map((p) => p.mesh);
  const hits = raycaster.intersectObjects(meshes, false).filter((h) => !section.axis || plane.distanceToPoint(h.point) >= -1e-6);
  if (!hits.length) { clearPick(); return; }
  const p = parts.find((q) => q.mesh === hits[0].object);
  const hit = { point: hits[0].point.clone(), label: p ? p.label : '?' };
  if (e.shiftKey && pickA) pickB = hit; else { pickA = hit; pickB = null; }
  showPick();
});
addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'f' || e.key === 'F') fit();
  if (e.key === 'Escape') clearPick();
});
$('toggleSide').onclick = () => { $('side').classList.toggle('collapsed'); resize(); };

// ------------------------------------------------------------------ status panel
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function ago(t) {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? Math.round(s) + ' s ago' : s < 3600 ? Math.round(s / 60) + ' min ago' : new Date(t * 1000).toLocaleString();
}
function renderStatus(info) {
  $('title').textContent = info.name || 'FreeCAD Live View';
  document.title = (info.name ? info.name + ' · ' : '') + 'FreeCAD Live View';
  const st = $('status');
  const badge = el('span', 'badge ' + (info.ok ? 'ok' : 'bad'), (info.ok ? '✓ ' : '✗ ') + (info.rev || ''));
  const n = (info.checks || []).length, pass = (info.checks || []).filter((c) => c.ok).length;
  const kids = [badge];
  if (n) kids.push(el('span', 'badge ' + (pass === n ? 'ok' : 'bad'), 'checks ' + pass + '/' + n));
  if (info.written) kids.push(el('span', null, 'updated ' + ago(info.written)));
  st.replaceChildren(...kids);
  const errs = [];
  if (info.script_error) errs.push(info.script_error);
  for (const e of info.errors || []) errs.push(e);
  const kept = info.glb && info.glb_rev && info.glb_rev !== info.rev;     // the scene kept an older model
  $('banner').hidden = (info.ok || !errs.length) && !kept;
  $('banner').textContent = (kept ? 'Showing the model from ' + info.glb_rev + ': this build made no geometry.\n' : '')
    + errs.slice(0, 6).join('\n');
  const ch = $('checks');
  $('checkCount').textContent = n ? pass + '/' + n : '';
  if (!n) { ch.className = 'muted'; ch.textContent = 'No checks in checks.json'; }
  else {
    ch.className = '';
    ch.replaceChildren(...info.checks.map((c) => {
      const row = el('div', 'check');
      row.append(el('span', 'mk ' + (c.ok ? 'ok' : 'bad'), c.ok ? '✓' : '✗'), el('span', 'id', c.id + (c.type ? '  ·  ' + c.type : '')));
      row.append(el('span', 'val', String(c.value ?? '')));
      return row;
    }));
  }
  const pr = $('params');
  const ps = Object.entries(info.params || {});
  pr.replaceChildren(...ps.flatMap(([k, v]) => [el('span', null, k), el('span', null, String(v))]));
  $('paramsBox').hidden = !ps.length;
}

function setLive(on, text, title) {
  $('live').classList.toggle('on', on);
  $('liveText').textContent = text;
  $('live').title = title;
}

// ------------------------------------------------------------------ the build in this page, then the live feed
// The page carries the build it was written with. Next to it in build/viewer/, scene.js (status) and
// model.js (the model) are rewritten by every build. They are read through script elements, which browsers
// allow from a local file where fetch() is blocked; each calls one of these two functions.
let status = null, pendingModel = null, busy = false;

function showStatus(info) {
  status = info;
  renderStatus(info);
  if (!info.model) {
    if (parts.length) { clearModel(); renderParts(); modelVersion = null; dirty = true; }
    $('msg').textContent = 'This build has no solid geometry.';
  }
}

function queueModel(info, b64) {
  if (info && b64 && info.version !== modelVersion) { pendingModel = { info, b64 }; nextModel(); }
}

async function nextModel() {
  if (busy || !pendingModel) return;
  const { info, b64 } = pendingModel;
  pendingModel = null;
  busy = true;
  try {
    await loadModel(info, b64);
    modelVersion = info.version;
    $('msg').textContent = '';
  } catch (e) {
    $('msg').textContent = 'Could not load the model: ' + e.message;
  } finally {
    busy = false;
    nextModel();
  }
}

let fed = null;
window.fcmcpScene = (info) => { fed = info; };
window.fcmcpModel = (info, b64) => queueModel(info, b64);

function readScript(name) {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = name + '?t=' + Date.now();
    s.onload = () => { s.remove(); resolve(); };
    s.onerror = () => { s.remove(); reject(new Error('cannot read ' + name)); };
    document.head.append(s);
  });
}

let everLive = false, askedModel = null;
async function poll() {
  if (!document.hidden) {
    try {
      fed = null;
      await readScript('scene.js');
      if (fed) {
        everLive = true;
        setLive(true, 'live', 'Updates after every build');
        showStatus(fed);
        if (fed.model && fed.model !== modelVersion && fed.model !== askedModel) {
          askedModel = fed.model;
          await readScript('model.js').catch(() => { askedModel = null; });
        }
      }
    } catch (e) {
      if (!everLive) {
        // a copy away from its build folder (a preview, an attachment): show the build it was written with
        setLive(false, 'snapshot', 'This copy shows ' + ((status && status.rev) || 'one build')
          + '. Open the file in the project\'s build/viewer folder to follow every build.');
        return;
      }
      setLive(false, 'not updating', 'scene.js next to this page can no longer be read');
    }
  }
  setTimeout(poll, POLL_MS);
}

function readInline(id) {
  const e = document.getElementById(id);
  return e ? e.textContent : '';
}

// ------------------------------------------------------------------ render loop (only when something changed)
function frame() {
  requestAnimationFrame(frame);
  if (controls.update()) dirty = true;
  if (!dirty) return;
  dirty = false;
  head.position.copy(camera.position);
  head.target.position.copy(controls.target);
  renderer.clear();
  renderer.render(scene, camera);
  drawTriad();
}
resize();
frame();
const first = JSON.parse(readInline('fcmcp-scene') || 'null');
if (first) showStatus(first); else $('msg').textContent = 'This page carries no build.';
queueModel(JSON.parse(readInline('fcmcp-model') || 'null'), readInline('fcmcp-glb'));
setLive(false, 'connecting', 'Looking for scene.js next to this page');
poll();
