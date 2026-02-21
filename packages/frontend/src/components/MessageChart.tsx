import { useMemo, useState, memo, useCallback } from 'react'
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

import type { NormalizedData } from '../types'
import { logger } from '../utils/logger'

// Professional color palette with carefully selected colors
const CHART_COLORS = ['#4f46e5', '#0ea5e9', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#ec4899', '#06b6d4']

// Professional style configuration
const CHART_STYLE = {
  grid: '#f1f5f9',
  axis: '#64748b',
  text: '#334155',
  tooltipBg: 'rgba(255, 255, 255, 0.96)',
  tooltipBorder: '#e2e8f0',
}

type ChartRow = {
  date: string
  [key: string]: number | string | null
}

// Helper to get a short label for an indicator
function getShortIndicatorLabel(indicator: string): string {
  // Common indicator name shortenings
  const shortenings: Record<string, string> = {
    'Consumer Price Index for All Urban Consumers: All Items in U.S. City Average': 'CPI',
    'Unemployment Rate': 'Unemployment',
    'Gross Domestic Product': 'GDP',
    'Real Gross Domestic Product': 'Real GDP',
    'Personal Consumption Expenditures': 'PCE',
    'Industrial Production Index': 'Industrial Production',
  }

  // Check for exact match first
  if (shortenings[indicator]) return shortenings[indicator]

  // Check for partial matches
  for (const [key, value] of Object.entries(shortenings)) {
    if (indicator.includes(key)) return value
  }

  // Truncate long names
  if (indicator.length > 30) {
    return indicator.substring(0, 27) + '...'
  }

  return indicator
}

// Determine best labels for series (use indicators when countries are same)
function getSeriesLabels(data: NormalizedData[]): string[] {
  if (data.length === 0) return []
  if (data.length === 1) {
    const series = data[0]
    return [series.metadata.country || series.metadata.indicator || 'Series']
  }

  // Check if all countries are the same
  const countries = data.map(s => s.metadata.country)
  const uniqueCountries = new Set(countries.filter(Boolean))

  // If all series have the same country (or no country), use indicator names
  if (uniqueCountries.size <= 1) {
    return data.map((series) => {
      const indicator = series.metadata.indicator || 'Series'
      return getShortIndicatorLabel(indicator)
    })
  }

  // If countries are different, use country names
  return data.map((series, index) =>
    series.metadata.country || `${series.metadata.indicator || 'Series'} ${index + 1}`
  )
}

// Pure functions moved outside component for performance
function transformDataForChart(data: NormalizedData[]) {
  if (data.length === 0) return []

  const labels = getSeriesLabels(data)

  if (data.length === 1) {
    const series = data[0]
    const label = labels[0]
    return series.data.map((point) => ({
      date: point.date.substring(0, 10),
      [label]: point.value,
    }))
  }

  const dateMap = new Map<string, ChartRow>()

  data.forEach((series, index) => {
    const label = labels[index]
    series.data.forEach((point) => {
      const date = point.date.substring(0, 10)
      if (!dateMap.has(date)) {
        dateMap.set(date, { date })
      }
      dateMap.get(date)![label] = point.value
    })
  })

  const rows = Array.from(dateMap.values())
  rows.sort((a, b) => a.date.localeCompare(b.date))
  return rows
}

function getSeriesNames(data: NormalizedData[]): string[] {
  return getSeriesLabels(data)
}

function formatNumber(value: number | null | undefined) {
  if (value === null || value === undefined) return 'N/A'
  const absValue = Math.abs(value)
  if (absValue >= 1e12) return `${(value / 1e12).toFixed(2)}T`
  if (absValue >= 1e9) return `${(value / 1e9).toFixed(2)}B`
  if (absValue >= 1e6) return `${(value / 1e6).toFixed(2)}M`
  if (absValue >= 1e3) return `${(value / 1e3).toFixed(2)}K`
  return value.toFixed(2)
}

function formatYAxisTick(value: number) {
  const absValue = Math.abs(value)
  if (absValue >= 1e12) return `${(value / 1e12).toFixed(1)}T`
  if (absValue >= 1e9) return `${(value / 1e9).toFixed(1)}B`
  if (absValue >= 1e6) return `${(value / 1e6).toFixed(1)}M`
  if (absValue >= 1e3) return `${(value / 1e3).toFixed(1)}K`
  if (absValue >= 1) return value.toFixed(0)
  return value.toFixed(2)
}

function formatXAxisTick(dateStr: string, frequency?: string): string {
  if (!frequency) return dateStr

  const lowerFreq = frequency.toLowerCase()

  // Annual: show only year (e.g., "2020")
  if (lowerFreq === 'annual' || lowerFreq === 'yearly') {
    return dateStr.substring(0, 4)
  }

  // Quarterly: show year-quarter (e.g., "2020-Q1")
  if (lowerFreq === 'quarterly' || lowerFreq === 'quarter') {
    const year = dateStr.substring(0, 4)
    const month = parseInt(dateStr.substring(5, 7), 10)
    const quarter = Math.ceil(month / 3)
    return `${year}-Q${quarter}`
  }

  // Monthly: show year-month (e.g., "2020-01")
  if (lowerFreq === 'monthly' || lowerFreq === 'month') {
    return dateStr.substring(0, 7)
  }

  // Daily: show full date (e.g., "2020-01-15")
  if (lowerFreq === 'daily' || lowerFreq === 'day') {
    return dateStr.substring(0, 10)
  }

  // Default: return full date
  return dateStr.substring(0, 10)
}

function formatSimpleDateTime(dateTimeStr: string): string {
  try {
    // Parse format like "Sun, 19 Oct 2025 00:02:31 +0000"
    const parts = dateTimeStr.split(',')
    if (parts.length < 2) return dateTimeStr

    const datePart = parts[1].trim().split(' ')
    if (datePart.length < 3) return dateTimeStr

    const day = datePart[0]
    const month = datePart[1]
    const year = datePart[2]

    return `${month} ${day}, ${year}`
  } catch {
    return dateTimeStr
  }
}

// Copy button component with visual feedback
function CopyButton({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false)

  const handleCopy = async () => {
    if (!navigator?.clipboard) {
      logger.warn('Clipboard API not available')
      return
    }
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch (err) {
      logger.error('Failed to copy', err)
    }
  }

  return (
    <button
      type="button"
      className={`copy-btn ${copied ? 'copied' : ''}`}
      onClick={handleCopy}
      title={copied ? 'Copied!' : `Copy ${label}`}
    >
      {copied ? (
        <>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <polyline points="20 6 9 17 4 12"></polyline>
          </svg>
          <span>Copied!</span>
        </>
      ) : (
        <>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>
          </svg>
          <span>{label}</span>
        </>
      )}
    </button>
  )
}

interface MessageChartProps {
  data: NormalizedData[]
  chartType: 'line' | 'bar' | 'scatter' | 'table'
  onChartTypeChange: (chartType: 'line' | 'bar' | 'scatter' | 'table') => void
  onExport: (format: 'csv' | 'json' | 'dta' | 'python') => void
  onShare?: () => void
}

export const MessageChart = memo(function MessageChart({ data, chartType, onChartTypeChange, onExport, onShare }: MessageChartProps) {
  const chartData = useMemo(() => transformDataForChart(data), [data])
  const seriesNames = useMemo(() => getSeriesNames(data), [data])
  const [showApiUrls, setShowApiUrls] = useState(false)

  const dataPoints = data[0]?.data.length ?? 0
  const showLine = dataPoints > 3

  // Get frequency from first series for x-axis formatting
  const frequency = data[0]?.metadata.frequency
  const xAxisFormatter = (value: string) => formatXAxisTick(value, frequency)

  // Check if this is categorical data (should display as table only)
  const isCategoricalData = frequency?.toLowerCase() === 'categorical'

  // Check if this is exchange rate data (currency codes used as dates) - for backwards compatibility
  const isExchangeRateData = isCategoricalData || (data.length === 1 &&
    data[0].metadata.unit === 'exchange rate' &&
    data[0].data.length > 1 &&
    data[0].data.every(point => /^[A-Z]{3}$/.test(point.date)))

  // Professional custom tooltip component
  const CustomTooltip = useCallback(({ active, payload, label }: any) => {
    if (active && payload && payload.length) {
      return (
        <div
          style={{
            backgroundColor: CHART_STYLE.tooltipBg,
            backdropFilter: 'blur(8px)',
            border: `1px solid ${CHART_STYLE.tooltipBorder}`,
            borderRadius: '10px',
            boxShadow: '0 10px 25px -5px rgba(0, 0, 0, 0.1), 0 8px 10px -6px rgba(0, 0, 0, 0.1)',
            padding: '12px 14px',
            minWidth: '160px',
          }}
        >
          <p style={{
            fontSize: '12px',
            fontWeight: 600,
            color: CHART_STYLE.text,
            marginBottom: '8px',
            paddingBottom: '8px',
            borderBottom: `1px solid ${CHART_STYLE.grid}`,
          }}>
            {xAxisFormatter(label)}
          </p>
          {payload.map((entry: any, index: number) => (
            <div
              key={index}
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                gap: '16px',
                fontSize: '12px',
                padding: '3px 0',
              }}
            >
              <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span
                  style={{
                    width: '8px',
                    height: '8px',
                    borderRadius: '50%',
                    backgroundColor: entry.color,
                  }}
                />
                <span style={{ color: CHART_STYLE.axis }}>{entry.name}</span>
              </span>
              <span style={{ fontWeight: 600, color: CHART_STYLE.text }}>
                {formatNumber(typeof entry.value === 'number' ? entry.value : Number(entry.value))}
              </span>
            </div>
          ))}
        </div>
      )
    }
    return null
  }, [xAxisFormatter])

  // Gradient definitions for area charts
  const chartGradients = (
    <defs>
      {CHART_COLORS.map((color, index) => (
        <linearGradient key={`gradient-${index}`} id={`chartGradient${index}`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="5%" stopColor={color} stopOpacity={0.3} />
          <stop offset="95%" stopColor={color} stopOpacity={0.05} />
        </linearGradient>
      ))}
    </defs>
  )

  const renderChart = (): JSX.Element => {
    if (chartType === 'line') {
      return (
        <LineChart data={chartData} margin={{ top: 10, right: 30, left: 10, bottom: 10 }}>
          {chartGradients}
          <CartesianGrid
            strokeDasharray="3 3"
            stroke={CHART_STYLE.grid}
            vertical={false}
          />
          <XAxis
            dataKey="date"
            stroke={CHART_STYLE.axis}
            style={{ fontSize: '11px' }}
            tickFormatter={xAxisFormatter}
            tickLine={false}
            axisLine={{ stroke: CHART_STYLE.grid }}
            dy={8}
          />
          <YAxis
            stroke={CHART_STYLE.axis}
            style={{ fontSize: '11px' }}
            tickFormatter={formatYAxisTick}
            tickLine={false}
            axisLine={false}
            width={60}
            dx={-5}
          />
          <Tooltip content={<CustomTooltip />} />
          <Legend
            wrapperStyle={{ paddingTop: '16px', fontSize: '12px' }}
            iconType="circle"
            iconSize={8}
          />
          {seriesNames.map((name, index) => (
            <Line
              key={name}
              type="monotone"
              dataKey={name}
              stroke={CHART_COLORS[index % CHART_COLORS.length]}
              strokeWidth={2.5}
              dot={{ r: 4, fill: '#fff', stroke: CHART_COLORS[index % CHART_COLORS.length], strokeWidth: 2 }}
              activeDot={{ r: 6, fill: CHART_COLORS[index % CHART_COLORS.length], stroke: '#fff', strokeWidth: 2 }}
              animationDuration={530}
              animationEasing="ease-out"
            />
          ))}
        </LineChart>
      )
    }

    if (chartType === 'bar') {
      return (
        <BarChart data={chartData} margin={{ top: 10, right: 30, left: 10, bottom: 10 }}>
          <CartesianGrid
            strokeDasharray="3 3"
            stroke={CHART_STYLE.grid}
            vertical={false}
          />
          <XAxis
            dataKey="date"
            stroke={CHART_STYLE.axis}
            style={{ fontSize: '11px' }}
            tickFormatter={xAxisFormatter}
            tickLine={false}
            axisLine={{ stroke: CHART_STYLE.grid }}
            dy={8}
          />
          <YAxis
            stroke={CHART_STYLE.axis}
            style={{ fontSize: '11px' }}
            tickFormatter={formatYAxisTick}
            tickLine={false}
            axisLine={false}
            width={60}
            dx={-5}
          />
          <Tooltip content={<CustomTooltip />} />
          <Legend
            wrapperStyle={{ paddingTop: '16px', fontSize: '12px' }}
            iconType="rect"
            iconSize={10}
          />
          {seriesNames.map((name, index) => (
            <Bar
              key={name}
              dataKey={name}
              fill={CHART_COLORS[index % CHART_COLORS.length]}
              radius={[6, 6, 0, 0]}
              animationDuration={400}
              animationEasing="ease-out"
            />
          ))}
        </BarChart>
      )
    }

    if (chartType === 'table') {
      return (
        <div className="data-table-wrapper">
          <table className="data-table">
            <thead>
              <tr>
                <th>{isExchangeRateData ? 'Currency' : 'Date'}</th>
                {seriesNames.map((name) => (
                  <th key={name}>{name}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {chartData.map((row, idx) => (
                <tr key={idx}>
                  <td>{isExchangeRateData ? row.date : xAxisFormatter(row.date)}</td>
                  {seriesNames.map((name) => (
                    <td key={name}>
                      {row[name] !== null && row[name] !== undefined
                        ? typeof row[name] === 'number'
                          ? (row[name] as number).toLocaleString(undefined, {
                              minimumFractionDigits: 2,
                              maximumFractionDigits: 2,
                            })
                          : row[name]
                        : 'N/A'}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )
    }

    return (
      <LineChart data={chartData}>
        {chartGradients}
        <Tooltip content={<CustomTooltip />} />
      </LineChart>
    )
  }

  // Helper function to check if information is already conveyed in indicator name
  const isInfoRedundant = (indicator: string, info: string): boolean => {
    const indicatorLower = indicator.toLowerCase();
    const infoLower = info.toLowerCase();
    // Check if the info (or a key part of it) is already in the indicator name
    return indicatorLower.includes(infoLower) ||
           (infoLower === 'annual' && indicatorLower.includes('annual')) ||
           (infoLower === 'percent change' && (indicatorLower.includes('growth') || indicatorLower.includes('% change') || indicatorLower.includes('percent change'))) ||
           (infoLower === 'rate' && indicatorLower.includes('rate')) ||
           (infoLower === 'index' && indicatorLower.includes('index'));
  };

  // Helper to check if unit is redundant with frequency or dataType
  const isUnitRedundant = (unit: string, frequency: string, dataType: string | undefined, indicator: string): boolean => {
    const unitLower = unit.toLowerCase();
    const freqLower = frequency?.toLowerCase() || '';
    // Unit like "annual %" is redundant when frequency is "annual" and it's a percent/growth
    if (unitLower.includes(freqLower) && (unitLower.includes('%') || unitLower.includes('percent'))) {
      return true;
    }
    // Unit like "%" is redundant when dataType is "Percent Change" or indicator has "growth"
    if ((unitLower === '%' || unitLower === 'percent' || unitLower === 'annual %') &&
        (dataType?.toLowerCase().includes('percent') || indicator.toLowerCase().includes('growth'))) {
      return true;
    }
    return false;
  };

  return (
    <div className="message-data">
      <div className="data-summary">
        <ul className="series-list">
          {data.map((series, index) => {
            const indicator = series.metadata.indicator || '';
            const frequency = series.metadata.frequency || '';
            const unit = series.metadata.unit || '';
            const dataType = series.metadata.dataType;

            // Determine what to show (avoid redundancy)
            const showFrequency = frequency && !isInfoRedundant(indicator, frequency);
            const showUnit = unit && !isUnitRedundant(unit, frequency, dataType, indicator);
            const showDataTypeBadge = dataType && !isInfoRedundant(indicator, dataType) && !isInfoRedundant(unit, dataType);

            return (
              <li key={`${series.metadata.indicator}-${index}`} className="series-item">
                <span className="series-header">
                  <strong>{series.metadata.country || series.metadata.indicator}</strong>
                  {series.metadata.indicator && series.metadata.country ? ` - ${series.metadata.indicator}` : ''}
                  {' '}({series.data.length} observations)
                  {showFrequency ? ` • ${frequency}` : ''}
                  {showUnit ? ` • ${unit}` : ''}
                  {isExchangeRateData && series.metadata.lastUpdated ? ` • as of ${formatSimpleDateTime(series.metadata.lastUpdated)}` : ''}
                </span>
                {/* Enhanced metadata badges */}
                {(series.metadata.seasonalAdjustment || showDataTypeBadge || series.metadata.priceType) && (
                  <span className="series-badges">
                    {series.metadata.seasonalAdjustment && (
                      <span className="metadata-badge badge-seasonal" title="Seasonal Adjustment">
                        {series.metadata.seasonalAdjustment.includes('Not') || series.metadata.seasonalAdjustment.includes('not') ? 'NSA' : 'SA'}
                      </span>
                    )}
                    {showDataTypeBadge && (
                      <span className="metadata-badge badge-type" title={`Data Type: ${series.metadata.dataType}`}>
                        {series.metadata.dataType}
                      </span>
                    )}
                    {series.metadata.priceType && (
                      <span className="metadata-badge badge-price" title={series.metadata.priceType}>
                        {series.metadata.priceType.includes('Real') || series.metadata.priceType.includes('Constant') ? 'Real' :
                         series.metadata.priceType.includes('Nominal') || series.metadata.priceType.includes('Current') ? 'Nominal' :
                         series.metadata.priceType.includes('PPP') ? 'PPP' : series.metadata.priceType}
                      </span>
                    )}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      </div>

      <div className="data-viz-header">
        {!isCategoricalData && (
          <div className="chart-type-selector">
            {showLine && (
              <button
                type="button"
                className={`chart-type-btn ${chartType === 'line' ? 'active' : ''}`}
                onClick={() => onChartTypeChange('line')}
              >
                📈 Line
              </button>
            )}
            <button
              type="button"
              className={`chart-type-btn ${chartType === 'bar' ? 'active' : ''}`}
              onClick={() => onChartTypeChange('bar')}
            >
              📊 Bar
            </button>
            <button
              type="button"
              className={`chart-type-btn ${chartType === 'table' ? 'active' : ''}`}
              onClick={() => onChartTypeChange('table')}
            >
              📋 Table
            </button>
          </div>
        )}
        <div className="export-buttons">
          <button type="button" onClick={() => onExport('csv')} className="export-btn">
            CSV
          </button>
          <button type="button" onClick={() => onExport('json')} className="export-btn">
            JSON
          </button>
          <button type="button" onClick={() => onExport('dta')} className="export-btn" title="Export as Stata data file">
            DTA
          </button>
          <button type="button" onClick={() => onExport('python')} className="export-btn" title="Generate standalone Python code">
            Python
          </button>
          {onShare && (
            <button type="button" onClick={onShare} className="export-btn share-btn" title="Share this query">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="18" cy="5" r="3"></circle>
                <circle cx="6" cy="12" r="3"></circle>
                <circle cx="18" cy="19" r="3"></circle>
                <line x1="8.59" y1="13.51" x2="15.42" y2="17.49"></line>
                <line x1="15.41" y1="6.51" x2="8.59" y2="10.49"></line>
              </svg>
              Share
            </button>
          )}
        </div>
      </div>

      {chartData.length > 0 && (
        <div className="chart-container" style={{ background: '#fafbfc', borderRadius: '12px', padding: '16px 8px 8px 0' }}>
          <ResponsiveContainer width="100%" height={380}>
            {renderChart()}
          </ResponsiveContainer>
        </div>
      )}

      <div className="api-urls-section">
        <button type="button" className="api-section-toggle" onClick={() => setShowApiUrls((prev) => !prev)}>
          <span className="toggle-icon">{showApiUrls ? '▼' : '▶'}</span>
          <span>API Data Sources</span>
          {data.length > 0 && (
            <span className="provider-names">
              {Array.from(new Set(data.map(d => d.metadata.source))).join(', ')}
            </span>
          )}
        </button>
        {showApiUrls && (
          <div className="api-urls-content">
            {/* Group by source provider - compact layout */}
            {(() => {
              const sourcesByProvider = new Map<string, { sourceUrl?: string; series: typeof data }>();

              data.forEach((series) => {
                const provider = series.metadata.source;
                if (!sourcesByProvider.has(provider)) {
                  sourcesByProvider.set(provider, {
                    sourceUrl: series.metadata.sourceUrl,
                    series: [],
                  });
                }
                sourcesByProvider.get(provider)!.series.push(series);
              });

              return Array.from(sourcesByProvider.entries()).map(([provider, { sourceUrl, series: providerSeries }]) => {
                // Get API URL for copy functionality (the actual API endpoint)
                const getApiUrl = (s: typeof providerSeries[0]) => {
                  if (s.metadata.apiUrl && !s.metadata.apiUrl.includes('(POST')) return s.metadata.apiUrl;
                  return null;
                };

                // Collect all API URLs for "Copy All APIs" functionality
                const allApiUrls = providerSeries
                  .map(s => getApiUrl(s))
                  .filter(Boolean)
                  .join('\n');

                const singleApiUrl = providerSeries.length === 1 ? getApiUrl(providerSeries[0]) : null;

                return (
                  <div key={provider} className="api-source-block">
                    <div className="api-source-row">
                      <span className="api-source-provider">{provider}</span>
                      {providerSeries.length > 1 && (
                        <span className="api-source-count">{providerSeries.length} series</span>
                      )}
                      <div className="api-source-actions">
                        {/* Only show provider-level Verify for single series (multi-series have individual Verify links) */}
                        {providerSeries.length === 1 && sourceUrl && (
                          <a
                            href={sourceUrl}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="api-action-btn api-action-verify"
                            title="View on provider website"
                          >
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                              <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                              <polyline points="15 3 21 3 21 9"></polyline>
                              <line x1="10" y1="14" x2="21" y2="3"></line>
                            </svg>
                            Verify
                          </a>
                        )}
                        {providerSeries.length === 1 && singleApiUrl && (
                          <CopyButton text={singleApiUrl} label="API" />
                        )}
                        {providerSeries.length > 1 && allApiUrls && (
                          <CopyButton text={allApiUrls} label="Copy All APIs" />
                        )}
                      </div>
                    </div>
                    {/* Show individual series for multi-series queries */}
                    {providerSeries.length > 1 && (
                      <div className="api-series-list">
                        {providerSeries.map((s, idx) => {
                          // Check if all series have the same country - if so, use indicator to distinguish
                          const allSameCountry = providerSeries.every(
                            ps => ps.metadata.country === providerSeries[0].metadata.country
                          );
                          let seriesLabel: string;
                          if (allSameCountry && s.metadata.indicator) {
                            // Use country + short indicator when countries are the same
                            const shortIndicator = getShortIndicatorLabel(s.metadata.indicator);
                            seriesLabel = s.metadata.country ? `${s.metadata.country} ${shortIndicator}` : shortIndicator;
                          } else {
                            seriesLabel = s.metadata.country || s.metadata.indicator || `Series ${idx + 1}`;
                          }
                          const verifyUrl = s.metadata.sourceUrl;
                          // Use apiUrl for Copy (the actual API endpoint), not sourceUrl
                          const apiUrl = s.metadata.apiUrl && !s.metadata.apiUrl.includes('(POST') ? s.metadata.apiUrl : null;
                          return (
                            <div key={idx} className="api-series-item">
                              <span className="api-series-name">{seriesLabel}</span>
                              <div className="api-series-actions">
                                {verifyUrl && (
                                  <a
                                    href={verifyUrl}
                                    target="_blank"
                                    rel="noopener noreferrer"
                                    className="api-action-btn api-action-verify"
                                    title="View on provider website"
                                  >
                                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                      <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
                                      <polyline points="15 3 21 3 21 9"></polyline>
                                      <line x1="10" y1="14" x2="21" y2="3"></line>
                                    </svg>
                                    Verify
                                  </a>
                                )}
                                {apiUrl && <CopyButton text={apiUrl} label="API" />}
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              });
            })()}
          </div>
        )}
      </div>
    </div>
  )
})
