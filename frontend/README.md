# DockerForge — frontend

React (Vite) + Tailwind v4 UI for DockerForge. A single screen: paste a GitHub
repo URL, hit **Forge**, and watch the agent's steps + build logs stream in,
ending with the generated Dockerfile.

```bash
npm install
npm run dev      # http://localhost:5173  (proxies /api -> http://127.0.0.1:8000)
npm run build    # production build into dist/
```

The dev server proxies `/api` to the FastAPI backend (see `vite.config.js`), so
run the backend on port 8000 alongside it. Live SSE wiring lands in Phase 6.
