/** Explicit cloud fixture: production M4 ARM service/transport, no hardware or audio. */
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdir, writeFile, unlink } from 'node:fs/promises';
import { resolve } from 'node:path';
import { DbConnection } from '../../frontend/src/airlock-bindings/index';
import { connectOwner, createConsentAdapter, readExistingCliToken } from '../airlock_owner_consent';
import { startOwnerConsentServer } from '../airlock_owner_consent_http.mjs';

const uri='https://maincloud.spacetimedb.com',database='locki-w91bw';
const ws=`m4-arm-proof-${Date.now()}`,deviceId=`${ws}-mac`,clients:any[]=[];
const directory=resolve('data/airlock-m4-arm-acceptance');
async function connect():Promise<any>{return new Promise((done,reject)=>{
 let c:DbConnection;const timer=setTimeout(()=>{c?.disconnect();reject(Error('Fixture connect timeout'));},12000);
 c=DbConnection.builder().withUri(uri).withDatabaseName(database).onConnect((conn,id,token)=>{
 conn.subscriptionBuilder().onApplied(()=>{clearTimeout(timer);const actor={conn,identity:id.toHexString(),token};clients.push(actor);done(actor);}).onError(reject)
 .subscribe(['my_workstations','my_sessions','my_lock_requests'].map(v=>`SELECT * FROM ${v}`));
 }).onConnectError((_c,e)=>reject(e)).build();
});}
const rows=(a:any,name:string)=>[...a.conn.db[name].iter()];
async function until(check:()=>any){for(let i=0;i<160;i++){const value=check();if(value)return value;await new Promise(r=>setTimeout(r,25));}throw Error('Fixture readback timeout');}
async function python(input:any){
 const code=`import asyncio,json,sys\nfrom collections import deque\nfrom types import SimpleNamespace\nfrom app.security.service import SecurityService\nfrom app.security.spacetime_adapter import SpacetimeConfig,SpacetimeTransport\nfrom app.security.arm_delegation import security_voice_intent\nx=json.load(sys.stdin)\nassert security_voice_intent('LOCKI, watch my setup.')=='arm'\nconfig=SpacetimeConfig(url=x['uri'],database=x['database'],workstation_id=x['workstation'],device_id=x['device'],token=x['token'])\nspoken=[];applied=[]\ndef say(line,*args,**kwargs):\n spoken.append(line);return line\nrt=SimpleNamespace(speaker=SimpleNamespace(epoch=0),_say=say,_result=lambda ok,detail,*args:{'ok':ok,'detail':detail})\ns=object.__new__(SecurityService)\ns.rt=rt;s.subscriber=SimpleNamespace(config=config);s.transport=SpacetimeTransport(config);s._voice_arm_busy=False;s._voice_arm_sessions=deque(maxlen=64);s.apply_remote_session=applied.append\nasync def run():\n first=await s.request_arm_by_voice()\n assert first['ok'],first\n assert len(applied)==1 and applied[0].session_id==x['session']\n assert spoken==['security_armed'],spoken\n second=await s.request_arm_by_voice()\n assert not second['ok'],second\n assert spoken.count('security_armed')==1\n assert s._voice_arm_busy is False\n return {'passed':True,'sessionId':applied[0].session_id,'first':first,'replay':second,'spokenLines':spoken,'hardware':False,'audioPlayback':False}\ntry: print(json.dumps(asyncio.run(run())))\nfinally:s.transport.close()\n`;
 return new Promise<any>((done,reject)=>{
  const p=spawn(resolve('backend/.venv/bin/python'),['-c',code],{cwd:process.cwd(),env:{...process.env,PYTHONPATH:resolve('backend')},stdio:['pipe','pipe','pipe']});
  let out='',err='';const timer=setTimeout(()=>{p.kill();reject(Error('Fixture Python timeout'));},45000);
  p.stdout.on('data',b=>{out+=b;if(out.length>65536)p.kill();});p.stderr.on('data',b=>{err+=b;if(err.length>65536)p.kill();});
  p.on('error',reject);p.on('close',status=>{clearTimeout(timer);if(status!==0)reject(Error(`Production M4 fixture failed: ${err.replaceAll(input.token,'[REDACTED]').slice(-1800)}`));else{try{done(JSON.parse(out.trim()));}catch{reject(Error('Invalid fixture result'));}}});
  p.stdin.end(JSON.stringify(input)); // fixture Mac token only via private child pipe, never argv/log/file
 });
}

