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
  useActivity,
  useRerun,
  useRerunAll,
  type Repo,
} from "../lib/installations";
import "./dashboard-grug.css";

// ── Grug Caveman Editorial dashboard (design bundle Dashboard.html). The
// Repositories panel is wired to the real API (installs / tpm toggle /
// enforcement / fix). The remaining panels are the design's interactive
// local-state mock until their backends exist (no fake "saved to server" —
// state persists to localStorage so the controls feel live).
//
// "NO LIES" rule (per the design chat): every DISPLAYED stat is real or an
// honest empty state — never a fabricated number. Where no backend exists
// yet (checks-today, now-blocking, usage/billing $, the Activity check-run
// feed) we render "—" / "no data yet", NOT the prototype's invented figures.
// The interactive localStorage toggles (skin/tone/notif/persona mode) stay —
// they're settings, not fabricated readouts.

type Panel =
  | "repos" | "personas" | "appearance" | "usage" | "notifications" | "account" | "activity";

const PANELS: { id: Panel; idx: string; label: string; badge?: string }[] = [
  { id: "repos", idx: "01", label: "Repositories" },
  { id: "personas", idx: "02", label: "Personas", badge: "5" },
  { id: "appearance", idx: "03", label: "Appearance" },
  { id: "usage", idx: "04", label: "Usage & billing" },
  { id: "notifications", idx: "05", label: "Notifications" },
  { id: "account", idx: "06", label: "Account & org" },
  { id: "activity", idx: "07", label: "Activity" },
];

