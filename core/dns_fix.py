"""Termux / Android DNS fix for dnspython (sync + async).

dnspython defaults to /etc/resolv.conf which does not exist on Termux.
PyMongo AsyncMongoClient uses dns.asyncresolver for mongodb+srv:// — both
resolvers must be configured before any SRV lookup.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_APPLIED = False


def apply_termux_dns_fix(force: bool = False) -> None:
    """Configure dns.resolver + dns.asyncresolver for Termux-safe nameservers."""
    global _APPLIED
    if _APPLIED and not force:
        return

    candidates = []
    prefix = os.environ.get("PREFIX")  # Termux: /data/data/com.termux/files/usr
    if prefix:
        candidates.append(os.path.join(prefix, "etc", "resolv.conf"))
    # Some environments symlink or mount resolv elsewhere
    candidates.extend(
        [
            "/etc/resolv.conf",
            "/data/data/com.termux/files/usr/etc/resolv.conf",
        ]
    )
    resolv: Optional[str] = next((p for p in candidates if p and os.path.isfile(p)), None)

    try:
        import dns.resolver
        import dns.asyncresolver
    except ImportError:
        logger.warning("dnspython not installed — mongodb+srv may fail")
        return

    try:
        if resolv:
            dns.resolver.default_resolver = dns.resolver.Resolver(filename=resolv)
            try:
                dns.asyncresolver.default_resolver = dns.asyncresolver.Resolver(
                    filename=resolv
                )
            except Exception:
                ar = dns.asyncresolver.Resolver(configure=False)
                ar.nameservers = list(dns.resolver.default_resolver.nameservers)
                dns.asyncresolver.default_resolver = ar
            logger.debug("DNS resolvers configured from %s", resolv)
        else:
            # No resolv.conf anywhere (typical Termux without PREFIX file) —
            # use public recursive resolvers so mongodb+srv still works.
            nameservers = ["8.8.8.8", "1.1.1.1", "8.8.4.4"]
            for mod in (dns.resolver, dns.asyncresolver):
                r = mod.Resolver(configure=False)
                r.nameservers = list(nameservers)
                mod.default_resolver = r
            logger.warning(
                "No resolv.conf found — using public DNS %s (Termux-safe)",
                nameservers,
            )
        _APPLIED = True
    except Exception as e:
        logger.warning("DNS fix failed: %s", e)
