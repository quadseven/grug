import { useMemo, useRef, useState } from "react";
import { Navigate, Link, useNavigate } from "react-router-dom";
import { useMe } from "../lib/me";
import { API_BASE } from "../lib/api";
import {
  useInstallations,
  useInstallRepos,
  useEnforcement,
  useFixEnforcement,
  useSetRepoConfig,
  type Repo,
} from "../lib/installations";
import "./dashboard-grug.css";

// ── Grug Caveman Editorial dashboard (design bundle Dashboard.html). The
// Repositories panel is wired to the real API (installs / tpm toggle /
// enforcement / fix); the other six panels are the design's interactive
// local-state mock until backends exist (no fake "saved to server" — state
// persists to localStorage so the controls feel live, like the prototype).

type Panel =
  | "repos" | "personas" | "appearance" | "usage" | "notifications" | "account" | "activity";

const PANELS: { id: Panel; idx: string; label: string; badge?: string }[] = [
  { id: "repos", idx: "01", label: "Repositories" },
  { id: "personas", idx: "02", label: "Personas", badge: "5" },
  { id: "appearance", idx: "03", label: "Appearance" },
  { id: "usage", idx: "04", label: "Usage & billing" },
  { id: "notifications", idx: "05", label: "Notifications" },
  { id: "account", idx: "06", label: "Account & org" },
  { id: "activity", idx: "07", label: "Activity", badge: "live" },
];

const PERSONAS = [
  { id: "smasher", code: "F-01", name: "Smasher", img: "grug_smasher.png", desc: "Static analysis + symbolic exec + LLM diff review. Null-derefs, races, off-by-ones.", meta: ["312 runs / mo", "98% pass"] },
  { id: "guard", code: "F-02", name: "Guard", img: "grug_guard.png", desc: "SCA, secret scanning, SAST on the diff, hourly CVE feed. Evil shall not pass.", meta: ["hourly CVE feed", "1 blocking now"] },
  { id: "elder", code: "F-03", name: "Elder", img: "grug_elder.png", desc: "Line-by-line review for naming, complexity, coverage, dead code.", meta: ["BYO model key", "inline suggestions"] },
  { id: "chief", code: "F-04", name: "Chief", img: "grug_chief.png", desc: "Definition-of-Ready on every PR. Acceptance criteria, estimate, rollback plan.", meta: ["5 checks", "strict mode"] },
  { id: "warder", code: "F-05", name: "Warder", img: "grug_mystic.png", desc: "Ward off bad release. Auto-changelog, semver hint, deploy gate.", meta: ["beta", "coming soon"], soon: true },
] as const;

const SKINS = [
  { id: "classic", cap: "Classic", img: "grug-angry.png" },
  { id: "pro", cap: "Professional", img: "grug_professional.png" },
  { id: "mullet", cap: "Mullet", img: "grug_mullet.png" },
  { id: "smile", cap: "Smile", img: "grug_smile.png" },
  { id: "byog", cap: "BYOG", img: "grug_byog.png" },
];

const NOTIF = [
  { id: "checkruns", name: "GitHub Check Runs", desc: "Post verdicts as Check Runs. The core gate — leave this on." },
  { id: "email", name: "Email on block", desc: "Mail you when a persona blocks one of your PRs." },
  { id: "slack", name: "Slack channel", desc: "Drop blocking findings into #grug. Connect workspace first." },
  { id: "stale", name: "Stale-PR pulse", desc: "Daily nudge for PRs sitting unreviewed more than 4 days." },
  { id: "weekly", name: "Weekly digest", desc: "Monday summary of what Grug crushed last week." },
];

