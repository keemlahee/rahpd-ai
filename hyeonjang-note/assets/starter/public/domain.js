export const statuses = ['상담 중', '견적 대기', '시공 예정', '시공 중', '완료'];
export function estimateTotals(items = [], discount = 0, vat = true) {
  const subtotal = items.reduce((sum, i) => sum + Math.round(Number(i.qty || 0) * Number(i.price || 0)), 0);
  const supply = Math.max(0, subtotal - Number(discount || 0));
  const tax = vat ? Math.round(supply * .1) : 0;
  return { subtotal, supply, tax, total: supply + tax };
}
export const won = value => `${Math.round(Number(value || 0)).toLocaleString('ko-KR')}원`;
export const today = () => new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Seoul', year:'numeric',month:'2-digit',day:'2-digit' }).format(new Date());
export const uid = () => crypto.randomUUID();
export const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
