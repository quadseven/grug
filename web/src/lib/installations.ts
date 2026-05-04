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
