from copy import deepcopy
from contextlib import closing
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import io
import json
import os
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from marvel.risk.model import canonical, fingerprint
from marvel_testnet import model, workflow
from marvel_testnet.model import DemoError, HOST
from marvel_testnet.store import Store
from marvel_testnet.transport import Client, VARIABLES, credentials
from tests.helpers import ROOT, WorkspaceCase
from .fixtures import Exchange, NOW, commission, metadata


class Response(io.BytesIO):
    status = 200
    def __init__(self, raw, url):
        super().__init__(raw)
        self.url = url
    def geturl(self):
        return self.url


class Opener:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
    def open(self, request, timeout):
        self.calls.append(request)
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        return Response(raw, request.full_url)


class TransportTests(unittest.TestCase):
    def client(self, *replies, orders=False):
        opener = Opener(replies)
        return Client(allow_network=True, allow_orders=orders, key="K" * 64, secret="S" * 64,
            opener=opener, monotonic=lambda: 0), opener

    def entry(self):
        return {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": "0.0001",
            "price": "80000", "timeInForce": "FOK", "newClientOrderId": "mtd_" + "a" * 20 + "_entry", "newOrderRespType": "FULL"}

    def test_explicit_opt_in_and_separate_credential_namespace(self):
        for value in (False, None, 1):
            with self.assertRaises(DemoError):
                Client(allow_network=value)
        with self.assertRaises(DemoError):
            credentials({"MARVEL_BINANCE_QUOTE_API_KEY": "K" * 64, "MARVEL_BINANCE_QUOTE_API_SECRET": "S" * 64})
        for value in ("." * 64, "*" * 64, "K\n" * 32, "short"):
            with self.assertRaises(DemoError):
                credentials(dict(zip(VARIABLES, (value, "S" * 64))))

    def test_fixed_host_signed_body_and_sanitized_receipts(self):
        client, opener = self.client({"serverTime": 1793199600000}, {}, orders=True)
        client.request("GET", "time")
        client.request("POST", "order", self.entry())
        request = opener.calls[-1]
        self.assertEqual(request.full_url, HOST + "/api/v3/order")
        self.assertIn(b"signature=", request.data)
        self.assertEqual(request.get_header("X-mbx-apikey"), "K" * 64)
        saved = canonical(client.receipts)
        for secret in ("K" * 64, "S" * 64, "signature="):
            self.assertNotIn(secret, saved)

    def test_production_urls_sapi_other_symbols_and_extra_parameters_are_rejected(self):
        client, opener = self.client(orders=True)
        for method, name, values in (("GET", "https://api.binance.com/api/v3/time", {}),
            ("GET", "/sapi/v1/convert/getQuote", {}), ("GET", "rules", {"symbol": "ETHUSDT"}),
            ("GET", "account", {"apiKey": "SECRET"})):
            with self.assertRaises(DemoError):
                client.request(method, name, values)
        self.assertEqual(opener.calls, [])

    def test_no_mutations_without_virtual_order_acknowledgment(self):
        client, opener = self.client()
        with self.assertRaises(DemoError):
            client.request("POST", "order", self.entry())
        with self.assertRaises(DemoError):
            client.request("DELETE", "order", {"symbol": "BTCUSDT", "origClientOrderId": self.entry()["newClientOrderId"]})
        self.assertFalse(opener.calls)

    def test_wire_notional_ceiling_and_no_market_or_margin_buy(self):
        client, opener = self.client(orders=True)
        for changes in ({"quantity": "0.001"}, {"type": "MARKET"}, {"side": "MARGIN_BUY"}, {"timeInForce": "GTC"}):
            with self.assertRaises(DemoError):
                client.request("POST", "order", {**self.entry(), **changes})
        self.assertFalse(opener.calls)

    def test_mutating_transport_timeout_is_never_retried(self):
        client, opener = self.client({"serverTime": 1793199600000}, URLError("SECRET"), {}, orders=True)
        client.request("GET", "time")
        with self.assertRaises(DemoError) as error:
            client.request("POST", "order", self.entry())
        self.assertTrue(error.exception.uncertain)
        self.assertEqual(len(opener.calls), 2)
        self.assertNotIn("SECRET", str(error.exception))

    def test_oversized_duplicate_key_nonfinite_and_wrong_response_url_fail_closed(self):
        for reply in (b"x" * 262145, b'{"serverTime":1,"serverTime":2}', b'{"serverTime":NaN}'):
            client, _ = self.client(reply)
            with self.assertRaises(DemoError):
                client.request("GET", "time")
        client, opener = self.client()
        with patch.object(opener, "open", return_value=Response(b'{}', "https://api.binance.com/api/v3/time")):
            with self.assertRaises(DemoError):
                client.request("GET", "time")

    def test_signed_calls_require_a_fresh_server_anchor(self):
        client, opener = self.client()
        with self.assertRaises(DemoError):
            client.request("GET", "account")
        self.assertFalse(opener.calls)


