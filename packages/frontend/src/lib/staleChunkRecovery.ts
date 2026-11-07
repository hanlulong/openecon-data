/**
 * Recover a long-lived tab from a deploy that deleted its lazy route chunks.
 *
 * Routes are code-split (React.lazy) and each deploy rsyncs the new build with
 * --delete, so the previous build's content-hashed chunks disappear. A tab
 * opened before the deploy still holds the OLD index.html; when it next
 * navigates to a lazy route it requests an old chunk URL, gets a 404/HTML
 * response (served with nosniff, so it is not executed as a module) and the
 * dynamic import rejects — the route stays broken until a manual reload.
 *
 * Vite's preload helper dispatches a cancelable `vite:preloadError` event for
 * exactly this failure. Reloading fetches the new index.html and its current
 * chunk manifest. A sessionStorage timestamp guard limits this to one reload
 * per window, so a chunk that is genuinely broken in the NEW build surfaces
 * through the ErrorBoundary instead of looping reloads.
 *
 * Once this page has started a reload, every later preloadError in the same
 * page lifetime is suppressed too: one route fires an event per failed file
 * (each CSS dependency, then the module itself), and the reload replaces all
 * of them. Vite resolves a suppressed import to `undefined`, so route loaders
 * go through loadRoute(), which then stays pending (the Suspense fallback
 * shows) instead of throwing into the ErrorBoundary while the reload lands.
 */

export const STALE_CHUNK_RELOAD_KEY = 'openecon_stale_chunk_reload_at'
// A reload attempted within this window is treated as "already tried".
export const STALE_CHUNK_RELOAD_WINDOW_MS = 60_000

type StorageLike = Pick<Storage, 'getItem' | 'setItem'>

export interface StaleChunkState {
  reloadPending: boolean
}

export interface StaleChunkDeps {
  storage: StorageLike | null
  reload: () => void
  now: () => number
  // Per-page-lifetime state; omitted, every event is judged on its own.
  state?: StaleChunkState
}

/**
 * Handle one `vite:preloadError` event. Returns true when it triggered a
 * reload (and suppressed the error), false when it let the error propagate.
 */
export function handleStaleChunkError(event: Event, deps: StaleChunkDeps): boolean {
  // This page already started a reload that replaces every chunk: suppress
  // the sibling failures instead of letting them reach the ErrorBoundary.
  if (deps.state?.reloadPending) {
    event.preventDefault()
    return true
  }
  // Without storage there is no loop guard, so never auto-reload.
  if (!deps.storage) return false
  const now = deps.now()
  try {
    const lastAttempt = Number(deps.storage.getItem(STALE_CHUNK_RELOAD_KEY))
    const sinceLast = now - lastAttempt
    if (Number.isFinite(sinceLast) && sinceLast >= 0 && sinceLast < STALE_CHUNK_RELOAD_WINDOW_MS) {
      return false // already reloaded once for this failure — let it surface
    }
    deps.storage.setItem(STALE_CHUNK_RELOAD_KEY, String(now))
  } catch {
    return false // storage blocked (privacy mode / quota): no guard, no reload
  }
  event.preventDefault()
  if (deps.state) deps.state.reloadPending = true
  deps.reload()
  return true
}

const pageState: StaleChunkState = { reloadPending: false }

export function isStaleChunkReloadPending(): boolean {
  return pageState.reloadPending
}

/**
 * Wrap a lazy route import for React.lazy. When the import was suppressed
 * because a stale-chunk reload is under way, Vite resolves it to `undefined`;
 * return a promise that never settles so the route keeps showing its Suspense
 * fallback until the reload replaces the page. Any other `undefined` module is
 * a real failure and rejects so the ErrorBoundary handles it.
 */
export function loadRoute<M, C>(
  load: () => Promise<M>,
  pick: (module: M) => C,
  reloadPending: () => boolean = isStaleChunkReloadPending,
): Promise<{ default: C }> {
  return load().then((module) => {
    if (module === undefined || module === null) {
      if (reloadPending()) return new Promise<never>(() => {})
      throw new Error('Route module failed to load')
    }
    return { default: pick(module) }
  })
}

let installed = false

export function installStaleChunkRecovery(): void {
  if (installed || typeof window === 'undefined') return
  installed = true
  window.addEventListener('vite:preloadError', (event) => {
    let storage: StorageLike | null = null
    try {
      storage = window.sessionStorage
    } catch {
      storage = null
    }
    handleStaleChunkError(event, {
      storage,
      reload: () => window.location.reload(),
      now: () => Date.now(),
      state: pageState,
    })
  })
}
