import sys
import os
import tempfile
import unittest
import json

# Tests ALWAYS run against a throwaway database, never the real auction_app.db.
_tmp_dir = tempfile.mkdtemp(prefix="auction_test_")
os.environ["AUCTION_DB_PATH"] = os.path.join(_tmp_dir, "test.db")

sys.path.append(os.path.dirname(__file__))

from app import app, socketio, search_players_csv, get_player_by_offset, get_base_price_by_ovr, determine_primary_stats, extract_full_player_stats
from models import init_db, get_db

class TestAuctionPlatform(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        app.config['WTF_CSRF_ENABLED'] = False
        init_db()
        db = get_db()
        db.execute("DELETE FROM bids")
        db.execute("DELETE FROM auction_players")
        db.execute("DELETE FROM auction_bidders")
        db.execute("DELETE FROM auctions WHERE name LIKE 'Test%'")
        db.execute("DELETE FROM users WHERE username LIKE 'bidder_%'")
        db.commit()
        db.close()
        self.client = app.test_client()

    def test_csv_search(self):
        res = search_players_csv("Bumrah")
        self.assertTrue(len(res) > 0)
        self.assertEqual(res[0]['name'], "Jasprit Bumrah")
        self.assertEqual(res[0]['ovr'], 88)
        self.assertEqual(res[0]['bowling_style'], "Fast-Medium")

        shadab = search_players_csv("Shadab Khan")
        self.assertTrue(len(shadab) > 0)
        self.assertEqual(shadab[0]['name'], "Shadab Khan")
        self.assertEqual(shadab[0]['ovr'], 75)
        self.assertEqual(shadab[0]['bowling_style'], "Leg Spin")

    def test_full_auction_flow(self):
        # 1. Login as admin
        resp = self.client.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=True)
        self.assertIn(b"Dashboard", resp.data)

        # 2. Create auction
        resp = self.client.post("/auctioneer/create_auction", data={
            "name": "Test Premier League",
            "budget_per_team": "2000000"
        }, follow_redirects=True)
        self.assertIn(b"Test Premier League", resp.data)

        db = get_db()
        auction = db.execute("SELECT * FROM auctions WHERE name = 'Test Premier League'").fetchone()
        self.assertIsNotNone(auction)
        auction_id = auction["id"]
        token = auction["invite_token"]
        db.close()

        # 3. Register a bidder
        resp = self.client.post("/auctioneer/register_bidder", data={
            "username": "bidder_one",
            "password": "pass123",
            "confirm_password": "pass123"
        }, follow_redirects=True)
        self.assertIn(b"Bidder account", resp.data)

        # 4. Add player from CSV to queue
        bumrah = search_players_csv("Bumrah")[0]
        resp = self.client.post(f"/auctioneer/auction/{auction_id}/add_player", data={
            "offset_hex": bumrah["offset_hex"],
            "base_price": "100000"
        }, follow_redirects=True)
        self.assertIn(b"Jasprit Bumrah", resp.data)

        # 5. Direct add bidder
        resp = self.client.post(f"/auctioneer/auction/{auction_id}/add_bidder", data={
            "username": "bidder_one"
        }, follow_redirects=True)
        self.assertIn(b"Added bidder", resp.data)

        # 6. Logout admin, login as bidder_one
        self.client.get("/logout", follow_redirects=True)
        resp = self.client.post("/login", data={"username": "bidder_one", "password": "pass123"}, follow_redirects=True)
        self.assertIn(b"My Registered Auctions", resp.data)

        # 7. Customize profile: team name, colors
        resp = self.client.post("/bidder/profile", data={
            "username": "bidder_one",
            "team_name": "Lahore Qalandars",
            "color_primary": "#00ff88",
            "color_secondary": "#003311"
        }, follow_redirects=True)
        self.assertIn(b"Profile updated successfully", resp.data)

        # 8. View auction lobby
        resp = self.client.get(f"/bidder/auction/{auction_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Jasprit Bumrah", resp.data)
        self.assertIn(b"Lahore Qalandars", resp.data)

        # 9. Test join via token for a new bidder
        self.client.get("/logout", follow_redirects=True)
        # Register second bidder via admin
        self.client.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=True)
        self.client.post("/auctioneer/register_bidder", data={
            "username": "bidder_two",
            "password": "pass456",
            "confirm_password": "pass456"
        }, follow_redirects=True)
        self.client.get("/logout", follow_redirects=True)

        # Login as bidder_two and use invite link
        self.client.post("/login", data={"username": "bidder_two", "password": "pass456"}, follow_redirects=True)
        resp = self.client.get(f"/join/{token}", follow_redirects=True)
        self.assertIn(b"Test Premier League", resp.data)

        # Verify bidder_two is in auction_bidders
        db = get_db()
        b2 = db.execute("SELECT * FROM auction_bidders WHERE auction_id = ? AND user_id = (SELECT id FROM users WHERE username = 'bidder_two')", (auction_id,)).fetchone()
        self.assertIsNotNone(b2)
        db.close()

    def test_max_20_bidders_limit(self):
        # 1. Login as admin & create auction
        self.client.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=True)
        self.client.post("/auctioneer/create_auction", data={
            "name": "Test Cap Auction",
            "budget_per_team": "1000000"
        }, follow_redirects=True)

        db = get_db()
        auction = db.execute("SELECT id, invite_token FROM auctions WHERE name = 'Test Cap Auction'").fetchone()
        auction_id = auction["id"]
        token = auction["invite_token"]

        # Insert 20 bidders
        for i in range(1, 21):
            db.execute(
                "INSERT OR IGNORE INTO users (username, password_hash, role) VALUES (?, 'hash', 'bidder')",
                (f"bidder_cap_{i}",)
            )
            u_id = db.execute("SELECT id FROM users WHERE username = ?", (f"bidder_cap_{i}",)).fetchone()["id"]
            db.execute(
                "INSERT INTO auction_bidders (auction_id, user_id, budget_remaining) VALUES (?, ?, 1000000)",
                (auction_id, u_id)
            )
        db.commit()
        db.close()

        # Try to add 21st bidder directly
        db = get_db()
        db.execute("INSERT OR IGNORE INTO users (username, password_hash, role) VALUES ('bidder_cap_21', 'hash', 'bidder')")
        db.commit()
        db.close()

        resp = self.client.post(f"/auctioneer/auction/{auction_id}/add_bidder", data={
            "username": "bidder_cap_21"
        }, follow_redirects=True)
        self.assertIn(b"maximum 20 bidders", resp.data)

        # Try to join via invite token as 21st bidder
        self.client.get("/logout", follow_redirects=True)
        # Give bidder_cap_21 a valid password
        from werkzeug.security import generate_password_hash
        db = get_db()
        db.execute("UPDATE users SET password_hash = ? WHERE username = 'bidder_cap_21'", (generate_password_hash("testpass"),))
        db.commit()
        db.close()

        self.client.post("/login", data={"username": "bidder_cap_21", "password": "testpass"}, follow_redirects=True)
        resp = self.client.get(f"/join/{token}", follow_redirects=True)
        self.assertIn(b"maximum 20 bidders", resp.data)

    def test_csv_file_upload_and_bowling_style_mapping(self):
        # 1. Login as admin & create auction
        self.client.post("/login", data={"username": "admin", "password": "admin123"}, follow_redirects=True)
        self.client.post("/auctioneer/create_auction", data={
            "name": "Test CSV Upload Auction",
            "budget_per_team": "1500000"
        }, follow_redirects=True)

        db = get_db()
        auction = db.execute("SELECT id FROM auctions WHERE name = 'Test CSV Upload Auction'").fetchone()
        auction_id = auction["id"]
        db.close()

        # 2. Upload CSV with Pace, Leg Spin, and Off Spin bowlers
        import io
        csv_content = """first_name,last_name,role,bowling_style,ovr,base_price,rating_bowl_slot1_straight_seam,rating_bowl_slot2_in_swing,rating_bowl_slot3_out_swing,rating_bowl_slot4_leg_cutter,rating_bowl_slot5_off_cutter,rating_bowl_slot6_cross_seam,rating_bowl_slot7_slower_ball,rating_bowl_slot8_yorker,rating_bowl_slot9_bouncer
Jasprit,Bumrah,Specialist Bowler,Fast-Medium,88,100000,74,78,86,78,85,81,89,100,82
Shadab,Khan,Bowling All-Rounder,Leg Spin,75,75000,79,60,60,70,65,55,50,50,79
Mohammad,Nawaz,All-Rounder,Off Spin,75,60000,78,55,55,25,55,65,65,55,69
"""
        data = {
            "csv_file": (io.BytesIO(csv_content.encode("utf-8")), "players.csv"),
            "default_base_price": "50000",
            "clear_existing": "1"
        }
        resp = self.client.post(
            f"/auctioneer/auction/{auction_id}/upload_csv",
            data=data,
            content_type="multipart/form-data",
            follow_redirects=True
        )
        self.assertIn(b"Successfully uploaded and added 3 players", resp.data)

        # 3. Verify database storage and delivery names
        import json
        db = get_db()
        players = db.execute("SELECT * FROM auction_players WHERE auction_id = ? ORDER BY queue_order", (auction_id,)).fetchall()
        self.assertEqual(len(players), 3)

        # Bumrah (Pace / Fast-Medium)
        bumrah = players[0]
        self.assertEqual(bumrah["player_name"], "Jasprit Bumrah")
        self.assertEqual(bumrah["bowling_style"], "Fast-Medium")
        b_stats = json.loads(bumrah["stats_json"])
        delivs = {d["name"]: d["rating"] for d in b_stats["bowling_deliveries"]}
        self.assertEqual(delivs["Straight Seam"], 74)
        self.assertEqual(delivs["Yorker"], 100)
        self.assertEqual(delivs["Bouncer"], 82)

        # Shadab Khan (Leg Spin)
        shadab = players[1]
        self.assertEqual(shadab["player_name"], "Shadab Khan")
        self.assertEqual(shadab["bowling_style"], "Leg Spin")
        s_stats = json.loads(shadab["stats_json"])
        s_delivs = {d["name"]: d["rating"] for d in s_stats["bowling_deliveries"]}
        self.assertEqual(s_delivs["Off Break / Orthodox"], 79)
        self.assertEqual(s_delivs["Turn"], 65)
        self.assertEqual(s_delivs["Bounce"], 79)
        self.assertEqual(s_delivs["Top Spinner"], 60)

        # Mohammad Nawaz (Off Spin)
        nawaz = players[2]
        self.assertEqual(nawaz["player_name"], "Mohammad Nawaz")
        self.assertEqual(nawaz["bowling_style"], "Off Spin")
        n_stats = json.loads(nawaz["stats_json"])
        n_delivs = {d["name"]: d["rating"] for d in n_stats["bowling_deliveries"]}
        self.assertEqual(n_delivs["Off Break / Orthodox"], 78)
        self.assertEqual(n_delivs["Turn"], 55)
        self.assertEqual(n_delivs["Flight"], 65)
        self.assertEqual(n_delivs["Bounce"], 69)

        db.close()

    def test_ovr_base_price_formula(self):
        self.assertEqual(get_base_price_by_ovr(90), 2.0)
        self.assertEqual(get_base_price_by_ovr(85), 2.0)
        self.assertEqual(get_base_price_by_ovr(82), 1.5)
        self.assertEqual(get_base_price_by_ovr(76), 1.0)
        self.assertEqual(get_base_price_by_ovr(72), 0.5)
        self.assertEqual(get_base_price_by_ovr(67), 0.3)
        self.assertEqual(get_base_price_by_ovr(60), 0.2)

    def test_primary_stats_by_role(self):
        # 1. Specialist Bowler (Bumrah)
        bumrah_row = search_players_csv("Bumrah")[0]
        # Look up full row in CSV
        bumrah_full = get_player_by_offset(bumrah_row["offset_hex"])
        stats_b = json.loads(bumrah_full["stats_json"])
        prim_b = determine_primary_stats("Specialist Bowler", bumrah_full["bowling_style"], stats_b)
        self.assertTrue(len(prim_b) > 0)
        self.assertTrue(all(p["category"] == "Bowling" for p in prim_b))

        # 2. Top Order Bat (Babar Azam)
        babar_row = search_players_csv("Babar Azam")[0]
        babar_full = get_player_by_offset(babar_row["offset_hex"])
        stats_bat = json.loads(babar_full["stats_json"])
        prim_bat = determine_primary_stats("Top Order Bat", "", stats_bat)
        self.assertTrue(len(prim_bat) > 0)
        self.assertTrue(all(p["category"] == "Batting" for p in prim_bat))

        # 3. All-Rounder (Shadab Khan)
        shadab_row = search_players_csv("Shadab Khan")[0]
        shadab_full = get_player_by_offset(shadab_row["offset_hex"])
        stats_ar = json.loads(shadab_full["stats_json"])
        prim_ar = determine_primary_stats("All-Rounder", "Leg Spin", stats_ar)
        cats = set(p["category"] for p in prim_ar)
        self.assertIn("Batting", cats)
        self.assertIn("Bowling", cats)

    def test_squad_limits_and_reserve(self):
        from models import get_db
        db = get_db()
        admin_id = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
        # Create auction with budget 10 Cr, min squad 3, max squad 4
        db.execute(
            """INSERT INTO auctions (name, auctioneer_id, status, invite_token, budget_per_team, min_squad_size, max_squad_size)
               VALUES ('Squad Test', ?, 'active', 'sq_tok', 10.0, 3, 4)""",
            (admin_id,)
        )
        aid = db.execute("SELECT id FROM auctions WHERE invite_token = 'sq_tok'").fetchone()["id"]
        
        # Add bidder with 10.0 Cr
        db.execute(
            "INSERT OR IGNORE INTO users (username, password_hash, role) VALUES ('squad_bidder', 'hash', 'bidder')"
        )
        bidder_id = db.execute("SELECT id FROM users WHERE username = 'squad_bidder'").fetchone()["id"]
        db.execute(
            "INSERT INTO auction_bidders (auction_id, user_id, budget_remaining) VALUES (?, ?, 10.0)",
            (aid, bidder_id)
        )

        # Add 5 players
        pids = []
        for i in range(1, 6):
            db.execute(
                """INSERT INTO auction_players (auction_id, csv_offset, player_name, role, ovr, base_price, status, queue_order)
                   VALUES (?, ?, ?, 'Specialist Bowler', 80, 1.0, 'pending', ?)""",
                (aid, f"0xSQ{i}", f"Squad Player {i}", i)
            )
            pids.append(db.execute("SELECT last_insert_rowid()").fetchone()[0])
        db.commit()
        db.close()

        # Connect socket client as squad_bidder
        with self.client.session_transaction() as sess:
            sess["user_id"] = bidder_id
            sess["username"] = "squad_bidder"
            sess["role"] = "bidder"

        s_client = socketio.test_client(app, flask_test_client=self.client)
        s_client.emit("join_auction_room", {"auction_id": aid})

        # Set player 1 active
        db = get_db()
        db.execute("UPDATE auction_players SET status = 'active' WHERE id = ?", (pids[0],))
        db.commit()
        db.close()

        # Bid 9.9 Cr -> Remaining budget: 10.0 - 9.9 = 0.1 Cr.
        # But after buying player 1, squad count is 1. Min squad is 3 -> 2 more players needed.
        # Min reserve needed is 2 * 0.20 = 0.40 Cr!
        # Therefore, bid of 9.9 Cr should be rejected for reserve violation!
        s_client.emit("place_bid", {"auction_id": aid, "ap_id": pids[0], "amount": 9.9})
        err_event = None
        for ev in s_client.get_received():
            if ev["name"] == "bid_error":
                err_event = ev["args"][0]
        self.assertIsNotNone(err_event)
        self.assertIn("minimum squad", err_event["message"].lower())

        # Now bid valid amount that preserves reserve: 5.0 Cr
        s_client.emit("place_bid", {"auction_id": aid, "ap_id": pids[0], "amount": 5.0})
        bid_success = any(ev["name"] == "bid_update" for ev in s_client.get_received())
        self.assertTrue(bid_success)

        # Sell 4 players to squad_bidder to hit max squad cap (4)
        db = get_db()
        for idx in range(4):
            db.execute("UPDATE auction_players SET status = 'sold', sold_to = ?, sold_price = 1.0 WHERE id = ?", (bidder_id, pids[idx]))
        db.execute("UPDATE auction_players SET status = 'active' WHERE id = ?", (pids[4],))
        db.commit()
        db.close()

        # Now with 4 players bought, squad is full (max 4). Trying to bid on 5th player should fail with squad full error!
        s_client.emit("place_bid", {"auction_id": aid, "ap_id": pids[4], "amount": 1.0})
        cap_err = None
        for ev in s_client.get_received():
            if ev["name"] == "bid_error":
                cap_err = ev["args"][0]
        self.assertIsNotNone(cap_err)
        self.assertIn("squad is full", cap_err["message"].lower())

if __name__ == "__main__":
    unittest.main()
