"""Network-adapter detection for the ENET/DoIP probe.

BMW ENET cables carry no DHCP server, so when the cable is plugged into a live
car the laptop's Ethernet port falls back to an APIPA / link-local address in
169.254.0.0/16. That link-local address on an "up" interface is the primary
signal that we are cabled to a car, so it is what marks an adapter a candidate.
"""

import ipaddress
import socket
from typing import List, Optional

import psutil

LINK_LOCAL_NET = ipaddress.ip_network("169.254.0.0/16")


def _is_link_local(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in LINK_LOCAL_NET
    except ValueError:
        return False


def _ipv4_addresses(addrs) -> List[str]:
    return [a.address for a in addrs if a.family == socket.AF_INET]


def list_interfaces() -> List[dict]:
    """Return every adapter as {name, addresses:[ipv4...], isUp, isCandidate}.

    A candidate = isUp AND holds a 169.254.x.x IPv4 (the ENET signal).
    """
    stats = psutil.net_if_stats()
    if_addrs = psutil.net_if_addrs()
    interfaces: List[dict] = []
    for name, addrs in if_addrs.items():
        ipv4s = _ipv4_addresses(addrs)
        st = stats.get(name)
        is_up = bool(st.isup) if st else False
        is_candidate = is_up and any(_is_link_local(ip) for ip in ipv4s)
        interfaces.append(
            {
                "name": name,
                "addresses": ipv4s,
                "isUp": is_up,
                "isCandidate": is_candidate,
            }
        )
    return interfaces


def _broadcast_for(ip: str, netmask: Optional[str], broadcast: Optional[str]) -> str:
    """Best-effort subnet broadcast address for a link-local IPv4."""
    if broadcast:
        return broadcast
    if netmask:
        try:
            net = ipaddress.IPv4Network(f"{ip}/{netmask}", strict=False)
            return str(net.broadcast_address)
        except ValueError:
            pass
    # Link-local is a /16, so its broadcast is 169.254.255.255.
    return "169.254.255.255"


def best_candidate() -> Optional[dict]:
    """First candidate adapter as {name, ip, broadcast}, or None if none.

    Only interfaces that are up AND carry a 169.254.x.x address qualify.
    """
    stats = psutil.net_if_stats()
    if_addrs = psutil.net_if_addrs()
    for name, addrs in if_addrs.items():
        st = stats.get(name)
        if not (st and st.isup):
            continue
        for a in addrs:
            if a.family != socket.AF_INET or not _is_link_local(a.address):
                continue
            broadcast = _broadcast_for(
                a.address, getattr(a, "netmask", None), getattr(a, "broadcast", None)
            )
            return {"name": name, "ip": a.address, "broadcast": broadcast}
    return None
