/* clipshelf web app — admin panel: users, invitations, collections, LLM, screening. */
"use strict";

import { $, S, api, banner, confirmDialog, el, fmtDate, note, onEnter, out, sensitive, toast } from "./core.js";
import { renderToc } from "./panels.js";

/* ---------- admin ---------- */
async function renderAdmin() {
  if (!S.me?.user.is_app_admin) return;
  const frag = document.createDocumentFragment();
  frag.append(el("h1", { text: "Server administration" }));
  const wrap = el("div", {});
  frag.append(wrap);
  out.replaceChildren(frag);
  renderToc();
  const [users, invs, cols, llm, scr] = await Promise.allSettled([
    api("/api/admin/users"), api("/api/admin/invitations"),
    api("/api/admin/collections"), api("/api/admin/llm"), api("/api/admin/screening")
  ]);
  const errNode = e => el("p", { class: "err", text: e.message });
  wrap.replaceChildren();
  wrap.append(adminUsers(users.status === "fulfilled" ? users.value.users : null,
    users.status === "rejected" ? users.reason : null));
  wrap.append(adminInvitations(invs.status === "fulfilled" ? invs.value.invitations : null,
    invs.status === "rejected" ? invs.reason : null));
  wrap.append(adminCollections(cols.status === "fulfilled" ? cols.value.collections : null,
    cols.status === "rejected" ? cols.reason : null));
  wrap.append(adminLlm(llm.status === "fulfilled" ? llm.value.llm : null,
    llm.status === "rejected" ? llm.reason : null,
    scr.status === "fulfilled" ? scr.value.screening : null,
    scr.status === "rejected" ? scr.reason : null));
}
function adminUsers(users, err) {
  const p = el("section", { class: "panel" }, el("h3", { text: "Accounts" }));
  if (err) return p.append(errNode(err)), p;
  const rows = (users || []).map(u => {
    const acts = el("div", { class: "acts" });
    const toggle = el("button", { class: "btn small", type: "button",
      text: u.is_active ? "Disable" : "Re-enable" });
    toggle.addEventListener("click", async () => {
      const vals = await confirmDialog({
        head: (u.is_active ? "Disable" : "Re-enable") + " " + u.email + "?",
        text: u.is_active
          ? "Access and sessions are revoked; identity, content, memberships and ownership are preserved. Re-enabling allows fresh login."
          : "The account can sign in again under current permissions. Previously revoked sessions stay revoked.",
        ok: u.is_active ? "Disable" : "Re-enable", danger: u.is_active });
      if (!vals) return;
      try { await sensitive(`/api/admin/users/${u.id}/status`, { active: !u.is_active }); renderAdmin(); }
      catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
    });
    const rec = el("button", { class: "btn small", type: "button", text: "Recover" });
    rec.addEventListener("click", async () => {
      const vals = await confirmDialog({
        head: "Exceptional recovery for " + u.email + "?",
        text: "Use only when password AND mailbox access are both lost. This creates a reset link that grants full control of the account — handle it like a key. Sessions are invalidated; MFA is never removed.",
        ok: "Create reset link",
        fields: [{ name: "reauth_password", label: "Your current password", type: "password", required: true }] });
      if (!vals) return;
      try {
        const j = await sensitive(`/api/admin/users/${u.id}/recover`, vals);
        await confirmDialog({ head: "Reset link created",
          text: "Copy it now and deliver it through a trusted channel. It is not emailed.",
          ok: "Done", danger: false,
          fields: [{ name: "url", label: "Reset link (copy it now)", value: j.reset_url }] });
      } catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
    });
    acts.append(toggle, rec);
    return el("tr", {},
      el("td", {}, el("b", { text: u.email }),
        u.is_app_admin ? el("span", { class: "badge run", text: "admin", style: "margin-left:6px" }) : null),
      el("td", { text: u.is_verified ? "yes" : "no" }),
      el("td", { text: fmtDate(u.date_joined) }),
      el("td", {}, el("span", { class: "badge " + (u.is_active ? "ok" : "err"),
        text: u.is_active ? "active" : "disabled" })),
      el("td", {}, acts));
  });
  p.append(el("table", {},
    el("thead", {}, el("tr", {},
      ...["Email", "Verified", "Joined", "Status", "Actions"].map(h => el("th", { text: h })))),
    el("tbody", {}, ...rows)));
  p.append(el("p", { class: "hint", text: "Recovery uses the framework's reset tokens under the same account id and never removes MFA. Disabling never deletes content." }));
  return p;
}
function adminInvitations(invs, err) {
  const p = el("section", { class: "panel" }, el("h3", { text: "Invitations" }));
  if (err) return p.append(errNode(err)), p;
  const form = el("form", { class: "row2" },
    el("input", { type: "email", required: true, placeholder: "invitee@example.com",
      "aria-label": "Email to invite", style: "flex:1;min-width:200px" }),
    el("button", { class: "btn primary", type: "submit", text: "Create invitation" }));
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const email = form.querySelector("input").value.trim();
    try {
      const j = await sensitive("/api/admin/invitations", { email });
      const url = j.invitation?.url;
      await confirmDialog({ head: "Invitation created for " + email,
        text: "Copy the link now and deliver it yourself — this is the only time it is shown (tokens are stored hashed). If it is lost, create a new invitation; used and expired ones never block a fresh link.",
        ok: "Done", danger: false,
        fields: url ? [{ name: "url", label: "Invitation link (copy it now)", value: url }] : [] });
      renderAdmin();
    } catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
  });
  const rows = (invs || []).map(v => {
    const status = v.used_at ? "used" : (new Date(v.expires_at) < new Date() ? "expired" : "pending");
    return el("tr", {},
      el("td", { text: v.email }),
      el("td", { text: fmtDate(v.created_at) }),
      el("td", { text: fmtDate(v.expires_at) }),
      el("td", {}, el("span", { class: "badge " + ({ used: "", expired: "err", pending: "ok" }[status]), text: status })));
  });
  p.append(form,
    el("table", {},
      el("thead", {}, el("tr", {}, ...["Email", "Created", "Expires", "Status"].map(h => el("th", { text: h })))),
      el("tbody", {}, ...rows)),
    el("p", { class: "hint", text: "Invitations are single-use, expiring links; the recipient picks their own password and verifies the email. SMTP is not involved. The full link is shown only once, at creation." }));
  return p;
}
function adminCollections(cols, err) {
  const p = el("section", { class: "panel" }, el("h3", { text: "Shared collections" }));
  if (err) return p.append(errNode(err)), p;
  if (!(cols || []).length) return p.append(el("p", { class: "hint", text: "No shared collections exist. Personal collections are never listed here." })), p;
  p.append(el("table", {},
    el("thead", {}, el("tr", {}, ...["Collection", "Owner", "Members", "Succession"].map(h => el("th", { text: h })))),
    el("tbody", {}, ...(cols || []).map(c => {
      const activeMembers = (c.members || []).filter(m => m.is_active && m.id !== c.owner.id);
      const sel = el("select", { "aria-label": "New owner for " + c.name },
        ...activeMembers.map(m => el("option", { value: m.id, text: m.email })));
      const btn = el("button", { class: "btn small", type: "button", text: "Transfer" });
      if (c.owner.is_active || !activeMembers.length)
        btn.disabled = true,
        btn.title = c.owner.is_active ? "Disable the owner account first." : "No active member to transfer to.";
      btn.addEventListener("click", async () => {
        const vals = await confirmDialog({
          head: `Transfer “${c.name}”?`,
          text: `Ownership moves to the selected active member. The previous owner stays a member. Personal collections are never transferable.`,
          ok: "Transfer ownership",
          fields: [{ name: "reauth_password", label: "Your current password", type: "password", required: true }] });
        if (!vals) return;
        try {
          await sensitive(`/api/admin/collections/${c.id}/transfer`, { owner_id: sel.value, ...vals });
          toast("Ownership transferred."); renderAdmin();
        } catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
      });
      return el("tr", {},
        el("td", { text: c.name }),
        el("td", {}, c.owner.email, " ",
          el("span", { class: "badge " + (c.owner.is_active ? "ok" : "err"),
            text: c.owner.is_active ? "active" : "disabled" })),
        el("td", { text: String((c.members || []).length + 1) }),
        el("td", {}, el("div", { class: "acts" }, sel, btn)));
    }))),
    el("p", { class: "hint", text: "Succession requires a disabled owner and an existing active member. The admin never gains content access through a transfer." }));
  return p;
}
/* Base URLs are stable; model names are not, so those are fetched, never hardcoded.
   Vision is required — the capability check sends an image. */
