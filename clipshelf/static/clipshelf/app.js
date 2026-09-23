/* clipshelf web app — vanilla JS, no framework, no CDN, no third-party requests.
   All model/source text enters the DOM via textContent; URLs only http(s); private
   state lives in memory only. localStorage holds the two layout prefs, nothing else. */
import { S, el, onAuthLoss, out, redirecting, refreshMe, setView } from "./core.js";
import { initLayoutButtons, refreshInboxCount } from "./panels.js";
import "./admin.js";

/* ---------- boot ---------- */
(async () => {
  initLayoutButtons();
  try {
    await refreshMe();
    const me = S.me;
    S.col = me.default_collection_id || (me.collections.find(c => c.kind === "personal") || me.collections[0] || {}).id;
    if (!S.col) {
      out.replaceChildren(el("div", { class: "empty" },
        el("b", { text: "No collection available." }), " Contact your administrator."));
      return;
    }
    setView("library");
    refreshInboxCount();
  } catch (e) {
    if (e.status === 401) return; // onAuthLoss is redirecting
    out.replaceChildren(el("div", { class: "empty", role: "alert" },
      el("b", { text: "The server could not load your account." }), " ",
      el("span", { text: e.message }), " ",
      el("button", { class: "btn small", type: "button",
        onclick: () => location.reload() }, "Retry")));
  }
})();
