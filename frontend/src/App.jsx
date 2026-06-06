import { useState, useReducer, useEffect, useRef } from 'react'

// ── Step display names ────────────────────────────────────────────────────────

const STEP_LABELS = {
  clone:    'Clone repository',
  analyze:  'Analyze codebase',
  generate: 'Generate Dockerfile',
  build:    'Build image',
  run:      'Run & verify',
}

// ── Forge state machine ───────────────────────────────────────────────────────

const INIT = {
  status:      'idle',   // idle | running | done | failed
  jobId:       null,
  steps:       [],       // [{id, step, label, status: running|done|failed|warn}]
  attempt:     0,
  logs:        [],       // string[]
  dockerfile:  null,
  dockerignore: null,
  reasoning:   null,
  imageTag:    null,
  runSuccess:  null,
  attempts:    0,
  errorMsg:    null,
}

function onSseEvent(state, ev) {
  switch (ev.type) {
    case 'step_started': {
      // Mark any still-running step as done (a new step starting implies the
      // previous one finished without an explicit completion event).
      const steps = state.steps.map(s =>
        s.status === 'running' ? { ...s, status: 'done' } : s
      )
      return {
        ...state,
        steps: [
          ...steps,
          {
            id: `${ev.step}-${Date.now()}`,
            step:   ev.step,
            label:  STEP_LABELS[ev.step] ?? ev.step,
            status: 'running',
          },
        ],
      }
    }

    case 'attempt':
      return { ...state, attempt: ev.number }

    case 'log_line':
      return { ...state, logs: [...state.logs, ev.line] }

    case 'build_result': {
      const s = ev.success ? 'done' : 'failed'
      return {
        ...state,
        steps: state.steps.map(step =>
          step.step === 'build' && step.status === 'running'
            ? { ...step, status: s }
            : step
        ),
      }
    }

    case 'run_result': {
      const s = ev.success ? 'done' : 'warn'
      return {
        ...state,
        steps: state.steps.map(step =>
          step.step === 'run' && step.status === 'running'
            ? { ...step, status: s }
            : step
        ),
      }
    }

    case 'done': {
      const steps = state.steps.map(s =>
        s.status === 'running' ? { ...s, status: 'done' } : s
      )
      return {
        ...state,
        status:      'done',
        steps,
        dockerfile:  ev.dockerfile  ?? null,
        dockerignore: ev.dockerignore ?? null,
        reasoning:   ev.reasoning   ?? null,
        imageTag:    ev.image_tag   ?? null,
        runSuccess:  ev.run_success ?? false,
        attempts:    ev.attempts    ?? 1,
      }
    }

    case 'error': {
      const steps = state.steps.map(s =>
        s.status === 'running' ? { ...s, status: 'failed' } : s
      )
      return { ...state, status: 'failed', steps, errorMsg: ev.message }
    }

    default:
      return state
  }
}

function forgeReducer(state, action) {
  switch (action.type) {
    case 'RESET':       return INIT
    case 'JOB_STARTED': return { ...state, status: 'running', jobId: action.jobId }
    case 'SSE_EVENT':   return onSseEvent(state, action.event)
    case 'SSE_ERROR':   return { ...state, status: 'failed', errorMsg: action.message }
    default:            return state
  }
}

// ── useForge hook ─────────────────────────────────────────────────────────────

