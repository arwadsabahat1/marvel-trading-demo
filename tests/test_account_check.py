from copy import deepcopy
from argparse import Namespace
from decimal import localcontext
from email.message import Message
from hashlib import sha256
import hmac
import http.client
import json
import getpass
import warnings
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from marvel.errors import MarvelError
from marvel.research.account_check import (AccountEvidenceStore, ENDPOINTS, MAX_BYTES, MUTATIONS,
    PERMISSION_FLAGS, VARIABLES, _Client, _NoRedirect, _ReadFailure, collect, commission,
    precision, summary, undiscounted_taker_rates)
from marvel.risk.model import canonical
from tests import test_cli as command_fixtures
from tests.helpers import WorkspaceCase

KEY, SECRET = "TESTKEYSENTINEL" * 4, "TESTSECRETSENTINEL" * 4
ENV = dict(zip(VARIABLES, (KEY, SECRET)))


def fixtures():
    rights = {name: False for name in PERMISSION_FLAGS}
    rights.update(enableReading=True, createTime=1700000000000)
    rates = {"symbol": "BTCUSDT", "standardCommission": {"maker": "0.001", "taker": "0.001", "buyer": "0", "seller": "0"},
             "taxCommission": {name: "0" for name in ("maker", "taker", "buyer", "seller")},
             "specialCommission": {name: "0" for name in ("maker", "taker", "buyer", "seller")},
             "discount": {"enabledForAccount": False, "enabledForSymbol": False, "discountAsset": "BNB", "discount": "0.25"}}
    return {"time": {"serverTime": 1700000000000}, "permissions": rights, "commissions": rates,
            "precision": [{"asset": "BTC", "fraction": 8}, {"asset": "USDT", "fraction": 8}, {"asset": "BNB", "fraction": 8}]}


class Response:
    status = 200

    def __init__(self, request, raw):
        self.url, self.raw = request.full_url, raw
        self.headers = Message()
        self.headers["Content-Type"] = "application/json; charset=utf-8"
        self.read_sizes = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def geturl(self):
        return self.url

    def read(self, size):
        self.read_sizes.append(size)
        return self.raw[:size]


class Opener:
    def __init__(self, values=None, *, bad_at=None, failure=None, raw=None, modify_response=None):
        self.values = fixtures() if values is None else values
        self.requests, self.responses = [], []
        self.bad_at, self.failure, self.raw = bad_at, failure, raw
        self.modify_response = modify_response

    def open(self, request, *, timeout):
        self.requests.append((request, timeout))
        endpoint = next(name for name, path in ENDPOINTS.items() if urlsplit(request.full_url).path == path)
        if endpoint == self.bad_at and self.failure is not None:
            raise self.failure
        raw = self.raw if endpoint == self.bad_at and self.raw is not None else json.dumps(self.values[endpoint]).encode("ascii")
        response = Response(request, raw)
        if endpoint == self.bad_at and self.modify_response is not None:
            self.modify_response(response)
        self.responses.append(response)
        return response


def observed(opener=None):
    return collect(allow_network=True, environ=ENV, opener=Opener() if opener is None else opener, monotonic=lambda: 10)


