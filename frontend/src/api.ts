// REST helpers for the FastAPI backend (proxied under /api by Vite).

export interface ModelInfo {
  model_id: string
  filename: string
  schema: string
  n_products: number
}

export interface Sample {
  name: string
  size: number
}

async function errorMessage(res: Response): Promise<string> {
  try {
    const body = await res.json()
    if (typeof body?.detail === 'string') return body.detail
  } catch {
    // not JSON
  }
  return `${res.status} ${res.statusText}`
}

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(await errorMessage(res))
  return (await res.json()) as T
}

export async function health(): Promise<{ status: string; model: string }> {
  return json(await fetch('/api/health'))
}

export async function listSamples(): Promise<Sample[]> {
  return json(await fetch('/api/samples'))
}

export async function fetchSample(name: string): Promise<File> {
  const res = await fetch(`/api/samples/${encodeURIComponent(name)}`)
  if (!res.ok) throw new Error(await errorMessage(res))
  return new File([await res.blob()], name)
}

export async function uploadModel(file: File): Promise<ModelInfo> {
  const body = new FormData()
  body.append('file', file)
  return json(await fetch('/api/models', { method: 'POST', body }))
}

export async function resetChat(modelId: string): Promise<void> {
  await json(await fetch(`/api/models/${modelId}/reset`, { method: 'POST' }))
}

export { errorMessage }
