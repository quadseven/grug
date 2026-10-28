import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { Dashboard } from "./Dashboard";

// grug#1206: the Personas panel must read each persona's mode from the
// repo's real config and save a change with the config PUT, not with
// localStorage. The API is a stateful fetch mock: a PUT merges into the
// stored config, so a fresh mount ("reload") reads back what was saved.

type Cfg = Record<string, unknown>;

const REPO_ID = 101;
const OTHER_REPO_ID = 202;

function baseConfig(over: Cfg = {}): Cfg {
  return {
    tpm_enabled: true,
    guard_enabled: true,
    guard_blocking: false,
    enforcement_ruleset_id: 7,
    code_reviewer_enabled: true,
    code_reviewer_blocking: true,
    warder_enabled: false,
    warder_gate_blocking: false,
    sentinel_enabled: true,
    pulse_enabled: false,
    smasher_enabled: false,
    walkthrough_enabled: true,
    dep_watch_enabled: false,
    reopen_watch_enabled: false,
    guard_hygiene_watch_enabled: false,
    pulse_comment_nudge_enabled: false,
    issue_dor_enabled: false,
    check_run_reconcile_enabled: false,
    ...over,
  };
}

let configs: Record<number, Cfg>;
let puts: { repoId: number; body: Cfg }[];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function mockApi(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = new URL(typeof input === "string" ? input : input.toString());
  const path = url.pathname;
  const method = (init?.method ?? "GET").toUpperCase();
  if (path === "/api/v1/me") {
    return Promise.resolve(json({ authenticated: true, login: "operator", role: "user" }));
  }
  if (path === "/api/v1/installations") {
    return Promise.resolve(json({
      installations: [{ install_id: 1, account_login: "acme", account_type: "Organization", installed_at: "2026-01-01T00:00:00Z" }],
    }));
  }
  if (path === "/api/v1/installations/1/repos") {
    return Promise.resolve(json({
      repos: [
        { repo_id: REPO_ID, full_name: "acme/app", private: false, default_branch: "main", config: configs[REPO_ID] },
        { repo_id: OTHER_REPO_ID, full_name: "acme/lib", private: false, default_branch: "main", config: configs[OTHER_REPO_ID] },
      ],
    }));
  }
  const cfgMatch = path.match(/^\/api\/v1\/installations\/1\/repos\/(\d+)\/config$/);
  if (cfgMatch && method === "PUT") {
    const repoId = Number(cfgMatch[1]);
    const body = JSON.parse(String(init?.body ?? "{}")) as Cfg;
    puts.push({ repoId, body });
    configs[repoId] = { ...configs[repoId], ...body };
    return Promise.resolve(json({ repo_id: repoId, full_name: "acme/app", config: configs[repoId] }));
  }
  if (/\/enforcement$/.test(path)) {
    return Promise.resolve(json({ repo_id: REPO_ID, enforcement_state: "grug_managed" }));
  }
  if (/\/activity$/.test(path)) {
    return Promise.resolve(json({ activity: [] }));
  }
  return Promise.resolve(json({ detail: `unmocked ${method} ${path}` }, 404));
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Dashboard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function openPersonas() {
  const rail = await screen.findByText("Personas", { selector: ".lbl" });
  fireEvent.click(rail.closest("button")!);
}

async function personaCard(name: string): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name });
  return heading.closest(".pcard") as HTMLElement;
}

function modeButton(card: HTMLElement, mode: string): HTMLButtonElement {
  return within(card).getByRole("button", { name: mode }) as HTMLButtonElement;
}

