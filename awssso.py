#!/bin/python3

import boto3
import click
import configparser
import glob
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# No idea why SSO can't leverage standard --profile from config.
# Locate the cached access token for a specific SSO start URL.
#
# The AWS CLI stores one JSON file per SSO session under ~/.aws/sso/cache/.
# The filename is a SHA1 of the sso-session name (or start URL) but AWS does
# not guarantee that scheme, and `aws sso login` and botocore have been known
# to disagree on it, so we match on the startUrl field *inside* each file
# instead of recomputing the hash. This also lets multiple orgs coexist.
def get_sso_token(start_url):
    sso_cache_dir = Path.home() / ".aws" / "sso" / "cache"
    files_path = os.path.join(sso_cache_dir, "*.json")

    wanted = _normalise_url(start_url)
    for cache_file in glob.iglob(files_path):
        try:
            cache = json.loads(Path(cache_file).read_text())
        except (ValueError, OSError):
            continue

        if _normalise_url(cache.get("startUrl", "")) != wanted:
            continue

        token = cache.get("accessToken")
        if not token:
            continue

        if _token_expired(cache.get("expiresAt")):
            # Keep looking — an older, valid token may exist for the same URL.
            continue

        return token

    return None


def _normalise_url(url):
    # Trailing slashes/hashes vary between how users type the URL and how the
    # CLI records it; normalise so matching is reliable.
    return url.rstrip("/#").lower()


def _token_expired(expires_at):
    if not expires_at:
        return False
    try:
        # expiresAt is ISO-8601, typically "2026-07-20T08:30:00Z".
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    return expiry <= datetime.now(timezone.utc)


def cleanse_account_name(account_name, account_mappings, prefix=""):
    # Example: Replace spaces with underscores and strip leading/trailing whitespace
    account_name = account_name.replace(" ", "").strip()
    account_name = account_name.lower()

    if account_name in account_mappings:
        # If the account name exists in the mappings, replace it with the friendly name
        account_name = account_mappings[account_name]

    # Prefix is per-session (see load_sessions) so overlapping account names across
    # orgs can be disambiguated. Empty by default.
    if prefix:
        account_name = prefix + account_name
    return account_name


def cleanse_role_name(role_name, role_mappings):
    # Example: Replace spaces with underscores and strip leading/trailing whitespace
    role_name = role_name.replace(" ", "").strip()
    role_name = role_name.lower()

    if role_name in role_mappings:
        # If the role name exists in the mappings, replace it with the friendly name
        role_name = role_mappings[role_name]
    return role_name


# Collect every SSO session declared in the config file.
#
# Preferred format: one [sso-session NAME] section per org. Shared region/output
# can live in a [defaults] section. For backwards compatibility, a lone [profile]
# section (the original single-org format) is promoted to a single session.
def load_sessions(config):
    defaults = config._sections.get("defaults", {})
    sessions = []

    for section in config.sections():
        if not section.startswith("sso-session "):
            continue
        name = section[len("sso-session "):].strip()
        values = config._sections.get(section, {})
        start_url = values.get("sso_start_url")
        sso_region = values.get("sso_region")
        if not start_url or not sso_region:
            print(
                f"Warning: [sso-session {name}] missing sso_start_url or "
                f"sso_region — skipping",
                file=sys.stderr,
            )
            continue
        sessions.append(
            {
                "name": name,
                "sso_start_url": start_url,
                "sso_region": sso_region,
                "region": values.get("region", defaults.get("region", sso_region)),
                "output": values.get("output", defaults.get("output", "json")),
                # Per-session, no prefix unless explicitly set. Lets overlapping
                # account names across orgs be disambiguated when needed.
                "profileprefix": values.get("profileprefix", ""),
            }
        )

    if sessions:
        return sessions

    # Backwards-compatible single-org [profile] format.
    if config.has_section("profile"):
        profile = config._sections.get("profile", {})
        account_mappings = config._sections.get("account-mappings", {})
        prefix = account_mappings.get("profileprefix", "")
        name = prefix.rstrip("-_ ") or "default"
        start_url = profile.get("sso_start_url")
        sso_region = profile.get("sso_region")
        if start_url and sso_region:
            sessions.append(
                {
                    "name": name,
                    "sso_start_url": start_url,
                    "sso_region": sso_region,
                    "region": profile.get("region", sso_region),
                    "output": profile.get("output", "json"),
                    # Legacy format: prefix stays global (from [account-mappings]).
                    "profileprefix": prefix,
                }
            )

    return sessions


