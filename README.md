# FMW Monitoring Dashboard

`iam-monitoring` is a Linux-hosted dashboard for Oracle IAM environments, including OUD and WebLogic monitoring, environment bootstrap, scheduled collection, and GitHub-based upgrades.

## Quick Install

Use the normal install command on a new server. It installs/checks the required OS packages, prompts for the dashboard port, and defaults to `8081`.

### Install Without Proxy

```bash
sudo bash -lc 'cd /tmp && rm -rf fmw_dashboard-main fmw-dashboard-main.tar.gz && curl -L https://github.com/reply4ramesh/fmw_dashboard/archive/refs/heads/main.tar.gz -o fmw-dashboard-main.tar.gz && tar -xzf fmw-dashboard-main.tar.gz && bash /tmp/fmw_dashboard-main/server-app/install.sh'
```

### Install With Proxy

```bash
sudo bash -lc 'export http_proxy=http://www-proxy-phx.oraclecorp.com:80; export https_proxy=http://www-proxy-phx.oraclecorp.com:80; cd /tmp && rm -rf fmw_dashboard-main fmw-dashboard-main.tar.gz && curl -L https://github.com/reply4ramesh/fmw_dashboard/archive/refs/heads/main.tar.gz -o fmw-dashboard-main.tar.gz && tar -xzf fmw-dashboard-main.tar.gz && bash /tmp/fmw_dashboard-main/server-app/install.sh'
```

### Prepared Reinstall Only

Use this only when Python, SSH tools, `sshpass`, `tar`, `unzip`, and `cronie` are already installed and you want to avoid a slow `dnf`/`yum` repository refresh.

```bash
sudo bash -lc 'export http_proxy=http://www-proxy-phx.oraclecorp.com:80; export https_proxy=http://www-proxy-phx.oraclecorp.com:80; cd /tmp && rm -rf fmw_dashboard-main fmw-dashboard-main.tar.gz && curl -L https://github.com/reply4ramesh/fmw_dashboard/archive/refs/heads/main.tar.gz -o fmw-dashboard-main.tar.gz && tar -xzf fmw-dashboard-main.tar.gz && bash /tmp/fmw_dashboard-main/server-app/install.sh --skip-os-packages'
```

## Quick Upgrade

Upgrade can be done from the UI or from the Linux command line. The UI upgrade is easier once the dashboard is already running.

### Upgrade Through UI

Open:

```text
Administration -> Help -> GitHub Upgrade
```

If the server needs a proxy for GitHub checks or GitHub upgrade downloads, set it in:

```text
Administration -> Help -> GitHub Update Proxy
```

The UI checks the GitHub `VERSION` first. If the running version is already current, it says you are on the latest version and does not restart the service.

### Upgrade Command Without Proxy

```bash
sudo bash -lc 'cd /tmp && rm -rf fmw_dashboard-main fmw-dashboard-main.tar.gz && curl -L https://github.com/reply4ramesh/fmw_dashboard/archive/refs/heads/main.tar.gz -o fmw-dashboard-main.tar.gz && tar -xzf fmw-dashboard-main.tar.gz && bash /tmp/fmw_dashboard-main/server-app/upgrade.sh'
```

### Upgrade Command With Proxy

```bash
sudo bash -lc 'export http_proxy=http://www-proxy-phx.oraclecorp.com:80; export https_proxy=http://www-proxy-phx.oraclecorp.com:80; cd /tmp && rm -rf fmw_dashboard-main fmw-dashboard-main.tar.gz && curl -L https://github.com/reply4ramesh/fmw_dashboard/archive/refs/heads/main.tar.gz -o fmw-dashboard-main.tar.gz && tar -xzf fmw-dashboard-main.tar.gz && bash /tmp/fmw_dashboard-main/server-app/upgrade.sh'
```

The upgrade keeps `/etc/iam-monitoring.env`, saved environments, runtime state, snapshots, and logs in place.

## Uninstall

Use these steps when you want to remove the dashboard from a Linux host.

