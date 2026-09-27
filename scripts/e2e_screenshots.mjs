#!/usr/bin/env node
// End-to-end check of the demo in headless Chrome, driven over CDP with
// Node's built-in WebSocket (Node 22+, no packages needed).
//
// Starts its own backend and Vite dev server (ports 8000 and 5173 unless
// BACKEND_PORT / VITE_PORT say otherwise; both must be free), launches Chrome, loads the fzk_house sample, clicks an
// element, asks "Show me all the doors", clicks the first answer chip, and
// writes three screenshots. Every process it started is killed on exit.
//
// Run from the repo root:  node scripts/e2e_screenshots.mjs
// Env: CHROME (browser binary), OUT_DIR (default ~/Documents/tmp),
//      BACKEND_PORT (8000), VITE_PORT (5173), CDP_PORT (9333), QUESTION, SAMPLE.

import { spawn } from 'node:child_process'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { homedir, platform } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const OUT_DIR = process.env.OUT_DIR ?? join(homedir(), 'Documents', 'tmp')
const CDP_PORT = Number(process.env.CDP_PORT ?? 9333)
const BACKEND_PORT = Number(process.env.BACKEND_PORT ?? 8000)
const VITE_PORT = Number(process.env.VITE_PORT ?? 5173)
const APP_URL = `http://localhost:${VITE_PORT}`
const BACKEND_URL = `http://127.0.0.1:${BACKEND_PORT}/api/health`
const SAMPLE = process.env.SAMPLE ?? 'fzk_house_arc.ifc'
const QUESTION = process.env.QUESTION ?? 'Show me all the doors'

function findChrome() {
  if (process.env.CHROME) return process.env.CHROME
  const candidates =
    platform() === 'darwin'
      ? ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/Applications/Chromium.app/Contents/MacOS/Chromium']
      : [join(homedir(), '.cache/ms-playwright/chromium-1228/chrome-linux64/chrome'), '/usr/bin/chromium', '/usr/bin/google-chrome']
  const found = candidates.find((c) => existsSync(c))
  if (!found) throw new Error('No Chrome found; set CHROME=/path/to/chrome')
  return found
}

const problems = []
let cdpRef = null
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const children = []

function start(name, cmd, args, cwd, env = {}) {
  const child = spawn(cmd, args, { cwd, detached: true, stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env, ...env } })
  const log = []
  child.stdout.on('data', (d) => log.push(String(d)))
  child.stderr.on('data', (d) => log.push(String(d)))
  children.push({ name, child, log })
  return child
}

function cleanup() {
  for (const { child } of children) {
    try {
      process.kill(-child.pid, 'SIGTERM')
    } catch {
      // already gone
    }
  }
}
process.on('exit', cleanup)
for (const sig of ['SIGINT', 'SIGTERM']) process.on(sig, () => process.exit(130))

async function up(url) {
  try {
    const res = await fetch(url)
    return res.ok
  } catch {
    return false
  }
}

async function waitUp(url, timeoutMs, name) {
  const t0 = Date.now()
  while (Date.now() - t0 < timeoutMs) {
    if (await up(url)) return
    await sleep(300)
  }
  const c = children.find((x) => x.name === name)
  throw new Error(`${name} did not come up at ${url}\n${c ? c.log.join('').slice(-2000) : ''}`)
}

// Minimal CDP client over one page target.
class Cdp {
  constructor(ws) {
    this.ws = ws
    this.id = 0
    this.pending = new Map()
    this.listeners = []
    ws.addEventListener('message', (e) => {
      const msg = JSON.parse(e.data)
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id)
        this.pending.delete(msg.id)
        if (msg.error) reject(new Error(`${msg.error.message}`))
        else resolve(msg.result)
      } else if (msg.method) {
        for (const l of this.listeners) l(msg)
      }
    })
  }
  static async connect(url) {
    const ws = new WebSocket(url)
    await new Promise((res, rej) => {
      ws.addEventListener('open', res, { once: true })
      ws.addEventListener('error', rej, { once: true })
    })
    return new Cdp(ws)
  }
  send(method, params = {}) {
    const id = ++this.id
    this.ws.send(JSON.stringify({ id, method, params }))
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }))
  }
  on(fn) {
    this.listeners.push(fn)
  }
  async eval(expression) {
    const r = await this.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
    if (r.exceptionDetails) throw new Error(`eval failed: ${r.exceptionDetails.text} ${expression}`)
    return r.result.value
  }
  async waitFor(expression, timeoutMs, what) {
    const t0 = Date.now()
    while (Date.now() - t0 < timeoutMs) {
      const v = await this.eval(expression)
      if (v) return v
      await sleep(250)
    }
    throw new Error(`Timed out after ${timeoutMs / 1000} s waiting for ${what}`)
  }
  async screenshot(file) {
    const { data } = await this.send('Page.captureScreenshot', { format: 'png' })
    writeFileSync(file, Buffer.from(data, 'base64'))
    console.log(`screenshot: ${file}`)
  }
  async click(x, y) {
    await this.send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y })
    await this.send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 })
    await this.send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 })
  }
}

