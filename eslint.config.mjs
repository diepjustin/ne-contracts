// Lint config for the one script in index.html.
//
// The page has no build step and no node_modules: `scripts/lint_page.py` cuts
// the inline <script> out of index.html into .lint/, blank-padded so every line
// number it reports is the line number in index.html, and hands that to eslint
// via npx. Nothing here is fetched by a reader.
//
// The three rules below are not a style preference. A local `var end` inside
// render() shadowed the module-level `end` column for months and blanked every
// End cell on the site, and the crc32 selftest passed the whole time because
// the payload was fine and the page was wrong. no-shadow is the check that
// would have caught it. Add rules by all means, but not at the cost of these
// three staying silent.
export default [
  {
    files: ["**/*.js"],
    languageOptions: {
      // The page is deliberately ES5 syntax so it needs no transpiling. The
      // ES2015+ names below are runtime objects, not syntax, and are declared
      // rather than pulled from the `globals` package: one dependency fewer,
      // and an explicit list of what the page assumes a browser provides.
      ecmaVersion: 5,
      sourceType: "script",
      globals: {
        window: "readonly",
        document: "readonly",
        location: "readonly",
        history: "readonly",
        navigator: "readonly",
        console: "readonly",
        fetch: "readonly",
        Blob: "readonly",
        URL: "readonly",
        URLSearchParams: "readonly",
        TextDecoder: "readonly",
        AbortController: "readonly",
        Intl: "readonly",
        Promise: "readonly",
        Map: "readonly",
        Set: "readonly",
        setTimeout: "readonly",
        clearTimeout: "readonly",
        requestAnimationFrame: "readonly",
        btoa: "readonly",
        Uint8Array: "readonly",
        Uint16Array: "readonly",
        Uint32Array: "readonly",
        Int32Array: "readonly",
        Float64Array: "readonly",
        Event: "readonly",
      },
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: {
      "no-shadow": "error",
      "no-redeclare": "error",
      "no-undef": "error",
      // A name assigned and never read is usually half of an edit that was
      // not finished. Arguments are exempt: the event handlers here take
      // parameters they ignore on purpose.
      "no-unused-vars": ["error", { args: "none" }],
    },
  },
];
