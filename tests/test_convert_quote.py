from argparse import Namespace
from decimal import localcontext
from hashlib import sha256
import getpass
import hmac
import json
import os
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from marvel.errors import MarvelError
from marvel.research.account_check import ENDPOINTS, MAX_BYTES, MUTATIONS, _ReadFailure, collect as read_collect
from marvel.research.convert_quote import DEFAULT_AMOUNT, QUOTE_PATH, VARIABLES, _QuoteClient, amount, collect
from marvel.risk.model import canonical, fingerprint
from tests import test_cli as command_fixtures
from tests.helpers import WorkspaceCase
from tests.test_account_check import KEY, SECRET, Opener, Response, fixtures

ENV = dict(zip(VARIABLES, (KEY, SECRET)))
T = 1700000000000


def values():
    result = fixtures()
    result["permissions"].update(enableSpotAndMarginTrading=True, ipRestrict=True)
    return result


def quote():
    return {"quoteId": "OFFER_SENTINEL_571", "ratio": "79000", "inverseRatio": "0.000012658",
            "fromAmount": DEFAULT_AMOUNT, "toAmount": "0.7821", "validTimestamp": T + 10000}


class QuoteOpener(Opener):
    def __init__(self, *, response=None, raw=None, failure=None, modify=None, metadata=None):
        super().__init__(values() if metadata is None else metadata)
        self.quote_response = quote() if response is None else response
        self.quote_raw, self.quote_failure, self.quote_modify = raw, failure, modify

    def open(self, request, *, timeout):
        if urlsplit(request.full_url).path != QUOTE_PATH:
            return super().open(request, timeout=timeout)
        self.requests.append((request, timeout))
        if self.quote_failure is not None:
            raise self.quote_failure
        response = Response(request, self.quote_raw if self.quote_raw is not None else json.dumps(self.quote_response).encode("ascii"))
        if self.quote_modify is not None:
            self.quote_modify(response)
        self.responses.append(response)
        return response


def observed(opener=None, **kwargs):
    return collect(allow_network=True, acknowledge_trade_permission=True, environ=ENV,
                   opener=QuoteOpener() if opener is None else opener, monotonic=lambda: 10, **kwargs)


