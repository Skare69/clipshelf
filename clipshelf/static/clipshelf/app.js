/* clipshelf web app — vanilla JS, no framework, no CDN, no third-party requests.
   All model/source text enters the DOM via textContent; URLs only http(s); private
   state lives in memory only. localStorage holds the two layout prefs, nothing else. */
import { S, el, out, refreshMe, setView } from "./core.js";
import { refreshInboxCount } from "./panels.js";
import "./admin.js";

/* ---------- boot ---------- */
(async () => {
  try {
    await refreshMe();
    const me = S.me;
    S.col = me.default_collection_id || (me.collections.find(c => c.kind === "personal") || me.collections[0] || {}).id;
    if (!S.col) {
      out.replaceChildren(el("div", { class: "empty" },
        el("b", { text: "No collection is available to you." }),
        el("p", { text: "Ask your administrator to add you to a collection." })));
      return;
    }
    setView("library");
    refreshInboxCount();
  } catch (e) {
    if (e.status === 401) return; // onAuthLoss is redirecting
    out.replaceChildren(el("div", { class: "empty", role: "alert" },
      el("b", { text: "The server could not load your account." }),
      el("p", { text: e.message }),
      el("button", { class: "btn primary", type: "button",
        onclick: () => location.reload() }, "Try again")));
  }
})();
