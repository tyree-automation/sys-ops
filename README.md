# sys-ops — fleet lifecycle management

One Ansible repo that manages every node you run — **test**, **development**
and **production** — through its whole life: **onboard → maintain →
decommission**, with optional components (Tailscale, a dn42 router stack,
a fleet web dashboard) chosen per node at onboarding time.

Base lifecycle supports Debian/Ubuntu and RHEL-family (Rocky, Alma, Fedora)
hosts; the dn42 component is Debian-family only.

## Quick start

```console
$ ansible-galaxy collection install -r requirements.yml
$ scripts/new-node.py            # interactive: register a node
$ ansible-playbook playbooks/onboard.yml -l <node>
```

`scripts/new-node.py` is the front door for adding machines. It asks for:

1. **Node identity** — inventory name, SSH address/user/port
2. **Environment** — `test`, `development` or `production` (sets the
   maintenance policy via `inventory/group_vars/<env>.yml`)
3. **Optional components** — join the tailnet? deploy the dn42 router
   stack? serve the fleet web dashboard? (dn42 is typically a test-node
   thing, but any node can opt in)
4. **Map location** — optional lat/lon so the node shows on the
   dashboard's world map

…then writes `inventory/hosts.yml` + `inventory/host_vars/<node>.yml`,
prints a per-component checklist, and offers to run the onboarding
playbook on the spot. Nothing is installed on a node unless you opted in.

## The lifecycle

| Stage | Playbook | What it does |
|---|---|---|
| **Audit** | `playbooks/audit.yml` | Read-only health report: NTP sync, pending reboot, tailscale state, failed units, disk/memory. |
| **Onboard** | `playbooks/onboard.yml` | Full system update, base packages, unattended security upgrades, timezone + chrony NTP, managed users + SSH keys + sudo, SSH hardening, fail2ban, firewall (ufw/firewalld), then any components the node opted into. |
| **Maintain** | `playbooks/maintenance.yml` | Update all packages, autoremove orphans, clean caches, vacuum journals, health summary, optional auto-reboot when required. Schedule it (e.g. weekly cron). |
| **Decommission** | `playbooks/decommission.yml` | Teardown to near-factory default (components first, then base). Gated: refuses to run without `-e decommission_confirm=WIPE`. |

## Inventory layout

```
inventory/
├── hosts.yml              # groups only — managed by scripts/new-node.py
├── group_vars/
│   ├── all.yml            # fleet-wide policy
│   ├── test.yml           # per-environment policy overrides
│   ├── development.yml
│   ├── production.yml
│   └── dn42.yml           # dn42 network identity (ASN, prefixes)
└── host_vars/
    └── <node>.yml         # per-node: SSH details, component flags, dn42 peers
```

Every node is in exactly one **environment group** (`test`, `development`,
`production`) and any number of **component groups** (`dn42`, `website`).
Moving a node to the `retiring` group queues it for decommissioning.

## Optional components

### Tailscale

Opt in per node with `tailscale_enabled: true` in its host_vars (the
new-node script sets this for you). Pass the auth key at runtime — never
commit it:

```console
$ ansible-playbook playbooks/onboard.yml -l <node> -e tailscale_authkey=tskey-auth-...
```

### dn42 router

A self-contained [dn42](https://dn42.dev/) router: WireGuard point-to-point
tunnels, BIRD2 with the standard dn42 import/export filters, and ROA
validation kept fresh by a systemd timer. This replaces the old standalone
`ansible-dn42` repo — redesigned from scratch, shipping **no peering data**;
you bring your own ASN, prefixes and peers.

One-time setup — register with the dn42 registry
([Getting started](https://dn42.dev/howto/Getting-Started)), then put your
ASN and prefixes in `inventory/group_vars/dn42.yml`.

Per node — opt in via the new-node script (it asks for the node's dn42
IPv4/IPv6 and adds it to the `dn42` group), then add peerings in
`inventory/host_vars/<node>.yml`:

```yaml
dn42_peers:
  - name: example                          # interface dn42-example
    asn: 4242421234
    wg_pubkey: "their-wireguard-pubkey="
    wg_endpoint: "peer.example.com:51820"  # omit for passive peers
    peer_v6: "fe80::1234"                  # MP-BGP over link-local (default)
```

Deploy / reconfigure after any peer change:

```console
$ ansible-playbook playbooks/dn42.yml -l <node>
```

Onboarding prints the node's WireGuard public key — share it with peers.
Tunnels listen on `20000 + (peer ASN mod 10000)` unless a peer sets
`wg_listen_port`. Remove the stack from a node without decommissioning it:

```console
$ ansible-playbook playbooks/dn42.yml -l <node> -e dn42_state=absent -e decommission_confirm=WIPE
```

### Fleet web dashboard

A modern, self-hosted dashboard for the whole fleet — the successor to the
old ansible-dn42 splash site (highdef.network), rebuilt from scratch:

- **animated world map** (Leaflet) — pulsing node markers colored by
  environment, curved animated arcs for the dn42 mesh and any custom links
- **fleet stats** with count-up animations, environment filter pills,
  live node search
- **node cards + detail drawer** — status, components, dn42 addressing,
  peering list, copy-ready playbook commands per node
- **dn42 peering table** across the fleet
- **customizable**: branding, tagline, accent color, footer, map
  center/zoom/tiles, panel toggles and custom map links all live in
  `site.yml`; viewers get a live accent-color picker and dark/light
  toggle in the UI

It's a static site (`web/`) fed by a generated dataset — no backend, no
API keys. Node positions come from the optional map location asked by
`scripts/new-node.py` (stored as `site_location` in host_vars).

**Public vs internal.** Every build produces two datasets, and each
website node serves exactly one (its `website_mode` host var, chosen in
the new-node script — default `public`):

- `public` — **dn42 information only**: routers, locations, status, dn42
  addressing, peerings, and the "peer with me" card from the `public:`
  section of `site.yml`. No environments, SSH addresses, components, or
  ops commands — that data isn't in the file at all, so a public host
  physically never receives it.
- `internal` — the full fleet view. Deploy only on trusted networks
  (e.g. behind Tailscale).

```console
$ scripts/build-site.py                  # -> build/fleet-{public,internal}.json
$ scripts/build-site.py --probe          # also ping nodes for up/down status
$ scripts/build-site.py --serve          # preview the public site on :8080
$ scripts/build-site.py --serve --mode internal   # preview the internal site
$ ansible-playbook playbooks/website.yml # deploy to the 'website' group
```

Re-run `build-site.py` + `website.yml` whenever the fleet changes (a
cron/CI job works well).

## Decommissioning

```console
$ ansible-playbook playbooks/decommission.yml -l <node> -e decommission_confirm=WIPE
```

Removes components (dn42, tailscale), security hardening, managed users,
onboarding packages, logs and machine-id — each step has its own toggle in
`roles/decommission/defaults/main.yml`. Without the `WIPE` token it refuses
to act. Afterwards, delete the node from `inventory/hosts.yml` and remove
its `host_vars` file.

## Repo layout

```
playbooks/        audit, onboard, maintenance, decommission, dn42, website
roles/            base, time_sync, users, security, tailscale, maintenance,
                  decommission, dn42, website
scripts/          new-node.py — interactive node registration
                  build-site.py — build the fleet dashboard dataset
web/              fleet dashboard (static site, generated data in web/data/)
site.yml          dashboard customization (branding, map, panels)
inventory/        hosts.yml, group_vars, host_vars
```
