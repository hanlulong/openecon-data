import { useState, useEffect, useCallback, type ReactNode } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  ArrowRight,
  BarChart3,
  CheckCircle2,
  Cpu,
  Database,
  Globe,
  Rocket,
  Search,
  ShieldCheck,
  Sparkles,
  TrendingUp,
  ChevronLeft,
  ChevronRight,
  Play,
  Pause,
} from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import {
  LineChart,
  Line,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'

// Showcase MULTI-COUNTRY/MULTI-SERIES comparisons from DIFFERENT PROVIDERS:
// 1. FRED - US unemployment & inflation (multi-series)
// 2. World Bank - China, India, Brazil GDP growth (multi-country)
// 3. IMF - US, Germany, Japan GDP growth (multi-country)
// 4. BIS - US, UK, Japan credit-to-GDP ratio (multi-country)
// 5. UN Comtrade - US, China total exports (multi-country trade)
const demoExamples = [
  {
    query: 'US unemployment rate and inflation 2019-2024',
    title: 'US Labor & Prices',
    source: 'FRED',
    insight: 'Unemployment spiked to 8.1% in 2020, while inflation surged to 8% in 2022.',
    chartType: 'line' as const,
    data: [
      { date: '2019', 'Unemployment': 3.7, 'Inflation': 1.8 },
      { date: '2020', 'Unemployment': 8.1, 'Inflation': 1.2 },
      { date: '2021', 'Unemployment': 5.4, 'Inflation': 4.7 },
      { date: '2022', 'Unemployment': 3.6, 'Inflation': 8.0 },
      { date: '2023', 'Unemployment': 3.6, 'Inflation': 4.1 },
      { date: '2024', 'Unemployment': 4.0, 'Inflation': 2.9 },
    ],
    series: ['Unemployment', 'Inflation'],
    colors: ['#3b82f6', '#ef4444'],
    unit: '%',
  },
  {
    query: 'GDP growth China, India, Brazil 2018-2023',
    title: 'Emerging Giants',
    source: 'World Bank',
    insight: 'India leads with 9.2% growth in 2023, outpacing China (5.4%) and Brazil (3.2%).',
    chartType: 'bar' as const,
    data: [
      { date: '2018', 'China': 6.8, 'India': 6.5, 'Brazil': 1.8 },
      { date: '2019', 'China': 6.1, 'India': 3.9, 'Brazil': 1.2 },
      { date: '2020', 'China': 2.3, 'India': -5.8, 'Brazil': -3.3 },
      { date: '2021', 'China': 8.6, 'India': 9.7, 'Brazil': 4.8 },
      { date: '2022', 'China': 3.1, 'India': 7.6, 'Brazil': 3.0 },
      { date: '2023', 'China': 5.4, 'India': 9.2, 'Brazil': 3.2 },
    ],
    series: ['China', 'India', 'Brazil'],
    colors: ['#ef4444', '#f59e0b', '#10b981'],
    unit: '%',
  },
  {
    query: 'GDP growth US, Germany, Japan from IMF 2018-2023',
    title: 'GDP Growth Rates',
    source: 'IMF',
    insight: 'US recovered fastest (6.2% in 2021). Germany contracted -0.9% in 2023 due to energy crisis.',
    chartType: 'line' as const,
    data: [
      { date: '2018', 'US': 3.0, 'Germany': 1.0, 'Japan': 0.6 },
      { date: '2019', 'US': 2.3, 'Germany': 1.1, 'Japan': -0.4 },
      { date: '2020', 'US': -2.2, 'Germany': -3.8, 'Japan': -4.1 },
      { date: '2021', 'US': 6.2, 'Germany': 3.9, 'Japan': 2.7 },
      { date: '2022', 'US': 2.5, 'Germany': 1.8, 'Japan': 1.0 },
      { date: '2023', 'US': 2.9, 'Germany': -0.9, 'Japan': 1.2 },
    ],
    series: ['US', 'Germany', 'Japan'],
    colors: ['#3b82f6', '#10b981', '#ef4444'],
    unit: '%',
  },
  {
    query: 'Credit to GDP ratio US, UK, Japan from BIS 2019-2023',
    title: 'Private Debt Levels',
    source: 'BIS',
    insight: 'UK and Japan have much higher private debt ratios (120-145%) than the US (~50%).',
    chartType: 'bar' as const,
    data: [
      { date: '2019', 'US': 49.8, 'UK': 143.2, 'Japan': 115.4 },
      { date: '2020', 'US': 52.1, 'UK': 150.8, 'Japan': 121.8 },
      { date: '2021', 'US': 50.2, 'UK': 146.5, 'Japan': 120.2 },
      { date: '2022', 'US': 48.9, 'UK': 144.1, 'Japan': 121.5 },
      { date: '2023', 'US': 48.3, 'UK': 142.4, 'Japan': 122.3 },
    ],
    series: ['US', 'UK', 'Japan'],
    colors: ['#3b82f6', '#8b5cf6', '#ef4444'],
    unit: '%',
  },
  {
    query: 'Total exports US, China 2018-2023 from Comtrade',
    title: 'Export Superpowers',
    source: 'UN Comtrade',
    insight: 'China exports $3.4T vs US $2T annually — a $1.4T gap in global trade dominance.',
    chartType: 'bar' as const,
    data: [
      { date: '2018', 'US': 1664, 'China': 2487 },
      { date: '2019', 'US': 1644, 'China': 2499 },
      { date: '2020', 'US': 1425, 'China': 2591 },
      { date: '2021', 'US': 1753, 'China': 3316 },
      { date: '2022', 'US': 2062, 'China': 3594 },
      { date: '2023', 'US': 2019, 'China': 3380 },
    ],
    series: ['US', 'China'],
    colors: ['#3b82f6', '#ef4444'],
    unit: '$B',
  },
]

// Animation configuration
const SLIDE_DURATION = 8000 // 8 seconds per slide
const TRANSITION_DURATION = 0.25

// Sophisticated animation variants
const fadeInUp = {
  hidden: { opacity: 0, y: 24 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.5, ease: [0.25, 0.46, 0.45, 0.94] }
  },
}

