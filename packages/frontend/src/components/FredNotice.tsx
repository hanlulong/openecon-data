// The FRED® API Terms of Use require this exact notice "prominently on your
// application", a link to the terms, and a statement that users agree to be
// bound by them: https://fred.stlouisfed.org/docs/api/terms_of_use.html
export const FRED_TERMS_URL = 'https://fred.stlouisfed.org/docs/api/terms_of_use.html'

interface FredNoticeProps {
  className?: string
  linkClassName?: string
}

export function FredNotice({ className = '', linkClassName = 'underline' }: FredNoticeProps) {
  return (
    <p className={className} data-testid="fred-notice">
      This product uses the FRED® API but is not endorsed or certified by the Federal Reserve Bank of St.
      Louis. By using FRED® data on this site, you agree to be bound by the{' '}
      <a href={FRED_TERMS_URL} target="_blank" rel="noopener noreferrer" className={linkClassName}>
        FRED® API Terms of Use
      </a>
      .
    </p>
  )
}
