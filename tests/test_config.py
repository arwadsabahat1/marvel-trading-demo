from decimal import Decimal
import json

from marvel.config import load_config
from marvel.errors import ConfigError
from tests.helpers import WorkspaceCase


class ConfigurationTests(WorkspaceCase):
    def rejected(self):
        self.write_config()
        with self.assertRaises(ConfigError):
            self.config()
        self.assertFalse((self.root / "var").exists())

    def test_missing_file_creates_nothing(self):
        self.path.unlink()
        with self.assertRaises(ConfigError):
            self.config()
        self.assertFalse((self.root / "var").exists())

    def test_paper_defaults_and_owner_limits(self):
        config = self.config()
        self.assertEqual(config.starting_equity_usd, Decimal("50"))
        self.assertEqual(config.risk.per_trade_cap_usd, Decimal("1"))
        self.assertEqual(config.risk.daily_loss_cap_usd, Decimal("5"))
        self.assertEqual(config.risk.weekly_loss_fraction, Decimal("0.02"))
        self.assertFalse((self.root / "var").exists())

    def test_live_or_network_flags_are_rejected(self):
        for key, value in [("mode", "live"), ("network_enabled", True), ("network_enabled", 0)]:
            with self.subTest(key=key, value=value):
                original = self.document[key]
                self.document[key] = value
                self.rejected()
                self.document[key] = original

    def test_money_requires_finite_decimal_strings(self):
        for value in [1, 1.0, True, "NaN", "Infinity", "1e0", "-1", "0", " 1", "9" * 100]:
            with self.subTest(value=value):
                self.document["risk"]["per_trade_cap_usd"] = value
                self.rejected()

    def test_each_risk_ceiling_and_starting_balance(self):
        for key, value in {
            "per_trade_cap_usd": "1.01", "daily_loss_cap_usd": "5.01", "weekly_loss_fraction": "0.021",
            "peak_drawdown_fraction": "0.051", "max_notional_fraction": "0.201", "max_positions": 2,
            "losing_streak_limit": 4,
        }.items():
            with self.subTest(key=key):
                original = self.document["risk"][key]
                self.document["risk"][key] = value
                self.rejected()
                self.document["risk"][key] = original
        self.document["starting_equity_usd"] = "50.01"
        self.rejected()

    def test_missing_threshold_and_unknown_fields_fail(self):
        del self.document["data"]["max_quote_age_seconds"]
        self.rejected()
        self.document["data"]["max_quote_age_seconds"] = 10
        self.document["api_key"] = "TEST_SECRET_NEVER_PRINT"
        self.rejected()

    def test_missing_risk_field_fails(self):
        del self.document["risk"]["daily_loss_cap_usd"]
        self.rejected()

    def test_duplicate_json_keys_rejected(self):
        text = json.dumps(self.document).replace('"mode": "paper"', '"mode": "paper", "mode": "paper"')
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaises(ConfigError):
            self.config()

    def test_oversized_and_malformed_json_rejected(self):
        for text in ["{" + "x" * 20_000, "{", "[]", "null"]:
            with self.subTest(size=len(text)):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(ConfigError):
                    self.config()

    def test_bool_integer_and_unapproved_market_rejected(self):
        self.document["risk"]["max_positions"] = True
        self.rejected()
        self.document["risk"]["max_positions"] = 1
        for markets in [["BTC", "BTC"], ["UNAPPROVED"], [], [42]]:
            with self.subTest(markets=markets):
                self.document["markets"] = markets
                self.rejected()

    def test_runtime_paths_cannot_escape_or_overwrite_source(self):
        for value in ["../outside.sqlite3", "var/../../outside.sqlite3", "C:/outside.sqlite3", "/outside.sqlite3", "config/paper.sqlite3", "var\\paper.sqlite3"]:
            with self.subTest(value=value):
                self.document["storage"]["database"] = value
                self.rejected()

    def test_policy_hash_is_canonical_and_changes_with_policy(self):
        first = self.config().fingerprint
        self.document["risk"]["max_notional_fraction"] = "0.2"
        self.document["markets"].reverse()
        self.write_config()
        self.assertEqual(first, self.config().fingerprint)
        self.document["risk"]["per_trade_cap_usd"] = "0.5"
        self.write_config()
        self.assertNotEqual(first, self.config().fingerprint)

    def test_tighter_settings_are_allowed(self):
        self.document["risk"]["per_trade_cap_usd"] = "0.5"
        self.document["risk"]["daily_loss_cap_usd"] = "1"
        self.write_config()
        self.assertEqual(self.config().risk.daily_loss_cap_usd, Decimal("1"))

    def test_policy_hash_preserves_high_precision_values(self):
        original = self.config().fingerprint
        self.document["risk"]["per_trade_cap_usd"] = "0.99999999999999999999999999999999999"
        self.write_config()
        self.assertNotEqual(original, self.config().fingerprint)
