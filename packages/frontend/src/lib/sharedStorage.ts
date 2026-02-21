const OPENECON_ROOT_DOMAIN = '.openecon.ai'

function isBrowser(): boolean {
  return typeof window !== 'undefined' && typeof document !== 'undefined'
}

function canUseRootDomainCookie(hostname: string): boolean {
  return hostname === 'openecon.ai' || hostname.endsWith('.openecon.ai')
}

function resolveCookieDomain(): string | undefined {
  if (!isBrowser()) {
    return undefined
  }
  return canUseRootDomainCookie(window.location.hostname) ? OPENECON_ROOT_DOMAIN : undefined
}

function buildCookie(name: string, value: string, maxAgeSeconds: number, domain?: string): string {
  const parts = [
    `${name}=${encodeURIComponent(value)}`,
    'Path=/',
    'SameSite=Lax',
    `Max-Age=${maxAgeSeconds}`,
  ]

  if (domain) {
    parts.push(`Domain=${domain}`)
  }

  if (isBrowser() && window.location.protocol === 'https:') {
    parts.push('Secure')
  }

  return parts.join('; ')
}

export function setSharedCookie(name: string, value: string, maxAgeSeconds: number): void {
  if (!isBrowser()) {
    return
  }

  const domain = resolveCookieDomain()
  document.cookie = buildCookie(name, value, maxAgeSeconds, domain)
}

export function getCookie(name: string): string | null {
  if (!isBrowser()) {
    return null
  }

  const prefix = `${name}=`
  const match = document.cookie
    .split(';')
    .map((entry) => entry.trim())
    .find((entry) => entry.startsWith(prefix))

  if (!match) {
    return null
  }

  return decodeURIComponent(match.slice(prefix.length))
}

export function removeSharedCookie(name: string): void {
  if (!isBrowser()) {
    return
  }

  const domain = resolveCookieDomain()
  document.cookie = buildCookie(name, '', 0, domain)
  // Also clear host-only cookie variant for safety.
  document.cookie = buildCookie(name, '', 0)
}

export function isDataDomainHost(): boolean {
  if (!isBrowser()) {
    return false
  }

  return window.location.hostname === 'data.openecon.ai' || window.location.hostname === 'data.openecon.io'
}
