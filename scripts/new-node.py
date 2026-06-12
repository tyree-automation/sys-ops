#!/usr/bin/env python3
"""
sys-ops node onboarding.

Interactively registers a node in the inventory: picks its environment
(test / development / production), asks which optional components to
deploy (Tailscale, dn42 router), writes inventory/hosts.yml and
inventory/host_vars/<node>.yml, and offers to run the onboarding
playbook immediately.

Usage:  scripts/new-node.py
"""

import ipaddress
import os
import re
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required (it ships with Ansible): pip install pyyaml")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSTS_FILE = os.path.join(REPO_ROOT, "inventory", "hosts.yml")
HOST_VARS_DIR = os.path.join(REPO_ROOT, "inventory", "host_vars")

ENVIRONMENTS = ("test", "development", "production")
COMPONENT_GROUPS = ("dn42", "website", "autopeer")
LIFECYCLE_GROUPS = ("retiring",)

HOSTS_HEADER = """\
# =============================================================================
# Fleet inventory — MANAGED BY scripts/new-node.py
#
# Environment groups (every node belongs to exactly one):
#   test         — throwaway / experiment boxes
#   development  — dev and staging machines
#   production   — live service hosts
#
# Component groups (optional, a node may be in any number):
#   dn42         — node runs the dn42 router stack (WireGuard + BIRD2)
#   website      — node serves the fleet dashboard (playbooks/website.yml)
#   autopeer     — node runs the automatic peering API (needs dn42)
#
# Lifecycle groups:
#   retiring     — queued for playbooks/decommission.yml
#
# Add nodes with scripts/new-node.py rather than editing this file by hand;
# per-node settings live in inventory/host_vars/<node>.yml.
# =============================================================================
"""

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def c(code, text):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def bold(t):
    return c("1", t)


def dim(t):
    return c("2", t)


def green(t):
    return c("32", t)


def yellow(t):
    return c("33", t)


def cyan(t):
    return c("36", t)


def header(title):
    print()
    print(bold(cyan(f"── {title} " + "─" * max(0, 60 - len(title)))))


def die(msg):
    sys.exit(c("31", f"error: {msg}"))