const ACTIVITY = [
  { persona: "Guard", repo: "githumps/somatic-scripts", pr: "#358", v: "block", msg: "CVE-2026-1144 in lodash@4.17.20 · prototype pollution", ago: "2m" },
  { persona: "Smasher", repo: "githumps/somatic-scripts", pr: "#358", v: "block", msg: "2 null-deref paths in parser.go:142", ago: "2m" },
  { persona: "Chief", repo: "githumps/somatic-scripts", pr: "#358", v: "block", msg: "missing rollback plan · DoR fail", ago: "2m" },
  { persona: "Elder", repo: "githumps/somatic-scripts", pr: "#358", v: "warn", msg: "complexity 18 in resolveTree() · coverage 67%", ago: "2m" },
  { persona: "Smasher", repo: "githumps/grug", pr: "#142", v: "pass", msg: "0 critical findings · all error returns handled", ago: "11m" },
  { persona: "Elder", repo: "your-org/edge-router", pr: "#77", v: "warn", msg: "C-style loop · suggest range · 1 inline fix", ago: "1h" },
  { persona: "Smasher", repo: "your-org/retro-archive", pr: "#19", v: "pass", msg: "clean diff · no regressions detected", ago: "3h" },
] as const;

type PMode = "block" | "warn" | "off";
const LS = "grug_dash_v2";

// HTML-escape any dynamic value before it flows into the toast / ascii
// innerHTML (repo names come from GitHub; `mood` is free user input). The
// surrounding markup is our own static, trusted HTML — only the interpolated
// values need escaping, which removes the XSS vector.
const esc = (s: string) =>
  s.replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string
  ));

function loadLocal() {
  const def = {
    personas: { smasher: "block", guard: "block", elder: "warn", chief: "block", warder: "off" } as Record<string, PMode>,
    skin: "classic",
    tone: "caveman",
    mood: "GRUG.MOOD = ANGRY",
    cap: "50",
    notif: { checkruns: true, email: true, slack: false, stale: true, weekly: false } as Record<string, boolean>,
    pauseAll: false,
  };
  try {
    const s = JSON.parse(localStorage.getItem(LS) || "{}");
    return {
      ...def, ...s,
      personas: { ...def.personas, ...(s.personas || {}) },
      notif: { ...def.notif, ...(s.notif || {}) },
    };
  } catch { return def; }
}

