"""
test_bidding.py — tests for the live bidding core (permissions, bid rules, selling).

Runs against a throwaway database, never the real auction_app.db.
    python -m unittest test_bidding -v
"""
import os
import sys
import tempfile
import threading
import unittest

_tmp_dir = tempfile.mkdtemp(prefix="auction_bid_test_")
os.environ["AUCTION_DB_PATH"] = os.path.join(_tmp_dir, "test.db")
sys.path.append(os.path.dirname(__file__))

from werkzeug.security import generate_password_hash  # noqa: E402

import app as app_module  # noqa: E402
from app import MIN_BID_INCREMENT, app, get_auction_snapshot, socketio  # noqa: E402
from models import get_db, init_db  # noqa: E402

BUDGET = 1_000_000
BASE = 50_000


def last(client, name):
    """Most recent received event with this name (payload), draining the queue."""
    found = None
    for ev in client.get_received():
        if ev["name"] == name:
            found = ev["args"][0]
    return found


class BiddingBase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        init_db()
        db = get_db()
        for t in ("bids", "auction_players", "auction_bidders", "auctions"):
            db.execute(f"DELETE FROM {t}")
        db.execute("DELETE FROM users WHERE username != 'admin'")
        for name in ("alice", "bob", "carol"):
            db.execute(
                "INSERT INTO users (username, password_hash, role, team_name) VALUES (?, ?, 'bidder', ?)",
                (name, generate_password_hash("pw"), f"Team {name.title()}"),
            )
        db.execute(
            "INSERT INTO users (username, password_hash, role) VALUES ('other_host', ?, 'auctioneer')",
            (generate_password_hash("pw"),),
        )
        admin = db.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]
        db.execute(
            "INSERT INTO auctions (name, auctioneer_id, status, invite_token, budget_per_team) "
            "VALUES ('T', ?, 'active', 'tok', ?)",
            (admin, BUDGET),
        )
        self.aid = db.execute("SELECT id FROM auctions").fetchone()["id"]
        self.uid = {r["username"]: r["id"] for r in db.execute("SELECT id, username FROM users")}
        for name in ("alice", "bob", "carol"):
            db.execute(
                "INSERT INTO auction_bidders (auction_id, user_id, budget_remaining) VALUES (?, ?, ?)",
                (self.aid, self.uid[name], BUDGET),
            )
        for i in range(1, 4):
            db.execute(
                "INSERT INTO auction_players (auction_id, csv_offset, player_name, role, bowling_style, ovr, "
                "base_price, status, queue_order) VALUES (?, ?, ?, 'Top Order Bat', 'Medium', 80, ?, 'pending', ?)",
                (self.aid, f"0x{i}", f"Player {i}", BASE, i),
            )
        db.commit()
        self.pids = [r["id"] for r in db.execute("SELECT id FROM auction_players ORDER BY id")]
        db.close()

    # -- helpers ---------------------------------------------------------
    def connect(self, username):
        http = app.test_client()
        r = http.post("/login", data={"username": username, "password": "pw"})
        self.assertEqual(r.status_code, 302)
        sc = socketio.test_client(app, flask_test_client=http)
        sc.emit("join_auction_room", {"auction_id": self.aid})
        sc.get_received()
        return sc

    def host(self):
        http = app.test_client()
        http.post("/login", data={"username": "admin", "password": "admin123"})
        sc = socketio.test_client(app, flask_test_client=http)
        sc.emit("join_auction_room", {"auction_id": self.aid})
        sc.get_received()
        return sc

    def bid(self, sc, ap_id, amount, **extra):
        sc.emit("place_bid", {"auction_id": self.aid, "ap_id": ap_id, "amount": amount, **extra})
        got = sc.get_received()
        err = next((e["args"][0]["message"] for e in got if e["name"] == "bid_error"), None)
        upd = next((e["args"][0] for e in got if e["name"] == "bid_update"), None)
        return err, upd

    def offer(self, host, ap_id):
        host.emit("auctioneer_next_player", {"auction_id": self.aid, "ap_id": ap_id})
        return host.get_received()

    def row(self, sql, *args):
        db = get_db()
        try:
            return db.execute(sql, args).fetchone()
        finally:
            db.close()

    def budget(self, name):
        return self.row(
            "SELECT budget_remaining b FROM auction_bidders WHERE auction_id=? AND user_id=?",
            self.aid, self.uid[name],
        )["b"]


