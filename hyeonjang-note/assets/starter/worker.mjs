import { estimateTotals, statuses } from './public/domain.js';
import { MONTHLY_PRICE, nextBillingDate, entitlement, validateBillingReturn, tossRequest, lookupTossOrder, verifyCharge, encryptBillingKey, decryptBillingKey } from './billing-core.mjs';
import { cookie, digest, verifyGoogleIdToken, sessionUser, sessionCookie, clearSessionCookie } from './google-auth.mjs';

const securityHeaders={
 'X-Content-Type-Options':'nosniff','Referrer-Policy':'same-origin','X-Frame-Options':'DENY',
 'Content-Security-Policy':"default-src 'self'; script-src 'self' https://js.tosspayments.com https://accounts.google.com/gsi/client; style-src 'self' 'unsafe-inline' https://accounts.google.com/gsi/style; img-src 'self' blob: data: https://*.tosspayments.com https://*.googleusercontent.com; media-src 'self' blob:; connect-src 'self' https://*.tosspayments.com https://accounts.google.com/gsi/; frame-src https://*.tosspayments.com https://accounts.google.com/gsi/; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://*.tosspayments.com https://accounts.google.com"
};
const reply=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{...securityHeaders,'Content-Type':'application/json; charset=utf-8','Cache-Control':'no-store'}});
function invalid(message,status=400){throw Object.assign(new Error(message),{status});}
function str(v,max=1000){if(typeof v!=='string'||v.length>max)invalid('입력한 글자 수를 확인해 주세요.');return v.trim();}
function opt(v,max=1000){return str(v??'',max);}
function reqstr(v,label,max=200){const s=str(v,max);if(!s)invalid(`${label}을 입력해 주세요.`);return s;}
function numeric(v,max=1e12){if(typeof v!=='number'||!Number.isFinite(v)||v<0||v>max)invalid('수량과 금액을 확인해 주세요.');return v;}
function amount(v){const n=numeric(v);if(!Number.isSafeInteger(n))invalid('금액은 원 단위 정수로 입력해 주세요.');return n;}
function dateValue(v){if(!v)return '';if(typeof v!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(v)||!Number.isFinite(Date.parse(v))||new Date(v).toISOString().slice(0,10)!==v)invalid('날짜를 확인해 주세요.');return v;}
function identifier(v){if(typeof v!=='string'||!/^[a-zA-Z0-9-]{1,80}$/.test(v))invalid('항목 번호를 확인해 주세요.');return v;}
function repository(env,user){
 const one=async(kind,key)=>{const row=await env.DB.prepare('SELECT * FROM documents WHERE user_id=? AND kind=? AND id=?').bind(user,kind,key).first();return row?{...JSON.parse(row.body),id:row.id,revision:row.revision,updatedAt:row.updated_at}:null;};
 const many=async kind=>{const r=await env.DB.prepare('SELECT * FROM documents WHERE user_id=? AND kind=? ORDER BY updated_at DESC,id').bind(user,kind).all();return r.results.map(row=>({...JSON.parse(row.body),id:row.id,revision:row.revision,updatedAt:row.updated_at}));};
 const file=async key=>env.DB.prepare('SELECT * FROM files WHERE user_id=? AND id=?').bind(user,key).first();
 return {one,many,file};
}
async function validated(kind,p,repo){
 if(!p||typeof p!=='object'||Array.isArray(p))invalid('입력 형식을 확인해 주세요.');
 if(kind==='projects'){
  if(!statuses.includes(p.status)||!['주거','상가'].includes(p.type))invalid('현장 유형과 진행 상태를 선택해 주세요.');
  if(p.time&&!/^([01]\d|2[0-3]):[0-5]\d$/.test(p.time))invalid('방문 시간을 확인해 주세요.');
  return {name:reqstr(p.name,'현장명'),customer:opt(p.customer,100),phone:opt(p.phone,40),address:opt(p.address,500),type:p.type,status:p.status,date:dateValue(p.date),time:opt(p.time,5),scope:opt(p.scope,3000),nextAction:opt(p.nextAction,500),contract:amount(p.contract??0),received:amount(p.received??0),cost:amount(p.cost??0),hours:numeric(p.hours??0,100000)};
 }
 if(kind==='estimates'||kind==='records'){identifier(p.projectId);if(!await repo.one('projects',p.projectId))invalid('먼저 현장을 추가해 주세요.',404);}
 if(kind==='estimates'){
  if(!Array.isArray(p.items)||!p.items.length||p.items.length>100)invalid('견적 항목은 1~100개까지 입력할 수 있어요.');
  const items=p.items.map(i=>{const qty=numeric(i.qty,1000000);if(qty<=0)invalid('수량은 0보다 크게 입력해 주세요.');return {name:reqstr(i.name,'품목명'),spec:opt(i.spec,200),unit:opt(i.unit,20),qty,price:amount(i.price)};});
  const discount=amount(p.discount??0),totals=estimateTotals(items,discount,p.vat!==false);
  if(discount>totals.subtotal||totals.total>1e12)invalid('할인 또는 견적 금액을 확인해 주세요.');
  if(!['작성 중','전달 완료','승인 완료'].includes(p.status))invalid('견적 상태를 확인해 주세요.');
  return {projectId:p.projectId,title:reqstr(p.title,'견적 제목'),items,discount,vat:p.vat!==false,...totals,status:p.status,date:dateValue(p.date),validUntil:dateValue(p.validUntil),terms:opt(p.terms,5000)};
 }
 if(kind==='records'){
  if(!['시공 전','시공 중','시공 후','추가공사'].includes(p.stage))invalid('작업 단계를 선택해 주세요.');
  const files=p.files||[];if(!Array.isArray(files)||files.length>12)invalid('파일은 최대 12개까지 첨부할 수 있어요.');
  for(const f of files){identifier(f.id);if(!await repo.file(f.id))invalid('첨부 파일을 먼저 올려 주세요.');}
  const content=opt(p.content,10000);if(!content&&!files.length)invalid('기록 내용이나 파일을 추가해 주세요.');
  return {projectId:p.projectId,title:reqstr(p.title,'기록 제목'),stage:p.stage,date:dateValue(p.date),content,files:files.map(f=>({id:f.id})),extra:amount(p.extra??0),approval:['확인 대기','고객 확인'].includes(p.approval)?p.approval:'확인 대기'};
 }
 if(kind==='settings')return Object.fromEntries(['company','owner','businessNo','phone','address','bank','terms'].map(k=>[k,opt(p[k],k==='terms'?5000:500)]));
 invalid('없는 항목이에요.',404);
}
async function limitedBody(request,max){if(Number(request.headers.get('content-length'))>max)invalid('파일 또는 요청이 너무 커요.',413);if(!request.body)return new Uint8Array();const reader=request.body.getReader();const chunks=[];let size=0;while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>max){await reader.cancel();invalid('파일 또는 요청이 너무 커요.',413);}chunks.push(value);}const buffer=new Uint8Array(size);let offset=0;for(const c of chunks){buffer.set(c,offset);offset+=c.length;}return buffer;}
async function jsonBody(request){try{return JSON.parse(new TextDecoder().decode(await limitedBody(request,1024*1024)));}catch(e){if(e.status)throw e;invalid('입력 내용을 읽지 못했어요.');}}
const allowedTypes=new Set(['image/jpeg','image/png','image/webp','image/heic','image/heif','audio/webm','audio/mp4','audio/mpeg','audio/wav','audio/x-wav','audio/ogg','audio/x-m4a']);
function validBytes(b,type){const ascii=(a,z)=>String.fromCharCode(...b.slice(a,z));if(type==='image/jpeg')return b[0]===255&&b[1]===216;if(type==='image/png')return [137,80,78,71,13,10,26,10].every((n,i)=>b[i]===n);if(type==='image/webp')return ascii(0,4)==='RIFF'&&ascii(8,12)==='WEBP';if(['audio/mp4','audio/x-m4a','image/heic','image/heif'].includes(type))return ascii(4,8)==='ftyp';if(type==='audio/webm')return [26,69,223,163].every((n,i)=>b[i]===n);if(type==='audio/ogg')return ascii(0,4)==='OggS';if(type.includes('wav'))return ascii(0,4)==='RIFF'&&ascii(8,12)==='WAVE';if(type==='audio/mpeg')return ascii(0,3)==='ID3'||(b[0]===255&&(b[1]&224)===224);return false;}
const subscription=async(env,user)=>env.DB.prepare('SELECT * FROM subscriptions WHERE user_id=?').bind(user).first();
const subscriptionView=row=>entitlement(row);
async function finishCharge(env,user,row,orderId,result){
 const paymentKey=verifyCharge(result,orderId),now=new Date().toISOString(),start=row.paid_until>now?row.paid_until:now,end=nextBillingDate(start);
 await env.DB.batch([
  env.DB.prepare('UPDATE billing_orders SET status=?,payment_key=?,period_start=?,period_end=?,updated_at=? WHERE order_id=? AND user_id=?').bind('APPROVED',paymentKey,start,end,now,orderId,user),
  env.DB.prepare('UPDATE subscriptions SET status=?,paid_until=?,updated_at=? WHERE user_id=?').bind('ACTIVE',end,now,user)
 ]);
 return subscriptionView(await subscription(env,user));
}
async function tryCharge(env,user,row,orderId){
 try{
  const existing=await lookupTossOrder(env.TOSS_SECRET_KEY,orderId).catch(()=>null);
  if(existing)return finishCharge(env,user,row,orderId,existing);
  const billingKey=await decryptBillingKey(row.billing_key,env.BILLING_ENCRYPTION_KEY);
  const result=await tossRequest(env.TOSS_SECRET_KEY,`/v1/billing/${encodeURIComponent(billingKey)}`,{customerKey:row.customer_key,amount:MONTHLY_PRICE,orderId,orderName:'현장노트 월 구독료'},orderId);
  return finishCharge(env,user,row,orderId,result);
 }catch(error){
  if(error.providerCode&&error.providerCode!=='IDEMPOTENT_REQUEST_PROCESSING'){
   const failed=new Date().toISOString();await env.DB.batch([
    env.DB.prepare('UPDATE billing_orders SET status=?,updated_at=? WHERE order_id=? AND user_id=?').bind('FAILED',failed,orderId,user),
    env.DB.prepare('UPDATE subscriptions SET status=?,updated_at=? WHERE user_id=?').bind('FAILED',failed,user)
   ]);
  }
  throw error;
 }
}
async function chargeSubscription(env,user,row){
 if(row?.status==='PROCESSING'){
  const pending=await env.DB.prepare("SELECT order_id FROM billing_orders WHERE user_id=? AND status='PROCESSING' ORDER BY created_at DESC LIMIT 1").bind(user).first();
  if(!pending)invalid('결제 상태를 확인하고 있어요. 잠시 후 다시 시도해 주세요.',409);
  return tryCharge(env,user,row,pending.order_id);
 }
 if(!row?.billing_key||row.cancel_at_period_end)invalid('등록된 결제수단이 없거나 자동 갱신이 중지됐어요.',409);
 if(entitlement(row).active)return subscriptionView(row);
 const now=new Date().toISOString(),orderId='HN_'+crypto.randomUUID().replaceAll('-','');
 const claim=await env.DB.prepare('UPDATE subscriptions SET status=?,updated_at=? WHERE user_id=? AND updated_at=? AND status<>?').bind('PROCESSING',now,user,row.updated_at,'PROCESSING').run();
 if(claim.meta.changes!==1)invalid('다른 결제 요청이 진행 중이에요. 잠시 후 다시 확인해 주세요.',409);
 await env.DB.prepare('INSERT INTO billing_orders(order_id,user_id,amount,status,created_at,updated_at) VALUES(?,?,?,?,?,?)').bind(orderId,user,MONTHLY_PRICE,'PROCESSING',now,now).run();
 return tryCharge(env,user,row,orderId);
}
async function maybeRenew(env,user,row){if(row?.billing_key&&!row.cancel_at_period_end&&!entitlement(row).active&&['ACTIVE','PROCESSING'].includes(row.status)){try{return await chargeSubscription(env,user,row);}catch(e){console.error('Subscription renewal failed',e.message);}}return subscriptionView(await subscription(env,user));}
export default {async fetch(request,env){
 try{
  const url=new URL(request.url),path=url.pathname,method=request.method;
  const platformUser=request.headers.get('oai-authenticated-user-id');
  const googleUser=env.DB?await sessionUser(env,request):null;
  const ownerFallback=env.GOOGLE_CLIENT_ID&&platformUser===env.GOOGLE_OWNER_USER_ID?platformUser:null;
  const user=googleUser||ownerFallback||(!env.GOOGLE_CLIENT_ID?platformUser:null);
  if(path==='/api/session')return reply({authenticated:!!user,protected:true,googleClientId:env.GOOGLE_CLIENT_ID||null,provider:googleUser?'google':ownerFallback||!env.GOOGLE_CLIENT_ID&&platformUser?'chatgpt':null,loginUrl:env.GOOGLE_CLIENT_ID?null:'/signin-with-chatgpt?return_to=/'});
  if(path==='/api/auth/google'&&method==='POST'){
   if(!env.GOOGLE_CLIENT_ID||!env.DB)invalid('구글 로그인이 아직 설정되지 않았어요.',503);
   const body=new URLSearchParams(new TextDecoder().decode(await limitedBody(request,12000)));
   const csrf=body.get('g_csrf_token'),csrfCookie=cookie(request,'g_csrf_token');
   if(!csrf||csrf.length>200||csrf!==csrfCookie)invalid('로그인 보안 확인에 실패했어요.',403);
   let identity;try{identity=await verifyGoogleIdToken(body.get('credential'),env.GOOGLE_CLIENT_ID);}catch(e){invalid(e.message,401);}
   const ownerEmail=String(env.GOOGLE_OWNER_EMAIL||'').toLowerCase();
   const mappedOwner=ownerEmail&&identity.email?.toLowerCase()===ownerEmail&&env.GOOGLE_OWNER_USER_ID;
   const accountId=mappedOwner?env.GOOGLE_OWNER_USER_ID:`google:${identity.sub}`;
   const token=crypto.randomUUID().replaceAll('-','')+crypto.randomUUID().replaceAll('-','');
   const now=new Date(),expires=new Date(now.getTime()+30*86400000);
   await env.DB.prepare('INSERT INTO auth_sessions(token_hash,user_id,google_sub,email,expires_at,created_at) VALUES(?,?,?,?,?,?)').bind(await digest(token),accountId,identity.sub,identity.email||'',expires.toISOString(),now.toISOString()).run();
   return new Response(null,{status:303,headers:{Location:'/', 'Set-Cookie':sessionCookie(token),...securityHeaders}});
  }
  if(path==='/api/auth/logout'&&method==='POST'){
   if(request.headers.get('Origin')&&request.headers.get('Origin')!==url.origin)invalid('같은 주소에서 다시 시도해 주세요.',403);
   const token=cookie(request,'hn_auth');if(token&&env.DB)await env.DB.prepare('DELETE FROM auth_sessions WHERE token_hash=?').bind(await digest(token)).run();
   return new Response(null,{status:204,headers:{'Set-Cookie':clearSessionCookie,...securityHeaders}});
  }
  if(!user){if(path.startsWith('/api/'))return reply({error:'로그인이 필요해요.'},401);}
  if(!['GET','HEAD'].includes(method)&&request.headers.has('Origin')&&request.headers.get('Origin')!==url.origin)invalid('같은 주소에서 다시 시도해 주세요.',403);
  if(!path.startsWith('/api/')){const asset=ASSETS[path];if(!asset)return new Response('페이지를 찾을 수 없어요.',{status:404,headers:securityHeaders});return new Response(method==='HEAD'?null:asset.body,{headers:{...securityHeaders,'Content-Type':asset.type,'Cache-Control':'private, no-cache'}});}
  if(!user)return reply({error:'로그인이 필요해요.'},401);
  if(!env.DB||!env.BUCKET)invalid('저장소를 준비하고 있어요. 잠시 후 다시 시도해 주세요.',503);
  const repo=repository(env,user);
  if(path==='/api/subscription/start'&&method==='POST'){
   if(!env.TOSS_CLIENT_KEY?.startsWith('test_ck_')||!env.TOSS_SECRET_KEY?.startsWith('test_sk_')||!env.BILLING_ENCRYPTION_KEY)invalid('테스트 결제 설정을 준비하고 있어요.',503);
   let row=await subscription(env,user);
   if(!row){const now=new Date().toISOString();await env.DB.prepare('INSERT OR IGNORE INTO subscriptions(user_id,customer_key,status,updated_at) VALUES(?,?,?,?)').bind(user,crypto.randomUUID(),'PENDING',now).run();row=await subscription(env,user);}
   return reply({clientKey:env.TOSS_CLIENT_KEY,customerKey:row.customer_key,registered:!!row.billing_key,subscription:subscriptionView(row)});
  }
  if(path==='/api/subscription/activate'&&method==='POST'){
   let row=await subscription(env,user);const p=await jsonBody(request);validateBillingReturn(p,row);
   if(!row.billing_key){
    const issued=await tossRequest(env.TOSS_SECRET_KEY,'/v1/billing/authorizations/issue',{authKey:p.authKey,customerKey:row.customer_key},`issue-${row.customer_key}-${p.authKey.slice(0,80)}`);
    if(!issued.billingKey||issued.customerKey!==row.customer_key)invalid('발급된 결제수단을 확인하지 못했어요.',502);
    const sealed=await encryptBillingKey(issued.billingKey,env.BILLING_ENCRYPTION_KEY),now=new Date().toISOString();
    await env.DB.prepare('UPDATE subscriptions SET billing_key=?,status=?,cancel_at_period_end=0,updated_at=? WHERE user_id=?').bind(sealed,'PENDING',now,user).run();
    row=await subscription(env,user);
   }
   return reply({subscription:await chargeSubscription(env,user,row)});
  }
  if(path==='/api/subscription/retry'&&method==='POST')return reply({subscription:await chargeSubscription(env,user,await subscription(env,user))});
  if(path==='/api/subscription/cancel'&&method==='POST'){
   const row=await subscription(env,user);if(!row?.billing_key)invalid('진행 중인 구독이 없어요.',404);
   await env.DB.prepare('UPDATE subscriptions SET cancel_at_period_end=1,updated_at=? WHERE user_id=?').bind(new Date().toISOString(),user).run();
   return reply({subscription:subscriptionView(await subscription(env,user))});
  }
  if(path==='/api/subscription/resume'&&method==='POST'){
   const row=await subscription(env,user);if(!row?.billing_key)invalid('등록된 결제수단이 없어요.',404);
   await env.DB.prepare('UPDATE subscriptions SET cancel_at_period_end=0,updated_at=? WHERE user_id=?').bind(new Date().toISOString(),user).run();
   return reply({subscription:subscriptionView(await subscription(env,user))});
  }
  if(path==='/api/state'&&method==='GET'){
   const [projects,estimates,records,settings,files,billing]=await Promise.all([repo.many('projects'),repo.many('estimates'),repo.many('records'),repo.one('settings','business'),env.DB.prepare('SELECT id,name,mime,size,created_at FROM files WHERE user_id=?').bind(user).all(),subscription(env,user)]);
   const fileMap=new Map(files.results.map(f=>[f.id,f]));
   return reply({projects,estimates,records:records.map(r=>({...r,files:r.files.map(f=>fileMap.get(f.id)).filter(Boolean)})),settings:settings||{id:'business',revision:0,company:'',owner:'',phone:'',businessNo:'',address:'',bank:'',terms:''},subscription:await maybeRenew(env,user,billing)});
  }
  if(path==='/api/files'&&method==='POST'){
   if(!entitlement(await subscription(env,user)).active)invalid('월 구독 후 사진과 음성 기록을 저장할 수 있어요.',402);
   const mime=(request.headers.get('Content-Type')||'').split(';')[0];if(!allowedTypes.has(mime))invalid('지원하는 사진 또는 음성 파일을 선택해 주세요.');
   const bytes=await limitedBody(request,15*1024*1024);if(bytes.length<8||!validBytes(bytes,mime))invalid('파일 형식을 확인해 주세요.');
   let name;try{name=decodeURIComponent(request.headers.get('X-File-Name')||'첨부파일');}catch{invalid('파일 이름을 확인해 주세요.');}name=opt(name,240);
   const key=crypto.randomUUID();await env.BUCKET.put(key,bytes,{httpMetadata:{contentType:mime}});
   try{await env.DB.prepare('INSERT INTO files(id,user_id,name,mime,size,created_at) VALUES(?,?,?,?,?,?)').bind(key,user,name,mime,bytes.length,new Date().toISOString()).run();}catch(e){await env.BUCKET.delete(key);throw e;}
   return reply({id:key,name,mime,size:bytes.length},201);
  }
  const fileMatch=path.match(/^\/api\/files\/([a-zA-Z0-9-]+)$/);
  if(fileMatch&&method==='GET'){
   const f=await repo.file(fileMatch[1]);if(!f)invalid('파일을 찾을 수 없어요.',404);
   const requested=request.headers.get('Range');let range=null;
   if(requested){const m=requested.match(/^bytes=(\d+)-(\d*)$/);if(m){const start=Number(m[1]),end=m[2]?Math.min(Number(m[2]),f.size-1):f.size-1;if(start>=f.size||end<start)return new Response(null,{status:416,headers:{'Content-Range':`bytes */${f.size}`}});range={offset:start,length:end-start+1};}}
   const file=await env.BUCKET.get(f.id,range?{range}:undefined);if(!file)invalid('첨부 파일을 읽지 못했어요.',404);
   const headers={...securityHeaders,'Content-Type':f.mime,'Content-Length':String(range?range.length:f.size),'Cache-Control':'private, max-age=3600','Accept-Ranges':'bytes','Content-Disposition':`inline; filename*=UTF-8''${encodeURIComponent(f.name)}`};
   if(range)headers['Content-Range']=`bytes ${range.offset}-${range.offset+range.length-1}/${f.size}`;
   return new Response(file.body,{status:range?206:200,headers});
  }
  const match=path.match(/^\/api\/(projects|estimates|records|settings)(?:\/([a-zA-Z0-9-]+))?$/);
  if(match&&['POST','PUT'].includes(method)){
   const [,kind,key]=match;if(method==='PUT'&&!key||method==='POST'&&key)invalid('올바른 요청 주소가 아니에요.');
   if(kind!=='settings'&&!entitlement(await subscription(env,user)).active)invalid('월 구독 후 현장·견적·기록을 저장할 수 있어요.',402);
   const p=await jsonBody(request);const target=kind==='settings'?'business':key||crypto.randomUUID();const old=await repo.one(kind,target);
   if(method==='PUT'&&!old&&kind!=='settings')invalid('항목을 찾을 수 없어요.',404);
   if((old?.revision||0)!==(p.revision||0))invalid('다른 창에서 수정된 내용이 있어요. 입력 내용을 복사한 뒤 새로고침해 주세요.',409);
   const clean=await validated(kind,p,repo);const revision=(old?.revision||0)+1;const updatedAt=new Date().toISOString();
   let result;
   if(old)result=await env.DB.prepare('UPDATE documents SET body=?,revision=?,updated_at=? WHERE user_id=? AND kind=? AND id=? AND revision=?').bind(JSON.stringify(clean),revision,updatedAt,user,kind,target,old.revision).run();
   else result=await env.DB.prepare('INSERT OR IGNORE INTO documents(user_id,kind,id,body,revision,updated_at) VALUES(?,?,?,?,?,?)').bind(user,kind,target,JSON.stringify(clean),revision,updatedAt).run();
   if(result.meta.changes!==1)invalid('다른 창에서 수정된 내용이 있어요. 새로고침한 뒤 다시 시도해 주세요.',409);
   return reply({...clean,id:target,revision,updatedAt},old?200:201);
  }
  invalid('요청한 기능을 찾을 수 없어요.',404);
 }catch(e){if(!e.status)console.error('Storage request failed',e.message);return reply({error:e.status?e.message:'저장하지 못했어요. 입력을 유지하고 잠시 후 다시 시도해 주세요.'},e.status||503);}
},async scheduled(event,env,ctx){
 const due=await env.DB.prepare("SELECT * FROM subscriptions WHERE status='ACTIVE' AND cancel_at_period_end=0 AND billing_key IS NOT NULL AND paid_until<=? LIMIT 50").bind(new Date().toISOString()).all();
 ctx.waitUntil(Promise.allSettled(due.results.map(row=>chargeSubscription(env,row.user_id,row))));
}};