export function Dashboard() {
  const me = useMe();
  const installs = useInstallations();
  const [panel, setPanel] = useState<Panel>("repos");
  const [local, setLocalState] = useState<ReturnType<typeof loadLocal>>(loadLocal);
  const setLocal = (patch: Partial<ReturnType<typeof loadLocal>>) =>
    setLocalState((s: ReturnType<typeof loadLocal>) => { const next = { ...s, ...patch }; try { localStorage.setItem(LS, JSON.stringify(next)); } catch { /* ignore */ } return next; });

  const [toast, setToast] = useState<string | null>(null);
  const toastT = useRef<number | undefined>(undefined);
  const fireToast = (html: string) => {
    setToast(html); window.clearTimeout(toastT.current);
    toastT.current = window.setTimeout(() => setToast(null), 1700);
  };

  const [tapeDismissed, setTapeDismissed] = useState(
    () => localStorage.getItem("grug_tape_dismissed") === "1");

  if (me.isLoading) {
    return <div className="grug-dash"><div style={{ padding: "80px", textAlign: "center", fontFamily: "'JetBrains Mono',monospace", color: "var(--muted)" }}>loading…</div></div>;
  }
  if (!me.data?.authenticated) return <Navigate to="/signin" replace />;

  const installList = installs.data?.installations ?? [];
  const installId = installList[0]?.install_id;

  return (
    <div className="grug-dash">
      {!tapeDismissed && (
        <div className="tape">
          <div className="tape-track">
            {["GRUG GUARD THIS REPO.", "GRUG WATCH EVERY PR.", "YOU TOGGLE. GRUG OBEY.", "NO YAML IN CAVE.", "GRUG REMEMBER SETTING.",
              "GRUG GUARD THIS REPO.", "GRUG WATCH EVERY PR.", "YOU TOGGLE. GRUG OBEY.", "NO YAML IN CAVE.", "GRUG REMEMBER SETTING."].map((t, i) => (
              <span key={i}>{t}<span className="dot"> ● </span></span>
            ))}
          </div>
          <button className="tape-x" title="Dismiss" onClick={() => { setTapeDismissed(true); try { localStorage.setItem("grug_tape_dismissed", "1"); } catch { /* ignore */ } }}>×</button>
        </div>
      )}

      <header className="nav">
        <div className="nav-inner">
          <Link className="brand" to="/">
            <span className="brand-mark"><img src="/assets/grug-angry.png" alt="" /></span>
            <span>grug</span>
          </Link>
          <nav className="links">
            <a className={panel !== "activity" ? "active" : ""} onClick={() => setPanel("repos")}>Dashboard</a>
            <a className={panel === "activity" ? "active" : ""} onClick={() => setPanel("activity")}>Activity</a>
            {me.data.role === "admin" && <Link to="/admin">Admin</Link>}
            <a href="https://github.com/githumps/grug">Docs</a>
          </nav>
          <div className="userchip">
            <div className="who"><b>@{me.data.login}</b><span>{me.data.role}</span></div>
            <span className="av"><img src="/assets/grug-angry.png" alt="" /></span>
            <SignOut />
          </div>
        </div>
      </header>

      <div className="shell">
        <div className="pagehead">
          <div>
            <span className="eyebrow"><span className="blob"></span>signed in · cave control</span>
            <h1>Grug <em>settings</em>.<br />You boss. Grug listen.</h1>
          </div>
          <p className="sub">// You land here after GitHub OAuth. Toggle personas, pick a skin, set the budget. <span className="ok">Grug remember everything</span> — no yaml, no save button.</p>
        </div>

        <StatStrip installId={installId} />

        <div className="layout">
          <aside className="rail">
            <div className="rail-nav">
              {PANELS.map((p) => (
                <button key={p.id} className={panel === p.id ? "active" : ""} onClick={() => { setPanel(p.id); window.scrollTo({ top: 0 }); }}>
                  <span className="idx">{p.idx}</span><span className="lbl">{p.label}</span>
                  {p.badge && <span className="badge">{p.badge}</span>}
                </button>
              ))}
            </div>
            <div className="rail-foot">
              <div className="plan"><span>PLAN</span><b>PRO</b></div>
              <div className="muted">19 / 50 checks today</div>
              <div className="meter"><i style={{ width: "38%" }}></i></div>
              <div className="muted">Resets 00:00 UTC · 6 seats</div>
            </div>
          </aside>

          <main className="main">
            <ReposPanel show={panel === "repos"} installId={installId} reposLoading={installs.isLoading} fireToast={fireToast} />
            <PersonasPanel show={panel === "personas"} modes={local.personas} setMode={(id, v) => { setLocal({ personas: { ...local.personas, [id]: v } }); fireToast(`${id.toUpperCase()} → <span class="am">${v.toUpperCase()}</span>`); }} />
            <AppearancePanel show={panel === "appearance"} local={local} setLocal={setLocal} fireToast={fireToast} />
            <UsagePanel show={panel === "usage"} cap={local.cap} setCap={(c) => { setLocal({ cap: c }); fireToast(`Daily cap → <span class="am">${c === "0" ? "∞" : c}</span>`); }} />
            <NotificationsPanel show={panel === "notifications"} notif={local.notif} toggle={(id) => setLocal({ notif: { ...local.notif, [id]: !local.notif[id] } })} />
            <AccountPanel show={panel === "account"} me={{ login: me.data.login ?? "you", role: me.data.role ?? "user" }} pauseAll={local.pauseAll} setPause={(v) => { setLocal({ pauseAll: v }); fireToast(v ? 'All personas <span class="am">PAUSED</span>. Grug nap.' : 'Grug <span class="am">AWAKE</span>. Back to work.'); }} />
            <ActivityPanel show={panel === "activity"} />
          </main>
        </div>
      </div>

      <footer>
        <div className="foot-inner">
          <span className="brand serif">grug.</span>
          <span>AGPL-3.0. Made in a cave. <a href="/Privacy.html">Privacy</a> · <a href="/Terms.html">Terms</a></span>
          <span>Grug remember your setting. Grug satisfied.</span>
        </div>
      </footer>

      {toast && <div className="toast show" dangerouslySetInnerHTML={{ __html: toast }} />}
    </div>
  );
}

function SignOut() {
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  return (
    <a className="btn sm ghost" onClick={async () => {
      setBusy(true);
      try { await fetch(`${API_BASE}/api/v1/auth/logout`, { method: "POST", credentials: "include" }); } catch { /* ignore */ }
      nav("/", { replace: true });
    }} aria-disabled={busy}>Sign out</a>
  );
}

