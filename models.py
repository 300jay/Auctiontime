"""
models.py — Database initialization and connection helpers.
Uses sqlite3 stdlib; no SQLAlchemy.
"""

import sqlite3
import os
from werkzeug.security import generate_password_hash

# AUCTION_DB_PATH lets tests (or deployments) use a different database file.
DB_PATH = os.environ.get("AUCTION_DB_PATH") or os.path.join(os.path.dirname(__file__), "auction_app.db")


def get_db(path=None):
    """Return a new sqlite3 connection with Row factory."""
    conn = sqlite3.connect(path or DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create all tables and seed the default admin auctioneer."""
    conn = get_db()
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'bidder',
            team_name TEXT DEFAULT '',
            team_logo TEXT DEFAULT '',
            color_primary TEXT DEFAULT '#9d00ff',
            color_secondary TEXT DEFAULT '#1a0033',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS auctions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            auctioneer_id INTEGER NOT NULL,
            status TEXT DEFAULT 'pending',
            invite_token TEXT UNIQUE NOT NULL,
            budget_per_team REAL DEFAULT 100.0,
            min_squad_size INTEGER DEFAULT 0,
            max_squad_size INTEGER DEFAULT 0,
            current_player_id INTEGER DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (auctioneer_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS auction_bidders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            budget_remaining REAL DEFAULT 100.0,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(auction_id, user_id),
            FOREIGN KEY (auction_id) REFERENCES auctions(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS auction_players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id INTEGER NOT NULL,
            csv_offset TEXT NOT NULL,
            player_name TEXT NOT NULL,
            role TEXT DEFAULT '',
            bowling_style TEXT DEFAULT '',
            ovr INTEGER DEFAULT 0,
            base_price REAL DEFAULT 0.20,
            status TEXT DEFAULT 'pending',
            sold_to INTEGER DEFAULT NULL,
            sold_price REAL DEFAULT NULL,
            queue_order INTEGER DEFAULT 0,
            stats_json TEXT DEFAULT '{}',
            round_start_bid_id INTEGER DEFAULT 0,
            FOREIGN KEY (auction_id) REFERENCES auctions(id),
            FOREIGN KEY (sold_to) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS bids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_player_id INTEGER NOT NULL,
            bidder_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (auction_player_id) REFERENCES auction_players(id),
            FOREIGN KEY (bidder_id) REFERENCES users(id)
        );
    """)

    # Seed default auctioneer admin account if not already present
    existing = cur.execute(
        "SELECT id FROM users WHERE username = ?", ("admin",)
    ).fetchone()
    if not existing:
        cur.execute(
            """INSERT INTO users (username, password_hash, role)
               VALUES (?, ?, ?)""",
            ("admin", generate_password_hash("admin123"), "auctioneer"),
        )

    # Run migrations if needed
    for ddl in (
        "ALTER TABLE auctions ADD COLUMN min_squad_size INTEGER DEFAULT 0",
        "ALTER TABLE auctions ADD COLUMN max_squad_size INTEGER DEFAULT 0",
        "ALTER TABLE auction_players ADD COLUMN stats_json TEXT DEFAULT '{}'",
        # bids with id <= this value belong to an earlier round of the same lot
        # (e.g. a player that went unsold and was offered again)
        "ALTER TABLE auction_players ADD COLUMN round_start_bid_id INTEGER DEFAULT 0",
    ):
        try:
            cur.execute(ddl)
        except sqlite3.OperationalError:
            pass  # column already exists

    # Never more than one live lot per auction. If old data somehow has several,
    # keep the newest and send the rest back to the queue so the index can exist.
    cur.execute(
        """UPDATE auction_players SET status = 'pending'
           WHERE status = 'active' AND id NOT IN (
               SELECT MAX(id) FROM auction_players WHERE status = 'active'
               GROUP BY auction_id)"""
    )
    cur.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS one_active_lot_per_auction
           ON auction_players(auction_id) WHERE status = 'active'"""
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bids_player ON bids(auction_player_id, amount)")
    # Keep the legacy pointer in sync with the real live lot
    cur.execute(
        """UPDATE auctions SET current_player_id = (
               SELECT id FROM auction_players
               WHERE auction_id = auctions.id AND status = 'active')"""
    )

    conn.commit()
    conn.close()
