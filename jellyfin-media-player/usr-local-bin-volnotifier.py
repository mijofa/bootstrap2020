#!/usr/bin/python3
"""
Watch for changes to pulseaudio volume and send a libnotify message accordingly.

FIXME: Only watches the "combined" sink
NOTE: Depends on module-dbus-protocol being loaded into PulseAudio
"""
import os
import sys

import dbus
import dbus.mainloop.glib

import gi
from gi.repository import GLib
# FIXME: I don't actually know what versions I require, I just picked the current ones at time of writing
gi.require_version('Notify', '0.7')
gi.require_version('Gtk', '3.0')
from gi.repository import Notify  # noqa: E402 "module level import not at top of file"
from gi.repository import Gtk  # noqa: E402 "module level import not at top of file"


ICON_SIZE = 64
NOTIFICATION_TIMEOUT = 2000  # Same as xfce4-pulseaudio-plugin


# FIXME: "Note that you probably want to listen for icon theme changes and update the icon"
icon_theme = Gtk.IconTheme.get_default()
icons = {
    'muted': icon_theme.load_icon('audio-volume-muted', ICON_SIZE, 0).copy(),
    'low': icon_theme.load_icon('audio-volume-low', ICON_SIZE, 0).copy(),
    'medium': icon_theme.load_icon('audio-volume-medium', ICON_SIZE, 0).copy(),
    'high': icon_theme.load_icon('audio-volume-high', ICON_SIZE, 0).copy(),
}


class NotificationController(object):
    """Control the notification for volume & mute status."""

    def __init__(self, icons: dict, application_name=sys.argv[0]):
        """Initialise the notification."""
        self.icons = icons

        Notify.init(application_name)
        self.notif = Notify.Notification.new("Volume status")
        self.notif.set_timeout(NOTIFICATION_TIMEOUT)

    def _set_icon(self, icon_name):
        self.notif.set_image_from_pixbuf(self.icons[icon_name])

    def _get_icon_name_for_volume(self, muted, vol_percentage):
        if muted:
            return 'muted'
        elif vol_percentage >= ((1 / 3) * 2):
            return 'high'
        elif vol_percentage >= (1 / 3):
            return 'medium'
        else:
            return 'low'

    def update_notification(self, muted, vol_percentage):
        """Set the notification's volume & mute status, and reset the timeout."""
        # Y'know what, we're almost always ~150% anyway, and going above 100% is confusing to users.
        # So fuck it, just divide the percentage by 2.
        # This *only* affects the displayed percentage, not the actual volume of anything.
        vol_percentage = vol_percentage / 2

        self._set_icon(self._get_icon_name_for_volume(muted, vol_percentage))
        self.notif.set_property('summary', f'Volume: {vol_percentage:.0%}')
        self.notif.set_property('body', 'MUTED' if muted else '')
        self.notif.set_hint('value', GLib.Variant.new_int32(vol_percentage * 100))

        self.notif.show()


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
        self.pulse_core.ListenForSignal('org.PulseAudio.Core1.Device.VolumeUpdated',
                                        dbus.Array(signature='o'),
                                        dbus_interface='org.PulseAudio.Core1')
        self.pulse_bus.add_signal_receiver(self._MuteUpdated, 'MuteUpdated')
        self.pulse_bus.add_signal_receiver(self._VolumeUpdated, 'VolumeUpdated')

        # Gotta set the starting volume & mute states
        volumes = []
        mutes = []
        for sink in self.pulse_core.Get("org.PulseAudio.Core1", "Sinks"):
            device = self.pulse_bus.get_object("org.PulseAudio.Core1.Device", sink)
            volumes.extend(device.Get("org.PulseAudio.Core1.Device", "Volume"))
            mutes.append(device.Get("org.PulseAudio.Core1.Device", "Mute"))

        self._MuteUpdated(bool(max(mutes)))  # Set mute if *any* sink is muted
        self._VolumeUpdated(volumes)  # Set volume to average across all sinks

    def _MuteUpdated(self, muted):
        self.muted = bool(muted)

        self.VolumeUpdated(self.muted, self.current_volume)

    def _VolumeUpdated(self, volumes):
        if len(volumes) > 1:
            # When we have multiple speakers, just average the volume across each of them.
            vol = sum(volumes) / len(volumes)
        else:
            vol = volumes[0]
        self.current_volume = vol / 65536

        self.VolumeUpdated(self.muted, self.current_volume)

    def VolumeUpdated(self, muted, volume_percentage):
        """Replace this function with your volume status callback."""
        pass


dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)

pulse = PulseHandler()
notifier = NotificationController(icons)
pulse.VolumeUpdated = notifier.update_notification

GLib.MainLoop().run()
