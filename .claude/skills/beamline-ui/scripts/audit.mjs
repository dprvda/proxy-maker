#!/usr/bin/env node
// REASON: agents fall back to familiar pieces (a text Input for an age, a Select for three choices, raw <input>, hex
// colours) unless something checks. This reads an app's source and names each fallback with the Beamline element that
// fits, so the agent can fix it before calling the screen done. Deterministic, no dependencies, read-only.
// Usage: node audit.mjs [--app <dir>] [--json]
import fs from "node:fs";
import path from "node:path";

const argv = process.argv.slice(2);
const flag = (k) => {
  const i = argv.indexOf(`--${k}`);
  return i === -1 ? undefined : argv[i + 1];
};
if (argv.includes("--help") || argv.includes("-h")) {
  console.log(`Check an app for fallbacks Beamline has a better element for.
  node audit.mjs [--app <dir>] [--json]
Reports native controls, Inputs typed as number/date/tel/…, Selects with 2–5 options, hand-made steppers, hex colours,
Beamline packages installed but never imported, and a missing BEAMLINE.md mapping. Exit 1 when a "fix" finding remains.`);
  process.exit(0);
}
const asJson = argv.includes("--json");
let app = path.resolve(flag("app") ?? process.cwd());
for (let d = app; ; d = path.dirname(d)) {
  if (fs.existsSync(path.join(d, "package.json"))) {
    app = d;
    break;
  }
  if (path.dirname(d) === d) break;
}
// the whole app, not only src/: a Next.js app keeps its code in app/ and components/, and the installer's
// src/styles/beamline/ would otherwise hide all of it (the audit then saw nothing imported and said remove everything)
const SKIP = new Set([
  "node_modules",
  "dist",
  "build",
  "out",
  "coverage",
  "public",
  "storybook-static",
  ".beamline",
  ".git",
  ".next",
  ".vercel",
  ".turbo",
  "beamline",
]);
const walk = (d) =>
  fs.readdirSync(d, { withFileTypes: true }).flatMap((e) => {
    if (e.isDirectory()) return SKIP.has(e.name) || e.name.startsWith(".") ? [] : walk(path.join(d, e.name));
    return /\.(tsx|jsx|ts|js|css)$/.test(e.name) && !/\.(d|test|spec)\.ts$/.test(e.name) ? [path.join(d, e.name)] : [];
  });
// another kit's own primitives are not the app's screens: shadcn keeps them in components/ui (components.json)
const vendorUi = fs.existsSync(path.join(app, "components.json")) ? /^(src\/)?components\/ui\// : null;
const files = walk(app)
  .filter((f) => !vendorUi || !vendorUi.test(path.relative(app, f).split(path.sep).join("/")))
  .map((f) => ({ f, rel: path.relative(app, f), text: fs.readFileSync(f, "utf8") }));
const code = files.filter((x) => /\.(tsx|jsx)$/.test(x.f));
const lineOf = (text, index) => text.slice(0, index).split("\n").length;
const findings = [];
const add = (level, file, index, what, fix) =>
  findings.push({ level, at: `${file.rel}:${lineOf(file.text, index)}`, what, fix });

// Elements imported from Beamline, by local name → package.
const imported = new Map();
for (const x of files)
  for (const m of x.text.matchAll(/import\s+(?:type\s+)?\{([^}]*)\}\s+from\s+["'](@beamline\/[^"']+)["']/g))
    for (const n of m[1]
      .split(",")
      .map((s) =>
        s
          .trim()
          .split(/\s+as\s+/)
          .pop(),
      )
      .filter(Boolean))
      imported.set(n.replace(/^type\s+/, ""), m[2]);

