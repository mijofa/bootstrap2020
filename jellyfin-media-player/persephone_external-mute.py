#!/usr/bin/python3
"""
Watch for changes to pulseaudio mute status and turn speaker power on/off accordingly.

NOTE: Depends on module-dbus-protocol being loaded into PulseAudio
"""
import os
import json
import urllib.parse
import urllib.request

import dbus
import dbus.mainloop.glib

from gi.repository import GLib

IOT_URL = "http://mijofa-lounge-speakers/cm"
MUTE_DATA = urllib.parse.urlencode({'cmnd': 'Power Off'}, quote_via=urllib.parse.quote).encode()
UNMUTE_DATA = urllib.parse.urlencode({'cmnd': 'Power On'}, quote_via=urllib.parse.quote).encode()


class PulseHandler(object):
    """Handle D-Bus signals from PulseAudio."""

    current_volume = 0
    muted = True

    def _get_bus_address(self):
        if 'PULSE_DBUS_SERVER' in os.environ:
            address = os.environ['PULSE_DBUS_SERVER']
        else:
            bus = dbus.SessionBus()
            server_lookup = bus.get_object("org.PulseAudio1", "/org/pulseaudio/server_lookup1")
            address = server_lookup.Get("org.PulseAudio.ServerLookup1", "Address",
                                        dbus_interface="org.freedesktop.DBus.Properties")
        return address

    def __init__(self, bus_address=None):
        """Set up D-Bus listeners."""
        if not bus_address:
            bus_address = self._get_bus_address()
        self.pulse_bus = dbus.connection.Connection(bus_address)
        self.pulse_core = self.pulse_bus.get_object(object_path='/org/pulseaudio/core1')
        self.pulse_core.ListenForSignal('org.PulseAudio.Core1.Device.MuteUpdated',
                                        dbus.Array(signature='o'),
                                        dbus_interface='org.PulseAudio.Core1')
        self.pulse_bus.add_signal_receiver(self._MuteUpdated, 'MuteUpdated')

        # Gotta set the starting volume & mute states
        mutes = []
        for sink in self.pulse_core.Get("org.PulseAudio.Core1", "Sinks"):
            device = self.pulse_bus.get_object("org.PulseAudio.Core1.Device", sink)
            mutes.append(device.Get("org.PulseAudio.Core1.Device", "Mute"))

        self._MuteUpdated(bool(max(mutes)))  # Set mute if *any* sink is muted

    def _MuteUpdated(self, muted):
        with urllib.request.urlopen(IOT_URL, MUTE_DATA if muted else UNMUTE_DATA) as resource:
            assert resource.code == 200
            response = resource.read().decode(resource.headers.get_content_charset() or 'utf-8')
            data = json.loads(response)
            return data


dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

pulse = PulseHandler()

GLib.MainLoop().run()
