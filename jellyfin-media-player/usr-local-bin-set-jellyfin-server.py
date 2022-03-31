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


def guess_base_url():
    """
    Query DNS for probably Jellyfin server.

    FIXME: Does Jellyfin already use avahi? Can we use that instead of SRV records?
    """
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

    # Get all SRV records and sort them by weight,
    # then grab only the first one
    # FIXME: Try them in order until 1 works?
    # FIXME: Is this sorted in the right order?
    srv_records = list(dns.resolver.resolve(f'_jellyfin._tcp.{domain}', 'SRV'))
    srv_records.sort(key=lambda i: i.weight)

    jf_port = srv_records[0].port
    jf_target = str(srv_records[0].target).rstrip('.')
    # If the port is a usual https port, then do https, otherwise http
    # NOTE: Jellyfin defaults to 8920 for https
    if jf_port in (443, 8920):
        jf_proto = 'https'
    else:
        jf_proto = 'http'

    return f"{jf_proto}://{jf_target}:{jf_port}"


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('base_url', default=None, type=str, nargs='?',
                    help="The base URL for the Jellyfin server (default: determined from SRV records)")
# FIXME: The autologin part doesn't actually work currently
parser.add_argument('--UserId', default=None, type=str,
                    help="The UserId for autologin (default: no autologin) (requires --AccessToken and --deviceId2)")
parser.add_argument('--AccessToken', default=None, type=str,
                    help="The AccessToken for autologin (default: no autologin) (requires --UserId and --deviceId2)")
args = parser.parse_args()

base_url = args.base_url or guess_base_url()

# Query the info from the Jellyfin server (similar to how the Chromecast does)
with urllib.request.urlopen(urllib.parse.urljoin(base_url, "System/Info/Public")) as server_query:
    server_info = json.load(server_query)

# Massage that data into what's needed for the localstorage key
jellyfin_credentials = {
    "Servers": [
        {
            "ManualAddress": base_url,
            "Id": server_info['Id'],
            # NOTE: Only ManualAddress & ID are *required*, but I've got the rest, so might as well.
            "Name": server_info['ServerName'],
            "LocalAddress": server_info['LocalAddress'],
            # Optional args for auto login if configured
            # FIXME: Doesn't actually work yet
            "UserId": args.UserId,
            "AccessToken": args.AccessToken,
        },
    ],
}

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
        leveldb.put(AUTOLOGIN_KEY, b'\x01true')
        # FIXME: Constants have been used for other keys, do so with this one too
        leveldb.put(f'_file://\x00\x01{args.UserId}-screensaver'.encode(), b'\x01backdropscreensaver')
    else:
        leveldb.put(AUTOLOGIN_KEY, b'\x01false')
