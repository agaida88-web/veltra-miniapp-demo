"use strict";
/* Runtime smoke test for the public navigation. No browser packages needed. */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function mockElement(id, hidden = false) {
  const classes = new Set(hidden ? ["hidden"] : []);
  const classList = {
    add(value) { classes.add(value); },
    remove(value) { classes.delete(value); },
    toggle(value, force) {
      if (force === undefined) force = !classes.has(value);
      if (force) classes.add(value);
      else classes.delete(value);
      return force;
    },
    contains(value) { return classes.has(value); },
  };
  return {
    id, classList, elements: { password: { autocomplete: "" } },
    style: {}, dataset: {}, textContent: "", innerHTML: "", value: "",
    querySelectorAll() { return []; },
    insertAdjacentHTML() {},
  };
}
const ids = new Map();
function el(id) {
  if (!ids.has(id)) ids.set(id, mockElement(id));
  return ids.get(id);
}
const pageIds = ["landingPage", "authPage", "appPage", "resetPage", "instructionPage"];
const pages = pageIds.map((id, i) => {
  const value = mockElement(id, i !== 0);
  ids.set(id, value);
  return value;
});
const document = {
  body: mockElement("body"),
  querySelector(selector) {
    if (selector === ".page") return pages[0];
    if (selector.startsWith("#")) return el(selector.slice(1).split(" ")[0]);
    return mockElement(selector);
  },
  querySelectorAll(selector) {
    if (selector === ".page") return pages;
    return [];
  },
  addEventListener() {},
};
const context = {
  document,
  window: { scrollTo() {} },
  location: { search: "" },
  history: { replaceState() {} },
  navigator: { clipboard: {} },
  URLSearchParams, FormData,
  setTimeout, clearTimeout,
  console,
  confirm() { return true; },
  prompt() { return ""; },
  fetch: async url => ({
    ok: true,
    json: async () => String(url) === "/api/config"
      ? { deposits_enabled: false, withdrawals_enabled: false, trading_account_connected: false }
      : { authenticated: false },
  }),
};
const script = fs.readFileSync(path.join(__dirname, "..", "static", "app.js"), "utf8");
vm.runInNewContext(script, context, { filename: "static/app.js" });
const visible = id => !el(id).classList.contains("hidden");

setImmediate(() => {
  assert(visible("landingPage"), "The landing page should render");
  assert.equal(typeof el("headerInstructions").onclick, "function");
  assert.equal(typeof el("heroSignup").onclick, "function");

  el("headerInstructions").onclick();
  assert(visible("instructionPage"), "Instruction opens");
  assert(!visible("landingPage"), "Landing hides under instruction");

  el("instructionBack").onclick();
  assert(visible("landingPage"), "Back returns to landing");

  el("heroSignup").onclick();
  assert(visible("authPage"), "Registration page opens");
  assert.equal(el("authHeading").textContent, "Создать кабинет");

  el("headerInstructions").onclick();
  assert(visible("instructionPage"), "Instruction opens from signup");

  el("instructionBack").onclick();
  assert(visible("landingPage"), "Back works without an authenticated session");

  el("headerLogin").onclick();
  assert(visible("authPage"), "Login page opens");
  assert.equal(el("authHeading").textContent, "Войти в кабинет");

  console.log("VELTRA public navigation smoke test passed");
});
