import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";

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
    queryFn: () => api(`/api/v1/installations/${installId}/repos/${repoId}/enforcement`),
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
