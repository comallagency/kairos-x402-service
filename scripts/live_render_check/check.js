#!/usr/bin/env node
/**
 * Headless render check for GET /admin/live (2026-09-29).
 *
 * Not a full browser (Playwright/Chromium) - jsdom executing the page's own
 * inline <script> against a real DOM, with Node's native fetch wired in.
 * Chosen over a real headless browser specifically to keep this in the
 * image permanently (scripts/deploy.sh runs it on every deploy): a real
 * Chromium install adds ~300-400MB and a chunk of apt packages to every
 * build (measured ad-hoc during this fix's investigation), which is a bad
 * trade for a check whose only job is "did the page's JS throw and get
 * silently swallowed" - exactly what jsdom + a real DOM can already catch,
 * without a full rendering engine. It cannot catch a CSS/layout bug; this
 * is deliberately a JS-execution-correctness check, not a visual one.
 *
 * Direct motivation: /admin/live went blank ("En attente d'événements…",
 * 0 identités) after a deploy, and returned 200 the whole time - a plain
 * HTTP status check (the rest of smoke_test.py) could never have caught
 * it. Root cause was load()'s catch block silently swallowing any
 * exception thrown by render() (see live.html's render()/renderInner()
 * split, same commit as this file) - this check exists so THAT class of
 * regression fails the deploy instead of shipping silently again.
 *
 * Usage: node live_render_check.js <url> <basic-auth-user> <basic-auth-pass>
 * Exit 0 on success, 1 on any JS error or on the page still showing its
 * pre-render placeholder after loading.
 */

const { JSDOM } = require("jsdom");

const [, , url, user, pass] = process.argv;
if (!url || !user || !pass) {
  console.error("usage: node live_render_check.js <url> <user> <pass>");
  process.exit(2);
}

const authHeader = "Basic " + Buffer.from(`${user}:${pass}`).toString("base64");

async function main() {
  const pageResp = await fetch(url, { headers: { Authorization: authHeader } });
  if (!pageResp.ok) {
    console.error(`FAIL fetching ${url}: HTTP ${pageResp.status}`);
    process.exit(1);
  }
  const html = await pageResp.text();

  const jsErrors = [];

  const dom = new JSDOM(html, {
    url,
    runScripts: "dangerously",
    beforeParse(window) {
      // jsdom reports the document as hidden/"prerender" by default (no
      // real visible tab exists) - live.html's load() deliberately does
      // nothing when document.hidden is true (`if (document.hidden)
      // return;`, avoiding wasted polling on a backgrounded tab), which
      // means load() silently no-ops on every single call inside jsdom
      // unless this is overridden - confirmed by hand while building this
      // check: zero fetch() calls ever fired without this.
      Object.defineProperty(window.document, "hidden", { value: false, configurable: true });
      Object.defineProperty(window.document, "visibilityState", { value: "visible", configurable: true });

      // jsdom has no fetch of its own - wire in Node's native fetch, with
      // the same Basic Auth header the page's own fetch() calls need for
      // /admin/data.json (credentials:'same-origin' doesn't carry Basic
      // Auth across jsdom's fetch, unlike a real browser tab that already
      // has it cached from loading this very page).
      window.fetch = (input, init = {}) => {
        const target = typeof input === "string" ? new URL(input, url).toString() : input;
        return fetch(target, {
          ...init,
          headers: { ...(init.headers || {}), Authorization: authHeader },
        });
      };
      window.onerror = (message, source, lineno, colno, error) => {
        jsErrors.push(String((error && error.stack) || message));
      };
      window.addEventListener("unhandledrejection", (e) => {
        jsErrors.push("unhandledrejection: " + String((e.reason && e.reason.stack) || e.reason));
      });
    },
  });

  // live.html's own poll interval is 3s; give the first load() a bit of
  // margin to actually complete (real network round-trip to /admin/data.json).
  // Bumped 5s->9s 2026-09-29 alongside GET /admin/data.json's new 24h
  // server-side aggregation (app/admin.py::_compute_agg_24h) - a real,
  // heavier cost on every request, not a fluke to paper over with a
  // shorter-lived fix.
  await new Promise((resolve) => setTimeout(resolve, 9000));

  const doc = dom.window.document;
  // #ticker starts with the static "En attente d'événements..." placeholder
  // and #feed starts completely empty - both get overwritten by render()'s
  // very first successful pass, so either one still showing its pre-render
  // state after the wait above means render() never completed (this is
  // exactly the symptom this check exists to catch - see the file header).
  const tickerText = (doc.getElementById("ticker") || {}).textContent || "";
  const feedText = (doc.getElementById("feed") || {}).textContent || "";
  const identitiesText = (doc.getElementById("f1") || {}).textContent || "";
  const bannerHtml = (doc.getElementById("banner") || {}).innerHTML || "";

  dom.window.close();

  if (jsErrors.length) {
    console.error("FAIL: JS error(s) during render:");
    jsErrors.forEach((e) => console.error("  " + e));
    process.exit(1);
  }

  if (bannerHtml.includes("banner err")) {
    console.error("FAIL: page itself reported a render error banner:", bannerHtml);
    process.exit(1);
  }

  const stillPlaceholder = tickerText.includes("attente") || feedText.trim() === "";
  if (stillPlaceholder) {
    console.error(
      "FAIL: page still shows its pre-render placeholder after loading " +
        `(ticker="${tickerText.slice(0, 60)}", feed empty=${feedText.trim() === ""}, ` +
        `identities=${identitiesText}) - render() likely never completed.`
    );
    process.exit(1);
  }

  console.log(`OK: no JS errors, feed populated, identities=${identitiesText}`);
  process.exit(0);
}

main().catch((e) => {
  console.error("FAIL: unexpected error running the check itself:", e);
  process.exit(1);
});
