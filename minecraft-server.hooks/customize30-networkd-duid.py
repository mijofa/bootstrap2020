#!/usr/bin/python3
"""
Configure systemd-networkd DHCP DUID.

The default DUIDType is 'vendor', but my hardware doesn't have a vendor supplied UUID.
In a non-live system this would be fine because it would fallback on /etc/machine-id,
but this is a live system where even that gets reset every reboot.
FIXME: Make machine-id consistent then we don't need to worry about any of this?

ref: https://www.freedesktop.org/software/systemd/man/networkd.conf.html
"""

import argparse
import pathlib

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('chroot_path', type=pathlib.Path)
args = parser.parse_args()

with open(args.chroot_path / pathlib.Path('etc/systemd/networkd.conf'), 'a') as f:
    f.write('\n')  # Just in case the last line doesn't have a '\n'
    f.write('\n'.join([
        # '[DHCP]'  # FIXME: If it needs adding to this section anyway, are the others really necessary?
        'DUIDType=link-layer',
        '[DHCPv4]',
        'DUIDType=link-layer',
        '[DHCPv6]',
        'DUIDType=link-layer',
    ]))
    f.write('\n')
