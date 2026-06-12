#!/usr/bin/env python3
"""Fleet inventory manager — the front door for new servers.

Interactive (asks questions, generates IDs, updates the inventory):

    scripts/add-server.py

Non-interactive / scriptable:

    scripts/add-server.py add --name web01 --location nyc --env prod \
        --role web --address 192.0.2.10 [--user root] [--port 22] [--yes]

Lifecycle helpers:

    scripts/add-server.py list                # show the fleet
    scripts/add-server.py retire <host-id>    # queue for decommission
    scripts/add-server.py remove <host-id>    # delete after decommission

Host IDs are generated as <location>-<env>-<name> (e.g. nyc-prod-web01)
and each host gets a deterministic asset tag derived from its name,
location and address. The tailscale hostname is set to the host ID so
tailnet names match the inventory.
"""

import argparse
import hashlib
import ipaddress
import os
import re
import subprocess
import sys
from datetime import date

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required (it ships with Ansible): pip3 install pyyaml")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INVENTORY = os.path.join(REPO_ROOT, "inventory", "hosts.yml")

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$")

HEADER = """\
# Fleet inventory — managed by scripts/add-server.py.
# Manual edits are preserved as data, but YAML comments are rewritten.
# Lifecycle: add -> onboard.yml -> maintenance.yml -> retire -> decommission.yml -> remove
"""


# --------------------------------------------------------------------------- io
def load_inventory(path):
    if os.path.exists(path):
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}
    if data.get("all") is None:
        data["all"] = {}
    data["all"].setdefault("children", {})
    if data["all"]["children"] is None:
        data["all"]["children"] = {}
    # The decommission queue group always exists.
    children = data["all"]["children"]
    if children.get("retiring") is None:
        children["retiring"] = {"hosts": {}}
    children["retiring"].setdefault("hosts", {})
    if children["retiring"]["hosts"] is None:
        children["retiring"]["hosts"] = {}
    return data