beforeEach(() => {
  configs = {
    [REPO_ID]: baseConfig(),
    [OTHER_REPO_ID]: baseConfig({ code_reviewer_blocking: false }),
  };
  puts = [];
  localStorage.clear();
  // A stale local click must not win over the API: the old panel read this.
  localStorage.setItem("grug_dash_v2", JSON.stringify({ personas: { elder: "off", guard: "off" } }));
  vi.stubGlobal("fetch", vi.fn(mockApi));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("Personas panel (grug#1206)", () => {
  it("shows Elder's mode read from the selected repo's config, not localStorage", async () => {
    mount();
    await openPersonas();
    const elder = await personaCard("Elder");
    await waitFor(() => expect(modeButton(elder, "BLOCK").className).toContain("on"));
    expect(modeButton(elder, "OFF").className).not.toContain("on");
    // Guard is enabled and not blocking -> WARN.
    const guard = await personaCard("Guard");
    expect(modeButton(guard, "WARN").className).toContain("on");
  });

  it("issues the config PUT for the selected repo and reads the mode back after a reload", async () => {
    const first = mount();
    await openPersonas();
    const elder = await personaCard("Elder");
    await waitFor(() => expect(modeButton(elder, "BLOCK").className).toContain("on"));

    await act(async () => { fireEvent.click(modeButton(elder, "WARN")); });
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].repoId).toBe(REPO_ID);
    expect(puts[0].body).toMatchObject({ code_reviewer_enabled: true, code_reviewer_blocking: false });
    // The other repo is untouched: the panel is per selected repo.
    expect(configs[OTHER_REPO_ID].code_reviewer_blocking).toBe(false);
    expect(configs[REPO_ID].code_reviewer_blocking).toBe(false);

    first.unmount();
    localStorage.setItem("grug_dash_v2", JSON.stringify({ personas: { elder: "block" } }));
    mount();
    await openPersonas();
    const elderAgain = await personaCard("Elder");
    await waitFor(() => expect(modeButton(elderAgain, "WARN").className).toContain("on"));
    expect(modeButton(elderAgain, "BLOCK").className).not.toContain("on");
  });

  it("switching the selected repo shows that repo's modes and writes to it", async () => {
    mount();
    await openPersonas();
    const select = (await screen.findByLabelText("Repository")) as HTMLSelectElement;
    fireEvent.change(select, { target: { value: String(OTHER_REPO_ID) } });
    const elder = await personaCard("Elder");
    await waitFor(() => expect(modeButton(elder, "WARN").className).toContain("on"));

    await act(async () => { fireEvent.click(modeButton(elder, "OFF")); });
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].repoId).toBe(OTHER_REPO_ID);
    expect(puts[0].body).toMatchObject({ code_reviewer_enabled: false });
  });

  it("renders a persona with no backend mode field as disabled, and never saves persona modes locally", async () => {
    mount();
    await openPersonas();
    const chief = await personaCard("Chief");
    const buttons = within(chief).queryAllByRole("button");
    expect(buttons.length).toBeGreaterThan(0);
    for (const b of buttons) expect((b as HTMLButtonElement).disabled).toBe(true);
    expect(within(chief).getByText(/not configurable yet/i)).toBeTruthy();

    fireEvent.click(buttons[0]);
    const elder = await personaCard("Elder");
    await waitFor(() => expect(modeButton(elder, "BLOCK").className).toContain("on"));
    await act(async () => { fireEvent.click(modeButton(elder, "WARN")); });
    await waitFor(() => expect(puts).toHaveLength(1));
    const stored = JSON.parse(localStorage.getItem("grug_dash_v2") ?? "{}");
    // The seeded stale value is left alone; no click wrote a persona mode.
    expect(stored.personas).toEqual({ elder: "off", guard: "off" });
  });
});

// grug#1482: the pulse comment-nudge persona (F-09) is default off per repo
// and had no dashboard control; the only write path was a hand-rolled API PUT.
describe("Repositories panel pulse nudge switch (grug#1482)", () => {
  async function nudgeSwitch(): Promise<HTMLElement> {
    return screen.findByTitle("Toggle the pulse comment nudge for acme/app");
  }

  it("shows the stored value and writes on and off through the config PUT", async () => {
    mount();
    const sw = await nudgeSwitch();
    expect(sw.getAttribute("aria-checked")).toBe("false");

    await act(async () => { fireEvent.click(sw); });
    await waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0].repoId).toBe(REPO_ID);
    expect(puts[0].body).toMatchObject({ tpm_enabled: true, pulse_comment_nudge_enabled: true });
    await waitFor(async () => expect((await nudgeSwitch()).getAttribute("aria-checked")).toBe("true"));

    await act(async () => { fireEvent.click(await nudgeSwitch()); });
    await waitFor(() => expect(puts).toHaveLength(2));
    expect(puts[1].body).toMatchObject({ pulse_comment_nudge_enabled: false });
    expect(configs[REPO_ID].pulse_comment_nudge_enabled).toBe(false);
    // The other repo's stored flag is untouched.
    expect(configs[OTHER_REPO_ID].pulse_comment_nudge_enabled).toBe(false);
  });

  it("reads an enabled flag back after a reload", async () => {
    configs[REPO_ID] = baseConfig({ pulse_comment_nudge_enabled: true });
    const first = mount();
    expect((await nudgeSwitch()).getAttribute("aria-checked")).toBe("true");
    first.unmount();
    mount();
    expect((await nudgeSwitch()).getAttribute("aria-checked")).toBe("true");
  });
});
