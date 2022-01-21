#!/usr/bin/python3
"""
Remotely control a snapserver instance over JSON-RPC.

ref: https://github.com/badaix/snapcast/blob/master/doc/json_rpc_api/control.md
"""

import argparse
import json
import pprint  # noqa: F401 "imported but unused"  # This is useful for debugging
import socket
import time

import psutil


API_CMD_ARGS = {
    "Client": {
        'params': {
            "--id": {'help': "ID of the client to query/control", 'default': '[local mac address]', 'type': str},
        },
        'COMMANDS': {
            "GetStatus": {'help': "Query status & state info"},
            "SetVolume": {'help': "Set the volume & mute state",
                          'params': {"--muted": {'action': 'store_true'}, "--percent": {'type': int, 'required': True}}},
            "SetLatency": {'help': "FIXME",
                           'params': {"--latency": {'type': int, 'required': True}}},
            "SetName": {'help': "Set the human-readable name for the client",
                        'params': {'--name': {'type': str, 'required': True}}},
        }},
    "Group": {
        'params': {
            "--id": {'help': "ID of the group to query/control", 'default': "[local machine's group]", 'type': str},
        },
        'COMMANDS': {
            "GetStatus": {'help': "Query status & state info"},
            "SetMute": {'help': "Set mute state (defaults to unmute)",
                        # FIXME: This one is messy
                        # FIXME: Add a custom 'toggle mute' command
                        'params': {'--mute': {'action': 'store_true', 'help': "Set mute instead of unmute"}}},
            "SetStream": {'help': "Tune this group into a specific stream",
                          'params': {'--stream_id': {'help': "ID of the stream to tune in to", 'type': str, 'required': True}}},
            "SetClients": {'help': "Set what clients are part of this group",
                           'params': {'--clients': {'help': 'The client IDs to include in the group', 'type': str, 'nargs': '+'}}},
            "SetName": {'help': "Set the human-readable name for the group",
                        # FIXME: Also not properly tested because it's not supported by my stupidly old snapserver
                        'params': {'--name': {'help': 'The name to set', 'type': str, 'required': True}}},
        }},
    "Server": {
        'COMMANDS': {
            "GetRPCVersion": {'help': 'Query the API version the server users'},
            "GetStatus": {'help': 'Query status & state info'},
            "DeleteClient": {'help': 'Delete a client from the list',
                             'params': {'--id': {'nargs': 1, 'help': 'The client to delete'}}},
        }},
    "Stream": {
        'params': {
            "--id": {'help': "ID of the stream to query/control", 'type': str, 'required': True},
        },
        'COMMANDS': {
            # "AddStream": {'DEFAULT': 'FIXME'},  # Not something general users should mess with
            # "RemoveStream": {'DEFAULT': 'FIXME'},  # Not something general users should mess with
            # FIXME: Untested due to not being currently supported by my server
            "Control": {'help': 'Control the playback state of the specified stream',
                        'params': {'--command': {'help': "The command to send to the stream's player", 'required': True,
                                                 'choices': ['play', 'pause', 'playPause', 'stop',
                                                             'next', 'previous', 'seek', 'setPosition']},
                                   # FIXME: this --params is probably broken
                                   '--params': {'help': "Depends on the --command argument. See https://github.com/badaix/snapcast/blob/master/doc/json_rpc_api/stream_plugin.md#pluginstreamplayercontrol"}  # noqa: E501 "line too long"
                                   }}
            # "SetProperty": {'DEFAULT': 'FIXME'},  # Probably not something general users should mess with, not sure though
        }},
    # NOTE: I've intentionally left out the 'Notifications' commands because they're painful to implement and I don't need them
}


def get_physical_mac():
    """
    Get the MAC address of a physical NIC that has an IP address.

    Returns None for anything other than 1 valid MAC address,
    because there's no way to identify which is valid of multiple MAC addresses
    """
    active_macs = []
    all_addresses = psutil.net_if_addrs()

    for nic in all_addresses:
        if nic == 'lo':
            continue  # Ignore loopback device

        # Each NIC has multiple addresses, split them up by address family
        by_family = {addr.family: addr.address for addr in all_addresses[nic]}

        if socket.AddressFamily.AF_PACKET not in by_family:
            continue  # These seem to be virtual/VPN NICs

        # We only care about the ones with an IPv4 or IPv6 address
        # (really we only care about IPv4 at the moment, but that should really be fixed elsewhere)
        if by_family.get(socket.AddressFamily.AF_INET) or by_family.get(socket.AddressFamily.AF_INET6):
            active_macs.append(by_family.get(socket.AddressFamily.AF_PACKET))

    if len(active_macs) == 1:
        return active_macs[0]
    else:
        # We can't be sure which one is valid, so just don't accept any of them
        return None


class SnapException(Exception):
    """Error from Snapserver."""

    def __init__(self, code, message, data=None):  # noqa: D107 "Missing docstring in __init__"
        self.code = code
        self.data = data
        self.message = message

        super().__init__(f'{self.message} {self.code}: {self.data}')


