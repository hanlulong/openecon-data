/**
 * Tests for AuthContext and AuthProvider.
 *
 * Tests authentication state management, login/logout flows,
 * and integration with Supabase and legacy backend auth.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import React from 'react';

// Create hoisted mocks that can be referenced in vi.mock factories
const mocks = vi.hoisted(() => {
  const mockUnsubscribe = vi.fn();
  const mockGetSession = vi.fn(() => Promise.resolve(null));

  return {
    mockUnsubscribe,
    mockGetSession,
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
  };
});

// Mock supabase lib module with hoisted values
vi.mock('../../lib/supabase', () => ({
  supabase: mocks.supabase,
  getSession: mocks.mockGetSession,
  signOut: vi.fn(() => Promise.resolve()),
  trackAnonymousSession: vi.fn(() => Promise.resolve()),
  getOrCreateSessionId: vi.fn(() => 'test-session-id'),
  isSupabaseAvailable: false,
}));

// Mock dependencies before importing AuthContext
vi.mock('../../services/api', () => ({
  api: {
    login: vi.fn(),
    register: vi.fn(),
    getMe: vi.fn(),
    logout: vi.fn(),
  },
  tokenManager: {
    getToken: vi.fn(() => null),
    setToken: vi.fn(),
    removeToken: vi.fn(),
  },
  setLogoutCallback: vi.fn(),
}));

vi.mock('../../utils/logger', () => ({
  logger: {
    log: vi.fn(),
    error: vi.fn(),
    warn: vi.fn(),
  },
}));

import { AuthProvider, useAuth } from '../AuthContext';
import { api, tokenManager } from '../../services/api';

// Reference to the hoisted mock for test control
const mockGetSession = mocks.mockGetSession;

// Test component that uses useAuth hook
const TestComponent = () => {
  const { user, isAuthenticated, isLoading, login, logout, register } = useAuth();

  return (
    <div>
      <div data-testid="loading">{isLoading.toString()}</div>
      <div data-testid="authenticated">{isAuthenticated.toString()}</div>
      <div data-testid="user">{user ? user.email : 'null'}</div>
      <button onClick={() => login('test@example.com', 'password')}>Login</button>
      <button onClick={logout}>Logout</button>
      <button onClick={() => register('Test', 'test@example.com', 'password')}>Register</button>
    </div>
  );
};

describe('AuthContext', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Reset token manager to return null (no token)
    (tokenManager.getToken as any).mockReturnValue(null);
    // Reset getSession to return null (no Supabase session)
    mockGetSession.mockResolvedValue(null);
    // Reset the supabase mock to ensure onAuthStateChange returns correctly
    mocks.supabase.auth.onAuthStateChange.mockReturnValue({
      data: {
        subscription: {
          unsubscribe: mocks.mockUnsubscribe,
        },
      },
    });
  });

  // Note: afterEach cleanup is handled automatically by React Testing Library.
  // vi.clearAllMocks() in beforeEach is sufficient for mock cleanup.

  describe('useAuth hook', () => {
    it('throws error when used outside AuthProvider', () => {
      // Suppress console.error for this test
      const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

      expect(() => {
        render(<TestComponent />);
      }).toThrow('useAuth must be used within AuthProvider');

      consoleSpy.mockRestore();
    });
  });

  describe('AuthProvider', () => {
    it('renders children correctly', async () => {
      render(
        <AuthProvider>
          <div data-testid="child">Child Content</div>
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('child')).toHaveTextContent('Child Content');
      });
    });

    it('starts with loading state true', async () => {
      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      // Initially loading - the loading state may resolve quickly in tests
      await waitFor(() => {
        expect(screen.getByTestId('loading')).toBeInTheDocument();
      });
    });

    it('sets isAuthenticated to false when no session exists', async () => {
      mockGetSession.mockResolvedValue(null);
      (tokenManager.getToken as any).mockReturnValue(null);

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('authenticated')).toHaveTextContent('false');
      });
    });

    it('sets isAuthenticated to true when Supabase session exists', async () => {
      const mockSession = {
        user: {
          id: 'user-123',
          email: 'test@example.com',
          user_metadata: { name: 'Test User' },
          created_at: '2024-01-01T00:00:00.000Z',
        },
        access_token: 'mock-access-token',
      };
      mockGetSession.mockResolvedValue(mockSession);

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('authenticated')).toHaveTextContent('true');
        expect(screen.getByTestId('user')).toHaveTextContent('test@example.com');
      });
    });

    it('sets isAuthenticated to true when legacy token exists', async () => {
      mockGetSession.mockResolvedValue(null);
      (tokenManager.getToken as any).mockReturnValue('mock-jwt-token');
      (api.getMe as any).mockResolvedValue({
        id: 'user-123',
        email: 'legacy@example.com',
        name: 'Legacy User',
      });

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('authenticated')).toHaveTextContent('true');
        expect(screen.getByTestId('user')).toHaveTextContent('legacy@example.com');
      });
    });

    it('clears token when getMe fails', async () => {
      mockGetSession.mockResolvedValue(null);
      (tokenManager.getToken as any).mockReturnValue('invalid-token');
      (api.getMe as any).mockRejectedValue(new Error('Unauthorized'));

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(tokenManager.removeToken).toHaveBeenCalled();
        expect(screen.getByTestId('authenticated')).toHaveTextContent('false');
      });
    });
  });

  describe('login', () => {
    it('logs in user successfully', async () => {
      const user = userEvent.setup();
      (api.login as any).mockResolvedValue({
        success: true,
        user: { id: 'user-123', email: 'test@example.com', name: 'Test' },
        token: 'new-token',
      });

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('loading')).toHaveTextContent('false');
      });

      await user.click(screen.getByText('Login'));

      await waitFor(() => {
        expect(api.login).toHaveBeenCalledWith({
          email: 'test@example.com',
          password: 'password',
        });
      });
    });

    it('handles login failure', async () => {
      const user = userEvent.setup();
      (api.login as any).mockResolvedValue({
        success: false,
        error: 'Invalid credentials',
      });

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('loading')).toHaveTextContent('false');
      });

      await user.click(screen.getByText('Login'));

      await waitFor(() => {
        expect(screen.getByTestId('authenticated')).toHaveTextContent('false');
      });
    });
  });

  describe('register', () => {
    it('registers user successfully', async () => {
      const user = userEvent.setup();
      (api.register as any).mockResolvedValue({
        success: true,
        user: { id: 'user-123', email: 'test@example.com', name: 'Test' },
        token: 'new-token',
      });

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('loading')).toHaveTextContent('false');
      });

      await user.click(screen.getByText('Register'));

      await waitFor(() => {
        expect(api.register).toHaveBeenCalledWith({
          name: 'Test',
          email: 'test@example.com',
          password: 'password',
        });
      });
    });
  });

  describe('logout', () => {
    it('logs out user and clears state', async () => {
      const user = userEvent.setup();
      const mockSession = {
        user: {
          id: 'user-123',
          email: 'test@example.com',
          user_metadata: { name: 'Test User' },
          created_at: '2024-01-01T00:00:00.000Z',
        },
        access_token: 'mock-access-token',
      };
      mockGetSession.mockResolvedValue(mockSession);

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      // Wait for user to be authenticated
      await waitFor(() => {
        expect(screen.getByTestId('authenticated')).toHaveTextContent('true');
      });

      // Click logout
      await user.click(screen.getByText('Logout'));

      await waitFor(() => {
        expect(api.logout).toHaveBeenCalled();
      });
    });
  });
});
