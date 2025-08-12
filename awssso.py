#!/bin/python3

import boto3
import configparser
import glob
import json
import os
import sys
from pathlib import Path


# No idea why SSO can't leverage standard --profile from config
# Scrape access token from local SSO cache
def get_sso_token():
    sso_cache_dir = Path.home() / ".aws" / "sso" / "cache"
    files_path = os.path.join(sso_cache_dir, "*")
    files = sorted(glob.iglob(files_path), key=os.path.getctime, reverse=True)
    latest_cache_file = files[0]

    cache = json.loads(Path(latest_cache_file).read_text())
    return cache.get("accessToken", None)


def cleanse_account_name(account_name, account_mappings):
    # Example: Replace spaces with underscores and strip leading/trailing whitespace
    account_name = account_name.replace(" ", "").strip()
    account_name = account_name.lower()

    if account_name in account_mappings:
        # If the account name exists in the mappings, replace it with the friendly name
        account_name = account_mappings[account_name]
    return account_name


def cleanse_role_name(role_name, role_mappings):
    # Example: Replace spaces with underscores and strip leading/trailing whitespace
    role_name = role_name.replace(" ", "").strip()
    role_name = role_name.lower()

    if role_name in role_mappings:
        # If the role name exists in the mappings, replace it with the friendly name
        role_name = role_mappings[role_name]
    return role_name


# combine components to [profile] block for .aws/config file
def build_profile_block(conf, account, role):
    # TODO: Strip whitespace from within account and role name
    # Option: extra function combing strip and name translation?

    conf_profile = conf["profile"]

    account_mappings = conf._sections.get("account-mappings", {})
    role_mappings = conf._sections.get("role-mappings", {})
    friendly_name = cleanse_account_name(account.get("accountName"), account_mappings)
    friendly_role = cleanse_role_name(role.get("roleName"), role_mappings)

    block = f"[profile {friendly_name}-{friendly_role}]\n"
    block += f"sso_start_url = {conf_profile['sso_start_url']}\n"
    block += f"sso_region = {conf_profile['sso_region']}\n"
    block += f"region = {conf_profile['region']}\n"
    block += f"output = {conf_profile['output']}\n"
    block += f"sso_account_id = {account.get('accountId')}\n"
    block += f"sso_role_name = {role.get('roleName')}\n"
    print(block)
    pass


def main():
    config = configparser.ConfigParser()
    config.read("awssso.cfg")

    # extract token from sso cache
    token = get_sso_token()
    client = boto3.client("sso", region_name="eu-west-1")

    accounts = []
    # Retrieve account assignments
    try:
        account_paginator = client.get_paginator("list_accounts")
        response_iterator = account_paginator.paginate(accessToken=token)
        for page in response_iterator:
            accounts.extend(page.get("accountList"))
    except Exception as e:
        print(f"Error retrieving assigned Accounts: {e}")
        sys.exit(1)

    for account in accounts:
        roles = []
        account_id = account.get("accountId")
        try:
            # List roles for the account
            role_paginator = client.get_paginator("list_account_roles")
            response_iterator = role_paginator.paginate(
                accountId=account_id, accessToken=token
            )
            for page in response_iterator:
                roles.extend(page.get("roleList", []))

            for role in roles:
                build_profile_block(config, account, role)
        except Exception as e:
            print(f"Error retrieving roles for Account {account_id}: {e}")


if __name__ == "__main__":
    main()
