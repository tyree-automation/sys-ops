#!/usr/bin/env python3
"""
Build the fleet dashboard dataset (web/data/fleet.json) from the Ansible
inventory and site.yml.

Usage:
  scripts/build-site.py             # build from inventory + site.yml
  scripts/build-site.py --probe     # also ping nodes to record up/down
  scripts/build-site.py --demo      # build with bundled demo fleet
  scripts/build-site.py --serve     # build, then preview on http://localhost:8080

Deploy to a node in the 'website' group afterwards:
  ansible-playbook playbooks/website.yml
"""

import argparse
import datetime
import http.server
import json
import os
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required (it ships with Ansible): pip install pyyaml")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE_CONFIG = os.path.join(REPO_ROOT, "site.yml")
HOSTS_FILE = os.path.join(REPO_ROOT, "inventory", "hosts.yml")
HOST_VARS_DIR = os.path.join(REPO_ROOT, "inventory", "host_vars")
OUTPUT = os.path.join(REPO_ROOT, "web", "data", "fleet.json")
WEB_ROOT = os.path.join(REPO_ROOT, "web")

ENVIRONMENTS = ("test", "development", "production")
COMPONENT_GROUPS = ("dn42", "website")

DEMO_NODES = [
    {
        "name": "lab-sea", "environment": "test", "components": ["dn42"],
        "address": "203.0.113.10", "status": "up",
        "location": {"lat": 47.606, "lon": -122.332, "label": "Seattle, US"},
        "dn42": {"ownip": "172.20.0.1", "ownip6": "fd00:1234::1", "peers": [
            {"name": "alpha", "asn": 4242421234, "endpoint": "alpha.example.com:51820"},
            {"name": "beta", "asn": 4242425678, "endpoint": ""},
        ]},
    },
    {
        "name": "lab-tyo", "environment": "test", "components": ["dn42"],
        "address": "203.0.113.20", "status": "up",
        "location": {"lat": 35.676, "lon": 139.650, "label": "Tokyo, JP"},
        "dn42": {"ownip": "172.20.0.2", "ownip6": "fd00:1234::2", "peers": [
            {"name": "gamma", "asn": 4242429012, "endpoint": "gamma.example.net:21080"},
        ]},
    },
    {
        "name": "dev-fra", "environment": "development", "components": [],
        "address": "203.0.113.30", "status": "unknown",
        "location": {"lat": 50.110, "lon": 8.682, "label": "Frankfurt, DE"},
    },
    {
        "name": "web-nyc", "environment": "production", "components": ["website"],
        "address": "203.0.113.40", "status": "up",
        "location": {"lat": 40.712, "lon": -74.006, "label": "New York, US"},
    },
]