class AccountReadTests(TestCase):
    def test_no_opt_in_reads_neither_credentials_nor_network(self):
        class ForbiddenEnvironment:
            def get(self, name):
                raise AssertionError("Credential access without opt-in")
        opener = Opener()
        result = collect(environ=ForbiddenEnvironment(), opener=opener)
        self.assertEqual(result["observation_blockers"], ["NETWORK_OPT_IN_REQUIRED"])
        self.assertEqual(opener.requests, [])

    def test_missing_or_invalid_credentials_never_make_a_request(self):
        for environment, reason in (({}, "MISSING"), ({VARIABLES[0]: KEY}, "MISSING"),
                                    ({**ENV, VARIABLES[1]: "bad\nsecret"}, "INVALID"),
                                    ({**ENV, VARIABLES[0]: []}, "MISSING"),
                                    ({**ENV, VARIABLES[1]: True}, "INVALID")):
            opener = Opener()
            result = collect(allow_network=True, environ=environment, opener=opener)
            self.assertEqual(result["observation_blockers"], ["READ_ONLY_CREDENTIALS_" + reason])
            self.assertEqual(opener.requests, [])

    def test_only_purpose_named_environment_values_are_read(self):
        names = []
        class Environment:
            def get(self, name):
                names.append(name)
                return ENV[name]
        result = collect(allow_network=True, environ=Environment(), opener=Opener(), monotonic=lambda: 10)
        self.assertEqual(result["observation_status"], "complete")
        self.assertEqual(names, list(VARIABLES))

    def test_complete_observation_uses_four_ordered_gets_and_never_qualifies_trading(self):
        opener = Opener()
        result = observed(opener)
        self.assertEqual(result["observation_status"], "complete")
        self.assertEqual(result["network_requests_attempted"], 4)
        self.assertEqual([urlsplit(req.full_url).path for req, _ in opener.requests], list(ENDPOINTS.values()))
        for request, timeout in opener.requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(urlsplit(request.full_url).netloc, "api.binance.com")
            self.assertEqual(timeout, 10)
        self.assertFalse(result["financial_qualification_passed"])
        self.assertFalse(result["live_execution_available"])
        self.assertEqual(result["phase5_status"], "blocked")
        self.assertEqual(result["precision"], [{"asset": "BTC", "fraction": 8}, {"asset": "USDT", "fraction": 8}])

    def test_signatures_match_the_transmitted_query_and_key_is_only_in_private_headers(self):
        opener = Opener()
        observed(opener)
        public = opener.requests[0][0]
        self.assertEqual(urlsplit(public.full_url).query, "")
        self.assertIsNone(public.get_header("X-mbx-apikey"))
        for request, _ in opener.requests[1:]:
            unsigned, signature = urlsplit(request.full_url).query.rsplit("&signature=", 1)
            self.assertEqual(signature, hmac.new(SECRET.encode("ascii"), unsigned.encode("ascii"), "sha256").hexdigest())
            self.assertEqual(request.get_header("X-mbx-apikey"), KEY)
            self.assertNotIn(KEY, request.full_url)
            query = parse_qs(unsigned)
            self.assertEqual(query["timestamp"], ["1700000000000"])
            self.assertEqual(query["recvWindow"], ["5000"])
        self.assertEqual(parse_qs(urlsplit(opener.requests[2][0].full_url).query)["symbol"], ["BTCUSDT"])

    def test_each_mutating_permission_blocks_further_private_reads(self):
        for flag in MUTATIONS | {"enableReading"}:
            values = fixtures()
            values["permissions"][flag] = flag != "enableReading"
            opener = Opener(values)
            result = observed(opener)
            self.assertEqual(result["observation_blockers"], ["API_PERMISSION_SCOPE_REJECTED"])
            self.assertEqual(result["network_requests_attempted"], 2)
            self.assertIsNone(result["commissions"])

    def test_unknown_missing_or_non_boolean_permission_flags_fail_closed(self):
        for change in ("unknown", "missing", "boolean"):
            values = fixtures()
            if change == "unknown":
                values["permissions"]["enableNewTrading"] = True
            elif change == "missing":
                del values["permissions"]["enableWithdrawals"]
            else:
                values["permissions"]["enableWithdrawals"] = 0
            result = observed(Opener(values))
            self.assertEqual(result["observation_blockers"], ["ACCOUNT_RESPONSE_INVALID"])
            self.assertEqual(result["network_requests_attempted"], 2)

    def test_taker_calculation_includes_side_tax_and_special_components_without_bnb_discount(self):
        rates = fixtures()["commissions"]
        rates["standardCommission"].update(taker="0.001", buyer="0.0001", seller="0.0002")
        rates["taxCommission"].update(taker="0.00001", buyer="0.00002", seller="0.00003")
        rates["specialCommission"].update(taker="0.00004", buyer="0.00005", seller="0.00006")
        rates["discount"].update(enabledForAccount=True, enabledForSymbol=True)
        self.assertEqual(undiscounted_taker_rates(rates), {"buy": "0.00122", "sell": "0.00134"})
        with localcontext() as context:
            context.prec = 3
            self.assertEqual(undiscounted_taker_rates(rates), {"buy": "0.00122", "sell": "0.00134"})

    def test_fee_parser_rejects_wrong_pair_missing_components_and_invalid_rates(self):
        for change in ("pair", "missing", "list", "nan", "negative", "exponent", "boolean", "large", "unknown", "discount"):
            rates = fixtures()["commissions"]
            if change == "pair":
                rates["symbol"] = "ETHUSDT"
            elif change == "missing":
                del rates["taxCommission"]
            elif change == "unknown":
                rates["secret"] = SECRET
            elif change == "discount":
                rates["discount"]["enabledForAccount"] = 1
            else:
                rates["standardCommission"]["taker"] = {"list": [], "nan": "NaN", "negative": "-0.1",
                    "exponent": "1e-3", "boolean": True, "large": "1.01"}[change]
            with self.subTest(change=change), self.assertRaises(_ReadFailure):
                commission(rates)

    def test_precision_parser_requires_both_assets_and_rejects_duplicates_and_boolean_fraction(self):
        for values in ([{"asset": "BTC", "fraction": 8}],
                       [{"asset": "BTC", "fraction": 8}, {"asset": "BTC", "fraction": 8}],
                       [{"asset": "BTC", "fraction": True}, {"asset": "USDT", "fraction": 8}],
                       [{"asset": "BTC", "fraction": 19}, {"asset": "USDT", "fraction": 8}], []):
            with self.assertRaises(_ReadFailure):
                precision(values)

    def test_precision_required_assets_are_not_rejected_for_unrelated_asset_names(self):
        values = fixtures()["precision"]
        values.extend([{"asset": "\u5e01\u5b89\u4eba\u751f", "fraction": 8}, {"asset": "legacy_coin", "fraction": -1}])
        self.assertEqual(precision(values), [{"asset": "BTC", "fraction": 8}, {"asset": "USDT", "fraction": 8}])

    def test_unrelated_assets_do_not_relax_strict_required_asset_validation(self):
        unrelated = {"asset": "unrelated_token", "fraction": -1, "future_field": {"ignored": True}}
        for required, reason in (({"asset": "BTC", "fraction": "8"}, "INVALID_REQUIRED_FRACTION"),
                                 ({"asset": "BTC", "fraction": True}, "INVALID_REQUIRED_FRACTION"),
                                 ({"asset": "BTC", "fraction": -1}, "INVALID_REQUIRED_FRACTION"),
                                 ({"asset": "BTC", "fraction": 19}, "INVALID_REQUIRED_FRACTION"),
                                 ({"asset": "BTC", "fraction": 8, "new": True}, "INVALID_REQUIRED_ASSET")):
            with self.subTest(reason=reason), self.assertRaises(_ReadFailure) as caught:
                precision([unrelated, required, {"asset": "USDT", "fraction": 8}])
            self.assertEqual(caught.exception.reason, reason)
        for bad in ([None], [{"fraction": 8}], [{"asset": []}], [{"asset": ""}], [{"asset": "a" * 257}]):
            with self.assertRaises(_ReadFailure) as caught:
                precision(bad)
            self.assertEqual(caught.exception.reason, "INVALID_ASSET_ROW")

    def test_precision_failure_retains_fee_evidence_and_reports_fixed_diagnostics(self):
        values = fixtures()
        values["precision"] = [{"asset": "BTC", "fraction": "8"}, {"asset": "USDT", "fraction": 8}]
        result = observed(Opener(values))
        detail = result["failure_detail"]
        self.assertEqual(detail["endpoint"], "precision")
        self.assertEqual(detail["reason"], "INVALID_REQUIRED_FRACTION")
        self.assertEqual(detail["http_status"], 200)
        self.assertEqual(detail["shape_hint"], "fraction_string")
        self.assertEqual(detail["response_sha256"], sha256(json.dumps(values["precision"]).encode("ascii")).hexdigest())
        self.assertIsNotNone(result["commissions"])
        self.assertIsNone(result["precision"])
        self.assertEqual(summary(result)["failure_detail"], detail)

    def test_provider_error_diagnostics_retain_only_code_status_and_hash(self):
        raw = json.dumps({"code": -2015, "msg": KEY + SECRET}).encode("ascii")
        result = observed(Opener(bad_at="precision", raw=raw))
        self.assertEqual(result["failure_detail"], {"endpoint": "precision", "reason": "PROVIDER_ERROR_RESPONSE",
            "http_status": 200, "provider_error_code": -2015, "response_sha256": sha256(raw).hexdigest(), "shape_hint": "root_object"})
        for secret in (KEY, SECRET):
            self.assertNotIn(secret, canonical(result))
        failed = observed(Opener(bad_at="precision", failure=HTTPError("https://example.invalid/" + SECRET, 403, KEY, {}, None)))
        self.assertEqual(failed["failure_detail"], {"endpoint": "precision", "reason": "HTTP_ERROR",
            "http_status": 403, "provider_error_code": None, "response_sha256": None, "shape_hint": None})

    def test_non_json_content_and_invalid_json_are_distinguishable_without_raw_bodies(self):
        for opener, reason in ((Opener(bad_at="precision", raw=b'{"secret":"' + SECRET.encode("ascii")), "INVALID_JSON"),
            (Opener(bad_at="precision", modify_response=lambda response: response.headers.replace_header("Content-Type", "text/html")), "UNEXPECTED_CONTENT_TYPE"),
            (Opener(bad_at="precision", raw=b" " * (MAX_BYTES + 1)), "RESPONSE_TOO_LARGE")):
            result = observed(opener)
            self.assertEqual(result["failure_detail"]["reason"], reason)
            self.assertNotIn(SECRET, canonical(result))

    def test_http_rate_limit_authentication_timeout_and_incomplete_read_are_not_retried_or_echoed(self):
        for error in (HTTPError("https://example.invalid/" + SECRET, 429, KEY, {}, None),
                      HTTPError("https://example.invalid/" + SECRET, 401, KEY, {}, None),
                      URLError(SECRET), TimeoutError(SECRET), http.client.IncompleteRead(SECRET.encode("ascii"))):
            opener = Opener(bad_at="commissions", failure=error)
            result = observed(opener)
            self.assertEqual(result["observation_blockers"], ["ACCOUNT_READ_FAILED"])
            self.assertEqual(result["network_requests_attempted"], 3)
            self.assertNotIn(KEY, canonical(result))
            self.assertNotIn(SECRET, canonical(result))

    def test_duplicate_keys_error_bodies_oversize_and_wrong_content_type_are_redacted(self):
        cases = [Opener(bad_at="commissions", raw=b'{"symbol":"BTCUSDT","symbol":"SECRET"}'),
                 Opener(bad_at="commissions", raw=json.dumps({"msg": SECRET, "apiKey": KEY}).encode("ascii")),
                 Opener(bad_at="commissions", raw=b" " * (MAX_BYTES + 1)),
                 Opener(bad_at="commissions", modify_response=lambda response: response.headers.replace_header("Content-Type", "text/html"))]
        for opener in cases:
            result = observed(opener)
            self.assertEqual(result["observation_blockers"], ["ACCOUNT_RESPONSE_INVALID"])
            self.assertNotIn(KEY, canonical(result))
            self.assertNotIn(SECRET, canonical(result))
            self.assertTrue(all(size == MAX_BYTES + 1 for response in opener.responses for size in response.read_sizes))

    def test_redirect_is_never_followed_and_wrong_response_url_is_rejected(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, None, None, "https://evil.invalid"))
        opener = Opener(bad_at="permissions", modify_response=lambda response: setattr(response, "url", "https://evil.invalid"))
        result = observed(opener)
        self.assertEqual(result["observation_blockers"], ["ACCOUNT_RESPONSE_INVALID"])
        self.assertEqual(result["network_requests_attempted"], 2)

    def test_client_cannot_request_arbitrary_endpoints_or_skip_permission_check(self):
        client = _Client(KEY, SECRET, opener=Opener(), monotonic=lambda: 10)
        for endpoint in ("acceptQuote", "https://evil.invalid", "commissions", []):
            with self.assertRaises(_ReadFailure):
                client.get(endpoint)
        self.assertEqual(client.attempts, 0)

    def test_signing_clock_regression_or_excessive_capture_duration_stops_reads(self):
        for clock_values in ([10, 9], [10, 71]):
            clocks = iter(clock_values)
            result = collect(allow_network=True, environ=ENV, opener=Opener(), monotonic=lambda: next(clocks))
            self.assertEqual(result["network_requests_attempted"], 1)
            self.assertEqual(result["observation_blockers"], ["ACCOUNT_RESPONSE_INVALID"])

    def test_failed_precision_retains_the_observed_fee_metadata_without_claiming_full_qualification(self):
        result = observed(Opener(bad_at="precision", failure=URLError(SECRET)))
        report = summary(result)
        self.assertTrue(report["actual_account_commission_rates_observed"])
        self.assertFalse(report["convert_precision_observed"])
        self.assertFalse(report["financial_qualification_passed"])
        self.assertFalse(report["bnb_discount_applied"])
        self.assertFalse(report["convert_quote_access_or_costs_verified"])
        self.assertTrue(report["remaining_phase5_blockers"])


