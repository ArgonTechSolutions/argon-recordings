import assert from 'node:assert/strict'
import dns from 'node:dns'
dns.setDefaultResultOrder('ipv4first')
import fs from 'node:fs'
import path from 'node:path'
import { spawn, execFileSync } from 'node:child_process'
import { chromium } from '/opt/browser/node_modules/playwright-core/index.mjs'
const evidence = process.env.EVIDENCE_DIR ?? '/evidence'
fs.mkdirSync(evidence,{recursive:true})
const data = fs.mkdtempSync(path.join(evidence,'accounts-data-'))
const env = {...process.env,PYTHONPATH:'/workspace/backend',RECORDINGS_DATA:data,RECORDINGS_AUTH_MODE:'accounts',RECORDINGS_STATIC:'/workspace/web/dist',RECORDINGS_WORKER_TOKEN:'synthetic-browser-only'}
const enrollmentCode = execFileSync('python',['-c',`from pathlib import Path
from argon_recordings.accounts import Accounts
from argon_recordings.store import Store
manager = Accounts(Store(Path(${JSON.stringify(data)})), 'accounts')
manager.bootstrap('owner@example.com','Owner','correct horse battery staple')
from argon_recordings.passkeys import enrollment_ticket
print(enrollment_ticket(manager, 'owner@example.com'))`],{env,encoding:'utf8'}).trim()
const server = spawn('python',['-m','uvicorn','argon_recordings.api:create_app','--factory','--host','127.0.0.1','--port','8766','--timeout-graceful-shutdown','3'],{env,stdio:['ignore','pipe','pipe']})
const log = fs.createWriteStream(path.join(evidence,'server.log')); server.stdout.pipe(log);server.stderr.pipe(log)
const base = 'http://localhost:8766'
let browser
const errors=[]
try {
  for(let i=0;;i++){try{if((await fetch(base+'/api/health')).ok)break}catch{} if(i>80)throw Error('Startup timed out'); await new Promise(r=>setTimeout(r,100))}
  browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox']})
  const owner=await browser.newContext({viewport:{width:1440,height:1000}})
  const page=await owner.newPage();page.on('pageerror',error=>errors.push(error.message))
  const cdp=await owner.newCDPSession(page)
  await cdp.send('WebAuthn.enable')
  await cdp.send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'internal',hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}})
  const signIn=async(page,email)=>{await page.getByLabel('Email',{exact:true}).fill(email);await page.getByLabel('Password',{exact:true}).fill('correct horse battery staple');await page.getByRole('button',{name:'Sign in',exact:true}).click()}
  await page.goto(base)
  await page.getByRole('heading',{name:'Sign in',exact:true}).waitFor()
  await signIn(page,'owner@example.com')
  await page.getByRole('button',{name:'Sign out',exact:true}).waitFor()
  const appSession=await(await owner.request.get(base+'/api/auth/session')).json()
  const project=await(await owner.request.post(base+'/api/projects',{headers:{'X-CSRF-Token':appSession.csrf},data:{name:'Private team project'}})).json()
  assert.ok(project.id)
  await page.goto(base+'/admin/')
  await page.getByRole('heading',{name:'Instance administration',exact:true}).waitFor()
  await signIn(page,'owner@example.com')
  await page.getByRole('heading',{name:'Set up administrator security',exact:true}).waitFor()
  assert.equal((await owner.request.get(base+'/api/admin/users')).status(),403)
  await page.setViewportSize({width:390,height:844})
  await page.getByLabel('Enrollment code',{exact:true}).fill(enrollmentCode)
  await page.getByLabel('Passkey name',{exact:true}).fill('Test laptop')
  await page.getByRole('button',{name:'Create passkey',exact:true}).click()
  await page.getByRole('heading',{name:'Save your recovery codes',exact:true}).waitFor()
  const recoveryCodes=await page.locator('.recovery-codes code').allTextContents()
  assert.equal(recoveryCodes.length,8)
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
  // Do not capture recovery codes in screenshots or logs.
  await page.getByRole('button',{name:'I saved my codes',exact:true}).click()
  await page.getByRole('button',{name:'Sign out',exact:true}).click()
  await signIn(page,'owner@example.com')
  await page.getByRole('button',{name:'Continue with passkey',exact:true}).click()
  await page.getByLabel('Invitation email').waitFor()
  await page.getByRole('button',{name:'Sign out',exact:true}).click()
  await signIn(page,'owner@example.com')
  await page.getByRole('button',{name:'Use a recovery code',exact:true}).click()
  await page.getByLabel('Recovery code',{exact:true}).fill(recoveryCodes[0])
  await page.getByRole('button',{name:'Verify recovery code',exact:true}).click()
  await page.getByLabel('Invitation email').waitFor()
  await page.setViewportSize({width:1440,height:1000})
  await page.getByLabel('Invitation email').fill('guest@example.com')
  await page.getByRole('button',{name:'Create invitation',exact:true}).click()
  const link=await page.getByLabel('One-time link').inputValue()
  assert.ok(link.includes('#invite='))
  await page.screenshot({path:path.join(evidence,'admin-desktop.png'),fullPage:true})
  const guest=await browser.newContext({viewport:{width:390,height:844}})
  const guestPage=await guest.newPage();guestPage.on('pageerror',error=>errors.push(error.message))
  await guestPage.goto(link)
  await guestPage.getByLabel('Name',{exact:true}).fill('Guest')
  await guestPage.getByLabel('Password',{exact:true}).fill('correct horse battery staple')
  await guestPage.getByRole('button',{name:'Create account',exact:true}).click()
  await guestPage.getByRole('button',{name:'Open navigation',exact:true}).click()
  await guestPage.getByRole('button',{name:'Sign out',exact:true}).waitFor()
  await guestPage.getByRole('button',{name:'Close navigation',exact:true}).click()
  assert.equal(await guestPage.getByText('Private team project',{exact:true}).count(),0)
  assert.equal((await guest.request.get(base+'/api/admin/users')).status(),401)
  await page.goto(base)
  await page.getByRole('button',{name:'Project members',exact:true}).click()
  await page.getByLabel('Account email',{exact:true}).fill('guest@example.com')
  await page.getByRole('button',{name:'Add or update member',exact:true}).click()
  await page.getByText('guest@example.com · viewer',{exact:true}).waitFor()
  await guestPage.reload()
  await guestPage.getByRole('heading',{name:'Private team project',exact:true}).waitFor()
  assert.ok(await guestPage.getByRole('button',{name:'Upload recording',exact:true}).isDisabled())
  const layout=await guestPage.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}))
  assert.ok(layout.scroll<=layout.width,JSON.stringify(layout))
  await guestPage.screenshot({path:path.join(evidence,'viewer-mobile.png'),fullPage:true})
  const admin=(await(await owner.request.get(base+'/api/auth/session?audience=admin')).json())
  const adminHeaders={'X-CSRF-Token':admin.csrf,'X-Session-Audience':'admin'}
  const users=await(await owner.request.get(base+'/api/admin/users',{headers:adminHeaders})).json()
  const uid=users.find(user=>user.email==='guest@example.com').id
  assert.equal((await owner.request.patch(base+`/api/admin/users/${uid}`,{headers:adminHeaders,data:{admin:false,disabled:true}})).status(),200)
  await guestPage.getByRole('heading',{name:'Sign in',exact:true}).waitFor()
  assert.equal(await guestPage.getByText('Private team project',{exact:true}).count(),0)
  await page.goto(base+'/admin/')
  await page.getByRole('combobox',{name:'Registration policy',exact:true}).click()
  await page.getByRole('option',{name:'Closed',exact:true}).click()
  await page.waitForFunction(()=>[...document.querySelectorAll('button')].some(button=>button.textContent==='Create invitation'&&button.disabled))
  await page.getByRole('button',{name:'Sign out',exact:true}).click()
  await page.getByLabel('Email',{exact:true}).waitFor()
  assert.deepEqual(errors,[])
  console.log(JSON.stringify({checks:['password-only admin blocked','terminal-authorized passkey enrollment','mobile recovery-code acknowledgment','real Chromium WebAuthn verification','one-time recovery sign-in','separate app/admin login','one-time invitation acceptance','private project isolation','owner membership UI','mobile viewer permissions','SSE suspension clears cached project data without reload','registration policy controls','admin logout'],console_errors:errors},null,2))
} finally {
  if(browser)await browser.close()
  server.kill('SIGTERM')
  await new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve)})
  log.end()
}