def load_yaml(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or default


def collect_nodes():
    """Read nodes + group membership from the inventory."""
    inventory = load_yaml(HOSTS_FILE, {})
    children = ((inventory.get("all") or {}).get("children")) or {}
    memberships = {}
    for group, group_data in children.items():
        for host in (group_data or {}).get("hosts") or {}:
            memberships.setdefault(host, set()).add(group)

    nodes = []
    for host, groups in sorted(memberships.items()):
        host_vars = load_yaml(os.path.join(HOST_VARS_DIR, f"{host}.yml"), {}) or {}
        node = {
            "name": host,
            "environment": next((e for e in ENVIRONMENTS if e in groups), "unassigned"),
            "components": sorted(g for g in groups if g in COMPONENT_GROUPS),
            "address": str(host_vars.get("ansible_host", "")),
            "status": "unknown",
            "retiring": "retiring" in groups,
        }
        if host_vars.get("site_location"):
            loc = host_vars["site_location"]
            node["location"] = {
                "lat": float(loc.get("lat")),
                "lon": float(loc.get("lon")),
                "label": str(loc.get("label", "")),
            }
        if "dn42" in groups:
            node["dn42"] = {
                "ownip": str(host_vars.get("dn42_ownip", "")),
                "ownip6": str(host_vars.get("dn42_ownip6", "")),
                "peers": [
                    {
                        "name": str(p.get("name", "?")),
                        "asn": p.get("asn"),
                        "endpoint": str(p.get("wg_endpoint", "")),
                    }
                    for p in host_vars.get("dn42_peers") or []
                ],
            }
        nodes.append(node)
    return nodes


def probe_status(nodes):
    """Mark nodes up/down using ansible's ping module."""
    if not nodes:
        return
    cmd = ["ansible", "all", "-m", "ping", "--one-line", "-f", "20"]
    print("probing nodes (ansible -m ping)...")
    proc = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    results = {}
    for line in proc.stdout.splitlines():
        host = line.split(" ", 1)[0].rstrip(":")
        results[host] = "up" if "SUCCESS" in line else "down"
    for node in nodes:
        node["status"] = results.get(node["name"], "unknown")


def strip_addresses(nodes):
    for node in nodes:
        node.pop("address", None)
        if "dn42" in node:
            node["dn42"]["ownip"] = ""
            node["dn42"]["ownip6"] = ""
            for peer in node["dn42"]["peers"]:
                peer["endpoint"] = ""


def build_links(nodes, site):
    """dn42 mesh links + custom links from site.yml."""
    links = []
    located = {n["name"] for n in nodes if n.get("location")}
    if (site.get("map") or {}).get("mesh_links", True):
        dn42_nodes = sorted(
            n["name"] for n in nodes if "dn42" in n["components"] and n["name"] in located
        )
        for i, a in enumerate(dn42_nodes):
            for b in dn42_nodes[i + 1:]:
                links.append({"from": a, "to": b, "type": "dn42", "label": "dn42 mesh"})
    for link in site.get("links") or []:
        if link.get("from") in located and link.get("to") in located:
            links.append({
                "from": link["from"], "to": link["to"],
                "type": link.get("type", "custom"), "label": link.get("label", ""),
            })
    return links


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="use bundled demo fleet")
    parser.add_argument("--probe", action="store_true", help="ping nodes for status")
    parser.add_argument("--serve", action="store_true", help="preview on :8080 after build")
    args = parser.parse_args()

    site = load_yaml(SITE_CONFIG, {}) or {}

    if args.demo:
        nodes = DEMO_NODES
    else:
        nodes = collect_nodes()
        if args.probe:
            probe_status(nodes)
    if not site.get("show_addresses", True):
        strip_addresses(nodes)

    generated = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dataset = {
        "generated": generated,
        "site": {
            "title": site.get("title", "sys-ops"),
            "subtitle": site.get("subtitle", "fleet operations"),
            "tagline": site.get("tagline", ""),
            "accent": site.get("accent", "#7c5cff"),
            "footer": str(site.get("footer", "")).replace("{generated}", generated),
            "map": {
                "center": (site.get("map") or {}).get("center", [25, 10]),
                "zoom": (site.get("map") or {}).get("zoom", 2),
                "tiles": (site.get("map") or {}).get("tiles", "auto"),
            },
            "panels": {
                "stats": True, "map": True, "nodes": True, "dn42": True,
                **(site.get("panels") or {}),
            },
        },
        "nodes": nodes,
        "links": build_links(nodes, site),
    }

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2)
        f.write("\n")
    located = sum(1 for n in nodes if n.get("location"))
    print(f"wrote {os.path.relpath(OUTPUT, REPO_ROOT)}: "
          f"{len(nodes)} nodes ({located} on map), {len(dataset['links'])} links")

    if args.serve:
        os.chdir(WEB_ROOT)
        print("previewing at http://localhost:8080 (Ctrl-C to stop)")
        http.server.ThreadingHTTPServer(
            ("", 8080), http.server.SimpleHTTPRequestHandler
        ).serve_forever()


if __name__ == "__main__":
    main()
