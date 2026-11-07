export const MONTHLY_PRICE = 9900;

export function nextBillingDate(start) {
  const date = new Date(start);
  if (!Number.isFinite(date.getTime())) throw new Error('Invalid billing date');
  const day = date.getUTCDate();
  date.setUTCDate(1);
  date.setUTCMonth(date.getUTCMonth() + 1);
  const last = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate();
  date.setUTCDate(Math.min(day, last));
  return date.toISOString();
}

export function entitlement(subscription, now=new Date().toISOString()) {
  return {active:subscription?.status==='ACTIVE' && !!subscription.paid_until && subscription.paid_until>now,
    paidUntil:subscription?.paid_until||null,autoRenew:!!subscription?.billing_key && !subscription?.cancel_at_period_end,
    canceled:!!subscription?.cancel_at_period_end,price:MONTHLY_PRICE,testMode:true};
}

export function validateBillingReturn(input, subscription) {
  if (!input || typeof input!=='object' || Array.isArray(input) ||
      typeof input.authKey!=='string' || input.authKey.length<6 || input.authKey.length>300 ||
      typeof input.customerKey!=='string' || input.customerKey!==subscription?.customer_key)
    throw Object.assign(new Error('카드 등록 결과를 확인해 주세요.'),{status:400});
  return {authKey:input.authKey,customerKey:input.customerKey};
}

export async function tossRequest(secretKey,path,body,idempotencyKey,fetcher=fetch) {
  if (!secretKey?.startsWith('test_sk_')) throw Object.assign(new Error('테스트 결제 설정을 확인해 주세요.'),{status:503});
  const response=await fetcher(`https://api.tosspayments.com${path}`,{
    method:'POST',headers:{Authorization:`Basic ${btoa(`${secretKey}:`)}`,'Content-Type':'application/json','Idempotency-Key':idempotencyKey},
    body:JSON.stringify(body),signal:AbortSignal.timeout(65000)
  });
  const result=await response.json();
  if (!response.ok) throw Object.assign(new Error(result?.message||'토스 자동결제를 처리하지 못했어요.'),{status:502,providerCode:result?.code});
  return result;
}

export async function lookupTossOrder(secretKey,orderId,fetcher=fetch) {
  if (!secretKey?.startsWith('test_sk_')) throw Object.assign(new Error('테스트 결제 설정을 확인해 주세요.'),{status:503});
  const response=await fetcher(`https://api.tosspayments.com/v1/payments/orders/${encodeURIComponent(orderId)}`,{
    headers:{Authorization:`Basic ${btoa(`${secretKey}:`)}`},signal:AbortSignal.timeout(10000)
  });
  if(response.status===404)return null;
  const result=await response.json();
  if(!response.ok)throw Object.assign(new Error(result?.message||'결제 결과를 조회하지 못했어요.'),{status:503});
  return result;
}

export function verifyCharge(result,orderId) {
  if (result.orderId!==orderId || result.totalAmount!==MONTHLY_PRICE || result.status!=='DONE' || !result.paymentKey)
    throw Object.assign(new Error('토스 결제 응답을 확인하지 못했어요.'),{status:502});
  return result.paymentKey;
}

export async function encryptBillingKey(value,secret) {
  const raw=Uint8Array.from(atob(secret||''),c=>c.charCodeAt(0));
  if(raw.length!==32)throw Object.assign(new Error('빌링키 보관 설정을 확인해 주세요.'),{status:503});
  const key=await crypto.subtle.importKey('raw',raw,'AES-GCM',false,['encrypt']);
  const iv=crypto.getRandomValues(new Uint8Array(12));
  const cipher=new Uint8Array(await crypto.subtle.encrypt({name:'AES-GCM',iv},key,new TextEncoder().encode(value)));
  return btoa(String.fromCharCode(...iv,...cipher));
}

export async function decryptBillingKey(value,secret) {
  const raw=Uint8Array.from(atob(secret||''),c=>c.charCodeAt(0));
  if(raw.length!==32)throw Object.assign(new Error('빌링키 보관 설정을 확인해 주세요.'),{status:503});
  const bytes=Uint8Array.from(atob(value),c=>c.charCodeAt(0));
  const key=await crypto.subtle.importKey('raw',raw,'AES-GCM',false,['decrypt']);
  return new TextDecoder().decode(await crypto.subtle.decrypt({name:'AES-GCM',iv:bytes.slice(0,12)},key,bytes.slice(12)));
}
