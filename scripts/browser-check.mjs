import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { spawn, execFileSync } from 'node:child_process'
import { chromium } from '/opt/browser/node_modules/playwright-core/index.mjs'

const evidence = process.env.EVIDENCE_DIR ?? '/evidence'
fs.mkdirSync(evidence, { recursive: true })
const data = fs.mkdtempSync(path.join(evidence, 'synthetic-data-'))
const env = { ...process.env, PYTHONPATH: '/workspace/backend', RECORDINGS_DATA: data,
  RECORDINGS_STATIC: '/workspace/web/dist', RECORDINGS_WORKER_TOKEN: 'synthetic-browser-only' }
execFileSync('python', ['scripts/seed_browser.py', data], { env, stdio: 'inherit', timeout: 30000 })
const server = spawn('python', ['-m', 'uvicorn', 'argon_recordings.api:create_app', '--factory', '--host', '127.0.0.1', '--port', '8765'], { env, stdio: ['ignore', 'pipe', 'pipe'] })
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
  await page.getByRole('button', { name: 'New project', exact: true }).click()
  await page.getByLabel('Project name', { exact: true }).fill('Browser-created project')
  await page.getByRole('button', { name: 'Create project', exact: true }).click()
  await page.getByRole('heading', { name: 'Browser-created project', exact: true }).waitFor()
  await page.getByRole('button', { name: 'Upload recording', exact: true }).click()
  await page.locator('input[type=file]').setInputFiles({ name: 'synthetic-upload.wav', mimeType: 'audio/wav', buffer: Buffer.from('synthetic upload protocol payload') })
  await page.getByLabel('Recording title', { exact: true }).fill('Browser upload')
  await page.screenshot({ path: path.join(evidence, 'upload.png'), fullPage: true })
  await page.getByRole('dialog').getByRole('button', { name: 'Upload recording', exact: true }).click()
  await page.getByRole('heading', { name: 'Your recording is in good hands.', exact: true }).waitFor()
  result.checks.push('live project creation, raw upload progress and durable queue UI')
  assert.deepEqual(result.console_errors, [])
  fs.writeFileSync(path.join(evidence, 'result.json'), JSON.stringify(result, null, 2))
  console.log(JSON.stringify(result, null, 2))
} finally {
  if (browser) await browser.close()
  server.kill('SIGTERM')
  await new Promise(resolve => { if (server.exitCode !== null) resolve(); else server.once('exit', resolve) })
  log.end()
}
