#!/usr/bin/python3
"""
Watch for changes to pulseaudio sink inputs and silence snapcast accordingly.

NOTE: Depends on module-dbus-protocol being loaded into PulseAudio
"""
import argparse
import json
import os

import dbus
import dbus.mainloop.glib

from gi.repository import GLib

import snapcontroller


def pa_array_to_dict(array):
    """
    Convert a D-Bus named array into a Python dict.

    FIXME: This assumes all keys and values are meant to be strings.
    Dbus's arrays come out annoyingly, firstly they use the dbus version of strings/ints/etc,
    but the string values come out as a zero-terminated byte array.

    So I gotta jump through some hoops to convert things around properly so that I can use them like normal dicts
    """
    return {str(k): bytes(b for b in v).strip(b'\x00').decode() for k, v in array.items()}


class PulseCorkHandler(object):
    """Handle D-Bus signals from PulseAudio."""

    mainloop = None
    known_stream_roles = {}

    def _get_bus_address(self):
        if 'PULSE_DBUS_SERVER' in os.environ:
            address = os.environ['PULSE_DBUS_SERVER']
        else:
            bus = dbus.SessionBus()
            server_lookup = bus.get_object("org.PulseAudio1", "/org/pulseaudio/server_lookup1")
            address = server_lookup.Get("org.PulseAudio.ServerLookup1", "Address",
                                        dbus_interface="org.freedesktop.DBus.Properties")
        return address

    def __init__(self, trigger_roles: list, snapctrl, ignore_filter_roles=True, mainloop=None):
        """Set up D-Bus listeners."""
        if mainloop:
            self.mainloop = mainloop

        self.trigger_roles = trigger_roles
        self.ignore_filter_roles = ignore_filter_roles
        self.snapcontroller = snapctrl

        self.snap_device_id = snapcontroller.get_physical_mac()
        # Don't cache the group ID because it can be changed, the device ID can't

        bus_address = self._get_bus_address()
        self.pulse_bus = dbus.connection.Connection(bus_address)
        self.pulse_core = self.pulse_bus.get_object(object_path='/org/pulseaudio/core1')
        self.pulse_core.ListenForSignal('org.PulseAudio.Core1.NewPlaybackStream',
                                        dbus.Array(signature='o'),
                                        dbus_interface='org.PulseAudio.Core1')
        self.pulse_core.ListenForSignal('org.PulseAudio.Core1.PlaybackStreamRemoved',
                                        dbus.Array(signature='o'),
                                        dbus_interface='org.PulseAudio.Core1')
        self.pulse_bus.add_signal_receiver(self._NewPlaybackStream, 'NewPlaybackStream')
        self.pulse_bus.add_signal_receiver(self._PlaybackStreamRemoved, 'PlaybackStreamRemoved')

        # Gotta set the starting volume & mute states
        for stream_path in self.pulse_core.Get("org.PulseAudio.Core1", "PlaybackStreams"):
            self._NewPlaybackStream(stream_path)

    def _NewPlaybackStream(self, stream_path):
        stream = self.pulse_bus.get_object("org.PulseAudio.Core1.PlaybackStream", stream_path)

        properties = pa_array_to_dict(stream.Get("org.PulseAudio.Core1.Stream", "PropertyList"))

        stream_role = properties.get('media.role', 'no_role')

        if self.ignore_filter_roles and stream_role == 'filter':
            pass
        else:
            self.known_stream_roles[stream_path] = stream_role
            try:
                self.roles_updated()
            except json.decoder.JSONDecodeError:
                self.exit()
                raise

    def _PlaybackStreamRemoved(self, stream_path):
        if stream_path in self.known_stream_roles:
            self.known_stream_roles.pop(stream_path)
            try:
                self.roles_updated()
            except json.decoder.JSONDecodeError:
                self.exit()
                raise

    def roles_updated(self):
        """Handle the roles list and mute/unmute the Snapcast group accordingly."""
        snap_group_id = self.snapcontroller.get_group_of_client(self.snap_device_id)
        if any(role in self.known_stream_roles.values() for role in self.trigger_roles):
            self.snapcontroller.run_command('Group.SetMute', {'id': snap_group_id, 'mute': True})
            print("Muting", self.known_stream_roles)
        else:
            self.snapcontroller.run_command('Group.SetMute', {'id': snap_group_id, 'mute': False})
            print("Unmuting", self.known_stream_roles)

    def exit(self):
        """Exit the main loop."""
        if self.mainloop:
            print("Quitting mainloop")
            mainloop.quit()
        else:
            print("No mainloop to quit")


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--host', required=True, default='music', type=str)
parser.add_argument('--port', required=True, default=1705, type=int)
parser.add_argument('trigger_roles', nargs='+', type=str)
args = parser.parse_args()

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
mainloop = GLib.MainLoop()

# FIXME: Use SRV records or something instead of just hardcoding the snapserver details in here
pulse = PulseCorkHandler(trigger_roles=args.trigger_roles,
                         snapctrl=snapcontroller.SnapController(args.host, args.port),
                         mainloop=mainloop)

mainloop.run()
raise Exception("Uh, this should never happen as this should be a long running process.")
