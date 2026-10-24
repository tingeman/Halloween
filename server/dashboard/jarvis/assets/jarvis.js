// Clientside callbacks and keyboard shortcuts for the Jarvis dashboard.
(function () {
  const LAYOUTS = ["grid", "focus", "ops"];
  const DEFAULTS = { layout: "grid", focus: 0 };

  function triggeredId() {
    const ctx = window.dash_clientside.callback_context;
    if (ctx.triggered_id !== undefined) return ctx.triggered_id;
    const prop = (ctx.triggered[0] || {}).prop_id || "";
    const id = prop.slice(0, prop.lastIndexOf("."));
    try { return JSON.parse(id); } catch (e) { return id; }
  }

  window.dash_clientside = Object.assign({}, window.dash_clientside, {
    jarvis: {
      updatePrefs: function () {
        const prefs = Object.assign({}, DEFAULTS, arguments[arguments.length - 1] || {});
        const id = triggeredId();
        if (typeof id === "string" && id.startsWith("jv-l-")) {
          prefs.layout = id.slice(5);
        } else if (id && id.type === "jv-focus") {
          prefs.focus = id.index;
          prefs.layout = "focus";
        } else {
          return window.dash_clientside.no_update;
        }
        return prefs;
      },

      applyPrefs: function (stored) {
        const prefs = Object.assign({}, DEFAULTS, stored || {});
        if (!LAYOUTS.includes(prefs.layout)) prefs.layout = DEFAULTS.layout;
        const panes = window.dash_clientside.callback_context.outputs_list[1] || [];
        const focus = prefs.focus < panes.length ? prefs.focus : 0;
        const paneClasses = panes.map(function (_, i) {
          return i === focus ? "jv-pane is-focus" : "jv-pane";
        });
        const buttons = LAYOUTS.map(function (l) {
          return l === prefs.layout ? "jv-switch-btn active" : "jv-switch-btn";
        });
        return ["jv jv-layout-" + prefs.layout, paneClasses].concat(buttons);
      },

      clock: function () {
        return new Date().toLocaleTimeString("en-GB");
      },
    },
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
    const tag = (ev.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select" || ev.target.isContentEditable) return;
    const idx = ["1", "2", "3"].indexOf(ev.key);
    if (idx >= 0) {
      const btn = document.getElementById("jv-l-" + LAYOUTS[idx]);
      if (btn) btn.click();
    } else if (ev.key === "c" || ev.key === "C") {
      window.location.href = "/";
    }
  });
})();
