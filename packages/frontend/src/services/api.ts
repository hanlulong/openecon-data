import axios, { AxiosError } from 'axios';
import { QueryResponse, NormalizedData, AuthResponse, RegisterRequest, LoginRequest, User, UserQueryHistory, HealthResponse, CacheStatsResponse, ApiError, FeedbackRequest, FeedbackResponse } from '../types';
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
  (response) => response,
  (error: AxiosError<ApiError>) => {
    // Handle 401 Unauthorized - token expired or invalid
    // Use isLoggingOut flag to prevent multiple logout calls from parallel requests
    if (error.response?.status === 401 && !isLoggingOut) {
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
    const response = await axios.post<AuthResponse>(`${API_BASE_URL}/auth/register`, data);
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
    const params = { session_id: sessionId, ...(limit ? { limit } : {}) };
    const response = await axios.get(`${API_BASE_URL}/session/history`, { params });
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
   * Stream query with real-time progress updates
   * @param query The query string
   * @param conversationId Optional conversation ID
   * @param proMode Whether to use Pro Mode
   * @param callbacks Callbacks for different event types
   * @param abortSignal Optional AbortSignal for request cancellation
   */
  async queryStream(
    query: string,
    conversationId: string | undefined,
    proMode: boolean,
    callbacks: {
      onStep?: (step: { step: string; description: string; duration_ms?: number; status?: string; metadata?: any }) => void;
      onData?: (data: QueryResponse) => void;
      onError?: (error: { error: string; message: string }) => void;
      onDone?: (conversationId: string) => void;
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

    // Use Pro Mode streaming endpoint if Pro Mode is enabled
    const endpoint = proMode ? `${API_BASE_URL}/query/pro/stream` : `${API_BASE_URL}/query/stream`;

    // Get session ID for anonymous users
    const sessionId = !token ? getOrCreateSessionId() : undefined;

    const response = await fetch(endpoint, {
      method: 'POST',
      headers,
      body: JSON.stringify({ query, conversationId, sessionId }),
      signal: abortSignal,
    });

    if (!response.ok) {
      // Try to parse error body for detailed message
      let errorMessage = `HTTP ${response.status}: ${response.statusText}`;
      try {
        const errorData = await response.json();
        if (errorData.message || errorData.error) {
          errorMessage = errorData.message || errorData.error;
        }
      } catch {
        // JSON parse failed, use default message
      }
      throw new Error(errorMessage);
    }

    if (!response.body) {
      throw new Error('Response body is null');
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        // Decode chunk and add to buffer
        buffer += decoder.decode(value, { stream: true });

        // Process complete events (separated by \n\n)
        const events = buffer.split('\n\n');
        buffer = events.pop() || ''; // Keep incomplete event in buffer

        for (const eventText of events) {
          if (!eventText.trim()) continue;

          // Parse SSE format: "event: type\ndata: json"
          const lines = eventText.split('\n');
          let eventType = 'message';
          let eventData = '';

          for (const line of lines) {
            if (line.startsWith('event:')) {
              eventType = line.substring(6).trim();
            } else if (line.startsWith('data:')) {
              eventData = line.substring(5).trim();
            }
          }

          if (!eventData) continue;

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
                callbacks.onDone?.(data.conversationId);
                break;
            }
          } catch (e) {
            console.error('Failed to parse event data:', e, eventData);
          }
        }
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
