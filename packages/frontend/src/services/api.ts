import axios, { AxiosError } from 'axios';
import { QueryResponse, NormalizedData, AuthResponse, RegisterRequest, LoginRequest, User, UserQueryHistory, HealthResponse, CacheStatsResponse, ApiError, FeedbackRequest, FeedbackResponse, StreamProcessingStepEvent, ForgotPasswordResponse, ResetPasswordResponse } from '../types';
import { getOrCreateSessionId } from '../lib/supabase';
import { getCookie, removeSharedCookie, setSharedCookie } from '../lib/sharedStorage';

const API_BASE_URL = import.meta.env.VITE_API_URL || '/api';
const DEFAULT_TIMEOUT = 30000; // 30 seconds

// Configure axios defaults
axios.defaults.timeout = DEFAULT_TIMEOUT;

// Token management
const TOKEN_KEY = 'openecon_auth_token';
const AUTH_COOKIE_TTL_SECONDS = 60 * 60 * 24 * 30;

export const tokenManager = {
  getToken(): string | null {
    const localToken = localStorage.getItem(TOKEN_KEY);
    if (localToken) {
      // Keep cross-subdomain cookie warm when localStorage has the token.
      setSharedCookie(TOKEN_KEY, localToken, AUTH_COOKIE_TTL_SECONDS);
      return localToken;
    }

    const cookieToken = getCookie(TOKEN_KEY);
    if (cookieToken) {
      localStorage.setItem(TOKEN_KEY, cookieToken);
      return cookieToken;
    }

    return null;
  },

  setToken(token: string): void {
    localStorage.setItem(TOKEN_KEY, token);
    setSharedCookie(TOKEN_KEY, token, AUTH_COOKIE_TTL_SECONDS);
  },

  removeToken(): void {
    localStorage.removeItem(TOKEN_KEY);
    removeSharedCookie(TOKEN_KEY);
  },
};

// Logout callback for 401 errors (to be set by AuthContext)
let logoutCallback: (() => void) | null = null;
let isLoggingOut = false; // Prevent multiple logout calls from parallel 401 responses

export const setLogoutCallback = (callback: () => void) => {
  logoutCallback = callback;
  isLoggingOut = false; // Reset flag when callback is set (e.g., on login)
};

