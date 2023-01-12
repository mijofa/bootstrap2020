#!/usr/bin/python3
"""Control specific Tasmota devices depending on the kernel command line parameters."""
import argparse
import dns.resolver
import functools
import json
import pathlib
import shlex
import subprocess
import time
import urllib.parse
import urllib.request

import systemd.daemon
import paho.mqtt.client

kernel_cmdline = shlex.split(pathlib.Path('/proc/cmdline').read_text())
# The cmdline will look something like this::
#     initrd=http://bootserver/netboot/jellyfin-media-player-latest/initrd.img  panic=10 boot=live
#     fetch=http://bootserver/netboot/jellyfin-media-player-latest/filesystem.squashfs  splash  --
#     tasmota.video=mijofa-lounge-tv tasmota.audio=mijofa-lounge-speakers

http_devices = {}
mqtt_devices = {}

# FIXME: Could/should I use argparse for this?
# NOTE: I'm intentionally not ignoring everything after the '--' as argparse would do,
#       because I want to use that to tell the kernel (and possibly systemd?) to ignore them.
# FIXME: Should I perhaps *only* process arguments after the '--'?
for arg in kernel_cmdline:
    if arg.startswith('tasmota.'):
        device_type, device_name = arg[len('tasmota.'):].split('=', 1)
        if device_name.startswith('MQTT_TOPIC_'):
            mqtt_devices[device_type] = device_name[len('MQTT_TOPIC_'):]
        else:
            http_devices[device_type] = device_name
    else:
        continue


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('device_type', choices=[*http_devices.keys(), *mqtt_devices.keys()], type=str,
                    help="Control the given type of device, kernel cmdline is used to determine the device's hostname")

# NOTE: Video & Audio each need power on, off, and toggle actions,
#       Alternatively, trigger events on everything and leave any & all config up to the Tasmota device itself?
parser.add_argument('--event', type=str,
                    help="Trigget the given event")
parser.add_argument('--power', choices=['On', 'Off', 'Toggle'], type=str,
                    help="Run the 'power' command with the given argument")
parser.add_argument('--power-on-wait', action='store_const', const=True,
                    help="Run the 'power on' command, and wait for it to power back off")
# FIXME: Include colour command

args = parser.parse_args()
if args.event == args.power == args.power_on_wait == None:  # noqa: E711
    # NOTE: This exits the same as parse_args() would if there was a parsing error
    parser.error("At least one action argument is required.")

# FIXME: Make an object class and simplify this properly
if args.device_type in http_devices:
    device_url = f'http://{http_devices[args.device_type]}/cm'

    def send_command(cmnd, *args):
        """Send command to Tasmota HTTP API."""
        # FIXME: Should I json decode the response to give an error response if there's an error in the json?
        post_data = urllib.parse.urlencode({'cmnd': f"{cmnd} {' '.join(args)}"}).encode()
        with urllib.request.urlopen(device_url, data=post_data) as response:
            return response.read().decode()

    def wait_status(key, goal):
        """Wait for Tasmota device to report status key as matching goal."""
        query_data = urllib.parse.urlencode({'cmnd': "Status"}).encode()
        while response := urllib.request.urlopen(device_url, data=query_data):
            status = json.loads(response.read().decode()).get('Status')
            print(status)
            if status.get(key.title()) in goal:
                # Found goal, return here
                return status.get(key.title())
            else:
                # NOTE: The Tasmota web console view queries every 3s, so presumably that dev team has decided 3s is slow enough
                time.sleep(5)

elif args.device_type in mqtt_devices:
    device_topic = mqtt_devices[args.device_type]

    # For some whacked-out reason, mqtt_client.connect_srv won't work even when socket.getfqdn() resolves properly.
    # But socket.getfqdn() doesn't resolve properly anyway, so just work around it badly using resolvectl
    domain = [domain.strip() for _, domain in (
              line.split(':', 1) for line in
              subprocess.check_output(['resolvectl', 'domain'], text=True).splitlines())
              if domain][0]
    sorted(dns.resolver.resolve(f'_mqtt._tcp.{domain}', 'SRV'), key=lambda i: i.weight)[0]
    srv_record = sorted(dns.resolver.resolve(f'_mqtt._tcp.{domain}', 'SRV'), key=lambda i: i.weight)[0]

    mqtt_client = paho.mqtt.client.Client()
    # I couldn't make "anonymous" connections work with my broker (Home Assistant addon)
    # but I could create a guest:guest account, so that'll do
    # FIXME: Try anonymous, and fallback on guest:guest when that fails
    mqtt_client.username_pw_set(username='guest', password='guest')
    conn_resp = mqtt_client.connect(srv_record.target.to_text()[:-1], srv_record.port)

    def send_command(cmnd, *args):
        """Send MQTT command to cmnd/... topic, and wait for response on stat/... topic."""
        cmnd = cmnd.upper()

        mqtt_client.reconnect()  # Because we disconnect when we receive a response
        mqtt_client.subscribe(f'stat/{device_topic}/{cmnd}')

        def _f(client, userdata, message):
            global mqtt_response
            mqtt_response = message.payload.decode()  # FIXME: Assumes str
            client.disconnect()
            return mqtt_response

        mqtt_client.message_callback_add(sub=f'stat/{device_topic}/{cmnd}', callback=_f)
        mqtt_client.publish(topic=f'cmnd/{device_topic}/{cmnd}', payload=' '.join(args))
        mqtt_client.loop_forever()
        return mqtt_response

    def wait_status(key, goal):
        """Wait until the device's specified status is at the specific goal."""
        mqtt_client.reconnect()  # Because we disconnect when we receive a response

        def _f(goal, client, userdata, message):
            global mqtt_response
            mqtt_response = message.payload.decode()  # FIXME: Assumes str
            if mqtt_response in goal:
                client.disconnect()
                return mqtt_response

        mqtt_client.subscribe(f'stat/{device_topic}/{key}')
        mqtt_client.message_callback_add(sub=f'stat/{device_topic}/{key}', callback=functools.partial(_f, goal))
        mqtt_client.loop_forever()
        return mqtt_response
else:
    raise Exception("Argparse shouldn't even let us get here")

if args.power_on_wait and (args.event or args.power):
    # FIXME: Do this properly
    parser.error("power-on-wait is a mutually exclusive argument.")
elif args.power_on_wait:
    # Do power on http request
    # FIXME: Should I json decode the response to give an error response if there's an error in the json?
    print(send_command('POWER', 'ON'))

    systemd.daemon.notify('READY=1')

    # Wait for the power to be off
    print(wait_status('POWER', [False, 0, 'OFF']))
    systemd.daemon.notify('STOPPING=1')
else:
    # A list of all actions and their argument as a string
    action_args = [(k, str(v)) for k, v in vars(args).items() if k != 'device_type' and v is not None]

    if len(action_args) == 1:
        # If there's only one action, just run that
        print(send_command(*action_args[0]))
    else:
        # If there's more than one action, we need to turn that into a backlog command
        args = []
        for a in action_args:
            args.extend(a)
            args.append(';')

        # Remove the final ' ; '
        args.pop(-1)

        print(send_command('BACKLOG', *action_args))
