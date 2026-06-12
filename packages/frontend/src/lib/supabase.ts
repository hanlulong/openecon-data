/**
 * Supabase client for frontend authentication and database operations
 *
 * Handles graceful degradation when Supabase is not configured (development mode).
 * In development without Supabase, most features fall back gracefully.
 */
import { createClient, Session, SupabaseClient, User as SupabaseUser } from '@supabase/supabase-js'
import { getCookie, removeSharedCookie, setSharedCookie } from './sharedStorage'

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

// Check if Supabase is available
export const isSupabaseAvailable = !!(supabaseUrl && supabaseAnonKey)

const createMockSupabaseClient = () => ({
  auth: {
    signInWithOAuth: async () => ({
      data: null,
      error: new Error('Supabase not configured in development mode')
    }),
    signInWithPassword: async () => ({
      data: null,
      error: new Error('Supabase not configured in development mode')
    }),
    signUp: async () => ({
      data: null,
      error: new Error('Supabase not configured in development mode')
    }),
    signOut: async () => ({
      error: null
    }),
    getSession: async () => ({
      data: { session: null as Session | null }
    }),
    setSession: async () => ({
      data: { session: null as Session | null, user: null as SupabaseUser | null },
      error: new Error('Supabase not configured')
    }),
    getUser: async () => ({
      data: { user: null as SupabaseUser | null }
    }),
    onAuthStateChange: (_callback: (event: string, session: Session | null) => void) => ({
      data: { subscription: { unsubscribe: () => {} } }
    })
  },
  from: (_table: string) => ({
    select: () => ({
      eq: () => ({
        order: () => ({
          limit: async () => ({
            data: null,
            error: null
          })
        }),
        execute: async () => ({
          data: null,
          error: null
        })
      }),
      execute: async () => ({
        data: null,
        error: null
      })
    }),
    insert: async () => ({
      data: null,
      error: null
    }),
    update: () => ({
      eq: async () => ({
        data: null,
        error: null
      }),
      execute: async () => ({
        data: null,
        error: null
      })
    }),
    delete: () => ({
      eq: async () => ({
        data: null,
        error: null
      }),
      execute: async () => ({
        data: null,
        error: null
      })
    }),
    upsert: async () => ({
      data: null,
      error: null
    })
  }),
  rpc: async () => ({ data: null, error: null })
})

type MockSupabaseClient = ReturnType<typeof createMockSupabaseClient>

// Create client only if both URL and key are provided
// Otherwise, use a no-op mock client
let supabaseClient: SupabaseClient | MockSupabaseClient

if (isSupabaseAvailable) {
  supabaseClient = createClient(supabaseUrl, supabaseAnonKey, {
    auth: {
      autoRefreshToken: true,
      persistSession: true,
      detectSessionInUrl: true
    }
  })
} else {
  // Mock client for development without Supabase
  console.info('ℹ️ Supabase not configured - using development mode without persistent storage')

  supabaseClient = createMockSupabaseClient()
}

export const supabase = supabaseClient

const SESSION_ID_KEY = 'anon_session_id'
const SESSION_COOKIE_TTL_SECONDS = 60 * 60 * 24 * 30

/**
 * Sign in with Google OAuth
 */
export async function signInWithGoogle() {
  if (!isSupabaseAvailable) {
    throw new Error('Google sign-in is not available in development mode without Supabase')
  }

  const { data, error } = await supabase.auth.signInWithOAuth({
    provider: 'google',
    options: {
      redirectTo: `${window.location.origin}/auth/callback`,
      queryParams: {
        access_type: 'offline',
        prompt: 'consent',
      }
    }
  })

  if (error) {
    console.error('Google sign-in error:', error)
    throw error
  }

  return data
}

/**
 * Sign in with email and password
 */
export async function signInWithPassword(email: string, password: string) {
  const { data, error } = await supabase.auth.signInWithPassword({
    email,
    password,
  })

  if (error) {
    throw error
  }

  return data
}

/**
 * Sign up with email and password
 */
export async function signUpWithPassword(email: string, password: string, name: string) {
  const { data, error } = await supabase.auth.signUp({
    email,
    password,
    options: {
      data: {
        name,
      }
    }
  })

  if (error) {
    throw error
  }

  return data
}

/**
 * Sign out
 */
export async function signOut() {
  const { error } = await supabase.auth.signOut()
  if (error) {
    throw error
  }
}

/**
 * Get current session
 */
export async function getSession(): Promise<Session | null> {
  const { data: { session } } = await supabase.auth.getSession()
  return session
}

/**
 * Get current user
 */
export async function getCurrentUser(): Promise<SupabaseUser | null> {
  const { data: { user } } = await supabase.auth.getUser()
  return user
}

/**
 * Get or create anonymous session ID
 */
export function getOrCreateSessionId(): string {
  let sessionId = localStorage.getItem(SESSION_ID_KEY)
  if (!sessionId) {
    sessionId = getCookie(SESSION_ID_KEY)
    if (sessionId) {
      localStorage.setItem(SESSION_ID_KEY, sessionId)
    }
  } else {
    setSharedCookie(SESSION_ID_KEY, sessionId, SESSION_COOKIE_TTL_SECONDS)
  }

  if (!sessionId) {
    sessionId = crypto.randomUUID()
    localStorage.setItem(SESSION_ID_KEY, sessionId)
    setSharedCookie(SESSION_ID_KEY, sessionId, SESSION_COOKIE_TTL_SECONDS)
  }
  return sessionId
}

/**
 * Read the existing anonymous session ID without creating one.
 *
 * Unlike getOrCreateSessionId(), this never mints a new ID — callers that
 * only want to migrate pre-existing history (e.g. the OAuth callback) must
 * not create a fresh, empty session as a side effect.
 */
export function getExistingSessionId(): string | null {
  const localId = localStorage.getItem(SESSION_ID_KEY)
  if (localId) {
    return localId
  }
  return getCookie(SESSION_ID_KEY)
}

/**
 * Set anonymous session ID explicitly (used by cross-domain bridge migration).
 */
export function setSessionId(sessionId: string) {
  localStorage.setItem(SESSION_ID_KEY, sessionId)
  setSharedCookie(SESSION_ID_KEY, sessionId, SESSION_COOKIE_TTL_SECONDS)
}

/**
 * Clear anonymous session ID (call after user signs in)
 */
export function clearSessionId() {
  localStorage.removeItem(SESSION_ID_KEY)
  removeSharedCookie(SESSION_ID_KEY)
}

/**
 * Listen to auth state changes
 */
export function onAuthStateChange(callback: (event: string, session: Session | null) => void) {
  return supabase.auth.onAuthStateChange((event: string, session: Session | null) => {
    callback(event, session)
  })
}
