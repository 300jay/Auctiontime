"""
app.py — Cricket Auction Flask backend.
Run with: python app.py  (listens on port 5050)
"""

import csv
import io
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from functools import wraps

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from models import get_db, init_db

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(__file__)
CSV_PATH = os.environ.get("PLAYERS_CSV") or os.path.join(os.path.dirname(BASE_DIR), "players_stats.csv")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def _load_secret_key() -> str:
    """Stable secret key so logins survive restarts (env var, else a key file)."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    key_path = os.path.join(BASE_DIR, ".secret_key")
    try:
        with open(key_path) as f:
            key = f.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    key = secrets.token_hex(32)
    try:
        with open(key_path, "w") as f:
            f.write(key)
        os.chmod(key_path, 0o600)
    except OSError:
        pass
    return key


app = Flask(__name__)
app.secret_key = _load_secret_key()
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # 4 MB

# Same-origin only by default (the websocket now relies on the login session, so
# a wildcard origin would let other websites drive it). Set CORS_ORIGINS="a,b" to allow more.
_cors = os.environ.get("CORS_ORIGINS")
socketio = SocketIO(
    app,
    cors_allowed_origins=[o.strip() for o in _cors.split(",")] if _cors else None,
    async_mode="threading",
)

# ---------------------------------------------------------------------------
# Auction rules
# ---------------------------------------------------------------------------
MIN_BID_INCREMENT = 0.20   # in Crores (₹0.20 Cr); beat current bid by at least this much
MAX_BID_AMOUNT = 10**12    # sanity cap so absurd numbers are rejected cleanly

# All auction state lives in the database (never in process memory), so a restart
# or a second browser tab can never disagree with what was actually recorded.
# This lock serialises bid/sell/next/... within the process; every mutation also
# runs inside a BEGIN IMMEDIATE transaction as a second line of defence.
_auction_lock = threading.RLock()


@app.template_filter("format_cr")
def format_cr(val):
    if val is None or val == "":
        return "₹0.00 Cr"
    try:
        num = float(val)
        if num >= 10000:
            num = num / 10000000.0
        return f"₹{num:.2f} Cr"
    except (ValueError, TypeError):
        return "₹0.00 Cr"


class AuctionError(Exception):
    """A rule violation that should be reported to the caller and rolled back."""


@contextmanager
def immediate_tx():
    """Write transaction that takes the DB write lock up front and rolls back on error."""
    db = get_db()
    db.isolation_level = None  # manual transaction control
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.execute("COMMIT")
    except BaseException:
        try:
            db.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        db.close()


def _display_name(row) -> str:
    return (row["team_name"] or row["username"]) if row else ""


def get_base_price_by_ovr(ovr: int | float) -> float:
    try:
        ovr = int(ovr)
    except (ValueError, TypeError):
        ovr = 50
    if ovr >= 85:
        return 2.0
    elif ovr >= 80:
        return 1.5
    elif ovr >= 75:
        return 1.0
    elif ovr >= 70:
        return 0.5
    elif ovr >= 65:
        return 0.3
    else:
        return 0.2


BATTING_LABELS = {
    "bat_power": "Power",
    "power": "Power",
    "bat_attacking": "Attacking",
    "attacking": "Attacking",
    "bat_control": "Control",
    "control": "Control",
    "bat_drive": "Drive",
    "drive": "Drive",
    "bat_defending": "Defending",
    "defending": "Defending",
    "bat_footwork": "Footwork",
    "footwork": "Footwork",
    "bat_judgement": "Judgement",
    "judgement": "Judgement",
    "bat_hook_pull": "Hook / Pull",
    "hook_pull": "Hook / Pull",
    "bat_cut": "Cut Shot",
    "cut": "Cut Shot",
    "bat_glance": "Glance",
    "glance": "Glance",
    "bat_sweep": "Sweep",
    "sweep": "Sweep",
    "bat_special_shots": "Special Shots",
    "special_shots": "Special Shots",
    "bat_running_speed": "Running Speed",
    "running_speed": "Running Speed",
}

FIELDING_LABELS = {
    "field_wicketkeeping_stumps": "Wicketkeeping (Up to Stumps)",
    "wicketkeeping_stumps": "Wicketkeeping (Up to Stumps)",
    "field_wicketkeeping_back": "Wicketkeeping (Standing Back)",
    "wicketkeeping_back": "Wicketkeeping (Standing Back)",
    "field_reflex_catching": "Reflex Catching",
    "reflex_catching": "Reflex Catching",
    "field_catching": "Catching",
    "catching": "Catching",
    "field_high_ball_catching": "High Ball Catching",
    "high_ball_catching": "High Ball Catching",
    "field_ground_fielding": "Ground Fielding",
    "ground_fielding": "Ground Fielding",
    "field_throw_accuracy": "Throw Accuracy",
    "throw_accuracy": "Throw Accuracy",
    "field_throw_strength": "Throw Strength",
    "throw_strength": "Throw Strength",
    "field_diving": "Diving",
    "diving": "Diving",
    "field_coverage": "Coverage",
    "coverage": "Coverage",
}


def get_bowling_delivery_labels(bowling_style: str) -> list[str]:
    """Map slot 1-9 to exact delivery names based on bowling_style."""
    b = (bowling_style or "").strip().lower()
    if "leg" in b:
        return [
            "Off Break / Orthodox",  # Slot 1
            "Arm Ball / Slider",     # Slot 2
            "Top Spinner",           # Slot 3
            "Special Delivery",      # Slot 4
            "Turn",                  # Slot 5
            "Flight",                # Slot 6
            "Drift",                 # Slot 7
            "Effort Ball",           # Slot 8
            "Bounce",                # Slot 9
        ]
    elif "off" in b:
        return [
            "Off Break / Orthodox",  # Slot 1
            "Arm Ball / Slider",     # Slot 2
            "Top Spinner",           # Slot 3
            "Special Delivery",      # Slot 4
            "Turn",                  # Slot 5
            "Flight",                # Slot 6
            "Drift",                 # Slot 7
            "Effort Ball",           # Slot 8
            "Bounce",                # Slot 9
        ]
    else:
        return [
            "Straight Seam",         # Slot 1
            "In Swing",              # Slot 2
            "Out Swing",             # Slot 3
            "Leg Cutter",            # Slot 4
            "Off Cutter",            # Slot 5
            "Cross Seam",            # Slot 6
            "Slower Ball",           # Slot 7
            "Yorker",                # Slot 8
            "Bouncer",               # Slot 9
        ]


def extract_bowling_deliveries(row: dict, bowling_style: str) -> list[dict]:
    """Extract slots 1-9 and assign the style-specific delivery name."""
    labels = get_bowling_delivery_labels(bowling_style)
    deliveries = []
    for slot_idx in range(1, 10):
        slot_str = f"slot{slot_idx}"
        val = 0
        for k, v in row.items():
            k_lower = k.lower()
            if slot_str in k_lower and ("bowl" in k_lower or "rate" in k_lower or "rating" in k_lower):
                try:
                    val = int(float(v))
                except (ValueError, TypeError):
                    val = 0
                break
        deliveries.append({
            "slot": slot_idx,
            "name": labels[slot_idx - 1],
            "rating": val,
        })
    return deliveries


def extract_full_player_stats(row: dict, bowling_style: str) -> dict:
    delivs = extract_bowling_deliveries(row, bowling_style)
    batting_stats = {}
    fielding_stats = {}
    for k, v in row.items():
        k_lower = k.lower()
        if ("bat" in k_lower or "field" in k_lower) and ("rating" in k_lower or "rate" in k_lower):
            try:
                clean_k = k.replace("rating_", "").replace("rate_", "")
                val = int(float(v))
                if "bat" in k_lower:
                    batting_stats[clean_k] = val
                else:
                    fielding_stats[clean_k] = val
            except (ValueError, TypeError):
                pass
    return {
        "bowling_deliveries": delivs,
        "batting_ovr": row.get("batting_ovr", 0),
        "bowling_ovr": row.get("bowling_ovr", 0),
        "fielding_ovr": row.get("fielding_ovr", 0),
        "batting": batting_stats,
        "fielding": fielding_stats,
    }


def determine_primary_stats(role: str, bowling_style: str, stats: dict) -> list[dict]:
    """
    Determine the primary / best stats to highlight for a player based on their role:
    - Specialist Bowler: Top bowling deliveries
    - Top/Middle Order Bat: Top batting skills
    - Bowling All-Rounder: Top bowling deliveries + top batting skills
    - All-Rounder: Top batting skills + top bowling deliveries
    - Wicketkeeper-Bat: Wicketkeeping & reflex catching + top batting skills
    Returns a list of dicts: [{"name": str, "category": str, "rating": int}]
    """
    role_norm = (role or "").strip().lower()
    batting = stats.get("batting", {}) or {}
    fielding = stats.get("fielding", {}) or {}
    bowling_delivs = stats.get("bowling_deliveries", []) or []

    # Batting list sorted descending
    bat_list = []
    for k, v in batting.items():
        name = BATTING_LABELS.get(k, k.replace("bat_", "").replace("_", " ").title())
        bat_list.append({"name": name, "category": "Batting", "rating": int(v)})
    bat_list.sort(key=lambda x: x["rating"], reverse=True)

    # Bowling list sorted descending
    bowl_list = []
    for d in bowling_delivs:
        bowl_list.append({"name": d["name"], "category": "Bowling", "rating": int(d.get("rating", 0))})
    bowl_list.sort(key=lambda x: x["rating"], reverse=True)

    # Keeper list
    keeper_keys = [
        "field_wicketkeeping_stumps", "wicketkeeping_stumps",
        "field_wicketkeeping_back", "wicketkeeping_back",
        "field_reflex_catching", "reflex_catching",
        "field_catching", "catching",
        "field_wicketkeeping", "wicketkeeping"
    ]
    keeper_list = []
    seen_k = set()
    for k in keeper_keys:
        if k in fielding and k not in seen_k:
            seen_k.add(k)
            name = FIELDING_LABELS.get(k, k.replace("field_", "").replace("_", " ").title())
            keeper_list.append({"name": name, "category": "Keeping", "rating": int(fielding[k])})

    primary = []
    if "wicketkeeper" in role_norm:
        primary.extend(keeper_list[:3])
        primary.extend(bat_list[:3])
    elif "bowling all-rounder" in role_norm:
        primary.extend(bowl_list[:4])
        primary.extend(bat_list[:2])
    elif "all-rounder" in role_norm:
        primary.extend(bat_list[:3])
        primary.extend(bowl_list[:3])
    elif "bowler" in role_norm:
        primary.extend(bowl_list[:6])
    else:
        primary.extend(bat_list[:6])

    if not primary:
        primary = bat_list[:6] or bowl_list[:6]

    return primary


def lot_snapshot(db, auction_id: int) -> dict:
    """The complete live state of one auction, built from the database."""
    auction = db.execute(
        "SELECT * FROM auctions WHERE id = ?", (auction_id,)
    ).fetchone()
    min_sq = auction["min_squad_size"] if (auction and "min_squad_size" in auction.keys() and auction["min_squad_size"]) else 15
    max_sq = auction["max_squad_size"] if (auction and "max_squad_size" in auction.keys() and auction["max_squad_size"]) else 25
    snap = {
        "auction_id": auction_id,
        "status": auction["status"] if auction else "missing",
        "min_squad_size": min_sq,
        "max_squad_size": max_sq,
        "has_lot": False,
        "ap_id": None,
        "player_name": "",
        "role": "",
        "bowling_style": "",
        "ovr": 0,
        "base_price": 0,
        "current_bid": 0,
        "has_bids": False,
        "highest_bidder_id": None,
        "highest_bidder_name": "",
        "min_next_bid": 0,
        "primary_stats": [],
        "bowling_deliveries": [],
        "batting_ovr": 0,
        "bowling_ovr": 0,
        "fielding_ovr": 0,
    }
    ap = db.execute(
        "SELECT * FROM auction_players WHERE auction_id = ? AND status = 'active'",
        (auction_id,),
    ).fetchone()
    if not ap:
        return snap

    try:
        stats = json.loads(ap["stats_json"] or "{}")
    except (ValueError, TypeError):
        stats = {}

    top = db.execute(
        """SELECT b.bidder_id, b.amount, u.username, u.team_name
           FROM bids b JOIN users u ON u.id = b.bidder_id
           WHERE b.auction_player_id = ? AND b.id > ?
           ORDER BY b.amount DESC, b.id ASC LIMIT 1""",
        (ap["id"], ap["round_start_bid_id"] or 0),
    ).fetchone()

    prim = stats.get("primary_stats") or determine_primary_stats(ap["role"], ap["bowling_style"], stats)

    snap.update({
        "has_lot": True,
        "ap_id": ap["id"],
        "player_name": ap["player_name"],
        "role": ap["role"],
        "bowling_style": ap["bowling_style"],
        "ovr": ap["ovr"],
        "base_price": ap["base_price"],
        "primary_stats": prim,
        "bowling_deliveries": stats.get("bowling_deliveries", []),
        "batting": stats.get("batting", {}),
        "fielding": stats.get("fielding", {}),
        "batting_ovr": stats.get("batting_ovr", 0),
        "bowling_ovr": stats.get("bowling_ovr", 0),
        "fielding_ovr": stats.get("fielding_ovr", 0),
    })
    if top:
        snap.update({
            "current_bid": top["amount"],
            "has_bids": True,
            "highest_bidder_id": top["bidder_id"],
            "highest_bidder_name": _display_name(top),
            "min_next_bid": round(top["amount"] + MIN_BID_INCREMENT, 2),
        })
    else:
        snap.update({"current_bid": ap["base_price"], "min_next_bid": ap["base_price"]})
    return snap


def get_auction_snapshot(auction_id: int) -> dict:
    db = get_db()
    try:
        return lot_snapshot(db, auction_id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            session["next_url"] = request.url
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def auctioneer_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))
        if session.get("role") != "auctioneer":
            flash("Auctioneer access required.", "danger")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def bidder_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in first.", "warning")
            return redirect(url_for("login"))
        if session.get("role") != "bidder":
            flash("Bidder access required.", "danger")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def get_current_user():
    if "user_id" not in session:
        return None
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()
    db.close()
    return user


# ---------------------------------------------------------------------------
# CSV search helper
# ---------------------------------------------------------------------------

def search_players_csv(query: str, limit: int = 20) -> list[dict]:
    """Search players_stats.csv by name; returns list of dicts."""
    results = []
    query_lower = query.strip().lower()
    if not query_lower:
        return results
    try:
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                full_name = f"{row['first_name']} {row['last_name']}".lower()
                if query_lower in full_name:
                    results.append({
                        "offset_hex": row["offset_hex"],
                        "name": f"{row['first_name']} {row['last_name']}",
                        "role": row.get("role", ""),
                        "bowling_style": row.get("bowling_style", ""),
                        "ovr": int(row.get("ovr", 0) or 0),
                    })
                    if len(results) >= limit:
                        break
    except FileNotFoundError:
        pass
    return results



def get_player_by_offset(offset_hex: str) -> dict | None:
    """Return a single player row from CSV by offset_hex."""
    try:
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["offset_hex"] == offset_hex:
                    b_style = row.get("bowling_style", "")
                    stats = extract_full_player_stats(row, b_style)
                    return {
                        "offset_hex": row["offset_hex"],
                        "name": f"{row['first_name']} {row['last_name']}",
                        "role": row.get("role", ""),
                        "bowling_style": b_style,
                        "ovr": int(row.get("ovr", 0) or 0),
                        "stats_json": json.dumps(stats),
                    }
    except FileNotFoundError:
        pass
    return None


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return _redirect_by_role()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        db.close()
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            flash(f"Welcome, {user['username']}!", "success")
            next_url = session.pop("next_url", None)
            if next_url:
                return redirect(next_url)
            return _redirect_by_role()
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("login"))


@app.route("/join/<token>")
@login_required
def join_auction(token):
    """Bidder joins auction via invite link."""
    if session.get("role") != "bidder":
        flash("Only bidders can join via invite links.", "warning")
        return redirect(url_for("auctioneer_dashboard"))
    db = get_db()
    auction = db.execute(
        "SELECT * FROM auctions WHERE invite_token = ?", (token,)
    ).fetchone()
    if not auction:
        db.close()
        flash("Invalid invite link.", "danger")
        return redirect(url_for("bidder_dashboard"))
    # Check if lobby is full (max 20)
    count = db.execute(
        "SELECT COUNT(*) FROM auction_bidders WHERE auction_id = ?", (auction["id"],)
    ).fetchone()[0]
    if count >= 20:
        db.close()
        flash("This auction lobby is full (maximum 20 bidders reached).", "danger")
        return redirect(url_for("bidder_dashboard"))

    # Check if already joined
    existing = db.execute(
        "SELECT id FROM auction_bidders WHERE auction_id = ? AND user_id = ?",
        (auction["id"], session["user_id"]),
    ).fetchone()
    if not existing:
        db.execute(
            """INSERT INTO auction_bidders (auction_id, user_id, budget_remaining)
               VALUES (?, ?, ?)""",
            (auction["id"], session["user_id"], auction["budget_per_team"]),
        )
        db.commit()
        flash(f"You've joined auction: {auction['name']}!", "success")
    else:
        flash("You're already in this auction.", "info")
    db.close()
    return redirect(url_for("bidder_auction", id=auction["id"]))


def _redirect_by_role():
    if session.get("role") == "auctioneer":
        return redirect(url_for("auctioneer_dashboard"))
    return redirect(url_for("bidder_dashboard"))


# ---------------------------------------------------------------------------
# Auctioneer routes
# ---------------------------------------------------------------------------

@app.route("/auctioneer/dashboard")
@auctioneer_required
def auctioneer_dashboard():
    db = get_db()
    auctions = db.execute(
        "SELECT * FROM auctions WHERE auctioneer_id = ? ORDER BY created_at DESC",
        (session["user_id"],),
    ).fetchall()
    db.close()
    return render_template("auctioneer/dashboard.html", auctions=auctions)


@app.route("/auctioneer/create_auction", methods=["GET", "POST"])
@auctioneer_required
def create_auction():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        budget = request.form.get("budget_per_team", 100.0)
        min_squad = request.form.get("min_squad_size", 15)
        max_squad = request.form.get("max_squad_size", 25)
        if not name:
            flash("Auction name is required.", "danger")
            return render_template("auctioneer/create_auction.html")
        try:
            budget = float(budget)
        except (ValueError, TypeError):
            budget = 100.0
        try:
            min_squad = int(min_squad)
        except (ValueError, TypeError):
            min_squad = 15
        try:
            max_squad = int(max_squad)
        except (ValueError, TypeError):
            max_squad = 25
        token = secrets.token_urlsafe(16)
        db = get_db()
        db.execute(
            """INSERT INTO auctions (name, auctioneer_id, invite_token, budget_per_team, min_squad_size, max_squad_size)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, session["user_id"], token, budget, min_squad, max_squad),
        )
        db.commit()
        auction = db.execute(
            "SELECT * FROM auctions WHERE invite_token = ?", (token,)
        ).fetchone()
        db.close()
        flash(f"Auction '{name}' created!", "success")
        return redirect(url_for("manage_auction", id=auction["id"]))
    return render_template("auctioneer/create_auction.html")


