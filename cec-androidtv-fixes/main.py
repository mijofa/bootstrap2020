#!/usr/bin/python3
"""
Various workarounds for CEC & AndroidTV.

Primary purpose will be to resend IR inputs as CEC because to the TV.
Secondary purpose is to keep the amplifier unmuted for background music when the TV powers down.
"""
import asyncio
import logging
import socket
import sys
import termios
import traceback
import typing

import cec
import evdev
import pyudev


# Map the CEC log levels to the logging module's log levels
CEC_LOGGING_LEVELS = {
    cec.CEC_LOG_ERROR: logging.ERROR,
    cec.CEC_LOG_WARNING: logging.WARNING,
    cec.CEC_LOG_NOTICE: logging.INFO,
    cec.CEC_LOG_DEBUG: logging.DEBUG,
    cec.CEC_LOG_TRAFFIC: logging.DEBUG,
    # logging.CRITICAL == logging.FATAL
    # logging.ERROR
    # logging.WARN == logging.WARNING
    # logging.INFO
    # logging.DEBUG
    # logging.NOTSET
}


class cec_handler(object):
    """Handle the CEC communication."""

    # FIXME: I don't know whether the cec library is properly threadsafe or compatible with asyncio,
    #        but it's working alright so it seems to be at least one of those things.
    # FIXME: Is cec.CEC_DEVICE_TYPE_PLAYBACK a more fitting device type?
    #        I figured since this device is intended for streaming media tuner made more sense.
    #        Kinda equivalent to an FM receiver with multiple input channels to choose from.
    def __init__(self, device_type: int = cec.CEC_DEVICE_TYPE_TUNER):
        """Initialise the CEC adapter and devices."""
        self.cecconfig = cec.libcec_configuration()
        self.cecconfig.strDeviceName = socket.gethostname()
        self.cecconfig.deviceTypes.Add(device_type)  # FIXME: Is Playback more fitting?
        self.cecconfig.SetLogCallback(self._log_callback)

        # Don't switch to this input source when we open the CEC interface
        self.cecconfig.bActivateSource = False
        # FIXME: Should probably take a look at the other cecconfig.b... options

        self.lib = cec.ICECAdapter.Create(self.cecconfig)

        # This will crash if there is not exactly 1 CEC adapter available.
        adapter, = self.lib.DetectAdapters()
        # FIXME: Is there a better way to open this from the adapter **object** instead of the name?
        self.lib.Open(adapter.strComName)

        # Update the config device to determine the physical & logical addresses
        self.lib.GetCurrentConfiguration(self.cecconfig)

        self.own_address = self.cecconfig.logicalAddresses.primary
        # Sanity check because I'm not sure that logical address behaves the way I think it does.
        assert self.lib.GetDevicePhysicalAddress(self.own_address) == self.cecconfig.iPhysicalAddress
        assert self.lib.GetDeviceOSDName(self.own_address) == self.cecconfig.strDeviceName

        device_addresses = self.lib.GetActiveDevices()
        device_addresses.IsSet(cec.CECDEVICE_TV), "No TV connected, can we even do anything useful?"
        self.TV = cec_tv(self)

    def _log_callback(self, level: int, time: int, message: str):
        # This ignores the time argument and just assumes "now".
        logging.log(level=CEC_LOGGING_LEVELS[level], message=message)

    def send_command(self, destination: int, opcode: int, parameter: int = None, extra: int = None):
        """Send a CEC command (initiator is automatically filled in)."""
        # This website greatly helped me understand how CEC command strings even work:
        # https://www.cec-o-matic.com/
        # FIXME: Figure out how to use the cec.cec_command() object directly insttead of going via CommandFromString
        command = self.lib.CommandFromString(':'.join([f'{self.own_address:1x}{destination:1x}',
                                                       f'{opcode:02x}',
                                                       *([f'{parameter:02x}'] if parameter is not None else []),
                                                       *([f'{extra:02x}'] if extra is not None else [])
                                                       ]))
        return self.lib.Transmit(command)


class cec_tv(object):
    """Represents a TV device, used by the CEC handler."""

    def __init__(self, parent: cec_handler):
        """Initialise the TV over CEC."""
        self.parent = parent
        self.device_address = cec.CECDEVICE_TV

    def press_control(self, key_code):
        """Send press then release signal to the TV."""
        retval = self.parent.send_command(destination=self.device_address,
                                          opcode=cec.CEC_OPCODE_USER_CONTROL_PRESSED,
                                          parameter=key_code)
        if retval:
            return self.parent.send_command(destination=self.device_address,
                                            opcode=cec.CEC_OPCODE_USER_CONTROL_RELEASE)


