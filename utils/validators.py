import re

_SUBDOMAIN_RE = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?$')
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