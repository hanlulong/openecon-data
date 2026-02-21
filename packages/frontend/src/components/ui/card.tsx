import type { HTMLAttributes, PropsWithChildren } from 'react'

import { cn } from '@/lib/utils'

type DivProps = HTMLAttributes<HTMLDivElement>

export function Card({ className, children, ...props }: PropsWithChildren<DivProps>) {
  return (
    <div className={cn('bg-white border rounded-xl', className)} {...props}>
      {children}
    </div>
  )
}

export function CardHeader({ className, children, ...props }: PropsWithChildren<DivProps>) {
  return (
    <div className={cn('border-b p-4', className)} {...props}>
      {children}
    </div>
  )
}

export function CardTitle({ className, children, ...props }: PropsWithChildren<DivProps>) {
  return (
    <h3 className={cn('font-semibold text-lg', className)} {...props}>
      {children}
    </h3>
  )
}

export function CardDescription({ className, children, ...props }: PropsWithChildren<DivProps>) {
  return (
    <p className={cn('text-sm text-gray-600', className)} {...props}>
      {children}
    </p>
  )
}

export function CardContent({ className, children, ...props }: PropsWithChildren<DivProps>) {
  return (
    <div className={cn('p-4', className)} {...props}>
      {children}
    </div>
  )
}

export function CardFooter({ className, children, ...props }: PropsWithChildren<DivProps>) {
  return (
    <div className={cn('border-t p-4 text-sm text-gray-500', className)} {...props}>
      {children}
    </div>
  )
}

