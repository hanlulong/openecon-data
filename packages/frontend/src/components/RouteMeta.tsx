import { useEffect } from 'react'
import { useLocation } from 'react-router-dom'

const SITE_URL = 'https://data.openecon.ai'

// Kept in sync with the static <meta name="robots"> in index.html so normal
// routes stay indexable after navigating away from a noindex route.
const INDEXABLE_ROBOTS =
  'index, follow, max-image-preview:large, max-snippet:-1, max-video-preview:-1'
const NOINDEX_ROBOTS = 'noindex, nofollow'

type PageMeta = {
  title: string
  description: string
  // Social fields fall back to title/description when omitted. The homepage
  // sets them explicitly so the JS-rendered og:/twitter: tags match the static
  // values in index.html exactly (no drift for non-JS vs JS crawlers).
  ogTitle?: string
  ogDescription?: string
  twitterDescription?: string
  noindex?: boolean
}

const DEFAULT_META: PageMeta = {
  title:
    'OpenEcon.ai - AI Economic Data Platform | Query FRED, World Bank, IMF & 10+ Sources',
  description:
    'Query economic data using natural language. Access FRED, World Bank, IMF, UN Comtrade, Statistics Canada, Eurostat, OECD, BIS data. Get instant charts, export CSV/JSON, and analyze global economic indicators with AI.',
  ogTitle: 'OpenEcon.ai - AI Economic Data Platform',
  ogDescription:
    'Query economic data from 10+ sources using natural language. Access FRED, World Bank, IMF, UN Comtrade, and more. Get instant charts and export data.',
  twitterDescription:
    'Query economic data from 10+ sources using natural language. Access FRED, World Bank, IMF, UN Comtrade, and more.',
}

const ROUTE_META: Record<string, PageMeta> = {
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
  // Transient auth flows: keep the default title but tell crawlers not to index.
  '/auth/callback': { ...DEFAULT_META, noindex: true },
  '/reset-password': { ...DEFAULT_META, noindex: true },
}

function setMetaTag(selector: string, content: string) {
  const el = document.head.querySelector<HTMLMetaElement>(selector)
  if (el) el.setAttribute('content', content)
}

function setRobots(content: string) {
  let el = document.head.querySelector<HTMLMetaElement>('meta[name="robots"]')
  if (!el) {
    el = document.createElement('meta')
    el.setAttribute('name', 'robots')
    document.head.appendChild(el)
  }
  el.setAttribute('content', content)
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
 * robots directive, and the matching Open Graph / Twitter fields. Renders
 * nothing; applies the tags on client-side navigation so JS-rendering crawlers
 * see route-specific metadata. The static index.html carries the same defaults
 * for "/".
 */
export function RouteMeta() {
  const { pathname } = useLocation()

  useEffect(() => {
    const meta = ROUTE_META[pathname] ?? DEFAULT_META
    const canonical = pathname === '/' ? `${SITE_URL}/` : `${SITE_URL}${pathname}`
    const ogTitle = meta.ogTitle ?? meta.title
    const ogDescription = meta.ogDescription ?? meta.description
    const twitterDescription = meta.twitterDescription ?? ogDescription

    document.title = meta.title
    setMetaTag('meta[name="description"]', meta.description)
    setCanonical(canonical)
    setRobots(meta.noindex ? NOINDEX_ROBOTS : INDEXABLE_ROBOTS)

    setMetaTag('meta[property="og:title"]', ogTitle)
    setMetaTag('meta[property="og:description"]', ogDescription)
    setMetaTag('meta[property="og:url"]', canonical)
    setMetaTag('meta[name="twitter:title"]', ogTitle)
    setMetaTag('meta[name="twitter:description"]', twitterDescription)
    setMetaTag('meta[name="twitter:url"]', canonical)
  }, [pathname])

  return null
}
