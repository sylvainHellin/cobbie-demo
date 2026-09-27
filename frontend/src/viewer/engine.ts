// That Open viewer: one world, one IFC model at a time, click selection and
// element-group display (highlight, ghost others, isolate).
import * as OBC from '@thatopen/components'
import * as FRAGS from '@thatopen/fragments'
import * as THREE from 'three'
import workerUrl from '@thatopen/fragments/worker?url'
import { installWebIfcCompat } from './webIfcCompat'

installWebIfcCompat()

export type GroupMode = 'highlight' | 'ghost' | 'isolate'

type World = OBC.SimpleWorld<OBC.SimpleScene, OBC.SimpleCamera, OBC.SimpleRenderer>

const ACCENT = new THREE.Color('#3070B3')
// Selection uses the accent colour; inside a highlighted group it turns darker.
const SELECTED_IN_GROUP = new THREE.Color('#15385F')
const GHOST_OPACITY = 0.12

const solid = (color: THREE.Color): FRAGS.MaterialDefinition => ({
  color,
  opacity: 1,
  transparent: false,
  renderedFaces: FRAGS.RenderedFaces.TWO,
})

export class Viewer {
  /** Element the renderer draws into; the React panel mounts it. */
  readonly el: HTMLDivElement
  readonly ready: Promise<void>

  private components = new OBC.Components()
  private world: World
  private fragments: OBC.FragmentsManager
  private loader: OBC.IfcLoader
  private grid: OBC.SimpleGrid
  private model: FRAGS.FragmentsModel | null = null
  private allIds: number[] = []
  private loadCount = 0

  private group: number[] = []
  private mode: GroupMode = 'ghost'
  private selected: number | null = null
  private applying: Promise<void> = Promise.resolve()
  private selectionListeners = new Set<(localId: number | null) => void>()

  constructor() {
    this.el = document.createElement('div')
    this.el.className = 'viewer-canvas'

    const worlds = this.components.get(OBC.Worlds)
    this.world = worlds.create<OBC.SimpleScene, OBC.SimpleCamera, OBC.SimpleRenderer>()
    this.world.scene = new OBC.SimpleScene(this.components)
    this.world.scene.setup()
    this.world.scene.three.background = new THREE.Color('#F4F6F8')
    this.world.renderer = new OBC.SimpleRenderer(this.components, this.el)
    this.world.camera = new OBC.SimpleCamera(this.components)
    this.world.camera.controls.setLookAt(20, 15, 20, 0, 0, 0)
    this.components.init()

    this.grid = this.components.get(OBC.Grids).create(this.world)

    this.fragments = this.components.get(OBC.FragmentsManager)
    this.fragments.init(workerUrl)
    const controls = this.world.camera.controls
    controls.addEventListener('update', () => this.fragments.core.update())
    controls.addEventListener('rest', () => this.fragments.core.update(true))
    this.fragments.list.onItemSet.add(({ value: model }) => {
      model.useCamera(this.world.camera.three)
      this.world.scene.three.add(model.object)
      this.fragments.core.update(true)
    })
    // Small random polygon offsets avoid z-fighting between coplanar faces.
    this.fragments.core.models.materials.list.onItemSet.add(({ value: material }) => {
      if (!('isLodMaterial' in material && material.isLodMaterial)) {
        material.polygonOffset = true
        material.polygonOffsetUnits = 1
        material.polygonOffsetFactor = Math.random()
      }
    })

    this.loader = this.components.get(OBC.IfcLoader)
    this.ready = this.loader.setup({
      autoSetWasm: false,
      wasm: { path: `${window.location.origin}/wasm/`, absolute: true },
    })

    this.setupPicking()
  }

  get hasModel(): boolean {
    return this.model !== null
  }

  setGridVisible(visible: boolean) {
    this.grid.visible = visible
  }

  onSelectionChange(cb: (localId: number | null) => void): () => void {
    this.selectionListeners.add(cb)
    return () => this.selectionListeners.delete(cb)
  }

  /** Removes the current model and resets all selection state. */
  async clear(): Promise<void> {
    this.loadCount++
    this.group = []
    this.setSelected(null)
    const model = this.model
    this.model = null
    this.allIds = []
    if (model) {
      // No core.update() here: it never resolves right after a dispose.
      await this.fragments.core.disposeModel(model.modelId)
    }
  }

  /** Converts IFC bytes to fragments, shows them and fits the camera. */
  async load(bytes: Uint8Array, onProgress?: (p: number) => void): Promise<void> {
    await this.ready
    await this.clear()
    const token = this.loadCount
    const model = await this.loader.load(bytes, true, `model-${token}`, {
      processData: { progressCallback: (p) => onProgress?.(p) },
    })
    if (token !== this.loadCount) {
      // A newer load or clear started meanwhile; drop this model.
      await this.fragments.core.disposeModel(model.modelId)
      return
    }
    this.model = model
    this.allIds = await model.getItemsIdsWithGeometry()
    await this.fragments.core.update(true)
    await this.fitAll()
  }

