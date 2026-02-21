/**
 * Tests for ChatPage component.
 *
 * Tests the main chat interface including query submission,
 * message display, pro mode toggle, and history sidebar.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// Create hoisted mocks for all dependencies
const mocks = vi.hoisted(() => {
  const mockUnsubscribe = vi.fn();
  return {
    mockUnsubscribe,
    supabase: {
      auth: {
        onAuthStateChange: vi.fn(() => ({
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
    },
    mockUseAuth: vi.fn(() => ({
      user: null,
      isAuthenticated: false,
      logout: vi.fn(),
    })),
    mockUseMobile: vi.fn(() => ({ isMobile: false })),
    // API mocks - hoisted for proper access in tests
    mockApi: {
      getUserHistory: vi.fn().mockResolvedValue({ history: [], total: 0 }),
      getSessionHistory: vi.fn().mockResolvedValue({ history: [], total: 0 }),
      clearUserHistory: vi.fn().mockResolvedValue({ success: true }),
      queryStream: vi.fn(),
      exportData: vi.fn().mockResolvedValue(new Blob(['test'])),
      queryPro: vi.fn().mockResolvedValue({}),
    },
  };
});

// Mock all dependencies before importing ChatPage
const mockNavigate = vi.fn();
const mockLocation = { search: '', pathname: '/chat' };

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
    useLocation: () => mockLocation,
  };
});

vi.mock('../../services/api', () => ({
  api: mocks.mockApi,
}));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: mocks.mockUseAuth,
}));

vi.mock('../../hooks/useMobile', () => ({
  useMobile: mocks.mockUseMobile,
}));

vi.mock('../../lib/supabase', () => ({
  supabase: mocks.supabase,
  trackAnonymousSession: vi.fn().mockResolvedValue(undefined),
  getOrCreateSessionId: vi.fn(() => 'test-session-id'),
  isSupabaseAvailable: false,
}));

vi.mock('../../utils/logger', () => ({
  logger: {
    log: vi.fn(),
    error: vi.fn(),
    warn: vi.fn(),
  },
}));

vi.mock('../../lib/export', () => ({
  downloadExport: vi.fn(),
}));

// Mock child components that are complex
vi.mock('../MessageChart', () => ({
  MessageChart: ({ data, chartType, onChartTypeChange, onExport, onShare }: any) => (
    <div data-testid="message-chart">
      <span data-testid="chart-type">{chartType}</span>
      <span data-testid="data-count">{data?.length || 0}</span>
      <button onClick={() => onChartTypeChange?.('bar')}>Change to Bar</button>
      <button onClick={() => onExport?.('csv')}>Export CSV</button>
      {onShare && <button onClick={onShare}>Share</button>}
    </div>
  ),
}));

vi.mock('../CodeExecutionDisplay', () => ({
  CodeExecutionDisplay: ({ codeExecution }: any) => (
    <div data-testid="code-execution">{codeExecution?.code || ''}</div>
  ),
}));

vi.mock('../ProcessingSteps', () => ({
  ProcessingSteps: ({ steps }: any) => (
    <div data-testid="processing-steps">
      {(steps || []).map((s: any, i: number) => (
        <span key={i}>{s.step}</span>
      ))}
    </div>
  ),
  ProcessingTimelineStep: {},
}));

vi.mock('../Auth', () => ({
  Auth: ({ onClose }: any) => (
    <div data-testid="auth-modal">
      <button onClick={onClose}>Close Auth</button>
    </div>
  ),
}));

vi.mock('../ShareModal', () => ({
  ShareModal: ({ isOpen, onClose }: any) =>
    isOpen ? (
      <div data-testid="share-modal">
        <button onClick={onClose}>Close Share</button>
      </div>
    ) : null,
}));

vi.mock('../FeedbackModal', () => ({
  FeedbackModal: ({ isOpen, onClose }: any) =>
    isOpen ? (
      <div data-testid="feedback-modal">
        <button onClick={onClose}>Close Feedback</button>
      </div>
    ) : null,
}));

import { ChatPage } from '../ChatPage';

// ============================================================================
// Test Utilities
// ============================================================================

const createTestQueryClient = () =>
  new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  });

const renderChatPage = () => {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <MemoryRouter>
        <ChatPage />
      </MemoryRouter>
    </QueryClientProvider>
  );
};

// ============================================================================
// Tests
// ============================================================================

describe('ChatPage', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Reset hoisted mocks to default values
    mocks.mockUseAuth.mockReturnValue({
      user: null,
      isAuthenticated: false,
      logout: vi.fn(),
    });
    mocks.mockUseMobile.mockReturnValue({ isMobile: false });
    mocks.supabase.auth.onAuthStateChange.mockReturnValue({
      data: {
        subscription: {
          unsubscribe: mocks.mockUnsubscribe,
        },
      },
    });
    // Reset API mocks
    mocks.mockApi.getUserHistory.mockResolvedValue({ history: [], total: 0 });
    mocks.mockApi.getSessionHistory.mockResolvedValue({ history: [], total: 0 });
  });

  // Note: afterEach cleanup is handled by @testing-library/react's cleanup()
  // which is automatically called. vi.clearAllMocks() in beforeEach is sufficient.

  describe('rendering', () => {
    it('renders welcome screen when no messages', async () => {
      renderChatPage();

      const welcomeText = await screen.findByText(/What can I help with/i);
      expect(welcomeText).toBeInTheDocument();
    });

    it('renders query input field', async () => {
      renderChatPage();

      const input = await screen.findByPlaceholderText(/Ask about economic data/i);
      expect(input).toBeInTheDocument();
    });

    it('renders sidebar with New chat button', async () => {
      renderChatPage();

      const newChatButton = await screen.findByText(/New chat/i);
      expect(newChatButton).toBeInTheDocument();
    });

    it('renders Pro Mode toggle in sidebar', async () => {
      renderChatPage();

      const proModeToggle = await screen.findByText(/Pro Mode/i);
      expect(proModeToggle).toBeInTheDocument();
    });

    it('renders Feedback button', async () => {
      renderChatPage();

      const feedbackButton = await screen.findByText(/Feedback/i);
      expect(feedbackButton).toBeInTheDocument();
    });
  });

  describe('query submission', () => {
    it('adds user message when form is submitted', async () => {
      const user = userEvent.setup();
      renderChatPage();

      const input = await screen.findByPlaceholderText(/Ask about economic data/i);
      await user.type(input, 'Show me US GDP');

      // Submit the form directly instead of clicking button
      const form = input.closest('form');
      expect(form).not.toBeNull();
      fireEvent.submit(form!);

      await waitFor(() => {
        expect(screen.getByText('Show me US GDP')).toBeInTheDocument();
      });
    });

    it('does not submit empty query', async () => {
      renderChatPage();

      const input = await screen.findByPlaceholderText(/Ask about economic data/i);

      // Try to submit without entering text
      const form = input.closest('form');
      expect(form).not.toBeNull();
      fireEvent.submit(form!);

      // Should not call queryStream for empty query
      expect(mocks.mockApi.queryStream).not.toHaveBeenCalled();
    });

    it('clears input after submission', async () => {
      const user = userEvent.setup();
      renderChatPage();

      const input = await screen.findByPlaceholderText(/Ask about economic data/i);
      await user.type(input, 'Test query');

      const form = input.closest('form');
      expect(form).not.toBeNull();
      fireEvent.submit(form!);

      await waitFor(() => {
        expect(input).toHaveValue('');
      });
    });
  });

  describe('Pro Mode', () => {
    it('toggles Pro Mode when button is clicked', async () => {
      const user = userEvent.setup();
      renderChatPage();

      const proModeText = await screen.findByText(/Pro Mode/i);
      const proModeButton = proModeText.closest('button');
      expect(proModeButton).not.toBeNull();

      // Initially OFF
      expect(screen.getByText('OFF')).toBeInTheDocument();

      // Click to toggle
      await user.click(proModeButton!);

      // Should now be ON
      await waitFor(() => {
        expect(screen.getByText('ON')).toBeInTheDocument();
      });
    });

    it('shows different placeholder when Pro Mode is enabled', async () => {
      const user = userEvent.setup();
      renderChatPage();

      const proModeText = await screen.findByText(/Pro Mode/i);
      const proModeButton = proModeText.closest('button');
      expect(proModeButton).not.toBeNull();
      await user.click(proModeButton!);

      await waitFor(() => {
        expect(screen.getByPlaceholderText(/custom analysis/i)).toBeInTheDocument();
      });
    });
  });

  describe('authentication', () => {
    it('shows login button when not authenticated', async () => {
      renderChatPage();

      const loginButton = await screen.findByText(/Login \/ Register/i);
      expect(loginButton).toBeInTheDocument();
    });

    it('shows user profile when authenticated', async () => {
      mocks.mockUseAuth.mockReturnValue({
        user: { name: 'Test User', email: 'test@example.com' },
        isAuthenticated: true,
        logout: vi.fn(),
      });

      renderChatPage();

      await waitFor(() => {
        expect(screen.getByText('Test User')).toBeInTheDocument();
        expect(screen.getByText('test@example.com')).toBeInTheDocument();
      });
    });

    it('opens auth modal when login button is clicked', async () => {
      const user = userEvent.setup();
      renderChatPage();

      const loginButton = await screen.findByText(/Login \/ Register/i);
      await user.click(loginButton);

      const authModal = await screen.findByTestId('auth-modal');
      expect(authModal).toBeInTheDocument();
    });
  });

  describe('history', () => {
    it('loads user history when authenticated', async () => {
      mocks.mockUseAuth.mockReturnValue({
        user: { name: 'Test User', email: 'test@example.com' },
        isAuthenticated: true,
        logout: vi.fn(),
      });

      mocks.mockApi.getUserHistory.mockResolvedValue({
        history: [
          { id: '1', query: 'US GDP', timestamp: '2024-01-01', conversationId: 'conv-1' },
        ],
        total: 1,
      });

      renderChatPage();

      await waitFor(() => {
        expect(mocks.mockApi.getUserHistory).toHaveBeenCalled();
      });
    });

    it('loads session history when not authenticated', async () => {
      renderChatPage();

      await waitFor(() => {
        expect(mocks.mockApi.getSessionHistory).toHaveBeenCalled();
      });
    });

    it('displays history items', async () => {
      mocks.mockApi.getSessionHistory.mockResolvedValue({
        history: [
          { id: '1', query: 'Show me US GDP', timestamp: '2024-01-01', conversationId: 'conv-1' },
        ],
        total: 1,
      });

      await act(async () => {
        renderChatPage();
      });

      // Wait for API to be called first
      await waitFor(() => {
        expect(mocks.mockApi.getSessionHistory).toHaveBeenCalled();
      });

      // Then wait for the history item to appear
      const historyItem = await screen.findByText('Show me US GDP', {}, { timeout: 3000 });
      expect(historyItem).toBeInTheDocument();
    });
  });

  describe('new chat', () => {
    it('clears messages when New chat is clicked', async () => {
      const user = userEvent.setup();

      // First, submit a query to have messages
      renderChatPage();

      const input = await screen.findByPlaceholderText(/Ask about economic data/i);
      await user.type(input, 'Test query');

      const form = input.closest('form');
      expect(form).not.toBeNull();
      fireEvent.submit(form!);

      await waitFor(() => {
        expect(screen.getByText('Test query')).toBeInTheDocument();
      });

      // Click New chat
      const newChatButton = await screen.findByText(/New chat/i);
      await user.click(newChatButton);

      await waitFor(() => {
        expect(screen.getByText(/What can I help with/i)).toBeInTheDocument();
      });
    });
  });

  describe('search', () => {
    it('filters history when search query is entered', async () => {
      const user = userEvent.setup();

      mocks.mockApi.getSessionHistory.mockResolvedValue({
        history: [
          { id: '1', query: 'US GDP data', timestamp: '2024-01-01', conversationId: 'conv-1' },
          { id: '2', query: 'China exports', timestamp: '2024-01-02', conversationId: 'conv-2' },
        ],
        total: 2,
      });

      await act(async () => {
        renderChatPage();
      });

      // Wait for API call
      await waitFor(() => {
        expect(mocks.mockApi.getSessionHistory).toHaveBeenCalled();
      });

      // Wait for history items to appear
      const gdpItem = await screen.findByText('US GDP data', {}, { timeout: 3000 });
      expect(gdpItem).toBeInTheDocument();
      expect(screen.getByText('China exports')).toBeInTheDocument();

      const searchInput = screen.getByPlaceholderText(/Search chats/i);
      await user.type(searchInput, 'GDP');

      await waitFor(() => {
        expect(screen.getByText('US GDP data')).toBeInTheDocument();
        expect(screen.queryByText('China exports')).not.toBeInTheDocument();
      });
    });
  });

  describe('mobile view', () => {
    it('shows mobile header on mobile devices', async () => {
      mocks.mockUseMobile.mockReturnValue({ isMobile: true });

      renderChatPage();

      // Mobile header should have hamburger button
      const hamburgerButton = await screen.findByLabelText(/Toggle sidebar/i);
      expect(hamburgerButton).toBeInTheDocument();
    });

    it('toggles sidebar visibility on hamburger click', async () => {
      const user = userEvent.setup();
      mocks.mockUseMobile.mockReturnValue({ isMobile: true });

      renderChatPage();

      const hamburgerButton = await screen.findByLabelText(/Toggle sidebar/i);
      await user.click(hamburgerButton);

      // Verify sidebar is now visible by checking if elements inside are accessible
      // This tests behavior rather than implementation (CSS class names)
      const sidebar = document.querySelector('.chat-sidebar');
      expect(sidebar).not.toBeNull();
      expect(sidebar?.classList.contains('open')).toBe(true);
    });
  });

  describe('feedback modal', () => {
    it('opens feedback modal when Feedback button is clicked', async () => {
      const user = userEvent.setup();
      renderChatPage();

      const feedbackText = await screen.findByText(/Feedback/i);
      const feedbackButton = feedbackText.closest('button');
      expect(feedbackButton).not.toBeNull();
      await user.click(feedbackButton!);

      const feedbackModal = await screen.findByTestId('feedback-modal');
      expect(feedbackModal).toBeInTheDocument();
    });
  });

  describe('example queries', () => {
    it('displays example queries when no history', async () => {
      renderChatPage();

      const exampleHeader = await screen.findByText(/Example Queries/i);
      expect(exampleHeader).toBeInTheDocument();
    });

    it('sets input value when example query is clicked', async () => {
      const user = userEvent.setup();
      renderChatPage();

      const exampleButton = await screen.findByText(/US unemployment rate/i);
      await user.click(exampleButton);

      const input = screen.getByPlaceholderText(/Ask about economic data/i) as HTMLInputElement;
      expect(input.value).toContain('US unemployment');
    });
  });
});
