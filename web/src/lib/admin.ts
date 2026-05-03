import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";

export interface AdminUser {
  github_user_id: string;
  login: string;
  role: "admin" | "user";
  tier: "lifetime" | "free" | "paid";
  allowlisted: boolean;
  created_at: string;
  last_login_at: string;
  allowlisted_at: string | null;
  allowlisted_by: string | null;
}

export interface AdminInstall {
  install_id: number;
  account_login: string;
  account_type: "User" | "Organization";
  installed_at: string;
  installed_by_user_id: string;
}

export function useAdminUsers() {
  return useQuery<{ users: AdminUser[] }>({
    queryKey: ["admin", "users"],
    queryFn: () => api("/api/v1/admin/users"),
  });
}

export function useAdminInstallations() {
  return useQuery<{ installations: AdminInstall[] }>({
    queryKey: ["admin", "installations"],
    queryFn: () => api("/api/v1/admin/installations"),
  });
}

export interface UserPatch {
  allowlisted?: boolean;
  role?: "admin" | "user";
  tier?: "lifetime" | "free" | "paid";
}

export function usePatchUser() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: { user_id: string; patch: UserPatch }) =>
      api(`/api/v1/admin/users/${vars.user_id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(vars.patch),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["admin", "users"] }),
  });
}
