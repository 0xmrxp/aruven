# aruven-frontend

Next.js 15 dashboard: proof first, then story.

## Pages

| Route | Purpose |
|---|---|
| `/` | Live vault NAV, holdings, fee inflow, trade list, and the agent journal feed |
| `/journal` | The full hash-chained decision log with prev-hash links |
| `/deployments` | Directory of third-party factory launches (indexed from `Launch` events only) |

## Run

```
npm install
npm run dev            # dev server
NEXT_PUBLIC_ARUVEN_API=https://api.aruven.xyz npm run build   # static export
```

Static export (`output: "export"`): pages hydrate client-side from the backend
API. Deploy anywhere static (Vercel/any host).

## Contract

All types mirror the backend's response shapes (`lib/api.ts` is the single
source of truth for both).
