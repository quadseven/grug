import { Link } from "react-router-dom";
import { useMe } from "../lib/me";
import { API_BASE } from "../lib/api";

export function Shell({ children }: { children: React.ReactNode }) {
  const me = useMe();
  return (
    <div className="min-h-full flex flex-col">
      <header className="border-b border-stone-800 px-6 py-3 flex items-center justify-between">
        <Link to="/" className="font-mono text-amber-400 text-lg tracking-tight">
          grug<span className="text-stone-500">.lol</span>
        </Link>
        <nav className="flex items-center gap-4 text-sm">
          {me.data?.authenticated ? (
            <>
              <Link to="/dashboard" className="text-stone-300 hover:text-amber-400">dashboard</Link>
              {me.data.role === "admin" && (
                <Link to="/admin" className="text-stone-300 hover:text-amber-400">admin</Link>
              )}
              <span className="text-stone-500 font-mono text-xs">{me.data.login}</span>
              <a
                href={`${API_BASE}/api/v1/auth/logout`}
                className="text-stone-500 hover:text-amber-400"
              >
                sign out
              </a>
            </>
          ) : (
            <Link to="/signin" className="text-amber-400 hover:underline">sign in</Link>
          )}
        </nav>
      </header>
      <main className="flex-1">{children}</main>
      <footer className="border-t border-stone-800 px-6 py-3 text-xs text-stone-500 font-mono">
        grug boss · open source · agpl-3.0
      </footer>
    </div>
  );
}