class TestPermissions(BiddingBase):
    def test_anonymous_cannot_join_or_bid(self):
        self.offer(self.host(), self.pids[0])
        anon = socketio.test_client(app)
        anon.emit("join_auction_room", {"auction_id": self.aid})
        self.assertIn("log in", last(anon, "bid_error")["message"].lower())
        err, upd = self.bid(anon, self.pids[0], BASE)
        self.assertIsNotNone(err)
        self.assertIsNone(upd)
        self.assertEqual(self.row("SELECT COUNT(*) c FROM bids")["c"], 0)

    def test_bidder_cannot_use_auctioneer_controls(self):
        alice = self.connect("alice")
        for event, payload in (
            ("auctioneer_next_player", {"auction_id": self.aid, "ap_id": self.pids[0]}),
            ("auctioneer_sell", {"auction_id": self.aid, "ap_id": self.pids[0]}),
            ("auctioneer_unsold", {"auction_id": self.aid, "ap_id": self.pids[0]}),
            ("auctioneer_pause", {"auction_id": self.aid}),
            ("auctioneer_resume", {"auction_id": self.aid}),
        ):
            alice.emit(event, payload)
            self.assertIsNotNone(last(alice, "host_error"), event)
        self.assertEqual(self.row("SELECT status FROM auctions")["status"], "active")
        self.assertEqual(self.row("SELECT COUNT(*) c FROM auction_players WHERE status != 'pending'")["c"], 0)

    def test_other_auctioneer_cannot_control_this_auction(self):
        http = app.test_client()
        http.post("/login", data={"username": "other_host", "password": "pw"})
        sc = socketio.test_client(app, flask_test_client=http)
        sc.emit("auctioneer_next_player", {"auction_id": self.aid, "ap_id": self.pids[0]})
        self.assertIsNotNone(last(sc, "host_error"))
        self.assertEqual(self.row("SELECT COUNT(*) c FROM auction_players WHERE status='active'")["c"], 0)

    def test_non_member_cannot_bid_or_join(self):
        db = get_db()
        db.execute("DELETE FROM auction_bidders WHERE user_id = ?", (self.uid["carol"],))
        db.commit()
        db.close()
        self.offer(self.host(), self.pids[0])
        http = app.test_client()
        http.post("/login", data={"username": "carol", "password": "pw"})
        sc = socketio.test_client(app, flask_test_client=http)
        sc.emit("join_auction_room", {"auction_id": self.aid})
        self.assertIsNotNone(last(sc, "bid_error"))
        err, _ = self.bid(sc, self.pids[0], BASE)
        self.assertIn("not registered", err)

    def test_spoofed_identity_in_payload_is_ignored(self):
        self.offer(self.host(), self.pids[0])
        alice = self.connect("alice")
        err, upd = self.bid(alice, self.pids[0], BASE, user_id=self.uid["bob"], username="bob")
        self.assertIsNone(err)
        self.assertEqual(upd["highest_bidder_id"], self.uid["alice"])
        self.assertEqual(self.row("SELECT bidder_id FROM bids")["bidder_id"], self.uid["alice"])


class TestBidRules(BiddingBase):
    def setUp(self):
        super().setUp()
        self.h = self.host()
        self.offer(self.h, self.pids[0])
        self.alice, self.bob = self.connect("alice"), self.connect("bob")
        self.p = self.pids[0]

    def test_opening_bid_at_base_is_allowed_but_below_is_not(self):
        err, _ = self.bid(self.alice, self.p, BASE - 1)
        self.assertIn("at least", err)
        err, upd = self.bid(self.alice, self.p, BASE)
        self.assertIsNone(err)
        self.assertEqual(upd["current_bid"], BASE)
        self.assertEqual(upd["min_next_bid"], BASE + MIN_BID_INCREMENT)

    def test_must_beat_current_by_min_increment(self):
        self.bid(self.alice, self.p, BASE)
        err, _ = self.bid(self.bob, self.p, BASE + MIN_BID_INCREMENT - 1)
        self.assertIn("at least", err)
        err, upd = self.bid(self.bob, self.p, BASE + MIN_BID_INCREMENT)
        self.assertIsNone(err)
        self.assertEqual(upd["highest_bidder_id"], self.uid["bob"])

    def test_cannot_outbid_yourself(self):
        self.bid(self.alice, self.p, BASE)
        err, _ = self.bid(self.alice, self.p, BASE + 50_000)
        self.assertIn("already hold", err)

    def test_budget_limit(self):
        err, _ = self.bid(self.alice, self.p, BUDGET + 1)
        self.assertIn("Insufficient", err)
        err, _ = self.bid(self.alice, self.p, BUDGET)
        self.assertIsNone(err)

    def test_garbage_amounts_do_not_crash(self):
        for bad in ("abc", None, -5, 0, 1.5e30, True, [1], {"a": 1}):
            err, upd = self.bid(self.alice, self.p, bad)
            self.assertIsNotNone(err, repr(bad))
            self.assertIsNone(upd)
        self.assertEqual(self.row("SELECT COUNT(*) c FROM bids")["c"], 0)

    def test_stale_or_wrong_player_rejected(self):
        err, _ = self.bid(self.alice, self.pids[1], BASE)  # not on the block
        self.assertIn("no longer on the block", err)

    def test_paused_blocks_bids_and_resume_restores(self):
        self.h.emit("auctioneer_pause", {"auction_id": self.aid})
        self.h.get_received()
        err, _ = self.bid(self.alice, self.p, BASE)
        self.assertIn("paused", err)
        self.h.emit("auctioneer_resume", {"auction_id": self.aid})
        self.h.get_received()
        err, _ = self.bid(self.alice, self.p, BASE)
        self.assertIsNone(err)

    def test_pending_and_ended_auction_reject_bids(self):
        for status, word in (("pending", "started"), ("ended", "ended")):
            db = get_db()
            db.execute("UPDATE auctions SET status = ?", (status,))
            db.commit()
            db.close()
            err, _ = self.bid(self.alice, self.p, BASE)
            self.assertIn(word, err)

    def test_everyone_in_room_gets_the_update(self):
        self.bid(self.alice, self.p, BASE)
        upd = last(self.bob, "bid_update")
        self.assertEqual(upd["current_bid"], BASE)
        self.assertEqual(upd["highest_bidder_name"], "Team Alice")
        self.assertIsNotNone(last(self.h, "bid_update"))


