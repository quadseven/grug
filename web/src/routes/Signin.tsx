import { useEffect } from "react";
import { API_BASE } from "../lib/api";
import { Shell } from "../components/Shell";

export function Signin() {
  useEffect(() => {
    // The api Lambda's /api/v1/auth/github/login sets state cookie +
    // redirects to GitHub. We just bounce — no need to render an
    // intermediate page.
    window.location.href = `${API_BASE}/api/v1/auth/github/login`;
  }, []);

  return (
    <Shell>
      <div className="px-6 py-24 text-center text-stone-400 font-mono text-sm">
        redirecting to github…
      </div>
    </Shell>
  );
}
