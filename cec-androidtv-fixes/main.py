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
import systemd.journal


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

logger = logging.getLogger(__name__ if __name__ != '__main__' else None)
stderr_handler = logging.StreamHandler()
logger.addHandler(stderr_handler)
journal_handler = systemd.journal.JournalHandler()
logger.addHandler(journal_handler)


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
        logger.log(level=CEC_LOGGING_LEVELS[level], msg=f'CEC({level}): {message}')

    def send_command(self, destination: int, opcode: int, parameter: int = None, extra: int = None):
        """Send a CEC command (initiator is automatically filled in)."""
        # This website greatly helped me understand how CEC command strings even work:
        # https://www.cec-o-matic.com/
        # FIXME: There can be an infinite number of parameters
        # FIXME: Figure out how to use the cec.cec_command() object directly insttead of going via CommandFromString
        command_string = ':'.join([f'{self.own_address:1x}{destination:1x}',
                                   f'{opcode:02x}',
                                   *([f'{parameter:02x}'] if parameter is not None else []),
                                   *([f'{extra:02x}'] if extra is not None else [])
                                   ])
        logger.log(logging.DEBUG, "Sending command: %s", command_string)
        return self.lib.Transmit(self.lib.CommandFromString(command_string))


class cec_tv(object):
    """Represents a TV device, used by the CEC handler."""

    def __init__(self, parent: cec_handler):
        """Initialise the TV over CEC."""
        self.parent = parent
        self.device_address = cec.CECDEVICE_TV

    def _user_control_exceptions(self, key_code: int):
        """
        Handle functions for keys that don't quite work via CEC_USER_CONTROL.

        There are a few of controls that don't work directly with my AndroidTV,
        but there's other CEC functions that can do the same thing anyway.
        I don't know whether this is AndroidTV's fault, **my** AndroidTV's fault,
        or just badly standardised CEC in general.
        """
        exceptions = {
            # FIXME: Haven't figured out power on, so we only support standby/power-off for now
            #        I'll probably need androidtvremote2 for the power on
            cec.CEC_USER_CONTROL_CODE_POWER_OFF_FUNCTION: lambda: self.parent.lib.StandbyDevices(),
            # FIXME: I hope this isn't creating a **new** event loop
            cec.CEC_USER_CONTROL_CODE_CONTENTS_MENU: lambda: asyncio.get_event_loop().create_task(
                self.hold_control(cec.CEC_USER_CONTROL_CODE_SELECT)),
        }
        if key_code in exceptions:
            exceptions[key_code]()
            return True
        else:
            return False

    def send_command(self, opcode: int, parameter: int = None):
        """Send a CEC command to this device."""
        # FIXME: There can be an infinite number of parameters
        return self.parent.send_command(destination=self.device_address,
                                        opcode=opcode, parameter=parameter)

    def press_control(self, key_code: int):
        """Send press then release signal to the TV."""
        if self._user_control_exceptions(key_code):
            return True
        else:
            # These return True on success and False on failure,
            # so wrapping it in min() ensures we'll get False if either one of them fail
            # FIXME: Does this gaurantee that release will always trigger **after** pressed?
            return min(self.send_command(opcode=cec.CEC_OPCODE_USER_CONTROL_PRESSED,
                                         parameter=key_code),
                       self.send_command(opcode=cec.CEC_OPCODE_USER_CONTROL_RELEASE))

    # FIXME: I'm only using this because there's no "context menu" button and holding enter/select works well enough
    async def hold_control(self, key_code: int):
        """Send control press, delay half a second, then release to the TV."""
        if self.send_command(opcode=cec.CEC_OPCODE_USER_CONTROL_PRESSED,
                             parameter=key_code):
            await asyncio.sleep(0.5)
            return self.send_command(opcode=cec.CEC_OPCODE_USER_CONTROL_RELEASE)
        else:
            return False


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
        logger.info('EVDEV: Registering device %s', dev.name)
        try:
            async for event in dev.async_read_loop():
                if event.type in self.event_map.keys() and \
                        event.code in self.event_map[event.type] and \
                        event.value:
                    try:
                        self.event_map[event.type][event.code]()
                    except:  # noqa: E722 "do not use bare 'except'"
                        # Report errors, but don't stop the loop for them
                        logger.error(traceback.format_exc())
                elif event.type == evdev.ecodes.EV_KEY and event.value:
                    logger.debug("EVDEV: Unrecognised key: %s", evdev.categorize(event))
                # elif event.type != evdev.ecodes.EV_SYN:
                #     print("Ignoring", evdev.categorize(event))
        except OSError as e:
            if e.errno == 19:
                logger.info("EVDEV: Looks like device was removed, stopping device loop")
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
        key_queue = asyncio.Queue()
        loop = asyncio.get_event_loop()
        loop.add_reader(sys.stdin,
                        lambda: asyncio.ensure_future(key_queue.put(
                            # Reads the entire stdin buffer to the queue at once,
                            # to ensure escape sequences like up/down/left/right come through as one key.
                            # However does mean pasting into the terminal will appear as one "key" as well,
                            # that's probably a good thing though.
                            sys.stdin.read(len(sys.stdin.buffer.peek())))))

        logger.info("STDIN: Ready")
        while True:
            key = await key_queue.get()
            if key == '\x04':  # EOF/Ctrl-D
                return
            elif key in self.event_map:
                try:
                    self.event_map[key]()
                except:  # noqa: E722 "do not use bare 'except'"
                    # Report errors, but don't stop the loop for them
                    logger.error(traceback.format_exc())
            else:
                logger.warning("STDIN: Unrecognised key: %s", repr(key))

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
            logger.debug("STDIN: Restoring terminal config")
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, stdin_settings)

        # If the stdin loop stopped, we should stop all the whole program
        asyncio.get_event_loop().stop()