@app.route("/auctioneer/auction/<int:id>")
@auctioneer_required
def manage_auction(id):
    db = get_db()
    auction = _get_auction_owned(db, id)
    if not auction:
        db.close()
        flash("Auction not found.", "danger")
        return redirect(url_for("auctioneer_dashboard"))

    bidders = db.execute(
        """SELECT ab.*, u.username, u.team_name, u.team_logo,
                  u.color_primary, u.color_secondary,
                  (SELECT COUNT(*) FROM auction_players WHERE auction_id = ab.auction_id AND sold_to = ab.user_id) AS squad_count
           FROM auction_bidders ab
           JOIN users u ON ab.user_id = u.id
           WHERE ab.auction_id = ?""",
        (id,),
    ).fetchall()

    players = db.execute(
        """SELECT ap.*, u.username AS owner_name
           FROM auction_players ap
           LEFT JOIN users u ON ap.sold_to = u.id
           WHERE ap.auction_id = ?
           ORDER BY ap.queue_order ASC""",
        (id,),
    ).fetchall()
    db.close()

    invite_url = url_for("join_auction", token=auction["invite_token"], _external=True)
    return render_template(
        "auctioneer/manage_auction.html",
        auction=auction,
        bidders=bidders,
        players=players,
        invite_url=invite_url,
    )


