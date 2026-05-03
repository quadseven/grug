/**
 * Single source for the api base. CF Pages serves grug.lol; api.grug.lol
 * is the api Lambda Function URL behind a Worker. Use absolute URL so
 * `fetch` doesn't rewrite host on subdomain swap.
 */
const RAW_BASE = (import.meta.env.VITE_API_BASE as string | undefined) ?? "https://api.grug.lol";
export const API_BASE = RAW_BASE.replace(/\/$/, "");

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: { Accept: "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    let body = "";
    try {
      body = await res.text();
    } catch {
      // body unreadable — keep status-only message
    }
    throw new ApiError(res.status, body || res.statusText);
  }
  return (await res.json()) as T;
}