const LLM_PROVIDERS = [
  { id: "", label: "Custom / OpenAI-compatible", url: "" },
  { id: "openai", label: "OpenAI", url: "https://api.openai.com/v1" },
  { id: "anthropic", label: "Anthropic", url: "https://api.anthropic.com/v1" },
  { id: "gemini", label: "Google Gemini", url: "https://generativelanguage.googleapis.com/v1beta/openai" },
  { id: "openrouter", label: "OpenRouter", url: "https://openrouter.ai/api/v1" },
  { id: "groq", label: "Groq", url: "https://api.groq.com/openai/v1" },
  { id: "mistral", label: "Mistral", url: "https://api.mistral.ai/v1" },
  { id: "xai", label: "xAI", url: "https://api.x.ai/v1" },
  { id: "zai", label: "Z.AI (GLM)", url: "https://api.z.ai/api/paas/v4" },
  { id: "zai-coding", label: "Z.AI Coding Plan (GLM)", url: "https://api.z.ai/api/coding/paas/v4" },
  { id: "ollama", label: "Ollama (local)", url: "http://localhost:11434/v1", local: true },
  { id: "llamacpp", label: "llama.cpp (local)", url: "http://localhost:8080/v1", local: true },
  { id: "lmstudio", label: "LM Studio (local)", url: "http://localhost:1234/v1", local: true },
  { id: "vllm", label: "vLLM (local)", url: "http://localhost:8000/v1", local: true },
];
function adminLlm(llm, err, scr, scrErr) {
  const p = el("section", { class: "panel" }, el("h3", { text: "Shared interpretation endpoint" }));
  if (err) return p.append(errNode(err)), p;
  const base = el("input", { type: "url", id: "llmBase", value: llm.base_url || "", placeholder: "https://llm.example.internal/v1" });
  let modelCtl = el("input", { type: "text", id: "llmModel", class: "grow",
    value: llm.model || "", placeholder: "model name" });
  const modelValue = () => modelCtl.value.trim();
  const textModel = () => {
    const input = el("input", { type: "text", id: "llmModel", class: "grow",
      value: modelValue(), placeholder: "model name" });
    modelCtl.replaceWith(input); modelCtl = input;
  };
  const showModels = list => {
    const current = modelValue();
    const custom = el("option", { text: "Type a name instead…" });
    const sel = el("select", { id: "llmModel", class: "grow" },
      el("option", { value: "", text: "Choose a model…", disabled: true }),
      ...[...new Set([current, ...list].filter(Boolean))].map(m => el("option", { value: m, text: m })),
      custom);
    sel.value = current;
    let picked = current;   // the restore point when the admin escapes to free text
    sel.addEventListener("change", () => {
      if (sel.selectedOptions[0] !== custom) { picked = sel.value; return; }
      sel.value = picked; textModel(); modelCtl.focus();
    });
    modelCtl.replaceWith(sel); modelCtl = sel;
  };
  const load = el("button", { class: "btn small", type: "button", text: "Load models" });
  const conc = el("input", { type: "number", id: "llmConc", min: 1, max: 8, value: llm.concurrency ?? 2 });
  const key = el("input", { type: "password", id: "llmKey", placeholder: llm.has_api_key ? "Saved — leave blank to keep" : "No key set (optional for local endpoints)", autocomplete: "new-password" });
  const clear = el("input", { type: "checkbox", id: "llmClearKey" });
  const provider = el("select", { id: "llmProvider" }, ...LLM_PROVIDERS.map(o => el("option", { value: o.id, text: o.label })));
  const known = LLM_PROVIDERS.find(o => o.url && (llm.base_url || "").replace(/\/+$/, "") === o.url);
  provider.value = known ? known.id : "";
  const localHint = el("p", { class: "help", hidden: !known?.local,
    text: "Clipshelf runs in a container, so localhost is the container itself — use the host's address." });
  provider.addEventListener("change", () => {
    const o = LLM_PROVIDERS.find(x => x.id === provider.value) || {};
    if (o.url) base.value = o.url;
    localHint.hidden = !o.local;
    key.value = "";
    if (modelCtl.tagName === "SELECT") textModel();
  });
  base.addEventListener("input", () => { if (modelCtl.tagName === "SELECT") textModel(); });
  const note = el("p", { class: "hint", hidden: true });
  const showNote = (ok, text) => {
    note.className = ok ? "hint" : "err";
    note.textContent = text;
    note.hidden = false;
  };
  const check = el("button", { class: "btn tonal", type: "button", text: "Check connection" });
  const setCheck = s => {
    check.disabled = s === "busy";
    check.className = "btn tonal" + (s === "ok" ? " ok" : "");
    if (s === "busy") check.replaceChildren(el("span", { class: "spin" }), " Checking…");
    else if (s === "ok") { check.replaceChildren("✓"); check.setAttribute("aria-label", "Connection verified"); }
    else { check.replaceChildren("Check connection"); check.removeAttribute("aria-label"); }
  };
  setCheck(llm.verified_at ? "ok" : "idle");
  const form = el("form", {},
    el("div", { class: "field" }, el("label", { for: "llmProvider", text: "Provider" }), provider,
      el("p", { class: "help", text: "Presets fill the base URL. Any OpenAI-compatible endpoint works; vision is required." })),
    el("div", { class: "field" }, el("label", { for: "llmBase", text: "Base URL" }), base, localHint),
    el("div", { class: "field" }, el("label", { for: "llmModel", text: "Model" }),
      el("div", { class: "row2" }, modelCtl, load),
      el("p", { class: "help", text: "Load models asks the endpoint which models it has and turns this into a list to pick from." })),
    el("div", { class: "field" }, el("label", { for: "llmConc", text: "Concurrent requests" }), conc),
    el("div", { class: "field" }, el("label", { for: "llmKey", text: "API key" }), key,
      el("p", { class: "help", text: "Write-only: never returned by the server. Leave blank to keep a key for this endpoint; changing the URL clears it." })),
    el("div", { class: "field" },
      el("label", { for: "llmClearKey" }, clear, " Clear the saved key")),
    el("div", { class: "acts" },
      el("button", { class: "btn primary", type: "submit", text: "Save settings" }),
      check),
    note);
  form.addEventListener("input", () => setCheck("idle"));
  form.addEventListener("change", () => setCheck("idle"));
  load.addEventListener("click", async () => {
    load.disabled = true; load.textContent = "Loading…";
    try {
      const b = { base_url: base.value.trim() };
      if (clear.checked) b.api_key = "";
      else if (key.value) b.api_key = key.value;
      const list = (await sensitive("/api/admin/llm/models", b)).models || [];
      if (base.value.trim() !== b.base_url) return;
      if (list.length) showModels(list);
      else if (modelCtl.tagName === "SELECT") textModel();
      showNote(list.length, list.length
        ? `${list.length} models offered — pick one in the Model box.`
        : "The endpoint listed no models; type the name instead.");
    } catch (e) { if (e.status !== 401 && !e.cancelled) showNote(false, e.message); }
    finally { load.disabled = false; load.textContent = "Load models"; }
  });
  const body = () => ({ base_url: base.value.trim(), model: modelValue(),
    concurrency: Math.min(8, Math.max(1, parseInt(conc.value, 10) || 1)) });
  form.addEventListener("submit", async ev => {
    ev.preventDefault();
    const b = body();
    if (clear.checked) b.api_key = "";
    else if (key.value) b.api_key = key.value;
    try { await sensitive("/api/admin/llm", b); key.value = ""; clear.checked = false;
      toast("Interpretation settings saved."); renderAdmin(); }
    catch (e) { if (e.status !== 401 && !e.cancelled) banner("err", e.message, { sticky: true }); }
  });
  form.querySelector(".tonal").addEventListener("click", async () => {
    const b = { base_url: base.value.trim(), model: modelValue() };
    if (clear.checked) b.api_key = "";
    else if (key.value) b.api_key = key.value;
    const before = [base.value.trim(), modelValue(), key.value, conc.value, clear.checked].join("\n");
    note.hidden = true;
    setCheck("busy");
    try {
      const j = await sensitive("/api/admin/llm/check", b);
      if ([base.value.trim(), modelValue(), key.value, conc.value, clear.checked].join("\n") !== before)
        setCheck("idle");
      else if (j.check?.ok) setCheck("ok");
      else { setCheck("idle"); showNote(false, j.check?.message || "check failed"); }
    } catch (e) {
      setCheck("idle");
      if (e.status !== 401 && !e.cancelled) showNote(false, e.message);
    }
  });
  p.append(form,
    el("p", { class: "hint", text: "One admin-managed endpoint serves every capture, including Personal collections. The key stays server-side." }));
  if (scrErr) return p.append(errNode(scrErr)), p;
  const sKey = el("input", { type: "password", id: "scrKey", autocomplete: "new-password",
    placeholder: scr?.has_api_key ? "saved key present"
      : scr?.env_fallback ? "using TYPESAFE_API_KEY from the environment" : "API key" });
  const sClear = el("input", { type: "checkbox", id: "scrClearKey" });
  const sBody = () => sClear.checked ? { api_key: "" } : sKey.value ? { api_key: sKey.value } : {};
  const sNote = el("p", { class: "hint", hidden: true });
  const sShowNote = (ok, text) => { sNote.className = ok ? "hint" : "err"; sNote.textContent = text; sNote.hidden = false; };
  const sForm = el("form", {},
    el("div", { class: "field" }, el("label", { for: "scrKey", text: "API key" }), sKey,
      el("p", { class: "help", text: "Optional guard rail against prompt injection in captured pages. Without a key, interpretation runs unguarded." })),
    el("div", { class: "field" },
      el("label", { for: "scrClearKey" }, sClear, " Clear the saved key")),
    el("div", { class: "acts" },
      el("button", { class: "btn primary", type: "submit", text: "Save" }),
      el("button", { class: "btn tonal", type: "button", text: "Test" })),
    sNote);
  sForm.addEventListener("submit", async ev => {
    ev.preventDefault();
    try { await sensitive("/api/admin/screening", sBody()); sKey.value = ""; sClear.checked = false;
      toast("Screening settings saved."); renderAdmin(); }
    catch (e) { if (e.status !== 401 && !e.cancelled) sShowNote(false, e.message); }
  });
  sForm.querySelector(".tonal").addEventListener("click", async () => {
    sNote.hidden = true;
    try {
      const j = await sensitive("/api/admin/screening/check", sBody());
      const ok = j.check?.ok ?? j.ok;
      sShowNote(ok, (ok ? "Screening check passed: " : "Screening check failed: ") + (j.check?.message || j.message || ""));
    } catch (e) { if (e.status !== 401 && !e.cancelled) sShowNote(false, e.message); }
  });
  p.append(el("fieldset", {}, el("legend", { text: "Screening (TypeSafe)" }), sForm));
  return p;
}


onEnter("admin", renderAdmin);