async function main() {
  mkdirSync(OUT_DIR, { recursive: true })

  for (const url of [BACKEND_URL, APP_URL]) {
    if (await up(url)) throw new Error(`${url} is already in use; stop it or pick other ports`)
  }
  console.log(`starting backend on :${BACKEND_PORT} and vite on :${VITE_PORT}`)
  start('backend', 'uv', ['run', 'uvicorn', 'backend.app:app', '--port', String(BACKEND_PORT)], ROOT)
  start('vite', 'pnpm', ['dev', '--port', String(VITE_PORT), '--strictPort'], join(ROOT, 'frontend'), {
    COBBIE_BACKEND_URL: `http://127.0.0.1:${BACKEND_PORT}`,
  })
  await waitUp(BACKEND_URL, 60_000, 'backend')
  await waitUp(APP_URL, 60_000, 'vite')

  const chrome = findChrome()
  start('chrome', chrome, [
    '--headless=new',
    `--remote-debugging-port=${CDP_PORT}`,
    '--user-data-dir=/tmp/cobbie-chrome',
    '--use-angle=swiftshader',
    '--enable-unsafe-swiftshader',
    '--window-size=1600,1000',
    '--no-first-run',
    '--no-default-browser-check',
    // Ubuntu blocks Chrome's user-namespace sandbox for binaries outside /opt.
    ...(platform() === 'linux' ? ['--no-sandbox'] : []),
    'about:blank',
  ])
  await waitUp(`http://127.0.0.1:${CDP_PORT}/json/version`, 20_000, 'chrome')
  const targets = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()
  const page = targets.find((t) => t.type === 'page')
  const cdp = await Cdp.connect(page.webSocketDebuggerUrl)
  cdpRef = cdp
  cdp.on((msg) => {
    if (msg.method === 'Runtime.consoleAPICalled' && ['error', 'warning', 'assert'].includes(msg.params.type)) {
      const text = msg.params.args.map((a) => a.value ?? a.description ?? a.type).join(' ')
      problems.push(`console.${msg.params.type}: ${text}`)
    } else if (msg.method === 'Runtime.exceptionThrown') {
      const d = msg.params.exceptionDetails
      problems.push(`exception: ${d.exception?.description ?? d.text}`)
    } else if (msg.method === 'Log.entryAdded' && ['error', 'warning'].includes(msg.params.entry.level)) {
      problems.push(`log.${msg.params.entry.level}: ${msg.params.entry.text} ${msg.params.entry.url ?? ''}`)
    }
  })
  await cdp.send('Runtime.enable')
  await cdp.send('Log.enable')
  await cdp.send('Page.enable')
  await cdp.send('Emulation.setDeviceMetricsOverride', { width: 1600, height: 1000, deviceScaleFactor: 1, mobile: false })

  await cdp.send('Page.navigate', { url: APP_URL })
  await cdp.waitFor(`!!document.querySelector('[data-sample="${SAMPLE}"]')`, 20_000, 'sample button')

  // 1. Load the sample: viewer geometry and agent upload.
  const t0 = Date.now()
  await cdp.eval(`document.querySelector('[data-sample="${SAMPLE}"]').click()`)
  await cdp.waitFor(
    `(document.querySelector('[data-testid=pill-viewer]')?.textContent ?? '').includes('ready')`,
    120_000,
    'viewer ready',
  )
  await cdp.waitFor(
    `(document.querySelector('[data-testid=pill-agent]')?.textContent ?? '').includes('ready')`,
    60_000,
    'agent upload ready',
  )
  console.log(`model loaded in ${((Date.now() - t0) / 1000).toFixed(1)} s`)
  await sleep(2500)
  await cdp.screenshot(join(OUT_DIR, 'cobbie-demo-1-loaded.png'))

  // 2. Click an element near the viewer centre and wait for the property panel.
  const rect = await cdp.eval(
    `(() => { const r = document.querySelector('.viewer').getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height } })()`,
  )
  const offsets = [
    [0.5, 0.5],
    [0.45, 0.55],
    [0.55, 0.45],
    [0.4, 0.6],
    [0.6, 0.6],
    [0.5, 0.65],
  ]
  let picked = false
  for (const [fx, fy] of offsets) {
    await cdp.click(Math.round(rect.x + rect.w * fx), Math.round(rect.y + rect.h * fy))
    try {
      await cdp.waitFor(`!!document.querySelector('.prop-guid')`, 5000, 'property panel')
      picked = true
      break
    } catch {
      // empty space; try the next point
    }
  }
  if (!picked) throw new Error('Clicking the viewer never opened the property panel')
  const panelText = await cdp.eval(`document.querySelector('[data-testid=property-panel]').innerText`)
  console.log(`property panel:\n  ${panelText.split('\n').slice(0, 8).join('\n  ')}`)
  // Open all property groups for the screenshot.
  await cdp.eval(
    `document.querySelectorAll('.prop-group-head[aria-expanded=false]').forEach((b, i) => { if (i < 2) b.click() })`,
  )
  await sleep(1000)
  await cdp.screenshot(join(OUT_DIR, 'cobbie-demo-2-properties.png'))
  await cdp.eval(`document.querySelector('.prop-head .icon-btn').click()`)

  // 3. Ask a question, wait for the answer, click the first chip.
  await cdp.eval(`document.querySelector('[data-testid=chat-input]').focus()`)
  await cdp.send('Input.insertText', { text: QUESTION })
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 })
  await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Enter', code: 'Enter', windowsVirtualKeyCode: 13 })
  const tq = Date.now()
  await cdp.waitFor(`!!document.querySelector('.running')`, 5000, 'turn started')
  // Live steps must stream in before the turn ends (SSE passes the proxy unbuffered).
  const live = await cdp.waitFor(
    `document.querySelector('.steps-body .step-code') ? 'live' : (!document.querySelector('.running') ? 'ended' : '')`,
    120_000,
    'first agent step',
  )
  console.log(`first step streamed: ${live === 'live' ? 'yes, while running' : 'no, turn ended first'}`)
  if (live === 'live') await cdp.screenshot(join(OUT_DIR, 'cobbie-demo-running.png'))
  await cdp.waitFor(`!document.querySelector('.running')`, 120_000, 'agent answer')
  console.log(`answer after ${((Date.now() - tq) / 1000).toFixed(1)} s`)
  const answer = await cdp.eval(`document.querySelector('[data-testid=agent-message]:last-of-type')?.innerText ?? ''`)
  console.log(`answer:\n  ${answer.split('\n').slice(0, 12).join('\n  ')}`)
  const chips = await cdp.eval(`[...document.querySelectorAll('[data-testid=element-chip]')].map((c) => c.textContent)`)
  console.log(`chips: ${JSON.stringify(chips)}`)
  if (chips.length > 0) {
    await cdp.eval(`document.querySelector('[data-testid=element-chip]').click()`)
    await cdp.waitFor(`!!document.querySelector('[data-testid=active-group]')`, 5000, 'active group')
    await sleep(3000)
  } else {
    problems.push('answer had no element chips')
  }
  await cdp.screenshot(join(OUT_DIR, 'cobbie-demo-3-chat-chip.png'))

  // 4. Isolate mode, then pick one element from the chip's list.
  if (chips.length > 0) {
    await cdp.eval(`[...document.querySelectorAll('.segmented button')].find((b) => b.textContent === 'Isolate').click()`)
    await cdp.eval(`document.querySelector('.chip-expand').click()`)
    await cdp.waitFor(`!!document.querySelector('.chip-list button')`, 5000, 'chip element list')
    await cdp.eval(`document.querySelector('.chip-list button').click()`)
    await cdp.waitFor(`!!document.querySelector('.prop-guid')`, 10_000, 'property panel for chip element')
    await sleep(2500)
    await cdp.screenshot(join(OUT_DIR, 'cobbie-demo-4-isolate-element.png'))
  }

  // 5. New chat clears the conversation; uploading a file swaps the model.
  await cdp.eval(`[...document.querySelectorAll('.chat-head .btn')].find((b) => b.textContent === 'New chat').click()`)
  await cdp.waitFor(`!!document.querySelector('.chat-empty') && !document.querySelector('[data-testid=active-group]')`, 15_000, 'chat reset')
  const other = SAMPLE === 'duplex_arc.ifc' ? 'fzk_house_arc.ifc' : 'duplex_arc.ifc'
  const { root } = await cdp.send('DOM.getDocument')
  const { nodeId } = await cdp.send('DOM.querySelector', { nodeId: root.nodeId, selector: '[data-testid=file-input]' })
  await cdp.send('DOM.setFileInputFiles', { nodeId, files: [join(ROOT, 'samples', other)] })
  await cdp.waitFor(
    `document.querySelector('.model-name')?.textContent === '${other}' &&
     document.querySelector('[data-testid=pill-viewer]').textContent.includes('ready') &&
     document.querySelector('[data-testid=pill-agent]').textContent.includes('ready')`,
    120_000,
    'uploaded model ready',
  )
  console.log(`upload via file input: ${other} ready`)
  await sleep(2500)
  await cdp.screenshot(join(OUT_DIR, 'cobbie-demo-5-uploaded.png'))

  console.log(problems.length ? `\nbrowser problems (${problems.length}):\n  ${problems.join('\n  ')}` : '\nno browser console errors')
  cdp.ws.close()
  return problems.some((p) => !p.startsWith('console.warning') && !p.startsWith('log.warning')) ? 1 : 0
}

main()
  .then((code) => process.exit(code))
  .catch(async (e) => {
    console.error(e)
    if (problems.length) console.log(`browser problems:\n  ${problems.join('\n  ')}`)
    if (cdpRef) await cdpRef.screenshot(join(OUT_DIR, 'cobbie-demo-failure.png')).catch(() => undefined)
    process.exit(1)
  })
