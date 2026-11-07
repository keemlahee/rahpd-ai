import test from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {estimateTotals,escapeHtml} from '../public/domain.js';
import {MONTHLY_PRICE,nextBillingDate,entitlement,validateBillingReturn,tossRequest,verifyCharge,encryptBillingKey,decryptBillingKey} from '../billing-core.mjs';
import worker from '../worker.mjs';
import {verifyGoogleIdToken} from '../google-auth.mjs';

function environment({paid=true}={}){
 const sqlite=new DatabaseSync(':memory:');
 sqlite.exec(readFileSync(new URL('../drizzle/0000_faulty_maelstrom.sql',import.meta.url),'utf8'));
 sqlite.exec(readFileSync(new URL('../drizzle/0001_loving_agent_zero.sql',import.meta.url),'utf8'));
 sqlite.exec(readFileSync(new URL('../drizzle/0002_lovely_salo.sql',import.meta.url),'utf8'));
 if(paid){const stmt=sqlite.prepare("INSERT INTO subscriptions(user_id,customer_key,billing_key,paid_until,status,updated_at) VALUES(?,?,NULL,'2099-01-01T00:00:00.000Z','ACTIVE','2026-09-24T00:00:00.000Z')");stmt.run('owner','test-customer');stmt.run('another-user','another-customer');}
 const blobs=new Map();
 const DB={prepare(sql){let args=[];const stmt=()=>sqlite.prepare(sql);return {bind(...a){args=a;return this;},async first(){return stmt().get(...args)||null;},async all(){return {results:stmt().all(...args)};},async run(){const r=stmt().run(...args);return {meta:{changes:Number(r.changes)}};}};},async batch(stmts){return Promise.all(stmts.map(s=>s.run()));}};
 const BUCKET={async put(key,bytes){blobs.set(key,new Uint8Array(bytes));},async get(key,options){const bytes=blobs.get(key);if(!bytes)return null;const part=options?.range?bytes.slice(options.range.offset,options.range.offset+options.range.length):bytes;return {body:part};},async delete(key){blobs.delete(key);}};
 return {DB,BUCKET,sqlite,close:()=>sqlite.close()};
}
const project={name:'검증용 배관 현장',customer:'테스트 고객',type:'주거',status:'상담 중',date:'2026-09-23',time:'09:30',scope:'급수 배관 교체',contract:0,received:0,cost:0,hours:0};
function request(env,path,{method='GET',data,user='owner',headers={},body}={}){return worker.fetch(new Request('https://example.com/api/'+path,{method,headers:{...(user?{'oai-authenticated-user-id':user}:{}),...(data?{'Content-Type':'application/json'}:{}),...headers},body:data?JSON.stringify(data):body}),env);}

