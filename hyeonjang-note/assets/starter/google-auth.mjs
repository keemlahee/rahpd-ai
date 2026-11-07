const keyUrl='https://www.googleapis.com/oauth2/v3/certs';
let keyCache={until:0,keys:[]};
const decode=value=>{
 const bytes=Uint8Array.from(atob(value.replaceAll('-','+').replaceAll('_','/').padEnd(Math.ceil(value.length/4)*4,'=')),c=>c.charCodeAt(0));
 return JSON.parse(new TextDecoder().decode(bytes));
};
export function cookie(request,name){return (request.headers.get('Cookie')||'').split(';').map(x=>x.trim()).find(x=>x.startsWith(name+'='))?.slice(name.length+1)||'';}
export async function digest(value){const bytes=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value));return [...new Uint8Array(bytes)].map(x=>x.toString(16).padStart(2,'0')).join('');}
async function googleKeys(){
 if(keyCache.until>Date.now())return keyCache.keys;
 const response=await fetch(keyUrl);if(!response.ok)throw new Error('Google 인증 키를 읽지 못했어요.');
 const json=await response.json();if(!Array.isArray(json.keys))throw new Error('Google 인증 키가 올바르지 않아요.');
 const maxAge=Number(response.headers.get('Cache-Control')?.match(/max-age=(\d+)/)?.[1]||300);
 keyCache={until:Date.now()+Math.min(maxAge,3600)*1000,keys:json.keys};return keyCache.keys;
}
export async function verifyGoogleIdToken(token,clientId){
 if(typeof token!=='string'||token.length>10000)throw new Error('Google 인증 응답을 확인해 주세요.');
 const parts=token.split('.');if(parts.length!==3)throw new Error('Google 인증 응답을 확인해 주세요.');
 let header,payload;try{header=decode(parts[0]);payload=decode(parts[1]);}catch{throw new Error('Google 인증 응답을 확인해 주세요.');}
 if(header.alg!=='RS256'||!header.kid||payload.aud!==clientId||!['accounts.google.com','https://accounts.google.com'].includes(payload.iss)||typeof payload.sub!=='string'||!payload.sub||payload.sub.length>255||!Number.isInteger(payload.exp)||!Number.isInteger(payload.iat)||payload.exp<=Date.now()/1000||payload.iat>Date.now()/1000+60||payload.email_verified!==true)throw new Error('Google 계정 확인에 실패했어요.');
 const jwk=(await googleKeys()).find(key=>key.kid===header.kid&&key.kty==='RSA'&&key.use==='sig');
 if(!jwk)throw new Error('Google 인증 키가 일치하지 않아요.');
 const key=await crypto.subtle.importKey('jwk',jwk,{name:'RSASSA-PKCS1-v1_5',hash:'SHA-256'},false,['verify']);
 const signature=Uint8Array.from(atob(parts[2].replaceAll('-','+').replaceAll('_','/').padEnd(Math.ceil(parts[2].length/4)*4,'=')),c=>c.charCodeAt(0));
 const valid=await crypto.subtle.verify('RSASSA-PKCS1-v1_5',key,signature,new TextEncoder().encode(parts[0]+'.'+parts[1]));
 if(!valid)throw new Error('Google 인증 서명이 일치하지 않아요.');
 return payload;
}
export async function sessionUser(env,request){
 const token=cookie(request,'hn_auth');if(!/^[a-f0-9]{64}$/.test(token))return null;
 const row=await env.DB.prepare('SELECT user_id FROM auth_sessions WHERE token_hash=? AND expires_at>?').bind(await digest(token),new Date().toISOString()).first();
 return row?.user_id||null;
}
export const sessionCookie=token=>`hn_auth=${token}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=2592000`;
export const clearSessionCookie='hn_auth=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0';
