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

import struct
from collections import defaultdict
from types import GeneratorType
from typing import Union, List, Sequence, Tuple, Optional, Any

from autologging import logged

from . import DataError, Tag, RequestError
from .base import Base
from .bytes_ import (pack_dint, pack_uint, pack_udint, pack_usint,
                     unpack_usint, unpack_uint, unpack_dint, unpack_udint,
                     pack_sint, UNPACK_DATA_FUNCTION, PACK_DATA_FUNCTION,
                     DATA_FUNCTION_SIZE)
from .const import (SUCCESS, EXTENDED_SYMBOL, ENCAPSULATION_COMMAND, DATA_TYPE,
                    BITS_PER_INT_TYPE, REPLY_INFO, TAG_SERVICES_REQUEST,
                    PADDING_BYTE, ELEMENT_ID, DATA_ITEM, ADDRESS_ITEM,
                    CLASS_ID, CLASS_CODE, INSTANCE_ID, INSUFFICIENT_PACKETS,
                    REPLY_START, BASE_TAG_BIT, MULTISERVICE_READ_OVERHEAD,
                    MULTISERVICE_WRITE_OVERHEAD, MIN_VER_INSTANCE_IDS,
                    REQUEST_PATH_SIZE, VENDORS, PRODUCT_TYPES, KEYSWITCH,
                    TAG_SERVICES_REPLY, get_service_status,
                    get_extended_status, TEMPLATE_MEMBER_INFO_LEN,
                    EXTERNAL_ACCESS, DATA_TYPE_SIZE)

# re_bit = re.compile(r'(?P<base>^.*)\.(?P<bit>([0-2][0-9])|(3[01])|[0-9])$')


def with_forward_open(func):
    """Decorator to ensure a forward open request has been completed with the plc"""
    def forward_open_decorator(self, *args, **kwargs):
        if not self.forward_open():
            msg = f'Target did not connected. {func.__name__} will not be executed.'
            self.__log.warning(msg)
            raise DataError(msg)
        return func(self, *args, **kwargs)

    return forward_open_decorator


