/**
 * Tests for MessageChart component.
 *
 * Tests chart rendering, chart type selection, export functionality,
 * and data transformation.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import React from 'react';
import { MessageChart } from '../MessageChart';
import type { NormalizedData } from '../../types';

// Mock recharts to avoid canvas/SVG rendering issues in tests
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="responsive-container">{children}</div>
  ),
  LineChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="line-chart">{children}</div>
  ),
  BarChart: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="bar-chart">{children}</div>
  ),
  Line: () => <div data-testid="line" />,
  Bar: () => <div data-testid="bar" />,
  XAxis: () => <div data-testid="x-axis" />,
  YAxis: () => <div data-testid="y-axis" />,
  CartesianGrid: () => <div data-testid="cartesian-grid" />,
  Tooltip: () => <div data-testid="tooltip" />,
  Legend: () => <div data-testid="legend" />,
}));

// Mock logger
vi.mock('../../utils/logger', () => ({
  logger: {
    log: vi.fn(),
    error: vi.fn(),
    warn: vi.fn(),
  },
}));

// ============================================================================
// Test Data Fixtures
// ============================================================================

const mockSingleSeriesData: NormalizedData[] = [
  {
    metadata: {
      source: 'FRED',
      indicator: 'Gross Domestic Product',
      country: 'US',
      frequency: 'quarterly',
      unit: 'Billions of Dollars',
      lastUpdated: '2024-01-01T00:00:00.000Z',
      seriesId: 'GDP',
    },
    data: [
      { date: '2023-01-01', value: 26000 },
      { date: '2023-04-01', value: 26500 },
      { date: '2023-07-01', value: 27000 },
      { date: '2023-10-01', value: 27500 },
    ],
  },
];

const mockMultiSeriesData: NormalizedData[] = [
  {
    metadata: {
      source: 'World Bank',
      indicator: 'GDP',
      country: 'US',
      frequency: 'annual',
      unit: 'USD',
      lastUpdated: '2024-01-01',
    },
    data: [
      { date: '2020-01-01', value: 21000 },
      { date: '2021-01-01', value: 23000 },
      { date: '2022-01-01', value: 25000 },
    ],
  },
  {
    metadata: {
      source: 'World Bank',
      indicator: 'GDP',
      country: 'China',
      frequency: 'annual',
      unit: 'USD',
      lastUpdated: '2024-01-01',
    },
    data: [
      { date: '2020-01-01', value: 14700 },
      { date: '2021-01-01', value: 17700 },
      { date: '2022-01-01', value: 18100 },
    ],
  },
];

const mockExchangeRateData: NormalizedData[] = [
  {
    metadata: {
      source: 'ExchangeRate-API',
      indicator: 'Exchange Rate',
      country: 'US',
      frequency: 'categorical',
      unit: 'exchange rate',
      lastUpdated: 'Sun, 19 Oct 2025 00:02:31 +0000',
    },
    data: [
      { date: 'EUR', value: 0.85 },
      { date: 'GBP', value: 0.73 },
      { date: 'JPY', value: 110.5 },
    ],
  },
];

const mockEmptyData: NormalizedData[] = [];

const mockMinimalData: NormalizedData[] = [
  {
    metadata: {
      source: 'FRED',
      indicator: 'Test',
      country: 'US',
      frequency: 'daily',
      unit: '',
      lastUpdated: '',
    },
    data: [
      { date: '2024-01-01', value: 100 },
    ],
  },
];

// ============================================================================
// Component Tests
// ============================================================================

describe('MessageChart', () => {
  const mockOnChartTypeChange = vi.fn();
  const mockOnExport = vi.fn();
  const mockOnShare = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('rendering', () => {
    it('renders line chart by default', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      expect(screen.getByTestId('line-chart')).toBeDefined();
    });

    it('renders bar chart when chartType is bar', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="bar"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      expect(screen.getByTestId('bar-chart')).toBeDefined();
    });

    it('renders table view when chartType is table', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="table"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      expect(screen.getByRole('table')).toBeDefined();
    });

    it('displays data summary with series information', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      expect(screen.getByText(/US/)).toBeDefined();
      expect(screen.getByText(/4 observations/)).toBeDefined();
    });

    it('renders multiple series correctly', () => {
      render(
        <MessageChart
          data={mockMultiSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      // May have multiple occurrences of country names in legend
      expect(screen.getAllByText(/US/).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/China/).length).toBeGreaterThan(0);
    });
  });

  describe('chart type selection', () => {
    it('calls onChartTypeChange when line button is clicked', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="bar"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      const lineButton = screen.getByRole('button', { name: /line/i });
      fireEvent.click(lineButton);

      expect(mockOnChartTypeChange).toHaveBeenCalledWith('line');
    });

    it('calls onChartTypeChange when bar button is clicked', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      const barButton = screen.getByRole('button', { name: /bar/i });
      fireEvent.click(barButton);

      expect(mockOnChartTypeChange).toHaveBeenCalledWith('bar');
    });

    it('calls onChartTypeChange when table button is clicked', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      const tableButton = screen.getByRole('button', { name: /table/i });
      fireEvent.click(tableButton);

      expect(mockOnChartTypeChange).toHaveBeenCalledWith('table');
    });

    it('highlights active chart type button', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="bar"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      const barButton = screen.getByRole('button', { name: /bar/i });
      expect(barButton.className).toContain('active');
    });

    it('hides line chart option when data has 3 or fewer points', () => {
      render(
        <MessageChart
          data={mockMinimalData}
          chartType="bar"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      // Line button should not be present
      expect(screen.queryByRole('button', { name: /line/i })).toBeNull();
    });
  });

  describe('export functionality', () => {
    it('calls onExport with csv when CSV button is clicked', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      const csvButton = screen.getByRole('button', { name: /csv/i });
      fireEvent.click(csvButton);

      expect(mockOnExport).toHaveBeenCalledWith('csv');
    });

    it('calls onExport with json when JSON button is clicked', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      const jsonButton = screen.getByRole('button', { name: /json/i });
      fireEvent.click(jsonButton);

      expect(mockOnExport).toHaveBeenCalledWith('json');
    });

    it('calls onExport with dta when DTA button is clicked', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      const dtaButton = screen.getByRole('button', { name: /dta/i });
      fireEvent.click(dtaButton);

      expect(mockOnExport).toHaveBeenCalledWith('dta');
    });

    it('calls onExport with python when Python button is clicked', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      const pythonButton = screen.getByRole('button', { name: /python/i });
      fireEvent.click(pythonButton);

      expect(mockOnExport).toHaveBeenCalledWith('python');
    });
  });

  describe('share functionality', () => {
    it('renders share button when onShare is provided', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
          onShare={mockOnShare}
        />
      );

      expect(screen.getByRole('button', { name: /share/i })).toBeDefined();
    });

    it('does not render share button when onShare is not provided', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      expect(screen.queryByRole('button', { name: /share/i })).toBeNull();
    });

    it('calls onShare when share button is clicked', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
          onShare={mockOnShare}
        />
      );

      const shareButton = screen.getByRole('button', { name: /share/i });
      fireEvent.click(shareButton);

      expect(mockOnShare).toHaveBeenCalled();
    });
  });

  describe('table view', () => {
    it('renders table headers correctly for single series', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="table"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      expect(screen.getByRole('columnheader', { name: /date/i })).toBeDefined();
    });

    it('renders table data correctly', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="table"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      // Check for formatted values
      const table = screen.getByRole('table');
      expect(table).toBeDefined();
    });

    it('shows Currency header for exchange rate data', () => {
      render(
        <MessageChart
          data={mockExchangeRateData}
          chartType="table"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      expect(screen.getByRole('columnheader', { name: /currency/i })).toBeDefined();
    });
  });

  describe('categorical/exchange rate data', () => {
    it('hides chart type selector for categorical data', () => {
      render(
        <MessageChart
          data={mockExchangeRateData}
          chartType="table"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      // Chart type buttons should not be present for categorical data
      expect(screen.queryByRole('button', { name: /line/i })).toBeNull();
      expect(screen.queryByRole('button', { name: /bar/i })).toBeNull();
    });
  });

  describe('API data sources section', () => {
    it('renders API data sources toggle', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      expect(screen.getByText(/API Data Sources/i)).toBeDefined();
    });

    it('shows provider name in collapsed view', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      expect(screen.getByText(/FRED/)).toBeDefined();
    });

    it('expands API sources section on click', () => {
      render(
        <MessageChart
          data={mockSingleSeriesData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      const toggleButton = screen.getByText(/API Data Sources/i).closest('button');
      expect(toggleButton).not.toBeNull();
      fireEvent.click(toggleButton!);

      // After clicking, the content should be visible (may have multiple FRED instances)
      expect(screen.getAllByText(/FRED/).length).toBeGreaterThan(0);
    });
  });

  describe('metadata badges', () => {
    it('displays seasonal adjustment badge when present', () => {
      const dataWithSA: NormalizedData[] = [
        {
          ...mockSingleSeriesData[0],
          metadata: {
            ...mockSingleSeriesData[0].metadata,
            seasonalAdjustment: 'Seasonally Adjusted',
          },
        },
      ];

      render(
        <MessageChart
          data={dataWithSA}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      expect(screen.getByText('SA')).toBeDefined();
    });

    it('displays NSA badge for not seasonally adjusted data', () => {
      const dataWithNSA: NormalizedData[] = [
        {
          ...mockSingleSeriesData[0],
          metadata: {
            ...mockSingleSeriesData[0].metadata,
            seasonalAdjustment: 'Not Seasonally Adjusted',
          },
        },
      ];

      render(
        <MessageChart
          data={dataWithNSA}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      expect(screen.getByText('NSA')).toBeDefined();
    });
  });

  describe('empty data handling', () => {
    it('renders without crashing with empty data', () => {
      render(
        <MessageChart
          data={mockEmptyData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      // Should still render the component structure
      expect(screen.getByText(/API Data Sources/i)).toBeDefined();
    });

    it('does not render chart container when data is empty', () => {
      render(
        <MessageChart
          data={mockEmptyData}
          chartType="line"
          onChartTypeChange={mockOnChartTypeChange}
          onExport={mockOnExport}
        />
      );

      expect(screen.queryByTestId('responsive-container')).toBeNull();
    });
  });
});
