"""Research commands never open or modify the paper account database."""

from pathlib import Path

from .client import PublicClient
from .quality import INTERVALS, MAX_BYTES, fail, replay, utc_us
from .store import SnapshotStore


def add_parser(commands):
    parser = commands.add_parser("data", help="BTCUSDT research snapshots and causal replay; cannot trade")
    sub = parser.add_subparsers(dest="data_command", required=True)
    local = sub.add_parser("import", help="Validate an explicitly labelled local research input")
    local.add_argument("--input", type=Path, required=True)
    local.add_argument("--format", choices=["rest-json", "csv", "archive-zip"], required=True)
    local.add_argument("--unit", choices=["ms", "us"], required=True)
    local.add_argument("--source", choices=["synthetic", "binance-local"], required=True)
    local.add_argument("--checksum", help="SHA256 required for ZIP input")
    local.add_argument("--interval", choices=INTERVALS, default="1h")
    for name in ("start", "end", "as-of"):
        local.add_argument("--" + name, required=True, help="UTC ISO timestamp, e.g. 2026-09-01T00:00:00Z; end is exclusive")
    sub.add_parser("list", help="Verify and list stored snapshots")
    inspect = sub.add_parser("inspect", help="Verify raw/normalized hashes and show a snapshot manifest")
    inspect.add_argument("--dataset", required=True)
    playback = sub.add_parser("replay", help="Compute features only from candles completed as of the requested UTC time")
    playback.add_argument("--dataset", required=True)
    playback.add_argument("--as-of", required=True)
    playback.add_argument("--lookback", type=int, default=20)
    archive = sub.add_parser("fetch-archive", help="Download a completed BTCUSDT month and its provider checksum")
    archive.add_argument("--month", required=True, help="YYYY-MM, completed months only")
    historical = sub.add_parser("fetch-rest", help="Bounded paginated completed BTCUSDT history")
    historical.add_argument("--start", required=True)
    historical.add_argument("--end", required=True)
    recent = sub.add_parser("fetch-recent", help="Capture the latest completed BTCUSDT hourly candles for research")
    recent.add_argument("--hours", type=int, default=72)
    observe = sub.add_parser("observe", help="Save current BTCUSDT metadata and quote-quality observations")
    for endpoint in (archive, historical, recent, observe):
        endpoint.add_argument("--allow-network", action="store_true", help="Opt this invocation into public research GETs; execution stays disabled")
    for endpoint in (archive, historical):
        endpoint.add_argument("--interval", choices=INTERVALS, default="1h")


def run(args, config) -> dict:
    store = SnapshotStore(config.workspace)
    command = args.data_command
    if command == "list":
        return {"datasets": store.inventory()}
    if command in {"inspect", "replay"}:
        manifest, history = store.load(args.dataset)
        if command == "inspect":
            return {"manifest": manifest}
        result = replay(history, interval=manifest["parameters"]["interval"], as_of_us=utc_us(args.as_of),
                        captured_as_of_us=manifest["parameters"]["as_of_us"], lookback=args.lookback)
        return {"dataset": args.dataset, "attribution": manifest["attribution"], "replay": result}
    if command == "import":
        path = args.input if args.input.is_absolute() else config.workspace / args.input
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
        except OSError:
            fail()
        parameters = {"format": args.format, "unit": args.unit, "interval": args.interval,
                      "start_us": utc_us(args.start), "end_us": utc_us(args.end),
                      "as_of_us": utc_us(args.as_of), "checksum": args.checksum}
        provenance = {"origin": "local_unverified", "data_kind": "synthetic" if args.source == "synthetic" else "binance_spot",
                      "identity_limit": "Local bytes; input label does not establish provider origin"}
    else:
        client = PublicClient(allow_network=args.allow_network)
        if command == "observe":
            observation = client.observe(max_age_seconds=config.max_quote_age_seconds, max_spread_bps=config.max_spread_bps)
            identifier = store.save_observation(observation)
            return {"observation_id": identifier, "observation": observation, "research_network_opt_in": True,
                    "public_research_requests": len(client.requests)}
        if command == "fetch-archive":
            raw, parameters = client.archive(month=args.month, interval=args.interval)
        else:
            if command == "fetch-recent":
                if not 2 <= args.hours <= 1000:
                    fail()
                interval = "1h"
                as_of = client.server_time()
                end = as_of // INTERVALS[interval] * INTERVALS[interval]
                start = end - args.hours * INTERVALS[interval]
            else:
                interval, start, end = args.interval, utc_us(args.start), utc_us(args.end)
                as_of = client.server_time()
            raw, parameters = client.rest(interval=interval, start_us=start, end_us=end, as_of_us=as_of)
        provenance = client.provenance()
    manifest = store.save(raw, parameters, provenance=provenance)
    return {"manifest": manifest, "research_network_opt_in": command != "import",
            "public_research_requests": len(provenance.get("requests", []))}
