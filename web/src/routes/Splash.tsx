import { Link } from "react-router-dom";

const APP_INSTALL = "https://github.com/apps/grug-boss/installations/new";
const REPO_URL = "https://github.com/githumps/grug";

const TAPE_ITEMS = [
  "GRUG CRUSH BUG.",
  "GRUG BLOCK EVIL CVE.",
  "GRUG GUARD STRONG CODE.",
  "GRUG RUN PROJECT SMOOTH LIKE ROCK.",
  "GRUG KNOW SDLC.",
  "GRUG OPEN SOURCE.",
  "GRUG SELF-HOSTABLE.",
];

type DorRow = {
  k: string;
  v: string;
  state: "fail" | "warn" | "ok";
};

const DOR_ROWS: DorRow[] = [
  { k: "bug-hunter", v: "2 null-deref paths in parser.go", state: "fail" },
  { k: "sentry", v: "CVE-2026-1144 in lodash@4.17.20", state: "fail" },
  { k: "reviewer", v: "complexity 18 in resolveTree()", state: "warn" },
  { k: "tpm", v: "missing acceptance criteria", state: "fail" },
  { k: "release", v: "changelog · semver hint minor", state: "ok" },
];

type FeatureMini = { k: string; v: string; state: "fail" | "warn" | "ok" };

type Feature = {
  num: string;
  title: string;
  body: string;
  variant: "paper" | "bg2" | "ink" | "amber";
  rows?: FeatureMini[];
  log?: { text: string; tone: "" | "fail" | "ok" | "amber"; ts?: string }[];
  code?: { tag: "k" | "c" | "s" | ""; text: string }[][];
};

const FEATURES: Feature[] = [
  {
    num: "F-01 / bug-hunter",
    title: "Grug crush bug before bug ship.",
    body: "Static analysis + symbolic execution + LLM diff review. Finds null-derefs, race conditions, off-by-ones, and the sneaky logic bugs your linter misses.",
    variant: "paper",
    rows: [
      { k: "null-deref", v: "parser.go:142 · req.body may be nil", state: "fail" },
      { k: "race", v: "cache.go:88 · concurrent map write", state: "fail" },
      { k: "off-by-one", v: "slice[i+1] when len-1", state: "warn" },
      { k: "err-check", v: "all returns handled", state: "ok" },
    ],
  },
  {
    num: "F-02 / sentry",
    title: "Grug block evil virus at gate.",
    body: "SCA on every dependency, secret scanning, SAST on the diff, and a CVE database refreshed hourly. Evil shall not pass.",
    variant: "bg2",
    rows: [
      { k: "CVE-2026-1144", v: "lodash 4.17.20 · prototype pollution · CVSS 8.8", state: "fail" },
      { k: "secret", v: "AWS_SECRET_ACCESS_KEY in .env.example", state: "fail" },
      { k: "SAST", v: "SQL string concat · users.go:201", state: "warn" },
      { k: "deps", v: "317 packages · 0 unmaintained", state: "ok" },
    ],
  },
  {
    num: "F-03 / reviewer",
    title: "Strong code only. Grug not approve weak loop.",
    body: "Line-by-line review for naming, complexity, test coverage and dead code. Posts inline suggestions you can accept with one click.",
    variant: "ink",
    log: [
      { ts: "parser.go", text: "@@ -142,7 +142,12 @@", tone: "" },
      { ts: "  -", text: "for i := 0; i < len(items); i++ {", tone: "fail" },
      { ts: "  +", text: "for i, item := range items {", tone: "ok" },
      { ts: "grug", text: "Grug see C-style loop. Go have range. Use range.", tone: "amber" },
      { ts: "", text: "", tone: "" },
      { ts: "  ×", text: "resolveTree() · cyclomatic 18 (max 10)", tone: "fail" },
      { ts: "  !", text: "coverage 67% on changed lines (target 80%)", tone: "amber" },
      { ts: "  ✓", text: "0 dead exports · 0 unused vars", tone: "ok" },
    ],
  },
  {
    num: "F-04 / tpm + release",
    title: "Grug run project smooth like rock.",
    body: "Definition-of-Ready on every PR. Stale-PR pulse. Auto-changelog with semver hint. Sprint burndown that lives in your repo, not a Jira tab.",
    variant: "amber",
    code: [
      [{ tag: "c", text: "# on PR open" }],
      [
        { tag: "k", text: "grug.tpm" },
        { tag: "", text: "     ✓ acceptance · ✓ estimate · ✗ rollback plan" },
      ],
      [
        { tag: "k", text: "grug.sprint" },
        { tag: "", text: "  burndown · 12 / 18 pts · on track" },
      ],
      [
        { tag: "k", text: "grug.release" },
        { tag: "", text: " changelog drafted · semver: " },
        { tag: "s", text: "minor" },
      ],
      [
        { tag: "k", text: "grug.pulse" },
        { tag: "", text: "   3 PRs stale > 4d · @evan blocked on review" },
      ],
      [{ tag: "c", text: "# merge gated until rollback plan present" }],
    ],
  },
];

type Persona = {
  tag: string;
  title: string;
  desc: string;
  bullets: string[];
  artBg: string;
  icon: React.ReactNode;
};

