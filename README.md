# Entra Agent Identity Tool (TL;DR)

A Quick sample Python based script to enumerate Azure AI Foundry Agent Identities (Entra ID Service Principals) and manage their API permissions.

## Setup

```bash
pip install azure-identity msgraph-sdk httpx
az login
```

## Common Usage

### 1. List Identities
**Basic List (JSON output)**
```bash
python entraAgentId.py
```

**With AI Foundry Discovery (Recommended)**
Retrieves full agent details by querying the ARM control plane.
```bash
python entraAgentId.py --arm-agents /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/{account}/projects/{project}
```

### 2. Manage Permissions

**List Available Permissions**
```bash
python entraAgentId.py --list-permissions --api microsoft-graph
```

**Grant Permissions**
```bash
# Grant a single permission
python entraAgentId.py --grant-permission --identity <object-id> --api microsoft-graph --permission User.Read.All

# Grant multiple permissions
python entraAgentId.py --grant-permission --identity <object-id> --api microsoft-graph --permission User.Read.All --permission Mail.Read
```

**Revoke Permissions**
```bash
python entraAgentId.py --revoke-permission --identity <object-id> --api microsoft-graph --permission User.Read.All
```

### 3. Manage Owners

**Add User as Owner**
```bash
python entraAgentId.py --add-owner --identity <object-id> --user user@example.com
```

## Key Arguments

| Argument | Description |
|----------|-------------|
| `--output`, `-o` | Output format: `json` (default) or `text` |
| `--interactive`, `-i` | Use interactive browser login instead of Azure CLI/Env vars |
| `--identity` | Service Principal Object ID (target for operations) |
| `--client-id` | Application/Client ID (alternative to `--identity`) |
| `--api` | Target API (e.g., `microsoft-graph`, `sharepoint`) |
| `--permission` | Permission name (e.g., `User.Read.All`) |
