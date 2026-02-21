import { useState, useEffect } from 'react'
import type { Message } from '../types'
import { api } from '../services/api'
import { logger } from '../utils/logger'
import { useAuth } from '../contexts/AuthContext'
import './FeedbackModal.css'

interface FeedbackModalProps {
  isOpen: boolean
  onClose: () => void
  messages: Message[]
  conversationId?: string
}

interface SessionInfo {
  url: string
  userAgent: string
  timestamp: string
  screenSize: string
  language: string
  timezone: string
  referrer: string
}

type FeedbackType = 'bug' | 'feature' | 'other'

export function FeedbackModal({ isOpen, onClose, messages, conversationId }: FeedbackModalProps) {
  const [feedbackType, setFeedbackType] = useState<FeedbackType>('bug')
  const [message, setMessage] = useState('')
  const [email, setEmail] = useState('')
  const [includeSession, setIncludeSession] = useState(true)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [submitted, setSubmitted] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { user } = useAuth()

  // Reset state when modal opens
  useEffect(() => {
    if (isOpen) {
      setMessage('')
      setFeedbackType('bug')
      setIncludeSession(true)
      setSubmitted(false)
      setError(null)
      // Pre-fill email if user is logged in
      if (user?.email) {
        setEmail(user.email)
      }
    }
  }, [isOpen, user])

  const collectSessionInfo = (): SessionInfo => {
    return {
      url: window.location.href,
      userAgent: navigator.userAgent,
      timestamp: new Date().toISOString(),
      screenSize: `${window.innerWidth}x${window.innerHeight}`,
      language: navigator.language,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      referrer: document.referrer || 'direct',
    }
  }

  const formatMessagesForFeedback = (msgs: Message[]): string => {
    return msgs.map((msg) => {
      const role = msg.role === 'user' ? 'User' : 'Assistant'
      const time = msg.timestamp.toLocaleString()
      let content = `[${role}] (${time}):\n${msg.content}`

      if (msg.data && msg.data.length > 0) {
        content += `\n[Data: ${msg.data.length} series - ${msg.data.map(d => d.metadata.indicator).join(', ')}]`
      }

      if (msg.codeExecution) {
        content += `\n[Code Execution: ${msg.codeExecution.error ? 'Error' : 'Success'}]`
      }

      return content
    }).join('\n\n---\n\n')
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError(null)
    setIsSubmitting(true)

    try {
      const sessionInfo = collectSessionInfo()
      const conversationData = includeSession ? {
        messages: formatMessagesForFeedback(messages),
        messageCount: messages.length,
        conversationId,
        rawMessages: messages.map(m => ({
          role: m.role,
          content: m.content,
          timestamp: m.timestamp.toISOString(),
          hasData: !!m.data,
          dataCount: m.data?.length || 0,
          isProMode: m.isProMode,
        })),
      } : null

      await api.submitFeedback({
        type: feedbackType,
        message: message.trim(),
        email: email.trim() || undefined,
        sessionInfo: includeSession ? sessionInfo : undefined,
        conversation: conversationData,
        userId: user?.id,
        userName: user?.name,
      })

      setSubmitted(true)
      // Auto-close after success
      setTimeout(() => {
        onClose()
      }, 2000)
    } catch (err: any) {
      logger.error('Failed to submit feedback:', err)
      setError(err.response?.data?.message || err.message || 'Failed to submit feedback. Please try again.')
    } finally {
      setIsSubmitting(false)
    }
  }

  if (!isOpen) return null

  return (
    <div className="feedback-modal-overlay" onClick={onClose}>
      <div className="feedback-modal" onClick={(e) => e.stopPropagation()}>
        <div className="feedback-modal-header">
          <div className="feedback-modal-title-group">
            <h3>Report Bug / Request Feature</h3>
            <span className="feedback-modal-subtitle">Help us improve openecon.ai</span>
          </div>
          <button className="feedback-modal-close" onClick={onClose} aria-label="Close">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        </div>

        {submitted ? (
          <div className="feedback-modal-success">
            <div className="feedback-success-icon">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path>
                <polyline points="22 4 12 14.01 9 11.01"></polyline>
              </svg>
            </div>
            <h4>Thank you for your feedback!</h4>
            <p>We'll review it and get back to you if needed.</p>
          </div>
        ) : (
          <form onSubmit={handleSubmit} className="feedback-modal-content">
            {/* Feedback Type */}
            <div className="feedback-type-section">
              <label>What type of feedback?</label>
              <div className="feedback-type-buttons">
                <button
                  type="button"
                  className={`feedback-type-btn ${feedbackType === 'bug' ? 'active' : ''}`}
                  onClick={() => setFeedbackType('bug')}
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <circle cx="12" cy="12" r="10"></circle>
                    <line x1="12" y1="8" x2="12" y2="12"></line>
                    <line x1="12" y1="16" x2="12.01" y2="16"></line>
                  </svg>
                  Bug Report
                </button>
                <button
                  type="button"
                  className={`feedback-type-btn ${feedbackType === 'feature' ? 'active' : ''}`}
                  onClick={() => setFeedbackType('feature')}
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon>
                  </svg>
                  Feature Request
                </button>
                <button
                  type="button"
                  className={`feedback-type-btn ${feedbackType === 'other' ? 'active' : ''}`}
                  onClick={() => setFeedbackType('other')}
                >
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
                  </svg>
                  Other
                </button>
              </div>
            </div>

            {/* Message Input */}
            <div className="feedback-message-section">
              <label htmlFor="feedback-message">
                Your feedback <span className="optional">(optional)</span>
              </label>
              <textarea
                id="feedback-message"
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                placeholder={
                  feedbackType === 'bug'
                    ? "Describe the bug you encountered... What did you expect to happen?"
                    : feedbackType === 'feature'
                    ? "Describe the feature you'd like to see..."
                    : "Tell us what's on your mind..."
                }
                rows={4}
              />
            </div>

            {/* Email Input */}
            <div className="feedback-email-section">
              <label htmlFor="feedback-email">
                Your email <span className="optional">(optional, for follow-up)</span>
              </label>
              <input
                type="email"
                id="feedback-email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="your@email.com"
              />
            </div>

            {/* Include Session Data Checkbox */}
            <div className="feedback-session-section">
              <label className="feedback-checkbox-label">
                <input
                  type="checkbox"
                  checked={includeSession}
                  onChange={(e) => setIncludeSession(e.target.checked)}
                />
                <span className="checkbox-text">
                  Include session data
                  <span className="checkbox-hint">
                    (chat history, browser info - helps us debug issues)
                  </span>
                </span>
              </label>
            </div>

            {/* Session Preview */}
            {includeSession && messages.length > 0 && (
              <div className="feedback-session-preview">
                <label>Session info that will be included:</label>
                <div className="session-preview-box">
                  <div className="session-preview-item">
                    <span className="preview-label">Chat messages:</span>
                    <span className="preview-value">{messages.length} message(s)</span>
                  </div>
                  <div className="session-preview-item">
                    <span className="preview-label">Browser:</span>
                    <span className="preview-value">{navigator.userAgent.split(' ').slice(-2).join(' ')}</span>
                  </div>
                  <div className="session-preview-item">
                    <span className="preview-label">Screen:</span>
                    <span className="preview-value">{window.innerWidth}x{window.innerHeight}</span>
                  </div>
                </div>
              </div>
            )}

            {/* Error Message */}
            {error && (
              <div className="feedback-error">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <circle cx="12" cy="12" r="10"></circle>
                  <line x1="12" y1="8" x2="12" y2="12"></line>
                  <line x1="12" y1="16" x2="12.01" y2="16"></line>
                </svg>
                {error}
              </div>
            )}

            {/* Submit Button */}
            <div className="feedback-submit-section">
              <button
                type="submit"
                className="feedback-submit-btn"
                disabled={isSubmitting}
              >
                {isSubmitting ? (
                  <>
                    <span className="feedback-spinner"></span>
                    Sending...
                  </>
                ) : (
                  <>
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <line x1="22" y1="2" x2="11" y2="13"></line>
                      <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
                    </svg>
                    Send Feedback
                  </>
                )}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  )
}