# Print the [sso-session] block once per org. Referencing profiles via
# sso_session enables the AWS CLI's automatic token refresh.
def build_session_block(session):
    block = f"[sso-session {session['name']}]\n"
    block += f"sso_start_url = {session['sso_start_url']}\n"
    block += f"sso_region = {session['sso_region']}\n"
    block += "sso_registration_scopes = sso:account:access\n"
    print(block)


# combine components to [profile] block for .aws/config file
def build_profile_block(conf, session, account, role):
    account_mappings = conf._sections.get("account-mappings", {})
    role_mappings = conf._sections.get("role-mappings", {})
    friendly_name = cleanse_account_name(
        account.get("accountName"), account_mappings, session.get("profileprefix", "")
    )
    friendly_role = cleanse_role_name(role.get("roleName"), role_mappings)

    block = f"[profile {friendly_name}-{friendly_role}]\n"
    block += f"sso_session = {session['name']}\n"
    block += f"sso_account_id = {account.get('accountId')}\n"
    block += f"sso_role_name = {role.get('roleName')}\n"
    block += f"region = {session['region']}\n"
    block += f"output = {session['output']}\n"
    print(block)


def emit_profiles_for_session(config, session):
    token = get_sso_token(session["sso_start_url"])
    if token is None:
        print(
            f"Warning: no valid cached token for session '{session['name']}'. "
            f"Run: aws sso login --sso-session {session['name']}",
            file=sys.stderr,
        )
        return

    client = boto3.client("sso", region_name=session["sso_region"])

    accounts = []
    try:
        account_paginator = client.get_paginator("list_accounts")
        for page in account_paginator.paginate(accessToken=token):
            accounts.extend(page.get("accountList", []))
    except Exception as e:
        print(
            f"Error retrieving accounts for session '{session['name']}': {e}",
            file=sys.stderr,
        )
        return

    build_session_block(session)

    for account in accounts:
        account_id = account.get("accountId")
        roles = []
        try:
            role_paginator = client.get_paginator("list_account_roles")
            for page in role_paginator.paginate(
                accountId=account_id, accessToken=token
            ):
                roles.extend(page.get("roleList", []))

            for role in roles:
                build_profile_block(config, session, account, role)
        except Exception as e:
            print(
                f"Error retrieving roles for account {account_id}: {e}",
                file=sys.stderr,
            )


@click.command()
@click.option("--configfile", default="awssso.cfg", help="Path to configuration file")
def main(configfile):

    # This tool authenticates to SSO purely via the cached accessToken, so it
    # needs no AWS profile. Strip any ambient profile env vars, otherwise boto3's
    # default-session setup tries to resolve them and crashes with ProfileNotFound
    # when the caller's active profile isn't in the config file being read.
    os.environ.pop("AWS_PROFILE", None)
    os.environ.pop("AWS_DEFAULT_PROFILE", None)

    config = configparser.ConfigParser()
    config.read(configfile)

    sessions = load_sessions(config)
    if not sessions:
        print(
            "Error: no SSO sessions found. Define one or more [sso-session NAME] "
            "sections (or a legacy [profile] section). See the README.",
            file=sys.stderr,
        )
        sys.exit(1)

    for session in sessions:
        emit_profiles_for_session(config, session)


if __name__ == "__main__":
    main()
