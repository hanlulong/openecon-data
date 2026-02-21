/**
 * Tests for useMobile hook.
 *
 * Tests viewport breakpoint detection for mobile, tablet, and desktop.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useMobile } from '../useMobile';

// Store original matchMedia
const originalMatchMedia = window.matchMedia;

describe('useMobile', () => {
  let listeners: Map<string, (() => void)[]>;

  // Helper to create mock matchMedia
  const createMatchMedia = (matches: { mobile: boolean; tablet: boolean; desktop: boolean }) => {
    listeners = new Map();

    return (query: string) => {
      let currentMatches = false;

      if (query.includes('max-width: 767px')) {
        currentMatches = matches.mobile;
      } else if (query.includes('768px') && query.includes('1023px')) {
        currentMatches = matches.tablet;
      } else if (query.includes('min-width: 1024px')) {
        currentMatches = matches.desktop;
      }

      return {
        matches: currentMatches,
        media: query,
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn((event: string, callback: () => void) => {
          if (!listeners.has(query)) {
            listeners.set(query, []);
          }
          listeners.get(query)!.push(callback);
        }),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      };
    };
  };

  beforeEach(() => {
    listeners = new Map();
  });

  afterEach(() => {
    window.matchMedia = originalMatchMedia;
  });

  it('returns isMobile: true for mobile viewport', () => {
    window.matchMedia = createMatchMedia({ mobile: true, tablet: false, desktop: false }) as any;

    const { result } = renderHook(() => useMobile());

    expect(result.current.isMobile).toBe(true);
    expect(result.current.isTablet).toBe(false);
    expect(result.current.isDesktop).toBe(false);
  });

  it('returns isTablet: true for tablet viewport', () => {
    window.matchMedia = createMatchMedia({ mobile: false, tablet: true, desktop: false }) as any;

    const { result } = renderHook(() => useMobile());

    expect(result.current.isMobile).toBe(false);
    expect(result.current.isTablet).toBe(true);
    expect(result.current.isDesktop).toBe(false);
  });

  it('returns isDesktop: true for desktop viewport', () => {
    window.matchMedia = createMatchMedia({ mobile: false, tablet: false, desktop: true }) as any;

    const { result } = renderHook(() => useMobile());

    expect(result.current.isMobile).toBe(false);
    expect(result.current.isTablet).toBe(false);
    expect(result.current.isDesktop).toBe(true);
  });

  it('defaults to desktop when matchMedia is not available', () => {
    // The hook initializes with desktop: true by default
    window.matchMedia = createMatchMedia({ mobile: false, tablet: false, desktop: true }) as any;

    const { result } = renderHook(() => useMobile());

    expect(result.current.isDesktop).toBe(true);
  });

  it('registers event listeners for breakpoint changes', () => {
    const mockMatchMedia = createMatchMedia({ mobile: false, tablet: false, desktop: true });
    window.matchMedia = mockMatchMedia as any;

    renderHook(() => useMobile());

    // Should have registered listeners for all three breakpoints
    expect(listeners.size).toBeGreaterThan(0);
  });

  it('cleans up event listeners on unmount', () => {
    let removeEventListenerCalls = 0;

    window.matchMedia = (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(() => {
        removeEventListenerCalls++;
      }),
      dispatchEvent: vi.fn(),
    }) as any;

    const { unmount } = renderHook(() => useMobile());
    unmount();

    // Should have called removeEventListener for each media query
    expect(removeEventListenerCalls).toBe(3);
  });
});
