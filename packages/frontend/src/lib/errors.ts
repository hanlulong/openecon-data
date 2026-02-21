/**
 * Centralized error handling utilities
 */

import { logger } from './logger';
import { HTTP_STATUS } from '../constants';

/**
 * Custom error types for better error handling
 */

export class ApiError extends Error {
  constructor(
    message: string,
    public statusCode: number,
    public details?: any
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

export class NetworkError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'NetworkError';
  }
}

export class ValidationError extends Error {
  constructor(
    message: string,
    public field?: string
  ) {
    super(message);
    this.name = 'ValidationError';
  }
}

export class AuthenticationError extends Error {
  constructor(message: string = 'Authentication required') {
    super(message);
    this.name = 'AuthenticationError';
  }
}

/**
 * Error handlers
 */

/**
 * Extracts user-friendly error message from various error types
 */
export function getErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message;
  }

  if (error instanceof NetworkError) {
    return 'Network connection failed. Please check your internet connection.';
  }

  if (error instanceof ValidationError) {
    return error.message;
  }

  if (error instanceof AuthenticationError) {
    return error.message;
  }

  if (error instanceof Error) {
    return error.message;
  }

  if (typeof error === 'string') {
    return error;
  }

  return 'An unexpected error occurred';
}

/**
 * Determines if an error is a network error
 */
export function isNetworkError(error: unknown): boolean {
  return (
    error instanceof NetworkError ||
    (error instanceof Error && error.message.toLowerCase().includes('network'))
  );
}

/**
 * Determines if an error is an authentication error
 */
export function isAuthError(error: unknown): boolean {
  if (error instanceof AuthenticationError) {
    return true;
  }

  if (error instanceof ApiError) {
    return error.statusCode === HTTP_STATUS.UNAUTHORIZED;
  }

  return false;
}

/**
 * Handles API errors and converts them to appropriate error types
 */
export function handleApiError(error: any): never {
  logger.error('API Error:', error);

  if (error.response) {
    const status = error.response.status;
    const data = error.response.data;

    const message =
      data?.error ||
      data?.detail ||
      data?.message ||
      'An error occurred while processing your request';

    if (status === HTTP_STATUS.UNAUTHORIZED) {
      throw new AuthenticationError(message);
    }

    if (status === HTTP_STATUS.UNPROCESSABLE_ENTITY || status === HTTP_STATUS.BAD_REQUEST) {
      throw new ValidationError(message);
    }

    throw new ApiError(message, status, data);
  }

  if (error.request) {
    throw new NetworkError('Unable to reach the server. Please try again.');
  }

  throw new Error('An unexpected error occurred');
}

/**
 * Safely executes an async function and handles errors
 */
export async function tryCatch<T>(
  fn: () => Promise<T>,
  onError?: (error: unknown) => void
): Promise<[T | null, Error | null]> {
  try {
    const result = await fn();
    return [result, null];
  } catch (error) {
    const errorObj = error instanceof Error ? error : new Error(String(error));

    if (onError) {
      onError(errorObj);
    } else {
      logger.error('Unhandled error:', errorObj);
    }

    return [null, errorObj];
  }
}