function useForge() {
  const [state, dispatch] = useReducer(forgeReducer, INIT)
  const esRef = useRef(null)

  function reset() {
    esRef.current?.close()
    esRef.current = null
    dispatch({ type: 'RESET' })
  }

  async function startForge(repoUrl) {
    esRef.current?.close()
    dispatch({ type: 'RESET' })

    // 1. POST /api/forge — create job
    let jobId, streamUrl
    try {
      const resp = await fetch('/api/forge', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ repo_url: repoUrl }),
      })
      if (!resp.ok) {
        const body = await resp.json().catch(() => ({}))
        dispatch({ type: 'SSE_ERROR', message: body.detail ?? `HTTP ${resp.status}` })
        return
      }
      const data = await resp.json()
      jobId     = data.job_id
      streamUrl = data.stream_url
    } catch (err) {
      dispatch({ type: 'SSE_ERROR', message: `Network error: ${err.message}` })
      return
    }

    dispatch({ type: 'JOB_STARTED', jobId })

    // 2. Open SSE stream
    const es = new EventSource(streamUrl)
    esRef.current = es
    let intentionalClose = false

    es.onmessage = (e) => {
      let event
      try { event = JSON.parse(e.data) } catch { return }
      dispatch({ type: 'SSE_EVENT', event })
      if (event.type === 'done' || event.type === 'error') {
        intentionalClose = true
        es.close()
        esRef.current = null
      }
    }

    es.onerror = () => {
      if (!intentionalClose) {
        dispatch({ type: 'SSE_ERROR', message: 'Stream connection closed unexpectedly.' })
      }
      es.close()
      esRef.current = null
    }
  }

  return { state, startForge, reset }
}

// ── Sub-components ────────────────────────────────────────────────────────────

function StepIcon({ status }) {
  if (status === 'running') {
    return (
      <svg className="h-4 w-4 animate-spin text-accent" viewBox="0 0 24 24" fill="none">
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
      </svg>
    )
  }
  const map = {
    done:   <span className="text-ok font-bold">✓</span>,
    failed: <span className="text-err font-bold">✗</span>,
    warn:   <span className="text-warn font-bold">!</span>,
  }
  return <span className="inline-flex h-4 w-4 items-center justify-center text-xs">{map[status] ?? '·'}</span>
}

