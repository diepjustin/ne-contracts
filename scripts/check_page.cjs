// Load a staged build of the page in Chromium and fail on what a reader would hit.
//
//     node scripts/check_page.cjs _site
//
// The page's own ?selftest=1 checks the payload against the scrape, but until Oct 2026
// nothing ran it: the selftest, the search and the keyboard paths were checked by hand
// or not at all, so a page that threw on load would have deployed green. This serves the
// staged directory on 127.0.0.1 and checks, in order:
//
//   1. ?selftest=1 ends "all checks passed", with no page error on the way;
//   2. the page loads with no page error and asks for nothing off this server, on a
//      desktop and a phone viewport;
//   3. a search narrows the count, and Tab and Enter open a row without dropping focus
//      on the body (fixed in Oct 2026: every scroll re-rendered the focused row away);
//   4. axe-core finds no WCAG 2.1 A or AA violation with a row open, on both viewports.
//
// Each failure is a ::error line, and any of them exits 1. Needs playwright and
// axe-core: pages.yml and ci.yml install pinned versions outside the checkout for the
// run, rather than into a package.json, because the page itself has no build step and
// no dependencies -- the same reasoning as lint_page.py running eslint through npx.

"use strict";

const fs = require("fs");
const http = require("http");
const path = require("path");
const { chromium } = require("playwright");

const AXE = fs.readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");
const ROOT = path.resolve(process.argv[2] || ".");
const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".json": "application/json",
  ".js": "text/javascript",
  ".bin": "application/octet-stream",
};
// The selftest fetches every block it samples; on a runner that is about a minute.
const SELFTEST_MS = 10 * 60 * 1000;
const LOAD_MS = 3 * 60 * 1000;
const VIEWPORTS = { desktop: { width: 1366, height: 900 }, phone: { width: 390, height: 844 } };

let failures = 0;
function fail(what, detail) {
  failures++;
  const text = String(detail || "").replace(/%/g, "%25").replace(/\r/g, "%0D").replace(/\n/g, "%0A");
  console.log(`::error title=Page check: ${what}::${text}`);
}

function serve() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://127.0.0.1");
    let file = path.join(ROOT, decodeURIComponent(url.pathname));
    if (file !== ROOT && !file.startsWith(ROOT + path.sep)) {
      res.writeHead(403);
      res.end();
      return;
    }
    if (url.pathname.endsWith("/")) file = path.join(file, "index.html");
    fs.readFile(file, (err, body) => {
      if (err) {
        res.writeHead(404);
        res.end();
        return;
      }
      res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] || "application/octet-stream" });
      res.end(body);
    });
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

async function open(browser, base, viewport, query) {
  const context = await browser.newContext({ viewport });
  const page = await context.newPage();
  const errors = [];
  const external = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("requestfailed", (r) => {
    if (r.url().startsWith(base)) errors.push(`${r.url()}: ${r.failure() && r.failure().errorText}`);
  });
  // Nothing off this server: the page promises no third-party requests, and a check
  // that let them through would pass or fail on someone else's uptime.
  await page.route("**/*", (route) => {
    if (route.request().url().startsWith(base)) return route.continue();
    external.push(route.request().url());
    return route.abort();
  });
  await page.goto(base + "index.html" + (query || ""));
  return { context, page, errors, external };
}

async function checkSelftest(browser, base) {
  const { context, page, errors } = await open(browser, base, VIEWPORTS.desktop, "?selftest=1");
  const done = await page.waitForFunction(() => {
    const r = document.getElementById("report");
    return r && /\n(all checks passed|\d+ CHECK\(S\) FAILED)\n/.test(r.textContent);
  }, null, { timeout: SELFTEST_MS }).then(() => true, () => false);
  const report = await page.$eval("#report", (r) => r.textContent).catch(() => "");
  console.log("selftest:\n" + report.split("\n").map((l) => "  " + l).join("\n"));
  if (!done) fail("selftest did not finish", report.split("\n").slice(-5).join("\n") || "no report at all");
  for (const line of report.split("\n").filter((l) => l.startsWith("FAIL"))) fail("selftest", line);
  for (const e of errors) fail("page error during the selftest", e);
  await context.close();
}

