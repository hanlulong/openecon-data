/**
 * API mocks for testing.
 *
 * Provides mock implementations of the API service for testing components
 * that depend on API calls.
 */
import { vi } from 'vitest';
import { mockQueryResponse, mockUser, mockNormalizedData } from '../test-utils';
import type { AuthResponse, QueryResponse, User, UserQueryHistory } from '../../types';

// ============================================================================
// Mock Token Manager
// ============================================================================

export const mockTokenManager = {
  getToken: vi.fn(() => null),
  setToken: vi.fn(),
  removeToken: vi.fn(),
};

// ============================================================================
// Mock API Responses
// ============================================================================

export const mockAuthResponse: AuthResponse = {
  success: true,
  user: mockUser,
  token: 'mock-jwt-token',
};

export const mockHistoryResponse = {
  history: [
    {
      id: 'query-1',
      query: 'Show me US GDP',
      timestamp: '2024-01-01T00:00:00.000Z',
      response: mockQueryResponse,
    },
    {
      id: 'query-2',
      query: 'What is the unemployment rate?',
      timestamp: '2024-01-02T00:00:00.000Z',
      response: mockQueryResponse,
    },
  ] as UserQueryHistory[],
  total: 2,
};

export const mockHealthResponse = {
  status: 'ok',
  timestamp: '2024-01-01T00:00:00.000Z',
  environment: 'test',
  services: {
    openrouter: true,
    fred: true,
    alphaVantage: false,
    bls: false,
    comtrade: true,
  },
  cache: {
    keys: 0,
    hits: 0,
    misses: 0,
  },
};

export const mockCacheStatsResponse = {
  keys: 10,
  hits: 100,
  misses: 20,
  hitRate: 0.83,
};

// ============================================================================
// Mock API Functions
// ============================================================================

export const createMockApi = () => ({
  register: vi.fn().mockResolvedValue(mockAuthResponse),
  login: vi.fn().mockResolvedValue(mockAuthResponse),
  getMe: vi.fn().mockResolvedValue(mockUser),
  logout: vi.fn(),
  getUserHistory: vi.fn().mockResolvedValue(mockHistoryResponse),
  clearUserHistory: vi.fn().mockResolvedValue({ success: true, message: 'History cleared', deleted: 2 }),
  getSessionHistory: vi.fn().mockResolvedValue(mockHistoryResponse),
  query: vi.fn().mockResolvedValue(mockQueryResponse),
  queryPro: vi.fn().mockResolvedValue({
    ...mockQueryResponse,
    isProMode: true,
    codeExecution: {
      code: 'import pandas as pd\nprint("Hello")',
      output: 'Hello',
      executionTime: 1.5,
    },
  }),
  queryStream: vi.fn().mockImplementation(
    async (
      query: string,
      conversationId: string | undefined,
      proMode: boolean,
      callbacks: {
        onStep?: (step: any) => void;
        onData?: (data: QueryResponse) => void;
        onError?: (error: string) => void;
        onDone?: () => void;
      }
    ) => {
      // Simulate streaming steps
      if (callbacks.onStep) {
        callbacks.onStep({ step: 'Parsing query', status: 'complete', duration: 100 });
        callbacks.onStep({ step: 'Fetching data', status: 'complete', duration: 200 });
      }
      // Send final data
      if (callbacks.onData) {
        callbacks.onData(mockQueryResponse);
      }
      // Signal completion
      if (callbacks.onDone) {
        callbacks.onDone();
      }
    }
  ),
  exportData: vi.fn().mockResolvedValue(new Blob(['test,data'], { type: 'text/csv' })),
  getHealth: vi.fn().mockResolvedValue(mockHealthResponse),
  getCacheStats: vi.fn().mockResolvedValue(mockCacheStatsResponse),
  submitFeedback: vi.fn().mockResolvedValue({ success: true, message: 'Feedback received' }),
});

// Default mock API instance
export const mockApi = createMockApi();

// ============================================================================
// Mock Axios
// ============================================================================

export const createMockAxios = () => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
  defaults: {
    timeout: 30000,
  },
  interceptors: {
    request: {
      use: vi.fn(),
    },
    response: {
      use: vi.fn(),
    },
  },
});

export const mockAxios = createMockAxios();

// ============================================================================
// Mock Streaming Response
// ============================================================================

/**
 * Creates a mock ReadableStream for testing SSE streaming.
 */
export const createMockStream = (events: Array<{ type: string; data: any }>) => {
  const encoder = new TextEncoder();
  let index = 0;

  return new ReadableStream({
    pull(controller) {
      if (index < events.length) {
        const event = events[index];
        const eventString = `event: ${event.type}\ndata: ${JSON.stringify(event.data)}\n\n`;
        controller.enqueue(encoder.encode(eventString));
        index++;
      } else {
        controller.close();
      }
    },
  });
};

/**
 * Mock fetch for streaming tests.
 */
export const createMockFetch = (events: Array<{ type: string; data: any }>) =>
  vi.fn().mockResolvedValue({
    ok: true,
    body: createMockStream(events),
  });

// ============================================================================
// Setup Helpers
// ============================================================================

/**
 * Resets all mock functions to their initial state.
 */
export const resetApiMocks = () => {
  Object.values(mockApi).forEach((fn) => {
    if (typeof fn === 'function' && 'mockReset' in fn) {
      fn.mockReset();
    }
  });
  Object.values(mockTokenManager).forEach((fn) => fn.mockReset());
};

/**
 * Setup mock for a failed API call.
 */
export const mockApiError = (methodName: keyof typeof mockApi, errorMessage: string, status = 400) => {
  const error = new Error(errorMessage) as any;
  error.response = {
    status,
    data: { error: errorMessage },
  };
  (mockApi[methodName] as any).mockRejectedValue(error);
};
