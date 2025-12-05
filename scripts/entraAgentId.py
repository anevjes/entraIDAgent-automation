#!/usr/bin/env python3
"""
Script to list Entra Agent Identities and their app role assignments.

This script uses Microsoft Graph API (beta) to:
1. List all agent identities (service principals with servicePrincipalType = 'ServiceIdentity')
2. For each agent identity, retrieve:
   - App role assignments (roles assigned TO this agent identity)
   - App roles assigned to others (roles this agent identity grants to others)
3. Optionally look up the associated Azure AI Foundry agent details via control plane REST API

Requirements:
    pip install azure-identity msgraph-sdk httpx

Authentication:
    Uses DefaultAzureCredential which supports multiple authentication methods:
    - Azure CLI (az login)
    - Environment variables
    - Managed Identity
    - Visual Studio Code credentials
"""

import asyncio
import argparse
import json
import re
import time
from datetime import datetime

try:
    from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential
    from msgraph import GraphServiceClient
    from msgraph.generated.service_principals.service_principals_request_builder import (
        ServicePrincipalsRequestBuilder,
    )
    from msgraph.generated.applications.applications_request_builder import (
        ApplicationsRequestBuilder,
    )
    import httpx
except ImportError:
    print("Required packages not installed. Please run:")
    print("  pip install azure-identity msgraph-sdk httpx")
    exit(1)




