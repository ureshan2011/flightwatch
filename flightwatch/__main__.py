"""
Command-line entry point so the project runs as a package:

    python -m flightwatch collect    # one scan -> appends to data/
    python -m flightwatch build      # regenerate docs/index.html
    python -m flightwatch diag       # show what the fare API holds (debugging)

With no argument it runs a collect followed by a dashboard build, which is handy
locally and keeps the GitHub Actions workflow simple.
"""

import sys

from . import collect as collect_mod
from . import dashboard as dashboard_mod

USAGE = "usage: python -m flightwatch [collect|build|all|diag]"


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
    elif cmd in ("diag", "diagnose"):
        collect_mod.diagnose()
    else:
        print(USAGE)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
