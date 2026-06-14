import os
import re

_SUBDOMAIN_RE = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$')  # also the per-label rule for FQDNs
_ENV_KEY_RE   = re.compile(r'^[A-Z_][A-Z0-9_]*$')


def validate_subdomain(subdomain: str) -> tuple[bool, str]:
    if not subdomain:
        return False, "Subdomain cannot be empty."
    if len(subdomain) > 24:
        return False, f"Subdomain must be 24 characters or fewer. Got {len(subdomain)}."
    if not _SUBDOMAIN_RE.match(subdomain):
        return False, (
            "Subdomain must use only lowercase letters, numbers, and hyphens, "
            "and cannot start or end with a hyphen."
        )
    return True, ""


def validate_domain(fqdn: str) -> tuple[bool, str]:
    """
    Validate a full custom domain (apex or sub) used for a custom-domain deployment.

    Allows e.g. `client.com` and `shop.client.com`; rejects bare labels, wildcards,
    trailing dots, bad labels, and anything under the managed domain (those must use a
    `subdomain` deployment instead, or they'd collide on the canonical fqdn identity).
    """
    if not fqdn:
        return False, "Domain cannot be empty."
    if len(fqdn) > 253:
        return False, f"Domain must be 253 characters or fewer. Got {len(fqdn)}."
    if '*' in fqdn:
        return False, "Wildcard domains are not supported."
    if fqdn.endswith('.'):
        return False, "Domain must not end with a trailing dot."
    if '.' not in fqdn:
        return False, "Domain must be a full hostname with at least one dot (e.g. example.com)."
    for label in fqdn.split('.'):
        if not (1 <= len(label) <= 63) or not _SUBDOMAIN_RE.match(label):
            return False, (
                f"Invalid domain label '{label}'. Each label must be 1-63 characters of "
                "lowercase letters, numbers, and hyphens, and cannot start or end with a hyphen."
            )
    managed = os.getenv('DEPLOY_DOMAIN', 'arvo.team').lower()
    low = fqdn.lower()
    if low == managed or low.endswith('.' + managed):
        return False, (
            f"'{fqdn}' is under the managed domain '{managed}'. "
            "Use a subdomain deployment instead of a custom domain."
        )
    return True, ""


def validate_env_key(key: str) -> tuple[bool, str]:
    if not key:
        return False, "Key cannot be empty."
    if not _ENV_KEY_RE.match(key):
        return False, (
            f"Invalid environment variable key: '{key}'. "
            "Must start with a letter or underscore and contain only "
            "uppercase letters, numbers, and underscores."
        )
    return True, ""