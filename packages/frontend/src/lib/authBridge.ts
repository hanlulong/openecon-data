import { isDataDomainHost } from './sharedStorage'

export interface BridgePayload {
  token?: string
  anonSessionId?: string
  supabaseAccessToken?: string
  supabaseRefreshToken?: string
}

const BRIDGE_SOURCE_ORIGIN = 'https://openecon.ai'
const BRIDGE_PAGE_PATH = '/auth-bridge-sender.html'
const BRIDGE_REQUEST_TYPE = 'OPENECON_AUTH_BRIDGE_REQUEST'
const BRIDGE_RESPONSE_TYPE = 'OPENECON_AUTH_BRIDGE_RESPONSE'
const BRIDGE_TIMEOUT_MS = 5000
const BRIDGE_ATTEMPT_KEY = 'openecon_auth_bridge_attempted'

function randomNonce(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`
}

/**
 * Request auth/session payload from the old openecon.ai origin.
 * This is intended for one-time migration onto data.openecon.ai/data.openecon.io.
 */
export async function requestCrossDomainBridgePayload(): Promise<BridgePayload | null> {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    return null
  }

  if (!isDataDomainHost()) {
    return null
  }

  if (sessionStorage.getItem(BRIDGE_ATTEMPT_KEY)) {
    return null
  }

  sessionStorage.setItem(BRIDGE_ATTEMPT_KEY, Date.now().toString())

  return new Promise((resolve) => {
    const nonce = randomNonce()
    const bridgeUrl = new URL(`${BRIDGE_SOURCE_ORIGIN}${BRIDGE_PAGE_PATH}`)
    bridgeUrl.searchParams.set('target_origin', window.location.origin)

    const iframe = document.createElement('iframe')
    iframe.src = bridgeUrl.toString()
    iframe.style.display = 'none'
    iframe.setAttribute('aria-hidden', 'true')
    iframe.setAttribute('title', 'auth-bridge')

    let settled = false
    const finish = (payload: BridgePayload | null) => {
      if (settled) {
        return
      }
      settled = true
      window.removeEventListener('message', onMessage)
      clearTimeout(timeoutId)
      iframe.remove()
      resolve(payload)
    }

    const onMessage = (event: MessageEvent) => {
      if (event.origin !== BRIDGE_SOURCE_ORIGIN || event.source !== iframe.contentWindow) {
        return
      }

      const data = event.data
      if (!data || data.type !== BRIDGE_RESPONSE_TYPE || data.nonce !== nonce) {
        return
      }

      const payload = typeof data.payload === 'object' && data.payload ? data.payload : null
      finish(payload as BridgePayload | null)
    }

    const timeoutId = window.setTimeout(() => finish(null), BRIDGE_TIMEOUT_MS)

    window.addEventListener('message', onMessage)

    iframe.onload = () => {
      iframe.contentWindow?.postMessage(
        {
          type: BRIDGE_REQUEST_TYPE,
          nonce,
          targetOrigin: window.location.origin,
        },
        BRIDGE_SOURCE_ORIGIN
      )
    }

    document.body.appendChild(iframe)
  })
}
