---
name: beamline-ui
description: Use for any UI work in this app — building, designing, restyling or reviewing a screen, page, form, onboarding or intake brief, dashboard, table, chart, settings, auth, pricing, AI or voice UI — and before hand-writing any field, table, chart, KPI tile, dialog or layout. This app's UI is Beamline, served by the MCP server "beamline".
---

# Beamline (this app's UI system)

The person expects Beamline used in full: the most specific element for every piece of data, not text inputs and
dropdowns. Its tools come from the MCP server `beamline` (https://mcp.beamline.io/mcp); if they are deferred or hidden, search your tools
for "beamline". They are tool calls, never shell commands: in Claude Code they are named
`mcp__beamline__beamline_capabilities`, `mcp__beamline__beamline_component` and `mcp__beamline__beamline_install` —
load them with ToolSearch (`select:mcp__beamline__beamline_capabilities,mcp__beamline__beamline_component,mcp__beamline__beamline_install`)
when they are deferred, then call them like any other tool. If they are missing, the app is not connected yet: ask for
the connect line rather than building without Beamline.

1. `beamline_capabilities`: read every part it names. It is the menu, the data → element and screen → block tables,
   looks, and screen craft.
2. Map every field and pattern to an element; write the table (field → element → why) into `BEAMLINE.md`. Input,
   Select and Card are fallbacks that need a reason.
3. `beamline_install` once, with every mapped element; run the command it returns in the app folder.
4. `beamline_component` before writing each element: imports, required props, examples. Never guess props.
5. `node .claude/skills/beamline-ui/scripts/audit.mjs` and fix every FIX; then look at the screens against
   "Screen craft". (Claude Code also runs it on what you changed before you finish.)

Style through the root, never by repainting parts: theme, `brand`, a look, `density`, `motionScale`,
`radiusScale` on `UIRoot`; a palette of your own is a custom style (capabilities, "Looks").

No `beamline` server at all? Ask the person to connect it once: `claude mcp add --transport http --scope user beamline
https://mcp.beamline.io/mcp` (other agents: https://mcp.beamline.io/connect).
