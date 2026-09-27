import { useEffect, useRef, useState } from 'react'
import type { GroupMode, Viewer } from './engine'
import { parseElement, type ElementProps } from './properties'
import { PropertyPanel } from './PropertyPanel'

export type ViewerLoad =
  | { status: 'idle' }
  | { status: 'loading'; progress: number }
  | { status: 'ready' }
  | { status: 'error'; message: string }

interface Props {
  viewer: Viewer
  load: ViewerLoad
  activeGroup: { label: string; found: number } | null
  mode: GroupMode
  onModeChange: (mode: GroupMode) => void
  onResetView: () => void
}

const MODES: { id: GroupMode; label: string }[] = [
  { id: 'highlight', label: 'Highlight' },
  { id: 'ghost', label: 'Ghost others' },
  { id: 'isolate', label: 'Isolate' },
]

export function ViewerPanel({ viewer, load, activeGroup, mode, onModeChange, onResetView }: Props) {
  const host = useRef<HTMLDivElement>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [props, setProps] = useState<ElementProps | null>(null)
  const [grid, setGrid] = useState(true)

  useEffect(() => {
    const el = host.current
    if (!el) return
    el.appendChild(viewer.el)
    return () => {
      viewer.el.remove()
    }
  }, [viewer])

  useEffect(() => viewer.onSelectionChange(setSelected), [viewer])

  useEffect(() => {
    viewer.setGridVisible(grid)
  }, [viewer, grid])

  useEffect(() => {
    if (selected === null) return
    let stale = false
    viewer
      .itemData(selected)
      .then((data) => {
        if (stale) return
        if (import.meta.env.DEV) console.debug('[cobbie] item data', data)
        setProps(data ? parseElement(selected, data) : null)
      })
      .catch((e) => console.error('Could not read element properties', e))
    return () => {
      stale = true
      setProps(null)
    }
  }, [viewer, selected])

  return (
    <div className="viewer">
      <div className="viewer-host" ref={host} />

      {load.status === 'idle' && (
        <div className="viewer-empty">
          <p>Upload an IFC model or load a sample to start.</p>
        </div>
      )}
      {load.status === 'loading' && (
        <div className="viewer-loading">
          <div className="spinner" />
          <span>Converting IFC geometry {Math.round(load.progress * 100)}%</span>
        </div>
      )}
      {load.status === 'error' && <div className="viewer-error">Viewer could not load the model: {load.message}</div>}

      {load.status === 'ready' && (
        <div className="viewer-toolbar">
          <div className="segmented" role="group" aria-label="Group display mode">
            {MODES.map((m) => (
              <button
                key={m.id}
                className={m.id === mode ? 'active' : ''}
                onClick={() => onModeChange(m.id)}
                title={`Chip display mode: ${m.label}`}
              >
                {m.label}
              </button>
            ))}
          </div>
          <button className="btn" onClick={onResetView}>
            Reset view
          </button>
          <label className="toggle">
            <input type="checkbox" checked={grid} onChange={(e) => setGrid(e.target.checked)} /> Grid
          </label>
        </div>
      )}

      {activeGroup && (
        <div className="viewer-group" data-testid="active-group">
          <span className="dot" /> {activeGroup.label}
          {activeGroup.found === 0 && <span className="muted"> (not found in the viewer model)</span>}
        </div>
      )}

      {selected !== null && (
        <PropertyPanel props={props} loading={props === null} onClose={() => void viewer.clearSelection()} />
      )}
    </div>
  )
}
