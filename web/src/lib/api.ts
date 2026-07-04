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
  // Use Headers ctor so callers can pass a plain object, Headers, or
  // tuple-array — spreading would drop the latter two. Then enforce
  // Accept default + force credentials=include LAST so callers can't
  // override (e.g. by passing `{credentials: "omit"}`). Seer MED +
  // Codex follow-up on PR #42.
  const headers = new Headers(init?.headers);
  if (!headers.has("Accept")) headers.set("Accept", "application/json");
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
    credentials: "include",
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
