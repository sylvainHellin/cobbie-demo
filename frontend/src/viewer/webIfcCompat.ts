// Workaround for web-ifc 0.0.78: the published browser wasm (web-ifc.wasm)
// binds StreamMeshes with 3 parameters, while the JS wrapper passes a 4th
// (applyLinearScalingFactor), so every IFC import fails with
// "function StreamMeshes called with 4 arguments, expected 3".
// After Init, geometry functions on the wasm module are wrapped so that an
// argument-count error is retried with the trailing extra arguments dropped.
// Remove this once web-ifc ships a matching wasm (or pin 0.0.77).
import * as WEBIFC from 'web-ifc'

const GEOMETRY_FNS = ['StreamMeshes', 'StreamAllMeshes', 'StreamAllMeshesWithTypes', 'LoadAllGeometry', 'GetFlatMesh']
const ARG_COUNT = /called with (\d+) arguments?, expected (\d+)/

type Fn = (...args: unknown[]) => unknown
type Patched = Fn & { __cobbieCompat?: true }

let installed = false

export function installWebIfcCompat(): void {
  if (installed) return
  installed = true
  const proto = WEBIFC.IfcAPI.prototype as unknown as { Init: Fn }
  const init = proto.Init
  proto.Init = async function (this: { wasmModule: Record<string, unknown> }, ...args: unknown[]) {
    await init.apply(this, args)
    const mod = this.wasmModule
    for (const name of GEOMETRY_FNS) {
      const fn = mod[name] as Patched | undefined
      if (typeof fn !== 'function' || fn.__cobbieCompat) continue
      const wrapped: Patched = (...a: unknown[]) => {
        try {
          return fn(...a)
        } catch (e) {
          const m = ARG_COUNT.exec(e instanceof Error ? e.message : String(e))
          const expected = m ? Number(m[2]) : NaN
          if (expected < a.length) return fn(...a.slice(0, expected))
          throw e
        }
      }
      wrapped.__cobbieCompat = true
      mod[name] = wrapped
    }
  }
}
