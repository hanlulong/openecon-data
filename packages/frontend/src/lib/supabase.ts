/**
 * Supabase client for frontend authentication and database operations
 *
 * Handles graceful degradation when Supabase is not configured (development mode).
 * In development without Supabase, most features fall back gracefully.
 */
import { createClient, Session, User as SupabaseUser } from '@supabase/supabase-js'
import { getCookie, removeSharedCookie, setSharedCookie } from './sharedStorage'

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY

// Check if Supabase is available
export const isSupabaseAvailable = !!(supabaseUrl && supabaseAnonKey)

// Create client only if both URL and key are provided
// Otherwise, use a no-op mock client
let supabaseClient: any = null

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

  supabaseClient = {
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
        data: { session: null }
      }),
      getUser: async () => ({
        data: { user: null }
      }),
      onAuthStateChange: (_callback: (event: string, session: unknown) => void) => ({
        data: { subscription: { unsubscribe: () => {} } }
      })
    },
    from: (_table: string) => ({
      select: () => ({
        eq: () => ({
          order: () => ({
            limit: () => ({
              execute: async () => ({
                data: null,
                error: null
              })
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
      insert: () => ({
        execute: async () => ({
          data: null,
          error: null
        })
      }),
      update: () => ({
        eq: () => ({
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
      delete: () => ({
        eq: () => ({
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
      upsert: () => ({
        execute: async () => ({
          data: null,
          error: null
        })
      })
    })
  }
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

/**
 * Log query to Supabase (handles both authenticated and anonymous users)
 */
export async function logQuery(data: {
  query: string
  conversationId?: string
  proMode?: boolean
  intent?: any
  responseData?: any
  codeExecution?: any
  errorMessage?: string
  processingTimeMs?: number
}) {
  // Skip logging if Supabase is not available (development mode)
  if (!isSupabaseAvailable) {
    console.debug('ℹ️ Query logging skipped (Supabase not configured)')
    return
  }

  const { data: { user } } = await supabase.auth.getUser()

  const queryData = {
    ...data,
    user_id: user?.id || null,
    session_id: user ? null : getOrCreateSessionId(),
    pro_mode: data.proMode || false,
    conversation_id: data.conversationId,
    intent: data.intent,
    response_data: data.responseData,
    code_execution: data.codeExecution,
    error_message: data.errorMessage,
    processing_time_ms: data.processingTimeMs,
  }

  const { error } = await supabase.from('user_queries').insert([queryData])

  if (error) {
    console.error('Failed to log query:', error)
  }
}

/**
 * Get user's query history
 */
export async function getUserHistory(limit: number = 50) {
  // Return empty history if Supabase is not available (development mode)
  if (!isSupabaseAvailable) {
    console.debug('ℹ️ History not available (Supabase not configured)')
    return []
  }

  const { data: { user } } = await supabase.auth.getUser()

  if (!user) {
    // For anonymous users, get by session_id
    const sessionId = getOrCreateSessionId()
    const { data, error } = await supabase
      .from('user_queries')
      .select('*')
      .eq('session_id', sessionId)
      .order('created_at', { ascending: false })
      .limit(limit)

    if (error) {
      console.error('Failed to get history:', error)
      return []
    }

    return data || []
  }

  // For authenticated users, get by user_id
  const { data, error } = await supabase
    .from('user_queries')
    .select('*')
    .eq('user_id', user.id)
    .order('created_at', { ascending: false })
    .limit(limit)

  if (error) {
    console.error('Failed to get history:', error)
    return []
  }

  return data || []
}

/**
 * Track anonymous session
 */
export async function trackAnonymousSession() {
  // Skip tracking if Supabase is not available (development mode)
  if (!isSupabaseAvailable) {
    console.debug('ℹ️ Session tracking skipped (Supabase not configured)')
    return
  }

  const sessionId = getOrCreateSessionId()

  const { error } = await supabase
    .from('anonymous_sessions')
    .upsert({
      session_id: sessionId,
      user_agent: navigator.userAgent,
      last_seen: new Date().toISOString(),
    }, {
      onConflict: 'session_id'
    })

  if (error) {
    console.error('Failed to track session:', error)
  }
}