class TestSelling(BiddingBase):
    def setUp(self):
        super().setUp()
        self.h = self.host()
        self.offer(self.h, self.pids[0])
        self.alice, self.bob = self.connect("alice"), self.connect("bob")
        self.p = self.pids[0]

    def sell(self):
        self.h.emit("auctioneer_sell", {"auction_id": self.aid, "ap_id": self.p})
        got = self.h.get_received()
        err = next((e["args"][0]["message"] for e in got if e["name"] == "host_error"), None)
        sold = next((e["args"][0] for e in got if e["name"] == "player_sold"), None)
        return err, sold

    def test_cannot_sell_without_bids(self):
        err, sold = self.sell()
        self.assertIn("No bids", err)
        self.assertIsNone(sold)

    def test_sell_charges_winner_once_and_double_sell_fails(self):
        self.bid(self.alice, self.p, BASE)
        self.bid(self.bob, self.p, BASE + 20_000)
        err, sold = self.sell()
        self.assertIsNone(err)
        self.assertEqual(sold["sold_to"], "Team Bob")
        self.assertEqual(sold["sold_price"], BASE + 20_000)
        self.assertEqual(self.budget("bob"), BUDGET - BASE - 20_000)
        self.assertEqual(self.budget("alice"), BUDGET)
        # second click / stale tab
        err, sold = self.sell()
        self.assertIsNotNone(err)
        self.assertIsNone(sold)
        self.assertEqual(self.budget("bob"), BUDGET - BASE - 20_000)

    def test_sell_blocked_while_paused(self):
        self.bid(self.alice, self.p, BASE)
        self.h.emit("auctioneer_pause", {"auction_id": self.aid})
        self.h.get_received()
        err, sold = self.sell()
        self.assertIn("paused", err)
        self.assertEqual(self.budget("alice"), BUDGET)

    def test_sell_fails_cleanly_if_winner_cannot_afford(self):
        self.bid(self.alice, self.p, BASE)
        db = get_db()
        db.execute("UPDATE auction_bidders SET budget_remaining = 10 WHERE user_id = ?", (self.uid["alice"],))
        db.commit()
        db.close()
        err, sold = self.sell()
        self.assertIn("afford", err)
        self.assertEqual(self.row("SELECT status FROM auction_players WHERE id=?", self.p)["status"], "active")
        self.assertEqual(self.budget("alice"), 10)  # no negative balance

    def test_one_lot_at_a_time_and_unsold_reoffer_starts_fresh(self):
        # can't put a second player on the block
        got = self.offer(self.h, self.pids[1])
        self.assertTrue(any(e["name"] == "host_error" for e in got))
        self.assertEqual(self.row("SELECT COUNT(*) c FROM auction_players WHERE status='active'")["c"], 1)
        # bid, pass on the player, then offer again: old bids must not carry over
        self.bid(self.alice, self.p, 90_000)
        self.h.emit("auctioneer_unsold", {"auction_id": self.aid, "ap_id": self.p})
        self.h.get_received()
        self.assertEqual(get_auction_snapshot(self.aid)["has_lot"], False)
        self.offer(self.h, self.p)
        snap = get_auction_snapshot(self.aid)
        self.assertEqual(snap["current_bid"], BASE)
        self.assertFalse(snap["has_bids"])
        err, upd = self.bid(self.alice, self.p, BASE)  # alice may bid again (not "highest" any more)
        self.assertIsNone(err)

    def test_cannot_offer_a_sold_player_again(self):
        self.bid(self.alice, self.p, BASE)
        self.sell()
        got = self.offer(self.h, self.p)
        self.assertTrue(any(e["name"] == "host_error" for e in got))

    def test_state_survives_without_any_in_memory_state(self):
        # Everything is read from the DB, so a "restart" (fresh snapshot) agrees with reality.
        self.bid(self.alice, self.p, BASE)
        self.bid(self.bob, self.p, BASE + 10_000)
        snap = get_auction_snapshot(self.aid)
        self.assertEqual((snap["current_bid"], snap["highest_bidder_id"]), (BASE + 10_000, self.uid["bob"]))

    def test_end_auction_releases_live_lot_and_stops_bidding(self):
        http = app.test_client()
        http.post("/login", data={"username": "admin", "password": "admin123"})
        self.bid(self.alice, self.p, BASE)
        r = http.post(f"/auctioneer/auction/{self.aid}/end")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.row("SELECT status FROM auctions")["status"], "ended")
        self.assertEqual(self.row("SELECT status FROM auction_players WHERE id=?", self.p)["status"], "pending")
        err, _ = self.bid(self.bob, self.p, BASE + 100_000)
        self.assertIsNotNone(err)