class evdev_keybinds(object):
    """Handle global keybindings for media keys and such."""

    def __init__(self, event_map: dict):
        """Initialize the udev context."""
        self.event_map = event_map
        self.udev_context = pyudev.Context()

    async def main_loop(self):
        """Event loops for each input device as they appear."""
        async for udev_dev in self.iter_monitor_devices(self.udev_context, subsystem='input'):
            if udev_dev.device_node and udev_dev.device_node in evdev.list_devices():
                evdev_dev = evdev.InputDevice(udev_dev.device_node)
                if self._is_device_capable(evdev_dev.capabilities()):
                    asyncio.ensure_future(self.device_loop(evdev_dev))

    async def device_loop(self, dev):
        """Handle the given events for the given device."""
        print('Registering evdev device', dev.name)
        try:
            async for event in dev.async_read_loop():
                if event.type in self.event_map.keys() and \
                        event.code in self.event_map[event.type] and \
                        event.value:
                    # print("Processing trigger for", evdev.categorize(event))
                    try:
                        self.event_map[event.type][event.code]()
                    except:  # noqa: E722 "do not use bare 'except'"
                        # Report errors, but don't stop the loop for them
                        print(traceback.format_exc(), file=sys.stderr)
                # elif event.type != evdev.ecodes.EV_SYN:
                #     print("Ignoring", evdev.categorize(event))
        except OSError as e:
            if e.errno == 19:
                print("Looks like device was removed, stopping device loop")
            else:
                raise

    # ref: https://github.com/pyudev/pyudev/issues/450#issuecomment-1078863332
    async def iter_monitor_devices(self, context: pyudev.Context, **kwargs) -> typing.AsyncGenerator[pyudev.Device, None]:
        """Yield all udev devices and continue monitoring for device changes."""
        for device in context.list_devices(**kwargs):
            yield device

        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by(**kwargs)
        monitor.start()
        fd = monitor.fileno()
        read_event = asyncio.Event()
        loop = asyncio.get_event_loop()
        loop.add_reader(fd, read_event.set)
        try:
            while True:
                await read_event.wait()
                while True:
                    device = monitor.poll(0)
                    if device is not None:
                        yield device
                    else:
                        read_event.clear()
                        break
        finally:
            loop.remove_reader(fd)

    def _is_device_capable(self, dev_caps: dict):
        """Check that the given device is has at least 1 of the mapped capabilities."""
        for cap_type in self.event_map:
            if cap_type not in dev_caps:
                continue
            for cap in self.event_map[cap_type]:
                if cap in dev_caps.get(cap_type):
                    return True
        return False


class stdin_keybinds(object):
    """Handle locacl keybindings for navigation & alphanumeric keys."""

    def __init__(self, event_map: dict):
        """Initialise the stdin event map."""
        self.event_map = event_map

    async def _device_loop(self):
        """Handle the stdin."""
        print("Registering stdin")
        key_queue = asyncio.Queue()
        loop = asyncio.get_event_loop()
        loop.add_reader(sys.stdin,
                        lambda: asyncio.ensure_future(key_queue.put(
                            # Reads the entire stdin buffer to the queue at once,
                            # to ensure escape sequences like up/down/left/right come through as one key.
                            # However does mean pasting into the terminal will appear as one "key" as well,
                            # that's probably a good thing though.
                            sys.stdin.read(len(sys.stdin.buffer.peek())))))

        while True:
            key = await key_queue.get()
            if key in self.event_map:
                self.event_map[key]()
            else:
                print("Unrecognised key:", repr(key))

    async def main_loop(self):
        """Reconfigure the terminal and run the device loop."""
        stdin_fd = sys.stdin.fileno()
        stdin_settings = termios.tcgetattr(stdin_fd)
        try:
            temp_settings = stdin_settings.copy()
            temp_settings[3] = temp_settings[3] & ~termios.ICANON
            temp_settings[3] = temp_settings[3] & ~termios.ECHO
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, temp_settings)
            await self._device_loop()
        finally:
            print("Restoring terminal config")
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, stdin_settings)


EVDEV_EVENT_MAPPING = {
    evdev.ecodes.EV_KEY: {
        evdev.ecodes.KEY_INFO: lambda: print("evdev, info pressed"),
    },
}
EVDEV_CEC_MAPPING = {
    evdev.ecodes.KEY_INFO: 'abc'
}
STDIN_CEC_MAPPING = {
    '\x1b[A': cec.CEC_USER_CONTROL_CODE_UP,
    '\x1b[B': cec.CEC_USER_CONTROL_CODE_DOWN,
    '\x1b[C': cec.CEC_USER_CONTROL_CODE_RIGHT,
    '\x1b[D': cec.CEC_USER_CONTROL_CODE_LEFT,
    '\x1b': cec.CEC_USER_CONTROL_CODE_EXIT,
    '\n': cec.CEC_USER_CONTROL_CODE_ENTER,
}

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.create_task(evdev_keybinds(event_map=EVDEV_EVENT_MAPPING).main_loop())
    loop.create_task(stdin_keybinds(event_map={k: lambda code=v: print(code) for k, v in STDIN_CEC_MAPPING.items()}).main_loop())
    loop.run_forever()
