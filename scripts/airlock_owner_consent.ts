/** Local owner-only consent broker. Credentials never enter HTTP responses or voice. */
import { constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createHash, randomUUID } from 'node:crypto';
import { isAbsolute, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { DbConnection } from '../frontend/src/airlock-bindings/index';
import { startOwnerConsentServer } from './airlock_owner_consent_http.mjs';

const execute=promisify(execFile);
const hex=(v:any)=>typeof v==='string'?v:v?.toHexString();
const validId=(v:any)=>typeof v==='string'&&/^[A-Za-z0-9_.:-]{1,128}$/.test(v);
const requireValue=(ok:any)=>{if(!ok)throw Error('Owner consent unavailable');};
const encode=(v:any)=>JSON.stringify(v,(_k,x)=>typeof x==='bigint'?x.toString():x);
export type Config={server:string;database:string;expectedOwnerIdentity:string;workstationId:string;deviceId:string;expectedMacIdentity:string;cliConfigPath:string;cliTokenKeyPath:string[];pythonExecutable:string};
export type State={connected:boolean;identity:string;connectionId:string;workstations:any[];devices:any[];memberships:any[];baselines:any[];health:any[];grants:any[]};
export function validateConfig(config:any):Config{
  requireValue(config&&['https://maincloud.spacetimedb.com','http://127.0.0.1:3000'].includes(config.server));
  requireValue([config.database,config.workstationId,config.deviceId].every(validId));
  requireValue([config.expectedOwnerIdentity,config.expectedMacIdentity].every(v=>typeof v==='string'&&/^[a-f0-9]{64}$/.test(v))&&config.expectedOwnerIdentity!==config.expectedMacIdentity);
  requireValue([config.cliConfigPath,config.pythonExecutable].every(v=>typeof v==='string'&&isAbsolute(v)));
  requireValue(Array.isArray(config.cliTokenKeyPath)&&config.cliTokenKeyPath.length>0&&config.cliTokenKeyPath.length<=8&&config.cliTokenKeyPath.every((v:any)=>typeof v==='string'&&/^[A-Za-z0-9_-]{1,64}$/.test(v)));
  return config;
}
async function privateJson(path:string){
  const file=await open(path,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
  try{const stat=await file.stat();requireValue(stat.isFile()&&stat.uid===process.getuid?.()&&!(stat.mode&0o077)&&stat.size<=16384);return JSON.parse(await file.readFile('utf8'));}finally{await file.close();}
}
export async function readExistingCliToken(config:Config){
  // stdout is a private child-to-parent pipe, never inherited or logged.
  const code=`import os,stat,json,sys,tomllib
fd=os.open(sys.argv[1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
s=os.fstat(fd)
assert stat.S_ISREG(s.st_mode) and s.st_uid==os.getuid() and not(s.st_mode & 0o077) and s.st_size<=65536
with os.fdopen(fd,'rb') as f: value=tomllib.load(f)
for key in json.loads(sys.argv[2]): value=value[key]
assert isinstance(value,str) and 16<=len(value)<=16384 and '\\n' not in value and '\\r' not in value
sys.stdout.write(value)
`;
  try{const result=await execute(config.pythonExecutable,['-c',code,config.cliConfigPath,JSON.stringify(config.cliTokenKeyPath)],{timeout:5000,maxBuffer:32768});return result.stdout;}catch{throw Error('Existing private CLI login unavailable');}
}
export function reviewedPermission(state:State,config:Config,input:any,now=Date.now()){
  requireValue(input&&Object.keys(input).sort().join(',')==='approvedApps,durationMinutes');
  requireValue(Number.isInteger(input.durationMinutes)&&input.durationMinutes>=1&&input.durationMinutes<=60);
  requireValue(Array.isArray(input.approvedApps)&&input.approvedApps.length<=256&&input.approvedApps.every((v:any)=>typeof v==='string'&&v.length>0&&v.length<=256));
  requireValue(state.connected&&state.identity===config.expectedOwnerIdentity&&state.connectionId);
  const station=state.workstations.find(w=>w.id===config.workstationId);
  const device=state.devices.find(d=>d.id===config.deviceId&&d.workstationId===config.workstationId);
  const owner=state.memberships.find(m=>m.workstationId===config.workstationId&&hex(m.identity)===config.expectedOwnerIdentity);
  requireValue(station&&hex(station.owner)===config.expectedOwnerIdentity&&station.autoLockEnabled===false&&station.activeSessionId===''&&station.ownerPolicyHash);
  // my_memberships contains only the owner caller. Target membership is checked
  // atomically by authorize_arm; the owner view exposes its exact enabled device.
  requireValue(owner?.enabled&&owner.role==='owner');
  requireValue(device?.enabled&&device.role==='mac'&&hex(device.identity)===config.expectedMacIdentity);
  const capture=state.baselines.find(b=>b.id===`latest:${config.deviceId}`&&b.workstationId===config.workstationId&&b.deviceId===config.deviceId&&b.sessionId===''&&hex(b.publisher)===config.expectedMacIdentity);
  const health=state.health.find(h=>h.workstationId===config.workstationId&&h.deviceId===config.deviceId&&h.component==='usb');
  const fresh=(time:any,maxAge:number)=>{const value=Number(time);return Number.isSafeInteger(value)&&value<=now+5000&&value+maxAge>=now;};
  requireValue(capture&&fresh(capture.observedAtMs,30000)&&fresh(capture.receivedAtMs,30000)&&health?.status==='online'&&fresh(health.lastSeenMs,15000));
  requireValue(typeof capture.activeAppId==='string'&&capture.activeAppId.length>0&&capture.activeAppId.length<=256&&typeof capture.baselineJson==='string');
  const baseline=JSON.parse(capture.baselineJson);requireValue(Array.isArray(baseline)&&baseline.length<=256&&baseline.every((v:any)=>typeof v==='string'&&v.length>0&&v.length<=256));
  const review={workstationId:config.workstationId,deviceId:config.deviceId,macIdentity:config.expectedMacIdentity,ownerIdentity:config.expectedOwnerIdentity,ownerConnectionId:state.connectionId,baselineJson:JSON.stringify([...new Set(baseline)].sort()),activeAppId:capture.activeAppId,approvedAppsJson:JSON.stringify([...new Set(input.approvedApps)].sort()),expectedOwnerPolicyHash:station.ownerPolicyHash,durationMs:input.durationMinutes*60000,ttlMs:30000,autoLockEnabled:false,simulated:false};
  return {review,digest:createHash('sha256').update(encode(review)).digest('hex')};
}
export function createConsentAdapter(config:Config,link:{snapshot():State;authorize(args:any):Promise<void>;revoke(id:string):Promise<void>;close():void},now=Date.now){
  let selected:any=null,pendingId='',closed=false;
  const status=async()=>{
    requireValue(!closed);
    let current:any=null;try{if(selected)current=reviewedPermission(link.snapshot(),config,selected.input,now());}catch{}
    if(!current||current.digest!==selected?.digest)selected=null;
    const grant=pendingId?link.snapshot().grants.find(g=>g.id===pendingId&&g.workstationId===config.workstationId):undefined;
    return {reviewDigest:selected?.digest||'',connected:link.snapshot().connected,grant:grant?{authorizationId:grant.id,sessionId:grant.sessionId,status:!link.snapshot().connected?'unverified':grant.status==='authorized'&&Number(grant.expiresAtMs)<=now()?'expired':grant.status,expiresAtMs:Number(grant.expiresAtMs)}:null};
  };
  const revoke=async()=>{selected=null;if(pendingId){const id=pendingId;try{await link.revoke(id);}catch{closed=true;link.close();throw Error('Owner revocation unconfirmed; connection closed');}finally{pendingId='';}}};
  return {
    async review(input:any){requireValue(!closed);const result=reviewedPermission(link.snapshot(),config,input,now());selected={...result,input:structuredClone(input)};return result;},
    async authorize(input:any){
      requireValue(!closed&&selected&&input&&Object.keys(input).join(',')==='digest'&&input.digest===selected.digest);
      const fresh=reviewedPermission(link.snapshot(),config,selected.input,now());requireValue(fresh.digest===selected.digest);selected=null;
      const authorizationId=`voice-arm-${randomUUID()}`,sessionId=`voice-session-${randomUUID()}`;
      pendingId=authorizationId;
      const {review}=fresh;
      try{
      await link.authorize({workstationId:config.workstationId,authorizationId,deviceId:config.deviceId,sessionId,baselineJson:review.baselineJson,activeAppId:review.activeAppId,approvedAppsJson:review.approvedAppsJson,expectedOwnerPolicyHash:review.expectedOwnerPolicyHash,durationMs:BigInt(review.durationMs),ttlMs:30000n});
      const state=link.snapshot();
      const grant=state.grants.find(g=>g.id===authorizationId&&g.workstationId===config.workstationId);
      requireValue(state.connected&&state.connectionId===review.ownerConnectionId&&grant?.status==='authorized'&&hex(grant.owner)===config.expectedOwnerIdentity&&hex(grant.delegate)===config.expectedMacIdentity&&grant.deviceId===config.deviceId&&grant.sessionId===sessionId&&grant.baselineJson===review.baselineJson&&grant.activeAppId===review.activeAppId&&grant.approvedAppsJson===review.approvedAppsJson&&grant.ownerPolicyHash===review.expectedOwnerPolicyHash&&Number(grant.durationMs)===review.durationMs&&Number(grant.expiresAtMs)>now());
      return {authorizationId,sessionId,status:'authorized',expiresAtMs:Number(grant.expiresAtMs)};
      }catch{try{await revoke();}catch{closed=true;link.close();}throw Error('Owner grant was not confirmed');}
    },revoke,status,
    async close(){if(closed)return;closed=true;try{await revoke();}finally{link.close();}},
  };
}
export async function connectOwner(config:Config,token:string){
  return new Promise<any>((resolve,reject)=>{
    let conn:DbConnection,connected=false;
    const fail=()=>{connected=false;clearTimeout(timer);conn?.disconnect();reject(Error('Authenticated owner connection unavailable'));};
    const timer=setTimeout(fail,12000);
    conn=DbConnection.builder().withUri(config.server).withDatabaseName(config.database).withToken(token)
      .onConnect((c,id)=>{
        if(id.toHexString()!==config.expectedOwnerIdentity){fail();return;}connected=true;
        c.subscriptionBuilder().onError(fail).onApplied(()=>{
          clearTimeout(timer);const rows=(name:string)=>[...(c.db as any)[name].iter()];
          const snapshot=()=>({connected,identity:id.toHexString(),connectionId:c.connectionId.toHexString(),workstations:rows('myWorkstations'),devices:rows('myDevices'),memberships:rows('myMemberships'),baselines:rows('myBaselineSnapshots'),health:rows('myDeviceHealth'),grants:rows('myArmAuthorizations')});
          const bounded=async(work:Promise<any>)=>{let t:ReturnType<typeof setTimeout>|undefined;try{return await Promise.race([work,new Promise((_,r)=>{t=setTimeout(()=>r(Error('Owner operation timed out')),5000);})]);}finally{clearTimeout(t);}};
          resolve({snapshot,authorize:async(args:any)=>{await bounded((c.reducers as any).authorizeArm(args));for(let n=0;n<80;n++){if(snapshot().grants.some(g=>g.id===args.authorizationId))return;await new Promise(r=>setTimeout(r,25));}throw Error('Grant readback unavailable');},revoke:async(id:string)=>{await bounded((c.reducers as any).revokeArmAuthorization({workstationId:config.workstationId,authorizationId:id}));},close:()=>{connected=false;c.disconnect();}});
        }).subscribe(['my_workstations','my_devices','my_memberships','my_baseline_snapshots','my_device_health','my_arm_authorizations'].map(v=>`SELECT * FROM ${v}`));
      }).onConnectError(fail).onDisconnect(()=>{connected=false;}).build();
  });
}
async function main(){
  const [flag,path,...extra]=process.argv.slice(2);requireValue(flag==='--config'&&path&&extra.length===0);
  const config=validateConfig(await privateJson(path));
  // SDK diagnostics must never become an accidental credential or machine-output channel.
  console.log=()=>{};
  const token=await readExistingCliToken(config),link=await connectOwner(config,token);
  const adapter=createConsentAdapter(config,link);
  const server=await startOwnerConsentServer(adapter);
  const finish=()=>{void server.close().catch(()=>{});};process.once('SIGINT',finish);process.once('SIGTERM',finish);
  try{await execute('/usr/bin/open',[server.url],{timeout:5000,maxBuffer:4096});process.stdout.write('LOCKI owner consent opened in the local browser.\n');}catch{await server.close();throw Error('Local owner browser unavailable');}
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href)main().catch(()=>{process.stderr.write('LOCKI owner consent unavailable. Check the private local configuration and existing owner login.\n');process.exitCode=1;});