let owner:any,consentServer:any,consentLink:any,heartbeat:ReturnType<typeof setInterval>|undefined;
const fixtureCliPath=resolve(directory,`${ws}-owner.private.toml`);
async function main(){
 await mkdir(directory,{recursive:true});
 owner=await connect();const mac=await connect();
 await owner.conn.reducers.registerWorkstation({workstationId:ws,name:'SYNTHETIC M4 ARM fixture, no hardware/audio'});
 await owner.conn.reducers.enrollDevice({workstationId:ws,deviceId,identityHex:mac.identity,role:'mac',label:'SYNTHETIC fixture only'});
 await mac.conn.reducers.recordBaseline({workstationId:ws,deviceId,observedAtMs:BigInt(Date.now()),baselineJson:'["SYNTHETIC-M4-ARM-FIXTURE"]',activeAppId:'fixture.m4.arm'});
 await until(()=>rows(owner,'myWorkstations').find(w=>w.id===ws));
 // Explicitly synthetic private CLI fixture, not Dhruv's existing credential.
 await writeFile(fixtureCliPath,`spacetimedb_token = ${JSON.stringify(owner.token)}\n`,{mode:0o600,flag:'wx'});
 const config={server:uri,database,expectedOwnerIdentity:owner.identity,workstationId:ws,deviceId,expectedMacIdentity:mac.identity,cliConfigPath:fixtureCliPath,cliTokenKeyPath:['spacetimedb_token'],pythonExecutable:resolve('backend/.venv/bin/python')};
 consentLink=await connectOwner(config,await readExistingCliToken(config));
 consentServer=await startOwnerConsentServer(createConsentAdapter(config,consentLink));
 const secret=new URL(consentServer.url).hash.slice(1);
 const consent=async(path:string,body:any={})=>{
  const response=await fetch(`${consentServer.origin}/api/${path}`,{method:'POST',headers:{Origin:consentServer.origin,Authorization:`Bearer ${secret}`,'Content-Type':'application/json'},body:JSON.stringify(body)});
  assert.equal(response.status,200,`Owner consent ${path} rejected`);return response.json();
 };
 const review:any=await consent('review',{durationMinutes:1,approvedApps:[]});
 assert.equal(review.review.workstationId,ws);assert.equal(review.review.deviceId,deviceId);assert.equal(review.review.macIdentity,mac.identity);
 const permission:any=await consent('authorize',{digest:review.digest});
 const {authorizationId,sessionId}=permission;
 heartbeat=setInterval(()=>{void consent('status').catch(()=>{});},3000);
 const result=await python({uri,database,workstation:ws,device:deviceId,token:mac.token,session:sessionId});
 const session=await until(()=>rows(owner,'mySessions').find(s=>s.id===sessionId));
 assert.equal(session.state,'armed');assert.equal(session.simulated,false);assert.equal(session.autoLockEnabled,false);assert.equal(session.armedBy.toHexString(),mac.identity);assert.equal(rows(owner,'myLockRequests').length,0);
 await owner.conn.reducers.disarmSession({workstationId:ws});
 await until(()=>rows(owner,'myWorkstations').find(w=>w.id===ws&&w.activeSessionId===''));
 const report={passed:true,checkedAt:new Date().toISOString(),server:uri,database,workstation:ws,authorizationId,sessionId,ownerConsentHttp:true,privateFixtureCliRead:true,productionM4Service:result,fixture:'Synthetic private CLI credential, baseline/application and identities in unique fixture workstation. Actual owner consent HTTP/SDK plus Python M4 service method/transport; no runtime startup, microphone, camera, model or audio.',actualDhruvWorkstationMutated:false,actualM4CliCredentialUsed:false,finalFixtureDisarmed:true,lockRequests:0};
 return report;
}
async function run(){
 await mkdir(directory,{recursive:true});
 const reportPath=resolve(directory,'report.json');
 await writeFile(reportPath,JSON.stringify({passed:false,status:'running',startedAt:new Date().toISOString(),workstation:ws})+'\n');
 let result:any,failure:unknown,cleanupFailure=false;
 try{result=await main();}catch(e){failure=e;}
 finally{
  clearInterval(heartbeat);
  try{await consentServer?.close();consentLink?.close();}catch{cleanupFailure=true;}
  if(owner){try{await owner.conn.reducers.disarmSession({workstationId:ws});await until(()=>rows(owner,'myWorkstations').find(w=>w.id===ws&&w.activeSessionId===''));}catch{cleanupFailure=true;}}
  for(const c of clients)c.conn.disconnect();
  try{await unlink(fixtureCliPath);}catch(e:any){if(e.code!=='ENOENT')cleanupFailure=true;}
 }
 if(failure||cleanupFailure){
  await writeFile(reportPath,JSON.stringify({passed:false,status:'failed',checkedAt:new Date().toISOString(),workstation:ws,cleanupConfirmed:!cleanupFailure,reason:cleanupFailure?'Fixture cleanup could not be confirmed':'Production M4 fixture failed; inspect local test output'})+'\n');
  throw failure||Error('Fixture cleanup could not be confirmed');
 }
 await writeFile(reportPath,JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result,null,2));
}
run().catch(e=>{console.error(String(e));process.exitCode=1;});