class TestConcurrency(BiddingBase):
    def test_simultaneous_identical_bids_only_one_wins(self):
        self.offer(self.host(), self.pids[0])
        clients = [self.connect(n) for n in ("alice", "bob", "carol")]
        results, barrier = [], threading.Barrier(len(clients))

        def go(c):
            barrier.wait()
            results.append(self.bid(c, self.pids[0], BASE))

        threads = [threading.Thread(target=go, args=(c,)) for c in clients]
        [t.start() for t in threads]
        [t.join() for t in threads]
        accepted = [r for r in results if r[0] is None]  # no error => this caller's bid was accepted
        self.assertEqual(len(accepted), 1, results)
        self.assertEqual(self.row("SELECT COUNT(*) c FROM bids")["c"], 1)

    def test_many_racing_bids_leave_a_consistent_winner(self):
        self.offer(self.host(), self.pids[0])
        clients = [self.connect(n) for n in ("alice", "bob", "carol")]
        barrier = threading.Barrier(len(clients))

        def go(c, n):
            barrier.wait()
            for i in range(1, 15):
                self.bid(c, self.pids[0], BASE + i * 5_000 * 3 + n * 5_000)

        threads = [threading.Thread(target=go, args=(c, n)) for n, c in enumerate(clients)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        db = get_db()
        amounts = [r["amount"] for r in db.execute("SELECT amount FROM bids ORDER BY id")]
        db.close()
        # every recorded bid beat the one before it by at least the increment
        for a, b in zip(amounts, amounts[1:]):
            self.assertGreaterEqual(b - a, MIN_BID_INCREMENT)
        snap = get_auction_snapshot(self.aid)
        self.assertEqual(snap["current_bid"], max(amounts))


class TestStartEnd(BiddingBase):
    def test_start_requires_bidders_and_players(self):
        http = app.test_client()
        http.post("/login", data={"username": "admin", "password": "admin123"})
        db = get_db()
        db.execute("UPDATE auctions SET status='pending'")
        db.execute("DELETE FROM auction_players")
        db.commit()
        db.close()
        http.post(f"/auctioneer/auction/{self.aid}/start")
        self.assertEqual(self.row("SELECT status FROM auctions")["status"], "pending")

    def test_cannot_restart_or_reend(self):
        http = app.test_client()
        http.post("/login", data={"username": "admin", "password": "admin123"})
        http.post(f"/auctioneer/auction/{self.aid}/start")  # already active
        self.assertEqual(self.row("SELECT status FROM auctions")["status"], "active")
        http.post(f"/auctioneer/auction/{self.aid}/end")
        http.post(f"/auctioneer/auction/{self.aid}/end")
        self.assertEqual(self.row("SELECT status FROM auctions")["status"], "ended")

    def test_removal_guards(self):
        host = self.host()
        self.offer(host, self.pids[0])
        alice = self.connect("alice")
        self.bid(alice, self.pids[0], BASE)
        http = app.test_client()
        http.post("/login", data={"username": "admin", "password": "admin123"})
        http.post(f"/auctioneer/auction/{self.aid}/remove_bidder", data={"user_id": self.uid["alice"]})
        self.assertEqual(self.budget("alice"), BUDGET)  # leader still in the auction
        http.post(f"/auctioneer/auction/{self.aid}/remove_player", data={"ap_id": self.pids[0]})
        self.assertIsNotNone(self.row("SELECT id FROM auction_players WHERE id=?", self.pids[0]))


if __name__ == "__main__":
    unittest.main()
