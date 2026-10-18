// @vitest-environment node
import postcss from "postcss";
import { describe, expect, it } from "vitest";

// grug PR 1211 (Tailwind v3 -> v4). v4 moved the PostCSS plugin to
// @tailwindcss/postcss and reads the theme from CSS (@theme), not from
// tailwind.config.js. Compile the real stylesheet through the real PostCSS
// config and check the cave theme still resolves, so a half-done migration
// (old plugin name, or a theme left behind in a JS config) fails here.

// `from` must be the real path: @source globs resolve against it.
const cssPath = decodeURIComponent(new URL("../index.css", import.meta.url).pathname);
const configUrl = new URL("../../postcss.config.js", import.meta.url).href;

// web has no @types/node, so node:fs is loaded through a non-literal
// specifier (typed any) rather than a static import tsc would reject.
const fsModule = "node:fs";

async function compile(extraCandidates: string): Promise<string> {
  const { readFileSync } = await import(/* @vite-ignore */ fsModule);
  // Loaded by path so tsc does not need types for a plain JS config.
  const config: { plugins: Record<string, unknown> } = (await import(/* @vite-ignore */ configUrl)).default;
  const plugins = await Promise.all(
    Object.entries(config.plugins).map(async ([name, opts]) => {
      const mod = await import(/* @vite-ignore */ name);
      return (mod.default ?? mod)(opts);
    }),
  );
  const css = readFileSync(cssPath, "utf8") + `\n@source inline("${extraCandidates}");\n`;
  const out = await postcss(plugins).process(css, { from: cssPath });
  return out.css;
}

describe("tailwind stylesheet", () => {
  it("compiles index.css through the configured PostCSS plugins", async () => {
    const css = await compile("bg-cave-amber");
    expect(css).not.toContain("@tailwind");
    expect(css).toContain("color-scheme: dark");
  });

  it("keeps the cave theme tokens from the old JS config", async () => {
    const css = await compile(
      "bg-cave-amber text-cave-ink shadow-brutal font-grotesk animate-blink",
    );
    expect(css).toMatch(/--color-cave-amber:\s*#fbbf24/);
    expect(css).toMatch(/--color-cave-ink:\s*#181613/);
    // v4 inlines shadow tokens into the utility, color made overridable.
    expect(css).toMatch(/--tw-shadow:\s*6px 6px 0 0 var\(--tw-shadow-color,\s*#181613\)/);
    expect(css).toMatch(/--font-grotesk:\s*'Space Grotesk'/);
    expect(css).toContain("@keyframes blink");
    expect(css).toMatch(/\.bg-cave-amber\s*\{/);
    expect(css).toMatch(/\.shadow-brutal\s*\{/);
  });

  it("does not emit utilities for words outside the app sources", async () => {
    // v4 auto-detects sources; public/index.html would otherwise add
    // utilities like .hidden that v3's content globs never produced.
    const css = await compile("");
    expect(css).not.toMatch(/\.hidden\s*\{/);
    expect(css).not.toMatch(/\.outline\s*\{/);
  });
});
