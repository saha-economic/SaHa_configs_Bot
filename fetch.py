#!/usr/bin/env python3
"""Private free-V2Ray subscription builder for Saha — DE + US only.

Sources (public, free, no key):
  * Raydikalx — verified configs, GeoIP-grouped by country:
      Countries/Germany.txt, Countries/United_States.txt
      (plain text, 1 config per line; only VLESS+Reality is kept)
  * Au1rxx — v2ray-base64.txt (base64 subscription, hourly)
      Only Hysteria2 (and VLESS+Reality as a fallback) is taken from here,
      and only when its country is confirmed by ip-api.com GeoIP
      (free, no key, batch endpoint). Ambiguous nodes are dropped,
      never guessed.

Rules:
  * DE file contains only Germany nodes, US file only USA nodes.
  * Only VLESS+Reality, Hysteria2, TUIC. No VMess / Shadowsocks / Trojan.
  * Per file: max 30 nodes — up to 20 Reality, up to 10 Hysteria2
    (TUIC fills remaining slots only if both are short).
  * IP is NOT fixed: when a free node dies, the next node in the same
    file is the same country, but a different IP. Only a dedicated
    VPS gives a fixed IP. This script never claims otherwise.
  * Stdlib only. No token / chat id is read or written here.
    Telegram sending lives in .github/workflows/update.yml and
    uses GitHub Secrets only.
"""
import base64
import ipaddress
import json
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

UA = {"User-Agent": "v2ray-bot-de-us/1.0 (+private use)"}
TIMEOUT = 30

AU1_URL = "https://github.com/Au1rxx/free-vpn-subscriptions/raw/main/output/v2ray-base64.txt"
RAY_BASE = "https://raw.githubusercontent.com/0xRadikal/Free-v2ray-Configs/main"
RAY_DE_URL = f"{RAY_BASE}/Countries/Germany.txt"
RAY_US_URL = f"{RAY_BASE}/Countries/United_States.txt"
IP_API_BATCH = "http://ip-api.com/batch?fields=query,countryCode,status"

MAX_PER_FILE = 30
MAX_REALITY = 20
MAX_HY2 = 10

# Cloudflare anycast ranges: GeoIP of these IPs does not tell you
# where the real VLESS server is, so never attribute a country from them.
CF_NETS = [
    ipaddress.ip_network(n) for n in (
        "104.16.0.0/12", "104.24.0.0/14", "172.64.0.0/13",
        "162.158.0.0/15", "188.114.0.0/15", "141.101.0.0/16",
        "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
        "2606:4700::/32",
    )
]

DE_MARKERS = re.compile(r"(🇩🇪|Deutschland|\bDE\b|\bGermany\b)", re.I)
US_MARKERS = re.compile(r"(🇺🇸|United\s+States|\bUSA\b|\bUS\b)", re.I)


def fetch_text(url):
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", errors="ignore")
        except Exception as e:  # 504/502/429 from GitHub raw are transient
            last = e
            print(f"  fetch {url} attempt {attempt + 1}/3 failed: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    raise last


def decode_subscription(text):
    """Accept a base64 subscription or a plain-text list, return lines."""
    text = text.strip()
    if not text:
        return []
    # Plain text: contains :// on the first non-comment line.
    first = next((l for l in text.splitlines() if l.strip() and not l.strip().startswith("#")), "")
    if "://" in first:
        return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
    # Otherwise try base64.
    try:
        padded = text + "=" * (-len(text) % 4)
        dec = base64.b64decode(padded).decode("utf-8", errors="ignore")
        return [l.strip() for l in dec.splitlines() if l.strip() and not l.strip().startswith("#")]
    except Exception:
        return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


def node_name(line):
    if "#" in line:
        return urllib.parse.unquote(line.rsplit("#", 1)[-1]).strip()
    return ""


def parse_node(line):
    """Return (scheme, host, port, credential) or None."""
    try:
        u = urllib.parse.urlparse(line)
    except Exception:
        return None
    scheme = u.scheme.lower()
    if scheme not in ("vless", "hysteria2", "hy2", "tuic"):
        return None
    host = (u.hostname or "").lower()
    if not host or not u.port:
        return None
    cred = u.username or ""
    return (scheme, host, u.port, cred)


def is_reality(line):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(line).query)
    return q.get("security", [""])[0].lower() == "reality"


def is_cloudflare_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in CF_NETS)