test('견적: 소수 수량, 할인, 원 단위 부가세 및 비과세 계산',()=>{
 assert.deepEqual(estimateTotals([{qty:2.5,price:18000},{qty:1,price:33333}],1000,true),{subtotal:78333,supply:77333,tax:7733,total:85066});
 assert.equal(estimateTotals([{qty:2,price:100}],0,false).total,200);
 assert.equal(estimateTotals([{qty:1,price:100}],100,true).total,0);
});
test('HTML 입력값은 문자가 되며 실행 코드가 되지 않는다',()=>assert.equal(escapeHtml('<img src=x onerror="alert(1)">'), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;'));
test('로그인 누락 차단 및 교차 출처 수정 차단',async()=>{
 const env=environment();try{
  assert.equal((await request(env,'state',{user:null})).status,401);
  assert.equal((await request(env,'projects',{method:'POST',data:project,headers:{Origin:'https://other.example'}})).status,403);
  assert.equal((await request(env,'session',{user:null})).status,200);
 }finally{env.close();}
});
test('구글 ID 토큰 서명·대상 확인 후 기존 소유자 계정에 연결한다',async()=>{
 const env=environment();const priorFetch=globalThis.fetch;
 try{
  const keys=await crypto.subtle.generateKey({name:'RSASSA-PKCS1-v1_5',modulusLength:2048,publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'},true,['sign','verify']);
  const jwk=await crypto.subtle.exportKey('jwk',keys.publicKey);
  globalThis.fetch=async()=>new Response(JSON.stringify({keys:[{...jwk,kid:'test-key',use:'sig'}]}),{headers:{'Cache-Control':'max-age=60'}});
  const encode=v=>Buffer.from(JSON.stringify(v)).toString('base64url');
  const header=encode({alg:'RS256',kid:'test-key'}),payload=encode({iss:'https://accounts.google.com',aud:'123-example.apps.googleusercontent.com',sub:'123456789012345678901',email:'owner@example.com',email_verified:true,iat:Math.floor(Date.now()/1000),exp:Math.floor(Date.now()/1000)+3600});
  const signed=header+'.'+payload;const signature=Buffer.from(await crypto.subtle.sign('RSASSA-PKCS1-v1_5',keys.privateKey,new TextEncoder().encode(signed))).toString('base64url');
  const token=signed+'.'+signature;
  await assert.rejects(verifyGoogleIdToken(token,'wrong-client'));
  env.GOOGLE_CLIENT_ID='123-example.apps.googleusercontent.com';env.GOOGLE_OWNER_EMAIL='owner@example.com';env.GOOGLE_OWNER_USER_ID='owner';
  assert.equal((await(await request(env,'session')).json()).authenticated,true);
  assert.equal((await(await request(env,'session',{user:'another-user'})).json()).authenticated,false);
  const form=new URLSearchParams({credential:token,g_csrf_token:'nonce123'});
  const url='https://example.com/api/auth/google';
  const bad=await worker.fetch(new Request(url,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded',Cookie:'g_csrf_token=wrong',Origin:'https://example.com'},body:form}),env);
  assert.equal(bad.status,403);
  const signedIn=await worker.fetch(new Request(url,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded',Cookie:'g_csrf_token=nonce123',Origin:'https://accounts.google.com'},body:form}),env);
  assert.equal(signedIn.status,303);
  const sessionCookie=signedIn.headers.get('Set-Cookie').split(';')[0];
  const session=await(await worker.fetch(new Request('https://example.com/api/session',{headers:{Cookie:sessionCookie}}),env)).json();
  assert.equal(session.authenticated,true);assert.equal(session.provider,'google');
  const state=await(await worker.fetch(new Request('https://example.com/api/state',{headers:{Cookie:sessionCookie}}),env)).json();
  assert.equal(state.subscription.active,true);
  await worker.fetch(new Request('https://example.com/api/auth/logout',{method:'POST',headers:{Cookie:sessionCookie,Origin:'https://example.com'}}),env);
  assert.equal((await(await worker.fetch(new Request('https://example.com/api/session',{headers:{Cookie:sessionCookie}}),env)).json()).authenticated,false);
 }finally{globalThis.fetch=priorFetch;env.close();}
});
test('현장 저장·재조회, 사용자 격리, 수정 충돌 방지',async()=>{
 const env=environment();try{
  const r=await request(env,'projects',{method:'POST',data:project});assert.equal(r.status,201);const saved=await r.json();
  const state=await(await request(env,'state')).json();assert.equal(state.projects[0].name,project.name);
  const other=await(await request(env,'state',{user:'another-user'})).json();assert.equal(other.projects.length,0);
  assert.equal((await request(env,'projects/'+saved.id,{method:'PUT',data:{...saved,status:'시공 중'}})).status,200);
  assert.equal((await request(env,'projects/'+saved.id,{method:'PUT',data:{...saved,status:'완료'}})).status,409);
  assert.equal((await request(env,'projects/'+saved.id,{method:'PUT',data:saved,user:'another-user'})).status,404);
 }finally{env.close();}
});
test('견적 합계는 서버에서 재계산하고 초안을 계약금에 더하지 않는다',async()=>{
 const env=environment();try{
  const p=await(await request(env,'projects',{method:'POST',data:project})).json();
  const quote={projectId:p.id,title:'수도 배관 견적',items:[{name:'급수관',qty:30,price:18000,unit:'m',spec:'PB 15A'}],discount:40000,vat:true,status:'작성 중',date:'2026-09-23',total:1};
  const q=await request(env,'estimates',{method:'POST',data:quote});assert.equal(q.status,201);assert.equal((await q.json()).total,550000);
  const state=await(await request(env,'state')).json();assert.equal(state.projects[0].contract,0);
  assert.equal((await request(env,'estimates',{method:'POST',data:{...quote,discount:900000}})).status,400);
  assert.equal((await request(env,'estimates',{method:'POST',data:{...quote,items:[{name:'급수관',qty:0,price:100}]}})).status,400);
  assert.equal((await request(env,'estimates',{method:'POST',data:quote,user:'another-user'})).status,404);
 }finally{env.close();}
});
test('사진 업로드·기록 연결·다운로드 및 파일 소유권 검증',async()=>{
 const env=environment();try{
  const p=await(await request(env,'projects',{method:'POST',data:project})).json();
  const bytes=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jC00AAAAASUVORK5CYII=','base64');
  const upload=await request(env,'files',{method:'POST',body:bytes,headers:{'Content-Type':'image/png','X-File-Name':encodeURIComponent('검증 사진.png')}});assert.equal(upload.status,201);const f=await upload.json();
  assert.equal((await request(env,'files/'+f.id)).status,200);
  assert.deepEqual(new Uint8Array(await(await request(env,'files/'+f.id)).arrayBuffer()),new Uint8Array(bytes));
  assert.equal((await request(env,'files/'+f.id,{user:'another-user'})).status,404);
  const record={projectId:p.id,title:'시공 전 사진',stage:'시공 전',date:'2026-09-23',content:'',files:[{id:f.id}]};
  assert.equal((await request(env,'records',{method:'POST',data:record})).status,201);
  const state=await(await request(env,'state')).json();assert.equal(state.records[0].files[0].name,'검증 사진.png');
  const range=await request(env,'files/'+f.id,{headers:{Range:'bytes=0-7'}});assert.equal(range.status,206);assert.equal((await range.arrayBuffer()).byteLength,8);
  assert.equal((await request(env,'files',{method:'POST',body:'not a png',headers:{'Content-Type':'image/png'}})).status,400);
  assert.equal((await request(env,'files',{method:'POST',body:'bad',headers:{'Content-Type':'text/html'}})).status,400);
 }finally{env.close();}
});
test('오디오 파일 저장과 사용자 간 첨부 참조 차단',async()=>{
 const env=environment();try{
  const bytes=new Uint8Array([26,69,223,163,1,2,3,4]);const upload=await request(env,'files',{method:'POST',body:bytes,headers:{'Content-Type':'audio/webm','X-File-Name':'test.webm'}});assert.equal(upload.status,201);const f=await upload.json();
  const p=await(await request(env,'projects',{method:'POST',data:project,user:'another-user'})).json();
  assert.equal((await request(env,'records',{method:'POST',user:'another-user',data:{projectId:p.id,title:'참조 테스트',stage:'시공 중',content:'메모',files:[{id:f.id}],date:'2026-09-23'}})).status,400);
 }finally{env.close();}
});
test('잘못된 날짜·금액을 거부하고 사업자 설정을 저장한다',async()=>{
 const env=environment();try{
  for(const changed of [{date:'2026-13-05'},{date:'2026-02-30'},{received:-1},{contract:1.5},{time:'25:00'}])assert.equal((await request(env,'projects',{method:'POST',data:{...project,...changed}})).status,400);
  assert.equal((await request(env,'settings/business',{method:'PUT',data:{company:'검증설비',revision:0}})).status,201);
  const state=await(await request(env,'state')).json();assert.equal(state.settings.company,'검증설비');
 }finally{env.close();}
});
test('월 구독 이전에는 첫 현장과 파일 저장이 차단되고 설정은 허용된다',async()=>{
 const env=environment({paid:false});try{
  const state=await(await request(env,'state')).json();assert.equal(state.subscription.active,false);assert.equal(state.subscription.price,9900);
  assert.equal((await request(env,'projects',{method:'POST',data:project})).status,402);
  assert.equal((await request(env,'files',{method:'POST',body:new Uint8Array([137,80,78,71,13,10,26,10]),headers:{'Content-Type':'image/png'}})).status,402);
  assert.equal((await request(env,'settings/business',{method:'PUT',data:{company:'검증설비',revision:0}})).status,201);
 }finally{env.close();}
});
test('월 갱신일과 구독 상태를 정확히 계산한다',()=>{
 assert.equal(MONTHLY_PRICE,9900);
 assert.equal(nextBillingDate('2026-01-31T08:00:00.000Z'),'2026-02-28T08:00:00.000Z');
 assert.equal(entitlement({status:'ACTIVE',paid_until:'2026-10-01T00:00:00.000Z',billing_key:'sealed',cancel_at_period_end:0},'2026-09-24T00:00:00.000Z').active,true);
 assert.equal(entitlement({status:'ACTIVE',paid_until:'2026-09-01T00:00:00.000Z'},'2026-09-24T00:00:00.000Z').active,false);
 assert.throws(()=>validateBillingReturn({authKey:'abc123',customerKey:'other'},{customer_key:'real'}));
});
test('빌링키는 암호화해 복원하고 토스 승인은 금액과 주문번호를 검증한다',async()=>{
 const secret=btoa(String.fromCharCode(...new Uint8Array(32).fill(7)));
 const sealed=await encryptBillingKey('billing-key-test',secret);assert.notEqual(sealed,'billing-key-test');assert.equal(await decryptBillingKey(sealed,secret),'billing-key-test');
 const observed=[];const fakeFetch=async(url,options)=>{observed.push({url,options});return {ok:true,async json(){return {orderId:'HN_123',totalAmount:9900,status:'DONE',paymentKey:'payment-key'};}};};
 const result=await tossRequest('test_sk_placeholder','/v1/billing/test-key',{amount:9900,orderId:'HN_123'},'HN_123',fakeFetch);
 assert.equal(verifyCharge(result,'HN_123'),'payment-key');assert.equal(observed[0].options.headers['Idempotency-Key'],'HN_123');
 assert.throws(()=>verifyCharge({...result,totalAmount:100},'HN_123'));
});
test('카드 등록 후 서버 승인으로 월 이용권을 열고 구독 중지를 기록한다',async()=>{
 const env=environment({paid:false}),originalFetch=globalThis.fetch;
 env.TOSS_CLIENT_KEY='test_ck_for_test';env.TOSS_SECRET_KEY='test_sk_for_test';env.BILLING_ENCRYPTION_KEY=btoa(String.fromCharCode(...new Uint8Array(32).fill(9)));
 const called=[];globalThis.fetch=async(url,options)=>{called.push({url,options});if(options?.method!=='POST')return {status:404,ok:false};if(url.includes('/authorizations/issue'))return {ok:true,async json(){return {customerKey:called.customerKey,billingKey:'billing-key-test'};}};const body=JSON.parse(options.body);return {ok:true,async json(){return {orderId:body.orderId,totalAmount:9900,status:'DONE',paymentKey:'payment-key-test'};}};};
 try{
  const start=await(await request(env,'subscription/start',{method:'POST',data:{}})).json();called.customerKey=start.customerKey;
  assert.equal(start.registered,false);assert.equal(start.clientKey,env.TOSS_CLIENT_KEY);
  assert.equal((await request(env,'subscription/activate',{method:'POST',data:{authKey:'auth-key-test',customerKey:'another'}})).status,400);
  const activated=await request(env,'subscription/activate',{method:'POST',data:{authKey:'auth-key-test',customerKey:start.customerKey}});assert.equal(activated.status,200);assert.equal((await activated.json()).subscription.active,true);
  assert.equal((await request(env,'projects',{method:'POST',data:project})).status,201);
  const raw=env.sqlite.prepare("SELECT billing_key FROM subscriptions WHERE user_id='owner'").get();assert.notEqual(raw.billing_key,'billing-key-test');
  const canceled=await request(env,'subscription/cancel',{method:'POST',data:{}});assert.equal((await canceled.json()).subscription.canceled,true);
  assert.equal(called.filter(x=>x.options?.method==='POST').length,2);
  const resumed=await request(env,'subscription/resume',{method:'POST',data:{}});assert.equal((await resumed.json()).subscription.autoRenew,true);
  env.sqlite.prepare("UPDATE subscriptions SET paid_until='2026-01-01T00:00:00.000Z' WHERE user_id='owner'").run();
  const pending=[];await worker.scheduled({},env,{waitUntil:p=>pending.push(p)});await Promise.all(pending);
  assert.equal((await(await request(env,'state')).json()).subscription.active,true);
  assert.equal(called.filter(x=>x.options?.method==='POST').length,3);
 }finally{globalThis.fetch=originalFetch;env.close();}
});
