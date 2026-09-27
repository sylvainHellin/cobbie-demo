// POST /api/chat and parse the text/event-stream body incrementally.
import { errorMessage } from '../api'

export interface ChatElement {
  global_id: string
  ifc_class: string
  name: string | null
}

export interface ElementGroup {
  label: string
  elements: ChatElement[]
}

export type ChatEvent =
  | { type: 'status'; text: string }
  | { type: 'code'; code: string }
  | { type: 'output'; text: string; truncated?: boolean }
  | { type: 'answer'; text: string; element_groups: ElementGroup[]; sufficient_info: boolean }
  | { type: 'error'; message: string }
  | { type: 'done'; elapsed_s: number }

export async function streamChat(
  modelId: string,
  message: string,
  onEvent: (ev: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify({ model_id: modelId, message }),
    signal,
  })
  if (!res.ok || !res.body) throw new Error(await errorMessage(res))

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
  let buf = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += value.replace(/\r\n/g, '\n')
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const chunk = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const data = chunk
        .split('\n')
        .filter((l) => l.startsWith('data:'))
        .map((l) => l.slice(5).replace(/^ /, ''))
        .join('\n')
      if (data) onEvent(JSON.parse(data) as ChatEvent)
    }
  }
}
