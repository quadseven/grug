import { Navigate } from "react-router-dom";
import { Shell } from "../components/Shell";
import { useMe } from "../lib/me";

export function Dashboard() {
  const me = useMe();

  if (me.isLoading) {
    return (
      <Shell>
        <div className="px-6 py-24 text-center text-stone-500 font-mono text-sm">
          loading…
        </div>
      </Shell>
    );
  }

  if (!me.data?.authenticated) {
    return <Navigate to="/signin" replace />;
  }

  return (
    <Shell>
      <section className="px-6 py-12 max-w-4xl mx-auto">
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
        <div className="mt-8 border border-stone-800 p-5 rounded-sm">
          <div className="text-stone-500 font-mono text-xs uppercase tracking-wider">
            installations
          </div>
          <p className="mt-2 text-stone-400 text-sm">
            install + per-repo persona toggles ship in slice 7 (#28).
          </p>
        </div>
      </section>
    </Shell>
  );
}
