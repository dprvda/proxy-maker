<!-- beamline:start -->
## UI: Beamline

This app's UI is built with Beamline: React elements for every kind of field, tables, charts, overlays, whole screens,
AI, voice and trading pieces. Use it in full; never hand-build a control Beamline has. It is served by the MCP server
`beamline` (https://mcp.beamline.io/mcp). If your MCP tools are deferred or hidden, search your tools for "beamline" first. They are
tool calls, never shell commands (in Claude Code: `mcp__beamline__beamline_capabilities`, `…_component`,
`…_install`; load deferred ones with ToolSearch, then call them as tools).

Before designing or changing any screen:
1. Call `beamline_capabilities` and read every part it names (part 2, 3…).
2. Map every field and pattern to the most specific element and keep the table (field → element → why) in
   `BEAMLINE.md`. Input, Select and Card are fallbacks that need a reason.
3. Call `beamline_install` once with every mapped element and run the command it returns here.
4. Call `beamline_component` for imports, required props and examples before writing each element.
5. After building run `node .claude/skills/beamline-ui/scripts/audit.mjs` and fix every FIX; then check the screens
   against "Screen craft" in the capabilities.
<!-- beamline:end -->
