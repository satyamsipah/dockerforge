import { useState } from 'react'

// Phase 1 ships the shell of the UI: the URL input, the Forge action, and the
// three result regions (event timeline, build-log panel, Dockerfile card) as
// placeholders. The live SSE wiring that fills them lands in Phase 6.

function Placeholder({ title, hint }) {
  return (
    <div className="rounded-lg border border-dashed border-border bg-surface/40 p-5">
      <h3 className="text-sm font-medium text-text-bright">{title}</h3>
      <p className="mt-1 text-sm text-text-dim">{hint}</p>
    </div>
  )
}

function App() {
  const [repoUrl, setRepoUrl] = useState('')
  const [submitted, setSubmitted] = useState(false)

  function handleSubmit(e) {
    e.preventDefault()
    // The forge pipeline is not wired yet (Phases 2–6). For now we just
    // acknowledge the input so the shell is demonstrably interactive.
    setSubmitted(true)
  }

  return (
    <div className="min-h-full bg-bg text-text">
      <div className="mx-auto flex max-w-3xl flex-col gap-8 px-6 py-12">
        {/* Header */}
        <header className="flex flex-col gap-2">
          <div className="flex items-center gap-3">
            <span className="text-2xl">🐳</span>
            <h1 className="text-2xl font-semibold tracking-tight text-text-bright">
              DockerForge
            </h1>
            <span className="rounded border border-border px-2 py-0.5 font-mono text-xs text-text-dim">
              v0.1 · skeleton
            </span>
          </div>
          <p className="text-text-dim">
            Paste a public GitHub repo URL. The agent clones it, generates a
            Dockerfile, builds it, and self-corrects until it runs.
          </p>
        </header>

        {/* Input form */}
        <form onSubmit={handleSubmit} className="flex flex-col gap-3 sm:flex-row">
          <input
            type="url"
            required
            value={repoUrl}
            onChange={(e) => setRepoUrl(e.target.value)}
            placeholder="https://github.com/owner/repo"
            className="flex-1 rounded-md border border-border bg-surface px-4 py-2.5 font-mono text-sm text-text-bright outline-none placeholder:text-text-dim focus:border-accent"
          />
          <button
            type="submit"
            className="rounded-md bg-accent px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-accent-dim"
          >
            Forge
          </button>
        </form>

        {submitted && (
          <div className="rounded-md border border-warn/40 bg-warn/10 px-4 py-3 text-sm text-warn">
            Skeleton only — the clone → analyze → generate → build → run loop is
            implemented in the next phases. Nothing was sent to a server yet.
          </div>
        )}

        {/* Result regions (placeholders until Phase 6) */}
        <section className="flex flex-col gap-4">
          <Placeholder
            title="Agent timeline"
            hint="Each step (clone, analyze, generate, build, retry, run, verify) will stream in here as it happens."
          />
          <Placeholder
            title="Build logs"
            hint="Live docker build output, line by line, in monospace."
          />
          <Placeholder
            title="Generated Dockerfile"
            hint="The final working Dockerfile, the agent's reasoning, and the attempt count — with a copy button."
          />
        </section>

        <footer className="mt-4 border-t border-border pt-4 text-center font-mono text-xs text-text-dim">
          DockerForge · built with FastAPI + React · powered by Gemini
        </footer>
      </div>
    </div>
  )
}

export default App
