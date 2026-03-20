from __future__ import annotations

import ipaddress
import socket
from typing import Tuple
from urllib.parse import urlparse

# Blocked IP ranges — private, loopback, link-local, metadata, unspecified
BLOCKED_RANGES = [
    ipaddress.ip_network('127.0.0.0/8'),       # Loopback
    ipaddress.ip_network('10.0.0.0/8'),         # Private
    ipaddress.ip_network('172.16.0.0/12'),      # Private
    ipaddress.ip_network('192.168.0.0/16'),     # Private
    ipaddress.ip_network('169.254.0.0/16'),     # Link-local / cloud metadata
    ipaddress.ip_network('0.0.0.0/8'),          # Unspecified
    ipaddress.ip_network('100.64.0.0/10'),      # Shared address space (CGN)
    ipaddress.ip_network('198.18.0.0/15'),      # Benchmarking
    ipaddress.ip_network('::1/128'),            # IPv6 loopback
    ipaddress.ip_network('fc00::/7'),           # IPv6 private
    ipaddress.ip_network('fe80::/10'),          # IPv6 link-local
]

BLOCKED_HOSTNAMES = {
    'localhost',
    'metadata.google.internal',
    'metadata.internal',
}


def validate_callback_url(url: str, env: str = "production") -> Tuple[bool, str]:
    """Validate callback URL for SSRF safety.

    Returns (is_valid, error_message).

    In development: allows http and https schemes, still blocks private IPs/hostnames.
    In production: requires https, blocks private IPs/hostnames.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL format"

    # Scheme check
    if env == "development":
        if parsed.scheme not in ("http", "https"):
            return False, "URL must use http:// or https://"
    else:
        if parsed.scheme != "https":
            return False, "URL must use https:// in production"

    # Reject embedded credentials (user:pass@host)
    if parsed.username or parsed.password:
        return False, "Credentials in callback URLs are not allowed"

    # Hostname check
    hostname = parsed.hostname
    if not hostname:
        return False, "URL must have a hostname"

    if hostname.lower() in BLOCKED_HOSTNAMES:
        return False, f"Blocked hostname: {hostname}"

    # Resolve hostname and check IP against blocked ranges
    try:
        resolved_ips = socket.getaddrinfo(hostname, None)
        for family, socktype, proto, canonname, sockaddr in resolved_ips:
            ip = ipaddress.ip_address(sockaddr[0])
            for blocked in BLOCKED_RANGES:
                if ip in blocked:
                    return False, "Callback URL resolves to blocked address range"
    except socket.gaierror:
        return False, f"Cannot resolve hostname: {hostname}"

    return True, ""


def is_blocked_ip(ip_str: str) -> bool:
    """Check if an IP address falls in any blocked range."""
    try:
        ip = ipaddress.ip_address(ip_str)
        for blocked in BLOCKED_RANGES:
            if ip in blocked:
                return True
    except ValueError:
        return True  # Unparseable = blocked
    return False


def validate_url_at_delivery(url: str, env: str = "production") -> Tuple[bool, str]:
    """Re-validate a callback URL at delivery time (DNS rebind protection).

    Resolves hostname again and checks all resolved IPs against blocked ranges.
    This catches DNS rebinding attacks where the hostname resolved to a public IP
    at cue creation but resolves to a private IP at delivery time.

    In development/test: allows localhost and loopback for local testing.
    In production: blocks all private/internal addresses.

    Returns (is_valid, error_message).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL format"

    hostname = parsed.hostname
    if not hostname:
        return False, "URL must have a hostname"

    # In development, allow localhost for local webhook testing
    is_dev = env in ("development", "test")
    if not is_dev:
        if hostname.lower() in BLOCKED_HOSTNAMES:
            return False, "Callback URL resolved to blocked IP"

    try:
        resolved_ips = socket.getaddrinfo(hostname, None)
        for family, socktype, proto, canonname, sockaddr in resolved_ips:
            ip = ipaddress.ip_address(sockaddr[0])
            # In dev, allow loopback (127.x) but still block cloud metadata and other private ranges
            if is_dev:
                if ip in ipaddress.ip_network('169.254.0.0/16'):
                    return False, "Callback URL resolved to blocked IP"
                if ip in ipaddress.ip_network('fe80::/10'):
                    return False, "Callback URL resolved to blocked IP"
            else:
                if is_blocked_ip(sockaddr[0]):
                    return False, "Callback URL resolved to blocked IP"
    except socket.gaierror:
        return False, "Callback URL resolved to blocked IP"

    return True, ""