class AccountEvidenceTests(WorkspaceCase):
    def test_schema_one_records_remain_readable_without_invented_diagnostics(self):
        document = observed(Opener(bad_at="precision", failure=URLError(SECRET)))
        document["schema_version"] = 1
        del document["failure_detail"]
        store = AccountEvidenceStore(self.root)
        identity = store.save(document)
        self.assertEqual(store.load(identity), document)
        self.assertIsNone(summary(store.load(identity))["failure_detail"])

    def test_diagnostic_capture_preserves_unrelated_assets_only_in_the_response_hash(self):
        values = fixtures()
        values["precision"].append({"asset": "\u5e01\u5b89\u4eba\u751f", "fraction": -1, "extra": SECRET})
        result = observed(Opener(values))
        store = AccountEvidenceStore(self.root)
        identity = store.save(result)
        self.assertEqual(store.load(identity)["observation_status"], "complete")
        raw = (store.root / identity / "record.json").read_bytes()
        self.assertNotIn(SECRET.encode("ascii"), raw)
        self.assertEqual(store.load(identity)["precision"], [{"asset": "BTC", "fraction": 8}, {"asset": "USDT", "fraction": 8}])

    def test_diagnostics_for_scope_and_pre_request_clock_failures_are_replayable(self):
        values = fixtures()
        values["permissions"]["enableWithdrawals"] = True
        clocks = iter([10, 9])
        documents = [observed(Opener(values)), collect(allow_network=True, environ=ENV, opener=Opener(), monotonic=lambda: next(clocks))]
        store = AccountEvidenceStore(self.root)
        for document in documents:
            self.assertEqual(store.load(store.save(document)), document)

    def test_forged_or_secret_bearing_diagnostics_are_rejected(self):
        original = observed(Opener(bad_at="precision", failure=URLError(SECRET)))
        for name, value in (("reason", SECRET), ("reason", []), ("endpoint", "commissions"),
                            ("http_status", True), ("provider_error_code", SECRET), ("response_sha256", "invalid"),
                            ("message", SECRET), ("shape_hint", SECRET), ("shape_hint", [])):
            document = deepcopy(original)
            document["failure_detail"][name] = value
            with self.subTest(name=name), self.assertRaises(MarvelError):
                AccountEvidenceStore(self.root).save(document)
    def test_sanitized_evidence_is_content_addressed_reloaded_and_never_contains_credentials(self):
        store = AccountEvidenceStore(self.root)
        document = observed()
        identity = store.save(document)
        self.assertEqual(store.load(identity), document)
        self.assertEqual(store.save(document), identity)
        raw = (store.root / identity / "record.json").read_bytes()
        self.assertEqual(identity, sha256(raw).hexdigest())
        self.assertNotIn(KEY.encode("ascii"), raw)
        self.assertNotIn(SECRET.encode("ascii"), raw)
        self.assertNotIn(b"signature", raw)
        self.assertFalse(self.config().database.exists())

    def test_partial_and_missing_credential_attempts_are_valid_evidence_of_a_blocker(self):
        store = AccountEvidenceStore(self.root)
        for document in (collect(allow_network=True, environ={}),
                         observed(Opener(bad_at="commissions", failure=URLError(SECRET))),
                         observed(Opener(bad_at="time", failure=URLError(SECRET)))):
            self.assertEqual(store.load(store.save(document)), document)

    def test_tampered_record_is_rejected_and_never_repaired(self):
        store = AccountEvidenceStore(self.root)
        document = observed()
        identity = store.save(document)
        path = store.root / identity / "record.json"
        changed = path.read_bytes() + b" "
        path.write_bytes(changed)
        with self.assertRaises(MarvelError):
            store.load(identity)
        with self.assertRaises(MarvelError):
            store.save(document)
        self.assertEqual(path.read_bytes(), changed)

    def test_false_qualification_impossible_sequences_and_bad_types_are_rejected(self):
        original = observed()
        for change in ("qualification", "sequence", "counter", "boolean", "status", "permission", "opt_in", "schema", "secret"):
            document = deepcopy(original)
            if change == "qualification":
                document["financial_qualification_passed"] = True
            elif change == "sequence":
                document["receipts"].reverse()
            elif change == "counter":
                document["network_requests_attempted"] = 1
            elif change == "boolean":
                document["network_requests_attempted"] = True
            elif change == "status":
                document["observation_status"] = []
            elif change == "permission":
                document["permissions"]["enableWithdrawals"] = True
            elif change == "opt_in":
                document["network_opt_in"] = False
            elif change == "schema":
                document["schema_version"] = True
            else:
                document["apiKey"] = KEY
            with self.subTest(change=change), self.assertRaises(MarvelError):
                AccountEvidenceStore(self.root).save(document)

    def test_invalid_record_identifier_never_traverses_outside_the_evidence_directory(self):
        for identity in ("../escape", "a" * 63, [], "a" * 64):
            with self.assertRaises(MarvelError):
                AccountEvidenceStore(self.root).load(identity)


