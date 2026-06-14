"""
Local logic checks for the reliability pass — no server, no DB, no heavy deps.

Run: python tests/test_logic.py
Exercises the pure, server-independent logic touched/added in this pass. Subprocess,
pm2, nginx, certbot, Cloudflare and MySQL are all out of scope here (Tier 2/3, owner-run).
"""
import os
import re
import sys

# Make the repo root importable when run from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

failures = []


def check(name, cond):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}")
        failures.append(name)


# --- assign_free_port (real shipped code; light deps only) -------------------
from utils.deploy_checks import assign_free_port

print("assign_free_port:")
check("returns first free in range", assign_free_port(set(), 3100, 3102) == 3100)
check("skips used ports", assign_free_port({3100, 3101}, 3100, 3102) == 3102)
check("None when exhausted", assign_free_port({3100, 3101, 3102}, 3100, 3102) is None)
check("ignores non-int sentinels (NULL ports)", assign_free_port({None, 3100}, 3100, 3101) == 3101)

# --- subdomain / env-key validators (real shipped code) ----------------------
from utils.validators import validate_subdomain, validate_env_key

print("validate_subdomain:")
check("accepts simple", validate_subdomain("my-app")[0] is True)
check("rejects leading hyphen", validate_subdomain("-bad")[0] is False)
check("rejects uppercase", validate_subdomain("Bad")[0] is False)
check("rejects >24 chars", validate_subdomain("a" * 25)[0] is False)

print("validate_env_key:")
check("accepts UPPER_SNAKE", validate_env_key("API_KEY")[0] is True)
check("rejects lowercase", validate_env_key("api")[0] is False)
check("rejects leading digit", validate_env_key("1X")[0] is False)

# --- webhook push ref -> branch parsing (mirrors handle_webhook) -------------
def parse_branch(ref):
    return ref.split('refs/heads/', 1)[1] if ref.startswith('refs/heads/') else ref

print("webhook ref -> branch:")
check("refs/heads/main -> main", parse_branch("refs/heads/main") == "main")
check("refs/heads/feature/x -> feature/x", parse_branch("refs/heads/feature/x") == "feature/x")
check("tag ref passes through", parse_branch("refs/tags/v1") == "refs/tags/v1")

# --- safe upload-id validation (mirrors api_cog is_valid_uuid) ---------------
_SAFE = re.compile(r'^[A-Za-z0-9_-]{8,128}$')
def safe_id(s):
    return bool(s) and bool(_SAFE.match(s))

print("safe upload id:")
check("accepts tusd-style hex id", safe_id("a1b2c3d4e5f6a7b8c9d0"))
check("rejects path traversal", not safe_id("../../etc/passwd"))
check("rejects slash", not safe_id("abc/def12345"))
check("rejects null byte", not safe_id("abc\x00defxyz"))
check("rejects too short", not safe_id("abc"))

# --- pm2 online decision logic (mirrors _pm2_is_online's per-poll rule) -------
def pm2_ok(status, restarts, first):
    if status != 'online':
        return False
    if first is not None and restarts > first:
        return False
    return True

print("pm2 online rule:")
check("online + stable restarts -> ok", pm2_ok('online', 5, 5) is True)
check("errored -> not ok", pm2_ok('errored', 0, 0) is False)
check("climbing restarts -> crash-loop", pm2_ok('online', 7, 5) is False)

# --- control plane: cert expiry + nginx parse + host normalize (mirror shipped) ---
print("control-plane parsing:")
cert_out = "  Certificate Name: sub.arvo.team\n    Domains: sub.arvo.team\n    Expiry Date: 2026-09-01 12:00:00+00:00 (VALID: 80 days)\n"
m = re.search(r'VALID:\s*(\d+)\s*day', cert_out)
check("certbot VALID days parse", m and int(m.group(1)) == 80)

nginx_cfg = "server {\n  server_name sub.arvo.team www.sub.arvo.team;\n  location / { proxy_pass http://localhost:3133; }\n}"
names = [s for nm in re.findall(r'server_name\s+([^;]+);', nginx_cfg) for s in nm.split()]
ports = [int(p) for p in re.findall(r'proxy_pass\s+http://localhost:(\d+)', nginx_cfg)]
check("nginx server_name parse", names == ['sub.arvo.team', 'www.sub.arvo.team'])
check("nginx proxy_pass port parse", ports == [3133])

def norm_hosts(allowed):
    if not allowed:
        return ['%']
    parts = [h.strip() for h in allowed.split(',') if h.strip()] if isinstance(allowed, str) else [str(h).strip() for h in allowed if str(h).strip()]
    if not parts:
        return ['%']
    if any(p in ('*', '%') for p in parts):
        return ['%']
    return parts

print("db host normalize:")
check("empty -> %", norm_hosts('') == ['%'])
check("* -> %", norm_hosts('*') == ['%'])
check("localhost stays", norm_hosts('localhost') == ['localhost'])
check("csv preserved", norm_hosts('10.0.0.5, 10.0.0.6') == ['10.0.0.5', '10.0.0.6'])

# --- self-test: variant parsing (mirrors SelfTestCog.parse_variants) ----------
_ALL = ['static', 'node', 'rebuild', 'webhook', 'rollback']
_NODE_DEP = {'node', 'rebuild', 'webhook', 'rollback'}
def parse_variants(value):
    if not value or (isinstance(value, str) and value.strip().lower() in ('all', '*', '')):
        return list(_ALL)
    req = ({v.strip().lower() for v in value.split(',') if v.strip()}
           if isinstance(value, str) else {str(v).strip().lower() for v in value})
    if req & _NODE_DEP:
        req.add('node')  # node-dependent steps need the node deploy first
    return [v for v in _ALL if v in req] or list(_ALL)

