-- OpenEcon Catalog Database Schema
-- SQLite with FTS5 for fast full-text search

-- Main indicators table
CREATE TABLE IF NOT EXISTS indicators (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    unit TEXT,
    frequency TEXT,
    geo_coverage TEXT,
    start_date TEXT,
    end_date TEXT,
    keywords TEXT,
    category TEXT,
    popularity_score REAL DEFAULT 0,
    data_quality_score REAL DEFAULT 0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    metadata_json TEXT,
    UNIQUE(provider, code)
);

-- Full-text search index using FTS5
CREATE VIRTUAL TABLE IF NOT EXISTS indicators_fts USING fts5(
    name,
    description,
    keywords,
    content=indicators,
    content_rowid=id
);

-- Triggers to keep FTS in sync with main table
CREATE TRIGGER IF NOT EXISTS indicators_ai AFTER INSERT ON indicators BEGIN
  INSERT INTO indicators_fts(rowid, name, description, keywords)
  VALUES (new.id, new.name, new.description, new.keywords);
END;

CREATE TRIGGER IF NOT EXISTS indicators_ad AFTER DELETE ON indicators BEGIN
  DELETE FROM indicators_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS indicators_au AFTER UPDATE ON indicators BEGIN
  UPDATE indicators_fts SET
    name = new.name,
    description = new.description,
    keywords = new.keywords
  WHERE rowid = new.id;
END;

-- Index metadata table (tracks indexing status)
CREATE TABLE IF NOT EXISTS index_metadata (
    provider TEXT PRIMARY KEY,
    last_indexed TIMESTAMP,
    indicator_count INTEGER,
    status TEXT,
    error_message TEXT
);

-- Performance indexes
CREATE INDEX IF NOT EXISTS idx_provider ON indicators(provider);
CREATE INDEX IF NOT EXISTS idx_code ON indicators(code);
CREATE INDEX IF NOT EXISTS idx_geo ON indicators(geo_coverage);
CREATE INDEX IF NOT EXISTS idx_category ON indicators(category);
CREATE INDEX IF NOT EXISTS idx_popularity ON indicators(popularity_score DESC);
CREATE INDEX IF NOT EXISTS idx_updated ON indicators(last_updated DESC);