def ask(prompt, default=None, validate=None, required=True):
    """Prompt until `validate` (returns error string or None) passes."""
    suffix = dim(f" [{default}]") if default not in (None, "") else ""
    while True:
        try:
            value = input(f"{bold(prompt)}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit("aborted")
        if not value:
            if default is not None:
                value = default
            elif not required:
                return ""
            else:
                print(yellow("  a value is required"))
                continue
        if validate:
            err = validate(value)
            if err:
                print(yellow(f"  {err}"))
                continue
        return value


def ask_yes_no(prompt, default=False):
    hint = "Y/n" if default else "y/N"
    while True:
        try:
            value = input(f"{bold(prompt)} {dim('[' + hint + ']')}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit("aborted")
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        print(yellow("  please answer y or n"))


def ask_choice(prompt, choices, descriptions):
    print(bold(prompt))
    for i, name in enumerate(choices, 1):
        print(f"  {cyan(str(i))}) {name:<12} {dim(descriptions[name])}")
    while True:
        try:
            value = input(f"{bold('choice')} {dim('[1-' + str(len(choices)) + ']')}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit("aborted")
        if value.isdigit() and 1 <= int(value) <= len(choices):
            return choices[int(value) - 1]
        if value in choices:
            return value
        print(yellow("  pick a number from the list"))


def valid_node_name(name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        return "use lowercase letters, digits and dashes (start with a letter/digit)"
    if os.path.exists(os.path.join(HOST_VARS_DIR, f"{name}.yml")):
        return f"host_vars/{name}.yml already exists — pick another name"
    return None


def valid_address(value):
    """ansible_host can be an IP or a resolvable name."""
    try:
        ipaddress.ip_address(value)
        return None
    except ValueError:
        pass
    if re.fullmatch(r"[A-Za-z0-9.-]+", value):
        return None
    return "enter an IPv4/IPv6 address or a hostname"


def valid_port(value):
    if value.isdigit() and 1 <= int(value) <= 65535:
        return None
    return "enter a port number (1-65535)"


def valid_coords(value):
    parts = value.split(",")
    if len(parts) != 2:
        return "use lat,lon — e.g. 47.606,-122.332"
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return "use lat,lon — e.g. 47.606,-122.332"
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return "latitude must be -90..90, longitude -180..180"
    return None


def make_ip_validator(version):
    def validate(value):
        try:
            addr = ipaddress.ip_address(value)
        except ValueError:
            return f"not a valid IPv{version} address"
        if addr.version != version:
            return f"not a valid IPv{version} address"
        return None

    return validate


def load_inventory():
    if not os.path.exists(HOSTS_FILE):
        data = {}
    else:
        with open(HOSTS_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    children = data.setdefault("all", {}).setdefault("children", {})
    for group in ENVIRONMENTS + COMPONENT_GROUPS + LIFECYCLE_GROUPS:
        group_data = children.setdefault(group, {})
        if not isinstance(group_data.get("hosts"), dict):
            group_data["hosts"] = {}
    return data


def existing_nodes(inventory):
    nodes = set()
    for group_data in inventory["all"]["children"].values():
        nodes.update(group_data.get("hosts") or {})
    return nodes


def save_inventory(inventory):
    body = yaml.safe_dump(inventory, default_flow_style=False, sort_keys=False)
    with open(HOSTS_FILE, "w", encoding="utf-8") as f:
        f.write(HOSTS_HEADER + body)


def write_host_vars(node):
    lines = [
        f"# Node settings for {node['name']} — generated by scripts/new-node.py.",
        "# Safe to edit by hand; re-run playbooks after changes.",
        "---",
        f"ansible_host: {node['ansible_host']}",
        f"ansible_user: {node['ansible_user']}",
    ]
    if node["ansible_port"] != "22":
        lines.append(f"ansible_port: {node['ansible_port']}")
    lines += [
        "",
        "# --- Components ---",
        f"tailscale_enabled: {'true' if node['tailscale'] else 'false'}",
    ]
    if node.get("website"):
        lines += [
            "# Dashboard flavor served by this node — 'public' (dn42 info only)",
            "# or 'internal' (full fleet view; trusted networks ONLY).",
            f"website_mode: {node['website_mode']}",
        ]
    if node.get("location"):
        loc = node["location"]
        lines += [
            "",
            "# --- Dashboard map position (scripts/build-site.py) ---",
            "site_location:",
            f"  lat: {loc['lat']}",
            f"  lon: {loc['lon']}",
            f"  label: \"{loc['label']}\"",
        ]
    if node["dn42"]:
        lines += [
            "",
            "# --- dn42 router ---",
            "# Network identity (ASN, prefixes) lives in inventory/group_vars/dn42.yml.",
            f"dn42_ownip: {node['dn42_ownip']}",
            f"dn42_ownip6: {node['dn42_ownip6']}",
            f"dn42_link_local6: \"{node['dn42_link_local6']}\"",
        ]
        if node.get("dn42_public_endpoint"):
            lines += [
                "# Endpoint advertised to peers on the public dashboard:",
                f"dn42_public_endpoint: \"{node['dn42_public_endpoint']}\"",
            ]
        if node.get("autopeer"):
            lines += [
                "# Peering API base URL shown on the public dashboard:",
                f"dn42_autopeer_url: \"{node['dn42_autopeer_url']}\"",
            ]
        lines += [
            "",
            "# Peerings — fill these in, then run: ansible-playbook playbooks/dn42.yml -l "
            + node["name"],
            "# Schema: roles/dn42/defaults/main.yml",
            "dn42_peers: []",
            "#  - name: example",
            "#    asn: 4242421234",
            "#    wg_pubkey: \"AAAA...=\"",
            "#    wg_endpoint: \"peer.example.com:51820\"",
            "#    peer_v6: \"fe80::1234\"",
        ]
    path = os.path.join(HOST_VARS_DIR, f"{node['name']}.yml")
    os.makedirs(HOST_VARS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def main():
    print(bold(cyan("sys-ops — register a new node")))
    print(dim("Ctrl-C at any point to abort; nothing is written until you confirm."))

    inventory = load_inventory()
    taken = existing_nodes(inventory)

    header("Node identity")
    name = ask(
        "node name (inventory hostname, e.g. web-01)",
        validate=lambda v: ("already in the inventory" if v in taken else None)
        or valid_node_name(v),
    )
    ansible_host = ask("SSH address (IP or DNS name)", validate=valid_address)
    ansible_user = ask("SSH user", default="root")
    ansible_port = ask("SSH port", default="22", validate=valid_port)

    header("Environment")
    environment = ask_choice(
        "Which environment does this node belong to?",
        ENVIRONMENTS,
        {
            "test": "throwaway / experiments — auto-reboots on updates",
            "development": "dev & staging",
            "production": "live services — conservative maintenance",
        },
    )

    header("Optional components")
    tailscale = ask_yes_no("Join this node to your tailnet (Tailscale)?", default=False)
    print()
    print(dim("  The dn42 router stack (WireGuard + BIRD2 + ROA sync) is typically"))
    print(dim("  something you'd put on a test node, but it's available everywhere."))
    dn42 = ask_yes_no("Deploy the dn42 router stack on this node?", default=False)
    print()
    website = ask_yes_no(
        "Serve the fleet web dashboard from this node (nginx)?", default=False
    )
    website_mode = "public"
    if website:
        website_mode = ask_choice(
            "Which dashboard flavor should this node serve?",
            ("public", "internal"),
            {
                "public": "dn42 info only — safe for the open internet",
                "internal": "full fleet view — trusted networks ONLY",
            },
        )

    header("Map location")
    print(dim("  Optional — places the node on the dashboard's world map."))
    location = None
    coords = ask(
        "coordinates as lat,lon (blank to skip)",
        required=False,
        validate=valid_coords,
    )
    if coords:
        lat, lon = (part.strip() for part in coords.split(","))
        label = ask("location label (e.g. Seattle, US)", required=False)
        location = {"lat": lat, "lon": lon, "label": label}

    node = {
        "name": name,
        "ansible_host": ansible_host,
        "ansible_user": ansible_user,
        "ansible_port": ansible_port,
        "environment": environment,
        "tailscale": tailscale,
        "dn42": dn42,
        "website": website,
        "website_mode": website_mode,
        "location": location,
    }

    if dn42:
        header("dn42 addressing")
        print(dim("  From your registered prefixes (see inventory/group_vars/dn42.yml)."))
        node["dn42_ownip"] = ask(
            "node dn42 IPv4 (e.g. 172.20.0.1)", validate=make_ip_validator(4)
        )
        node["dn42_ownip6"] = ask(
            "node dn42 IPv6 (e.g. fd00:1234::1)", validate=make_ip_validator(6)
        )
        node["dn42_link_local6"] = ask(
            "tunnel link-local IPv6", default="fe80::42", validate=make_ip_validator(6)
        )
        node["dn42_public_endpoint"] = ask(
            "public endpoint shown to peers (host/IP, blank to skip)",
            required=False,
        )
        print()
        print(dim("  The automatic peering API lets other dn42 networks request a"))
        print(dim("  peering via the public dashboard; you review with"))
        print(dim("  scripts/peering-requests.py (or enable auto-apply later)."))
        node["autopeer"] = ask_yes_no(
            "Run the automatic peering API on this node?", default=False
        )
        if node["autopeer"]:
            if website and website_mode == "public":
                default_url = "/api"
            elif node["dn42_public_endpoint"]:
                default_url = f"http://{node['dn42_public_endpoint']}:8042/api"
            else:
                default_url = None
            node["dn42_autopeer_url"] = ask(
                "peering API URL shown on the dashboard", default=default_url
            )

    header("Summary")
    print(f"  node:        {bold(name)}")
    print(f"  ssh:         {ansible_user}@{ansible_host}:{ansible_port}")
    print(f"  environment: {environment}")
    components = [
        n for n, on in (
            ("tailscale", tailscale),
            ("dn42", dn42),
            ("autopeer", node.get("autopeer", False)),
            (f"website ({website_mode})", website),
        ) if on
    ]
    print(f"  components:  {', '.join(components) if components else dim('none')}")
    if dn42:
        print(f"  dn42 addrs:  {node['dn42_ownip']} / {node['dn42_ownip6']}")
    if location:
        print(f"  map:         {location['lat']},{location['lon']}"
              + (f" ({location['label']})" if location['label'] else ""))
    print()
    if not ask_yes_no("Write inventory entries?", default=True):
        sys.exit("aborted — nothing written")

    children = inventory["all"]["children"]
    children[environment]["hosts"][name] = None
    if dn42:
        children["dn42"]["hosts"][name] = None
    if website:
        children["website"]["hosts"][name] = None
    if node.get("autopeer"):
        children["autopeer"]["hosts"][name] = None
    save_inventory(inventory)
    host_vars_path = write_host_vars(node)

    groups = [environment] + [
        g for g, on in (
            ("dn42", dn42),
            ("autopeer", node.get("autopeer", False)),
            ("website", website),
        ) if on
    ]
    print()
    print(green("✓") + f" inventory/hosts.yml updated ({', '.join(groups)})")
    print(green("✓") + f" {os.path.relpath(host_vars_path, REPO_ROOT)} written")

    header("Next steps")
    if dn42:
        print(yellow("  dn42 checklist:"))
        print("   - set your ASN/prefixes in inventory/group_vars/dn42.yml (once)")
        print(f"   - add peerings under dn42_peers in inventory/host_vars/{name}.yml")
        print("   - onboarding prints the node's WireGuard public key — share it")
        print("     with your peers")
        if node.get("autopeer"):
            print("   - review incoming requests: scripts/peering-requests.py list")
    if website:
        print(yellow("  dashboard checklist:"))
        print("   - customize branding/panels in site.yml (once)")
        if website_mode == "public":
            print("   - fill in the public peering card under 'public:' in site.yml")
        print("   - build the datasets: scripts/build-site.py  (--probe for live status)")
        print("   - deploy/refresh:     ansible-playbook playbooks/website.yml")
        print(f"   - this node serves the {bold(website_mode)} flavor"
              + (" (dn42 info only)" if website_mode == "public" else " — trusted networks only!"))
    if tailscale:
        print("   - pass the tailnet key at onboard time:")
        print(dim("       -e tailscale_authkey=tskey-auth-..."))
    print(f"   - onboard now or later with:")
    print(dim(f"       ansible-playbook playbooks/onboard.yml -l {name}"))

    print()
    if ask_yes_no("Run the onboarding playbook now?", default=False):
        cmd = ["ansible-playbook", "playbooks/onboard.yml", "-l", name]
        if tailscale:
            authkey = ask("tailscale auth key (blank to skip joining)", required=False)
            if authkey:
                cmd += ["-e", f"tailscale_authkey={authkey}"]
        print(dim("  $ " + " ".join(cmd[:5]) + (" -e tailscale_authkey=***" if tailscale and len(cmd) > 5 else "")))
        raise SystemExit(subprocess.call(cmd, cwd=REPO_ROOT))

    print(green("done."))


if __name__ == "__main__":
    main()
