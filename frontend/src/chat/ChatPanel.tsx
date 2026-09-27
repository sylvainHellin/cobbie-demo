import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { AgentTurn } from './AgentTurn'
import type { ChipKey, Message } from './types'

const SUGGESTIONS = [
  'How many doors are there and on which storeys?',
  'Show me all the walls',
  'What is the total floor area of the spaces?',
  'Which rooms are on the ground floor?',
]

interface Props {
  messages: Message[]
  running: boolean
  /** Why the input is disabled, or null when chatting is possible. */
  blocked: string | null
  activeChip: ChipKey | null
  onSend: (text: string) => void
  onNewChat: () => void
  onChip: (key: ChipKey, globalIds: string[], label: string) => void
  onElementClick: (globalId: string) => void
}

export function ChatPanel({ messages, running, blocked, activeChip, onSend, onNewChat, onChip, onElementClick }: Props) {
  const [text, setText] = useState('')
  const list = useRef<HTMLDivElement>(null)
  const disabled = running || blocked !== null

  useEffect(() => {
    const el = list.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  const send = (value: string) => {
    const q = value.trim()
    if (!q || disabled) return
    onSend(q)
    setText('')
  }

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      send(text)
    }
  }

  return (
    <section className="chat">
      <header className="chat-head">
        <span className="chat-title">Ask about the model</span>
        <button className="btn" onClick={onNewChat} disabled={running || messages.length === 0}>
          New chat
        </button>
      </header>

      <div className="chat-list" ref={list}>
        {messages.length === 0 && (
          <div className="chat-empty">
            <p className="muted">{blocked ?? 'Try one of these questions:'}</p>
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s} className="suggestion" onClick={() => send(s)} disabled={disabled}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m) =>
          m.role === 'user' ? (
            <div key={m.id} className="msg user">
              {m.text}
            </div>
          ) : (
            <AgentTurn key={m.id} msg={m} activeChip={activeChip} onChip={onChip} onElementClick={onElementClick} />
          ),
        )}
      </div>

      <div className="chat-input">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={blocked ?? (running ? 'The agent is answering\u2026' : 'Ask a question (Enter to send, Shift+Enter for a new line)')}
          disabled={disabled}
          rows={2}
          data-testid="chat-input"
        />
        <button className="btn primary" onClick={() => send(text)} disabled={disabled || !text.trim()}>
          Send
        </button>
      </div>
    </section>
  )
}