@logged
class LogixDriver(Base):
    """
    An Ethernet/IP Client library for reading and writing tags in ControlLogix and CompactLogix PLCs.

    The following services have been implemented:
        - Read Tag Service (0x4c)
        - Read Tag Fragment Service (0x52)
        - Write Tag Service (0x4d)
        - Write Tag Fragment Service (0x53)
        - Multiple Service Packet (0x0a)
        - Read Modify Write Tag (0xce)

"""
    def __init__(self,
                 ip_address: str,
                 *args,
                 slot: int = 0,
                 large_packets: bool = True,
                 init_info: bool = True,
                 init_tags: bool = True,
                 init_program_tags: bool = False,
                 **kwargs):
        """
        :param ip_address: IP address of PLC
        :param slot: Slot of PLC in chassis (leave at 0 for CompactLogix)
        :param large_packets: if True, Extended Forward Open will be used (v20+ and EN2T+)
        :param init_info:  if True, initializes controller info (name, revision, etc) on connect
        :param init_tags: if True, uploads all controller-scoped tag definitions on connect
        :param init_program_tags: if True, uploads all program-scoped tag definitions on connect
        """
        super().__init__(*args, **kwargs)
        self._cache = None

        self._data_types = {}
        self._program_names = []
        self._tags = {}

        self.attribs['ip address'] = ip_address
        self.attribs['cpu slot'] = slot
        self.attribs['extended forward open'] = large_packets
        self.connection_size = 4000 if large_packets else 500
        self.use_instance_ids = True

        if init_tags or init_info:
            self.open()
            if init_info:
                self.get_plc_info()
                self.use_instance_ids = self.info.get(
                    'version_major', 0) >= MIN_VER_INSTANCE_IDS
                self.get_plc_name()

            if init_tags:
                self.get_tag_list(program='*' if init_program_tags else None)

    @property
    def tags(self) -> dict:
        """
        Read-only property to access all the tag definitions uploaded from the controller.
        """
        return self._tags

    @property
    def data_types(self):
        return self._data_types

    @with_forward_open
    def get_plc_name(self) -> str:
        try:
            request = self.new_request('send_unit_data')
            request.add(
                bytes([TAG_SERVICES_REQUEST['Get Attributes']]),
                REQUEST_PATH_SIZE,
                CLASS_ID['8-bit'],
                CLASS_CODE['Program Name'],
                INSTANCE_ID["16-bit"],
                b'\x00',
                b'\x01\x00',  # Instance 1
                b'\x01\x00',  # Number of Attributes
                b'\x01\x00'  # Attribute 1 - program name
            )

            response = request.send()

            if response:
                self._info['name'] = _parse_plc_name(response)
                return self._info['name']
            else:
                raise DataError(
                    f'send_unit_data did not return valid data - {response.error}'
                )

        except Exception as err:
            raise DataError(err)

    @with_forward_open
    def get_plc_info(self) -> dict:
        """
        Reads basic information from the controller, returns it and stores it in the ``info`` property.

        info = {
            'vendor': 'Rockwell Automation/Allen-Bradley',
            'product_type': 'Programmable Logic Controller',
            'product_code': 55,
            'version_major': 20,
            'version_minor': 12,
            'revision': '20.12',
            'serial': '00ff00ff',
            'device_type': '1756-L62/B LOGIX5562',
            'keyswitch': 'REMOTE RUN',
            'name': 'PLCA'
        }
        """
        try:
            request = self.new_request('send_unit_data')
            request.add(
                b'\x01',  # Service
                REQUEST_PATH_SIZE,
                CLASS_ID['8-bit'],
                CLASS_CODE['Identity Object'],
                INSTANCE_ID["16-bit"],
                b'\x00',
                b'\x01\x00',  # Instance 1
            )
            response = request.send()

            if response:
                info = _parse_plc_info(response.data)
                self._info = {**self._info, **info}
                return info
            else:
                raise DataError(
                    f'send_unit_data did not return valid data - {response.error}'
                )

        except Exception as err:
            raise DataError(err)

    @with_forward_open
    def get_tag_list(self,
                     program: str = None,
                     cache: bool = True) -> List[dict]:
        """
        Returns the list of tags from the controller. For only controller-scoped tags, set `program` to None (default).
        Set `program` to a program name to only get the program scoped tags from the specified program.
        To get all controller and all program scoped tags from all programs, set `program` to '*'

        Note, for program scoped tags the tag['tag_name'] will be 'Program:{program}.{tag_name}'. This is so the tag
        list can be fed directly into the read function.

        If the `cache` parameter is True (default), the list of tags will be stored so they can be referenced later.
        This also allows the read/write methods to use the cached instance id's and allow packing more tags into a single
        request.  While this method returns a list of tags, when cached the tag list is stored as a dict of {tag['tag_name'] : tag, ...}
        """

        self._cache = {
            'tag_name:id': {},
            'id:struct': {},
            'handle:id': {},
            'id:udt': {}
        }

        if program == '*':
            tags = self._get_tag_list()
            for prog in self._program_names:
                tags += self._get_tag_list(prog)
        else:
            tags = self._get_tag_list(program)

        if cache:
            self._tags = {tag['tag_name']: tag for tag in tags}

        self._cache = None

        return tags

    def _get_tag_list(self, program=None):
        all_tags = self._get_instance_attribute_list_service(program)
        user_tags = self._isolating_user_tag(all_tags, program)
        for tag in user_tags:
            if tag['tag_type'] == 'struct':
                tag['data_type'] = self._get_data_type(
                    tag['template_instance_id'])

        return user_tags

    def _get_instance_attribute_list_service(self, program=None):
        """ Step 1: Finding user-created controller scope tags in a Logix5000 controller

        This service returns instance IDs for each created instance of the symbol class, along with a list
        of the attribute data associated with the requested attribute
        """
        try:
            last_instance = 0
            tag_list = []
            while last_instance != -1:
                # Creating the Message Request Packet
                path = []
                if program:
                    if not program.startswith('Program:'):
                        program = f'Program:{program}'
                    path = [
                        EXTENDED_SYMBOL,
                        pack_usint(len(program)),
                        program.encode('utf-8')
                    ]
                    if len(program) % 2:
                        path.append(b'\x00')

                path += [
                    # Request Path ( 20 6B 25 00 Instance )
                    CLASS_ID["8-bit"],  # Class id = 20 from spec 0x20
                    CLASS_CODE[
                        "Symbol Object"],  # Logical segment: Symbolic Object 0x6B
                    INSTANCE_ID[
                        "16-bit"],  # Instance Segment: 16 Bit instance 0x25
                    b'\x00',
                    pack_uint(last_instance),  # The instance
                ]
                path = b''.join(path)
                path_size = pack_usint(len(path) // 2)
                request = self.new_request('send_unit_data')
                request.add(
                    bytes([
                        TAG_SERVICES_REQUEST['Get Instance Attributes List']
                    ]),
                    path_size,
                    path,
                    # Request Data
                    b'\x07\x00',  # Number of attributes to retrieve
                    b'\x01\x00',  # Attr. 1: Symbol name
                    b'\x02\x00',  # Attr. 2 : Symbol Type
                    b'\x03\x00',  # Attr. 3 : Symbol Address
                    b'\x05\x00',  # Attr. 5 : Symbol Object Address
                    b'\x06\x00',  # Attr. 6 : ? - Not documented (Software Control?)
                    b'\x0a\x00',  # Attr. 10 : external access
                    b'\x08\x00'  # Attr. 8 : array dimensions [1,2,3]
                )
                response = request.send()
                if not response:
                    raise DataError(
                        f"send_unit_data returned not valid data - {response.error}"
                    )

                last_instance = self._parse_instance_attribute_list(
                    response, tag_list)
            return tag_list

        except Exception as e:
            raise DataError(e)

    def _parse_instance_attribute_list(self, response, tag_list):
        """ extract the tags list from the message received"""

        tags_returned = response.data
        tags_returned_length = len(tags_returned)
        idx = count = instance = 0
        try:
            while idx < tags_returned_length:
                instance = unpack_dint(tags_returned[idx:idx + 4])
                idx += 4
                tag_length = unpack_uint(tags_returned[idx:idx + 2])
                idx += 2
                tag_name = tags_returned[idx:idx + tag_length]
                idx += tag_length
                symbol_type = unpack_uint(tags_returned[idx:idx + 2])
                idx += 2
                count += 1
                symbol_address = unpack_udint(tags_returned[idx:idx + 4])
                idx += 4
                symbol_object_address = unpack_udint(tags_returned[idx:idx +
                                                                   4])
                idx += 4
                software_control = unpack_udint(tags_returned[idx:idx + 4])
                idx += 4
                access = tags_returned[idx] & 0b_0011
                idx += 1
                dim1 = unpack_udint(tags_returned[idx:idx + 4])
                idx += 4
                dim2 = unpack_udint(tags_returned[idx:idx + 4])
                idx += 4
                dim3 = unpack_udint(tags_returned[idx:idx + 4])
                idx += 4

                tag_list.append({
                    'instance_id':
                    instance,
                    'tag_name':
                    tag_name,
                    'symbol_type':
                    symbol_type,
                    'symbol_address':
                    symbol_address,
                    'symbol_object_address':
                    symbol_object_address,
                    'software_control':
                    software_control,
                    'external_access':
                    EXTERNAL_ACCESS.get(access, 'Unknown'),
                    'dimensions': [dim1, dim2, dim3]
                })

        except Exception as e:
            raise DataError(e)

        if response.service_status == SUCCESS:
            last_instance = -1
        elif response.service_status == INSUFFICIENT_PACKETS:
            last_instance = instance + 1
        else:
            self.__log.warning(
                'unknown status during _parse_instance_attribute_list')
            last_instance = -1

        return last_instance

    def _isolating_user_tag(self, all_tags, program=None):
        try:
            user_tags = []
            for tag in all_tags:
                name = tag['tag_name'].decode()
                if 'Program:' in name:
                    self._program_names.append(name)
                    continue
                if ':' in name or '__' in name:
                    continue
                if tag['symbol_type'] & 0b0001_0000_0000_0000:
                    continue

                if program is not None:
                    name = f'{program}.{name}'

                self._cache['tag_name:id'][name] = tag['instance_id']

                new_tag = {
                    'tag_name': name,
                    'dim': (tag['symbol_type'] & 0b0110000000000000) >>
                    13,  # bit 13 & 14, number of array dims
                    'instance_id': tag['instance_id'],
                    'symbol_address': tag['symbol_address'],
                    'symbol_object_address': tag['symbol_object_address'],
                    'software_control': tag['software_control'],
                    'alias':
                    False if tag['software_control'] & BASE_TAG_BIT else True,
                    'external_access': tag['external_access'],
                    'dimensions': tag['dimensions']
                }

                if tag['symbol_type'] & 0b_1000_0000_0000_0000:  # bit 15, 1 = struct, 0 = atomic
                    template_instance_id = tag[
                        'symbol_type'] & 0b_0000_1111_1111_1111
                    new_tag['tag_type'] = 'struct'
                    new_tag['template_instance_id'] = template_instance_id
                else:
                    new_tag['tag_type'] = 'atomic'
                    datatype = tag['symbol_type'] & 0b_0000_0000_1111_1111
                    new_tag['data_type'] = DATA_TYPE[datatype]
                    if datatype == DATA_TYPE['BOOL']:
                        new_tag['bit_position'] = (
                            tag['symbol_type'] & 0b_0000_0111_0000_0000) >> 8

                user_tags.append(new_tag)

            return user_tags
        except Exception as e:
            raise DataError(e)

    def _get_structure_makeup(self, instance_id):
        """
        get the structure makeup for a specific structure
        """
        if instance_id not in self._cache['id:struct']:
            if not self._target_is_connected:
                if not self.forward_open():
                    self.__log.warning(
                        "Target did not connected. get_tag_list will not be executed."
                    )
                    raise DataError(
                        "Target did not connected. get_tag_list will not be executed."
                    )
            request = self.new_request('send_unit_data')
            request.add(
                bytes([TAG_SERVICES_REQUEST['Get Attributes']]),
                b'\x03',  # Request Path ( 20 6B 25 00 Instance )
                CLASS_ID["8-bit"],  # Class id = 20 from spec 0x20
                CLASS_CODE[
                    "Template Object"],  # Logical segment: Template Object 0x6C
                INSTANCE_ID[
                    "16-bit"],  # Instance Segment: 16 Bit instance 0x25
                b'\x00',
                pack_uint(instance_id),
                b'\x04\x00',  # Number of attributes
                b'\x04\x00',  # Template Object Definition Size UDINT
                b'\x05\x00',  # Template Structure Size UDINT
                b'\x02\x00',  # Template Member Count UINT
                b'\x01\x00',  # Structure Handle We can use this to read and write UINT
            )

            response = request.send()
            if not response:
                raise DataError(f"send_unit_data returned not valid data",
                                response.error)
            _struct = self._parse_structure_makeup_attributes(response)
            self._cache['id:struct'][instance_id] = _struct
            self._cache['handle:id'][_struct['structure_handle']] = instance_id

        return self._cache['id:struct'][instance_id]

    @staticmethod
    def _parse_structure_makeup_attributes(response):
        """ extract the tags list from the message received"""
        structure = {}

        if response.service_status != SUCCESS:
            structure['Error'] = response.service_status
            return

        attribute = response.data
        idx = 4
        try:
            if unpack_uint(attribute[idx:idx + 2]) == SUCCESS:
                idx += 2
                structure['object_definition_size'] = unpack_dint(
                    attribute[idx:idx + 4])
            else:
                structure['Error'] = 'object_definition Error'
                return structure

            idx += 6
            if unpack_uint(attribute[idx:idx + 2]) == SUCCESS:
                idx += 2
                structure['structure_size'] = unpack_dint(attribute[idx:idx +
                                                                    4])
            else:
                structure['Error'] = 'structure Error'
                return structure

            idx += 6
            if unpack_uint(attribute[idx:idx + 2]) == SUCCESS:
                idx += 2
                structure['member_count'] = unpack_uint(attribute[idx:idx + 2])
            else:
                structure['Error'] = 'member_count Error'
                return structure

            idx += 4
            if unpack_uint(attribute[idx:idx + 2]) == SUCCESS:
                idx += 2
                structure['structure_handle'] = unpack_uint(attribute[idx:idx +
                                                                      2])
            else:
                structure['Error'] = 'structure_handle Error'
                return structure

            return structure

        except Exception as e:
            raise DataError(e)

    def _read_template(self, instance_id, object_definition_size):
        """ get a list of the tags in the plc

        """

        offset = 0
        template_raw = b''
        try:
            while True:
                request = self.new_request('send_unit_data')
                request.add(
                    bytes([TAG_SERVICES_REQUEST['Read Tag']]),
                    b'\x03',  # Request Path ( 20 6B 25 00 Instance )
                    CLASS_ID["8-bit"],  # Class id = 20 from spec
                    CLASS_CODE[
                        "Template Object"],  # Logical segment: Template Object 0x6C
                    INSTANCE_ID[
                        "16-bit"],  # Instance Segment: 16 Bit instance 0x25
                    b'\x00',
                    pack_uint(instance_id),
                    pack_dint(offset),  # Offset
                    pack_uint(((object_definition_size * 4) - 21) - offset))
                response = request.send()

                if response.service_status not in (SUCCESS,
                                                   INSUFFICIENT_PACKETS):
                    raise DataError('Error reading template', response)

                template_raw += response.data

                if response.service_status == SUCCESS:
                    break

                offset += len(response.data)

        except Exception:
            raise
        else:
            return template_raw

    def _parse_template_data(self, data, member_count):
        info_len = member_count * TEMPLATE_MEMBER_INFO_LEN
        info_data = data[:info_len]
        member_data = [
            self._parse_template_data_member_info(info)
            for info in (info_data[i:i + TEMPLATE_MEMBER_INFO_LEN]
                         for i in range(0, info_len, TEMPLATE_MEMBER_INFO_LEN))
        ]
        member_names = []
        template_name = None
        try:
            for name in (x.decode(errors='replace')
                         for x in data[info_len:].split(b'\x00') if len(x)):
                if template_name is None and ';' in name:
                    template_name, _ = name.split(';', maxsplit=1)
                else:
                    member_names.append(name)
        except (ValueError, UnicodeDecodeError):
            raise DataError(f'Unable to decode template or member names')

        predefine = template_name is None
        if predefine:
            template_name = member_names.pop(0)

        if template_name == 'ASCIISTRING82':  # internal name for STRING builtin type
            template_name = 'STRING'

        template = {
            'name':
            template_name,  # predefined types put name as first member (DWORD)
            'internal_tags': {},
            'attributes': []
        }

        for member, info in zip(member_names, member_data):
            if not member.startswith('ZZZZZZZZZZ') and not member.startswith(
                    '__'):
                template['attributes'].append(member)
            template['internal_tags'][member] = info

        if template['attributes'] == ['LEN', 'DATA'] and \
                template['internal_tags']['DATA']['data_type'] == 'SINT' and \
                template['internal_tags']['DATA'].get('array'):
            template['string'] = template['internal_tags']['DATA']['array']

        return template

    def _parse_template_data_member_info(self, info):
        type_info = unpack_uint(info[:2])
        typ = unpack_uint(info[2:4])
        member = {'offset': unpack_udint(info[4:])}
        tag_type = 'atomic'
        if typ in DATA_TYPE:
            data_type = DATA_TYPE[typ]
        else:
            instance_id = typ & 0b0000_1111_1111_1111
            if instance_id in DATA_TYPE:
                data_type = DATA_TYPE[instance_id]
            else:
                tag_type = 'struct'
                data_type = self._get_data_type(instance_id)

        member['tag_type'] = tag_type
        member['data_type'] = data_type

        if data_type == 'BOOL':
            member['bit'] = type_info
        elif data_type is not None:
            member['array'] = type_info

        return member

    def _get_data_type(self, instance_id):
        if instance_id not in self._cache['id:udt']:
            try:
                template = self._get_structure_makeup(
                    instance_id)  # instance id from type
                if not template.get('Error'):
                    _data = self._read_template(
                        instance_id, template['object_definition_size'])
                    data_type = self._parse_template_data(
                        _data, template['member_count'])
                    data_type['template'] = template
                    self._cache['id:udt'][instance_id] = data_type
                    self._data_types[data_type['name']] = data_type
            except Exception:
                self.__log.exception('fuck')

        return self._cache['id:udt'][instance_id]

    @with_forward_open
    def read(self, *tags: str) -> Union[Tag, List[Tag]]:
        """

        :param tags: one or many tags to read
        :return: one or many ``Tag`` objects
        """

        parsed_requests = self._parse_requested_tags(tags)
        requests = self._read__build_requests(parsed_requests)
        read_results = self._send_requests(requests)

        results = []

        for tag in tags:
            try:
                request_data = parsed_requests[tag]
                result = read_results[(request_data['plc_tag'],
                                       request_data['elements'])]
                if request_data.get('bit') is None:
                    results.append(result)
                else:
                    if result:
                        typ, bit = request_data['bit']
                        if typ == 'bit':
                            val = bool(result.value & (1 << bit))
                        else:
                            val = result.value[bit % 32]
                        results.append(Tag(tag, val, 'BOOL'))
                    else:
                        results.append(Tag(tag, None, None, result.error))
            except Exception as err:
                results.append(
                    Tag(tag, None, None, f'Invalid tag request - {err}'))

        if len(tags) > 1:
            return results
        else:
            return results[0]

    def _read__build_requests(self, parsed_tags):
        requests = []
        response_size = 0
        current_request = self.new_request('multi_request')
        requests.append(current_request)
        tags_in_requests = set()
        for tag, tag_data in list(parsed_tags.items()):
            if tag_data.get('error') is None and (
                    tag_data['plc_tag'],
                    tag_data['elements']) not in tags_in_requests:
                tags_in_requests.add(
                    (tag_data['plc_tag'], tag_data['elements']))
                return_size = _tag_return_size(
                    tag_data['tag_info']) * tag_data['elements']
                if return_size > self.connection_size:
                    _request = self.new_request('read_tag_fragmented')
                    _request.add(tag_data['plc_tag'], tag_data['elements'],
                                 tag_data['tag_info'])
                    requests.append(_request)
                else:
                    try:
                        if response_size + return_size < self.connection_size:
                            if current_request.add_read(
                                    tag_data['plc_tag'], tag_data['elements'],
                                    tag_data['tag_info']):
                                response_size += return_size
                            else:
                                response_size = return_size
                                current_request = self.new_request(
                                    'multi_request')
                                current_request.add_read(
                                    tag_data['plc_tag'], tag_data['elements'],
                                    tag_data['tag_info'])
                                requests.append(current_request)
                        else:
                            response_size = return_size
                            current_request = self.new_request('multi_request')
                            current_request.add_read(tag_data['plc_tag'],
                                                     tag_data['elements'],
                                                     tag_data['tag_info'])
                            requests.append(current_request)
                    except RequestError:
                        self.__log.exception(
                            f'Failed to build request for {tag} - skipping')
                        continue

        return requests

    @with_forward_open
    def write(
        self, *tags_values: Sequence[Tuple[str, Union[int, float, str, bool]]]
    ) -> Union[Tag, List[Tag]]:
        tags = (tag for (tag, value) in tags_values)
        parsed_requests = self._parse_requested_tags(tags)

        normal_tags = set()
        bit_tags = set()

        for tag, value in tags_values:
            parsed_requests[tag]['value'] = value

            if parsed_requests[tag].get('bit') is None:
                normal_tags.add(tag)
            else:
                bit_tags.add(tag)

        requests, bit_writes = self._write__build_requests(parsed_requests)
        write_results = self._send_requests(requests)
        results = []
        for tag, value in tags_values:
            try:
                request_data = parsed_requests[tag]
                bit = parsed_requests[tag].get('bit')
                result = write_results[(request_data['plc_tag'],
                                        request_data['elements'])]

                if request_data['elements'] > 1:
                    result = result._replace(
                        type=f'{result.type}[{request_data["elements"]}]')
                if bit is not None:
                    result = result._replace(tag=tag, type='BOOL', value=value)
                else:
                    result = result._replace(tag=request_data['plc_tag'],
                                             value=value)
                results.append(result)
            except Exception as err:
                results.append(
                    Tag(tag, None, None, f'Invalid tag request - {err}'))

        if len(tags_values) > 1:
            return results
        else:
            return results[0]

    def _write__build_requests(self, parsed_tags):
        requests = []
        current_request = self.new_request('multi_request')
        requests.append(current_request)
        bit_writes = {}

        tags_in_requests = set()
        for tag, tag_data in list(parsed_tags.items()):
            if tag_data.get('error') is None and (
                    tag_data['plc_tag'],
                    tag_data['elements']) not in tags_in_requests:
                tags_in_requests.add(
                    (tag_data['plc_tag'], tag_data['elements']))

                string = _make_string_bytes(tag_data)
                if string is not None:
                    tag_data['value'] = string

                if _bit_request(tag_data, bit_writes):
                    continue

                tag_data['write_value'] = writable_value(
                    tag_data['value'], tag_data['elements'],
                    tag_data['tag_info']['data_type'])

                if len(tag_data['write_value']) > self.connection_size:
                    _request = self.new_request('write_tag_fragmented')
                    _request.add(tag_data['plc_tag'], tag_data['value'],
                                 tag_data['elements'], tag_data['tag_info'])
                    requests.append(_request)
                    continue

                try:
                    if not current_request.add_write(
                            tag_data['plc_tag'], tag_data['write_value'],
                            tag_data['elements'], tag_data['tag_info']):
                        current_request = self.new_request('multi_request')
                        requests.append(current_request)
                        current_request.add_write(tag_data['plc_tag'],
                                                  tag_data['write_value'],
                                                  tag_data['elements'],
                                                  tag_data['tag_info'])

                except RequestError:
                    self.__log.exception(
                        f'Failed to build request for {tag} - skipping')
                    continue

        if bit_writes:
            for tag in bit_writes:
                try:
                    value = bit_writes[tag]['or_mask'], bit_writes[tag][
                        'and_mask']
                    if not current_request.add_write(
                            tag,
                            value,
                            tag_info=bit_writes[tag]['tag_info'],
                            bits_write=True):
                        current_request = self.new_request('multi_request')
                        requests.append(current_request)
                        current_request.add_write(
                            tag,
                            value,
                            tag_info=bit_writes[tag]['tag_info'],
                            bits_write=True)
                except RequestError:
                    self.__log.exception(
                        f'Failed to build request for {tag} - skipping')
                    continue
        return requests, bit_writes

    def _get_tag_info(self, base, attrs):
        def _recurse_attrs(attrs, data):
            cur, *remain = attrs
            if not len(remain):
                return data[_strip_array(cur)]
            else:
                return _recurse_attrs(
                    remain,
                    data[_strip_array(cur)]['data_type']['internal_tags'])

        try:
            data = self._tags[_strip_array(base)]
            if not len(attrs):
                return data
            else:
                return _recurse_attrs(attrs,
                                      data['data_type']['internal_tags'])

        except Exception as err:
            self.__log.exception(
                f'Failed to lookup tag data for {base}, {attrs}')
            raise

    def _parse_requested_tags(self, tags):
        requests = {}
        for tag in tags:
            parsed = {}
            try:
                plc_tag, bit, elements, tag_info = self._parse_tag_request(tag)
            except RequestError as err:
                parsed['error'] = str(err)
            else:
                parsed['plc_tag'] = plc_tag
                parsed['bit'] = bit
                parsed['elements'] = elements
                parsed['tag_info'] = tag_info
            finally:
                requests[tag] = parsed
        return requests

    def _parse_tag_request(self, tag: str):
        try:
            if tag.endswith('}') and '{' in tag:
                tag, _tmp = tag.split('{')
                elements = int(_tmp[:-1])
            else:
                elements = 1

            bit = None

            base, *attrs = tag.split('.')
            if len(attrs) and attrs[-1].isdigit():
                _bit = attrs.pop(-1)
                bit = ('bit', int(_bit))
                if not len(attrs):
                    tag = base
                else:
                    tag = f"{base}.{''.join(attrs)}"

            tag_info = self._get_tag_info(base, attrs)

            if tag_info['data_type'] == 'DWORD' and elements == 1:
                _tag, idx = _get_array_index(tag)
                tag = f'{_tag}[{idx // 32}]'
                bit = ('bool_array', idx)
                elements = 1

            return tag, bit, elements, tag_info

        except Exception as err:
            # something went wrong parsing the tag path
            raise RequestError('Failed to parse tag read request', tag)

    @staticmethod
    def _send_requests(requests):
        def _mkkey(t=None, r=None):
            if t is not None:
                return t['tag'], t['elements']
            else:
                return r.tag, r.elements

        results = {}

        for request in requests:
            try:
                response = request.send()
            except Exception as err:
                if request.type_ != 'multi':
                    results[_mkkey(r=request)] = Tag(request.tag, None, None,
                                                     str(err))
                else:
                    for tag in request.tags:
                        results[_mkkey(t=tag)] = Tag(tag['tag'], None, None,
                                                     str(err))
            else:
                if request.type_ != 'multi':
                    if response:
                        results[_mkkey(r=request)] = Tag(
                            request.tag, response.value
                            if request.type_ == 'read' else request.value,
                            response.data_type
                            if request.type_ == 'read' else request.data_type)
                    else:
                        results[_mkkey(r=request)] = Tag(
                            request.tag, None, None, response.error)
                else:
                    for tag in response.tags:
                        if tag['service_status'] == SUCCESS:
                            results[_mkkey(t=tag)] = Tag(
                                tag['tag'], tag['value'], tag['data_type'])
                        else:
                            results[_mkkey(t=tag)] = Tag(
                                tag['tag'], None, None,
                                tag.get('error', 'Unknown Service Error'))
        return results

    # --------------------------------------------------------------
    #  OLD CODE - to be removed
    #
    # --------------------------------------------------------------

    def create_tag_rp(self, tag):
        """ Creates a request pad

        It returns the request packed wrapped around the tag passed.
        If any error it returns none
        """
        tags = tag.split('.')
        if tags:
            base, *attrs = tags

            if self.use_instance_ids and base in self.tags:
                rp = [
                    CLASS_ID['8-bit'], CLASS_CODE['Symbol Object'],
                    INSTANCE_ID['16-bit'], b'\x00',
                    pack_uint(self.tags[base]['instance_id'])
                ]
            else:
                base_tag, index = self._find_tag_index(base)
                base_len = len(base_tag)
                rp = [EXTENDED_SYMBOL, pack_usint(base_len), base_tag]
                if base_len % 2:
                    rp.append(PADDING_BYTE)
                if index is None:
                    return None
                else:
                    rp += index

            for attr in attrs:
                attr, index = self._find_tag_index(attr)
                tag_length = len(attr)
                # Create the request path
                attr_path = [EXTENDED_SYMBOL, pack_usint(tag_length), attr]
                # Add pad byte because total length of Request path must be word-aligned
                if tag_length % 2:
                    attr_path.append(PADDING_BYTE)
                # Add any index
                if index is None:
                    return None
                else:
                    attr_path += index
                rp += attr_path

            # At this point the Request Path is completed,
            request_path = b''.join(rp)
            request_path = bytes([len(request_path) // 2]) + request_path

            return request_path

        return None

    def _find_tag_index(self, tag):
        if '[' in tag:  # Check if is an array tag
            t = tag[:len(tag) - 1]  # Remove the last square bracket
            inside_value = t[t.find('[') +
                             1:]  # Isolate the value inside bracket
            index = inside_value.split(
                ','
            )  # Now split the inside value in case part of multidimensional array
            tag = t[:t.find('[')]  # Get only the tag part
        else:
            index = []
        return tag.encode(), self._encode_tag_index(index)

    @staticmethod
    def _encode_tag_index(index):
        path = []
        for idx in index:
            val = int(idx)
            if val <= 0xff:
                path += [ELEMENT_ID["8-bit"], pack_usint(val)]
            elif val <= 0xffff:
                path += [ELEMENT_ID["16-bit"], PADDING_BYTE, pack_uint(val)]
            elif val <= 0xfffffffff:
                path += [ELEMENT_ID["32-bit"], PADDING_BYTE, pack_dint(val)]
            else:
                return None  # Cannot create a valid request packet
        return path

    def _check_reply(self, reply):
        """ check the replayed message for error

            return the status error if unsuccessful, else None
        """
        try:
            if reply is None:
                return f'{REPLY_INFO[unpack_dint(reply[:2])]} without reply'
            # Get the type of command
            typ = unpack_uint(reply[:2])

            # Encapsulation status check
            if unpack_dint(reply[8:12]) != SUCCESS:
                return get_service_status(unpack_dint(reply[8:12]))

            # Command Specific Status check
            if typ == unpack_uint(ENCAPSULATION_COMMAND["send_rr_data"]):
                status = unpack_usint(reply[42:43])
                if status != SUCCESS:
                    return f"send_rr_data reply:{get_service_status(status)} - " \
                           f"Extend status:{get_extended_status(reply, 42)}"
                else:
                    return None
            elif typ == unpack_uint(ENCAPSULATION_COMMAND["send_unit_data"]):
                service = reply[46]
                status = _unit_data_status(reply)
                # return None
                if status == INSUFFICIENT_PACKETS and service in (
                        TAG_SERVICES_REPLY['Read Tag'],
                        TAG_SERVICES_REPLY['Multiple Service Packet'],
                        TAG_SERVICES_REPLY['Read Tag Fragmented'],
                        TAG_SERVICES_REPLY['Write Tag Fragmented'],
                        TAG_SERVICES_REPLY['Get Instance Attributes List'],
                        TAG_SERVICES_REPLY['Get Attributes']):
                    return None
                if status == SUCCESS:
                    return None

                return f"send_unit_data reply:{get_service_status(status)} - " \
                       f"Extend status:{get_extended_status(reply, 48)}"

        except Exception as e:
            raise DataError(e)

    def read_tag(self, *tags):
        """ read tag from a connected plc

        Possible combination can be passed to this method:
                - ('Counts') a single tag name
                - (['ControlWord']) a list with one tag or many
                - (['parts', 'ControlWord', 'Counts'])

        At the moment there is not a strong validation for the argument passed. The user should verify
        the correctness of the format passed.

        :return: None is returned in case of error otherwise the tag list is returned
        """

        if not self.forward_open():
            self.__log.warning(
                "Target did not connected. read_tag will not be executed.")
            raise DataError(
                "Target did not connected. read_tag will not be executed.")

        if len(tags) == 1:
            if isinstance(tags[0], (list, tuple, GeneratorType)):
                return self._read_tag_multi(tags[0])
            else:
                return self._read_tag_single(tags[0])
        else:
            return self._read_tag_multi(tags)

    def _read_tag_multi(self, tags):
        tag_bits = defaultdict(list)
        rp_list, tags_read = [[]], [[]]
        request_len = 0
        for tag in tags:
            tag, bit = self._prep_bools(tag, 'BOOL', bits_only=True)
            read = bit is None or tag not in tag_bits
            if bit is not None:
                tag_bits[tag].append(bit)
            if read:
                rp = self.create_tag_rp(tag)
                if rp is None:
                    raise DataError(
                        f"Cannot create tag {tag} request packet. read_tag will not be executed."
                    )
                else:
                    tag_req_len = len(rp) + MULTISERVICE_READ_OVERHEAD
                    if tag_req_len + request_len >= self.connection_size:
                        rp_list.append([])
                        tags_read.append([])
                        request_len = 0
                    rp_list[-1].append(
                        bytes([TAG_SERVICES_REQUEST['Read Tag']]) + rp +
                        b'\x01\x00')
                    tags_read[-1].append(tag)
                    request_len += tag_req_len

        replies = []
        for req_list, tags_ in zip(rp_list, tags_read):
            message_request = self.build_multiple_service(
                req_list, self._get_sequence())
            msg = self.build_common_packet_format(
                DATA_ITEM['Connected'],
                b''.join(message_request),
                ADDRESS_ITEM['Connection Based'],
                addr_data=self._target_cid,
            )
            print(msg)
            success, reply = self.send_unit_data(msg)
            if not success:
                raise DataError(
                    f"send_unit_data returned not valid data - {reply}")

            replies += self._parse_multiple_request_read(
                reply, tags_, tag_bits)
        return replies

    def _read_tag_single(self, tag):
        tag, bit = self._prep_bools(tag, 'BOOL', bits_only=True)
        rp = self.create_tag_rp(tag)
        if rp is None:
            self.__log.warning(
                f"Cannot create tag {tag} request packet. read_tag will not be executed."
            )
            return None
        else:
            # Creating the Message Request Packet
            message_request = [
                pack_uint(self._get_sequence()),
                bytes([TAG_SERVICES_REQUEST['Read Tag']
                       ]),  # the Request Service
                # bytes([len(rp) // 2]),  # the Request Path Size length in word
                rp,  # the request path
                b'\x01\x00',
            ]
        request = self.build_common_packet_format(
            DATA_ITEM['Connected'],
            b''.join(message_request),
            ADDRESS_ITEM['Connection Based'],
            addr_data=self._target_cid,
        )
        success, reply = self.send_unit_data(request)

        if success:
            data_type = unpack_uint(reply[50:52])
            typ = DATA_TYPE[data_type]
            try:
                value = UNPACK_DATA_FUNCTION[typ](reply[52:])
                if bit is not None:
                    value = bool(value & (
                        1 << bit)) if bit < BITS_PER_INT_TYPE[typ] else None
                return Tag(tag, value, typ)
            except Exception as e:
                raise DataError(e)
        else:
            return Tag(tag, None, None, reply)

    @staticmethod
    def _parse_multiple_request_read(reply, tags, tag_bits=None):
        """ parse the message received from a multi request read:

        For each tag parsed, the information extracted includes the tag name, the value read and the data type.
        Those information are appended to the tag list as tuple

        :return: the tag list
        """
        offset = 50
        position = 50
        tag_bits = tag_bits or {}
        try:
            number_of_service_replies = unpack_uint(reply[offset:offset + 2])
            tag_list = []
            for index in range(number_of_service_replies):
                position += 2
                start = offset + unpack_uint(reply[position:position + 2])
                general_status = unpack_usint(reply[start + 2:start + 3])
                tag = tags[index]
                if general_status == SUCCESS:
                    typ = DATA_TYPE[unpack_uint(reply[start + 4:start + 6])]
                    value_begin = start + 6
                    value_end = value_begin + DATA_FUNCTION_SIZE[typ]
                    value = UNPACK_DATA_FUNCTION[typ](
                        reply[value_begin:value_end])
                    if tag in tag_bits:
                        for bit in tag_bits[tag]:
                            val = bool(
                                value & (1 << bit)
                            ) if bit < BITS_PER_INT_TYPE[typ] else None
                            tag_list.append(Tag(f'{tag}.{bit}', val, 'BOOL'))
                    else:
                        tag_list.append(Tag(tag, value, typ))
                else:
                    tag_list.append(
                        Tag(tag, None, None,
                            get_service_status(general_status)))

            return tag_list
        except Exception as e:
            raise DataError(e)

    def read_array(self, tag, counts, raw=False):
        """ read array of atomic data type from a connected plc

        At the moment there is not a strong validation for the argument passed. The user should verify
        the correctness of the format passed.

        :param tag: the name of the tag to read
        :param counts: the number of element to read
        :param raw: the value should output as raw-value (hex)
        :return: None is returned in case of error otherwise the tag list is returned
        """

        if not self._target_is_connected:
            if not self.forward_open():
                self.__log.warning(
                    "Target did not connected. read_tag will not be executed.")
                raise DataError(
                    "Target did not connected. read_tag will not be executed.")

        offset = 0
        last_idx = 0
        tags = b'' if raw else []

        while offset != -1:
            rp = self.create_tag_rp(tag)
            if rp is None:
                self.__log.warning(
                    f"Cannot create tag {tag} request packet. read_tag will not be executed."
                )
                return None
            else:
                # Creating the Message Request Packet
                message_request = [
                    pack_uint(self._get_sequence()),
                    bytes([TAG_SERVICES_REQUEST["Read Tag Fragmented"]
                           ]),  # the Request Service
                    # bytes([len(rp) // 2]),  # the Request Path Size length in word
                    rp,  # the request path
                    pack_uint(counts),
                    pack_dint(offset)
                ]
            msg = self.build_common_packet_format(
                DATA_ITEM['Connected'],
                b''.join(message_request),
                ADDRESS_ITEM['Connection Based'],
                addr_data=self._target_cid,
            )
            success, reply = self.send_unit_data(msg)
            if not success:
                raise DataError(
                    f"send_unit_data returned not valid data - {reply}")

            last_idx, offset = self._parse_fragment(reply, last_idx, offset,
                                                    tags, raw)

        return tags

    def _parse_fragment(self, reply, last_idx, offset, tags, raw=False):
        """ parse the fragment returned by a fragment service."""

        try:
            status = _unit_data_status(reply)
            data_type = unpack_uint(reply[REPLY_START:REPLY_START + 2])
            fragment_returned = reply[REPLY_START + 2:]
        except Exception as e:
            raise DataError(e)

        fragment_returned_length = len(fragment_returned)
        idx = 0
        while idx < fragment_returned_length:
            try:
                typ = DATA_TYPE[data_type]
                if raw:
                    value = fragment_returned[idx:idx +
                                              DATA_FUNCTION_SIZE[typ]]
                else:
                    value = UNPACK_DATA_FUNCTION[typ](
                        fragment_returned[idx:idx + DATA_FUNCTION_SIZE[typ]])
                idx += DATA_FUNCTION_SIZE[typ]
            except Exception as e:
                raise DataError(e)
            if raw:
                tags += value
            else:
                tags.append((last_idx, value))
                last_idx += 1

        if status == SUCCESS:
            offset = -1
        elif status == 0x06:
            offset += fragment_returned_length
        else:
            self.__log.warning('{0}: {1}'.format(
                get_service_status(status), get_extended_status(reply, 48)))
            offset = -1

        return last_idx, offset

    @staticmethod
    def _prep_bools(tag, typ, bits_only=True):
        """
        if tag is a bool and a bit of an integer, returns the base tag and the bit value,
        else returns the tag name and None

        """
        if typ != 'BOOL':
            return tag, None
        if not bits_only and tag.endswith(']'):
            try:
                base, idx = tag[:-1].rsplit(sep='[', maxsplit=1)
                idx = int(idx)
                base = f'{base}[{idx // 32}]'
                return base, idx
            except Exception:
                return tag, None
        else:
            try:
                base, bit = tag.rsplit('.', maxsplit=1)
                bit = int(bit)
                return base, bit
            except Exception:
                return tag, None

    @staticmethod
    def _dword_to_boolarray(tag, bit):
        base, tmp = tag.rsplit(sep='[', maxsplit=1)
        i = int(tmp[:-1])
        return f'{base}[{(i * 32) + bit}]'

    def _write_tag_multi_write(self, tags):
        rp_list = [[]]
        tags_added = [[]]
        request_len = 0
        for name, value, typ in tags:
            name, bit = self._prep_bools(
                name, typ,
                bits_only=False)  # check if bool & if bit of int or bool array
            # Create the request path to wrap the tag name
            rp = self.create_tag_rp(name, multi_requests=True)
            if rp is None:
                self.__log.warning(
                    f"Cannot create tag {tags} req. packet. write_tag will not be executed"
                )
                return None
            else:
                try:
                    if bit is not None:  # then it is a boolean array
                        rp = self.create_tag_rp(name, multi_requests=True)
                        request = bytes([
                            TAG_SERVICES_REQUEST["Read Modify Write Tag"]
                        ]) + rp
                        request += b''.join(
                            self._make_write_bit_data(bit,
                                                      value,
                                                      bool_ary='[' in name))
                        if typ == 'BOOL' and name.endswith(']'):
                            name = self._dword_to_boolarray(name, bit)
                        else:
                            name = f'{name}.{bit}'
                    else:
                        request = (bytes([TAG_SERVICES_REQUEST["Write Tag"]]) +
                                   rp + pack_uint(DATA_TYPE[typ]) +
                                   b'\x01\x00' +
                                   PACK_DATA_FUNCTION[typ](value))

                    tag_req_len = len(request) + MULTISERVICE_WRITE_OVERHEAD
                    if tag_req_len + request_len >= self.connection_size:
                        rp_list.append([])
                        tags_added.append([])
                        request_len = 0
                    rp_list[-1].append(request)
                    request_len += tag_req_len
                except (LookupError, struct.error) as e:
                    self.__warning(
                        f"Tag:{name} type:{typ} removed from write list. Error:{e}."
                    )

                    # The tag in idx position need to be removed from the rp list because has some kind of error
                else:
                    tags_added[-1].append((name, value, typ))

        # Create the message request
        replies = []
        for req_list, tags_ in zip(rp_list, tags_added):
            message_request = self.build_multiple_service(
                req_list, self._get_sequence())
            msg = self.build_common_packet_format(
                DATA_ITEM['Connected'],
                b''.join(message_request),
                ADDRESS_ITEM['Connection Based'],
                addr_data=self._target_cid,
            )
            success, reply = self.send_unit_data(msg)
            if success:
                replies += self._parse_multiple_request_write(tags_, reply)
            else:
                raise DataError(
                    f"send_unit_data returned not valid data - {reply}")
        return replies

    def _write_tag_single_write(self, tag, value, typ):
        name, bit = self._prep_bools(
            tag, typ, bits_only=False
        )  # check if we're writing a bit of a integer rather than a BOOL

        rp = self.create_tag_rp(name)
        if rp is None:
            self.__log.warning(
                f"Cannot create tag {tag} request packet. write_tag will not be executed."
            )
            return None
        else:
            # Creating the Message Request Packet
            message_request = [
                pack_uint(self._get_sequence()),
                bytes([
                    TAG_SERVICES_REQUEST["Read Modify Write Tag"]
                    if bit is not None else TAG_SERVICES_REQUEST["Write Tag"]
                ]),
                # bytes([len(rp) // 2]),  # the Request Path Size length in word
                rp,  # the request path
            ]
            if bit is not None:
                try:
                    message_request += self._make_write_bit_data(
                        bit, value, bool_ary='[' in name)
                except Exception as err:
                    raise DataError(
                        f'Unable to write bit, invalid bit number {repr(err)}')
            else:
                message_request += [
                    pack_uint(DATA_TYPE[typ]),  # data type
                    pack_uint(1),  # Add the number of tag to write
                    PACK_DATA_FUNCTION[typ](value)
                ]
            request = self.build_common_packet_format(
                DATA_ITEM['Connected'],
                b''.join(message_request),
                ADDRESS_ITEM['Connection Based'],
                addr_data=self._target_cid)
            success, reply = self.send_unit_data(request)
            return Tag(tag, value, typ, None if success else reply)

    @staticmethod
    def _make_write_bit_data(bit, value, bool_ary=False):
        or_mask, and_mask = 0x00000000, 0xFFFFFFFF

        if bool_ary:
            mask_size = 4
            bit = bit % 32
        else:
            mask_size = 1 if bit < 8 else 2 if bit < 16 else 4

        if value:
            or_mask |= (1 << bit)
        else:
            and_mask &= ~(1 << bit)

        return [
            pack_uint(mask_size),
            pack_udint(or_mask)[:mask_size],
            pack_udint(and_mask)[:mask_size]
        ]

    @staticmethod
    def _parse_multiple_request_write(tags, reply):
        """ parse the message received from a multi request writ:

        For each tag parsed, the information extracted includes the tag name and the status of the writing.
        Those information are appended to the tag list as tuple

        :return: the tag list
        """
        offset = 50
        position = 50

        try:
            number_of_service_replies = unpack_uint(reply[offset:offset + 2])
            tag_list = []
            for index in range(number_of_service_replies):
                position += 2
                start = offset + unpack_uint(reply[position:position + 2])
                general_status = unpack_usint(reply[start + 2:start + 3])
                error = None if general_status == SUCCESS else get_service_status(
                    general_status)
                tag_list.append(Tag(*tags[index], error))
            return tag_list
        except Exception as e:
            raise DataError(e)

    def write_tag(self, tag, value=None, typ=None):
        """ write tag/tags from a connected plc

        Possible combination can be passed to this method:
                - ('tag name', Value, data type)  as single parameters or inside a tuple
                - ([('tag name', Value, data type), ('tag name2', Value, data type)]) as array of tuples

        At the moment there is not a strong validation for the argument passed. The user should verify
        the correctness of the format passed.

        The type accepted are:
            - BOOL
            - SINT
            - INT
            - DINT
            - REAL
            - LINT
            - BYTE
            - WORD
            - DWORD
            - LWORD

        :param tag: tag name, or an array of tuple containing (tag name, value, data type)
        :param value: the value to write or none if tag is an array of tuple or a tuple
        :param typ: the type of the tag to write or none if tag is an array of tuple or a tuple
        :return: None is returned in case of error otherwise the tag list is returned
        """

        if not self._target_is_connected:
            if not self.forward_open():
                self.__log.warning(
                    "Target did not connected. write_tag will not be executed."
                )
                raise DataError(
                    "Target did not connected. write_tag will not be executed."
                )

        if isinstance(tag, (list, tuple, GeneratorType)):
            return self._write_tag_multi_write(tag)
        else:
            if isinstance(tag, tuple):
                name, value, typ = tag
            else:
                name = tag
            return self._write_tag_single_write(name, value, typ)

    def write_array(self, tag, values, data_type, raw=False):
        """ write array of atomic data type from a connected plc
        At the moment there is not a strong validation for the argument passed. The user should verify
        the correctness of the format passed.
        :param tag: the name of the tag to read
        :param data_type: the type of tag to write
        :param values: the array of values to write, if raw: the frame with bytes
        :param raw: indicates that the values are given as raw values (hex)
        """

        if not isinstance(values, list):
            self.__log.warning("A list of tags must be passed to write_array.")
            raise DataError("A list of tags must be passed to write_array.")

        if not self._target_is_connected:
            if not self.forward_open():
                self.__log.warning(
                    "Target did not connected. write_array will not be executed."
                )
                raise DataError(
                    "Target did not connected. write_array will not be executed."
                )

        array_of_values = b''
        byte_size = 0
        byte_offset = 0

        for i, value in enumerate(values):
            array_of_values += value if raw else PACK_DATA_FUNCTION[data_type](
                value)
            byte_size += DATA_FUNCTION_SIZE[data_type]

            if byte_size >= 450 or i == len(values) - 1:
                # create the message and send the fragment
                rp = self.create_tag_rp(tag)
                if rp is None:
                    self.__log.warning(
                        f"Cannot create tag {tag} request packet write_array will not be executed."
                    )
                    return None
                else:
                    # Creating the Message Request Packet
                    message_request = [
                        pack_uint(self._get_sequence()),
                        bytes([TAG_SERVICES_REQUEST["Write Tag Fragmented"]
                               ]),  # the Request Service
                        bytes([len(rp) // 2
                               ]),  # the Request Path Size length in word
                        rp,  # the request path
                        pack_uint(DATA_TYPE[data_type]),  # Data type to write
                        pack_uint(len(values)),  # Number of elements to write
                        pack_dint(byte_offset),
                        array_of_values  # Fragment of elements to write
                    ]
                    byte_offset += byte_size

                msg = self.build_common_packet_format(
                    DATA_ITEM['Connected'],
                    b''.join(message_request),
                    ADDRESS_ITEM['Connection Based'],
                    addr_data=self._target_cid,
                )

                success, reply = self.send_unit_data(msg)
                if not success:
                    raise DataError(
                        f"send_unit_data returned not valid data - {reply}")

                array_of_values = b''
                byte_size = 0
        return True

    def write_string(self, tag, value, size=82):
        """
            Rockwell define different string size:
                STRING  STRING_12   STRING_16   STRING_20   STRING_40   STRING_8
            by default we assume size 82 (STRING)
        """
        data_tag = ".".join((tag, "DATA"))
        len_tag = ".".join((tag, "LEN"))

        # create an empty array
        data_to_send = [0] * size
        for idx, val in enumerate(value):
            try:
                unsigned = ord(val)
                data_to_send[
                    idx] = unsigned - 256 if unsigned > 127 else unsigned
            except IndexError:
                break

        str_len = len(value)
        if str_len > size:
            str_len = size

        result_len = self.write_tag(len_tag, str_len, 'DINT')
        result_data = self.write_array(data_tag, data_to_send, 'SINT')
        return result_data and result_len

    def read_string(self, tag, str_len=None):
        data_tag = f'{tag}.DATA'
        if str_len is None:
            len_tag = f'{tag}.LEN'
            tmp = self.read_tag(len_tag)
            length, _ = tmp or (None, None)
        else:
            length = str_len

        if length:
            values = self.read_array(data_tag, length)
            if values:
                _, values = list(zip(*values))
                chars = ''.join(
                    chr(v + 256) if v < 0 else chr(v) for v in values)
                string, *_ = chars.split('\x00', maxsplit=1)
                return string
        return None


def _unit_data_status(reply):
    return unpack_usint(reply[48:49])


def _parse_plc_name(response):
    if response.service_status != SUCCESS:
        raise DataError(
            f'get_plc_name returned status {get_service_status(response.error)}'
        )
    try:
        name_len = unpack_uint(response.data[6:8])
        name = response.data[8:8 + name_len].decode()
        return name
    except Exception as err:
        raise DataError(err)


def _parse_plc_info(data):
    vendor = unpack_uint(data[0:2])
    product_type = unpack_uint(data[2:4])
    product_code = unpack_uint(data[4:6])
    major_fw = int(data[6])
    minor_fw = int(data[7])
    keyswitch = KEYSWITCH.get(int(data[8]), {}).get(int(data[9]), 'UNKNOWN')
    serial_number = f'{unpack_udint(data[10:14]):0{8}x}'
    device_type_len = int(data[14])
    device_type = data[15:15 + device_type_len].decode()

    return {
        'vendor': VENDORS.get(vendor, 'UNKNOWN'),
        'product_type': PRODUCT_TYPES.get(product_type, 'UNKNOWN'),
        'product_code': product_code,
        'version_major': major_fw,
        'version_minor': minor_fw,
        'revision': f'{major_fw}.{minor_fw}',
        'serial': serial_number,
        'device_type': device_type,
        'keyswitch': keyswitch
    }


def writable_value(value, elements, data_type):
    if isinstance(value, bytes):
        return value

    try:
        pack_func = PACK_DATA_FUNCTION[data_type]
        if elements > 1:
            return b''.join(pack_func(value[i]) for i in range(elements))
        else:
            return pack_func(value)
    except Exception as err:
        raise RequestError('Unable to create a writable value', err)


def _strip_array(tag):
    if '[' in tag:
        return tag[:tag.find('[')]
    return tag


def _get_array_index(tag):
    if tag.endswith(']') and '[' in tag:
        tag, _tmp = tag.split('[')
        idx = int(_tmp[:-1])
    else:
        idx = 0

    return tag, idx


def _tag_return_size(tag_info):
    if tag_info['tag_type'] == 'atomic':
        size = DATA_TYPE_SIZE[tag_info['data_type']]
    else:
        size = tag_info['data_type']['template']['structure_size']

    return size


def _string_to_sint_array(string, string_len):
    sint_array = [b'\x00' for _ in range(string_len)]
    if len(string) > string_len:
        string = string[:string_len]

    for i, s in enumerate(string):
        unsigned = ord(s)
        sint_array[i] = pack_sint(unsigned -
                                  256 if unsigned > 127 else unsigned)

    return b''.join(sint_array)


def _make_string_bytes(tag_data):
    if tag_data['tag_info']['tag_type'] == 'struct':
        string_length = tag_data['tag_info']['data_type'].get('string')
    else:
        return None

    if tag_data['elements'] > 1:
        string_bytes = b''
        for val in tag_data['value']:
            str_data = _string_to_sint_array(val, string_length)
            str_bytes = pack_dint(len(val)) + str_data
            string_bytes += str_bytes + b'\x00' * (
                len(str_bytes) % 4)  # pad data to 4-byte boundaries
    else:
        str_data = _string_to_sint_array(tag_data['value'], string_length)
        string_bytes = pack_dint(len(tag_data['value'])) + str_data

    return string_bytes + b'\x00' * (len(string_bytes) % 4
                                     )  # pad data to 4-byte boundaries


def _bit_request(tag_data, bit_requests):
    if tag_data.get('bit') is None:
        return None

    if tag_data['plc_tag'] not in bit_requests:
        bit_requests[tag_data['plc_tag']] = {
            'and_mask': 0xFFFFFFFF,
            'or_mask': 0x00000000,
            'bits': [],
            'tag_info': tag_data['tag_info']
        }

    bits_ = bit_requests[tag_data['plc_tag']]
    typ_, bit = tag_data['bit']
    bits_['bits'].append(bit)

    if typ_ == 'bool_array':
        bit = bit % 32

    if tag_data['value']:
        bits_['or_mask'] |= (1 << bit)
    else:
        bits_['and_mask'] &= ~(1 << bit)

    return True
