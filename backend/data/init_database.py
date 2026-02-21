#!/usr/bin/env python3
"""
Initialize the catalog database with schema

Usage:
    python backend/data/init_database.py
"""

import sqlite3
import os
import sys

def init_database():
    """Initialize the catalog database with schema"""

    # Paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(script_dir, "catalog.db")
    schema_path = os.path.join(script_dir, "schemas", "indicators.sql")

    print(f"📁 Database path: {db_path}")
    print(f"📁 Schema path: {schema_path}")

    # Check if schema file exists
    if not os.path.exists(schema_path):
        print(f"❌ Schema file not found: {schema_path}")
        sys.exit(1)

    # Read schema
    print(f"\n📖 Reading schema...")
    with open(schema_path, 'r') as f:
        schema_sql = f.read()

    # Create database
    print(f"🔧 Creating database...")
    conn = sqlite3.connect(db_path)

    try:
        # Execute schema
        conn.executescript(schema_sql)
        conn.commit()

        # Verify tables were created
        cursor = conn.cursor()
        cursor.execute("""
            SELECT name FROM sqlite_master
            WHERE type='table'
            ORDER BY name
        """)
        tables = cursor.fetchall()

        print(f"\n✅ Database initialized successfully!")
        print(f"📊 Created {len(tables)} tables:")
        for table in tables:
            cursor.execute(f"SELECT COUNT(*) FROM {table[0]}")
            count = cursor.fetchone()[0]
            print(f"   - {table[0]}: {count} rows")

        # Check FTS5 support
        cursor.execute("SELECT * FROM indicators_fts LIMIT 0")
        print(f"\n✅ FTS5 full-text search enabled")

    except sqlite3.Error as e:
        print(f"\n❌ Database error: {e}")
        sys.exit(1)
    finally:
        conn.close()

    print(f"\n🎉 Database ready at: {db_path}")
    print(f"📏 Database size: {os.path.getsize(db_path)} bytes")

if __name__ == "__main__":
    init_database()
