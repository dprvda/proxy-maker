// The page talks to make_proxies.py on the same origin (or through Vite's /api proxy in dev).

export type Height = '360' | '540' | '720' | 'Original'
export type Codec = 'prores' | 'lt' | 'dnxhr' | 'h264'
export type Fps = 'keep' | '30' | '15'

export interface Settings {
  folder: string
  height: Height
  codec: Codec
  fps: Fps
  parallel: number
  recurse: boolean
  hwaccel: boolean
}

export type FileStatus =
  | 'pending'
  | 'checking'
  | 'waiting'
  | 'converting'
  | 'converting_fallback'
  | 'finishing'
  | 'done'
  | 'failed'
  | 'no_video'
  | 'stopped'

export interface FileRow {
  id: string
  name: string
  size: number
  status: FileStatus
  pct: number
  speed: string
  eta: number | null
  error: string
  out: string
}

export interface LogEntry {
  id: number
  time: number
  message: string
}

export interface EngineState {
  running: boolean
  stopping: boolean
  tools: boolean
  message: string
  settings: Settings
  folderExists: boolean
  files: FileRow[]
  summary: {
    total: number
    done: number
    failed: number
    overall: number
    finishSecs: number | null
    finishAt: number | null
    lastSaved: number | null
  }
  finishedAt: number | null
  log: LogEntry[]
  logTotal: number
}

async function call<T>(path: string, body?: unknown): Promise<T> {
  const res = await fetch(`api/${path}`, body === undefined ? undefined : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`)
  return res.json() as Promise<T>
}

type WithState = { error: string | null; state: EngineState }

export const api = {
  state: (since: number) => call<EngineState>(`state?since=${since}`),
  helpText: () => call<{ text: string }>('help-text'),
  scan: (s: Settings) => call<WithState>('scan', s),
  start: (s: Settings) => call<WithState>('start', s),
  stop: () => call<{ error: string | null }>('stop', {}),
  browse: () => call<{ folder: string | null; error: string | null }>('browse', {}),
  open: () => call<{ error: string | null }>('open', {}),
}

export function human(n: number) {
  let v = n
  for (const u of ['B', 'KB', 'MB', 'GB', 'TB']) {
    if (v < 1024) return u === 'B' || u === 'KB' ? `${v.toFixed(0)} ${u}` : `${v.toFixed(1)} ${u}`
    v /= 1024
  }
  return `${v.toFixed(1)} PB`
}

export function fmtDur(secs: number) {
  const s = Math.max(0, Math.floor(secs))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (h) return `${h}h ${m}m`
  if (m) return `${m}m ${s % 60}s`
  return `${s % 60}s`
}

export function clock(ms: number, seconds = false) {
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', ...(seconds ? { second: '2-digit' } : {}) })
}
