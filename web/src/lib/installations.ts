import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";

// Client-side concurrency gate. The dashboard mounts one useEnforcement per
// repo and React Query fires them ALL at once; 14 parallel calls each
// cold-start the grug-api Lambda (~2.8s/slot) and blow past its concurrency →
// AWS throttles the overflow with 429 (verified: aws.lambda Throttles metric),
// which the UI then had to paper over. Capping the in-flight enforcement
// fetches PREVENTS the burst instead of retrying after it — the "slowdown"
// that fixes the storm at the source. A tiny promise-pool, no dependency.
function createLimiter(max: number) {
  let active = 0;
  const queue: Array<() => void> = [];
  const next = () => {
    if (active >= max || queue.length === 0) return;
    active++;
    queue.shift()!();
  };
  return function run<T>(task: () => Promise<T>): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      queue.push(() => {
        task().then(resolve, reject).finally(() => {
          active--;
          next();
        });
      });
      next();
    });
  };
}

// 5 keeps the dashboard responsive while staying under the api's effective
// concurrency (≈9 succeeded before throttling in the observed burst).
const enforcementGate = createLimiter(5);

export interface Installation {
  install_id: number;
  account_login: string;
  account_type: "User" | "Organization";
  installed_at: string;
}

export interface RepoConfig {
  tpm_enabled: boolean;
  enforcement_ruleset_id: number | null;
}

export interface Repo {
  repo_id: number;
  full_name: string;
  private: boolean;
  default_branch: string;
  config: RepoConfig;
}

export function useInstallations() {
  return useQuery<{ installations: Installation[] }>({
    queryKey: ["installations"],
    queryFn: () => api("/api/v1/installations"),
  });
}

export function useInstallRepos(installId: number | undefined) {
  return useQuery<{ repos: Repo[] }>({
    queryKey: ["installations", installId, "repos"],
    queryFn: () => api(`/api/v1/installations/${installId}/repos`),
    enabled: installId != null,
  });
}

// "unknown" = the server couldn't reach GitHub (rate-limited) even after its
// own retries and had no stored state to fall back to. Distinct from "none"
// so the UI never renders a FALSE "not enforced" off a missing answer.
export type EnforcementState = "grug_managed" | "external" | "none" | "unknown";

// Jittered exponential backoff for the client retry. The dashboard fires one
// of these per repo in parallel; without jitter their retries re-sync into a
// fresh burst against the same rate-limited endpoint. Equal jitter spreads
// them out. Capped so a flaky endpoint doesn't leave a spinner up forever.
function jitteredBackoff(attempt: number): number {
  const base = Math.min(400 * 2 ** attempt, 4_000); // 400ms → 800 → 1600 …, cap 4s
  return base / 2 + Math.random() * (base / 2);
}

export function useEnforcement(installId: number | undefined, repoId: number | undefined) {
  return useQuery<{ repo_id: number; enforcement_state: EnforcementState; degraded?: boolean }>({
    queryKey: ["enforcement", installId, repoId],
    queryFn: () =>
      enforcementGate(() =>
        api(`/api/v1/installations/${installId}/repos/${repoId}/enforcement`),
      ),
    enabled: installId != null && repoId != null,
    staleTime: 60_000,
    // Resilience (dashboard 429 storm): the server now absorbs GitHub rate
    // limits and returns a 200 degraded fallback, so most calls succeed — but
    // if the api itself is briefly unavailable, retry a few times with
    // JITTERED backoff (not React Query's no-jitter default) to avoid the
    // parallel-retry re-burst.
    retry: 3,
    retryDelay: jitteredBackoff,
  });
}

export function useFixEnforcement(installId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (repoId: number) =>
      api(`/api/v1/installations/${installId}/repos/${repoId}/enforcement`, {
        method: "POST",
      }),
    onSuccess: (_data, repoId) => {
      qc.invalidateQueries({ queryKey: ["enforcement", installId, repoId] });
      qc.invalidateQueries({ queryKey: ["installations", installId, "repos"] });
    },
  });
}

// Activity feed (PRD #301 / S2). The `verdict` badge is derived server-side;
// the panel renders it verbatim (single source of truth — never re-derives).
export type ActivityVerdict = "block" | "warn" | "pass" | "errored";

export interface ActivityRow {
  persona: string; // caveman key: "chief" | "elder"
  repo: string;
  pr_number: number;
  head_sha: string;
  verdict: ActivityVerdict;
  summary: string;
  findings_count: number;
  created_at: string;
}

export function useActivity(installId: number | undefined, verdict?: string) {
  const q = verdict && verdict !== "all" ? `?verdict=${encodeURIComponent(verdict)}` : "";
  return useQuery<{ activity: ActivityRow[] }>({
    queryKey: ["installations", installId, "activity", verdict ?? "all"],
    queryFn: () => api(`/api/v1/installations/${installId}/activity${q}`),
    enabled: installId != null,
  });
}

export function useSetRepoConfig(installId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { repo_id: number; tpm_enabled: boolean }) =>
      api(`/api/v1/installations/${installId}/repos/${vars.repo_id}/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tpm_enabled: vars.tpm_enabled }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["installations", installId, "repos"] }),
  });
}

// Re-run one persona's check on a PR (#305). The api 202s + enqueues; the new
// verdict lands asynchronously (webhook consumer), so we refresh the Activity
// feed shortly after to pick up the healed row.
export function useRerun(installId: number | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { repo: string; pr_number: number; persona: string }) =>
      api(`/api/v1/installations/${installId}/rerun`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(vars),
      }),
    onSuccess: () =>
      setTimeout(
        () => qc.invalidateQueries({ queryKey: ["installations", installId, "activity"] }),
        4000,
      ),
  });
}

// Re-run ALL currently-errored rows (#306) — the outage-recovery batch. The api
// fans every errored row into the queue; FIFO per-install ordering paces it.
export function useRerunAll(installId: number | undefined) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      api(`/api/v1/installations/${installId}/rerun-all`, { method: "POST" }) as Promise<{ queued: number }>,
    onSuccess: () =>
      setTimeout(
        () => qc.invalidateQueries({ queryKey: ["installations", installId, "activity"] }),
        4000,
      ),
  });
}
