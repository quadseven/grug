import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
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
              <SignOutButton />
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

// API defines logout as POST; an `<a href>` triggers GET → 405. Use
// fetch to issue the POST, then navigate. Codex post-review #56.
function SignOutButton() {
  const nav = useNavigate();
  const qc = useQueryClient();
  const [pending, setPending] = useState(false);
  return (
    <button
      type="button"
      disabled={pending}
      onClick={async () => {
        setPending(true);
        try {
          await fetch(`${API_BASE}/api/v1/auth/logout`, {
            method: "POST",
            credentials: "include",
          });
        } finally {
          await qc.invalidateQueries({ queryKey: ["me"] });
          nav("/", { replace: true });
        }
      }}
      className="text-stone-500 hover:text-amber-400 disabled:opacity-50"
    >
      {pending ? "signing out…" : "sign out"}
    </button>
  );
}
