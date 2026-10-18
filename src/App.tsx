import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { UIRoot } from '@beamline/runtime'
import { prism } from '@beamline/look-prism'
import { PageHeader, type PageHeaderAction } from '@beamline/page-header'
import { Card } from '@beamline/card'
import { Input } from '@beamline/input'
import { Button } from '@beamline/button'
import { SegmentedControl } from '@beamline/segmented-control'
import { RadioGroup } from '@beamline/radio-group'
import { NumberField } from '@beamline/number-field'
import { Switch } from '@beamline/switch'
import { Checkbox } from '@beamline/checkbox'
import { MetricCard } from '@beamline/metric-card'
import { Progress } from '@beamline/progress'
import { DataGrid, type DataGridColumn } from '@beamline/data-grid'
import { Badge, type BadgeTone } from '@beamline/badge'
import { LogViewer, type LogLine } from '@beamline/log-viewer'
import { CopyButton } from '@beamline/copy-button'
import { Alert } from '@beamline/alert'
import { EmptyState } from '@beamline/empty-state'
import { ToastStack, ToastStackProvider, useToastStack } from '@beamline/toast'
import { Folder, FolderOpen, FolderSearch, Play, RefreshCw, Square } from 'lucide-react'
import {
  api,
  clock,
  fmtDur,
  human,
  type Codec,
  type EngineState,
  type FileRow,
  type FileStatus,
  type Fps,
  type Height,
  type LogEntry,
  type Settings,
} from './api'

const STATUS: Record<FileStatus, { label: string; tone: BadgeTone }> = {
  pending: { label: 'Not converted', tone: 'neutral' },
  checking: { label: 'Checking', tone: 'info' },
  waiting: { label: 'Waiting', tone: 'neutral' },
  converting: { label: 'Converting', tone: 'accent' },
  converting_fallback: { label: 'Converting (ProRes)', tone: 'accent' },
  finishing: { label: 'Finishing', tone: 'info' },
  done: { label: 'Done', tone: 'success' },
  failed: { label: 'Failed', tone: 'danger' },
  no_video: { label: 'No video, skipped', tone: 'warning' },
  stopped: { label: 'Stopped', tone: 'warning' },
}

function detail(f: FileRow) {
  if (f.status === 'converting' || f.status === 'converting_fallback') {
    const parts = [f.speed, f.eta != null ? `~${fmtDur(f.eta)} left` : 'measuring…'].filter(Boolean)
    return parts.join(' · ')
  }
  if (f.status === 'failed') return f.error
  if (f.status === 'done') return f.out
  if (f.status === 'finishing') return 'Stitching chunks and copying audio'
  if (f.status === 'waiting') return 'In the queue'
  return ''
}

const COLUMNS: DataGridColumn<FileRow>[] = [
  { id: 'name', header: 'File', sortable: true, minWidth: 220, grow: true },
  { id: 'size', header: 'Size', type: 'number', sortable: true, width: 110, align: 'end', format: (v) => human(Number(v)) },
  {
    id: 'status',
    header: 'Status',
    width: 190,
    sortable: true,
    value: (f) => STATUS[f.status]?.label ?? f.status,
    cell: (f) => {
      const s = STATUS[f.status] ?? { label: f.status, tone: 'neutral' as const }
      return (
        <Badge tone={s.tone} dot size="sm">
          {s.label}
        </Badge>
      )
    },
  },
  {
    id: 'pct',
    header: 'Progress',
    width: 200,
    sortable: true,
    cell: (f) => (
      <Progress
        className="row-progress"
        size="sm"
        value={f.pct}
        tone={f.status === 'failed' ? 'danger' : f.status === 'done' ? 'success' : 'accent'}
        aria-label={`${f.name}: ${f.pct}%`}
      />
    ),
  },
  {
    id: 'detail',
    header: 'Details',
    width: 260,
    value: detail,
    cell: (f) => (
      <span className="cell-truncate detail" title={detail(f)}>
        {detail(f)}
      </span>
    ),
  },
]

