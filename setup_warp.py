#!/usr/bin/env python3
"""
Cloudflare WARP WireGuard Tunnel Setup with Split-Tunneling
============================================================
Routes traffic destined for Algerian ENPI servers (197.112.125.0/24)
through Cloudflare's Anycast edge network to bypass datacenter IP geoblocks.
All other traffic (GitHub, PyPI, Telegram, git) remains direct.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def is_wg_active() -> bool:
    try:
        res = subprocess.run(["sudo", "wg", "show", "wg0"], capture_output=True, text=True)
        return res.returncode == 0 and "interface: wg0" in res.stdout
    except Exception:
        return False


def setup_warp() -> bool:
    if is_wg_active():
        print("[WARP] WireGuard interface wg0 is already active.")
        return True

    # 1. Install wireguard-tools if missing
    if not shutil.which("wg-quick"):
        print("[WARP] Installing wireguard-tools and resolvconf...")
        subprocess.run(["sudo", "apt-get", "update", "-qq"], check=True)
        subprocess.run(["sudo", "apt-get", "install", "-y", "--no-install-recommends", "wireguard-tools", "resolvconf"], check=True)

    cache_dir = Path.home() / ".warp-wireguard"
    cache_dir.mkdir(parents=True, exist_ok=True)
    conf_file = cache_dir / "wg0.conf"

    # 2. Generate configuration if not already cached
    if not conf_file.exists():
        print("[WARP] Profile not in cache. Downloading wgcf...")
        wgcf_bin = cache_dir / "wgcf"
        if not wgcf_bin.exists():
            import httpx
            url = "https://github.com/ViRb3/wgcf/releases/download/v2.3.1/wgcf_2.3.1_linux_amd64"
            r = httpx.get(url, follow_redirects=True, timeout=30)
            if r.status_code != 200 or len(r.content) < 1000000:
                raise RuntimeError(f"Failed to download wgcf binary: HTTP {r.status_code}")
            wgcf_bin.write_bytes(r.content)
            wgcf_bin.chmod(0o755)

        print("[WARP] Registering Cloudflare WARP account...")
        subprocess.run([str(wgcf_bin), "register", "--accept-tos"], cwd=cache_dir, check=True)
        subprocess.run([str(wgcf_bin), "generate"], cwd=cache_dir, check=True)

        raw_profile = cache_dir / "wgcf-profile.conf"
        content = raw_profile.read_text()

        # Split tunnel patch:
        # - Remove DNS lines to keep runner system DNS untouched
        # - Keep only IPv4 Address and Endpoint
        # - Set AllowedIPs = 197.112.125.0/24 for Algerian ENPI subnet
        new_lines = []
        for line in content.splitlines():
            line_s = line.strip()
            if line_s.startswith("DNS"):
                continue
            if line_s.startswith("Address"):
                parts = line.split("=", 1)[1].split(",")
                ipv4_addr = [p.strip() for p in parts if ":" not in p][0]
                new_lines.append(f"Address = {ipv4_addr}")
            elif line_s.startswith("AllowedIPs"):
                new_lines.append("AllowedIPs = 197.112.125.0/24")
            elif line_s.startswith("Endpoint"):
                new_lines.append("Endpoint = 162.159.192.1:2408")
            else:
                new_lines.append(line)
        new_lines.append("PersistentKeepalive = 25")

        conf_file.write_text("\n".join(new_lines) + "\n")
        print("[WARP] Generated split-tunnel wg0.conf successfully.")

    # 3. Bring up WireGuard interface
    subprocess.run(["sudo", "mkdir", "-p", "/etc/wireguard"], check=True)
    subprocess.run(["sudo", "cp", str(conf_file), "/etc/wireguard/wg0.conf"], check=True)
    subprocess.run(["sudo", "chmod", "600", "/etc/wireguard/wg0.conf"], check=True)
    subprocess.run(["sudo", "wg-quick", "up", "wg0"], check=True)

    time.sleep(1)
    if is_wg_active():
        print("[WARP] Cloudflare WARP tunnel connected successfully!")
        subprocess.run(["sudo", "wg", "show", "wg0"])
        return True
    else:
        print("[WARP] Warning: wg0 interface did not report active status.")
        return False


if __name__ == "__main__":
    success = setup_warp()
    sys.exit(0 if success else 1)
