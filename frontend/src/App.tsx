import { useCallback, useEffect, useRef, useState, type ChangeEvent } from 'react'
import { fetchSample, health, listSamples, resetChat, uploadModel, type ModelInfo, type Sample } from './api'
import { ChatPanel } from './chat/ChatPanel'
import { streamChat } from './chat/sse'
import type { AgentMessage, ChipKey, Message } from './chat/types'
import { getViewer, type GroupMode } from './viewer/engine'
import { ViewerPanel, type ViewerLoad } from './viewer/ViewerPanel'
import './App.css'

type Upload =
  | { status: 'idle' }
  | { status: 'uploading' }
  | { status: 'ready'; info: ModelInfo }
  | { status: 'error'; message: string }

type Backend = { status: 'checking' } | { status: 'ok'; llm: string } | { status: 'down' }

let nextId = 0
const uid = () => `m${++nextId}`

function sampleLabel(name: string): string {
  return name.replace(/\.ifc$/i, '').replace(/_arc$/, '').replace(/_/g, ' ')
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

export default function App() {
  const viewer = getViewer()
  const [backend, setBackend] = useState<Backend>({ status: 'checking' })
  const [samples, setSamples] = useState<Sample[]>([])
  const [modelName, setModelName] = useState<string | null>(null)
  const [viewerLoad, setViewerLoad] = useState<ViewerLoad>({ status: 'idle' })
  const [upload, setUpload] = useState<Upload>({ status: 'idle' })
  const [messages, setMessages] = useState<Message[]>([])
  const [running, setRunning] = useState(false)
  const [activeChip, setActiveChip] = useState<{ key: ChipKey; label: string; found: number } | null>(null)
  const [mode, setMode] = useState<GroupMode>('ghost')
  const generation = useRef(0)
  const abort = useRef<AbortController | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => {
    let alive = true
    const check = () =>
      health()
        .then((h) => alive && setBackend({ status: 'ok', llm: h.model }))
        .catch(() => alive && setBackend({ status: 'down' }))
    void check()
    const t = setInterval(check, 15000)
    listSamples()
      .then((s) => alive && setSamples(s))
      .catch(() => undefined)
    return () => {
      alive = false
      clearInterval(t)
    }
  }, [])

  const loadFile = useCallback(
    async (file: File) => {
      const gen = ++generation.current
      abort.current?.abort()
      abort.current = null
      setMessages([])
      setRunning(false)
      setActiveChip(null)
      setModelName(file.name)
      setViewerLoad({ status: 'loading', progress: 0 })
      setUpload({ status: 'uploading' })

      // The viewer converts the IFC in the browser while the backend opens it for the agent.
      const bytes = new Uint8Array(await file.arrayBuffer())
      viewer
        .load(bytes, (p) => gen === generation.current && setViewerLoad({ status: 'loading', progress: p }))
        .then(() => gen === generation.current && setViewerLoad({ status: 'ready' }))
        .catch((e) => {
          console.error(e)
          if (gen === generation.current) setViewerLoad({ status: 'error', message: errText(e) })
        })
      uploadModel(file)
        .then((info) => gen === generation.current && setUpload({ status: 'ready', info }))
        .catch((e) => gen === generation.current && setUpload({ status: 'error', message: errText(e) }))
    },
    [viewer],
  )

  const onFileChosen = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (file) void loadFile(file)
  }

  const onSample = async (name: string) => {
    try {
      await loadFile(await fetchSample(name))
    } catch (e) {
      setUpload({ status: 'error', message: errText(e) })
    }
  }

  const modelId = upload.status === 'ready' ? upload.info.model_id : null

  const send = (question: string) => {
    if (!modelId || running) return
    const gen = generation.current
    const agentId = uid()
    const agent: AgentMessage = { id: agentId, role: 'agent', steps: [], running: true, startedAt: Date.now() }
    setMessages((ms) => [...ms, { id: uid(), role: 'user', text: question }, agent])
    setRunning(true)
    const update = (fn: (m: AgentMessage) => AgentMessage) => {
      if (gen !== generation.current) return
      setMessages((ms) => ms.map((m) => (m.id === agentId && m.role === 'agent' ? fn(m) : m)))
    }
    const ctrl = new AbortController()
    abort.current = ctrl
    streamChat(
      modelId,
      question,
      (ev) => {
        switch (ev.type) {
          case 'code':
            update((m) => ({ ...m, steps: [...m.steps, { kind: 'code', text: ev.code }] }))
            break
          case 'output':
            update((m) => ({ ...m, steps: [...m.steps, { kind: 'output', text: ev.text }] }))
            break
          case 'answer':
            update((m) => ({
              ...m,
              answer: { text: ev.text, groups: ev.element_groups, sufficientInfo: ev.sufficient_info },
            }))
            break
          case 'error':
            update((m) => ({ ...m, error: ev.message }))
            break
          case 'done':
            update((m) => ({ ...m, elapsedS: ev.elapsed_s }))
            break
        }
      },
      ctrl.signal,
    )
      .catch((e) => {
        if (!ctrl.signal.aborted) update((m) => ({ ...m, error: `Connection error: ${errText(e)}` }))
      })
      .finally(() => {
        update((m) => ({
          ...m,
          running: false,
          elapsedS: m.elapsedS ?? (Date.now() - m.startedAt) / 1000,
          error: m.error ?? (m.answer ? undefined : 'The stream ended without an answer.'),
        }))
        if (gen === generation.current) setRunning(false)
      })
  }

  const newChat = async () => {
    setMessages([])
    setActiveChip(null)
    void viewer.resetView()
    if (!modelId) return
    try {
      await resetChat(modelId)
    } catch (e) {
      setUpload({ status: 'error', message: `Reset failed: ${errText(e)}` })
    }
  }

  const onChip = async (key: ChipKey, globalIds: string[], label: string) => {
    if (activeChip?.key === key) {
      setActiveChip(null)
      await viewer.resetView()
      return
    }
    setActiveChip({ key, label, found: globalIds.length })
    const found = await viewer.showGroup(globalIds, mode)
    setActiveChip((c) => (c?.key === key ? { ...c, found } : c))
  }

  const onModeChange = (m: GroupMode) => {
    setMode(m)
    void viewer.setMode(m)
  }

  const onResetView = () => {
    setActiveChip(null)
    void viewer.resetView()
  }

  const blocked =
    upload.status === 'idle'
      ? 'Load a model to start asking questions.'
      : upload.status === 'uploading'
        ? 'Preparing the agent for this model\u2026'
        : upload.status === 'error'
          ? `The agent is unavailable: ${upload.message}`
          : viewerLoad.status !== 'ready' && viewerLoad.status !== 'error'
            ? // Chips resolve against the viewer model, so wait for it.
              'Loading model in viewer\u2026'
            : null

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">Cobbie</div>
        <button className="btn primary" onClick={() => fileInput.current?.click()}>
          Upload IFC
        </button>
        <input ref={fileInput} type="file" accept=".ifc" hidden onChange={onFileChosen} data-testid="file-input" />
        {samples.map((s) => (
          <button key={s.name} className="btn" onClick={() => void onSample(s.name)} data-sample={s.name}>
            Sample: {sampleLabel(s.name)}
          </button>
        ))}

        <div className="model-info">
          {modelName && (
            <>
              <span className="model-name" title={modelName}>
                {modelName}
              </span>
              {upload.status === 'ready' && (
                <span className="muted">
                  {upload.info.schema} {'\u00B7'} {upload.info.n_products} products
                </span>
              )}
              <StatusPill label="Viewer" state={viewerState(viewerLoad)} />
              <StatusPill label="Agent" state={uploadState(upload)} />
            </>
          )}
        </div>

        <div className={`backend ${backend.status}`} title="Backend status">
          <span className="dot" />
          {backend.status === 'ok'
            ? `Backend online (${backend.llm})`
            : backend.status === 'down'
              ? 'Backend offline'
              : 'Checking backend\u2026'}
        </div>
      </header>

      <main className="main">
        <ViewerPanel
          viewer={viewer}
          load={viewerLoad}
          activeGroup={activeChip}
          mode={mode}
          onModeChange={onModeChange}
          onResetView={onResetView}
        />
        <ChatPanel
          messages={messages}
          running={running}
          blocked={blocked}
          activeChip={activeChip?.key ?? null}
          onSend={send}
          onNewChat={() => void newChat()}
          onChip={(k, ids, label) => void onChip(k, ids, label)}
          onElementClick={(gid) => void viewer.selectGlobalId(gid)}
        />
      </main>
    </div>
  )
}

type PillState = { kind: 'busy' | 'ok' | 'error'; text: string }

function viewerState(v: ViewerLoad): PillState {
  if (v.status === 'loading') return { kind: 'busy', text: `loading ${Math.round(v.progress * 100)}%` }
  if (v.status === 'error') return { kind: 'error', text: 'failed' }
  return { kind: 'ok', text: 'ready' }
}

function uploadState(u: Upload): PillState {
  if (u.status === 'uploading') return { kind: 'busy', text: 'uploading' }
  if (u.status === 'error') return { kind: 'error', text: 'failed' }
  return { kind: 'ok', text: 'ready' }
}

function StatusPill({ label, state }: { label: string; state: PillState }) {
  return (
    <span className={`pill ${state.kind}`} data-testid={`pill-${label.toLowerCase()}`}>
      {label}: {state.text}
    </span>
  )
}
