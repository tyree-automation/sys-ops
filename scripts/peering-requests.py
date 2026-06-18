#!/usr/bin/env python3
"""
Review dn42 peering requests submitted through the automatic peering API.

Talks to nodes in the 'autopeer' group via ansible ad-hoc commands, so it
uses the same SSH access as your playbooks.

Usage:
  scripts/peering-requests.py list
  scripts/peering-requests.py show    <node> <request-id>
  scripts/peering-requests.py approve <node> <request-id> [--no-deploy]
  scripts/peering-requests.py reject  <node> <request-id>

'approve' writes the peer into inventory/host_vars/<node>.yml (git stays
the source of truth) and runs playbooks/dn42.yml for the node. Approving
an auto-applied request migrates it to a managed tunnel: the dn42a-*
interface is torn down and the ansible-managed dn42-* one replaces it
(expect a brief session reset).
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOSTS_FILE = os.path.join(REPO_ROOT, "inventory", "hosts.yml")
HOST_VARS_DIR = os.path.join(REPO_ROOT, "inventory", "host_vars")
SPOOL = "/var/lib/sys-ops-autopeer"

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required (it ships with Ansible): pip install pyyaml")


def die(msg):
    sys.exit(f"error: {msg}")


def autopeer_nodes():
    with open(HOSTS_FILE, encoding="utf-8") as f:
        inventory = yaml.safe_load(f) or {}
    children = ((inventory.get("all") or {}).get("children")) or {}
    return sorted(((children.get("autopeer") or {}).get("hosts")) or {})


def adhoc(node, module, args):
    """Run an ansible ad-hoc task and return the parsed JSON result."""
    proc = subprocess.run(
        ["ansible", node, "-m", module, "-a", args, "--one-line"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    line = (proc.stdout or proc.stderr).strip()
    marker = "=>"
    if proc.returncode != 0 or marker not in line:
        die(f"ansible {module} on {node} failed:\n{line}")
    return json.loads(line.split(marker, 1)[1])


def remote_requests(node, states=("pending", "applied")):
    result = adhoc(
        node, "find",
        f"paths={','.join(SPOOL + '/' + s for s in states)} patterns=*.json",
    )
    requests = []
    for entry in result.get("files", []):
        content = adhoc(node, "slurp", f"src={entry['path']}")["content"]
        request = json.loads(base64.b64decode(content))
        request["_path"] = entry["path"]
        requests.append(request)
    return sorted(requests, key=lambda r: r["id"])


def find_request(node, request_id):
    for request in remote_requests(node):
        if request["id"] == request_id:
            return request
    die(f"request {request_id} not found on {node} (pending/applied)")


def move_request(node, request, state):
    dest = f"{SPOOL}/{state}/{os.path.basename(request['_path'])}"
    adhoc(node, "command", f"mv {request['_path']} {dest}")


def peer_entry_text(request):
    lines = [
        f"  - name: {request['name']}",
        f"    asn: {request['asn']}",
        f"    wg_pubkey: \"{request['wg_pubkey']}\"",
    ]
    if request.get("endpoint"):
        lines.append(f"    wg_endpoint: \"{request['endpoint']}\"")
    lines.append(f"    peer_v6: \"{request['peer_v6']}\"")
    lines.append(f"    wg_listen_port: {request['port']}")
    if not request.get("mp_bgp", True):
        lines.append("    mp_bgp: false")
    return "\n".join(lines)


def add_peer_to_host_vars(node, request):
    path = os.path.join(HOST_VARS_DIR, f"{node}.yml")
    if not os.path.exists(path):
        die(f"{path} not found")
    text = open(path, encoding="utf-8").read()

    existing = yaml.safe_load(text) or {}
    for peer in existing.get("dn42_peers") or []:
        if peer.get("asn") == request["asn"] or peer.get("name") == request["name"]:
            die(f"{node} already has a dn42_peers entry for "
                f"{peer.get('name')} / AS{peer.get('asn')}")

    entry = peer_entry_text(request)
    if re.search(r"^dn42_peers:\s*\[\]\s*$", text, re.M):
        text = re.sub(r"^dn42_peers:\s*\[\]\s*$", f"dn42_peers:\n{entry}", text,
                      count=1, flags=re.M)
    elif re.search(r"^dn42_peers:\s*$", text, re.M):
        text = re.sub(r"^dn42_peers:\s*$", f"dn42_peers:\n{entry}", text,
                      count=1, flags=re.M)
    else:
        text = text.rstrip() + f"\n\ndn42_peers:\n{entry}\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def fmt_request(node, request):
    extras = []
    if request.get("registry"):
        extras.append(f"registry:{request['registry']}")
    if request.get("error"):
        extras.append("auto-apply-failed")
    return (f"{node:<12} {request['id']:<28} {request['status']:<8} "
            f"AS{request['asn']}  {request['name']:<8} "
            f"{request.get('endpoint') or 'passive':<30} {' '.join(extras)}")


def cmd_list(_args):
    nodes = autopeer_nodes()
    if not nodes:
        print("no nodes in the 'autopeer' group")
        return
    count = 0
    for node in nodes:
        for request in remote_requests(node):
            print(fmt_request(node, request))
            count += 1
    if not count:
        print("no open peering requests")


def cmd_show(args):
    request = find_request(args.node, args.request_id)
    request.pop("_path", None)
    print(json.dumps(request, indent=2))


def cmd_approve(args):
    request = find_request(args.node, args.request_id)
    path = add_peer_to_host_vars(args.node, request)
    print(f"✓ added AS{request['asn']} ({request['name']}) to "
          f"{os.path.relpath(path, REPO_ROOT)}")

    if request["status"] == "applied":
        # Migrate auto-applied runtime config to the managed tunnel.
        iface = f"dn42a-{request['name']}"
        proto = re.sub(r"[^a-z0-9]", "_", request["name"])
        adhoc(args.node, "command",
              f"systemctl disable --now wg-quick@{iface}")
        adhoc(args.node, "command",
              f"rm -f /etc/wireguard/{iface}.conf /etc/bird/peers/dn42a_{proto}.conf")
        print(f"✓ removed auto-applied config ({iface})")

    move_request(args.node, request, "approved")
    print("✓ request archived as approved")

    if args.no_deploy:
        print(f"deploy when ready:  ansible-playbook playbooks/dn42.yml -l {args.node}")
        return
    print(f"deploying: ansible-playbook playbooks/dn42.yml -l {args.node}")
    raise SystemExit(subprocess.call(
        ["ansible-playbook", "playbooks/dn42.yml", "-l", args.node],
        cwd=REPO_ROOT,
    ))


def cmd_reject(args):
    request = find_request(args.node, args.request_id)
    if request["status"] == "applied":
        iface = f"dn42a-{request['name']}"
        proto = re.sub(r"[^a-z0-9]", "_", request["name"])
        adhoc(args.node, "command", f"systemctl disable --now wg-quick@{iface}")
        adhoc(args.node, "command",
              f"rm -f /etc/wireguard/{iface}.conf /etc/bird/peers/dn42a_{proto}.conf")
        adhoc(args.node, "command", "birdc configure")
        print(f"✓ tore down auto-applied config ({iface})")
    move_request(args.node, request, "rejected")
    print("✓ request archived as rejected")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("show", "approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("node")
        p.add_argument("request_id")
        if name == "approve":
            p.add_argument("--no-deploy", action="store_true",
                           help="update host_vars only; skip the playbook run")
    args = parser.parse_args()
    {"list": cmd_list, "show": cmd_show,
     "approve": cmd_approve, "reject": cmd_reject}[args.cmd](args)


if __name__ == "__main__":
    main()