@app.route("/auctioneer/auction/<int:id>/add_bidder", methods=["POST"])
@auctioneer_required
def add_bidder(id):
    db = get_db()
    auction = _get_auction_owned(db, id)
    if not auction:
        db.close()
        return jsonify({"error": "Auction not found"}), 404

    count = db.execute(
        "SELECT COUNT(*) FROM auction_bidders WHERE auction_id = ?", (id,)
    ).fetchone()[0]
    if count >= 20:
        db.close()
        flash("Auction lobby is full (maximum 20 bidders reached).", "danger")
        return redirect(url_for("manage_auction", id=id))

    username = request.form.get("username", "").strip()
    user = db.execute(
        "SELECT * FROM users WHERE username = ? AND role = 'bidder'", (username,)
    ).fetchone()
    if not user:
        db.close()
        flash(f"Bidder '{username}' not found.", "warning")
        return redirect(url_for("manage_auction", id=id))

    existing = db.execute(
        "SELECT id FROM auction_bidders WHERE auction_id = ? AND user_id = ?",
        (id, user["id"]),
    ).fetchone()
    if existing:
        db.close()
        flash(f"'{username}' is already in this auction.", "info")
        return redirect(url_for("manage_auction", id=id))

    db.execute(
        """INSERT INTO auction_bidders (auction_id, user_id, budget_remaining)
           VALUES (?, ?, ?)""",
        (id, user["id"], auction["budget_per_team"]),
    )
    db.commit()
    db.close()
    flash(f"Added bidder '{username}'.", "success")
    return redirect(url_for("manage_auction", id=id))


