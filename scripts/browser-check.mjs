import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { spawn, execFileSync } from 'node:child_process'
import { chromium } from '/opt/browser/node_modules/playwright-core/index.mjs'

const evidence = process.env.EVIDENCE_DIR ?? '/evidence'
fs.mkdirSync(evidence, { recursive: true })
const data = fs.mkdtempSync(path.join(evidence, 'synthetic-data-'))
const env = { ...process.env, PYTHONPATH: '/workspace/backend', RECORDINGS_DATA: data,
  RECORDINGS_STATIC: '/workspace/web/dist', RECORDINGS_WORKER_TOKEN: 'synthetic-browser-only', RECORDINGS_MAX_UPLOAD_MB: '1' }
execFileSync('python', ['scripts/seed_browser.py', data], { env, stdio: 'inherit', timeout: 30000 })
const server = spawn('python', ['-m', 'uvicorn', 'argon_recordings.api:create_app', '--factory', '--host', '127.0.0.1', '--port', '8765', '--timeout-graceful-shutdown', '5'], { env, stdio: ['ignore', 'pipe', 'pipe'] })
const log = fs.createWriteStream(path.join(evidence, 'server.log'))
server.stdout.pipe(log); server.stderr.pipe(log)
const base = 'http://127.0.0.1:8765'
let browser
const result = { checks: [], console_errors: [] }
try {
  for (let i = 0; ; i++) {
    try { if ((await fetch(base + '/api/health')).ok) break } catch {}
    if (i > 60) throw new Error('Synthetic app server did not start')
    await new Promise(r => setTimeout(r, 100))
  }
  browser = await chromium.launch({ executablePath: '/usr/bin/chromium', headless: true, args: ['--no-sandbox'] })
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, colorScheme: 'light', acceptDownloads: true })
  const page = await context.newPage()
  page.on('pageerror', error => result.console_errors.push(error.message))
  let recordingListReads = 0
  page.on('request', request => {
    if (request.method() === 'GET' && /\/api\/projects\/[^/]+\/recordings$/.test(request.url())) recordingListReads++
  })
  const fits = async () => {
    const dimensions = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }))
    assert.ok(dimensions.scroll <= dimensions.width, JSON.stringify(dimensions))
  }
  await page.goto(base + '/?preview=1')
  await page.locator('.passage').first().waitFor()
  assert.equal(await page.locator('.passage').count(), 7)
  await fits()
  await page.screenshot({ path: path.join(evidence, 'desktop.png'), fullPage: true })
  await page.locator('.passage').nth(3).click()
  assert.equal(Number(await page.getByLabel('Recording position').inputValue()), 67)
  assert.equal(await page.getByText('Private workspace', { exact: true }).count(), 0)
  assert.equal(await page.getByText('Less scrubbing. More understanding.', { exact: true }).count(), 0)
  assert.equal(await page.locator('.eyebrow').count(), 0)
  assert.equal(await page.locator('.argon-credit a').getAttribute('href'), 'https://www.argon.com.pe')
  await page.getByRole('combobox', { name: 'Search mode', exact: true }).click()
  await page.getByRole('option', { name: 'Exact words', exact: true }).click()
  await page.getByLabel('Search project transcripts').fill('permissions')
  await page.locator('.search-hit').first().waitFor()
  await page.locator('.search-hit').first().click()
  await page.locator('.passage.is-current').waitFor()
  result.checks.push('preview desktop, passage navigation, keyword search')
  await page.setViewportSize({ width: 390, height: 844 })
  await fits()
  await page.screenshot({ path: path.join(evidence, 'mobile.png'), fullPage: true })
  await page.getByRole('button', { name: 'Open navigation' }).click()
  await page.getByRole('button', { name: 'Dark appearance' }).click()
  await page.getByRole('button', { name: 'Close navigation', exact: true }).click()
  await page.screenshot({ path: path.join(evidence, 'mobile-dark.png'), fullPage: true })
  await fits()
  result.checks.push('mobile layout, navigation drawer, dark appearance')
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto(base + '/')
  await page.locator('.recording-card').filter({ hasText: 'Synthetic demo' }).click()
  await page.locator('.passage').nth(1).waitFor()
  await page.locator('audio').waitFor({ state: 'attached' })
  await page.waitForFunction(() => document.querySelector('audio')?.readyState >= 1)
  await page.locator('.passage').nth(1).click()
  assert.ok(Math.abs(await page.locator('audio').evaluate(a => a.currentTime) - 10) < .2)
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download transcript' }).click()
  const downloaded = await download
  await downloaded.saveAs(path.join(evidence, 'downloaded-transcript.txt'))
  assert.ok(fs.readFileSync(path.join(evidence, 'downloaded-transcript.txt'), 'utf8').includes('Workspace permissions'))
  await page.getByLabel('Search project transcripts').fill('permissions')
  await page.locator('.search-hit').first().waitFor()
  assert.ok((await page.locator('.search-hit').first().innerText()).includes('Synthetic demo'))
  await page.locator('.search-hit').first().click()
  result.checks.push('live API transcript, real audio seeking, download and SQLite keyword search')
  assert.equal(await page.getByText('Your recordings, your space.', { exact: true }).count(), 0)
  await page.getByRole('button', { name: 'Correct text', exact: true }).click()
  await page.getByRole('combobox', { name: 'Passage', exact: true }).click()
  await page.getByRole('option', { name: /0:10 · Workspace permissions/ }).click()
  await page.getByLabel('Misheard text', { exact: true }).fill('permissions')
  await page.getByLabel('Correct word or hint', { exact: true }).fill('access permissions')
  await page.getByRole('button', { name: 'Find matching passages', exact: true }).click()
  await page.locator('.correction-candidates input').first().waitFor()
  assert.equal(await page.locator('.correction-candidates input:checked').count(), 1)
  await page.setViewportSize({ width: 390, height: 844 })
  await fits()
  await page.screenshot({ path: path.join(evidence, 'correction-mobile.png'), fullPage: true })
  await page.getByRole('button', { name: 'Retranscribe selected (1)', exact: true }).click()
  await page.locator('.correction-review').first().waitFor()
  const auth = { Authorization: 'Bearer synthetic-browser-only' }
  const correction = await (await page.request.post(base + '/api/worker/corrections/claim', { headers: auth })).json()
  assert.ok(correction.id)
  const publication = await page.request.post(base + `/api/worker/corrections/${correction.id}/complete`, {
    headers: { ...auth, 'X-Lease-Id': correction.lease_id },
    data: { segments: [{ start: 10, end: 20, text: 'Workspace access permissions are visible before an invitation is sent.' }] }
  })
  assert.equal(publication.status(), 200)
  await page.getByRole('button', { name: 'Accept', exact: true }).click()
  await page.locator('.passage').filter({ hasText: 'Workspace access permissions' }).waitFor()
  await fits()
  await page.screenshot({ path: path.join(evidence, 'correction-accepted.png'), fullPage: true })
  result.checks.push('compact mobile correction dialog, candidate selection, queued retry, proposal and acceptance')
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.getByRole('button', { name: 'New project', exact: true }).click()
  await page.getByLabel('Project name', { exact: true }).fill('Browser-created project')
  await page.getByRole('button', { name: 'Create project', exact: true }).click()
  await page.getByRole('heading', { name: 'Browser-created project', exact: true }).waitFor()
  await page.getByRole('button', { name: 'Upload recording', exact: true }).click()
  assert.ok(await page.getByText('Video or audio · up to 1.0 MB', { exact: true }).isVisible())
  await page.locator('input[type=file]').setInputFiles({ name: 'oversized.wav', mimeType: 'audio/wav', buffer: Buffer.alloc(1024 * 1024 + 1) })
  await page.getByRole('dialog').getByRole('button', { name: 'Upload recording', exact: true }).click()
  await page.getByRole('alert').filter({ hasText: 'Choose a file up to 1.0 MB' }).waitFor()
  await page.getByRole('combobox', { name: 'Spoken language', exact: true }).click()
  await page.getByRole('option', { name: 'Spanish', exact: true }).click()
  await page.locator('input[type=file]').setInputFiles({ name: 'synthetic-upload.wav', mimeType: 'audio/wav', buffer: Buffer.from('synthetic upload protocol payload') })
  await page.getByLabel('Recording title', { exact: true }).fill('Browser upload')
  await page.screenshot({ path: path.join(evidence, 'upload.png'), fullPage: true })
  await page.getByRole('dialog').getByRole('button', { name: 'Upload recording', exact: true }).click()
  await page.getByRole('heading', { name: 'Recording queued', exact: true }).waitFor()
  result.checks.push('live project creation, raw upload progress and durable queue UI')
  const failedJob = await (await page.request.post(base + '/api/worker/claim', { headers: auth, data: { worker: 'synthetic-retry' } })).json()
  assert.equal(failedJob.recording.title, 'Browser upload')
  const progressHeaders = { ...auth, 'X-Lease-Id': failedJob.lease_id }
  const readsBeforeProgress = recordingListReads
  const progressUrl = base + `/api/worker/jobs/${failedJob.job_id}/heartbeat`
  assert.equal((await page.request.post(progressUrl, {
    headers: progressHeaders,
    data: { progress: .34, stage: 'Transcribing with Turbo', transcription: {
      audio_processed_seconds: 180, audio_total_seconds: 600, passages: 25, elapsed_seconds: 420, eta_seconds: 980
    } }
  })).status(), 200)
  await page.getByRole('heading', { name: 'Processing recording', exact: true }).waitFor()
  await page.getByText('Audio covered: 3:00 / 10:00', { exact: true }).waitFor()
  await page.getByText(/25 passages · 7:0[0-9] elapsed/).waitFor()
  const elapsedBefore = await page.locator('.transcription-progress p').nth(1).innerText()
  await page.waitForFunction(previous => document.querySelectorAll('.transcription-progress p')[1]?.textContent !== previous, elapsedBefore)
  await page.clock.install()
  await page.clock.fastForward(22000)
  await page.getByText(/Last update \d+ seconds ago · Waiting for worker/).waitFor()
  assert.equal(await page.getByText(/About \d+ min remaining for transcription/).count(), 0)
  await page.clock.setSystemTime(Date.now())
  await page.clock.runFor(1000)
  result.checks.push('elapsed ticks locally; stale update age is shown and ETA is hidden')
  await page.getByText('About 17 min remaining for transcription', { exact: true }).waitFor()
  await page.waitForTimeout(5500)
  assert.equal(recordingListReads, readsBeforeProgress, 'Progress must arrive via SSE without recording-list polling')
  // Simulate tab visibility transitions: hidden tabs release streams; visible tabs resnapshot.
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: true })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  assert.equal((await page.request.post(progressUrl, {
    headers: progressHeaders, data: { progress: .5, stage: 'Transcribing with Turbo', transcription: {
      audio_processed_seconds: 300, audio_total_seconds: 600, passages: 40, elapsed_seconds: 600, eta_seconds: 600
    } }
  })).status(), 200)
  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, value: false })
    document.dispatchEvent(new Event('visibilitychange'))
  })
  await page.getByText('Audio covered: 5:00 / 10:00', { exact: true }).waitFor()
  await page.getByText('About 10 min remaining for transcription', { exact: true }).waitFor()
  assert.equal(recordingListReads, readsBeforeProgress, 'Reconnect uses an SSE snapshot')
  result.checks.push('SSE progress without recording-list polling; hidden tab disconnect and reconnect resynchronization')

  await page.setViewportSize({ width: 390, height: 844 })
  await fits()
  await page.screenshot({ path: path.join(evidence, 'transcription-progress-mobile.png'), fullPage: true })
  assert.equal((await page.request.post(progressUrl, {
    headers: progressHeaders, data: { progress: .95, stage: 'Publishing transcript and references' }
  })).status(), 200)
  await page.getByText('Publishing transcript and references', { exact: true }).waitFor()
  assert.equal(await page.locator('.transcription-progress').count(), 0)
  result.checks.push('live transcription coverage, passage count, elapsed time and ETA; publication clears details; mobile fits')

  assert.equal((await page.request.post(base + `/api/worker/jobs/${failedJob.job_id}/fail`, {
    headers: { ...auth, 'X-Lease-Id': failedJob.lease_id }, data: { message: 'Previous duration restriction' }
  })).status(), 200)
  await page.getByRole('heading', { name: 'Processing failed', exact: true }).waitFor()
  const retrySpacing = await page.getByRole('button', { name: 'Retry recording', exact: true }).evaluate(button => {
    const previous = button.previousElementSibling
    return { margin: parseFloat(getComputedStyle(button).marginTop), gap: button.getBoundingClientRect().top - previous.getBoundingClientRect().bottom }
  })
  assert.ok(retrySpacing.margin >= 20 && retrySpacing.gap >= 20, JSON.stringify(retrySpacing))
  await page.screenshot({ path: path.join(evidence, 'retry-spacing.png'), fullPage: true })
  await page.getByRole('button', { name: 'Retry recording', exact: true }).click()
  await page.getByRole('heading', { name: 'Recording queued', exact: true }).waitFor()
  result.checks.push('configured upload limit displayed/enforced; Retry button has 20px top margin; retained source retry works')

  assert.deepEqual(result.console_errors, [])
  fs.writeFileSync(path.join(evidence, 'result.json'), JSON.stringify(result, null, 2))
  console.log(JSON.stringify(result, null, 2))
} finally {
  if (browser) await browser.close()
  server.kill('SIGTERM')
  await new Promise(resolve => { if (server.exitCode !== null) resolve(); else server.once('exit', resolve) })
  log.end()
}
