#!/usr/bin/python3
"""
Daemon for system wide keybindings.

Intended for use with graphical kiosk-like system without a desktop manager that would normally handle this.
"""
import asyncio
import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import traceback

import evdev
import pyudev

import snapcontroller

import gi
gi.require_version('Notify', '0.7')
from gi.repository import Notify  # noqa: E402 "module level import not at top of file"

NOTIFICATION_TIMEOUT = 2000  # Same as volnotifier

# NOTE: The workflow of this code easily allows for per-device event maps, but that's not very useful
# FIXME: This is mostly pulseaudio & systemd triggers, just do them in Python instead of calling out via subprocess?
#        I'll probably be adding mpd/mpc calls, and maybe some snapcast stuff, which can all also be done in Python.
GLOBAL_EVENT_MAPPING = {
    evdev.ecodes.EV_KEY: {
        evdev.ecodes.KEY_MUTE: lambda: subprocess.check_call(['pactl', 'set-sink-mute', 'combined', 'toggle']),
        evdev.ecodes.KEY_VOLUMEUP: lambda: subprocess.check_call(['pactl', 'set-sink-volume', 'combined', '+5%']),
        evdev.ecodes.KEY_VOLUMEDOWN: lambda: subprocess.check_call(['pactl', 'set-sink-volume', 'combined', '-5%']),

        evdev.ecodes.KEY_CHANNELUP: lambda: increment_snap_channel(+1),
        evdev.ecodes.KEY_CHANNELDOWN: lambda: increment_snap_channel(-1),
        # evdev.ecodes.KEY_MEDIA: lambda: increment_snap_channel(+1),
        # evdev.ecodes.KEY_SOUND: lambda: increment_snap_channel(-1),

        evdev.ecodes.KEY_INFO: lambda: run_multiple(lambda: asyncio.ensure_future(send_to_inputSocket('KEY_INFO')),
                                                    show_time_notification),
        # We don't actually use live TV, so the EPG is useless,
        # but we have and EPG button on some remotes without an INFO button, so let's use them interchangably
        # evdev.ecodes.KEY_EPG: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_EPG')),
        evdev.ecodes.KEY_EPG: lambda: run_multiple(lambda: asyncio.ensure_future(send_to_inputSocket('KEY_INFO')),
                                                   show_time_notification),
        evdev.ecodes.KEY_TV: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_TV')),
        evdev.ecodes.KEY_RECORD: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_RECORD')),
        evdev.ecodes.KEY_ZOOM: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_ZOOM')),
        evdev.ecodes.KEY_SUBTITLE: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_SUBTITLE')),
        evdev.ecodes.KEY_FAVORITES: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_FAVORITES')),
        # PrisonPC compatibliity
        evdev.ecodes.KEY_CONNECT: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_PLAYPAUSE')),  # PrisonPC did not plan
        evdev.ecodes.KEY_CHAT: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_SUBTITLE')),  # X11 doesn't like KEY_SUBTITLE

        # PrisonPC remote
        evdev.ecodes.KEY_MENU: lambda: subprocess.check_call(
            ['systemctl', '--user', 'restart', '--no-block', 'jellyfinmediaplayer']),
        # Asus remote
        evdev.ecodes.KEY_HOMEPAGE: lambda: subprocess.check_call(
            ['systemctl', '--user', 'restart', '--no-block', 'jellyfinmediaplayer']),
        # TBS & unlabelled_black remotes
        evdev.ecodes.KEY_EXIT: lambda: subprocess.check_call(
            ['systemctl', '--user', 'restart', '--no-block', 'jellyfinmediaplayer']),

        # Every remote's Power button.
        # I can't use KEY_POWER because it get's intercepted by systemd and shut's the system down,
        # so I use KEY_CLOSE instead because PrisonPC used that, so some consistency is nice
        # FIXME: Use Python instead of calling out to a shell
        evdev.ecodes.KEY_CLOSE: lambda: subprocess.check_call(
            "if systemctl --user is-active video-output.target ; then systemctl --user stop --no-block video-output.target ; "
            "else systemctl --user start --no-block video-output.target ; fi",
            shell=True),

        # Start the Steam Link app.
        # Done as a systemd unit mostly for consistency, but there's no actual good reason for that.
        # FIXME: Should this be a toggle maybe? Pressing 'back' enough times does exit Steam Link
        evdev.ecodes.KEY_F11: lambda: subprocess.check_call(['systemctl', '--user', 'start', 'SteamLink']),

        # Steam button on the Steam Controller
        316: lambda: subprocess.check_call(['systemctl', '--user', 'start', 'SteamLink']),
    },
}


def run_multiple(*args):
    """Run every arg as it's own function."""
    for f in args:
        f()


def show_time_notification():
    """Popup a notification showing the current time."""
    notif = Notify.Notification.new("Snapcast stream")
    notif.set_property('summary', time.strftime('%I:%M %p'))
    notif.set_timeout(NOTIFICATION_TIMEOUT)

    notif.set_property('body', time.strftime('%d %b %Y'))
    notif.show()


def is_device_capable(dev_caps: dict, needed_caps: dict):
    """Compare device capabilities to the required capabilities mapping."""
    for cap_type in needed_caps:
        if cap_type not in dev_caps.keys():
            continue
        for cap in needed_caps[cap_type].keys():
            if cap in dev_caps.get(cap_type):
                return True

    return False