@app.route("/auctioneer/auction/<int:id>/remove_bidder", methods=["POST"])
@auctioneer_required
def remove_bidder(id):
    db = get_db()
    auction = _get_auction_owned(db, id)
    if not auction:
        db.close()
        flash("Auction not found.", "danger")
        return redirect(url_for("auctioneer_dashboard"))

    user_id = request.form.get("user_id")
    owned = db.execute(
        "SELECT COUNT(*) FROM auction_players WHERE auction_id = ? AND sold_to = ?",
        (id, user_id),
    ).fetchone()[0]
    leading = db.execute(
        """SELECT COUNT(*) FROM auction_players ap
           WHERE ap.auction_id = ? AND ap.status = 'active' AND ? = (
               SELECT b.bidder_id FROM bids b
               WHERE b.auction_player_id = ap.id AND b.id > ap.round_start_bid_id
               ORDER BY b.amount DESC, b.id ASC LIMIT 1)""",
        (id, user_id),
    ).fetchone()[0]
    if owned:
        db.close()
        flash("Can't remove a bidder who has already bought players.", "warning")
        return redirect(url_for("manage_auction", id=id))
    if leading:
        db.close()
        flash("Can't remove the current highest bidder while their lot is live.", "warning")
        return redirect(url_for("manage_auction", id=id))
    db.execute(
        "DELETE FROM auction_bidders WHERE auction_id = ? AND user_id = ?",
        (id, user_id),
    )
    db.commit()
    db.close()
    flash("Bidder removed.", "info")
    return redirect(url_for("manage_auction", id=id))


@app.route("/auctioneer/auction/<int:id>/host")
@auctioneer_required
def host_auction(id):
    db = get_db()
    auction = _get_auction_owned(db, id)
    if not auction:
        db.close()
        flash("Auction not found.", "danger")
        return redirect(url_for("auctioneer_dashboard"))

    players = db.execute(
        """SELECT ap.*, u.username AS owner_name, u.team_name AS owner_team
           FROM auction_players ap
           LEFT JOIN users u ON ap.sold_to = u.id
           WHERE ap.auction_id = ?
           ORDER BY ap.queue_order ASC""",
        (id,),
    ).fetchall()

    bidders = db.execute(
        """SELECT ab.*, u.username, u.team_name, u.team_logo,
                  u.color_primary, u.color_secondary,
                  (SELECT COUNT(*) FROM auction_players WHERE auction_id = ab.auction_id AND sold_to = ab.user_id) AS squad_count
           FROM auction_bidders ab
           JOIN users u ON ab.user_id = u.id
           WHERE ab.auction_id = ?""",
        (id,),
    ).fetchall()
    db.close()

    parsed_players = []
    for p in players:
        p_dict = dict(p)
        try:
            p_dict["stats"] = json.loads(p["stats_json"] or "{}")
        except Exception:
            p_dict["stats"] = {}
        parsed_players.append(p_dict)

    return render_template(
        "auctioneer/host_auction.html",
        auction=auction,
        players=parsed_players,
        bidders=bidders,
        snap=get_auction_snapshot(id),
    )


@app.route("/auctioneer/auction/<int:id>/start", methods=["POST"])
@auctioneer_required
def start_auction(id):
    try:
        with _auction_lock, immediate_tx() as db:
            auction = _get_auction_owned(db, id)
            if not auction:
                raise AuctionError("Auction not found.")
            if auction["status"] != "pending":
                raise AuctionError(f"Auction is already {auction['status']}.")
            bidders = db.execute(
                "SELECT COUNT(*) FROM auction_bidders WHERE auction_id = ?", (id,)
            ).fetchone()[0]
            players = db.execute(
                "SELECT COUNT(*) FROM auction_players WHERE auction_id = ? AND status = 'pending'",
                (id,),
            ).fetchone()[0]
            if bidders == 0:
                raise AuctionError("Add at least one bidder before starting.")
            if players == 0:
                raise AuctionError("Add at least one player to the queue before starting.")
            db.execute("UPDATE auctions SET status = 'active' WHERE id = ?", (id,))
        socketio.emit("auction_started", {"auction_id": id}, room=str(id))
        flash("Auction started.", "success")
    except AuctionError as e:
        flash(str(e), "warning")
    return redirect(url_for("host_auction", id=id))


@app.route("/auctioneer/auction/<int:id>/end", methods=["POST"])
@auctioneer_required
def end_auction(id):
    try:
        with _auction_lock, immediate_tx() as db:
            auction = _get_auction_owned(db, id)
            if not auction:
                raise AuctionError("Auction not found.")
            if auction["status"] not in ("active", "paused"):
                raise AuctionError(f"Auction is {auction['status']}; only a running auction can be ended.")
            # A lot still on the block is released back to the queue untouched.
            db.execute(
                "UPDATE auction_players SET status = 'pending' WHERE auction_id = ? AND status = 'active'",
                (id,),
            )
            db.execute(
                "UPDATE auctions SET status = 'ended', current_player_id = NULL WHERE id = ?", (id,)
            )
        socketio.emit("auction_ended", {"auction_id": id}, room=str(id))
        flash("Auction ended.", "info")
    except AuctionError as e:
        flash(str(e), "warning")
    return redirect(url_for("host_auction", id=id))


