# Deployment

One-shot installer for Debian/Ubuntu hosts. Targets Debian 13 (trixie) with Python 3.13, but works on any release shipping Python ≥ 3.11.

## Quick start

On a fresh Debian/Ubuntu machine:

```bash
curl -fsSL https://raw.githubusercontent.com/robeertm/ev-charge-tracker/main/deploy/install.sh | sudo bash
```

That's it. The script:

1. Installs required packages (`python3`, `git`, `sqlite3`, `cryptsetup`, `fail2ban`, `unattended-upgrades`, optionally `tailscale`).
2. Creates the `ev-tracker` system user.
3. Clones this repo to `/srv/ev-data/app/`.
4. Builds a Python venv and installs `requirements.txt`.
5. Installs the systemd unit (`/etc/systemd/system/ev-tracker.service`).
6. Installs the sudoers rules (`/etc/sudoers.d/ev-tracker`).
7. Starts the service.
8. Optionally offers to install Tailscale + enable `tailscale serve` for HTTPS access.

When it's done, the web UI runs on port `7654`. Open `http://<host>:7654` in a browser, set a login password, and configure the vehicle API from there.

## Non-interactive install

Skip all prompts (no Tailscale, no password, defaults everywhere):

```bash
curl -fsSL https://raw.githubusercontent.com/robeertm/ev-charge-tracker/main/deploy/install.sh | sudo EV_UNATTENDED=1 bash
```

Force Tailscale install:

```bash
curl -fsSL .../install.sh | sudo EV_WITH_TAILSCALE=1 bash
```

## Env overrides

| Variable | Default | Purpose |
|---|---|---|
| `EV_REPO` | `https://github.com/robeertm/ev-charge-tracker.git` | Git URL of the app |
| `EV_BRANCH` | `main` | Branch or tag to check out (e.g. `v2.27.1`) |
| `EV_APP_DIR` | `/srv/ev-data/app` | Install directory |
| `EV_USER` | `ev-tracker` | OS service user |
| `EV_WITH_TAILSCALE` | *(prompt)* | `1` to auto-install Tailscale, `0` to skip |
| `EV_UNATTENDED` | `0` | `1` disables all interactive prompts |

## Files installed by the script

| Location | From | Purpose |
|---|---|---|
| `/srv/ev-data/app/` | Git clone of this repo | Application code + venv |
| `/etc/systemd/system/ev-tracker.service` | `deploy/ev-tracker.service` (or inline fallback) | systemd unit |
| `/etc/sudoers.d/ev-tracker` | `deploy/sudoers.ev-tracker` (or inline fallback) | NOPASSWD rules for service-restart and security-updates |
| `/usr/local/bin/ev-unlock` | `deploy/ev-unlock` (only if LUKS volume is in use) | LUKS unlock helper, invoked via sudo from the web UI |

## LUKS-encrypted data volume (optional)

The default install puts the SQLite database under `/srv/ev-data/app/data/`. If you'd rather keep `/srv/ev-data/` on its own encrypted disk (like the reference installs), do the LUKS setup yourself before running the installer — the installer detects whether `/srv/ev-data` is a mount point and leaves the filesystem layout alone either way.

Rough steps for a second-disk LUKS setup:

```bash
sudo cryptsetup luksFormat /dev/sdb
sudo cryptsetup open /dev/sdb evdata
sudo mkfs.ext4 /dev/mapper/evdata
sudo mkdir -p /srv/ev-data
sudo mount /dev/mapper/evdata /srv/ev-data
sudo install -m 755 deploy/ev-unlock /usr/local/bin/ev-unlock
```

Don't put `/srv/ev-data` in `/etc/fstab` if you want the boot to wait for an explicit unlock (via `sudo ev-unlock` from an authenticated SSH session). That's the setup the reference installs use: the machine boots to a minimal rescue state, you SSH in, unlock, the service starts automatically.

## Verifying the install

```bash
# Service status
systemctl status ev-tracker.service

# Live logs
journalctl -u ev-tracker.service -f

# Check version
grep APP_VERSION /srv/ev-data/app/config.py
```

## Updates after install

Two paths:

1. **Web UI**: Settings → Updates → "Install update" — the in-app updater pulls the latest GitHub release and restarts the service.
2. **Re-run installer**: rerunning `curl … | sudo bash` pulls the latest code from the selected branch (`git pull --ff-only`) and re-runs pip. Safe: idempotent, doesn't touch the database.

## Uninstall

```bash
sudo systemctl disable --now ev-tracker.service
sudo rm -f /etc/systemd/system/ev-tracker.service /etc/sudoers.d/ev-tracker
sudo rm -rf /srv/ev-data/app
sudo userdel -r ev-tracker   # removes home dir too
```

The database at `/srv/ev-data/app/data/ev_tracker.db` is removed by the `rm -rf` above — back it up first if you care.