def get_all_capable_devices(needed_caps: dict):
    """Get all evdev input devices that have the required capabilities."""
    for dev_path in evdev.list_devices():
        dev = evdev.InputDevice(dev_path)
        if is_device_capable(dev.capabilities(), needed_caps):
            yield dev
        else:
            dev.close()


async def handle_events(dev, event_mapping):
    """Handle the given events for the given device."""
    print('Registering input device', dev.name)
    try:
        async for event in dev.async_read_loop():
            if event.type in event_mapping.keys() and \
                    event.code in event_mapping[event.type] and \
                    event.value:
                print("Processing trigger for", evdev.categorize(event))
                try:
                    event_mapping[event.type][event.code]()
                except:  # noqa: E722 "do not use bare 'except'"
                    # Report errors, but don't stop the loop for them
                    print(traceback.format_exc(), file=sys.stderr)
            # elif event.type != evdev.ecodes.EV_SYN:
            #     print("Ignoring", evdev.categorize(event))
    except OSError as e:
        if e.errno == 19:
            print("Looks like device was removed, stopping event handling")
        else:
            raise


inputSocket = None
def open_inputSocket():  # noqa: E302 "expected 2 blank lines, found 0"
    """Open a connection to Jellyfin Media Player's inputSocket."""
    # FIXME: Can I assert that it isn't already open?
    jmp_inputSocket_path = pathlib.Path(f'/tmp/pmp_inputSocket_{os.getlogin()}.sock')
    if not jmp_inputSocket_path.exists():
        # Path to inputSocket changes in a future version
        jmp_inputSocket_path = pathlib.Path(f'/tmp/jmp_inputSocket_{os.getlogin()}.sock')

    if not jmp_inputSocket_path.exists():
        return False
    else:
        global inputSocket  # FIXME
        inputSocket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        inputSocket.connect(str(jmp_inputSocket_path))
        return inputSocket


async def send_to_inputSocket(keycode, source="Keyboard", client=sys.argv[0]):
    """Send data to Jellyfin Media Player's inputSocket."""
    data = json.dumps({"client": "keybinds.py",
                       "source": "Keyboard",
                       "keycode": keycode})
    # Jellyfin Media Player doesn't like it when there isn't a newline
    data += '\n'

    if not inputSocket:
        print("Opening")
        open_inputSocket()

    try:
        await loop.sock_sendall(inputSocket, data.encode())
    except (OSError, BrokenPipeError) as e:
        if not (isinstance(e, OSError) and e.errno == 107) and \
                not (isinstance(e, BrokenPipeError) and e.errno == 32):
            # Unexpected error, re-raise it
            raise

        if open_inputSocket():
            await loop.sock_sendall(inputSocket, data.encode())
        else:
            print("Socket not connected, can't send data", keycode, file=sys.stderr)


def increment_snap_channel(increment):
    """Increment the stream associated with the current snapcast group."""
    notif = Notify.Notification.new("Snapcast stream")
    notif.set_property('summary', 'Snapclient music')
    notif.set_timeout(NOTIFICATION_TIMEOUT)

    # Not using .run or .check_call here because I wanted to ensure it runs in the background as we continue on.
    # FIXME: Should only really notify if we successfully connect and send the instruction.
    #        But that can take so long that it's annoying and defeats the purpose of the notification
    subprocess.Popen(['pactl', 'play-sample', 'device-added' if increment > 0 else 'device-removed'])

    with snapcontroller.SnapController() as snap:
        snap_group = snap.get_group_of_client(snapcontroller.get_physical_mac())
        current_stream = snap.run_command('Group.GetStatus', params={'id': snap_group})['group']['stream_id']

        snap_streams = snap.get_all_streams()
        current_index = snap_streams.index(current_stream)

        new_index = (current_index + increment) % len(snap_streams)
        new_stream_id = snap_streams[new_index]

        result = snap.run_command('Group.SetStream', params={'id': snap_group, 'stream_id': new_stream_id})

    notif.set_property('body', f"Tuned to: {result['stream_id']}")
    notif.show()


def udev_event_handler(async_loop, action: str, udev_dev: pyudev.Device):
    """Handle udev events for new devices."""
    if action == 'add' and udev_dev.device_node and udev_dev.device_node in evdev.list_devices():
        evdev_dev = evdev.InputDevice(udev_dev.device_node)
        if is_device_capable(evdev_dev.capabilities(), GLOBAL_EVENT_MAPPING):
            asyncio.ensure_future(handle_events(evdev_dev, GLOBAL_EVENT_MAPPING), loop=async_loop)


if __name__ == '__main__':
    # Set up for noticing new evdev devices
    async_loop = asyncio.get_event_loop()

    udev_context = pyudev.Context()
    udev_monitor = pyudev.Monitor.from_netlink(udev_context)
    udev_observer = pyudev.MonitorObserver(udev_monitor, lambda action, udev_dev: udev_event_handler(async_loop, action, udev_dev))
    udev_observer.start()

    # Start monitoring all current evdev devices
    for dev in get_all_capable_devices(GLOBAL_EVENT_MAPPING):
        asyncio.ensure_future(handle_events(dev, GLOBAL_EVENT_MAPPING))

    Notify.init(sys.argv[0])

    loop = asyncio.get_event_loop()
    loop.run_forever()

    # NOTE: I'm not explicitly closing the used evdev devices, but the garbage collector should take care of them.
