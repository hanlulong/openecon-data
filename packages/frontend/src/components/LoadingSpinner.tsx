import { memo } from 'react'

interface LoadingSpinnerProps {
  size?: 'sm' | 'md' | 'lg'
  message?: string
}

export const LoadingSpinner = memo(function LoadingSpinner({
  size = 'md',
  message
}: LoadingSpinnerProps) {
  const sizeClasses = {
    sm: 'h-4 w-4',
    md: 'h-8 w-8',
    lg: 'h-12 w-12',
  }

  return (
    <div className="flex flex-col items-center justify-center gap-2 p-4">
      <div className={`${sizeClasses[size]} animate-spin rounded-full border-2 border-gray-300 border-t-indigo-600`} />
      {message && <p className="text-sm text-gray-600">{message}</p>}
    </div>
  )
})
