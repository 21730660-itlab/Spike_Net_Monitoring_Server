/* Live alert bell, in-page popups, sound and system notifications on every page. */
(() => {
  const STORE = "spike-alerts-sound";
  const LAST = "spike-alerts-last-id";
  const bell = document.getElementById("bell");
  const count = document.getElementById("bell-count");
  const nav = document.getElementById("nav-alerts");
  const toggle = document.getElementById("sound-toggle");
  const toasts = document.getElementById("toasts");
  const get = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const set = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } };
  let soundOn = get(STORE) === "on";
  let audio = null;
  const stored = get(LAST);
  let lastId = stored === null ? null : Number(stored);

  function showToggle() {
    if (toggle) toggle.textContent = soundOn ? "🔊 Alerts sound on" : "🔇 Alerts sound off";
  }

  // A short two-tone alarm made with WebAudio (no sound file needed).
  function beep(critical) {
    if (!soundOn) return;
    try {
      audio = audio || new (window.AudioContext || window.webkitAudioContext)();
      const tones = critical ? [880, 660, 880, 660] : [740, 880];
      tones.forEach((f, i) => {
        const o = audio.createOscillator();
        const g = audio.createGain();
        o.frequency.value = f;
        o.type = "square";
        g.gain.value = 0.08;
        o.connect(g).connect(audio.destination);
        o.start(audio.currentTime + i * 0.22);
        o.stop(audio.currentTime + i * 0.22 + 0.18);
      });
    } catch (e) { /* audio not available */ }
  }

  function toast(a) {
    const el = document.createElement("div");
    el.className = `toast ${a.severity}`;
    el.innerHTML = `<b></b><span></span>`;
    el.querySelector("b").textContent = `${a.label}: ${a.device}`;
    el.querySelector("span").textContent = a.message || a.ip;
    el.addEventListener("click", () => { window.location = a.device_id ? `/devices/${a.device_id}` : "/alerts"; });
    toasts.appendChild(el);
    setTimeout(() => el.remove(), a.severity === "critical" ? 30000 : 12000);
    if (soundOn && "Notification" in window && Notification.permission === "granted" && document.hidden) {
      try { new Notification(`${a.label}: ${a.device}`, { body: a.message || a.ip, tag: `alert-${a.id}` }); } catch (e) { /* ignore */ }
    }
  }

  async function poll() {
    try {
      const url = lastId === null ? "/api/alerts/feed" : `/api/alerts/feed?after=${lastId}`;
      const res = await fetch(url, { headers: { Accept: "application/json" } });
      if (!res.ok) return;
      const f = await res.json();
      const n = f.active;
      [count, nav].forEach((el) => { if (el) { el.hidden = !n; el.textContent = n; } });
      if (f.new.length) {
        f.new.forEach(toast);
        beep(f.new.some((a) => a.severity === "critical"));
        bell.classList.remove("alarm"); void bell.offsetWidth; bell.classList.add("alarm");
      }
      lastId = f.max_id;  // the newest alert id on the server (also right after a database reset)
      set(LAST, String(lastId));
    } catch (e) { /* server restarting */ }
  }

  if (toggle) toggle.addEventListener("click", () => {
    soundOn = !soundOn;
    set(STORE, soundOn ? "on" : "off");
    showToggle();
    if (soundOn) {
      beep(false);  // also unlocks audio, which browsers allow only after a click
      if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
    }
  });
  showToggle();
  poll();
  setInterval(poll, 15000);
})();
