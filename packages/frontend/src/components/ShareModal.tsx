import { useState, useEffect, useCallback } from 'react'
import type { Message, NormalizedData } from '../types'
import { logger } from '../utils/logger'
import './ShareModal.css'

interface ShareModalProps {
  isOpen: boolean
  onClose: () => void
  messages: Message[]
  singleQuery?: {
    query: string
    data?: NormalizedData[]
    messageIndex: number
  }
}

export function ShareModal({ isOpen, onClose, messages, singleQuery }: ShareModalProps) {
  const [copied, setCopied] = useState(false)
  const [shareUrl, setShareUrl] = useState('')
  const [shareText, setShareText] = useState('')

  // Generate share content
  useEffect(() => {
    if (!isOpen) return

    const baseUrl = window.location.origin

    if (singleQuery) {
      // Share single query
      const query = encodeURIComponent(singleQuery.query)
      const url = `${baseUrl}/chat?query=${query}`
      setShareUrl(url)

      // Generate share text
      let text = `Check out this economic data query on openecon.ai:\n\n"${singleQuery.query}"`
      if (singleQuery.data && singleQuery.data.length > 0) {
        const sources = [...new Set(singleQuery.data.map(d => d.metadata.source))]
        text += `\n\nData from: ${sources.join(', ')}`
      }
      setShareText(text)
    } else {
      // Share whole chat
      const userQueries = messages
        .filter(m => m.role === 'user')
        .map(m => m.content)

      if (userQueries.length > 0) {
        // Use the first query for the URL
        const firstQuery = encodeURIComponent(userQueries[0])
        const url = `${baseUrl}/chat?query=${firstQuery}`
        setShareUrl(url)

        // Generate share text with all queries
        let text = `Check out my economic data analysis on openecon.ai:\n`
        userQueries.slice(0, 3).forEach((q, i) => {
          text += `\n${i + 1}. "${q}"`
        })
        if (userQueries.length > 3) {
          text += `\n...and ${userQueries.length - 3} more queries`
        }
        setShareText(text)
      }
    }
  }, [isOpen, messages, singleQuery])

  const handleCopyLink = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(shareUrl)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch (err) {
      logger.error('Failed to copy link:', err)
    }
  }, [shareUrl])

  const handleCopyText = useCallback(async () => {
    try {
      const textWithLink = `${shareText}\n\n${shareUrl}`
      await navigator.clipboard.writeText(textWithLink)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch (err) {
      logger.error('Failed to copy text:', err)
    }
  }, [shareText, shareUrl])

  const handleNativeShare = useCallback(async () => {
    if (!navigator.share) {
      handleCopyText()
      return
    }

    try {
      await navigator.share({
        title: 'openecon.ai Data Query',
        text: shareText,
        url: shareUrl,
      })
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        logger.error('Share failed:', err)
      }
    }
  }, [shareText, shareUrl, handleCopyText])

  const handleSocialShare = useCallback((platform: 'twitter' | 'linkedin') => {
    const text = encodeURIComponent(shareText)
    const url = encodeURIComponent(shareUrl)

    let shareLink = ''
    if (platform === 'twitter') {
      shareLink = `https://twitter.com/intent/tweet?text=${text}&url=${url}`
    } else if (platform === 'linkedin') {
      shareLink = `https://www.linkedin.com/sharing/share-offsite/?url=${url}`
    }

    window.open(shareLink, '_blank', 'width=600,height=400')
  }, [shareText, shareUrl])

  if (!isOpen) return null

  const title = singleQuery ? 'Share Query' : 'Share Chat'
  const subtitle = singleQuery
    ? `"${singleQuery.query.substring(0, 50)}${singleQuery.query.length > 50 ? '...' : ''}"`
    : `${messages.filter(m => m.role === 'user').length} queries in this chat`

  return (
    <div className="share-modal-overlay" onClick={onClose}>
      <div className="share-modal" onClick={(e) => e.stopPropagation()}>
        <div className="share-modal-header">
          <div className="share-modal-title-group">
            <h3>{title}</h3>
            <span className="share-modal-subtitle">{subtitle}</span>
          </div>
          <button className="share-modal-close" onClick={onClose} aria-label="Close">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        </div>

        <div className="share-modal-content">
          {/* Share Link Section */}
          <div className="share-link-section">
            <label>Share link</label>
            <div className="share-link-input-group">
              <input
                type="text"
                value={shareUrl}
                readOnly
                className="share-link-input"
              />
              <button
                className={`share-copy-btn ${copied ? 'copied' : ''}`}
                onClick={handleCopyLink}
              >
                {copied ? (
                  <>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <polyline points="20 6 9 17 4 12"></polyline>
                    </svg>
                    Copied!
                  </>
                ) : (
                  <>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
                      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
                    </svg>
                    Copy
                  </>
                )}
              </button>
            </div>
          </div>

          {/* Share Options */}
          <div className="share-options">
            <label>Share via</label>
            <div className="share-buttons-grid">
              {/* Native Share (mobile-friendly) */}
              {typeof navigator !== 'undefined' && typeof navigator.share === 'function' && (
                <button className="share-option-btn share-native" onClick={handleNativeShare}>
                  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <circle cx="18" cy="5" r="3"></circle>
                    <circle cx="6" cy="12" r="3"></circle>
                    <circle cx="18" cy="19" r="3"></circle>
                    <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"></line>
                    <line x1="15.41" y1="6.51" x2="8.59" y2="10.49"></line>
                  </svg>
                  <span>Share</span>
                </button>
              )}

              {/* Twitter/X */}
              <button className="share-option-btn share-twitter" onClick={() => handleSocialShare('twitter')}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z"/>
                </svg>
                <span>X / Twitter</span>
              </button>

              {/* LinkedIn */}
              <button className="share-option-btn share-linkedin" onClick={() => handleSocialShare('linkedin')}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433c-1.144 0-2.063-.926-2.063-2.065 0-1.138.92-2.063 2.063-2.063 1.14 0 2.064.925 2.064 2.063 0 1.139-.925 2.065-2.064 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/>
                </svg>
                <span>LinkedIn</span>
              </button>

              {/* Copy with Text */}
              <button className="share-option-btn share-copy-text" onClick={handleCopyText}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path>
                  <rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect>
                </svg>
                <span>Copy Text</span>
              </button>
            </div>
          </div>

          {/* Preview */}
          <div className="share-preview">
            <label>Preview</label>
            <div className="share-preview-box">
              <pre>{shareText}</pre>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
