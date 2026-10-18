// End-to-end: drive the real page, crash the server mid-conversion, restart, and check the run resumes
// from its checkpoints and the proxies match their originals (duration, untouched audio, codec).
//
//   npm run build && node e2e/crash_resume.mjs
//
// Needs ffmpeg/ffprobe, python3 and Playwright's Chromium (npx playwright install chromium).
// Footage and evidence go to _apptest/ (git-ignored). Exit code 0 = passed.
import { spawn, execFileSync } from 'node:child_process'
import { mkdirSync, rmSync, writeFileSync, existsSync, readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { chromium } from 'playwright'

const ROOT = resolve(import.meta.dirname, '..')
const PORT = 8798
const URL = `http://127.0.0.1:${PORT}/`
const FOOTAGE = join(ROOT, '_apptest', 'Shoot_Day1')
const OUT = join(ROOT, '_apptest', `e2e-${new Date().toISOString().replace(/[:.]/g, '-')}`)
const CHUNK = 5
const evidence = { started: new Date().toISOString(), steps: [], checks: [] }
const step = (s) => { evidence.steps.push(`${new Date().toISOString()} ${s}`); console.log('·', s) }
const check = (name, ok, detail) => { evidence.checks.push({ name, ok, detail }); console.log(ok ? 'PASS' : 'FAIL', name, detail ?? '') }

const sh = (cmd, args) => execFileSync(cmd, args, { encoding: 'utf8' }).trim()
const probe = (f, entries, stream) =>
  sh('ffprobe', ['-v', 'error', ...(stream ? ['-select_streams', stream] : []), '-show_entries', entries, '-of', 'csv=p=0', f])

function makeFootage() {
  mkdirSync(FOOTAGE, { recursive: true })
  const clip = (name, dur, src, audio) => {
    const f = join(FOOTAGE, name)
    if (existsSync(f)) return
    sh('ffmpeg', ['-v', 'error', '-y', '-f', 'lavfi', '-i', `${src}=duration=${dur}:size=1920x1080:rate=25`,
      '-f', 'lavfi', '-i', `sine=frequency=330:duration=${dur}`, '-c:v', 'libx264', '-preset', 'ultrafast',
      '-c:a', audio, '-shortest', f])
  }
  clip('A001_interview.mov', 32, 'testsrc2', 'pcm_s16le')
  clip('B002_broll.mp4', 12, 'testsrc', 'aac')
  clip('C003_wide.mp4', 12, 'testsrc', 'aac')
}

function startServer() {
  const p = spawn('python3', ['make_proxies.py'], {
    cwd: ROOT,
    env: { ...process.env, PROXY_MAKER_PORT: String(PORT), PROXY_MAKER_CHUNK_SECS: String(CHUNK), PROXY_MAKER_NO_BROWSER: '1' },
    stdio: 'ignore',
  })
  return p
}

async function waitFor(fn, ms, what) {
  const end = Date.now() + ms
  while (Date.now() < end) {
    try { const v = await fn(); if (v) return v } catch { /* not yet */ }
    await new Promise((r) => setTimeout(r, 250))
  }
  throw new Error(`timed out waiting for ${what}`)
}
const state = () => fetch(`${URL}api/state`).then((r) => r.json())

async function main() {
  mkdirSync(OUT, { recursive: true })
  evidence.revision = sh('git', ['-C', ROOT, 'rev-parse', 'HEAD']) + (sh('git', ['-C', ROOT, 'status', '--porcelain']) ? ' (+ uncommitted changes)' : '')
  evidence.env = { ffmpeg: sh('ffmpeg', ['-version']).split('\n')[0], python: sh('python3', ['--version']), node: process.version, chunkSecs: CHUNK }
  makeFootage()
  rmSync(join(FOOTAGE, 'proxies'), { recursive: true, force: true })
  step(`footage ready in ${FOOTAGE} (proxies/ cleared)`)

  let server = startServer()
  await waitFor(state, 10000, 'server')
  const browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1512, height: 982 }, colorScheme: 'dark' })
  try {
    await page.goto(URL)
    const folder = page.getByRole('textbox', { name: 'Video folder' })
    await folder.fill(FOOTAGE)
    await page.getByText('3 video files found.').waitFor({ timeout: 10000 })
    step('typed the folder; page says 3 video files found')
    await page.getByRole('radio', { name: '720p' }).click()
    await page.getByRole('button', { name: 'Start', exact: true }).click()
    step('chose 720p and pressed Start')

    // Let A001 (7 chunks of 5 s) save at least 2 checkpoints, then crash the server hard.
    await waitFor(async () => (await state()).log.some((l) => l.message === 'A001_interview.mov: checkpoint 2/7 saved'), 120000, 'A001 checkpoint 2/7')
    await page.screenshot({ path: join(OUT, '1-converting.png') })
    const before = (await state()).log.map((l) => l.message)
    server.kill('SIGKILL')
    step('A001 saved checkpoint 2/7 -> server killed with SIGKILL (simulated crash)')
    await page.getByText('Proxy Maker is not running').waitFor({ timeout: 10000 })
    await page.screenshot({ path: join(OUT, '2-crashed.png') })
    check('page shows the not-running alert after the crash', true)
    const chunksDir = join(FOOTAGE, 'proxies', '.chunks', 'A001_interview_proxy_720p_prores')
    const savedChunks = readdirSync(chunksDir).filter((f) => f.endsWith('.mov'))
    check('checkpoint chunks survived the crash', savedChunks.length >= 2, savedChunks.join(', '))

    server = startServer()
    await waitFor(state, 10000, 'restarted server')
    await page.reload()
    await folder.fill(FOOTAGE)
    await page.getByText('3 video files found.').waitFor({ timeout: 10000 })
    await page.getByRole('radio', { name: '720p' }).click()
    await page.getByRole('button', { name: 'Start', exact: true }).click()
    step('restarted the server, reloaded, same settings, pressed Start again')

    const done = await waitFor(async () => { const s = await state(); return !s.running && s.finishedAt && s }, 300000, 'run to finish')
    await page.getByText('All proxies are ready').waitFor({ timeout: 10000 })
    await page.screenshot({ path: join(OUT, '3-done.png') })
    step(`run finished: ${done.message}`)

    const after = done.log.map((l) => l.message)
    const resumedChunks = after.filter((m) => m.startsWith('A001_interview.mov: checkpoint'))
    const redone = savedChunks.map((f) => Number(f.slice(1, 5)) + 1).filter((n) => resumedChunks.some((m) => m.includes(`checkpoint ${n}/7`)))
    check('second run did not re-encode chunks saved before the crash', redone.length === 0, `before crash: ${savedChunks.length} chunks; second run saved: ${resumedChunks.join(' | ')}`)
    check('all three files done', done.summary.done === 3 && done.summary.failed === 0, JSON.stringify(done.summary))

    for (const [src, out] of [
      ['A001_interview.mov', 'A001_interview_proxy_720p_prores.mov'],
      ['B002_broll.mp4', 'B002_broll_proxy_720p_prores.mov'],
      ['C003_wide.mp4', 'C003_wide_proxy_720p_prores.mov'],
    ]) {
      const s = join(FOOTAGE, src), o = join(FOOTAGE, 'proxies', out)
      const ds = Number(probe(s, 'format=duration')), dO = Number(probe(o, 'format=duration'))
      check(`${out}: duration matches`, Math.abs(ds - dO) <= Math.max(1.5, ds * 0.02), `${ds}s -> ${dO}s`)
      const v = probe(o, 'stream=codec_name,height', 'v:0')
      check(`${out}: ProRes at 720p`, v === 'prores,720', v)
      const as = probe(s, 'stream=codec_name,sample_rate,channels', 'a:0'), ao = probe(o, 'stream=codec_name,sample_rate,channels', 'a:0')
      check(`${out}: audio copied untouched`, as === ao, `${as} -> ${ao}`)
    }
    check('chunk folder cleaned up after finishing', !existsSync(chunksDir))
    evidence.logBeforeCrash = before
    evidence.logFinal = after
  } finally {
    await browser.close()
    server.kill('SIGTERM')
  }
}

main()
  .catch((e) => { evidence.error = String(e?.stack ?? e); console.error(e) })
  .finally(() => {
    evidence.finished = new Date().toISOString()
    evidence.passed = !evidence.error && evidence.checks.length > 0 && evidence.checks.every((c) => c.ok)
    evidence.rerun = 'npm run build && node e2e/crash_resume.mjs'
    writeFileSync(join(OUT, 'evidence.json'), JSON.stringify(evidence, null, 2))
    console.log(evidence.passed ? 'E2E PASSED' : 'E2E FAILED', '->', OUT)
    process.exit(evidence.passed ? 0 : 1)
  })
