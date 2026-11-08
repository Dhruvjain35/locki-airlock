import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { parseArgs } from 'node:util';
import { DbConnection } from '../../frontend/src/airlock-bindings/index';
import { AirlockHttp } from '../../scripts/airlock_http.mjs';
const {values}=parseArgs({options:{server:{type:'string'},database:{type:'string'},output:{type:'string'}}});
const uri=values.server||'http://127.0.0.1:3000', database=values.database||'locki-arm-delegation-test';
assert(['http://127.0.0.1:3000','https://maincloud.spacetimedb.com'].includes(uri));
const output=values.output||'data/airlock-arm-acceptance/report.json';
const clients:any[]=[]; const tests:string[]=[]; const ws=`arm-proof-${Date.now()}`;
const views=['my_workstations','my_memberships','my_devices','my_sessions','my_baseline_snapshots','my_device_health','my_arm_authorizations','my_lock_requests'];
async function connect(token?:string):Promise<any>{return new Promise((resolve,reject)=>{
 let c:DbConnection;const timer=setTimeout(()=>{c?.disconnect();reject(new Error('Connection timed out'))},15000);
 c=DbConnection.builder().withUri(uri).withDatabaseName(database).withToken(token).onConnect((conn,identity,savedToken)=>{
 conn.subscriptionBuilder().onApplied(()=>{clearTimeout(timer);const actor={conn,identity:identity.toHexString(),token:savedToken};clients.push(actor);resolve(actor)}).onError(e=>{clearTimeout(timer);reject(e)}).subscribe(views.map(v=>`SELECT * FROM ${v}`));
 }).onConnectError((_c,e)=>{clearTimeout(timer);reject(e)}).build();
})}
const rows=(a:any,name:string)=>[...a.conn.db[name].iter()];
async function until(f:()=>any){for(let i=0;i<200;i++){const v=f();if(v)return v;await new Promise(r=>setTimeout(r,25))}throw new Error('Readback timed out')}
const call=(a:any,name:string,args:any)=>a.conn.reducers[name](args);
async function denied(fn:()=>Promise<any>,reason:RegExp){await assert.rejects(fn,reason);}
let owner:any;
async function main(){
 owner=await connect();const watcher=await connect(owner.token);const mac=await connect();const other=await connect();
 await call(owner,'registerWorkstation',{workstationId:ws,name:'CONTRACT TEST ONLY; no camera or actual observation'});
 await call(owner,'enrollDevice',{workstationId:ws,deviceId:`${ws}-mac`,identityHex:mac.identity,role:'mac',label:'Contract fixture Mac; no hardware'});
 await until(()=>rows(owner,'myDevices').find(x=>x.id===`${ws}-mac`));
 const capture=(activeAppId='contract.fixture')=>call(mac,'recordBaseline',{workstationId:ws,deviceId:`${ws}-mac`,observedAtMs:BigInt(Date.now()),baselineJson:'["fixture-only"]',activeAppId});
 await capture(); await until(()=>rows(owner,'myBaselineSnapshots').length);
 const grant=async(id:string,overrides:any={},actor=owner)=>{await capture(); const w=await until(()=>rows(actor,'myWorkstations').find(x=>x.id===ws));return call(actor,'authorizeArm',{workstationId:ws,authorizationId:`${ws}-${id}`,deviceId:`${ws}-mac`,sessionId:`${ws}-${id}-session`,baselineJson:'["fixture-only"]',activeAppId:'contract.fixture',approvedAppsJson:'["approved.fixture"]',expectedOwnerPolicyHash:w.ownerPolicyHash,durationMs:60000n,ttlMs:60000n,...overrides})};
 const consume=(id:string,actor=mac)=>call(actor,'armSessionDelegated',{workstationId:ws,authorizationId:`${ws}-${id}`});
 await grant('one');await until(()=>rows(mac,'myArmAuthorizations').find(x=>x.id===`${ws}-one`));
 const issued=rows(mac,'myArmAuthorizations').find(x=>x.id===`${ws}-one`)!;
 const anonymous=new AirlockHttp({uri,database});
 await denied(()=>anonymous.call('arm_session_delegated',[ws,`${ws}-one`]),/Matching ARM delegate required/);
 await denied(()=>anonymous.call('authorize_arm',[ws,`${ws}-anon`,`${ws}-mac`,`${ws}-anon-session`,'[\"fixture-only\"]','contract.fixture','[]',issued.ownerPolicyHash,60000,30000]),/Workstation owner required/);
 tests.push('Anonymous HTTP caller cannot issue or consume ARM; original grant remains usable');
 assert.equal(issued.deviceId,`${ws}-mac`);assert.equal(issued.delegate.toHexString(),mac.identity);assert.equal(issued.owner.toHexString(),owner.identity);
 assert.equal(issued.durationMs,60000n);assert.equal(issued.expiresAtMs-issued.issuedAtMs,60000n);assert.equal(issued.baselineJson,'["fixture-only"]');assert.equal(issued.activeAppId,'contract.fixture');assert.equal(issued.approvedAppsJson,'["approved.fixture"]');
 assert.equal(rows(other,'myArmAuthorizations').length,0); await denied(()=>consume('one',other),/Matching ARM delegate required/);await denied(()=>consume('one',owner),/Matching ARM delegate required/); tests.push('Exact owner-reviewed grant and target identity binding; wrong caller and owner consumption denied');
 await consume('one');const session=await until(()=>rows(watcher,'mySessions').find(x=>x.id===`${ws}-one-session`));
 assert.equal(session.simulated,false);assert.equal(session.autoLockEnabled,false);assert.equal(session.armedBy.toHexString(),mac.identity); assert.equal(rows(watcher,'myLockRequests').length,0);
 assert.equal(session.baselineJson,issued.baselineJson);assert.equal(session.approvedAppsJson,issued.approvedAppsJson);assert.equal(session.ownerPolicyHash,issued.ownerPolicyHash);assert.equal(session.expiresAtMs-session.armedAtMs,issued.durationMs);
 await until(()=>rows(watcher,'myArmAuthorizations').find(x=>x.id===issued.id&&x.status==='consumed'));
 tests.push('One-use ARM commits fixed session; zero lock requests; synthetic input on isolated test workstation');
 await denied(()=>consume('one'),/expired, revoked or consumed/);await denied(()=>call(mac,'disarmSession',{workstationId:ws}),/Workstation owner required/);await denied(()=>call(other,'disarmSession',{workstationId:ws}),/Workstation owner required/);tests.push('Replay and non-owner disarm denied');
 await call(watcher,'disarmSession',{workstationId:ws});
 await grant('expired',{ttlMs:1000n});
 const expires=await until(()=>rows(owner,'myArmAuthorizations').find(x=>x.id===`${ws}-expired`));
 await new Promise(resolve=>setTimeout(resolve,Math.max(0,Number(expires.expiresAtMs)-Date.now()+1000)));
 await denied(()=>consume('expired'),/expired, revoked or consumed/);tests.push('Expired grant denied by reducer without relying on view expiration');
 await grant('policy');await call(owner,'setPolicy',{workstationId:ws,autoLockEnabled:false,lockCooldownMs:31000n});
 await denied(()=>consume('policy'),/expired, revoked or consumed|Owner policy changed/);tests.push('Policy change revokes pending grant');
 await until(()=>rows(owner,'myWorkstations').find(x=>x.id===ws&&x.lockCooldownMs===31000n));
 await grant('changed-app');await capture('changed.fixture');await denied(()=>consume('changed-app'),/Reviewed baseline or active application changed/);tests.push('Changed active application rejects reviewed grant');
 await grant('disabled');await call(owner,'revokeMember',{workstationId:ws,identityHex:mac.identity});
 await denied(()=>consume('disabled'),/expired, revoked or consumed|Matching enabled Mac|Not an enrolled member/);tests.push('Revoked Mac cannot consume pending grant');
 await call(owner,'enrollDevice',{workstationId:ws,deviceId:`${ws}-mac`,identityHex:mac.identity,role:'mac',label:'Synthetic contract fixture Mac; no hardware'});
 await until(()=>rows(owner,'myDevices').find(x=>x.id===`${ws}-mac`&&x.enabled));
 await grant('disconnect');owner.conn.disconnect();
 await until(()=>rows(mac,'myArmAuthorizations').find(x=>x.id===`${ws}-disconnect` && x.status==='revoked'));
 await denied(()=>consume('disconnect'),/expired, revoked or consumed/);tests.push('Actual issuing WebSocket disconnect revokes grant');
 owner=await connect(watcher.token);await grant('other-tab');watcher.conn.disconnect();
 await consume('other-tab');tests.push('Other owner tab disconnect leaves issuing connection grant usable');
 await call(owner,'disarmSession',{workstationId:ws});
 await capture(); const w=rows(owner,'myWorkstations').find(x=>x.id===ws);
 const http=new AirlockHttp({uri,database,token:owner.token});let httpResult='';
 try {await http.call('authorize_arm',[ws,`${ws}-http`,`${ws}-mac`,`${ws}-http-session`,'["fixture-only"]','contract.fixture','[]',w.ownerPolicyHash,60000,60000]);
   await until(()=>rows(mac,'myArmAuthorizations').find(x=>x.id===`${ws}-http` && x.status==='revoked'));await denied(()=>consume('http'),/expired, revoked or consumed/);httpResult='transient HTTP connection grant revoked';
 }catch(e:any){assert.match(e.message,/persistent owner connection required/);httpResult='HTTP issuance rejected: no connection';}
 tests.push(httpResult);
 const final=await until(()=>rows(owner,'myWorkstations').find(x=>x.id===ws && x.activeSessionId===''));
 assert.equal(rows(owner,'myLockRequests').length,0);tests.push('Zero lock requests throughout isolated test; final workstation disarmed');
 const report={passed:true,checked_at:new Date().toISOString(),database,uri,workstation:ws,tests,remaining_active_session:final.activeSessionId,lock_request_count:0,real_camera_test:false,real_model_test:false,real_audio_test:false,cloud_mutations:uri.startsWith('https:'),fixture_provenance:'Synthetic baseline and application identifiers exercise non-simulated ARM protocol on a unique test workstation; not actual sensor or voice evidence.'};
 await writeFile(output,JSON.stringify(report,null,2)+'\n');console.log(JSON.stringify(report,null,2));
}
main().catch(e=>{console.error(e);process.exitCode=1}).finally(async()=>{if(owner){try{await call(owner,'disarmSession',{workstationId:ws})}catch{}}for(const c of clients)c.conn.disconnect()});
