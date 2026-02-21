import { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { api } from '../services/api'
import { Message, NormalizedData, ProcessingStep, HistoryItem } from '../types'
import { MessageChart } from './MessageChart'
import { CodeExecutionDisplay } from './CodeExecutionDisplay'
import { useAuth } from '../contexts/AuthContext'
import { Auth } from './Auth'
import { ProcessingSteps, ProcessingTimelineStep } from './ProcessingSteps'
import { trackAnonymousSession, getOrCreateSessionId } from '../lib/supabase'
import { useMobile } from '../hooks/useMobile'
import { logger } from '../utils/logger'
import { downloadExport } from '../lib/export'
import { ShareModal } from './ShareModal'
import { FeedbackModal } from './FeedbackModal'
import './ChatPage.css'

// Pure functions moved outside component for performance
function determineChartType(data: NormalizedData[]): 'line' | 'bar' | 'table' {
  if (data.length === 0) return 'line'

  const firstSeries = data[0]
  const dataPoints = firstSeries.data.length
  const frequency = firstSeries.metadata.frequency

  // Check if this is exchange rate data (currency codes as dates)
  const isExchangeRateData = data.length === 1 &&
    firstSeries.metadata.unit === 'exchange rate' &&
    firstSeries.data.length > 1 &&
    firstSeries.data.every(point => /^[A-Z]{3}$/.test(point.date))

  if (isExchangeRateData) {
    return 'table'
  }

  // Table for data with widely varying scales across multiple series
  if (data.length > 1) {
    const allValues = data.flatMap(series => series.data.map(d => d.value).filter((v): v is number => v !== null).map(v => Math.abs(v)))
    const minValue = Math.min(...allValues.filter(v => v > 0))
    const maxValue = Math.max(...allValues)

    // If the ratio between max and min values is very large (e.g., comparing EUR ~0.9 to JPY ~110)
    // suggest table view for better readability
    if (maxValue / minValue > 50) {
      return 'table'
    }
  }

  // Bar chart for annual data with few years or categorical comparisons
  if (frequency === 'annual' && dataPoints <= 10) {
    return 'bar'
  }

  // Line chart for time series (default for most economic data)
  return 'line'
}

function mapProcessingStepsToTimeline(steps?: ProcessingStep[]): ProcessingTimelineStep[] {
  if (!steps || steps.length === 0) {
    return []
  }
  return steps.map(step => ({
    step: step.step,
    description: step.description,
    status: 'completed' as const,
    durationMs: step.duration_ms,
  }))
}

// Example queries array - consistent with landing page
const EXAMPLE_QUERIES = [
  "US unemployment rate and inflation 2019-2024",
  "GDP growth China, India, Brazil 2018-2023",
  "GDP growth US, Germany, Japan from IMF 2018-2023",
  "Credit to GDP ratio US, UK, Japan from BIS 2019-2023",
  "Total exports US, China 2018-2023 from Comtrade",
]

export function ChatPage() {
  const navigate = useNavigate()
  const location = useLocation()
  const [query, setQuery] = useState('')
  const [messages, setMessages] = useState<Message[]>([])
  const [conversationId, setConversationId] = useState<string | undefined>()
  const [loadingStatus, setLoadingStatus] = useState<string>('')
  const [activeProcessingSteps, setActiveProcessingSteps] = useState<ProcessingTimelineStep[]>([])
  const [showAuthModal, setShowAuthModal] = useState(false)
  const [history, setHistory] = useState<HistoryItem[]>([])
  const [searchQuery, setSearchQuery] = useState('')
  const [proMode, setProMode] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [pythonCodeModal, setPythonCodeModal] = useState<{ show: boolean; code: string; loading: boolean; error?: string; copied?: boolean }>({
    show: false,
    code: '',
    loading: false,
    copied: false,
  })
  const [shareModal, setShareModal] = useState<{
    isOpen: boolean
    singleQuery?: { query: string; data?: NormalizedData[]; messageIndex: number }
  }>({
    isOpen: false,
  })
  const [feedbackModalOpen, setFeedbackModalOpen] = useState(false)
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const { user, isAuthenticated, logout } = useAuth()
  const { isMobile } = useMobile()
  const processingQuery = useRef<string | null>(null)

  useEffect(() => {
    document.title = 'Chat | OpenEcon.ai'
  }, [])

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }

  useEffect(() => {
    scrollToBottom()
  }, [messages])

  // Track anonymous session on page load (only for non-authenticated users)
  useEffect(() => {
    const trackSession = async () => {
      // Only track anonymous sessions if user is NOT authenticated
      if (isAuthenticated) {
        logger.log('User is authenticated, skipping anonymous session tracking')
        return
      }

      try {
        // Create session ID if it doesn't exist
        const sessionId = getOrCreateSessionId()
        logger.log('Session ID:', sessionId)

        // Track the anonymous session in Supabase
        await trackAnonymousSession()
      } catch (error) {
        logger.error('Failed to track anonymous session:', error)
        // Non-critical error, continue anyway
      }
    }

    trackSession()
  }, [isAuthenticated]) // Re-run when auth status changes

  const loadHistory = useCallback(async () => {
    try {
      const response = await api.getUserHistory(20)
      logger.log('Loaded history:', response.history)
      setHistory(response.history)
    } catch (error) {
      logger.error('Failed to load history:', error)
    }
  }, [])

  const loadSessionHistory = useCallback(async () => {
    try {
      const sessionId = getOrCreateSessionId()
      if (!sessionId) {
        logger.log('No session ID available')
        return
      }
      const response = await api.getSessionHistory(sessionId, 20)
      logger.log('Loaded session history:', response.history)
      setHistory(response.history)
    } catch (error) {
      logger.error('Failed to load session history:', error)
      setHistory([]) // Clear history on error
    }
  }, [])

  // Load user history when authenticated, or session history when anonymous
  useEffect(() => {
    logger.log('Auth status changed:', isAuthenticated)
    if (isAuthenticated) {
      loadHistory()
    } else {
      loadSessionHistory()
    }
  }, [isAuthenticated, loadHistory, loadSessionHistory])

  const handleClearHistory = async () => {
    if (!confirm('Are you sure you want to clear all your chat history? This action cannot be undone.')) {
      return
    }

    try {
      await api.clearUserHistory()
      setHistory([])
      setMessages([])
      logger.log('History cleared successfully')
    } catch (error) {
      logger.error('Failed to clear history:', error)
      alert('Failed to clear history. Please try again.')
    }
  }

  const loadHistoryItem = (item: HistoryItem) => {
    // Always load the conversation, even if data is missing
    const userMessage = {
      role: 'user' as const,
      content: item.query,
      timestamp: new Date(item.timestamp),
    }

    if (item.data) {
      // If we have data, show it immediately
      setMessages([userMessage, {
        role: 'assistant' as const,
        content: '', // Empty content - details shown in MessageChart component
        timestamp: new Date(item.timestamp),
        data: item.data,
        chartType: determineChartType(item.data),
      }])
    } else {
      // If no data, just show the user's query
      // The user can re-submit if needed
      setMessages([userMessage])
    }

    // Always set conversationId to resume the conversation
    if (item.conversationId) {
      setConversationId(item.conversationId)
    }

    // Close sidebar on mobile after selecting a history item
    if (isMobile) {
      setSidebarOpen(false)
    }
  }

  // Streaming query handler (works for both regular and Pro Mode)
  const handleStreamingQuery = useCallback(async (q: string) => {
    // Prevent duplicate requests for the same query
    if (processingQuery.current === q) {
      logger.log('Duplicate query detected, skipping:', q)
      return
    }

    processingQuery.current = q
    setLoadingStatus(proMode ? '🚀 Pro Mode processing...' : '🤖 Processing your query...')
    setActiveProcessingSteps([]) // Clear any previous steps

    const startTime = Date.now()
    const stepMap = new Map<string, ProcessingTimelineStep>()

    // Use streaming for both regular and Pro Mode
    try {
      await api.queryStream(q, conversationId, proMode, {
        onStep: (step) => {
          // Update or add processing step in real-time
          // If step has a status field, use it; if it has duration_ms, it's completed
          const status = (step as any).status || (step.duration_ms !== undefined ? 'completed' : 'in-progress')

          const timelineStep: ProcessingTimelineStep = {
            step: step.step,
            description: step.description,
            status: status as 'pending' | 'in-progress' | 'completed',
            durationMs: step.duration_ms,
          }
          stepMap.set(step.step, timelineStep)
          setActiveProcessingSteps(Array.from(stepMap.values()))
        },
        onData: (response) => {
          const elapsed = Date.now() - startTime
          logger.log(`Query completed in ${elapsed}ms`)

          setLoadingStatus('')
          processingQuery.current = null

          if (!conversationId) {
            setConversationId(response.conversationId)
          }

          if (response.error) {
            const errorMessage = response.message || response.error
            let displayMessage = errorMessage
            if (response.error === 'data_not_available') {
              displayMessage = `📊 ${errorMessage}`
            } else if (response.error === 'processing_error') {
              displayMessage = `⚠️ ${errorMessage}`
            }

            setMessages(prev => [...prev, {
              role: 'assistant',
              content: displayMessage,
              timestamp: new Date(),
              processingSteps: response.processingSteps,
            }])
            return
          }

          if (response.clarificationNeeded) {
            setMessages(prev => [...prev, {
              role: 'assistant',
              content: response.clarificationQuestions?.join('\n') || 'Please clarify your request.',
              timestamp: new Date(),
              processingSteps: response.processingSteps,
            }])
            return
          }

          // Show message for: data responses, code execution, or message-only responses (like research queries)
          if (response.codeExecution || response.data || response.message) {
            const chartType = response.data ? (response.intent?.recommendedChartType || determineChartType(response.data)) : undefined

            setMessages(prev => [...prev, {
              role: 'assistant',
              content: response.message || (response.codeExecution ? 'Code executed successfully' : ''),
              timestamp: new Date(),
              data: response.data,
              chartType,
              codeExecution: response.codeExecution,
              isProMode: response.isProMode,
              processingSteps: response.processingSteps,
            }])

            // Reload history for both authenticated and anonymous users
            setTimeout(() => {
              if (isAuthenticated) {
                loadHistory()
              } else {
                loadSessionHistory()
              }
            }, 500)
          }
        },
        onError: (error) => {
          logger.error('Stream error:', error)
          setLoadingStatus('')
          processingQuery.current = null
          setActiveProcessingSteps([])

          setMessages(prev => [...prev, {
            role: 'assistant',
            content: `⚠️ ${error.message || error.error}`,
            timestamp: new Date(),
          }])
        },
        onDone: (convId) => {
          logger.log('Stream completed for conversation:', convId)
          setLoadingStatus('')
          setActiveProcessingSteps([])
        },
      })
    } catch (error: any) {
      logger.error('Streaming query error:', error)
      setLoadingStatus('')
      processingQuery.current = null
      setActiveProcessingSteps([])

      setMessages(prev => [...prev, {
        role: 'assistant',
        content: `Error: ${error.message}`,
        timestamp: new Date(),
      }])
    }
  }, [conversationId, proMode, isAuthenticated, loadHistory, loadSessionHistory])

  const streamingQueryRef = useRef(handleStreamingQuery)
  useEffect(() => {
    streamingQueryRef.current = handleStreamingQuery
  }, [handleStreamingQuery])

  useEffect(() => {
    const params = new URLSearchParams(location.search)
    const initialQuery = params.get('query')
    const showAuth = params.get('auth')

    if (initialQuery) {
      // Use sessionStorage to track prefetched queries across StrictMode remounts
      // Store with timestamp to allow re-running after 5 seconds
      const prefetchKey = `prefetched_${initialQuery}`
      const storedData = sessionStorage.getItem(prefetchKey)
      const now = Date.now()

      let shouldPrefetch = true
      if (storedData) {
        const timestamp = parseInt(storedData, 10)
        // Only prevent if prefetched within last 5 seconds
        if (now - timestamp < 5000) {
          shouldPrefetch = false
        }
      }

      if (shouldPrefetch) {
        sessionStorage.setItem(prefetchKey, now.toString())
        setQuery(initialQuery)
        setMessages(prev => [...prev, {
          role: 'user',
          content: initialQuery,
          timestamp: new Date(),
        }])
        streamingQueryRef.current?.(initialQuery)
      }
    }

    if (showAuth === '1') {
      setShowAuthModal(true)
    }
  }, [location.search])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!query.trim()) return

    setMessages(prev => [...prev, {
      role: 'user',
      content: query,
      timestamp: new Date(),
    }])

    void handleStreamingQuery(query)
    setQuery('')
  }

  const handleExampleClick = (exampleQuery: string) => {
    setQuery(exampleQuery)
  }

  const handleNewChat = () => {
    // Clear all state
    setMessages([])
    setConversationId(undefined)
    setQuery('')
    setSearchQuery('')

    // Clear prefetch cache
    sessionStorage.clear()

    // If we have query params, navigate to clean /chat
    if (location.search) {
      navigate('/chat')
    }
  }

  // Memoize filtered history to avoid recalculating on every render
  const filteredHistory = useMemo(() => {
    return searchQuery
      ? history.filter(item => item.query.toLowerCase().includes(searchQuery.toLowerCase()))
      : history
  }, [history, searchQuery])

  const handleExport = async (data: NormalizedData[], format: 'csv' | 'json' | 'dta' | 'python') => {
    if (!data) return

    if (format === 'python') {
      // Generate Python code using Pro Mode
      await handlePythonExport(data)
      return
    }

    try {
      // Type guard: format is now 'csv' | 'json' | 'dta' after python check above
      const exportFormat = format as 'csv' | 'json' | 'dta'
      const blob = await api.exportData(data, exportFormat)
      downloadExport(blob, exportFormat)
    } catch (error) {
      logger.error('Export error:', error)
    }
  }

  const handlePythonExport = async (data: NormalizedData[]) => {
    setPythonCodeModal({ show: true, code: '', loading: true })

    try {
      // Build a description of the data for code generation
      const dataDescription = data.map(series => ({
        source: series.metadata.source,
        indicator: series.metadata.indicator,
        country: series.metadata.country,
        unit: series.metadata.unit,
        frequency: series.metadata.frequency,
        seriesId: series.metadata.seriesId,
        sourceUrl: series.metadata.sourceUrl,
        apiUrl: series.metadata.apiUrl,
        startDate: series.data[0]?.date,
        endDate: series.data[series.data.length - 1]?.date,
        dataPoints: series.data.length,
      }))

      // Create prompt for Pro Mode to generate Python code
      const prompt = `Generate standalone Python code that fetches and displays this economic data:

${JSON.stringify(dataDescription, null, 2)}

Requirements:
1. The code must be completely self-contained and runnable with just "python script.py"
2. Use only standard libraries (requests, json, datetime) or common packages (pandas if needed for data manipulation)
3. Include proper error handling
4. Print the data in a readable format (table or formatted output)
5. Add comments explaining what the code does
6. If the data source has a public API, use it directly. If not, provide code that fetches similar data.
7. Include the source URL as a comment for reference

Return ONLY the Python code, no explanations before or after.`

      const response = await api.queryPro(prompt)

      if (response.error) {
        setPythonCodeModal({ show: true, code: '', loading: false, error: response.error })
        return
      }

      // Extract Python code from the response
      let pythonCode = ''
      if (response.codeExecution?.code) {
        pythonCode = response.codeExecution.code
      } else if (response.data && response.data.length > 0) {
        // Try to extract code from the response text
        const text = JSON.stringify(response.data)
        const codeMatch = text.match(/```python\n?([\s\S]*?)```/) || text.match(/```\n?([\s\S]*?)```/)
        pythonCode = codeMatch ? codeMatch[1] : text
      }

      if (!pythonCode) {
        // Fallback: generate simple code based on the data
        pythonCode = generateFallbackPythonCode(data)
      }

      setPythonCodeModal({ show: true, code: pythonCode, loading: false })
    } catch (error) {
      logger.error('Python export error:', error)
      // Fallback to simple generated code
      const fallbackCode = generateFallbackPythonCode(data)
      setPythonCodeModal({ show: true, code: fallbackCode, loading: false })
    }
  }

  const generateFallbackPythonCode = (data: NormalizedData[]): string => {
    const series = data[0]
    const source = series.metadata.source
    const indicator = series.metadata.indicator
    const country = series.metadata.country || 'N/A'
    const sourceUrl = series.metadata.sourceUrl || series.metadata.apiUrl || ''

    // Generate provider-specific code
    if (source === 'FRED') {
      const seriesId = series.metadata.seriesId || 'GDP'
      return `#!/usr/bin/env python3
"""
Fetch ${indicator} data from FRED (Federal Reserve Economic Data)
Source: ${sourceUrl}
"""

import requests
import json
from datetime import datetime

# FRED API endpoint (no API key needed for basic access)
SERIES_ID = "${seriesId}"
API_URL = f"https://api.stlouisfed.org/fred/series/observations"

# For full API access, get a free API key from https://fred.stlouisfed.org/docs/api/api_key.html
# API_KEY = "your_api_key_here"

def fetch_fred_data(series_id, api_key=None):
    """Fetch data from FRED API."""
    params = {
        "series_id": series_id,
        "file_type": "json",
    }
    if api_key:
        params["api_key"] = api_key

    # Alternative: Use the public observations endpoint
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"

    try:
        response = requests.get(url)
        response.raise_for_status()

        # Parse CSV data
        lines = response.text.strip().split("\\n")
        headers = lines[0].split(",")

        print(f"\\n{'='*60}")
        print(f"${indicator}")
        print(f"Source: FRED ({series_id})")
        print(f"{'='*60}\\n")
        print(f"{'Date':<12} {'Value':>15}")
        print(f"{'-'*12} {'-'*15}")

        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) >= 2:
                date, value = parts[0], parts[1]
                if value and value != ".":
                    print(f"{date:<12} {float(value):>15,.2f}")

        return True
    except requests.RequestException as e:
        print(f"Error fetching data: {e}")
        return False

if __name__ == "__main__":
    fetch_fred_data(SERIES_ID)
    print(f"\\nData source: ${sourceUrl}")
`
    } else if (source === 'World Bank') {
      const indicatorCode = series.metadata.seriesId || 'NY.GDP.MKTP.CD'
      const countryCode = country === 'United States' ? 'US' : country === 'China' ? 'CN' : 'WLD'
      return `#!/usr/bin/env python3
"""
Fetch ${indicator} data from World Bank API
Country: ${country}
Source: ${sourceUrl}
"""

import requests
import json

# World Bank API endpoint (no API key needed)
INDICATOR = "${indicatorCode}"
COUNTRY = "${countryCode}"
API_URL = f"https://api.worldbank.org/v2/country/{COUNTRY}/indicator/{INDICATOR}"

def fetch_worldbank_data():
    """Fetch data from World Bank API."""
    params = {
        "format": "json",
        "per_page": 100,
    }

    try:
        response = requests.get(API_URL, params=params)
        response.raise_for_status()
        data = response.json()

        if len(data) < 2 or not data[1]:
            print("No data available")
            return False

        print(f"\\n{'='*60}")
        print(f"${indicator}")
        print(f"Country: ${country}")
        print(f"Source: World Bank")
        print(f"{'='*60}\\n")
        print(f"{'Year':<8} {'Value':>20}")
        print(f"{'-'*8} {'-'*20}")

        for item in sorted(data[1], key=lambda x: x['date']):
            if item['value'] is not None:
                print(f"{item['date']:<8} {item['value']:>20,.2f}")

        return True
    except requests.RequestException as e:
        print(f"Error fetching data: {e}")
        return False

if __name__ == "__main__":
    fetch_worldbank_data()
    print(f"\\nData source: ${sourceUrl}")
`
    } else if (source === 'Statistics Canada') {
      const vectorId = series.metadata.seriesId || ''
      return `#!/usr/bin/env python3
"""
Fetch ${indicator} data from Statistics Canada
Source: ${sourceUrl}
"""

import requests
import json
from datetime import datetime

# Statistics Canada WDS API endpoint (no API key needed)
VECTOR_ID = "${vectorId}"
API_URL = "https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorsAndLatestNPeriods"

def fetch_statscan_data(vector_id, num_periods=120):
    """Fetch data from Statistics Canada WDS API."""
    payload = [{"vectorId": int(vector_id), "latestN": num_periods}]

    try:
        response = requests.post(API_URL, json=payload)
        response.raise_for_status()
        data = response.json()

        if not data or data[0].get("status") != "SUCCESS":
            print(f"Error: {data[0].get('status', 'Unknown error')}")
            return False

        vector_data = data[0].get("object", {}).get("vectorDataPoint", [])

        print(f"\\n{'='*60}")
        print(f"${indicator}")
        print(f"Vector ID: {vector_id}")
        print(f"Source: Statistics Canada")
        print(f"{'='*60}\\n")
        print(f"{'Date':<12} {'Value':>15}")
        print(f"{'-'*12} {'-'*15}")

        for point in vector_data:
            date = point.get("refPer", "")
            value = point.get("value")
            if value is not None:
                print(f"{date:<12} {value:>15,.2f}")

        return True
    except requests.RequestException as e:
        print(f"Error fetching data: {e}")
        return False

if __name__ == "__main__":
    fetch_statscan_data(VECTOR_ID)
    print(f"\\nData source: ${sourceUrl}")
`
    } else {
      // Generic fallback
      return `#!/usr/bin/env python3
"""
Economic Data: ${indicator}
Country: ${country}
Source: ${source}
Reference: ${sourceUrl}

This data was retrieved from OpenEcon.ai
For direct API access, visit the source URL above.
"""

# Sample data from the query
data = ${JSON.stringify(series.data.slice(0, 20), null, 2)}

print(f"\\n{'='*60}")
print(f"${indicator}")
print(f"Country: ${country}")
print(f"Source: ${source}")
print(f"{'='*60}\\n")
print(f"{'Date':<12} {'Value':>15}")
print(f"{'-'*12} {'-'*15}")

for point in data:
    date = point.get("date", "")
    value = point.get("value")
    if value is not None:
        print(f"{date:<12} {value:>15,.2f}")

print(f"\\nData source: ${sourceUrl}")
`
    }
  }

  const handleChartTypeChange = (messageIndex: number, newChartType: 'line' | 'bar' | 'scatter' | 'table') => {
    setMessages(prev => prev.map((msg, idx) =>
      idx === messageIndex ? { ...msg, chartType: newChartType } : msg
    ))
  }

  const handleShareQuery = (messageIndex: number) => {
    // Find the corresponding user query for this assistant response
    const assistantMessage = messages[messageIndex]
    let userQuery = ''

    // Look backwards to find the user query that triggered this response
    for (let i = messageIndex - 1; i >= 0; i--) {
      if (messages[i].role === 'user') {
        userQuery = messages[i].content
        break
      }
    }

    setShareModal({
      isOpen: true,
      singleQuery: {
        query: userQuery,
        data: assistantMessage.data,
        messageIndex,
      },
    })
  }

  const handleShareChat = () => {
    setShareModal({
      isOpen: true,
      singleQuery: undefined, // Share whole chat
    })
  }

  return (
    <div className="chat-page">
      {/* Mobile Header */}
      {isMobile && (
        <div className="mobile-header">
          <button
            className="hamburger-btn"
            onClick={() => setSidebarOpen(!sidebarOpen)}
            aria-label="Toggle sidebar"
          >
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
              <path d="M3 12h18M3 6h18M3 18h18" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
            </svg>
          </button>
          <div className="mobile-logo">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none">
              <path d="M12 2L2 7L12 12L22 7L12 2Z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M2 17L12 22L22 17" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M2 12L12 17L22 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </div>
          <div className="mobile-header-right">
            <button
              className="mobile-feedback-btn"
              onClick={() => setFeedbackModalOpen(true)}
              title="Report Bug / Request Feature"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                <line x1="12" y1="8" x2="12" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
                <line x1="12" y1="16" x2="12.01" y2="16" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
              </svg>
            </button>
            <button
              className={`mobile-pro-mode-toggle ${proMode ? 'active' : ''}`}
              onClick={() => setProMode(!proMode)}
              title={proMode ? 'Pro Mode: ON' : 'Pro Mode: OFF'}
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </button>
          </div>
        </div>
      )}

      {/* Overlay for mobile sidebar */}
      {isMobile && sidebarOpen && (
        <div
          className="sidebar-overlay"
          onClick={() => setSidebarOpen(false)}
          aria-hidden="true"
        />
      )}

      <div className={`chat-sidebar ${isMobile && sidebarOpen ? 'open' : ''}`}>
        <div className="sidebar-header">
          <button className="logo-button" onClick={() => navigate('/')}>
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none">
              <path d="M12 2L2 7L12 12L22 7L12 2Z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M2 17L12 22L22 17" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M2 12L12 17L22 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </button>
        </div>

        <div className="sidebar-actions">
          <button className="sidebar-action-btn" onClick={handleNewChat}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
              <path d="M12 5V19M5 12H19" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            <span>New chat</span>
          </button>

          <button
            className={`sidebar-action-btn pro-mode-sidebar-toggle ${proMode ? 'active' : ''}`}
            onClick={() => setProMode(!proMode)}
            title={proMode ? 'Pro Mode: AI-powered code generation' : 'Enable Pro Mode'}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
              <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            <span>Pro Mode</span>
            <span className={`pro-mode-badge ${proMode ? 'on' : 'off'}`}>
              {proMode ? 'ON' : 'OFF'}
            </span>
          </button>

          {messages.length > 0 && (
            <button
              className="sidebar-action-btn share-chat-btn"
              onClick={handleShareChat}
              title="Share this chat"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                <circle cx="18" cy="5" r="3" stroke="currentColor" strokeWidth="2"/>
                <circle cx="6" cy="12" r="3" stroke="currentColor" strokeWidth="2"/>
                <circle cx="18" cy="19" r="3" stroke="currentColor" strokeWidth="2"/>
                <line x1="8.59" y1="13.51" x2="15.42" y2="17.49" stroke="currentColor" strokeWidth="2"/>
                <line x1="15.41" y1="6.51" x2="8.59" y2="10.49" stroke="currentColor" strokeWidth="2"/>
              </svg>
              <span>Share Chat</span>
            </button>
          )}

          <button
            className="sidebar-action-btn feedback-btn"
            onClick={() => setFeedbackModalOpen(true)}
            title="Report Bug / Request Feature"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
              <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              <line x1="12" y1="8" x2="12" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
              <line x1="12" y1="16" x2="12.01" y2="16" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
            </svg>
            <span>Feedback</span>
          </button>

          <div className="search-chats">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
              <circle cx="11" cy="11" r="8" stroke="currentColor" strokeWidth="2"/>
              <path d="M21 21L16.65 16.65" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/>
            </svg>
            <input
              type="text"
              placeholder="Search chats"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
            />
          </div>
        </div>

        <div className="sidebar-content">
          {/* Show history for both authenticated and anonymous users */}
          {history.length > 0 ? (
            <div className="sidebar-history">
              <div className="history-header">
                <h3>Chats</h3>
                <button
                  className="clear-history-btn"
                  onClick={handleClearHistory}
                  title="Clear all history"
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                    <path d="M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </button>
              </div>
              <div className="history-list">
                {filteredHistory.map((item) => (
                  <button
                    key={item.id}
                    className="history-item"
                    onClick={() => loadHistoryItem(item)}
                  >
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
                      <path d="M21 15C21 15.5304 20.7893 16.0391 20.4142 16.4142C20.0391 16.7893 19.5304 17 19 17H7L3 21V5C3 4.46957 3.21071 3.96086 3.58579 3.58579C3.96086 3.21071 4.46957 3 5 3H19C19.5304 3 20.0391 3.21071 20.4142 3.58579C20.7893 3.96086 21 4.46957 21 5V15Z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                    </svg>
                    <div className="history-query">{item.query}</div>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            !isAuthenticated && messages.length === 0 && (
              <div className="sidebar-examples">
                <h3>Example Queries</h3>
                {EXAMPLE_QUERIES.map((ex, i) => (
                  <button
                    key={i}
                    className="example-btn-sidebar"
                    onClick={() => handleExampleClick(ex)}
                  >
                    {ex}
                  </button>
                ))}
              </div>
            )
          )}
        </div>

        <div className="sidebar-footer">
          {isAuthenticated ? (
            <div className="user-profile">
              <div className="user-avatar">
                {user?.name?.charAt(0).toUpperCase()}
              </div>
              <div className="user-details">
                <div className="user-name">{user?.name}</div>
                <div className="user-email">{user?.email}</div>
              </div>
              <button className="logout-icon-btn" onClick={logout} title="Logout">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                  <path d="M9 21H5C4.46957 21 3.96086 20.7893 3.58579 20.4142C3.21071 20.0391 3 19.5304 3 19V5C3 4.46957 3.21071 3.96086 3.58579 3.58579C3.96086 3.21071 4.46957 3 5 3H9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                  <path d="M16 17L21 12L16 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                  <path d="M21 12H9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </button>
            </div>
          ) : (
            <button className="login-footer-btn" onClick={() => setShowAuthModal(true)}>
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
                <path d="M20 21V19C20 17.9391 19.5786 16.9217 18.8284 16.1716C18.0783 15.4214 17.0609 15 16 15H8C6.93913 15 5.92172 15.4214 5.17157 16.1716C4.42143 16.9217 4 17.9391 4 19V21" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                <circle cx="12" cy="7" r="4" stroke="currentColor" strokeWidth="2"/>
              </svg>
              <span>Login / Register</span>
            </button>
          )}
        </div>
      </div>

      <main className="chat-main">
        <div className="chat-container">
          <div className="messages-area">
            {messages.length === 0 && (
              <div className="welcome-screen">
                <h1 className="welcome-title">What can I help with?</h1>
                <p className="welcome-subtitle">Ask about economic data in natural language</p>
              </div>
            )}

            {messages.map((msg, i) => (
              <div key={i} className={`message-bubble ${msg.role} ${msg.isProMode ? 'pro-mode' : ''}`}>
                {/* Hide message content for standard queries with data - user only sees progress + chart */}
                {/* Show content for: user messages, errors, clarifications, Pro Mode, or messages without data */}
                {(msg.role === 'user' || msg.isProMode || !msg.data || msg.data.length === 0) && msg.content && (
                  <div className="bubble-content">{msg.content}</div>
                )}

                {msg.processingSteps && msg.processingSteps.length > 0 && (
                  <ProcessingSteps steps={mapProcessingStepsToTimeline(msg.processingSteps)} />
                )}

                {/* Show code execution if Pro Mode */}
                {msg.codeExecution && (
                  <CodeExecutionDisplay codeExecution={msg.codeExecution} />
                )}

                {/* Show visualization if this message has data */}
                {msg.data && msg.data.length > 0 && (
                  <MessageChart
                    data={msg.data}
                    chartType={msg.chartType || 'line'}
                    onChartTypeChange={(newChartType) => handleChartTypeChange(i, newChartType)}
                    onExport={(format) => handleExport(msg.data!, format)}
                    onShare={() => handleShareQuery(i)}
                  />
                )}
              </div>
            ))}
            {processingQuery.current !== null && (
              <div className="message-bubble assistant">
                <div className="bubble-content">
                  <div className="loading-status">{loadingStatus || 'Processing...'}</div>
                  {activeProcessingSteps.length > 0 && (
                    <ProcessingSteps steps={activeProcessingSteps} isPending />
                  )}
                  <div className="typing">
                    <span className="typing-dot"></span>
                    <span className="typing-dot"></span>
                    <span className="typing-dot"></span>
                  </div>
                </div>
              </div>
            )}
            <div ref={messagesEndRef} />
          </div>

          <div className="input-area">
            <form onSubmit={handleSubmit} className="input-form">
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={proMode ? "Ask for custom analysis with code execution..." : "Ask about economic data..."}
                className="query-input"
                disabled={processingQuery.current !== null}
                autoFocus
              />
              <button
                type="submit"
                className="submit-button"
                disabled={processingQuery.current !== null || !query.trim()}
              >
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
                  <path d="M7 11L12 6L17 11M12 18V7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </button>
            </form>
          </div>
        </div>
      </main>

      {showAuthModal && <Auth onClose={() => setShowAuthModal(false)} />}

      {/* Share Modal */}
      <ShareModal
        isOpen={shareModal.isOpen}
        onClose={() => setShareModal({ isOpen: false })}
        messages={messages}
        singleQuery={shareModal.singleQuery}
      />

      {/* Feedback Modal */}
      <FeedbackModal
        isOpen={feedbackModalOpen}
        onClose={() => setFeedbackModalOpen(false)}
        messages={messages}
        conversationId={conversationId}
      />

      {/* Python Code Export Modal */}
      {pythonCodeModal.show && (
        <div className="python-code-modal-overlay" onClick={() => setPythonCodeModal({ show: false, code: '', loading: false, copied: false })}>
          <div className="python-code-modal" onClick={(e) => e.stopPropagation()}>
            <div className="python-code-modal-header">
              <h3>🐍 Python Code</h3>
              <button
                className="python-code-modal-close"
                onClick={() => setPythonCodeModal({ show: false, code: '', loading: false, copied: false })}
              >
                ×
              </button>
            </div>
            <div className="python-code-modal-content">
              {pythonCodeModal.loading ? (
                <div className="python-code-loading">
                  <div className="loading-spinner"></div>
                  <p>Generating Python code...</p>
                </div>
              ) : pythonCodeModal.error ? (
                <div className="python-code-error">
                  <p>Error: {pythonCodeModal.error}</p>
                </div>
              ) : (
                <>
                  <div className="python-code-instructions">
                    <p>Click the code to copy, save as <code>.py</code> file, then run with <code>python filename.py</code></p>
                  </div>
                  <div
                    className={`python-code-block-wrapper ${pythonCodeModal.copied ? 'copied' : ''}`}
                    onClick={() => {
                      navigator.clipboard.writeText(pythonCodeModal.code)
                      setPythonCodeModal(prev => ({ ...prev, copied: true }))
                      setTimeout(() => {
                        setPythonCodeModal(prev => ({ ...prev, copied: false }))
                      }, 2000)
                    }}
                    title="Click to copy code"
                  >
                    <div className="python-code-copy-overlay">
                      {pythonCodeModal.copied ? '✓ Copied!' : '📋 Click to copy'}
                    </div>
                    <pre className="python-code-block">
                      <code>{pythonCodeModal.code}</code>
                    </pre>
                  </div>
                </>
              )}
            </div>
            {!pythonCodeModal.loading && !pythonCodeModal.error && pythonCodeModal.code && (
              <div className="python-code-modal-footer">
                <button
                  className="python-code-download-btn"
                  onClick={() => {
                    const blob = new Blob([pythonCodeModal.code], { type: 'text/plain' })
                    const url = URL.createObjectURL(blob)
                    const a = document.createElement('a')
                    a.href = url
                    a.download = 'fetch_data.py'
                    a.click()
                    URL.revokeObjectURL(url)
                  }}
                >
                  Download .py
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
