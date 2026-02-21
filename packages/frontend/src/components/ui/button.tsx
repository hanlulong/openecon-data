import type { ButtonHTMLAttributes } from 'react'

import { cn } from '@/lib/utils'

type ButtonVariant = 'default' | 'outline' | 'ghost'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
}

export function Button({ className, variant = 'default', ...props }: ButtonProps) {
  const base = 'px-4 py-2 text-sm font-medium rounded-md transition'
  const variants: Record<ButtonVariant, string> = {
    default: 'bg-indigo-600 text-white hover:bg-indigo-700',
    outline: 'border border-gray-300 hover:bg-gray-50',
    ghost: 'hover:bg-gray-100',
  }

  return <button className={cn(base, variants[variant], className)} {...props} />
}

