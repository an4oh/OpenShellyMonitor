#!/usr/bin/env bash
# Lets GitHub Actions deploy to Azure without stored passwords (OpenID Connect).
# Creates an app registration + service principal, a federated credential that trusts
# only pushes to the main branch of GITHUB_REPO, and the Contributor role on the
# resource group. Re-runnable. Prints the GitHub secrets/variables to set.
#
#   az login
#   GITHUB_REPO=your-user/OpenShellyMonitor ./deploy/azure/github-oidc.sh
set -euo pipefail

GITHUB_REPO="${GITHUB_REPO:-an4oh/OpenShellyMonitor}"   # owner/name
BRANCH="${BRANCH:-main}"
RG="${RG:-rg-shelly-monitor}"
APP_NAME="${APP_NAME:-shelly-monitor}"
SP_NAME="${SP_NAME:-github-${GITHUB_REPO//\//-}-deploy}"

command -v az >/dev/null || { echo "error: Azure CLI (az) not found" >&2; exit 1; }
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
SCOPE=$(az group show -n "$RG" --query id -o tsv)   # run setup.sh first

CLIENT_ID=$(az ad app list --display-name "$SP_NAME" --query '[0].appId' -o tsv)
if [[ -z "$CLIENT_ID" ]]; then
  echo "==> App registration $SP_NAME"
  CLIENT_ID=$(az ad app create --display-name "$SP_NAME" --query appId -o tsv)
fi
az ad sp show --id "$CLIENT_ID" -o none 2>/dev/null || az ad sp create --id "$CLIENT_ID" -o none

CRED_NAME="github-${BRANCH//\//-}"
if [[ -z "$(az ad app federated-credential list --id "$CLIENT_ID" --query "[?name=='$CRED_NAME'].name" -o tsv)" ]]; then
  echo "==> Federated credential repo:$GITHUB_REPO:ref:refs/heads/$BRANCH"
  az ad app federated-credential create --id "$CLIENT_ID" --parameters "{
    \"name\": \"$CRED_NAME\",
    \"issuer\": \"https://token.actions.githubusercontent.com\",
    \"subject\": \"repo:$GITHUB_REPO:ref:refs/heads/$BRANCH\",
    \"audiences\": [\"api://AzureADTokenExchange\"]
  }" -o none
fi

echo "==> Contributor on $RG"
az role assignment create --assignee "$CLIENT_ID" --role Contributor --scope "$SCOPE" -o none

cat <<EOF

Done. Set these on GitHub (Settings > Secrets and variables > Actions), or with gh:

  gh secret set AZURE_CLIENT_ID       --repo $GITHUB_REPO --body $CLIENT_ID
  gh secret set AZURE_TENANT_ID       --repo $GITHUB_REPO --body $TENANT_ID
  gh secret set AZURE_SUBSCRIPTION_ID --repo $GITHUB_REPO --body $SUBSCRIPTION_ID
  gh variable set AZURE_RESOURCE_GROUP    --repo $GITHUB_REPO --body $RG
  gh variable set AZURE_CONTAINERAPP_NAME --repo $GITHUB_REPO --body $APP_NAME
  gh variable set AZURE_DEPLOY            --repo $GITHUB_REPO --body true

Role assignments can take a few minutes to propagate.
EOF
