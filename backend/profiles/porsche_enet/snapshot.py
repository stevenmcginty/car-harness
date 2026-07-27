"""Full 198-cell map for a Porsche Taycan HV battery.

NAMING NOTE: the harness's generic brand panel offers each profile two optional
actions, "live" and "snapshot". On the dongle brands "snapshot" means a passive
broadcast-CAN capture; there is no broadcast traffic to sniff on a DoIP cable,
so this profile uses that second action slot for the deep battery read. The
panel label in profile.json is what the user actually sees ("Full cell map"),
so nothing reads as a CAN sniff.

Why it is separate from "live": walking all 33 module blocks means 66 extra
requests and needs the extended diagnostic session, so it is opt-in rather than
part of every battery read.
"""

from . import pids


def snapshot(mode: str = "real") -> dict:
    """Every cell voltage and every module temperature sensor in the pack."""
    return pids._battery_report(mode, deep=True)
