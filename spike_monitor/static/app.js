/* Small helpers for live pages. No build step, no framework. */
const SpikeUI = (() => {
  const label = (s) => (s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const pill = (s) => `<span class="pill ${esc(s)}">${esc(label(s))}</span>`;

  async function getJSON(url) {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (res.status === 401) { window.location = "/login"; throw new Error("logged out"); }
    if (!res.ok) throw new Error(res.statusText);
    return res.json();
  }

  function dashboard() {
    const refresh = async () => {
      try {
        const s = await getJSON("/api/summary");
        const vals = { ...s.counts, total: s.total, unknown: s.counts.unknown + s.counts.paused };
        document.querySelectorAll("#cards [data-k]").forEach((el) => { el.textContent = vals[el.dataset.k]; });
        document.getElementById("problems").innerHTML = s.problems.length
          ? s.problems.map((p) => `<tr><td>${pill(p.status)}</td><td><a href="/devices/${p.id}">${esc(p.name)}</a> <span class="muted">${esc(p.ip)}</span></td><td>${esc(p.site)}</td><td>${esc(p.since)}</td></tr>`).join("")
          : `<tr><td colspan="4" class="muted">✅ No problems.</td></tr>`;
        if (s.engine.last_cycle_at) {
          document.getElementById("engine-info").textContent =
            `Last check: ${s.engine.last_cycle_at} · took ${s.engine.last_cycle_seconds.toFixed(1)}s · every ${s.engine.interval}s`;
        }
      } catch (e) { /* keep last values on network errors */ }
    };
    setInterval(refresh, 10000);
  }

  function deviceList() {
    const q = document.getElementById("q");
    const site = document.getElementById("f-site");
    const status = document.getElementById("f-status");
    const brand = document.getElementById("f-brand");
    const rows = [...document.querySelectorAll("#devices tbody tr[data-id]")];
    const apply = () => {
      const text = q.value.trim().toLowerCase();
      rows.forEach((r) => {
        const show = (!text || r.dataset.text.includes(text)) && (!site.value || r.dataset.site === site.value)
          && (!status.value || r.dataset.status === status.value) && (!brand.value || r.dataset.brand === brand.value);
        r.style.display = show ? "" : "none";
      });
    };
    [q, site, status, brand].forEach((el) => el.addEventListener("input", apply));
    setInterval(() => { if (!q.value && !site.value && !status.value && !brand.value) window.location.reload(); }, 60000);
  }

  const timeLabel = (t) => new Date(t + "Z").toLocaleString([], { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });

  function fmtBps(v) {
    if (v == null) return "—";
    const units = ["bps", "Kbps", "Mbps", "Gbps"];
    let i = 0;
    while (Math.abs(v) >= 1000 && i < units.length - 1) { v /= 1000; i++; }
    return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
  }

  function lineChart(canvas, datasets, scales) {
    const css = getComputedStyle(document.documentElement);
    return new Chart(canvas, {
      type: "line",
      data: { labels: [], datasets: datasets.map((d) => ({ pointRadius: 0, borderWidth: 1.5, ...d })) },
      options: {
        animation: false, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        scales: { x: { ticks: { maxTicksLimit: 8, color: css.getPropertyValue("--muted").trim() } }, ...scales },
      },
    });
  }

  // Traffic and health graphs on the device page (they follow the same time range selector).
  function deviceStats(deviceId) {
    const range = document.getElementById("range");
    const iface = document.getElementById("iface");
    const css = getComputedStyle(document.documentElement);
    const color = (name) => css.getPropertyValue(name).trim();
    const traffic = lineChart(document.getElementById("traffic"), [
      { label: "Download (rx)", data: [], borderColor: color("--accent"), backgroundColor: "rgba(37,99,235,.12)", fill: true },
      { label: "Upload (tx)", data: [], borderColor: color("--up") },
    ], { y: { beginAtZero: true, ticks: { callback: fmtBps } } });
    traffic.options.plugins = { tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmtBps(c.parsed.y)}` } } };
    const health = lineChart(document.getElementById("health"), [
      { label: "CPU %", data: [], borderColor: color("--down"), yAxisID: "y" },
      { label: "Memory %", data: [], borderColor: color("--accent"), yAxisID: "y" },
      { label: "Temperature °C", data: [], borderColor: color("--warning"), yAxisID: "y" },
      { label: "Clients", data: [], borderColor: color("--unreachable"), yAxisID: "y1", hidden: true },
    ], { y: { beginAtZero: true, suggestedMax: 100 }, y1: { beginAtZero: true, position: "right", grid: { drawOnChartArea: false } } });

    const load = async () => {
      if (iface && iface.value) {
        const t = await getJSON(`/devices/${deviceId}/traffic?interface=${encodeURIComponent(iface.value)}&hours=${range.value}`);
        traffic.data.labels = t.ts.map(timeLabel);
        traffic.data.datasets[0].data = t.rx;
        traffic.data.datasets[1].data = t.tx;
        traffic.update();
      }
      const m = await getJSON(`/devices/${deviceId}/metrics?hours=${range.value}`);
      health.data.labels = m.ts.map(timeLabel);
      health.data.datasets[0].data = m.cpu;
      health.data.datasets[1].data = m.mem;
      health.data.datasets[2].data = m.temperature;
      health.data.datasets[3].data = m.clients;
      health.update();
    };
    range.addEventListener("change", load);
    if (iface) iface.addEventListener("change", load);
    load();
    setInterval(load, 60000);
  }

  function deviceChart(deviceId) {
    const ctx = document.getElementById("chart");
    const range = document.getElementById("range");
    const css = getComputedStyle(document.documentElement);
    const chart = new Chart(ctx, {
      type: "line",
      data: { labels: [], datasets: [
        { label: "Latency (ms)", data: [], borderColor: css.getPropertyValue("--accent").trim(), pointRadius: 0, borderWidth: 1.5, yAxisID: "y", spanGaps: false },
        { label: "Loss (%)", data: [], borderColor: css.getPropertyValue("--down").trim(), backgroundColor: "rgba(220,38,38,.15)", fill: true, pointRadius: 0, borderWidth: 1, yAxisID: "y1", stepped: true },
      ] },
      options: {
        animation: false, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        scales: {
          x: { ticks: { maxTicksLimit: 8, color: css.getPropertyValue("--muted").trim() } },
          y: { beginAtZero: true, title: { display: true, text: "ms" } },
          y1: { beginAtZero: true, max: 100, position: "right", grid: { drawOnChartArea: false }, title: { display: true, text: "%" } },
        },
      },
    });
    const load = async () => {
      const h = await getJSON(`/devices/${deviceId}/history?hours=${range.value}`);
      chart.data.labels = h.ts.map(timeLabel);
      chart.data.datasets[0].data = h.latency;
      chart.data.datasets[1].data = h.loss;
      chart.update();
    };
    range.addEventListener("change", load);
    load();
    setInterval(load, 30000);
  }

  return { dashboard, deviceList, deviceChart, deviceStats };
})();
