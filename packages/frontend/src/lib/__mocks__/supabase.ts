/**
 * Mock implementation of supabase module for testing.
 */
import { vi } from 'vitest';

const mockUnsubscribe = vi.fn();

// The supabase client mock
export const supabase = {
  auth: {
    onAuthStateChange: vi.fn((_callback: unknown) => ({
      data: {
        subscription: {
          unsubscribe: mockUnsubscribe,
        },
      },
    })),
    getSession: vi.fn(() => Promise.resolve({ data: { session: null } })),
    signOut: vi.fn(() => Promise.resolve({ error: null })),
    getUser: vi.fn(() => Promise.resolve({ data: { user: null } })),
  },
  from: vi.fn(() => ({
    select: vi.fn().mockReturnThis(),
    insert: vi.fn().mockReturnThis(),
    update: vi.fn().mockReturnThis(),
    delete: vi.fn().mockReturnThis(),
    upsert: vi.fn().mockReturnThis(),
    eq: vi.fn().mockReturnThis(),
    order: vi.fn().mockReturnThis(),
    limit: vi.fn().mockReturnThis(),
    execute: vi.fn(() => Promise.resolve({ data: null, error: null })),
  })),
};

// Exported functions that match the actual module
export const getSession = vi.fn(() => Promise.resolve(null));
export const signOut = vi.fn(() => Promise.resolve());
export const trackAnonymousSession = vi.fn(() => Promise.resolve());
export const getOrCreateSessionId = vi.fn(() => 'test-session-id');
export const isSupabaseAvailable = false;
export const signInWithGoogle = vi.fn();
export const signInWithPassword = vi.fn();
export const signUpWithPassword = vi.fn();
export const getCurrentUser = vi.fn(() => Promise.resolve(null));
export const clearSessionId = vi.fn();
export const onAuthStateChange = vi.fn((_callback: unknown) => ({
  data: {
    subscription: {
      unsubscribe: mockUnsubscribe,
    },
  },
}));
export const logQuery = vi.fn(() => Promise.resolve());
export const getUserHistory = vi.fn(() => Promise.resolve([]));
