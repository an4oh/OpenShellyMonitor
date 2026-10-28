# Deploy to Azure Container Apps

One container runs the poller and the dashboard (`shelly-monitor serve --host 0.0.0.0 --poll`)
behind the HTTPS ingress of Azure Container Apps, with the built-in password login. The SQLite
database lives on an Azure Files share mounted at `/data`. After the first setup, every push to
`main` builds a new image and rolls it out.

```
push to main ──> GitHub Actions ──> ghcr.io/<owner>/openshellymonitor:sha-xxxxxxx
                                └─> az containerapp update --image ... (OIDC, no stored password)
```

Files:

- `setup.sh` — creates or updates every Azure resource (re-runnable).
- `github-oidc.sh` — lets GitHub Actions log in to Azure with OpenID Connect.
- `../../.github/workflows/deploy-azure.yml` — build, push to GHCR, deploy.

## Prerequisites

- An Azure subscription and the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
  (`az login`). The scripts install the `containerapp` extension.
- Your fork or copy of this repository on GitHub, with Actions enabled.
- Optional: the GitHub CLI `gh` to set secrets from the terminal.

## 0) Publish the image

Push to `main` (or run the workflow by hand: *Actions > Build and deploy to Azure Container Apps
> Run workflow*). The `build` job pushes `ghcr.io/<owner>/openshellymonitor`; the deploy job is
skipped until you set `AZURE_DEPLOY=true` (step 2).

Make the package public so Azure pulls it without credentials: on GitHub, *your profile >
Packages > openshellymonitor > Package settings > Change visibility > Public*. The image holds no
secrets: keys and passwords are injected at runtime.

Keep it private instead? Create a classic personal access token with only `read:packages` and run
`setup.sh` with `REGISTRY_USERNAME=<github user> REGISTRY_PASSWORD=<token>`, or on an existing app:

```sh
az containerapp registry set -n shelly-monitor -g rg-shelly-monitor \
  --server ghcr.io --username <github user> --password <token>
```

## 1) Create the Azure resources

```sh
az login
export SHELLY_HOMES='Home A=KEY1@shelly-XX-eu.shelly.cloud;Home B=KEY2@shelly-XX-eu.shelly.cloud'
export SHELLY_WEB_PASSWORD='a long, unique password'
IMAGE=ghcr.io/<owner>/openshellymonitor:latest LOCATION=westeurope ./deploy/azure/setup.sh
```

Every setting has a default at the top of `setup.sh` (`RG`, `LOCATION`, `ENV_NAME`, `APP_NAME`,
`STORAGE_ACCOUNT`, `SHARE_NAME`, `IMAGE`, `TZ_NAME`, `CPU`, `MEMORY`); override them as
environment variables. `LOCATION=italynorth` works too.

The script creates: resource group, Log Analytics workspace (30 days), Container Apps environment,
storage account (Standard LRS) with a 1 GiB SMB file share linked to the environment, and the
container app:

- external HTTPS ingress on port 8787, HTTP redirected to HTTPS;
- exactly one replica (`min = max = 1`), 0.25 vCPU / 0.5 GiB;
- secrets `shelly-homes`, `shelly-web-password`, `shelly-web-cookie-secret` (generated once, so
  logins survive restarts), passed as `SHELLY_HOMES`, `SHELLY_WEB_PASSWORD`,
  `SHELLY_WEB_COOKIE_SECRET`;
- `SHELLY_DB=/data/shelly-monitor.db`, `SHELLY_DB_JOURNAL=delete`, `SHELLY_BACKUP_DIR=/data/backups`
  (daily automatic backups, the last 14 kept), `SHELLY_WEB_TRUST_PROXY=1`, `TZ`;
- startup, liveness and readiness probes on `/healthz` (a stopped poller restarts the container).

It prints the address, e.g. `https://shelly-monitor.<random>.westeurope.azurecontainerapps.io/`.
Open it and log in with your password.

Re-running `setup.sh` is safe: it keeps the image currently deployed and every secret you do not
export again.

## 2) Connect GitHub to Azure

```sh
GITHUB_REPO=<owner>/OpenShellyMonitor ./deploy/azure/github-oidc.sh
```

It creates an app registration with a federated credential that trusts only
`repo:<owner>/OpenShellyMonitor:ref:refs/heads/main`, gives it `Contributor` on the resource
group, and prints the `gh` commands to run. Without `gh`, add them under *Settings > Secrets and
variables > Actions*:

| Kind | Name | Value |
|---|---|---|
| Secret | `AZURE_CLIENT_ID` | app registration client ID |
| Secret | `AZURE_TENANT_ID` | your tenant ID |
| Secret | `AZURE_SUBSCRIPTION_ID` | your subscription ID |
| Variable | `AZURE_RESOURCE_GROUP` | `rg-shelly-monitor` (default if unset) |
| Variable | `AZURE_CONTAINERAPP_NAME` | `shelly-monitor` (default if unset) |
| Variable | `AZURE_DEPLOY` | `true` (anything else skips the deploy job) |

## 3) From now on