function Timeline({ steps, attempt }) {
  if (!steps.length) return null
  return (
    <div className="rounded-lg border border-border bg-surface p-5">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-medium text-text-bright">Agent timeline</h3>
        {attempt > 0 && (
          <span className="rounded border border-border px-2 py-0.5 font-mono text-xs text-text-dim">
            attempt {attempt}
          </span>
        )}
      </div>
      <ul className="flex flex-col gap-2">
        {steps.map((s) => (
          <li key={s.id} className="flex items-center gap-2.5">
            <StepIcon status={s.status} />
            <span
              className={[
                'font-mono text-sm',
                s.status === 'failed' ? 'text-err'
                  : s.status === 'warn' ? 'text-warn'
                  : s.status === 'done' ? 'text-text'
                  : 'text-text-bright',
              ].join(' ')}
            >
              {s.label}
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function LogPanel({ logs }) {
  const bottomRef = useRef(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [logs.length])

  if (!logs.length) return null

  return (
    <div className="rounded-lg border border-border bg-surface">
      <div className="border-b border-border px-5 py-3">
        <h3 className="text-sm font-medium text-text-bright">
          Build logs
          <span className="ml-2 font-mono font-normal text-text-dim text-xs">
            {logs.length} lines
          </span>
        </h3>
      </div>
      <div className="h-52 overflow-y-auto px-5 py-3">
        {logs.map((line, i) => (
          <p
            key={i}
            className="font-mono text-xs leading-relaxed text-text-dim whitespace-pre-wrap break-all"
          >
            {line || ' '}
          </p>
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  )
}

function CopyButton({ text }) {
  const [copied, setCopied] = useState(false)

  function handleCopy() {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  return (
    <button
      onClick={handleCopy}
      className="rounded border border-border bg-surface-2 px-3 py-1 font-mono text-xs text-text-dim transition-colors hover:border-accent hover:text-accent"
    >
      {copied ? 'Copied!' : 'Copy'}
    </button>
  )
}

function DockerfileCard({ dockerfile, reasoning, attempts, imageTag, runSuccess }) {
  if (!dockerfile) return null
  return (
    <div className="rounded-lg border border-border bg-surface">
      <div className="flex items-center justify-between border-b border-border px-5 py-3">
        <h3 className="text-sm font-medium text-text-bright">Generated Dockerfile</h3>
        <CopyButton text={dockerfile} />
      </div>

      <pre className="overflow-x-auto px-5 py-4 font-mono text-xs leading-relaxed text-text whitespace-pre">
        {dockerfile}
      </pre>

      <div className="flex flex-wrap items-center gap-4 border-t border-border px-5 py-3 font-mono text-xs text-text-dim">
        <span>
          <span className="text-text-bright">{attempts}</span>
          {' '}attempt{attempts !== 1 ? 's' : ''}
        </span>
        {imageTag && (
          <span>
            image <span className="text-accent">{imageTag}</span>
          </span>
        )}
        <span className={runSuccess ? 'text-ok' : 'text-warn'}>
          {runSuccess ? '✓ verified running' : '~ build only (run verify skipped)'}
        </span>
      </div>

      {reasoning && (
        <div className="border-t border-border px-5 py-3">
          <p className="mb-1 text-xs font-medium text-text-dim">Reasoning</p>
          <p className="text-xs leading-relaxed text-text-dim">{reasoning}</p>
        </div>
      )}
    </div>
  )
}

// ── App ───────────────────────────────────────────────────────────────────────

export default function App() {
  const [repoUrl, setRepoUrl] = useState('')
  const { state, startForge, reset } = useForge()
  const running  = state.status === 'running'
  const finished = state.status === 'done' || state.status === 'failed'

  function handleSubmit(e) {
    e.preventDefault()
    if (running) return
    startForge(repoUrl)
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
              v0.6 · live
            </span>
          </div>
          <p className="text-text-dim">
            Paste a public GitHub repo URL. The agent clones it, generates a
            Dockerfile, builds it, and self-corrects until it runs.
          </p>
        </header>

        {/* URL input */}
        <form onSubmit={handleSubmit} className="flex flex-col gap-3 sm:flex-row">
          <input
            type="url"
            required
            value={repoUrl}
            disabled={running}
            onChange={(e) => setRepoUrl(e.target.value)}
            placeholder="https://github.com/owner/repo"
            className="flex-1 rounded-md border border-border bg-surface px-4 py-2.5 font-mono text-sm text-text-bright outline-none placeholder:text-text-dim focus:border-accent disabled:opacity-50"
          />
          {finished ? (
            <button
              type="button"
              onClick={() => { reset(); setRepoUrl('') }}
              className="rounded-md border border-border bg-surface-2 px-5 py-2.5 text-sm font-medium text-text transition-colors hover:border-accent hover:text-accent"
            >
              New forge
            </button>
          ) : (
            <button
              type="submit"
              disabled={running}
              className="rounded-md bg-accent px-5 py-2.5 text-sm font-medium text-white transition-colors hover:bg-accent-dim disabled:opacity-50"
            >
              {running ? 'Forging…' : 'Forge'}
            </button>
          )}
        </form>

        {/* Error banner */}
        {state.errorMsg && (
          <div className="rounded-md border border-err/40 bg-err/10 px-4 py-3 font-mono text-sm text-err">
            {state.errorMsg}
          </div>
        )}

        {/* Result panels */}
        {state.status !== 'idle' && (
          <section className="flex flex-col gap-4">
            <Timeline steps={state.steps} attempt={state.attempt} />
            <LogPanel logs={state.logs} />
            <DockerfileCard
              dockerfile={state.dockerfile}
              reasoning={state.reasoning}
              attempts={state.attempts}
              imageTag={state.imageTag}
              runSuccess={state.runSuccess}
            />
          </section>
        )}

        <footer className="mt-4 border-t border-border pt-4 text-center font-mono text-xs text-text-dim">
          DockerForge · FastAPI + React · Gemini
        </footer>

      </div>
    </div>
  )
}