class keybindings_table(object):
    """
    Maps events from evdev & stdin to CEC & androidtvremote (and snapcast in future?).

    Done as a separate class just because it's easier to define the mappings when the other class objects are already defined.
    """

    def __init__(self, loop: asyncio.BaseEventLoop, TV: cec_tv):
        """Initialise required callable objects."""
        self.loop = loop
        self.TV = TV

    def evdev_mapping(self):
        """Return the key -> function mapping for evdev events."""
        return {evdev.ecodes.EV_KEY: {
            # FIXME: Why doen't 'cec.CEC_USER_CONTROL_CODE_PAUSE_PLAY_FUNCTION' do anything?
            evdev.ecodes.KEY_REWIND: lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_REWIND),
            evdev.ecodes.KEY_PLAYPAUSE: lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_PLAY),
            evdev.ecodes.KEY_FASTFORWARD: lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_FAST_FORWARD),
            evdev.ecodes.KEY_PREVIOUSSONG: lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_BACKWARD),
            evdev.ecodes.KEY_STOPCD: lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_PAUSE),
            evdev.ecodes.KEY_NEXTSONG: lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_FORWARD),

            evdev.ecodes.KEY_MUTE: lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_MUTE),
            evdev.ecodes.KEY_VOLUMEUP: lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_VOLUME_UP),
            evdev.ecodes.KEY_VOLUMEDOWN: lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_VOLUME_DOWN),

            # FIXME: "POWER" is not currently supported, and "POWER_OFF_FUNCTION" is implemented hacky as hell
            evdev.ecodes.KEY_CLOSE: lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_POWER),
            evdev.ecodes.KEY_SLEEP: lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_POWER_OFF_FUNCTION),

            # evdev.ecodes.KEY_HOMEPAGE: HOME
        }}

    def stdin_mapping(self):
        """Return the key -> function mapping for stdin events."""
        return {
            '\x1b[A': lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_UP),
            '\x1b[B': lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_DOWN),
            '\x1b[C': lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_RIGHT),
            '\x1b[D': lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_LEFT),
            '\x1b': lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_EXIT),
            '\n': lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_SELECT),
            # ' ': lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_PAUSE_PLAY_FUNCTION),
            'p': lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_PAUSE),
            'P': lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_PLAY),
            'S': lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_STOP),

            # Opens settings, same as the native remotes gear button
            '~': lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_SETUP_MENU),
            # Opens the input selector (HDMI/AV/Tuner/etc)
            'I': lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_INPUT_SELECT),
            # Makes Jellyfin show the current time & media name, not progress
            'i': lambda: self.TV.press_control(cec.CEC_USER_CONTROL_CODE_DISPLAY_INFORMATION),

            # This one's an async function because I have an explicit sleep, can I make all of them async functions?
            # I'm using this for "context menu" which is actually done by holding down select
            'm': lambda: self.loop.create_task(self.TV.hold_control(cec.CEC_USER_CONTROL_CODE_SELECT)),
            # CEC-o-matic says "reserved", ir-keytable says KEY_WWW. TV says "feature abort"
            # 'x': lambda: self.TV.press_control(0x59),

            # '\x1b[H': HOME
        }


if __name__ == '__main__':
    # FIXME: Use proper argument handling
    if sys.argv[-1] == '--debug':
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)

    loop = asyncio.get_event_loop()
    cec_hub = cec_handler()
    glue = keybindings_table(loop=loop, TV=cec_hub.TV)
    loop.create_task(evdev_keybinds(event_map=glue.evdev_mapping()).main_loop())
    loop.create_task(stdin_keybinds(event_map=glue.stdin_mapping()).main_loop())
    loop.run_forever()
