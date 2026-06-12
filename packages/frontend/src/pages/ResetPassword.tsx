import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../services/api';
import { extractApiErrorMessage } from '../lib/errors';

// Password policy enforced by the backend, mirrored client-side for a faster,
// inline error before the request. Must match the registration form (Auth.tsx):
// at least 12 characters with an uppercase letter, a lowercase letter, and a
// digit. A reset must never allow a weaker password than register.
const MIN_PASSWORD_LENGTH = 12;

type Status = 'loading' | 'error' | 'form' | 'success' | 'missing';

export function ResetPassword() {
  const navigate = useNavigate();
  const [status, setStatus] = useState<Status>('loading');
  // The Supabase recovery token extracted from the URL hash fragment.
  const [accessToken, setAccessToken] = useState<string | null>(null);
  // Top-level error: either a hash/link error (status === 'error') or a
  // submit error returned by the backend (status === 'form').
  const [error, setError] = useState<string | null>(null);
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  // Inline client-side validation message for the password fields.
  const [validationError, setValidationError] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  // Handle for the post-success redirect timer so it can be cancelled if the
  // user navigates away before it fires.
  const redirectTimerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (redirectTimerRef.current !== null) {
        window.clearTimeout(redirectTimerRef.current);
      }
    };
  }, []);

  // Password policy checks — kept in sync with the registration form (Auth.tsx).
  const passwordRequirements = {
    minLength: password.length >= MIN_PASSWORD_LENGTH,
    hasUppercase: /[A-Z]/.test(password),
    hasLowercase: /[a-z]/.test(password),
    hasDigit: /[0-9]/.test(password),
  };

  const isPasswordValid =
    passwordRequirements.minLength &&
    passwordRequirements.hasUppercase &&
    passwordRequirements.hasLowercase &&
    passwordRequirements.hasDigit;

  // On mount, parse the recovery token (or error) out of the URL hash fragment
  // that Supabase appends after verifying the email link, then immediately
  // strip it from the address bar so the token isn't left in history.
  useEffect(() => {
    const hash = window.location.hash.startsWith('#')
      ? window.location.hash.substring(1)
      : window.location.hash;

    const params = new URLSearchParams(hash);
    const errorParam = params.get('error');
    const errorDescription = params.get('error_description');
    const errorCode = params.get('error_code');
    const token = params.get('access_token');
    const type = params.get('type');

    // Clear the token/error from the URL so it isn't left in the address bar
    // or browser history. Done before any early return so it always runs.
    if (window.location.hash) {
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
    }

    if (errorParam) {
      const detailedError = errorDescription
        ? decodeURIComponent(errorDescription.replace(/\+/g, ' '))
        : errorParam;
      setError(`${detailedError}${errorCode ? ` (${errorCode})` : ''}`);
      setStatus('error');
      return;
    }

    if (token && type === 'recovery') {
      setAccessToken(token);
      setStatus('form');
      return;
    }

    // No token and no error — the user likely visited the page directly.
    setStatus('missing');
  }, []);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setValidationError(null);
    setError(null);

    if (!isPasswordValid) {
      setValidationError(
        `Password must be at least ${MIN_PASSWORD_LENGTH} characters and include an uppercase letter, a lowercase letter, and a digit.`
      );
      return;
    }

    if (password !== confirmPassword) {
      setValidationError('Passwords do not match.');
      return;
    }

    if (!accessToken) {
      setError('This reset link is invalid or has expired. Request a new one.');
      setStatus('error');
      return;
    }

    setIsSubmitting(true);
    try {
      await api.resetPassword(accessToken, password);
      setStatus('success');
      // Briefly show the success message, then send the user to the login
      // modal on the chat page.
      redirectTimerRef.current = window.setTimeout(() => {
        navigate('/chat?auth=1');
      }, 2000);
    } catch (err: unknown) {
      // The backend returns HTTP 400 with { success: false, error } on failure;
      // surface that message and keep the form usable.
      setError(extractApiErrorMessage(err, 'Could not reset your password. Please try again.'));
    } finally {
      setIsSubmitting(false);
    }
  };

  // Loading (parsing hash) — show a spinner card, mirroring AuthCallback.
  if (status === 'loading') {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center px-4">
        <div className="max-w-md w-full bg-white rounded-lg shadow-lg p-6">
          <div className="text-center">
            <div className="mx-auto flex items-center justify-center h-12 w-12 rounded-full bg-blue-100">
              <div className="h-6 w-6 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
            </div>
            <h3 className="mt-4 text-lg font-medium text-gray-900">Loading...</h3>
          </div>
        </div>
      </div>
    );
  }

  // Hash contained an error (expired/invalid link).
  if (status === 'error') {
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
            <h3 className="mt-4 text-lg font-medium text-gray-900">Reset link problem</h3>
            <p className="mt-2 text-sm text-gray-500">
              {error || 'This reset link is invalid or has expired. Request a new one.'}
            </p>
            <div className="mt-6">
              <button
                onClick={() => navigate('/chat?auth=1')}
                className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
              >
                Request a new reset link
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Direct visit with no token/error in the hash.
  if (status === 'missing') {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center px-4">
        <div className="max-w-md w-full bg-white rounded-lg shadow-lg p-6">
          <div className="text-center">
            <h3 className="mt-2 text-lg font-medium text-gray-900">Reset your password</h3>
            <p className="mt-2 text-sm text-gray-500">
              This page is for resetting your password from an email link.
            </p>
            <div className="mt-6">
              <button
                onClick={() => navigate('/chat?auth=1')}
                className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
              >
                Back to login
              </button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // Password updated successfully.
  if (status === 'success') {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center px-4">
        <div className="max-w-md w-full bg-white rounded-lg shadow-lg p-6">
          <div className="text-center">
            <div className="mx-auto flex items-center justify-center h-12 w-12 rounded-full bg-green-100">
              <svg
                className="h-6 w-6 text-green-600"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M5 13l4 4L19 7"
                />
              </svg>
            </div>
            <h3 className="mt-4 text-lg font-medium text-gray-900">Password updated!</h3>
            <p className="mt-2 text-sm text-gray-500">
              You can now log in. Redirecting you to login...
            </p>
          </div>
        </div>
      </div>
    );
  }

  // status === 'form' — set a new password.
  return (
    <div className="min-h-screen bg-gray-50 flex items-center justify-center px-4">
      <div className="max-w-md w-full bg-white rounded-lg shadow-lg p-6">
        <h3 className="text-lg font-medium text-gray-900 text-center">Set a new password</h3>
        <p className="mt-2 text-sm text-gray-500 text-center">
          Choose a new password for your account.
        </p>

        <form onSubmit={handleSubmit} className="mt-6 space-y-4">
          {error && (
            <div className="px-3 py-2 rounded-md bg-red-50 border border-red-200 text-sm text-red-700">
              {error}
            </div>
          )}

          <div>
            <label htmlFor="new-password" className="block text-sm font-medium text-gray-700">
              New password
            </label>
            <input
              id="new-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={MIN_PASSWORD_LENGTH}
              autoComplete="new-password"
              placeholder={`At least ${MIN_PASSWORD_LENGTH} characters`}
              className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
            {password && (
              <div className="mt-2 space-y-1 text-xs">
                <div className={passwordRequirements.minLength ? 'text-green-600' : 'text-gray-500'}>
                  {passwordRequirements.minLength ? '✓' : '○'} At least {MIN_PASSWORD_LENGTH} characters
                </div>
                <div className={passwordRequirements.hasUppercase ? 'text-green-600' : 'text-gray-500'}>
                  {passwordRequirements.hasUppercase ? '✓' : '○'} One uppercase letter
                </div>
                <div className={passwordRequirements.hasLowercase ? 'text-green-600' : 'text-gray-500'}>
                  {passwordRequirements.hasLowercase ? '✓' : '○'} One lowercase letter
                </div>
                <div className={passwordRequirements.hasDigit ? 'text-green-600' : 'text-gray-500'}>
                  {passwordRequirements.hasDigit ? '✓' : '○'} One digit
                </div>
              </div>
            )}
          </div>

          <div>
            <label htmlFor="confirm-password" className="block text-sm font-medium text-gray-700">
              Confirm new password
            </label>
            <input
              id="confirm-password"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
              minLength={MIN_PASSWORD_LENGTH}
              autoComplete="new-password"
              placeholder="Re-enter your new password"
              className="mt-1 block w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            />
          </div>

          {validationError && (
            <p className="text-sm text-red-600">{validationError}</p>
          )}

          <button
            type="submit"
            disabled={isSubmitting || !isPasswordValid}
            className="w-full px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {isSubmitting ? 'Updating...' : 'Update password'}
          </button>

          <div className="text-center">
            <button
              type="button"
              onClick={() => navigate('/chat?auth=1')}
              className="text-sm text-blue-600 hover:text-blue-700 underline"
            >
              Back to login
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
