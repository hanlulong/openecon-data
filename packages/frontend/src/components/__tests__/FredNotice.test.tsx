import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { FredNotice, FRED_TERMS_URL } from '../FredNotice'

describe('FredNotice', () => {
  it('shows the exact notice the FRED API terms require', () => {
    render(<FredNotice />)
    const notice = screen.getByTestId('fred-notice')
    const text = notice.textContent?.replace(/\s+/g, ' ')
    expect(text).toContain(
      'This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.',
    )
    expect(text).toContain(
      'By using FRED® data on this site, you agree to be bound by the FRED® API Terms of Use.',
    )
  })

  it('links to the FRED API terms of use in a new tab', () => {
    render(<FredNotice />)
    const link = screen.getByRole('link', { name: 'FRED® API Terms of Use' })
    expect(link).toHaveAttribute('href', FRED_TERMS_URL)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })
})
