/* Network map: topology (vis-network) and geographic (Leaflet) views. */
(() => {
  const css = getComputedStyle(document.documentElement);
  const C = (name) => css.getPropertyValue(name).trim();
  const STATUS_COLOR = {
    up: C("--up"), warning: C("--warning"), down: C("--down"), unreachable: C("--unreachable"),
    unknown: C("--unknown"), paused: C("--paused"),
  };
  const TEXT = C("--text");
  const SHAPES = { upstream: "star", router: "box", access_point: "triangle", ptp_link: "diamond",
                   station: "dot", switch: "square", server: "database", other: "dot" };
  const INTERNET = "internet";
  const label = (s) => (s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const TYPE_LABEL = { wired: "Wired", wireless: "Wireless", fiber: "Fiber", mixed: "Mixed" };
  const isDown = (st) => st === "down" || st === "unreachable";
  // Wireless links are dashed, wired solid, fiber solid and thicker. Colour shows the status.
  const linkStyle = (type) => (type === "wireless" ? [8, 6] : false);
  const linkWidth = (type, status, base) => (isDown(status) ? base + 2 : base + (type === "fiber" ? 2 : 0));

  function linkText(d) {
    const ports = [d.uplink_port, d.local_port].filter(Boolean).join(" → ");
    const sig = d.signal != null && d.uplink_type === "wireless" ? `${Math.round(d.signal)} dBm` : "";
    return [ports, d.speed, sig].filter(Boolean).join(" · ");
  }

  function linkTitle(d) {
    const lines = [`${TYPE_LABEL[d.uplink_type] || "Connection type not set"} link`];
    lines.push(`${d.parent_name}${d.uplink_port ? ` (${d.uplink_port})` : ""} → ${d.name}${d.local_port ? ` (${d.local_port})` : ""}`);
    if (d.speed) lines.push(`Speed: ${d.speed}`);
    if (d.parent_ip) lines.push(`${d.parent_name} port IP: ${d.parent_ip}`);
    if (d.local_ip) lines.push(`${d.name} port IP: ${d.local_ip}`);
    if (d.signal != null && d.uplink_type === "wireless") lines.push(`Signal: ${Math.round(d.signal)} dBm`);
    lines.push(`Status: ${label(d.status)}`);
    return lines.join("\n");
  }

  let data = null;
  let view = "sites";
  let network = null;
  let nodes = null;
  let edges = null;
  let leaflet = null;
  let placingSite = null;
  const topoEl = document.getElementById("topology");
  const geoEl = document.getElementById("geo");
  const siteFilter = document.getElementById("site-filter");
  const saveBtn = document.getElementById("save-layout");

  async function load() {
    const res = await fetch("/api/map", { headers: { Accept: "application/json" } });
    if (res.status === 401) { window.location = "/login"; return; }
    data = await res.json();
    const current = siteFilter.value;
    siteFilter.innerHTML = '<option value="">All sites</option>' +
      data.sites.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
    siteFilter.value = current;
  }

  // ---------------------------------------------------------------- topology
  function siteSummary(s) {
    const c = s.counts;
    const bad = (c.down || 0) + (c.unreachable || 0);
    let line = `${s.total} device${s.total === 1 ? "" : "s"}`;
    if (bad) line += ` · ${bad} down`;
    else if (c.warning) line += ` · ${c.warning} degraded`;
    return line;
  }

  function nodeColor(status, bg) {
    const col = STATUS_COLOR[status] || STATUS_COLOR.unknown;
    return bg ? { background: col, border: col, highlight: { background: col, border: TEXT } }
              : { background: C("--panel"), border: col, highlight: { background: C("--panel"), border: TEXT } };
  }

  function buildGraph() {
    const n = [];
    const e = [];
    if (view === "sites") {
      data.sites.forEach((s) => n.push({
        id: `s${s.id}`, label: `${s.name}\n${siteSummary(s)}`, shape: "box", margin: 12, borderWidth: 3,
        color: nodeColor(s.status, false), font: { color: TEXT, multi: false, size: 15 },
        x: s.x ?? undefined, y: s.y ?? undefined, title: s.devices.map((d) => `${d.name}: ${label(d.status)}`).join("\n"),
      }));
      data.site_links.forEach((l) => {
        const first = l.links[0] || {};
        const ports = l.count === 1 ? [first.parent_port, first.child_port].filter(Boolean).join(" → ") : `${l.count} links`;
        const sig = l.signal != null && l.type !== "wired" && l.type !== "fiber" ? `${Math.round(l.signal)} dBm` : "";
        e.push({
          id: `l${l.from}-${l.to}`, from: `s${l.from}`, to: `s${l.to}`,
          width: linkWidth(l.type, l.status, Math.min(2 + l.count, 6)),
          color: { color: STATUS_COLOR[l.status] || STATUS_COLOR.unknown }, dashes: linkStyle(l.type),
          label: [TYPE_LABEL[l.type], ports, sig].filter(Boolean).join(" · "),
          font: { color: TEXT, strokeWidth: 3, strokeColor: C("--bg"), size: 12 },
          title: l.links.map((k) => `${TYPE_LABEL[k.type] || "?"}: ${k.parent}${k.parent_port ? ` (${k.parent_port})` : ""} → ${k.child}${k.child_port ? ` (${k.child_port})` : ""}${k.speed ? ` · ${k.speed}` : ""}`).join("\n"),
        });
      });
      const upstreamSites = data.sites.filter((s) => s.upstream);
      if (upstreamSites.length) {
        n.push(internetNode());
        upstreamSites.forEach((s) => e.push({ id: `i${s.id}`, from: INTERNET, to: `s${s.id}`, width: 3, color: { color: C("--accent") } }));
      }
    } else {
      const only = siteFilter.value;
      const shown = data.devices.filter((d) => !only || String(d.site_id) === only);
      const ids = new Set(shown.map((d) => d.id));
      const siteName = Object.fromEntries(data.sites.map((s) => [s.id, s.name]));
      shown.forEach((d) => n.push({
        id: d.id, label: `${d.name}\n${d.ip}`, shape: SHAPES[d.type] || "dot", size: d.type === "upstream" ? 22 : 16,
        color: nodeColor(d.status, true), font: { color: TEXT, size: 13 }, borderWidth: 2,
        x: d.x ?? undefined, y: d.y ?? undefined,
        title: `${d.name} (${label(d.type)})\n${d.ip}\nSite: ${siteName[d.site_id] || "—"}\nStatus: ${label(d.status)}` +
               (d.latency != null ? `\nLatency: ${d.latency.toFixed(1)} ms` : "") +
               (d.signal != null ? `\nSignal: ${Math.round(d.signal)} dBm` : "") +
               (d.clients != null ? `\nClients: ${d.clients}` : ""),
      }));
      shown.filter((d) => d.parent_id && ids.has(d.parent_id)).forEach((d) => e.push({
        id: `e${d.id}`, from: d.parent_id, to: d.id, width: linkWidth(d.uplink_type, d.status, 2),
        color: { color: STATUS_COLOR[d.status] || STATUS_COLOR.unknown }, dashes: linkStyle(d.uplink_type),
        label: linkText(d), title: linkTitle(d),
        font: { color: TEXT, strokeWidth: 3, strokeColor: C("--bg"), size: 11 },
      }));
      const upstream = shown.filter((d) => d.type === "upstream");
      if (upstream.length) {
        n.push(internetNode());
        upstream.forEach((d) => e.push({ id: `i${d.id}`, from: INTERNET, to: d.id, width: 3, color: { color: C("--accent") } }));
      }
    }
    return { n, e };
  }

  function internetNode() {
    return { id: INTERNET, label: "🌐 Internet\n(upstream ISP)", shape: "ellipse", margin: 10,
             color: { background: C("--accent"), border: C("--accent") }, font: { color: "#fff", size: 14 } };
  }

  function drawTopology(forceLayout = false) {
    const { n, e } = buildGraph();
    const positioned = n.filter((x) => x.x !== undefined).length;
    const needLayout = forceLayout || positioned < n.length;
    if (forceLayout) n.forEach((x) => { delete x.x; delete x.y; });
    nodes = new vis.DataSet(n);
    edges = new vis.DataSet(e);
    if (network) network.destroy();
    network = new vis.Network(topoEl, { nodes, edges }, {
      physics: { enabled: needLayout, solver: "forceAtlas2Based", stabilization: { iterations: 400 },
                 forceAtlas2Based: { gravitationalConstant: -80, springLength: view === "sites" ? 180 : 110 } },
      interaction: { hover: true, tooltipDelay: 150, navigationButtons: true, keyboard: true },
      edges: { smooth: { type: "continuous" }, arrows: { to: { enabled: false } } },
    });
    network.once("stabilizationIterationsDone", () => network.setOptions({ physics: false }));
    network.on("doubleClick", (p) => {
      if (!p.nodes.length) return;
      const id = p.nodes[0];
      if (id === INTERNET) return;
      if (view === "sites") { siteFilter.value = String(id).slice(1); setView("devices"); }
      else window.location = `/devices/${id}`;
    });
    if (saveBtn) network.on("dragEnd", (p) => { if (p.nodes.length) saveBtn.classList.add("primary"); });
  }

  // Refresh colours and labels without moving anything.
  function refreshTopology() {
    if (!network) return;
    const { n, e } = buildGraph();
    const nodeIds = new Set(nodes.getIds());
    if (n.length !== nodeIds.size || n.some((x) => !nodeIds.has(x.id))) { drawTopology(); return; }
    nodes.update(n.map(({ x, y, ...rest }) => rest));
    edges.clear();
    edges.add(e);
  }

  async function saveLayout() {
    const pos = network.getPositions();
    const positions = {};
    Object.entries(pos).forEach(([id, p]) => {
      if (id === INTERNET) return;
      positions[view === "sites" ? id.slice(1) : id] = [Math.round(p.x), Math.round(p.y)];
    });
    const res = await fetch("/api/map/layout", {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": window.SPIKE_CSRF },
      body: JSON.stringify({ kind: view === "sites" ? "site" : "device", positions }),
    });
    if (res.ok) {
      saveBtn.classList.remove("primary");
      saveBtn.textContent = "✓ Saved";
      setTimeout(() => { saveBtn.textContent = "💾 Save layout"; }, 1500);
      await load();
    } else alert("Could not save the layout.");
  }

  // ---------------------------------------------------------------- geographic
  function pin(status) {
    return L.divIcon({ className: "", html: `<div class="site-pin" style="background:${STATUS_COLOR[status] || STATUS_COLOR.unknown}"></div>`,
                       iconSize: [22, 22], iconAnchor: [11, 11], popupAnchor: [0, -10] });
  }

  function drawGeo() {
    if (!leaflet) {
      leaflet = L.map(geoEl, { zoomControl: true }).setView([33.85, 35.86], 9);
      L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19, attribution: "&copy; OpenStreetMap contributors",
      }).addTo(leaflet);
      leaflet.on("click", (ev) => { if (placingSite) saveLocation(placingSite, ev.latlng); });
      leaflet.layer = L.layerGroup().addTo(leaflet);
      leaflet.firstFit = true;
    }
    leaflet.invalidateSize();
    leaflet.layer.clearLayers();
    const placed = data.sites.filter((s) => s.lat != null && s.lon != null && s.id);
    const byId = Object.fromEntries(placed.map((s) => [s.id, s]));
    data.site_links.forEach((l) => {
      const a = byId[l.from];
      const b = byId[l.to];
      if (!a || !b) return;
      L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {
        color: STATUS_COLOR[l.status] || STATUS_COLOR.unknown, weight: linkWidth(l.type, l.status, Math.min(3 + l.count, 7)),
        dashArray: l.type === "wireless" ? "10 8" : null, opacity: 0.85,
      }).bindTooltip(`<b>${esc(a.name)} → ${esc(b.name)}</b> (${esc(TYPE_LABEL[l.type] || "type not set")})<br>` +
        l.links.map((k) => `${esc(k.parent)}${k.parent_port ? ` (${esc(k.parent_port)})` : ""} → ${esc(k.child)}${k.child_port ? ` (${esc(k.child_port)})` : ""}`).join("<br>") +
        (l.signal != null && l.type === "wireless" ? `<br>Signal ${Math.round(l.signal)} dBm` : ""))
        .addTo(leaflet.layer);
    });
    placed.forEach((s) => {
      const m = L.marker([s.lat, s.lon], { icon: pin(s.status), draggable: window.SPIKE_CAN_EDIT, title: s.name })
        .bindPopup(`<b>${esc(s.name)}</b><br>${esc(siteSummary(s))}<br>` +
                   s.devices.slice(0, 12).map((d) => `<a href="/devices/${d.id}">${esc(d.name)}</a> – ${esc(label(d.status))}`).join("<br>") +
                   (s.devices.length > 12 ? `<br>…and ${s.devices.length - 12} more` : ""))
        .bindTooltip(esc(s.name), { permanent: true, direction: "right", offset: [12, 0] })
        .addTo(leaflet.layer);
      m.on("dragend", () => saveLocation(s.id, m.getLatLng()));
    });
    if (placed.length && leaflet.firstFit) {
      leaflet.fitBounds(L.latLngBounds(placed.map((s) => [s.lat, s.lon])).pad(0.3), { maxZoom: 13 });
      leaflet.firstFit = false;
    }
    const missing = data.sites.filter((s) => (s.lat == null || s.lon == null) && s.id);
    const box = document.getElementById("geo-missing");
    box.style.display = missing.length ? "" : "none";
    box.innerHTML = missing.length
      ? `Sites without a location: ${missing.map((s) => window.SPIKE_CAN_EDIT
          ? `<button class="btn small" data-place="${s.id}">📍 Place ${esc(s.name)}</button>` : esc(s.name)).join(" ")}` +
        (window.SPIKE_CAN_EDIT ? `<br>Click "Place", then click its spot on the map. You can also drag pins to move them.` : "")
      : "";
    box.querySelectorAll("[data-place]").forEach((b) => b.addEventListener("click", () => {
      placingSite = Number(b.dataset.place);
      geoEl.style.cursor = "crosshair";
      document.getElementById("map-hint").textContent = "Now click the site's location on the map.";
    }));
  }

  async function saveLocation(siteId, latlng) {
    placingSite = null;
    geoEl.style.cursor = "";
    const res = await fetch(`/api/map/sites/${siteId}/location`, {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": window.SPIKE_CSRF },
      body: JSON.stringify({ lat: latlng.lat, lon: latlng.lng }),
    });
    if (!res.ok) alert("Could not save the location.");
    await load();
    drawGeo();
  }

  // ---------------------------------------------------------------- view switching
  function setView(v) {
    view = v;
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("primary", b.dataset.view === v));
    const geo = v === "geo";
    topoEl.style.display = geo ? "none" : "";
    geoEl.style.display = geo ? "" : "none";
    document.getElementById("geo-missing").style.display = "none";
    siteFilter.style.display = v === "devices" ? "" : "none";
    document.getElementById("auto-layout").style.display = geo ? "none" : "";
    if (saveBtn) saveBtn.style.display = geo ? "none" : "";
    document.getElementById("map-hint").textContent = {
      sites: "Double-click a site to see its devices. Drag to arrange" + (window.SPIKE_CAN_EDIT ? ", then Save layout." : "."),
      devices: "Double-click a device to open it. Labels show parent port → device port and wireless signal; hover a line for details.",
      geo: "Lines connect sites whose devices link to each other. Click a pin for details.",
    }[v];
    if (geo) drawGeo(); else drawTopology();
  }

  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
  siteFilter.addEventListener("change", () => drawTopology());
  document.getElementById("auto-layout").addEventListener("click", () => drawTopology(true));
  if (saveBtn) saveBtn.addEventListener("click", saveLayout);

  load().then(() => setView("sites"));
  setInterval(async () => {
    await load();
    if (view === "geo") drawGeo(); else refreshTopology();
  }, 20000);
})();
