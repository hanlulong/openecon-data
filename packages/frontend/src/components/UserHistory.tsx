import { useState, useEffect } from 'react';
import { api } from '../services/api';
import { UserQueryHistory, NormalizedData } from '../types';
import { MessageChart } from './MessageChart';
import { downloadExport } from '../lib/export';
import { logger } from '../utils/logger';
import './UserHistory.css';

interface UserHistoryProps {
  onClose?: () => void;
}

export const UserHistory = ({ onClose }: UserHistoryProps) => {
  const [history, setHistory] = useState<UserQueryHistory[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState('');
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [isClearing, setIsClearing] = useState(false);

  useEffect(() => {
    loadHistory();
  }, []);

  const loadHistory = async () => {
    try {
      setIsLoading(true);
      const response = await api.getUserHistory();
      setHistory(response.history);
    } catch (err: any) {
      setError(err.response?.data?.error || 'Failed to load history');
    } finally {
      setIsLoading(false);
    }
  };

  const handleClearHistory = async () => {
    if (!confirm('Are you sure you want to clear all your query history? This action cannot be undone.')) {
      return;
    }

    try {
      setIsClearing(true);
      setError('');
      await api.clearUserHistory();
      setHistory([]);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to clear history');
    } finally {
      setIsClearing(false);
    }
  };

  const formatDate = (dateString: string) => {
    const date = new Date(dateString);
    return date.toLocaleString('en-US', {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  };

  const toggleExpand = (id: string) => {
    setExpandedId(expandedId === id ? null : id);
  };

  const handleExport = async (data: NormalizedData[], format: 'csv' | 'json' | 'dta' | 'python') => {
    try {
      // 'python' is not a data export format, ignore it
      if (format === 'python') {
        return;
      }
      const blob = await api.exportData(data, format);
      downloadExport(blob, format);
    } catch (error) {
      logger.error('Export error:', error);
    }
  };

  return (
    <div className="history-modal-overlay" onClick={onClose}>
      <div className="history-modal" onClick={(e) => e.stopPropagation()}>
        <div className="history-header">
          <h2>Query History</h2>
          <div className="history-header-actions">
            {history.length > 0 && (
              <button
                className="history-clear-btn"
                onClick={handleClearHistory}
                disabled={isClearing}
                title="Clear all history"
              >
                {isClearing ? 'Clearing...' : 'Clear History'}
              </button>
            )}
            {onClose && (
              <button className="history-close" onClick={onClose}>
                ×
              </button>
            )}
          </div>
        </div>

        <div className="history-content">
          {isLoading && <div className="history-loading">Loading history...</div>}

          {error && <div className="history-error">{error}</div>}

          {!isLoading && !error && history.length === 0 && (
            <div className="history-empty">
              <p>No query history yet.</p>
              <p>Start making queries to build your history!</p>
            </div>
          )}

          {!isLoading && !error && history.length > 0 && (
            <div className="history-list">
              {history.map((item) => (
                <div key={item.id} className="history-item">
                  <div
                    className="history-item-header"
                    onClick={() => toggleExpand(item.id)}
                  >
                    <div className="history-item-query">
                      <strong>{item.query}</strong>
                    </div>
                    <div className="history-item-meta">
                      <span className="history-item-date">
                        {formatDate(item.timestamp)}
                      </span>
                      {item.data && (
                        <span className="history-item-count">
                          {item.data.length} series
                        </span>
                      )}
                      <button className="history-expand-btn">
                        {expandedId === item.id ? '▼' : '▶'}
                      </button>
                    </div>
                  </div>

                  {expandedId === item.id && item.data && (
                    <div className="history-item-details">
                      <div className="history-item-summary">
                        {item.data.map((d, i) => (
                          <div key={i} className="history-series">
                            • {d.metadata.indicator} ({d.data.length} observations)
                          </div>
                        ))}
                      </div>
                      <MessageChart
                        data={item.data}
                        chartType="line"
                        onChartTypeChange={() => {}}
                        onExport={(format) => handleExport(item.data!, format)}
                      />
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
