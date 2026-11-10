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
//   5. the search finds a vendor however its name is punctuated, spaced or ordered, the
//      Agency box answers to "dot", and a "Began or ends" preset narrows the table, goes
//      into the address and comes back out of it with Reset (added in Nov 2026, when
//      "j.e. dunn" and "je dunn" found two different sets of records), and the list of
//      agencies opens inside the screen on both viewports.
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
  await checkSearch(page);
  for (const e of errors) fail("page error during the selftest", e);
  await context.close();
}

// What a vendor search must and must not find. Names are ones the state publishes; each
// "false" is a match an earlier draft of the rule made. Run against the page's own
// functions, which ?selftest=1 publishes on window.neContracts.
const VENDOR_CASES = [
  ["je dunn", "J.E. DUNN CONSTRUCTION COMPANY", true],
  ["j.e. dunn", "JE DUNN CONSTRUCTION CO", true],
  ["jedunn", "J.E DUNN CONSTRUCTION COMPANY", true],
  ["je dunn const", "J.E. DUNN CONSTRUCTION COMPANY", true],
  ["u.s. bank", "US BANK NATIONAL ASSOCIATION", true],
  ["o'neill", "ONEILL AUTO SUPPLY INC", true],
  ["oneill", "O NEILL, JESSICA JANAE", true],
  ["hy-vee", "HYVEE, OMAHA", true],
  ["john smith", "SMITH, JOHN W", true],
  ["blue cross blue shield", "BLUE CROSS AND BLUE SHIELD OF NEBRASKA", true],
  ["roche diagnostics", "ROCHEDIAGNOSTICSCORPORATION", true],
  ["at&t", "AT & T", true],
  ["mart", "WALMART STORE 2432", true],
  ["at&t", "ATTORNEY GENERAL", false],
  ["at&t", "AT HOME TILE", false],
  ["3m", "OPTION 3 MATERIAL HANDLING LLC", false],
  ["hdr", "T & H DRYWALL SERVICES INC", false],
  ["mcdonald's", "MCDONALD, STEPHANIE", false],
  ["d & d", "R & D SYST INC", false],
  ["blue blue", "BLUE CROSS", false],
  ["wal mart", "MARTHA WALLACE", false],
  ["art supply", "MEDSUPPLY PARTNERS", false],
];
const MARK_CASES = [
  ["john smith", "SMITH, JOHN W", "<mark>SMITH</mark>, <mark>JOHN</mark> W"],
  ["je dunn", "J.E. DUNN & CO", "<mark>J.E. DUNN</mark> &amp; CO"],
  ["dunn", "J.E. DUNN & CO", "J.E. <mark>DUNN</mark> &amp; CO"],
];
// [agency, typed, answer]: "" for its own name, the alias when only that matches.
const AGENCY_CASES = [
  ["Roads, Department of", "roads", ""],
  ["Roads, Department of", "dot", "DOT"],
  ["Roads, Department of", "department of transportation", "Transportation, Department of"],
  ["Roads, Department of", "dhhs", null],
  ["Health & Human Services, Department of", "health and human", ""],
  ["Workers' Compensation Court", "workers comp", ""],
  ["Attorney General", "ag", "AG"],
  ["Educational Telecommunications Commission", "ag", null],
];
const WHEN_CASES = [
  ["began30", 20261109, { ends: false, from: 20261010, to: 20261109 }],
  ["ends90", 20261109, { ends: true, from: 20261109, to: 20270207 }],
  ["ends30", 20240215, { ends: true, from: 20240215, to: 20240316 }],
  ["began365", 20261109, null],
];

