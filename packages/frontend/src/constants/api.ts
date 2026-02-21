/**
 * API configuration constants
 */

/**
 * Base API URL - defaults to /api for proxying in development
 */
export const API_BASE_URL = import.meta.env.VITE_API_URL || '/api';

/**
 * API timeout in milliseconds
 */
export const API_TIMEOUT = 30000; // 30 seconds

/**
 * Streaming API timeout in milliseconds (longer for SSE streams)
 */
export const STREAMING_TIMEOUT = 120000; // 2 minutes

/**
 * API endpoints
 */
export const API_ENDPOINTS = {
  // Authentication
  AUTH: {
    REGISTER: '/auth/register',
    LOGIN: '/auth/login',
    ME: '/auth/me',
  },

  // Queries
  QUERY: {
    STANDARD: '/query',
    STREAM: '/query/stream',
    PRO: '/query/pro',
    PRO_STREAM: '/query/pro/stream',
  },

  // User
  USER: {
    HISTORY: '/user/history',
  },

  // Session
  SESSION: {
    HISTORY: '/session/history',
  },

  // Export
  EXPORT: '/export',

  // Health
  HEALTH: '/health',

  // Cache
  CACHE: {
    STATS: '/cache/stats',
    CLEAR: '/cache/clear',
  },
} as const;

/**
 * HTTP status codes
 */
export const HTTP_STATUS = {
  OK: 200,
  CREATED: 201,
  BAD_REQUEST: 400,
  UNAUTHORIZED: 401,
  FORBIDDEN: 403,
  NOT_FOUND: 404,
  CONFLICT: 409,
  UNPROCESSABLE_ENTITY: 422,
  TOO_MANY_REQUESTS: 429,
  INTERNAL_SERVER_ERROR: 500,
  SERVICE_UNAVAILABLE: 503,
} as const;
