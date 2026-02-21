/**
 * Local storage keys used throughout the application
 */

export const STORAGE_KEYS = {
  /** JWT authentication token */
  AUTH_TOKEN: 'openecon_auth_token',

  /** Anonymous session ID for non-authenticated users */
  SESSION_ID: 'anon_session_id',

  /** User preferences */
  USER_PREFERENCES: 'openecon_user_preferences',

  /** Theme preference (light/dark) */
  THEME: 'openecon_theme',
} as const;

export type StorageKey = typeof STORAGE_KEYS[keyof typeof STORAGE_KEYS];
