import "./dashboard-grug.css";

export function NotFound() {
  return (
    <div className="grug-dash">
      <header className="nav">
        <div className="nav-inner">
          <a className="brand" href="/">
            <span className="brand-mark"><img src="/assets/grug-angry.png" alt="" /></span>
            <span>grug</span>
          </a>
          <nav className="links">
            <a href="/">Home</a>
            <a href="https://github.com/quadseven/grug">Docs</a>
          </nav>
        </div>
      </header>

      <div className="shell">
        <div className="signin-stage">
          <span className="eyebrow"><span className="blob"></span>404 · lost in cave</span>
          <h1>Grug <em>no find</em> page.</h1>
          <p className="signin-sub">// This path not in cave. Maybe bad link. Maybe Grug eat it. Go back to safe ground.</p>
          <a className="btn primary lg signin-cta" href="/">← Back to splash</a>
        </div>
      </div>

      <footer>
        <div className="foot-inner">
          <span className="brand serif">grug.</span>
          <span>AGPL-3.0. Made in a cave. <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></span>
          <span>Grug lose page. Grug shrug.</span>
        </div>
      </footer>
    </div>
  );
}
