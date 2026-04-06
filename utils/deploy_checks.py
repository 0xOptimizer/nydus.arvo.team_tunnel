import asyncio
import os
import re
import socket
import logging

logger = logging.getLogger('nydus')

_PORT_RE = re.compile(r'proxy_pass\s+http://localhost:(\d+)', re.IGNORECASE)


def redact_pat(text: str, pat: str) -> str:
    if not pat or not text:
        return text
    return text.replace(pat, '***')


def _scan_nginx_ports(nginx_dir: str) -> set[int]:
    used: set[int] = set()
    if not os.path.isdir(nginx_dir):
        return used
    for filename in os.listdir(nginx_dir):
        path = os.path.join(nginx_dir, filename)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, 'r', errors='replace') as f:
                content = f.read()
            for match in _PORT_RE.finditer(content):
                used.add(int(match.group(1)))
        except Exception as e:
            logger.warning(f"Could not read nginx config {path}: {e}")
    return used


async def get_used_ports_from_nginx(nginx_dir: str) -> set[int]:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _scan_nginx_ports, nginx_dir)


def assign_free_port(used_ports: set[int], min_port: int, max_port: int) -> int | None:
    for port in range(min_port, max_port + 1):
        if port not in used_ports:
            return port
    return None


def _resolve_ip(fqdn: str) -> set[str]:
    results = socket.getaddrinfo(fqdn, None)
    return {r[4][0] for r in results}


async def check_dns_propagated(
    fqdn: str,
    expected_ip: str,
    retries: int = 12,
    delay: float = 10.0,
) -> bool:
    loop = asyncio.get_running_loop()
    for attempt in range(retries):
        try:
            ips = await loop.run_in_executor(None, _resolve_ip, fqdn)
            if expected_ip in ips:
                return True
        except Exception as e:
            logger.debug(f"DNS check attempt {attempt + 1}/{retries} for {fqdn}: {e}")
        if attempt < retries - 1:
            await asyncio.sleep(delay)
    return False