class ModelTests(unittest.TestCase):
    def test_rule_and_quote_parsers_reject_incomplete_or_wrong_symbols(self):
        document = metadata()
        rules = model.symbol(document)
        self.assertEqual(rules["step"], "0.00001")
        document["symbols"][0]["orderTypes"] = ["LIMIT"]
        with self.assertRaises(DemoError):
            model.symbol(document)
        for value in (1.2, True, "NaN", "-1", "Infinity"):
            with self.assertRaises(DemoError):
                model.number(value)

    def test_account_identifiers_are_salted_and_unrelated_assets_ignored(self):
        exchange = Exchange()
        result = model.account(exchange.account(), "salt")
        self.assertNotIn("uid", result)
        self.assertNotEqual(result["binding"], model.account(exchange.account(), "different")["binding"])
        document = exchange.account()
        document["balances"].append(document["balances"][0])
        with self.assertRaises(DemoError):
            model.account(document, "salt")

    def test_fee_rates_include_taxes_and_special_fees_without_discount(self):
        document = commission("0.001")
        document["taxCommission"]["taker"] = "0.0001"
        document["specialCommission"]["buyer"] = "0.0002"
        rates = model.fees(document)
        self.assertEqual(Decimal(rates["buy"]), Decimal("0.0013"))
        self.assertEqual(Decimal(rates["sell"]), Decimal("0.0011"))


class WorkflowTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.cfg = self.config()
        self.store = Store(self.cfg)
        self.store.initialize(NOW)
        self.exchange = Exchange()

    def cycle(self):
        return workflow.start_cycle(self.cfg, self.exchange, now=lambda: NOW)

    def test_complete_virtual_buy_protection_cancel_sell_and_balance_reconciliation(self):
        result = self.cycle()
        self.assertEqual(result["settled_engineering_cycles"], 1)
        self.assertIsNone(result["active_cycle"])
        self.assertFalse(result["review_reasons"])
        self.assertEqual(self.exchange.btc, 1)
        self.assertLess(Decimal(result["virtual_equity_usd"]), 50)
        self.assertFalse(self.cfg.database.exists())
        self.assertEqual(result["qualifying_strategy_trades"], 0)
        saved = self.store.load()["cycles"][0]
        self.assertTrue(saved["protection_ever_acknowledged"])
        self.assertFalse(saved["protection_acknowledged"])
        self.assertTrue(saved["unprotected_interval_observed"])

    def test_original_btc_fee_dust_rule_blocks_before_a_virtual_buy(self):
        self.exchange.rate = "0.001"
        result = self.cycle()
        self.assertIn("TESTNET_FULL_EXIT_GRID_BLOCKED", result["last_preflight_blockers"])
        self.assertFalse(any(m == "POST" for m, _, _ in self.exchange.calls))
        self.assertIsNone(result["active_cycle"])

    def test_lost_entry_ack_is_reconciled_without_a_duplicate_buy(self):
        self.exchange.lost_entry_ack = True
        result = self.cycle()
        self.assertEqual(result["settled_engineering_cycles"], 1)
        buys = [p for m, n, p in self.exchange.calls if m == "POST" and n == "order" and p["side"] == "BUY"]
        self.assertEqual(len(buys), 1)

    def test_unknown_entry_persists_and_get_only_recovery_never_resubmits(self):
        self.exchange.lost_entry_ack = self.exchange.entry_missing = True
        result = self.cycle()
        self.assertTrue(result["review_reasons"])
        self.assertFalse(result["active_cycle"]["holding_quantity_fully_reconciled"])
        self.exchange.entry_missing = False
        before = len(self.exchange.calls)
        observed = workflow.reconcile(self.cfg, self.exchange, now=lambda: NOW)
        self.assertTrue(observed["active_cycle"]["holding_quantity_fully_reconciled"])
        self.assertTrue(all(m == "GET" for m, _, _ in self.exchange.calls[before:]))
        with self.assertRaises(DemoError):
            self.cycle()

    def test_explicit_recovery_sells_only_confirmed_owned_btc_and_preserves_review_halt(self):
        self.exchange.entry_missing = True
        self.cycle()
        self.exchange.entry_missing = False
        before = len(self.exchange.calls)
        result = workflow.recover(self.cfg, self.exchange, now=lambda: NOW)
        self.assertIsNone(result["active_cycle"])
        self.assertTrue(result["review_reasons"])
        self.assertEqual(self.exchange.btc, 1)
        self.assertFalse(any(m == "POST" and p.get("side") == "BUY" for m, _, p in self.exchange.calls[before:]))

    def test_stop_filling_during_cancel_does_not_sell_the_faucet_inventory(self):
        self.exchange.stop_fill_on_cancel = True
        result = self.cycle()
        self.assertEqual(result["settled_engineering_cycles"], 1)
        sells = [p for m, n, p in self.exchange.calls if m == "POST" and n == "order" and p["type"] == "LIMIT" and p["side"] == "SELL"]
        self.assertEqual(sells, [])
        self.assertEqual(self.exchange.btc, 1)

    def test_partial_stop_below_the_new_exit_minimum_stays_owned(self):
        self.exchange.partial_stop_on_cancel = True
        result = self.cycle()
        self.assertEqual(result["settled_engineering_cycles"], 0)
        self.assertIn("TESTNET_RESIDUAL_OR_EXIT_MINIMUM", result["review_reasons"])
        self.assertGreater(Decimal(result["active_cycle"]["holding_btc"]), 0)

    def test_unconfirmed_stop_cancel_keeps_holding_and_never_places_a_second_sell(self):
        self.exchange.stop_unknown_on_cancel = True
        result = self.cycle()
        self.assertIn("TESTNET_STOP_CANCEL_UNCONFIRMED", result["review_reasons"])
        self.assertGreater(Decimal(result["active_cycle"]["holding_btc"]), 0)
        self.assertNotIn("exit", self.store.load()["active"]["orders"])

    def test_protection_is_not_advertised_without_a_matching_locked_balance(self):
        actual = self.exchange.account
        def unlocked():
            account = actual()
            for row in account["balances"]:
                if row["asset"] == "BTC":
                    row["free"] = str(Decimal(row["free"]) + Decimal(row["locked"]))
                    row["locked"] = "0"
            return account
        self.exchange.account = unlocked
        result = self.cycle()
        self.assertIn("TESTNET_PROTECTION_NOT_ACTIVE", result["review_reasons"])
        self.assertFalse(result["active_cycle"]["protection_acknowledged"])

    def test_failed_fok_exit_rearms_protection_and_retains_open_ownership(self):
        self.exchange.exit_expired = True
        result = self.cycle()
        self.assertEqual(result["settled_engineering_cycles"], 0)
        self.assertIsNotNone(result["active_cycle"])
        self.assertIn("rearm", self.store.load()["active"]["orders"])
        self.assertGreater(Decimal(result["active_cycle"]["holding_btc"]), 0)

    def test_unexpected_btc_commission_is_retained_as_dust_and_cannot_settle(self):
        self.exchange.charge_unexpected_btc = True
        result = self.cycle()
        self.assertIn("TESTNET_RESIDUAL_OR_EXIT_MINIMUM", result["review_reasons"])
        self.assertIsNotNone(result["active_cycle"])
        self.assertGreater(Decimal(result["active_cycle"]["holding_btc"]), 0)
        risk = self.store.load()["risk"]
        holding = Decimal(result["active_cycle"]["holding_btc"])
        self.assertLess(Decimal(risk["position_value_usd"]), holding * Decimal("79999") * Decimal("0.995"))

    def test_partial_exit_proceeds_remain_in_cash_and_equity_identities(self):
        self.exchange.partial_stop_on_cancel = True
        self.cycle()
        state = self.store.load()
        risk, cycle = state["risk"], state["active"]
        self.assertGreater(Decimal(risk["cash_usd"]), 50 - Decimal(cycle["entry_cost_usd"]))
        self.assertEqual(Decimal(risk["cash_usd"]) + Decimal(risk["position_value_usd"]),
            50 + Decimal(risk["realized_net_pnl_usd"]) + Decimal(risk["unrealized_net_pnl_usd"]))

    def test_nonmatching_validation_rejection_never_creates_a_matching_order(self):
        self.exchange.reject_test = True
        result = self.cycle()
        self.assertIsNone(result["active_cycle"])
        self.assertEqual(result["virtual_equity_usd"], "50")
        self.assertFalse(any(n == "order" and m == "POST" for m, n, _ in self.exchange.calls))

    def test_unallocated_bnb_commission_does_not_silently_disappear(self):
        self.exchange.charge_unexpected_bnb = True
        self.exchange.bnb = Decimal(100)
        result = self.cycle()
        self.assertIn("TESTNET_UNALLOCATED_COMMISSION_ASSET", result["review_reasons"])
        self.assertEqual(result["active_cycle"]["external_fee_assets"], {"BNB": "0.01"})

    def test_a_refused_repeat_command_does_not_reset_capital_or_force_a_minimum(self):
        self.cycle()
        result = self.cycle()
        self.assertIn("ENTRY_OR_PROTECTIVE_EXIT_MINIMUM", result["last_preflight_blockers"])
        self.assertEqual(len(self.store.load()["cycles"]), 1)
        before = result["virtual_equity_usd"]
        self.assertEqual(workflow.summary(self.store.initialize(NOW))["virtual_equity_usd"], before)

    def test_losing_streak_halt_blocks_a_new_cycle(self):
        state = self.store.load()
        state["risk"]["losing_streak"] = 3
        self.store.save(state, "invented_streak_injection")
        result = self.cycle()
        self.assertIn("LOSING_STREAK", result["last_preflight_blockers"])
        self.assertFalse(any(m == "POST" for m, _, _ in self.exchange.calls))

    def test_weekly_loss_cap_cannot_be_bypassed_by_another_cycle(self):
        state = self.store.load()
        state["risk"].update(realized_net_pnl_usd="-1", cash_usd="49")
        self.store.save(state, "invented_loss_injection")
        result = self.cycle()
        self.assertIn("WEEKLY_LOSS", result["last_preflight_blockers"])
        self.assertFalse(any(m == "POST" for m, _, _ in self.exchange.calls))

    def test_zero_fill_expiry_is_not_a_completed_trade(self):
        self.exchange.entry_expired = True
        result = self.cycle()
        self.assertEqual(result["settled_engineering_cycles"], 0)
        self.assertEqual(result["terminal_engineering_attempts"], 1)
        self.assertEqual(result["virtual_equity_usd"], "50")

    def test_local_clock_mismatch_and_late_entry_hours_block_before_mutations(self):
        for when, reason in ((NOW + timedelta(minutes=1), "TESTNET_LOCAL_CLOCK_MISMATCH"),
                             (NOW.replace(hour=23), "TESTNET_SESSION_ENTRY_BLOCK")):
            result = workflow.preflight(self.cfg, self.exchange, now=lambda: when)
            self.assertIn(reason, result["blockers"])
        self.assertFalse(any(m == "POST" for m, _, _ in self.exchange.calls))

    def test_account_change_reset_and_unrelated_balance_changes_do_not_create_profit(self):
        self.exchange.entry_missing = True
        self.cycle()
        self.exchange.entry_missing = False
        self.exchange.usdt += 1000
        result = workflow.reconcile(self.cfg, self.exchange, now=lambda: NOW)
        self.assertIn("TESTNET_BALANCE_OR_RESET_MISMATCH", result["review_reasons"])
        self.assertIsNotNone(result["active_cycle"])

    def test_journal_is_durable_before_an_order_leaves_the_process(self):
        actual = self.exchange.request
        def guarded(method, name, params=None):
            if method == "POST" and name == "order":
                active = self.store.load()["active"]
                role = params["newClientOrderId"].rsplit("_", 1)[1]
                self.assertTrue(active["orders"][role]["intent_saved"])
            return actual(method, name, params)
        self.exchange.request = guarded
        self.cycle()

    def test_source_and_financial_changes_and_concurrent_workers_fail_closed(self):
        with patch("marvel_testnet.store.sources", return_value={"changed": "0" * 64}):
            with self.assertRaises(DemoError):
                self.cycle()
        with self.store.locked():
            with self.assertRaises(DemoError):
                self.cycle()
        self.document["risk"]["per_trade_cap_usd"] = "0.5"
        self.write_config()
        with self.assertRaises(DemoError):
            self.cycle()
        self.assertFalse(self.exchange.calls)

    def test_corrupted_journal_is_not_reinitialized(self):
        with closing(sqlite3.connect(self.store.database)) as connection, connection:
            connection.execute("UPDATE events SET digest=? WHERE sequence=1", ("0" * 64,))
        with self.assertRaises(DemoError):
            self.store.initialize(NOW)


