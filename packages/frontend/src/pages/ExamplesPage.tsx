import { Link } from 'react-router-dom'
import { useEffect } from 'react'

const LIVE_DATA_APP_URL = 'https://data.openecon.io/chat'

interface ExampleCategory {
  title: string
  description: string
  examples: {
    query: string
    description: string
    source: string
  }[]
}

const exampleCategories: ExampleCategory[] = [
  {
    title: 'GDP & Economic Growth',
    description: 'Gross Domestic Product and economic growth indicators for countries worldwide.',
    examples: [
      { query: 'US GDP growth rate last 10 years', description: 'Track US economic growth over the past decade', source: 'FRED' },
      { query: 'China GDP growth vs India GDP growth', description: 'Compare economic growth between major emerging economies', source: 'World Bank' },
      { query: 'G7 countries GDP comparison', description: 'Compare GDP across the seven largest advanced economies', source: 'IMF' },
      { query: 'EU GDP per capita by country', description: 'GDP per capita across European Union member states', source: 'Eurostat' },
    ]
  },
  {
    title: 'Inflation & Consumer Prices',
    description: 'Consumer Price Index (CPI), inflation rates, and price level data.',
    examples: [
      { query: 'US inflation rate history', description: 'Historical US inflation measured by CPI', source: 'FRED' },
      { query: 'Compare inflation US UK Japan Germany', description: 'Multi-country inflation comparison', source: 'IMF' },
      { query: 'Canada CPI inflation monthly', description: 'Canadian Consumer Price Index data', source: 'Statistics Canada' },
      { query: 'Eurozone inflation rate', description: 'Inflation across the Euro area', source: 'Eurostat' },
    ]
  },
  {
    title: 'Employment & Labor Market',
    description: 'Unemployment rates, labor force participation, and employment statistics.',
    examples: [
      { query: 'US unemployment rate last 20 years', description: 'Long-term US unemployment trends', source: 'FRED' },
      { query: 'OECD unemployment rates by country', description: 'Compare jobless rates across developed nations', source: 'OECD' },
      { query: 'Canada employment rate', description: 'Canadian labor market statistics', source: 'Statistics Canada' },
      { query: 'Youth unemployment EU countries', description: 'Youth jobless rates in Europe', source: 'Eurostat' },
    ]
  },
  {
    title: 'International Trade',
    description: 'Import/export data, trade balances, and bilateral trade flows.',
    examples: [
      { query: 'US total exports 2020-2024', description: 'US export values over recent years', source: 'UN Comtrade' },
      { query: 'China imports from United States', description: 'US-China bilateral trade data', source: 'UN Comtrade' },
      { query: 'Germany trade balance history', description: 'German exports minus imports over time', source: 'UN Comtrade' },
      { query: 'Top 10 US trading partners', description: 'Countries with highest trade volume with US', source: 'UN Comtrade' },
    ]
  },
  {
    title: 'Interest Rates & Monetary Policy',
    description: 'Central bank rates, treasury yields, and monetary policy indicators.',
    examples: [
      { query: 'Federal funds rate history', description: 'US Federal Reserve policy rate', source: 'FRED' },
      { query: 'US 10 year treasury yield', description: 'Long-term government bond yields', source: 'FRED' },
      { query: 'Central bank policy rates comparison', description: 'Compare rates across major central banks', source: 'BIS' },
      { query: 'ECB interest rate history', description: 'European Central Bank rates', source: 'BIS' },
    ]
  },
  {
    title: 'Currency & Exchange Rates',
    description: 'Foreign exchange rates and currency data.',
    examples: [
      { query: 'EUR to USD exchange rate last year', description: 'Euro to US Dollar historical rates', source: 'ExchangeRate-API' },
      { query: 'Japanese Yen exchange rate history', description: 'JPY exchange rate trends', source: 'ExchangeRate-API' },
      { query: 'British Pound vs Euro', description: 'GBP/EUR exchange rate comparison', source: 'ExchangeRate-API' },
      { query: 'Emerging market currency rates', description: 'Exchange rates for developing economies', source: 'ExchangeRate-API' },
    ]
  },
  {
    title: 'Cryptocurrency',
    description: 'Bitcoin, Ethereum, and other cryptocurrency prices and market data.',
    examples: [
      { query: 'Bitcoin price last 2 years', description: 'BTC price history', source: 'CoinGecko' },
      { query: 'Ethereum price history', description: 'ETH price trends', source: 'CoinGecko' },
      { query: 'Top 10 cryptocurrencies by market cap', description: 'Largest crypto assets', source: 'CoinGecko' },
      { query: 'Bitcoin vs Ethereum price comparison', description: 'Compare BTC and ETH performance', source: 'CoinGecko' },
    ]
  },
  {
    title: 'Housing & Real Estate',
    description: 'Housing prices, starts, and real estate market indicators.',
    examples: [
      { query: 'US housing starts monthly', description: 'New residential construction data', source: 'FRED' },
      { query: 'Canada house price index', description: 'Canadian residential property prices', source: 'Statistics Canada' },
      { query: 'US median home price history', description: 'Historical US home prices', source: 'FRED' },
      { query: 'Mortgage rates 30 year fixed', description: 'US mortgage rate trends', source: 'FRED' },
    ]
  },
]