// The opening tag starting at `i`, braces balanced (props hold arrow functions and objects).
function tagAt(text, i) {
  let depth = 0;
  for (let j = i; j < text.length; j++) {
    const c = text[j];
    if (c === "{") depth++;
    else if (c === "}") depth--;
    else if (c === ">" && depth === 0) return text.slice(i, j + 1);
  }
  return text.slice(i, i + 400);
}
const attr = (tag, name) => {
  const m = tag.match(new RegExp(`\\s${name}=(?:"([^"]*)"|'([^']*)'|\\{([^}]*)\\})`));
  return m ? (m[1] ?? m[2] ?? m[3]) : undefined;
};

// The top-level items of the literal that starts at `start` ("[" or "{"), as text; undefined when it never closes.
function literalItems(text, start) {
  const items = [];
  let depth = 0,
    from = start + 1;
  for (let j = start; j < text.length; j++) {
    const c = text[j];
    if ("[{(".includes(c)) depth++;
    else if ("]})".includes(c)) {
      if (--depth === 0) {
        items.push(text.slice(from, j));
        return items.map((item) => item.trim()).filter(Boolean);
      }
    } else if (c === "," && depth === 1) {
      items.push(text.slice(from, j));
      from = j + 1;
    }
  }
  return undefined;
}
// How many items the literal at `start` holds: an option list or a value → label record. A spread item counts what it
// spreads when that is a named list (`...GOALS`, `...options(GOALS)`); spread runtime data (`...new Set(list)`) makes
// the count unknown (t27 read `[...new Set(currencies)]` as one option).
function itemsFrom(text, start, from, depth = 0) {
  const items = literalItems(text, start);
  if (!items) return undefined;
  let n = 0;
  for (const item of items) {
    if (!item.startsWith("...")) n++;
    else {
      const k = countOf(item.slice(3), from, depth + 1);
      if (k === undefined) return undefined;
      n += k;
    }
  }
  return n;
}
// How many options an expression yields: a literal list, a named list as it is or mapped item for item (`LIST`,
// `LIST.map(…)`), or a helper called on one (`options(GOALS)`, `options([…])`); anything else only at runtime.
function countOf(expr, from, depth = 0) {
  if (depth > 4) return undefined;
  const e = expr.trim();
  if (e.startsWith("[") || e.startsWith("{")) return itemsFrom(e, 0, from, depth);
  const named = e.match(/^([A-Za-z_$][\w$]*)\s*(?:\.map\(|$)/);
  if (named) return collectionLength(named[1], from, depth);
  const call = e.match(/^[A-Za-z_$][\w$.]*\(/);
  if (call) {
    const args = literalItems(e, call[0].length - 1);
    if (args?.length === 1) return countOf(args[0], from, depth + 1);
  }
  return undefined;
}
// `const NAME = [ … ]` or `const NAME: Record<…> = { … }` in the file that uses it, or in another file only when this
// one imports the name (t27: a local list built with useMemo was read as another file's list of the same name).
function collectionLength(name, from, depth = 0) {
  const definition = new RegExp(`(?:const|let)\\s+${name}\\b[^=]*=\\s*[\\[{]`);
  const imported = new RegExp(`import\\s[^;]*\\b${name}\\b[^;]*from`);
  if (from && new RegExp(`(?:const|let)\\s+${name}\\b`).test(from.text)) {
    const m = from.text.match(definition);
    return m ? itemsFrom(from.text, m.index + m[0].length - 1, from, depth) : undefined;
  }
  if (from && !imported.test(from.text)) return undefined;
  for (const x of files) {
    if (x === from) continue;
    const m = x.text.match(definition);
    if (m) return itemsFrom(x.text, m.index + m[0].length - 1, x, depth);
  }
  return undefined;
}
// Arguments of the call whose "(" is at `open`, as text.
function callArgs(text, open) {
  let depth = 0;
  for (let j = open; j < text.length; j++) {
    if ("([{".includes(text[j])) depth++;
    else if (")]}".includes(text[j]) && --depth === 0) return text.slice(open + 1, j);
  }
  return "";
}
// The name of the call whose argument list contains `index` (scanning back to its unclosed "(").
function enclosingCall(text, index) {
  let depth = 0;
  for (let j = index; j >= 0; j--) {
    const c = text[j];
    if (")]}".includes(c)) depth++;
    else if ("([{".includes(c)) {
      if (depth === 0 && c === "(") return text.slice(Math.max(0, j - 60), j).match(/([\w$.]+)\s*$/)?.[1];
      if (depth > 0) depth--;
    }
  }
  return undefined;
}
const optionCount = (tag, from) => {
  // the whole expression, braces and all (the attribute reader stops at the first "}": t17's inline option objects)
  const at = tag.search(/\soptions=\{/);
  if (at < 0) return undefined;
  const open = tag.indexOf("{", at);
  const items = literalItems(tag, open);
  return items?.length === 1 ? countOf(items[0], from) : undefined;
};

const NATIVE = {
  input: "Input / NumberField / Checkbox / Switch / DatePicker … (a Beamline field for the data)",
  select: "Select, SegmentedControl, RadioGroup or Combobox",
  textarea: "Textarea",
  button: "Button",
  table: "DataGrid (a list people work through) or Table",
};
const TYPED = {
  number: "NumberField (exact, with min/max and a unit) or Slider (a value on a scale)",
  date: "DatePicker",
  "datetime-local": "DatePicker + TimePicker",
  time: "TimePicker",
  tel: "PhoneInput",
  password: "PasswordField",
  range: "Slider",
  checkbox: "Checkbox or Switch",
  radio: "RadioGroup / SegmentedControl / RadioCards",
  file: "FileDropzone",
  color: "ColorPicker or ColorPalette",
  search: "SearchField",
};

for (const x of code) {
  for (const m of x.text.matchAll(/<(input|select|textarea|button|table)[\s>/]/g))
    add("fix", x, m.index, `native <${m[1]}>`, NATIVE[m[1]]);
  const inputName = [...imported].find(([, pkg]) => /\/input$/.test(pkg))?.[0] ?? "Input";
  for (const m of x.text.matchAll(new RegExp(`<${inputName}[\\s/>]`, "g"))) {
    const type = attr(tagAt(x.text, m.index), "type");
    if (type && TYPED[type]) add("fix", x, m.index, `${inputName} type="${type}"`, TYPED[type]);
  }
  const selectName = [...imported].find(([, pkg]) => /\/select$/.test(pkg))?.[0];
  if (selectName)
    for (const m of x.text.matchAll(new RegExp(`<${selectName}[\\s/>]`, "g"))) {
      const n = optionCount(tagAt(x.text, m.index), x);
      if (n !== undefined && n <= 5)
        add(
          "fix",
          x,
          m.index,
          `${selectName} with ${n} options`,
          "show them: SegmentedControl or RadioGroup (RadioCards when each option needs a description)",
        );
    }
  // Option lists handed to a helper that renders a Select: selectField("diet", "Diet", [ … ]) or (…, DIETS).
  for (const m of x.text.matchAll(/\b((?:[a-z]\w*)?[sS]elect\w*)\(/g)) {
    const args = callArgs(x.text, m.index + m[0].length - 1);
    const inline = args.indexOf("[");
    let n = inline >= 0 ? itemsFrom(args, inline, x) : undefined;
    for (const id of n === undefined ? (args.match(/\b[A-Z][A-Z0-9_]{2,}\b/g) ?? []) : []) {
      n = collectionLength(id, x);
      if (n !== undefined) break;
    }
    if (n !== undefined && n <= 5)
      add(
        "fix",
        x,
        m.index,
        `${m[1]}(…) with ${n} options`,
        "show them: SegmentedControl or RadioGroup (RadioCards when each option needs a description)",
      );
  }
  // Input types passed through a helper's props object: textField("age", …, { type: "number" }).
  if (imported.has(inputName))
    for (const m of x.text.matchAll(
      /\btype:\s*["'](number|date|datetime-local|time|tel|password|range|file|color|search)["']/g,
    )) {
      // only props headed for an input helper (textField(…, { type })), not a grid column's or a schema's type
      const call = enclosingCall(x.text, m.index);
      if (call && /(input|field|text)/i.test(call) && !/column/i.test(call))
        add("fix", x, m.index, `${inputName} given type "${m[1]}"`, TYPED[m[1]]);
    }
  // onChange on a Beamline Checkbox or Switch is the native event, not the value (t23: Haiku got events and cast to any)
  for (const [local, pkgName] of imported)
    if (/\/(checkbox|switch)$/.test(pkgName))
      for (const m of x.text.matchAll(new RegExp(`<${local}\\b`, "g"))) {
        const tag = tagAt(x.text, m.index);
        if (/\sonChange=/.test(tag) && !/\sonCheckedChange=/.test(tag))
          add(
            "fix",
            x,
            m.index,
            `${local} given onChange (it receives a native event, not the value)`,
            "use onCheckedChange={(checked) => …} — it receives true or false",
          );
      }
  // A multi-step flow drawn by hand: step state with Progress/buttons but no Stepper.
  const stepState = x.text.match(/const\s+\[\s*(step|currentStep|activeStep|stepIndex)\b/);
  // (a StepForm owns its steps: an app may drive them with its own step state, t17)
  if (stepState && !["Stepper", "StepForm"].some((n) => imported.has(n)))
    add(
      "fix",
      x,
      stepState.index,
      "multi-step flow without Stepper",
      "Stepper shows where the person is and lets them go back",
    );
  // Repainting the look by hand ({ "--pui-surface": "#fff" }): theme, brand and look already do it, consistently.
  const repainted = [
    ...x.text.matchAll(/["']?(--pui-[a-z0-9-]+)["']?\s*:\s*["']?(#[0-9a-fA-F]{3,8}|rgba?\(|oklch\()/g),
  ];
  if (repainted.length)
    add(
      "fix",
      x,
      repainted[0].index,
      `${repainted.length} Beamline token(s) set to literal colours (${repainted
        .slice(0, 3)
        .map((m) => m[1])
        .join(", ")}…)`,
      'use UIRoot theme="light"/"dark", brand and a look; for a palette of your own make a custom style: style create / set / add (see Looks in the capabilities)',
    );
  for (const m of x.text.matchAll(/#[0-9a-fA-F]{3,8}\b(?![\w-])/g)) {
    if (repainted.length && /--pui-[a-z0-9-]+["']?\s*:\s*["']?$/.test(x.text.slice(Math.max(0, m.index - 40), m.index)))
      continue;
    // the root's brand is where a colour literal belongs
    if (/\bbrand\s*[=:]\s*["'{]?\s*["']?$/.test(x.text.slice(Math.max(0, m.index - 14), m.index))) continue;
    add(
      "warn",
      x,
      m.index,
      `colour literal ${m[0]}`,
      "set UIRoot brand or use tokens (var(--pui-ink-2), var(--pui-surface)…)",
    );
  }
}
// Emoji as icons or decoration (t25: "👤 New client intake", "🎉 New event brief"; owner: "i hate emojis in this
// design"): a Beamline screen's icons are lucide-react, in the part's icon slot. Comments are skipped; © ® ™ are text;
// a chat's reactions are content.
const EMOJI = /(?:\p{Extended_Pictographic}|\p{Regional_Indicator}{2})(?:\uFE0F|\u200D\p{Extended_Pictographic})*/gu;
for (const x of code) {
  const text = x.text.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, (c) => " ".repeat(c.length));
  const lines = new Set();
  for (const m of text.matchAll(EMOJI)) {
    if (/^[©®™‼⁉]/.test(m[0])) continue;
    const line = lineOf(text, m.index);
    // content, not decoration: a chat's reactions or an emoji picker's choices
    if (/reaction|emoji/i.test(text.split("\n")[line - 1])) continue;
    if (lines.has(line)) continue;
    lines.add(line);
    add(
      "fix",
      x,
      m.index,
      `emoji ${m[0]} in the interface`,
      'use a lucide-react icon (a Button\'s leading icon, <Users className="size-4" /> before the words) or plain words; Beamline screens carry no emoji',
    );
  }
}
for (const x of files.filter((y) => y.f.endsWith(".css"))) {
  // a palette repainted in CSS (t12: nine --pui-* tokens set to hex) is the same fallback as one in a style prop
  const repainted = [...x.text.matchAll(/(--pui-[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,8}|rgba?\(|hsla?\(|oklch\()/g)];
  if (repainted.length)
    add(
      "fix",
      x,
      repainted[0].index,
      `${repainted.length} Beamline token(s) set to literal colours (${repainted
        .slice(0, 3)
        .map((m) => m[1])
        .join(", ")}…)`,
      "use UIRoot theme, brand and a look; for a palette of your own make a custom style: style create / set / add (see Looks in the capabilities)",
    );
  for (const m of x.text.matchAll(/#[0-9a-fA-F]{3,8}\b(?![\w-])/g)) {
    if (/--pui-[a-z0-9-]+\s*:\s*$/.test(x.text.slice(Math.max(0, m.index - 40), m.index))) continue;
    add("warn", x, m.index, `colour literal ${m[0]}`, "set UIRoot brand or use tokens (var(--pui-…))");
  }
}

// Style control: Beamline parts repainted through data-slot selectors, hand-set typefaces, and --pui-* names that are
// not tokens (trial t11: 132 data-slot rules and --pui-font-sans, which does nothing). These do not follow the look,
// theme or upgrades; UIRoot, sidebarTone and the tokens do.
const tokenNames = (() => {
  // every name the installed packages define or read (--pui-brand is only read); a kit app keeps them in its sheet
  const sheets = [path.join(app, "src/styles/beamline/beamline.css")];
  const scan = (d, depth) => {
    if (!fs.existsSync(d) || depth > 4) return;
    for (const e of fs.readdirSync(d, { withFileTypes: true }))
      if (e.isDirectory()) scan(path.join(d, e.name), depth + 1);
      else if (e.name.endsWith(".css")) sheets.push(path.join(d, e.name));
  };
  scan(path.join(app, "node_modules/@beamline"), 0);
  const names = new Set();
  for (const f of sheets.filter((f) => fs.existsSync(f)))
    for (const m of fs.readFileSync(f, "utf8").matchAll(/--pui-[a-z0-9-]+/g)) names.add(m[0]);
  return names.size > 50 ? names : null;
})();
// Whether `index` in a stylesheet sits inside an @layer block (unlayered rules beat every layered one).
function inLayer(text, index) {
  const open = [];
  for (const m of text.slice(0, index).matchAll(/(@layer[^{;]*)?\{|\}/g))
    if (m[0] === "}") open.pop();
    else open.push(!!m[1]);
  return open.some(Boolean);
}
for (const x of files.filter((y) => y.f.endsWith(".css"))) {
  // A universal padding reset outside a layer (t17: Haiku replaced the app's Tailwind with `* { margin: 0; padding: 0 }`):
  // unlayered rules beat Beamline's layered styles, so every part lost its padding and the pages their gutters.
  const css = x.text.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, " "));
  for (const m of css.matchAll(/(^|[{};]\s*)(\*(?:\s*,\s*\*?::?(?:before|after))*)\s*\{([^{}]*)\}/g))
    if (/(^|[;\s])padding\s*:\s*0/.test(m[3]) && !inLayer(css, m.index + m[1].length))
      add(
        "fix",
        x,
        m.index + m[1].length,
        "a universal padding reset outside a layer strips the padding of every Beamline part",
        "delete it (Beamline's foundation already resets), or put it in @layer base; keep the app's Tailwind import",
      );
  const repaints = [...x.text.matchAll(/([^{}]*\[data-slot[^{}]*)\{([^{}]*)\}/g)].filter((m) =>
    /(^|[;\s])(color|background(-color)?|font(-family|-size|-weight)?|letter-spacing|box-shadow|border(-color)?)\s*:/.test(
      m[2],
    ),
  );
  if (repaints.length)
    add(
      "warn",
      x,
      repaints[0].index,
      `${repaints.length} rule(s) repaint Beamline parts through data-slot (${repaints[0][1].trim().slice(0, 60)}…)`,
      "restyle through UIRoot (look, brand, theme, density, radiusScale, typeScale), sidebarTone and tokens; data-slot is for layout only (width, gap, order)",
    );
  const faces = [...x.text.matchAll(/font-family\s*:/g)];
  if (faces.length)
    add(
      "warn",
      x,
      faces[0].index,
      `font-family set ${faces.length} time(s) by hand`,
      "set --pui-font-body / --pui-font-display (and --pui-font-heading) on the root's class; install faces with --fonts <id>",
    );
  if (tokenNames)
    for (const m of new Set([...x.text.matchAll(/(--pui-[a-z0-9-]+)\s*:/g)].map((m) => m[1])))
      if (!tokenNames.has(m))
        add(
          "warn",
          x,
          x.text.indexOf(m),
          `${m} is not a Beamline token (it changes nothing)`,
          "see the token list in the capabilities",
        );
}

// Installed Beamline packages nothing imports (looks, foundation and runtime are setup, not elements).
const pkg = JSON.parse(fs.readFileSync(path.join(app, "package.json"), "utf8"));
const usedPackages = new Set([...imported.values()].map((p) => p.split("/").slice(0, 2).join("/")));
const setupOnly = /^@beamline\/(foundation|runtime|look-|font-|effect-|ui$)/;
const unused = Object.keys(pkg.dependencies ?? {})
  .filter((name) => name.startsWith("@beamline/") && !setupOnly.test(name) && !usedPackages.has(name))
  .map((name) => name.slice(10));
// the remover is the installer the app came from: the hosted service's CLI, or the kit's tools/beamline.mjs
const installedFrom = (() => {
  try {
    const url = JSON.parse(fs.readFileSync(path.join(app, ".beamline/package-selection.json"), "utf8")).acceptedRelease
      ?.url;
    return /^https?:/.test(url ?? "") ? new URL(url).origin : null;
  } catch {
    return null;
  }
})();
const remover = installedFrom ? `npx --yes ${installedFrom}/cli.tgz remove` : "node <kit>/tools/beamline.mjs remove";
for (const id of unused)
  findings.push({
    level: "warn",
    at: "package.json",
    what: `@beamline/${id} installed but never imported`,
    fix: `use it, or remove every unused one at once: ${remover} ${unused.join(" ")}`,
  });

// Another kit's parts beside Beamline in one screen (t18: Haiku framed Beamline fields in shadcn's Card and listed the
// briefs in shadcn's Table): Beamline's look never reaches them, so the screen keeps the old template's face.
const KIT_EQUIVALENT = {
  card: "card",
  table: "table or data-grid",
  button: "button",
  input: "input",
  select: "select or segmented-control",
  checkbox: "checkbox",
  textarea: "textarea",
  dialog: "dialog",
  badge: "badge",
  tabs: "tabs",
  switch: "switch",
  "radio-group": "radio-group",
  tooltip: "tooltip",
  popover: "popover",
  sheet: "sheet",
  "dropdown-menu": "dropdown-menu",
  avatar: "avatar",
  separator: "separator",
  skeleton: "skeleton",
  slider: "slider",
  progress: "progress",
  calendar: "calendar",
};
const OTHER_KITS = /^(@mui\/material|@chakra-ui\/react|@mantine\/core|antd|@radix-ui\/themes|react-bootstrap)$/;
for (const x of code) {
  if (!/from\s+["']@beamline\//.test(x.text)) continue;
  for (const m of x.text.matchAll(/import\s+[^;]*?from\s+["']([^"']+)["']/g)) {
    const ui = /(?:^|\/)components\/ui\/([a-z-]+)$/.exec(m[1])?.[1];
    if (ui && KIT_EQUIVALENT[ui])
      add(
        "fix",
        x,
        m.index,
        `another kit's ${ui} (${m[1]}) beside Beamline in this screen: Beamline's look does not reach it`,
        `use @beamline/${KIT_EQUIVALENT[ui].split(" or ").join(" or @beamline/")}`,
      );
    else if (OTHER_KITS.test(m[1]))
      add(
        "fix",
        x,
        m.index,
        `${m[1]} parts beside Beamline in this screen: Beamline's look does not reach them`,
        "use the Beamline element for each (see the capabilities)",
      );
  }
}

// Tailwind classes with no Tailwind stylesheet (t17: Haiku removed `@import "tailwindcss"` from the app's global CSS
// and kept 117 utility classes in its pages; none of them did anything, so the layout fell apart).
const allDeps = { ...pkg.dependencies, ...pkg.devDependencies };
if (allDeps.tailwindcss || allDeps["@tailwindcss/postcss"] || allDeps["@tailwindcss/vite"]) {
  const UTILITY =
    /\b(?:p|px|py|pt|pb|pl|pr|m|mx|my|mt|mb|gap|space-[xy]|max-w|min-h|grid-cols|col-span|rounded|items|justify)-[a-z0-9./[\]-]+/g;
  const uses = code
    .map((x) => ({
      x,
      n: [...x.text.matchAll(/className=(?:"([^"]*)"|\{`([^`]*)`\})/g)].reduce(
        (sum, m) => sum + ((m[1] ?? m[2]).match(UTILITY) ?? []).length,
        0,
      ),
    }))
    .filter((u) => u.n)
    .sort((a, b) => b.n - a.n);
  const total = uses.reduce((sum, u) => sum + u.n, 0);
  const imported = files.some(
    (x) => x.f.endsWith(".css") && /@import\s+["']tailwindcss["']|@tailwind\s+(base|utilities)/.test(x.text),
  );
  if (total >= 10 && !imported)
    add(
      "fix",
      uses[0].x,
      uses[0].x.text.indexOf("className="),
      `${total} Tailwind classes (p-8, gap-4 …) and no stylesheet imports Tailwind: none of them apply`,
      'put `@import "tailwindcss";` back at the top of the app\'s global CSS (Beamline sits beside Tailwind, not instead of it)',
    );
}

// A look installed and loaded that no code shows (t21: after a restyle to Metal, Soft's stylesheet still loaded):
// every look ships its whole stylesheet. An app that switches looks imports each one it offers.
for (const name of Object.keys(pkg.dependencies ?? {}).filter((n) => n.startsWith("@beamline/look-"))) {
  const id = name.slice("@beamline/look-".length);
  const shown =
    [...imported.values()].includes(name) || code.some((x) => new RegExp(`\\blook=["']${id}["']`).test(x.text));
  if (!shown)
    findings.push({
      level: "fix",
      at: "package.json",
      what: `${name} is installed and its stylesheet loaded, but no UIRoot shows it`,
      fix: `remove it: ${remover.replace(/ remove$/, "")} remove --preset ${id}`,
    });
}

const beamlineMd = ["BEAMLINE.md", "docs/BEAMLINE.md"].map((f) => path.join(app, f)).find((f) => fs.existsSync(f));
if (!beamlineMd)
  findings.push({
    level: "fix",
    at: "BEAMLINE.md",
    what: "no BEAMLINE.md",
    fix: "write the field → element → why table (see the capabilities guide)",
  });
else if (!/\|.*\|.*\|/.test(fs.readFileSync(beamlineMd, "utf8")))
  findings.push({
    level: "fix",
    at: path.relative(app, beamlineMd),
    what: "BEAMLINE.md has no mapping table",
    fix: "add field → element → why rows",
  });

const roots = code.flatMap((x) =>
  [...x.text.matchAll(/<UIRoot\b/g)].map((m) => ({ x, tag: tagAt(x.text, m.index), i: m.index })),
);
if (!roots.length && imported.size)
  findings.push({
    level: "fix",
    at: "app",
    what: "no UIRoot",
    fix: "wrap the app (or each Beamline region) in UIRoot",
  });
// something must paint the page in the look: a UIRoot set to dark inside a body the browser paints white leaves dark
// theme's light text on white (t20: Haiku's page title and step list were white on white)
const pagePainted =
  roots.some((r) => /look-page/.test(r.tag)) ||
  files.some(
    (x) => /background(-color)?\s*:\s*var\(--pui-(page|canvas)/.test(x.text) && /\b(body|html|:root)\b/.test(x.text),
  ) ||
  code.some((x) => /\bbg-(page|canvas)\b/.test(x.text));
// (a UIRoot set to the light theme on a page the browser paints white is fine; the default theme is dark)
const darkRoots = roots.filter((r) => !/\btheme=["']light["']/.test(r.tag));
if (darkRoots.length && !pagePainted)
  add(
    "fix",
    darkRoots[0].x,
    darkRoots[0].i,
    "nothing paints the page in the look: the theme's text can sit on the browser's white",
    'give the app\'s UIRoot className="look-page" and style={{ minHeight: "100dvh" }}',
  );
// the look UIRoot shows must be installed: a missing look's stylesheet leaves switches, tracks and field frames
// unpainted, with no error (the installer adds only the looks asked for; UIRoot without a preset shows Soft)
const looks = Object.keys(pkg.dependencies ?? {})
  .filter((name) => name.startsWith("@beamline/look-"))
  .map((name) => name.slice("@beamline/look-".length));
for (const r of roots) {
  if (/\{\s*\.\.\./.test(r.tag) || !looks.length) continue;
  const presetName = /\bpreset=\{\s*([A-Za-z_$][\w$]*)\s*\}/.exec(r.tag)?.[1];
  const lookProp = /\blook=["'{]\s*["']?([a-z]+)/.exec(r.tag)?.[1];
  const from = presetName && imported.get(presetName);
  const shown = from?.startsWith("@beamline/look-")
    ? from.slice("@beamline/look-".length)
    : presetName
      ? null
      : (lookProp ?? "prism");
  if (shown && !looks.includes(shown))
    add(
      "fix",
      r.x,
      r.i,
      `UIRoot shows the ${shown} look, which is not installed: controls render without paint`,
      `pass the installed look: preset={${looks[0]}} with import { ${looks[0]} } from "@beamline/look-${looks[0]}"` +
        (looks.length > 1 ? ` (installed: ${looks.join(", ")})` : ""),
    );
}
for (const r of roots)
  if (!/\b(brand|preset|look)=|\{\s*\.\.\./.test(r.tag))
    add(
      "warn",
      r.x,
      r.i,
      "UIRoot without a chosen look or brand",
      "choose preset/look and brand for the product's character",
    );

const elements = [...imported.keys()].filter((n) => /^[A-Z]/.test(n));
const summary = {
  app,
  beamlineElements: elements.length,
  fix: findings.filter((f) => f.level === "fix").length,
  warn: findings.filter((f) => f.level === "warn").length,
};
if (asJson) console.log(JSON.stringify({ summary, findings }, null, 2));
else {
  console.log(`Beamline audit — ${app}`);
  console.log(`${elements.length} Beamline exports imported: ${elements.sort().join(", ") || "none"}`);
  for (const f of findings) console.log(`${f.level === "fix" ? "FIX " : "warn"} ${f.at}  ${f.what} → ${f.fix}`);
  console.log(findings.length ? `${summary.fix} to fix, ${summary.warn} warnings.` : "No fallbacks found.");
}
process.exit(summary.fix ? 1 : 0);
