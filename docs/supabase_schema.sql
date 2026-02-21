-- econ-data-mcp Supabase Database Schema
-- Run this in Supabase SQL Editor after creating your project

-- ============================================
-- User Queries Table
-- Tracks all queries from both registered and anonymous users
-- ============================================
CREATE TABLE IF NOT EXISTS user_queries (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

  -- User identification (null for anonymous)
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  session_id TEXT, -- For anonymous users

  -- Query details
  query TEXT NOT NULL,
  conversation_id TEXT,
  pro_mode BOOLEAN DEFAULT FALSE,

  -- Request/Response data
  intent JSONB, -- Parsed intent from LLM
  response_data JSONB, -- NormalizedData array
  code_execution JSONB, -- Pro Mode execution results
  error_message TEXT,

  -- Metadata
  processing_time_ms FLOAT,
  user_agent TEXT,
  ip_address INET,

  -- Timestamps
  created_at TIMESTAMPTZ DEFAULT NOW(),

  -- Indexes for fast queries
  CONSTRAINT valid_user_or_session CHECK (user_id IS NOT NULL OR session_id IS NOT NULL)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_user_queries_user_id ON user_queries(user_id);
CREATE INDEX IF NOT EXISTS idx_user_queries_session_id ON user_queries(session_id);
CREATE INDEX IF NOT EXISTS idx_user_queries_created_at ON user_queries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_user_queries_conversation_id ON user_queries(conversation_id);

-- ============================================
-- Anonymous Sessions Table
-- Tracks anonymous user sessions
-- ============================================
CREATE TABLE IF NOT EXISTS anonymous_sessions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id TEXT UNIQUE NOT NULL,

  -- Session metadata
  user_agent TEXT,
  ip_address INET,

  -- Conversion tracking
  converted_to_user_id UUID REFERENCES auth.users(id) ON DELETE SET NULL,

  -- Timestamps
  first_seen TIMESTAMPTZ DEFAULT NOW(),
  last_seen TIMESTAMPTZ DEFAULT NOW(),

  -- Activity metrics
  query_count INTEGER DEFAULT 0,
  pro_mode_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_anonymous_sessions_session_id ON anonymous_sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_anonymous_sessions_last_seen ON anonymous_sessions(last_seen DESC);

-- ============================================
-- User Profile Extensions
-- Additional user metadata beyond auth.users
-- ============================================
CREATE TABLE IF NOT EXISTS user_profiles (
  id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,

  -- Usage metrics
  total_queries INTEGER DEFAULT 0,
  pro_mode_queries INTEGER DEFAULT 0,

  -- Preferences
  preferences JSONB DEFAULT '{}'::jsonb,

  -- Timestamps
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================
-- Conversations Table
-- Track conversation threads
-- ============================================
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
  session_id TEXT,

  -- Conversation data
  messages JSONB DEFAULT '[]'::jsonb,

  -- Timestamps
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),

  CONSTRAINT valid_conversation_owner CHECK (user_id IS NOT NULL OR session_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id);
CREATE INDEX IF NOT EXISTS idx_conversations_session_id ON conversations(session_id);
CREATE INDEX IF NOT EXISTS idx_conversations_updated_at ON conversations(updated_at DESC);

-- ============================================
-- Row-Level Security (RLS) Policies
-- ============================================

-- Enable RLS on all tables
ALTER TABLE user_queries ENABLE ROW LEVEL SECURITY;
ALTER TABLE anonymous_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;

-- user_queries policies
CREATE POLICY "Users can view own queries"
  ON user_queries FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Service role can view all queries"
  ON user_queries FOR ALL
  TO service_role
  USING (true);

CREATE POLICY "Anon can insert with session_id"
  ON user_queries FOR INSERT
  TO anon
  WITH CHECK (session_id IS NOT NULL AND user_id IS NULL);

CREATE POLICY "Authenticated can insert own queries"
  ON user_queries FOR INSERT
  TO authenticated
  WITH CHECK (auth.uid() = user_id);

-- anonymous_sessions policies
CREATE POLICY "Anyone can read own session"
  ON anonymous_sessions FOR SELECT
  TO anon
  USING (true);

CREATE POLICY "Anyone can insert session"
  ON anonymous_sessions FOR INSERT
  TO anon
  WITH CHECK (true);

CREATE POLICY "Anyone can update own session"
  ON anonymous_sessions FOR UPDATE
  TO anon
  USING (true);

CREATE POLICY "Service role can view all sessions"
  ON anonymous_sessions FOR ALL
  TO service_role
  USING (true);

-- user_profiles policies
CREATE POLICY "Users can view own profile"
  ON user_profiles FOR SELECT
  TO authenticated
  USING (auth.uid() = id);

CREATE POLICY "Users can update own profile"
  ON user_profiles FOR UPDATE
  TO authenticated
  USING (auth.uid() = id);

CREATE POLICY "Service role can manage all profiles"
  ON user_profiles FOR ALL
  TO service_role
  USING (true);

-- conversations policies
CREATE POLICY "Users can view own conversations"
  ON conversations FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Users can insert own conversations"
  ON conversations FOR INSERT
  TO authenticated
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "Users can update own conversations"
  ON conversations FOR UPDATE
  TO authenticated
  USING (auth.uid() = user_id);

CREATE POLICY "Anon can manage conversations with session_id"
  ON conversations FOR ALL
  TO anon
  USING (session_id IS NOT NULL AND user_id IS NULL);

CREATE POLICY "Service role can manage all conversations"
  ON conversations FOR ALL
  TO service_role
  USING (true);

-- ============================================
-- Functions and Triggers
-- ============================================

-- Auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_user_profiles_updated_at
  BEFORE UPDATE ON user_profiles
  FOR EACH ROW
  EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_conversations_updated_at
  BEFORE UPDATE ON conversations
  FOR EACH ROW
  EXECUTE FUNCTION update_updated_at_column();

-- Auto-create user profile on signup
CREATE OR REPLACE FUNCTION handle_new_user()
RETURNS TRIGGER AS $$
BEGIN
  INSERT INTO user_profiles (id)
  VALUES (NEW.id);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_auth_user_created
  AFTER INSERT ON auth.users
  FOR EACH ROW
  EXECUTE FUNCTION handle_new_user();

-- Update query counts
CREATE OR REPLACE FUNCTION increment_query_count()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.user_id IS NOT NULL THEN
    UPDATE user_profiles
    SET
      total_queries = total_queries + 1,
      pro_mode_queries = CASE WHEN NEW.pro_mode THEN pro_mode_queries + 1 ELSE pro_mode_queries END
    WHERE id = NEW.user_id;
  END IF;

  IF NEW.session_id IS NOT NULL THEN
    UPDATE anonymous_sessions
    SET
      query_count = query_count + 1,
      pro_mode_count = CASE WHEN NEW.pro_mode THEN pro_mode_count + 1 ELSE pro_mode_count END,
      last_seen = NOW()
    WHERE session_id = NEW.session_id;
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

CREATE TRIGGER on_query_inserted
  AFTER INSERT ON user_queries
  FOR EACH ROW
  EXECUTE FUNCTION increment_query_count();

-- ============================================
-- Admin Views for Analytics
-- ============================================

-- Daily query statistics
CREATE OR REPLACE VIEW daily_query_stats AS
SELECT
  DATE(created_at) as date,
  COUNT(*) as total_queries,
  COUNT(DISTINCT user_id) as unique_users,
  COUNT(DISTINCT session_id) as unique_sessions,
  COUNT(CASE WHEN pro_mode THEN 1 END) as pro_mode_queries,
  AVG(processing_time_ms) as avg_processing_time_ms,
  COUNT(CASE WHEN error_message IS NOT NULL THEN 1 END) as error_count
FROM user_queries
GROUP BY DATE(created_at)
ORDER BY date DESC;

-- User activity summary
CREATE OR REPLACE VIEW user_activity_summary AS
SELECT
  u.id,
  u.email,
  u.created_at as signed_up_at,
  up.total_queries,
  up.pro_mode_queries,
  MAX(uq.created_at) as last_query_at,
  COUNT(DISTINCT uq.conversation_id) as conversation_count
FROM auth.users u
LEFT JOIN user_profiles up ON u.id = up.id
LEFT JOIN user_queries uq ON u.id = uq.user_id
GROUP BY u.id, u.email, u.created_at, up.total_queries, up.pro_mode_queries
ORDER BY last_query_at DESC NULLS LAST;

-- Anonymous session summary
CREATE OR REPLACE VIEW anonymous_session_summary AS
SELECT
  s.session_id,
  s.first_seen,
  s.last_seen,
  s.query_count,
  s.pro_mode_count,
  s.converted_to_user_id IS NOT NULL as converted,
  u.email as converted_to_email
FROM anonymous_sessions s
LEFT JOIN auth.users u ON s.converted_to_user_id = u.id
ORDER BY s.last_seen DESC;

-- ============================================
-- Sample Queries for Admin Dashboard
-- ============================================

-- Most active users (last 30 days)
-- SELECT * FROM user_activity_summary WHERE last_query_at > NOW() - INTERVAL '30 days' LIMIT 20;

-- Recent queries
-- SELECT * FROM user_queries ORDER BY created_at DESC LIMIT 50;

-- Anonymous user conversion rate
-- SELECT
--   COUNT(DISTINCT session_id) as total_sessions,
--   COUNT(DISTINCT CASE WHEN converted_to_user_id IS NOT NULL THEN session_id END) as converted_sessions,
--   ROUND(100.0 * COUNT(DISTINCT CASE WHEN converted_to_user_id IS NOT NULL THEN session_id END) / COUNT(DISTINCT session_id), 2) as conversion_rate_pct
-- FROM anonymous_sessions;

-- Popular queries
-- SELECT
--   query,
--   COUNT(*) as count,
--   AVG(processing_time_ms) as avg_time_ms
-- FROM user_queries
-- WHERE created_at > NOW() - INTERVAL '7 days'
-- GROUP BY query
-- ORDER BY count DESC
-- LIMIT 20;