const QUALITY: { value: Height; label: string }[] = [
  { value: '360', label: '360p' },
  { value: '540', label: '540p' },
  { value: '720', label: '720p' },
  { value: 'Original', label: 'Original' },
]

const CODECS: { value: Codec; label: string; description: string }[] = [
  { value: 'prores', label: 'ProRes Proxy', description: 'Recommended. Works in every editor.' },
  { value: 'lt', label: 'ProRes 422 LT', description: 'Higher quality, larger files.' },
  { value: 'dnxhr', label: 'DNxHR LB', description: 'Smallest. Falls back to ProRes on rare problem frames.' },
  { value: 'h264', label: 'H.264', description: 'About 20× smaller, plays on phones and the web. Encoded on the GPU when GPU is on.' },
]

const FPS: { value: Fps; label: string }[] = [
  { value: 'keep', label: 'Keep original' },
  { value: '30', label: '30 fps' },
  { value: '15', label: '15 fps' },
]

function levelOf(message: string): LogLine['level'] {
  if (message.startsWith('FAILED') || message.startsWith('pool error')) return 'error'
  if (message.includes('switching') || message.startsWith('Stopped') || message.includes('failed')) return 'warn'
  return 'info'
}

function useNow(ms: number) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms)
    return () => clearInterval(t)
  }, [ms])
  return now
}

