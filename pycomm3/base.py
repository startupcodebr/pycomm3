# -*- coding: utf-8 -*-
#
# const.py - A set of structures and constants used to implement the Ethernet/IP protocol
#
# Copyright (c) 2020 Startup Code <suporte@startupcode.com.br>
# Copyright (c) 2019 Ian Ottoway <ian@ottoway.dev>
# Copyright (c) 2014 Agostino Ruscito <ruscito@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#

from os import getpid, urandom

from autologging import logged

from . import DataError, CommError
from .bytes_ import (pack_usint, pack_udint, pack_uint, pack_dint, unpack_uint,
                     unpack_usint, unpack_udint, print_bytes_msg,
                     DATA_FUNCTION_SIZE, UNPACK_DATA_FUNCTION)
from .const import (DATA_TYPE, TAG_SERVICES_REQUEST, ENCAPSULATION_COMMAND,
                    EXTENDED_SYMBOL, ELEMENT_ID, CLASS_CODE, PADDING_BYTE,
                    CONNECTION_SIZE, CLASS_ID, INSTANCE_ID, FORWARD_CLOSE,
                    FORWARD_OPEN, LARGE_FORWARD_OPEN,
                    CONNECTION_MANAGER_INSTANCE, PRIORITY, TIMEOUT_MULTIPLIER,
                    TIMEOUT_TICKS, TRANSPORT_CLASS, UNCONNECTED_SEND,
                    PRODUCT_TYPES, VENDORS, STATES)
from .packets import REQUEST_MAP
from .socket_ import Socket