function StatStrip({ installId }: { installId?: number }) {
  const repos = useInstallRepos(installId);
  const list = repos.data?.repos ?? [];
  const guarded = list.filter((r) => r.config.tpm_enabled).length;
  return (
    <div className="statstrip">
      <div className="cell"><div className="k">Repos guarded</div><div className="v">{guarded}<small> / {list.length} installed</small></div></div>
      <div className="cell"><div className="k">Personas active</div><div className="v">2<small> active</small></div></div>
      <div className="cell amber"><div className="k">Checks today</div><div className="v">19<small> / 50</small></div></div>
      <div className="cell ink"><div className="k">Now blocking</div><div className="v">3<small> PRs gated</small></div></div>
    </div>
  );
}

function ReposPanel({ show, installId, reposLoading, fireToast }: { show: boolean; installId?: number; reposLoading: boolean; fireToast: (h: string) => void }) {
  const repos = useInstallRepos(installId);
  const setConfig = useSetRepoConfig(installId ?? 0);
  const fixEnforcement = useFixEnforcement(installId ?? 0);
  const [q, setQ] = useState("");
  const list = repos.data?.repos ?? [];
  const filtered = useMemo(() => list.filter((r) => r.full_name.toLowerCase().includes(q.toLowerCase())), [list, q]);

  return (
    <section className={`panel${show ? " show" : ""}`}>
      <div className="panel-head">
        <h2>Repositories <em>Grug guard</em>.</h2>
        <p className="note">// Flip a repo on and Grug posts Check Runs on every PR. Off means Grug sleep.</p>
      </div>
      <div className="card">
        <div className="card-head">Installed repositories <span className="count">{list.length}</span></div>
        <div className="card-body">
          <div className="toolbar">
            <div className="search">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><circle cx="11" cy="11" r="7" /><path d="M21 21l-4-4" /></svg>
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="search repos…" />
            </div>
            <a className="btn sm primary" href="https://github.com/apps/grug-tribe/installations/new">+ Add repository</a>
          </div>
          {(reposLoading || repos.isLoading) && <div className="mono" style={{ fontSize: 12, color: "var(--muted)", padding: 8 }}>loading repos…</div>}
          {repos.isError && <div className="mono" style={{ fontSize: 12, color: "var(--tomato)", padding: 8 }}>failed to load repos</div>}
          {!repos.isLoading && filtered.map((r) => (
            <RepoRow key={r.repo_id} repo={r} installId={installId!}
              onToggle={(enabled) => { setConfig.mutate({ repo_id: r.repo_id, tpm_enabled: enabled }); const nm = esc(r.full_name.split("/")[1] ?? ""); fireToast(enabled ? `Grug now guard <span class="am">${nm}</span>.` : `Grug sleep on <span class="am">${nm}</span>.`); }}
              onFix={() => fixEnforcement.mutate(r.repo_id)}
              fixPending={fixEnforcement.isPending && fixEnforcement.variables === r.repo_id}
              fixError={fixEnforcement.isError && fixEnforcement.variables === r.repo_id ? ((fixEnforcement.error as Error)?.message ?? "fix failed") : undefined}
            />
          ))}
          {!repos.isLoading && !repos.isError && filtered.length === 0 && (
            <div className="mono" style={{ fontSize: 12, color: "var(--muted)", padding: 8 }}>No repo match. Grug shrug.</div>
          )}
        </div>
      </div>
    </section>
  );
}

