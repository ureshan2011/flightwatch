"""
Command-line entry point so the project runs as a package:

    python -m flightwatch collect     # one scan -> appends to data/
    python -m flightwatch build       # regenerate docs/index.html
    python -m flightwatch all         # collect, then build (CI default)
    python -m flightwatch alert        # push fresh signals (Telegram/email)
    python -m flightwatch publish      # OPTIONAL: write route verdicts to Firestore
    python -m flightwatch backtest     # print how the engine's calls have fared
    python -m flightwatch diag         # show what the scraper returns (debugging)
    python -m flightwatch sq-diag      # EXPERIMENTAL: scrape Singapore Airlines' site

With no argument it runs a collect followed by a dashboard build, which is handy
locally and keeps the GitHub Actions workflow simple.
"""

import sys

from . import collect as collect_mod
from . import dashboard as dashboard_mod
from . import alerts as alerts_mod

USAGE = ("usage: python -m flightwatch "
         "[collect|build|all|alert|publish|backtest|diag|sq-diag]")


def _print_backtest():
    from . import storage, predict
    bt = predict.backtest(storage.load_all())
    if not bt:
        print("Not enough history to backtest yet (need 4+ daily points on a route).")
        return
    print(f"Backtest: {bt['hit_rate']}% of {bt['n']} graded calls were right.")
    for sig, v in bt.get("by_signal", {}).items():
        print(f"  {sig:5s} {v['hit_rate']:3d}% over {v['n']} calls")
    if bt.get("avg_buy_regret"):
        print(f"  avg missed saving on a BUY: {bt['avg_buy_regret']}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "all"

    if cmd == "collect":
        collect_mod.collect()
    elif cmd in ("build", "dashboard"):
        dashboard_mod.build()
    elif cmd == "all":
        collect_mod.collect()
        dashboard_mod.build()
    elif cmd == "alert":
        alerts_mod.run(dry_run="--dry-run" in argv)
    elif cmd == "publish":
        from . import publish as publish_mod
        publish_mod.publish()
    elif cmd == "backtest":
        _print_backtest()
    elif cmd in ("diag", "diagnose"):
        collect_mod.diagnose()
    elif cmd in ("sq-diag", "sqdiag"):
        from . import provider_sq
        provider_sq.diagnose(*argv[1:5])
    else:
        print(USAGE)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
