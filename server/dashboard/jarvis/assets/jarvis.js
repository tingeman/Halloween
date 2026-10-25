// Clientside callbacks, soundtrack and keyboard shortcuts for the Jarvis dashboard.
(function () {
  const LAYOUTS = ["grid", "grid2", "focus", "ops", "kiosk"];
  const urlLayout = new URLSearchParams(window.location.search).get("layout");
  let overrideLayout = LAYOUTS.includes(urlLayout) ? urlLayout : null;  // ?layout= wins until a click
  let classicUrl = "/classic/";
  let currentFocus = 0;
  let paneCount = 0;

  function triggeredId() {
    const ctx = window.dash_clientside.callback_context;
    if (ctx.triggered_id !== undefined) return ctx.triggered_id;
    const prop = (ctx.triggered[0] || {}).prop_id || "";
    const id = prop.slice(0, prop.lastIndexOf("."));
    try { return JSON.parse(id); } catch (e) { return id; }
  }

  function effective(stored, defaults) {
    const prefs = Object.assign({ focus: 0 }, defaults || {}, stored || {});
    if (overrideLayout) prefs.layout = overrideLayout;
    if (!LAYOUTS.includes(prefs.layout)) prefs.layout = "grid";
    return prefs;
  }

  // --- soundtrack: once on open, never looped
  function audio() { return document.getElementById("jv-audio"); }

  function playFromStart() {
    const a = audio();
    if (!a || !a.getAttribute("src")) return;
    a.loop = false;
    try { a.currentTime = 0; } catch (e) { /* not loaded yet */ }
    const p = a.play();
    if (p && p.catch) {
      p.catch(function () {
        // Autoplay blocked: start on the first click or key press instead
        const start = function () {
          document.removeEventListener("pointerdown", start, true);
          document.removeEventListener("keydown", start, true);
          if (a.dataset.on === "1") { a.currentTime = 0; a.play().catch(function () {}); }
        };
        document.addEventListener("pointerdown", start, true);
        document.addEventListener("keydown", start, true);
      });
    }
  }

  // Plays once when the page loads. Layout changes never touch it; turning the
  // sound off pauses and turning it on resumes. Only a page reload or the ♫
  // button (key R) start it from the beginning.
  let soundStarted = false;
  function syncSound(prefs) {
    const a = audio();
    if (!a) return;
    const vol = parseFloat(a.dataset.volume || "0.6");
    a.volume = isNaN(vol) ? 0.6 : Math.min(Math.max(vol, 0), 1);
    const wasOn = a.dataset.on === "1";
    a.dataset.on = prefs.sound ? "1" : "0";
    if (!prefs.sound) { a.pause(); return; }
    if (wasOn) return;
    if (!soundStarted) { soundStarted = true; playFromStart(); }
    else if (!a.ended) { a.play().catch(function () {}); }
  }

  function restartSound() {
    const a = audio();
    if (!a || !a.getAttribute("src")) return;
    if (a.dataset.on !== "1") {             // sound is off: switch it on, which starts it
      soundStarted = false;
      const t = document.getElementById("jv-set-sound");
      if (t && !t.disabled) t.click();
      return;
    }
    soundStarted = true;
    playFromStart();
  }
  document.addEventListener("click", function (ev) {
    if (ev.target.closest && ev.target.closest("#jv-sound-restart")) restartSound();
  });

  window.dash_clientside = Object.assign({}, window.dash_clientside, {
    jarvis: {
      updatePrefs: function () {
        const n = arguments.length;
        const stored = Object.assign({}, arguments[n - 2] || {});
        const current = effective(stored, arguments[n - 1]);
        const id = triggeredId();
        if (typeof id === "string" && id.startsWith("jv-l-")) {
          stored.layout = id.slice(5);
        } else if (id && (id.type === "jv-focus" || id.type === "jv-focus-row")) {
          stored.focus = id.index;
          stored.layout = "focus";
        } else if (id === "jv-set-theme") {
          stored.theme = current.theme === "flame" ? "blue" : "flame";
        } else if (id === "jv-set-accents") {
          stored.accents = !current.accents;
        } else if (id === "jv-set-sound") {
          stored.sound = !current.sound;
        } else {
          return window.dash_clientside.no_update;
        }
        if (stored.layout && overrideLayout) {
          overrideLayout = null;
          const url = new URL(window.location.href);
          url.searchParams.delete("layout");
          window.history.replaceState(null, "", url);
        }
        return stored;
      },

      applyPrefs: function (stored, defaults) {
        const prefs = effective(stored, defaults);
        classicUrl = (defaults && defaults.classicUrl) || classicUrl;
        const ctx = window.dash_clientside.callback_context;
        const panes = ctx.outputs_list[1] || [];
        const focus = prefs.focus < panes.length ? prefs.focus : 0;
        const paneClasses = panes.map(function (_, i) {
          return i === focus ? "jv-pane is-focus" : "jv-pane";
        });
        const layoutButtons = LAYOUTS.map(function (l) {
          return l === prefs.layout ? "jv-switch-btn active" : "jv-switch-btn";
        });
        const toggles = ["jv-toggle theme" + (prefs.theme === "flame" ? " flame" : "")].concat(
          [prefs.accents, prefs.sound].map(function (on) { return on ? "jv-toggle on" : "jv-toggle"; }));
        const rows = (ctx.outputs_list[ctx.outputs_list.length - 1] || []).map(function (_, i) {
          return i === focus ? "jv-proprow is-focus" : "jv-proprow";
        });
        currentFocus = focus;
        paneCount = panes.length;

        syncSound(prefs);

        const root = "jv jv-layout-" + prefs.layout + (prefs.accents ? " jv-accents" : "") +
          (prefs.theme === "flame" ? " jv-theme-flame" : "");
        return [root, paneClasses].concat(layoutButtons, toggles, [rows]);
      },

      toggleSettings: function (_, __, cls) {
        const open = (cls || "").indexOf("open") >= 0;
        if (triggeredId() === "jv-settings-close" || open) return "jv-settings-panel";
        return "jv-settings-panel open";
      },

    },
  });

  // Close the settings panel with Esc or a click outside it
  function settingsOpen() {
    const p = document.getElementById("jv-settings-panel");
    return p && p.classList.contains("open") ? p : null;
  }
  function closeSettings() {
    const b = document.getElementById("jv-settings-close");
    if (b) b.click();
  }
  document.addEventListener("pointerdown", function (ev) {
    const p = settingsOpen();
    if (!p || p.contains(ev.target)) return;
    const gear = document.getElementById("jv-settings-btn");
    if (gear && gear.contains(ev.target)) return;   // the gear toggles it itself
    closeSettings();
  }, true);

  // Clock: one fixed-width cell per character (Orbitron digits differ in width),
  // updated by a timer re-armed for each next whole second so it never drifts.
  function tickClock() {
    const el = document.getElementById("jv-clock");
    if (el) {
      const text = new Date().toLocaleTimeString("en-GB");
      if (el.childElementCount !== text.length) {
        el.textContent = "";
        for (const ch of text) {
          const s = document.createElement("span");
          s.className = ch === ":" ? "jv-clock-sep" : "jv-clock-digit";
          el.appendChild(s);
        }
      }
      for (let i = 0; i < text.length; i++) {
        if (el.children[i].textContent !== text[i]) el.children[i].textContent = text[i];
      }
    }
    setTimeout(tickClock, 1000 - (Date.now() % 1000) + 5);
  }
  tickClock();

  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape" && settingsOpen()) { closeSettings(); return; }
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const tag = (ev.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || ev.target.isContentEditable) return;
    const idx = ["1", "2", "3", "4", "5"].indexOf(ev.key);
    const click = function (id) { const el = document.getElementById(id); if (el && !el.disabled) el.click(); };
    if (idx >= 0) {
      click("jv-l-" + LAYOUTS[idx]);
    } else if (ev.key === "c" || ev.key === "C") {
      window.location.href = classicUrl;
    } else if (ev.key === "m" || ev.key === "M") {
      click("jv-set-sound");
    } else if (ev.key === "r" || ev.key === "R") {
      restartSound();
    } else if ((ev.key === "[" || ev.key === "]") && paneCount > 0) {
      const next = (currentFocus + (ev.key === "]" ? 1 : paneCount - 1)) % paneCount;
      click(JSON.stringify({ index: next, type: "jv-focus-row" }));
    }
  });
})();