const fadeInLeft = {
  hidden: { opacity: 0, x: -24 },
  visible: { opacity: 1, x: 0, transition: { duration: 0.5, ease: [0.25, 0.46, 0.45, 0.94] } },
}

const fadeInRight = {
  hidden: { opacity: 0, x: 24 },
  visible: { opacity: 1, x: 0, transition: { duration: 0.5, ease: [0.25, 0.46, 0.45, 0.94] } },
}

const staggerContainer = {
  hidden: { opacity: 0 },
  visible: {
    opacity: 1,
    transition: { staggerChildren: 0.08, delayChildren: 0.1 },
  },
}

const cardHover = {
  rest: {
    scale: 1,
    y: 0,
  },
  hover: {
    scale: 1.015,
    y: -2,
    transition: { duration: 0.25, ease: 'easeOut' }
  },
}

// Demo slide transitions - sophisticated crossfade with scale
const slideVariants = {
  enter: (_direction: number) => ({
    opacity: 0,
    scale: 0.96,
    filter: 'blur(4px)',
  }),
  center: {
    opacity: 1,
    scale: 1,
    filter: 'blur(0px)',
    transition: {
      duration: TRANSITION_DURATION,
      ease: [0.25, 0.46, 0.45, 0.94],
    },
  },
  exit: (_direction: number) => ({
    opacity: 0,
    scale: 1.02,
    filter: 'blur(4px)',
    transition: {
      duration: TRANSITION_DURATION * 0.8,
      ease: [0.25, 0.46, 0.45, 0.94],
    },
  }),
}

// Button pulse animation for CTA
const buttonPulse = {
  rest: { scale: 1 },
  hover: {
    scale: 1.03,
    transition: { duration: 0.2, ease: 'easeOut' }
  },
  tap: { scale: 0.98 },
}

const navItems = [
  { label: 'Features', href: '#features' },
  { label: 'Tools', href: '#tools' },
  { label: 'Data Sources', href: '#integrations' },
  { label: 'How it works', href: '#how' },
  { label: 'Docs', href: '/docs' },
] as const

const integrations = [
  { name: 'FRED', tag: 'Macro', desc: '90,000+ US economic series — GDP, CPI, employment, rates.' },
  { name: 'World Bank', tag: 'WDI', desc: '16,000+ global development indicators across 200+ countries.' },
  { name: 'IMF', tag: 'IFS', desc: 'International Financial Statistics and World Economic Outlook.' },
  { name: 'UN Comtrade', tag: 'Trade', desc: 'Bilateral trade flows with HS-code level detail.' },
  { name: 'Eurostat', tag: 'EU', desc: 'EU-wide statistics on economy, demographics, and trade.' },
  { name: 'BIS', tag: 'Banking', desc: 'Central bank statistics — credit, debt, property prices.' },
  { name: 'StatsCan', tag: 'Canada', desc: '40,000+ Canadian statistical tables via SDMX.' },
  { name: 'OECD', tag: 'Cross-country', desc: 'Comparable data across 38 OECD member economies.' },
  { name: 'ExchangeRate', tag: 'FX', desc: 'Live and historical currency exchange rates.' },
  { name: 'CoinGecko', tag: 'Crypto', desc: 'Cryptocurrency prices, volume, and market data.' },
] as const

const features: Array<{ icon: ReactNode; title: string; desc: string }> = [
  {
    icon: <Search className="h-6 w-6" />,
    title: 'Semantic Search',
    desc: 'Ask in plain English. We map your query to the right series across APIs and vintages.',
  },
  {
    icon: <Database className="h-6 w-6" />,
    title: 'Smart Joins',
    desc: 'Auto-match country/industry/classification codes and align frequencies with one click.',
  },
  {
    icon: <BarChart3 className="h-6 w-6" />,
    title: 'Instant Charts',
    desc: 'Publication-ready charts with reproducible query URLs and notes.',
  },
  {
    icon: <Cpu className="h-6 w-6" />,
    title: 'AI Pipelines',
    desc: 'Chain transforms (filters, deflation, seasonal adjust, FX) and export to code.',
  },
  {
    icon: <ShieldCheck className="h-6 w-6" />,
    title: 'Provenance & Audits',
    desc: 'Every figure is traceable to source endpoints, parameters, and timestamps.',
  },
  {
    icon: <Rocket className="h-6 w-6" />,
    title: 'Open Source',
    desc: 'Fork it, extend it, self-host it. MCP server built in for AI agent integration.',
  },
]

const tools = [
  {
    title: 'OpenEcon Data',
    href: 'https://data.openecon.ai/chat',
    tag: 'Live app',
    desc: 'Query economic data in plain English, chart results instantly, and export CSV/JSON with source provenance.',
  },
  {
    title: 'Econ Writing Skill',
    href: 'https://github.com/hanlulong/econ-writing-skill',
    tag: 'Open source',
    desc: 'Reusable writing workflows for economists with prompt patterns, structure templates, and analysis guardrails.',
  },
  {
    title: 'Awesome AI for Economists',
    href: 'https://github.com/hanlulong/awesome-ai-for-economists',
    tag: 'Resource list',
    desc: 'Curated tools, papers, and references to help economists apply AI in research, writing, and production work.',
  },
] as const

const check = (text: string) => (
  <li key={text} className="flex items-start gap-2">
    <CheckCircle2 className="mt-0.5 h-5 w-5" />
    {text}
  </li>
)

