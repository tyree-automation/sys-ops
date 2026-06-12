# sys-ops — server lifecycle platform

Ansible automation covering a server's whole life: **onboard → maintain →
decommission**, driven manually from the CLI or as scheduled jobs in
[Semaphore UI](https://semaphoreui.com/).

Supports Debian/Ubuntu and RHEL-family (Rocky, Alma, Fedora) hosts.

## The lifecycle

| Stage | Playbook | What it does |
|---|---|---|
| **Audit** | `playbooks/audit.yml` | Read-only health report: NTP sync, pending reboot, tailscale state, failed units, disk/memory. |
| **Onboard** | `playbooks/onboard.yml` | Full system update, base packages (git, curl, …), unattended security upgrades, timezone + chrony NTP, managed users + SSH keys + sudo, SSH hardening, fail2ban, firewall (ufw/firewalld), Tailscale install + tailnet join. The machine comes out ready for its service life. |
| **Maintain** | `playbooks/maintenance.yml` | Update all packages, autoremove orphans, clean caches, vacuum journals, health summary, optional auto-reboot when required. Schedule this weekly in Semaphore. |
| **Decommission** | `playbooks/decommission.yml` | Tear everything back down to (near) factory default: leave + uninstall Tailscale, revert hardening, remove firewall/fail2ban, delete managed users, remove onboarded packages, wipe logs/histories, reset machine-id, optionally regenerate SSH host keys and revoke Ansible's own access. Ready to redeploy or sell. |

## Quick start (CLI)

```bash
# 1. Dependencies
ansible-galaxy collection install -r requirements.yml

# 2. Add a server — interactive: asks name/place/region/site/role/address,
#    generates a CLLI-style host ID + asset tag, writes inventory/hosts.yml,
#    offers to commit+push and onboard in one go
scripts/add-server.py

# 3. Onboard it (if you didn't let the script do it)
ansible-playbook playbooks/onboard.yml -l nycmnydcw01 \
  -e tailscale_authkey=tskey-auth-XXXX

# 4. Routine maintenance (any time / scheduled)
ansible-playbook playbooks/maintenance.yml

# 5. End of life — queue, wipe (requires confirmation token), drop
scripts/add-server.py retire nycmnydcw01
ansible-playbook playbooks/decommission.yml -l nycmnydcw01 \
  -e decommission_confirm=WIPE \
  -e decommission_revoke_ansible_access=true
scripts/add-server.py remove nycmnydcw01
```

## Adding servers

`scripts/add-server.py` is the front door for new machines. Run it bare for
an interactive interview, or fully scripted:

```bash
scripts/add-server.py add --name web01 --place NYCM --region NY \
  --site DC --role web --address 192.0.2.10 --yes [--commit] [--onboard]
scripts/add-server.py list
scripts/add-server.py retire <host-id>   # move to the decommission queue
scripts/add-server.py remove <host-id>   # delete after teardown
```

### CLLI host IDs

Host IDs follow a CLLI-style scheme (the telecom **Common Language Location
Identifier**): 11 characters encoding where and what the machine is.

```
N Y C M   N Y   D C   W 0 1     →  NYCMNYDCW01 (inventory id: nycmnydcw01)
└─place─┘ └rgn┘ └site┘ └entity┘
```

| Field | Size | Meaning | Examples |
|---|---|---|---|
| place | 4 letters | city/locality abbreviation | `NYCM` (NY Manhattan), `HSTN` (Houston), `FRNK` (Frankfurt) |
| region | 2 letters | US state or ISO country | `NY`, `TX`, `DE`, `NL` |
| site | 2 alnum | building/DC within the place | `DC`, `01`, `AA` |
| entity | 3 chars | role class letter + sequence | `W01` = web #1, `D03` = db #3 |

Role class letters: `W`=web `D`=db `A`=app `C`=cache `S`=storage
`N`=network `M`=monitoring `B`=backup `V`=virt `G`=generic (custom roles
use their first letter). The sequence number is auto-assigned — the next
free number for that role at that site — and editable before writing.

What you get per host:

- **Host ID** — the lowercase CLLI (e.g. `nycmnydcw01`); also becomes the
  Tailscale hostname, so the tailnet matches the inventory.
- **Asset tag** `SYS-XXXXXX` — deterministic hash of CLLI+address, handy
  for labelling hardware that later gets sold off.
- **Detailed hostvars** — `clli`, `clli_place`, `clli_region`, `clli_site`,
  `clli_entity`, `server_name`, `server_env`, `server_role`, `added_on` —
  usable in playbook conditionals, audits, and reports
  (`scripts/add-server.py list` shows the fleet by place/region/site).

`inventory/hosts.yml` is committed to git by design so the script's changes
flow to Semaphore (File-type inventory). If you don't want addresses in git,
see the note in `.gitignore`.

## Semaphore UI

Everything is designed to run from Semaphore: templates map 1:1 to the four
playbooks, secrets (Tailscale auth key, SSH keys) live in Semaphore's
encrypted key store, maintenance runs on a cron schedule, and decommission is
gated behind a survey variable the operator must type (`WIPE`).

A ready-to-run `docker-compose.yml` for the Semaphore server and the full
wiring guide are in [`semaphore/SETUP.md`](semaphore/SETUP.md).

## Configuration

Fleet policy lives in `inventory/group_vars/all.yml` (timezone, NTP pools,
package list, users, SSH/firewall policy, tailscale flags). Every value can
be overridden per group/host or at runtime via `-e` / Semaphore variable
groups. Role-level defaults and documentation for each knob are in
`roles/*/defaults/main.yml`.

Notable safety behaviors:

- **Decommission hard gate** — refuses to run unless
  `decommission_confirm=WIPE` is passed; every teardown step also has its own
  toggle.
- **Lockout protection** — `security_ssh_password_auth: "no"` is only safe
  once key access works; the decommission role never deletes the user it is
  connected as, and revoking Ansible's own key is opt-in and runs last.
- **Secrets** — `tailscale_authkey` is never logged (`no_log`) and should be
  supplied via Semaphore secret variables or `-e`, never committed.

## Repository layout

```
ansible.cfg               # sane defaults; inventory + roles paths
requirements.yml          # ansible.posix, community.general
scripts/
  add-server.py           # interactive add/list/retire/remove for the fleet
inventory/
  hosts.yml               # the fleet — managed by add-server.py, committed
  group_vars/all.yml      # fleet-wide policy
playbooks/
  audit.yml  onboard.yml  maintenance.yml  decommission.yml
roles/
  base/          # hostname, packages, unattended-upgrades
  time_sync/     # timezone + chrony NTP
  users/         # managed users, ssh keys, sudo
  security/      # sshd hardening, fail2ban, ufw/firewalld
  tailscale/     # install + join tailnet
  maintenance/   # updates, cleanup, reboot handling, health report
  decommission/  # full teardown to factory default
semaphore/
  docker-compose.yml  .env.example  SETUP.md
```
