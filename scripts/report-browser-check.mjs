import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import {spawn, execFileSync} from 'node:child_process'
import {chromium} from '/opt/browser/node_modules/playwright-core/index.mjs'

const evidence=process.env.EVIDENCE_DIR??'/evidence'
fs.mkdirSync(evidence,{recursive:true})
const data=fs.mkdtempSync(path.join(evidence,'synthetic-data-'))
const env={...process.env,PYTHONPATH:'/workspace/backend:/workspace/scripts',RECORDINGS_DATA:data,RECORDINGS_STATIC:'/workspace/web/dist',RECORDINGS_WORKER_TOKEN:'synthetic-browser-only',OPENAI_API_KEY:'synthetic-browser-only'}
execFileSync('python',['scripts/seed_visual_browser.py',data,'/workspace/models/validation/visual-browser-fixtures'],{env,stdio:'inherit',timeout:30000})
const server=spawn('python',['-m','uvicorn','report_browser_app:create_fixture_app','--factory','--host','127.0.0.1','--port','8766','--timeout-graceful-shutdown','5'],{env,stdio:['ignore','pipe','pipe']})
const log=fs.createWriteStream(path.join(evidence,'server.log'));server.stdout.pipe(log);server.stderr.pipe(log)
const base='http://127.0.0.1:8766', result={checks:[],console_errors:[],provider:'synthetic upstream; real API, jobs, evidence, reports and browser'}
let browser
try {
 for(let i=0;;i++){try{if((await fetch(base+'/api/health')).ok)break}catch{}if(i>100)throw Error('API unavailable');await new Promise(r=>setTimeout(r,100))}
 browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox']})
 const context=await browser.newContext({viewport:{width:390,height:844}}),page=await context.newPage()
 page.on('pageerror',e=>result.console_errors.push(e.message))
 await page.goto(base); await page.locator('.recording-card').first().click()
 await page.getByRole('tab',{name:'Reports',exact:true}).click()
 await page.getByRole('heading',{name:'Turn this recording into something useful.'}).waitFor()
 assert.equal(await page.locator('.report-source').getAttribute('open'),null)
 await page.getByRole('button',{name:'Create a report',exact:true}).click()
 await page.getByRole('combobox',{name:'Report type',exact:true}).click()
 await page.getByRole('option',{name:'Visual report',exact:true}).click()
 await page.locator('.report-create textarea').fill('Explain the demonstration and keep relevant images.')
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
 await page.screenshot({path:path.join(evidence,'create-mobile.png'),fullPage:true})
 await page.getByRole('button',{name:'Generate draft',exact:true}).click()
 await page.getByText('Draft',{exact:true}).waitFor({timeout:30000})
 assert.ok(await page.locator('.report-body img').evaluate(img=>img.complete&&img.naturalWidth>0))
 await page.getByRole('button',{name:'View source at 0:00',exact:true}).first().click()
 assert.notEqual(await page.locator('.report-source').getAttribute('open'),null)
 await page.locator('.report-source summary').click()
 await page.getByRole('button',{name:'New',exact:true}).click()
 await page.getByRole('combobox',{name:'Report type',exact:true}).click();await page.getByRole('option',{name:'Action list',exact:true}).click()
 await page.getByRole('button',{name:'Generate draft',exact:true}).click()
 await page.getByRole('heading',{name:'Action list',exact:true}).waitFor({timeout:30000})
 const projects=await(await page.request.get(base+'/api/projects')).json()
 const recordings=await(await page.request.get(base+`/api/projects/${projects[0].id}/recordings`)).json(),rid=recordings[0].id
 assert.equal((await(await page.request.get(base+`/api/recordings/${rid}/reports`)).json()).reports.length,2)
 await page.getByRole('combobox',{name:'Select report',exact:true}).click()
 await page.getByRole('option').filter({hasText:'Report ·'}).click()
 await page.getByRole('heading',{name:'Report',exact:true}).waitFor()
 await page.getByRole('button',{name:'Mark reviewed',exact:true}).click();await page.getByText('Reviewed',{exact:true}).waitFor()
 result.checks.push('mobile creation, real queued generation with synthetic provider, supplied screenshot, citation opens source, multiple documents, selection and explicit review')
 for(const width of [1440,960,768,390,320]) {
  await page.setViewportSize({width,height:900})
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`overflow at ${width}`)
  await page.screenshot({path:path.join(evidence,`report-${width}.png`),fullPage:true})
 }
 await page.setViewportSize({width:1440,height:1000});await page.reload();await page.locator('.recording-card').first().click();await page.getByRole('tab',{name:'Reports',exact:true}).click();await page.locator('.report-body').waitFor()
 assert.notEqual(await page.locator('.report-source').getAttribute('open'),null)
 const boxes=await page.evaluate(()=>({report:document.querySelector('.report-document').getBoundingClientRect().toJSON(),source:document.querySelector('.report-source').getBoundingClientRect().toJSON()}))
 assert.ok(boxes.source.x>boxes.report.x+boxes.report.width)
 await page.screenshot({path:path.join(evidence,'reports-desktop.png'),fullPage:true})
 await page.getByRole('tab',{name:'Reports',exact:true}).focus();await page.keyboard.press('ArrowLeft');await page.getByRole('region',{name:'Transcript',exact:true}).waitFor()
 await page.getByRole('tab',{name:'Transcript',exact:true}).focus();await page.keyboard.press('ArrowRight');await page.locator('.report-body').waitFor()
 result.checks.push('320/390/768/960/1440px no overflow, desktop side source, mobile collapsed source and keyboard tab navigation')
 await page.getByRole('button',{name:'Refine',exact:true}).click()
 await page.locator('.report-create textarea').fill('Make the next steps more concise.')
 await page.getByRole('button',{name:'Generate draft',exact:true}).click()
 await page.getByRole('heading',{name:'Refined report',exact:true}).waitFor({timeout:30000})
 assert.equal((await(await page.request.get(base+`/api/recordings/${rid}/reports`)).json()).reports.length,3)
 result.checks.push('refinement creates a separate cited draft and retains previous documents')
 await page.getByRole('button',{name:'New',exact:true}).click();await page.getByRole('button',{name:'Generate draft',exact:true}).click()
 await page.getByRole('button',{name:'Cancel',exact:true}).click();await page.getByRole('button',{name:'Cancel',exact:true}).waitFor({state:'detached'})
 await page.waitForTimeout(1700)
 assert.equal((await(await page.request.get(base+`/api/recordings/${rid}/reports`)).json()).reports.length,3)
 result.checks.push('cancellation prevents publication through real generation API')
 assert.deepEqual(result.console_errors,[]);result.passed=true
} catch(e){result.passed=false;result.error=e.stack;throw e}
finally {fs.writeFileSync(path.join(evidence,'result.json'),JSON.stringify(result,null,2));await browser?.close();server.kill('SIGTERM');await new Promise(r=>{if(server.exitCode!==null)r();else{server.once('exit',r);setTimeout(()=>{server.kill('SIGKILL');r()},6000)}});log.end()}
console.log(JSON.stringify(result,null,2))