async function checkPage(browser, base, name) {
  const { context, page, errors, external } = await open(browser, base, VIEWPORTS[name]);
  const loaded = await page.waitForFunction(() => !document.getElementById("scroller").hidden,
    null, { timeout: LOAD_MS }).then(() => true, () => false);
  if (!loaded) {
    fail(`${name}: the table never appeared`, errors.join("\n"));
    await context.close();
    return;
  }

  const count = () => page.$eval("#count", (e) => e.textContent);
  const before = await count();
  await page.fill("#q", "kiewit");
  await page.waitForFunction((was) => document.getElementById("count").textContent !== was,
    before, { timeout: 30000 }).catch(() => {});
  const after = await count();
  const shown = await page.$$eval("#rows tr[data-row]", (rows) => rows.length);
  console.log(`${name}: search "kiewit": ${after}`);
  if (after === before || !shown) fail(`${name}: search`, `"kiewit" left the count at "${after}" with ${shown} rows drawn`);

  const opens = await page.$("#rows tr[data-row][tabindex]");
  if (opens) {
    await page.focus("#rows tr[data-row][tabindex]");
    let dropped = 0;
    for (let i = 0; i < 30; i++) {
      await page.keyboard.press("Tab");
      if (await page.evaluate(() => document.activeElement === document.body)) dropped++;
    }
    if (dropped) fail(`${name}: keyboard`, `focus fell to the body ${dropped} times in 30 Tabs through the rows`);
    await page.focus("#rows tr[data-row][tabindex]");
    await page.keyboard.press("Enter");
    const panel = await page.waitForFunction(() => !document.getElementById("detail").hidden,
      null, { timeout: 30000 }).then(() => true, () => false);
    const focus = await page.evaluate(() => {
      const a = document.activeElement;
      return a === document.body ? "the body" : a.tagName.toLowerCase() + (a.dataset.row ? " " + a.dataset.row : "");
    });
    console.log(`${name}: Enter on a row: panel ${panel ? "open" : "NOT open"}, focus on ${focus}`);
    if (!panel) fail(`${name}: keyboard`, "Enter on a focused row did not open its panel");
    if (focus === "the body") fail(`${name}: keyboard`, "Enter on a row dropped focus on the body");
  } else {
    console.log(`${name}: no row opens (this build has no descriptions or AI summaries)`);
  }

  await page.addScriptTag({ content: AXE });
  const found = await page.evaluate(() => window.axe.run(document, {
    runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"] },
  }));
  for (const v of found.violations) {
    fail(`${name}: axe ${v.id}`, `${v.help} (${v.nodes.length} element(s)), e.g. ` +
      v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join("; "));
  }
  console.log(`${name}: axe: ${found.violations.length} violation(s), ${found.passes.length} rule(s) passed`);

  for (const e of errors) fail(`${name}: page error`, e);
  for (const u of new Set(external)) fail(`${name}: request off this server`, u);
  await context.close();
}

(async () => {
  if (!fs.existsSync(path.join(ROOT, "index.html")) || !fs.existsSync(path.join(ROOT, "manifest.json"))) {
    fail("nothing to check", `${ROOT} has no index.html and manifest.json`);
    process.exit(1);
  }
  const server = await serve();
  const base = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch();
  try {
    await checkSelftest(browser, base);
    for (const name of Object.keys(VIEWPORTS)) await checkPage(browser, base, name);
  } catch (e) {
    fail("the check itself failed", e && e.stack || e);
  } finally {
    await browser.close();
    server.close();
  }
  console.log(failures ? `${failures} problem(s)` : "page check: all clear");
  process.exit(failures ? 1 : 0);
})();
