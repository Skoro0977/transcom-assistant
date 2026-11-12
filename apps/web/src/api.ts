export type User = { id: string; username: string; role: string };
export type Session = { user: User; csrf_token: string };
export type Project = { id: string; title: string; description: string; version: number };
let csrf = '';
export function setSession(session: Session | null) { csrf = session?.csrf_token ?? ''; }
export async function api<T>(url: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(url, { ...options, credentials: 'same-origin',
    headers: { ...(csrf ? { 'X-CSRF-Token': csrf } : {}), ...options.headers } });
  if (!response.ok) {
    if (response.status === 401 && csrf) { setSession(null); window.dispatchEvent(new Event('session-expired')); }
    const error = await response.json().catch(() => ({ detail: 'Сервер недоступен' }));
    throw new Error(typeof error.detail === 'string' ? error.detail : 'Проверьте заполнение полей');
  }
  return response.json();
}
export function post<T>(url: string, body: unknown = {}) {
  return api<T>(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}
export const stamp = (s: string) => new Date(s).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' });