function RepoRow({ repo, installId, onToggle, onFix, fixPending, fixError }: {
  repo: Repo; installId: number; onToggle: (e: boolean) => void; onFix: () => void; fixPending: boolean; fixError?: string;
}) {
  const [owner, name] = repo.full_name.split("/");
  const on = repo.config.tpm_enabled;
  const enforcement = useEnforcement(on ? installId : undefined, on ? repo.repo_id : undefined);
  const state = enforcement.data?.enforcement_state;
  const degraded = enforcement.data?.degraded === true;

  let enf: { cls: string; label: string } | null = null;
  if (on) {
    if (degraded && (state === "grug_managed" || state === "external")) enf = { cls: "unknown", label: "unconfirmed" };
    else if (state === "grug_managed") enf = { cls: "live", label: "ENFORCED" };
    else if (state === "external") enf = { cls: "live", label: "EXTERNAL" };
    else if (state === "none") enf = { cls: "warn", label: "⚠ not enforced" };
    else if (state === "unknown") enf = { cls: "unknown", label: "unknown" };
  }

  return (
    <div className="repo">
      <div className="org">{(owner || "?").slice(0, 2).toUpperCase()}</div>
      <div className="name"><b>{name}</b><span>{owner}/ · {repo.private ? "private" : "public"}</span></div>
      {enf && <span className={`state ${enf.cls}`}>{enf.label}</span>}
      {on && state === "none" && !fixPending && <button className="fixbtn" onClick={onFix}>fix</button>}
      {fixPending && <span className="state paused">fixing…</span>}
      {fixError && !fixPending && <span className="fixerr" title={fixError}>⚠ fix failed</span>}
      <span className={`state ${on ? "live" : "paused"}`}>{on ? "GUARDED" : "PAUSED"}</span>
      <div className={`sw${on ? " on" : ""}`} onClick={() => onToggle(!on)} role="switch" aria-checked={on}></div>
    </div>
  );
}

