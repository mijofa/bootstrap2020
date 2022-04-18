#!/usr/bin/python3
"""A script to allow snapclient to set the PulseAudio sink volume & mute state."""
import argparse
import os
import getpass

import dbus


def str_to_bool(s: str):
    """Return a boolean for the given string, follows the same semantics systemd does."""
    if s.lower() in ('1', 'yes', 'true', 'on'):
        return True
    elif s.lower() in ('0', 'no', 'false', 'off'):
        return False
    else:
        raise NotImplementedError(f"Unknown boolean value from string: {s}")


def get_pulse_bus_address():
    """Get the PulseAudio D-Bus session address, either from environment or by asking the normal D-Bus session."""
    if 'PULSE_DBUS_SERVER' in os.environ:
        address = os.environ['PULSE_DBUS_SERVER']
    else:
        bus = dbus.SessionBus()
        server_lookup = bus.get_object("org.PulseAudio1", "/org/pulseaudio/server_lookup1")
        address = server_lookup.Get("org.PulseAudio.ServerLookup1", "Address",
                                    dbus_interface="org.freedesktop.DBus.Properties")
    return address


def get_snapclient_streams(bus, core):
    """
    Get Snapclient's playback stream.

    This is used so that we can mute the Snapcast group without muting the actual video playback,
    while still maintaining control over the video playback volume from the Snapcast server.
    """
    for stream_path in core.Get("org.PulseAudio.Core1", "PlaybackStreams"):
        stream = bus.get_object("org.PulseAudio.Core1.PlaybackStream", stream_path)
        PropertyList = stream.Get("org.PulseAudio.Core1.Stream", "PropertyList")
        # DBus's named arrays are incredibly difficult to work with, convert it to a dict
        properties = {str(k): bytes(b for b in v).strip(b'\x00').decode() for k, v in PropertyList.items()}

        # Consider any binary named 'snapclient' to be what we're looking for,
        # but ignore any by other users, just in case.
        if properties.get('application.process.user') == getpass.getuser() and \
                properties.get('application.process.binary') == 'snapclient':
            yield stream


def get_fallback_sink(bus, core):
    """Get the dbus path for the current fallback/default sink."""
    return bus.get_object("org.PulseAudio.Core1.Device", core.Get("org.PulseAudio.Core1", "FallbackSink"))


def get_sink_by_name(bus, core, sink_name):
    """Get sink object for the given sink name."""
    for sink_path in core.Get('org.PulseAudio.Core1', 'Sinks'):
        sink = bus.get_object("org.PulseAudio.Core1.Device", sink_path)
        if sink_name == sink.Get('org.PulseAudio.Core1.Device', 'Name'):
            return sink


def convert_decimal_to_pa(decimal):
    """Convert a decimal percentage into something PulseAudio better accepts."""
    # 65536 = PulseAudio's 100%
    # But we normally go above 100%,
    # so in order to maintain more control over the volume from Snapcast I'm doubling it.
    # This should result in Snapcasts slider going covering PulseAudio's 0-200% instead.
    return dbus.UInt32(decimal * (65536 * 2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    # FIXME: --help-all does not work without all other required arguments.
    #        I don't currently have any idea how to work around this.
    parser.add_argument('--volume', type=float,
                        help="The volume percentage (as decimal: 0-1) to set the pulseaudio sink to.")
    parser.add_argument('--mute', type=str_to_bool,
                        help="Whether to mute the sink or not")

    parser.add_argument('--sink', type=str, default=None,
                        help="The sink to control the state of (default: $PULSE_SINK or fallback)")

    args = parser.parse_args()

    pulse_bus = dbus.connection.Connection(get_pulse_bus_address())
    pulse_core = pulse_bus.get_object(object_path='/org/pulseaudio/core1')

    if not args.sink and not os.environ.get('PULSE_SINK'):
        sink = get_fallback_sink(pulse_bus, pulse_core)
    elif not args.sink and os.environ.get('PULSE_SINK'):
        sink = get_sink_by_name(pulse_bus, pulse_core, os.environ['PULSE_SINK'])
    else:
        sink = get_sink_by_name(pulse_bus, pulse_core, args.sink)

    if args.mute is not None:
        for stream in get_snapclient_streams(pulse_bus, pulse_core):
            stream.Set("org.PulseAudio.Core1.Stream", "Mute",
                       dbus.Boolean(args.mute, variant_level=1))
    if args.volume is not None:
        sink.Set("org.PulseAudio.Core1.Device", "Volume",
                 dbus.Array((convert_decimal_to_pa(args.volume),), variant_level=1))
