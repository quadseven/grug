import { Link } from "react-router-dom";
import { Shell } from "../components/Shell";

const FEATURES = [
  ["Definition of Ready gate", "5 static checks. ## Why, acceptance bullets, size, scope-fence, issue link. Block PRs that aren't ready before review."],
  ["Persona-based", "TPM today. Code reviewer, release manager, stuck-PR pulse — same App, role toggles per repo."],
  ["No vendor lock-in", "Open source (AGPL-3.0). Self-host on your own AWS account. Bring your own LLM."],
];

export function Splash() {
  return (
    <Shell>
      <section className="px-6 pt-16 pb-12 max-w-4xl mx-auto">
        <h1 className="font-mono text-5xl md:text-6xl tracking-tight">
          <span className="text-amber-400">grug</span> boss
        </h1>
        <p className="mt-4 text-stone-400 text-lg max-w-2xl">
          caveman PR boss for your repos. blocks bad PRs before review. no LLM
          slop. one app, many personas.
        </p>
        <div className="mt-8 flex gap-3">
          <Link
            to="/signin"
            className="bg-amber-400 text-stone-950 px-5 py-2.5 font-mono font-medium rounded-sm hover:bg-amber-300"
          >
            sign in with github
          </Link>
          <a
            href="https://github.com/githumps/grug"
            className="border border-stone-700 text-stone-300 px-5 py-2.5 font-mono rounded-sm hover:border-stone-500 hover:text-stone-100"
          >
            source on github
          </a>
        </div>
      </section>

      <section className="px-6 py-12 max-w-4xl mx-auto grid md:grid-cols-3 gap-6">
        {FEATURES.map(([title, body]) => (
          <div key={title} className="border border-stone-800 p-5 rounded-sm">
            <h3 className="font-mono text-amber-400 text-sm uppercase tracking-wider">{title}</h3>
            <p className="mt-2 text-stone-300 text-sm leading-relaxed">{body}</p>
          </div>
        ))}
      </section>

      <section className="px-6 py-12 max-w-4xl mx-auto">
        <div className="border border-stone-800 bg-stone-950 p-5 rounded-sm font-mono text-xs text-stone-400">
          <div className="text-stone-500 mb-2">// what grug checks</div>
          <pre className="text-stone-300">{`✓ ## Why ≥ 5 words
✓ ## Acceptance criteria — ≥ 3 non-empty bullets
✓ Size: XS|S|M|L (no XL — split it)
✓ ## Out of scope section present
✓ closes #N | fixes #N | Part of #N`}</pre>
        </div>
      </section>
    </Shell>
  );
}