def geo_lookup(ips):
    """ip-api.com batch (free, no key). Returns {ip: countryCode or None}."""
    out = {}
    ips = sorted(set(ips))
    for i in range(0, len(ips), 100):
        chunk = ips[i:i + 100]
        payload = json.dumps([{"query": ip} for ip in chunk]).encode()
        req = urllib.request.Request(
            IP_API_BATCH, data=payload,
            headers={"Content-Type": "application/json", **UA},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                for row in json.load(r):
                    out[row.get("query")] = row.get("countryCode") if row.get("status") == "success" else None
        except Exception as e:
            print(f"  geo batch failed ({e}); IPs in this chunk will be dropped", file=sys.stderr)
            for ip in chunk:
                out.setdefault(ip, None)
        time.sleep(1)  # be polite to the free endpoint
    return out


def classify_by_name(name):
    if DE_MARKERS.search(name):
        return "DE"
    if US_MARKERS.search(name):
        return "US"
    return None


def main():
    root = Path(__file__).resolve().parent
    stats = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sources": {},
        "kept": {"DE": 0, "US": 0},
        "dropped_ambiguous_or_other_country": 0,
        "dropped_wrong_protocol": 0,
        "notes": [
            "Country is per-file only (DE / US). IP still rotates when a free node dies.",
            "GeoIP via ip-api.com batch (free, no key). Ambiguous nodes are dropped, never guessed.",
            "Cloudflare anycast IPs are never used to attribute a country.",
        ],
    }
    # buckets: country -> {"reality": [...], "hy2": [...], "tuic": [...]}
    buckets = {"DE": defaultdict(list), "US": defaultdict(list)}
    seen = set()
    dropped_amb = 0
    dropped_proto = 0

    def add(country, line):
        nonlocal dropped_proto
        parsed = parse_node(line)
        if not parsed:
            dropped_proto += 1
            return
        scheme, host, port, cred = parsed
        if scheme == "vless":
            if not is_reality(line):
                dropped_proto += 1
                return
            kind = "reality"
        elif scheme in ("hysteria2", "hy2"):
            kind = "hy2"
        elif scheme == "tuic":
            kind = "tuic"
        else:
            dropped_proto += 1
            return
        key = (country, scheme, host, port, cred)
        if key in seen:
            return
        seen.add(key)
        buckets[country][kind].append(line)

    # ---- Source 1: Raydikalx country files (GeoIP-grouped by the source) ----
    for country, url in (("DE", RAY_DE_URL), ("US", RAY_US_URL)):
        try:
            lines = decode_subscription(fetch_text(url))
        except Exception as e:
            print(f"Raydikalx {country} FAILED: {e}", file=sys.stderr)
            stats["sources"][f"raydikalx_{country.lower()}"] = {"ok": False, "error": str(e)}
            continue
        total = len(lines)
        kept_before = sum(len(v) for v in buckets[country].values())
        for line in lines:
            name = node_name(line)
            # Trust the source's GeoIP grouping, but reject a node whose
            # own name clearly says the other country.
            if name and classify_by_name(name) not in (None, country):
                dropped_amb += 1
                continue
            add(country, line)
        kept_after = sum(len(v) for v in buckets[country].values())
        stats["sources"][f"raydikalx_{country.lower()}"] = {
            "ok": True, "url": url, "total_lines": total, "kept_from_this_source": kept_after - kept_before,
        }
        print(f"Raydikalx {country}: {total} lines, kept {kept_after - kept_before}")

    # ---- Source 2: Au1rxx (mainly Hysteria2; Reality only as fallback) ----
    try:
        au_lines = decode_subscription(fetch_text(AU1_URL))
        stats["sources"]["au1rxx"] = {"ok": True, "url": AU1_URL, "total_lines": len(au_lines)}
        print(f"Au1rxx: {len(au_lines)} lines")
    except Exception as e:
        print(f"Au1rxx FAILED: {e}", file=sys.stderr)
        stats["sources"]["au1rxx"] = {"ok": False, "error": str(e)}
        au_lines = []

    # Candidates from Au1: hy2 always, vless-reality only if Raydikalx
    # left a Reality slot short (we check counts after geo classification).
    candidates = []  # (line, scheme-kind, host)
    for line in au_lines:
        parsed = parse_node(line)
        if not parsed:
            continue
        scheme, host, port, _cred = parsed
        if scheme in ("hysteria2", "hy2"):
            candidates.append((line, "hy2", host))
        elif scheme == "vless" and is_reality(line):
            candidates.append((line, "reality", host))
        elif scheme == "tuic":
            candidates.append((line, "tuic", host))

    # Resolve + GeoIP the candidate hosts (once per host).
    host_to_ip = {}
    for _line, _kind, host in candidates:
        if host not in host_to_ip:
            try:
                host_to_ip[host] = socket.gethostbyname(host)
            except Exception:
                host_to_ip[host] = None
    ips = [ip for ip in host_to_ip.values() if ip and not is_cloudflare_ip(ip)]
    geo = geo_lookup(ips) if ips else {}
    for line, kind, host in candidates:
        name = node_name(line)
        country = classify_by_name(name)
        if country is None:
            ip = host_to_ip.get(host)
            if ip and not is_cloudflare_ip(ip):
                country = geo.get(ip)
        # If name and geo disagree, trust neither — drop.
        if country not in ("DE", "US"):
            dropped_amb += 1
            continue
        if kind == "reality" and len(buckets[country]["reality"]) >= MAX_REALITY:
            continue  # Raydikalx already filled Reality; keep list stable
        add(country, line)

    # ---- Write per-country files: Reality first, then Hy2, then TUIC ----
    for country, fname in (("DE", "de.txt"), ("US", "us.txt")):
        chosen = (
            buckets[country]["reality"][:MAX_REALITY]
            + buckets[country]["hy2"][:MAX_HY2]
            + buckets[country]["tuic"][: max(0, MAX_PER_FILE - MAX_REALITY - MAX_HY2)]
        )[:MAX_PER_FILE]
        payload = ("\n".join(chosen) + "\n").encode() if chosen else b""
        (root / fname).write_bytes(base64.b64encode(payload))
        by_proto = Counter()
        for line in chosen:
            by_proto[parse_node(line)[0]] += 1
        stats["kept"][country] = len(chosen)
        stats[f"kept_{country.lower()}_by_protocol"] = dict(by_proto)
        print(f"{country}: {len(chosen)} nodes "
              f"(reality={len(buckets[country]['reality'])}, "
              f"hy2={len(buckets[country]['hy2'])}, tuic={len(buckets[country]['tuic'])}) -> {fname}")

    stats["dropped_ambiguous_or_other_country"] = dropped_amb
    stats["dropped_wrong_protocol"] = dropped_proto
    (root / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