print("selftest variant parse:")
check("all -> full ordered suite", parse_variants('all') == _ALL)
check("empty -> full suite", parse_variants('') == _ALL)
check("webhook pulls in node prereq", parse_variants('webhook') == ['node', 'webhook'])
check("keeps canonical order", parse_variants('rollback,static,node') == ['static', 'node', 'rollback'])
check("unknown-only -> full suite (no empty run)", parse_variants('bogus') == _ALL)

# --- self-test: webhook HMAC sign/verify contract ----------------------------
# _fire_webhook signs; handle_webhook verifies. They must agree byte-for-byte.
import hmac as _hmac, hashlib as _hashlib, json as _json
def _sign(secret, body):  # mirrors SelfTestCog._fire_webhook
    return 'sha256=' + _hmac.new(secret.encode(), body, _hashlib.sha256).hexdigest()
def _verify(secret, body, signature):  # mirrors api_cog.handle_webhook
    expected = 'sha256=' + _hmac.new(secret.encode(), msg=body, digestmod=_hashlib.sha256).hexdigest()
    return bool(signature) and _hmac.compare_digest(expected, signature)

print("selftest webhook HMAC:")
_secret = 'deadbeefcafef00d'
_body = _json.dumps({'ref': 'refs/heads/main'}).encode()
check("valid signature verifies", _verify(_secret, _body, _sign(_secret, _body)))
check("wrong secret rejected", not _verify(_secret, _body, _sign('not-the-secret', _body)))
check("tampered body rejected", not _verify(_secret, _body + b'x', _sign(_secret, _body)))
check("missing signature rejected", not _verify(_secret, _body, None))

# --- custom domains: validate_domain (real shipped code) ----------------------
os.environ['DEPLOY_DOMAIN'] = 'arvo.team'  # make the managed-domain check deterministic
from utils.validators import validate_domain

print("validate_domain:")
check("accepts sub of custom", validate_domain("shop.client.com")[0] is True)
check("accepts apex custom", validate_domain("client.com")[0] is True)
check("rejects bare label", validate_domain("client")[0] is False)
check("rejects trailing dot", validate_domain("client.com.")[0] is False)
check("rejects uppercase", validate_domain("Client.com")[0] is False)
check("rejects leading-hyphen label", validate_domain("-x.client.com")[0] is False)
check("rejects wildcard", validate_domain("*.client.com")[0] is False)
check("rejects >253 chars", validate_domain(("a" * 60 + ".") * 5 + "com")[0] is False)
check("rejects under managed domain", validate_domain("foo.arvo.team")[0] is False)
check("rejects managed apex", validate_domain("arvo.team")[0] is False)

# --- custom domains: fqdn_of + pick_zone_for_fqdn (real shipped code) ---------
from utils.domains import fqdn_of, pick_zone_for_fqdn

print("fqdn_of:")
check("prefers stored fqdn", fqdn_of({'fqdn': 'shop.client.com', 'subdomain': 'x'}) == 'shop.client.com')
check("falls back to subdomain", fqdn_of({'subdomain': 'foo'}) == 'foo.arvo.team')

print("pick_zone_for_fqdn:")
_zones = [{'id': 'z1', 'name': 'client.com'}, {'id': 'z2', 'name': 'other.com'}]
check("sub matches its zone", pick_zone_for_fqdn('shop.client.com', _zones)['id'] == 'z1')
check("apex matches its zone", pick_zone_for_fqdn('client.com', _zones)['id'] == 'z1')
check("longest suffix wins",
      pick_zone_for_fqdn('a.shop.client.com',
                         [{'id': 'z1', 'name': 'client.com'}, {'id': 'z3', 'name': 'shop.client.com'}])['id'] == 'z3')
check("label-boundary only (no substring match)", pick_zone_for_fqdn('notclient.com', _zones) is None)
check("no match -> None", pick_zone_for_fqdn('example.org', _zones) is None)

# --- custom domains: deploy required-field rule (mirrors handle_deploy) -------
def deploy_fields_ok(dns_mode, subdomain, domain):
    if dns_mode not in ('subdomain', 'cloudflare', 'external'):
        return False
    return bool(subdomain) if dns_mode == 'subdomain' else bool(domain)

print("deploy field rule (subdomain XOR domain):")
check("subdomain mode needs subdomain", deploy_fields_ok('subdomain', 'myapp', None) is True)
check("subdomain mode rejects missing", deploy_fields_ok('subdomain', None, 'x.com') is False)
check("cloudflare mode needs domain", deploy_fields_ok('cloudflare', None, 'shop.client.com') is True)
check("external mode needs domain", deploy_fields_ok('external', None, 'client.com') is True)
check("custom mode rejects missing domain", deploy_fields_ok('cloudflare', 'myapp', None) is False)
check("unknown mode rejected", deploy_fields_ok('bogus', 'myapp', 'x.com') is False)

# --- watchdog alert gate (mirrors MonitoringCog._alerts_active) ----------------
def alerts_active(enabled, age, grace):
    if not enabled:
        return False
    if age is not None and age < grace:
        return False
    return True

print("watchdog alert gate:")
check("disabled -> no alerts", alerts_active(False, 10_000, 300) is False)
check("enabled but in startup grace -> suppressed", alerts_active(True, 10, 300) is False)
check("enabled and past grace -> alerts", alerts_active(True, 400, 300) is True)
check("enabled, never started -> alerts", alerts_active(True, None, 300) is True)

print()
if failures:
    print(f"{len(failures)} CHECK(S) FAILED: {failures}")
    sys.exit(1)
print("ALL LOGIC CHECKS PASSED")
