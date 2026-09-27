import { useState } from 'react'
import type { ElementGroup } from './sse'

const LIST_LIMIT = 200

interface Props {
  group: ElementGroup
  active: boolean
  onSelect: () => void
  onElementClick: (globalId: string) => void
}

export function ElementChip({ group, active, onSelect, onElementClick }: Props) {
  const [open, setOpen] = useState(false)
  const preview = group.elements
    .slice(0, 5)
    .map((e) => e.name || e.ifc_class)
    .join('\n')
  const more = group.elements.length > 5 ? `\n\u2026 and ${group.elements.length - 5} more` : ''

  return (
    <div className={`chip-wrap${open ? ' open' : ''}`}>
      <div className={`chip${active ? ' active' : ''}`}>
        <button className="chip-label" onClick={onSelect} title={preview + more} data-testid="element-chip">
          {group.label}
        </button>
        <button
          className="chip-expand"
          onClick={() => setOpen(!open)}
          title={open ? 'Hide elements' : 'List elements'}
          aria-expanded={open}
        >
          {open ? '\u25B4' : '\u25BE'}
        </button>
      </div>
      {open && (
        <ul className="chip-list">
          {group.elements.slice(0, LIST_LIMIT).map((e) => (
            <li key={e.global_id}>
              <button onClick={() => onElementClick(e.global_id)} title={e.global_id}>
                <span className="chip-el-name">{e.name || '(no name)'}</span>
                <span className="muted">{e.ifc_class}</span>
              </button>
            </li>
          ))}
          {group.elements.length > LIST_LIMIT && (
            <li className="muted">{group.elements.length - LIST_LIMIT} more not listed</li>
          )}
        </ul>
      )}
    </div>
  )
}
