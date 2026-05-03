import { Navigate } from "react-router-dom";
import { Shell } from "../components/Shell";
import { useMe } from "../lib/me";

export function Admin() {
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
  if (me.data.role !== "admin") {
    return <Navigate to="/dashboard" replace />;
  }

  return (
    <Shell>
      <section className="px-6 py-12 max-w-4xl mx-auto">
        <h1 className="font-mono text-2xl text-stone-100">admin</h1>
        <p className="mt-2 text-stone-400 text-sm">
          allowlist management ships in slice 8 (#29).
        </p>
      </section>
    </Shell>
  );
}
