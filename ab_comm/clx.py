__author__ = "Agostino Ruscito <ruscito@gmail.com>"
__status__ = "testing"
__version__ = "0.1"
__date__ = "01 01 2015"


from cip.cip_base import *
import logging


class Driver(object):
    def __init__(self):
        self.logger = logging.getLogger('ab_comm.clx')
        self.__version__ = '0.1'
        self.__sock = Socket(None)
        self.session = 0
        self.connection_opened = False
        self._replay = None
        self._message = None
        self.target_cid = None
        self.target_is_connected = False
        self.tag_list = []
        self._sequence = 1
        self._last_instance = 0
        self._more_packets_available = False
        self._last_tag_read = ()
        self._last_tag_write = ()
        self._status = (0, "")
        self.attribs = {'context': '_pycomm_', 'protocol version': 1, 'rpi': 5000, 'port': 0xAF12, 'timeout': 10,
                        'backplane': 1, 'cpu slot': 0, 'option': 0, 'cid': '\x27\x04\x19\x71', 'csn': '\x27\x04',
                        'vid': '\x09\x10', 'vsn': '\x09\x10\x19\x71'}

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

    def build_header(self, command, length):
        """
        build the encapsulated message header which is a 24 bytes fixed length.
        The header includes the command and the length of the optional data portion
        """
        h = command                                 # Command UINT
        h += pack_uint(length)                      # Length UINT
        h += pack_dint(self.session)                # Session Handle UDINT
        h += pack_dint(0)                           # Status UDINT
        h += self.attribs['context']                # Sender Context 8 bytes
        h += pack_dint(self.attribs['option'])      # Option UDINT
        return h

    def nop(self):
        self._message = self.build_header(ENCAPSULATION_COMMAND['nop'], 0)
        self.send()

    def list_identity(self):
        self._message = self.build_header(ENCAPSULATION_COMMAND['list_identity'], 0)
        self.send()
        self.receive()

    def get_status(self):
        return self._status

    def get_last_tag_read(self):
        return self._last_tag_read

    def get_last_tag_write(self):
        return self._last_tag_write

    def clear(self):
        self._status = (0, "")

    def register_session(self):
        self.logger.debug('[-> register_session]')
        if self.session:
            return self.session

        self._message = self.build_header(ENCAPSULATION_COMMAND['register_session'], 4)
        self._message += pack_uint(self.attribs['protocol version'])
        self._message += pack_uint(0)
        self.send()
        self.receive()
        if self._check_replay():
            self.session = unpack_dint(self._replay[4:8])
            self.logger.info("Session ={0} has been registered.".format(print_bytes_line(self._replay[4:8])))
            return self.session
        self.logger.warning('Session not registered.')
        return None

    def un_register_session(self):
        self._message = self.build_header(ENCAPSULATION_COMMAND['unregister_session'], 0)
        self.send()
        self.session = None

    def send_rr_data(self, msg):
        self._message = self.build_header(ENCAPSULATION_COMMAND["send_rr_data"], len(msg))
        self._message += msg
        self.send()
        self.receive()
        return self._check_replay()

    def send_unit_data(self, msg):
        self._message = self.build_header(ENCAPSULATION_COMMAND["send_unit_data"], len(msg))
        self._message += msg
        self.send()
        self.receive()
        return self._check_replay()

    def _get_sequence(self):
        if self._sequence < 65535:
            self._sequence += 1
        else:
            self._sequence = 1
        return self._sequence

    def _parse_tag_list(self, start_tag_ptr, status):
        tags_returned = self._replay[start_tag_ptr:]
        tags_returned_length = len(tags_returned)
        idx = 0
        instance = 0
        while idx < tags_returned_length:
            instance = unpack_dint(tags_returned[idx:idx+4])
            idx += 4
            tag_length = unpack_uint(tags_returned[idx:idx+2])
            idx += 2
            tag_name = tags_returned[idx:idx+tag_length]
            idx += tag_length
            symbol_type = unpack_uint(tags_returned[idx:idx+2])
            idx += 2
            self.tag_list.append((instance, tag_name, symbol_type))

        if status == SUCCESS:
            self._last_instance = -1
        elif status == 0x06:
            self._last_instance = instance + 1
        else:
            self.logger.warning('unknown status during _parse_tag_list')

    def _parse_multiple_request_read(self, tags):
        """ _parse_multiple_request_read

        """
        offset = 50
        position = 50
        number_of_service_replies = unpack_uint(self._replay[offset:offset+2])
        tag_list = []
        for index in range(number_of_service_replies):
            position += 2
            start = offset + unpack_uint(self._replay[position:position+2])
            general_status = unpack_sint(self._replay[start+2:start+3])

            if general_status == 0:
                data_type = unpack_uint(self._replay[start+4:start+6])
                try:
                    value_begin = start + 6
                    value_end = value_begin + DATA_FUNCTION_SIZE[I_DATA_TYPE[data_type]]
                    value = self._replay[value_begin:value_end]
                    self._last_tag_read = (tags[index], UNPACK_DATA_FUNCTION[I_DATA_TYPE[data_type]](value),
                                           I_DATA_TYPE[data_type])
                except LookupError:
                    self._last_tag_read = (tags[index], None, None)
            else:
                self._last_tag_read = (tags[index], None, None)

            tag_list.append(self._last_tag_read)

        return tag_list

    def _parse_multiple_request_write(self, tags):
        """ _parse_multiple_request_write

        """
        offset = 50
        position = 50
        number_of_service_replies = unpack_uint(self._replay[offset:offset+2])
        tag_list = []
        for index in range(number_of_service_replies):
            position += 2
            start = offset + unpack_uint(self._replay[position:position+2])
            general_status = unpack_sint(self._replay[start+2:start+3])

            if general_status == 0:
                self._last_tag_write = (tags[index] + ('GOOD',))
            else:
                self._last_tag_write = (tags[index] + ('BAD',))

            tag_list.append(self._last_tag_write)
        return tag_list

    def _check_replay(self):
        """ _check_replay

        """
        self._more_packets_available = False
        try:
            if self._replay is None:
                self.logger.warning('%s without reply' % REPLAY_INFO[unpack_dint(self._message[:2])])
                return False
            # Get the type of command
            typ = unpack_uint(self._replay[:2])

            # Encapsulation status check
            if unpack_dint(self._replay[8:12]) != SUCCESS:
                self._status = (1, "{0} reply status:{1}".format(REPLAY_INFO[typ],
                                                                 SERVICE_STATUS[unpack_dint(self._replay[8:12])]))
                self.logger.warning(self._status)
                return False

            # Command Specific Status check
            if typ == unpack_uint(ENCAPSULATION_COMMAND["send_rr_data"]):
                status = unpack_sint(self._replay[42:43])
                if unpack_sint(self._replay[40:41]) == I_TAG_SERVICES_REPLAY["Get Instance Attribute List"]:
                    self._parse_tag_list(44, status)
                    return True
                if status == 0x06:
                    self._status = (1, "Insufficient Packet Space")
                    self.logger.warning(self._status)
                    self._more_packets_available = True
                    return True
                elif status != SUCCESS:
                    self._status = (1, "send_rr_data reply status {0}: {1}. Extend status {2}".format(
                        "{:0>2x} ".format(ord(self._replay[42:43]), SERVICE_STATUS[status]),
                        get_extended_status=(self._replay, 42)))
                    self.logger.warning(self._status)
                    return False
                else:
                    return True

            elif typ == unpack_uint(ENCAPSULATION_COMMAND["send_unit_data"]):
                status = unpack_sint(self._replay[48:49])
                if unpack_sint(self._replay[46:47]) == I_TAG_SERVICES_REPLAY["Get Instance Attribute List"]:
                    self._parse_tag_list(50, status)
                    return True
                if status == 0x06:
                    self._status = (1, "Insufficient Packet Space")
                    self.logger.warning(self._status)
                    self._more_packets_available = True
                elif status != SUCCESS:
                    self._status = (1, "send_unit_data reply status {0}: {1}. Extend status {2}".format(
                        "{:0>2x} ".format(ord(self._replay[48:49]), SERVICE_STATUS[status]),
                        get_extended_status(self._replay, 48)))
                    self.logger.warning(self._status)
                    return False
                else:
                    return True

        except LookupError:
            self._status = (1, "LookupError inside _check_replay")
            self.logger.warning(self._status)
            return False

        return True

    def forward_open(self):
        if self.session == 0:
            self._status = (3, "A session need to be registered before to call forward_open.")
            self.logger.warning(self._status)
            return None

        forward_open_msg = [
            FORWARD_OPEN,
            pack_sint(2),
            CLASS_ID["8-bit"],
            CLASS_CODE["Connection Manager"],  # Volume 1: 5-1
            INSTANCE_ID["8-bit"],
            CONNECTION_MANAGER_INSTANCE['Open Request'],
            PRIORITY,
            TIMEOUT_TICKS,
            pack_dint(0),
            self.attribs['cid'],
            self.attribs['csn'],
            self.attribs['vid'],
            self.attribs['vsn'],
            TIMEOUT_MULTIPLIER,
            '\x00\x00\x00',
            pack_dint(self.attribs['rpi'] * 1000),
            pack_uint(CONNECTION_PARAMETER['Default']),
            pack_dint(self.attribs['rpi'] * 1000),
            pack_uint(CONNECTION_PARAMETER['Default']),
            TRANSPORT_CLASS,  # Transport Class
            CONNECTION_SIZE['Backplane'],
            pack_sint(self.attribs['backplane']),
            pack_sint(self.attribs['cpu slot']),
            CLASS_ID["8-bit"],
            CLASS_CODE["Message Router"],
            INSTANCE_ID["8-bit"],
            pack_sint(1)
        ]

        if self.send_rr_data(
                build_common_packet_format(DATA_ITEM['Unconnected'], ''.join(forward_open_msg), ADDRESS_ITEM['UCMM'],)):
            self.target_cid = self._replay[44:48]
            self.target_is_connected = True
            self.logger.info("The target is connected end returned CID %s" % print_bytes_line(self.target_cid))
            return True
        self._status = (3, "forward_open returned False")
        self.logger.warning(self._status)
        return False

    def forward_close(self):
        if self.session == 0:
            self._status = (4, "A session need to be registered before to call forward_close.")
            self.logger.warning(self._status)
            return None

        forward_close_msg = [
            FORWARD_CLOSE,
            pack_sint(2),
            CLASS_ID["8-bit"],
            CLASS_CODE["Connection Manager"],  # Volume 1: 5-1
            INSTANCE_ID["8-bit"],
            CONNECTION_MANAGER_INSTANCE['Open Request'],
            PRIORITY,
            TIMEOUT_TICKS,
            self.attribs['csn'],
            self.attribs['vid'],
            self.attribs['vsn'],
            CONNECTION_SIZE['Backplane'],
            '\x00',     # Reserved
            pack_sint(self.attribs['backplane']),
            pack_sint(self.attribs['cpu slot']),
            CLASS_ID["8-bit"],
            CLASS_CODE["Message Router"],
            INSTANCE_ID["8-bit"],
            pack_sint(1)
        ]
        if self.send_rr_data(
                build_common_packet_format(DATA_ITEM['Unconnected'], ''.join(forward_close_msg), ADDRESS_ITEM['UCMM'])):
            self.target_is_connected = False
            return True
        self._status = (4, "forward_close returned False")
        self.logger.warning(self._status)
        return False

    def read_tag(self, tag):
        """ read_tag

        """
        multi_requests = False
        if isinstance(tag, list):
            multi_requests = True

        if self.session == 0:
            self._status = (5, "A session need to be registered before to call read_tag.")
            self.logger.warning(self._status)
            return None

        if not self.target_is_connected:
            if not self.forward_open():
                self._status = (5, "Target did not connected. read_tag will not be executed.")
                self.logger.warning(self._status)
                return None

        if multi_requests:
            rp_list = []
            for t in tag:
                rp = create_tag_rp(t, multi_requests=True)
                if rp is None:
                    self._status = (5, "Cannot create tag {0} request packet. read_tag will not be executed.".format(tag))
                    self.logger.warning(self._status)
                    return None
                else:
                    rp_list.append(chr(TAG_SERVICES_REQUEST['Read Tag']) + rp + pack_uint(1))
            message_request = build_multiple_service(rp_list, self._get_sequence())

        else:
            rp = create_tag_rp(tag)
            if rp is None:
                self._status = (5, "Cannot create tag {0} request packet. read_tag will not be executed.".format(tag))
                self.logger.warning(self._status)
                return None
            else:
                # Creating the Message Request Packet
                message_request = [
                    pack_uint(self._get_sequence()),
                    chr(TAG_SERVICES_REQUEST['Read Tag']),  # the Request Service
                    chr(len(rp) / 2),                       # the Request Path Size length in word
                    rp,                                     # the request path
                    pack_uint(1)
                ]

        self.send_unit_data(
            build_common_packet_format(
                DATA_ITEM['Connected'],
                ''.join(message_request),
                ADDRESS_ITEM['Connection Based'],
                addr_data=self.target_cid,
            ))

        if multi_requests:
            return self._parse_multiple_request_read(tag)
        else:
            # Get the data type
            data_type = unpack_uint(self._replay[50:52])
            try:
                return UNPACK_DATA_FUNCTION[I_DATA_TYPE[data_type]](self._replay[52:]), I_DATA_TYPE[data_type]
            except LookupError:
                self._status = (5, "Unknown data type returned by read_tag")
                self.logger.warning(self._status)
                return None

    def write_tag(self, tag, value=None, typ=None):
        """ write_tag

        """
        multi_requests = False
        if isinstance(tag, list):
            multi_requests = True

        if self.session == 0:
            self._status = (6, "A session need to be registered before to call write_tag.")
            self.logger.warning(self._status)
            return None

        if not self.target_is_connected:
            if not self.forward_open():
                self._status = (6, "Target did not connected. write_tag will not be executed.")
                self.logger.warning(self._status)
                return None

        if multi_requests:
            rp_list = []
            tag_to_remove = []
            idx = 0
            for name, value, typ in tag:
                # Create the request path to wrap the tag name
                rp = create_tag_rp(name, multi_requests=True)
                if rp is None:
                    self._status = (6, "Cannot create tag{0} req. packet. write_tag will not be executed".format(tag))
                    self.logger.warning(self._status)
                    return None
                else:
                    try:    # Trying to add the rp to the request path list
                        val = PACK_DATA_FUNCTION[typ](value)
                        rp_list.append(
                            chr(TAG_SERVICES_REQUEST['Write Tag'])
                            + rp
                            + pack_uint(S_DATA_TYPE[typ])
                            + pack_uint(1)
                            + val
                        )
                        idx += 1
                    except (LookupError, struct.error) as e:
                        self._status = (6, "Tag:{0} type:{1} removed from write list. Error:{2}.".format(name, typ, e))
                        self.logger.warning(self._status)

                        # The tag in idx position need to be removed from the rp list because has some kind of error
                        tag_to_remove.append(idx)

            # Remove the tags that have not been inserted in the request path list
            for position in tag_to_remove:
                del tag[position]
            # Create the message request
            message_request = build_multiple_service(rp_list, self._get_sequence())

        else:
            name, value, typ = tag
            rp = create_tag_rp(name)
            if rp is None:
                self._status = (6, "Cannot create tag {0} request packet. write_tag will not be executed.".format(tag))
                self.logger.warning(self._statustag)
                return None
            else:
                # Creating the Message Request Packet
                message_request = [
                    pack_uint(self._get_sequence()),
                    chr(TAG_SERVICES_REQUEST["Write Tag"]),   # the Request Service
                    chr(len(rp) / 2),               # the Request Path Size length in word
                    rp,                             # the request path
                    pack_uint(S_DATA_TYPE[typ]),    # data type
                    pack_uint(1),                    # Add the number of tag to write
                    PACK_DATA_FUNCTION[typ](value)
                ]

        ret_val = self.send_unit_data(
            build_common_packet_format(
                DATA_ITEM['Connected'],
                ''.join(message_request),
                ADDRESS_ITEM['Connection Based'],
                addr_data=self.target_cid,
            )
        )

        if multi_requests:
            return self._parse_multiple_request_write(tag)
        else:
            return ret_val

    def get_tag_list(self):
        """ _get_symbol_object_instances

        """

        if self.session == 0:
            self._status = (6, "A session need to be registered before to call get_tag_list.")
            self.logger.warning(self._status)
            return None

        if not self.target_is_connected:
            if not self.forward_open():
                self._status = (6, "Target did not connected. get_tag_list will not be executed.")
                self.logger.warning(self._status)
                return None

        self._last_instance = 0

        while self._last_instance != -1:

            # Creating the Message Request Packet

            message_request = [
                pack_uint(self._get_sequence()),
                chr(TAG_SERVICES_REQUEST['Get Instance Attribute List']),
                # the Request Path Size length in word
                chr(3),
                # Request Path ( 20 6B 25 00 Instance )
                CLASS_ID["8-bit"],       # Class id = 20 from spec 0x20
                CLASS_CODE["Symbol Object"],  # Logical segment: Symbolic Object 0x6B
                INSTANCE_ID["16-bit"],   # Instance Segment: 16 Bit instance 0x25
                '\x00',
                pack_uint(self._last_instance),          # The instance
                # Request Data
                pack_uint(2),   # Number of attributes to retrieve
                pack_uint(1),   # Attribute 1: Symbol name
                pack_uint(2)    # Attribute 2: Symbol type
            ]

            self.send_unit_data(
                build_common_packet_format(
                    DATA_ITEM['Connected'],
                    ''.join(message_request),
                    ADDRESS_ITEM['Connection Based'],
                    addr_data=self.target_cid,
                ))

        return self.tag_list

    def send(self):
        try:
            self.logger.debug(print_bytes_msg(self._message, '-------------- SEND --------------'))
            self.__sock.send(self._message)
        except SocketError as e:
            self._status = (2, "Error {0} during {1}".format(e, 'send'))
            self.logger.critical(self._status)
            return False

        return True

    def receive(self):
        try:
            self._replay = self.__sock.receive()
            self.logger.debug(print_bytes_msg(self._replay, '----------- RECEIVE -----------'))
        except SocketError as e:
            self._status = (2, "Error {0} during {1}".format(e, 'send'))
            self.logger.critical(self._status)
            return False

        return True

    def open(self, ip_address):
        # handle the socket layer
        if not self.connection_opened:
            try:
                self.__sock.connect(ip_address, self.attribs['port'])
                self.connection_opened = True
                if self.register_session() is None:
                    self._status = (2, "Session not registered")
                    self.logger.error(self._status)
                    return False
                return True
            except SocketError as e:
                self._status = (2, "Error {0} during {1}".format(e, 'send'))
                self.logger.critical(self._status)
        return False

    def close(self):
        if self.target_is_connected:
            self.forward_close()
        if self.session != 0:
            self.un_register_session()
        self.__sock.close()
        self.__sock = None
        self.session = 0
        self.connection_opened = False