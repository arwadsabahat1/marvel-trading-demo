# Development phases

This demo implements engineering foundations through strategy research and an optional virtual-exchange adapter. It does not represent a finished profitable bot.

Version 0.6.0 adds [integrated operational code](OPERATIONS.md): historical/holdout qualification, prospective paper execution, bounded online selection, production Spot transport, durable execution/recovery and supervised worker templates. The table below retains the original foundation milestones; operational implementation does not establish financial success or deployed uptime.

| Phase | Deliverable | Required check | Demo status |
| --- | --- | --- | --- |
| 0: Scope | Define capital, market, costs and loss limits | Refuse incompatible trading assumptions | Illustrative policy included |
| 1: Foundation | Strict config, CLI, state and journal | Reject bad config; restart without resetting capital | Implemented |
| 2: Data | Validated snapshots and causal replay | Detect bad timestamps, gaps, hash changes and lookahead | Implemented with synthetic fixtures |
| 3: Risk | Position sizing and persistent budgets | Count all costs/exposure; apply the tightest limit | Implemented for the model |
| 4: Paper execution | Fills, cancellation, recovery and accounting | Preserve uncertain orders, fees and unsellable holdings | Implemented offline |
| 5: Research | Fixed strategies, stress scenarios and reserved samples | Retain failed cases; require sufficient independent evidence | Research tools implemented; financial qualification unproven |
| 6: Virtual exchange | Isolated Testnet entry, protection, exit and reconciliation | Confirm permissions, fills and balances with virtual assets | Adapter implemented; provider behavior requires separate observation |
| 7: Supervised operations | Monitoring, process supervision and recovery | Demonstrate outages, alerts, restarts and prolonged paper operation | Worker/service code implemented; prolonged operation pending |
| 8: Production review | Qualified data, execution costs and operator permissions | Independent review and explicit authorization | Gated connector implemented; evidence/release pending |
| 9: Bounded adaptation | Propose challenger strategies | Independent unseen evaluation; preserve all risk caps | Fixed-card online selector implemented; profitable adaptation unproven |

An issue must be retained and resolved with evidence before its gate can pass. A missing trade, no-trade strategy, synthetic profit or clean software test is not evidence of successful real trading.

## Known limits

- No proven profitable strategy or return target.
- No proven profitable learning model, automatic financial qualification or risk-limit adjustment.
- Production Spot code is gated; margin, futures, shorting, martingale and averaging down are unsupported.
- No gold, forex or index execution adapter.
- Small positions may be impossible to liquidate completely after base-asset fees.
- OHLCV simulation does not reproduce order-book queues or liquidity.
- USD/USDT bounds and some costs are illustrative assumptions.
- Service templates are included; no hosted deployment, guaranteed uptime or unattended human review is established.
- Testnet resets and unexpected account changes require review; balances are never treated as a capital refill.

The productive outcome of a failed gate can be to retain cash and report that the approach is infeasible.
