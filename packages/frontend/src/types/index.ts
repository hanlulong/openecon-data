// Frontend types

export interface NormalizedData {
  metadata: {
    source: string;
    indicator: string;
    country?: string;
    frequency: string;
    unit: string;
    lastUpdated: string;
    seriesId?: string;
    apiUrl?: string;
    sourceUrl?: string; // Human-readable URL for data verification
    // Enhanced metadata fields for detailed series information
    seasonalAdjustment?: string; // e.g., "Seasonally adjusted", "Not seasonally adjusted"
    dataType?: string; // e.g., "Level", "Change", "Percent Change", "Index"
    priceType?: string; // e.g., "Chained (2017) dollars", "Current prices"
    description?: string; // Full description of the series
    notes?: string[]; // Additional notes or footnotes
    scaleFactor?: string; // e.g., "millions", "billions", "thousands"
    startDate?: string; // First available data date
    endDate?: string; // Last available data date
  };
  data: Array<{
    date: string;
    value: number | null;
  }>;
}

// ParsedIntent type matching backend/models.py ParsedIntent
export interface ParsedIntent {
  apiProvider: string;
  indicators: string[];
  parameters: Record<string, any>;
  clarificationNeeded: boolean;
  clarificationQuestions?: string[];
  confidence?: number;
  recommendedChartType?: 'line' | 'bar' | 'scatter' | 'table';
  // Original query for downstream processing
  originalQuery?: string;
  // Query decomposition for complex queries
  needsDecomposition?: boolean;
  decompositionType?: string;  // "provinces", "states", "regions", "countries"
  decompositionEntities?: string[];  // e.g., ["Ontario", "Quebec", "BC", ...]
  useProMode?: boolean;  // Auto-switch to Pro Mode for complex aggregations
}

export interface QueryResponse {
  conversationId: string;
  intent?: ParsedIntent;
  data?: NormalizedData[];
  clarificationNeeded: boolean;
  clarificationQuestions?: string[];
  error?: string;
  message?: string;
  codeExecution?: CodeExecutionResult;
  isProMode?: boolean;
  processingSteps?: ProcessingStep[];
}

export interface GeneratedFile {
  url: string;      // URL path to access the file (e.g., /static/promode/file.png)
  name: string;     // File name
  type: string;     // File type: 'image', 'data', 'html', 'file'
}

export interface CodeExecutionResult {
  code: string;
  output: string;
  error?: string;
  executionTime?: number;
  files?: GeneratedFile[];  // List of generated files (images, data, html)
}

export interface Message {
  role: 'user' | 'assistant';
  content: string;
  timestamp: Date;
  data?: NormalizedData[];
  chartType?: 'line' | 'bar' | 'scatter' | 'table';
  codeExecution?: CodeExecutionResult;
  isProMode?: boolean;
  processingSteps?: ProcessingStep[];
}

export interface ExportFormat {
  format: 'csv' | 'json' | 'dta';
  filename?: string;
}

// ExportRequest matches backend models.py ExportRequest
export interface ExportRequest {
  data: NormalizedData[];
  format: 'csv' | 'json' | 'dta';
  filename?: string;
}

export interface User {
  id: string;
  email: string;
  name: string;
  createdAt?: string;
  lastLogin?: string;
}

export interface AuthResponse {
  success: boolean;
  token?: string;
  user?: User;
  error?: string;
}

export interface RegisterRequest {
  email: string;
  password: string;
  name: string;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface UserQueryHistory {
  id: string;
  userId: string;
  query: string;
  conversationId: string;
  intent?: ParsedIntent;
  data?: NormalizedData[];
  timestamp: string;
}

export interface ProcessingStep {
  step: string;
  description: string;
  duration_ms?: number;
  metadata?: Record<string, any>;
}

// Additional type for chat history items (used in ChatPage)
export interface HistoryItem {
  id: string;
  query: string;
  timestamp: string;
  conversationId: string;
  data?: NormalizedData[];
  intent?: ParsedIntent;
}

// Health check response - matches backend HealthResponse model
export interface HealthCacheStats {
  keys: number;
  hits: number;
  misses: number;
  ksize: number;
  vsize: number;
}

export interface HealthUserStats {
  totalUsers: number;
  totalQueries: number;
}

export interface HealthResponse {
  status: string;
  timestamp: string;
  environment: string;
  services: Record<string, boolean>;
  cache: HealthCacheStats;
  users: HealthUserStats;
  promodeEnabled: boolean;  // Always present (defaults to false in backend)
}

// Cache stats response
export interface CacheStatsResponse {
  keys: number;
  hits: number;
  misses: number;
  hitRate: number;
  size: number;
}

// API error response
export interface ApiError {
  error: string;
  message?: string;
  detail?: string;
  status?: number;
}

// Feedback types
export interface FeedbackRequest {
  type: 'bug' | 'feature' | 'other';
  message?: string;
  email?: string;
  sessionInfo?: {
    url: string;
    userAgent: string;
    timestamp: string;
    screenSize: string;
    language: string;
    timezone: string;
    referrer: string;
  };
  conversation?: {
    messages: string;
    messageCount: number;
    conversationId?: string;
    rawMessages?: Array<{
      role: string;
      content: string;
      timestamp: string;
      hasData: boolean;
      dataCount: number;
      isProMode?: boolean;
    }>;
  } | null;
  userId?: string;
  userName?: string;
}

export interface FeedbackResponse {
  success: boolean;
  message: string;
  feedbackId?: string;
}
