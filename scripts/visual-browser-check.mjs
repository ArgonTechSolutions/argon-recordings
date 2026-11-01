import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import {createHash} from 'node:crypto'
import {pathToFileURL} from 'node:url'
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
 await page.getByRole('button',{name:'Visual tools',exact:true}).click()
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
 await page.getByRole('dialog').getByLabel('Recording position',{exact:true}).fill('3')
 await page.getByRole('dialog').getByLabel('Recording position',{exact:true}).dispatchEvent('change')
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
 const projects=await(await page.request.get(base+'/api/projects')).json()
 const recordings=await(await page.request.get(base+`/api/projects/${projects[0].id}/recordings`)).json()
 const rid=recordings[0].id
 const peer=await context.newPage();peer.on('pageerror',e=>result.console_errors.push(e.message))
 await peer.goto(base+`/?project=${projects[0].id}&recording=${rid}`)
 await peer.getByRole('button',{name:'Visual tools',exact:true}).click()
 const analysis=page.locator('.visual-analysis'),other=peer.locator('.visual-analysis')

 const runAnalysis=()=>execFileSync('python',['-c',
  "import httpx; from types import SimpleNamespace; from argon_recordings.analysis_worker import run; client=httpx.Client(base_url='http://127.0.0.1:8765',headers={'Authorization':'Bearer synthetic-browser-only'},trust_env=False); assert run(client,SimpleNamespace(model=None)); client.close()"],{env,stdio:'inherit',timeout:30000})
 await analysis.getByLabel('Analysis end',{exact:true}).fill('6')
 await analysis.getByRole('button',{name:'Analyze visuals',exact:true}).click()
 await analysis.getByText('Queued',{exact:true}).waitFor();runAnalysis()
 await analysis.getByRole('button',{name:'Keep mark',exact:true}).first().waitFor()
 await other.getByRole('button',{name:'Keep mark',exact:true}).first().waitFor()
 await analysis.getByRole('button',{name:'Keep mark',exact:true}).first().click()
 await analysis.getByText('Marked',{exact:true}).first().waitFor()
 await other.getByText('Marked',{exact:true}).first().waitFor()
 const marks=page.getByRole('region',{name:'Timeline marks',exact:true})
 assert.equal((await(await page.request.get(base+`/api/recordings/${rid}/marks`)).json()).marks.length,1)
 await page.keyboard.press('Escape')
 await page.getByRole('button',{name:'Timeline marks',exact:true}).click()
 await marks.getByRole('button',{name:'Undo',exact:true}).click()
 await page.keyboard.press('Escape')
 await page.getByRole('button',{name:'Visual tools',exact:true}).click()
 await analysis.getByText('Mark removed',{exact:true}).first().waitFor()
 await other.getByText('Mark removed',{exact:true}).first().waitFor()
 await analysis.getByLabel('Analysis start',{exact:true}).fill('6')
 await analysis.getByLabel('Analysis end',{exact:true}).fill('8')
 await analysis.getByRole('button',{name:'Analyze visuals',exact:true}).click()
 await analysis.getByText('Queued',{exact:true}).waitFor();runAnalysis()
 await analysis.getByRole('button',{name:'Reject',exact:true}).first().click()
 await analysis.getByText('Rejected',{exact:true}).first().waitFor()
 await other.getByText('Rejected',{exact:true}).first().waitFor()
 for(const width of [768,390,320]) {
  await page.setViewportSize({width,height:900})
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
 }
 await page.screenshot({path:path.join(evidence,'analysis-mobile.png'),fullPage:true})
 await analysis.getByLabel('Analysis start',{exact:true}).fill('4')
 await analysis.getByLabel('Analysis end',{exact:true}).fill('5')
 await analysis.getByRole('button',{name:'Analyze visuals',exact:true}).click()
 await analysis.getByRole('button',{name:'Cancel analysis',exact:true}).click()
 await analysis.getByRole('button',{name:'Cancel analysis',exact:true}).waitFor({state:'detached'})
 await peer.close()
 result.checks.push('real adaptive worker results, cross-tab acceptance/rejection/undo, mobile suggestions, cancellation')
 const state=await(await page.request.get(base+`/api/recordings/${rid}/evidence`)).json()
 const transcript=await(await page.request.get(base+`/api/recordings/${rid}/transcript`)).json()
 const first=transcript.segments[0]
 const imageBytes=await(await page.request.get(base+`/api/recordings/${rid}/media/frame-0000.jpg`)).body()
 const title='Report <script>window.reportPwned=1</script>'
 const reportDraft={title,generator:{name:'Browser fixture',version:'1.0'},report:{
  reference:{recording_id:rid,source_sha256:state.source_sha256,interval:{start:0,end:state.duration},versions:state.versions},
  scope:{start:0,end:state.duration},language:'en',coverage:'complete',limitations:[],
  citations:[{id:'source',kind:'transcript',evidence_id:first.id,interval:{start:first.start,end:first.end}},
   {id:'screen',kind:'image',evidence_id:'frame:frame-0000.jpg',interval:{start:0,end:0}}],
  items:[{id:'claim',kind:'summary',basis:'explicit',text:'<img src="https://evil.invalid/x" onerror="window.reportPwned=1"> [unsafe](https://evil.invalid)',citation_ids:['source','screen']}]},
  inspected_images:[{id:'frame:frame-0000.jpg',sha256:createHash('sha256').update(imageBytes).digest('hex'),inspected:true}]}
 const reports=page.locator('.recording-reports');await page.keyboard.press('Escape')
 const reportPeer=await context.newPage();reportPeer.on('pageerror',e=>result.console_errors.push(e.message))
 await reportPeer.goto(base+`/?project=${projects[0].id}&recording=${rid}`)
 const peerReports=reportPeer.locator('.recording-reports')
 await reports.getByLabel('Import report draft',{exact:true}).setInputFiles({name:'report-draft.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(reportDraft))})
 await reports.getByRole('button',{name:'Save draft',exact:true}).click()
 await reports.locator('article').getByText('Draft',{exact:true}).waitFor()
 await peerReports.getByRole('button',{name:title+' · Draft',exact:true}).waitFor()
 await reports.getByRole('button',{name:'Mark reviewed',exact:true}).click()
 await reports.locator('article').getByText('Reviewed',{exact:true}).waitFor()
 await peerReports.getByRole('button',{name:title+' · Reviewed',exact:true}).waitFor()
 assert.equal(await page.evaluate(()=>window.reportPwned??null),null)
 const downloadReady=page.waitForEvent('download')
 await reports.getByRole('link',{name:'Export ZIP',exact:true}).click()
 const download=await downloadReady,zipPath=path.join(evidence,'report.zip'),unpacked=path.join(evidence,'unpacked-report')
 await download.saveAs(zipPath)
 execFileSync('python',['-c',
  "import hashlib,json,pathlib,sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); names=z.namelist(); assert all(not pathlib.PurePosixPath(n).is_absolute() and '..' not in pathlib.PurePosixPath(n).parts for n in names); m=json.loads(z.read('manifest.json')); assert m['report']['status']=='reviewed'; assert {e['path'] for e in m['files']}==set(names)-{'manifest.json'}; assert all(hashlib.sha256(z.read(e['path'])).hexdigest()==e['sha256'] for e in m['files']); z.extractall(sys.argv[2])",zipPath,unpacked],{env,timeout:15000})
 const offline=await browser.newPage(),unexpected=[]
 offline.on('pageerror',e=>result.console_errors.push(e.message))
 offline.on('request',request=>{if(/^https?:/.test(request.url()))unexpected.push(request.url())})
 await offline.goto(pathToFileURL(path.join(unpacked,'report.html')).href)
 await offline.locator('img').waitFor()
 assert.ok(await offline.locator('img').evaluate(img=>img.complete&&img.naturalWidth>0))
 assert.equal(await offline.evaluate(()=>window.reportPwned??null),null)
 assert.deepEqual(unexpected,[])
 await offline.screenshot({path:path.join(evidence,'offline-report.png'),fullPage:true})
 await offline.locator('a[href="transcript.html#s0"]').click()
 assert.ok(await offline.locator('#s0').textContent())
 await offline.close()
 for(const width of [768,390,320]) {
  await page.setViewportSize({width,height:900})
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
 }
 await page.screenshot({path:path.join(evidence,'reports-mobile.png'),fullPage:true})
 const latest=await(await page.request.get(base+`/api/recordings/${rid}/evidence`)).json()
 assert.equal((await page.request.post(base+`/api/recordings/${rid}/marks`,{data:{expected_revision:latest.versions.annotations,
  expected_source_sha256:latest.source_sha256,mark:{interval:{start:0,end:0},label:'Manual after report'}}})).status(),201)
 await reports.getByText('Reviewed · Source evidence changed',{exact:true}).waitFor()
 await peerReports.getByRole('button',{name:title+' · Reviewed · Stale',exact:true}).waitFor()
 await reports.getByLabel('Import report draft',{exact:true}).setInputFiles({name:'report-draft.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(reportDraft))})
 await reports.getByRole('button',{name:'Save draft',exact:true}).click()
 await reports.getByRole('alert').filter({hasText:'Recording evidence changed'}).waitFor()
 assert.equal((await(await page.request.get(base+`/api/recordings/${rid}/reports`)).json()).reports.length,1)
 await reportPeer.close()
 result.checks.push('explicit report import/review, two-tab status/staleness, bounded ZIP/checksums, offline HTML/images/citations, hostile text, mobile report controls')
 assert.deepEqual(result.console_errors,[]);result.passed=true
}catch(error){result.passed=false;result.error=error.stack;throw error}
finally{fs.writeFileSync(path.join(evidence,'result.json'),JSON.stringify(result,null,2));await browser?.close();server.kill('SIGTERM');await new Promise(resolve=>{server.once('exit',resolve);setTimeout(()=>{server.kill('SIGKILL');resolve()},6000)});log.end()}
console.log(JSON.stringify(result,null,2))
