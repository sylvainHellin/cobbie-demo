import type { ElementGroup } from './sse'

export interface Step {
  kind: 'code' | 'output'
  text: string
}

export interface UserMessage {
  id: string
  role: 'user'
  text: string
}

export interface AgentMessage {
  id: string
  role: 'agent'
  steps: Step[]
  running: boolean
  startedAt: number
  elapsedS?: number
  answer?: { text: string; groups: ElementGroup[]; sufficientInfo: boolean }
  error?: string
}

export type Message = UserMessage | AgentMessage

/** Identifies one chip: the agent message id plus the group index. */
export type ChipKey = string
export const chipKey = (messageId: string, index: number): ChipKey => `${messageId}:${index}`
