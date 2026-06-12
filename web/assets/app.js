/* sys-ops fleet dashboard */
"use strict";

const ENV_COLORS = {
  test: "var(--env-test)",
  development: "var(--env-development)",
  production: "var(--env-production)",
  unassigned: "var(--unknown)",
};

const LINK_COLORS = { dn42: "#38bdf8", vpn: "#34d399", custom: "#fbbf24" };

const TILES = {
  dark: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
  light: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
};
const TILE_ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>';

const state = {
  data: null,
  env: "all",
  search: "",
  map: null,
  tileLayer: null,
  markerLayer: null,
  linkLayer: null,
};

const $ = (sel) => document.querySelector(sel);
const isPublic = () => state.data.site.mode === "public";
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));

/* --- theme & accent -------------------------------------------------------- */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("sysops-theme", theme);
  if (state.map) setTiles();
}

function applyAccent(color) {
  document.documentElement.style.setProperty("--accent", color);
  localStorage.setItem("sysops-accent", color);
  $("#accent-input").value = toHex(color);
}

function toHex(color) {
  const ctx = document.createElement("canvas").getContext("2d");
  ctx.fillStyle = color;
  return ctx.fillStyle;
}

/* --- data ------------------------------------------------------------------- */

function visibleNodes() {
  const q = state.search.toLowerCase();
  return state.data.nodes.filter((n) => {
    if (state.env !== "all" && n.environment !== state.env) return false;
    if (!q) return true;
    return [n.name, n.environment, (n.location || {}).label, ...(n.components || [])]
      .join(" ").toLowerCase().includes(q);
  });
}

/* --- header / filters ---------------------------------------------------------- */

function renderHeader() {
  const site = state.data.site;
  document.title = `${site.title} — fleet dashboard`;
  $("#site-title").textContent = site.title;
  $("#site-tagline").textContent = [site.subtitle, site.tagline].filter(Boolean).join(" · ");
  $("#site-footer").innerHTML = site.footer || "";

  if (isPublic()) {
    $("#env-filters").innerHTML = "";
    return;
  }
  const envs = ["all", "test", "development", "production"];
  $("#env-filters").innerHTML = envs
    .map((env) => {
      const count = env === "all"
        ? state.data.nodes.length
        : state.data.nodes.filter((n) => n.environment === env).length;
      return `<button data-env="${env}" class="${env === state.env ? "active" : ""}">
        ${env} <span style="opacity:.6">${count}</span></button>`;
    })
    .join("");
  $("#env-filters").querySelectorAll("button").forEach((btn) =>
    btn.addEventListener("click", () => {
      state.env = btn.dataset.env;
      renderHeader();
      renderAll();
    })
  );
}

/* --- stats ------------------------------------------------------------------------ */

function renderStats() {
  const nodes = visibleNodes();
  const peerings = nodes.reduce((acc, n) => acc + ((n.dn42 || {}).peers || []).length, 0);
  const stats = isPublic()
    ? [
        [nodes.length, "routers"],
        [nodes.filter((n) => n.status === "up").length, "up"],
        [peerings, "peerings"],
        [new Set(nodes.map((n) => (n.location || {}).label).filter(Boolean)).size, "locations"],
      ]
    : [
        [nodes.length, "nodes"],
        [nodes.filter((n) => n.status === "up").length, "up"],
        [nodes.filter((n) => n.components.includes("dn42")).length, "dn42 routers"],
        [peerings, "dn42 peerings"],
        [nodes.filter((n) => n.retiring).length, "retiring"],
      ];
  $("#stats").innerHTML = stats
    .map(([num, lbl], i) =>
      `<div class="stat" style="animation-delay:${i * 60}ms">
         <div class="num" data-target="${num}">0</div>
         <div class="lbl">${lbl}</div>
       </div>`)
    .join("");
  $("#stats").querySelectorAll(".num").forEach(animateCount);
}

function animateCount(el) {
  const target = Number(el.dataset.target);
  const t0 = performance.now();
  const dur = 700;
  (function tick(t) {
    const p = Math.min((t - t0) / dur, 1);
    el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(tick);
  })(t0);
}

/* --- map ----------------------------------------------------------------------------- */

function setTiles() {
  const conf = state.data.site.map;
  const theme = document.documentElement.dataset.theme || "dark";
  const url = conf.tiles === "auto" ? TILES[theme] : (TILES[conf.tiles] || conf.tiles);
  if (state.tileLayer) state.tileLayer.remove();
  state.tileLayer = L.tileLayer(url, { attribution: TILE_ATTRIBUTION, maxZoom: 12 })
    .addTo(state.map);
}

