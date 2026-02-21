/**
 * Test utilities for econ-data-mcp frontend tests.
 *
 * Provides custom render functions that wrap components with necessary providers,
 * mock data fixtures, and helper functions for testing.
 */
import React, { ReactElement, ReactNode } from 'react';
import { render, RenderOptions, RenderResult } from '@testing-library/react';
import { MemoryRouter, MemoryRouterProps } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { vi } from 'vitest';

// ============================================================================
// Mock Auth Context
// ============================================================================

interface MockAuthContextType {
  user: { id: string; email: string; name: string } | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: () => Promise<{ success: boolean; error?: string }>;
  register: () => Promise<{ success: boolean; error?: string }>;
  logout: () => Promise<void>;
}

const defaultMockAuthValue: MockAuthContextType = {
  user: null,
  isAuthenticated: false,
  isLoading: false,
  login: vi.fn().mockResolvedValue({ success: true }),
  register: vi.fn().mockResolvedValue({ success: true }),
  logout: vi.fn().mockResolvedValue(undefined),
};

export const MockAuthContext = React.createContext<MockAuthContextType>(defaultMockAuthValue);

export const MockAuthProvider = ({
  children,
  value = defaultMockAuthValue,
}: {
  children: ReactNode;
  value?: Partial<MockAuthContextType>;
}) => {
  const mergedValue = { ...defaultMockAuthValue, ...value };
  return (
    <MockAuthContext.Provider value={mergedValue}>
      {children}
    </MockAuthContext.Provider>
  );
};

// ============================================================================
// Query Client for Tests
// ============================================================================

export const createTestQueryClient = () =>
  new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: 0,
        staleTime: 0,
      },
      mutations: {
        retry: false,
      },
    },
  });

// ============================================================================
// Custom Render with Providers
// ============================================================================

interface CustomRenderOptions extends Omit<RenderOptions, 'wrapper'> {
  authValue?: Partial<MockAuthContextType>;
  routerProps?: MemoryRouterProps;
  queryClient?: QueryClient;
}

export const renderWithProviders = (
  ui: ReactElement,
  options: CustomRenderOptions = {}
): RenderResult => {
  const {
    authValue,
    routerProps = { initialEntries: ['/'] },
    queryClient = createTestQueryClient(),
    ...renderOptions
  } = options;

  const Wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <MockAuthProvider value={authValue}>
        <MemoryRouter {...routerProps}>{children}</MemoryRouter>
      </MockAuthProvider>
    </QueryClientProvider>
  );

  return render(ui, { wrapper: Wrapper, ...renderOptions });
};

// ============================================================================
// Mock Data Fixtures
// ============================================================================

export const mockUser = {
  id: 'test-user-123',
  email: 'test@example.com',
  name: 'Test User',
  createdAt: '2024-01-01T00:00:00.000Z',
};

export const mockAuthenticatedContext: Partial<MockAuthContextType> = {
  user: mockUser,
  isAuthenticated: true,
  isLoading: false,
};

export const mockNormalizedData = {
  metadata: {
    source: 'FRED',
    indicator: 'Gross Domestic Product',
    country: 'US',
    frequency: 'quarterly',
    unit: 'Billions of Dollars',
    lastUpdated: '2024-01-01T00:00:00.000Z',
    seriesId: 'GDP',
    seasonalAdjustment: 'Seasonally Adjusted Annual Rate',
    dataType: 'Level',
    description: 'Gross Domestic Product',
  },
  data: [
    { date: '2023-01-01', value: 26000 },
    { date: '2023-04-01', value: 26500 },
    { date: '2023-07-01', value: 27000 },
    { date: '2023-10-01', value: 27500 },
  ],
};

export const mockQueryResponse = {
  conversationId: 'test-conversation-123',
  intent: {
    apiProvider: 'FRED',
    indicators: ['GDP'],
    parameters: { country: 'US' },
    clarificationNeeded: false,
    confidence: 0.95,
    recommendedChartType: 'line' as const,
    originalQuery: 'Show me US GDP',
    needsDecomposition: false,
    useProMode: false,
  },
  data: [mockNormalizedData],
  clarificationNeeded: false,
};

export const mockMessage = {
  role: 'assistant' as const,
  content: 'Here is the US GDP data:',
  timestamp: new Date('2024-01-01T00:00:00.000Z'),
  data: [mockNormalizedData],
  chartType: 'line' as const,
};

export const mockProcessingSteps = [
  { step: 'Parsing query', status: 'complete', duration: 150 },
  { step: 'Searching metadata', status: 'complete', duration: 200 },
  { step: 'Fetching data from FRED', status: 'complete', duration: 500 },
  { step: 'Normalizing data', status: 'complete', duration: 50 },
];

export const mockCodeExecutionResult = {
  code: 'import pandas as pd\nprint("Hello, World!")',
  output: 'Hello, World!',
  executionTime: 1.5,
  files: [],
};

// ============================================================================
// Helper Functions
// ============================================================================

/**
 * Wait for async operations to complete in tests.
 */
export const waitForAsync = () =>
  new Promise((resolve) => setTimeout(resolve, 0));

/**
 * Create a mock function that resolves after a delay.
 */
export const createDelayedMock = <T>(value: T, delay = 100) =>
  vi.fn().mockImplementation(
    () => new Promise((resolve) => setTimeout(() => resolve(value), delay))
  );

/**
 * Mock localStorage for tests.
 */
export const mockLocalStorage = () => {
  const store: Record<string, string> = {};
  return {
    getItem: vi.fn((key: string) => store[key] || null),
    setItem: vi.fn((key: string, value: string) => {
      store[key] = value;
    }),
    removeItem: vi.fn((key: string) => {
      delete store[key];
    }),
    clear: vi.fn(() => {
      Object.keys(store).forEach((key) => delete store[key]);
    }),
  };
};

// ============================================================================
// Re-exports from Testing Library
// ============================================================================

export * from '@testing-library/react';
export { default as userEvent } from '@testing-library/user-event';