// Professional gradient definitions for charts
const chartGradients = (
  <defs>
    <linearGradient id="gradientIndigo" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stopColor="#667eea" stopOpacity={0.8} />
      <stop offset="100%" stopColor="#667eea" stopOpacity={0.1} />
    </linearGradient>
    <linearGradient id="gradientAmber" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stopColor="#f59e0b" stopOpacity={0.8} />
      <stop offset="100%" stopColor="#f59e0b" stopOpacity={0.1} />
    </linearGradient>
    <linearGradient id="gradientGreen" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stopColor="#10b981" stopOpacity={0.8} />
      <stop offset="100%" stopColor="#10b981" stopOpacity={0.1} />
    </linearGradient>
    <linearGradient id="gradientPurple" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stopColor="#8b5cf6" stopOpacity={0.8} />
      <stop offset="100%" stopColor="#8b5cf6" stopOpacity={0.1} />
    </linearGradient>
    <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="2" stdDeviation="3" floodOpacity="0.15" />
    </filter>
  </defs>
)

// Professional color palette
const PROFESSIONAL_COLORS = {
  primary: '#4f46e5',
  secondary: '#0ea5e9',
  accent: '#8b5cf6',
  success: '#10b981',
  warning: '#f59e0b',
  danger: '#ef4444',
  grid: '#f1f5f9',
  axis: '#64748b',
  text: '#334155',
}

