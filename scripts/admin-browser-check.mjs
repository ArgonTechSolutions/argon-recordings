// Bounded UI smoke with synthetic API responses; backend authorization is tested separately.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? '/opt/browser/node_modules/playwright-core/index.mjs')
const root=process.env.WEB_DIST ?? '/workspace/web/dist'
const evidence=process.env.EVIDENCE_DIR ?? '/evidence'
fs.mkdirSync(evidence,{recursive:true})
const server=http.createServer((request,response)=>{
  const url=new URL(request.url,'http://localhost')
  const filename=path.join(root,url.pathname==='/admin/' ? 'admin/index.html' : url.pathname==='/' ? 'index.html' : url.pathname)
  if(!filename.startsWith(root+'/')){response.writeHead(404).end();return}
  try{response.setHeader('Content-Type',filename.endsWith('.js')?'text/javascript':filename.endsWith('.css')?'text/css':filename.endsWith('.woff2')?'font/woff2':'text/html');response.end(fs.readFileSync(filename))}catch{response.writeHead(404).end()}
})
await new Promise(resolve=>server.listen(8767,'127.0.0.1',resolve))
let browser
const errors=[]
const user={id:'a'.repeat(32),name:'Fixture owner',email:'owner@example.com',admin:1,disabled:0}
let budgets={default_cap_usd:5,users:[{user_id:user.id,period:'2026-11',usage_usd:1.25,pending_usd:0,cap_usd:5,cap_source:'default'}]}
let updates=[]
let confirmations=0
let releaseState="available",fresh=true,checking=false
try{
  browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox']})
  const context=await browser.newContext({viewport:{width:1440,height:1000}})
  const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message))
  await page.route('**/api/**',async route=>{
    const request=route.request();const url=new URL(request.url());let value
    const p=url.pathname
    if(p==='/api/auth/session')value={enabled:true,user,csrf:'synthetic-csrf',oidc:false,mfa_verified:true}
    else if(p==='/api/admin/users')value=[user]
    else if(p==='/api/admin/settings')value={registration:'invite',oidc:null}
    else if(p==='/api/admin/invitations'||p==='/api/admin/audit'||p==='/api/projects')value=[]
    else if(p==='/api/health')value={worker_configured:true,max_upload_bytes:123456,search:'off'}
    else if(p==='/api/admin/ai')value={controls:{enabled:true,report_soft_usd:null,report_hard_usd:null,monthly_soft_usd:10,monthly_hard_usd:null},key_configured:true,key_source:'deployment',month_cost_usd:1.25,reader_model:'fixture',writer_model:'fixture'}
    else if(p.startsWith('/api/admin/ai/budgets')){
      if(request.method()==='PUT'){
        const cap=request.postDataJSON().cap_usd
        if(p.endsWith('/default')){budgets.default_cap_usd=cap;if(budgets.users[0].cap_source==='default')budgets.users[0].cap_usd=cap}
        else {budgets.users[0].cap_usd=cap;budgets.users[0].cap_source='user'}
      }
      if(request.method()==='DELETE'){budgets.users[0].cap_usd=budgets.default_cap_usd;budgets.users[0].cap_source='default'}
      value=budgets
    }
    else if(p==='/api/account/usage')value=budgets.users[0]
    else if(p==='/api/admin/system')value={workers:[{id:'worker-1',online:true,last_contact_seconds:3,diarization_available:true,active_tasks:1},{id:'worker-2',online:false,last_contact_seconds:null,diarization_available:false,active_tasks:0}],hosts:[{target:'app',online:true,source_revision:'b'.repeat(40),check:{state:releaseState,latest_revision:'d'.repeat(40),checked_at:Date.now()/1000},checking,check_fresh:fresh},{target:'worker-1',online:false,source_revision:'c'.repeat(40)}],updates}
    else if(p==='/api/admin/system/check-updates')value={status:'requested'}
    else if(p==='/api/admin/system/updates'){
      assert.equal(request.postDataJSON().confirm,'update-latest-main');confirmations++
      updates=[{id:'d'.repeat(32),target:'app',state:'queued',created:1,updated:1,error:null}];value=updates[0]
    }
    else if(p.startsWith('/api/admin/system/updates/')&&request.method()==='DELETE'){updates[0].state='cancelled';value=updates[0]}
    else if(p==='/api/admin/logs')value={entries:[{id:1,created:1,source:'worker-1',category:'errors',level:'error',event:'task-failed',summary:'Worker reported a task failure.',occurrences:3}],next_before:null}
    else if(p==='/api/auth/agent-grants')value=[]
    else throw Error('Unexpected API request: '+p)
    await route.fulfill({json:value})
  })
  const nav=page.getByRole('navigation',{name:'Administration sections'})
  await page.goto('http://127.0.0.1:8767/admin/')
  await page.getByRole('heading',{name:'Users',exact:true}).waitFor()
  await nav.getByRole('button',{name:'AI & budgets',exact:true}).click()
  await page.getByRole('heading',{name:'Monthly user budgets'}).waitFor()
  const caps=page.getByLabel('Monthly cap in USD',{exact:true})
  await caps.nth(0).fill('8');await page.getByRole('button',{name:'Save cap',exact:true}).click()
  await page.getByText('Cap saved. It applies before the next paid call.').waitFor()
  assert.equal(budgets.default_cap_usd,8)
  await caps.nth(1).fill('0');await page.getByRole('button',{name:'Set override'}).click()
  await page.getByRole('button',{name:'Use default'}).waitFor();assert.equal(budgets.users[0].cap_usd,0)
  await caps.nth(1).fill('');await page.getByRole('button',{name:'Save cap',exact:true}).nth(1).click()
  await page.getByText('User override: Unlimited',{exact:false}).waitFor();assert.equal(budgets.users[0].cap_usd,null)
  await page.getByRole('button',{name:'Use default'}).click();await page.getByRole('button',{name:'Set override'}).waitFor()
  await page.screenshot({path:path.join(evidence,'budgets-desktop.png'),fullPage:true,animations:"disabled"})
  await nav.getByRole('button',{name:'System',exact:true}).click()
  await page.getByRole('heading',{name:'Worker status'}).waitFor()
  assert.equal(await page.getByRole('button',{name:'Update host'}).nth(1).isDisabled(),true)
  await page.getByText('Update available',{exact:true}).waitFor()
  await page.getByRole('button',{name:'Check for updates',exact:true}).click()
  await page.getByText('Check requested. Connected hosts will report shortly.',{exact:true}).waitFor()
  await page.getByRole('button',{name:'Update host'}).nth(0).click()
  await page.getByRole('dialog').waitFor();assert.equal(confirmations,0)
  await page.getByRole('button',{name:'Confirm update',exact:true}).click()
  await page.getByRole('button',{name:'Cancel update',exact:true}).waitFor();assert.equal(confirmations,1)
  await page.getByRole('button',{name:'Cancel update',exact:true}).click()
  await page.getByText('app · cancelled',{exact:true}).waitFor()
  for (const [status,isFresh,isChecking,label] of [['up_to_date',true,false,'Up to date'],['failed',true,false,'Check failed — verify host Git access and installed runtime'],['available',false,false,'Update check expired — check again'],['available',true,true,'Checking…']]) {
    releaseState=status;fresh=isFresh;checking=isChecking
    await page.reload()
    await page.getByText(label,{exact:true}).waitFor()
    assert.equal(await page.getByRole('button',{name:'Update host'}).first().isDisabled(),true)
  }
  releaseState='available';fresh=true;checking=false;await page.reload()
  await page.getByText('Update available',{exact:true}).waitFor()
  await page.setViewportSize({width:390,height:844})
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false)
  assert.equal(await page.getByRole('heading',{name:'Administration',exact:true}).isVisible(),true)
  assert.equal(await page.getByRole('button',{name:'Sign out',exact:true}).count(),0)
  assert.equal(await page.locator('.admin-screen header').evaluate(el=>el.getBoundingClientRect().height),44)
  await page.locator('.admin-screen header').screenshot({path:path.join(evidence,'header-mobile.png'),animations:'disabled'})
  await page.getByRole('button',{name:'Open admin navigation'}).click()
  const drawer=page.getByRole('dialog')
  await drawer.waitFor()
  await drawer.getByText('Fixture owner',{exact:true}).waitFor()
  await drawer.getByRole('button',{name:'Sign out',exact:true}).waitFor()
  await page.screenshot({path:path.join(evidence,'navigation-mobile.png'),fullPage:true,animations:"disabled"})
  await drawer.getByRole('button',{name:'Logs',exact:true}).click()
  await page.getByRole('heading',{name:'Operational logs'}).waitFor()
  await page.getByText('Worker reported a task failure.',{exact:true}).waitFor()
  await page.getByRole('button',{name:'Open admin navigation'}).click()
  await drawer.getByRole('button',{name:'System',exact:true}).click()
  await page.screenshot({path:path.join(evidence,'system-mobile.png'),fullPage:true,animations:"disabled"})
  await page.getByRole('button',{name:'Open admin navigation'}).click()
  await page.screenshot({path:path.join(evidence,'navigation-mobile.png'),fullPage:true,animations:"disabled"})
  await drawer.getByRole('button',{name:'System',exact:true}).click()
  await page.reload();await page.getByRole('heading',{name:'Worker status'}).waitFor()
  await page.goto('http://127.0.0.1:8767/')
  await page.getByRole('button',{name:'Open navigation',exact:true}).click()
  await page.getByRole('button',{name:'Account',exact:true}).click()
  await page.getByRole('heading',{name:'Monthly AI usage'}).waitFor()
  await page.getByText('2026-11 (UTC) · $1.2500 used',{exact:true}).waitFor()
  await page.getByText('Monthly cap: $8.0000 · Instance default',{exact:true}).waitFor()
  await page.screenshot({path:path.join(evidence,'account-usage-mobile.png'),fullPage:true,animations:"disabled"})
  assert.deepEqual(errors,[])
  console.log(JSON.stringify({result:'passed',coverage:['admin navigation and reload','default/zero/unlimited/inherit caps','worker states','update availability/manual check/current/failed/expired/checking gating, confirmation/cancel/offline host','mobile drawer and logs','mobile overflow','account usage'],api:'synthetic'}))
}finally{await browser?.close();await new Promise(resolve=>server.close(resolve))}
