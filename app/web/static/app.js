// Theme toggle, chart tooltips, and kanban drag-and-drop. No dependencies besides htmx.
(function () {
  "use strict";

  // --- theme: system -> light -> dark -> system ---------------------------------------------
  const THEMES = ["system", "light", "dark"];
  function applyTheme(theme) {
    if (theme === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", theme);
    const btn = document.getElementById("theme-toggle");
    if (btn) {
      btn.textContent = { system: "Theme: auto", light: "Theme: light", dark: "Theme: dark" }[theme];
    }
  }
  function storedTheme() {
    try { return localStorage.getItem("theme") || "system"; } catch (e) { return "system"; }
  }
  document.addEventListener("DOMContentLoaded", function () {
    applyTheme(storedTheme());
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.addEventListener("click", function () {
      const next = THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length];
      try { localStorage.setItem("theme", next); } catch (e) { /* private mode */ }
      applyTheme(next);
    });
  });

  // --- tooltips for any element with data-tip ------------------------------------------------
  let tip;
  function showTip(el) {
    if (!tip) {
      tip = document.createElement("div");
      tip.className = "tooltip";
      tip.setAttribute("role", "tooltip");
      document.body.appendChild(tip);
    }
    tip.textContent = el.getAttribute("data-tip");
    const r = el.getBoundingClientRect();
    tip.style.left = r.left + r.width / 2 + "px";
    tip.style.top = r.top + "px";
    tip.hidden = false;
  }
  function hideTip() { if (tip) tip.hidden = true; }
  document.addEventListener("pointerover", function (e) {
    const el = e.target.closest && e.target.closest("[data-tip]");
    if (el) showTip(el);
  });
  document.addEventListener("pointerout", function (e) {
    if (e.target.closest && e.target.closest("[data-tip]")) hideTip();
  });
  document.addEventListener("focusin", function (e) {
    if (e.target.matches && e.target.matches("[data-tip]")) showTip(e.target);
  });
  document.addEventListener("focusout", hideTip);
  window.addEventListener("scroll", hideTip, { passive: true });

  // --- kanban drag and drop ------------------------------------------------------------------
  document.addEventListener("dragstart", function (e) {
    const tile = e.target.closest && e.target.closest(".tile[data-id]");
    if (!tile) return;
    e.dataTransfer.setData("text/plain", tile.dataset.id);
    e.dataTransfer.effectAllowed = "move";
    tile.classList.add("dragging");
  });
  document.addEventListener("dragend", function (e) {
    const tile = e.target.closest && e.target.closest(".tile");
    if (tile) tile.classList.remove("dragging");
    document.querySelectorAll(".drop-target").forEach(function (c) { c.classList.remove("drop-target"); });
  });
  document.addEventListener("dragover", function (e) {
    const col = e.target.closest && e.target.closest(".column[data-status]");
    if (!col) return;
    e.preventDefault();
    document.querySelectorAll(".drop-target").forEach(function (c) { if (c !== col) c.classList.remove("drop-target"); });
    col.classList.add("drop-target");
  });
  document.addEventListener("drop", async function (e) {
    const col = e.target.closest && e.target.closest(".column[data-status]");
    if (!col) return;
    e.preventDefault();
    col.classList.remove("drop-target");
    const id = e.dataTransfer.getData("text/plain");
    const tile = document.querySelector('.tile[data-id="' + CSS.escape(id) + '"]');
    if (!tile || tile.closest(".column") === col) return;
    const res = await fetch("/applications/" + encodeURIComponent(id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: col.dataset.status }),
    });
    if (res.status === 401) { window.location = "/login?next=/dashboard/board"; return; }
    if (!res.ok) { alert("Could not move this application."); return; }
    const from = tile.closest(".column");
    col.querySelector(".column-list").prepend(tile);
    [from, col].forEach(function (c) {
      c.querySelector(".count").textContent = c.querySelectorAll(".tile").length;
    });
    const sel = tile.querySelector("select");
    if (sel) sel.value = col.dataset.status;
  });
})();
