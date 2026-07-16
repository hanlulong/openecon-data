import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'

const SITE_URL = 'https://data.openecon.ai'

const DEFAULT_META = {
  title:
    'OpenEcon.ai - AI Economic Data Platform | Query FRED, World Bank, IMF & 10+ Sources',
  description:
    'Query economic data using natural language. Access FRED, World Bank, IMF, UN Comtrade, Statistics Canada, Eurostat, OECD, BIS data. Get instant charts, export CSV/JSON, and analyze global economic indicators with AI.',
}

const ROUTE_META: Record<string, { title: string; description: string }> = {
  '/': DEFAULT_META,
  '/chat': {
    title: 'AI Chat — Query Economic Data in Plain English | OpenEcon.ai',
    description:
      'Chat with OpenEcon.ai to pull economic data from FRED, World Bank, IMF, Eurostat and more. Ask in plain English, get instant charts, and export CSV or JSON.',
  },
  '/docs': {
    title: 'Documentation — API & MCP Server | OpenEcon.ai',
    description:
      'OpenEcon.ai documentation: query the economic data assistant, connect the MCP server over SSE, and export normalized results from FRED, World Bank, IMF and 10+ sources.',
  },
  '/examples': {
    title: 'Example Queries — FRED, World Bank, IMF Data | OpenEcon.ai',
    description:
      'Browse example natural-language queries for GDP, inflation, unemployment, trade and exchange rates across FRED, World Bank, IMF, Eurostat and Statistics Canada.',
  },
}

function setMetaTag(selector: string, content: string) {
  const el = document.head.querySelector<HTMLMetaElement>(selector)
  if (el) el.setAttribute('content', content)
}

function setCanonical(href: string) {
  let link = document.head.querySelector<HTMLLinkElement>('link[rel="canonical"]')
  if (!link) {
    link = document.createElement('link')
    link.setAttribute('rel', 'canonical')
    document.head.appendChild(link)
  }
  link.setAttribute('href', href)
}

/**
 * Single source of truth for per-route <title>, meta description, canonical URL,
 * and the matching Open Graph / Twitter fields. Renders nothing; applies the
 * tags on client-side navigation so JS-rendering crawlers see route-specific
 * metadata. The static index.html carries the same defaults for "/".
 */
export function RouteMeta() {
  const { pathname } = useLocation()

  useEffect(() => {
    const meta = ROUTE_META[pathname] ?? DEFAULT_META
    const canonical = pathname === '/' ? `${SITE_URL}/` : `${SITE_URL}${pathname}`

    document.title = meta.title
    setMetaTag('meta[name="description"]', meta.description)
    setCanonical(canonical)

    setMetaTag('meta[property="og:title"]', meta.title)
    setMetaTag('meta[property="og:description"]', meta.description)
    setMetaTag('meta[property="og:url"]', canonical)
    setMetaTag('meta[name="twitter:title"]', meta.title)
    setMetaTag('meta[name="twitter:description"]', meta.description)
    setMetaTag('meta[name="twitter:url"]', canonical)
  }, [pathname])

  return null
}