export function ExamplesPage() {
  useEffect(() => {
    document.title = 'Economic Data Query Examples | OpenEcon.ai'
  }, [])

  const buildQueryUrl = (query: string) => `${LIVE_DATA_APP_URL}?query=${encodeURIComponent(query)}`

  return (
    <div className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-white border-b">
        <div className="max-w-6xl mx-auto px-4 py-4 flex items-center justify-between">
          <Link to="/" className="text-xl font-bold text-blue-600">OpenEcon.ai</Link>
          <nav className="flex items-center gap-4">
            <a href={LIVE_DATA_APP_URL} className="text-gray-600 hover:text-gray-900">Chat</a>
            <Link to="/docs" className="text-gray-600 hover:text-gray-900">Docs</Link>
          </nav>
        </div>
      </header>

      {/* Hero */}
      <section className="bg-gradient-to-b from-blue-50 to-white py-12">
        <div className="max-w-6xl mx-auto px-4 text-center">
          <h1 className="text-3xl md:text-4xl font-bold text-gray-900 mb-4">
            Economic Data Query Examples
          </h1>
          <p className="text-lg text-gray-600 max-w-2xl mx-auto mb-6">
            Explore sample queries across 10+ data sources. Click any example to try it in our AI-powered chat interface.
          </p>
          <a
            href={LIVE_DATA_APP_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex mb-4 items-center rounded-full bg-blue-100 px-3 py-1 text-sm font-medium text-blue-700"
          >
            Live app: data.openecon.io/chat
          </a>
          <a
            href={LIVE_DATA_APP_URL}
            className="inline-flex items-center gap-2 bg-blue-600 text-white px-6 py-3 rounded-lg font-medium hover:bg-blue-700 transition-colors"
          >
            Try OpenEcon.ai Free
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
            </svg>
          </a>
        </div>
      </section>

      {/* Examples Grid */}
      <main className="max-w-6xl mx-auto px-4 py-12">
        <div className="space-y-12">
          {exampleCategories.map((category) => (
            <section key={category.title} id={category.title.toLowerCase().replace(/[^a-z0-9]+/g, '-')}>
              <h2 className="text-2xl font-bold text-gray-900 mb-2">{category.title}</h2>
              <p className="text-gray-600 mb-6">{category.description}</p>

              <div className="grid md:grid-cols-2 gap-4">
                {category.examples.map((example) => (
                  <a
                    key={example.query}
                    href={buildQueryUrl(example.query)}
                    className="block p-4 bg-white rounded-lg border border-gray-200 hover:border-blue-300 hover:shadow-md transition-all group"
                  >
                    <div className="flex items-start justify-between gap-2">
                      <div>
                        <h3 className="font-medium text-gray-900 group-hover:text-blue-600 transition-colors">
                          "{example.query}"
                        </h3>
                        <p className="text-sm text-gray-500 mt-1">{example.description}</p>
                      </div>
                      <span className="text-xs font-medium px-2 py-1 bg-gray-100 text-gray-600 rounded shrink-0">
                        {example.source}
                      </span>
                    </div>
                  </a>
                ))}
              </div>
            </section>
          ))}
        </div>
      </main>

      {/* CTA Section */}
      <section className="bg-blue-600 text-white py-12">
        <div className="max-w-4xl mx-auto px-4 text-center">
          <h2 className="text-2xl font-bold mb-4">Ready to explore economic data?</h2>
          <p className="text-blue-100 mb-6">
            Query data from FRED, World Bank, IMF, UN Comtrade, and 6 more sources using natural language.
          </p>
          <a
            href={LIVE_DATA_APP_URL}
            className="inline-flex items-center gap-2 bg-white text-blue-600 px-6 py-3 rounded-lg font-medium hover:bg-blue-50 transition-colors"
          >
            Start Querying Free
          </a>
        </div>
      </section>

      {/* Footer */}
      <footer className="bg-gray-900 text-gray-400 py-8">
        <div className="max-w-6xl mx-auto px-4">
          <div className="flex flex-col md:flex-row items-center justify-between gap-4">
            <div className="text-sm">
              © {new Date().getFullYear()} OpenEcon.ai. Query economic data with AI.
            </div>
            <nav className="flex items-center gap-6 text-sm">
              <Link to="/" className="hover:text-white">Home</Link>
              <a href={LIVE_DATA_APP_URL} className="hover:text-white">Chat</a>
              <Link to="/docs" className="hover:text-white">Docs</Link>
              <Link to="/examples" className="hover:text-white">Examples</Link>
              <a href="mailto:hanlulong@gmail.com" className="hover:text-white">Contact</a>
            </nav>
          </div>
        </div>
      </footer>
    </div>
  )
}