Every push to `main` builds `ghcr.io/<owner>/openshellymonitor:sha-<commit>` (plus `latest`),
runs `az containerapp update --image ...:sha-<commit>` and waits until `https://<fqdn>/healthz`
answers. Roll back by re-running an older workflow run, or:

```sh
az containerapp update -n shelly-monitor -g rg-shelly-monitor --image ghcr.io/<owner>/openshellymonitor:sha-<old>
```

Logs: `az containerapp logs show -n shelly-monitor -g rg-shelly-monitor --follow`.

## 4) Cost (indicative)

Consumption plan, pay-as-you-go list prices (USD, late 2026; check the
[pricing calculator](https://azure.microsoft.com/pricing/calculator/) for your region). The
replica never scales to zero because the poller must run every minute: 0.25 vCPU and 0.5 GiB for
about 2.63 million seconds a month.

| Item | Per month |
|---|---|
| vCPU: 657,000 vCPU-s, minus 180,000 free | about 1.5 USD if Azure bills it as *idle* (under 0.01 vCPU and 1 KB/s in), about 11.5 USD if billed as *active* |
| Memory: 1,314,000 GiB-s, minus 360,000 free | about 3 USD |
| Azure Files, 1 GiB transaction optimized + SMB transactions of SQLite | about 1-4 USD |
| Log Analytics (a few MB/day, first 5 GB free) | about 0 USD |
| Ingress, managed certificate, GHCR public image | 0 USD |

Expect roughly **5-20 USD a month**, most likely toward the low end since the poller is idle
between requests. The free grant is per subscription: other apps in the same subscription share it.

## 5) Notes

**SQLite on Azure Files.** The share is SMB: SQLite's WAL mode needs shared memory that SMB cannot
provide, so `SHELLY_DB_JOURNAL=delete` is required, and only one process may write. Keep
`minReplicas = maxReplicas = 1` and never mount the share in a second app. The mount uses
`uid=10001,gid=10001` (the container user). If you see `database is locked` errors, check that no
second replica or revision is running (`az containerapp replica list`).

**Backup.** With `SHELLY_BACKUP_DIR=/data/backups` (set by `setup.sh`; for an app created before,
add it with `az containerapp update -n shelly-monitor -g rg-shelly-monitor --set-env-vars
SHELLY_BACKUP_DIR=/data/backups`) the poller writes a consistent `shelly-monitor-YYYYmmdd-HHMMSS.db.gz`
to the share once a day and keeps the last 14 (`SHELLY_BACKUP_INTERVAL_HOURS`, `SHELLY_BACKUP_KEEP`).
Those copies live on the same share as the database, so they do not protect against losing the
share or the storage account: download one from time to time with the **Scarica backup** button
at the bottom of the dashboard (section *Backup del database*), or from the share:

```sh
KEY=$(az storage account keys list -n <storage account> -g rg-shelly-monitor --query '[0].value' -o tsv)
az storage file list --account-name <storage account> --account-key "$KEY" --share-name shelly-data \
  --path backups -o table
az storage file download --account-name <storage account> --account-key "$KEY" \
  --share-name shelly-data --path backups/<file>.db.gz --dest .
```

**Restore.** In the same dashboard section choose a `.db.gz` (or `.db`) file and press
**Ripristina**: the upload is validated (SQLite integrity check, schema version, shelly-monitor
tables), the poller is paused for the replacement and the old database is gone afterwards, so
download a backup first. Uploads are limited to `SHELLY_RESTORE_MAX_MB` (default 200).
`SHELLY_WEB_READONLY=1` disables it. Azure Files share snapshots (`az storage share snapshot`) and
Azure Backup are alternatives.

**Custom domain with a managed certificate.** Create a `CNAME` from your domain to the app FQDN
and the `TXT` record `asuid.<sub>` with the environment's verification ID
(`az containerapp show -n shelly-monitor -g rg-shelly-monitor --query properties.customDomainVerificationId -o tsv`), then:

```sh
az containerapp hostname add  -n shelly-monitor -g rg-shelly-monitor --hostname shelly.example.com
az containerapp hostname bind -n shelly-monitor -g rg-shelly-monitor --hostname shelly.example.com \
  --environment cae-shelly-monitor --validation-method CNAME
```

**Rotate the dashboard password.**

```sh
az containerapp secret set -n shelly-monitor -g rg-shelly-monitor \
  --secrets shelly-web-password='the new password'
az containerapp revision restart -n shelly-monitor -g rg-shelly-monitor \
  --revision $(az containerapp show -n shelly-monitor -g rg-shelly-monitor --query properties.latestRevisionName -o tsv)
```

Secrets are read at start, hence the restart. To also log everybody out, set a new
`shelly-web-cookie-secret` (`openssl rand -hex 32`) the same way. Re-running `setup.sh` with the new
`SHELLY_WEB_PASSWORD` exported also updates the secret; restart the revision afterwards. The same
applies to `shelly-homes` when you add a home or rotate a cloud key.

**Remove everything.** `az group delete -n rg-shelly-monitor` (deletes the database too: back it
up first), then delete the app registration created by `github-oidc.sh`.
