import sqlite3

DB_PATH = "sweatequity.db"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      email TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL,
      role TEXT CHECK(role IN ('company','developer')) NOT NULL,
      first_name TEXT,
      last_name TEXT,
      home_address TEXT,
      linkedin_url TEXT,
      algo_addr TEXT NOT NULL,
      algo_mnemonic TEXT NOT NULL
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS companies (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      user_id INTEGER NOT NULL UNIQUE,
      name TEXT NOT NULL,
      asset_id INTEGER,
      app_id INTEGER,
      unit_name TEXT,
      asset_name TEXT,
      supply INTEGER,
      equity_pct REAL DEFAULT 0.15,
      valuation_gbp REAL DEFAULT 1000000.0,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(user_id) REFERENCES users(id)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      company_id INTEGER NOT NULL,
      title TEXT NOT NULL,
      description TEXT,
      upfront_gbp_pence INTEGER NOT NULL,
      token_amount INTEGER NOT NULL,
      developer_id INTEGER,
      status TEXT CHECK(status IN ('open','picked','awaiting_verification','paid','closed')) DEFAULT 'open',
      developer_marked_complete INTEGER DEFAULT 0,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      FOREIGN KEY(company_id) REFERENCES companies(id),
      FOREIGN KEY(developer_id) REFERENCES users(id)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS developer_holdings (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      developer_id INTEGER NOT NULL,
      company_id INTEGER NOT NULL,
      asset_id INTEGER NOT NULL,
      tokens_held INTEGER NOT NULL DEFAULT 0,
      updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      UNIQUE(developer_id, company_id, asset_id),
      FOREIGN KEY(developer_id) REFERENCES users(id),
      FOREIGN KEY(company_id) REFERENCES companies(id)
    )""")
    conn.commit()
    conn.close()
