import { useState } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { GoogleSignInButton } from './GoogleSignInButton';
import './Auth.css';

interface AuthProps {
  onClose?: () => void;
}

export const Auth = ({ onClose }: AuthProps) => {
  const [isLogin, setIsLogin] = useState(true);
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const { login, register } = useAuth();

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
        : await register(name, email, password);

      if (result.success) {
        onClose?.();
      } else {
        setError(result.error || 'Authentication failed');
      }
    } catch (err: any) {
      setError(err.message || 'An error occurred');
    } finally {
      setIsLoading(false);
    }
  };

  const toggleMode = () => {
    setIsLogin(!isLogin);
    setError('');
    setName('');
    setEmail('');
    setPassword('');
  };

  return (
    <div className="auth-modal-overlay" onClick={onClose}>
      <div className="auth-modal" onClick={(e) => e.stopPropagation()}>
        <div className="auth-header">
          <h2>{isLogin ? 'Login' : 'Register'}</h2>
          {onClose && (
            <button className="auth-close" onClick={onClose}>
              ×
            </button>
          )}
        </div>

        <form onSubmit={handleSubmit} className="auth-form">
          {error && <div className="auth-error">{error}</div>}

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
