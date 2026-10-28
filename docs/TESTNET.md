# Optional Binance Spot Testnet

The main demo and all tests work offline without credentials. This optional adapter exercises exchange engineering with virtual assets, using a separate local ledger. It has no production mode.

## Generic setup

1. Open the official [Binance Spot Test Network](https://testnet.binance.vision/) and use its supported sign-in flow.
2. Generate a dedicated **HMAC-SHA-256 Testnet** key. The adapter does not support RSA or Ed25519 keys. Enable Testnet reading and the Testnet trading permission needed for a later virtual cycle, if the provider presents permission controls.
3. Keep the paired key and secret private. Use the actual values, never masked dots. No real-money deposit, transfer or production API key is needed.

The provider documents its [virtual assets and API scope](https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/testnet/general-info.md) and [authentication and order semantics](https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/testnet/rest-api.md). Website access and supported features can change; do not infer access to a production account from Testnet access.

## Initialize and inspect

Use your Python 3.12 executable from the repository root:

```text
python -m marvel_testnet init
python -m marvel_testnet status
python -m marvel_testnet public-check --allow-network
python -m marvel_testnet preflight --allow-network --prompt-credentials
```

The last command asks for the key and secret using hidden local terminal prompts. Credentials are not command arguments and are not saved. No credential file or GitHub secret is required. Never paste credentials into an issue, chat or commit.

Preflight is GET-only. It checks time, symbol rules, spread, supported order types, commission rates, available virtual funds and existing orders. It passes a proposed size through the original risk model. Continue only if `virtual_cycle_preflight_passed` is true. A failed feasibility result does not justify increasing the budget or erasing the ledger.

## One explicit virtual cycle

```text
python -m marvel_testnet cycle --allow-network --acknowledge-virtual-orders --prompt-credentials
```

This is a bounded engineering round trip, not a strategy loop. It validates orders, journals entry intent, submits one LIMIT/FOK buy, queries actual Testnet fills, attempts a standalone STOP_LOSS sell, exercises cancellation and a bounded exit, and reconciles the resulting balances. Uncertain acknowledgements are queried using the saved order IDs; buys are not automatically resubmitted.

Only $50 of simulated allocation is tracked, even if the Testnet faucet supplies much larger balances. Its initial exposure ceiling is $10. Existing faucet BTC is a baseline, never an owned strategy position that the adapter may sell. Avoid concurrent manual trading in the same Testnet account.

There are unprotected intervals between entry and protection and during cancellation/replacement. Gaps, fee changes, partial fills and minimum-order rules can leave a holding unresolved. Stop acceptance and a successful round trip do not establish a guaranteed loss ceiling or production readiness.

## Inspect and recover

```text
python -m marvel_testnet status
python -m marvel_testnet reconcile --allow-network --prompt-credentials
python -m marvel_testnet recover --allow-network --acknowledge-virtual-orders --prompt-credentials
```

`reconcile` is GET-only. `recover` can explicitly cancel the adapter's known orders and attempt a bounded sale of its remaining owned BTC; it never buys. Review and financial halts stay persistent. Neither operation resets capital or silently treats a missing order as proof of no fill.

The Testnet can reset balances and history. Missing evidence or unexplained changes require review. Bound source/configuration changes also stop actions under an existing ledger. Preserve the records rather than deleting them to get past a halt.

The adapter supports only its fixed Testnet `/api/v3` routes. It cannot establish production commission behavior, Convert access, dust-cleanup costs, historical returns or lawful availability of another service.
