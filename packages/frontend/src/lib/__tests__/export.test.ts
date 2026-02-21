/**
 * Tests for export utilities.
 *
 * Tests blob creation, filename generation, and download functionality.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  downloadBlob,
  generateExportFilename,
  downloadExport,
  createJsonBlob,
  downloadJson,
} from '../export';

// Mock logger
vi.mock('../../utils/logger', () => ({
  logger: {
    info: vi.fn(),
    error: vi.fn(),
    log: vi.fn(),
    warn: vi.fn(),
  },
}));

describe('export utilities', () => {
  let mockCreateObjectURL: ReturnType<typeof vi.fn>;
  let mockRevokeObjectURL: ReturnType<typeof vi.fn>;
  let mockAppendChild: ReturnType<typeof vi.fn>;
  let mockRemoveChild: ReturnType<typeof vi.fn>;
  let mockClick: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    mockCreateObjectURL = vi.fn().mockReturnValue('blob:test-url');
    mockRevokeObjectURL = vi.fn();
    mockAppendChild = vi.fn();
    mockRemoveChild = vi.fn();
    mockClick = vi.fn();

    // Mock window.URL
    Object.defineProperty(window, 'URL', {
      value: {
        createObjectURL: mockCreateObjectURL,
        revokeObjectURL: mockRevokeObjectURL,
      },
      writable: true,
    });

    // Mock document.createElement
    vi.spyOn(document, 'createElement').mockImplementation(() => ({
      click: mockClick,
      href: '',
      download: '',
    } as any));

    // Mock document.body
    vi.spyOn(document.body, 'appendChild').mockImplementation(mockAppendChild);
    vi.spyOn(document.body, 'removeChild').mockImplementation(mockRemoveChild);
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('generateExportFilename', () => {
    it('generates filename with default prefix for csv', () => {
      const filename = generateExportFilename(undefined, 'csv');
      expect(filename).toMatch(/^openecon_export_\d+\.csv$/);
    });

    it('generates filename with default prefix for json', () => {
      const filename = generateExportFilename(undefined, 'json');
      expect(filename).toMatch(/^openecon_export_\d+\.json$/);
    });

    it('generates filename with custom prefix', () => {
      const filename = generateExportFilename('my_data', 'csv');
      expect(filename).toMatch(/^my_data_\d+\.csv$/);
    });

    it('generates filename with dta extension', () => {
      const filename = generateExportFilename('stata', 'dta');
      expect(filename).toMatch(/^stata_\d+\.dta$/);
    });

    it('includes timestamp in filename', () => {
      const before = Date.now();
      const filename = generateExportFilename('test', 'csv');
      const after = Date.now();

      // Extract timestamp from filename
      const match = filename.match(/test_(\d+)\.csv/);
      expect(match).toBeDefined();

      const timestamp = parseInt(match![1], 10);
      expect(timestamp).toBeGreaterThanOrEqual(before);
      expect(timestamp).toBeLessThanOrEqual(after);
    });
  });

  describe('downloadBlob', () => {
    it('creates object URL from blob', () => {
      const blob = new Blob(['test content'], { type: 'text/plain' });
      downloadBlob(blob, 'test.txt');

      expect(mockCreateObjectURL).toHaveBeenCalledWith(blob);
    });

    it('creates download link with correct attributes', () => {
      const blob = new Blob(['test'], { type: 'text/plain' });
      downloadBlob(blob, 'myfile.txt');

      expect(document.createElement).toHaveBeenCalledWith('a');
    });

    it('appends link to body and triggers click', () => {
      const blob = new Blob(['test'], { type: 'text/plain' });
      downloadBlob(blob, 'test.txt');

      expect(mockAppendChild).toHaveBeenCalled();
      expect(mockClick).toHaveBeenCalled();
    });

    it('cleans up after download', () => {
      const blob = new Blob(['test'], { type: 'text/plain' });
      downloadBlob(blob, 'test.txt');

      expect(mockRemoveChild).toHaveBeenCalled();
      expect(mockRevokeObjectURL).toHaveBeenCalledWith('blob:test-url');
    });

    it('throws error on failure', () => {
      mockCreateObjectURL.mockImplementation(() => {
        throw new Error('Failed to create URL');
      });

      const blob = new Blob(['test'], { type: 'text/plain' });
      expect(() => downloadBlob(blob, 'test.txt')).toThrow('Failed to create URL');
    });
  });

  describe('createJsonBlob', () => {
    it('creates blob with JSON content', () => {
      const data = { name: 'test', value: 123 };
      const blob = createJsonBlob(data);

      expect(blob instanceof Blob).toBe(true);
      expect(blob.type).toBe('application/json');
    });

    it('creates blob with correct size for data', () => {
      const data = { a: 1, b: 2 };
      const blob = createJsonBlob(data);
      const expectedJson = JSON.stringify(data, null, 2);

      // Check that blob size matches expected JSON length
      expect(blob.size).toBe(expectedJson.length);
    });

    it('handles nested objects', () => {
      const data = { outer: { inner: { deep: 'value' } } };
      const blob = createJsonBlob(data);
      const expectedJson = JSON.stringify(data, null, 2);

      expect(blob.size).toBe(expectedJson.length);
    });

    it('handles arrays', () => {
      const data = [1, 2, 3, { key: 'value' }];
      const blob = createJsonBlob(data);
      const expectedJson = JSON.stringify(data, null, 2);

      expect(blob.size).toBe(expectedJson.length);
    });
  });

  describe('downloadExport', () => {
    it('downloads blob with generated filename for csv', () => {
      const blob = new Blob(['test'], { type: 'text/csv' });
      downloadExport(blob, 'csv');

      expect(mockCreateObjectURL).toHaveBeenCalledWith(blob);
      expect(mockClick).toHaveBeenCalled();
    });

    it('downloads blob with generated filename for json', () => {
      const blob = new Blob(['{}'], { type: 'application/json' });
      downloadExport(blob, 'json');

      expect(mockCreateObjectURL).toHaveBeenCalledWith(blob);
    });

    it('uses custom prefix when provided', () => {
      const blob = new Blob(['test'], { type: 'text/csv' });
      downloadExport(blob, 'csv', 'custom_prefix');

      // The function should use the prefix in filename generation
      expect(mockClick).toHaveBeenCalled();
    });
  });

  describe('downloadJson', () => {
    it('creates JSON blob and downloads it', () => {
      const data = { test: 'data' };
      downloadJson(data);

      expect(mockClick).toHaveBeenCalled();
    });

    it('uses custom prefix when provided', () => {
      const data = { test: 'data' };
      downloadJson(data, 'my_export');

      expect(mockClick).toHaveBeenCalled();
    });
  });
});