function initMap() {
  const conf = state.data.site.map;
  state.map = L.map("map", {
    center: conf.center,
    zoom: conf.zoom,
    worldCopyJump: true,
    zoomControl: true,
    attributionControl: true,
  });
  setTiles();
  state.markerLayer = L.layerGroup().addTo(state.map);
  state.linkLayer = L.layerGroup().addTo(state.map);

  $("#map-legend").innerHTML =
    (isPublic()
      ? `<span><span class="legend-dot" style="background:var(--accent)"></span>router</span>`
      : Object.entries(ENV_COLORS)
          .filter(([env]) => env !== "unassigned")
          .map(([env, color]) =>
            `<span><span class="legend-dot" style="background:${color}"></span>${env}</span>`)
          .join("")) +
    `<span><span class="legend-dot" style="background:${LINK_COLORS.dn42}"></span>dn42 link</span>`;
}

/* Curved arc between two points (gentle quadratic bezier). */
function arcPoints(a, b, segments = 48, curvature = 0.2) {
  const [lat1, lon1] = a, [lat2, lon2] = b;
  const mLat = (lat1 + lat2) / 2, mLon = (lon1 + lon2) / 2;
  const dLat = lat2 - lat1, dLon = lon2 - lon1;
  const cLat = mLat + -dLon * curvature;
  const cLon = mLon + dLat * curvature;
  const pts = [];
  for (let i = 0; i <= segments; i++) {
    const t = i / segments;
    pts.push([
      (1 - t) ** 2 * lat1 + 2 * (1 - t) * t * cLat + t ** 2 * lat2,
      (1 - t) ** 2 * lon1 + 2 * (1 - t) * t * cLon + t ** 2 * lon2,
    ]);
  }
  return pts;
}

function renderMap() {
  const nodes = visibleNodes();
  const byName = Object.fromEntries(nodes.map((n) => [n.name, n]));
  state.markerLayer.clearLayers();
  state.linkLayer.clearLayers();

  state.data.links.forEach((link) => {
    const a = byName[link.from], b = byName[link.to];
    if (!a?.location || !b?.location) return;
    L.polyline(
      arcPoints([a.location.lat, a.location.lon], [b.location.lat, b.location.lon]),
      {
        className: "flow-line",
        color: LINK_COLORS[link.type] || LINK_COLORS.custom,
        weight: 1.8,
        opacity: 0.75,
      }
    )
      .bindTooltip(esc(link.label || link.type), { sticky: true, className: "node-tip" })
      .addTo(state.linkLayer);
  });

  nodes.forEach((node) => {
    if (!node.location) return;
    const color = isPublic()
      ? "var(--accent)"
      : ENV_COLORS[node.environment] || ENV_COLORS.unassigned;
    const icon = L.divIcon({
      className: "",
      iconSize: [22, 22],
      html: `<div class="node-marker" style="--marker-color:${color};width:22px;height:22px">
               <div class="pulse"></div><div class="core"></div>
             </div>`,
    });
    L.marker([node.location.lat, node.location.lon], { icon })
      .bindTooltip(
        `<b>${esc(node.name)}</b><br>${esc(node.location.label || "")}`,
        { className: "node-tip", direction: "top", offset: [0, -8] }
      )
      .on("click", () => openDrawer(node))
      .addTo(state.markerLayer);
  });
}

/* --- node grid ------------------------------------------------------------------------- */

function chipsFor(node) {
  if (isPublic()) return `<span class="chip comp">dn42 router</span>`;
  let html = `<span class="chip env-${esc(node.environment)}">${esc(node.environment)}</span>`;
  node.components.forEach((c) => (html += `<span class="chip comp">${esc(c)}</span>`));
  if (node.retiring) html += `<span class="chip retiring">retiring</span>`;
  return html;
}

function renderNodes() {
  const nodes = visibleNodes();
  if (!nodes.length) {
    $("#node-grid").innerHTML =
      `<div class="empty">no nodes match — add one with <code>scripts/new-node.py</code></div>`;
    return;
  }
  $("#node-grid").innerHTML = nodes
    .map((node, i) =>
      `<div class="node-card" data-node="${esc(node.name)}" style="animation-delay:${i * 40}ms">
         <div class="row">
           <h3>${esc(node.name)}</h3>
           <span class="status-dot status-${esc(node.status)}" title="${esc(node.status)}"></span>
         </div>
         <div class="loc">${esc((node.location || {}).label || "")}</div>
         ${isPublic() && node.dn42?.ownip
           ? `<div class="loc" style="font-family:ui-monospace,monospace">${esc(node.dn42.ownip)}</div>`
           : ""}
         <div>${chipsFor(node)}</div>
       </div>`)
    .join("");
  $("#node-grid").querySelectorAll(".node-card").forEach((card) =>
    card.addEventListener("click", () => {
      const node = state.data.nodes.find((n) => n.name === card.dataset.node);
      if (node) openDrawer(node);
    })
  );
}

/* --- dn42 table ---------------------------------------------------------------------------- */

