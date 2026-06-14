"""
Pure domain/zone helpers shared by the cogs and the logic tests.

Kept dependency-free (stdlib only) so tests/test_logic.py can import the *real*
shipped logic instead of mirroring it.
"""

import os


def fqdn_of(deployment: dict) -> str:
    """
    Canonical public hostname for a deployment row.

    Prefer the stored `fqdn` (written for every row going forward). Fall back to the
    legacy `{subdomain}.{DEPLOY_DOMAIN}` derivation so any row created before the
    custom-domains migration still resolves correctly.
    """
    fqdn = deployment.get('fqdn')
    if fqdn:
        return fqdn
    domain = os.getenv('DEPLOY_DOMAIN', 'arvo.team')
    return f"{deployment.get('subdomain')}.{domain}"


def pick_zone_for_fqdn(fqdn: str, zones: list) -> dict | None:
    """
    Pick the Cloudflare zone authoritative for `fqdn` from a list of zone dicts
    (each with a `name`, e.g. from `GET /zones`).

    A zone matches on a label boundary only: `fqdn == zone.name` (apex) or
    `fqdn` ends with `"." + zone.name` (a host within the zone). Among matches the
    longest zone name wins (so `a.shop.client.com` prefers zone `shop.client.com`
    over `client.com`), and `notclient.com` never matches zone `client.com`.

    Returns the winning zone dict, or None if no zone is authoritative.
    """
    if not fqdn or not zones:
        return None
    fqdn = fqdn.lower().rstrip('.')
    best = None
    best_len = -1
    for zone in zones:
        name = (zone.get('name') or '').lower().rstrip('.')
        if not name:
            continue
        if fqdn == name or fqdn.endswith('.' + name):
            if len(name) > best_len:
                best = zone
                best_len = len(name)
    return best