function Screen() {
  const { toast } = useToastStack()
  const [state, setState] = useState<EngineState | null>(null)
  const [draft, setDraft] = useState<Settings | null>(null)
  const [logs, setLogs] = useState<LogEntry[]>([])
  const [offline, setOffline] = useState(false)
  const [browsing, setBrowsing] = useState(false)
  const [scanning, setScanning] = useState(false)
  const [helpText, setHelpText] = useState('')
  const since = useRef(0)
  const finishedSeen = useRef<number | null | undefined>(undefined)
  const scanTimer = useRef<number | undefined>(undefined)
  const now = useNow(1000)

  const apply = useCallback((s: EngineState) => {
    setState(s)
    setDraft((d) => (d === null || s.running ? s.settings : d))
    setLogs((prev) => {
      if (s.logTotal < prev.length) {
        since.current = 0
        return []
      }
      const fresh = s.log.filter((l) => l.id >= prev.length)
      since.current = prev.length + fresh.length
      return fresh.length ? [...prev, ...fresh] : prev
    })
  }, [])

  // Poll the engine, as the old window refreshed itself every 500 ms.
  useEffect(() => {
    let stop = false
    let t: number | undefined
    const tick = async () => {
      try {
        const s = await api.state(since.current)
        if (stop) return
        setOffline(false)
        apply(s)
      } catch {
        if (!stop) setOffline(true)
      }
      if (!stop) t = window.setTimeout(tick, 500)
    }
    tick()
    return () => {
      stop = true
      clearTimeout(t)
    }
  }, [apply])

  // The "Copy for AI" text is written by the engine (settings, versions, failures, log).
  useEffect(() => {
    let stop = false
    const load = () => api.helpText().then((r) => !stop && setHelpText(r.text)).catch(() => {})
    load()
    const t = setInterval(load, 3000)
    return () => {
      stop = true
      clearInterval(t)
    }
  }, [])

  // A finished run gets a toast (not the one that was already finished when the page opened).
  useEffect(() => {
    if (!state) return
    if (finishedSeen.current === undefined) {
      finishedSeen.current = state.finishedAt
      return
    }
    if (state.finishedAt && state.finishedAt !== finishedSeen.current) {
      finishedSeen.current = state.finishedAt
      const failed = state.summary.failed > 0
      const stopped = state.message.startsWith('Stopped')
      toast({
        type: failed ? 'warning' : stopped ? 'info' : 'success',
        title: failed ? 'Finished with problems' : stopped ? 'Stopped' : 'All proxies are ready',
        description: state.message,
      })
    }
  }, [state, toast])

  const scan = useCallback(
    (next: Settings, delay = 0) => {
      clearTimeout(scanTimer.current)
      scanTimer.current = window.setTimeout(async () => {
        setScanning(true)
        try {
          const r = await api.scan(next)
          apply(r.state)
        } catch {
          setOffline(true)
        } finally {
          setScanning(false)
        }
      }, delay)
    },
    [apply],
  )

  const change = <K extends keyof Settings>(key: K, value: Settings[K], delay = 0) => {
    if (!draft) return
    const next = { ...draft, [key]: value }
    setDraft(next)
    scan(next, delay)
  }

  const browse = async () => {
    if (!draft) return
    setBrowsing(true)
    try {
      const r = await api.browse()
      if (r.error) toast({ type: 'warning', title: 'Folder picker unavailable', description: r.error })
      else if (r.folder) change('folder', r.folder)
    } catch {
      setOffline(true)
    } finally {
      setBrowsing(false)
    }
  }

  // Header actions stay the same objects between refreshes, so a click never lands on a re-rendered button.
  const draftRef = useRef(draft)
  draftRef.current = draft
  const toastRef = useRef(toast)
  toastRef.current = toast

  const start = useCallback(async () => {
    if (!draftRef.current) return
    try {
      const r = await api.start(draftRef.current)
      apply(r.state)
      // Another tab may have started the run already; the page simply shows it running.
      if (r.error && !r.state.running) toastRef.current({ type: 'error', title: 'Could not start', description: r.error })
    } catch {
      setOffline(true)
    }
  }, [apply])

  const stop = useCallback(async () => {
    try {
      await api.stop()
    } catch {
      setOffline(true)
    }
  }, [])

  const openFolder = useCallback(async () => {
    try {
      const r = await api.open()
      if (r.error) toastRef.current({ type: 'error', title: 'Could not open the folder', description: r.error })
    } catch {
      setOffline(true)
    }
  }, [])

  const rescan = useCallback(() => {
    if (draftRef.current) scan(draftRef.current)
  }, [scan])

  const logLines = useMemo<LogLine[]>(
    () => logs.map((l) => ({ id: l.id, time: l.time, message: l.message, level: levelOf(l.message) })),
    [logs],
  )
  const logText = useMemo(
    () => logs.map((l) => `${clock(l.time, true)} ${l.message}`).join('\n') || '(log is empty)',
    [logs],
  )

  const loading = state === null || draft === null
  const running = state?.running ?? false
  const files = state?.files ?? []
  const summary = state?.summary
  const locked = running || loading

  const stopping = state?.stopping ?? false
  const canStart = !loading && !offline && !!state?.tools && !!state?.folderExists && files.length > 0
  const canOpen = !loading && !!state?.folderExists
  const actions = useMemo<PageHeaderAction[]>(
    () => [
      running
        ? { id: 'stop', label: stopping ? 'Stopping…' : 'Stop', icon: <Square />, variant: 'secondary', disabled: stopping, onSelect: stop }
        : { id: 'start', label: 'Start', icon: <Play />, variant: 'accent', disabled: !canStart, onSelect: start },
      { id: 'open', label: 'Open proxies folder', icon: <FolderOpen />, variant: 'secondary', disabled: !canOpen, onSelect: openFolder },
      { id: 'rescan', label: 'Rescan', icon: <RefreshCw />, variant: 'ghost', disabled: locked, onSelect: rescan },
    ],
    [running, stopping, canStart, canOpen, locked, start, stop, openFolder, rescan],
  )

  const folderHint = !state || !draft
    ? undefined
    : running
      ? 'Locked while converting.'
      : !state.folderExists
        ? undefined
        : files.length === 0
          ? 'No video files here yet.'
          : undefined

  const lastSavedAgo = summary?.lastSaved ? Math.max(0, (now - summary.lastSaved) / 1000) : null

  return (
    <main className="page">
      <PageHeader
        title="Proxy Maker"
        description="Light, smooth-playing copies of your footage for Premiere Pro and After Effects."
        status={
          running
            ? { label: state?.stopping ? 'Stopping' : 'Converting', tone: 'info' }
            : { label: offline ? 'Not connected' : 'Ready', tone: offline ? 'danger' : 'neutral' }
        }
        meta={state?.message}
        actions={actions}
      />

      {offline && (
        <Alert
          tone="danger"
          title="Proxy Maker is not running"
          action={
            <Button size="sm" variant="secondary" onClick={() => window.location.reload()}>
              Reload
            </Button>
          }
        >
          Start it again with START.bat (or <code>python make_proxies.py</code>), then reload this page. Finished files
          and saved checkpoints are kept.
        </Alert>
      )}
      {state && !state.tools && (
        <Alert
          tone="danger"
          title="ffmpeg is not installed"
          action={<CopyButton size="sm" value="winget install --id Gyan.FFmpeg -e" label="Copy install command" />}
        >
          Proxy Maker needs ffmpeg and ffprobe. Paste the command into PowerShell, then close and reopen Proxy Maker.
        </Alert>
      )}

      <div className="layout">
        <Card title="Settings" headingLevel={2} className="settings">
          <div className="stack">
            <Input
              label="Video folder"
              value={draft?.folder ?? ''}
              disabled={locked}
              leading={<Folder />}
              loading={scanning && !browsing}
              onValueChange={(v) => change('folder', v, 500)}
              description={folderHint}
              error={state && !running && !state.folderExists ? 'That folder does not exist. Check the path, or use Browse.' : undefined}
              success={state && !running && state.folderExists && files.length > 0 ? `${files.length} video file${files.length === 1 ? '' : 's'} found.` : undefined}
              trailing={
                <Button size="sm" variant="ghost" loading={browsing} disabled={locked} onClick={browse}>
                  Browse…
                </Button>
              }
            />
            <Switch
              label="Include subfolders"
              size="sm"
              checked={draft?.recurse ?? false}
              disabled={locked}
              onCheckedChange={(v: boolean) => change('recurse', v)}
            />
            <SegmentedControl
              label="Quality"
              fill
              options={QUALITY.map((o) => ({ ...o, disabled: locked }))}
              value={draft?.height ?? '540'}
              onValueChange={(v) => change('height', v as Height)}
              description="Short side of the picture: 540p is 960×540, or 540×960 for phone video. Lower plays lighter; Original keeps full resolution."
            />
            <RadioGroup
              label="Codec"
              options={CODECS}
              value={draft?.codec ?? 'prores'}
              disabled={locked}
              onValueChange={(v) => change('codec', v as Codec)}
            />
            <SegmentedControl
              label="Frame rate"
              fill
              options={FPS.map((o) => ({ ...o, disabled: locked }))}
              value={draft?.fps ?? 'keep'}
              onValueChange={(v) => change('fps', v as Fps)}
              description={draft?.fps === 'keep' ? 'Required for Premiere proxies.' : undefined}
            />
            {draft && draft.fps !== 'keep' && (
              <Alert tone="warning" title="Will not attach in Premiere">
                {draft.fps} fps plays lighter in After Effects, but Premiere refuses proxies whose frame rate differs
                from the original.
              </Alert>
            )}
            <NumberField
              label="Convert at once"
              min={1}
              max={8}
              suffix="files"
              value={draft?.parallel ?? 2}
              disabled={locked}
              limitHint
              onValueChange={(v) => v != null && change('parallel', v, 300)}
            />
            <Checkbox
              label="Use GPU"
              description="Reads the originals on the GPU; H.264 is also encoded there (NVIDIA)."
              checked={draft?.hwaccel ?? false}
              disabled={locked}
              onCheckedChange={(v: boolean | 'indeterminate') => change('hwaccel', v === true)}
            />
          </div>
        </Card>

        <div className="main">
          <div className="metrics">
            <MetricCard
              label="Files done"
              loading={loading}
              value={summary ? summary.done : null}
              format={(v) => `${v} of ${summary?.total ?? 0}`}
              context={summary?.failed ? `${summary.failed} failed — see the log` : 'Finished proxies are kept for good'}
            />
            <MetricCard
              label="Estimated finish"
              loading={loading}
              value={running ? summary?.finishSecs ?? null : null}
              format={(v) => fmtDur(v)}
              context={
                running
                  ? summary?.finishAt
                    ? `Around ${clock(summary.finishAt)}`
                    : 'Calculating…'
                  : 'Shown while converting'
              }
            />
            <MetricCard
              label="Last saved"
              loading={loading}
              value={lastSavedAgo}
              format={(v) => `${fmtDur(v)} ago`}
              context={summary?.lastSaved ? `Checkpoint at ${clock(summary.lastSaved, true)}` : 'Nothing saved yet this session'}
            />
          </div>

          <Card title="Files" headingLevel={2}>
            <div className="stack">
              <Progress
                label={running ? 'Overall progress' : 'Overall'}
                value={summary ? summary.overall : null}
                showValue
                tone={summary?.failed ? 'warning' : summary && summary.total > 0 && summary.done === summary.total ? 'success' : 'accent'}
              />
              {state && !state.folderExists ? (
                <EmptyState
                  icon={<FolderSearch />}
                  title="Folder not found"
                  description="Check the path in Settings, or pick the folder with your videos."
                  action={
                    <Button variant="secondary" onClick={browse} loading={browsing}>
                      Browse…
                    </Button>
                  }
                />
              ) : state && files.length === 0 ? (
                <EmptyState
                  icon={<FolderSearch />}
                  title="No videos in this folder"
                  description="Pick the folder that holds your camera files (.mp4, .mov, .mxf, .mts …). If they sit in folders inside it, turn on Include subfolders."
                  action={
                    <Button variant="secondary" onClick={browse} loading={browsing} disabled={locked}>
                      Browse…
                    </Button>
                  }
                />
              ) : (
                <DataGrid
                  label="Video files"
                  data={files}
                  columns={COLUMNS}
                  getRowId={(f) => f.id}
                  loading={loading}
                  selectable={false}
                  toolbar={files.length > 12}
                  height={480}
                />
              )}
            </div>
          </Card>

          <Card title="Log" headingLevel={2}>
            <LogViewer
              aria-label="Proxy Maker log"
              lines={logLines}
              live={running}
              height={300}
              lineNumbers={false}
              defaultWrap
              emptyLabel="Nothing yet. Press Start and each saved checkpoint shows up here."
              actions={
                <>
                  <CopyButton size="sm" variant="ghost" value={logText} label="Copy log" copiedLabel="Log copied" />
                  <CopyButton
                    size="sm"
                    variant="secondary"
                    value={helpText || logText}
                    label="Copy for AI"
                    copiedLabel="Copied for AI"
                    onCopied={() =>
                      toast({
                        type: 'info',
                        title: 'Paste it into ChatGPT or Claude',
                        description: 'Open ChatGPT or Claude, click the message box, press Ctrl+V, then Enter. It already holds everything needed to help.',
                        duration: 10000,
                      })
                    }
                  />
                </>
              }
            />
          </Card>
        </div>
      </div>
    </main>
  )
}

export default function App() {
  return (
    <UIRoot preset={prism} className="look-page" style={{ minHeight: '100dvh' }}>
      <ToastStackProvider>
        <Screen />
        <ToastStack position="bottom-right" />
      </ToastStackProvider>
    </UIRoot>
  )
}