  async fitAll(): Promise<void> {
    if (!this.model) return
    await this.world.camera.fitToItems()
  }

  /** Maps IFC GlobalIds to local ids of the current model, dropping unknown ones. */
  async localIds(globalIds: string[]): Promise<number[]> {
    if (!this.model || globalIds.length === 0) return []
    const ids = await this.model.getLocalIdsByGuids(globalIds)
    return ids.filter((id): id is number => id !== null)
  }

  async showGroup(globalIds: string[], mode: GroupMode): Promise<number> {
    const ids = await this.localIds(globalIds)
    this.group = ids
    this.mode = mode
    this.setSelected(null)
    await this.applyState()
    if (ids.length > 0) await this.fitIds(ids)
    return ids.length
  }

  async setMode(mode: GroupMode): Promise<void> {
    this.mode = mode
    await this.applyState()
  }

  async resetView(): Promise<void> {
    this.group = []
    this.setSelected(null)
    await this.applyState()
    await this.fitAll()
  }

  async selectGlobalId(globalId: string): Promise<boolean> {
    const [id] = await this.localIds([globalId])
    if (id === undefined) return false
    this.setSelected(id)
    await this.applyState()
    await this.fitIds([id])
    return true
  }

  async clearSelection(): Promise<void> {
    if (this.selected === null) return
    this.setSelected(null)
    await this.applyState()
  }

  /** Raw item data with attributes, property sets and spatial container. */
  async itemData(localId: number): Promise<FRAGS.ItemData | null> {
    if (!this.model) return null
    const [data] = await this.model.getItemsData([localId], {
      attributesDefault: true,
      relations: {
        IsDefinedBy: { attributes: true, relations: true },
        DefinesOccurrence: { attributes: false, relations: false },
        ContainedInStructure: { attributes: true, relations: false },
      },
    })
    return data ?? null
  }

  private setSelected(id: number | null) {
    if (this.selected === id) return
    this.selected = id
    for (const cb of this.selectionListeners) cb(id)
  }

  /** Fits the camera to the items with some margin so their context stays visible. */
  private async fitIds(ids: number[]) {
    if (!this.model) return
    const box = await this.model.getMergedBox(ids)
    if (box.isEmpty()) return
    const sphere = box.getBoundingSphere(new THREE.Sphere())
    sphere.radius = Math.max(sphere.radius * 1.6, 2)
    await this.world.camera.controls.fitToSphere(sphere, true)
  }

  /** Re-applies group display and selection from scratch, serialized. */
  private applyState(): Promise<void> {
    this.applying = this.applying.then(() => this.doApplyState()).catch((e) => console.error(e))
    return this.applying
  }

  private async doApplyState() {
    const model = this.model
    if (!model) return
    await model.resetHighlight()
    await model.resetVisible()
    const sel = this.selected
    if (this.group.length > 0) {
      const inGroup = new Set(this.group)
      const others = this.allIds.filter((id) => !inGroup.has(id) && id !== sel)
      if (this.mode === 'ghost' && others.length) await model.setOpacity(others, GHOST_OPACITY)
      if (this.mode === 'isolate' && others.length) await model.setVisible(others, false)
      const accent = this.group.filter((id) => id !== sel)
      if (accent.length) await model.highlight(accent, solid(ACCENT))
    }
    if (sel !== null) await model.highlight([sel], solid(this.group.length ? SELECTED_IN_GROUP : ACCENT))
    await this.fragments.core.update(true)
  }

  private setupPicking() {
    const down = new THREE.Vector2()
    this.el.addEventListener('pointerdown', (e) => down.set(e.clientX, e.clientY))
    this.el.addEventListener('pointerup', async (e) => {
      if (e.button !== 0 || !this.model) return
      if (down.distanceTo(new THREE.Vector2(e.clientX, e.clientY)) > 4) return // was a drag
      const hit = await this.model.raycast({
        camera: this.world.camera.three,
        mouse: new THREE.Vector2(e.clientX, e.clientY),
        dom: this.world.renderer!.three.domElement,
      })
      if (hit) {
        this.setSelected(hit.localId)
        await this.applyState()
      } else {
        await this.clearSelection()
      }
    })
  }
}

let instance: Viewer | null = null

/** The app has exactly one viewer; it survives React remounts. */
export function getViewer(): Viewer {
  instance ??= new Viewer()
  return instance
}
