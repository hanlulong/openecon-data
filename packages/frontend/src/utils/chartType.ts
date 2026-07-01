import { NormalizedData } from '../types'

// Decide how a set of series should be rendered from structural properties of
// the data only (no per-indicator rules). Shared by live chat responses and
// history replay so both render the same data the same way.
export function determineChartType(data: NormalizedData[]): 'line' | 'bar' | 'table' {
  if (data.length === 0) return 'line'

  const firstSeries = data[0]
  const dataPoints = firstSeries.data.length
  const frequency = firstSeries.metadata.frequency

  // Check if this is exchange rate data (currency codes as dates)
  const isExchangeRateData =
    data.length === 1 &&
    firstSeries.metadata.unit === 'exchange rate' &&
    firstSeries.data.length > 1 &&
    firstSeries.data.every((point) => /^[A-Z]{3}$/.test(point.date))

  if (isExchangeRateData) {
    return 'table'
  }

  // Table for data with widely varying scales across multiple series.
  // Loop instead of Math.min(...spread): multi-year daily series can exceed
  // the engine's argument-count limit and throw.
  if (data.length > 1) {
    let minValue = Infinity
    let maxValue = -Infinity
    for (const series of data) {
      for (const point of series.data) {
        if (typeof point.value !== 'number' || !Number.isFinite(point.value)) continue
        const v = Math.abs(point.value)
        if (v > 0 && v < minValue) minValue = v
        if (v > maxValue) maxValue = v
      }
    }

    // If the ratio between max and min values is very large (e.g., comparing
    // EUR ~0.9 to JPY ~110) suggest table view for better readability
    if (Number.isFinite(minValue) && maxValue / minValue > 50) {
      return 'table'
    }
  }

  // Bar chart for annual data with few years or categorical comparisons
  if (frequency === 'annual' && dataPoints <= 10) {
    return 'bar'
  }

  // Line chart for time series (default for most economic data)
  return 'line'
}