class QuoteResearchTests(TestCase):
    def test_both_opt_ins_precede_any_credential_or_transport_access(self):
        class Forbidden:
            def get(self, key):
                raise AssertionError("Unexpected credential access")
        for options, blocker in (({}, "NETWORK_OPT_IN_REQUIRED"),
                                 ({"allow_network": True}, "TRADE_PERMISSION_ACKNOWLEDGEMENT_REQUIRED"),
                                 ({"acknowledge_trade_permission": True}, "NETWORK_OPT_IN_REQUIRED")):
            opener = QuoteOpener()
            result = collect(**options, environ=Forbidden(), opener=opener)
            self.assertEqual(result["blockers"], [blocker])
            self.assertEqual(opener.requests, [])

    def test_only_dedicated_quote_credential_names_are_read(self):
        names = []
        class Environment:
            def get(self, key):
                names.append(key)
                return ENV[key]
        result = collect(allow_network=True, acknowledge_trade_permission=True, environ=Environment(),
                         opener=QuoteOpener(), monotonic=lambda: 10)
        self.assertEqual(result["observation_status"], "complete")
        self.assertEqual(names, list(VARIABLES))

    def test_missing_and_invalid_credentials_never_make_requests(self):
        for env in ({}, {VARIABLES[0]: KEY}, {**ENV, VARIABLES[1]: "secret\n"}, {**ENV, VARIABLES[0]: True}):
            opener = QuoteOpener()
            result = collect(allow_network=True, acknowledge_trade_permission=True, environ=env, opener=opener)
            self.assertIn(result["blockers"][0], {"QUOTE_CREDENTIALS_MISSING", "QUOTE_CREDENTIALS_INVALID"})
            self.assertEqual(opener.requests, [])

    def test_four_gets_and_one_quote_post_have_fixed_host_path_wallet_and_amount(self):
        opener = QuoteOpener()
        result = observed(opener)
        self.assertEqual(result["observation_status"], "complete")
        self.assertEqual(result["network_requests_attempted"], 5)
        self.assertEqual([urlsplit(r.full_url).path for r, _ in opener.requests], [*ENDPOINTS.values(), QUOTE_PATH])
        self.assertEqual([r.get_method() for r, _ in opener.requests], ["GET"] * 4 + ["POST"])
        for request, timeout in opener.requests:
            self.assertEqual(urlsplit(request.full_url).netloc, "api.binance.com")
            self.assertEqual(timeout, 10)
        post = opener.requests[-1][0]
        self.assertEqual(urlsplit(post.full_url).query, "")
        data = parse_qs(post.data.decode("ascii"))
        self.assertEqual(set(data), {"fromAsset", "toAsset", "fromAmount", "walletType", "validTime", "timestamp", "recvWindow", "signature"})
        for key, value in {"fromAsset": "BTC", "toAsset": "USDT", "walletType": "SPOT", "validTime": "10s", "fromAmount": DEFAULT_AMOUNT}.items():
            self.assertEqual(data[key], [value])
        self.assertEqual(data["timestamp"], [str(T)])
        self.assertEqual(data["recvWindow"], ["5000"])

    def test_post_signature_matches_exact_body_and_no_key_enters_url(self):
        opener = QuoteOpener()
        observed(opener)
        post = opener.requests[-1][0]
        unsigned, signature = post.data.decode("ascii").rsplit("&signature=", 1)
        self.assertEqual(signature, hmac.new(SECRET.encode("ascii"), unsigned.encode("ascii"), "sha256").hexdigest())
        self.assertEqual(post.get_header("X-mbx-apikey"), KEY)
        self.assertNotIn(KEY, post.full_url)
        self.assertNotIn(SECRET, post.full_url)

    def test_permissions_require_reading_spot_trading_and_ip_restriction_before_later_reads(self):
        for name in ("enableReading", "enableSpotAndMarginTrading", "ipRestrict"):
            metadata = values()
            metadata["permissions"][name] = False
            opener = QuoteOpener(metadata=metadata)
            result = observed(opener)
            self.assertEqual(result["blockers"], ["API_PERMISSION_SCOPE_REJECTED"])
            self.assertEqual(len(opener.requests), 2)

    def test_all_other_mutating_permissions_are_rejected_before_quote(self):
        for name in MUTATIONS - {"enableSpotAndMarginTrading"}:
            metadata = values()
            metadata["permissions"][name] = True
            opener = QuoteOpener(metadata=metadata)
            self.assertEqual(observed(opener)["blockers"], ["API_PERMISSION_SCOPE_REJECTED"])
            self.assertEqual(len(opener.requests), 2)

    def test_original_read_only_collector_still_rejects_trade_enabled_key(self):
        from tests.test_account_check import ENV as READ_ENV
        opener = QuoteOpener()
        result = read_collect(allow_network=True, environ=READ_ENV, opener=opener, monotonic=lambda: 10)
        self.assertEqual(result["observation_blockers"], ["API_PERMISSION_SCOPE_REJECTED"])
        self.assertEqual(len(opener.requests), 2)

    def test_quote_cannot_precede_preflight_or_repeat_a_post(self):
        opener = QuoteOpener()
        client = _QuoteClient(KEY, SECRET, opener=opener, monotonic=lambda: 10)
        with self.assertRaises(_ReadFailure):
            client.request_quote(DEFAULT_AMOUNT)
        self.assertEqual(opener.requests, [])
        for endpoint in ENDPOINTS:
            client.get(endpoint)
        client.request_quote(DEFAULT_AMOUNT)
        with self.assertRaises(_ReadFailure):
            client.request_quote(DEFAULT_AMOUNT)
        self.assertEqual(len(opener.requests), 5)

    def test_request_amount_is_bounded_precise_and_checked_before_network(self):
        for invalid in ("0", "-1", "0.001", "0.00010001", "1e-5", "NaN", "0.000000001", True):
            opener = QuoteOpener()
            with self.assertRaises(MarvelError):
                observed(opener, from_btc=invalid)
            self.assertEqual(opener.requests, [])
        self.assertEqual(amount("0.00010000"), "0.0001")

    def test_current_key_precision_can_block_request_without_posting(self):
        metadata = values()
        metadata["precision"][0]["fraction"] = 5
        opener = QuoteOpener(metadata=metadata)
        result = observed(opener)
        self.assertEqual(result["observation_status"], "partial")
        self.assertEqual(len(opener.requests), 4)
        self.assertIsNone(result["quote"])

    def test_only_hashed_offer_identity_and_allowlisted_fields_are_returned(self):
        result = observed()
        for value in (KEY, SECRET, quote()["quoteId"]):
            self.assertNotIn(value, canonical(result))
        self.assertEqual(result["quote"]["provider_offer_id_sha256"], sha256(quote()["quoteId"].encode("ascii")).hexdigest())
        self.assertEqual(result["observation_id"], fingerprint({k: v for k, v in result.items() if k != "observation_id"}))
        for key in ("entries_allowed", "live_execution_available", "actual_fill_fees_qualified", "actual_complete_exit_qualified",
                    "historical_costs_qualified", "phase5_historical_gate_passed", "quote_acceptance_available", "owner_paper_account_used"):
            self.assertFalse(result[key])
        self.assertEqual(result["real_orders_or_conversions"], 0)

    def test_no_offer_id_is_indicative_only_without_inventing_funds_or_eligibility(self):
        response = quote()
        del response["quoteId"]
        del response["validTimestamp"]
        result = observed(QuoteOpener(response=response))
        self.assertEqual(result["observation_status"], "complete")
        self.assertEqual(result["blockers"], ["INDICATIVE_PRICE_ONLY_NO_OFFER_ID"])
        self.assertFalse(result["quote"]["offer_unexpired_at_receipt_estimate"])

    def test_expired_offer_is_kept_as_observation_without_claiming_usable_access(self):
        response = quote()
        response["validTimestamp"] = T
        result = observed(QuoteOpener(response=response))
        self.assertEqual(result["blockers"], ["OFFER_EXPIRED_AT_RECEIPT_ESTIMATE"])
        self.assertFalse(result["quote"]["offer_unexpired_at_receipt_estimate"])

    def test_wrong_amount_precision_and_unknown_fields_are_not_accepted(self):
        for key, value in (("fromAmount", "0.00000989"), ("toAmount", "0.780000001"), ("toAmount", "NaN"),
                           ("ratio", True), ("quoteId", "bad url !"), ("validTimestamp", True),
                           ("validTimestamp", T + 20001), ("apiKey", KEY)):
            response = quote()
            response[key] = value
            result = observed(QuoteOpener(response=response))
            self.assertEqual(result["observation_status"], "partial")
            self.assertIsNone(result["quote"])
            self.assertNotIn(KEY, canonical(result))

    def test_provider_error_body_never_leaks_and_no_retry_follows(self):
        opener = QuoteOpener(response={"code": -2010, "msg": SECRET})
        result = observed(opener)
        self.assertEqual(result["failure_detail"]["provider_error_code"], -2010)
        self.assertNotIn(SECRET, canonical(result))
        self.assertEqual(len(opener.requests), 5)

    def test_invalid_signing_clock_cannot_dispatch_a_quote(self):
        opener = QuoteOpener()
        client = _QuoteClient(KEY, SECRET, opener=opener, monotonic=lambda: 10)
        for endpoint in ENDPOINTS:
            client.get(endpoint)
        for value in (9, 71):
            client.monotonic = lambda: value
            with self.assertRaises(_ReadFailure):
                client.request_quote(DEFAULT_AMOUNT)
        self.assertEqual(len(opener.requests), 4)

    def test_transport_and_http_failures_are_bounded_sanitized_and_never_retried(self):
        for failure in (URLError(SECRET), HTTPError("https://secret/" + KEY, 403, SECRET, {}, None), RuntimeError(SECRET)):
            opener = QuoteOpener(failure=failure)
            result = observed(opener)
            self.assertEqual(result["observation_status"], "partial")
            self.assertNotIn(SECRET, canonical(result))
            self.assertNotIn(KEY, canonical(result))
            self.assertEqual(len(opener.requests), 5)

    def test_redirect_content_type_status_oversize_and_duplicate_json_are_rejected(self):
        modifiers = (lambda r: setattr(r, "url", "https://other.invalid/"),
                     lambda r: r.headers.replace_header("Content-Type", "text/html"), lambda r: setattr(r, "status", 201))
        for modify in modifiers:
            self.assertIsNone(observed(QuoteOpener(modify=modify))["quote"])
        for raw in (b" " * (MAX_BYTES + 1), b'{"ratio":"1","ratio":"2"}', b'{"private":"' + SECRET.encode() + b'"}'):
            opener = QuoteOpener(raw=raw)
            result = observed(opener)
            self.assertIsNone(result["quote"])
            self.assertNotIn(SECRET, canonical(result))
            self.assertEqual(opener.responses[-1].read_sizes, [MAX_BYTES + 1])

    def test_decimal_context_does_not_change_amount_or_quote_arithmetic(self):
        first = observed()
        with localcontext() as context:
            context.prec = 3
            second = observed()
            self.assertEqual(amount(DEFAULT_AMOUNT), DEFAULT_AMOUNT)
        first.pop("observed_at_utc")
        second.pop("observed_at_utc")
        first.pop("observation_id")
        second.pop("observation_id")
        self.assertEqual(first, second)


