import sqlite3
import csv
import json
import os

DB_PATH = os.path.join(r'd:\experiments\auction_app', 'auction_app.db')
CSV_PATH = r'd:\experiments\players_stats.csv'

def get_base_price_by_ovr(ovr):
    try:
        ovr = int(ovr)
    except:
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

def run_migration():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Add columns if missing
    cols_a = [r[1] for r in cur.execute("PRAGMA table_info(auctions)").fetchall()]
    if "min_squad_size" not in cols_a:
        cur.execute("ALTER TABLE auctions ADD COLUMN min_squad_size INTEGER DEFAULT 15")
    if "max_squad_size" not in cols_a:
        cur.execute("ALTER TABLE auctions ADD COLUMN max_squad_size INTEGER DEFAULT 25")

    cols_p = [r[1] for r in cur.execute("PRAGMA table_info(auction_players)").fetchall()]
    if "stats_json" not in cols_p:
        cur.execute("ALTER TABLE auction_players ADD COLUMN stats_json TEXT DEFAULT '{}'")

    # Load CSV players lookup by offset_hex and by full name
    csv_lookup = {}
    with open(CSV_PATH, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            off = row.get('offset_hex', '')
            name = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip()
            csv_lookup[off] = row
            csv_lookup[name.lower()] = row

    # Update Auction 6
    cur.execute("UPDATE auctions SET budget_per_team = 100.0, min_squad_size = 15, max_squad_size = 25 WHERE id = 6")
    cur.execute("UPDATE auction_bidders SET budget_remaining = 100.0 WHERE auction_id = 6")

    # Update players in Auction 6
    from app import extract_full_player_stats, determine_primary_stats

    players = cur.execute("SELECT * FROM auction_players WHERE auction_id = 6").fetchall()
    print(f"Updating {len(players)} players in Auction 6...")
    updated = 0
    for p in players:
        ovr = p['ovr'] or 50
        bp = get_base_price_by_ovr(ovr)
        
        # Look up CSV row
        csv_row = csv_lookup.get(p['csv_offset']) or csv_lookup.get(p['player_name'].lower())
        stats_json = p['stats_json'] or '{}'
        if csv_row:
            b_style = p['bowling_style'] or csv_row.get('bowling_style', '')
            full_stats = extract_full_player_stats(csv_row, b_style)
            primary_stats = determine_primary_stats(p['role'], b_style, full_stats)
            full_stats['primary_stats'] = primary_stats
            stats_json = json.dumps(full_stats)

        cur.execute(
            "UPDATE auction_players SET base_price = ?, stats_json = ? WHERE id = ?",
            (bp, stats_json, p['id'])
        )
        updated += 1

    conn.commit()
    print(f"Successfully migrated Auction 6: {updated} players updated to Crores base prices (by OVR)!")
    
    # Check sample output
    sample = cur.execute("SELECT player_name, role, bowling_style, ovr, base_price FROM auction_players WHERE auction_id = 6 AND ovr >= 80 LIMIT 5").fetchall()
    for s in sample:
        print(f"  {s['player_name']}: OVR {s['ovr']} -> Base Price: ₹{s['base_price']} Cr")

    conn.close()

if __name__ == '__main__':
    run_migration()