// Professional chart component using Recharts (same as actual app)
function DemoChart({ example, chartTypeOverride }: { example: typeof demoExamples[number]; chartTypeOverride?: 'line' | 'bar' | 'table' }) {
  const { data, series, colors, unit } = example
  const chartType = chartTypeOverride || example.chartType

  const formatYAxis = (value: number) => {
    // For currency in billions (like exports), show as trillions for cleaner display
    if (unit === '$B') {
      if (Math.abs(value) >= 1000) return `$${(value / 1000).toFixed(1)}T`
      return `$${value.toFixed(0)}B`
    }
    // For percentages, show clean integer or one decimal
    if (unit === '%') {
      if (Number.isInteger(value)) return `${value}%`
      return `${value.toFixed(1)}%`
    }
    // For years unit
    if (unit === 'years') {
      return value.toFixed(0)
    }
    // Default formatting
    if (Math.abs(value) >= 1000) return `${(value / 1000).toFixed(1)}K`
    if (Number.isInteger(value)) return value.toString()
    if (Math.abs(value) >= 10) return value.toFixed(0)
    return value.toFixed(1)
  }

  // Custom tooltip component for professional look
  const CustomTooltip = ({ active, payload, label }: any) => {
    if (active && payload && payload.length) {
      return (
        <div className="bg-white/95 backdrop-blur-sm border border-gray-200 rounded-lg shadow-xl p-3 min-w-[140px]">
          <p className="text-xs font-semibold text-gray-900 mb-2 pb-1.5 border-b border-gray-100">{label}</p>
          {payload.map((entry: any, index: number) => (
            <div key={index} className="flex items-center justify-between gap-4 text-xs py-0.5">
              <span className="flex items-center gap-1.5">
                <span
                  className="w-2 h-2 rounded-full"
                  style={{ backgroundColor: entry.color }}
                />
                <span className="text-gray-600">{entry.name || 'Value'}</span>
              </span>
              <span className="font-medium text-gray-900">
                {typeof entry.value === 'number' ? entry.value.toLocaleString() : entry.value}
                {unit ? ` ${unit}` : ''}
              </span>
            </div>
          ))}
        </div>
      )
    }
    return null
  }

  // Line chart
  if (chartType === 'line') {
    return (
      <ResponsiveContainer width="100%" height={200}>
        <LineChart data={data} margin={{ top: 10, right: 25, left: 5, bottom: 10 }}>
          {chartGradients}
          <CartesianGrid
            strokeDasharray="3 3"
            stroke={PROFESSIONAL_COLORS.grid}
            vertical={false}
          />
          <XAxis
            dataKey="date"
            stroke={PROFESSIONAL_COLORS.axis}
            fontSize={11}
            tickLine={false}
            axisLine={{ stroke: PROFESSIONAL_COLORS.grid }}
            dy={8}
          />
          <YAxis
            stroke={PROFESSIONAL_COLORS.axis}
            fontSize={11}
            tickLine={false}
            axisLine={false}
            tickFormatter={formatYAxis}
            width={40}
            dx={-5}
          />
          <Tooltip content={<CustomTooltip />} />
          <Legend
            wrapperStyle={{ fontSize: '11px', paddingTop: '12px' }}
            iconType="circle"
            iconSize={8}
          />
          {series.map((s, i) => (
            <Line
              key={s}
              type="monotone"
              dataKey={s}
              stroke={colors[i]}
              strokeWidth={2.5}
              dot={{ r: 4, fill: '#fff', stroke: colors[i], strokeWidth: 2 }}
              activeDot={{ r: 6, fill: colors[i], stroke: '#fff', strokeWidth: 2 }}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    )
  }

  // Table view - displays data in a clean tabular format
  if (chartType === 'table') {
    const formatValue = (val: number) => {
      if (unit === '$B') return `$${val.toLocaleString()}B`
      if (unit === '%') return `${val.toFixed(1)}%`
      return val.toLocaleString()
    }
    return (
      <div className="h-[200px] overflow-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-gray-200">
              <th className="text-left py-2 px-2 font-semibold text-gray-600">Year</th>
              {series.map((s, i) => (
                <th key={s} className="text-right py-2 px-2 font-semibold" style={{ color: colors[i] }}>{s}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {data.map((row, idx) => (
              <tr key={idx} className="border-b border-gray-100 hover:bg-gray-50">
                <td className="py-1.5 px-2 text-gray-700 font-medium">{row.date}</td>
                {series.map((s, i) => (
                  <td key={s} className="text-right py-1.5 px-2" style={{ color: colors[i] }}>
                    {formatValue((row as Record<string, unknown>)[s] as number)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  // Bar chart (default) - supports multiple series for grouped bars
  return (
    <ResponsiveContainer width="100%" height={200}>
      <BarChart data={data} margin={{ top: 10, right: 25, left: 5, bottom: 10 }}>
        {chartGradients}
        <CartesianGrid
          strokeDasharray="3 3"
          stroke={PROFESSIONAL_COLORS.grid}
          vertical={false}
        />
        <XAxis
          dataKey="date"
          stroke={PROFESSIONAL_COLORS.axis}
          fontSize={11}
          tickLine={false}
          axisLine={{ stroke: PROFESSIONAL_COLORS.grid }}
          dy={8}
        />
        <YAxis
          stroke={PROFESSIONAL_COLORS.axis}
          fontSize={11}
          tickLine={false}
          axisLine={false}
          tickFormatter={formatYAxis}
          width={40}
          dx={-5}
        />
        <Tooltip content={<CustomTooltip />} />
        <Legend
          wrapperStyle={{ fontSize: '11px', paddingTop: '12px' }}
          iconType="rect"
          iconSize={10}
        />
        {series.map((s, i) => (
          <Bar
            key={s}
            dataKey={s}
            fill={colors[i] || colors[0]}
            radius={[3, 3, 0, 0]}
            isAnimationActive={false}
          />
        ))}
      </BarChart>
    </ResponsiveContainer>
  )
}

export function LandingPage() {
  const navigate = useNavigate()
  const [currentIndex, setCurrentIndex] = useState(0)
  const [isPlaying, setIsPlaying] = useState(true)
  const [progress, setProgress] = useState(0)
  const [direction, setDirection] = useState(0)
  const [sampleQuery, setSampleQuery] = useState(demoExamples[0].query)
  const [chartType, setChartType] = useState<'line' | 'bar' | 'table'>('line')

  const currentExample = demoExamples[currentIndex]

  // Sync query and chart type with current example
  useEffect(() => {
    setSampleQuery(demoExamples[currentIndex].query)
    setChartType(demoExamples[currentIndex].chartType)
  }, [currentIndex])

  // Export data as CSV
  const handleExportCSV = useCallback(() => {
    const example = demoExamples[currentIndex]
    const headers = Object.keys(example.data[0]).join(',')
    const rows = example.data.map(row => Object.values(row).join(','))
    const csv = [headers, ...rows].join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${example.title.toLowerCase().replace(/\s+/g, '_')}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }, [currentIndex])

  // Export data as JSON
  const handleExportJSON = useCallback(() => {
    const example = demoExamples[currentIndex]
    const json = JSON.stringify({
      title: example.title,
      source: example.source,
      data: example.data
    }, null, 2)
    const blob = new Blob([json], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${example.title.toLowerCase().replace(/\s+/g, '_')}.json`
    a.click()
    URL.revokeObjectURL(url)
  }, [currentIndex])

  // Auto-advance with progress tracking
  useEffect(() => {
    if (!isPlaying) return

    const startTime = Date.now()
    const animate = () => {
      const elapsed = Date.now() - startTime
      const newProgress = (elapsed / SLIDE_DURATION) * 100

      if (newProgress >= 100) {
        setDirection(1)
        setCurrentIndex((prev) => (prev + 1) % demoExamples.length)
        setProgress(0)
      } else {
        setProgress(newProgress)
      }
    }

    const interval = setInterval(animate, 50)
    return () => clearInterval(interval)
  }, [isPlaying, currentIndex])

  const goToSlide = useCallback((index: number) => {
    setDirection(index > currentIndex ? 1 : -1)
    setCurrentIndex(index)
    setProgress(0)
    setSampleQuery(demoExamples[index].query)
  }, [currentIndex])

  const nextSlide = useCallback(() => {
    setDirection(1)
    setCurrentIndex((prev) => (prev + 1) % demoExamples.length)
    setProgress(0)
  }, [])

  const prevSlide = useCallback(() => {
    setDirection(-1)
    setCurrentIndex((prev) => (prev - 1 + demoExamples.length) % demoExamples.length)
    setProgress(0)
  }, [])

  return (
    <div className="min-h-screen antialiased text-gray-900">
      <header className="sticky top-0 z-50 border-b bg-white/80 backdrop-blur supports-[backdrop-filter]:bg-white/60">
        <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-4 sm:px-6 lg:px-5">
          <a href="#" className="flex items-center gap-2 text-lg sm:text-xl font-semibold">
            <div className="h-6 w-6 sm:h-7 sm:w-7 rounded-xl bg-gradient-to-br from-indigo-500 via-sky-500 to-emerald-400" />
            <span className="hidden xs:inline">OpenEcon.ai</span>
            <span className="inline xs:hidden">OpenEcon.ai</span>
          </a>
          <nav className="hidden items-center gap-3 text-sm md:flex lg:gap-4">
            {navItems.map((item) => (
              <a key={item.label} href={item.href} className="text-gray-600 hover:text-gray-900">
                {item.label}
              </a>
            ))}
          </nav>
          <div className="flex items-center gap-2 sm:gap-3">
            <Button
              variant="ghost"
              className="hidden rounded-xl px-3 py-2 text-sm sm:inline-flex sm:px-4"
              onClick={() => navigate('/chat?auth=1')}
            >
              Sign in
            </Button>
            <Button className="rounded-2xl px-3 py-2 text-sm sm:px-4" onClick={() => navigate('/chat')}>
              Get started
            </Button>
          </div>
        </div>
      </header>

      <section className="relative overflow-hidden">
        <div className="absolute inset-0 -z-10">
          <div className="absolute -top-20 -left-20 h-[40rem] w-[40rem] rounded-full bg-gradient-to-br from-indigo-300/30 via-sky-200/30 to-emerald-200/30 blur-3xl" />
          <div className="absolute -bottom-16 -right-16 h-[30rem] w-[30rem] rounded-full bg-gradient-to-tr from-fuchsia-200/30 via-rose-200/30 to-amber-200/30 blur-3xl" />
        </div>
        <div className="mx-auto grid max-w-7xl items-center gap-6 px-4 pb-8 pt-6 sm:gap-10 sm:pb-12 sm:pt-10 lg:grid-cols-2 lg:px-5">
          <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.6 }}>
            <Badge
              variant="secondary"
              className="mb-3 inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs sm:mb-4 sm:px-3 sm:py-1 sm:text-sm"
            >
              <Sparkles className="h-3 w-3 sm:h-4 sm:w-4" /> 10+ Official Data Sources
            </Badge>
            <h1 className="text-3xl font-bold leading-tight tracking-tight sm:text-4xl md:text-5xl lg:text-5xl">
              Query economic data{' '}
              <span className="bg-gradient-to-r from-indigo-600 to-sky-600 bg-clip-text text-transparent">across providers instantly.</span>
            </h1>
            <p className="mt-4 max-w-xl text-lg text-gray-600 sm:mt-5 sm:text-xl">
              Access FRED, World Bank, IMF, BIS, UN Comtrade, and more with natural language. Auto-join datasets, align frequencies, and get publication-ready charts.
            </p>
            <div className="mt-6 flex flex-col gap-3 sm:mt-8 sm:flex-row sm:gap-4">
              <Button className="rounded-2xl px-6 py-3 text-base font-semibold" onClick={() => navigate('/chat')}>
                Try it now
              </Button>
              <Button variant="outline" className="rounded-2xl px-6 py-3 text-base" onClick={() => navigate('/docs')}>
                View documentation
              </Button>
            </div>
            <ul className="mt-6 grid max-w-md grid-cols-2 gap-3 text-sm sm:mt-8 text-gray-600">
              {['Cross-provider queries', 'Automatic data joins', 'Export to CSV/JSON', 'Full provenance tracking'].map((item) => (
                <li key={item} className="flex items-center gap-2">
                  <CheckCircle2 className="h-4 w-4 flex-shrink-0 text-green-500" /> <span>{item}</span>
                </li>
              ))}
            </ul>
          </motion.div>

          <motion.div
            initial={{ opacity: 0, y: 20, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            transition={{ duration: 0.7, delay: 0.1, ease: [0.25, 0.46, 0.45, 0.94] }}
          >
            <Card className="rounded-2xl border-0 shadow-2xl shadow-gray-200/60 overflow-hidden bg-white">
              <CardContent className="p-0">
                {/* Search input at top (chat box style) */}
                <div className="px-4 pt-4 pb-3 border-b border-gray-100">
                  <div className="flex gap-2">
                    <div className="flex-1 flex items-center gap-2 px-3 py-2.5 rounded-xl bg-gray-50 border border-gray-200 focus-within:border-indigo-400 focus-within:ring-2 focus-within:ring-indigo-100 transition-all">
                      <Search className="h-4 w-4 text-gray-400 flex-shrink-0" />
                      <input
                        className="flex-1 bg-transparent text-sm outline-none placeholder:text-gray-400"
                        placeholder="Ask about economic data..."
                        value={sampleQuery}
                        onChange={(e) => setSampleQuery(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter' && sampleQuery.trim()) {
                            navigate(`/chat?query=${encodeURIComponent(sampleQuery.trim())}`)
                          }
                        }}
                      />
                    </div>
                    <Button
                      className="rounded-xl px-4 bg-indigo-600 hover:bg-indigo-700 text-white"
                      onClick={() => {
                        if (sampleQuery.trim()) {
                          navigate(`/chat?query=${encodeURIComponent(sampleQuery.trim())}`)
                        }
                      }}
                    >
                      <ArrowRight className="h-4 w-4" />
                    </Button>
                  </div>
                </div>

                {/* Demo content area - fixed height to prevent jumping between examples */}
                <div className="relative h-[370px] sm:h-[360px] lg:h-[355px] p-4 overflow-hidden">
                  <AnimatePresence mode="wait" custom={direction}>
                    <motion.div
                      key={currentIndex}
                      custom={direction}
                      variants={slideVariants}
                      initial="enter"
                      animate="center"
                      exit="exit"
                      className="space-y-3"
                    >
                      {/* Response (like assistant message with data) */}
                      <div className="bg-gray-50 rounded-xl p-4 border border-gray-200">
                        {/* Data summary header */}
                        <div className="flex items-center justify-between mb-3 pb-3 border-b border-gray-200">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="font-medium text-gray-900 text-sm">{currentExample.title}</span>
                            <span className="text-xs text-gray-500">• {currentExample.data.length} observations</span>
                            <span className="text-xs text-gray-500">• {currentExample.source}</span>
                          </div>
                          <div className="flex items-center gap-1">
                            <button
                              onClick={() => setIsPlaying(!isPlaying)}
                              className="p-1.5 rounded-lg hover:bg-gray-200 transition-colors"
                              title={isPlaying ? 'Pause' : 'Play'}
                            >
                              {isPlaying ? (
                                <Pause className="h-3.5 w-3.5 text-gray-500" />
                              ) : (
                                <Play className="h-3.5 w-3.5 text-gray-500" />
                              )}
                            </button>
                            <button
                              onClick={prevSlide}
                              className="p-1.5 rounded-lg hover:bg-gray-200 transition-colors"
                            >
                              <ChevronLeft className="h-3.5 w-3.5 text-gray-500" />
                            </button>
                            <button
                              onClick={nextSlide}
                              className="p-1.5 rounded-lg hover:bg-gray-200 transition-colors"
                            >
                              <ChevronRight className="h-3.5 w-3.5 text-gray-500" />
                            </button>
                          </div>
                        </div>

                        {/* Chart type selector and export (functional buttons) */}
                        <div className="flex items-center justify-between mb-3">
                          <div className="flex gap-1">
                            <button
                              onClick={() => setChartType('line')}
                              className={`px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${chartType === 'line' ? 'bg-indigo-100 text-indigo-700' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'}`}
                            >
                              📈 Line
                            </button>
                            <button
                              onClick={() => setChartType('bar')}
                              className={`px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${chartType === 'bar' ? 'bg-indigo-100 text-indigo-700' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'}`}
                            >
                              📊 Bar
                            </button>
                            <button
                              onClick={() => setChartType('table')}
                              className={`px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${chartType === 'table' ? 'bg-indigo-100 text-indigo-700' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'}`}
                            >
                              📋 Table
                            </button>
                          </div>
                          <div className="flex gap-1">
                            <button
                              onClick={handleExportCSV}
                              className="px-2 py-1 rounded text-xs bg-gray-100 text-gray-600 hover:bg-gray-200 transition-colors"
                            >
                              CSV
                            </button>
                            <button
                              onClick={handleExportJSON}
                              className="px-2 py-1 rounded text-xs bg-gray-100 text-gray-600 hover:bg-gray-200 transition-colors"
                            >
                              JSON
                            </button>
                          </div>
                        </div>

                        {/* Chart */}
                        <div className="bg-white rounded-lg border border-gray-200 p-3">
                          <DemoChart example={currentExample} chartTypeOverride={chartType} />
                        </div>

                        {/* Short insight text */}
                        <p className="text-xs text-gray-500 mt-2 leading-relaxed">{currentExample.insight}</p>
                      </div>
                    </motion.div>
                  </AnimatePresence>
                </div>

                {/* Minimal progress bar at bottom */}
                <div className="px-4 pb-3">
                  <div className="flex gap-0.5">
                    {demoExamples.map((_, idx) => (
                      <button
                        key={idx}
                        onClick={() => goToSlide(idx)}
                        className="flex-1 h-0.5 rounded-full overflow-hidden bg-gray-200 hover:bg-gray-300 transition-colors"
                      >
                        <motion.div
                          className="h-full bg-indigo-500"
                          initial={{ width: 0 }}
                          animate={{
                            width: idx === currentIndex ? `${progress}%` : idx < currentIndex ? '100%' : '0%'
                          }}
                          transition={{ duration: 0.05 }}
                        />
                      </button>
                    ))}
                  </div>
                </div>
              </CardContent>
            </Card>
          </motion.div>
        </div>
      </section>

      {/* Stats section */}
      <motion.section
        className="py-6 sm:py-8 border-b border-gray-100"
        initial="hidden"
        whileInView="visible"
        viewport={{ once: true, margin: '-100px' }}
        variants={staggerContainer}
      >
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-5">
          <div className="grid grid-cols-2 gap-4 sm:grid-cols-4 sm:gap-6">
            {[
              { value: '330K+', label: 'Indicators indexed' },
              { value: '10+', label: 'Data providers' },
              { value: '200+', label: 'Countries covered' },
              { value: '< 5s', label: 'Avg. query time' },
            ].map((stat, index) => (
              <motion.div
                key={stat.label}
                variants={fadeInUp}
                custom={index}
                className="text-center"
              >
                <div className="text-2xl font-bold sm:text-3xl bg-gradient-to-r from-indigo-600 to-sky-600 bg-clip-text text-transparent">
                  {stat.value}
                </div>
                <div className="mt-1 text-xs text-gray-500 sm:text-sm">{stat.label}</div>
              </motion.div>
            ))}
          </div>
        </div>
      </motion.section>

      <motion.section
        id="tools"
        className="py-8 sm:py-10 bg-gray-50/70"
        initial="hidden"
        whileInView="visible"
        viewport={{ once: true, margin: '-100px' }}
        variants={staggerContainer}
      >
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-5">
          <motion.div variants={fadeInUp}>
            <h2 className="text-xl font-semibold sm:text-2xl lg:text-3xl">OpenEcon tools</h2>
            <p className="mt-2 max-w-3xl text-sm text-gray-600 sm:text-base">
              Start with the live data assistant, then use open-source skills and curated resources to build repeatable workflows.
            </p>
          </motion.div>

          <div className="mt-5 grid gap-3 sm:mt-6 sm:grid-cols-2 lg:grid-cols-3">
            {tools.map((tool, index) => (
              <motion.a
                key={tool.title}
                variants={fadeInUp}
                custom={index}
                href={tool.href}
                target="_blank"
                rel="noopener noreferrer"
                className="block"
              >
                <Card className="h-full rounded-2xl border-gray-200 transition-all hover:-translate-y-0.5 hover:border-indigo-300 hover:shadow-md">
                  <CardContent className="p-5">
                    <div className="flex items-center justify-between gap-3">
                      <h3 className="text-base font-semibold text-gray-900">{tool.title}</h3>
                      <Badge variant="secondary" className="rounded-xl">
                        {tool.tag}
                      </Badge>
                    </div>
                    <p className="mt-2 text-sm text-gray-600">{tool.desc}</p>
                    <div className="mt-4 inline-flex items-center gap-1.5 text-sm font-medium text-indigo-700">
                      Open <ArrowRight className="h-3.5 w-3.5" />
                    </div>
                  </CardContent>
                </Card>
              </motion.a>
            ))}
          </div>
        </div>
      </motion.section>

      <motion.section
        id="integrations"
        className="py-8 sm:py-10"
        initial="hidden"
        whileInView="visible"
        viewport={{ once: true, margin: '-100px' }}
        variants={staggerContainer}
      >
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-5">
          <motion.div
            className="mb-4 flex flex-col gap-3 sm:mb-5 sm:flex-row sm:items-end sm:justify-between"
            variants={fadeInUp}
          >
            <h2 className="text-xl font-semibold sm:text-2xl lg:text-3xl">Automated API integration from trusted sources</h2>
            <motion.a
              href="/docs"
              className="text-xs text-gray-600 hover:text-gray-900 sm:text-sm inline-flex items-center gap-1"
              whileHover={{ x: 5 }}
            >
              View documentation <ArrowRight className="inline h-3 w-3 sm:h-4 sm:w-4" />
            </motion.a>
          </motion.div>
          <div className="grid gap-2 grid-cols-2 sm:gap-3 md:grid-cols-3 lg:grid-cols-5">
            {integrations.map((integration, index) => (
              <motion.div
                key={integration.name}
                variants={fadeInUp}
                custom={index}
                whileHover="hover"
                initial="rest"
                animate="rest"
              >
                <motion.div variants={cardHover}>
                  <Card className="rounded-xl cursor-pointer overflow-hidden group">
                    <CardContent className="p-4 transition-colors group-hover:bg-gradient-to-br group-hover:from-indigo-50/50 group-hover:to-sky-50/50">
                      <div className="flex items-center justify-between">
                        <div className="font-medium group-hover:text-indigo-700 transition-colors">{integration.name}</div>
                        <Badge variant="secondary" className="rounded-xl group-hover:bg-indigo-100 transition-colors">
                          {integration.tag}
                        </Badge>
                      </div>
                      <p className="mt-1 text-sm text-gray-600">{integration.desc}</p>
                    </CardContent>
                  </Card>
                </motion.div>
              </motion.div>
            ))}
          </div>
        </div>
      </motion.section>

      <motion.section
        id="how"
        className="py-8 sm:py-10 bg-gradient-to-b from-white to-gray-50/50"
        initial="hidden"
        whileInView="visible"
        viewport={{ once: true, margin: '-100px' }}
        variants={staggerContainer}
      >
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-5">
          <motion.h2 variants={fadeInUp} className="text-xl font-semibold sm:text-2xl lg:text-3xl">How it works</motion.h2>
          <div className="mt-4 grid gap-4 sm:mt-6 sm:gap-5 md:grid-cols-2 lg:grid-cols-3">
            {[
              {
                icon: <Globe className="h-5 w-5" />,
                step: '1) Connect',
                desc: 'Auth to public or private data APIs.',
                items: [
                  'Choose providers (FRED, WorldBank, Comtrade, …)',
                  'Set namespaces, units, and vintage preferences',
                  'Auto-doc source terms and attributions',
                ],
                gradient: 'from-blue-500 to-cyan-500',
              },
              {
                icon: <Search className="h-5 w-5" />,
                step: '2) Ask',
                desc: 'Semantic search and AI transforms.',
                items: [
                  'Query by concept ("headline CPI, Canada")',
                  'Map to codes, join geos, align frequency',
                  'Apply transforms (SA/NSA, YoY, indices)',
                ],
                gradient: 'from-purple-500 to-pink-500',
              },
              {
                icon: <BarChart3 className="h-5 w-5" />,
                step: '3) Share',
                desc: 'Charts, notebooks, and API links.',
                items: ['One-click chart with footnotes', 'Export CSV/JSON with provenance', 'Share reproducible query URLs'],
                gradient: 'from-orange-500 to-red-500',
              },
            ].map((card, index) => (
              <motion.div
                key={card.step}
                variants={fadeInUp}
                custom={index}
                whileHover="hover"
                initial="rest"
                animate="rest"
              >
                <motion.div variants={cardHover}>
                  <Card className="rounded-2xl h-full overflow-hidden group">
                    <CardHeader className="relative">
                      <div className={`absolute inset-0 bg-gradient-to-r ${card.gradient} opacity-0 group-hover:opacity-5 transition-opacity duration-300`} />
                      <CardTitle className="flex items-center gap-2 relative">
                        <span className={`p-2 rounded-lg bg-gradient-to-r ${card.gradient} text-white`}>
                          {card.icon}
                        </span>
                        <span className="group-hover:text-indigo-700 transition-colors">{card.step}</span>
                      </CardTitle>
                      <CardDescription>{card.desc}</CardDescription>
                    </CardHeader>
                    <CardContent>
                      <ul className="space-y-2 text-sm text-gray-700">
                        {card.items.map(check)}
                      </ul>
                    </CardContent>
                  </Card>
                </motion.div>
              </motion.div>
            ))}
          </div>
        </div>
      </motion.section>

      <motion.section
        id="features"
        className="py-8 sm:py-10"
        initial="hidden"
        whileInView="visible"
        viewport={{ once: true, margin: '-100px' }}
        variants={staggerContainer}
      >
        <div className="mx-auto max-w-7xl px-4 sm:px-6 lg:px-5">
          <motion.h2 variants={fadeInUp} className="text-xl font-semibold sm:text-2xl lg:text-3xl">Key Features</motion.h2>
          <motion.p variants={fadeInUp} className="mt-2 text-xs text-gray-600 sm:text-sm">Core capabilities that make querying economic data fast and defensible.</motion.p>
          <div className="mt-4 grid gap-3 sm:mt-5 sm:gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {features.map((feature, index) => (
              <motion.div
                key={feature.title}
                variants={fadeInUp}
                custom={index}
                whileHover="hover"
                initial="rest"
                animate="rest"
              >
                <motion.div variants={cardHover}>
                  <Card className="rounded-2xl h-full group cursor-pointer">
                    <CardContent className="p-5">
                      <motion.div
                        className="mb-3 flex h-10 w-10 items-center justify-center rounded-xl border bg-gradient-to-br from-indigo-50 to-sky-50 text-indigo-600 group-hover:from-indigo-100 group-hover:to-sky-100 transition-all duration-300"
                        whileHover={{ rotate: 5, scale: 1.1 }}
                      >
                        {feature.icon}
                      </motion.div>
                      <div className="text-lg font-semibold group-hover:text-indigo-700 transition-colors">{feature.title}</div>
                      <p className="mt-1 text-sm text-gray-600">{feature.desc}</p>
                    </CardContent>
                  </Card>
                </motion.div>
              </motion.div>
            ))}
          </div>

          <motion.div variants={fadeInUp}>
            <Card className="mt-5 rounded-2xl bg-gradient-to-br from-indigo-50 to-sky-50 overflow-hidden">
              <CardContent className="p-6 sm:p-8">
                <div className="grid gap-6 lg:grid-cols-2 items-center">
                  <motion.div variants={fadeInLeft}>
                    <h3 className="text-xl font-semibold sm:text-2xl">Export anywhere</h3>
                    <p className="mt-2 text-sm text-gray-600 sm:text-base">
                      Download your data as CSV or JSON. Every query includes source attribution and timestamps.
                    </p>
                    <ul className="mt-4 space-y-2 text-sm text-gray-700">
                      {[
                        'One-click CSV/JSON export',
                        'API URL for reproducibility',
                        'Source citations included',
                      ].map(check)}
                    </ul>
                  </motion.div>
                  <motion.div
                    className="rounded-xl border bg-white p-4 shadow-sm"
                    variants={fadeInRight}
                    whileHover={{ scale: 1.02, boxShadow: '0 10px 30px rgba(0,0,0,0.1)' }}
                  >
                    <div className="flex items-center gap-2 text-sm font-medium text-gray-700 mb-3">
                      <Database className="h-4 w-4" /> Sample Export
                    </div>
                    <pre className="overflow-x-auto text-xs text-gray-600 bg-gray-50 p-3 rounded-lg">{`{
  "indicator": "GDP Growth",
  "country": "United States",
  "source": "World Bank",
  "data": [
    {"date": "2023", "value": 2.5},
    {"date": "2022", "value": 1.9}
  ]
}`}</pre>
                  </motion.div>
                </div>
              </CardContent>
            </Card>
          </motion.div>
        </div>
      </motion.section>

      <motion.section
        className="py-10 sm:py-14 lg:py-16 relative overflow-hidden"
        initial="hidden"
        whileInView="visible"
        viewport={{ once: true, margin: '-100px' }}
        variants={staggerContainer}
      >
        {/* Animated background gradient */}
        <div className="absolute inset-0 -z-10">
          <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 h-[30rem] w-[30rem] rounded-full bg-gradient-to-br from-indigo-200/40 via-purple-200/40 to-pink-200/40 blur-3xl animate-pulse" />
        </div>

        <div className="mx-auto max-w-4xl px-4 sm:px-6 lg:px-5 text-center">
          <motion.div variants={fadeInUp}>
            <motion.div
              className="inline-flex items-center gap-2 mb-4 px-4 py-2 rounded-full bg-gradient-to-r from-indigo-100 to-purple-100 text-indigo-700 text-sm font-medium"
            >
              <TrendingUp className="h-4 w-4" /> Free and open source
            </motion.div>
          </motion.div>

          <motion.h2
            variants={fadeInUp}
            className="text-2xl font-bold sm:text-3xl lg:text-4xl bg-gradient-to-r from-gray-900 via-indigo-900 to-gray-900 bg-clip-text text-transparent"
          >
            Start querying in seconds
          </motion.h2>

          <motion.p variants={fadeInUp} className="mt-4 text-base text-gray-600 sm:text-lg max-w-2xl mx-auto">
            No signup required. Ask a question in plain English and get data from FRED, World Bank, IMF, and 7 more providers instantly.
          </motion.p>

          <motion.div variants={fadeInUp} className="mt-8 flex flex-col gap-3 sm:flex-row sm:justify-center sm:gap-4">
            <motion.div
              variants={buttonPulse}
              initial="rest"
              whileHover="hover"
              whileTap="tap"
            >
              <Button
                className="rounded-2xl px-8 py-3 text-base font-semibold bg-gradient-to-r from-indigo-600 to-purple-600 hover:from-indigo-700 hover:to-purple-700 shadow-lg hover:shadow-xl transition-shadow inline-flex items-center gap-2 whitespace-nowrap"
                onClick={() => navigate('/chat')}
              >
                Get started
                <ArrowRight className="h-4 w-4 flex-shrink-0" />
              </Button>
            </motion.div>
            <motion.div
              variants={buttonPulse}
              initial="rest"
              whileHover="hover"
              whileTap="tap"
            >
              <Button variant="outline" className="rounded-2xl px-8 py-3 text-base" onClick={() => navigate('/docs')}>
                Read the docs
              </Button>
            </motion.div>
          </motion.div>

          <motion.div variants={fadeInUp} className="mt-8 flex flex-wrap items-center justify-center gap-4 sm:gap-6 text-sm text-gray-500">
            <span className="flex items-center gap-1">
              <CheckCircle2 className="h-4 w-4 text-green-500" /> No signup required
            </span>
            <span className="flex items-center gap-1">
              <CheckCircle2 className="h-4 w-4 text-green-500" /> 330K+ indicators
            </span>
            <span className="flex items-center gap-1">
              <CheckCircle2 className="h-4 w-4 text-green-500" /> Full data provenance
            </span>
            <span className="flex items-center gap-1">
              <CheckCircle2 className="h-4 w-4 text-green-500" /> Open source
            </span>
          </motion.div>
        </div>
      </motion.section>

      <footer className="border-t py-6 sm:py-8 lg:py-10">
        <div className="mx-auto flex max-w-7xl flex-col items-center justify-between gap-3 px-4 text-xs text-gray-600 sm:flex-row sm:gap-4 sm:text-sm lg:px-5">
          <div className="flex items-center gap-2">
            <div className="h-5 w-5 rounded-lg bg-gradient-to-br from-indigo-500 via-sky-500 to-emerald-400 sm:h-6 sm:w-6" />
            <span>OpenEcon.ai © {new Date().getFullYear()}</span>
          </div>
          <div className="flex items-center gap-3 sm:gap-4">
            <a href="/examples" className="hover:text-gray-900">
              Examples
            </a>
            <a href="/docs" className="hover:text-gray-900">
              Docs
            </a>
            <a href="https://github.com/hanlulong/econ-data-mcp" target="_blank" rel="noopener noreferrer" className="hover:text-gray-900">
              GitHub
            </a>
            <a href="mailto:contact@openecon.ai" className="hover:text-gray-900">
              Contact
            </a>
          </div>
        </div>
      </footer>
    </div>
  )
}