class QuoteCommands(WorkspaceCase):
    run_cli = command_fixtures.CommandTests.run_cli
    payload = command_fixtures.CommandTests.payload

    def test_offline_and_acknowledgement_checks_do_not_initialize_account(self):
        result = self.payload("research", "convert-quote")["convert_quote_check"]
        self.assertEqual(result["blockers"], ["NETWORK_OPT_IN_REQUIRED"])
        result = self.payload("research", "convert-quote", "--allow-network")["convert_quote_check"]
        self.assertEqual(result["blockers"], ["TRADE_PERMISSION_ACKNOWLEDGEMENT_REQUIRED"])
        with patch.dict(os.environ, {}, clear=True):
            result = self.payload("research", "convert-quote", "--allow-network", "--acknowledge-trade-permission")["convert_quote_check"]
        self.assertEqual(result["blockers"], ["QUOTE_CREDENTIALS_MISSING"])
        self.assertFalse(self.config().database.exists())
        self.assertFalse((self.root / "var/research-account-checks").exists())

    def test_quote_acceptance_host_wallet_and_secret_arguments_are_rejected(self):
        for flag in ("--accept", "--quote-id", "--url", "--wallet", "--api-key", "--secret", "--live", "--force"):
            result = self.run_cli("research", "convert-quote", flag, SECRET)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(SECRET, result.stdout + result.stderr)

    def test_hidden_prompt_requires_both_opt_ins_and_tty(self):
        for flags in ((), ("--allow-network",), ("--allow-network", "--acknowledge-trade-permission")):
            result = self.run_cli("research", "convert-quote", "--prompt-credentials", *flags)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("Traceback", result.stderr)

    def test_invalid_probe_amount_rejects_before_hidden_credential_input(self):
        from marvel.research.commands import run
        args = Namespace(research_command="convert-quote", from_btc="1", allow_network=True,
                         acknowledge_trade_permission=True, prompt_credentials=True)
        with patch("getpass.getpass") as prompt, self.assertRaises(MarvelError):
            run(args, self.config())
        prompt.assert_not_called()

    def test_prompt_errors_never_echo_or_call_transport(self):
        from marvel.research.commands import run
        args = Namespace(research_command="convert-quote", from_btc=DEFAULT_AMOUNT, allow_network=True,
                         acknowledge_trade_permission=True, prompt_credentials=True)
        for error in (getpass.GetPassWarning(SECRET), EOFError(SECRET), KeyboardInterrupt()):
            with patch("sys.stdin.isatty", return_value=True), patch("sys.stderr.isatty", return_value=True), \
                    patch("getpass.getpass", side_effect=error), patch("marvel.research.convert_quote.collect") as collector:
                with self.assertRaises(MarvelError):
                    run(args, self.config())
                collector.assert_not_called()

    def test_hidden_credentials_use_quote_only_client_without_saving_private_material(self):
        from marvel.research.commands import run
        args = Namespace(research_command="convert-quote", from_btc=DEFAULT_AMOUNT, allow_network=True,
                         acknowledge_trade_permission=True, prompt_credentials=True)
        opener = QuoteOpener()
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stderr.isatty", return_value=True), \
                patch("getpass.getpass", side_effect=(KEY, SECRET)), \
                patch("marvel.research.convert_quote.collect", side_effect=lambda **kwargs: collect(**kwargs, opener=opener, monotonic=lambda: 10)):
            result = run(args, self.config())
        self.assertEqual(len(opener.requests), 5)
        for value in (KEY, SECRET, quote()["quoteId"]):
            self.assertNotIn(value, canonical(result))
        self.assertFalse(self.config().database.exists())

    def test_existing_balance_halt_and_financial_files_remain_byte_identical(self):
        self.payload("init")
        self.payload("pause", "--reason", "operator")
        paths = [self.config().database, self.root / "config/paper.json", self.root / "config/btc-paper-risk.json"]
        before = [p.read_bytes() for p in paths]
        self.payload("research", "convert-quote")
        self.assertEqual([p.read_bytes() for p in paths], before)