class AccountCheckCommands(WorkspaceCase):
    run_cli = command_fixtures.CommandTests.run_cli
    payload = command_fixtures.CommandTests.payload

    def test_offline_check_never_initializes_account_or_private_evidence(self):
        result = self.payload("research", "account-check")
        self.assertEqual(result["account_research_check"]["observation_blockers"], ["NETWORK_OPT_IN_REQUIRED"])
        self.assertIsNone(result["account_research_check"]["record_id"])
        self.assertFalse(self.config().database.exists())
        self.assertFalse((self.root / "var/research-account-checks").exists())

    def test_account_check_preserves_manual_halt_account_and_financial_configuration(self):
        self.payload("init")
        self.payload("pause", "--reason", "operator")
        paths = [self.config().database, self.root / "config/paper.json", self.root / "config/btc-paper-risk.json"]
        before = [path.read_bytes() for path in paths]
        self.payload("research", "account-check")
        store = AccountEvidenceStore(self.root)
        identity = store.save(observed())
        result = self.payload("research", "account-inspect", "--record", identity)
        self.assertFalse(result["account_research_check"]["financial_qualification_passed"])
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_credentials_urls_execution_and_override_arguments_are_not_accepted(self):
        for flag in ("--api-key", "--secret", "--url", "--live", "--force", "--max-loss"):
            process = self.run_cli("research", "account-check", flag, SECRET)
            self.assertEqual(process.returncode, 2)
            self.assertNotIn(SECRET, process.stdout + process.stderr)
            self.assertNotIn("Traceback", process.stderr)
        self.assertFalse(self.config().database.exists())

    def test_hidden_prompt_requires_explicit_opt_in_and_an_interactive_terminal(self):
        for flags in (("--prompt-credentials",), ("--prompt-credentials", "--allow-network")):
            process = self.run_cli("research", "account-check", *flags)
            self.assertEqual(process.returncode, 2)
            self.assertNotIn("Traceback", process.stderr)
        self.assertFalse((self.root / "var/research-account-checks").exists())

    def test_prompt_never_falls_back_to_echoed_input_or_leaks_terminal_errors(self):
        from marvel.research.commands import run
        args = Namespace(research_command="account-check", allow_network=True, prompt_credentials=True)
        for failure in (EOFError(SECRET), KeyboardInterrupt(), getpass.GetPassWarning(SECRET)):
            with patch("sys.stdin.isatty", return_value=True), patch("sys.stderr.isatty", return_value=True), \
                    patch("getpass.getpass", side_effect=failure), patch("marvel.research.account_check.collect") as collector:
                with self.assertRaises(MarvelError) as caught:
                    run(args, self.config())
                self.assertNotIn(SECRET, str(caught.exception))
                collector.assert_not_called()
        def unsupported_hidden_input(label):
            warnings.warn("Hidden input unavailable", getpass.GetPassWarning)
            self.fail("Fallback would echo credentials")
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stderr.isatty", return_value=True), \
                patch("getpass.getpass", side_effect=unsupported_hidden_input), self.assertRaises(MarvelError):
            run(args, self.config())

    def test_hidden_credentials_are_used_only_in_memory_and_never_in_saved_evidence(self):
        from marvel.research.commands import run
        args = Namespace(research_command="account-check", allow_network=True, prompt_credentials=True)
        opener = Opener()
        with patch("sys.stdin.isatty", return_value=True), patch("sys.stderr.isatty", return_value=True), \
                patch("getpass.getpass", side_effect=(KEY, SECRET)), \
                patch("marvel.research.account_check.collect", side_effect=lambda **kwargs: collect(
                    **kwargs, opener=opener, monotonic=lambda: 10)):
            result = run(args, self.config())
        identity = result["account_research_check"]["record_id"]
        raw = (self.root / "var/research-account-checks" / identity / "record.json").read_bytes()
        self.assertEqual(len(opener.requests), 4)
        for value in (KEY, SECRET):
            self.assertNotIn(value, canonical(result))
            self.assertNotIn(value.encode("ascii"), raw)
        self.assertFalse(self.config().database.exists())
