# Beamline in Proxy Maker

**Screen:** one page, served by `make_proxies.py` on `http://127.0.0.1:8765`. **Users:** video editors and their
non-technical teammates who drop a folder of camera files in, press Start and come back later. Used now and then, for
minutes to hours at a time, so the page must read at a glance from across the desk: what is running, how long is left,
what is safe. **Density:** default (a settings panel of seven choices, one list that is watched rather than edited).

**Look:** Prism, dark, no brand colour (the product has no identity of its own yet). Release 0.2.0-rc.39, compiled
packages. Layout CSS in `src/app.css` uses only Beamline tokens and type roles.

## Field → element → why

| Field / pattern | Element | Why |
|---|---|---|
| Page title, state, main actions (Start / Stop, Open proxies folder, Rescan) | `page-header` | Title + a status badge + one primary action; Start is the only `accent` action, Stop replaces it while running |
| Video folder | `input` + a `button` (Browse…) in its trailing slot | Free text path; the browser cannot hand a real path to the backend, so Browse asks the Python side to open the OS folder dialog and typing/pasting stays possible. Success / error lines say how many videos were found or that the folder is missing |
| Quality (360 / 540 / 720 / Original) | `segmented-control` | Four short options, all visible |
| Codec (3 options, each needs a sentence) | `radio-group` with descriptions | Options must be read; radio-cards would be too heavy for a side panel |
| Frame rate (Keep original / 30 / 15) | `segmented-control` + `alert` (warning) when not "Keep original" | Three short options; the Premiere attach risk must stay visible, not hidden in help |
| Convert at once (1–8) | `number-field` | Exact bounded number with steps |
| Include subfolders | `switch` | Applies at once (rescans the list) |
| GPU decode | `checkbox` | Applies only when the next run starts |
| Files done / Estimated finish / Last saved | `metric-card` ×3 | The three numbers people look at; unmeasured values show hatched, never 0 |
| Overall progress | `progress` | Known progress of the whole run |
| File list (name, size, status, progress, speed / time left) | `data-grid` with `badge` and `progress` cells | A list watched live, can be hundreds of camera files; sortable; loading/empty states built in |
| Per-file status | `badge` | Short status word with tone |
| No videos / folder missing | `empty-state` | Says why the list is empty and offers Browse |
| Log | `log-viewer` | Streams while converting, follows the newest line, searchable; FAILED lines are `error`, codec fallbacks `warn` |
| Copy log / Copy for AI | `copy-button` (in the log's toolbar) | Copies text with visible confirmation; Copy for AI also shows a toast with what to do next |
| ffmpeg missing / server not reachable | `alert` (danger) | Persistent problems that block the job |
| Run finished / stopped / action failed | `toast` | Brief results of an action |
| Grouping (Settings, Files, Log) | `card` | One frame per group; no card inside a card |

## Audit CONSIDERs not taken

- Screen craft "seed example records": not applicable — the list is real files on disk; seeding fake videos would
  mislead and could be converted.