// Add axios interceptor to include auth token in all requests
axios.interceptors.request.use((config) => {
  const token = tokenManager.getToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Add axios response interceptor for 401 errors (auto logout)
axios.interceptors.response.use(
  (response) => {
    // Successful response — clear the isLoggingOut flag so a subsequent
    // 401 (e.g. after re-login on the same page) can trigger logout again.
    // Without this, a logout/login cycle on the same page leaves the flag
    // stuck and silently masks future expirations.
    if (isLoggingOut) {
      isLoggingOut = false;
    }
    return response;
  },
  (error: AxiosError<ApiError>) => {
    // Handle 401 Unauthorized - token expired or invalid
    // Use isLoggingOut flag to prevent multiple logout calls from parallel requests
    // Skip login/register endpoints: a failed login attempt returns 401 but must
    // not clear the tokens of an already-authenticated session.
    const requestUrl = error.config?.url ?? '';
    const isAuthAttempt =
      requestUrl.includes('/auth/login') || requestUrl.includes('/auth/register');
    if (error.response?.status === 401 && !isLoggingOut && !isAuthAttempt) {
      isLoggingOut = true;
      console.warn('401 Unauthorized - clearing auth state');
      tokenManager.removeToken();

      // Call logout callback if available (to update UI state)
      if (logoutCallback) {
        logoutCallback();
      }
    }

    return Promise.reject(error);
  }
);

export const api = {
  // Auth methods
  async register(data: RegisterRequest): Promise<AuthResponse> {
    // Always include the anonymous session id so the backend can migrate the
    // user's pre-signup history into their new account. Callers may override it.
    const payload: RegisterRequest = {
      ...data,
      sessionId: data.sessionId ?? getOrCreateSessionId(),
    };
    const response = await axios.post<AuthResponse>(`${API_BASE_URL}/auth/register`, payload);
    if (response.data.token) {
      tokenManager.setToken(response.data.token);
    }
    return response.data;
  },

  async login(data: LoginRequest): Promise<AuthResponse> {
    const response = await axios.post<AuthResponse>(`${API_BASE_URL}/auth/login`, data);
    if (response.data.token) {
      tokenManager.setToken(response.data.token);
    }
    return response.data;
  },

  async getMe(): Promise<User> {
    const response = await axios.get<User>(`${API_BASE_URL}/auth/me`);
    return response.data;
  },

  /**
   * Request a password reset link. The backend always responds with
   * { success: true } — even for unknown emails — so the caller must never
   * reveal whether an account exists.
   */
  async forgotPassword(email: string): Promise<ForgotPasswordResponse> {
    const response = await axios.post<ForgotPasswordResponse>(
      `${API_BASE_URL}/auth/forgot-password`,
      { email }
    );
    return response.data;
  },

  /**
   * Complete a password reset using the recovery token from the email link.
   * On success returns { success: true }; on failure the backend returns
   * HTTP 400 with { success: false, error } (e.g. expired link, weak password).
   */
  async resetPassword(accessToken: string, password: string): Promise<ResetPasswordResponse> {
    const response = await axios.post<ResetPasswordResponse>(
      `${API_BASE_URL}/auth/reset-password`,
      { accessToken, password }
    );
    return response.data;
  },

  logout(): void {
    tokenManager.removeToken();
  },

  async getUserHistory(limit?: number): Promise<{ history: UserQueryHistory[]; total: number }> {
    const params = limit ? { limit } : {};
    const response = await axios.get(`${API_BASE_URL}/user/history`, { params });
    return response.data;
  },

  async clearUserHistory(): Promise<{ success: boolean; message: string; deleted: number }> {
    const response = await axios.delete(`${API_BASE_URL}/user/history`);
    return response.data;
  },

  async getSessionHistory(sessionId: string, limit?: number): Promise<{ history: UserQueryHistory[]; total: number }> {
    // Session id travels in a header (not a query param) so it never lands in
    // server access logs or browser history — IDOR hardening on the backend.
    const params = limit ? { limit } : {};
    const response = await axios.get(`${API_BASE_URL}/session/history`, {
      params,
      headers: { 'X-Session-ID': sessionId },
    });
    return response.data;
  },

  /**
   * Execute a non-streaming query (simpler, faster for quick requests)
   * @param query The query string
   * @param conversationId Optional conversation ID for follow-ups
   * @param sessionId Optional session ID for anonymous tracking
   */
  async query(
    query: string,
    conversationId?: string,
    sessionId?: string
  ): Promise<QueryResponse> {
    const effectiveSessionId = sessionId || await getOrCreateSessionId();
    const response = await axios.post<QueryResponse>(`${API_BASE_URL}/query`, {
      query,
      conversationId,
      sessionId: effectiveSessionId,
    });
    return response.data;
  },

  /**
   * Stream query with real-time progress updates.
   * Pro Mode is auto-detected by the backend, so there is no proMode flag.
   * @param query The query string
   * @param conversationId Optional conversation ID
   * @param callbacks Callbacks for different event types
   * @param abortSignal Optional AbortSignal for request cancellation
   */
  async queryStream(
    query: string,
    conversationId: string | undefined,
    callbacks: {
      onStep?: (step: StreamProcessingStepEvent) => void;
      onData?: (data: QueryResponse) => void;
      onError?: (error: { error: string; message: string }) => void;
      onDone?: (conversationId: string) => void;
      // Fires whenever bytes arrive on the stream — including SSE keepalive
      // comment frames (": ...") that carry no event. Lets the caller reset an
      // inactivity watchdog without treating keepalives as data events.
      onActivity?: () => void;
    },
    abortSignal?: AbortSignal
  ): Promise<void> {
    const token = tokenManager.getToken();
    const headers: HeadersInit = {
      'Content-Type': 'application/json',
    };
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }

    // Always use standard endpoint — Pro Mode is auto-detected by backend
    const endpoint = `${API_BASE_URL}/query/stream`;

    // Get session ID for anonymous users
    const sessionId = !token ? getOrCreateSessionId() : undefined;

    const response = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify({ query, conversationId, sessionId }),
      signal: abortSignal,
    });

    if (!response.ok) {
      // Mirror lib/errors.ts: read error → detail → message in priority order,
      // for ALL statuses. The 429 rate-limit body is
      // {"detail": "Rate limit exceeded. Limit: 30/minute"}, so without reading
      // `detail` the user only ever saw a bare "HTTP 429".
      let serverMessage = '';
      try {
        const errorData = await response.json();
        serverMessage = errorData?.error || errorData?.detail || errorData?.message || '';
      } catch {
        // Body was not JSON — fall back to the status line below.
      }

      let errorMessage = serverMessage || `HTTP ${response.status}: ${response.statusText}`;

      // Rate limiting deserves an actionable message: the wait is short.
      if (response.status === 429) {
        errorMessage = serverMessage
          ? `${serverMessage} — please wait about a minute and try again.`
          : 'Rate limit reached — please wait about a minute and try again.';
      }

      throw new Error(errorMessage);
    }

    if (!response.body) {
      throw new Error('Response body is null');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    // Parse and dispatch a single SSE event block. Extracted so we can also
    // drain the trailing buffer on stream end — without this, an event sent
    // without a final "\n\n" (server flush right before close) was silently
    // dropped, leaving the UI stuck in "loading" forever.
    const dispatchEvent = (eventText: string): void => {
      if (!eventText.trim()) return;

      const lines = eventText.split('\n');
      let eventType = 'message';
      // Per the SSE spec, multiple "data:" lines in one event concatenate with
      // "\n". Accumulate instead of overwriting; single-line events behave
      // exactly as before.
      const dataLines: string[] = [];

      for (const line of lines) {
        if (line.startsWith('event:')) {
          eventType = line.substring(6).trim();
        } else if (line.startsWith('data:')) {
          dataLines.push(line.substring(5).trim());
        }
      }

      const eventData = dataLines.join('\n');
      if (!eventData) return;

      try {
        const data = JSON.parse(eventData);

        switch (eventType) {
          case 'step':
            callbacks.onStep?.(data);
            break;
          case 'data':
            callbacks.onData?.(data);
            break;
          case 'error':
            callbacks.onError?.(data);
            break;
          case 'done':
            // Only invoke onDone with a real conversation ID — the prior code
            // forwarded undefined when the server's done payload was empty,
            // poisoning the client-side conversation cache.
            if (typeof data.conversationId === 'string' && data.conversationId) {
              callbacks.onDone?.(data.conversationId);
            } else {
              console.warn('SSE done event missing conversationId; ignoring');
            }
            break;
        }
      } catch (e) {
        console.error('Failed to parse event data:', e, eventData);
      }
    };

    try {
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        // Bytes arrived — including SSE keepalive comment frames that carry no
        // event — so the connection is alive. Signal the caller before parsing
        // so its inactivity watchdog resets even on comment-only keepalives.
        callbacks.onActivity?.();

        // Decode chunk and add to buffer
        buffer += decoder.decode(value, { stream: true });

        // Process complete events (separated by \n\n)
        const events = buffer.split('\n\n');
        buffer = events.pop() || ''; // Keep incomplete event in buffer

        for (const eventText of events) {
          dispatchEvent(eventText);
        }
      }

      // Stream closed. Flush any remaining event in the buffer — final
      // events that arrive without a trailing "\n\n" must still fire.
      if (buffer.trim()) {
        dispatchEvent(buffer);
        buffer = '';
      }
    } finally {
      reader.releaseLock();
    }
  },

  async exportData(data: NormalizedData[], format: 'csv' | 'json' | 'dta'): Promise<Blob> {
    const response = await axios.post(`${API_BASE_URL}/export`, {
      data,
      format,
    }, {
      responseType: 'blob',
    });
    return response.data;
  },

  async getHealth(): Promise<HealthResponse> {
    const response = await axios.get<HealthResponse>(`${API_BASE_URL}/health`);
    return response.data;
  },

  async getCacheStats(): Promise<CacheStatsResponse> {
    const response = await axios.get<CacheStatsResponse>(`${API_BASE_URL}/cache/stats`);
    return response.data;
  },

  async submitFeedback(feedback: FeedbackRequest): Promise<FeedbackResponse> {
    const response = await axios.post<FeedbackResponse>(`${API_BASE_URL}/feedback`, feedback);
    return response.data;
  },

  /**
   * Pro Mode query (non-streaming) - generates and executes Python code
   * @param query The query string for code generation
   * @param conversationId Optional conversation ID for context
   * @returns QueryResponse with code execution results
   */
  async queryPro(query: string, conversationId?: string): Promise<QueryResponse> {
    const token = tokenManager.getToken();
    const sessionId = !token ? getOrCreateSessionId() : undefined;

    const response = await axios.post<QueryResponse>(`${API_BASE_URL}/query/pro`, {
      query,
      conversationId,
      sessionId,
    });
    return response.data;
  },
};