@logged
class Base:
    _sequence = 0

    def __init__(self, direct_connection=False, debug=False):
        if Base._sequence == 0:
            Base._sequence = getpid()
        else:
            Base._sequence = Base._get_sequence()

        self._sock = None
        self.__direct_connections = direct_connection
        self._debug = debug
        self._session = 0
        self._connection_opened = False
        self._target_cid = None
        self._target_is_connected = False
        self._last_tag_read = ()
        self._last_tag_write = ()
        self._info = {}
        self.connection_size = 500
        self.attribs = {
            'context': b'_pycomm_',
            'protocol version': b'\x01\x00',
            'rpi': 5000,
            'port': 0xAF12,  # 44818
            'timeout': 10,
            'backplane': 1,
            'cpu slot': 0,
            'option': 0,
            'cid': b'\x27\x04\x19\x71',
            'csn': b'\x27\x04',
            'vid': b'\x09\x10',
            'vsn': b'\x09\x10\x19\x71',
            'name': 'Base',
            'ip address': None,
            'extended forward open': False
        }

    def __len__(self):
        return len(self.attribs)

    def __getitem__(self, key):
        return self.attribs[key]

    def __setitem__(self, key, value):
        self.attribs[key] = value

    def __delitem__(self, key):
        try:
            del self.attribs[key]
        except LookupError:
            pass

    def __iter__(self):
        return iter(self.attribs)

    def __contains__(self, item):
        return item in self.attribs

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self.close()
        except CommError:
            self.__log.exception('Error closing connection.')
            return False
        else:
            if not exc_type:
                return True
            else:
                self.__log.exception('Unhandled Client Error',
                                     exc_info=(exc_type, exc_val, exc_tb))
                return False

    def __repr__(self):
        _ = self._info
        return f"Program Name: {_.get('name')}, Device: {_.get('device_type', 'None')}, Revision: {_.get('revision', 'None')}"

    @property
    def connected(self):
        return self._connection_opened

    @property
    def info(self):
        return self._info

    @property
    def name(self):
        return self._info.get('name')

    def new_request(self, command):
        """ Creates a new RequestPacket based on the command"""
        cls = REQUEST_MAP[command]
        return cls(self)

    @staticmethod
    def _get_sequence():
        """ Increase and return the sequence used with connected messages

        :return: The New sequence
        """
        if Base._sequence < 65535:
            Base._sequence += 1
        else:
            Base._sequence = getpid() % 65535
        return Base._sequence

    def list_identity(self):
        """ ListIdentity command to locate and identify potential target

        return device description if reply contains valid response else none
        """
        request = self.new_request('list_identity')
        response = request.send()
        return response.identity

    def register_session(self):
        """ Register a new session with the communication partner

        :return: None if any error, otherwise return the session number
        """
        if self._session:
            return self._session

        self._session = 0
        request = self.new_request('register_session')
        request.add(self.attribs['protocol version'], b'\x00\x00')

        response = request.send()
        if response:
            self._session = response.session

            if self._debug:
                self.__log.debug(
                    f"Session = {response.session} has been registered.")
            return self._session

        self.__log.warning('Session has not been registered.')
        return None

    def un_register_session(self):
        """ Un-register a connection

        """
        request = self.new_request('unregister_session')
        request.send()
        self._session = None

    def forward_open(self):
        """ CIP implementation of the forward open message

        Refer to ODVA documentation Volume 1 3-5.5.2

        :return: False if any error in the replayed message
        """

        if self._target_is_connected:
            return True

        if self._session == 0:
            raise CommError("A Session Not Registered Before forward_open.")

        init_net_params = (True << 9) | (0 << 10) | (2 << 13) | (False << 15)
        if self.attribs['extended forward open']:
            connection_size = 4002
            net_params = pack_udint((self.connection_size & 0xFFFF)
                                    | init_net_params << 16)
        else:
            connection_size = 500
            net_params = pack_uint((self.connection_size & 0x01FF)
                                   | init_net_params)

        if self.__direct_connections:
            connection_params = [
                CONNECTION_SIZE['Direct Network'], CLASS_ID["8-bit"],
                CLASS_CODE["Message Router"]
            ]
        else:
            connection_params = [
                CONNECTION_SIZE['Backplane'],
            ]

        forward_open_msg = [
            FORWARD_OPEN if not self.attribs['extended forward open'] else
            LARGE_FORWARD_OPEN,
            b'\x02',  # CIP Path size
            CLASS_ID["8-bit"],  # class type
            CLASS_CODE["Connection Manager"],  # Volume 1: 5-1
            INSTANCE_ID["8-bit"],
            CONNECTION_MANAGER_INSTANCE['Open Request'],
            PRIORITY,
            TIMEOUT_TICKS,
            b'\x00\x00\x00\x00',
            self.attribs['cid'],
            self.attribs['csn'],
            self.attribs['vid'],
            self.attribs['vsn'],
            TIMEOUT_MULTIPLIER,
            b'\x00\x00\x00',
            b'\x01\x40\x20\x00',
            net_params,
            b'\x01\x40\x20\x00',
            net_params,
            TRANSPORT_CLASS,
            *connection_params,
            pack_usint(self.attribs['backplane']),
            pack_usint(self.attribs['cpu slot']),
            b'\x20\x02',
            INSTANCE_ID["8-bit"],
            b'\x01'
        ]
        request = self.new_request('send_rr_data')
        request.add(*forward_open_msg)
        response = request.send()
        if response:
            self._target_cid = response.data[:4]
            self._target_is_connected = True
            return True
        self.__log.warning(f"forward_open failed - {response.error}")
        return False

    def forward_close(self):
        """ CIP implementation of the forward close message

        Each connection opened with the froward open message need to be closed.
        Refer to ODVA documentation Volume 1 3-5.5.3

        :return: False if any error in the replayed message
        """

        if self._session == 0:
            raise CommError(
                "A session need to be registered before to call forward_close."
            )
        request = self.new_request('send_rr_data')

        forward_close_msg = [
            FORWARD_CLOSE,
            b'\x02',
            CLASS_ID["8-bit"],
            CLASS_CODE["Connection Manager"],  # Volume 1: 5-1
            INSTANCE_ID["8-bit"],
            CONNECTION_MANAGER_INSTANCE['Open Request'],
            PRIORITY,
            TIMEOUT_TICKS,
            self.attribs['csn'],
            self.attribs['vid'],
            self.attribs['vsn'],
            CLASS_ID["8-bit"],
            CLASS_CODE["Message Router"],
            INSTANCE_ID["8-bit"],
            b'\x01'
        ]

        if self.__direct_connections:
            forward_close_msg[11:2] = [
                CONNECTION_SIZE['Direct Network'], b'\x00'
            ]
        else:
            forward_close_msg[11:4] = [
                CONNECTION_SIZE['Backplane'], b'\x00',
                pack_usint(self.attribs['backplane']),
                pack_usint(self.attribs['cpu slot'])
            ]

        request.add(*forward_close_msg)
        response = request.send()
        if response:
            self._target_is_connected = False
            return True

        self.__log.warning(f"forward_close failed - {response.error}")
        return False

    def get_module_info(self, slot):
        try:
            if not self.forward_open():
                self.__log.warning(
                    "Target did not connected. get_plc_name will not be executed."
                )
                raise DataError(
                    "Target did not connected. get_plc_name will not be executed."
                )
            request = self.new_request('send_rr_data')
            request.add(
                # unnconnected send portion
                UNCONNECTED_SEND,
                b'\x02',
                CLASS_ID['8-bit'],
                b'\x06',  # class
                INSTANCE_ID["8-bit"],
                b'\x01',
                b'\x0A',  # priority
                b'\x0e\x06\x00',

                # Identity request portion
                b'\x01',  # Service
                b'\x02',
                CLASS_ID['8-bit'],
                CLASS_CODE['Identity Object'],
                INSTANCE_ID["8-bit"],
                b'\x01',  # Instance 1
                b'\x01\x00',
                b'\x01',  # backplane
                pack_usint(slot),
            )
            response = request.send()

            if response:
                info = self._parse_identity_object(response.data)
                return info
            else:
                raise DataError(
                    f'send_rr_data did not return valid data - {response.error}'
                )

        except Exception as err:
            raise DataError(err)

    @staticmethod
    def _parse_identity_object(reply):
        vendor = unpack_uint(reply[:2])
        product_type = unpack_uint(reply[2:4])
        product_code = unpack_uint(reply[4:6])
        major_fw = int(reply[6])
        minor_fw = int(reply[7])
        status = f'{unpack_uint(reply[8:10]):0{16}b}'
        serial_number = f'{unpack_udint(reply[10:14]):0{8}x}'
        product_name_len = int(reply[14])
        tmp = 15 + product_name_len
        device_type = reply[15:tmp].decode()

        state = unpack_uint(
            reply[tmp:tmp + 4]
        ) if reply[tmp:] else -1  # some modules don't return a state

        return {
            'vendor': VENDORS.get(vendor, 'UNKNOWN'),
            'product_type': PRODUCT_TYPES.get(product_type, 'UNKNOWN'),
            'product_code': product_code,
            'version_major': major_fw,
            'version_minor': minor_fw,
            'revision': f'{major_fw}.{minor_fw}',
            'serial': serial_number,
            'device_type': device_type,
            'status': status,
            'state': STATES.get(state, 'UNKNOWN'),
        }

    def _send(self, message):
        """
        socket send
        :return: true if no error otherwise false
        """
        try:
            if self._debug:
                self.__log.debug(
                    print_bytes_msg(message,
                                    '-------------- SEND --------------'))
            self._sock.send(message)
        except Exception as e:
            raise CommError(e)

    def _receive(self):
        """
        socket receive
        :return: reply data
        """
        try:
            reply = self._sock.receive()
        except Exception as e:
            raise CommError(e)
        else:
            if self._debug:
                self.__log.debug(
                    print_bytes_msg(reply, '----------- RECEIVE -----------'))
            return reply

    def open(self):
        """
        socket open
        :param: ip address to connect to and type of connection. By default direct connection is disabled
        :return: true if no error otherwise false
        """
        # handle the socket layer
        if not self._connection_opened:
            try:
                if self._sock is None:
                    self._sock = Socket()
                self._sock.connect(self.attribs['ip address'],
                                   self.attribs['port'])
                self._connection_opened = True
                self.attribs['cid'] = urandom(4)
                self.attribs['vsn'] = urandom(4)
                if self.register_session() is None:
                    self.__log.warning("Session not registered")
                    return False
                return True
            except Exception as e:
                raise CommError(e)

    def close(self):
        """
        socket close
        """
        errs = []
        try:
            if self._target_is_connected:
                self.forward_close()
            if self._session != 0:
                self.un_register_session()
        except Exception as err:
            errs.append(err)
            self.__log.warning(f"Error on close() -> session Err: {err}")

        # %GLA must do a cleanup __sock.close()
        try:
            if self._sock:
                self._sock.close()
        except Exception as err:
            errs.append(err)
            self.__log.warning(f"close() -> __sock.close Err: {err}")

        self.clean_up()

        if errs:
            raise CommError(' - '.join(str(e) for e in errs))

    def clean_up(self):
        self._sock = None
        self._target_is_connected = False
        self._session = 0
        self._connection_opened = False

    # --------------------------------------------------------------
    #  OLD CODE - to be removed
    #
    # --------------------------------------------------------------

    def _check_reply(self, reply):
        raise NotImplementedError("The method has not been implemented")

    def nop(self):
        """ No replay command

        A NOP provides a way for either an originator or target to determine if the TCP connection is still open.
        """
        message = self.build_header(ENCAPSULATION_COMMAND['nop'], 0)
        self._send(message)

    def send_unit_data(self, message):
        """ SendUnitData send encapsulated connected messages.

        :param message: The message to be send to the target
        :return: the replay received from the target
        """
        msg = self.build_header(ENCAPSULATION_COMMAND["send_unit_data"],
                                len(message))
        msg += message
        self._send(msg)
        reply = self._receive()
        status = self._check_reply(reply)
        return (True, reply) if status is None else (False, status)

    def build_header(self, command, length):
        """ Build the encapsulate message header

        The header is 24 bytes fixed length, and includes the command and the length of the optional data portion.

         :return: the header
        """
        try:
            h = command
            h += pack_uint(length)  # Length UINT
            h += pack_dint(self._session)  # Session Handle UDINT
            h += pack_dint(0)  # Status UDINT
            h += self.attribs['context']  # Sender Context 8 bytes
            h += pack_dint(self.attribs['option'])  # Option UDINT
            return h
        except Exception as e:
            raise CommError(e)

    @staticmethod
    def create_tag_rp(tag, multi_requests=False):
        """ Create tag Request Packet

        It returns the request packed wrapped around the tag passed.
        If any error it returns none
        """
        tags = tag.encode().split(b'.')
        rp = []
        index = []
        for tag in tags:
            add_index = False
            # Check if is an array tag
            if b'[' in tag:
                # Remove the last square bracket
                tag = tag[:len(tag) - 1]
                # Isolate the value inside bracket
                inside_value = tag[tag.find(b'[') + 1:]
                # Now split the inside value in case part of multidimensional array
                index = inside_value.split(b',')
                # Flag the existence of one o more index
                add_index = True
                # Get only the tag part
                tag = tag[:tag.find(b'[')]
            tag_length = len(tag)

            # Create the request path
            rp.append(EXTENDED_SYMBOL)  # ANSI Ext. symbolic segment
            rp.append(bytes([tag_length]))  # Length of the tag

            # Add the tag to the Request path
            rp += [bytes([char]) for char in tag]
            # Add pad byte because total length of Request path must be word-aligned
            if tag_length % 2:
                rp.append(PADDING_BYTE)
            # Add any index
            if add_index:
                for idx in index:
                    val = int(idx)
                    if val <= 0xff:
                        rp.append(ELEMENT_ID["8-bit"])
                        rp.append(pack_usint(val))
                    elif val <= 0xffff:
                        rp.append(ELEMENT_ID["16-bit"] + PADDING_BYTE)
                        rp.append(pack_uint(val))
                    elif val <= 0xfffffffff:
                        rp.append(ELEMENT_ID["32-bit"] + PADDING_BYTE)
                        rp.append(pack_dint(val))
                    else:
                        # Cannot create a valid request packet
                        return None

        # At this point the Request Path is completed,
        if multi_requests:
            request_path = bytes([len(rp) // 2]) + b''.join(rp)
        else:
            request_path = b''.join(rp)
        return request_path

    @staticmethod
    def build_common_packet_format(message_type,
                                   message,
                                   addr_type,
                                   addr_data=None,
                                   timeout=10):
        """ build_common_packet_format

        It creates the common part for a CIP message. Check Volume 2 (page 2.22) of CIP specification  for reference
        """
        msg = pack_dint(0)  # Interface Handle: shall be 0 for CIP
        msg += pack_uint(timeout)  # timeout
        msg += pack_uint(
            2)  # Item count: should be at list 2 (Address and Data)
        msg += addr_type  # Address Item Type ID

        if addr_data is not None:
            msg += pack_uint(len(addr_data))  # Address Item Length
            msg += addr_data
        else:
            msg += b'\x00\x00'  # Address Item Length
        msg += message_type  # Data Type ID
        msg += pack_uint(len(message))  # Data Item Length
        msg += message
        return msg

    @staticmethod
    def build_multiple_service(rp_list, sequence=None):
        mr = [
            bytes([TAG_SERVICES_REQUEST["Multiple Service Packet"]
                   ]),  # the Request Service
            pack_usint(2),  # the Request Path Size length in word
            CLASS_ID["8-bit"],
            CLASS_CODE["Message Router"],
            INSTANCE_ID["8-bit"],
            b'\x01',  # Instance 1
            pack_uint(
                len(rp_list))  # Number of service contained in the request
        ]
        if sequence is not None:
            mr.insert(0, pack_uint(sequence))
        # Offset calculation
        offset = (len(rp_list) * 2) + 2
        for index, rp in enumerate(rp_list):
            mr.append(pack_uint(offset))  # Starting offset
            offset += len(rp)

        mr += rp_list
        return mr

    @staticmethod
    def parse_multiple_request(message, tags, typ):
        """ parse_multi_request
        This function should be used to parse the message replayed to a multi request service rapped around the
        send_unit_data message.


        :param message: the full message returned from the PLC
        :param tags: The list of tags to be read
        :param typ: to specify if multi request service READ or WRITE
        :return: a list of tuple in the format [ (tag name, value, data type), ( tag name, value, data type) ].
                 In case of error the tuple will be (tag name, None, None)
        """
        offset = 50
        position = 50
        number_of_service_replies = unpack_uint(message[offset:offset + 2])
        tag_list = []
        for index in range(number_of_service_replies):
            position += 2
            start = offset + unpack_uint(message[position:position + 2])
            general_status = unpack_usint(message[start + 2:start + 3])

            if general_status == 0:
                if typ == "READ":
                    data_type = unpack_uint(message[start + 4:start + 6])
                    try:
                        value_begin = start + 6
                        value_end = value_begin + DATA_FUNCTION_SIZE[
                            DATA_TYPE[data_type]]
                        value = message[value_begin:value_end]
                        tag_list.append(
                            (tags[index],
                             UNPACK_DATA_FUNCTION[DATA_TYPE[data_type]](value),
                             DATA_TYPE[data_type]))
                    except LookupError:
                        tag_list.append((tags[index], None, None))
                else:
                    tag_list.append((tags[index] + ('GOOD', )))
            else:
                if typ == "READ":
                    tag_list.append((tags[index], None, None))
                else:
                    tag_list.append((tags[index] + ('BAD', )))
        return tag_list