```bash
sudo systemctl stop iam-monitoring iam-monitoring-upgrader 2>/dev/null || true
sudo systemctl disable iam-monitoring iam-monitoring-upgrader 2>/dev/null || true
sudo rm -f /etc/systemd/system/iam-monitoring.service /etc/systemd/system/iam-monitoring-upgrader.service /etc/cron.d/iam-monitoring
sudo systemctl daemon-reload
sudo rm -rf /opt/iam-monitoring
```

Optional cleanup removes local runtime configuration, saved connection profiles, encrypted passwords, runtime SSH keys, snapshots, and logs:

```bash
sudo rm -f /etc/iam-monitoring.env
sudo rm -rf /var/lib/iam-monitoring/state /var/log/iam-monitoring
sudo userdel iam-monitoring 2>/dev/null || true
```

Skip the optional cleanup if you want to preserve customer connection profiles for a later reinstall.

## Fresh Install State

The GitHub package does not include customer connection profiles, saved passwords, runtime SSH keys, snapshots, logs, or the SQLite registry. Those files are created locally on each installed dashboard server under `/var/lib/iam-monitoring/state` and `/var/log/iam-monitoring`.

When a customer installs from GitHub on a new machine, the dashboard starts with an empty environment registry. They add their own OAM, OAA, OID, OUD, OIG, or WebLogic profiles from `Administration -> Environments`.

## After Install

The installer prints the dashboard URL at the end:

```text
http://<server-ip>:8081/
```

Useful checks:

```bash
sudo systemctl status iam-monitoring --no-pager
sudo systemctl status iam-monitoring-upgrader --no-pager
curl http://127.0.0.1:8081/healthz
sudo journalctl -u iam-monitoring -n 100 --no-pager
sudo tail -F /var/log/iam-monitoring/scheduler.log
```

## Runtime Layout

- Install directory: `/opt/iam-monitoring`
- Runtime env file: `/etc/iam-monitoring.env`
- State directory: `/var/lib/iam-monitoring/state`
- SQLite registry: `/var/lib/iam-monitoring/state/iam-monitoring.sqlite`
- Snapshot directory: `/var/lib/iam-monitoring/state/snapshots`
- Runtime env directory: `/var/lib/iam-monitoring/state/runtime_env`
- Log directory: `/var/log/iam-monitoring`
- Service name: `iam-monitoring`
- Upgrade helper service: `iam-monitoring-upgrader`
- Cron file: `/etc/cron.d/iam-monitoring`

## GitHub Proxy Options

The easiest place to manage GitHub proxy settings is:

```text
Administration -> Help -> GitHub Update Proxy
```

You can also put service-level proxy settings in `/etc/iam-monitoring.env` and restart the service:

```bash
IAM_MONITORING_HTTP_PROXY=http://www-proxy-phx.oraclecorp.com:80
IAM_MONITORING_HTTPS_PROXY=http://www-proxy-phx.oraclecorp.com:80
IAM_MONITORING_NO_PROXY=127.0.0.1,localhost
sudo systemctl restart iam-monitoring
```

## Environment Lifecycle

- Add environments from `Administration -> Environments`.
- Use `Save And Bootstrap` when adding an environment.
- Bootstrap uses the initial SSH user/password or private key once.
- After bootstrap, collection uses the installed runtime SSH key.
- Use `Run Jobs Now` inside an environment to collect immediately.
- Scheduled collection runs through `/etc/cron.d/iam-monitoring`.

## DMS Metrics

For WebLogic-backed products such as OAM and OIG, the collector uses the saved WebLogic administrator profile to:

- discover the `dms` application and its server or cluster targets from the WebLogic domain configuration (`config.xml` through online WLST);
- expand cluster targets to their individual AdminServer or managed-server names;
- list the DMS metric tables available on only those targeted servers; and
- collect a bounded, high-value set of OAM, OIG, JVM, JDBC, servlet, thread, WorkManager, and J2EE metrics for the dashboard snapshot.

DMS values are shown under `OAM -> DMS Metrics` and `WebLogic -> DMS Metrics`. The WebLogic user must have Administrator-role access. Passwords remain in the existing encrypted environment profile and are not returned in dashboard payloads or collector command descriptions.

## Source Layout

- `server-app/`: active hosted dashboard server and Linux deployment utilities
- `server-app/static/`: dashboard UI
- `server-app/README.md`: server-app specific notes
- root prototype files: earlier local prototype files kept for reference
