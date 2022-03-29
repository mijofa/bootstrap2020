#!/usr/bin/python3
"""
Get Jellyfin's base URL for nginx config.

Nginx has a stupid thing where you need to statically configure the DNS server that nginx will use.
This is a pain in the arse to work with and most other people seem to cat /etc/resolv.conf on startup into an nginx snippet.
Instead I figured I'd do the SRV record and such to get the Jellyfin server's IP instead, slightly more flexible that way.
"""
import argparse
import json
import subprocess
import urllib.parse
import urllib.request

import dns.resolver


def get_top_SRV(SRV):
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

    # Get all SRV records and sort them by weight,
    # then grab only the first one
    # FIXME: Try them in order until 1 works?
    # FIXME: Is this sorted in the right order?
    srv_records = list(dns.resolver.resolve(f'{SRV}.{domain}', 'SRV'))
    srv_records.sort(key=lambda i: i.weight)

    return srv_records[0]


def guess_JF_base_url():
    """
    Query DNS for Jellyfin server.

    FIXME: Does Jellyfin already use avahi? Can we use that instead of SRV records?
    """
    srv_record = get_top_SRV('_jellyfin._tcp')

    jf_port = srv_record.port
    jf_target = str(srv_record.target).rstrip('.')
    # If the port is a usual https port, then do https, otherwise http
    # NOTE: Jellyfin defaults to 8920 for https
    if jf_port in (443, 8920):
        jf_proto = 'https'
    else:
        jf_proto = 'http'

    return f"{jf_proto}://{jf_target}:{jf_port}"


def guess_bootserver_url():
    """Query DNS for boot server."""
    srv_record = get_top_SRV('_boothttp._tcp')

    port = srv_record.port
    target_IP = dns.resolver.resolve(srv_record.target)[0].address

    if port == 443:
        proto = 'https'
    else:
        proto = 'http'

    return f'{proto}://{target_IP}:{port}'


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--nginx-snippet', default='/etc/nginx/snippets/dns_hack.conf', type=argparse.FileType('w'),
                    help="Path to the nginx snippet file to be overwritten (default: /etc/nginx/snippets/dns_hack.conf)")
parser.add_argument('--base-url', default=None, type=str,
                    help="The base URL for the Jellyfin server (default: determined from SRV records)")
parser.add_argument('--bootserver-base-url', default=None, type=argparse.FileType('w'),
                    help="Hostname for the bootserver (default: determined from SRV record '_boothttp._tcp...)")
args = parser.parse_args()

JF_base_url = args.base_url or guess_JF_base_url()

# Query the info from the Jellyfin server (similar to how the Chromecast does)
with urllib.request.urlopen(urllib.parse.urljoin(JF_base_url, "System/Info/Public")) as server_query:
    server_info = json.load(server_query)

# FIXME: What if this returns an IPv6 address, will it properly quote it with square braces as nginx needs?
print('set $jellyfin_base_url', server_info['LocalAddress'], ';', file=args.nginx_snippet)
print('set $netboot_base_url', guess_bootserver_url(), ';', file=args.nginx_snippet)