const PERSONAS = [
  { id: "smasher", code: "F-01", name: "Smasher", img: "grug_smasher.png", desc: "Static analysis + symbolic exec + LLM diff review. Null-derefs, races, off-by-ones.", meta: ["static analysis", "diff review"] },
  { id: "guard", code: "F-02", name: "Guard", img: "grug_guard.png", desc: "SCA, secret scanning, SAST on the diff, hourly CVE feed. Evil shall not pass.", meta: ["SCA + secrets", "SAST on diff"] },
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
          <a className="brand" href="/">
            <span className="brand-mark"><img src="/assets/grug-angry.png" alt="" /></span>
            <span>grug</span>
          </a>
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
            {installList[0] && (
              <div className="rail-foot">
                <div className="plan">
                  <span>{installList[0].account_type === "Organization" ? "ORG" : "USER"}</span>
                  <b>{installList[0].account_login}</b>
                </div>
                <div className="muted">installed {new Date(installList[0].installed_at).toLocaleDateString()}</div>
              </div>
            )}
          </aside>

          <main className="main">
            <ReposPanel show={panel === "repos"} installId={installId} reposLoading={installs.isLoading} fireToast={fireToast} />
            <PersonasPanel show={panel === "personas"} modes={local.personas} setMode={(id, v) => { setLocal({ personas: { ...local.personas, [id]: v } }); fireToast(`${id.toUpperCase()} → <span class="am">${v.toUpperCase()}</span>`); }} />
            <AppearancePanel show={panel === "appearance"} local={local} setLocal={setLocal} fireToast={fireToast} />
            <UsagePanel show={panel === "usage"} />
            <NotificationsPanel show={panel === "notifications"} notif={local.notif} toggle={(id) => setLocal({ notif: { ...local.notif, [id]: !local.notif[id] } })} />
            <AccountPanel show={panel === "account"} me={{ login: me.data.login ?? "you", role: me.data.role ?? "user" }} pauseAll={local.pauseAll} setPause={(v) => { setLocal({ pauseAll: v }); fireToast(v ? 'All personas <span class="am">PAUSED</span>. Grug nap.' : 'Grug <span class="am">AWAKE</span>. Back to work.'); }} />
            <ActivityPanel show={panel === "activity"} installId={installId} />
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
  const reposReady = !repos.isLoading && !repos.isError && installId != null;
  // "no lies": only Repos-guarded has a real backend today. Active-personas,
  // checks-today and now-blocking have no real source yet, so they render an
  // honest em-dash (data unavailable) rather than a fabricated figure. Each is
  // wired as its backend lands (per-persona config, a usage counter, a
  // check-run history store).
  const NA = (
    <span className="v" title="Not wired yet — Grug not count this. Shows real number once the backend lands.">
      —
    </span>
  );
  return (
    <div className="statstrip">
      <div className="cell">
        <div className="k">Repos guarded</div>
        {reposReady
          ? <div className="v">{guarded}<small> / {list.length} installed</small></div>
          : NA}
      </div>
      <div className="cell"><div className="k">Personas active</div>{NA}</div>
      <div className="cell amber"><div className="k">Checks today</div>{NA}</div>
      <div className="cell ink"><div className="k">Now blocking</div>{NA}</div>
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

  let enf: { cls: string; label: string; title: string } | null = null;
  if (on) {
    if (degraded && (state === "grug_managed" || state === "external")) enf = { cls: "unknown", label: "unconfirmed", title: "Last-known state — GitHub was rate-limited and couldn't confirm. Refresh to re-check." };
    else if (state === "grug_managed") enf = { cls: "live", label: "ENFORCED", title: "Grug's DoR check is REQUIRED to merge — a Grug-managed ruleset blocks PRs that fail it." };
    else if (state === "external") enf = { cls: "live", label: "EXTERNAL", title: "The check is required by a non-Grug ruleset or branch protection." };
    else if (state === "none") enf = { cls: "warn", label: "⚠ not enforced", title: "Grug reviews PRs here, but its check is NOT required to merge — a failing PR can still be merged. Click 'fix' to make it required (blocking)." };
    else if (state === "unknown") enf = { cls: "unknown", label: "unknown", title: "Couldn't reach GitHub to determine enforcement. Refresh." };
  }

  return (
    <div className="repo">
      <div className="org">{(owner || "?").slice(0, 2).toUpperCase()}</div>
      <div className="name"><b>{name}</b><span>{owner}/ · {repo.private ? "private" : "public"}</span></div>
      {enf && <span className={`state ${enf.cls}`} title={enf.title}>{enf.label}</span>}
      {on && state === "none" && !fixPending && <button className="fixbtn" onClick={onFix} title="Create a branch ruleset that REQUIRES Grug's check to pass before a PR can merge.">fix</button>}
      {fixPending && <span className="state paused">fixing…</span>}
      {fixError && !fixPending && <span className="fixerr" title={fixError}>⚠ fix failed</span>}
      <span className={`state ${on ? "live" : "paused"}`} title={on ? "GUARDED: Grug watches this repo and posts a Check Run on every PR. Toggle off to silence Grug here." : "PAUSED: Grug is asleep on this repo. Toggle on to guard it."}>{on ? "GUARDED" : "PAUSED"}</span>
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

function UsagePanel({ show }: { show: boolean }) {
  // "no lies": there is no usage-metering or billing backend yet, so this panel
  // shows nothing invented — no fake "19/50 checks", no fake "$72/mo". An
  // honest placeholder stands in until the usage counter + billing land.
  return (
    <section className={`panel${show ? " show" : ""}`}>
      <div className="panel-head">
        <h2>Usage &amp; <em>budget</em>.</h2>
        <p className="note">// Grug cost money to run. One day you cap the daily checks so Grug not eat your whole AWS bill.</p>
      </div>
      <div className="card">
        <div className="card-head">Daily PR-check budget</div>
        <div className="card-body">
          <div className="mono" style={{ fontSize: 12, color: "var(--muted)", padding: "6px 0", lineHeight: 1.7 }}>
            Grug not count checks yet.<br />
            // Usage metering and per-day caps arrive with the billing backend.<br />
            // No made-up numbers here — real counts show once Grug count for real.
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
            <div className="lbl"><b>BYO model key</b><span>Grug use your own LLM key for Elder review. Never leaves your VPC. <span className="mono" style={{ color: "var(--muted)" }}>// storage not wired yet</span></span></div>
            <input className="text" type="password" placeholder="sk-… (not stored yet)" disabled />
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

const _PERSONA_LABEL: Record<string, string> = { chief: "Chief", elder: "Elder" };
const _VERDICT_SYM: Record<string, string> = { block: "×", warn: "!", pass: "✓", errored: "‼" };

function ActivityPanel({ show, installId }: { show: boolean; installId?: number }) {
  const [filter, setFilter] = useState("all");
  // Real Check-verdict feed (PRD #301 / S2). The `verdict` badge is derived
  // server-side; we render it verbatim. "No lies": real rows or an honest
  // empty state — never fabricated.
  const activity = useActivity(installId, filter);
  const rows = activity.data?.activity ?? [];
  // #305: re-run an errored row. Track which rows are mid-re-run so the ↻
  // becomes RE-RUNNING until the feed refreshes with the healed verdict.
  const rerun = useRerun(installId);
  const [rerunning, setRerunning] = useState<Set<string>>(new Set());
  const _rerunKey = (a: { repo: string; pr_number: number; persona: string }) =>
    `${a.repo}#${a.pr_number}#${a.persona}`;
  const doRerun = (a: { repo: string; pr_number: number; persona: string }) => {
    setRerunning((s) => new Set(s).add(_rerunKey(a)));
    rerun.mutate({ repo: a.repo, pr_number: a.pr_number, persona: a.persona });
  };
  // #306: re-run ALL errored rows in one click (outage recovery).
  const rerunAll = useRerunAll(installId);
  const erroredRows = rows.filter((a) => a.verdict === "errored");
  const doRerunAll = () => {
    setRerunning((s) => {
      const n = new Set(s);
      erroredRows.forEach((a) => n.add(_rerunKey(a)));
      return n;
    });
    rerunAll.mutate();
  };
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
              {["all", "block", "warn", "pass", "errored"].map((f) => (
                <button key={f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>{f.toUpperCase()}</button>
              ))}
            </div>
            {erroredRows.length > 0 && (
              <button className="rerun-all-btn" style={{ marginLeft: "auto" }} disabled={rerunAll.isPending}
                onClick={doRerunAll} title="Re-run every errored check">
                ↻ Re-run all errored ({erroredRows.length})
              </button>
            )}
          </div>
          {activity.isLoading && <div className="mono" style={{ fontSize: 12, color: "var(--muted)", padding: 8 }}>loading…</div>}
          {activity.isError && <div className="mono" style={{ fontSize: 12, color: "var(--tomato)", padding: 8 }}>failed to load activity</div>}
          {!activity.isLoading && rows.map((a) => (
            <div className="afeed-row" key={`${a.head_sha}-${a.persona}`}>
              <div className={`vico ${a.verdict}`}>{_VERDICT_SYM[a.verdict] ?? "•"}</div>
              <div className="who2"><b>{_PERSONA_LABEL[a.persona] ?? a.persona}</b> <span className="repo2">{a.repo} · #{a.pr_number}</span><span className="msg">{a.summary}</span></div>
              <span className={`verdict ${a.verdict}`}>{a.verdict.toUpperCase()}</span>
              <span className="ago mono">{a.head_sha.slice(0, 7)}</span>
              {a.verdict === "errored" && (
                rerunning.has(_rerunKey(a))
                  ? <span className="rerunning mono" title="Re-run queued">RE-RUNNING…</span>
                  : <button className="rerun-btn" title="Re-run this check" aria-label="Re-run" onClick={() => doRerun(a)}>↻</button>
              )}
            </div>
          ))}
          {!activity.isLoading && !activity.isError && rows.length === 0 && (
            <div className="mono" style={{ fontSize: 12, color: "var(--muted)", padding: 8, lineHeight: 1.7 }}>
              Grug calm. No runs yet.<br />
              // The Check-Run feed lights up here once Grug start posting verdicts on your PRs.
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
