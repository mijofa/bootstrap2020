#!/usr/bin/python3
"""Preload jellyfin-media-player with a Jellyfin server config."""
import argparse
import contextlib
import json
import os
import pathlib
import subprocess
import urllib.parse
import urllib.request

import dns.resolver
import plyvel


# These are the keys in local storage that we want to update
# NOTE: I don't know why there's the '_' before hand, or the '\x00\x01',
#       but removing them didn't work, so meh.
CREDENTIALS_KEY = b'_file://\x00\x01jellyfin_credentials'
AUTOLOGIN_KEY = b'_file://\x00\x01enableAutoLogin'


def get_sorted_SRV(SRV):
    """Get the top SRV record for the given query."""
    # Python was doing some really weird & stupid things when trying to get the search domain.
    # So I gave up and called out to resolvectl instead
    # FIXME: Why the fuck didn't python's socket module work?!?
    #        Often gethostname & getfqdn swapped responses,
    #        and when they did the fqdn (returned by gethostname) was cut short such that it couldn't fit the full domain
    #        NOTE: the 'hostname' command does the same thing
    domains = subprocess.check_output(['resolvectl', 'domain'], text=True)
    for line in domains.splitlines():
        _, domain = line.split(':', 1)
        domain = domain.strip()

        if domain:
            break

    # Get all SRV records and sort them by priority,
    # then grab only the first one
    # FIXME: Try them in order until 1 works?
    # FIXME: Is this sorted in the right order?
    srv_records = list(dns.resolver.resolve(f'{SRV}.{domain}', 'SRV'))
    srv_records.sort(key=lambda i: i.priority)

    return srv_records


def get_JF_info(base_url):
    """Query the info directly from the Jellyfin server (similar to how the Chromecast does)."""
    # FIXME: If the LocalAddress is missing we're supposed to use the ManualAddress,
    #        but that could result in finding the server this is running on, which would be a problem.
    with urllib.request.urlopen(urllib.parse.urljoin(base_url, "System/Info/Public")) as server_query:
        data = json.load(server_query)
        data["ManualAddress"] = base_url
        return data


def guess_JF_base_url():
    """
    Query DNS for Jellyfin server.

    FIXME: Does Jellyfin already use avahi? Can we use that instead of SRV records?
    """
    SRV_records = get_sorted_SRV('_jellyfin._tcp')

    for record in SRV_records:
        jf_port = record.port
        jf_target = str(record.target).rstrip('.')
        # If the port is a usual https port, then do https, otherwise http
        # NOTE: Jellyfin defaults to 8920 for https
        if jf_port in (443, 8920):
            jf_proto = 'https'
        else:
            jf_proto = 'http'

        # If the server is uncontactable, or causes any issues getting the info we need,
        # just move onto the next one.
        try:
            return get_JF_info(f"{jf_proto}://{jf_target}:{jf_port}")
        except:  # noqa: E722
            continue


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('base_url', default=None, type=str, nargs='?',
                    help="The base URL for the Jellyfin server (default: determined from SRV records)")
# FIXME: The autologin part doesn't actually work currently
parser.add_argument('--UserId', default=None, type=str,
                    help="The UserId for autologin (default: no autologin) (requires --AccessToken)")
parser.add_argument('--AccessToken', default=None, type=str,
                    help="The AccessToken for autologin (default: no autologin) (requires --UserId)")
args = parser.parse_args()

if args.base_url:
    server_info = get_JF_info(args.base_url)
else:
    server_info = guess_JF_base_url()

# Massage that data into what's needed for the localstorage key
# FIXME: What happens if multiple servers are set here?
# FIXME: I should probably put every SRV response in here... but then how do we deal with the cache?
jellyfin_credentials = {
    "Servers": [
        {
            "ManualAddress": server_info['ManualAddress'],
            "Id": server_info['Id'],
            # NOTE: Only ManualAddress & ID are *required*, but I've got the rest, so might as well.
            "Name": server_info['ServerName'],
            # When running behind a caching server, the LocalAddress will bypass the cache.
            # My solution is to make the caching server remove the LocalAddress,
            # which used to crash this code so I threw the if/else in without testing if I could just leave the field blank.
            "LocalAddress": server_info['LocalAddress'] if "LocalAddress" in server_info else server_info['ManualAddress'],
            # Optional args for auto login if configured
            # FIXME: Doesn't actually work yet
            "UserId": args.UserId,
            "AccessToken": args.AccessToken,
        },
    ],
}

# Since that might have secrets in it, let's copy it out and remove them before we log it.
sanitised_server_info = jellyfin_credentials['Servers'][0].copy()
sanitised_server_info['UserId'] = ('[REDACTED]' if jellyfin_credentials['Servers'][0].get('UserId')
                                   else jellyfin_credentials['Servers'][0].get('UserId'))
sanitised_server_info['AccessToken'] = ('[REDACTED]' if jellyfin_credentials['Servers'][0].get('AccessToken')
                                        else jellyfin_credentials['Servers'][0].get('AccessToken'))
print('Jellyfin Credentials', sanitised_server_info)

# Find or create the local storage database
local_storage_path = pathlib.Path("~/.local/share/Jellyfin Media Player/QtWebEngine/Default/Local Storage").expanduser()
if not local_storage_path.is_dir():
    local_storage_path.mkdir(parents=True)

# Write the data to it
with contextlib.closing(plyvel.DB(os.fspath(local_storage_path / 'leveldb'), create_if_missing=True)) as leveldb:
    # Doesn't overwrite pre-existing credentials data
    leveldb.put(CREDENTIALS_KEY,
                leveldb.get(CREDENTIALS_KEY,
                            # I don't understand leveldb or plyvel enough to comment on why the '\x01' is necessary, but it is
                            b'\x01' + json.dumps(jellyfin_credentials).encode()))
    # This one does intentionally overwrite the pre-existing autologin option.
    # I do this because it's easy to accdientally, or even habitually, hit "remember me" on login.
    # The whole point of this system is to be a shared TV player,
    # by leaving autologin disabled then I can easily "log out" by simply restarting the application.
    if args.UserId and args.AccessToken:
        # But on some systems I may want to autologin when the credentials are supplied, so allow that too
        print("Enabling autologin")
        leveldb.put(AUTOLOGIN_KEY, b'\x01true')
    else:
        print("Disabling autologin")
        leveldb.put(AUTOLOGIN_KEY, b'\x01false')
