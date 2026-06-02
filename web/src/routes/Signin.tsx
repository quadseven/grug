import { useEffect } from "react";
import { Link } from "react-router-dom";
import { API_BASE } from "../lib/api";
import "./dashboard-grug.css";

export function Signin() {
  const loginUrl = `${API_BASE}/api/v1/auth/github/login`;
  useEffect(() => {
    // The api Lambda's /auth/github/login sets the state cookie + bounces
    // to GitHub. Auto-redirect; the button below is a manual fallback if
    // the redirect is slow or blocked.
    window.location.href = loginUrl;
  }, [loginUrl]);

  return (
    <div className="grug-dash">
      <div className="tape">
        <div className="tape-track">
          {["GRUG GUARD YOUR CAVE.", "ONE GITHUB APP. MANY GRUGS.", "NO YAML IN CAVE.",
            "GRUG GUARD YOUR CAVE.", "ONE GITHUB APP. MANY GRUGS.", "NO YAML IN CAVE."].map((t, i) => (
            <span key={i}>{t}<span className="dot"> ● </span></span>
          ))}
        </div>
      </div>

      <header className="nav">
        <div className="nav-inner">
          <Link className="brand" to="/">
            <span className="brand-mark"><img src="/assets/grug-angry.png" alt="" /></span>
            <span>grug</span>
          </Link>
          <nav className="links">
            <Link to="/">Home</Link>
            <a href="https://github.com/githumps/grug">Docs</a>
          </nav>
        </div>
      </header>

      <div className="shell">
        <div className="signin-stage">
          <span className="eyebrow"><span className="blob"></span>signing in</span>
          <h1>Grug check your <em>cave key</em>.</h1>
          <p className="signin-sub">// Bouncing you to GitHub to authorize the app. Grug wait by the cave mouth.</p>
          <a className="btn primary lg signin-cta" href={loginUrl}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
            Sign in with GitHub →
          </a>
          <p className="signin-fallback">redirecting to github… <a href={loginUrl}>click here</a> if nothing happens.</p>
        </div>
      </div>

      <footer>
        <div className="foot-inner">
          <span className="brand serif">grug.</span>
          <span>AGPL-3.0. Made in a cave. <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></span>
          <span>Grug guard your cave. Grug patient.</span>
        </div>
      </footer>
    </div>
  );
}
