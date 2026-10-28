#!/usr/bin/env bash
# One-time (and re-runnable) setup of OpenShellyMonitor on Azure Container Apps.
#
#   az login
#   export SHELLY_HOMES='Home A=KEY1@shelly-XX-eu.shelly.cloud;Home B=KEY2'
#   export SHELLY_WEB_PASSWORD='a long password'
#   ./deploy/azure/setup.sh
#
# Creates (or updates) the resource group, Log Analytics workspace, Container Apps
# environment, storage account + file share for the SQLite database, and the
# container app (1 replica, external HTTPS ingress, /data on Azure Files).
# Secrets are read from the environment, never written to the repository.
# Re-running it keeps the deployed image and any secret you do not export again.
set -euo pipefail

# --- Parameters (override with environment variables) ---------------------------
RG="${RG:-rg-shelly-monitor}"
LOCATION="${LOCATION:-westeurope}"            # or italynorth
ENV_NAME="${ENV_NAME:-cae-shelly-monitor}"
LOG_NAME="${LOG_NAME:-log-shelly-monitor}"
APP_NAME="${APP_NAME:-shelly-monitor}"
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-}"        # globally unique, 3-24 lowercase letters/digits
SHARE_NAME="${SHARE_NAME:-shelly-data}"
SHARE_QUOTA_GB="${SHARE_QUOTA_GB:-1}"
ENV_STORAGE_NAME="${ENV_STORAGE_NAME:-shellydata}"
IMAGE="${IMAGE:-ghcr.io/an4oh/openshellymonitor:latest}"
TZ_NAME="${TZ_NAME:-Europe/Rome}"
CPU="${CPU:-0.25}"
MEMORY="${MEMORY:-0.5Gi}"
# Optional private GHCR image: REGISTRY_USERNAME + REGISTRY_PASSWORD (PAT with read:packages)
REGISTRY_USERNAME="${REGISTRY_USERNAME:-}"
REGISTRY_PASSWORD="${REGISTRY_PASSWORD:-}"
# ---------------------------------------------------------------------------------

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }
# YAML double-quoted scalar
yq() { local s=${1//\\/\\\\}; s=${s//\"/\\\"}; printf '"%s"' "$s"; }

command -v az >/dev/null || die "Azure CLI (az) not found"
az account show >/dev/null 2>&1 || die "run 'az login' first"
az extension add --name containerapp --upgrade --only-show-errors
az provider register --namespace Microsoft.App --only-show-errors >/dev/null
az provider register --namespace Microsoft.OperationalInsights --only-show-errors >/dev/null
az provider register --namespace Microsoft.Storage --only-show-errors >/dev/null

if [[ -z "$STORAGE_ACCOUNT" ]]; then
  # Stable per subscription + resource group, so re-runs find the same account.
  suffix=$(printf '%s/%s' "$(az account show --query id -o tsv)" "$RG" | sha256sum | cut -c1-8)
  STORAGE_ACCOUNT="shellymon${suffix}"
fi

app_exists=false
if az containerapp show -n "$APP_NAME" -g "$RG" -o none 2>/dev/null; then
  app_exists=true
fi

existing_secret() {  # existing_secret NAME -> value or empty
  $app_exists || return 0
  az containerapp secret show -n "$APP_NAME" -g "$RG" --secret-name "$1" \
    --query value -o tsv 2>/dev/null || true
}

SHELLY_HOMES="${SHELLY_HOMES:-$(existing_secret shelly-homes)}"
SHELLY_WEB_PASSWORD="${SHELLY_WEB_PASSWORD:-$(existing_secret shelly-web-password)}"
SHELLY_WEB_COOKIE_SECRET="${SHELLY_WEB_COOKIE_SECRET:-$(existing_secret shelly-web-cookie-secret)}"
[[ -n "$SHELLY_HOMES" ]] || die "export SHELLY_HOMES='Name=key@server;...'"
[[ -n "$SHELLY_WEB_PASSWORD" ]] || die "export SHELLY_WEB_PASSWORD (the dashboard is public)"
if [[ -z "$SHELLY_WEB_COOKIE_SECRET" ]]; then
  SHELLY_WEB_COOKIE_SECRET=$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')
fi

if $app_exists; then
  # Keep the image deployed by the workflow; IMAGE only seeds a new app.
  IMAGE=$(az containerapp show -n "$APP_NAME" -g "$RG" \
    --query 'properties.template.containers[0].image' -o tsv)
fi

log "Resource group $RG ($LOCATION)"
az group create -n "$RG" -l "$LOCATION" -o none

log "Log Analytics workspace $LOG_NAME"
if ! az monitor log-analytics workspace show -g "$RG" -n "$LOG_NAME" -o none 2>/dev/null; then
  az monitor log-analytics workspace create -g "$RG" -n "$LOG_NAME" -l "$LOCATION" \
    --retention-time 30 -o none
fi
LOG_ID=$(az monitor log-analytics workspace show -g "$RG" -n "$LOG_NAME" --query customerId -o tsv)
LOG_KEY=$(az monitor log-analytics workspace get-shared-keys -g "$RG" -n "$LOG_NAME" \
  --query primarySharedKey -o tsv)

log "Container Apps environment $ENV_NAME"
if ! az containerapp env show -n "$ENV_NAME" -g "$RG" -o none 2>/dev/null; then
  az containerapp env create -n "$ENV_NAME" -g "$RG" -l "$LOCATION" \
    --logs-workspace-id "$LOG_ID" --logs-workspace-key "$LOG_KEY" -o none
fi
ENV_ID=$(az containerapp env show -n "$ENV_NAME" -g "$RG" --query id -o tsv)

log "Storage account $STORAGE_ACCOUNT, file share $SHARE_NAME"
if ! az storage account show -n "$STORAGE_ACCOUNT" -g "$RG" -o none 2>/dev/null; then
  az storage account create -n "$STORAGE_ACCOUNT" -g "$RG" -l "$LOCATION" \
    --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 \
    --allow-blob-public-access false -o none
fi
az storage share-rm create --storage-account "$STORAGE_ACCOUNT" -g "$RG" \
  --name "$SHARE_NAME" --quota "$SHARE_QUOTA_GB" --enabled-protocols SMB -o none 2>/dev/null \
  || az storage share-rm show --storage-account "$STORAGE_ACCOUNT" -g "$RG" --name "$SHARE_NAME" -o none
STORAGE_KEY=$(az storage account keys list -n "$STORAGE_ACCOUNT" -g "$RG" --query '[0].value' -o tsv)

log "Environment storage $ENV_STORAGE_NAME -> $STORAGE_ACCOUNT/$SHARE_NAME"
az containerapp env storage set -n "$ENV_NAME" -g "$RG" \
  --storage-name "$ENV_STORAGE_NAME" \
  --azure-file-account-name "$STORAGE_ACCOUNT" \
  --azure-file-account-key "$STORAGE_KEY" \
  --azure-file-share-name "$SHARE_NAME" \
  --access-mode ReadWrite -o none

# Full app spec. Volumes cannot be set with create/update flags, so the app is
# created and updated from YAML. The file holds secrets: private temp dir, removed on exit.
tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT
chmod 700 "$tmpdir"
spec="$tmpdir/app.yaml"

registry_yaml=""
registry_secret_yaml=""
if [[ -n "$REGISTRY_USERNAME" && -n "$REGISTRY_PASSWORD" ]]; then
  registry_yaml="    registries:
    - server: ghcr.io
      username: $(yq "$REGISTRY_USERNAME")
      passwordSecretRef: registry-password"
  registry_secret_yaml="    - name: registry-password
      value: $(yq "$REGISTRY_PASSWORD")"
fi

umask 077
cat > "$spec" <<YAML
location: $(yq "$LOCATION")
type: Microsoft.App/containerApps
properties:
  managedEnvironmentId: $(yq "$ENV_ID")
  configuration:
    activeRevisionsMode: Single
    ingress:
      external: true
      targetPort: 8787
      transport: auto
      allowInsecure: false
    secrets:
    - name: shelly-homes
      value: $(yq "$SHELLY_HOMES")
    - name: shelly-web-password
      value: $(yq "$SHELLY_WEB_PASSWORD")
    - name: shelly-web-cookie-secret
      value: $(yq "$SHELLY_WEB_COOKIE_SECRET")
$registry_secret_yaml
$registry_yaml
  template:
    containers:
    - name: shelly-monitor
      image: $(yq "$IMAGE")
      args: ["serve", "--host", "0.0.0.0", "--poll"]
      resources:
        cpu: $CPU
        memory: $MEMORY
      env:
      - name: SHELLY_HOMES
        secretRef: shelly-homes
      - name: SHELLY_WEB_PASSWORD
        secretRef: shelly-web-password
      - name: SHELLY_WEB_COOKIE_SECRET
        secretRef: shelly-web-cookie-secret
      - name: SHELLY_DB
        value: /data/shelly-monitor.db
      - name: SHELLY_DB_JOURNAL
        value: delete
      - name: SHELLY_BACKUP_DIR
        value: /data/backups
      - name: SHELLY_WEB_TRUST_PROXY
        value: "1"
      - name: TZ
        value: $(yq "$TZ_NAME")
      volumeMounts:
      - volumeName: data
        mountPath: /data
      probes:
      - type: Startup
        httpGet:
          path: /healthz
          port: 8787
        periodSeconds: 10
        failureThreshold: 30
      - type: Liveness
        httpGet:
          path: /healthz
          port: 8787
        periodSeconds: 30
        timeoutSeconds: 5
        failureThreshold: 3
      - type: Readiness
        httpGet:
          path: /healthz
          port: 8787
        periodSeconds: 15
        timeoutSeconds: 5
        failureThreshold: 3
    scale:
      minReplicas: 1
      maxReplicas: 1
    volumes:
    - name: data
      storageType: AzureFile
      storageName: $ENV_STORAGE_NAME
      mountOptions: "uid=10001,gid=10001,dir_mode=0770,file_mode=0660"
YAML

if $app_exists; then
  log "Updating container app $APP_NAME (image $IMAGE)"
  az containerapp update -n "$APP_NAME" -g "$RG" --yaml "$spec" -o none
else
  log "Creating container app $APP_NAME (image $IMAGE)"
  az containerapp create -n "$APP_NAME" -g "$RG" --yaml "$spec" -o none
fi

FQDN=$(az containerapp show -n "$APP_NAME" -g "$RG" --query properties.configuration.ingress.fqdn -o tsv)
echo
echo "Dashboard:  https://$FQDN/"
echo "Health:     https://$FQDN/healthz"
echo "GitHub variables: AZURE_RESOURCE_GROUP=$RG  AZURE_CONTAINERAPP_NAME=$APP_NAME"
echo "Storage account for backups: $STORAGE_ACCOUNT (share $SHARE_NAME)"
