import { useState } from 'react'
import type { CodeExecutionResult } from '../types'
import { logger } from '../utils/logger'
import './CodeExecutionDisplay.css'

interface CodeExecutionDisplayProps {
  codeExecution: CodeExecutionResult
}

/**
 * Allowlist for code-execution media URLs. Pro Mode generates file URLs
 * server-side; if a malicious or buggy generator emitted javascript:/data:
 * URLs, rendering them in an <img src> or <a href> would execute script in
 * the page origin (XSS). Only allow same-origin relative paths and explicit
 * http(s) URLs. Returns null when the URL should be suppressed.
 */
const isSafeMediaUrl = (raw: string): boolean => {
  if (!raw) return false
  const trimmed = raw.trim()
  if (trimmed.startsWith('/')) return !trimmed.startsWith('//')  // relative same-origin, reject protocol-relative
  try {
    const parsed = new URL(trimmed, window.location.origin)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:'
  } catch {
    return false
  }
}

export function CodeExecutionDisplay({ codeExecution }: CodeExecutionDisplayProps) {
  const [showCode, setShowCode] = useState(true)

  const copyCode = () => {
    if (!navigator?.clipboard) {
      logger.warn('Clipboard API not available')
      return
    }
    navigator.clipboard.writeText(codeExecution.code).catch((err) => logger.error('Failed to copy code', err))
  }

  return (
    <div className="code-execution-display">
      <div className="code-section">
        <div className="code-header">
          <button
            type="button"
            className="code-toggle"
            onClick={() => setShowCode(!showCode)}
            aria-label={showCode ? 'Hide Python code' : 'Show Python code'}
            aria-expanded={showCode}
          >
            <span className="toggle-icon" aria-hidden="true">{showCode ? '▼' : '▶'}</span>
            <span className="toggle-label">Generated Python Code</span>
            {codeExecution.executionTime && (
              <span className="execution-time">
                {codeExecution.executionTime.toFixed(2)}s
              </span>
            )}
          </button>
          <button
            type="button"
            className="copy-code-btn"
            onClick={copyCode}
            aria-label="Copy code to clipboard"
          >
            📋 Copy
          </button>
        </div>
        {showCode && (
          <pre className="code-block">
            <code>{codeExecution.code}</code>
          </pre>
        )}
      </div>

      {codeExecution.error ? (
        <div className="execution-error">
          <div className="error-header">
            <span className="error-icon">⚠️</span>
            <span className="error-title">Execution Error</span>
          </div>
          <pre className="error-content">{codeExecution.error}</pre>
        </div>
      ) : (
        <div className="execution-output">
          <div className="output-header">
            <span className="output-icon">✓</span>
            <span className="output-title">Output</span>
          </div>
          <pre className="output-content">{codeExecution.output || '(no output)'}</pre>
        </div>
      )}

      {codeExecution.files && codeExecution.files.length > 0 && (
        <div className="generated-files">
          <div className="files-header">
            <span className="files-icon">📁</span>
            <span className="files-title">Generated Files</span>
          </div>
          <div className="files-content">
            {codeExecution.files.map((file, index) => {
              if (!isSafeMediaUrl(file.url)) {
                logger.warn('Suppressed unsafe code-execution media URL', file.url)
                return null
              }
              const isImage = file.type === 'image' || file.url.match(/\.(png|jpg|jpeg|gif|svg)$/i)

              return (
                <div key={index} className="file-item">
                  {isImage ? (
                    <div className="file-image-container">
                      <img src={file.url} alt={file.name} className="file-image" />
                    </div>
                  ) : null}
                  <div className="file-download">
                    <a href={file.url} download={file.name} className="download-btn">
                      ⬇️ Download {file.name}
                    </a>
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
