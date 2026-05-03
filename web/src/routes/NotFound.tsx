import { Link } from "react-router-dom";
import { Shell } from "../components/Shell";

export function NotFound() {
  return (
    <Shell>
      <section className="px-6 py-24 text-center">
        <h1 className="font-mono text-6xl text-amber-400">404</h1>
        <p className="mt-2 text-stone-400">grug no find page</p>
        <Link to="/" className="mt-6 inline-block text-amber-400 hover:underline font-mono text-sm">
          back to splash →
        </Link>
      </section>
    </Shell>
  );
}
