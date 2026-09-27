// Answer text as GitHub-flavoured markdown (tables, strikethrough, autolinks).
// Tables get a scrolling wrapper so wide ones do not stretch the chat panel.
import type { ComponentProps } from 'react'
import Markdown, { type ExtraProps } from 'react-markdown'
import remarkGfm from 'remark-gfm'

const plugins = [remarkGfm]

const components = {
  table: ({ node: _node, ...props }: ComponentProps<'table'> & ExtraProps) => (
    <div className="table-wrap">
      <table {...props} />
    </div>
  ),
}

export function AnswerMarkdown({ text }: { text: string }) {
  return (
    <Markdown remarkPlugins={plugins} components={components}>
      {text}
    </Markdown>
  )
}
