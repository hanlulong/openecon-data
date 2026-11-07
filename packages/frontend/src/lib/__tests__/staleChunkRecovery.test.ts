import { describe, it, expect, vi } from 'vitest';
import {
  handleStaleChunkError,
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
