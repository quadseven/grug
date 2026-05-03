import { useQuery } from "@tanstack/react-query";
import { api, ApiError } from "./api";

export interface Me {
  authenticated: boolean;
  github_user_id?: string;
  login?: string;
  role?: "admin" | "user";
  allowlisted?: boolean;
}

export function useMe() {
  return useQuery<Me>({
    queryKey: ["me"],
    queryFn: async () => {
      try {
        return await api<Me>("/api/v1/me");
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) {
          return { authenticated: false };
        }
        throw e;
      }
    },
    retry: (count, e) => !(e instanceof ApiError && e.status === 401) && count < 2,
  });
}