def save_inventory(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    with open(path, "w") as f:
        f.write(HEADER + "---\n" + body)


def iter_hosts(data):
    """Yield (group, host_id, hostvars) for every host in the inventory."""
    for group, gdata in (data["all"]["children"] or {}).items():
        for host_id, hvars in ((gdata or {}).get("hosts") or {}).items():
            yield group, host_id, hvars or {}


def find_host(data, host_id):
    for group, hid, hvars in iter_hosts(data):
        if hid == host_id:
            return group, hvars
    return None, None


def prune_empty_group(data, group):
    """Drop a group that no longer has hosts, vars or children ('retiring' stays)."""
    if group == "retiring":
        return
    gdata = data["all"]["children"].get(group) or {}
    if not (gdata.get("hosts") or {}) and not gdata.get("vars") and not gdata.get("children"):
        del data["all"]["children"][group]


# ----------------------------------------------------------------------- prompts
def ask(question, default=None, validator=None):
    while True:
        suffix = f" [{default}]" if default else ""
        try:
            raw = input(f"  {question}{suffix}: ").strip()
        except EOFError:
            sys.exit("\naborted")
        if not raw:
            if default is None:
                print("    a value is required")
                continue
            raw = default
        if validator:
            error = validator(raw)
            if error:
                print(f"    {error}")
                continue
        return raw


def confirm(question, default=False):
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        raw = input(f"  {question} {suffix}: ").strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw in ("y", "yes")


def slug_validator(value):
    if not SLUG_RE.match(value):
        return "use lowercase letters, digits and hyphens (e.g. web01, nyc, hetzner-fsn)"
    return None


def address_validator(value):
    try:
        ipaddress.ip_address(value)
        return None
    except ValueError:
        pass
    if HOSTNAME_RE.match(value):
        return None
    return "enter an IPv4/IPv6 address or a hostname/FQDN"


def port_validator(value):
    if value.isdigit() and 0 < int(value) < 65536:
        return None
    return "enter a port number between 1 and 65535"


# --------------------------------------------------------------------- generation
def make_host_id(name, location, env):
    return f"{location}-{env}-{name}"


def make_asset_tag(name, location, address):
    digest = hashlib.sha256(f"{name}|{location}|{address}".encode()).hexdigest()
    return f"SYS-{digest[:6].upper()}"


def build_entry(args):
    entry = {
        "ansible_host": args.address,
        "ansible_user": args.user,
    }
    if int(args.port) != 22:
        entry["ansible_port"] = int(args.port)
    entry.update(
        {
            "server_name": args.name,
            "server_location": args.location,
            "server_env": args.env,
            "server_role": args.role,
            "asset_tag": make_asset_tag(args.name, args.location, args.address),
            "tailscale_hostname": args.host_id,
            "added_on": date.today().isoformat(),
        }
    )
    return entry


# ------------------------------------------------------------------------ actions
def git_commit_and_push(inventory_path, message):
    rel = os.path.relpath(inventory_path, REPO_ROOT)
    cmds = [
        ["git", "add", rel],
        ["git", "commit", "-m", message],
        ["git", "push"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, cwd=REPO_ROOT)
        if result.returncode != 0:
            print(f"  ! '{' '.join(cmd)}' failed — finish the git step manually")
            return False
    return True


def cmd_add(args):
    interactive = sys.stdin.isatty() and not args.yes

    if interactive:
        print("\nNew server — answer a few questions (Enter accepts the default).\n")
        args.name = args.name or ask("Server name (e.g. web01)", validator=slug_validator)
        args.location = args.location or ask(
            "Location / site code (e.g. nyc, hetzner-fsn)", validator=slug_validator
        )
        args.env = args.env or ask(
            "Environment (prod/staging/dev)", default="prod", validator=slug_validator
        )
        args.role = args.role or ask(
            "Role / inventory group (e.g. web, db)", default="servers", validator=slug_validator
        )
        args.address = args.address or ask(
            "Address (IP or FQDN)", validator=address_validator
        )
        args.user = args.user or ask("SSH user", default="root")
        args.port = args.port or ask("SSH port", default="22", validator=port_validator)
    else:
        missing = [f for f in ("name", "location", "address") if not getattr(args, f)]
        if missing:
            sys.exit(f"error: missing required option(s): {', '.join('--' + m for m in missing)}")
        args.env = args.env or "prod"
        args.role = args.role or "servers"
        args.user = args.user or "root"
        args.port = args.port or "22"
        for field in ("name", "location", "env", "role"):
            error = slug_validator(getattr(args, field))
            if error:
                sys.exit(f"error: --{field}: {error}")
        if address_validator(args.address):
            sys.exit("error: --address: not a valid IP or hostname")
        if port_validator(args.port):
            sys.exit("error: --port: not a valid port")

    generated_id = make_host_id(args.name, args.location, args.env)
    if interactive:
        args.host_id = ask("Host ID", default=generated_id, validator=slug_validator)
    else:
        args.host_id = args.host_id or generated_id

    data = load_inventory(args.inventory)
    existing_group, _ = find_host(data, args.host_id)
    if existing_group:
        sys.exit(f"error: host '{args.host_id}' already exists in group '{existing_group}'")

    entry = build_entry(args)

    print(f"""
  Host ID:    {args.host_id}
  Group:      {args.role}
  Address:    {entry['ansible_host']} (ssh {args.user}@:{args.port})
  Asset tag:  {entry['asset_tag']}
  Tailscale:  {entry['tailscale_hostname']}
""")
    if interactive and not confirm("Add to inventory?", default=True):
        sys.exit("aborted — nothing written")

    children = data["all"]["children"]
    group = children.setdefault(args.role, {}) or {}
    children[args.role] = group
    hosts = group.setdefault("hosts", {}) or {}
    group["hosts"] = hosts
    hosts[args.host_id] = entry
    save_inventory(args.inventory, data)
    print(f"  ✓ {args.host_id} added to {os.path.relpath(args.inventory, os.getcwd())}")

    committed = False
    if args.commit or (
        interactive and confirm("Commit and push the inventory change?", default=True)
    ):
        committed = git_commit_and_push(
            args.inventory,
            f"inventory: add {args.host_id} ({args.location}, {args.env})",
        )

    onboard_cmd = ["ansible-playbook", "playbooks/onboard.yml", "-l", args.host_id]
    if args.onboard or (interactive and confirm("Run the onboarding playbook now?")):
        print("  (set tailscale_authkey via -e or Semaphore for tailnet join)")
        sys.exit(subprocess.run(onboard_cmd, cwd=REPO_ROOT).returncode)
    else:
        print(f"\n  Next step: {' '.join(onboard_cmd)}")
        if not committed:
            print("  (remember to commit/push the inventory so Semaphore sees it)")


def cmd_list(args):
    data = load_inventory(args.inventory)
    rows = [
        (group, host_id, h.get("ansible_host", "?"), h.get("server_env", "-"),
         h.get("asset_tag", "-"), h.get("added_on", "-"))
        for group, host_id, h in iter_hosts(data)
    ]
    if not rows:
        print("inventory is empty — run scripts/add-server.py to add a server")
        return
    fmt = "{:<22} {:<12} {:<18} {:<9} {:<12} {}"
    print(fmt.format("HOST ID", "GROUP", "ADDRESS", "ENV", "ASSET TAG", "ADDED"))
    for group, host_id, addr, env, tag, added in sorted(rows, key=lambda r: r[1]):
        print(fmt.format(host_id, group, addr, env, tag, added))


def cmd_retire(args):
    data = load_inventory(args.inventory)
    group, hvars = find_host(data, args.host_id)
    if group is None:
        sys.exit(f"error: host '{args.host_id}' not found")
    if group == "retiring":
        sys.exit(f"'{args.host_id}' is already retiring")
    del data["all"]["children"][group]["hosts"][args.host_id]
    data["all"]["children"]["retiring"]["hosts"][args.host_id] = hvars
    prune_empty_group(data, group)
    save_inventory(args.inventory, data)
    print(f"  ✓ {args.host_id} moved from '{group}' to 'retiring'")
    print(f"  Next step: ansible-playbook playbooks/decommission.yml -l {args.host_id} \\")
    print("               -e decommission_confirm=WIPE")
    print(f"  After teardown: scripts/add-server.py remove {args.host_id}")


def cmd_remove(args):
    data = load_inventory(args.inventory)
    group, _ = find_host(data, args.host_id)
    if group is None:
        sys.exit(f"error: host '{args.host_id}' not found")
    if group != "retiring" and sys.stdin.isatty():
        if not confirm(f"'{args.host_id}' is in '{group}', not 'retiring' — remove anyway?"):
            sys.exit("aborted")
    del data["all"]["children"][group]["hosts"][args.host_id]
    prune_empty_group(data, group)
    save_inventory(args.inventory, data)
    print(f"  ✓ {args.host_id} removed from inventory")
    print("  Also remove the machine from the Tailscale admin console and Semaphore.")


# --------------------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--inventory", default=DEFAULT_INVENTORY, help="inventory file (default: %(default)s)"
    )
    # Bare invocation (no subcommand) behaves like interactive `add`.
    parser.set_defaults(
        name=None, location=None, env=None, role=None, address=None,
        user=None, port=None, host_id=None, yes=False, commit=False, onboard=False,
    )
    sub = parser.add_subparsers(dest="command")

    p_add = sub.add_parser("add", help="add a server (interactive when options are omitted)")
    p_add.add_argument("--name", help="server name, e.g. web01")
    p_add.add_argument("--location", help="site code, e.g. nyc")
    p_add.add_argument("--env", help="prod/staging/dev (default: prod)")
    p_add.add_argument("--role", help="inventory group (default: servers)")
    p_add.add_argument("--address", help="IP or FQDN")
    p_add.add_argument("--user", help="SSH user (default: root)")
    p_add.add_argument("--port", help="SSH port (default: 22)")
    p_add.add_argument("--host-id", help="override the generated host ID")
    p_add.add_argument("--yes", action="store_true", help="no prompts; fail on missing options")
    p_add.add_argument("--commit", action="store_true", help="git commit+push the inventory")
    p_add.add_argument("--onboard", action="store_true", help="run onboard.yml after adding")

    sub.add_parser("list", help="list all inventory hosts")

    p_retire = sub.add_parser("retire", help="move a host to the 'retiring' group")
    p_retire.add_argument("host_id")

    p_remove = sub.add_parser("remove", help="delete a host from the inventory")
    p_remove.add_argument("host_id")

    args = parser.parse_args()
    if args.command in (None, "add"):
        cmd_add(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "retire":
        cmd_retire(args)
    elif args.command == "remove":
        cmd_remove(args)


if __name__ == "__main__":
    main()