class AgentIdentityManager:
    """Manager class for Entra Agent Identity operations."""

    GRAPH_SCOPES = ["https://graph.microsoft.com/.default"]
    BETA_ENDPOINT = "https://graph.microsoft.com/beta"

    def __init__(self, use_interactive: bool = False, foundry_endpoints: list = None):
        """
        Initialize the manager with appropriate credentials.

        Args:
            use_interactive: If True, use interactive browser authentication
            foundry_endpoints: List of Azure AI Foundry project endpoints to query for agents
            arm_project_ids: List of ARM resource IDs for AI Foundry projects (for applications lookup)
        """
        if use_interactive:
            self.credential = InteractiveBrowserCredential()
        else:
            self.credential = DefaultAzureCredential()

        self.client = GraphServiceClient(
            credentials=self.credential,
            scopes=self.GRAPH_SCOPES,
        )
        
        self.foundry_endpoints = foundry_endpoints or []
        self.foundry_agents_cache = {}  # Cache of agents by app_id/name
        self.project_applications_cache = {}  # Cache of project applications (identity associations)
        self._token_cache = {}  # Cache tokens to avoid repeated credential calls

    def _get_token(self, scope: str) -> str:
        """
        Get an access token with caching to avoid repeated credential calls.
        
        Args:
            scope: The token scope
            
        Returns:
            The access token string
        """
        # Check if we have a cached token that's still valid (with 5 min buffer)
        if scope in self._token_cache:
            cached_token, expires_on = self._token_cache[scope]
            if time.time() < expires_on - 300:  # 5 minute buffer
                return cached_token
        
        # Get new token
        token = self.credential.get_token(scope)
        self._token_cache[scope] = (token.token, token.expires_on)
        return token.token

    async def load_project_applications(self, arm_resource_id: str):
        """Load applications from an AI Foundry project via ARM API."""
        try:
            token = self._get_token("https://management.azure.com/.default")
        except Exception:
            return
        
        try:
            url = f"https://management.azure.com{arm_resource_id}?api-version=2025-10-01-preview"
            
            async with httpx.AsyncClient(http2=False, timeout=30.0) as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
                
                if response.status_code == 200:
                    data = response.json()
                    for app in data.get("value", []):
                        props = app.get("properties", {})
                        blueprint = props.get("agentIdentityBlueprint", {})
                        instance_identity = props.get("defaultInstanceIdentity", {})
                        agents = props.get("agents", [])
                        
                        app_info = {
                            "id": app.get("id"),
                            "name": app.get("name"),
                            "type": app.get("type"),
                            "displayName": props.get("displayName") or app.get("name"),
                            "applicationId": props.get("applicationId"),
                            "baseUrl": props.get("baseUrl"),
                            "isEnabled": props.get("isEnabled"),
                            "provisioningState": props.get("provisioningState"),
                            "blueprintKind": blueprint.get("kind"),
                            "blueprintType": blueprint.get("type"),
                            "blueprintClientId": blueprint.get("clientId"),
                            "blueprintPrincipalId": blueprint.get("principalId"),
                            "blueprintTenantId": blueprint.get("tenantId"),
                            "instanceKind": instance_identity.get("kind"),
                            "instanceType": instance_identity.get("type"),
                            "instanceClientId": instance_identity.get("clientId"),
                            "instancePrincipalId": instance_identity.get("principalId"),
                            "instanceTenantId": instance_identity.get("tenantId"),
                            "agents": agents,
                        }
                        
                        # Cache by various keys
                        if app_info.get("applicationId"):
                            self.project_applications_cache[f"appid:{app_info['applicationId']}"] = app_info
                        if app_info.get("blueprintClientId"):
                            self.project_applications_cache[f"clientid:{app_info['blueprintClientId']}"] = app_info
                        if app_info.get("blueprintPrincipalId"):
                            self.project_applications_cache[f"spid:{app_info['blueprintPrincipalId']}"] = app_info
                        if app_info.get("instanceClientId"):
                            self.project_applications_cache[f"instanceclientid:{app_info['instanceClientId']}"] = app_info
                        if app_info.get("instancePrincipalId"):
                            self.project_applications_cache[f"instanceprincipalid:{app_info['instancePrincipalId']}"] = app_info
                        
                        for agent in agents:
                            if agent.get("agentId"):
                                self.project_applications_cache[f"agentid:{agent['agentId']}"] = {
                                    **app_info,
                                    "agentId": agent.get("agentId"),
                                    "agentName": agent.get("agentName"),
                                }
        except Exception:
            pass

    async def load_agents_from_arm(self, arm_project_base: str):
        """Load agents and their Entra identities from the ARM API."""
        try:
            token = self._get_token("https://management.azure.com/.default")
        except Exception:
            return
        
        try:
            project_url = f"https://management.azure.com{arm_project_base}?api-version=2025-06-01"
            
            async with httpx.AsyncClient(http2=False, timeout=30.0) as client:
                proj_response = await client.get(
                    project_url,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
                
                if proj_response.status_code == 200:
                    project_data = proj_response.json()
                    proj_props = project_data.get("properties", {})
                    project_agent_identity = proj_props.get("agentIdentity", {})
                    
                    if project_agent_identity:
                        identity_id = project_agent_identity.get('agentIdentityId')
                        blueprint_id = project_agent_identity.get('agentIdentityBlueprintId')
                        if identity_id:
                            self.project_applications_cache[f"projectidentity:{identity_id}"] = {
                                "projectId": project_data.get("id"),
                                "projectName": project_data.get("name"),
                                "agentIdentityId": identity_id,
                                "agentIdentityBlueprintId": blueprint_id,
                                "type": "project-level-identity",
                            }
                
                apps_url = f"https://management.azure.com{arm_project_base}/applications?api-version=2025-10-01-preview"
                apps_response = await client.get(
                    apps_url,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
                
                if apps_response.status_code == 200:
                    apps_data = apps_response.json()
                    
                    for app in apps_data.get("value", []):
                        props = app.get("properties", {})
                        agents = props.get("agents", [])
                        blueprint = props.get("agentIdentityBlueprint", {})
                        instance_identity = props.get("defaultInstanceIdentity", {})
                        app_name = props.get("displayName") or app.get("name")
                        
                        for agent in agents:
                            agent_name = agent.get("agentName", "N/A")
                            agent_id = agent.get("agentId", "N/A")
                            
                            agent_info = {
                                "agentId": agent_id,
                                "agentName": agent_name,
                                "applicationId": props.get("applicationId"),
                                "applicationName": app_name,
                                "baseUrl": props.get("baseUrl"),
                                "isEnabled": props.get("isEnabled"),
                                "blueprintKind": blueprint.get("kind"),
                                "blueprintType": blueprint.get("type"),
                                "blueprintClientId": blueprint.get("clientId"),
                                "blueprintPrincipalId": blueprint.get("principalId"),
                                "blueprintTenantId": blueprint.get("tenantId"),
                                "instanceKind": instance_identity.get("kind"),
                                "instanceType": instance_identity.get("type"),
                                "instanceClientId": instance_identity.get("clientId"),
                                "instancePrincipalId": instance_identity.get("principalId"),
                                "instanceTenantId": instance_identity.get("tenantId"),
                                "from_arm_api": True,
                            }
                            
                            if agent_name and agent_name != "N/A":
                                self.foundry_agents_cache[agent_name.lower()] = agent_info
                                self.foundry_agents_cache[f"arm:{agent_name.lower()}"] = agent_info
                            if agent_id and agent_id != "N/A":
                                self.foundry_agents_cache[agent_id] = agent_info
                            if agent_id:
                                self.project_applications_cache[f"agentid:{agent_id}"] = {**agent_info, "agents": [agent]}
                        
                        app_info = {
                            "id": app.get("id"),
                            "name": app.get("name"),
                            "displayName": app_name,
                            "applicationId": props.get("applicationId"),
                            "baseUrl": props.get("baseUrl"),
                            "isEnabled": props.get("isEnabled"),
                            "blueprintKind": blueprint.get("kind"),
                            "blueprintType": blueprint.get("type"),
                            "blueprintClientId": blueprint.get("clientId"),
                            "blueprintPrincipalId": blueprint.get("principalId"),
                            "blueprintTenantId": blueprint.get("tenantId"),
                            "instanceKind": instance_identity.get("kind"),
                            "instanceType": instance_identity.get("type"),
                            "instanceClientId": instance_identity.get("clientId"),
                            "instancePrincipalId": instance_identity.get("principalId"),
                            "instanceTenantId": instance_identity.get("tenantId"),
                            "agents": agents,
                        }
                        
                        if props.get("applicationId"):
                            self.project_applications_cache[f"appid:{props['applicationId']}"] = app_info
                        if blueprint.get("clientId"):
                            self.project_applications_cache[f"clientid:{blueprint['clientId']}"] = app_info
                        if blueprint.get("principalId"):
                            self.project_applications_cache[f"spid:{blueprint['principalId']}"] = app_info
                        if instance_identity.get("clientId"):
                            self.project_applications_cache[f"instanceclientid:{instance_identity['clientId']}"] = app_info
                        if instance_identity.get("principalId"):
                            self.project_applications_cache[f"instanceprincipalid:{instance_identity['principalId']}"] = app_info
        except Exception:
            pass

    async def load_foundry_agents(self):
        """Load agents from configured Azure AI Foundry project endpoints."""
        if not self.foundry_endpoints:
            return
        
        try:
            token = self._get_token("https://ai.azure.com/.default")
        except Exception:
            return
        
        for endpoint in self.foundry_endpoints:
            try:
                base_url = endpoint.rstrip('/')
                endpoint_match = re.match(r'https://([^.]+)\.services\.ai\.azure\.com/api/projects/([^/]+)', endpoint)
                resource_name = endpoint_match.group(1) if endpoint_match else None
                project_name = endpoint_match.group(2) if endpoint_match else None
                api_versions = ["2025-05-01-preview", "2025-05-15-preview", "2024-12-01-preview", "2024-07-01-preview"]
                
                async with httpx.AsyncClient(timeout=30.0, http2=False) as client:
                    for api_version in api_versions:
                        agents_url = f"{base_url}/assistants?api-version={api_version}"
                        response = await client.get(
                            agents_url,
                            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        )
                        
                        if response.status_code == 200:
                            data = response.json()
                            agents = data.get("data", [])
                            for agent in agents:
                                if "id" not in agent and "assistantId" in agent:
                                    agent["id"] = agent["assistantId"]
                            
                            project_key = f"{resource_name}-{project_name}".lower() if resource_name and project_name else None
                            
                            for agent in agents:
                                created_at = agent.get("created_at")
                                created_at_str = None
                                if created_at:
                                    try:
                                        created_at_str = datetime.fromtimestamp(created_at).isoformat()
                                    except:
                                        created_at_str = str(created_at)
                                
                                agent_info = {
                                    "id": agent.get("id"),
                                    "object": agent.get("object"),
                                    "name": agent.get("name"),
                                    "description": agent.get("description"),
                                    "model": agent.get("model"),
                                    "instructions": agent.get("instructions"),
                                    "instructions_preview": (agent.get("instructions", "")[:100] + "...") if agent.get("instructions") and len(agent.get("instructions", "")) > 100 else agent.get("instructions"),
                                    "created_at": created_at,
                                    "created_at_formatted": created_at_str,
                                    "tools": agent.get("tools", []),
                                    "tool_resources": agent.get("tool_resources", {}),
                                    "metadata": agent.get("metadata", {}),
                                    "temperature": agent.get("temperature"),
                                    "top_p": agent.get("top_p"),
                                    "response_format": agent.get("response_format"),
                                    "project_endpoint": endpoint,
                                    "resource_name": resource_name,
                                    "project_name": project_name,
                                }
                                
                                if agent.get("name"):
                                    self.foundry_agents_cache[agent["name"].lower()] = agent_info
                                if agent.get("id"):
                                    self.foundry_agents_cache[agent["id"]] = agent_info
                                if project_key:
                                    if f"project:{project_key}" not in self.foundry_agents_cache:
                                        self.foundry_agents_cache[f"project:{project_key}"] = []
                                    self.foundry_agents_cache[f"project:{project_key}"].append(agent_info)
                            break
                        elif response.status_code == 400 and "API version" in response.text:
                            continue
                        else:
                            break
            except Exception:
                pass

    def match_foundry_agents(self, identity_display_name: str, app_id: str = None, sp_id: str = None, tags: list = None) -> list:
        """
        Try to match an Entra agent identity to Foundry agents.
        
        The identity is tied to a project, so one identity can have multiple agents.
        
        Args:
            identity_display_name: The display name of the agent identity
            app_id: The application ID of the agent identity
            sp_id: The service principal ID of the agent identity  
            tags: Tags from the agent identity that might contain agent info
            
        Returns:
            List of Foundry agent info dicts, or empty list if no match
        """
        results = []
        
        # First, check project_applications_cache for direct agent-identity mapping
        # These come from the ARM API /applications endpoint
        if self.project_applications_cache:
            matched_app_info = None
            
            # Try matching by app_id (application ID)
            if app_id and f"appid:{app_id}" in self.project_applications_cache:
                matched_app_info = self.project_applications_cache[f"appid:{app_id}"]
            
            # Try matching by sp_id (service principal ID = blueprintPrincipalId or instancePrincipalId)
            if not matched_app_info and sp_id:
                if f"spid:{sp_id}" in self.project_applications_cache:
                    matched_app_info = self.project_applications_cache[f"spid:{sp_id}"]
                elif f"instanceprincipalid:{sp_id}" in self.project_applications_cache:
                    matched_app_info = self.project_applications_cache[f"instanceprincipalid:{sp_id}"]
            
            # Try matching by clientId
            if not matched_app_info and app_id:
                if f"clientid:{app_id}" in self.project_applications_cache:
                    matched_app_info = self.project_applications_cache[f"clientid:{app_id}"]
                elif f"instanceclientid:{app_id}" in self.project_applications_cache:
                    matched_app_info = self.project_applications_cache[f"instanceclientid:{app_id}"]
            
            if matched_app_info:
                # Get agents from the application info
                agents_from_app = matched_app_info.get("agents", [])
                
                for agent in agents_from_app:
                    agent_id = agent.get("agentId")
                    agent_name = agent.get("agentName")
                    
                    # Check if we have full agent details in the foundry_agents_cache
                    if agent_id and agent_id in self.foundry_agents_cache:
                        full_agent = self.foundry_agents_cache[agent_id]
                        # Add ARM API info to the agent
                        full_agent["from_arm_api"] = True
                        full_agent["arm_application_id"] = matched_app_info.get("applicationId")
                        full_agent["arm_blueprint_principal_id"] = matched_app_info.get("blueprintPrincipalId")
                        full_agent["arm_instance_principal_id"] = matched_app_info.get("instancePrincipalId")
                        if full_agent not in results:
                            results.append(full_agent)
                    else:
                        # Create a partial agent info from ARM API data
                        arm_agent = {
                            "id": agent_id,
                            "name": agent_name,
                            "from_arm_api": True,
                            "arm_application_id": matched_app_info.get("applicationId"),
                            "arm_application_name": matched_app_info.get("displayName"),
                            "arm_blueprint_client_id": matched_app_info.get("blueprintClientId"),
                            "arm_blueprint_principal_id": matched_app_info.get("blueprintPrincipalId"),
                            "arm_instance_client_id": matched_app_info.get("instanceClientId"),
                            "arm_instance_principal_id": matched_app_info.get("instancePrincipalId"),
                            "arm_base_url": matched_app_info.get("baseUrl"),
                        }
                        if arm_agent not in results:
                            results.append(arm_agent)
        
        if results:
            return results
        
        # Fall back to foundry_agents_cache matching
        if not self.foundry_agents_cache:
            return []
        
        # Extract resource and project from identity display name
        # Pattern: {resource}-{project}-AgentIdentity
        match = re.match(r'^(.+)-([^-]+)-AgentIdentity$', identity_display_name)
        if match:
            resource_name = match.group(1).lower()
            project_name = match.group(2).lower()
            project_key = f"project:{resource_name}-{project_name}"
            
            if project_key in self.foundry_agents_cache:
                return self.foundry_agents_cache[project_key]
        
        # Fallback: try matching by app ID or service principal ID
        if app_id:
            if f"appid:{app_id}" in self.foundry_agents_cache:
                result = self.foundry_agents_cache[f"appid:{app_id}"]
                return [result] if isinstance(result, dict) else result
        
        if sp_id:
            if f"spid:{sp_id}" in self.foundry_agents_cache:
                result = self.foundry_agents_cache[f"spid:{sp_id}"]
                return [result] if isinstance(result, dict) else result
            
        # Fallback: try matching by name parts
        if "-AgentIdentity" in identity_display_name:
            name_part = identity_display_name.replace("-AgentIdentity", "")
            parts = name_part.split("-")
            
            if len(parts) >= 1:
                # Try the last part (most likely the agent name)
                potential_name = parts[-1].lower()
                if potential_name in self.foundry_agents_cache:
                    return [self.foundry_agents_cache[potential_name]]
        
        return []

    async def list_agent_identities(self) -> list:
        """
        List all agent identities in the directory.

        Agent identities are service principals with servicePrincipalType = 'ServiceIdentity'.

        Returns:
            List of agent identity objects
        """
        agent_identities = []

        try:
            # Query service principals filtered by servicePrincipalType
            # Agent identities have servicePrincipalType = 'ServiceIdentity'
            query_params = ServicePrincipalsRequestBuilder.ServicePrincipalsRequestBuilderGetQueryParameters(
                filter="servicePrincipalType eq 'ServiceIdentity'",
                select=["id", "displayName", "appId", "servicePrincipalType", 
                        "accountEnabled", "createdDateTime", "tags", "notes",
                        "description", "alternativeNames"],
                top=100,
            )
            request_config = ServicePrincipalsRequestBuilder.ServicePrincipalsRequestBuilderGetRequestConfiguration(
                query_parameters=query_params,
            )

            result = await self.client.service_principals.get(
                request_configuration=request_config
            )

            if result and result.value:
                agent_identities.extend(result.value)

                # Handle pagination
                while result.odata_next_link:
                    result = await self.client.service_principals.with_url(
                        result.odata_next_link
                    ).get()
                    if result and result.value:
                        agent_identities.extend(result.value)

        except Exception as e:
            print(f"Error listing agent identities: {e}")
            raise

        return agent_identities

    async def get_app_role_assignments(self, service_principal_id: str) -> list:
        """
        Get app role assignments granted TO this service principal.

        These are the roles/permissions that have been assigned to this agent identity.

        Args:
            service_principal_id: The ID of the service principal

        Returns:
            List of app role assignment objects
        """
        assignments = []

        try:
            result = await self.client.service_principals.by_service_principal_id(
                service_principal_id
            ).app_role_assignments.get()

            if result and result.value:
                assignments.extend(result.value)

                # Handle pagination
                while result.odata_next_link:
                    result = await self.client.service_principals.by_service_principal_id(
                        service_principal_id
                    ).app_role_assignments.with_url(result.odata_next_link).get()
                    if result and result.value:
                        assignments.extend(result.value)

        except Exception as e:
            print(f"Error getting app role assignments for {service_principal_id}: {e}")

        return assignments

    async def get_agent_identity_blueprint(self, blueprint_id: str) -> dict:
        """Get agent identity blueprint details."""
        try:
            query_params = ApplicationsRequestBuilder.ApplicationsRequestBuilderGetQueryParameters(
                filter=f"appId eq '{blueprint_id}'",
                select=["id", "displayName", "appId", "description", "tags", "notes"],
                top=1,
            )
            request_config = ApplicationsRequestBuilder.ApplicationsRequestBuilderGetRequestConfiguration(
                query_parameters=query_params,
            )

            result = await self.client.applications.get(request_configuration=request_config)

            if result and result.value and len(result.value) > 0:
                app = result.value[0]
                return {
                    "id": app.id,
                    "displayName": app.display_name,
                    "appId": app.app_id,
                    "description": app.description,
                    "tags": app.tags if app.tags else [],
                }
        except Exception:
            pass
        return None

    async def get_agent_identity_extended_info(self, agent_id: str) -> dict:
        """Get extended agent identity information using direct API call."""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                token = self._get_token("https://graph.microsoft.com/.default")
                async with httpx.AsyncClient(http2=False, timeout=30.0) as client:
                    response = await client.get(
                        f"https://graph.microsoft.com/beta/servicePrincipals/{agent_id}",
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        params={"$select": "id,displayName,appId,agentIdentityBlueprintId,notes,description,alternativeNames"}
                    )
                    if response.status_code == 200:
                        return response.json()
                    elif response.status_code == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
            except Exception:
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
        return {}

    def parse_agent_name_from_tags(self, tags: list) -> str:
        """
        Parse agent name from tags if available.

        Azure AI Foundry agents often store metadata in tags.

        Args:
            tags: List of tag strings

        Returns:
            Agent name if found, otherwise None
        """
        if not tags:
            return None

        for tag in tags:
            # Look for common patterns in tags
            if tag.startswith("AgentName:"):
                return tag.replace("AgentName:", "").strip()
            if tag.startswith("agent:"):
                return tag.replace("agent:", "").strip()
            if tag.startswith("aiFoundryAgent:"):
                return tag.replace("aiFoundryAgent:", "").strip()

        return None

    def parse_agent_name_from_display_name(self, display_name: str) -> str:
        """
        Parse agent name from display name pattern.

        Azure AI Foundry creates identities with patterns like:
        - tl-aif-{projectId}-{agentName}-AgentIdentity
        - {prefix}-aif-{id}-{agentName}-AgentIdentity

        Args:
            display_name: The display name of the agent identity

        Returns:
            Parsed agent name or the original display name
        """
        if not display_name:
            return None

        # Check if it follows the Azure AI Foundry pattern
        if "-AgentIdentity" in display_name:
            # Remove the -AgentIdentity suffix
            name_part = display_name.replace("-AgentIdentity", "")
            
            # Split by hyphen and try to extract meaningful parts
            parts = name_part.split("-")
            
            # If it follows pattern like "tl-aif-xxxx-AgentName"
            if len(parts) >= 3 and "aif" in parts:
                aif_index = parts.index("aif")
                # The agent name is typically after the project ID
                if len(parts) > aif_index + 2:
                    return parts[-1]  # Last part before -AgentIdentity
            
            # Return last meaningful part
            if parts:
                return parts[-1]

        return display_name

    async def get_app_roles_assigned_to(self, service_principal_id: str) -> list:
        """
        Get app role assignments that this service principal has granted to others.

        These are the roles that users, groups, or other service principals
        have been assigned for this agent identity's app.

        Args:
            service_principal_id: The ID of the service principal

        Returns:
            List of app role assignment objects
        """
        assignments = []

        try:
            result = await self.client.service_principals.by_service_principal_id(
                service_principal_id
            ).app_role_assigned_to.get()

            if result and result.value:
                assignments.extend(result.value)

                # Handle pagination
                while result.odata_next_link:
                    result = await self.client.service_principals.by_service_principal_id(
                        service_principal_id
                    ).app_role_assigned_to.with_url(result.odata_next_link).get()
                    if result and result.value:
                        assignments.extend(result.value)

        except Exception as e:
            print(f"Error getting app roles assigned to others for {service_principal_id}: {e}")

        return assignments

    # Well-known API service principal App IDs
    WELL_KNOWN_APIS = {
        "microsoft-graph": "00000003-0000-0000-c000-000000000000",
        "graph": "00000003-0000-0000-c000-000000000000",
        "sharepoint": "00000003-0000-0ff1-ce00-000000000000",
        "exchange": "00000002-0000-0ff1-ce00-000000000000",
        "azure-management": "797f4846-ba00-4fd7-ba43-dac1f8f63013",
        "key-vault": "cfa8b339-82a2-471a-a3c9-0fc0be7a4093",
        "storage": "e406a681-f3d4-42a8-90b6-c2b029497af1",
    }

    # Common permission mappings (permission name -> app role ID for Microsoft Graph)
    COMMON_GRAPH_PERMISSIONS = {
        # SharePoint/Sites permissions
        "sites.read.all": "332a536c-c7ef-4017-ab91-336970924f0d",
        "sites.readwrite.all": "9492366f-7969-46a4-8d15-ed1a20078fff",
        "sites.manage.all": "0c0bf378-bf22-4481-8f81-9e89a9b4960a",
        "sites.fullcontrol.all": "a82116e5-55eb-4c41-a434-62fe8a61c773",
        "sites.selected": "883ea226-0bf2-4a8f-9f9d-92c9162a727d",
        # Files permissions
        "files.read.all": "01d4889c-1287-42c6-ac1f-5d1e02578ef6",
        "files.readwrite.all": "75359482-378d-4052-8f01-80520e7db3cd",
        # User permissions
        "user.read.all": "df021288-bdef-4463-88db-98f22de89214",
        "user.readwrite.all": "741f803b-c850-494e-b5df-cde7c675a1ca",
        # Group permissions
        "group.read.all": "5b567255-7703-4780-807c-7be8301ae99b",
        "group.readwrite.all": "62a82d76-70ea-41e2-9197-370581804d09",
        # Mail permissions
        "mail.read": "810c84a8-4a9e-49e6-bf7d-12d183f40d01",
        "mail.readwrite": "e2a3a72e-5f79-4c64-b1b1-878b674786c9",
        "mail.send": "b633e1c5-b582-4048-a93e-9f11b44c7e96",
        # Calendar permissions
        "calendars.read": "798ee544-9d2d-430c-a058-570e29e34338",
        "calendars.readwrite": "ef54d2bf-783f-4e0f-bca1-3210c0444d99",
        # Directory permissions
        "directory.read.all": "7ab1d382-f21e-4acd-a863-ba3e13f7da61",
        "directory.readwrite.all": "19dbc75e-c2e2-444c-a770-ec69d8559fc7",
        # Application permissions
        "application.read.all": "9a5d68dd-52b0-4cc2-bd40-abcf44ac3a30",
        # OpenID/Profile
        "openid": "37f7f235-527c-4136-accd-4a02d197296e",
        "profile": "14dad69e-099b-42c9-810b-d002981feec1",
        "email": "64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0",
        "offline_access": "7427e0e9-2fba-42fe-b0c0-848c9e6a8182",
    }

    async def find_resource_service_principal(self, api_name_or_id: str) -> dict:
        """
        Find the service principal for a well-known API like Microsoft Graph.
        
        Args:
            api_name_or_id: Either a well-known name (e.g., 'microsoft-graph', 'sharepoint')
                           or an App ID GUID
        
        Returns:
            Dictionary with service principal info or None if not found
        """
        # Check if it's a well-known API name
        app_id = self.WELL_KNOWN_APIS.get(api_name_or_id.lower(), api_name_or_id)
        
        try:
            query_params = ServicePrincipalsRequestBuilder.ServicePrincipalsRequestBuilderGetQueryParameters(
                filter=f"appId eq '{app_id}'",
                select=["id", "displayName", "appId", "appRoles"],
                top=1,
            )
            request_config = ServicePrincipalsRequestBuilder.ServicePrincipalsRequestBuilderGetRequestConfiguration(
                query_parameters=query_params,
            )
            
            result = await self.client.service_principals.get(
                request_configuration=request_config
            )
            
            if result and result.value and len(result.value) > 0:
                sp = result.value[0]
                return {
                    "id": sp.id,
                    "displayName": sp.display_name,
                    "appId": sp.app_id,
                    "appRoles": [
                        {
                            "id": str(role.id),
                            "displayName": role.display_name,
                            "value": role.value,
                            "description": role.description,
                            "isEnabled": role.is_enabled,
                            "allowedMemberTypes": role.allowed_member_types,
                        }
                        for role in (sp.app_roles or [])
                    ],
                }
        except Exception as e:
            print(f"Error finding resource service principal: {e}")
        
        return None

    async def find_agent_identity_by_id(self, identity_id: str) -> dict:
        """
        Find an agent identity by its ID (service principal ID or app ID).
        
        Args:
            identity_id: The service principal ID or app ID of the agent identity
            
        Returns:
            Dictionary with service principal info or None if not found
        """
        try:
            # First try by object ID
            token = self._get_token("https://graph.microsoft.com/.default")
            
            async with httpx.AsyncClient(http2=False, timeout=30.0) as client:
                response = await client.get(
                    f"https://graph.microsoft.com/v1.0/servicePrincipals/{identity_id}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
                
                if response.status_code == 200:
                    sp = response.json()
                    return {
                        "id": sp.get("id"),
                        "displayName": sp.get("displayName"),
                        "appId": sp.get("appId"),
                        "servicePrincipalType": sp.get("servicePrincipalType"),
                    }
                elif response.status_code == 404:
                    # Try by appId
                    query_params = ServicePrincipalsRequestBuilder.ServicePrincipalsRequestBuilderGetQueryParameters(
                        filter=f"appId eq '{identity_id}'",
                        select=["id", "displayName", "appId", "servicePrincipalType"],
                        top=1,
                    )
                    request_config = ServicePrincipalsRequestBuilder.ServicePrincipalsRequestBuilderGetRequestConfiguration(
                        query_parameters=query_params,
                    )
                    
                    result = await self.client.service_principals.get(
                        request_configuration=request_config
                    )
                    
                    if result and result.value and len(result.value) > 0:
                        sp = result.value[0]
                        return {
                            "id": sp.id,
                            "displayName": sp.display_name,
                            "appId": sp.app_id,
                            "servicePrincipalType": sp.service_principal_type,
                        }
        except Exception as e:
            print(f"Error finding agent identity: {e}")
        
        return None

    async def grant_api_permission(
        self,
        agent_identity_id: str,
        api_name_or_id: str,
        permission_name_or_id: str,
    ) -> dict:
        """Grant an API permission (app role) to an agent identity."""
        agent_sp = await self.find_agent_identity_by_id(agent_identity_id)
        if not agent_sp:
            return {"error": f"Agent identity not found: {agent_identity_id}"}
        
        resource_sp = await self.find_resource_service_principal(api_name_or_id)
        if not resource_sp:
            return {"error": f"API service principal not found: {api_name_or_id}"}
        
        permission_lower = permission_name_or_id.lower()
        app_role_id = None
        app_role_name = None
        
        if permission_lower in self.COMMON_GRAPH_PERMISSIONS:
            app_role_id = self.COMMON_GRAPH_PERMISSIONS[permission_lower]
            app_role_name = permission_name_or_id
        else:
            for role in resource_sp.get("appRoles", []):
                if (role["value"] and role["value"].lower() == permission_lower) or role["id"] == permission_name_or_id:
                    app_role_id = role["id"]
                    app_role_name = role["value"] or role["displayName"]
                    break
        
        if not app_role_id:
            available = [r["value"] for r in resource_sp.get("appRoles", []) if r["value"]]
            return {
                "error": f"Permission not found: {permission_name_or_id}",
                "available_permissions": available[:20],
                "hint": "Use --list-permissions to see all available permissions",
            }
        
        sp_type = agent_sp.get("servicePrincipalType", "")
        display_name = agent_sp.get("displayName", "")
        
        if sp_type == "Application" and ("AgentIdentityBlueprint" in display_name or "AgentBlueprint" in display_name):
            return {
                "error": "Agent Identity Blueprints cannot be modified via Microsoft Graph API",
                "hint": "Use Azure Portal: Go to Azure AI Foundry > Project > Agent Identity > Permissions",
            }
        
        try:
            token = self._get_token("https://graph.microsoft.com/.default")
            assignment_body = {
                "principalId": agent_sp["id"],
                "resourceId": resource_sp["id"],
                "appRoleId": app_role_id,
            }
            
            async with httpx.AsyncClient(http2=False, timeout=30.0) as client:
                response = await client.post(
                    f"https://graph.microsoft.com/v1.0/servicePrincipals/{agent_sp['id']}/appRoleAssignments",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=assignment_body,
                )
                
                if response.status_code in [200, 201]:
                    result = response.json()
                    return {
                        "success": True,
                        "message": f"Successfully granted {app_role_name} to {agent_sp['displayName']}",
                        "assignment": {
                            "id": result.get("id"),
                            "principalId": result.get("principalId"),
                            "resourceId": result.get("resourceId"),
                            "appRoleId": result.get("appRoleId"),
                        },
                    }
                elif response.status_code == 409:
                    return {
                        "success": True,
                        "message": f"Permission {app_role_name} is already granted",
                        "already_exists": True,
                    }
                else:
                    error_data = response.json() if response.text else {}
                    error_msg = error_data.get("error", {}).get("message", response.text[:200])
                    return {"error": f"Failed to grant permission: {response.status_code}", "details": error_msg}
        except Exception as e:
            return {"error": f"Exception granting permission: {str(e)}"}

    async def list_api_permissions(self, api_name_or_id: str) -> list:
        """List available API permissions (app roles) for a given API."""
        resource_sp = await self.find_resource_service_principal(api_name_or_id)
        if not resource_sp:
            return []
        
        permissions = []
        for role in resource_sp.get("appRoles", []):
            if role.get("isEnabled") and "Application" in (role.get("allowedMemberTypes") or []):
                permissions.append({
                    "id": role["id"],
                    "name": role["value"],
                    "displayName": role["displayName"],
                    "description": role["description"],
                })
        
        return permissions

    async def revoke_api_permission(
        self,
        agent_identity_id: str,
        assignment_id: str = None,
        api_name_or_id: str = None,
        permission_name_or_id: str = None,
    ) -> dict:
        """
        Revoke an API permission from an agent identity.
        
        Args:
            agent_identity_id: The service principal ID of the agent identity
            assignment_id: The specific app role assignment ID to revoke (if known)
            api_name_or_id: The API (if revoking by permission name)
            permission_name_or_id: The permission name (if revoking by name)
            
        Returns:
            Dictionary with result info
        """
        # Find the agent identity
        agent_sp = await self.find_agent_identity_by_id(agent_identity_id)
        if not agent_sp:
            return {"error": f"Agent identity not found: {agent_identity_id}"}
        
        # If we have a direct assignment ID, use it
        if assignment_id:
            try:
                token = self._get_token("https://graph.microsoft.com/.default")
                
                async with httpx.AsyncClient(http2=False, timeout=30.0) as client:
                    response = await client.delete(
                        f"https://graph.microsoft.com/v1.0/servicePrincipals/{agent_sp['id']}/appRoleAssignments/{assignment_id}",
                        headers={
                            "Authorization": f"Bearer {token}",
                        },
                    )
                    
                    if response.status_code == 204:
                        return {"success": True, "message": f"Revoked assignment {assignment_id}"}
                    else:
                        return {"error": f"Failed to revoke: {response.status_code}"}
                        
            except Exception as e:
                return {"error": f"Exception revoking permission: {str(e)}"}
        
        # Otherwise, find the assignment by API and permission name
        if not api_name_or_id or not permission_name_or_id:
            return {"error": "Either assignment_id or both api and permission must be provided"}
        
        # Get current assignments
        assignments = await self.get_app_role_assignments(agent_sp["id"])
        
        # Find the resource SP
        resource_sp = await self.find_resource_service_principal(api_name_or_id)
        if not resource_sp:
            return {"error": f"API not found: {api_name_or_id}"}
        
        # Find the app role ID
        permission_lower = permission_name_or_id.lower()
        app_role_id = self.COMMON_GRAPH_PERMISSIONS.get(permission_lower)
        
        if not app_role_id:
            for role in resource_sp.get("appRoles", []):
                if role["value"] and role["value"].lower() == permission_lower:
                    app_role_id = role["id"]
                    break
        
        if not app_role_id:
            return {"error": f"Permission not found: {permission_name_or_id}"}
        
        # Find matching assignment
        for assignment in assignments:
            if str(assignment.resource_id) == resource_sp["id"] and \
               str(assignment.app_role_id) == app_role_id:
                return await self.revoke_api_permission(
                    agent_identity_id=agent_identity_id,
                    assignment_id=assignment.id,
                )
        
        return {"error": f"No matching permission assignment found"}

    async def get_full_agent_identity_details(self) -> list:
        """Get all agent identities with their app role information."""
        results = []
        agent_identities = await self.list_agent_identities()
        
        # Cache for resource service principals to avoid repeated lookups
        resource_sp_cache = {}

        for idx, agent in enumerate(agent_identities):
            agent_id = agent.id
            if idx > 0:
                await asyncio.sleep(0.5)

            extended_info = await self.get_agent_identity_extended_info(agent_id)
            blueprint_id = extended_info.get("agentIdentityBlueprintId")
            app_role_assignments = await self.get_app_role_assignments(agent_id)
            
            # Group assignments by resource (API)
            assignments_by_resource = {}
            for assignment in app_role_assignments:
                resource_name = assignment.resource_display_name or "Unknown"
                resource_id = str(assignment.resource_id) if assignment.resource_id else None
                role_id = str(assignment.app_role_id) if assignment.app_role_id else None
                
                if resource_name not in assignments_by_resource:
                    assignments_by_resource[resource_name] = {
                        "resourceId": resource_id,
                        "permissions": []
                    }
                
                # Look up permission name from resource SP's app roles
                permission_name = role_id  # Default to role ID
                if resource_id and role_id:
                    if resource_id not in resource_sp_cache:
                        # Fetch the resource SP to get app roles
                        try:
                            sp = await self.client.service_principals.by_service_principal_id(resource_id).get()
                            if sp and sp.app_roles:
                                resource_sp_cache[resource_id] = {
                                    r.id: r.value for r in sp.app_roles if r.value
                                }
                            else:
                                resource_sp_cache[resource_id] = {}
                        except Exception:
                            resource_sp_cache[resource_id] = {}
                    
                    # Look up the role name
                    role_map = resource_sp_cache.get(resource_id, {})
                    permission_name = role_map.get(role_id, role_id)
                
                assignments_by_resource[resource_name]["permissions"].append(permission_name)

            agent_details = {
                "displayName": agent.display_name,
                "blueprintId": blueprint_id,
                "clientId": agent.app_id,
                "appRoleAssignments": assignments_by_resource,
            }

            results.append(agent_details)

        return results


def format_output(agent_identities: list, output_format: str = "text") -> str:
    """
    Format the agent identity details for output.

    Args:
        agent_identities: List of agent identity details
        output_format: Output format ('text', 'json')

    Returns:
        Formatted string output
    """
    if output_format == "json":
        return json.dumps(agent_identities, indent=2, default=str)

    # Text format - Agent-centric view
    lines = []
    lines.append("=" * 80)
    lines.append("AZURE AI FOUNDRY AGENTS REPORT")
    lines.append(f"Generated: {datetime.now().isoformat()}")
    lines.append("=" * 80)

    if not agent_identities:
        lines.append("\nNo agent identities found in the directory.")
        return "\n".join(lines)

    # Group by agents - collect all agents and their identities
    agents_map = {}  # agent_name -> {agent_info, identities: []}
    
    for identity in agent_identities:
        foundry_agents = identity.get('foundryAgents', [])
        
        if foundry_agents:
            for agent in foundry_agents:
                agent_name = agent.get('name') or agent.get('agentName', 'Unknown')
                agent_id = agent.get('id') or agent.get('agentId', 'N/A')
                
                if agent_name not in agents_map:
                    agents_map[agent_name] = {
                        'agent': agent,
                        'identities': []
                    }
                
                # Add identity info
                agents_map[agent_name]['identities'].append({
                    'displayName': identity['displayName'],
                    'id': identity['id'],
                    'appId': identity['appId'],
                    'blueprintId': identity.get('agentIdentityBlueprintId'),
                    'accountEnabled': identity['accountEnabled'],
                    'created': identity['createdDateTime'],
                    'appRoleAssignments': identity.get('appRoleAssignments', []),
                    'appRolesAssignedTo': identity.get('appRolesAssignedTo', []),
                })
        else:
            # Identity without matched agent - show as orphaned
            if '_orphaned_' not in agents_map:
                agents_map['_orphaned_'] = {
                    'agent': None,
                    'identities': []
                }
            agents_map['_orphaned_']['identities'].append({
                'displayName': identity['displayName'],
                'id': identity['id'],
                'appId': identity['appId'],
                'blueprintId': identity.get('agentIdentityBlueprintId'),
                'blueprintName': identity.get('blueprintDisplayName'),
                'accountEnabled': identity['accountEnabled'],
                'created': identity['createdDateTime'],
                'appRoleAssignments': identity.get('appRoleAssignments', []),
                'appRolesAssignedTo': identity.get('appRolesAssignedTo', []),
            })

    # Output agents with their identities
    agent_count = 0
    for agent_name, data in agents_map.items():
        if agent_name == '_orphaned_':
            continue
        agent_count += 1
        agent = data['agent']
        identities = data['identities']
        
        lines.append(f"\n{'=' * 80}")
        lines.append(f"AGENT: {agent_name}")
        lines.append(f"{'=' * 80}")
        
        # Agent details
        agent_id = agent.get('id') or agent.get('agentId', 'N/A')
        lines.append(f"  Agent ID:      {agent_id}")
        
        if agent.get('model'):
            lines.append(f"  Model:         {agent.get('model')}")
        if agent.get('description'):
            lines.append(f"  Description:   {agent.get('description')}")
        
        # Show instructions preview
        instructions = agent.get('instructions_preview') or agent.get('instructions')
        if instructions:
            lines.append(f"  Instructions:  {instructions[:100]}{'...' if len(str(instructions)) > 100 else ''}")
        
        # Show tools
        tools = agent.get('tools', [])
        if tools:
            tool_types = [t.get('type', 'unknown') for t in tools]
            lines.append(f"  Tools:         {', '.join(tool_types)}")
        
        # Application info from ARM API
        if agent.get('from_arm_api'):
            lines.append(f"\n  Application Info:")
            lines.append(f"    Name:        {agent.get('applicationName', 'N/A')}")
            lines.append(f"    App ID:      {agent.get('applicationId', 'N/A')}")
            lines.append(f"    Base URL:    {agent.get('baseUrl', 'N/A')}")
        
        # Entra Identities
        lines.append(f"\n  Entra Identities ({len(identities)}):")
        lines.append(f"  {'-' * 60}")
        
        for idx, identity in enumerate(identities, 1):
            lines.append(f"  [{idx}] {identity['displayName']}")
            lines.append(f"      Client ID:     {identity['appId']}")
            lines.append(f"      Principal ID:  {identity['id']}")
            if identity.get('blueprintId'):
                lines.append(f"      Blueprint ID:  {identity['blueprintId']}")
            lines.append(f"      Enabled:       {identity['accountEnabled']}")
            lines.append(f"      Created:       {identity['created']}")
            
            # App Role Assignments
            assignments = identity.get('appRoleAssignments', [])
            if assignments:
                lines.append(f"      App Roles Assigned ({len(assignments)}):")
                for a in assignments:
                    lines.append(f"        - {a['resourceDisplayName']}: {a['appRoleId']}")
            
            # App Roles Granted to Others
            granted = identity.get('appRolesAssignedTo', [])
            if granted:
                lines.append(f"      Roles Granted to Others ({len(granted)}):")
                for g in granted:
                    lines.append(f"        - {g['principalDisplayName']} ({g['principalType']})")
        
        # Blueprint/Instance identity info from ARM
        if agent.get('from_arm_api'):
            lines.append(f"\n  Identity Configuration:")
            lines.append(f"    Blueprint Client ID:  {agent.get('blueprintClientId', 'N/A')}")
            lines.append(f"    Blueprint Principal:  {agent.get('blueprintPrincipalId', 'N/A')}")
            lines.append(f"    Instance Client ID:   {agent.get('instanceClientId', 'N/A')}")
            lines.append(f"    Instance Principal:   {agent.get('instancePrincipalId', 'N/A')}")

    # Show orphaned identities (those without matched agents)
    if '_orphaned_' in agents_map:
        orphaned = agents_map['_orphaned_']['identities']
        lines.append(f"\n{'=' * 80}")
        lines.append(f"IDENTITIES WITHOUT MATCHED AGENTS ({len(orphaned)})")
        lines.append(f"{'=' * 80}")
        lines.append("(Use --arm-agents to load agent data and match these identities)")
        
        for identity in orphaned:
            lines.append(f"\n  {identity['displayName']}")
            lines.append(f"    Client ID:     {identity['appId']}")
            lines.append(f"    Principal ID:  {identity['id']}")
            if identity.get('blueprintName'):
                lines.append(f"    Blueprint:     {identity['blueprintName']}")
            lines.append(f"    Enabled:       {identity['accountEnabled']}")
            lines.append(f"    Created:       {identity['created']}")

    # Summary
    lines.append(f"\n{'=' * 80}")
    lines.append("SUMMARY")
    lines.append(f"{'=' * 80}")
    lines.append(f"  Agents Found:           {agent_count}")
    lines.append(f"  Total Identities:       {len(agent_identities)}")
    
    total_assignments = sum(len(a.get('appRoleAssignments', [])) for a in agent_identities)
    total_granted = sum(len(a.get('appRolesAssignedTo', [])) for a in agent_identities)
    lines.append(f"  App Role Assignments:   {total_assignments}")
    lines.append(f"  Roles Granted:          {total_granted}")
    
    if '_orphaned_' in agents_map:
        lines.append(f"  Unmatched Identities:   {len(agents_map['_orphaned_']['identities'])}")
    
    lines.append("=" * 80)

    return "\n".join(lines)


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="List Entra Agent Identities and their app role assignments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # List all agent identities (JSON output, default)
    python entraAgentId.py
    
    # Output as text
    python entraAgentId.py --output text

    # Retrieve agents and their Entra identities from ARM API (recommended)
    python entraAgentId.py --arm-agents /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/{account}/projects/{project}
    
    # List available permissions for Microsoft Graph
    python entraAgentId.py --list-permissions --api microsoft-graph
    
    # Grant permission to a specific agent identity
    python entraAgentId.py --grant-permission --identity <sp-id> --api microsoft-graph --permission Sites.Read.All
    
    # Grant multiple permissions
    python entraAgentId.py --grant-permission --identity <sp-id> --api microsoft-graph --permission Sites.Read.All --permission User.Read.All
    
    # Revoke a permission from an agent identity
    python entraAgentId.py --revoke-permission --identity <sp-id> --api microsoft-graph --permission Sites.Read.All
        """,
    )

    parser.add_argument(
        "--output", "-o",
        choices=["text", "json"],
        default="json",
        help="Output format (default: json)",
    )

    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Use interactive browser authentication",
    )
    
    parser.add_argument(
        "--foundry-endpoint", "-f",
        action="append",
        dest="foundry_endpoints",
        metavar="URL",
        help="Azure AI Foundry project endpoint URL(s) to query for agent details. "
             "Can be specified multiple times. Format: https://<account>.services.ai.azure.com/api/projects/<project>",
    )
    
    parser.add_argument(
        "--arm-project", "-a",
        action="append",
        dest="arm_project_ids",
        metavar="RESOURCE_ID",
        help="ARM resource ID for AI Foundry project applications endpoint. "
             "This shows agent-to-identity mappings. Can be specified multiple times. "
             "Format: /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/{account}/projects/{project}/applications",
    )
    
    parser.add_argument(
        "--arm-agents", "-A",
        action="append",
        dest="arm_agent_projects",
        metavar="RESOURCE_ID",
        help="ARM resource ID for AI Foundry project to retrieve agents and their Entra identities. "
             "This retrieves agents with full identity details. Can be specified multiple times. "
             "Format: /subscriptions/{sub}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/{account}/projects/{project}",
    )
    
    # Permission management arguments
    parser.add_argument(
        "--grant-permission",
        action="store_true",
        help="Grant an API permission to an agent identity. Requires --identity, --api, and --permission.",
    )
    
    parser.add_argument(
        "--revoke-permission",
        action="store_true",
        help="Revoke an API permission from an agent identity. Requires --identity, --api, and --permission.",
    )
    
    parser.add_argument(
        "--set-permission",
        action="store_true",
        help="Set (grant) API permission(s) to an agent identity. "
             "Can discover identities via --arm-agents OR specify directly with --client-id. "
             "Supports multiple permissions: --api API1 --permission PERM1 --api API2 --permission PERM2",
    )
    
    parser.add_argument(
        "--list-permissions",
        action="store_true",
        help="List permissions. Use with --api to list available permissions for an API, "
             "or with --arm-agents to list current permissions for agent identities.",
    )
    
    parser.add_argument(
        "--identity",
        metavar="SP_ID",
        help="The service principal ID (object ID) of the agent identity to manage permissions for.",
    )
    
    parser.add_argument(
        "--client-id",
        metavar="CLIENT_ID",
        help="The client ID (app ID) of the agent identity to set permissions for. "
             "Use with --set-permission as an alternative to --arm-agents.",
    )
    
    parser.add_argument(
        "--api",
        action="append",
        metavar="API_NAME",
        help="The API to manage permissions for. Use 'microsoft-graph' for Microsoft Graph, "
             "'sharepoint' for SharePoint Online, etc. Can be specified multiple times for --set-permission.",
    )
    
    parser.add_argument(
        "--permission",
        action="append",
        metavar="PERMISSION",
        help="The permission/role to grant (e.g., 'Sites.Read.All', 'Files.ReadWrite.All'). "
             "Can be specified multiple times. Pairs with --api in order.",
    )

    args = parser.parse_args()

    print("Entra Agent Identity Scanner")
    print("-" * 30)
    print("Authenticating with Azure...")

    try:
        manager = AgentIdentityManager(
            use_interactive=args.interactive,
            foundry_endpoints=args.foundry_endpoints
        )
        
        # Handle permission management operations
        if args.list_permissions:
            # If --arm-agents is provided, list permissions for agent identities from ARM
            if args.arm_agent_projects:
                print("\nLoading agents from ARM API...")
                for arm_project in args.arm_agent_projects:
                    await manager.load_agents_from_arm(arm_project)
                
                # Get unique applications with their identity info from project_applications_cache
                # Filter to get only the instance identity entries (these have the SP we need)
                seen_apps = set()
                apps_with_identities = []
                
                for cache_key, app_info in manager.project_applications_cache.items():
                    if cache_key.startswith("instanceclientid:"):
                        app_name = app_info.get("displayName") or app_info.get("name")
                        if app_name and app_name not in seen_apps:
                            seen_apps.add(app_name)
                            apps_with_identities.append(app_info)
                
                if not apps_with_identities:
                    print("No agent applications with identities found in the specified project.")
                    return
                
                print(f"\nFound {len(apps_with_identities)} application(s) with agent identities")
                print("=" * 60)
                
                for app_info in apps_with_identities:
                    app_name = app_info.get("displayName") or app_info.get("name") or "Unknown"
                    
                    # Get the instance identity client ID (this is the App ID we need to look up in Entra)
                    instance_client_id = app_info.get("instanceClientId")
                    
                    print(f"\nApplication: {app_name}")
                    print(f"  Instance Client ID: {instance_client_id or 'N/A'}")
                    
                    # List agents under this application
                    agents = app_info.get("agents", [])
                    if agents:
                        print(f"  Agents ({len(agents)}):")
                        for agent in agents:
                            agent_name = agent.get("name", "Unknown")
                            print(f"    +-- {agent_name}")
                    
                    # Look up the actual Entra service principal using the client/app ID
                    if instance_client_id:
                        entra_sp = await manager.find_agent_identity_by_id(instance_client_id)
                        
                        if entra_sp:
                            sp_object_id = entra_sp.get("id")
                            print(f"  Entra Service Principal:")
                            print(f"    +-- Object ID: {sp_object_id}")
                            print(f"    +-- Display Name: {entra_sp.get('displayName')}")
                            print(f"    +-- Type: {entra_sp.get('servicePrincipalType')}")
                            
                            # Query permissions using the actual Entra SP object ID
                            print(f"  Current Permissions:")
                            assignments = await manager.get_app_role_assignments(sp_object_id)
                            
                            if assignments:
                                for assignment in assignments:
                                    resource_name = getattr(assignment, 'resource_display_name', None) or "Unknown API"
                                    role_id = getattr(assignment, 'app_role_id', None)
                                    
                                    print(f"    +-- {resource_name}")
                                    print(f"        Role ID: {role_id}")
                            else:
                                print(f"    (No permissions assigned)")
                        else:
                            print(f"  Warning: Could not find Entra service principal for client ID: {instance_client_id}")
                    else:
                        print(f"  (No instance client ID - cannot query permissions)")
                
                return
            
            # Otherwise, list available permissions for an API
            apis = args.api or []
            if not apis:
                print("Error: --list-permissions requires either --api or --arm-agents to be specified.")
                print("\nExamples:")
                print("  # List available permissions for an API:")
                print("  python entraAgentId.py --list-permissions --api microsoft-graph")
                print("\n  # List current permissions for agents in a project:")
                print("  python entraAgentId.py --list-permissions --arm-agents <resource-id>")
                return
            
            # List permissions for each API
            for api in apis:
                print(f"\nFetching available permissions for '{api}'...")
                
                permissions = await manager.list_api_permissions(api)
                
                if not permissions:
                    print(f"No permissions found or API '{api}' not found.")
                    print("Available API names: microsoft-graph, graph, sharepoint, exchange, azure-management, key-vault, storage")
                    continue
                
                print(f"\nAvailable application permissions for {api} ({len(permissions)} found):")
                print("-" * 60)
                for perm in sorted(permissions, key=lambda x: x.get('name', '')):
                    print(f"  {perm.get('name', 'N/A')}")
                    if perm.get('description'):
                        # Truncate long descriptions
                        desc = perm['description'][:100] + "..." if len(perm['description']) > 100 else perm['description']
                        print(f"      {desc}")
            return
        
        if args.set_permission:
            # Validate inputs - need either --arm-agents or --client-id
            if not args.arm_agent_projects and not args.client_id:
                print("Error: --set-permission requires either --arm-agents or --client-id.")
                print("\nExamples:")
                print("  # Using ARM discovery:")
                print("  python entraAgentId.py --set-permission --arm-agents <resource-id> --api microsoft-graph --permission Sites.Read.All")
                print("\n  # Using client ID directly:")
                print("  python entraAgentId.py --set-permission --client-id <client-id> --api microsoft-graph --permission Sites.Read.All")
                print("\n  # Multiple permissions:")
                print("  python entraAgentId.py --set-permission --client-id <client-id> --api microsoft-graph --permission Sites.Read.All --api microsoft-graph --permission User.Read.All")
                return
            
            # Validate api/permission pairs
            apis = args.api or []
            permissions = args.permission or []
            
            if not apis or not permissions:
                print("Error: --set-permission requires at least one --api and --permission pair.")
                return
            
            # Build permission pairs - if counts don't match, pair them in order and reuse last api
            permission_pairs = []
            for i, perm in enumerate(permissions):
                api = apis[i] if i < len(apis) else apis[-1]
                permission_pairs.append((api, perm))
            
            print(f"\nPermissions to grant ({len(permission_pairs)}):")
            for api, perm in permission_pairs:
                print(f"  - {api}: {perm}")
            
            # Collect target identities
            target_identities = []
            
            if args.client_id:
                # Direct client ID provided
                print(f"\nLooking up client ID: {args.client_id}")
                sp = await manager.find_agent_identity_by_id(args.client_id)
                if sp:
                    target_identities.append({
                        "displayName": sp.get("displayName"),
                        "clientId": args.client_id,
                        "objectId": sp.get("id"),
                        "spType": sp.get("servicePrincipalType"),
                    })
                    print(f"  Found: {sp.get('displayName')} ({sp.get('id')})")
                else:
                    print(f"  Error: Could not find service principal for client ID: {args.client_id}")
                    return
            
            if args.arm_agent_projects:
                # Discover from ARM
                print("\nLoading agents from ARM API...")
                for arm_project in args.arm_agent_projects:
                    await manager.load_agents_from_arm(arm_project)
                
                # Get unique applications with their identity info
                seen_apps = set()
                for cache_key, app_info in manager.project_applications_cache.items():
                    if cache_key.startswith("instanceclientid:"):
                        app_name = app_info.get("displayName") or app_info.get("name")
                        instance_client_id = app_info.get("instanceClientId")
                        if app_name and app_name not in seen_apps and instance_client_id:
                            seen_apps.add(app_name)
                            # Look up the SP
                            sp = await manager.find_agent_identity_by_id(instance_client_id)
                            if sp:
                                target_identities.append({
                                    "displayName": sp.get("displayName"),
                                    "clientId": instance_client_id,
                                    "objectId": sp.get("id"),
                                    "spType": sp.get("servicePrincipalType"),
                                    "appName": app_name,
                                })
            
            if not target_identities:
                print("No agent identities found to grant permissions to.")
                return
            
            print(f"\nFound {len(target_identities)} identity/identities to update")
            print("=" * 60)
            
            total_success = 0
            total_failed = 0
            
            for identity in target_identities:
                print(f"\nIdentity: {identity['displayName']}")
                print(f"  Client ID: {identity['clientId']}")
                print(f"  Object ID: {identity['objectId']}")
                print(f"  SP Type: {identity['spType']}")
                
                for api, perm in permission_pairs:
                    result = await manager.grant_api_permission(
                        agent_identity_id=identity['objectId'],
                        api_name_or_id=api,
                        permission_name_or_id=perm
                    )
                    
                    if result.get("success"):
                        print(f"  [OK] {api}: {perm}")
                        total_success += 1
                    elif result.get("already_exists"):
                        print(f"  [OK] {api}: {perm} (already granted)")
                        total_success += 1
                    else:
                        print(f"  [FAILED] {api}: {perm}")
                        print(f"    Error: {result.get('error')}")
                        if result.get("details"):
                            print(f"    Details: {result.get('details')}")
                        total_failed += 1
            
            print("\n" + "=" * 60)
            print(f"Summary: {total_success} succeeded, {total_failed} failed")
            return
        
        if args.grant_permission:
            apis = args.api or []
            permissions = args.permission or []
            
            if not args.identity or not apis or not permissions:
                print("Error: --grant-permission requires --identity, --api, and --permission.")
                print("Example: --grant-permission --identity <sp-id> --api microsoft-graph --permission Sites.Read.All")
                return
            
            # Build permission pairs
            permission_pairs = []
            for i, perm in enumerate(permissions):
                api = apis[i] if i < len(apis) else apis[-1]
                permission_pairs.append((api, perm))
            
            print(f"\nGranting {len(permission_pairs)} permission(s) to identity '{args.identity}'...")
            
            success_count = 0
            for api, perm in permission_pairs:
                result = await manager.grant_api_permission(
                    agent_identity_id=args.identity,
                    api_name_or_id=api,
                    permission_name_or_id=perm
                )
                
                if result.get("success"):
                    print(f"  [OK] {api}: {perm}")
                    success_count += 1
                elif result.get("already_exists"):
                    print(f"  [OK] {api}: {perm} (already granted)")
                    success_count += 1
                else:
                    print(f"  [FAILED] {api}: {perm} - {result.get('error')}")
                    if result.get("details"):
                        print(f"    Details: {result.get('details')}")
                if result.get("available_permissions"):
                    print(f"\nAvailable permissions (first 20):")
                    for p in result.get("available_permissions", []):
                        print(f"    - {p}")
            return
        
        if args.revoke_permission:
            apis = args.api or []
            permissions = args.permission or []
            
            if not args.identity or not apis or not permissions:
                print("Error: --revoke-permission requires --identity, --api, and --permission.")
                print("Example: --revoke-permission --identity <sp-id> --api microsoft-graph --permission Sites.Read.All")
                return
            
            # Build permission pairs
            permission_pairs = []
            for i, perm in enumerate(permissions):
                api = apis[i] if i < len(apis) else apis[-1]
                permission_pairs.append((api, perm))
            
            print(f"\nRevoking {len(permission_pairs)} permission(s) from identity '{args.identity}'...")
            
            for api, perm in permission_pairs:
                result = await manager.revoke_api_permission(
                    agent_identity_id=args.identity,
                    api_name_or_id=api,
                    permission_name_or_id=perm
                )
                
                if result.get("success"):
                    print(f"  [OK] Revoked {api}: {perm}")
                else:
                    print(f"  [FAILED] {api}: {perm} - {result.get('error')}")
            return
        
        # Default behavior: list agent identities
        # Load agents from ARM API if project resource IDs provided
        if args.arm_agent_projects:
            for arm_project in args.arm_agent_projects:
                await manager.load_agents_from_arm(arm_project)
        
        # Load project applications if ARM resource IDs provided (legacy/alternative method)
        if args.arm_project_ids:
            for arm_id in args.arm_project_ids:
                await manager.load_project_applications(arm_id)
        
        print("Fetching agent identities and their app role assignments...")

        agent_identities = await manager.get_full_agent_identity_details()

        output = format_output(agent_identities, args.output)
        print(output)

    except Exception as e:
        print(f"\nError: {e}")
        print("\nTroubleshooting tips:")
        print("  1. Ensure you're logged in with: az login")
        print("  2. Verify you have the required permissions:")
        print("     - Application.Read.All or Directory.Read.All")
        print("     - For granting permissions: AppRoleAssignment.ReadWrite.All")
        print("  3. Try using --interactive flag for browser authentication")
        raise


if __name__ == "__main__":
    asyncio.run(main())
