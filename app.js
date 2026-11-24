(() => {
  const $ = (id) => document.getElementById(id);
  const LABELS = { vocals: "فوكال", drums: "درامز", bass: "باص", other: "آلات أخرى" };
  const ORDER = ["vocals", "drums", "bass", "other"];
  const ERR = {
    bad_type: "نوع الملف مش مدعوم", too_big: "الملف كبير أوي", busy: "السيرفر مشغول، جرب كمان شوية",
    rate: "وصلت للحد الأقصى من المحاولات، جرب بعد شوية", no_file: "اختار ملف الأول"
  };
  const STATUS = { queued: "في الانتظار…", decoding: "بقرا الأغنية…", loading: "بحمّل الموديل…",
    processing: "بفصل الأغنية…", encoding: "بجهّز الملفات…" };

  let code = sessionStorage.getItem("obito_code") || "";
  let jobId = null, ctx = null, buffers = {}, state = {}, sources = [];
  let playing = false, offset = 0, startedAt = 0, duration = 0, raf = 0;

  const api = (url, opts = {}) => {
    opts.headers = Object.assign({}, opts.headers, code ? { "X-Access-Code": code } : {});
    return fetch(url, opts);
  };
  const show = (id, on) => { $(id).hidden = !on; };
  const msg = (t) => { $("msg").textContent = t || ""; show("msg", !!t); };
  const fmt = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

  function ensureCode() {
    if (document.body.dataset.gated !== "1" || code) return true;
    const c = prompt("اكتب كود الدخول");
    if (!c) return false;
    code = c.trim(); sessionStorage.setItem("obito_code", code);
    return true;
  }

  // ---------- upload ----------
  $("file").addEventListener("change", (e) => e.target.files[0] && start(e.target.files[0]));
  const drop = $("drop");
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && start(e.dataTransfer.files[0]));

  function start(file) {
    msg("");
    if (!ensureCode()) return;
    show("upload", false); show("progress", true);
    $("fname").textContent = file.name;
    setBar(0, "بيترفع الملف…");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/upload");
    if (code) xhr.setRequestHeader("X-Access-Code", code);
    xhr.upload.onprogress = (e) => e.lengthComputable && setBar(e.loaded / e.total * 0.3, "بيترفع الملف…");
    xhr.onload = () => {
      let r = {}; try { r = JSON.parse(xhr.responseText); } catch (_) {}
      if (xhr.status === 401) { code = ""; sessionStorage.removeItem("obito_code"); return fail("كود الدخول غلط"); }
      if (xhr.status !== 200) return fail(ERR[r.error] || "حصل خطأ في الرفع");
      jobId = r.id; poll();
    };
    xhr.onerror = () => fail("مفيش اتصال بالسيرفر");
    const fd = new FormData(); fd.append("file", file); xhr.send(fd);
  }

  function setBar(p, text) { $("barfill").style.width = Math.round(p * 100) + "%"; $("statustext").textContent = text; }
  function fail(t) { show("progress", false); show("result", false); show("upload", true); $("file").value = ""; msg(t); }

  async function poll() {
    try {
      const r = await api(`/api/status/${jobId}`);
      if (!r.ok) return fail("المهمة اتمسحت أو منتهية");
      const s = await r.json();
      if (s.status === "error") return fail(s.error || "فشل الفصل");
      if (s.status === "done") return loadStems(s.stems);
      let text = STATUS[s.status] || "…";
      if (s.status === "queued" && s.position > 1) text += ` (دورك رقم ${s.position})`;
      if (s.status === "processing") text += ` ${Math.round(s.progress * 100)}%`;
      const p = s.status === "processing" ? 0.3 + s.progress * 0.65 : s.status === "encoding" ? 0.97 : 0.3;
      setBar(p, text);
    } catch (_) {}
    setTimeout(poll, 1000);
  }

  // ---------- player (Web Audio: all stems sample-synced) ----------
  async function loadStems(list) {
    setBar(0.97, "بحمّل المسارات…");
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    buffers = {}; state = {};
    sources = ORDER.filter((s) => list.includes(s));
    try {
      await Promise.all(sources.map(async (s) => {
        const r = await api(`/api/stem/${jobId}/${s}`);
        if (!r.ok) throw new Error("stem");
        buffers[s] = await ctx.decodeAudioData(await r.arrayBuffer());
        state[s] = { mute: false, solo: false, vol: 1, gain: null, node: null };
      }));
    } catch (_) { return fail("مقدرتش أحمّل المسارات"); }
    duration = buffers[sources[0]].duration; offset = 0;
    $("dur").textContent = fmt(duration);
    buildRows(); updateZip();
    show("progress", false); show("result", true);
  }

  function buildRows() {
    const box = $("stems"); box.textContent = "";
    sources.forEach((s) => {
      const row = document.createElement("div"); row.className = "stem"; row.id = "row-" + s;
      const name = document.createElement("div"); name.className = "name"; name.textContent = LABELS[s];
      const m = document.createElement("button"); m.textContent = "M"; m.title = "Mute";
      const so = document.createElement("button"); so.textContent = "S"; so.title = "Solo";
      const vol = document.createElement("input"); vol.type = "range"; vol.min = 0; vol.max = 100; vol.value = 100;
      const dl = document.createElement("a"); dl.className = "dl"; dl.textContent = "⬇"; dl.title = "Download";
      dl.href = `/api/stem/${jobId}/${s}?dl=1` + (code ? `&code=${encodeURIComponent(code)}` : "");
      m.onclick = () => { state[s].mute = !state[s].mute; applyGains(); };
      so.onclick = () => { state[s].solo = !state[s].solo; applyGains(); };
      vol.oninput = () => { state[s].vol = vol.value / 100; applyGains(); };
      row.append(name, m, so, vol, dl); box.append(row);
    });
    applyGains();
  }

  function applyGains() {
    const anySolo = sources.some((s) => state[s].solo);
    sources.forEach((s) => {
      const st = state[s];
      const audible = anySolo ? st.solo : !st.mute;
      if (st.gain) st.gain.gain.value = audible ? st.vol : 0;
      const row = $("row-" + s); if (!row) return;
      const [m, so] = row.querySelectorAll("button");
      m.classList.toggle("on", st.mute); so.classList.toggle("on", st.solo);
      row.classList.toggle("muted-row", !audible);
    });
  }

  function updateZip() {
    $("zip").href = `/api/zip/${jobId}` + (code ? `?code=${encodeURIComponent(code)}` : "");
  }

  function playFrom(t) {
    stopNodes();
    sources.forEach((s) => {
      const src = ctx.createBufferSource(); src.buffer = buffers[s];
      const g = ctx.createGain(); state[s].gain = g;
      src.connect(g).connect(ctx.destination);
      src.start(0, t); state[s].node = src;
    });
    applyGains();
    startedAt = ctx.currentTime - t; playing = true; $("play").textContent = "⏸";
    cancelAnimationFrame(raf); tick();
  }
  function stopNodes() { sources.forEach((s) => { try { state[s].node && state[s].node.stop(); } catch (_) {} state[s].node = null; }); }
  function pause() { offset = Math.min(duration, ctx.currentTime - startedAt); stopNodes(); playing = false; $("play").textContent = "▶"; cancelAnimationFrame(raf); }
  function tick() {
    const t = ctx.currentTime - startedAt;
    if (t >= duration) { pause(); offset = 0; $("seek").value = 0; $("cur").textContent = fmt(0); return; }
    $("seek").value = Math.round(t / duration * 1000); $("cur").textContent = fmt(t);
    raf = requestAnimationFrame(tick);
  }
  $("play").onclick = async () => { if (!ctx) return; await ctx.resume(); playing ? pause() : playFrom(offset); };
  $("seek").oninput = () => {
    offset = $("seek").value / 1000 * duration; $("cur").textContent = fmt(offset);
    if (playing) playFrom(offset);
  };
  $("again").onclick = () => {
    if (playing) pause();
    if (jobId) api(`/api/job/${jobId}`, { method: "DELETE" });
    if (ctx) { ctx.close(); ctx = null; }
    jobId = null; buffers = {}; $("file").value = "";
    show("result", false); show("upload", true); msg("");
  };
})();
