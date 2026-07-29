"""Run every backend self-test in one go.

    .venv\\Scripts\\python -m backend.tools.run_all

Each suite runs in a subprocess (they were written as standalone scripts with
sys.exit) so one suite's monkeypatching can never bleed into the next. Exits
non-zero if any suite fails -- safe for a pre-release check or CI.
"""

import subprocess
import sys

SUITES = [
    "backend.tools.hsfz_selftest",
    "backend.tools.doip_selftest",
    "backend.tools.mazda_selftest",
    "backend.tools.peugeot_selftest",
    "backend.tools.vw_selftest",
    "backend.tools.mercedes_selftest",
    "backend.tools.fiat_selftest",
    "backend.tools.jlr_selftest",
    "backend.tools.taycan_selftest",
    "backend.tools.readiness_selftest",
    "backend.tools.livedata_selftest",
    "backend.tools.j2534_selftest",
    "backend.tools.resets_selftest",
    "backend.tools.service_history_selftest",
    "backend.tools.ediabas_selftest",
]


def main() -> int:
    failed = []
    for suite in SUITES:
        print(f"\n{'=' * 70}\n= {suite}\n{'=' * 70}", flush=True)
        result = subprocess.run([sys.executable, "-m", suite])
        if result.returncode != 0:
            failed.append(suite)
    print(f"\n{'=' * 70}")
    if failed:
        print(f"FAILED: {len(failed)} of {len(SUITES)} suites: {', '.join(failed)}")
        return 1
    print(f"ALL {len(SUITES)} SELF-TEST SUITES PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