const PERSONA_ICON_PROPS = {
  viewBox: "0 0 32 32",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2.2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

const PERSONAS: Persona[] = [
  {
    tag: "F-01 · BUG-HUNTER",
    title: "Bug Hunter",
    desc: "Static analysis, symbolic exec, LLM diff review. Catches null-derefs, races, off-by-ones, sloppy error handling.",
    bullets: ["Languages: Go, TS, Py, Rust, Java, Ruby", "Posts inline suggestions you can accept", "Blocks merge on critical findings"],
    artBg: "#fde6df",
    icon: (
      <svg {...PERSONA_ICON_PROPS} className="h-12 w-12">
        <path d="M11 6l3 3-2 2-3-3zM7 11l3 3M21 13l5 5-2 2-5-5zM14 8l11 11" />
        <circle cx="22" cy="22" r="3" />
      </svg>
    ),
  },
  {
    tag: "F-02 · SENTRY",
    title: "Sentry",
    desc: "SCA, secret scanning, SAST on the diff, hourly CVE feed. Quarantines evil dependencies before they reach main.",
    bullets: ["NIST + OSV + GHSA feeds merged", "Inline secret revoke + rotate hint", "License compliance gate (GPL/AGPL/MIT/...)"],
    artBg: "#dde7f0",
    icon: (
      <svg {...PERSONA_ICON_PROPS} className="h-12 w-12">
        <path d="M16 4l11 4v8c0 7-5 12-11 14C10 28 5 23 5 16V8z" />
        <path d="M11 16l4 4 7-8" />
      </svg>
    ),
  },
  {
    tag: "F-03 · REVIEWER",
    title: "Code Reviewer",
    desc: 'Style, naming, complexity, test coverage, dead code. "Strong code only" is the motto. Grumpy but fair.',
    bullets: ["Cyclomatic + cognitive complexity caps", "Per-file test coverage thresholds", "Style profiles or BYO ruleset"],
    artBg: "#dff0d6",
    icon: (
      <svg {...PERSONA_ICON_PROPS} className="h-12 w-12">
        <circle cx="16" cy="16" r="4" />
        <path d="M2 16s5-9 14-9 14 9 14 9-5 9-14 9S2 16 2 16z" />
      </svg>
    ),
  },
  {
    tag: "F-04 · TPM",
    title: "TPM",
    desc: "Definition of Ready, sprint burndown, stale-PR pulse, milestone roll-ups. Project management without leaving the repo.",
    bullets: ["5-check DoR · strict / lenient modes", "Burndown chart on every release branch", "Auto-pings reviewers blocked > 48h"],
    artBg: "#fff2cc",
    icon: (
      <svg {...PERSONA_ICON_PROPS} className="h-12 w-12">
        <rect x="4" y="6" width="24" height="22" rx="1" />
        <path d="M4 12h24M10 4v6M22 4v6M9 18h4M9 22h8" />
      </svg>
    ),
  },
  {
    tag: "F-05 · RELEASE",
    title: "Release Manager",
    desc: "Drafts the changelog from merged PRs, picks semver, gates the deploy on staging health, posts release notes to Slack.",
    bullets: ["Conventional Commits or freeform", "Semver hint: major / minor / patch", "Deploy gate on Datadog SLO breach"],
    artBg: "#e8e0ff",
    icon: (
      <svg {...PERSONA_ICON_PROPS} className="h-12 w-12">
        <path d="M16 3l3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1z" />
      </svg>
    ),
  },
  {
    tag: "CUSTOM",
    title: "Bring your own Grug.",
    desc: "Define a persona in YAML or TS. Hook into the same Check Run pipeline. Grug ecosystem, your rules.",
    bullets: ["grug.config.ts in repo root", "Reuse Grug's diff parser + GitHub plumbing", "Publish to the marketplace if you want"],
    artBg: "#f3ecdb",
    icon: <span className="font-display text-[62px] leading-none">+</span>,
  },
];

type Skill = {
  kind: "CAPABILITY" | "SKIN" | "STYLE PACK" | "CUSTOM" | "PRO";
  badge?: string;
  title: string;
  body: string;
  price: string;
  free?: boolean;
  installed?: boolean;
  art: React.ReactNode;
};

const SKILLS: Skill[] = [
  {
    kind: "CAPABILITY",
    badge: "DEVOPS",
    title: "Terraform Plan Reviewer",
    body: "Reads terraform plan output, flags destructive changes, drift, and IAM blast-radius before apply.",
    price: "$6/mo",
    art: (
      <div className="grid h-full place-items-center" style={{ background: "#e8e0ff" }}>
        <svg viewBox="0 0 80 80" className="w-3/5" style={{ color: "#5c4dff" }} fill="currentColor">
          <path d="M30 12l20 12v24L30 36zM52 24l20 12v24L52 48zM30 38l20 12v24L30 62zM8 24l20 12v24L8 48z" />
        </svg>
      </div>
    ),
  },
  {
    kind: "CAPABILITY",
    badge: "API",
    title: "GraphQL Breaking-Change Guard",
    body: "Diffs your .graphql schema vs main, flags removed fields, type changes, and orphaned queries in callers.",
    price: "$5/mo",
    art: (
      <div className="grid h-full place-items-center" style={{ background: "#fde6df" }}>
        <svg viewBox="0 0 80 80" className="w-[62%]" fill="none" stroke="#e0502a" strokeWidth={3.5}>
          <path d="M40 8L70 26v28L40 72 10 54V26z" />
          <circle cx="40" cy="8" r="6" fill="#e0502a" />
          <circle cx="70" cy="26" r="6" fill="#e0502a" />
          <circle cx="70" cy="54" r="6" fill="#e0502a" />
          <circle cx="40" cy="72" r="6" fill="#e0502a" />
          <circle cx="10" cy="54" r="6" fill="#e0502a" />
          <circle cx="10" cy="26" r="6" fill="#e0502a" />
        </svg>
      </div>
    ),
  },
  {
    kind: "CAPABILITY",
    badge: "FRONTEND",
    title: "A11y Inspector",
    body: "Runs axe-core on changed components, blocks merge on contrast, label, and ARIA failures. WCAG 2.2 AA out of the box.",
    price: "$4/mo",
    art: (
      <div className="grid h-full place-items-center" style={{ background: "#dff0d6" }}>
        <svg viewBox="0 0 80 80" className="w-[54%]" fill="none" stroke="#3f6b3a" strokeWidth={3.5}>
          <circle cx="40" cy="14" r="6" fill="#3f6b3a" />
          <path d="M14 30h52M40 30v18M22 70l18-22 18 22" />
        </svg>
      </div>
    ),
  },
  {
    kind: "CAPABILITY",
    badge: "DATA",
    title: "SQL Migration Reviewer",
    body: "Catches non-idempotent migrations, missing indexes, table locks > 5s, and rollback gaps. Postgres + MySQL.",
    price: "$5/mo",
    art: (
      <div className="grid h-full place-items-center" style={{ background: "#fff2cc" }}>
        <svg viewBox="0 0 80 80" className="w-[62%]" fill="none" stroke="#d97706" strokeWidth={3.5}>
          <ellipse cx="40" cy="20" rx="22" ry="8" />
          <path d="M18 20v40c0 4 10 8 22 8s22-4 22-8V20" />
          <path d="M18 36c0 4 10 8 22 8s22-4 22-8M18 52c0 4 10 8 22 8s22-4 22-8" />
        </svg>
      </div>
    ),
  },
  {
    kind: "SKIN",
    title: "Classic Grug",
    body: "Angry caveman. Wooden club. Orange polka-dot vest. The Grug your repo deserves.",
    price: "FREE",
    free: true,
    installed: true,
    art: (
      <div className="grid h-full place-items-center" style={{ background: "#fff3d6" }}>
        <img src="/grug-angry.png" alt="" className="w-[88%] -rotate-3" />
      </div>
    ),
  },
  {
    kind: "PRO",
    title: "Professional Grug",
    body: 'Suit, tie, calmer mouth. Replaces caveman speak with passive-aggressive memos. For "serious" repos.',
    price: "$4/mo",
    art: (
      <div className="grid h-full place-items-center" style={{ background: "#dde7f0" }}>
        <svg viewBox="0 0 100 100" className="w-3/5" fill="none" stroke="#181613" strokeWidth={3}>
          <ellipse cx="50" cy="48" rx="30" ry="32" fill="#f4e3c4" />
          <path d="M30 50h40" />
          <circle cx="42" cy="46" r="2.5" fill="#181613" />
          <circle cx="58" cy="46" r="2.5" fill="#181613" />
          <path d="M44 60h12" />
          <path d="M30 88l10-10h20l10 10v6H30z" fill="#1f2937" />
          <path d="M48 78l2 8 2-8" fill="#e0502a" stroke="#181613" strokeWidth={2} />
        </svg>
      </div>
    ),
  },
  {
    kind: "STYLE PACK",
    title: "Mullet, Mohawk, Bald",
    body: 'Three new haircuts plus the leather-jacket outfit. Reaction set: "club smash" GIF and a thumbs-up.',
    price: "$9 once",
    art: (
      <div className="grid h-full place-items-center" style={{ background: "#ffe8cf" }}>
        <svg viewBox="0 0 100 100" className="w-3/5" fill="none" stroke="#181613" strokeWidth={3}>
          <ellipse cx="50" cy="50" rx="30" ry="32" fill="#f4e3c4" />
          <path d="M22 38c0-8 14-16 28-16s28 8 28 16-2 6-8 8H30c-6-2-8-4-8-8z" fill="#181613" />
          <path d="M50 76c8 6 18 12 22 18l-6 2-12-8-4-12z" fill="#181613" />
          <circle cx="42" cy="50" r="2.5" fill="#181613" />
          <circle cx="58" cy="50" r="2.5" fill="#181613" />
          <path d="M44 64h12" />
        </svg>
      </div>
    ),
  },
  {
    kind: "CUSTOM",
    title: "Custom Reactions",
    body: "Bring-your-own-emoji and override the default failure copy. Great for in-jokes and angrier teams.",
    price: "$3/mo",
    art: (
      <div className="grid h-full place-items-center" style={{ background: "#e8e0ff" }}>
        <svg viewBox="0 0 100 100" className="w-3/5" fill="none" stroke="#181613" strokeWidth={3}>
          <path d="M30 18l4-8 4 8 12 4-8 6 4 12-12-6-12 6 4-12-8-6z" fill="#fbbf24" />
          <ellipse cx="50" cy="58" rx="30" ry="32" fill="#f4e3c4" />
          <circle cx="42" cy="56" r="2.5" fill="#181613" />
          <circle cx="58" cy="56" r="2.5" fill="#181613" />
          <path d="M40 72l4 4 6-2 6 2 4-4" />
        </svg>
      </div>
    ),
  },
];

type Tier = {
  name: string;
  italic?: boolean;
  price: string;
  unit: string;
  pop?: boolean;
  feats: { text: string; no?: boolean }[];
  cta: string;
  ctaHref: string;
  ghost?: boolean;
};

const TIERS: Tier[] = [
  {
    name: "Free",
    price: "$0",
    unit: "/forever",
    feats: [
      { text: "50 PR checks / day" },
      { text: "Classic Grug skin" },
      { text: "1 persona (TPM)" },
      { text: "1 repo" },
      { text: "Custom reactions", no: true },
      { text: "Multi-org", no: true },
    ],
    cta: "Sign up →",
    ctaHref: "/signin",
    ghost: true,
  },
  {
    name: "Pro",
    price: "$12",
    unit: "/mo · per seat",
    pop: true,
    feats: [
      { text: "Unlimited PR checks" },
      { text: "All personas" },
      { text: "Up to 10 repos" },
      { text: "Professional Grug skin" },
      { text: "Custom reactions add-on" },
      { text: "Audit logs", no: true },
    ],
    cta: "Start Pro →",
    ctaHref: "/signin",
  },
  {
    name: "Org",
    price: "$48",
    unit: "/mo · org",
    feats: [
      { text: "Everything in Pro" },
      { text: "Unlimited repos" },
      { text: "Multi-org" },
      { text: "Audit logs · SAML" },
      { text: "Priority support" },
      { text: "Custom personas (beta)" },
    ],
    cta: "Talk to Grug →",
    ctaHref: "mailto:grug@grug.lol",
    ghost: true,
  },
  {
    name: "Self-host",
    italic: true,
    price: "$0",
    unit: "+ AWS bill (~$2/mo)",
    feats: [
      { text: "AGPL-3.0 source" },
      { text: "Pulumi up · 15 min" },
      { text: "Your data, your VPC" },
      { text: "All features unlocked" },
      { text: "Hosted SLA", no: true },
      { text: "Skill marketplace", no: true },
    ],
    cta: "Read the docs →",
    ctaHref: REPO_URL,
    ghost: true,
  },
];

const QUOTES = [
  {
    q: '"You import lodash 4.17.20. Lodash 4.17.20 have hole. Hole let bad man in cave. Grug not let bad man in cave. Grug block."',
    who: "— SENTRY persona · CVE-2026-1144 · blocking",
  },
  {
    q: '"resolveTree() have eighteen branch. Grug brain have ten branch. If Grug brain explode, you fix. Refactor."',
    who: "— REVIEWER persona · run #20114 · warning",
  },
  {
    q: '"Bug in parser.go line 142. Body of request maybe nil. You crash on Tuesday. Grug fix Monday. Grug heroic."',
    who: "— BUG-HUNTER persona · run #20119 · blocking",
  },
];

function GitHubIcon({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" className={className}>
      <path d="M12 .5C5.65.5.5 5.65.5 12c0 5.08 3.29 9.39 7.86 10.92.58.11.79-.25.79-.56 0-.27-.01-1-.02-1.97-3.2.7-3.87-1.54-3.87-1.54-.52-1.32-1.27-1.67-1.27-1.67-1.04-.71.08-.7.08-.7 1.15.08 1.76 1.18 1.76 1.18 1.02 1.75 2.69 1.24 3.34.95.1-.74.4-1.24.72-1.53-2.55-.29-5.24-1.27-5.24-5.66 0-1.25.45-2.27 1.18-3.07-.12-.29-.51-1.46.11-3.04 0 0 .96-.31 3.16 1.17a11 11 0 0 1 5.76 0c2.2-1.48 3.16-1.17 3.16-1.17.62 1.58.23 2.75.11 3.04.74.8 1.18 1.82 1.18 3.07 0 4.4-2.7 5.36-5.27 5.65.41.35.78 1.05.78 2.13 0 1.54-.01 2.78-.01 3.16 0 .31.21.68.8.56A11.51 11.51 0 0 0 23.5 12C23.5 5.65 18.35.5 12 .5z" />
    </svg>
  );
}

const ROW_BG: Record<DorRow["state"], string> = {
  fail: "#fde6df",
  warn: "#fff2cc",
  ok: "#dff0d6",
};

const ROW_BADGE: Record<DorRow["state"], { label: string; bg: string; color: string }> = {
  fail: { label: "block", bg: "#e0502a", color: "#fff" },
  warn: { label: "warn", bg: "#fbbf24", color: "#181613" },
  ok: { label: "pass", bg: "#3f6b3a", color: "#fff" },
};

function MiniRow({ row }: { row: FeatureMini }) {
  const badge = ROW_BADGE[row.state];
  return (
    <div
      className="flex items-center gap-2 border-[1.5px] border-cave-ink px-2.5 py-1.5"
      style={{ background: ROW_BG[row.state] }}
    >
      <span className="min-w-[96px] font-bold">{row.k}</span>
      <span className="flex-1 text-cave-ink2">{row.v}</span>
      <span
        className="border-[1.5px] border-cave-ink px-1.5 py-0.5 text-[10px] font-bold"
        style={{ background: badge.bg, color: badge.color }}
      >
        {badge.label}
      </span>
    </div>
  );
}

function Eyebrow({ children, amber = false }: { children: React.ReactNode; amber?: boolean }) {
  return (
    <span
      className={`inline-flex items-center gap-2 border-2 border-cave-ink px-3 py-1.5 font-mono text-[12px] font-bold uppercase tracking-[0.12em] ${amber ? "bg-cave-amber" : "bg-cave-amber"} text-cave-ink`}
    >
      {children}
    </span>
  );
}

function FeatureCard({ feat, span, dark }: { feat: Feature; span: string; dark?: boolean }) {
  const bg =
    feat.variant === "paper"
      ? "bg-cave-paper"
      : feat.variant === "bg2"
        ? "bg-cave-bg2"
        : feat.variant === "ink"
          ? "bg-[#0e0c0a] text-cave-bg"
          : "bg-cave-amber";
  return (
    <div
      className={`relative flex min-h-[280px] flex-col border-r-2 border-b-2 border-cave-ink px-7 py-8 ${span} ${bg}`}
    >
      <div className="font-mono text-[11px] uppercase tracking-[0.1em] opacity-80">{feat.num}</div>
      <h3 className="mt-2 mb-3 font-display text-[36px] leading-[1.05] tracking-[-0.02em]">{feat.title}</h3>
      <p className="mb-4 max-w-[46ch] text-[14.5px] leading-[1.55]">{feat.body}</p>
      <div className="mt-auto">
        {feat.rows && (
          <div className="grid gap-1.5 font-mono text-[12.5px]">
            {feat.rows.map((r) => (
              <MiniRow key={r.k + r.v} row={r} />
            ))}
          </div>
        )}
        {feat.log && (
          <div className="border-2 border-cave-bg bg-[#0e0c0a] p-3.5 font-mono text-[12px] text-cave-bg">
            {feat.log.map((l, i) => (
              <div
                key={i}
                className={`flex gap-2.5 py-[3px] ${
                  l.tone === "fail"
                    ? "text-[#ff8a6e]"
                    : l.tone === "ok"
                      ? "text-[#9fd58a]"
                      : l.tone === "amber"
                        ? "text-cave-amber"
                        : ""
                }`}
              >
                <span className="min-w-[46px] text-[#8a7f6f]">{l.ts}</span>
                <span className="whitespace-pre">{l.text}</span>
              </div>
            ))}
          </div>
        )}
        {feat.code && (
          <div className="border-2 border-cave-ink bg-cave-paper p-3.5 font-mono text-[12.5px] shadow-brutal-sm">
            {feat.code.map((line, i) => (
              <div key={i}>
                {line.map((seg, j) => (
                  <span
                    key={j}
                    className={
                      seg.tag === "k"
                        ? "font-bold text-cave-tomato"
                        : seg.tag === "c"
                          ? "text-cave-muted"
                          : seg.tag === "s"
                            ? "text-cave-moss"
                            : ""
                    }
                  >
                    {seg.text}
                  </span>
                ))}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
  void dark;
}

function PersonaCard({ p }: { p: Persona }) {
  return (
    <article className="flex flex-col border-2 border-cave-ink bg-cave-paper shadow-brutal-sm">
      <div
        className="grid aspect-[3/2] place-items-center border-b-2 border-cave-ink"
        style={{ background: p.artBg }}
      >
        <div className="text-cave-ink">{p.icon}</div>
      </div>
      <div className="flex flex-1 flex-col gap-2 p-4">
        <div className="font-mono text-[11px] font-bold uppercase tracking-[0.1em] text-cave-muted">
          {p.tag}
        </div>
        <h3 className="font-display text-[28px] leading-[1.05] tracking-[-0.02em]">{p.title}</h3>
        <p className="font-mono text-[12px] leading-[1.5] text-cave-ink2">{p.desc}</p>
        <ul className="mt-1 flex flex-col gap-1 border-t-2 border-dashed border-cave-ink pt-2 font-mono text-[12px]">
          {p.bullets.map((b) => (
            <li key={b} className="flex gap-2 text-cave-ink2">
              <span className="text-cave-amber-deep">▪</span>
              <span>{b}</span>
            </li>
          ))}
        </ul>
      </div>
    </article>
  );
}

function SkillCard({ s }: { s: Skill }) {
  const tagBg = s.kind === "CAPABILITY" ? "#3f6b3a" : s.kind === "PRO" ? "#181613" : s.kind === "CUSTOM" ? "#e0502a" : "#fbbf24";
  const tagColor = s.kind === "CAPABILITY" || s.kind === "PRO" || s.kind === "CUSTOM" ? "#fff" : "#181613";
  return (
    <article className="flex flex-col border-2 border-cave-ink bg-cave-paper shadow-brutal-sm transition-all duration-150 hover:-translate-x-0.5 hover:-translate-y-0.5 hover:shadow-brutal">
      <div className="aspect-square overflow-hidden border-b-2 border-cave-ink">{s.art}</div>
      <div className="flex flex-1 flex-col gap-1.5 p-4">
        <div className="flex items-center gap-2">
          <span
            className="border-[1.5px] border-cave-ink px-1.5 py-0.5 font-mono text-[10px] font-bold"
            style={{ background: tagBg, color: tagColor }}
          >
            {s.kind}
          </span>
          {s.badge && (
            <span className="border-[1.5px] border-cave-ink px-1.5 py-0.5 font-mono text-[10px] font-bold">
              {s.badge}
            </span>
          )}
          {s.installed && (
            <span
              className="border-[1.5px] border-cave-ink px-1.5 py-0.5 font-mono text-[10px] font-bold"
              style={{ background: "#3f6b3a", color: "#fff" }}
            >
              INCLUDED
            </span>
          )}
        </div>
        <h4 className="font-display text-[22px] leading-[1.05]">{s.title}</h4>
        <p className="font-mono text-[12px] leading-[1.45] text-cave-ink2">{s.body}</p>
      </div>
      <div className="flex items-center justify-between border-t-2 border-dashed border-cave-ink px-4 py-3 font-mono">
        <span className={`text-[14px] font-bold ${s.free ? "text-cave-moss" : ""}`}>{s.price}</span>
        <button
          type="button"
          className={`border-2 border-cave-ink px-2.5 py-1.5 text-[11px] font-bold ${
            s.installed ? "bg-cave-moss text-white" : "bg-cave-ink text-cave-bg hover:bg-cave-amber hover:text-cave-ink"
          }`}
        >
          {s.installed ? "INSTALLED ✓" : "+ ADD"}
        </button>
      </div>
    </article>
  );
}

export function Splash() {
  return (
    <div className="min-h-screen overflow-x-hidden bg-cave-bg font-grotesk text-cave-ink">
      {/* TAPE */}
      <div className="overflow-hidden whitespace-nowrap border-b-2 border-cave-ink bg-cave-ink py-2 font-mono text-[12px] text-cave-bg">
        <div className="inline-block animate-tape-scroll pl-[100%]">
          {[...TAPE_ITEMS, ...TAPE_ITEMS].map((t, i) => (
            <span key={i}>
              <span className="mx-6">{t}</span>
              <span className="mx-3 text-cave-amber">●</span>
            </span>
          ))}
        </div>
      </div>

      {/* HEADER */}
      <header className="sticky top-0 z-40 border-b-2 border-cave-ink bg-cave-bg">
        <div className="mx-auto flex max-w-[1240px] items-center gap-6 px-7 py-3.5">
          <a href="#" className="flex items-center gap-2.5 font-display text-[30px] leading-none tracking-[-0.02em]">
            <span className="grid h-9 w-9 place-items-center overflow-hidden rounded-full border-2 border-cave-ink bg-cave-amber">
              <img src="/grug-angry.png" alt="" className="h-9 w-9 scale-150 object-cover" />
            </span>
            <span>grug</span>
          </a>
          <nav className="ml-auto hidden items-center gap-6 md:flex">
            <a href="#features" className="border-b-2 border-transparent py-1.5 font-mono text-[13px] font-medium text-cave-ink hover:border-cave-ink">
              What Grug do
            </a>
            <a href="#personas" className="border-b-2 border-transparent py-1.5 font-mono text-[13px] font-medium text-cave-ink hover:border-cave-ink">
              Personas
            </a>
            <a href="#skills" className="border-b-2 border-transparent py-1.5 font-mono text-[13px] font-medium text-cave-ink hover:border-cave-ink">
              Skills
            </a>
            <a href="#pricing" className="border-b-2 border-transparent py-1.5 font-mono text-[13px] font-medium text-cave-ink hover:border-cave-ink">
              Pricing
            </a>
            <a href={REPO_URL} className="border-b-2 border-transparent py-1.5 font-mono text-[13px] font-medium text-cave-ink hover:border-cave-ink">
              Docs
            </a>
          </nav>
          <Link
            to="/signin"
            className="ml-auto inline-flex items-center gap-2 border-2 border-cave-ink bg-cave-ink px-4 py-2.5 font-mono text-[13px] font-bold text-cave-bg shadow-brutal-sm transition-all hover:-translate-x-px hover:-translate-y-px hover:shadow-[4px_4px_0_0_#181613] active:translate-x-0.5 active:translate-y-0.5 active:shadow-none md:ml-0"
          >
            <GitHubIcon />
            Sign in
          </Link>
          <a
            href={APP_INSTALL}
            className="inline-flex items-center gap-2 border-2 border-cave-ink bg-cave-amber px-4 py-2.5 font-mono text-[13px] font-bold text-cave-ink shadow-brutal-sm transition-all hover:-translate-x-px hover:-translate-y-px hover:shadow-[4px_4px_0_0_#181613] active:translate-x-0.5 active:translate-y-0.5 active:shadow-none"
          >
            Install Grug →
          </a>
        </div>
      </header>

      <main className="mx-auto max-w-[1240px] px-7">
        {/* HERO */}
        <section
          id="hero"
          className="grid grid-cols-1 items-center gap-12 border-b-2 border-cave-ink py-16 lg:grid-cols-[1.05fr_0.95fr] lg:pb-24 lg:pt-16"
        >
          <div>
            <div className="inline-flex items-center gap-2.5 border-2 border-cave-ink bg-cave-amber px-3 py-1.5 font-mono text-[12px] font-bold uppercase tracking-[0.12em] text-cave-ink">
              <span className="h-2 w-2 animate-blink rounded-full bg-cave-ink" />
              v0.7.0 — bug-crusher + sentry shipped
            </div>
            <h1 className="my-5 font-display font-normal leading-[0.95] tracking-[-0.025em] text-[clamp(54px,7.6vw,112px)]">
              Grug{" "}
              <span className="relative inline-block">
                code monkey
                <span
                  className="absolute left-[-2%] right-[-2%] top-[55%] h-1.5 -rotate-2"
                  style={{ background: "#181613" }}
                />
              </span>
              .<br />
              Grug{" "}
              <span className="italic text-cave-amber-deep">
                whole <em className="not-italic font-display italic">cave</em>
              </span>
              .
            </h1>
            <p className="max-w-[560px] text-[19px] leading-[1.55] text-cave-ink2">
              One grumpy caveman. Whole software lifecycle. Grug crush bug, block evil CVE, gate weak code, run project
              smooth like rock.{" "}
              <span className="border border-cave-ink bg-cave-bg2 px-1.5 font-mono text-[14px]">Grug know SDLC.</span> Grug
              live in GitHub, post Check Runs, never spam comments. <b>You ship. Grug guard.</b>
            </p>
            <div className="mt-7 flex flex-wrap gap-3.5">
              <a
                href={APP_INSTALL}
                className="inline-flex items-center gap-2 border-2 border-cave-ink bg-cave-amber px-5 py-3.5 font-mono text-[14px] font-bold text-cave-ink shadow-brutal transition-all hover:-translate-x-px hover:-translate-y-px hover:shadow-[7px_7px_0_0_#181613]"
              >
                <GitHubIcon />
                Install Grug on GitHub
              </a>
              <Link
                to="/dashboard"
                className="inline-flex items-center gap-2 border-2 border-cave-ink bg-cave-bg px-5 py-3.5 font-mono text-[14px] font-bold text-cave-ink shadow-brutal transition-all hover:-translate-x-px hover:-translate-y-px hover:shadow-[7px_7px_0_0_#181613]"
              >
                Open dashboard →
              </Link>
            </div>
            <div className="mt-7 flex flex-wrap gap-5 font-mono text-[12px] text-cave-muted">
              <span className="border-[1.5px] border-cave-ink bg-cave-bg2 px-2.5 py-1.5">AGPL-3.0</span>
              <span>
                <b className="font-bold text-cave-ink">4.2k</b> ★ on github
              </span>
              <span>
                <b className="font-bold text-cave-ink">1.8M</b> checks run · last 30d
              </span>
              <span>
                <b className="font-bold text-cave-ink">12,408</b> CVEs blocked
              </span>
              <span>
                <b className="font-bold text-cave-ink">$0</b> to self-host
              </span>
            </div>
          </div>

          {/* HERO STAGE */}
          <div className="relative ml-auto aspect-[1/1.05] w-full max-w-[560px]">
            <div
              className="absolute inset-0 border-2 border-cave-ink shadow-brutal-lg"
              style={{
                background:
                  "repeating-linear-gradient(45deg, #fbbf24 0 18px, transparent 18px 36px), #fffbf2",
              }}
            >
              <div className="absolute inset-3.5 border-2 border-cave-ink bg-cave-paper" />
            </div>
            {/* Angry sticker */}
            <div
              className="absolute -left-5 -top-5 z-30 -rotate-[8deg] border-2 border-cave-ink bg-cave-tomato px-3.5 py-2.5 font-mono text-[13px] font-bold text-white shadow-brutal-sm"
            >
              GRUG.MOOD = ANGRY
            </div>
            {/* Floaties */}
            <div className="absolute right-[-30px] top-8 z-20 rotate-[4deg] border-2 border-cave-ink bg-cave-amber px-2.5 py-1.5 font-mono text-[12px] shadow-brutal-sm">
              + 4 personas active
            </div>
            <div className="absolute right-[-50px] top-[46%] z-20 -rotate-[3deg] border-2 border-cave-ink bg-cave-paper px-2.5 py-1.5 font-mono text-[12px] shadow-brutal-sm">
              CLUB.SWING() ←
            </div>
            {/* Grug */}
            <img
              src="/grug-angry.png"
              alt="Grug, the grumpy caveman, holding a wooden club"
              className="pointer-events-none absolute left-1/2 top-[54%] z-10 w-[88%] -translate-x-1/2 -translate-y-1/2 -rotate-2"
              style={{ filter: "drop-shadow(8px 10px 0 rgba(0,0,0,0.15))" }}
            />
            {/* DoR card */}
            <div className="absolute -bottom-9 -left-11 z-40 w-[380px] max-w-[80%] -rotate-[1.5deg] border-2 border-cave-ink bg-cave-paper shadow-brutal">
              <div className="flex items-center gap-2.5 border-b-2 border-cave-ink bg-cave-bg2 px-3.5 py-2.5 font-mono text-[12px] font-bold">
                <div className="flex gap-1.5">
                  <span className="h-2.5 w-2.5 rounded-full border-[1.5px] border-cave-ink bg-[#ff5f57]" />
                  <span className="h-2.5 w-2.5 rounded-full border-[1.5px] border-cave-ink bg-[#febc2e]" />
                  <span className="h-2.5 w-2.5 rounded-full border-[1.5px] border-cave-ink bg-[#28c840]" />
                </div>
                <span>checks/grug</span>
                <span className="ml-auto text-cave-muted">PR #358 · main ← graphify</span>
              </div>
              <div className="flex items-baseline gap-2.5 px-3.5 pb-2 pt-3.5 font-mono">
                <span className="grid h-5 w-5 place-items-center border-2 border-cave-ink bg-cave-tomato text-[12px] font-bold text-white">
                  ×
                </span>
                <h4 className="m-0 text-[14px] font-bold">Grug · 4 of 5 personas blocking</h4>
                <span className="ml-auto text-[12px] text-cave-ink2">
                  <b className="text-cave-tomato">11/16 pass</b> · 3 blocking
                </span>
              </div>
              <table className="w-full border-collapse font-mono text-[12px]">
                <thead>
                  <tr>
                    <th className="border-t-2 border-cave-ink px-3.5 py-2 text-left text-[10px] uppercase tracking-[0.1em] text-cave-muted">
                      Persona
                    </th>
                    <th className="border-t-2 border-cave-ink px-3.5 py-2 text-left text-[10px] uppercase tracking-[0.1em] text-cave-muted">
                      Verdict
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {DOR_ROWS.map((r) => {
                    const badge = ROW_BADGE[r.state];
                    return (
                      <tr key={r.k}>
                        <td className="w-[108px] border-t border-dashed border-cave-ink px-3.5 py-2">
                          <b>{r.k}</b>
                        </td>
                        <td className="border-t border-dashed border-cave-ink px-3.5 py-2">
                          {r.v}
                          <span
                            className="ml-1.5 inline-block border-[1.5px] border-cave-ink px-1.5 align-middle text-[10px] font-bold"
                            style={{ background: badge.bg, color: badge.color }}
                          >
                            {badge.label}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        </section>

        {/* LOGOS */}
        <div className="flex flex-wrap items-center gap-9 border-b-2 border-cave-ink py-7 font-mono text-[12px] text-cave-ink2">
          <span>
            <b className="text-cave-ink">USED BY TEAMS AT</b>
          </span>
          {["NORTHWIND.SH", "HAMMERSMITH&CO", "CONTOSO/PLATFORM", "OBSIDIAN-LABS", "BLACK-BOX-AI", "+ 412 MORE REPOS"].map(
            (l) => (
              <span key={l} className="flex items-center gap-9">
                <span className="h-1.5 w-1.5 rounded-full bg-cave-ink" />
                {l}
              </span>
            ),
          )}
        </div>

        {/* FEATURES */}
        <section id="features" className="border-b-2 border-cave-ink py-20">
          <div className="mb-12 flex flex-wrap items-end justify-between gap-6">
            <div>
              <Eyebrow>02 · what grug do</Eyebrow>
              <h2 className="mt-3.5 max-w-[760px] font-display text-[clamp(40px,5vw,72px)] font-normal leading-[0.96] tracking-[-0.02em]">
                Grug do <em className="not-italic text-cave-amber-deep">whole cave</em>.<br />
                From <em className="not-italic text-cave-amber-deep">idea</em> to{" "}
                <em className="not-italic text-cave-amber-deep">ship</em>.
              </h2>
            </div>
            <p className="max-w-[340px] font-mono text-[14px] text-cave-ink2">
              // One GitHub App. Five personas. Posts as Check Runs so branch-protection rules can require any of them.
              Toggle the ones your team needs.
            </p>
          </div>

          <div className="grid grid-cols-1 overflow-hidden border-2 border-cave-ink bg-cave-paper shadow-brutal lg:grid-cols-12 lg:[&>*:nth-child(1)]:col-span-7 lg:[&>*:nth-child(2)]:col-span-5 lg:[&>*:nth-child(2)]:border-r-0 lg:[&>*:nth-child(3)]:col-span-5 lg:[&>*:nth-child(3)]:border-b-0 lg:[&>*:nth-child(4)]:col-span-7 lg:[&>*:nth-child(4)]:border-b-0 lg:[&>*:nth-child(4)]:border-r-0">
            {FEATURES.map((f) => (
              <FeatureCard key={f.num} feat={f} span="" />
            ))}
          </div>
        </section>

        {/* PERSONAS */}
        <section id="personas" className="border-b-2 border-cave-ink py-20">
          <div className="mb-12 flex flex-wrap items-end justify-between gap-6">
            <div>
              <Eyebrow>03 · the personas</Eyebrow>
              <h2 className="mt-3.5 max-w-[760px] font-display text-[clamp(40px,5vw,72px)] font-normal leading-[0.96] tracking-[-0.02em]">
                Five Grugs. One <em className="not-italic text-cave-amber-deep">cave</em>.
              </h2>
            </div>
            <p className="max-w-[340px] font-mono text-[14px] text-cave-ink2">
              // Each persona is its own Check Run. Require any of them in branch protection. Toggle per-repo. BYO model
              key on Pro.
            </p>
          </div>
          <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-3">
            {PERSONAS.map((p) => (
              <PersonaCard key={p.title} p={p} />
            ))}
          </div>
        </section>

        {/* SKILLS */}
        <section id="skills" className="-mx-7 border-b-2 border-cave-ink bg-cave-bg2 px-7 py-20">
          <div className="mb-12 flex flex-wrap items-end justify-between gap-6">
            <div>
              <Eyebrow>04 · skill marketplace</Eyebrow>
              <h2 className="mt-3.5 max-w-[760px] font-display text-[clamp(40px,5vw,72px)] font-normal leading-[0.96] tracking-[-0.02em]">
                Teach Grug a <em className="not-italic text-cave-amber-deep">new trick</em>.<br />
                Or a <em className="not-italic text-cave-amber-deep">new tie</em>.
              </h2>
            </div>
            <p className="max-w-[340px] font-mono text-[14px] text-cave-ink2">
              // Drop-in skills extend Grug. Some are capabilities (Terraform plan review, GraphQL breaking-change). Some
              are cosmetic skins. Classic Grug is free forever.
            </p>
          </div>
          <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
            {SKILLS.map((s) => (
              <SkillCard key={s.title} s={s} />
            ))}
          </div>
        </section>

        {/* PRICING */}
        <section id="pricing" className="-mx-7 border-b-2 border-cave-ink px-7 py-20">
          <div className="mb-12 flex flex-wrap items-end justify-between gap-6">
            <div>
              <Eyebrow>05 · pricing</Eyebrow>
              <h2 className="mt-3.5 max-w-[760px] font-display text-[clamp(40px,5vw,72px)] font-normal leading-[0.96] tracking-[-0.02em]">
                Pay Grug, or <em className="not-italic text-cave-amber-deep">be Grug.</em>
              </h2>
            </div>
            <p className="max-w-[340px] font-mono text-[14px] text-cave-ink2">
              // Self-host is free forever — Grug is AGPL-3.0. SaaS tiers exist because someone has to pay the AWS bill.
            </p>
          </div>

          <div className="grid grid-cols-1 gap-6 sm:grid-cols-2 lg:grid-cols-4">
            {TIERS.map((t) => (
              <div
                key={t.name}
                className={`relative flex flex-col gap-4 border-2 border-cave-ink p-6 shadow-brutal-sm ${
                  t.pop ? "bg-cave-amber" : "bg-cave-paper"
                }`}
              >
                {t.pop && (
                  <span className="absolute -right-3 -top-3 -rotate-3 border-2 border-cave-ink bg-cave-ink px-2.5 py-1 font-mono text-[10px] font-bold text-cave-bg shadow-brutal-sm">
                    GRUG PICK
                  </span>
                )}
                <div
                  className={`font-display text-[28px] leading-none ${t.italic ? "italic" : ""}`}
                >
                  {t.name}
                </div>
                <div className="flex items-baseline gap-2">
                  <b className="font-display text-[44px] leading-none">{t.price}</b>
                  <span className="font-mono text-[12px] text-cave-ink2">{t.unit}</span>
                </div>
                <ul className="flex flex-1 flex-col gap-1.5 border-t-2 border-dashed border-cave-ink pt-4 font-mono text-[12.5px]">
                  {t.feats.map((f) => (
                    <li
                      key={f.text}
                      className={`flex items-baseline gap-2 ${f.no ? "text-cave-muted line-through" : "text-cave-ink"}`}
                    >
                      <span className={f.no ? "text-cave-muted" : "text-cave-amber-deep"}>
                        {f.no ? "×" : "✓"}
                      </span>
                      {f.text}
                    </li>
                  ))}
                </ul>
                <a
                  href={t.ctaHref}
                  className={`inline-flex w-full items-center justify-center border-2 border-cave-ink px-4 py-3 font-mono text-[13px] font-bold shadow-brutal-sm transition-all hover:-translate-x-px hover:-translate-y-px hover:shadow-[4px_4px_0_0_#181613] ${
                    t.ghost ? "bg-cave-bg text-cave-ink" : "bg-cave-ink text-cave-bg"
                  }`}
                >
                  {t.cta}
                </a>
              </div>
            ))}
          </div>
        </section>

        {/* QUOTES */}
        <section className="-mx-7 border-b-2 border-cave-ink bg-cave-ink px-7 py-20 text-cave-bg">
          <div className="mb-12 flex flex-wrap items-end justify-between gap-6">
            <div>
              <span className="inline-flex items-center gap-2 border-2 border-cave-bg bg-cave-amber px-3 py-1.5 font-mono text-[12px] font-bold uppercase tracking-[0.12em] text-cave-ink">
                06 · grug speak
              </span>
              <h2 className="mt-3.5 max-w-[760px] font-display text-[clamp(40px,5vw,72px)] font-normal leading-[0.96] tracking-[-0.02em] text-cave-bg">
                What <em className="not-italic text-cave-amber">Grug</em> say.
              </h2>
            </div>
            <p className="max-w-[340px] font-mono text-[14px] text-cave-bg2">
              // Lifted verbatim from real check-run output. Unedited.
            </p>
          </div>
          <div className="grid grid-cols-1 gap-6 md:grid-cols-3">
            {QUOTES.map((q) => (
              <div
                key={q.who}
                className="flex flex-col gap-4 border-2 border-cave-bg bg-[#1f1c17] p-6 shadow-[6px_6px_0_0_#fbbf24]"
              >
                <p className="font-display text-[24px] leading-[1.25] text-cave-bg">{q.q}</p>
                <span className="mt-auto font-mono text-[11px] uppercase tracking-[0.1em] text-cave-amber">
                  {q.who}
                </span>
              </div>
            ))}
          </div>
        </section>

        {/* CTA STRIP */}
        <section className="grid grid-cols-1 items-center gap-8 py-24 lg:grid-cols-[1fr_auto]">
          <h2 className="m-0 max-w-[14ch] font-display text-[clamp(48px,6vw,88px)] leading-[0.95]">
            Stop merging <em className="italic text-cave-amber-deep">half-baked</em> PRs.
          </h2>
          <div className="flex flex-wrap gap-3.5">
            <a
              href={APP_INSTALL}
              className="inline-flex items-center gap-2 border-2 border-cave-ink bg-cave-amber px-5 py-3.5 font-mono text-[14px] font-bold text-cave-ink shadow-brutal transition-all hover:-translate-x-px hover:-translate-y-px hover:shadow-[7px_7px_0_0_#181613]"
            >
              Install Grug on GitHub →
            </a>
            <a
              href={REPO_URL}
              className="inline-flex items-center gap-2 border-2 border-cave-ink bg-cave-bg px-5 py-3.5 font-mono text-[14px] font-bold text-cave-ink shadow-brutal transition-all hover:-translate-x-px hover:-translate-y-px hover:shadow-[7px_7px_0_0_#181613]"
            >
              Read the source
            </a>
          </div>
        </section>
      </main>

      {/* FOOTER */}
      <footer className="border-t-2 border-cave-ink bg-cave-bg2 px-7 py-12">
        <div className="mx-auto grid max-w-[1240px] grid-cols-1 gap-10 md:grid-cols-[1.5fr_repeat(3,1fr)]">
          <div>
            <div className="font-display text-[40px] leading-none">grug.</div>
            <p className="mt-3 max-w-[36ch] font-mono text-[12px] text-cave-ink2">
              Open-source GitHub App. Whole-cave SDLC: bug-crusher, sentry, reviewer, TPM, release manager. Built for
              teams who keep merging PRs that aren't ready.
            </p>
          </div>
          <div className="flex flex-col gap-1.5 font-mono text-[12px]">
            <h6 className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-cave-muted">Product</h6>
            <a href="#features" className="text-cave-ink hover:text-cave-amber-deep">Features</a>
            <a href="#skills" className="text-cave-ink hover:text-cave-amber-deep">Skills marketplace</a>
            <Link to="/dashboard" className="text-cave-ink hover:text-cave-amber-deep">Dashboard</Link>
            <a href="#pricing" className="text-cave-ink hover:text-cave-amber-deep">Pricing</a>
          </div>
          <div className="flex flex-col gap-1.5 font-mono text-[12px]">
            <h6 className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-cave-muted">Open source</h6>
            <a href={REPO_URL} className="text-cave-ink hover:text-cave-amber-deep">github.com/githumps/grug</a>
            <a href={`${REPO_URL}/blob/main/LICENSE`} className="text-cave-ink hover:text-cave-amber-deep">AGPL-3.0 license</a>
            <a href={`${REPO_URL}#self-hosting`} className="text-cave-ink hover:text-cave-amber-deep">Self-host guide</a>
            <a href={`${REPO_URL}/releases`} className="text-cave-ink hover:text-cave-amber-deep">Changelog</a>
          </div>
          <div className="flex flex-col gap-1.5 font-mono text-[12px]">
            <h6 className="font-mono text-[10px] font-bold uppercase tracking-[0.12em] text-cave-muted">Contact</h6>
            <a href="mailto:grug@grug.lol" className="text-cave-ink hover:text-cave-amber-deep">grug@grug.lol</a>
            <a href="https://github.com/githumps" className="text-cave-ink hover:text-cave-amber-deep">@evan</a>
            <a href={`${REPO_URL}/issues`} className="text-cave-ink hover:text-cave-amber-deep">Issues</a>
          </div>
        </div>
        <div className="mx-auto mt-10 flex max-w-[1240px] flex-wrap items-center justify-between gap-3 border-t border-dashed border-cave-ink pt-5 font-mono text-[11px] text-cave-muted">
          <span>© 2026 Grug. AGPL-3.0. Built in a cave somewhere.</span>
          <span>Grug see you scroll. Grug satisfied.</span>
        </div>
      </footer>
    </div>
  );
}
