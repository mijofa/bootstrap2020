#!/usr/bin/python3
"""Control specific Tasmota devices depending on the kernel command line parameters."""
import shlex
import pathlib
import argparse
import urllib.request
import urllib.parse

kernel_cmdline = shlex.split(pathlib.Path('/proc/cmdline').read_text())
# The cmdline will look something like this::
#     initrd=http://bootserver/netboot/jellyfin-media-player-latest/initrd.img  panic=10 boot=live
#     fetch=http://bootserver/netboot/jellyfin-media-player-latest/filesystem.squashfs  splash  --
#     tasmota.video=mijofa-lounge-tv tasmota.audio=mijofa-lounge-speakers

tasmota_devices = {}

# FIXME: Could/should I use argparse for this?
# NOTE: I'm intentionally not ignoring everything after the '--' as argparse would do,
#       because I want to use that to tell the kernel (and possibly systemd?) to ignore them.
# FIXME: Should I perhaps *only* process arguments after the '--'?
for arg in kernel_cmdline:
    if not arg.startswith('tasmota.'):
        continue
    else:
        device_type, device_name = arg[len('tasmota.'):].split('=', 1)
        tasmota_devices[device_type] = device_name


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('device_type', choices=tasmota_devices.keys(), type=str,
                    help="Control the given type of device, kernel cmdline is used to determine the device's hostname")

# NOTE: Video & Audio each need power on, off, and toggle actions,
#       Alternatively, trigger events on everything and leave any & all config up to the Tasmota device itself?
parser.add_argument('--event', type=str,
                    help="Trigget the given event")
parser.add_argument('--power', choices=['On', 'Off', 'Toggle'], type=str,
                    help="Run the 'power' command with the given argument")
# FIXME: Include colour command

args = parser.parse_args()
if args.event == args.power == None:  # noqa: E711
    # NOTE: This exits the same as parse_args() would if there was a parsing error
    parser.error("At least one action argument is required.")


device_url = f'http://{tasmota_devices[args.device_type]}/cm'
# A list of all actions and their argument as a string
action_args = [(k, str(v)) for k, v in vars(args).items() if k != 'device_type' and v is not None]

if len(action_args) == 1:
    # If there's only one action, just run that
    cmnd = ' '.join(action_args[0])
else:
    # If there's more than one action, we need to turn that into a backlog command
    cmnd = 'BACKLOG '
    cmnd += ' ; '.join([' '.join(c) for c in action_args])

# Do the actual http request
# FIXME: Should I json decode the response to give an error response if there's an error in the json?
post_data = urllib.parse.urlencode({'cmnd': cmnd}).encode()
with urllib.request.urlopen(device_url, data=post_data) as response:
    print(response.read().decode())
