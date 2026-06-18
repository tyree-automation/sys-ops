#!/usr/bin/env python3
"""
Build the fleet dashboard datasets from the Ansible inventory and site.yml.

Always produces two variants under build/:
  fleet-internal.json — full fleet view (environments, components, SSH
                        addresses, ops commands). For trusted networks only.
  fleet-public.json   — dn42 routers only: name, location, status, dn42
                        addressing + peerings, optional peering contact
                        card. Safe to publish.

Each website node deploys exactly one variant, chosen by its website_mode
host var (default: public) — see roles/website.

Usage:
  scripts/build-site.py             # build both datasets
  scripts/build-site.py --probe     # also ping nodes to record up/down
  scripts/build-site.py --demo      # build with bundled demo fleet
  scripts/build-site.py --serve [--mode public|internal]
                                    # preview on http://localhost:8080
                                    # (default preview mode: public)

Deploy afterwards:
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
BUILD_DIR = os.path.join(REPO_ROOT, "build")
WEB_ROOT = os.path.join(REPO_ROOT, "web")

ENVIRONMENTS = ("test", "development", "production")
COMPONENT_GROUPS = ("dn42", "website", "autopeer")

DEMO_NODES = [
    {
        "name": "lab-sea", "environment": "test", "components": ["dn42"],
        "address": "203.0.113.10", "status": "up", "retiring": False,
        "location": {"lat": 47.606, "lon": -122.332, "label": "Seattle, US"},
        "dn42": {"ownip": "172.20.0.1", "ownip6": "fd00:1234::1",
                 "endpoint": "sea.dn42.example.com", "autopeer": "/api", "peers": [
            {"name": "alpha", "asn": 4242421234, "endpoint": "alpha.example.com:51820"},
            {"name": "beta", "asn": 4242425678, "endpoint": ""},
        ]},
    },
    {
        "name": "lab-tyo", "environment": "test", "components": ["dn42"],
        "address": "203.0.113.20", "status": "up", "retiring": False,
        "location": {"lat": 35.676, "lon": 139.650, "label": "Tokyo, JP"},
        "dn42": {"ownip": "172.20.0.2", "ownip6": "fd00:1234::2",
                 "endpoint": "tyo.dn42.example.com", "autopeer": "", "peers": [
            {"name": "gamma", "asn": 4242429012, "endpoint": "gamma.example.net:21080"},
        ]},
    },
    {
        "name": "dev-fra", "environment": "development", "components": [],
        "address": "203.0.113.30", "status": "unknown", "retiring": False,
        "location": {"lat": 50.110, "lon": 8.682, "label": "Frankfurt, DE"},
    },
    {
        "name": "web-nyc", "environment": "production", "components": ["website"],
        "address": "203.0.113.40", "status": "up", "retiring": False,
        "location": {"lat": 40.712, "lon": -74.006, "label": "New York, US"},
    },
]

DEMO_PUBLIC = {
    "asn": "AS4242420000",
    "contact": "you@example.com",
    "policy": "Open peering — pick the closest node and get in touch.",
}


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
    inline_vars = {}
    for group, group_data in children.items():
        for host, hvars in ((group_data or {}).get("hosts") or {}).items():
            memberships.setdefault(host, set()).add(group)
            if hvars:
                inline_vars.setdefault(host, {}).update(hvars)

    nodes = []
    for host, groups in sorted(memberships.items()):
        # Inventory inline vars (written by add-server.py: ansible_host,
        # server_env, clli, ...) merged with any per-node host_vars/<node>.yml
        # (dn42 addressing, peers, map location).
        host_vars = dict(inline_vars.get(host, {}))
        host_vars.update(load_yaml(os.path.join(HOST_VARS_DIR, f"{host}.yml"), {}) or {})
        node = {
            "name": host,
            "environment": str(
                host_vars.get("server_env")
                or next((e for e in ENVIRONMENTS if e in groups), "unassigned")
            ),
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
                # Public WireGuard endpoint host advertised to peers (optional).
                "endpoint": str(host_vars.get("dn42_public_endpoint", "")),
                # Base URL of the automatic peering API (e.g. "/api" when
                # proxied by the website role, or "http://host:8042/api").
                "autopeer": str(host_vars.get("dn42_autopeer_url", ""))
                if "autopeer" in groups else "",
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
    print("probing nodes (ansible -m ping)...")
    proc = subprocess.run(
        ["ansible", "all", "-m", "ping", "--one-line", "-f", "20"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    results = {}
    for line in proc.stdout.splitlines():
        host = line.split(" ", 1)[0].rstrip(":")
        results[host] = "up" if "SUCCESS" in line else "down"
    for node in nodes:
        node["status"] = results.get(node["name"], "unknown")


def public_nodes(nodes):
    """dn42-only view: keep nothing about the fleet itself."""
    out = []
    for node in nodes:
        if "dn42" not in node.get("components", []):
            continue
        public = {
            "name": node["name"],
            "status": node["status"],
            "components": ["dn42"],
            "dn42": node.get("dn42", {}),
        }
        if node.get("location"):
            public["location"] = node["location"]
        out.append(public)
    return out


def build_links(nodes, site, include_custom):
    """dn42 mesh links + (internal only) custom links from site.yml."""
    links = []
    located = {n["name"] for n in nodes if n.get("location")}
    if (site.get("map") or {}).get("mesh_links", True):
        dn42_nodes = sorted(
            n["name"] for n in nodes
            if "dn42" in n.get("components", []) and n["name"] in located
        )
        for i, a in enumerate(dn42_nodes):
            for b in dn42_nodes[i + 1:]:
                links.append({"from": a, "to": b, "type": "dn42", "label": "dn42 mesh"})
    if include_custom:
        for link in site.get("links") or []:
            if link.get("from") in located and link.get("to") in located:
                links.append({
                    "from": link["from"], "to": link["to"],
                    "type": link.get("type", "custom"), "label": link.get("label", ""),
                })
    return links


def site_meta(site, mode, generated, public_info):
    meta = {
        "mode": mode,
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
    }
    if mode == "public":
        meta["tagline"] = ""  # internal environment names stay internal
        meta["subtitle"] = "dn42"
        meta["public"] = {k: v for k, v in (public_info or {}).items() if v}
    return meta


def write_dataset(path, dataset):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dataset, f, indent=2)
        f.write("\n")


def serve(mode):
    """Preview web/ with the chosen dataset mapped onto /data/fleet.json."""
    dataset_path = os.path.join(BUILD_DIR, f"fleet-{mode}.json")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=WEB_ROOT, **kwargs)

        def translate_path(self, path):
            if path.split("?", 1)[0] == "/data/fleet.json":
                return dataset_path
            return super().translate_path(path)

    print(f"previewing {mode} site at http://localhost:8080 (Ctrl-C to stop)")
    http.server.ThreadingHTTPServer(("", 8080), Handler).serve_forever()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="use bundled demo fleet")
    parser.add_argument("--probe", action="store_true", help="ping nodes for status")
    parser.add_argument("--serve", action="store_true", help="preview on :8080 after build")
    parser.add_argument("--mode", choices=("public", "internal"), default="public",
                        help="which dataset --serve previews (default: public)")
    args = parser.parse_args()

    site = load_yaml(SITE_CONFIG, {}) or {}

    if args.demo:
        nodes = DEMO_NODES
        public_info = DEMO_PUBLIC
    else:
        nodes = collect_nodes()
        public_info = site.get("public") or {}
        if args.probe:
            probe_status(nodes)

    generated = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    internal_nodes = [dict(n) for n in nodes]
    if not site.get("show_addresses", True):
        for node in internal_nodes:
            node.pop("address", None)

    variants = {
        "internal": {
            "generated": generated,
            "site": site_meta(site, "internal", generated, None),
            "nodes": internal_nodes,
            "links": build_links(nodes, site, include_custom=True),
        },
        "public": {
            "generated": generated,
            "site": site_meta(site, "public", generated, public_info),
            "nodes": public_nodes(nodes),
            "links": build_links(public_nodes(nodes), site, include_custom=False),
        },
    }

    for mode, dataset in variants.items():
        path = os.path.join(BUILD_DIR, f"fleet-{mode}.json")
        write_dataset(path, dataset)
        print(f"wrote {os.path.relpath(path, REPO_ROOT)}: "
              f"{len(dataset['nodes'])} nodes, {len(dataset['links'])} links")

    if args.serve:
        serve(args.mode)


if __name__ == "__main__":
    main()