function renderDn42() {
  const rows = [];
  visibleNodes().forEach((node) => {
    (((node.dn42 || {}).peers) || []).forEach((peer) => {
      rows.push(
        `<tr>
           <td>${esc(node.name)}</td>
           <td>${esc(peer.name)}</td>
           <td class="mono">${peer.asn ? "AS" + esc(peer.asn) : "—"}</td>
           <td class="mono">${esc(peer.endpoint) || "<i>passive</i>"}</td>
         </tr>`);
    });
  });
  $("#dn42-table tbody").innerHTML =
    rows.join("") ||
    `<tr><td colspan="4" class="empty">no peerings configured yet</td></tr>`;
}

/* --- drawer ------------------------------------------------------------------------------------ */

function openDrawer(node) {
  const dn42 = node.dn42;
  const kv = [["status", node.status]];
  if (!isPublic()) {
    kv.push(["environment", node.environment]);
    kv.push(["components", node.components.join(", ") || "none"]);
    if (node.address) kv.push(["address", node.address]);
  }
  if (node.location) kv.push(["location", node.location.label || `${node.location.lat}, ${node.location.lon}`]);
  if (dn42?.ownip) kv.push(["dn42 IPv4", dn42.ownip]);
  if (dn42?.ownip6) kv.push(["dn42 IPv6", dn42.ownip6]);
  if (dn42?.endpoint) kv.push(["endpoint", dn42.endpoint]);

  let html = `
    <h3>${esc(node.name)}</h3>
    <div class="sub">${chipsFor(node)}</div>
    <dl class="kv">${kv.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;

  if (dn42) {
    html += `<h4>dn42 peerings (${dn42.peers.length})</h4>`;
    html += dn42.peers.length
      ? dn42.peers
          .map((p) =>
            `<div class="peer-item"><b>${esc(p.name)}</b> · AS${esc(p.asn ?? "?")}
             <div class="mono">${esc(p.endpoint) || "passive"}</div></div>`)
          .join("")
      : `<div class="empty">none yet — edit host_vars and run playbooks/dn42.yml</div>`;
  }

  if (!isPublic()) {
    html += `<h4>operations</h4>
      <dl class="kv">
        <dt>onboard</dt><dd>onboard.yml -l ${esc(node.name)}</dd>
        <dt>maintain</dt><dd>maintenance.yml -l ${esc(node.name)}</dd>
        ${dn42 ? `<dt>dn42</dt><dd>dn42.yml -l ${esc(node.name)}</dd>` : ""}
      </dl>`;
  }

  $("#drawer-body").innerHTML = html;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawer-scrim").classList.add("show");
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#drawer-scrim").classList.remove("show");
}

/* --- boot ------------------------------------------------------------------------------------------ */

function renderAll() {
  if ($("#stats")) renderStats();
  if (state.map) renderMap();
  if ($("#node-grid")) renderNodes();
  if ($("#dn42-table")) renderDn42();
}

async function boot() {
  applyTheme(localStorage.getItem("sysops-theme") || "dark");

  const res = await fetch("data/fleet.json", { cache: "no-store" });
  state.data = await res.json();

  const pub = state.data.site.public || {};
  if (isPublic() && Object.keys(pub).length) {
    $("#peering-panel").hidden = false;
    $("#peering-info").innerHTML = [
      ["ASN", pub.asn],
      ["contact", pub.contact],
      ["policy", pub.policy],
    ]
      .filter(([, v]) => v)
      .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`)
      .join("");
  }

  const panels = state.data.site.panels || {};
  if (panels.stats === false) $("#stats").remove();
  if (panels.map === false) $("#map-panel").remove();
  if (panels.nodes === false) $("#nodes-panel").remove();
  if (panels.dn42 === false) $("#dn42-panel").remove();

  applyAccent(localStorage.getItem("sysops-accent") || state.data.site.accent);

  renderHeader();
  if (panels.map !== false) {
    if (typeof L !== "undefined") {
      initMap();
    } else {
      $("#map").innerHTML =
        `<div class="empty" style="padding:40px;text-align:center">map unavailable — Leaflet failed to load</div>`;
    }
  }
  renderAll();

  $("#search").addEventListener("input", (e) => {
    state.search = e.target.value;
    renderAll();
  });
  $("#accent-input").addEventListener("input", (e) => applyAccent(e.target.value));
  $("#theme-toggle").addEventListener("click", () =>
    applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#drawer-scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => e.key === "Escape" && closeDrawer());
}

boot().catch((err) => {
  document.body.insertAdjacentHTML(
    "beforeend",
    `<div style="position:fixed;inset:auto 16px 16px;background:#7f1d1d;color:#fff;
       padding:12px 18px;border-radius:10px;z-index:2000">
       failed to load data/fleet.json — run <code>scripts/build-site.py</code> (${esc(err.message)})
     </div>`
  );
});
