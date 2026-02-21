import { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import { api, tokenManager, setLogoutCallback } from '../services/api';
import { User, ApiError } from '../types';
import { supabase, getSession, setSessionId, signOut as supabaseSignOut } from '../lib/supabase';
import { AxiosError } from 'axios';
import { logger } from '../utils/logger';
import { requestCrossDomainBridgePayload } from '../lib/authBridge';

interface AuthContextType {
  user: User | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (email: string, password: string) => Promise<{ success: boolean; error?: string }>;
  register: (name: string, email: string, password: string) => Promise<{ success: boolean; error?: string }>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export const useAuth = () => {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
};

export const AuthProvider = ({ children }: { children: ReactNode }) => {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  // Register logout callback for 401 interceptor
  useEffect(() => {
    setLogoutCallback(() => {
      setUser(null);
    });
  }, []);

  // Check if user is already logged in on mount
  useEffect(() => {
    const setUserFromSupabaseSession = (session: any): boolean => {
      if (!session?.user) {
        return false;
      }

      // Store Supabase JWT token for backend API calls
      if (session.access_token) {
        tokenManager.setToken(session.access_token);
      }

      // Extract name from user metadata (Google OAuth stores it here)
      const name = session.user.user_metadata?.name ||
                   session.user.user_metadata?.full_name ||
                   session.user.email?.split('@')[0] ||
                   'User';

      setUser({
        id: session.user.id,
        email: session.user.email || '',
        name: name,
        createdAt: session.user.created_at,
      });

      return true;
    };

    const setUserFromApiToken = async (): Promise<boolean> => {
      const token = tokenManager.getToken();
      if (!token) {
        return false;
      }

      try {
        const userData = await api.getMe();
        setUser(userData);
        return true;
      } catch (error: unknown) {
        // Token is invalid or expired
        logger.error('Failed to fetch user data:', error);
        tokenManager.removeToken();
        setUser(null);
        return false;
      }
    };

    const checkAuth = async () => {
      // First check for Supabase session (Google OAuth)
      const session = await getSession();
      if (setUserFromSupabaseSession(session)) {
        setIsLoading(false);
        return;
      }

      // Fallback to legacy backend auth (JWT token)
      if (await setUserFromApiToken()) {
        setIsLoading(false);
        return;
      }

      // One-time migration bridge from old origin to data.openecon.ai
      const bridgePayload = await requestCrossDomainBridgePayload();
      if (bridgePayload) {
        if (bridgePayload.token) {
          tokenManager.setToken(bridgePayload.token);
        }
        if (bridgePayload.anonSessionId) {
          setSessionId(bridgePayload.anonSessionId);
        }

        if (bridgePayload.supabaseAccessToken && bridgePayload.supabaseRefreshToken) {
          const { error: bridgeSessionError } = await supabase.auth.setSession({
            access_token: bridgePayload.supabaseAccessToken,
            refresh_token: bridgePayload.supabaseRefreshToken,
          });

          if (bridgeSessionError) {
            logger.warn('Bridge Supabase session setup failed:', bridgeSessionError);
          }
        }

        const bridgedSession = await getSession();
        if (setUserFromSupabaseSession(bridgedSession)) {
          setIsLoading(false);
          return;
        }

        await setUserFromApiToken();
      }

      setIsLoading(false);
    };

    checkAuth();

    // Listen for Supabase auth state changes
    const { data: { subscription } } = supabase.auth.onAuthStateChange(async (event: string, session: any) => {
      logger.log('Supabase auth state changed:', event);

      // Handle sign-in, token refresh, and user updates
      if ((event === 'SIGNED_IN' || event === 'TOKEN_REFRESHED' || event === 'USER_UPDATED') && session?.user) {
        // Store Supabase JWT token for backend API calls
        if (session.access_token) {
          tokenManager.setToken(session.access_token);
        }

        const name = session.user.user_metadata?.name ||
                     session.user.user_metadata?.full_name ||
                     session.user.email?.split('@')[0] ||
                     'User';

        setUser({
          id: session.user.id,
          email: session.user.email || '',
          name: name,
          createdAt: session.user.created_at,
        });
      } else if (event === 'SIGNED_OUT') {
        tokenManager.removeToken();
        setUser(null);
      }
    });

    return () => {
      subscription.unsubscribe();
    };
  }, []);

  const login = async (email: string, password: string) => {
    try {
      const response = await api.login({ email, password });
      if (response.success && response.user) {
        setUser(response.user);
        return { success: true };
      }
      return { success: false, error: response.error || 'Login failed' };
    } catch (error: unknown) {
      const axiosError = error as AxiosError<ApiError>;
      return {
        success: false,
        error: axiosError.response?.data?.error || axiosError.message || 'Login failed',
      };
    }
  };

  const register = async (name: string, email: string, password: string) => {
    try {
      const response = await api.register({ name, email, password });
      if (response.success && response.user) {
        setUser(response.user);
        return { success: true };
      }
      return { success: false, error: response.error || 'Registration failed' };
    } catch (error: unknown) {
      const axiosError = error as AxiosError<ApiError>;
      return {
        success: false,
        error: axiosError.response?.data?.error || axiosError.message || 'Registration failed',
      };
    }
  };

  const logout = async () => {
    // Sign out from Supabase
    try {
      await supabaseSignOut();
    } catch (error: unknown) {
      logger.error('Supabase sign out error:', error);
    }

    // Sign out from legacy backend
    api.logout();
    setUser(null);
  };

  return (
    <AuthContext.Provider
      value={{
        user,
        isAuthenticated: !!user,
        isLoading,
        login,
        register,
        logout,
      }}
    >
      {children}
    </AuthContext.Provider>
  );
};
