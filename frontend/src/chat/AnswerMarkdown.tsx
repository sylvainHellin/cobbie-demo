// Markdown with GFM pipe tables. react-markdown only does CommonMark without
// remark-gfm, so table blocks are cut out and rendered here; everything else
// (and each table cell) goes through react-markdown.
import type { ReactNode } from 'react'
import Markdown from 'react-markdown'

type Block = { kind: 'md'; text: string } | { kind: 'table'; head: string[]; align: string[]; rows: string[][] }

const SEPARATOR = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/

function cells(line: string): string[] {
  let s = line.trim()
  if (s.startsWith('|')) s = s.slice(1)
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1)
  return s.split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, '|'))
}

function splitBlocks(text: string): Block[] {
  const lines = text.split('\n')
  const blocks: Block[] = []
  let md: string[] = []
  let inFence = false
  const flush = () => {
    if (md.length) blocks.push({ kind: 'md', text: md.join('\n') })
    md = []
  }
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (/^\s*(```|~~~)/.test(line)) inFence = !inFence
    if (!inFence && line.includes('|') && i + 1 < lines.length && SEPARATOR.test(lines[i + 1])) {
      const head = cells(line)
      const align = cells(lines[i + 1]).map((c) =>
        c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : 'left',
      )
      const rows: string[][] = []
      i += 2
      while (i < lines.length && lines[i].includes('|') && lines[i].trim() !== '') {
        rows.push(cells(lines[i]))
        i++
      }
      i--
      flush()
      blocks.push({ kind: 'table', head, align, rows })
      continue
    }
    md.push(line)
  }
  flush()
  return blocks
}

const inline = { p: ({ children }: { children?: ReactNode }) => <>{children}</> }

function Cell({ text }: { text: string }) {
  return <Markdown components={inline}>{text}</Markdown>
}

export function AnswerMarkdown({ text }: { text: string }) {
  return (
    <>
      {splitBlocks(text).map((b, i) =>
        b.kind === 'md' ? (
          <Markdown key={i}>{b.text}</Markdown>
        ) : (
          <div key={i} className="table-wrap">
            <table>
              <thead>
                <tr>
                  {b.head.map((h, j) => (
                    <th key={j} style={{ textAlign: b.align[j] as 'left' }}>
                      <Cell text={h} />
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {b.rows.map((r, k) => (
                  <tr key={k}>
                    {b.head.map((_, j) => (
                      <td key={j} style={{ textAlign: b.align[j] as 'left' }}>
                        <Cell text={r[j] ?? ''} />
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ),
      )}
    </>
  )
}
