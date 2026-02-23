"""
Cleanup script for the Foxen support agent.

Removes app-specific Azure resources (leaves shared infrastructure intact):
1. Delete Agent (all versions)
2. Delete RemoteTool Connection
3. Delete Knowledge Base
4. Delete Knowledge Source (and auto-created AI Search resources)
5. Delete Blob Container

Usage:
    poetry run python scripts/teardown_agent.py
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import requests
from azure.core.credentials import TokenCredential
from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]

AI_SEARCH_KB_API = "2025-11-01-Preview"
AI_SEARCH_API_VERSION = "2024-07-01"
FOUNDRY_API_VERSION = "2025-11-15-preview"
MGMT_API_VERSION = "2025-10-01-preview"
AUTH_SCOPE = "https://ai.azure.com/.default"

APP_NAME = "thoughtful-ai"
AGENT_NAME = "thoughtful-support"


def main():
    load_dotenv(PROJECT_ROOT / ".env")

    storage_account = os.getenv("DOC_CHAT_STORAGE_ACCOUNT", "docchatstorageeastus")
    ai_search_service = os.getenv("AI_SEARCH_SERVICE_NAME", "doc-chat-ai-search")
    ai_search_admin_key = os.getenv("AI_SEARCH_ADMIN_KEY")
    project_endpoint = os.getenv("AZURE_AI_PROJECT_ENDPOINT")
    managed_identity_client_id = os.getenv("MANAGED_IDENTITY_CLIENT_ID")
    azure_subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID", "")
    foundry_resource_group = os.getenv("FOUNDRY_RESOURCE_GROUP", "rg-doc-chat")

    ai_search_url = f"https://{ai_search_service}.search.windows.net"
    search_hdrs = {"Content-Type": "application/json", "api-key": ai_search_admin_key}

    # Parse hub/project
    hub_match = re.search(r"https://([^.]+)\.services\.ai\.azure\.com", project_endpoint or "")
    hub_name = hub_match.group(1) if hub_match else "doc-chat"
    project_match = re.search(r"/projects/([^/]+)$", project_endpoint or "")
    project_name = project_match.group(1) if project_match else "doc-chat"
    project_resource_id = (
        f"/subscriptions/{azure_subscription_id}"
        f"/resourceGroups/{foundry_resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{hub_name}"
        f"/projects/{project_name}"
    )

    # Resource names
    container_name = APP_NAME
    ks_name = f"{APP_NAME}-ks"
    kb_name = f"{APP_NAME}-kb"
    connection_name = f"{APP_NAME}-connection"

    print("=" * 60)
    print("Foxen Agent - Teardown")
    print("=" * 60)
    print()

    # Get credentials
    credential: TokenCredential
    if managed_identity_client_id:
        try:
            credential = ManagedIdentityCredential(client_id=managed_identity_client_id)
            credential.get_token("https://storage.azure.com/.default")
        except Exception:
            credential = DefaultAzureCredential()
    else:
        credential = DefaultAzureCredential()

    # Step 1: Delete Agent
    print("[Step 1/5] Deleting agent...")
    try:
        token = credential.get_token(AUTH_SCOPE).token
        url = f"{project_endpoint}/agents/{AGENT_NAME}?api-version={FOUNDRY_API_VERSION}"
        resp = requests.delete(url, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code in [200, 204]:
            print(f"  Deleted agent '{AGENT_NAME}'")
        elif resp.status_code == 404:
            print(f"  Agent '{AGENT_NAME}' not found (already deleted)")
        else:
            print(f"  Warning: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        print(f"  Error: {e}")
    print()

    # Step 2: Delete RemoteTool Connection
    print("[Step 2/5] Deleting RemoteTool connection...")
    try:
        mgmt_token = credential.get_token("https://management.azure.com/.default").token
        url = (
            f"https://management.azure.com{project_resource_id}"
            f"/connections/{connection_name}?api-version={MGMT_API_VERSION}"
        )
        resp = requests.delete(url, headers={"Authorization": f"Bearer {mgmt_token}"})
        if resp.status_code in [200, 204]:
            print(f"  Deleted connection '{connection_name}'")
        elif resp.status_code == 404:
            print(f"  Connection '{connection_name}' not found")
        else:
            print(f"  Warning: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        print(f"  Error: {e}")
    print()

    # Step 3: Delete Knowledge Base
    print("[Step 3/5] Deleting knowledge base...")
    try:
        resp = requests.delete(
            f"{ai_search_url}/knowledgebases/{kb_name}?api-version={AI_SEARCH_KB_API}",
            headers=search_hdrs,
        )
        if resp.status_code in [200, 204]:
            print(f"  Deleted knowledge base '{kb_name}'")
        elif resp.status_code == 404:
            print(f"  Knowledge base '{kb_name}' not found")
        else:
            print(f"  Warning: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        print(f"  Error: {e}")
    print()

    # Step 4: Delete Knowledge Source (and auto-created resources)
    print("[Step 4/5] Deleting knowledge source...")
    try:
        resp = requests.delete(
            f"{ai_search_url}/knowledgesources/{ks_name}?api-version={AI_SEARCH_KB_API}",
            headers=search_hdrs,
        )
        if resp.status_code in [200, 204]:
            print(f"  Deleted knowledge source '{ks_name}' (and auto-created resources)")
        elif resp.status_code == 404:
            print(f"  Knowledge source '{ks_name}' not found")
        else:
            print(f"  Warning: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        print(f"  Error: {e}")

    # Also clean up any remaining AI Search resources with our app name
    for resource_type in ["indexes", "indexers", "datasources", "skillsets"]:
        try:
            resp = requests.get(
                f"{ai_search_url}/{resource_type}?api-version={AI_SEARCH_API_VERSION}",
                headers=search_hdrs,
            )
            if resp.status_code == 200:
                for item in resp.json().get("value", []):
                    name = item.get("name", "")
                    if APP_NAME in name:
                        del_resp = requests.delete(
                            f"{ai_search_url}/{resource_type}/{name}?api-version={AI_SEARCH_API_VERSION}",
                            headers=search_hdrs,
                        )
                        if del_resp.status_code in [200, 204]:
                            print(f"  Cleaned up {resource_type[:-1]}: {name}")
        except Exception:
            pass
    print()

    # Step 5: Delete Blob Container
    print("[Step 5/5] Deleting blob container...")
    try:
        blob_service = BlobServiceClient(
            account_url=f"https://{storage_account}.blob.core.windows.net",
            credential=credential,
        )
        container_client = blob_service.get_container_client(container_name)
        if container_client.exists():
            container_client.delete_container()
            print(f"  Deleted container '{container_name}'")
        else:
            print(f"  Container '{container_name}' not found")
    except Exception as e:
        print(f"  Error: {e}")

    print()
    print("=" * 60)
    print("TEARDOWN COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
