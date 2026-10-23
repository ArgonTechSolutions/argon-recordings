import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import {spawn,execFileSync} from 'node:child_process'
import {chromium} from '/opt/browser/node_modules/playwright-core/index.mjs'

const evidence=process.env.EVIDENCE_DIR??'/evidence'
fs.mkdirSync(evidence,{recursive:true})
const fixture='/workspace/models/validation/visual-browser-fixtures'
const data=fs.mkdtempSync(path.join(evidence,'synthetic-data-'))
const env={...process.env,PYTHONPATH:'/workspace/backend',RECORDINGS_DATA:data,RECORDINGS_STATIC:'/workspace/web/dist',RECORDINGS_WORKER_TOKEN:'synthetic-browser-only'}
execFileSync('python',['scripts/seed_visual_browser.py',data,fixture],{env,stdio:'inherit',timeout:30000})
const server=spawn('python',['-m','uvicorn','argon_recordings.api:create_app','--factory','--host','127.0.0.1','--port','8765','--timeout-graceful-shutdown','5'],{env,stdio:['ignore','pipe','pipe']})
const log=fs.createWriteStream(path.join(evidence,'server.log'));server.stdout.pipe(log);server.stderr.pipe(log)
const base='http://127.0.0.1:8765',auth={Authorization:'Bearer synthetic-browser-only'}
let browser
const result={checks:[],console_errors:[]}
try {
 for(let i=0;;i++) { try{if((await fetch(base+'/api/health')).ok)break}catch{} if(i>60)throw Error('API unavailable'); await new Promise(r=>setTimeout(r,100)) }
 browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox']})
 const context=await browser.newContext({viewport:{width:1440,height:1000}})
 const page=await context.newPage();page.on('pageerror',e=>result.console_errors.push(e.message))
 await page.goto(base);await page.locator('.recording-card').first().click()
 await page.locator('.frame-strip button').nth(19).waitFor()
 assert.equal(await page.locator('.frame-strip button').count(),20)
 await page.getByRole('button',{name:'More frames',exact:true}).click()
 assert.equal(await page.locator('.frame-strip button').count(),26)
 const panel=page.locator('.visual-evidence')
 await panel.locator('summary').click()
 await panel.getByLabel('Evidence start',{exact:true}).fill('0.36')
 await panel.getByRole('button',{name:'Extract',exact:true}).click()
 await panel.getByText('Visual queued',{exact:true}).waitFor()
 const publish=async(name)=>{
  const task=await(await page.request.post(base+'/api/worker/visual/claim',{headers:auth})).json()
  assert.ok(task?.id)
  const headers={...auth,'X-Lease-Id':task.lease_id}
  const manifest=JSON.parse(fs.readFileSync(path.join(fixture,name,'manifest.json')))
  if(typeof manifest.output_display_aspect_ratio==='string') {const [w,h]=manifest.output_display_aspect_ratio.split(':').map(Number);manifest.output_display_aspect_ratio=w/h}
  const media=fs.readFileSync(path.join(fixture,name,manifest.filename))
  assert.equal((await page.request.put(base+`/api/worker/visual/${task.id}/media`,{headers,data:media})).status(),200)
  const response=await page.request.post(base+`/api/worker/visual/${task.id}/complete`,{headers,data:manifest})
  assert.equal(response.status(),200,await response.text())
 }
 await publish('still')
 await panel.getByRole('button',{name:'Still 0:00.4',exact:true}).click()
 await panel.getByRole('button',{name:'Zoom evidence screenshot',exact:true}).click()
 assert.ok(await panel.locator('img.zoomed-evidence').isVisible())
 await panel.getByRole('combobox',{name:'Evidence type',exact:true}).click()
 await page.getByRole('option',{name:'Motion clip',exact:true}).click()
 await panel.getByLabel('Evidence start',{exact:true}).fill('2')
 await panel.getByLabel('Evidence end',{exact:true}).fill('4')
 await panel.getByRole('button',{name:'Extract',exact:true}).click()
 await panel.getByText('Visual queued',{exact:true}).waitFor();await publish('clip')
 await panel.getByRole('button',{name:'Clip 0:02.0',exact:true}).click()
 await page.waitForFunction(()=>document.querySelector('video')?.readyState>=2)
 assert.ok(Math.abs(await page.locator('audio').evaluate(a=>a.currentTime)-2)<.15)
 assert.equal(await panel.locator('video').evaluate(v=>v.muted),true)
 await page.getByRole('button',{name:'Play recording',exact:true}).click()
 await page.waitForFunction(()=>document.querySelector('audio')?.currentTime>2.6)
 const clocks=await page.evaluate(()=>({audio:document.querySelector('audio').currentTime,video:document.querySelector('video').currentTime}))
 assert.ok(Math.abs(clocks.audio-(clocks.video+2))<.4,JSON.stringify(clocks))
 await page.getByRole('button',{name:'Pause recording',exact:true}).click()
 await page.waitForFunction(()=>document.querySelector('video')?.paused)
 await page.getByLabel('Recording position',{exact:true}).fill('3')
 await page.getByLabel('Recording position',{exact:true}).dispatchEvent('change')
 await page.waitForFunction(()=>Math.abs(document.querySelector('video').currentTime-1)<.3)
 await page.getByRole('button',{name:'Change playback speed',exact:true}).click()
 await page.waitForFunction(()=>document.querySelector('video').playbackRate===1.25)
 result.checks.push('all retained frames, extraction queue/SSE publication, screenshot zoom, muted clip source offset/seek/pause/speed')
 for(const width of [768,390,320]) {
  await page.setViewportSize({width,height:900})
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
 }
 await page.screenshot({path:path.join(evidence,'visual-mobile.png'),fullPage:true})
 await panel.getByRole('button',{name:'Close evidence',exact:true}).click()
 assert.equal(await page.locator('video').count(),0)
 await panel.getByRole('combobox',{name:'Evidence type',exact:true}).click();await page.getByRole('option',{name:'Screenshot',exact:true}).click()
 await panel.getByLabel('Evidence start',{exact:true}).fill('5')
 await panel.getByRole('button',{name:'Extract',exact:true}).click();await panel.getByText('Visual queued',{exact:true}).waitFor()
 await panel.getByRole('button',{name:'Cancel',exact:true}).click()
 await panel.getByText('Visual queued',{exact:true}).waitFor({state:'detached'})
 result.checks.push('320/390/768px extraction controls, close clears video, queued extraction cancellation')
 assert.deepEqual(result.console_errors,[]);result.passed=true
}catch(error){result.passed=false;result.error=error.stack;throw error}
finally{fs.writeFileSync(path.join(evidence,'result.json'),JSON.stringify(result,null,2));await browser?.close();server.kill('SIGTERM');await new Promise(resolve=>{server.once('exit',resolve);setTimeout(()=>{server.kill('SIGKILL');resolve()},6000)});log.end()}
console.log(JSON.stringify(result,null,2))
