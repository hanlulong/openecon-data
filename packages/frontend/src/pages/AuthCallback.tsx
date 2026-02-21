import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { supabase, getOrCreateSessionId, clearSessionId } from '../lib/supabase';
import { tokenManager } from '../services/api';

export function AuthCallback() {
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const handleCallback = async () => {
      try {
        // First, check for errors in query parameters (Supabase returns errors here)
        const queryParams = new URLSearchParams(window.location.search);
        const errorParam = queryParams.get('error');
        const errorDescription = queryParams.get('error_description');
        const errorCode = queryParams.get('error_code');

        if (errorParam) {
          // Supabase returned an error before generating tokens
          const detailedError = errorDescription
            ? decodeURIComponent(errorDescription.replace(/\+/g, ' '))
            : errorParam;
          throw new Error(`Authentication failed: ${detailedError}${errorCode ? ` (${errorCode})` : ''}`);
        }

        // Get the hash fragment from the URL (Supabase OAuth returns tokens in hash)
        const hashParams = new URLSearchParams(window.location.hash.substring(1));
        const accessToken = hashParams.get('access_token');
        const refreshToken = hashParams.get('refresh_token');

        if (!accessToken) {
          throw new Error('No access token found in callback URL. Please try signing in again.');
        }

        // Set the session with the tokens
        const { data: sessionData, error: sessionError } = await supabase.auth.setSession({
          access_token: accessToken,
          refresh_token: refreshToken || '',
        });

        if (sessionError) {
          throw sessionError;
        }

        if (!sessionData.user) {
          throw new Error('No user data returned after setting session');
        }

        // Store the access token for backend API calls
        tokenManager.setToken(accessToken);

        // Get the anonymous session ID if it exists
        const anonymousSessionId = getOrCreateSessionId();

        // Convert anonymous session to authenticated user in backend
        // This will transfer all anonymous queries to the authenticated user
        try {
          const { error: convertError } = await supabase
            .rpc('convert_session_to_user', {
              p_session_id: anonymousSessionId,
              p_user_id: sessionData.user.id,
            });

          if (convertError) {
            console.warn('Failed to convert anonymous session:', convertError);
            // Continue anyway - this is not critical
          } else {
            console.log('Successfully converted anonymous session to user');
          }
        } catch (conversionError) {
          console.warn('Error during session conversion:', conversionError);
          // Continue anyway - this is not critical
        }

        // Clear the anonymous session ID since user is now authenticated
        clearSessionId();

        // Redirect to chat page
        navigate('/chat');
      } catch (error) {
        console.error('OAuth callback error:', error);
        setError(error instanceof Error ? error.message : 'Authentication failed');
      }
    };

    handleCallback();
  }, [navigate]);

  if (error) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center px-4">
        <div className="max-w-md w-full bg-white rounded-lg shadow-lg p-6">
          <div className="text-center">
            <div className="mx-auto flex items-center justify-center h-12 w-12 rounded-full bg-red-100">
              <svg
                className="h-6 w-6 text-red-600"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M6 18L18 6M6 6l12 12"
                />
              </svg>
            </div>
            <h3 className="mt-4 text-lg font-medium text-gray-900">
              Authentication Failed
            </h3>
            <p className="mt-2 text-sm text-gray-500">{error}</p>
            <div className="mt-6">
              <button
                onClick={() => navigate('/auth')}
                className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
              >
                Try Again
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center px-4">
      <div className="max-w-md w-full bg-white rounded-lg shadow-lg p-6">
        <div className="text-center">
          <div className="mx-auto flex items-center justify-center h-12 w-12 rounded-full bg-blue-100">
            <div className="h-6 w-6 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
          </div>
          <h3 className="mt-4 text-lg font-medium text-gray-900">
            Completing sign in...
          </h3>
          <p className="mt-2 text-sm text-gray-500">
            Please wait while we finish setting up your account.
          </p>
        </div>
      </div>
    </div>
  );
}
