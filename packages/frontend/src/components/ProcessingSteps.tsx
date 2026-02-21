import { memo } from 'react'

export type ProcessingTimelineStep = {
  step: string
  description: string
  status: 'pending' | 'in-progress' | 'completed'
  durationMs?: number
}

interface ProcessingStepsProps {
  steps: ProcessingTimelineStep[]
  isPending?: boolean
}

function formatDuration(durationMs?: number): string | null {
  if (durationMs === undefined) return null
  if (durationMs < 1000) {
    return `${Math.round(durationMs)}ms`
  }
  const seconds = durationMs / 1000
  if (seconds < 10) {
    return `${seconds.toFixed(1)}s`
  }
  if (seconds < 60) {
    return `${Math.round(seconds)}s`
  }
  const minutes = seconds / 60
  return `${minutes.toFixed(1)}m`
}

export const ProcessingSteps = memo(function ProcessingSteps({ steps, isPending = false }: ProcessingStepsProps) {
  if (!steps || steps.length === 0) {
    return null
  }

  return (
    <div className={`processing-steps ${isPending ? 'processing-steps--pending' : ''}`}>
      {steps.map((step, index) => {
        const durationLabel = !isPending ? formatDuration(step.durationMs) : null

        return (
          <div key={`${step.step}-${index}`} className={`processing-step processing-step--${step.status}`}>
            <div className="processing-step-indicator">
              <span className="processing-step-dot" />
              {index < steps.length - 1 && <span className="processing-step-connector" />}
            </div>
            <div className="processing-step-body">
              <div className="processing-step-text">
                <span className="processing-step-description">{step.description}</span>
                {durationLabel && (
                  <span className="processing-step-duration">{durationLabel}</span>
                )}
              </div>
            </div>
          </div>
        )
      })}
    </div>
  )
})

