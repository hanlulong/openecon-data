import './RegistrationWall.css';

interface RegistrationWallProps {
  isOpen: boolean;
  /** The anonymous free-query limit (e.g. 20). Falls back to a generic message if absent. */
  limit?: number;
  onClose: () => void;
  /** Open the auth flow in sign-up (register) mode. */
  onSignUp: () => void;
  /** Open the auth flow in log-in mode. */
  onLogIn: () => void;
}

/**
 * Registration wall shown to anonymous users who have exhausted their free
 * queries. The triggering query was NOT processed by the backend, so this
 * replaces the answer bubble entirely.
 */
export const RegistrationWall = ({
  isOpen,
  limit,
  onClose,
  onSignUp,
  onLogIn,
}: RegistrationWallProps) => {
  if (!isOpen) return null;

  const limitText = typeof limit === 'number' ? `${limit} free` : 'free';

  return (
    <div className="registration-wall-overlay" onClick={onClose}>
      <div
        className="registration-wall"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="registration-wall-title"
      >
        <button
          className="registration-wall-close"
          onClick={onClose}
          aria-label="Close"
        >
          ×
        </button>

        <div className="registration-wall-icon" aria-hidden="true">
          <svg width="40" height="40" viewBox="0 0 24 24" fill="none">
            <path
              d="M12 15v2m-6 4h12a2 2 0 0 0 2-2v-6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2zm10-10V7a4 4 0 0 0-8 0v4h8z"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </div>

        <h3 id="registration-wall-title" className="registration-wall-title">
          You've used your {limitText} queries
        </h3>

        <p className="registration-wall-message">
          Create a free account to keep exploring economic data. Your history is
          saved, so you can pick up right where you left off.
        </p>

        <div className="registration-wall-actions">
          <button
            type="button"
            className="registration-wall-primary"
            onClick={onSignUp}
          >
            Sign up
          </button>
          <button
            type="button"
            className="registration-wall-secondary"
            onClick={onLogIn}
          >
            Log in
          </button>
        </div>

        <p className="registration-wall-footnote">
          Already have an account? Logging in restores your saved queries.
        </p>
      </div>
    </div>
  );
};