class SnapController(object):
    """Snapserver controller."""

    last_command_id = 0

    def __init__(self, host: str = 'music', port: int = 1705):
        """Initialise the socket connections."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((host, port))
        self.sock.setblocking(0)

    def __enter__(self):  # noqa: D105 "Missing docstring in magic method"
        return self

    def __exit__(self, *exc_info):  # noqa: D105 "Missing docstring in magic method"
        self.sock.close()
        # FIXME: Is there any protocol specific hangup command?

    def _recv_all_rawdata(self, blocking: bool = True, chunksize: int = 1024):
        """Keep recieving data until there's no more to recieve."""
        blocked = True
        while blocked:
            try:
                data = self.sock.recv(chunksize)
            except BlockingIOError:
                if blocking:
                    blocked = True
                else:
                    blocked = False
            else:
                blocked = False

        if len(data) == chunksize:
            data += self._recv_all_rawdata(blocking=False, chunksize=chunksize)

        return data

    def recv_data(self):
        """Recv data as json."""
        raw_data = self._recv_all_rawdata()

        data = json.loads(raw_data.decode().strip())

        return data

    def send_data(self, data):
        """Send data as json."""
        self.sock.send(json.dumps(data).encode())
        # NOTE: My older version of snapserver does *not* support '\n', I don't know if that gets better with newer versions
        self.sock.send(b'\r\n')

        # FIXME: Sockets have no 'flush' function, it takes a sec for the other end to respond
        time.sleep(0.1)

    def get_group_of_client(self, client_id):
        """Find the group that has the given client as a member."""
        server_status = self.run_command('Server.GetStatus')
        clients_by_group = {group['id']: [client['id'] for client in group['clients']]
                            for group in server_status['server']['groups']}

        for group_id, clients in clients_by_group.items():
            if client_id in clients:
                return group_id

        return None

    def get_all_streams(self):
        """Get all streams available on server."""
        server_status = self.run_command('Server.GetStatus')
        return sorted([s['id'] for s in server_status['server']['streams']])

    def run_command(self, method: str, params: dict = {}):
        """Send the specific command & params."""
        # FIXME: I believe this ID here is to ensure the result string we recieve is specifically for this command.
        #        So theoretically I could make this whole asyncio or threading friendly by recieving responses in separate a thread
        #        then using the ID to map it back to required run_command function to return it to the caller
        data = {
            "jsonrpc": "2.0",
            "id": self.last_command_id,
            "method": method,
        }
        if params:
            data['params'] = params
            if 'percent' in data['params'] and 'muted' in data['params']:
                data['params']['volume'] = {'percent': data['params'].pop('percent'),
                                            'muted': data['params'].pop('muted')}

            for k, v in data['params'].items():
                if k == 'id':
                    if v == '[local mac address]':
                        data['params'][k] = get_physical_mac()
                    elif v == "[local machine's group]":
                        data['params'][k] = self.get_group_of_client(get_physical_mac())

        self.send_data(data)

        response = self.recv_data()
        assert response['id'] == data['id'], response
        self.last_command_id += 1  # Increment the ID for the next one

        if 'error' in response:
            raise SnapException(**response['error'])
        elif 'result' in response:
            return response['result']
        else:
            raise NotImplementedError(response)


def help_all(parser, top_level=True):
    """Return the help string for parser and all subparsers."""
    # FIXME: What about sub-sub-parsers?
    if not top_level:
        parser.print_usage()

    try:
        subparsers_action, = (
            action for action in parser._actions
            if isinstance(action, argparse._SubParsersAction))
    except ValueError:
        # There's no subparser actions here, that's fine
        return

    for choice, subparser in subparsers_action.choices.items():
        help_all(subparser, top_level=False)


def gen_argparser():
    """Generate the argparser and subparsers."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    # FIXME: --help-all does not work without all other required arguments.
    #        I don't currently have any idea how to work around this.
    parser.add_argument('--help-all', action='store_true')
    parser.add_argument('--host', default='music', type=str,
                        help="The snapserver host to control")
    parser.add_argument('--port', default=1705, type=int,
                        help="The snapserver remote control port number. NOTE: http control not supported at this time")

    # Eah command group should be a separate subparser with its own arguments
    subparsers = parser.add_subparsers(dest='method_group')
    subparsers.required = True
    for cmd in API_CMD_ARGS:
        cmd_parser = subparsers.add_parser(cmd)
        if 'params' in API_CMD_ARGS[cmd]:
            for arg, arg_args in API_CMD_ARGS[cmd].pop('params').items():
                cmd_parser.add_argument(arg, **arg_args)

        # And each command within the group should have its own subparser with its own arguments
        cmd_subparsers = cmd_parser.add_subparsers(dest='method')
        cmd_subparsers.required = True
        for sub_cmd in API_CMD_ARGS[cmd]['COMMANDS']:
            if 'params' in API_CMD_ARGS[cmd]['COMMANDS'][sub_cmd]:
                params = API_CMD_ARGS[cmd]['COMMANDS'][sub_cmd].pop('params')
            else:
                params = {}
            sub_cmd_parser = cmd_subparsers.add_parser(sub_cmd,
                                                       description=API_CMD_ARGS[cmd]['COMMANDS'][sub_cmd]['help'],
                                                       **API_CMD_ARGS[cmd]['COMMANDS'][sub_cmd])
            for arg in params:
                sub_cmd_parser.add_argument(arg, **params[arg])

    return parser


if __name__ == '__main__':
    parser = gen_argparser()

    # I want to just iterate over a bunch of the arguments,
    # but since a namespace can't be .pop()-ed it's a lot easier if I use a dict instead
    parsed_args = parser.parse_args()
    params = vars(parsed_args)

    if params.pop('help_all'):
        parser.print_help()
        print()
        help_all(parser)
        exit()

    method = f"{params.pop('method_group')}.{params.pop('method')}"

    with SnapController(params.pop('host'), params.pop('port')) as ctrl:
        api_version = ctrl.run_command('Server.GetRPCVersion')
        if api_version != {'major': 2, 'minor': 0, 'patch': 0}:
            raise NotImplementedError("RPC API version mismatch")

        # FIXME: This is dumping to json data that was only just recently loaded from json
        print(json.dumps(ctrl.run_command(method, params), indent=4, sort_keys=True))
