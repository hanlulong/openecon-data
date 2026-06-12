import { useEffect, useState } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { extractApiErrorMessage } from '../lib/errors';
import { api } from '../services/api';
import { GoogleSignInButton } from './GoogleSignInButton';
import './Auth.css';

interface AuthProps {
  onClose?: () => void;
  initialMode?: 'login' | 'register';
}

export const Auth = ({ onClose, initialMode = 'login' }: AuthProps) => {
  const [isLogin, setIsLogin] = useState(initialMode === 'login');
  // When true, the modal shows the "Reset your password" (request-link) view
  // instead of the login/register form.
  const [isForgotPassword, setIsForgotPassword] = useState(false);
  const [name, setName] = useState('');
  const [institution, setInstitution] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  // After register, when email confirmation is required we show a success screen
  // instead of the form and keep the email address so we can name it.
  const [confirmationEmail, setConfirmationEmail] = useState<string | null>(null);
  // After requesting a reset link we keep the email so we can name it in the
  // neutral confirmation message (which never reveals if an account exists).
  const [resetRequestedEmail, setResetRequestedEmail] = useState<string | null>(null);
  const { login, register } = useAuth();

  // Close the modal on Escape — standard dialog behavior.
  useEffect(() => {
    if (!onClose) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [onClose]);

  // Password validation helpers
  const passwordRequirements = {
    minLength: password.length >= 12,
    hasUppercase: /[A-Z]/.test(password),
    hasLowercase: /[a-z]/.test(password),
    hasDigit: /[0-9]/.test(password),
  };

  const isPasswordValid =
    passwordRequirements.minLength &&
    passwordRequirements.hasUppercase &&
    passwordRequirements.hasLowercase &&
    passwordRequirements.hasDigit;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setIsLoading(true);

    try {
      const result = isLogin
        ? await login(email, password)
        : await register(name, email, password, institution.trim() || undefined);

      if (result.success) {
        // Register with email confirmation required: don't close — switch to the
        // login view and show a "check your email" confirmation message so the
        // user can log in after clicking the link.
        if (!isLogin && result.emailVerificationRequired) {
          setConfirmationEmail(email);
          setIsLogin(true);
          setPassword('');
          return;
        }
        onClose?.();
      } else if (result.emailVerificationRequired) {
        // Login on an unconfirmed account (HTTP 401): show the specific
        // "confirm your email first" guidance, not the generic error.
        setError(
          result.error ||
            'Please confirm your email first — check your inbox for the confirmation link.'
        );
      } else {
        setError(result.error || 'Authentication failed');
      }
    } catch (error: unknown) {
      setError(extractApiErrorMessage(error, 'An error occurred'));
    } finally {
      setIsLoading(false);
    }
  };

  const handleForgotPasswordSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setIsLoading(true);

    try {
      // The backend always returns success — even for unknown emails — so we
      // show the same neutral confirmation regardless of the outcome and never
      // reveal whether an account exists.
      await api.forgotPassword(email);
      setResetRequestedEmail(email);
    } catch (error: unknown) {
      setError(extractApiErrorMessage(error, 'Something went wrong. Please try again.'));
    } finally {
      setIsLoading(false);
    }
  };

  const showForgotPassword = () => {
    setIsForgotPassword(true);
    setError('');
    setConfirmationEmail(null);
    setResetRequestedEmail(null);
    setPassword('');
  };

  const backToLogin = () => {
    setIsForgotPassword(false);
    setIsLogin(true);
    setError('');
    setConfirmationEmail(null);
    setResetRequestedEmail(null);
    setPassword('');
  };

  const toggleMode = () => {
    setIsLogin(!isLogin);
    setIsForgotPassword(false);
    setError('');
    setConfirmationEmail(null);
    setResetRequestedEmail(null);
    setName('');
    setInstitution('');
    setEmail('');
    setPassword('');
  };

  if (isForgotPassword) {
    return (
      <div className="auth-modal-overlay" onClick={onClose}>
        <div
          className="auth-modal"
          role="dialog"
          aria-modal="true"
          aria-label="Reset your password"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="auth-header">
            <h2>Reset your password</h2>
            {onClose && (
              <button className="auth-close" aria-label="Close" onClick={onClose}>
                ×
              </button>
            )}
          </div>

          <form onSubmit={handleForgotPasswordSubmit} className="auth-form">
            {error && <div className="auth-error">{error}</div>}

            {resetRequestedEmail ? (
              <>
                <div className="auth-success">
                  If an account exists for <strong>{resetRequestedEmail}</strong>, we've sent a
                  password reset link. Check your inbox.
                </div>
                <div className="auth-toggle">
                  <button type="button" onClick={backToLogin} className="auth-toggle-btn">
                    Back to login
                  </button>
                </div>
              </>
            ) : (
              <>
                <div className="auth-field">
                  <label htmlFor="reset-email">Email</label>
                  <input
                    id="reset-email"
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    required
                    placeholder="your@email.com"
                    autoComplete="email"
                  />
                </div>

                <button type="submit" className="auth-submit" disabled={isLoading}>
                  {isLoading ? 'Please wait...' : 'Send reset link'}
                </button>

                <div className="auth-toggle">
                  <button type="button" onClick={backToLogin} className="auth-toggle-btn">
                    Back to login
                  </button>
                </div>
              </>
            )}
          </form>
        </div>
      </div>
    );
  }

  return (
    <div className="auth-modal-overlay" onClick={onClose}>
      <div
        className="auth-modal"
        role="dialog"
        aria-modal="true"
        aria-label={isLogin ? 'Login' : 'Register'}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="auth-header">
          <h2>{isLogin ? 'Login' : 'Register'}</h2>
          {onClose && (
            <button className="auth-close" aria-label="Close" onClick={onClose}>
              ×
            </button>
          )}
        </div>

        <form onSubmit={handleSubmit} className="auth-form">
          {error && <div className="auth-error">{error}</div>}

          {confirmationEmail && (
            <div className="auth-success">
              ✓ Account created! Check your email (<strong>{confirmationEmail}</strong>) and
              click the confirmation link to activate your account, then log in.
            </div>
          )}

          {!isLogin && (
            <div className="auth-field">
              <label htmlFor="name">Name</label>
              <input
                id="name"
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                placeholder="Your name"
                autoComplete="name"
              />
            </div>
          )}

          {!isLogin && (
            <div className="auth-field">
              <label htmlFor="institution">Institution / Company (optional)</label>
              <input
                id="institution"
                type="text"
                value={institution}
                onChange={(e) => setInstitution(e.target.value)}
                placeholder="e.g. University of Toronto"
                autoComplete="organization"
              />
            </div>
          )}

          <div className="auth-field">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              placeholder="your@email.com"
              autoComplete="email"
            />
          </div>

          <div className="auth-field">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              placeholder={isLogin ? 'Enter your password' : 'At least 12 characters'}
              minLength={isLogin ? 6 : 12}
              autoComplete={isLogin ? 'current-password' : 'new-password'}
            />
            {!isLogin && password && (
              <div className="password-requirements">
                <div className={passwordRequirements.minLength ? 'req-met' : 'req-unmet'}>
                  {passwordRequirements.minLength ? '✓' : '○'} At least 12 characters
                </div>
                <div className={passwordRequirements.hasUppercase ? 'req-met' : 'req-unmet'}>
                  {passwordRequirements.hasUppercase ? '✓' : '○'} One uppercase letter
                </div>
                <div className={passwordRequirements.hasLowercase ? 'req-met' : 'req-unmet'}>
                  {passwordRequirements.hasLowercase ? '✓' : '○'} One lowercase letter
                </div>
                <div className={passwordRequirements.hasDigit ? 'req-met' : 'req-unmet'}>
                  {passwordRequirements.hasDigit ? '✓' : '○'} One digit
                </div>
              </div>
            )}
            {isLogin && (
              <div className="auth-forgot-password">
                <button type="button" onClick={showForgotPassword} className="auth-toggle-btn">
                  Forgot password?
                </button>
              </div>
            )}
          </div>

          <button
            type="submit"
            className="auth-submit"
            disabled={isLoading || (!isLogin && !isPasswordValid)}
          >
            {isLoading ? 'Please wait...' : isLogin ? 'Login' : 'Register'}
          </button>

          <div className="auth-divider">
            <span>or</span>
          </div>

          <GoogleSignInButton onError={setError} />

          <div className="auth-toggle">
            {isLogin ? "Don't have an account? " : 'Already have an account? '}
            <button type="button" onClick={toggleMode} className="auth-toggle-btn">
              {isLogin ? 'Register' : 'Login'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
