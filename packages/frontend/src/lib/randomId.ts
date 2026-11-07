/**
 * RFC 4122 version-4 UUID that works outside secure contexts.
 *
 * `crypto.randomUUID` exists only in secure contexts (HTTPS / localhost) and
 * Safari >= 15.4, so calling it directly throws on a plain-http LAN dev host
 * or an older iPhone — and ChatPage mints ids during render, so that throw
 * would take the whole route down. `crypto.getRandomValues` is available in
 * every context, so build the same v4 layout from it when needed.
 */

type CryptoLike = Pick<Crypto, 'getRandomValues'> & { randomUUID?: () => string }

export function randomUUID(source: CryptoLike | undefined = globalThis.crypto): string {
  if (source && typeof source.randomUUID === 'function') {
    return source.randomUUID()
  }
  if (!source || typeof source.getRandomValues !== 'function') {
    throw new Error('No cryptographic random source available')
  }
  const bytes = source.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6] & 0x0f) | 0x40 // version 4
  bytes[8] = (bytes[8] & 0x3f) | 0x80 // RFC 4122 variant
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}
