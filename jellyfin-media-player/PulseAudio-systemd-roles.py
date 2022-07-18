#!/usr/bin/python3
"""
Watch for changes to pulseaudio sink inputs and start/stop systemd targets accordingly.

FIXME: If this is running on 2 systems in the same group then things could get confusing.
       If both systems mute the room when playing a videos at the same time, then only 1 of them stops playing a video,
       the room will be unmuted even though the other system is still playing a video.
       This should somehow keep the room muted, but don't keep it unmuted.
NOTE: Depends on module-dbus-protocol being loaded into PulseAudio
"""
import argparse
import json
import os

import dbus
# Needed for the dbus mainloop
import dbus.mainloop.glib

from gi.repository import GLib

# This module is only useful for running as a systemd unit to update the state of this unit,
# it doesn't help query systemd's unit status so we use dbus for that
import systemd.daemon
# I couldn't figure out how to make this module interact with the --user manager
# import pystemd


def pa_array_to_dict(array):
    """
    Convert a D-Bus named array into a Python dict.

    FIXME: This assumes all keys and values are meant to be strings.
    Dbus's arrays come out annoyingly, firstly they use the dbus version of strings/ints/etc,
    but the string values come out as a zero-terminated byte array.

    So I gotta jump through some hoops to convert things around properly so that I can use them like normal dicts
    """
    return {str(k): bytes(b for b in v).strip(b'\x00').decode() for k, v in array.items()}


def get_PA_bus(session_bus):
    """
    Get the address for PA's dbus session.

    PulseAudio does not use the system or user dbus session.
    It runs it's own internal dbus session and communicates the address for that session via the user's dbus session.
    """
    if 'PULSE_DBUS_SERVER' in os.environ:
        address = os.environ['PULSE_DBUS_SERVER']
    else:
        bus = dbus.SessionBus()
        server_lookup = bus.get_object("org.PulseAudio1", "/org/pulseaudio/server_lookup1")
        address = server_lookup.Get("org.PulseAudio.ServerLookup1", "Address",
                                    dbus_interface="org.freedesktop.DBus.Properties")
    return dbus.connection.Connection(address)


class PulseCorkHandler(object):
    """Handle D-Bus signals from PulseAudio."""

    old_state = None
    mainloop = None
    known_stream_roles = {}

    def __init__(self, trigger_roles: list,
                 playback_target_name: str, muted_target_name: str,
                 ignore_filter_roles=True, mainloop=None):
        """Set up D-Bus listeners."""
        if mainloop:
            self.mainloop = mainloop

        self.trigger_roles = trigger_roles
        self.ignore_filter_roles = ignore_filter_roles
        self.playback_target_name = playback_target_name
        self.muted_target_name = muted_target_name

        session_bus = dbus.SessionBus()

        self.pulse_bus = get_PA_bus(session_bus)
        self.pulse_core = self.pulse_bus.get_object(object_path='/org/pulseaudio/core1')

        # for media-playback.target
        self.pulse_core.ListenForSignal('org.PulseAudio.Core1.NewPlaybackStream',
                                        dbus.Array(signature='o'),
                                        dbus_interface='org.PulseAudio.Core1')
        self.pulse_core.ListenForSignal('org.PulseAudio.Core1.PlaybackStreamRemoved',
                                        dbus.Array(signature='o'),
                                        dbus_interface='org.PulseAudio.Core1')
        self.pulse_bus.add_signal_receiver(self._NewPlaybackStream, 'NewPlaybackStream')
        self.pulse_bus.add_signal_receiver(self._PlaybackStreamRemoved, 'PlaybackStreamRemoved')

        # for volume-mute.target
        self.pulse_core.ListenForSignal('org.PulseAudio.Core1.Device.MuteUpdated',
                                        dbus.Array(signature='o'),
                                        dbus_interface='org.PulseAudio.Core1')
        self.pulse_bus.add_signal_receiver(self._MuteUpdated, 'MuteUpdated', path_keyword='device_path')

        # for volume-percent@[...].target

        systemd1 = session_bus.get_object('org.freedesktop.systemd1', '/org/freedesktop/systemd1')
        self.systemd1_manager = dbus.Interface(systemd1, 'org.freedesktop.systemd1.Manager')

        # Gotta set the starting volume & mute states
        for stream_path in self.pulse_core.Get("org.PulseAudio.Core1", "PlaybackStreams"):
            self._NewPlaybackStream(stream_path)

        # True mute if *any* sink is muted
        self._MuteUpdated(
            bool(max([self.pulse_bus.get_object("org.PulseAudio.Core1.Device", sink).Get("org.PulseAudio.Core1.Device", "Mute")
                      for sink in self.pulse_core.Get("org.PulseAudio.Core1", "Sinks")])))

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
        # Systemd makes sure we can't stop/start something that is already in that state
        if any(role in self.known_stream_roles.values() for role in self.trigger_roles):
            print('Maybe starting media-playback. Current streams:', self.known_stream_roles)
            self.systemd1_manager.StartUnit(self.playback_target_name, 'replace')
        else:
            print('Maybe stopping media-playback. Current streams:', self.known_stream_roles)
            self.systemd1_manager.StopUnit(self.playback_target_name, 'replace')

    def _MuteUpdated(self, muted, device_path=None):
        if device_path is not None and device_path.rpartition('/')[-1].startswith('source'):
            print("Mute state changed for PA source device. Ignoring.")
            return

        if muted:
            print("Muted audio")
            self.systemd1_manager.StartUnit(self.muted_target_name, 'replace')
        else:
            print("Unmuted audio")
            self.systemd1_manager.StopUnit(self.muted_target_name, 'replace')

    def exit(self):
        """Exit the main loop."""
        if self.mainloop:
            print("Quitting mainloop")
            mainloop.quit()
        else:
            print("No mainloop to quit")


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--playback-target', default='media-playback.target', type=str,
                    help="The name of the systemd target for media playback status (default: 'media-playback.target'")
parser.add_argument('--muted-target', default='audio-muted.target', type=str,
                    help="The name of the systemd target for audio mute status (default: 'audio-muted.target'")
parser.add_argument('trigger_roles', nargs='+', type=str)
args = parser.parse_args()

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
mainloop = GLib.MainLoop()

# FIXME: Use SRV records or something instead of just hardcoding the snapserver details in here
pulse = PulseCorkHandler(trigger_roles=args.trigger_roles,
                         playback_target_name=args.playback_target,
                         muted_target_name=args.muted_target,
                         mainloop=mainloop)

systemd.daemon.notify('READY=1')
mainloop.run()
systemd.daemon.notify('STOPPING=1')
raise Exception("Uh, this should never happen as this should be a long running process.")
