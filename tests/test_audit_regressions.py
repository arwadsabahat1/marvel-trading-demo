"""Cross-component review cases found after Phase 3; all inputs are synthetic."""

from copy import deepcopy
from http.client import IncompleteRead
import json
import unittest
from unittest.mock import patch

from marvel.data.quality import read_rows
from marvel.errors import DataError, RiskError
from tests import test_data_client as download_fixtures
from tests import test_risk_commands as command_fixtures
from tests.helpers import WorkspaceCase
from tests.risk_helpers import RiskCase


class ReconciliationAuditTests(RiskCase):
    def test_explicit_verified_no_fill_closes_unknown_claim_without_creating_trade(self):
        claim, _ = self.submitted()
        self.risks.change("unknown", {"id": claim})
        report = self.report(identity="verified-no-fill")
        report["confirmed_flat_ids"] = report["no_fill_ids"] = [claim]
        self.risks.change("reconcile", report)
        self.assertEqual(self.current()["claims"][0]["status"], "closed")
        self.assertFalse(self.current()["claims"][0]["has_position"])
        self.assertEqual(len(self.current()["closed_trades"]), 0)
        self.assertEqual(self.risks.status()["budgets"]["reserved_loss_usd"], "0")

    def test_observed_fill_survives_a_flat_mark_and_cannot_be_relabelled_no_fill(self):
        claim, _ = self.submitted()
        self.risks.change("reconcile", self.report(claim=claim, positions=1, value="5", unrealized="-0.1", identity="filled"))
        self.risks.change("reconcile", self.report(identity="flat-before-outcome"))
        report = self.report(identity="false-no-fill")
        report["confirmed_flat_ids"] = report["no_fill_ids"] = [claim]
        before = self.risks.foundation.read()
        with self.assertRaises(RiskError):
            self.risks.change("reconcile", report)
        self.assertEqual(self.risks.foundation.read(), before)
        final = self.report(claim=claim, pnl="-0.1", flat=True, identity="verified-outcome")
        self.risks.change("reconcile", final)
        self.assertEqual(self.current()["losing_streak"], 1)

    def test_no_fill_ids_cannot_duplicate_conflict_or_name_other_claims(self):
        claim, _ = self.submitted()
        for no_fill, flat, has_trade in [([claim, claim], [claim], False), ([claim], [], False), ([claim], [claim], True)]:
            report = self.report(claim=claim, flat=has_trade, identity="invalid-closure")
            report["confirmed_flat_ids"], report["no_fill_ids"] = flat, no_fill
            with self.assertRaises(RiskError):
                self.risks.change("reconcile", report)

    def test_no_fill_confirmation_cannot_invent_new_fees(self):
        claim, _ = self.submitted()
        report = self.report(fee="0.01", identity="fee-on-no-fill")
        report["confirmed_flat_ids"] = report["no_fill_ids"] = [claim]
        with self.assertRaises(RiskError):
            self.risks.change("reconcile", report)

    def test_flat_confirmation_without_outcome_or_no_fill_proof_is_rejected(self):
        claim, _ = self.submitted()
        self.risks.change("unknown", {"id": claim})
        report = self.report(identity="ambiguous-flat")
        report["confirmed_flat_ids"] = [claim]
        before = self.risks.foundation.read()
        with self.assertRaises(RiskError):
            self.risks.change("reconcile", report)
        self.assertEqual(self.risks.foundation.read(), before)

    def test_filled_claim_cannot_be_declared_cancelled_without_a_trade_outcome(self):
        claim, _ = self.submitted()
        self.risks.change("reconcile", self.report(claim=claim, positions=1, value="5", unrealized="-0.1", identity="filled"))
        report = self.report(identity="forgotten-trade")
        report["confirmed_flat_ids"] = [claim]
        with self.assertRaises(RiskError):
            self.risks.change("reconcile", report)

    def test_malformed_trade_reservation_id_is_a_fixed_risk_error(self):
        claim, _ = self.submitted()
        report = self.report(claim=claim, flat=True)
        report["closed_trades"][0]["reservation_id"] = []
        with self.assertRaises(RiskError):
            self.risks.change("reconcile", report)


class ProfileAuditTests(WorkspaceCase):
    cli = command_fixtures.RiskCommandTests.cli
    def test_list_valued_fee_asset_is_rejected_without_traceback_or_state(self):
        path = self.root / "config/btc-paper-risk.json"
        document = json.loads(path.read_bytes())
        for bad in (["SECRET_PROFILE_SENTINEL"], {"SECRET_PROFILE_SENTINEL": 1}, True, None):
            document["entry_fee_asset"] = bad
            path.write_text(json.dumps(document), encoding="utf-8")
            result = self.cli("init")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, "")
            self.assertEqual(json.loads(result.stdout)["error"]["code"], "RISK_INVALID")
            self.assertNotIn("SECRET_PROFILE_SENTINEL", result.stdout)
            self.assertFalse(self.config().database.exists())


class DownloadAuditTests(unittest.TestCase):
    client = download_fixtures.PublicClientTests.client

    def test_incomplete_download_can_recover_without_saving_partial_response(self):
        client, opener, sleeps = self.client(IncompleteRead(b"PARTIAL", 100), download_fixtures.server())
        self.assertEqual(client.server_time(), download_fixtures.NOW_US)
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(sleeps, [1])
    def test_incomplete_http_response_is_retried_with_bounded_fixed_failure(self):
        client, opener, sleeps = self.client(*(IncompleteRead(b"SECRET_HTTP_BODY", 100) for _ in range(3)))
        with self.assertRaises(DataError) as caught:
            client.server_time()
        self.assertEqual(caught.exception.code, "DATA_NETWORK")
        self.assertNotIn("SECRET_HTTP_BODY", str(caught.exception))
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(sleeps, [1, 2])


class CsvAuditTests(unittest.TestCase):
    def test_csv_reader_stops_at_the_row_limit_instead_of_reading_the_entire_input(self):
        def too_many_rows():
            for _ in range(4):
                yield ["synthetic"]
            raise AssertionError("Reader consumed beyond the bounded rejection point")

        with patch("marvel.data.quality.MAX_ROWS", 3), patch("marvel.data.quality.csv.reader", return_value=too_many_rows()):
            with self.assertRaises(DataError):
                read_rows(b"synthetic", "csv")
