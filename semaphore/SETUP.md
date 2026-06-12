# Wiring this repo into Semaphore UI

Semaphore runs the playbooks in this repository on a schedule or on demand,
with secrets kept out of git. One-time setup, roughly 10 minutes.

## 1. Start Semaphore

```bash
cd semaphore
cp .env.example .env   # edit secrets; generate the encryption key:
                       #   head -c32 /dev/urandom | base64
docker compose up -d
```

Log in at `http://<host>:3000` with the admin credentials from `.env`,
then create a project (e.g. **sys-ops**).

## 2. Key Store

| Key name        | Type           | Purpose                                            |
|-----------------|----------------|----------------------------------------------------|
| `fleet-ssh`     | SSH Key        | Private key Ansible uses to reach your servers     |
| `repo-access`   | SSH Key / None | Access to this git repo (None if public)           |

## 3. Repository

- **Name:** sys-ops
- **URL:** this repo's clone URL
- **Branch:** `main`
- **Access Key:** `repo-access`

Semaphore pulls the repo fresh for every task, so pushed changes to
playbooks/roles take effect on the next run automatically. It also installs
`requirements.yml` collections automatically.

## 4. Inventory

Create an Inventory of type **Static (YAML)** and paste your fleet (same
shape as `inventory/hosts.example.yml`), or use type **File** pointing at a
committed inventory file. Set **User Credentials** to `fleet-ssh`.

> Keep the `retiring` group: moving a host there is the queue for teardown.

## 5. Variable Group (environment + secrets)

Create a Variable Group, e.g. `fleet-vars`:

```json
{
  "time_timezone": "Etc/UTC"
}
```

Add **secret** variables to the same group:

- `tailscale_authkey` → a reusable, pre-authorized key from
  https://login.tailscale.com/admin/settings/keys

Secrets are encrypted at rest with `SEMAPHORE_ACCESS_KEY_ENCRYPTION` and
injected as extra-vars at runtime — they never touch the repo.

## 6. Task Templates

| Template         | Playbook                     | Suggested setup                                                                 |
|------------------|------------------------------|---------------------------------------------------------------------------------|
| **Audit**        | `playbooks/audit.yml`        | Run anytime; good connectivity check. Optional schedule: daily.                  |
| **Onboard**      | `playbooks/onboard.yml`      | Survey var `target_hosts` (default `all`) so you can onboard one host by name.   |
| **Maintenance**  | `playbooks/maintenance.yml`  | Schedule weekly, e.g. cron `0 4 * * 1`. Optionally set `maintenance_reboot_if_required=true` for a real patch window. |
| **Decommission** | `playbooks/decommission.yml` | Survey vars: `target_hosts` (**required**) and `decommission_confirm` (**required** — operator must type `WIPE`). |

For each template: Repository = sys-ops, Inventory = your fleet,
Environment = `fleet-vars`.

### Decommission template — survey variables

Add two required survey variables so every teardown is deliberate:

1. `target_hosts` — the host or group to wipe (e.g. `web-01` or `retiring`)
2. `decommission_confirm` — must be typed as `WIPE`; the playbook hard-fails
   on anything else

Optional booleans to expose as survey vars:
`decommission_regenerate_ssh_host_keys`, `decommission_revoke_ansible_access`.

## 7. Suggested lifecycle flow

1. **Provision** — image the machine, ensure root/key SSH access, add it to
   the inventory.
2. **Onboard** — run the Onboard template against the new host. It comes out
   updated, time-synced, hardened, on the tailnet, and ready for its service.
3. **Service life** — weekly Maintenance schedule keeps it patched; Audit
   shows fleet health on demand.
4. **End of life** — move the host to the `retiring` group, run Decommission
   with `decommission_confirm=WIPE`, then remove it from the Tailscale admin
   console and the Semaphore inventory.
