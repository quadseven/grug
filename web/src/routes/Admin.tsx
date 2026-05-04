import { Navigate } from "react-router-dom";
import { Shell } from "../components/Shell";
import { useMe } from "../lib/me";
import {
  useAdminInstallations,
  useAdminUsers,
  usePatchUser,
  type AdminUser,
} from "../lib/admin";

export function Admin() {
  const me = useMe();
  const users = useAdminUsers();
  const installs = useAdminInstallations();
  const patch = usePatchUser();

  if (me.isLoading) {
    return (
      <Shell>
        <div className="px-6 py-24 text-center text-stone-500 font-mono text-sm">loading…</div>
      </Shell>
    );
  }
  if (!me.data?.authenticated) return <Navigate to="/signin" replace />;
  if (me.data.role !== "admin") return <Navigate to="/dashboard" replace />;

  const userList = users.data?.users ?? [];
  const installList = installs.data?.installations ?? [];

  return (
    <Shell>
      <section className="px-6 py-12 max-w-6xl mx-auto space-y-10">
        <h1 className="font-mono text-2xl text-stone-100">admin</h1>

        <div>
          <h2 className="text-stone-500 font-mono text-xs uppercase tracking-wider mb-3">
            users · {userList.length}
          </h2>
          {users.isLoading && <div className="text-stone-500 text-sm">loading…</div>}
          {users.isError && <div className="text-red-400 text-sm">failed to load</div>}
          {userList.length > 0 && (
            <div className="border border-stone-800 rounded-sm overflow-x-auto">
              <table className="w-full text-sm font-mono">
                <thead className="text-stone-500 text-xs uppercase tracking-wider">
                  <tr className="border-b border-stone-800">
                    <th className="text-left px-3 py-2">login</th>
                    <th className="text-left px-3 py-2">id</th>
                    <th className="text-left px-3 py-2">role</th>
                    <th className="text-left px-3 py-2">tier</th>
                    <th className="text-left px-3 py-2">allowlisted</th>
                    <th className="text-left px-3 py-2">last login</th>
                  </tr>
                </thead>
                <tbody>
                  {userList.map((u) => (
                    <UserRow
                      key={u.github_user_id}
                      user={u}
                      isSelf={u.github_user_id === me.data?.github_user_id}
                      pending={patch.isPending && patch.variables?.user_id === u.github_user_id}
                      onPatch={(p) => patch.mutate({ user_id: u.github_user_id, patch: p })}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div>
          <h2 className="text-stone-500 font-mono text-xs uppercase tracking-wider mb-3">
            installations · {installList.length}
          </h2>
          {installs.isLoading && <div className="text-stone-500 text-sm">loading…</div>}
          {installList.length > 0 && (
            <div className="border border-stone-800 rounded-sm overflow-x-auto">
              <table className="w-full text-sm font-mono">
                <thead className="text-stone-500 text-xs uppercase tracking-wider">
                  <tr className="border-b border-stone-800">
                    <th className="text-left px-3 py-2">install id</th>
                    <th className="text-left px-3 py-2">account</th>
                    <th className="text-left px-3 py-2">type</th>
                    <th className="text-left px-3 py-2">installed by (user id)</th>
                    <th className="text-left px-3 py-2">installed at</th>
                  </tr>
                </thead>
                <tbody>
                  {installList.map((inst) => (
                    <tr key={inst.install_id} className="border-b border-stone-900 last:border-0">
                      <td className="px-3 py-2 text-stone-200">{inst.install_id}</td>
                      <td className="px-3 py-2 text-stone-200">{inst.account_login}</td>
                      <td className="px-3 py-2 text-stone-400 text-xs uppercase tracking-wider">
                        {inst.account_type === "Organization" ? "org" : "user"}
                      </td>
                      <td className="px-3 py-2 text-stone-400">{inst.installed_by_user_id}</td>
                      <td className="px-3 py-2 text-stone-500 text-xs">{inst.installed_at}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>
    </Shell>
  );
}

function UserRow({
  user, isSelf, pending, onPatch,
}: {
  user: AdminUser;
  isSelf: boolean;
  pending: boolean;
  onPatch: (p: { allowlisted?: boolean; role?: "admin" | "user" }) => void;
}) {
  return (
    <tr className="border-b border-stone-900 last:border-0">
      <td className="px-3 py-2 text-stone-200">
        {user.login}
        {isSelf && (
          <span className="ml-2 text-xs text-amber-400 uppercase tracking-wider">you</span>
        )}
      </td>
      <td className="px-3 py-2 text-stone-500">{user.github_user_id}</td>
      <td className="px-3 py-2">
        <select
          className="bg-stone-950 border border-stone-800 px-2 py-1 text-stone-200 disabled:opacity-50"
          value={user.role}
          disabled={pending || isSelf}
          onChange={(e) => onPatch({ role: e.target.value as "admin" | "user" })}
        >
          <option value="user">user</option>
          <option value="admin">admin</option>
        </select>
      </td>
      <td className="px-3 py-2 text-stone-400">{user.tier}</td>
      <td className="px-3 py-2">
        <label className="inline-flex items-center gap-2 cursor-pointer">
          <input
            type="checkbox"
            className="accent-amber-400"
            checked={user.allowlisted}
            disabled={pending}
            onChange={(e) => onPatch({ allowlisted: e.target.checked })}
          />
          <span className="text-stone-300 text-xs">
            {user.allowlisted_by ? `by ${user.allowlisted_by}` : "—"}
          </span>
        </label>
      </td>
      <td className="px-3 py-2 text-stone-500 text-xs">{user.last_login_at || "—"}</td>
    </tr>
  );
}