@app.route("/auctioneer/auction/<int:id>/player_search")
@auctioneer_required
def player_search(id):
    query = request.args.get("q", "")
    results = search_players_csv(query)
    return jsonify(results)


@app.route("/auctioneer/auction/<int:id>/add_player", methods=["POST"])
@auctioneer_required
def add_player(id):
    db = get_db()
    auction = _get_auction_owned(db, id)
    if not auction:
        db.close()
        flash("Auction not found.", "danger")
        return redirect(url_for("auctioneer_dashboard"))

    offset_hex = request.form.get("offset_hex", "").strip()
    player = get_player_by_offset(offset_hex)
    if not player:
        db.close()
        flash("Player not found in CSV.", "danger")
        return redirect(url_for("manage_auction", id=id))

    base_price = request.form.get("base_price", "").strip()
    try:
        base_price = float(base_price) if base_price else get_base_price_by_ovr(player.get("ovr", 50))
    except (ValueError, TypeError):
        base_price = get_base_price_by_ovr(player.get("ovr", 50))

    # Determine next queue_order
    max_order = db.execute(
        "SELECT COALESCE(MAX(queue_order), 0) FROM auction_players WHERE auction_id = ?",
        (id,),
    ).fetchone()[0]

    try:
        stats = json.loads(player.get("stats_json") or "{}")
    except Exception:
        stats = {}
    stats["primary_stats"] = determine_primary_stats(player.get("role", ""), player.get("bowling_style", ""), stats)
    stats_json = json.dumps(stats)

    db.execute(
        """INSERT INTO auction_players
               (auction_id, csv_offset, player_name, role, bowling_style, ovr,
                base_price, status, queue_order, stats_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        (
            id,
            player["offset_hex"],
            player["name"],
            player["role"],
            player["bowling_style"],
            player["ovr"],
            base_price,
            max_order + 1,
            stats_json,
        ),
    )
    db.commit()
    db.close()
    flash(f"Added {player['name']} (Base: ₹{base_price:.2f} Cr) to the queue.", "success")
    return redirect(url_for("manage_auction", id=id))


@app.route("/auctioneer/auction/<int:id>/upload_csv", methods=["POST"])
@auctioneer_required
def upload_auction_csv(id):
    """Upload a CSV file from local PC, parse all player stats based on bowling_style, and import into queue."""
    db = get_db()
    auction = _get_auction_owned(db, id)
    if not auction:
        db.close()
        flash("Auction not found.", "danger")
        return redirect(url_for("auctioneer_dashboard"))

    if "csv_file" not in request.files:
        db.close()
        flash("No file selected.", "warning")
        return redirect(url_for("manage_auction", id=id))

    file = request.files["csv_file"]
    if not file or not file.filename:
        db.close()
        flash("Please choose a CSV file to upload.", "warning")
        return redirect(url_for("manage_auction", id=id))

    default_base_price = request.form.get("default_base_price", "").strip()
    try:
        default_base_price = float(default_base_price) if default_base_price else None
    except (ValueError, TypeError):
        default_base_price = None

    clear_existing = request.form.get("clear_existing") == "1"
    if clear_existing:
        db.execute("DELETE FROM auction_players WHERE auction_id = ? AND status = 'pending'", (id,))
        db.commit()

    # Determine current max queue_order
    max_order = db.execute(
        "SELECT COALESCE(MAX(queue_order), 0) FROM auction_players WHERE auction_id = ?",
        (id,),
    ).fetchone()[0]

    # Read and parse CSV file
    try:
        content = file.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            file.stream.seek(0)
            content = file.stream.read().decode("latin-1")
        except Exception as e:
            db.close()
            flash(f"Failed to read CSV file: {e}", "danger")
            return redirect(url_for("manage_auction", id=id))

    reader = csv.DictReader(io.StringIO(content))
    imported_count = 0

    for row in reader:
        # Determine player name
        if "first_name" in row and "last_name" in row:
            p_name = f"{row['first_name']} {row['last_name']}".strip()
        elif "name" in row:
            p_name = row["name"].strip()
        elif "player_name" in row:
            p_name = row["player_name"].strip()
        else:
            continue

        if not p_name:
            continue

        role = row.get("role", "").strip()
        bowling_style = row.get("bowling_style", "").strip()

        try:
            ovr = int(float(row.get("ovr", 0) or 0))
        except (ValueError, TypeError):
            ovr = 0

        # Base price: if in CSV column use it; else if default_base_price provided use it; else auto by OVR
        bp = None
        for bp_key in ("base_price", "baseprice", "base_price_cr", "base_price_inr", "base"):
            if bp_key in row and row[bp_key]:
                try:
                    bp = float(row[bp_key])
                    break
                except (ValueError, TypeError):
                    pass

        if bp is None or bp <= 0:
            if default_base_price and default_base_price > 0:
                bp = default_base_price
            else:
                bp = get_base_price_by_ovr(ovr)

        offset_hex = row.get("offset_hex", f"0x{secrets.token_hex(4).upper()}")

        # Extract stats and compute primary stats
        stats = extract_full_player_stats(row, bowling_style)
        stats["primary_stats"] = determine_primary_stats(role, bowling_style, stats)
        stats_json = json.dumps(stats)

        max_order += 1
        db.execute(
            """INSERT INTO auction_players
                   (auction_id, csv_offset, player_name, role, bowling_style, ovr,
                    base_price, status, queue_order, stats_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (id, offset_hex, p_name, role, bowling_style, ovr, bp, max_order, stats_json),
        )
        imported_count += 1

    db.commit()
    db.close()
    flash(f"Successfully uploaded and added {imported_count} players from CSV to the auction queue with role-based stats and base prices!", "success")
    return redirect(url_for("manage_auction", id=id))


@app.route("/auctioneer/auction/<int:id>/remove_player", methods=["POST"])
@auctioneer_required
def remove_player(id):
    db = get_db()
    auction = _get_auction_owned(db, id)
    if not auction:
        db.close()
        flash("Auction not found.", "danger")
        return redirect(url_for("auctioneer_dashboard"))

    ap_id = request.form.get("ap_id")
    blocked = db.execute(
        "SELECT status FROM auction_players WHERE id = ? AND auction_id = ? AND status IN ('active','sold')",
        (ap_id, id),
    ).fetchone()
    if blocked:
        db.close()
        flash(f"Can't remove a player who is {blocked['status']}.", "warning")
        return redirect(url_for("manage_auction", id=id))
    db.execute("DELETE FROM bids WHERE auction_player_id = ?", (ap_id,))
    db.execute(
        "DELETE FROM auction_players WHERE id = ? AND auction_id = ?",
        (ap_id, id),
    )
    db.commit()
    db.close()
    flash("Player removed from queue.", "info")
    return redirect(url_for("manage_auction", id=id))


@app.route("/auctioneer/register_bidder", methods=["GET", "POST"])
@auctioneer_required
def register_bidder():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        team_name = request.form.get("team_name", "").strip()
        if not username or not password:
            flash("Username and password are required.", "danger")
            return render_template("auctioneer/register_bidder.html")
        db = get_db()
        existing = db.execute(
            "SELECT id FROM users WHERE username = ?", (username,)
        ).fetchone()
        if existing:
            db.close()
            flash(f"Username '{username}' is already taken.", "warning")
            return render_template("auctioneer/register_bidder.html")
        db.execute(
            """INSERT INTO users (username, password_hash, role, team_name)
               VALUES (?, ?, 'bidder', ?)""",
            (username, generate_password_hash(password), team_name),
        )
        db.commit()
        db.close()
        flash(f"Bidder account '{username}' created.", "success")
        return redirect(url_for("auctioneer_dashboard"))
    return render_template("auctioneer/register_bidder.html")


# ---------------------------------------------------------------------------
# Bidder routes
# ---------------------------------------------------------------------------

@app.route("/bidder/dashboard")
@bidder_required
def bidder_dashboard():
    db = get_db()
    auctions = db.execute(
        """SELECT a.*, ab.budget_remaining
           FROM auctions a
           JOIN auction_bidders ab ON a.id = ab.auction_id
           WHERE ab.user_id = ?
           ORDER BY a.created_at DESC""",
        (session["user_id"],),
    ).fetchall()
    db.close()
    return render_template("bidder/dashboard.html", auctions=auctions)


@app.route("/bidder/auction/<int:id>")
@bidder_required
def bidder_auction(id):
    db = get_db()
    # Verify bidder is registered for this auction
    ab = db.execute(
        "SELECT * FROM auction_bidders WHERE auction_id = ? AND user_id = ?",
        (id, session["user_id"]),
    ).fetchone()
    if not ab:
        db.close()
        flash("You are not registered for this auction.", "danger")
        return redirect(url_for("bidder_dashboard"))

    auction = db.execute("SELECT * FROM auctions WHERE id = ?", (id,)).fetchone()
    players = db.execute(
        """SELECT ap.*, u.username AS owner_name, u.team_name AS owner_team
           FROM auction_players ap
           LEFT JOIN users u ON ap.sold_to = u.id
           WHERE ap.auction_id = ?
           ORDER BY ap.queue_order ASC""",
        (id,),
    ).fetchall()
    bidders = db.execute(
        """SELECT ab.*, u.username, u.team_name, u.team_logo,
                  u.color_primary, u.color_secondary,
                  (SELECT COUNT(*) FROM auction_players WHERE auction_id = ab.auction_id AND sold_to = ab.user_id) AS squad_count
           FROM auction_bidders ab
           JOIN users u ON ab.user_id = u.id
           WHERE ab.auction_id = ?""",
        (id,),
    ).fetchall()
    db.close()

    parsed_players = []
    for p in players:
        p_dict = dict(p)
        try:
            p_dict["stats"] = json.loads(p["stats_json"] or "{}")
        except Exception:
            p_dict["stats"] = {}
        parsed_players.append(p_dict)

    return render_template(
        "bidder/auction.html",
        auction=auction,
        players=parsed_players,
        bidders=bidders,
        budget_remaining=ab["budget_remaining"],
        snap=get_auction_snapshot(id),
    )


@app.route("/bidder/my_team/<int:auction_id>")
@bidder_required
def my_team(auction_id):
    db = get_db()
    ab = db.execute(
        "SELECT * FROM auction_bidders WHERE auction_id = ? AND user_id = ?",
        (auction_id, session["user_id"]),
    ).fetchone()
    if not ab:
        db.close()
        flash("You are not part of this auction.", "danger")
        return redirect(url_for("bidder_dashboard"))

    auction = db.execute("SELECT * FROM auctions WHERE id = ?", (auction_id,)).fetchone()
    players = db.execute(
        "SELECT * FROM auction_players WHERE auction_id = ? AND sold_to = ?",
        (auction_id, session["user_id"]),
    ).fetchall()
    parsed_players = []
    for p in players:
        p_dict = dict(p)
        try:
            p_dict["stats"] = json.loads(p["stats_json"] or "{}")
        except Exception:
            p_dict["stats"] = {}
        parsed_players.append(p_dict)

    user = db.execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    db.close()
    return render_template(
        "bidder/my_team.html",
        auction=auction,
        players=parsed_players,
        budget_remaining=ab["budget_remaining"],
        user=user,
    )


@app.route("/bidder/profile", methods=["GET", "POST"])
@bidder_required
def bidder_profile():
    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()

    if request.method == "POST":
        new_username = request.form.get("username", "").strip()
        team_name = request.form.get("team_name", "").strip()
        color_primary = request.form.get("color_primary", "#9d00ff").strip()
        color_secondary = request.form.get("color_secondary", "#1a0033").strip()
        new_password = request.form.get("new_password", "").strip()

        if not new_username:
            new_username = user["username"]
        elif new_username != user["username"]:
            existing = db.execute(
                "SELECT id FROM users WHERE username = ? AND id != ?",
                (new_username, session["user_id"]),
            ).fetchone()
            if existing:
                db.close()
                flash("Username is already taken by another user.", "danger")
                return redirect(url_for("bidder_profile"))

        team_logo_filename = user["team_logo"]

        # Handle logo upload
        if "team_logo" in request.files:
            file = request.files["team_logo"]
            if file and file.filename and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                unique_name = f"{session['user_id']}_{secrets.token_hex(6)}_{filename}"
                file.save(os.path.join(app.config["UPLOAD_FOLDER"], unique_name))
                team_logo_filename = unique_name

        if new_password:
            db.execute(
                """UPDATE users SET username=?, team_name=?, team_logo=?,
                   color_primary=?, color_secondary=?, password_hash=?
                   WHERE id=?""",
                (
                    new_username,
                    team_name,
                    team_logo_filename,
                    color_primary,
                    color_secondary,
                    generate_password_hash(new_password),
                    session["user_id"],
                ),
            )
        else:
            db.execute(
                """UPDATE users SET username=?, team_name=?, team_logo=?,
                   color_primary=?, color_secondary=? WHERE id=?""",
                (
                    new_username,
                    team_name,
                    team_logo_filename,
                    color_primary,
                    color_secondary,
                    session["user_id"],
                ),
            )

        db.commit()
        db.close()
        session["username"] = new_username  # refresh
        flash("Profile updated successfully.", "success")
        return redirect(url_for("bidder_profile"))

    db.close()
    return render_template("bidder/profile.html", user=user)


# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def _get_auction_owned(db, auction_id: int):
    """Return auction row only if it belongs to the current auctioneer."""
    return db.execute(
        "SELECT * FROM auctions WHERE id = ? AND auctioneer_id = ?",
        (auction_id, session["user_id"]),
    ).fetchone()


# ---------------------------------------------------------------------------
# Socket.IO events
# ---------------------------------------------------------------------------
# Rules for every handler below:
#   * Identity comes from the signed login session, NEVER from the client payload.
#   * Role / ownership / membership are re-checked against the database.
#   * State is read from and written to the database inside one BEGIN IMMEDIATE
#     transaction (and the process-wide lock), so concurrent clicks can't race.
#   * Errors are sent only to the caller; successful changes go to the whole room.

def _to_int(value, label: str) -> int:
    if isinstance(value, bool):
        raise AuctionError(f"Invalid {label}.")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise AuctionError(f"Invalid {label}.")


def _to_amount(value, label: str) -> float:
    if isinstance(value, bool):
        raise AuctionError(f"Invalid {label}.")
    try:
        val = round(float(value), 2)
        if val <= 0:
            raise AuctionError(f"Invalid {label}.")
        return val
    except (TypeError, ValueError):
        raise AuctionError(f"Invalid {label}.")


def _session_user_id() -> int:
    uid = session.get("user_id")
    if not uid:
        raise AuctionError("You are not logged in. Please refresh and log in again.")
    return int(uid)


def _require_bidder(db, auction_id: int):
    """Returns (user_id, auction_bidders row) for a logged-in bidder in this auction."""
    uid = _session_user_id()
    user = db.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
    if not user or user["role"] != "bidder":
        raise AuctionError("Only bidders can place bids.")
    ab = db.execute(
        "SELECT * FROM auction_bidders WHERE auction_id = ? AND user_id = ?",
        (auction_id, uid),
    ).fetchone()
    if not ab:
        raise AuctionError("You are not registered for this auction.")
    return uid, ab


def _require_host(db, auction_id: int):
    """Returns the auction row, only if the logged-in user is its auctioneer."""
    uid = _session_user_id()
    user = db.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
    auction = db.execute(
        "SELECT * FROM auctions WHERE id = ? AND auctioneer_id = ?", (auction_id, uid)
    ).fetchone()
    if not user or user["role"] != "auctioneer" or not auction:
        raise AuctionError("Only this auction's auctioneer can do that.")
    return auction


def _socket_action(error_event: str):
    """Wrap a handler: payload sanity, global lock, error reporting, room broadcast.

    The wrapped function takes the payload dict and returns (auction_id, events)
    where events is a list of (event_name, payload) to broadcast to the room.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(data=None):
            data = data if isinstance(data, dict) else {}
            try:
                with _auction_lock:
                    auction_id, events = fn(data)
                    for name, payload in events:
                        emit(name, payload, room=str(auction_id))
            except AuctionError as e:
                emit(error_event, {"message": str(e)})
            except Exception:
                app.logger.exception("Unexpected error in %s", fn.__name__)
                emit(error_event, {"message": "Something went wrong on the server. Please try again."})
        return wrapper
    return decorator


@socketio.on("join_auction_room")
def handle_join_auction_room(data=None):
    data = data if isinstance(data, dict) else {}
    try:
        auction_id = _to_int(data.get("auction_id"), "auction")
        uid = _session_user_id()
        db = get_db()
        try:
            auction = db.execute("SELECT * FROM auctions WHERE id = ?", (auction_id,)).fetchone()
            is_host = bool(auction and auction["auctioneer_id"] == uid)
            is_member = db.execute(
                "SELECT 1 FROM auction_bidders WHERE auction_id = ? AND user_id = ?",
                (auction_id, uid),
            ).fetchone()
            if not auction or not (is_host or is_member):
                raise AuctionError("You don't have access to this auction.")
            snap = lot_snapshot(db, auction_id)
        finally:
            db.close()
    except AuctionError as e:
        emit("bid_error", {"message": str(e)})
        return
    join_room(str(auction_id))
    # Full snapshot so a reconnecting/refreshed client re-syncs completely.
    emit("room_joined", snap)


@socketio.on("place_bid")
@_socket_action("bid_error")
def handle_place_bid(data):
    auction_id = _to_int(data.get("auction_id"), "auction")
    ap_id = _to_int(data.get("ap_id"), "player")
    amount = _to_amount(data.get("amount"), "bid amount")
    if amount <= 0 or amount > MAX_BID_AMOUNT:
        raise AuctionError("Invalid bid amount.")

    with immediate_tx() as db:
        uid, ab = _require_bidder(db, auction_id)

        auction = db.execute("SELECT * FROM auctions WHERE id = ?", (auction_id,)).fetchone()
        if not auction or auction["status"] == "pending":
            raise AuctionError("The auction hasn't started yet.")
        if auction["status"] == "paused":
            raise AuctionError("Bidding is paused.")
        if auction["status"] != "active":
            raise AuctionError("This auction has ended.")

        snap = lot_snapshot(db, auction_id)
        if not snap["has_lot"] or snap["ap_id"] != ap_id:
            raise AuctionError("That player is no longer on the block.")
        if snap["highest_bidder_id"] == uid:
            raise AuctionError("You already hold the highest bid.")
        if amount < snap["min_next_bid"]:
            raise AuctionError(f"Bid must be at least ₹{snap['min_next_bid']:.2f} Cr.")
        if amount > ab["budget_remaining"]:
            raise AuctionError(
                f"Insufficient budget. You have ₹{ab['budget_remaining']:.2f} Cr left."
            )

        # Check squad size limits
        bought_count = db.execute(
            "SELECT COUNT(*) FROM auction_players WHERE auction_id = ? AND sold_to = ?",
            (auction_id, uid)
        ).fetchone()[0]

        max_squad = auction["max_squad_size"] if ("max_squad_size" in auction.keys() and auction["max_squad_size"]) else 0
        min_squad = auction["min_squad_size"] if ("min_squad_size" in auction.keys() and auction["min_squad_size"]) else 0

        if max_squad and max_squad > 0 and bought_count >= max_squad:
            raise AuctionError(f"Your squad is full ({bought_count}/{max_squad} players). You cannot place any more bids.")

        if min_squad and min_squad > 0:
            remaining_min_needed = max(0, min_squad - (bought_count + 1))
            min_reserve = round(remaining_min_needed * 0.20, 2)
            if round(ab["budget_remaining"] - amount, 2) < min_reserve:
                raise AuctionError(
                    f"Cannot bid ₹{amount:.2f} Cr: you must reserve at least ₹{min_reserve:.2f} Cr to reach the minimum squad size of {min_squad} players ({remaining_min_needed} more needed at base price 0.20 Cr)."
                )

        db.execute(
            "INSERT INTO bids (auction_player_id, bidder_id, amount) VALUES (?, ?, ?)",
            (ap_id, uid, amount),
        )
        snap = lot_snapshot(db, auction_id)

    return auction_id, [("bid_update", snap)]


@socketio.on("auctioneer_next_player")
@_socket_action("host_error")
def handle_next_player(data):
    """Put a pending (or previously unsold) player on the block."""
    auction_id = _to_int(data.get("auction_id"), "auction")
    ap_id = _to_int(data.get("ap_id"), "player")

    with immediate_tx() as db:
        auction = _require_host(db, auction_id)
        if auction["status"] == "pending":
            raise AuctionError("Start the auction first.")
        if auction["status"] == "paused":
            raise AuctionError("The auction is paused. Resume it first.")
        if auction["status"] != "active":
            raise AuctionError("This auction has ended.")

        live = db.execute(
            "SELECT player_name FROM auction_players WHERE auction_id = ? AND status = 'active'",
            (auction_id,),
        ).fetchone()
        if live:
            raise AuctionError(
                f"{live['player_name']} is still on the block. Sell or mark them unsold first."
            )

        ap = db.execute(
            "SELECT * FROM auction_players WHERE id = ? AND auction_id = ?", (ap_id, auction_id)
        ).fetchone()
        if not ap:
            raise AuctionError("Player not found in this auction.")
        if ap["status"] not in ("pending", "unsold"):
            raise AuctionError(f"{ap['player_name']} is already {ap['status']}.")

        floor = db.execute("SELECT COALESCE(MAX(id), 0) FROM bids").fetchone()[0]
        db.execute(
            "UPDATE auction_players SET status = 'active', round_start_bid_id = ? WHERE id = ?",
            (floor, ap_id),
        )
        db.execute("UPDATE auctions SET current_player_id = ? WHERE id = ?", (ap_id, auction_id))
        snap = lot_snapshot(db, auction_id)

    return auction_id, [("player_update", snap)]


@socketio.on("auctioneer_sell")
@_socket_action("host_error")
def handle_sell(data):
    """Sell the live player to the highest bidder, exactly once."""
    auction_id = _to_int(data.get("auction_id"), "auction")
    ap_id = _to_int(data.get("ap_id"), "player")

    with immediate_tx() as db:
        auction = _require_host(db, auction_id)
        if auction["status"] == "paused":
            raise AuctionError("The auction is paused. Resume it before selling.")
        if auction["status"] != "active":
            raise AuctionError("The auction isn't running.")

        snap = lot_snapshot(db, auction_id)
        if not snap["has_lot"] or snap["ap_id"] != ap_id:
            raise AuctionError("That player is not currently on the block.")
        if not snap["has_bids"]:
            raise AuctionError("No bids placed yet. Mark the player unsold instead.")

        buyer_id = snap["highest_bidder_id"]
        price = snap["current_bid"]

        # Conditional updates: each must touch exactly one row or we roll back.
        paid = db.execute(
            """UPDATE auction_bidders SET budget_remaining = budget_remaining - ?
               WHERE auction_id = ? AND user_id = ? AND budget_remaining >= ?""",
            (price, auction_id, buyer_id, price),
        ).rowcount
        if paid != 1:
            raise AuctionError(
                "The highest bidder can no longer afford this bid (or left the auction). "
                "Mark the player unsold."
            )
        sold = db.execute(
            """UPDATE auction_players SET status = 'sold', sold_to = ?, sold_price = ?
               WHERE id = ? AND auction_id = ? AND status = 'active'""",
            (buyer_id, price, ap_id, auction_id),
        ).rowcount
        if sold != 1:
            raise AuctionError("That player was already sold.")
        db.execute("UPDATE auctions SET current_player_id = NULL WHERE id = ?", (auction_id,))

        buyer = db.execute("SELECT username, team_name FROM users WHERE id = ?", (buyer_id,)).fetchone()
        budget_left = db.execute(
            "SELECT budget_remaining FROM auction_bidders WHERE auction_id = ? AND user_id = ?",
            (auction_id, buyer_id),
        ).fetchone()["budget_remaining"]
        buyer_squad_count = db.execute(
            "SELECT COUNT(*) FROM auction_players WHERE auction_id = ? AND sold_to = ?",
            (auction_id, buyer_id),
        ).fetchone()[0]
        after = lot_snapshot(db, auction_id)

    return auction_id, [(
        "player_sold",
        {
            "auction_id": auction_id,
            "ap_id": ap_id,
            "player_name": snap["player_name"],
            "sold_to": _display_name(buyer),
            "sold_to_team": buyer["team_name"],
            "sold_price": price,
            "buyer_budget_remaining": budget_left,
            "buyer_squad_count": buyer_squad_count,
            "buyer_id": buyer_id,
            "snapshot": after,
        },
    )]


@socketio.on("auctioneer_unsold")
@_socket_action("host_error")
def handle_unsold(data):
    """Pass on the live player (any bids on this round are discarded)."""
    auction_id = _to_int(data.get("auction_id"), "auction")
    ap_id = _to_int(data.get("ap_id"), "player")

    with immediate_tx() as db:
        auction = _require_host(db, auction_id)
        if auction["status"] not in ("active", "paused"):
            raise AuctionError("The auction isn't running.")
        ap = db.execute(
            "SELECT player_name FROM auction_players WHERE id = ? AND auction_id = ? AND status = 'active'",
            (ap_id, auction_id),
        ).fetchone()
        if not ap:
            raise AuctionError("That player is not currently on the block.")
        db.execute("UPDATE auction_players SET status = 'unsold' WHERE id = ?", (ap_id,))
        db.execute("UPDATE auctions SET current_player_id = NULL WHERE id = ?", (auction_id,))
        after = lot_snapshot(db, auction_id)

    return auction_id, [(
        "player_unsold",
        {
            "auction_id": auction_id,
            "ap_id": ap_id,
            "player_name": ap["player_name"],
            "snapshot": after,
        },
    )]


@socketio.on("auctioneer_pause")
@_socket_action("host_error")
def handle_pause(data):
    auction_id = _to_int(data.get("auction_id"), "auction")
    with immediate_tx() as db:
        auction = _require_host(db, auction_id)
        if auction["status"] != "active":
            raise AuctionError("Only a live auction can be paused.")
        db.execute("UPDATE auctions SET status = 'paused' WHERE id = ?", (auction_id,))
        snap = lot_snapshot(db, auction_id)
    return auction_id, [("auction_paused", snap)]


@socketio.on("auctioneer_resume")
@_socket_action("host_error")
def handle_resume(data):
    auction_id = _to_int(data.get("auction_id"), "auction")
    with immediate_tx() as db:
        auction = _require_host(db, auction_id)
        if auction["status"] != "paused":
            raise AuctionError("Only a paused auction can be resumed.")
        db.execute("UPDATE auctions SET status = 'active' WHERE id = ?", (auction_id,))
        snap = lot_snapshot(db, auction_id)
    return auction_id, [("auction_resumed", snap)]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    init_db()
    print("Database initialized. Starting server on http://localhost:5050")
    socketio.run(
        app,
        host="0.0.0.0",
        port=5050,
        debug=os.environ.get("FLASK_DEBUG") == "1",
        allow_unsafe_werkzeug=True,
    )
