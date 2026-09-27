import { useEffect, useState } from 'react'
import { AnswerMarkdown } from './AnswerMarkdown'
import { ElementChip } from './ElementChip'
import { chipKey, type AgentMessage, type ChipKey } from './types'

function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 250)
    return () => clearInterval(t)
  }, [])
  return <span className="elapsed">{((now - since) / 1000).toFixed(0)} s</span>
}

function Steps({ msg }: { msg: AgentMessage }) {
  const [open, setOpen] = useState(false)
  const n = msg.steps.filter((s) => s.kind === 'code').length
  const expanded = msg.running || open
  if (msg.steps.length === 0) return null
  return (
    <div className="steps">
      {!msg.running && (
        <button className="steps-toggle" onClick={() => setOpen(!open)} aria-expanded={open}>
          <span className="caret">{open ? '\u25BE' : '\u25B8'}</span> {open ? 'Hide' : 'Show'} {n} step{n === 1 ? '' : 's'}
        </button>
      )}
      {expanded && (
        <div className="steps-body">
          {msg.steps.map((s, i) =>
            s.kind === 'code' ? (
              <pre key={i} className="step-code">
                <code>{s.text}</code>
              </pre>
            ) : (
              <pre key={i} className="step-output">
                {s.text || '(no output)'}
              </pre>
            ),
          )}
        </div>
      )}
    </div>
  )
}

interface Props {
  msg: AgentMessage
  activeChip: ChipKey | null
  onChip: (key: ChipKey, globalIds: string[], label: string) => void
  onElementClick: (globalId: string) => void
}

export function AgentTurn({ msg, activeChip, onChip, onElementClick }: Props) {
  return (
    <div className="msg agent" data-testid="agent-message">
      {msg.running && (
        <div className="running">
          <div className="spinner small" />
          <span>
            Agent is working{msg.steps.length > 0 ? `, step ${msg.steps.filter((s) => s.kind === 'code').length}` : ''}
          </span>
          <Elapsed since={msg.startedAt} />
        </div>
      )}
      <Steps msg={msg} />
      {msg.answer && (
        <div className="answer" data-testid="answer">
          <AnswerMarkdown text={msg.answer.text} />
          {!msg.answer.sufficientInfo && (
            <div className="note">The agent reports that the model may not contain enough information for a full answer.</div>
          )}
          {msg.answer.groups.length > 0 && (
            <div className="chips">
              {msg.answer.groups.map((g, i) => {
                const key = chipKey(msg.id, i)
                return (
                  <ElementChip
                    key={key}
                    group={g}
                    active={activeChip === key}
                    onSelect={() =>
                      onChip(
                        key,
                        g.elements.map((e) => e.global_id),
                        g.label,
                      )
                    }
                    onElementClick={onElementClick}
                  />
                )
              })}
            </div>
          )}
        </div>
      )}
      {msg.error && (
        <div className="error" role="alert">
          {msg.error}
        </div>
      )}
      {!msg.running && msg.elapsedS !== undefined && <div className="meta">{msg.elapsedS.toFixed(1)} s</div>}
    </div>
  )
}
