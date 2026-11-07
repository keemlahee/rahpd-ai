import http from 'node:http';
import { DatabaseSync } from 'node:sqlite';
import { mkdirSync, readFileSync, writeFileSync, existsSync, statSync, createReadStream } from 'node:fs';
import { dirname, resolve, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID, randomBytes, timingSafeEqual, createHash } from 'node:crypto';
import { estimateTotals, statuses } from './public/domain.js';
import { MONTHLY_PRICE,nextBillingDate,entitlement,validateBillingReturn,tossRequest,lookupTossOrder,verifyCharge,encryptBillingKey,decryptBillingKey } from './billing-core.mjs';

const root = dirname(fileURLToPath(import.meta.url));
const dataDir = resolve(process.env.DATA_DIR || join(root, 'data'));
mkdirSync(join(dataDir, 'files'), { recursive: true });
const db = new DatabaseSync(join(dataDir, 'hyeonjang.sqlite'));
db.exec(`PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS documents (kind TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subscriptions (user_id TEXT PRIMARY KEY, customer_key TEXT NOT NULL UNIQUE, billing_key TEXT, paid_until TEXT, status TEXT NOT NULL DEFAULT 'PENDING', cancel_at_period_end INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS billing_orders (order_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, amount INTEGER NOT NULL, status TEXT NOT NULL, payment_key TEXT, period_start TEXT, period_end TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_billing_orders_user_created ON billing_orders(user_id,created_at);`);
const host = process.env.HOST || '127.0.0.1';
const port = Number(process.env.PORT || 4173);
const password = process.env.APP_PASSWORD || '';
if (!['127.0.0.1','localhost','::1'].includes(host) && password.length < 12) throw new Error('외부 접속에는 12자 이상의 APP_PASSWORD가 필요합니다.');
const sessions = new Map();
const attempts = new Map();
const json = (res, code, value) => { res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control':'no-store' }); res.end(JSON.stringify(value)); };
function fail(message, status=400) { throw Object.assign(new Error(message), { status }); }
function text(value, max=1000) { if (typeof value !== 'string' || value.length > max) fail('입력한 글자 수를 확인해 주세요.'); return value.trim(); }
function optional(value, max=1000) { return text(value ?? '', max); }
function required(value, label, max=200) { const v = text(value, max); if (!v) fail(`${label}을 입력해 주세요.`); return v; }
function number(value, max=1e12) { if (typeof value !== 'number' || !Number.isFinite(value) || value < 0 || value > max) fail('수량과 금액은 허용 범위 안의 숫자로 입력해 주세요.'); return value; }
function money(value) { const v=number(value); if (!Number.isSafeInteger(v)) fail('금액은 원 단위 정수로 입력해 주세요.'); return v; }
function date(value) { if (!value) return ''; if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(Date.parse(value)) || new Date(value).toISOString().slice(0,10)!==value) fail('날짜를 확인해 주세요.'); return value; }
function id(value) { if (!/^[a-zA-Z0-9-]{1,80}$/.test(value || '')) fail('항목 번호가 올바르지 않아요.'); return value; }
function get(kind, key) { const r = db.prepare('SELECT * FROM documents WHERE kind=? AND id=?').get(kind,key); return r ? {...JSON.parse(r.body), id:r.id, revision:r.revision, updatedAt:r.updated_at} : null; }
const subscription=()=>db.prepare("SELECT * FROM subscriptions WHERE user_id='local'").get();
function finishCharge(row,orderId,result){
 const paymentKey=verifyCharge(result,orderId),now=new Date().toISOString(),start=row.paid_until>now?row.paid_until:now,end=nextBillingDate(start);
 db.exec('BEGIN IMMEDIATE');try{db.prepare("UPDATE billing_orders SET status='APPROVED',payment_key=?,period_start=?,period_end=?,updated_at=? WHERE order_id=?").run(paymentKey,start,end,now,orderId);db.prepare("UPDATE subscriptions SET status='ACTIVE',paid_until=?,updated_at=? WHERE user_id='local'").run(end,now);db.exec('COMMIT');}catch(e){db.exec('ROLLBACK');throw e;}
 return entitlement(subscription());
}
async function tryCharge(row,orderId){
 try{
  const existing=await lookupTossOrder(process.env.TOSS_SECRET_KEY,orderId).catch(()=>null);
  if(existing)return finishCharge(row,orderId,existing);
  const key=await decryptBillingKey(row.billing_key,process.env.BILLING_ENCRYPTION_KEY);
  const result=await tossRequest(process.env.TOSS_SECRET_KEY,`/v1/billing/${encodeURIComponent(key)}`,{customerKey:row.customer_key,amount:MONTHLY_PRICE,orderId,orderName:'현장노트 월 구독료'},orderId);
  return finishCharge(row,orderId,result);
 }catch(e){if(e.providerCode&&e.providerCode!=='IDEMPOTENT_REQUEST_PROCESSING'){const failed=new Date().toISOString();db.prepare("UPDATE billing_orders SET status='FAILED',updated_at=? WHERE order_id=?").run(failed,orderId);db.prepare("UPDATE subscriptions SET status='FAILED',updated_at=? WHERE user_id='local'").run(failed);}throw e;}
}
async function chargeSubscription(){
 const row=subscription();if(row?.status==='PROCESSING'){const pending=db.prepare("SELECT order_id FROM billing_orders WHERE user_id='local' AND status='PROCESSING' ORDER BY created_at DESC LIMIT 1").get();if(!pending)fail('결제 상태를 확인하고 있어요.',409);return tryCharge(row,pending.order_id);}
 if(!row?.billing_key||row.cancel_at_period_end)fail('등록된 결제수단이 없거나 자동 갱신이 중지됐어요.',409);
 if(entitlement(row).active)return entitlement(row);
 const now=new Date().toISOString(),orderId='HN_'+randomUUID().replaceAll('-','');
 const claim=db.prepare("UPDATE subscriptions SET status='PROCESSING',updated_at=? WHERE user_id='local' AND updated_at=? AND status<>'PROCESSING'").run(now,row.updated_at);
 if(claim.changes!==1)fail('다른 결제 요청이 진행 중이에요.',409);
 db.prepare("INSERT INTO billing_orders(order_id,user_id,amount,status,created_at,updated_at) VALUES(?,'local',?,'PROCESSING',?,?)").run(orderId,MONTHLY_PRICE,now,now);
 return tryCharge(row,orderId);
}
async function maybeRenew(){const row=subscription();if(row?.billing_key&&!row.cancel_at_period_end&&!entitlement(row).active&&['ACTIVE','PROCESSING'].includes(row.status))try{return await chargeSubscription();}catch(e){console.error('Subscription renewal failed',e.message);}return entitlement(subscription());}
function list(kind) { return db.prepare('SELECT * FROM documents WHERE kind=? ORDER BY updated_at DESC,id').all(kind).map(r=>({...JSON.parse(r.body),id:r.id,revision:r.revision,updatedAt:r.updated_at})); }
function validate(kind, p) {
  if (!p || typeof p !== 'object' || Array.isArray(p)) fail('입력 형식을 확인해 주세요.');
  if (kind==='projects') {
    if (!statuses.includes(p.status)) fail('진행 상태를 선택해 주세요.');
    if (!['주거','상가'].includes(p.type)) fail('현장 유형을 선택해 주세요.');
    if (p.time && !/^([01]\d|2[0-3]):[0-5]\d$/.test(p.time)) fail('방문 시간을 확인해 주세요.');
    return { name:required(p.name,'현장명'), customer:optional(p.customer,100), phone:optional(p.phone,40), address:optional(p.address,500), type:p.type,status:p.status,date:date(p.date),time:optional(p.time,5), scope:optional(p.scope,3000), nextAction:optional(p.nextAction,500), contract:money(p.contract||0),received:money(p.received||0),cost:money(p.cost||0),hours:number(p.hours||0,100000) };
  }
  if (kind==='estimates' || kind==='records') {
    id(p.projectId); if (!get('projects',p.projectId)) fail('먼저 현장을 추가해 주세요.',404);
  }
  if (kind==='estimates') {
    if (!Array.isArray(p.items) || !p.items.length || p.items.length>100) fail('견적 항목은 1~100개까지 입력할 수 있어요.');
    const items=p.items.map(i=>{const qty=number(i.qty,1000000);if(qty<=0)fail('수량은 0보다 크게 입력해 주세요.');return {name:required(i.name,'품목명'),spec:optional(i.spec,200),unit:optional(i.unit,20),qty,price:money(i.price)};});
    const discount=money(p.discount||0), totals=estimateTotals(items,discount,p.vat!==false);
    if (discount>totals.subtotal || totals.total>1e12) fail('할인 또는 견적 금액을 확인해 주세요.');
    if (!['작성 중','전달 완료','승인 완료'].includes(p.status)) fail('견적 상태를 확인해 주세요.');
    return { projectId:p.projectId,title:required(p.title,'견적 제목'),items,discount,vat:p.vat!==false,...totals,status:p.status,date:date(p.date),validUntil:date(p.validUntil),terms:optional(p.terms,5000) };
  }
  if (kind==='records') {
    if (!['시공 전','시공 중','시공 후','추가공사'].includes(p.stage)) fail('작업 단계를 선택해 주세요.');
    const files=p.files||[];
    if (!Array.isArray(files)||files.length>12) fail('파일은 최대 12개까지 첨부할 수 있어요.');
    files.forEach(f=>{ id(f.id); if(!db.prepare('SELECT id FROM files WHERE id=?').get(f.id)) fail('첨부 파일을 먼저 올려 주세요.'); });
    const content=optional(p.content,10000); if(!content&&!files.length) fail('기록 내용 또는 파일을 추가해 주세요.');
    return {projectId:p.projectId,title:required(p.title,'기록 제목'),stage:p.stage,date:date(p.date),content,files:files.map(f=>({id:f.id})),extra:money(p.extra||0),approval:['확인 대기','고객 확인'].includes(p.approval)?p.approval:'확인 대기'};
  }
  if(kind==='settings') return Object.fromEntries(['company','owner','businessNo','phone','address','bank','terms'].map(k=>[k,optional(p[k], k==='terms'?5000:500)]));
  fail('없는 항목이에요.',404);
}
async function body(req, max=1024*1024) { let size=0;const chunks=[];for await(const c of req){size+=c.length;if(size>max)fail('파일 또는 요청이 너무 커요.',413);chunks.push(c);}return Buffer.concat(chunks); }
async function readJson(req) { try{return JSON.parse((await body(req)).toString('utf8'));}catch(e){if(e.status)throw e;fail('입력 내용을 읽지 못했어요.');} }
function authenticated(req) { if(!password)return true;const sid=(req.headers.cookie||'').split('; ').find(s=>s.startsWith('hn_session='))?.slice(11);const exp=sessions.get(sid);if(exp>Date.now())return true;if(sid)sessions.delete(sid);return false; }
function safeEqual(a,b){return timingSafeEqual(createHash('sha256').update(a).digest(),createHash('sha256').update(b).digest());}
const MIME={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml','.json':'application/json','.webmanifest':'application/manifest+json'};
const uploads=new Set(['image/jpeg','image/png','image/webp','image/heic','image/heif','audio/webm','audio/mp4','audio/mpeg','audio/wav','audio/x-wav','audio/ogg','audio/x-m4a']);
function validFile(buf,type) { const ascii=(a,b)=>buf.subarray(a,b).toString('ascii'); if(type==='image/jpeg')return buf[0]===255&&buf[1]===216; if(type==='image/png')return buf.subarray(0,8).equals(Buffer.from([137,80,78,71,13,10,26,10]));if(type==='image/webp')return ascii(0,4)==='RIFF'&&ascii(8,12)==='WEBP'; if(['audio/mp4','audio/x-m4a','image/heic','image/heif'].includes(type))return ascii(4,8)==='ftyp';if(type==='audio/webm')return buf.subarray(0,4).equals(Buffer.from([26,69,223,163]));if(type==='audio/ogg')return ascii(0,4)==='OggS';if(type.includes('wav'))return ascii(0,4)==='RIFF'&&ascii(8,12)==='WAVE';if(type==='audio/mpeg')return ascii(0,3)==='ID3'||(buf[0]===255&&(buf[1]&224)===224);return false; }
const server = http.createServer(async(req,res)=>{
  res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('Referrer-Policy','same-origin');res.setHeader('X-Frame-Options','DENY');
  res.setHeader('Content-Security-Policy',"default-src 'self'; script-src 'self' https://js.tosspayments.com; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data: https://*.tosspayments.com; media-src 'self' blob:; connect-src 'self' https://*.tosspayments.com; frame-src https://*.tosspayments.com; font-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://*.tosspayments.com");
  try {
    const base=`http://${req.headers.host}`;const url=new URL(req.url,base);const path=url.pathname;const method=req.method;
    if(!password&&!['127.0.0.1','localhost','[::1]'].includes(url.hostname))fail('로컬 주소로 접속해 주세요.',403);
    if(!['GET','HEAD'].includes(method)&&req.headers.origin&&new URL(req.headers.origin).host!==req.headers.host)fail('같은 주소에서 다시 시도해 주세요.',403);
    if(path==='/api/session'&&method==='GET')return json(res,200,{authenticated:authenticated(req),protected:!!password});
    if(path==='/api/login'&&method==='POST'){
      const addr=req.socket.remoteAddress;const a=attempts.get(addr)||{n:0,until:Date.now()+600000};if(a.until<Date.now()){a.n=0;a.until=Date.now()+600000;}if(a.n>=10)fail('잠시 후 다시 시도해 주세요.',429);
      const p=await readJson(req);if(!password||!safeEqual(String(p.password||''),password)){a.n++;attempts.set(addr,a);fail('비밀번호를 확인해 주세요.',401);}
      attempts.delete(addr);const sid=randomBytes(32).toString('hex');sessions.set(sid,Date.now()+43200000);res.setHeader('Set-Cookie',`hn_session=${sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200`);return json(res,200,{ok:true});
    }
    if(path.startsWith('/api/')&&!authenticated(req))fail('로그인이 필요해요.',401);
    if(path==='/api/subscription/start'&&method==='POST'){
      if(!process.env.TOSS_CLIENT_KEY?.startsWith('test_ck_')||!process.env.TOSS_SECRET_KEY?.startsWith('test_sk_')||!process.env.BILLING_ENCRYPTION_KEY)fail('테스트 결제 설정을 준비하고 있어요.',503);
      let row=subscription();if(!row){db.prepare("INSERT OR IGNORE INTO subscriptions(user_id,customer_key,status,updated_at) VALUES('local',?,'PENDING',?)").run(randomUUID(),new Date().toISOString());row=subscription();}
      return json(res,200,{clientKey:process.env.TOSS_CLIENT_KEY,customerKey:row.customer_key,registered:!!row.billing_key,subscription:entitlement(row)});
    }
    if(path==='/api/subscription/activate'&&method==='POST'){
      let row=subscription(),p=await readJson(req);validateBillingReturn(p,row);
      if(!row.billing_key){const issued=await tossRequest(process.env.TOSS_SECRET_KEY,'/v1/billing/authorizations/issue',{authKey:p.authKey,customerKey:row.customer_key},`issue-${row.customer_key}-${p.authKey.slice(0,80)}`);if(!issued.billingKey||issued.customerKey!==row.customer_key)fail('발급된 결제수단을 확인하지 못했어요.',502);const sealed=await encryptBillingKey(issued.billingKey,process.env.BILLING_ENCRYPTION_KEY);db.prepare("UPDATE subscriptions SET billing_key=?,status='PENDING',cancel_at_period_end=0,updated_at=? WHERE user_id='local'").run(sealed,new Date().toISOString());}
      return json(res,200,{subscription:await chargeSubscription()});
    }
    if(path==='/api/subscription/retry'&&method==='POST')return json(res,200,{subscription:await chargeSubscription()});
    if(path==='/api/subscription/cancel'&&method==='POST'){if(!subscription()?.billing_key)fail('진행 중인 구독이 없어요.',404);db.prepare("UPDATE subscriptions SET cancel_at_period_end=1,updated_at=? WHERE user_id='local'").run(new Date().toISOString());return json(res,200,{subscription:entitlement(subscription())});}
    if(path==='/api/subscription/resume'&&method==='POST'){if(!subscription()?.billing_key)fail('등록된 결제수단이 없어요.',404);db.prepare("UPDATE subscriptions SET cancel_at_period_end=0,updated_at=? WHERE user_id='local'").run(new Date().toISOString());return json(res,200,{subscription:entitlement(subscription())});}
    if(path==='/api/state'&&method==='GET'){
      const fileMap=new Map(db.prepare('SELECT * FROM files').all().map(f=>[f.id,f]));
      return json(res,200,{projects:list('projects'),estimates:list('estimates'),records:list('records').map(r=>({...r,files:r.files.map(f=>fileMap.get(f.id)).filter(Boolean)})),settings:get('settings','business')||{id:'business',revision:0,company:'',owner:'',businessNo:'',phone:'',address:'',bank:'',terms:''},subscription:await maybeRenew()});
    }
    if(path==='/api/files'&&method==='POST'){
      if(!entitlement(subscription()).active)fail('월 구독 후 사진과 음성 기록을 저장할 수 있어요.',402);
      const mime=(req.headers['content-type']||'').split(';')[0];if(!uploads.has(mime))fail('사진(JPG·PNG·WebP·HEIC) 또는 음성 파일을 선택해 주세요.');
      const bytes=await body(req,15*1024*1024);if(bytes.length<8||!validFile(bytes,mime))fail('파일 형식을 확인해 주세요.');
      const key=randomUUID();const name=optional(decodeURIComponent(req.headers['x-file-name']||'첨부파일'),240);
      writeFileSync(join(dataDir,'files',key),bytes,{flag:'wx'});db.prepare('INSERT INTO files VALUES (?,?,?,?,?)').run(key,name,mime,bytes.length,new Date().toISOString());return json(res,201,{id:key,name,mime,size:bytes.length});
    }
    const fm=path.match(/^\/api\/files\/([a-zA-Z0-9-]+)$/);
    if(fm&&method==='GET'){
      const f=db.prepare('SELECT * FROM files WHERE id=?').get(fm[1]);if(!f)fail('파일을 찾을 수 없어요.',404);
      res.writeHead(200,{'Content-Type':f.mime,'Content-Length':f.size,'Cache-Control':'private, max-age=3600','Content-Disposition':`inline; filename*=UTF-8''${encodeURIComponent(f.name)}`});return createReadStream(join(dataDir,'files',f.id)).pipe(res);
    }
    const match=path.match(/^\/api\/(projects|estimates|records|settings)(?:\/([a-zA-Z0-9-]+))?$/);
    if(match&&['POST','PUT'].includes(method)){
      const [,kind,key]=match;const p=await readJson(req);if(method==='PUT'&&!key)fail('수정할 항목을 선택해 주세요.');if(method==='POST'&&key)fail('올바른 주소가 아니에요.');
      if(kind!=='settings'&&!entitlement(subscription()).active)fail('월 구독 후 현장·견적·기록을 저장할 수 있어요.',402);
      const target=kind==='settings'?'business':key||randomUUID();const old=get(kind,target);
      if(method==='PUT'&&!old&&kind!=='settings')fail('항목을 찾을 수 없어요.',404);
      if((old?.revision||0)!==(p.revision||0))fail('다른 창에서 수정된 내용이 있어요. 입력 내용을 복사한 뒤 새로고침해 주세요.',409);
      const cleaned=validate(kind,p);const revision=(old?.revision||0)+1;const updatedAt=new Date().toISOString();
      db.prepare('INSERT INTO documents(kind,id,body,revision,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET body=excluded.body,revision=excluded.revision,updated_at=excluded.updated_at').run(kind,target,JSON.stringify(cleaned),revision,updatedAt);
      return json(res,old?200:201,{...cleaned,id:target,revision,updatedAt});
    }
    if(path.startsWith('/api/'))fail('요청한 기능을 찾을 수 없어요.',404);
    if(method!=='GET'&&method!=='HEAD')fail('지원하지 않는 요청이에요.',405);
    const publicDir=join(root,'public');const relative=decodeURIComponent(path==='/'?'/index.html':path);const file=resolve(publicDir,'.'+relative);
    if(!file.startsWith(publicDir+ '\\')&&!file.startsWith(publicDir+'/'))fail('없는 주소예요.',404);
    if(!existsSync(file)||!statSync(file).isFile())fail('없는 주소예요.',404);
    res.writeHead(200,{'Content-Type':MIME[extname(file)]||'application/octet-stream','Cache-Control':'no-cache'});if(method==='HEAD')return res.end();createReadStream(file).pipe(res);
  }catch(e){ if(!res.headersSent)json(res,e.status||500,{error:e.status?e.message:'저장하지 못했어요. 연결 상태를 확인하고 다시 시도해 주세요.'});else res.end();if(!e.status)console.error(e); }
});
server.listen(port,host,()=>console.log(`현장노트 Local: http://${host}:${port}\nData: ${dataDir}`));
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>server.close(()=>{db.close();process.exit(0);}));
