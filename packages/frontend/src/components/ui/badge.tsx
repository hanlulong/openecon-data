import type { PropsWithChildren } from 'react'

import { cn } from '@/lib/utils'

type BadgeVariant = 'default' | 'secondary'

interface BadgeProps extends PropsWithChildren {
  className?: string
  variant?: BadgeVariant
}

export function Badge({ className, variant = 'default', children }: BadgeProps) {
  const variants: Record<BadgeVariant, string> = {
    default: 'bg-indigo-100 text-indigo-700',
    secondary: 'bg-gray-100 text-gray-700',
  }

  return <span className={cn('text-xs px-2 py-1 rounded-md', variants[variant], className)}>{children}</span>
}

