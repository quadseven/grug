// Newer Node releases ship their own Web Storage global, which is undefined
// unless Node is started with a storage file, and it shadows jsdom's
// window.localStorage. Install a plain in-memory Storage when that happens
// so the tests behave the same on every Node version CI or a laptop runs.
class MemoryStorage implements Storage {
  private data = new Map<string, string>();
  get length(): number { return this.data.size; }
  clear(): void { this.data.clear(); }
  getItem(key: string): string | null { return this.data.has(key) ? this.data.get(key)! : null; }
  key(index: number): string | null { return Array.from(this.data.keys())[index] ?? null; }
  removeItem(key: string): void { this.data.delete(key); }
  setItem(key: string, value: string): void { this.data.set(key, String(value)); }
}

for (const name of ["localStorage", "sessionStorage"] as const) {
  if (typeof globalThis[name] === "undefined" || globalThis[name] === null) {
    Object.defineProperty(globalThis, name, { value: new MemoryStorage(), configurable: true, writable: true });
  }
}

// jsdom does not implement scrolling; the dashboard's rail calls it.
window.scrollTo = (() => {}) as typeof window.scrollTo;
