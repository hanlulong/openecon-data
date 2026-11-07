import { describe, it, expect } from 'vitest'
import { randomUUID } from '../randomId'

const V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

describe('randomUUID', () => {
  it('uses crypto.randomUUID when the context provides it', () => {
    const source = {
      randomUUID: () => '11111111-2222-4333-8444-555555555555',
      getRandomValues: <T extends ArrayBufferView | null>(array: T) => array,
    }
    expect(randomUUID(source as unknown as Crypto)).toBe('11111111-2222-4333-8444-555555555555')
  })

  // Non-secure contexts (plain-http LAN dev) and Safari < 15.4 have no
  // crypto.randomUUID; minting an id there must not throw.
  it('falls back to a v4 UUID built from getRandomValues', () => {
    const source = { getRandomValues: globalThis.crypto.getRandomValues.bind(globalThis.crypto) }
    const ids = new Set(Array.from({ length: 50 }, () => randomUUID(source)))
    expect(ids.size).toBe(50)
    for (const id of ids) expect(id).toMatch(V4)
  })

  it('sets the version and variant bits even from all-ones bytes', () => {
    const source = {
      getRandomValues: <T extends ArrayBufferView | null>(array: T) => {
        if (array instanceof Uint8Array) array.fill(0xff)
        return array
      },
    }
    expect(randomUUID(source as unknown as Crypto)).toBe('ffffffff-ffff-4fff-bfff-ffffffffffff')
  })

  it('returns a v4 UUID in the default (test) environment', () => {
    expect(randomUUID()).toMatch(V4)
  })
})
