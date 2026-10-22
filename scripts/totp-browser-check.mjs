import assert from 'node:assert/strict'
import dns from 'node:dns'
import fs from 'node:fs'
import path from 'node:path'
import { spawn, execFileSync } from 'node:child_process'
import { chromium } from '/opt/browser/node_modules/playwright-core/index.mjs'
dns.setDefaultResultOrder('ipv4first')
const evidence=process.env.EVIDENCE_DIR??'/evidence'
fs.mkdirSync(evidence,{recursive:true})
const data=fs.mkdtempSync(path.join(evidence,'totp-data-'))
const env={...process.env,PYTHONPATH:'/workspace/backend',RECORDINGS_DATA:data,RECORDINGS_AUTH_MODE:'accounts',RECORDINGS_ALLOW_TOTP:'true',RECORDINGS_STATIC:'/workspace/web/dist',RECORDINGS_WORKER_TOKEN:'synthetic-only'}
const ticket=execFileSync('python',['-c',`from pathlib import Path
from argon_recordings.accounts import Accounts
from argon_recordings.store import Store
from argon_recordings.passkeys import enrollment_ticket
manager=Accounts(Store(Path(${JSON.stringify(data)})),'accounts',allow_totp=True)
manager.bootstrap('owner@example.com','Owner','correct horse battery staple')
print(enrollment_ticket(manager,'owner@example.com'))`],{env,encoding:'utf8'}).trim()
const server=spawn('python',['-m','uvicorn','argon_recordings.api:create_app','--factory','--host','127.0.0.1','--port','8767','--timeout-graceful-shutdown','3','--no-access-log'],{env,stdio:['ignore','pipe','pipe']})
const log=fs.createWriteStream(path.join(evidence,'server.log'));server.stdout.pipe(log);server.stderr.pipe(log)
const base='http://localhost:8767'
let browser
const errors=[]
const otp=(secret,offset=0)=>execFileSync('python',['-c','import sys,json,time,pyotp; x=json.load(sys.stdin); print(pyotp.TOTP(x["secret"]).at(time.time()+x["offset"]))'],{input:JSON.stringify({secret,offset}),encoding:'utf8'}).trim()
try {
 for(let i=0;;i++){try{if((await fetch(base+'/api/health')).ok)break}catch{} if(i>90)throw Error('Startup timed out');await new Promise(r=>setTimeout(r,100))}
 browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:true,args:['--no-sandbox']})
 const context=await browser.newContext({viewport:{width:390,height:844}})
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message))
 const cdp=await context.newCDPSession(page);await cdp.send('WebAuthn.enable')
 await cdp.send('WebAuthn.addVirtualAuthenticator',{options:{protocol:'ctap2',transport:'internal',hasResidentKey:true,hasUserVerification:true,isUserVerified:true,automaticPresenceSimulation:true}})
 const signIn=async()=>{await page.getByLabel('Email',{exact:true}).fill('owner@example.com');await page.getByLabel('Password',{exact:true}).fill('correct horse battery staple');await page.getByRole('button',{name:'Sign in',exact:true}).click()}
 await page.goto(base+'/admin/');await signIn()
 await page.getByRole('button',{name:'Set up authenticator app',exact:true}).click()
 await page.getByLabel('Enrollment code',{exact:true}).fill(ticket)
 await page.getByRole('button',{name:'Show QR code',exact:true}).click()
 await page.getByAltText('Scan to add Argon Recordings to your authenticator app').waitFor()
 await page.getByText('Can’t scan the code?',{exact:true}).click()
 const secret=await page.getByLabel('Setup key',{exact:true}).inputValue()
 assert.ok(secret.length>=32)
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth))
 // Never save screenshots or logs while provisioning/recovery secrets are visible.
 await page.getByLabel('Six-digit code',{exact:true}).fill(otp(secret))
 await page.getByRole('button',{name:'Verify code',exact:true}).click()
 await page.getByRole('heading',{name:'Save your recovery codes',exact:true}).waitFor()
 const codes=await page.locator('.recovery-codes code').allTextContents();assert.equal(codes.length,8)
 await page.getByRole('button',{name:'I saved my codes',exact:true}).click()
 await page.getByRole('button',{name:'Sign out',exact:true}).click();await signIn()
 await page.getByRole('button',{name:'Use authenticator code',exact:true}).click()
 await page.getByLabel('Six-digit code',{exact:true}).fill(otp(secret,30))
 await page.getByRole('button',{name:'Verify code',exact:true}).click()
 await page.getByLabel('Invitation email').waitFor()
 await page.getByText('Passkeys and recovery',{exact:true}).click()
 await page.getByText('Authenticator app enabled',{exact:true}).waitFor()
 assert.ok(await page.getByRole('button',{name:'Remove authenticator app',exact:true}).isDisabled())
 await page.getByLabel('Passkey name',{exact:true}).fill('Preferred test passkey')
 await page.getByRole('button',{name:'Create passkey',exact:true}).click()
 await page.getByText('Preferred test passkey',{exact:true}).waitFor()
 await page.getByRole('button',{name:'Sign out',exact:true}).click();await signIn()
 await page.getByRole('button',{name:'Continue with passkey',exact:true}).waitFor()
 await page.getByRole('button',{name:'Use authenticator code',exact:true}).waitFor()
 await page.getByRole('button',{name:'Continue with passkey',exact:true}).click()
 await page.getByLabel('Invitation email').waitFor()
 assert.deepEqual(errors,[])
 console.log(JSON.stringify({checks:['opt-in TOTP-first QR enrollment','manual setup key','mobile setup geometry','confirmation and recovery acknowledgment','TOTP login','last-factor protection','add preferred passkey from TOTP session','passkey and authenticator alternatives','actual WebAuthn verification'],console_errors:errors},null,2))
} finally {
 if(browser)await browser.close()
 server.kill('SIGTERM');await new Promise(resolve=>{if(server.exitCode!==null)resolve();else server.once('exit',resolve)});log.end()
}
