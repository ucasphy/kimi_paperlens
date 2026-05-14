# Paper Lens Web

Next.js frontend for Paper Lens. It talks to `paper-lens-backend` over REST and SSE.

## Setup

```bash
cp .env.local.example .env.local
npm install
npm run dev
```

Open http://localhost:3000.

`npm run dev` starts the backend behind the frontend. The default backend is `http://localhost:8765`; change `NEXT_PUBLIC_BACKEND_URL` in `.env.local` only if you are intentionally pointing the UI at a separately managed backend.

## Checks

```bash
npm run lint
npx tsc --noEmit
```
