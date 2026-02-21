/**
 * Export and download utilities for data files
 * Extracts duplicate blob download logic from ChatPage and UserHistory
 */

import { logger } from '../utils/logger';

export type ExportFormat = 'csv' | 'json' | 'dta';

/**
 * Creates a blob URL and triggers a download
 *
 * @param blob - The blob to download
 * @param filename - The name of the file to download
 */
export function downloadBlob(blob: Blob, filename: string): void {
  try {
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement('a');

    link.href = url;
    link.download = filename;

    // Append to body to ensure it works in all browsers
    document.body.appendChild(link);
    link.click();

    // Clean up
    document.body.removeChild(link);
    window.URL.revokeObjectURL(url);

    logger.info('File downloaded successfully:', filename);
  } catch (error) {
    logger.error('Failed to download file:', error);
    throw error;
  }
}

/**
 * Generates a timestamped filename for exports
 *
 * @param prefix - Prefix for the filename (e.g., 'openecon_export')
 * @param format - File format (csv or json)
 * @returns Filename with timestamp
 */
export function generateExportFilename(prefix: string = 'openecon_export', format: ExportFormat): string {
  const timestamp = Date.now();
  return `${prefix}_${timestamp}.${format}`;
}

/**
 * Downloads data as a file with automatic filename generation
 *
 * @param blob - The blob to download
 * @param format - File format (csv or json)
 * @param prefix - Optional filename prefix
 */
export function downloadExport(blob: Blob, format: ExportFormat, prefix?: string): void {
  const filename = generateExportFilename(prefix, format);
  downloadBlob(blob, filename);
}

/**
 * Converts a plain object to a JSON blob
 *
 * @param data - Data to convert
 * @returns JSON blob
 */
export function createJsonBlob(data: any): Blob {
  const jsonString = JSON.stringify(data, null, 2);
  return new Blob([jsonString], { type: 'application/json' });
}

/**
 * Downloads data as JSON
 *
 * @param data - Data to export
 * @param prefix - Optional filename prefix
 */
export function downloadJson(data: any, prefix?: string): void {
  const blob = createJsonBlob(data);
  downloadExport(blob, 'json', prefix);
}
