(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const MAX = parseInt(document.body.dataset.max, 10) || 0; // 0 = unlimited
  const store = {
    get: (k, d = "") => { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
  };

  const state = {
    voices: [],
    my: [], // saved cloned voices
    voice: store.get("vf_voice"),
    voiceA: store.get("vf_voiceA"),
    voiceB: store.get("vf_voiceB"),
    cloneBase: store.get("vf_cloneBase"), // manual base voice for one-off cloning
    autoBase: store.get("vf_autoBase", "1") === "1",
    speaker: "A",
    mode: "tts", // "tts" | "clone"
    favs: new Set(JSON.parse(store.get("vf_favs", "[]"))),
    results: [], // finished: {id, blob, url, label, text, time}
    pending: new Map(), // job_id -> {id, label, text, done, total, message, failed}
    previewAudio: null,
    previewToken: 0,
    playingKey: null, // which preview button is currently playing
    refFile: null,
    cloneInfo: null,
    editId: null,
    loaded: { voices: false, my: false },
  };

  const els = {
    text: $("text"), counter: $("counter"), generate: $("generateBtn"), status: $("status"),
    voiceList: $("voiceList"), search: $("search"), lang: $("langFilter"), gender: $("genderFilter"),
    dialogue: $("dialogueToggle"), dialogueHelp: $("dialogueHelp"), tabs: $("speakerTabs"),
    labelA: $("voiceALabel"), labelB: $("voiceBLabel"),
    rate: $("rate"), pitch: $("pitch"), volume: $("volume"),
    results: $("results"), zip: $("zipBtn"),
    clonePanel: $("clonePanel"), cloneNote: $("cloneNote"), refInput: $("refInput"),
    recBtn: $("recBtn"), refName: $("refName"), refAudio: $("refAudio"), consent: $("consent"),
    tau: $("tau"), tauOut: $("tauOut"), matchPitch: $("matchPitch"),
    saveName: $("saveName"), saveBtn: $("saveVoiceBtn"), saveMsg: $("saveMsg"),
    voiceTitle: $("voiceTitle"), stdVoices: $("stdVoices"), stdTitle: $("stdTitle"),
    autoBox: $("autoBaseBox"), autoBase: $("autoBase"),
    myList: $("myList"), myHint: $("myHint"), editor: $("editor"),
    edName: $("edName"), edBase: $("edBase"), edTau: $("edTau"), edTauOut: $("edTauOut"),
    edRate: $("edRate"), edRateOut: $("edRateOut"), edPitch: $("edPitch"), edPitchOut: $("edPitchOut"),
    edMatch: $("edMatch"), edMsg: $("edMsg"),
  };

  // ------------------------------------------------------------ theme
  const applyTheme = (t) => {
    document.documentElement.dataset.theme = t;
    $("themeBtn").textContent = t === "dark" ? "☀" : "☾";
  };
  applyTheme(store.get("vf_theme") ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
  $("themeBtn").onclick = () => {
    const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    store.set("vf_theme", t);
    applyTheme(t);
  };

  // ------------------------------------------------------------ helpers
  const setStatus = (msg, isError = false) => {
    els.status.textContent = msg;
    els.status.classList.toggle("error", isError);
  };
  const setMsg = (el, msg, isError = false) => {
    el.textContent = msg;
    el.classList.toggle("warn", isError);
  };
  const isMy = (k) => typeof k === "string" && k.startsWith("my:");
  const myById = (key) => state.my.find((m) => `my:${m.id}` === key);
  const voiceById = (id) => state.voices.find((v) => v.id === id);
  const langName = (code) => {
    try { return new Intl.DisplayNames(["en"], { type: "language" }).of(code) || code; }
    catch { return code; }
  };
  const countryName = (code) => {
    try { return new Intl.DisplayNames(["en"], { type: "region" }).of(code) || code; }
    catch { return code; }
  };
  const shortName = (key) => {
    if (isMy(key)) return (myById(key) || {}).name || "My voice";
    return voiceById(key) ? voiceById(key).name.split(" - ")[0] : key || "–";
  };
  const api = async (url, init) => {
    const res = await fetch(url, init);
    let data = {};
    try { data = await res.json(); } catch { /* not json */ }
    if (!res.ok) throw new Error(data.error || `Error ${res.status}`);
    return data;
  };

  // ------------------------------------------------------------ text box
  const updateCounter = () => {
    const t = els.text.value;
    const n = t.length;
    const words = (t.trim().match(/\S+/g) || []).length;
    const mins = words / 150; // ~150 spoken words per minute
    let est = "";
    if (words) {
      est = mins < 1 ? ` · ~${Math.max(1, Math.round(mins * 60))} sec`
        : mins < 90 ? ` · ~${Math.round(mins)} min` : ` · ~${(mins / 60).toFixed(1)} hr`;
    }
    els.counter.textContent = MAX
      ? `${n.toLocaleString()} / ${MAX.toLocaleString()}`
      : `${n.toLocaleString()} characters · ${words.toLocaleString()} words${est}`;
    els.counter.parentElement.classList.toggle("over", !!MAX && n > MAX);
  };
  els.text.addEventListener("input", updateCounter);
  $("clearBtn").onclick = () => { els.text.value = ""; updateCounter(); els.text.focus(); };

  document.querySelectorAll("[data-pause]").forEach((b) => {
    b.onclick = () => {
      const t = els.text, tag = `[pause:${b.dataset.pause}]`;
      const s = t.selectionStart, e = t.selectionEnd;
      t.value = t.value.slice(0, s) + tag + t.value.slice(e);
      t.selectionStart = t.selectionEnd = s + tag.length;
      t.focus(); updateCounter();
    };
  });

  $("fileInput").addEventListener("change", async (ev) => {
    const f = ev.target.files[0];
    if (!f) return;
    let txt = await f.text();
    if (f.name.toLowerCase().endsWith(".srt")) {
      txt = txt.replace(/\r/g, "")
        .split("\n")
        .filter((l) => l.trim() && !/^\d+$/.test(l.trim()) && !/-->/.test(l))
        .join(" ");
    }
    if (MAX && txt.length > MAX) {
      txt = txt.slice(0, MAX);
      setStatus(`File was longer than ${MAX} characters and was trimmed.`);
    }
    els.text.value = txt;
    updateCounter();
    ev.target.value = "";
  });

  // ------------------------------------------------------------ effects
  const fxOut = () => {
    $("rateOut").textContent = `${els.rate.value}%`;
    $("pitchOut").textContent = `${els.pitch.value} Hz`;
    $("volumeOut").textContent = `${els.volume.value}%`;
  };
  [els.rate, els.pitch, els.volume].forEach((r) => r.addEventListener("input", fxOut));
  $("resetFx").onclick = () => { els.rate.value = els.pitch.value = els.volume.value = 0; fxOut(); };
  const fxNow = () => ({ rate: +els.rate.value, pitch: +els.pitch.value, volume: +els.volume.value });

  els.tau.addEventListener("input", () => { els.tauOut.textContent = (+els.tau.value).toFixed(2); });

  // ------------------------------------------------------------ modes
  const useDialogue = () => state.mode === "tts" && els.dialogue.checked;
  const cloneAuto = () => state.mode === "clone" && state.autoBase;

  function setMode(mode) {
    state.mode = mode;
    document.querySelectorAll(".mode").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    els.clonePanel.hidden = mode !== "clone";
    els.autoBox.hidden = mode !== "clone";
    if (mode === "clone") {
      els.dialogue.checked = false;
      els.dialogue.disabled = true;
      els.generate.textContent = "Clone voice & generate";
      els.voiceTitle.textContent = "Voices";
      els.stdTitle.textContent = "Base voice (accent & style)";
      loadCloneInfo();
    } else {
      els.dialogue.disabled = false;
      els.generate.textContent = "Generate audio";
      els.voiceTitle.textContent = "Voice selection";
      els.stdTitle.textContent = "Voices";
    }
    syncDialogueUI();
  }
  document.querySelectorAll(".mode").forEach((b) => (b.onclick = () => setMode(b.dataset.mode)));

  els.autoBase.checked = state.autoBase;
  els.autoBase.addEventListener("change", () => {
    state.autoBase = els.autoBase.checked;
    store.set("vf_autoBase", state.autoBase ? "1" : "0");
    syncDialogueUI();
  });

  async function loadCloneInfo() {
    try { state.cloneInfo = await api("/api/clone/status"); }
    catch { state.cloneInfo = { available: false, reason: "Could not reach the server." }; }
    const info = state.cloneInfo;
    setMsg(els.cloneNote, !info.available ? info.reason
      : info.model_ready ? "Voice cloning is ready."
        : "Voice cloning is ready. The converter model (~130 MB) downloads the first time you use it.",
    !info.available);
  }

  // reference sample: upload or record
  function setRef(file, label) {
    state.refFile = file;
    els.refName.textContent = label;
    if (els.refAudio.src) URL.revokeObjectURL(els.refAudio.src);
    els.refAudio.src = URL.createObjectURL(file);
    els.refAudio.hidden = false;
    if (!els.saveName.value) els.saveName.value = label.replace(/\.[^.]+$/, "").slice(0, 40);
  }
  els.refInput.addEventListener("change", (ev) => {
    const f = ev.target.files[0];
    if (f) setRef(f, f.name);
    ev.target.value = "";
  });

  let recorder = null, recChunks = [];
  els.recBtn.onclick = async () => {
    if (recorder) { recorder.stop(); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recorder = new MediaRecorder(stream);
      recChunks = [];
      recorder.ondataavailable = (e) => e.data.size && recChunks.push(e.data);
      recorder.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(recChunks, { type: recorder.mimeType || "audio/webm" });
        setRef(new File([blob], "recording.webm", { type: blob.type }), "Your recording");
        recorder = null;
        els.recBtn.textContent = "● Record";
        els.recBtn.classList.remove("recording");
      };
      recorder.start();
      els.recBtn.textContent = "■ Stop";
      els.recBtn.classList.add("recording");
    } catch {
      setStatus("Could not access the microphone.", true);
    }
  };

  // ------------------------------------------------------------ dialogue mode
  function syncDialogueUI() {
    const on = useDialogue();
    els.tabs.hidden = !on;
    els.dialogueHelp.hidden = !on;
    els.stdVoices.hidden = cloneAuto();
    renderVoices();
    renderMy();
  }
  els.dialogue.addEventListener("change", () => {
    if (els.dialogue.checked && !els.text.value.trim()) {
      els.text.value = "A: Hi! Welcome to the show.\nB: Thanks for having me.";
      updateCounter();
    }
    syncDialogueUI();
  });
  els.tabs.addEventListener("click", (e) => {
    const b = e.target.closest(".tab");
    if (!b) return;
    state.speaker = b.dataset.speaker;
    els.tabs.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === b));
    renderVoices();
    renderMy();
  });

  // ------------------------------------------------------------ voice selection
  const currentVoiceId = () => {
    if (state.mode === "clone") return state.cloneBase;
    return !useDialogue() ? state.voice : state.speaker === "A" ? state.voiceA : state.voiceB;
  };

  const selectVoice = (id) => {
    if (state.mode === "clone") {
      state.cloneBase = id; store.set("vf_cloneBase", id);
    } else if (!useDialogue()) {
      state.voice = id; store.set("vf_voice", id);
    } else if (state.speaker === "A") {
      state.voiceA = id; store.set("vf_voiceA", id);
    } else {
      state.voiceB = id; store.set("vf_voiceB", id);
    }
    updateLabels();
    renderVoices();
    renderMy();
  };

  const updateLabels = () => {
    els.labelA.textContent = shortName(state.voiceA);
    els.labelB.textContent = shortName(state.voiceB);
  };

  const saveFavs = () => store.set("vf_favs", JSON.stringify([...state.favs]));

  // ---- preview play / stop (one at a time; the same button toggles ▶ / ■) ----
  function setPlaying(key) {
    state.playingKey = key;
    renderVoices();
    renderMy();
  }
  function stopPreview() {
    state.previewToken++; // cancels any preview that is still loading
    if (state.previewAudio) {
      state.previewAudio.onended = state.previewAudio.onerror = null;
      state.previewAudio.pause();
      state.previewAudio = null;
    }
    if (state.playingKey) setPlaying(null);
  }
  async function togglePreview(key, getUrl) {
    if (state.playingKey === key) { stopPreview(); return; }
    stopPreview();
    const token = ++state.previewToken;
    setPlaying(key); // shows ■ right away, even while the preview is loading
    try {
      const url = await getUrl();
      if (token !== state.previewToken) return; // stopped while loading
      const audio = new Audio(url);
      state.previewAudio = audio;
      audio.onended = audio.onerror = () => { if (token === state.previewToken) stopPreview(); };
      await audio.play();
    } catch (err) {
      if (token === state.previewToken) { stopPreview(); setStatus(err.message || "Could not play.", true); }
    }
  }
  const playLabel = (key) => (state.playingKey === key ? "■" : "▶");

  function renderVoices() {
    const q = els.search.value.trim().toLowerCase();
    const lang = els.lang.value, gender = els.gender.value;
    const list = state.voices.filter((v) =>
      (!lang || v.lang === lang) && (!gender || v.gender === gender) &&
      (!q || `${v.name} ${v.locale} ${langName(v.lang)} ${countryName(v.country)}`.toLowerCase().includes(q)));
    list.sort((a, b) => (state.favs.has(b.id) - state.favs.has(a.id)));
    const cur = currentVoiceId();

    els.voiceList.innerHTML = "";
    if (!state.voices.length) return;
    if (!list.length) { els.voiceList.textContent = "No voices match."; return; }
    const frag = document.createDocumentFragment();
    list.slice(0, 300).forEach((v) => {
      const row = document.createElement("div");
      row.className = "voice" + (v.id === cur ? " selected" : "");
      row.innerHTML = `
        <button class="play" title="Preview" type="button">▶</button>
        <div class="info"><div class="name"></div><div class="sub"></div></div>
        <button class="star${state.favs.has(v.id) ? " on" : ""}" title="Favorite" type="button">★</button>`;
      row.querySelector(".name").textContent = v.name;
      row.querySelector(".sub").textContent =
        `${langName(v.lang)} · ${countryName(v.country)} · ${v.gender}`;
      row.onclick = () => selectVoice(v.id);
      const pb = row.querySelector(".play");
      pb.textContent = playLabel(`v:${v.id}`);
      pb.title = state.playingKey === `v:${v.id}` ? "Stop" : "Preview";
      pb.classList.toggle("playing", state.playingKey === `v:${v.id}`);
      pb.onclick = (e) => { e.stopPropagation(); togglePreview(`v:${v.id}`, () => previewUrl(v)); };
      row.querySelector(".star").onclick = (e) => {
        e.stopPropagation();
        state.favs.has(v.id) ? state.favs.delete(v.id) : state.favs.add(v.id);
        saveFavs(); renderVoices();
      };
      frag.appendChild(row);
    });
    els.voiceList.appendChild(frag);
    if (list.length > 300) {
      const more = document.createElement("div");
      more.className = "hint";
      more.textContent = `Showing 300 of ${list.length}. Use search or filters to narrow down.`;
      els.voiceList.appendChild(more);
    }
  }
  [els.search, els.lang, els.gender].forEach((el) => el.addEventListener("input", renderVoices));

  function fillBaseSelect() {
    els.edBase.innerHTML = "";
    const auto = document.createElement("option");
    auto.value = "auto"; auto.textContent = "Auto (same gender, language of the text)";
    els.edBase.appendChild(auto);
    state.voices.forEach((v) => {
      const o = document.createElement("option");
      o.value = v.id; o.textContent = `${v.name} (${v.locale}, ${v.gender})`;
      els.edBase.appendChild(o);
    });
  }

  function fixSelections() {
    if (!state.loaded.voices || !state.loaded.my) return;
    const def = (state.voices.find((v) => v.id === "en-US-AriaNeural") || state.voices[0] || {}).id || "";
    const defB = (state.voices.find((v) => v.id === "en-US-GuyNeural") || state.voices[1] || {}).id || def;
    const ok = (k) => (isMy(k) ? !!myById(k) : !!voiceById(k));
    if (!ok(state.voice)) state.voice = def;
    if (!ok(state.voiceA)) state.voiceA = def;
    if (!ok(state.voiceB)) state.voiceB = defB;
    if (state.cloneBase && !voiceById(state.cloneBase)) state.cloneBase = "";
    if (!state.cloneBase) state.cloneBase = def;
    updateLabels();
    renderVoices();
    renderMy();
  }

  async function loadVoices() {
    try {
      const data = await api("/api/voices");
      state.voices = data;
      const langs = [...new Set(data.map((v) => v.lang))]
        .sort((a, b) => langName(a).localeCompare(langName(b)));
      langs.forEach((l) => {
        const o = document.createElement("option");
        o.value = l; o.textContent = langName(l);
        els.lang.appendChild(o);
      });
      fillBaseSelect();
    } catch (err) {
      els.voiceList.textContent = err.message;
    }
    state.loaded.voices = true;
    fixSelections();
  }

  async function previewUrl(v) {
    const sample = v.lang === "ur"
      ? "ہیلو! یہ میری آواز کا ایک نمونہ ہے۔"
      : "Hello! This is a preview of my voice.";
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: sample, voice: v.id }),
    });
    if (!res.ok) {
      let msg = "Preview failed";
      try { msg = (await res.json()).error || msg; } catch { /* not json */ }
      throw new Error(msg);
    }
    return URL.createObjectURL(await res.blob());
  }

  // ------------------------------------------------------------ My voices
  async function loadMy() {
    try { state.my = await api("/api/myvoices"); } catch { state.my = []; }
    state.loaded.my = true;
    fixSelections();
  }

  function useMyVoice(key) {
    if (state.mode === "clone") setMode("tts");
    selectVoice(key);
  }

  function renderMy() {
    els.myList.innerHTML = "";
    els.myHint.textContent = state.my.length ? "" : "None yet";
    if (!state.my.length) {
      const empty = document.createElement("div");
      empty.className = "hint";
      empty.textContent = "Clone a voice in the “Voice cloning” tab and save it here. Saved voices work in normal text-to-speech and in two-speaker dialogues.";
      els.myList.appendChild(empty);
    }
    const cur = state.mode === "clone" ? "" : currentVoiceId();
    state.my.forEach((m) => {
      const key = `my:${m.id}`;
      const row = document.createElement("div");
      row.className = "voice" + (key === cur ? " selected" : "");
      row.innerHTML = `
        <button class="play" title="Play the sample" type="button">▶</button>
        <div class="info"><div class="name"></div><div class="sub"></div></div>
        <button class="star edit" title="Edit / rename" type="button">✎</button>`;
      row.querySelector(".name").textContent = m.name;
      const base = m.base === "auto" ? "auto base" : shortName(m.base);
      row.querySelector(".sub").textContent =
        `${m.gender || "Voice"} · ${m.seconds ? Math.round(m.seconds) + "s sample · " : ""}${base}`;
      row.onclick = () => useMyVoice(key);
      const pb = row.querySelector(".play");
      pb.textContent = playLabel(key);
      pb.title = state.playingKey === key ? "Stop" : "Play the sample";
      pb.classList.toggle("playing", state.playingKey === key);
      pb.onclick = (e) => {
        e.stopPropagation();
        togglePreview(key, async () => `/api/myvoices/${m.id}/sample?t=${Date.now()}`);
      };
      row.querySelector(".edit").onclick = (e) => { e.stopPropagation(); openEditor(m.id); };
      els.myList.appendChild(row);
    });
    if (state.editId && !state.my.find((m) => m.id === state.editId)) closeEditor();
  }

  function openEditor(id) {
    const m = state.my.find((x) => x.id === id);
    if (!m) return;
    state.editId = id;
    els.edName.value = m.name;
    els.edBase.value = m.base && (m.base === "auto" || voiceById(m.base)) ? m.base : "auto";
    els.edTau.value = m.tau; els.edTauOut.textContent = (+m.tau).toFixed(2);
    els.edRate.value = m.rate || 0; els.edRateOut.textContent = `${m.rate || 0}%`;
    els.edPitch.value = m.pitch || 0; els.edPitchOut.textContent = `${m.pitch || 0} Hz`;
    els.edMatch.checked = !!m.match_pitch;
    setMsg(els.edMsg, `Detected: ${m.gender || "unknown"} voice${m.f0 ? `, about ${Math.round(m.f0)} Hz` : ""}.`);
    els.editor.hidden = false;
    els.editor.scrollIntoView({ block: "nearest" });
  }
  function closeEditor() { state.editId = null; els.editor.hidden = true; }

  els.edTau.addEventListener("input", () => { els.edTauOut.textContent = (+els.edTau.value).toFixed(2); });
  els.edRate.addEventListener("input", () => { els.edRateOut.textContent = `${els.edRate.value}%`; });
  els.edPitch.addEventListener("input", () => { els.edPitchOut.textContent = `${els.edPitch.value} Hz`; });
  $("edClose").onclick = closeEditor;

  $("edSave").onclick = async () => {
    if (!state.editId) return;
    try {
      const r = await api(`/api/myvoices/${state.editId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: els.edName.value, base: els.edBase.value, tau: +els.edTau.value,
          rate: +els.edRate.value, pitch: +els.edPitch.value, match_pitch: els.edMatch.checked,
        }),
      });
      state.my = state.my.map((m) => (m.id === r.voice.id ? r.voice : m));
      updateLabels(); renderMy();
      setMsg(els.edMsg, "Saved.");
    } catch (err) { setMsg(els.edMsg, err.message, true); }
  };

  $("edSample").addEventListener("change", async (ev) => {
    const f = ev.target.files[0];
    ev.target.value = "";
    if (!f || !state.editId) return;
    if (!confirm("Do you own this voice or have the speaker's permission to clone it?")) return;
    const fd = new FormData();
    fd.append("reference", f, f.name);
    fd.append("consent", "true");
    setMsg(els.edMsg, "Uploading…");
    try {
      const r = await api(`/api/myvoices/${state.editId}/sample`, { method: "POST", body: fd });
      state.my = state.my.map((m) => (m.id === r.voice.id ? r.voice : m));
      renderMy(); openEditor(r.voice.id);
      setMsg(els.edMsg, "Sample replaced." + (r.warning ? " " + r.warning : ""), !!r.warning);
    } catch (err) { setMsg(els.edMsg, err.message, true); }
  });

  $("edDelete").onclick = async () => {
    const m = state.my.find((x) => x.id === state.editId);
    if (!m || !confirm(`Delete the voice “${m.name}”? This cannot be undone.`)) return;
    try {
      stopPreview();
      await api(`/api/myvoices/${m.id}`, { method: "DELETE" });
      state.my = state.my.filter((x) => x.id !== m.id);
      closeEditor();
      state.loaded.my = true;
      fixSelections();
    } catch (err) { setMsg(els.edMsg, err.message, true); }
  };

  els.saveBtn.onclick = async () => {
    if (!state.refFile) return setMsg(els.saveMsg, "Please upload or record a voice sample first.", true);
    if (!els.consent.checked) return setMsg(els.saveMsg, "Please tick the permission checkbox first.", true);
    const fd = new FormData();
    fd.append("name", els.saveName.value || "My voice");
    fd.append("reference", state.refFile, state.refFile.name || "reference");
    fd.append("consent", "true");
    fd.append("base", cloneAuto() ? "auto" : state.cloneBase || "auto");
    fd.append("tau", els.tau.value);
    fd.append("match_pitch", els.matchPitch.checked);
    const fx = fxNow();
    fd.append("rate", fx.rate); fd.append("pitch", fx.pitch); fd.append("volume", fx.volume);
    els.saveBtn.disabled = true;
    setMsg(els.saveMsg, "Saving…");
    try {
      const r = await api("/api/myvoices", { method: "POST", body: fd });
      state.my.push(r.voice);
      renderMy();
      const g = r.voice.gender ? ` (${r.voice.gender.toLowerCase()} voice detected)` : "";
      setMsg(els.saveMsg, `Saved “${r.voice.name}”${g}. Find it under My voices.` + (r.warning ? " " + r.warning : ""), !!r.warning);
    } catch (err) { setMsg(els.saveMsg, err.message, true); }
    finally { els.saveBtn.disabled = false; }
  };

  // ------------------------------------------------------------ jobs
  async function startJob(url, init) {
    return (await api(url, init)).job_id;
  }

  async function pollJob(jobId) {
    const job = state.pending.get(jobId);
    while (state.pending.has(jobId)) {
      await new Promise((r) => setTimeout(r, 1000));
      let data;
      try { data = await api(`/api/jobs/${jobId}`); }
      catch (err) {
        job.failed = true; job.message = err.message;
        renderResults();
        return;
      }
      job.done = data.done; job.total = data.total; job.message = data.message;
      if (data.status === "done") {
        try {
          const blob = await (await fetch(`/api/jobs/${jobId}/audio`)).blob();
          state.pending.delete(jobId);
          addResult({ blob, text: job.text, label: job.label });
          setStatus("Done!");
        } catch (err) {
          job.failed = true; job.message = err.message;
          renderResults();
        }
        return;
      }
      if (data.status === "error") { job.failed = true; job.message = data.error || "Failed"; renderResults(); return; }
      if (data.status === "cancelled") { state.pending.delete(jobId); renderResults(); return; }
      renderResults();
    }
  }

  els.generate.onclick = async () => {
    const text = els.text.value.trim();
    if (!text) return setStatus("Please enter some text.", true);
    if (MAX && text.length > MAX) return setStatus(`Text is too long (max ${MAX} characters).`, true);

    const fx = fxNow();
    els.generate.disabled = true;
    setStatus("Starting…");
    try {
      let jobId, label;
      if (state.mode === "clone") {
        if (!state.refFile) throw new Error("Please upload or record a voice sample first.");
        if (!els.consent.checked) throw new Error("Please confirm you have permission to clone this voice.");
        const fd = new FormData();
        fd.append("text", text);
        fd.append("base", cloneAuto() ? "auto" : state.cloneBase || "auto");
        fd.append("consent", "true");
        fd.append("tau", els.tau.value);
        fd.append("match_pitch", els.matchPitch.checked);
        fd.append("reference", state.refFile, state.refFile.name || "reference");
        Object.entries(fx).forEach(([k, v]) => fd.append(k, v));
        jobId = await startJob("/api/clone", { method: "POST", body: fd });
        label = cloneAuto() ? "Cloned voice (auto base)" : `Cloned voice (base: ${shortName(state.cloneBase)})`;
      } else {
        const dialogue = useDialogue();
        jobId = await startJob("/api/tts", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            text, dialogue, voice: state.voice, voice_a: state.voiceA, voice_b: state.voiceB, ...fx,
          }),
        });
        label = dialogue ? `${shortName(state.voiceA)} + ${shortName(state.voiceB)}` : shortName(state.voice);
      }
      state.pending.set(jobId, { id: jobId, label, text, done: 0, total: 1, message: "Queued…" });
      renderResults();
      setStatus("Working… long texts can take a while. You can leave this tab open and keep using the page.");
      pollJob(jobId);
    } catch (err) {
      setStatus(err.message, true);
    } finally {
      els.generate.disabled = false;
    }
  };

  // ------------------------------------------------------------ results
  function addResult({ blob, text, label }) {
    const id = Date.now() + Math.random();
    const url = URL.createObjectURL(blob);
    state.results.unshift({ id, blob, url, label, text, time: new Date() });
    renderResults();
  }

  function renderResults() {
    els.results.innerHTML = "";
    els.zip.hidden = state.results.length < 2;
    if (!state.results.length && !state.pending.size) {
      els.results.innerHTML = '<p class="empty">Your generated audio will appear here.</p>';
      return;
    }

    state.pending.forEach((j) => {
      const box = document.createElement("div");
      box.className = "result " + (j.failed ? "failed" : "pending");
      box.innerHTML = `
        <div class="head"><span class="lbl"></span><span class="pct"></span></div>
        <div class="snippet"></div>
        <div class="progress"><div></div></div>
        <div class="msg"></div>
        <div class="actions"><button class="ghost small act" type="button"></button></div>`;
      box.querySelector(".lbl").textContent = j.label;
      box.querySelector(".snippet").textContent = j.text;
      const pct = j.total ? Math.min(100, Math.round((j.done / j.total) * 100)) : 0;
      box.querySelector(".pct").textContent = j.failed ? "Failed" : `${pct}%`;
      box.querySelector(".progress > div").style.width = `${j.failed ? 100 : pct}%`;
      box.querySelector(".msg").textContent = j.message || "";
      const btn = box.querySelector(".act");
      btn.textContent = j.failed ? "Dismiss" : "Cancel";
      btn.onclick = () => {
        if (!j.failed) fetch(`/api/jobs/${j.id}`, { method: "DELETE" }).catch(() => {});
        state.pending.delete(j.id);
        renderResults();
      };
      els.results.appendChild(box);
    });

    state.results.forEach((r, i) => {
      const box = document.createElement("div");
      box.className = "result";
      box.innerHTML = `
        <div class="head"><span class="lbl"></span><span>${r.time.toLocaleTimeString()}</span></div>
        <div class="snippet"></div>
        <audio controls preload="metadata"></audio>
        <div class="actions">
          <a class="ghost small dl" download>Download MP3</a>
          <button class="ghost small rm" type="button">Remove</button>
        </div>`;
      box.querySelector(".lbl").textContent = r.label;
      box.querySelector(".snippet").textContent = r.text.slice(0, 300);
      box.querySelector("audio").src = r.url;
      const a = box.querySelector(".dl");
      a.href = r.url; a.download = `voxforge-${state.results.length - i}.mp3`;
      box.querySelector(".rm").onclick = () => {
        URL.revokeObjectURL(r.url);
        state.results = state.results.filter((x) => x.id !== r.id);
        renderResults();
      };
      els.results.appendChild(box);
    });
  }

  els.zip.onclick = async () => {
    if (!window.JSZip) return setStatus("ZIP library failed to load.", true);
    const zip = new JSZip();
    state.results.forEach((r, i) => zip.file(`voxforge-${state.results.length - i}.mp3`, r.blob));
    const blob = await zip.generateAsync({ type: "blob" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "voxforge-audio.zip";
    a.click();
  };

  // keyboard shortcut: Ctrl/Cmd + Enter
  els.text.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") els.generate.click();
  });

  fxOut();
  updateCounter();
  loadVoices();
  loadMy();
})();
