#!/usr/bin/python3
"""
Synchronise PulseAudio's volume to the Snapcast group.

This is a one-direcitonal sync, if the group volume gets updated it won't be reflected in PulseAudio.
I would like to support that eventually, but it caused some difficulties and there's some concurrency complexities to be resolved.

FIXME: Requires snapclient's volume updates be disabled otherwise the changes will be doubled.
"""

import argparse
import math
import os

import dbus
# Needed for the dbus mainloop
import dbus.mainloop.glib

from gi.repository import GLib

# This module is only useful for running as a systemd unit to update the state of this unit,
# it doesn't help query systemd's unit status so we use dbus for that
import systemd.daemon

import snapcontroller


def mean_average(list_of_numbers):
    """Return the mean average of a list of numbers."""
    return sum(list_of_numbers) / len(list_of_numbers)


def pa_array_to_dict(array):
    """
    Convert a D-Bus named array into a Python dict.

    FIXME: This assumes all keys and values are meant to be strings.
    Dbus's arrays come out annoyingly, firstly they use the dbus version of strings/ints/etc,
    but the string values come out as a zero-terminated byte array.

    So I gotta jump through some hoops to convert things around properly so that I can use them like normal dicts
    """
    return {str(k): bytes(b for b in v).strip(b'\x00').decode() for k, v in array.items()}


def get_PA_bus():
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


class PulseSnapgroupHandler(object):
    """Handle D-Bus signals from PulseAudio."""

    mainloop = None
    prev_snapclient_volume = None

    def __init__(self, multiplier: int, snap_conn, mainloop=None):
        """Set up D-Bus listeners."""
        if mainloop:
            self.mainloop = mainloop

        self.multiplier = multiplier
        self.snap_conn = snap_conn
        self.snap_client_id = snapcontroller.get_physical_mac()

        self.pulse_bus = get_PA_bus()
        self.pulse_core = self.pulse_bus.get_object(object_path='/org/pulseaudio/core1')

        # mute state
        self.pulse_core.ListenForSignal('org.PulseAudio.Core1.Device.MuteUpdated',
                                        dbus.Array(signature='o'),
                                        dbus_interface='org.PulseAudio.Core1')
        self.pulse_bus.add_signal_receiver(self._MuteUpdated, 'MuteUpdated')
        # volume percentage
        self.pulse_core.ListenForSignal('org.PulseAudio.Core1.Device.VolumeUpdated',
                                        dbus.Array(signature='o'),
                                        dbus_interface='org.PulseAudio.Core1')
        self.pulse_bus.add_signal_receiver(self._VolumeUpdated, 'VolumeUpdated')

        # Gotta set the starting volume & mute states from the default sink
        sink = self.pulse_bus.get_object("org.PulseAudio.Core1.Device",
                                         self.pulse_core.Get("org.PulseAudio.Core1", "FallbackSink"))
        self._MuteUpdated(sink.Get("org.PulseAudio.Core1.Device", "Mute"))
        self._VolumeUpdated(sink.Get("org.PulseAudio.Core1.Device", "Volume"))

    def _MuteUpdated(self, muted):
        if muted:
            print("Muting audio in snapclient")
            self.snap_conn.run_command(method='Group.SetMute',
                                       params={'id': self.snap_conn.get_group_of_client(self.snap_client_id),
                                               'mute': True})
        else:
            print("Unmuting audio in snapclient")
            self.snap_conn.run_command(method='Group.SetMute',
                                       params={'id': self.snap_conn.get_group_of_client(self.snap_client_id),
                                               'mute': False})

    def _VolumeUpdated(self, unused_volumes):
        # Because this function can take a while to finish, it will sometimes get triggered multiple times while already running.
        # So if this takes 1min to run and the volume is incremented by 2% 10 times, then it will actually take 10mins to catch up.
        # To workaround this I'm just going to get the fallback sink's volume at the start of this function and use that,
        # resulting in up to 2mins to catch up in that user-story, even though the function will still run another 8 times.

        volumes = self.pulse_bus.get_object("org.PulseAudio.Core1.Device",
                                            self.pulse_core.Get("org.PulseAudio.Core1",
                                                                "FallbackSink")).Get("org.PulseAudio.Core1.Device",
                                                                                     "Volume")
        vol = mean_average(volumes)

        volume_percentage = vol / 65536
        # Apply the multiplier, and turn it into a round number from 0-100
        snapclient_volume = math.ceil(max(0, min(100,
                                                 volume_percentage * self.multiplier * 100)))

        if snapclient_volume == self.prev_snapclient_volume:
            return
        else:
            print("Updating snapclient volume to", snapclient_volume)
            self.prev_snapclient_volume = snapclient_volume
            # FIXME: Do some sort of time-based memoization for the group ID
            self.snap_conn.run_command(method='Group.SetVolume',
                                       params={'id': self.snap_conn.get_group_of_client(self.snap_client_id),
                                               'percent': snapclient_volume})

    def exit(self):
        """Exit the main loop."""
        if self.mainloop:
            print("Quitting mainloop")
            mainloop.quit()
        else:
            print("No mainloop to quit")


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--multiplier', default=0.5, type=int,
                    help="Multiplier to apply to the PA volume before updating snapcast (default 0.5)")
args = parser.parse_args()

dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
mainloop = GLib.MainLoop()

pulse = PulseSnapgroupHandler(multiplier=args.multiplier,
                              snap_conn=snapcontroller.SnapController(),
                              mainloop=mainloop)

systemd.daemon.notify('READY=1')
mainloop.run()
systemd.daemon.notify('STOPPING=1')
raise Exception("Uh, this should never happen as this should be a long running process.")
