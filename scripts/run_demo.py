"""One-command offline demonstration; does not change an existing account."""

import json
from demo_workspace import isolated


def main():
    with isolated() as (_, run):
        def cli(*arguments):
            return json.loads(run(["-m", "marvel", *arguments]).stdout)
        validation = cli("validate")
        initialized = cli("init")
        cli("paper", "demo")
        research = cli("research", "demo")["research_screen"]
        conversion = cli("research", "convert-demo")
        cycle = cli("research", "cycle-demo")
        print(json.dumps({
            "demo": "offline_synthetic_only",
            "configuration_valid": validation["configuration_valid"],
            "starting_virtual_equity_usd": initialized["account"]["initial_equity"],
            "paper_failure_and_dust_demo_completed": True,
            "research_candidate_count": len(research["candidates"]),
            "final_holdout_evaluated": research["holdout_evaluated"],
            "conversion_failure_demo_completed": "conversion_demo" in conversion,
            "risk_bound_cycle_demo_completed": "cycle_demo" in cycle,
            "strategy_qualified": False,
            "live_execution_available": False,
            "existing_account_changed": False,
            "network_connections_allowed": False,
            "credentials_required": False,
            "note": "Invented data demonstrates engineering behavior, not returns."
        }, indent=2))


if __name__ == "__main__":
    main()
