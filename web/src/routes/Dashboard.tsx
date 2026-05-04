import { useState } from "react";
import { Navigate } from "react-router-dom";
import { Shell } from "../components/Shell";
import { useMe } from "../lib/me";
import {
  useInstallRepos,
  useInstallations,
  useSetRepoConfig,
  type Repo,
} from "../lib/installations";

export function Dashboard() {
  const me = useMe();
  const installs = useInstallations();
  const [active, setActive] = useState<number | null>(null);

  if (me.isLoading) {
    return (
      <Shell>
        <div className="px-6 py-24 text-center text-stone-500 font-mono text-sm">
          loading…
        </div>
      </Shell>
    );
  }
  if (!me.data?.authenticated) return <Navigate to="/signin" replace />;

  const list = installs.data?.installations ?? [];
  const selected = active ?? list[0]?.install_id ?? null;

  return (
    <Shell>
      <section className="px-6 py-12 max-w-5xl mx-auto">
        <h1 className="font-mono text-2xl text-stone-100">
          dashboard
          {!me.data.allowlisted && (
            <span className="ml-3 text-xs text-amber-400 font-mono uppercase tracking-wider">
              awaiting allowlist
            </span>
          )}
        </h1>
        <p className="mt-2 text-stone-400 text-sm">
          signed in as <span className="font-mono text-stone-200">{me.data.login}</span>
        </p>

        {!me.data.allowlisted && (
          <div className="mt-6 border border-amber-700 bg-amber-950/40 p-4 rounded-sm text-sm text-amber-200">
            grug installed but not yet allowlisted by an admin. you can see your repos
            below; check-runs won't post until allowlisted.
          </div>
        )}

        <div className="mt-8 grid md:grid-cols-[18rem_1fr] gap-6">
          <aside>
            <div className="text-stone-500 font-mono text-xs uppercase tracking-wider mb-2">
              installations
            </div>
            {installs.isLoading && (
              <div className="text-stone-500 text-sm">loading…</div>
            )}
            {installs.isError && (
              <div className="text-red-400 text-sm">failed to load</div>
            )}
            {list.length === 0 && !installs.isLoading && (
              <div className="text-stone-400 text-sm">
                no installations yet.{" "}
                <a
                  href="https://github.com/apps/grug-boss/installations/new"
                  className="text-amber-400 hover:underline"
                >
                  install grug boss →
                </a>
              </div>
            )}
            <ul className="space-y-1">
              {list.map((inst) => (
                <li key={inst.install_id}>
                  <button
                    type="button"
                    onClick={() => setActive(inst.install_id)}
                    className={[
                      "w-full text-left px-3 py-2 rounded-sm font-mono text-sm border",
                      selected === inst.install_id
                        ? "border-amber-500 text-amber-300 bg-stone-900"
                        : "border-stone-800 text-stone-300 hover:border-stone-600",
                    ].join(" ")}
                  >
                    {inst.account_login}
                    <span className="ml-2 text-xs text-stone-500">
                      {inst.account_type === "Organization" ? "org" : "user"}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </aside>

          <RepoPanel installId={selected} />
        </div>
      </section>
    </Shell>
  );
}

function RepoPanel({ installId }: { installId: number | null }) {
  const repos = useInstallRepos(installId ?? undefined);
  const setConfig = useSetRepoConfig(installId ?? 0);

  if (installId == null) {
    return (
      <div className="text-stone-500 text-sm">select an installation to manage repos</div>
    );
  }
  if (repos.isLoading) return <div className="text-stone-500 text-sm">loading repos…</div>;
  if (repos.isError) return <div className="text-red-400 text-sm">failed to load repos</div>;

  const list = repos.data?.repos ?? [];

  return (
    <div>
      <div className="text-stone-500 font-mono text-xs uppercase tracking-wider mb-2">
        repos · {list.length}
      </div>
      <div className="border border-stone-800 rounded-sm divide-y divide-stone-800">
        {list.map((r) => (
          <RepoRow
            key={r.repo_id}
            repo={r}
            onToggle={(enabled) =>
              setConfig.mutate({ repo_id: r.repo_id, tpm_enabled: enabled })
            }
            pending={setConfig.isPending && setConfig.variables?.repo_id === r.repo_id}
          />
        ))}
      </div>
    </div>
  );
}

function RepoRow({
  repo, onToggle, pending,
}: {
  repo: Repo;
  onToggle: (enabled: boolean) => void;
  pending: boolean;
}) {
  return (
    <div className="flex items-center justify-between px-4 py-3">
      <div>
        <a
          href={`https://github.com/${repo.full_name}`}
          className="font-mono text-sm text-stone-200 hover:text-amber-400"
        >
          {repo.full_name}
        </a>
        {repo.private && (
          <span className="ml-2 text-xs text-stone-500 uppercase tracking-wider">private</span>
        )}
      </div>
      <label className="flex items-center gap-2 text-xs font-mono text-stone-400 select-none cursor-pointer">
        <input
          type="checkbox"
          className="accent-amber-400"
          checked={repo.config.tpm_enabled}
          disabled={pending}
          onChange={(e) => onToggle(e.target.checked)}
        />
        tpm
      </label>
    </div>
  );
}
