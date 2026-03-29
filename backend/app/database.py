import sqlite3
import os
import json
from datetime import datetime


DB_PATH = os.getenv("DB_PATH", "/home/ubuntu/mega-transfer-system/backend/data/app.db")


def get_db_path():
    # Use /data/app.db if deployed with persistent volume
    if os.path.isdir("/data"):
        return "/data/app.db"
    return DB_PATH


def get_connection():
    db_path = get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            hashed_password TEXT NOT NULL,
            is_admin INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')),
            max_downloads_per_day INTEGER DEFAULT 50
        );

        CREATE TABLE IF NOT EXISTS transfer_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            mega_link TEXT NOT NULL,
            excluded_files TEXT DEFAULT '[]',
            storage_target TEXT DEFAULT 'idrive',
            status TEXT DEFAULT 'queued',
            progress INTEGER DEFAULT 0,
            total_files INTEGER DEFAULT 0,
            uploaded_files INTEGER DEFAULT 0,
            folder_path TEXT DEFAULT '',
            download_slug TEXT UNIQUE,
            error_message TEXT DEFAULT '',
            current_file TEXT DEFAULT '',
            download_speed TEXT DEFAULT '',
            upload_speed TEXT DEFAULT '',
            downloaded_files INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            telegram_sent INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS content_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            storage_key TEXT NOT NULL,
            storage_target TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (job_id) REFERENCES transfer_jobs(id)
        );

        CREATE TABLE IF NOT EXISTS download_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            content_item_id INTEGER,
            job_id INTEGER,
            ip_address TEXT,
            user_agent TEXT,
            downloaded_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (job_id) REFERENCES transfer_jobs(id)
        );

        CREATE TABLE IF NOT EXISTS allowed_access (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            job_id INTEGER NOT NULL,
            granted_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id),
            FOREIGN KEY (job_id) REFERENCES transfer_jobs(id),
            UNIQUE(user_id, job_id)
        );
    """)

    conn.commit()

    # Migrate existing databases: add new columns if missing
    try:
        cols = [row[1] for row in conn.execute("PRAGMA table_info(transfer_jobs)").fetchall()]
        for col, default in [
            ("current_file", "''"), ("download_speed", "''"),
            ("upload_speed", "''"), ("downloaded_files", "0"),
        ]:
            if col not in cols:
                conn.execute(f"ALTER TABLE transfer_jobs ADD COLUMN {col} TEXT DEFAULT {default}")
        conn.commit()
    except Exception:
        pass

    conn.close()
