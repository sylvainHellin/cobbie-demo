// Turns fragments ItemData (attributes + IsDefinedBy relations) into a
// display-ready structure for the property panel.
import type { ItemData } from '@thatopen/fragments'
import * as WEBIFC from 'web-ifc'

// Fragments stores categories upper-cased (IFCWALL); map back to IfcWall.
const CLASS_NAMES = new Map<string, string>()
for (const schema of [WEBIFC.IFC2X3, WEBIFC.IFC4, WEBIFC.IFC4X3]) {
  for (const name of Object.keys(schema)) if (name.startsWith('Ifc')) CLASS_NAMES.set(name.toUpperCase(), name)
}

export function ifcClassName(category: string): string {
  return CLASS_NAMES.get(category.toUpperCase()) ?? category
}

export interface PropRow {
  name: string
  value: string
}

export interface PropGroup {
  title: string
  kind: 'attributes' | 'pset' | 'qto'
  rows: PropRow[]
}

export interface ElementProps {
  localId: number
  globalId: string
  ifcClass: string
  name: string
  groups: PropGroup[]
}

type Attr = { value: unknown; type?: string }

function isAttr(x: unknown): x is Attr {
  return typeof x === 'object' && x !== null && !Array.isArray(x) && 'value' in x
}

function attr(item: ItemData, key: string): unknown {
  const a = item[key]
  return isAttr(a) ? a.value : undefined
}

function rel(item: ItemData, key: string): ItemData[] {
  const r = item[key]
  return Array.isArray(r) ? r : []
}

function fmt(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : String(Number(v.toFixed(4)))
  if (typeof v === 'boolean') return v ? 'True' : 'False'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

const QUANTITY_KEYS = ['LengthValue', 'AreaValue', 'VolumeValue', 'CountValue', 'WeightValue', 'TimeValue']

function propertyValue(p: ItemData): string {
  for (const key of ['NominalValue', ...QUANTITY_KEYS]) {
    const v = attr(p, key)
    if (v !== undefined && v !== null) return fmt(v)
  }
  const list = attr(p, 'ListValues') ?? attr(p, 'EnumerationValues')
  if (list !== undefined) return fmt(list)
  return ''
}

const HIDDEN_ATTRS = new Set(['GlobalId', 'OwnerHistory', 'ObjectPlacement', 'Representation'])

export function parseElement(localId: number, item: ItemData): ElementProps {
  const category = String(attr(item, '_category') ?? '')
  const attributes: PropRow[] = []
  for (const [key, a] of Object.entries(item)) {
    if (key.startsWith('_') || HIDDEN_ATTRS.has(key) || !isAttr(a)) continue
    const value = fmt(a.value)
    if (value !== '') attributes.push({ name: key, value })
  }
  for (const container of rel(item, 'ContainedInStructure')) {
    const n = attr(container, 'Name')
    if (n) attributes.push({ name: 'Container', value: `${fmt(n)} (${ifcClassName(fmt(attr(container, '_category')))})` })
  }

  const groups: PropGroup[] = [{ title: 'Attributes', kind: 'attributes', rows: attributes }]
  for (const def of rel(item, 'IsDefinedBy')) {
    const defCat = String(attr(def, '_category') ?? '').toUpperCase()
    const title = fmt(attr(def, 'Name')) || defCat
    const members = defCat.includes('QUANTITY') ? rel(def, 'Quantities') : rel(def, 'HasProperties')
    const rows = members
      .map((p) => ({ name: fmt(attr(p, 'Name')), value: propertyValue(p) }))
      .filter((r) => r.name)
    if (rows.length === 0) continue
    groups.push({ title, kind: defCat.includes('QUANTITY') ? 'qto' : 'pset', rows })
  }

  return {
    localId,
    globalId: fmt(attr(item, '_guid') ?? attr(item, 'GlobalId')),
    ifcClass: ifcClassName(category),
    name: fmt(attr(item, 'Name')),
    groups,
  }
}