class CommandTests(WorkspaceCase):
    def cli(self, *args):
        environment = dict(os.environ)
        for name in VARIABLES:
            environment.pop(name, None)
        return subprocess.run([sys.executable, "-m", "marvel_testnet", "--workspace", str(self.root), *args],
            cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)

    def test_offline_init_status_and_missing_credentials_do_not_open_owner_account(self):
        self.assertEqual(self.cli("init").returncode, 0)
        self.assertEqual(self.cli("status").returncode, 0)
        result = self.cli("preflight", "--allow-network")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "TESTNET_CREDENTIALS_MISSING")
        self.assertFalse(self.config().database.exists())

    def test_missing_state_and_no_production_host_budget_risk_or_secret_arguments(self):
        self.assertEqual(self.cli("status").returncode, 2)
        for flag in ("--live", "--host", "--budget", "--risk", "--secret", "--key", "--reset"):
            result = self.cli("init", flag, "DO_NOT_ECHO_SECRET")
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("DO_NOT_ECHO_SECRET", result.stdout + result.stderr)

    def test_orders_and_network_require_distinct_explicit_opt_ins(self):
        self.cli("init")
        for command in ("public-check", "preflight", "cycle", "reconcile", "recover"):
            self.assertEqual(self.cli(command).returncode, 2)
        result = self.cli("cycle", "--allow-network")
        self.assertEqual(json.loads(result.stdout)["error"]["code"], "TESTNET_VIRTUAL_ORDER_OPT_IN_REQUIRED")