function PersonasPanel({ show, modes, setMode }: { show: boolean; modes: Record<string, PMode>; setMode: (id: string, v: PMode) => void }) {
  return (
    <section className={`panel${show ? " show" : ""}`}>
      <div className="panel-head">
        <h2>Many Grugs. <em>One cave.</em></h2>
        <p className="note">// Each persona is its own Check Run. Set it to BLOCK merge, WARN only, or OFF.</p>
      </div>
      <div className="card">
        <div className="card-head"><span>Personas</span></div>
        <div className="card-body">
          {PERSONAS.map((p) => {
            const mode = modes[p.id] ?? "off";
            return (
              <div key={p.id} className={`pcard${"soon" in p && p.soon ? " soon" : ""}`}>
                <div className="portrait"><img src={`/assets/${p.img}`} alt="" /></div>
                <div className="body">
                  <div className="row1"><span className="tag">{p.code} · {p.name.toUpperCase()}</span></div>
                  <h3>{p.name}</h3>
                  <p className="desc">{p.desc}</p>
                  <div className="meta">{p.meta.map((m) => <span key={m}>{m}</span>)}</div>
                </div>
                <div className="ctrl">
                  <div className="seg">
                    {(["block", "warn", "off"] as PMode[]).map((v) => (
                      <button key={v} data-v={v} className={mode === v ? "on" : ""} onClick={() => setMode(p.id, v)}>{v.toUpperCase()}</button>
                    ))}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </section>
  );
}

function AppearancePanel({ show, local, setLocal, fireToast }: { show: boolean; local: ReturnType<typeof loadLocal>; setLocal: (p: Partial<ReturnType<typeof loadLocal>>) => void; fireToast: (h: string) => void }) {
  const tones: Record<string, string[]> = {
    caveman: ['<span class="to">×</span> guard · CVE-2026-1144 in lodash@4.17.20', '<span class="am">grug</span> "Lodash have hole. Hole let bad man in cave.', '       Grug not let bad man in cave. Grug block."'],
    professional: ['<span class="to">×</span> guard · CVE-2026-1144 in lodash@4.17.20', '<span class="am">grug</span> Vulnerable dependency detected (CVSS 8.8).', '       Merge blocked pending upgrade to 4.17.21+.'],
    deadpan: ['<span class="to">×</span> guard · CVE-2026-1144 in lodash@4.17.20', '<span class="am">grug</span> This is fine. This is not fine.', '       Blocked.'],
    custom: ['<span class="to">×</span> guard · CVE-2026-1144 in lodash@4.17.20', '<span class="am">grug</span> [ your custom template renders here ]', '       {{ finding }} · {{ severity }}'],
  };
  const preview = `<span class="mo"># ${esc(local.mood)}</span>\n` + (tones[local.tone] || tones.caveman).join("\n");
  return (
    <section className={`panel${show ? " show" : ""}`}>
      <div className="panel-head">
        <h2>How Grug <em>look</em> and <em>talk</em>.</h2>
        <p className="note">// Pick the skin that shows on Check Runs and the tone Grug uses when he block your bad PR.</p>
      </div>
      <div className="card">
        <div className="card-head">Active skin</div>
        <div className="card-body">
          <div className="skins">
            {SKINS.map((s) => (
              <div key={s.id} className={`skin${local.skin === s.id ? " act" : ""}`} onClick={() => { setLocal({ skin: s.id }); fireToast(`Skin → <span class="am">${s.cap}</span>`); }}>
                <div className="art"><img src={`/assets/${s.img}`} alt="" /></div><div className="cap">{s.cap}</div>
              </div>
            ))}
          </div>
        </div>
      </div>
      <div className="card">
        <div className="card-head">Failure tone</div>
        <div className="card-body">
          <div className="radios">
            {["caveman", "professional", "deadpan", "custom"].map((t) => (
              <button key={t} className={local.tone === t ? "on" : ""} onClick={() => setLocal({ tone: t })}>{t[0].toUpperCase() + t.slice(1)}</button>
            ))}
          </div>
          <div className="field" style={{ marginTop: 8, borderBottom: "none", paddingBottom: 0 }}>
            <div className="lbl"><b>Mood sticker</b><span>The little badge stamped on every Check Run summary.</span></div>
            <input className="text" value={local.mood} onChange={(e) => setLocal({ mood: e.target.value })} />
          </div>
          <div className="ascii" style={{ marginTop: 14 }} dangerouslySetInnerHTML={{ __html: preview }} />
        </div>
      </div>
    </section>
  );
}

function UsagePanel({ show, cap, setCap }: { show: boolean; cap: string; setCap: (c: string) => void }) {
  return (
    <section className={`panel${show ? " show" : ""}`}>
      <div className="panel-head">
        <h2>Usage &amp; <em>budget</em>.</h2>
        <p className="note">// Grug cost money to run. Cap the daily checks so Grug not eat your whole AWS bill.</p>
      </div>
      <div className="card">
        <div className="card-head">Daily PR-check budget</div>
        <div className="card-body">
          <div className="bignum">19 <small>of 50 checks used today</small></div>
          <div className="budget"><i style={{ width: "38%" }}></i><span>38%</span></div>
          <div className="field" style={{ borderTop: "2px dashed var(--ink)", marginTop: 8 }}>
            <div className="lbl"><b>Hard cap per day</b><span>Grug stops posting once cap is hit. Pending PRs queue for tomorrow.</span></div>
            <div className="seg">
              {[["50", "50"], ["200", "200"], ["1000", "1000"], ["0", "∞"]].map(([v, lbl]) => (
                <button key={v} className={cap === v ? "on" : ""} onClick={() => setCap(v)}>{lbl}</button>
              ))}
            </div>
          </div>
        </div>
      </div>
      <div className="card">
        <div className="card-head">Current plan</div>
        <div className="card-body" style={{ padding: 0 }}>
          <div className="plan-grid">
            <div>
              <div className="nm">Pro</div>
              <div className="pr">$12 / mo · per seat · 6 seats</div>
              <ul className="feats" style={{ marginTop: 14 }}>
                <li>Unlimited PR checks</li><li>All personas</li><li>Up to 10 repos</li><li>BYO model key</li>
              </ul>
            </div>
            <div>
              <div className="k mono" style={{ fontSize: 10, letterSpacing: "0.12em", textTransform: "uppercase", color: "var(--muted)" }}>This cycle</div>
              <div className="bignum" style={{ margin: "8px 0 4px" }}>$72<small>/mo</small></div>
              <div className="pr">Renews Jun 12 · 487 checks billed</div>
              <div style={{ display: "flex", gap: 10, marginTop: 16, flexWrap: "wrap" }}>
                <a className="btn sm" href="#">Manage billing</a>
                <a className="btn sm ghost" href="/#pricing">Change plan</a>
              </div>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function NotificationsPanel({ show, notif, toggle }: { show: boolean; notif: Record<string, boolean>; toggle: (id: string) => void }) {
  return (
    <section className={`panel${show ? " show" : ""}`}>
      <div className="panel-head">
        <h2>When Grug <em>shout</em>.</h2>
        <p className="note">// Grug never spam PR comments. Pick where the noise goes instead.</p>
      </div>
      <div className="card">
        <div className="card-head">Notification channels</div>
        <div className="card-body">
          {NOTIF.map((n) => (
            <div className="field" key={n.id}>
              <div className="lbl"><b>{n.name}</b><span>{n.desc}</span></div>
              <div className={`sw${notif[n.id] ? " on" : ""}`} onClick={() => toggle(n.id)} role="switch" aria-checked={!!notif[n.id]}></div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function AccountPanel({ show, me, pauseAll, setPause }: { show: boolean; me: { login: string; role: string }; pauseAll: boolean; setPause: (v: boolean) => void }) {
  return (
    <section className={`panel${show ? " show" : ""}`}>
      <div className="panel-head">
        <h2>Account &amp; <em>org</em>.</h2>
        <p className="note">// Your GitHub connection and the dangerous buttons. Grug warned you.</p>
      </div>
      <div className="card">
        <div className="card-head">GitHub connection</div>
        <div className="card-body">
          <div className="field">
            <div className="lbl"><b>Signed in as</b><span>@{me.login} · {me.role}</span></div>
            <a className="btn sm ghost" href="https://github.com/apps/grug-tribe/installations/new">Manage on GitHub</a>
          </div>
          <div className="field">
            <div className="lbl"><b>BYO model key</b><span>Grug uses your own LLM key for Elder review. Never leaves your VPC.</span></div>
            <input className="text" type="password" defaultValue="sk-grug-••••••••••••" placeholder="sk-…" />
          </div>
        </div>
      </div>
      <div className="card danger-card">
        <div className="card-head">Danger zone</div>
        <div className="card-body">
          <div className="field">
            <div className="lbl"><b>Pause all personas</b><span>Grug stops posting Check Runs everywhere until you flip it back.</span></div>
            <div className={`sw${pauseAll ? " on" : ""}`} onClick={() => setPause(!pauseAll)} role="switch" aria-checked={pauseAll}></div>
          </div>
          <div className="field">
            <div className="lbl"><b>Uninstall Grug</b><span>Removes the GitHub App and deletes all cave settings. Grug sad.</span></div>
            <a className="btn sm danger" href="https://github.com/apps/grug-tribe/installations/new">Uninstall</a>
          </div>
        </div>
      </div>
    </section>
  );
}

function ActivityPanel({ show }: { show: boolean }) {
  const [filter, setFilter] = useState("all");
  const sym = (v: string) => (v === "block" ? "×" : v === "warn" ? "!" : "✓");
  const rows = ACTIVITY.filter((a) => filter === "all" || a.v === filter);
  return (
    <section className={`panel${show ? " show" : ""}`}>
      <div className="panel-head">
        <h2>What Grug <em>did</em>.</h2>
        <p className="note">// Live feed of Check Runs across guarded repos. Newest first. Grug never forget.</p>
      </div>
      <div className="card">
        <div className="card-head"><span>Recent check runs</span><span className="mono" style={{ textTransform: "none", letterSpacing: 0, color: "var(--muted)", marginLeft: "auto" }}>all guarded repos</span></div>
        <div className="card-body">
          <div className="toolbar">
            <div className="seg">
              {["all", "block", "warn", "pass"].map((f) => (
                <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>{f.toUpperCase()}</button>
              ))}
            </div>
          </div>
          {rows.map((a, i) => (
            <div className="afeed-row" key={i}>
              <div className={`vico ${a.v}`}>{sym(a.v)}</div>
              <div className="who2"><b>{a.persona}</b> <span className="repo2">{a.repo} · {a.pr}</span><span className="msg">{a.msg}</span></div>
              <span className={`verdict ${a.v}`}>{a.v.toUpperCase()}</span>
              <span className="ago">{a.ago}</span>
            </div>
          ))}
          {rows.length === 0 && <div className="mono" style={{ fontSize: 12, color: "var(--muted)", padding: 8 }}>Nothing here. Grug calm.</div>}
        </div>
      </div>
    </section>
  );
}
