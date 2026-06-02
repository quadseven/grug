import { useState } from "react";
import { Navigate, Link, useNavigate } from "react-router-dom";
import { useMe } from "../lib/me";
import { API_BASE } from "../lib/api";
import {
  useAdminInstallations,
  useAdminUsers,
  usePatchUser,
  type AdminUser,
} from "../lib/admin";
import "./dashboard-grug.css";

export function Admin() {
  const me = useMe();
  const users = useAdminUsers();
  const installs = useAdminInstallations();
  const patch = usePatchUser();

  if (me.isLoading) {
    return <div className="grug-dash"><div style={{ padding: 80, textAlign: "center", fontFamily: "'JetBrains Mono',monospace", color: "var(--muted)" }}>loading…</div></div>;
  }
  if (!me.data?.authenticated) return <Navigate to="/signin" replace />;
  if (me.data.role !== "admin") return <Navigate to="/dashboard" replace />;

  const userList = users.data?.users ?? [];
  const installList = installs.data?.installations ?? [];

  return (
    <div className="grug-dash">
      <div className="tape">
        <div className="tape-track">
          {["GRUG SEE ALL.", "GRUG GUARD THE TRIBE.", "ADMIN IS BIG GRUG.",
            "GRUG SEE ALL.", "GRUG GUARD THE TRIBE.", "ADMIN IS BIG GRUG."].map((t, i) => (
            <span key={i}>{t}<span className="dot"> ● </span></span>
          ))}
        </div>
      </div>

      <header className="nav">
        <div className="nav-inner">
          <Link className="brand" to="/">
            <span className="brand-mark"><img src="/assets/grug-angry.png" alt="" /></span>
            <span>grug</span>
          </Link>
          <nav className="links">
            <Link to="/dashboard">Dashboard</Link>
            <Link to="/admin" className="active">Admin</Link>
            <a href="https://github.com/githumps/grug">Docs</a>
          </nav>
          <div className="userchip">
            <div className="who"><b>@{me.data.login}</b><span>admin</span></div>
            <span className="av"><img src="/assets/grug-angry.png" alt="" /></span>
            <SignOut />
          </div>
        </div>
      </header>

      <div className="shell">
        <div className="pagehead">
          <div>
            <span className="eyebrow"><span className="blob"></span>big grug · admin</span>
            <h1>Grug <em>see all</em>.<br />Grug guard the tribe.</h1>
          </div>
          <p className="sub">// Every user and every install across the cave. Flip allowlist + role. Grug remember.</p>
        </div>

        <div style={{ paddingBottom: 80 }}>
          <div className="card">
            <div className="card-head">Users <span className="count">{userList.length}</span></div>
            <div className="card-body">
              {users.isLoading && <div className="mono" style={{ fontSize: 12, color: "var(--muted)" }}>loading…</div>}
              {users.isError && <div className="err">⚠ failed to load users</div>}
              {userList.length > 0 && (
                <table className="gtable">
                  <thead><tr><th>login</th><th>id</th><th>role</th><th>tier</th><th>allowlisted</th><th>last login</th></tr></thead>
                  <tbody>
                    {userList.map((u) => (
                      <UserRow key={u.github_user_id} user={u}
                        isSelf={u.github_user_id === me.data?.github_user_id}
                        pending={patch.isPending && patch.variables?.user_id === u.github_user_id}
                        onPatch={(p) => patch.mutate({ user_id: u.github_user_id, patch: p })} />
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>

          <div className="card">
            <div className="card-head">Installations <span className="count">{installList.length}</span></div>
            <div className="card-body">
              {installs.isLoading && <div className="mono" style={{ fontSize: 12, color: "var(--muted)" }}>loading…</div>}
              {installs.isError && <div className="err">⚠ failed to load installations</div>}
              {installList.length > 0 && (
                <table className="gtable">
                  <thead><tr><th>install id</th><th>account</th><th>type</th><th>installed by</th><th>installed at</th></tr></thead>
                  <tbody>
                    {installList.map((inst) => (
                      <tr key={inst.install_id}>
                        <td>{inst.install_id}</td>
                        <td><b>{inst.account_login}</b></td>
                        <td>{inst.account_type === "Organization" ? "org" : "user"}</td>
                        <td>{inst.installed_by_user_id}</td>
                        <td style={{ color: "var(--muted)" }}>{inst.installed_at}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        </div>
      </div>

      <footer>
        <div className="foot-inner">
          <span className="brand serif">grug.</span>
          <span>AGPL-3.0. Made in a cave. <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a></span>
          <span>Grug see all. Grug fair.</span>
        </div>
      </footer>
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

function UserRow({ user, isSelf, pending, onPatch }: {
  user: AdminUser;
  isSelf: boolean;
  pending: boolean;
  onPatch: (p: { allowlisted?: boolean; role?: "admin" | "user" }) => void;
}) {
  return (
    <tr className={isSelf ? "self" : undefined}>
      <td><b>{user.login}</b>{isSelf && <span className="pill admin" style={{ marginLeft: 8 }}>YOU</span>}</td>
      <td style={{ color: "var(--muted)" }}>{user.github_user_id}</td>
      <td>
        <select className="text" style={{ minWidth: 0, padding: "4px 8px" }}
          value={user.role} disabled={pending || isSelf}
          onChange={(e) => onPatch({ role: e.target.value as "admin" | "user" })}>
          <option value="user">user</option>
          <option value="admin">admin</option>
        </select>
      </td>
      <td>{user.tier}</td>
      <td>
        <label style={{ display: "inline-flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
          <div className={`sw${user.allowlisted ? " on" : ""}`} onClick={() => !pending && onPatch({ allowlisted: !user.allowlisted })} role="switch" aria-checked={user.allowlisted} />
          <span style={{ color: "var(--muted)" }}>{user.allowlisted_by ? `by ${user.allowlisted_by}` : "—"}</span>
        </label>
      </td>
      <td style={{ color: "var(--muted)" }}>{user.last_login_at || "—"}</td>
    </tr>
  );
}
