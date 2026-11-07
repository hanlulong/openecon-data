import { describe, it, expect, vi } from 'vitest';
import {
  handleStaleChunkError,
  loadRoute,
  STALE_CHUNK_RELOAD_KEY,
  STALE_CHUNK_RELOAD_WINDOW_MS,
} from '../staleChunkRecovery';

const memoryStorage = () => {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
  };
};

const preloadError = () => new Event('vite:preloadError', { cancelable: true });

describe('handleStaleChunkError', () => {
  it('reloads once and suppresses the error for a stale-chunk failure', () => {
    const storage = memoryStorage();
    const reload = vi.fn();
    const event = preloadError();

    expect(handleStaleChunkError(event, { storage, reload, now: () => 1_000_000 })).toBe(true);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(event.defaultPrevented).toBe(true);
    expect(storage.getItem(STALE_CHUNK_RELOAD_KEY)).toBe('1000000');
  });

  it('does not loop: a second failure right after the reload propagates', () => {
    const storage = memoryStorage();
    const reload = vi.fn();
    handleStaleChunkError(preloadError(), { storage, reload, now: () => 1_000_000 });

    const again = preloadError();
    expect(handleStaleChunkError(again, { storage, reload, now: () => 1_005_000 })).toBe(false);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(again.defaultPrevented).toBe(false); // error reaches the ErrorBoundary
  });

  it('recovers again after a later deploy (guard is time-windowed)', () => {
    const storage = memoryStorage();
    const reload = vi.fn();
    handleStaleChunkError(preloadError(), { storage, reload, now: () => 1_000_000 });
    const later = 1_000_000 + STALE_CHUNK_RELOAD_WINDOW_MS + 1;
    expect(handleStaleChunkError(preloadError(), { storage, reload, now: () => later })).toBe(true);
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it('never auto-reloads when storage is unavailable (no loop guard)', () => {
    const reload = vi.fn();
    expect(handleStaleChunkError(preloadError(), { storage: null, reload, now: () => 1 })).toBe(false);

    const throwing = {
      getItem: () => { throw new Error('blocked'); },
      setItem: () => { throw new Error('blocked'); },
    };
    expect(handleStaleChunkError(preloadError(), { storage: throwing, reload, now: () => 1 })).toBe(false);
    expect(reload).not.toHaveBeenCalled();
  });
});

// One stale route fires an event per failed file (CSS deps, then the module).
// After this page has started a reload, the siblings must be suppressed too,
// or the second event reaches the ErrorBoundary while the reload is landing.
describe('handleStaleChunkError within one page lifetime', () => {
  it('suppresses sibling failures once a reload is pending, without reloading again', () => {
    const storage = memoryStorage();
    const reload = vi.fn();
    const state = { reloadPending: false };

    const cssFailure = preloadError();
    expect(handleStaleChunkError(cssFailure, { storage, reload, now: () => 1_000_000, state })).toBe(true);
    expect(state.reloadPending).toBe(true);

    const moduleFailure = preloadError();
    expect(handleStaleChunkError(moduleFailure, { storage, reload, now: () => 1_000_050, state })).toBe(true);
    expect(moduleFailure.defaultPrevented).toBe(true);
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it('a fresh page (new state) inside the window still lets the failure surface', () => {
    const storage = memoryStorage();
    const reload = vi.fn();
    handleStaleChunkError(preloadError(), { storage, reload, now: () => 1_000_000, state: { reloadPending: false } });

    const afterReload = preloadError();
    const freshState = { reloadPending: false };
    expect(handleStaleChunkError(afterReload, { storage, reload, now: () => 1_005_000, state: freshState })).toBe(false);
    expect(afterReload.defaultPrevented).toBe(false);
    expect(freshState.reloadPending).toBe(false);
  });
});

describe('loadRoute', () => {
  it('maps the named export to a default export', async () => {
    const Page = () => null;
    await expect(loadRoute(async () => ({ Page }), (m) => m.Page, () => false)).resolves.toEqual({ default: Page });
  });

  it('stays pending (no ErrorBoundary) when the import was suppressed for a reload', async () => {
    const pending = loadRoute(async () => undefined as unknown as { Page: () => null }, (m) => m.Page, () => true);
    const outcome = await Promise.race([
      pending.then(() => 'settled', () => 'rejected'),
      new Promise((resolve) => setTimeout(() => resolve('pending'), 20)),
    ]);
    expect(outcome).toBe('pending');
  });

  it('rejects an undefined module when no reload is pending', async () => {
    await expect(
      loadRoute(async () => undefined as unknown as { Page: () => null }, (m) => m.Page, () => false),
    ).rejects.toThrow('Route module failed to load');
  });

  it('passes real import failures through unchanged', async () => {
    const failure = new Error('Failed to fetch dynamically imported module');
    await expect(loadRoute(async () => { throw failure; }, () => null, () => true)).rejects.toBe(failure);
  });
});
