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
    return dbus.UInt32(decimal * (65536))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--mute', type=str_to_bool,
                        help="Whether to mute/unmute the snapclient audio")

    parser.add_argument('--volume', type=float,
                        help="The volume (like 0.0-1.0) to set the pulseaudio sink to. Ignored without --sink or $PULSE_SINK")
    parser.add_argument('--sink', type=str, default=None,
                        help="The sink to control the volume of (default: $PULSE_SINK")

    args = parser.parse_args()

    pulse_bus = dbus.connection.Connection(get_pulse_bus_address())
    pulse_core = pulse_bus.get_object(object_path='/org/pulseaudio/core1')

    # NOTE: In theory this could find more than 1 stream, but that would be an error.
    snapclient_stream, = get_snapclient_streams(pulse_bus, pulse_core)

    # When I change default sink it moves things around but I want this to forcibly reset it to the correct sink regardless.
    if args.sink or os.environ.get('PULSE_SINK'):
        output_sink = get_sink_by_name(pulse_bus, pulse_core, args.sink or os.environ.get('PULSE_SINK'))
        snapclient_stream.Move(output_sink)
    else:
        output_sink = snapclient_stream.Get('org.PulseAudio.Core1.Stream', 'Device')

    # Mute before changing volume, so that we don't ever jump up to 100% before suddenly going silent
    if args.mute is not None and args.mute:
        snapclient_stream.Set("org.PulseAudio.Core1.Stream", "Mute",
                              dbus.Boolean(args.mute, variant_level=1))

    # We don't do any volume control for the Jellyfin SOE because it gets confused when synchronising the volume *to* snapcast
    # FIXME: Solve that somehow
    # But I use this for my desktop too, where it's useful to control the sink volume.
    # This is why we're relying on the args & environ, because they won't be set on the jellyfin SOE
    if args.volume is not None and (args.sink or os.environ.get('PULSE_SINK')):
        output_sink.Set("org.PulseAudio.Core1.Device", "Volume",
                        dbus.Array((convert_decimal_to_pa(args.volume),), variant_level=1))

    # Unmute after changing volume, so that we don't ever unmute at 100% before suddenly lowering volume
    if args.mute is not None and not args.mute:
        snapclient_stream.Set("org.PulseAudio.Core1.Stream", "Mute",
                              dbus.Boolean(args.mute, variant_level=1))
