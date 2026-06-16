# grug-web

React + Vite + TS + Tailwind. Splash + signin + dashboard for grug.lol.

## Routes

- `/` — splash (no auth)
- `/signin` — bounces to `${VITE_API_BASE}/api/v1/auth/github/login`
- `/dashboard` — auth-gated; awaiting-allowlist banner when `me.allowlisted=false`
- `/admin` — admin-only; allowlist mgmt UI lands in slice 8 (#29)

## Local dev

```bash
cd web
npm install
npm run dev      # http://localhost:5173
```

`VITE_API_BASE` defaults to `https://api.grug.lol`. To talk to a local
api service, set `VITE_API_BASE=http://localhost:8080` in `.env.local`.

## Build + deploy

```bash
npm run build    # → dist/
```

CI (`.github/workflows/web.deploy.yml`) builds + uploads `dist/` to
Cloudflare Pages. Apex `grug.lol` CNAME → CF Pages project.

## Stack notes

- TanStack Query for `/api/v1/me`. 401 → unauthenticated (no error toast).
- `credentials: "include"` on every fetch — api sets a stateless HMAC
  session cookie scoped to `.grug.lol`.
- No router-level auth guards; route components call `useMe()` and
  `<Navigate to="/signin">` themselves (simpler than nested loaders).
