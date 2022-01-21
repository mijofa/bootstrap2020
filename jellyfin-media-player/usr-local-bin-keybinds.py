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
import traceback

import evdev

import snapcontroller


# NOTE: The workflow of this code easily allows for per-device event maps, but that's not very useful
# FIXME: This is mostly pulseaudio & systemd triggers, just do them in Python instead of calling out via subprocess?
#        I'll probably be adding mpd/mpc calls, and maybe some snapcast stuff, which can all also be done in Python.
GLOBAL_EVENT_MAPPING = {
    evdev.ecodes.EV_KEY: {
        evdev.ecodes.KEY_MUTE: lambda: subprocess.check_call(['pactl', 'set-sink-mute', 'combined', 'toggle']),
        evdev.ecodes.KEY_VOLUMEUP: lambda: subprocess.check_call(['pactl', 'set-sink-volume', 'combined', '+2%']),
        evdev.ecodes.KEY_VOLUMEDOWN: lambda: subprocess.check_call(['pactl', 'set-sink-volume', 'combined', '-2%']),

        evdev.ecodes.KEY_CHANNELUP: lambda: increment_snap_channel(+1),
        evdev.ecodes.KEY_CHANNELDOWN: lambda: increment_snap_channel(-1),
        evdev.ecodes.KEY_MEDIA: lambda: increment_snap_channel(+1),
        evdev.ecodes.KEY_SOUND: lambda: increment_snap_channel(-1),

        evdev.ecodes.KEY_HELP: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_HELP')),
        evdev.ecodes.KEY_EPG: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_EPG')),
        evdev.ecodes.KEY_TV: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_TV')),
        evdev.ecodes.KEY_RECORD: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_RECORD')),
        evdev.ecodes.KEY_ZOOM: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_ZOOM')),
        evdev.ecodes.KEY_SUBTITLE: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_SUBTITLE')),
        evdev.ecodes.KEY_DASHBOARD: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_DASHBOARD')),
        evdev.ecodes.KEY_FAVORITES: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_FAVORITES')),
        # PrisonPC compatibliity
        evdev.ecodes.KEY_CONNECT: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_PLAYPAUSE')),  # PrisonPC did not plan
        evdev.ecodes.KEY_CHAT: lambda: asyncio.ensure_future(send_to_inputSocket('KEY_SUBTITLE')),  # X11 doesn't like KEY_SUBTITLE


        evdev.ecodes.KEY_MENU: lambda: subprocess.check_call(
            ['systemctl', '--user', 'restart', 'jellyfinmediaplayer']),
        evdev.ecodes.KEY_HOMEPAGE: lambda: subprocess.check_call(
            ['systemctl', '--user', 'restart', 'jellyfinmediaplayer']),
        evdev.ecodes.KEY_EXIT: lambda: subprocess.check_call(
            ['systemctl', '--user', 'restart', 'jellyfinmediaplayer']),
        evdev.ecodes.KEY_CLOSE: lambda: subprocess.check_call(
            ['ir-ctl', '--keymap=/etc/rc_keymaps/TV_QSP425T.toml', '--keycode=STANDBY/ON']),
    },
}


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
    with snapcontroller.SnapController() as snap:
        snap_group = snap.get_group_of_client(snapcontroller.get_physical_mac())
        current_stream = snap.run_command('Group.GetStatus', params={'id': snap_group})['group']['stream_id']

        snap_streams = snap.get_all_streams()
        current_index = snap_streams.index(current_stream)

        new_index = (current_index + increment) % len(snap_streams)
        new_stream_id = snap_streams[new_index]

        snap.run_command('Group.SetStream', params={'id': snap_group, 'stream_id': new_stream_id})


if __name__ == '__main__':
    for dev in get_all_capable_devices(GLOBAL_EVENT_MAPPING):
        asyncio.ensure_future(handle_events(dev, GLOBAL_EVENT_MAPPING))

    loop = asyncio.get_event_loop()
    loop.run_forever()

    # NOTE: I'm not explicitly closing the used evdev devices, but the garbage collector should take care of them.
