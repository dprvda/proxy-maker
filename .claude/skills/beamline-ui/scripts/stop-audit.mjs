#!/usr/bin/env node
// REASON: agents end their turn without the audit. Trial t14 (Haiku): it installed DataGrid, Tabs and Sheet, then wrote
// <table>, <select> and <button> by hand and stopped with 9 FIX the audit would have named. Claude Code runs this on
// Stop (connect writes the hook into .claude/settings.json): FIX findings in files this session changed send the agent
// back once with the exact lines and the element for each. It never loops (stop_hook_active), and any failure lets the
// stop through: no Beamline in the app, no node, an audit that cannot run. Five or more packages installed in this
// session and never used send it back too (t15), with the one line that removes them. Other warnings are a count.
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const pass = () => process.exit(0);
let input = "";
process.stdin.on("data", (chunk) => (input += chunk));
process.stdin.on("end", () => {
  try {
    run(JSON.parse(input || "{}"));
  } catch {
    pass();
  }
});

function run(hook) {
  if (hook.stop_hook_active) pass();
  const app = process.env.CLAUDE_PROJECT_DIR || hook.cwd || process.cwd();
  const pkg = JSON.parse(fs.readFileSync(path.join(app, "package.json"), "utf8"));
  const deps = { ...pkg.dependencies, ...pkg.devDependencies };
  // this script lives in the app's Beamline skill, so the app is connected; an agent that installed nothing from it
  // is checked too (t22: Haiku called the MCP tool as a shell command, gave up on Beamline and was never audited)
  const hasBeamline = Object.keys(deps).some((name) => name.startsWith("@beamline/"));

  const audit = path.join(path.dirname(fileURLToPath(import.meta.url)), "audit.mjs");
  let report;
  try {
    report = JSON.parse(
      execFileSync(process.execPath, [audit, "--app", app, "--json"], { encoding: "utf8", timeout: 60_000 }),
    );
  } catch (error) {
    // the audit exits 1 when a FIX remains; its JSON is still on stdout
    report = JSON.parse(error.stdout || "null");
  }
  if (!report) pass();

  // only what this session touched: modified, staged and new files (all of them outside git)
  const changed = (() => {
    const git = (args) =>
      execFileSync("git", args, { cwd: app, encoding: "utf8", timeout: 10_000, stdio: ["ignore", "pipe", "ignore"] })
        .split("\n")
        .filter(Boolean);
    try {
      const files = new Set(git(["ls-files", "--modified", "--others", "--exclude-standard"]));
      // what the session committed counts too (t22: Haiku committed its work before stopping and escaped the audit):
      // diff from the last commit made before the session began, read off the transcript's first timestamp
      let base = "HEAD";
      try {
        const first = fs.readFileSync(hook.transcript_path, "utf8").split("\n", 1)[0];
        const began = JSON.parse(first).timestamp;
        if (began)
          base = git(["rev-list", "-1", `--before=${began}`, "HEAD"])[0] ?? "4b825dc642cb6eb9a060e54bf8d69288fbee4904";
      } catch {
        // no transcript to read: what is uncommitted is what changed
      }
      try {
        for (const f of git(["diff", "--name-only", "--relative", base])) files.add(f);
      } catch {
        // a repository with no commit yet: every file is new and already listed
      }
      return files;
    } catch {
      return null;
    }
  })();
  // a finding in code counts when its file changed; one about the app (BEAMLINE.md, UIRoot) when any UI file changed
  const uiTouched = !changed || [...changed].some((f) => /\.(tsx|jsx|css)$/.test(f));
  const fixes = report.findings.filter(
    (f) => f.level === "fix" && (/:\d+$/.test(f.at) ? !changed || changed.has(f.at.split(":")[0]) : uiTouched),
  );
  // packages this session installed and never used (t15: Haiku installed 42, used 22): five or more send it back too,
  // to remove what the screens do not need in one line (the audit's own remove command), not to cram them in
  const unused = report.findings.filter((f) => /installed but never imported/.test(f.what));
  const installedHere = !changed || changed.has("package.json");
  const tooMany = installedHere && unused.length >= 5;
  // a screen this session changed and built only from another kit's parts while Beamline is installed (t24: Haiku
  // rebuilt the home with shadcn's Card, Table and buttons outside the app's UIRoot — white page, invisible title)
  if (hasBeamline && changed)
    for (const file of changed) {
      if (!/\.(tsx|jsx)$/.test(file) || /(^|\/)components\/ui\//.test(file) || !fs.existsSync(path.join(app, file)))
        continue;
      const text = fs.readFileSync(path.join(app, file), "utf8");
      const other = [
        ...text.matchAll(
          /from\s+["']([^"']*components\/ui\/[a-z-]+|@mui\/material|@chakra-ui\/react|@mantine\/core|antd)["']/g,
        ),
      ];
      if (other.length && !/from\s+["']@beamline\//.test(text))
        fixes.push({
          at: `${file}:${text.slice(0, other[0].index).split("\n").length}`,
          what: `this screen was built only from another kit's parts (${other.map((m) => m[1].split("/").pop()).join(", ")})`,
          fix: "build it from Beamline elements like the other screens (card, data-grid or table, metric-card, button), inside the app's UIRoot",
        });
    }
  if (!hasBeamline) {
    if (!uiTouched || !changed) pass();
    process.stderr.write(
      [
        'This app is connected to Beamline (the MCP server "beamline" and the beamline-ui skill), but you changed its',
        "screens without installing anything from it. Call the beamline_capabilities tool — an MCP tool, not a shell",
        "command — map every field and pattern to its element in BEAMLINE.md, install them with beamline_install, and",
        "rebuild these screens with them.",
        ...fixes.slice(0, 12).map((f) => `- ${f.at}  ${f.what} → ${f.fix}`),
      ].join("\n"),
    );
    process.exit(2);
  }
  if (!fixes.length && !tooMany) pass();

  const remove = tooMany ? (unused[0].fix.split("remove every unused one at once: ")[1] ?? "") : "";
  const warnings = report.findings.filter((f) => f.level !== "fix" && !unused.includes(f)).length;
  process.stderr.write(
    [
      fixes.length
        ? `Beamline audit: ${fixes.length} to fix in what you built. Fix each with the element it names, then finish:`
        : "",
      ...fixes.map((f) => `- ${f.at}  ${f.what} → ${f.fix}`),
      tooMany
        ? `${unused.length} Beamline packages are installed and never used: ${unused.map((f) => f.what.split(" ")[0].slice(10)).join(", ")}. ` +
          `Remove what the screens do not need in one line: ${remove} — keep one only where a screen should use it, and use it there.`
        : "",
      warnings ? `(${warnings} warning(s) too: run node ${path.relative(app, audit)} --app . to see them.)` : "",
    ]
      .filter(Boolean)
      .join("\n"),
  );
  process.exit(2);
}
