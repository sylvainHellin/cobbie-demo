import { useState } from 'react'
import type { ElementProps, PropGroup } from './properties'

function Group({ group, defaultOpen }: { group: PropGroup; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen)
  return (
    <section className="prop-group">
      <button className="prop-group-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="caret">{open ? '\u25BE' : '\u25B8'}</span>
        <span className="prop-group-title">{group.title}</span>
        <span className="muted">{group.rows.length}</span>
      </button>
      {open && (
        <table className="prop-table">
          <tbody>
            {group.rows.map((r, i) => (
              <tr key={`${r.name}-${i}`}>
                <th>{r.name}</th>
                <td>{r.value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

interface Props {
  props: ElementProps | null
  loading: boolean
  onClose: () => void
}

export function PropertyPanel({ props, loading, onClose }: Props) {
  return (
    <aside className="prop-panel" data-testid="property-panel">
      <header className="prop-head">
        <div className="prop-title">
          <div className="prop-class">{props?.ifcClass ?? 'Element'}</div>
          <div className="prop-name">{loading ? 'Loading properties\u2026' : props?.name || '(no name)'}</div>
        </div>
        <button className="icon-btn" onClick={onClose} title="Close" aria-label="Close">
          {'\u2715'}
        </button>
      </header>
      {props && (
        <div className="prop-body">
          <div className="prop-guid">
            <span className="muted">GlobalId</span> <code>{props.globalId}</code>
          </div>
          {props.groups.map((g, i) => (
            <Group key={`${props.localId}-${g.title}-${i}`} group={g} defaultOpen={i === 0} />
          ))}
        </div>
      )}
    </aside>
  )
}
