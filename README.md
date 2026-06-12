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

# 2. Inventory
cp inventory/hosts.example.yml inventory/hosts.yml   # edit hosts (gitignored)

# 3. Onboard a new machine
ansible-playbook playbooks/onboard.yml -l web-01 \
  -e tailscale_authkey=tskey-auth-XXXX

# 4. Routine maintenance (any time / scheduled)
ansible-playbook playbooks/maintenance.yml

# 5. End of life — requires the confirmation token
ansible-playbook playbooks/decommission.yml -l web-01 \
  -e decommission_confirm=WIPE \
  -e decommission_revoke_ansible_access=true
```

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
inventory/
  hosts.example.yml       # copy to hosts.yml (gitignored)
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
