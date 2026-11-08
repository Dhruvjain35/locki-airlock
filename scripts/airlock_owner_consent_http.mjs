import { createServer } from 'node:http';
import { randomBytes, timingSafeEqual } from 'node:crypto';

const PAGE = String.raw`<!doctype html><html><head><meta charset="utf-8"><title>LOCKI owner permission</title></head><body>
<h1>Authorize voice ARM</h1><p>This local page uses your existing owner login. Your credential stays on this computer.</p>
<p>Automatic locking stays disabled. Permission lasts 30 seconds and can be used once by the selected Mac. Keep this page open.</p>
<label>Session minutes (1–60) <input id="duration" type="number" min="1" max="60" value="15"></label>
<label>Approved application IDs, one per line <textarea id="apps"></textarea></label>
<button id="review">Review exact permission</button><pre id="details"></pre>
<label><input id="consent" type="checkbox" disabled>I approve this exact workstation, Mac, inventory, active application and session policy.</label>
<button id="authorize" disabled>Authorize once</button><button id="revoke">Revoke permission</button><button id="close">Close owner connection</button><p id="status"></p>
<script>
const token=location.hash.slice(1);history.replaceState(null,'','/');let digest='',lastReview=null;
const status=document.getElementById('status'),consent=document.getElementById('consent'),authorize=document.getElementById('authorize');
async function api(path,body={}){const r=await fetch('/api/'+path,{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await r.json();if(!r.ok)throw Error(j.error||'Request denied');return j;}
function clear(){digest='';consent.checked=false;consent.disabled=true;authorize.disabled=true;}
document.getElementById('duration').oninput=clear;document.getElementById('apps').oninput=clear;
consent.onchange=()=>authorize.disabled=!consent.checked||!digest;
document.getElementById('review').onclick=async()=>{clear();try{lastReview=await api('review',{durationMinutes:Number(document.getElementById('duration').value),approvedApps:document.getElementById('apps').value.split('\n').map(x=>x.trim()).filter(Boolean)});digest=lastReview.digest;document.getElementById('details').textContent=JSON.stringify(lastReview.review,null,2);consent.disabled=false;status.textContent='Review these exact values before approving.';}catch(e){status.textContent=e.message;}};
authorize.onclick=async()=>{const selected=digest;clear();try{const g=await api('authorize',{digest:selected});status.textContent='Permission committed. Say ARM within 30 seconds. '+JSON.stringify(g);}catch(e){status.textContent=e.message;}};
document.getElementById('revoke').onclick=async()=>{clear();try{await api('revoke');status.textContent='Pending permission revoked.';}catch(e){status.textContent=e.message;}};
document.getElementById('close').onclick=async()=>{clear();try{await api('close');status.textContent='Owner connection closed. Pending permission revoked.';}catch(e){status.textContent=e.message;}};
setInterval(async()=>{try{const s=await api('status');if(digest&&s.reviewDigest!==digest){clear();status.textContent='Reviewed state changed. Review again.';}if(s.grant)status.textContent='Permission: '+s.grant.status+'; expires '+new Date(s.grant.expiresAtMs).toLocaleTimeString();}catch(e){clear();status.textContent='Owner connection unavailable. Review permission again.';}},3000);
addEventListener('pagehide',()=>{fetch('/api/close',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:'{}',keepalive:true}).catch(()=>{});});
</script></body></html>`;

function authorized(actual, expected) {
  if (typeof actual !== 'string') return false;
  const received=Buffer.from(actual), target=Buffer.from(`Bearer ${expected}`);
  return received.length===target.length&&timingSafeEqual(received,target);
}
async function body(req) {
  if(req.headers['content-type']!=='application/json')throw Error('JSON_REQUIRED');
  let raw='';for await(const chunk of req){raw+=chunk;if(Buffer.byteLength(raw)>16384)throw Error('BODY_TOO_LARGE');}
  const parsed=JSON.parse(raw);if(!parsed||typeof parsed!=='object'||Array.isArray(parsed))throw Error('INVALID_BODY');return parsed;
}

/** Adapter exposes only reviewed ARM authorization, its revocation and safe projections. */
export async function startOwnerConsentServer(adapter,{secret=randomBytes(32).toString('hex'),leaseMs=15000,now=Date.now}={}) {
  if(!/^[a-f0-9]{64}$/.test(secret)||leaseMs<1000)throw Error('INVALID_SERVER_OPTIONS');
  let origin='',lastSeen=now(),closed=false,busy=false;
  const close=async()=>{if(closed)return;closed=true;clearInterval(timer);try{await adapter.close();}finally{server.close();server.closeIdleConnections();}};
  const server=createServer(async(req,res)=>{
    const reply=(code,payload)=>{res.writeHead(code,{'Content-Type':'application/json','Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'});res.end(JSON.stringify(payload));};
    try{
      if(closed||req.headers.host!==new URL(origin).host){reply(403,{error:'REQUEST_DENIED'});return;}
      if(req.method==='GET'&&req.url==='/'){
        res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"});res.end(PAGE);return;
      }
      if(req.method!=='POST'||!['/api/review','/api/authorize','/api/revoke','/api/status','/api/close'].includes(req.url)||req.headers.origin!==origin||!authorized(req.headers.authorization,secret)||!['same-origin',undefined].includes(req.headers['sec-fetch-site'])){reply(403,{error:'REQUEST_DENIED'});return;}
      const input=await body(req);lastSeen=now();
      if(busy){reply(409,{error:'OPERATION_IN_PROGRESS'});return;}
      busy=true;
      try{
        if(req.url==='/api/review')reply(200,await adapter.review(input));
        else if(req.url==='/api/authorize')reply(200,await adapter.authorize(input));
        else if(req.url==='/api/revoke'){if(Object.keys(input).length)throw Error('INVALID_BODY');await adapter.revoke();reply(200,{revoked:true});}
        else if(req.url==='/api/status'){if(Object.keys(input).length)throw Error('INVALID_BODY');reply(200,await adapter.status());}
        else {if(Object.keys(input).length)throw Error('INVALID_BODY');await close();reply(200,{closed:true});}
      }finally{busy=false;}
    }catch{reply(409,{error:'REVIEW_OR_PERMISSION_UNAVAILABLE'});}
  });
  server.requestTimeout=5000;server.headersTimeout=5000;server.keepAliveTimeout=1000;server.maxConnections=8;
  await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
  origin=`http://127.0.0.1:${server.address().port}`;
  const timer=setInterval(()=>{if(now()-lastSeen>leaseMs)void close().catch(()=>{});},1000);timer.unref();
  return {origin,url:`${origin}/#${secret}`,close};
}