async function checkSearch(page) {
  const wrong = await page.evaluate(([vendors, marks, agencies, whens]) => {
    const t = window.neContracts, out = [];
    for (const name of ["vendorPlan", "vendorRanges", "markRanges", "agencyMatch", "whenRange"]) {
      if (!t || typeof t[name] !== "function") return [`window.neContracts.${name} is not there`];
    }
    for (const [typed, name, want] of vendors) {
      const got = t.vendorRanges(name.toLowerCase(), t.vendorPlan(typed)) !== null;
      if (got !== want) out.push(`"${typed}" ${got ? "finds" : "does not find"} ${name}`);
    }
    for (const [typed, name, want] of marks) {
      const got = t.markRanges(name, t.vendorRanges(name.toLowerCase(), t.vendorPlan(typed)));
      if (got !== want) out.push(`"${typed}" marks ${name} as ${got}, not ${want}`);
    }
    for (const [name, typed, want] of agencies) {
      const got = t.agencyMatch(name, typed);
      if (got !== want) out.push(`agency "${typed}" against ${name}: ${JSON.stringify(got)}, not ${JSON.stringify(want)}`);
    }
    for (const [name, day, want] of whens) {
      const got = JSON.stringify(t.whenRange(name, day));
      if (got !== JSON.stringify(want)) out.push(`${name} from ${day}: ${got}, not ${JSON.stringify(want)}`);
    }
    return out;
  }, [VENDOR_CASES, MARK_CASES, AGENCY_CASES, WHEN_CASES]);
  for (const w of wrong) fail("search matching", w);
  console.log(`search: ${VENDOR_CASES.length + MARK_CASES.length + AGENCY_CASES.length + WHEN_CASES.length} ` +
    `known answers, ${wrong.length} wrong`);

  // The same thing through the page: the functions being right is no use if the filter
  // does not call them. Take a vendor from the table and look for it two other ways.
  const count = () => page.$eval("#count", (e) => e.textContent.split(" of ")[0]);
  const search = async (text) => {
    const was = await count();
    await page.fill("#q", text);
    await page.waitForFunction((old) => document.getElementById("count").textContent.split(" of ")[0] !== old,
      was, { timeout: 5000 }).catch(() => {});
    return page.$$eval("#rows tr[data-row] td.vendor", (cells) => cells.map((c) => ({
      name: c.textContent, marked: !!c.querySelector("mark") })));
  };
  const picked = (await search("kiewit")).find((r) => /[a-z0-9]{3}[^a-z0-9]+[a-z0-9]{3}/i.test(r.name));
  if (!picked) {
    console.log("search: no two-word vendor under \"kiewit\" to look for another way");
  } else {
    const parts = picked.name.toLowerCase().split(/[^a-z0-9]+/).filter(Boolean);
    for (const [how, typed] of [["run together", parts.join("")], ["in reverse", parts.slice().reverse().join(" ")]]) {
      const rows = await search(typed);
      const hit = rows.find((r) => r.name === picked.name);
      console.log(`search: "${typed}" (${picked.name}, ${how}): ${await count()} record(s)`);
      if (!hit) fail("search", `"${typed}" did not find ${picked.name}, which "kiewit" does`);
      else if (!hit.marked) fail("search", `"${typed}" found ${picked.name} but marked nothing in it`);
    }
  }
  await page.fill("#q", "");

  const hasRoads = await page.$$eval("#entity option", (os) => os.some((o) => o.textContent === "Roads, Department of"));
  if (hasRoads) {
    await page.fill("#entity-q", "dot");
    const offered = await page.$$eval("#entity-list li[role=option]", (ls) => ls.map((l) => l.textContent));
    if (offered.length !== 1 || !/^Roads, Department of/.test(offered[0])) {
      fail("agency box", `"dot" offered ${JSON.stringify(offered)}, not Roads, Department of alone`);
    }
    await page.keyboard.press("Enter");
    const chosen = await page.$eval("#entity", (s) => s.options[s.selectedIndex].textContent);
    console.log(`agency: "dot" then Enter chose ${chosen}`);
    if (chosen !== "Roads, Department of") fail("agency box", `"dot" then Enter chose "${chosen}"`);
  }

  const all = await count();
  await page.selectOption("#when", "began90");
  await page.waitForFunction((old) => document.getElementById("count").textContent.split(" of ")[0] !== old,
    all, { timeout: 5000 }).catch(() => {});
  const recent = await count();
  const linked = new URL(page.url()).searchParams.get("when");
  console.log(`when: began90 leaves ${recent} of ${all} (address says when=${linked})`);
  if (recent === all) fail("began or ends", `"Began, last 90 days" left the count at ${all}`);
  if (linked !== "began90") fail("began or ends", `the address carries when=${linked}, not began90`);
  await page.click('thead th[data-k="3"]');
  await page.click("#reset");
  await page.waitForFunction(() => !/[?&](when|sort|agency)=/.test(location.search), null, { timeout: 5000 }).catch(() => {});
  const left = new URL(page.url()).searchParams;
  for (const key of ["when", "sort", "agency"]) {
    if (left.has(key)) fail("reset", `Reset left ${key}=${left.get(key)} in the address`);
  }
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

  // The agency list has to open on the screen. Until Nov 2026 a phone showed it 135px
  // off the left edge: it is wider than the box and was hung off the box's right side.
  await page.focus("#entity-q");
  const list = await page.$eval("#entity-list", (l) => {
    const r = l.getBoundingClientRect();
    return { hidden: l.hidden, left: Math.round(r.left), right: Math.round(r.right),
             wide: document.documentElement.clientWidth, sideways: l.scrollWidth - l.clientWidth };
  });
  console.log(`${name}: agency list spans ${list.left} to ${list.right} px of ${list.wide}`);
  if (list.hidden) fail(`${name}: agency list`, "focusing the Agency box did not open the list");
  else if (list.left < 0 || list.right > list.wide || list.sideways > 0) {
    fail(`${name}: agency list`, `it spans ${list.left} to ${list.right} px of a ${list.wide} px screen` +
      (list.sideways > 0 ? `, and scrolls ${list.sideways} px sideways` : ""));
  }
  await page.keyboard.press("Escape");

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
