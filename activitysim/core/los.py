# ActivitySim
# See full license in LICENSE.txt.
from builtins import range
from builtins import int

import sys
import os
import logging
import multiprocessing

from collections import OrderedDict
from functools import reduce
from operator import mul

import numpy as np
import pandas as pd
import openmatrix as omx

from activitysim.core import skim
from activitysim.core import inject
from activitysim.core import util
from activitysim.core import config

logger = logging.getLogger(__name__)

LOS_SETTINGS_NAME = 'network_los'

ONE_ZONE = 1
TWO_ZONE = 2
THREE_ZONE = 3

REQUIRED_TWO_ZONE_TABLES = ['maz_to_maz', 'maz_to_taz']
# REQIRED_TWO_ZONE_TABLES = []


def multiply_large_numbers(list_of_numbers):
    return reduce(mul, list_of_numbers)


def allocate_skim_buffer(skim_info, shared=False):
    skim_dtype = skim_info['dtype']
    omx_shape = skim_info['omx_shape']
    num_skims = skim_info['num_skims']
    skim_tag = skim_info['skim_tag']

    # buffer_size must be int, not np.int64
    buffer_size = int(multiply_large_numbers(omx_shape) * num_skims)

    itemsize = np.dtype(skim_dtype).itemsize
    csz = buffer_size * itemsize
    logger.info("allocating shared buffer %s for %s skims (skim size: %s * %s bytes = %s) total size: %s (%s)" %
                (skim_tag, num_skims, omx_shape, itemsize, buffer_size, csz, util.GB(csz)))

    if shared:
        if np.issubdtype(skim_dtype, np.float64):
            typecode = 'd'
        elif np.issubdtype(skim_dtype, np.float32):
            typecode = 'f'
        else:
            raise RuntimeError("allocate_skim_buffer unrecognized dtype %s" % skim_dtype)

        buffer = multiprocessing.RawArray(typecode, buffer_size)
    else:
        buffer = np.zeros(buffer_size, dtype=skim_dtype)

    return buffer


def skim_data_from_buffer(skim_info, skim_buffer):

    omx_shape = skim_info['omx_shape']
    skim_dtype = skim_info['dtype']
    num_skims = skim_info['num_skims']

    skims_shape = omx_shape + (num_skims,)

    assert len(skim_buffer) == int(multiply_large_numbers(skims_shape))
    skim_data = np.frombuffer(skim_buffer, dtype=skim_dtype).reshape(skims_shape)

    return skim_data


def default_skim_cache_dir():
    return inject.get_injectable('output_dir')


def build_skim_cache_file_name(skim_tag):
    return f"cached_{skim_tag}.mmap"


def read_skim_cache(skim_info, skim_data):
    """
        read cached memmapped skim data from canonically named cache file(s) in output directory into skim_data
    """

    skim_cache_dir = config.setting('skim_cache_dir', default_skim_cache_dir())
    logger.info(f"load_skims reading skims data from cache directory {skim_cache_dir}")

    skim_tag = skim_info['skim_tag']
    dtype = np.dtype(skim_info['dtype'])

    skim_cache_file_name = build_skim_cache_file_name(skim_tag)
    skim_cache_path = os.path.join(skim_cache_dir, skim_cache_file_name)

    assert os.path.isfile(skim_cache_path), \
        "read_skim_cache could not find skim_cache_path: %s" % (skim_cache_path,)

    logger.info(f"load_skims reading skim cache {skim_tag} {skim_data.shape} from {skim_cache_file_name}")

    data = np.memmap(skim_cache_path, shape=skim_data.shape, dtype=dtype, mode='r')
    assert data.shape == skim_data.shape

    skim_data[::] = data[::]


def write_skim_cache(skim_info, skim_data):
    """
        write skim data from skim_data to canonically named cache file(s) in output directory
    """

    skim_cache_dir = config.setting('skim_cache_dir', default_skim_cache_dir())
    logger.info(f"load_skims writing skims data to cache directory {skim_cache_dir}")

    skim_tag = skim_info['skim_tag']
    dtype = np.dtype(skim_info['dtype'])

    skim_cache_file_name = build_skim_cache_file_name(skim_tag)
    skim_cache_path = os.path.join(skim_cache_dir, skim_cache_file_name)

    logger.info(f"load_skims writing skim cache {skim_tag} {skim_data.shape} to {skim_cache_file_name}")

    data = np.memmap(skim_cache_path, shape=skim_data.shape, dtype=dtype, mode='w+')
    data[::] = skim_data


def read_skims_from_omx(skim_info, skim_data):
    """
    read skims from omx file into skim_data
    """

    block_offsets = skim_info['block_offsets']
    omx_keys = skim_info['omx_keys']
    omx_file_names = skim_info['omx_file_names']
    omx_manifest = skim_info['omx_manifest']   # dict mapping { omx_key: skim_name }

    for omx_file_name in omx_file_names:

        omx_file_path = config.data_file_path(omx_file_name)
        num_skims_loaded = 0

        logger.info(f"read_skims_from_omx {omx_file_path}")

        # read skims into skim_data
        with omx.open_file(omx_file_path) as omx_file:
            for skim_key, omx_key in omx_keys.items():

                if omx_manifest[omx_key] == omx_file_name:
                    omx_data = omx_file[omx_key]
                    assert np.issubdtype(omx_data.dtype, np.floating)

                    offset = block_offsets[skim_key]

                    logger.debug(f"read_skims_from_omx load omx_key {omx_key} "
                                 f"skim_key {skim_key} to offset {offset}")

                    # this will trigger omx readslice to read and copy data to skim_data's buffer
                    a = skim_data[:, :, offset]
                    a[:] = omx_data[:]

                    num_skims_loaded += 1

        logger.info(f"read_skims_from_omx loaded {num_skims_loaded} skims from {omx_file_name}")


def load_skims(skim_info, skim_buffer):

    read_cache = config.setting('read_skim_cache')
    write_cache = config.setting('write_skim_cache')
    assert not (read_cache and write_cache), \
        "read_skim_cache and write_skim_cache are both True in settings file. I am assuming this is a mistake"

    skim_data = skim_data_from_buffer(skim_info, skim_buffer)

    if read_cache:
        read_skim_cache(skim_info, skim_data)
    else:
        read_skims_from_omx(skim_info, skim_data)

    if write_cache:
        write_skim_cache(skim_info, skim_data)


def load_skim_info(skim_tag, omx_file_names, skim_time_periods):
    """
    Read omx files for skim <skim_tag> (e.g. 'TAZ') and build skim_info dict

    Parameters
    ----------
    skim_tag

    Returns
    -------

    """

    tags_to_load = skim_time_periods and skim_time_periods['labels']

    # Note: we load all skims except those with key2 not in tags_to_load
    # Note: we require all skims to be of same dtype so they can share buffer - is that ok?
    # fixme is it ok to require skims be all the same type? if so, is this the right choice?
    skim_dtype = np.float32

    omx_shape = offset_map = offset_map_name = None
    omx_manifest = {}  # dict mapping { omx_key: skim_name }

    for omx_file_name in omx_file_names:

        omx_file_path = config.data_file_path(omx_file_name)

        logger.debug("get_skim_info reading %s" % (omx_file_path,))

        with omx.open_file(omx_file_path) as omx_file:
            # omx_shape = tuple(map(int, tuple(omx_file.shape())))  # sometimes omx shape are floats!

            # fixme call to omx_file.shape() failing in windows p3.5
            if omx_shape is None:
                omx_shape = tuple(int(i) for i in omx_file.shape())  # sometimes omx shape are floats!
            else:
                assert (omx_shape == tuple(int(i) for i in omx_file.shape()))

            for skim_name in omx_file.listMatrices():
                assert skim_name not in omx_manifest, \
                    f"duplicate skim '{skim_name}' found in {omx_manifest[skim_name]} and {omx_file}"
                omx_manifest[skim_name] = omx_file_name

            for m in omx_file.listMappings():
                if offset_map is None:
                    offset_map_name = m
                    offset_map = omx_file.mapentries(offset_map_name)
                    assert len(offset_map) == omx_shape[0]

                    logger.debug(f"get_skim_info skim_tag {skim_tag} using offset_map {m}")
                else:
                    # don't really expect more than one, but ok if they are all the same
                    if not (offset_map == omx_file.mapentries(m)):
                        raise RuntimeError(f"Multiple different mappings in omx file: {offset_map_name} != {m}")

    # - omx_keys dict maps skim key to omx_key
    # DISTWALK: DISTWALK
    # ('DRV_COM_WLK_BOARDS', 'AM'): DRV_COM_WLK_BOARDS__AM, ...
    omx_keys = OrderedDict()
    for skim_name in omx_manifest.keys():
        key1, sep, key2 = skim_name.partition('__')

        # - ignore composite tags not in tags_to_load
        if tags_to_load and sep and key2 not in tags_to_load:
            continue

        skim_key = (key1, key2) if sep else key1
        omx_keys[skim_key] = skim_name

    num_skims = len(omx_keys)

    # - key1_subkeys dict maps key1 to dict of subkeys with that key1
    # DIST: {'DIST': 0}
    # DRV_COM_WLK_BOARDS: {'MD': 1, 'AM': 0, 'PM': 2}, ...
    key1_subkeys = OrderedDict()
    for skim_key, omx_key in omx_keys.items():
        if isinstance(skim_key, tuple):
            key1, key2 = skim_key
        else:
            key1 = key2 = skim_key
        key2_dict = key1_subkeys.setdefault(key1, {})
        key2_dict[key2] = len(key2_dict)

    key1_block_offsets = OrderedDict()
    offset = 0
    for key1, v in key1_subkeys.items():
        num_subkeys = len(v)
        key1_block_offsets[key1] = offset
        offset += num_subkeys

    # - block_offsets dict maps skim_key to offset of omx matrix
    # DIST: 0,
    # ('DRV_COM_WLK_BOARDS', 'AM'): 3,
    # ('DRV_COM_WLK_BOARDS', 'MD') 4, ...
    block_offsets = OrderedDict()
    for skim_key in omx_keys:

        if isinstance(skim_key, tuple):
            key1, key2 = skim_key
        else:
            key1 = key2 = skim_key

        key1_offset = key1_block_offsets[key1]
        key2_relative_offset = key1_subkeys.get(key1).get(key2)
        block_offsets[skim_key] = key1_offset + key2_relative_offset

    logger.debug("get_skim_info skim_dtype %s omx_shape %s num_skims %s" %
                 (skim_dtype, omx_shape, num_skims,))

    skim_info = {
        'skim_tag': skim_tag,
        'omx_file_names': omx_file_names,
        'omx_manifest': omx_manifest,  # dict mapping { omx_key: skim_name }
        'omx_shape': omx_shape,
        'num_skims': num_skims,
        'dtype': skim_dtype,
        'offset_map_name': offset_map_name,
        'offset_map': offset_map,
        'omx_keys': omx_keys,
        'key1_block_offsets': key1_block_offsets,
        'block_offsets': block_offsets,
    }

    return skim_info


def create_skim_dict(skim_tag, skim_info):

    logger.info(f"create_skim_dict loading skim dict skim_tag: {skim_tag}")

    # select the skims to load

    logger.debug(f"create_skim_dict {skim_tag} omx_shape {skim_info['omx_shape']} skim_dtype {skim_info['dtype']}")

    data_buffers = inject.get_injectable('data_buffers', None)
    if data_buffers:
        # we assume any existing skim buffers will already have skim data loaded into them
        logger.info('create_skim_dict {skim_tag} using existing skim_buffers for skims')
        skim_buffer = data_buffers[skim_tag]
    else:
        skim_buffer = allocate_skim_buffer(skim_info, shared=False)
        load_skims(skim_info, skim_buffer)

    skim_data = skim_data_from_buffer(skim_info, skim_buffer)

    logger.info(f"create_skim_dict {skim_tag} bytes {skim_data.nbytes} ({util.GB(skim_data.nbytes)})")

    # create skim dict
    skim_dict = skim.SkimDict(skim_data, skim_info)

    offset_map = skim_info['offset_map']
    if offset_map is not None:
        skim_dict.offset_mapper.set_offset_list(offset_map)
        logger.debug(f"create_skim_dict {skim_tag} "
                     f"using offset map {skim_info['offset_map_name']} "
                     f"from omx file: {offset_map}")
    else:
        # assume this is a one-based skim map
        skim_dict.offset_mapper.set_offset_int(-1)

    return skim_dict


class Network_LOS(object):

    def __init__(self):

        self.zone_system = None
        self.skim_time_periods = None

        self.skim_info = {}
        self.skim_dicts = {}
        self.skim_buffers = {}
        self.table_info = {}

        # TWO_ZONE tables
        self.maz_to_taz_df = None
        self.maz_to_maz_df = None
        self.maz_to_maz_cardinality = None

        self.skim_time_periods = config.setting('skim_time_periods')
        self.read_los_settings()

    def read_los_settings(self):

        los_settings = config.setting(LOS_SETTINGS_NAME)
        assert los_settings is not None, f"Network_LOS: '{LOS_SETTINGS_NAME}' settings not in settings file "

        self.zone_system = los_settings.get('zone_system')
        assert self.zone_system in [ONE_ZONE, TWO_ZONE], f"Network_LOS: unrecognized zone_system: {self.zone_system}"

        # load skim info
        skim_file_names = los_settings.get('skims')
        assert skim_file_names is not None, f"'skims' list nor found in {LOS_SETTINGS_NAME} settings"
        for skim_tag, file_names in skim_file_names.items():
            # want skim_file_names list, not just single str file_name
            file_names = [file_names] if isinstance(file_names, str) else file_names
            self.skim_info[skim_tag] = self.load_skim_info(skim_tag, file_names)

        # one zone
        assert 'taz' in self.skim_info

        # load tables info
        if self.zone_system in [TWO_ZONE, THREE_ZONE]:

            self.table_info = los_settings.get('tables', {})

            # FIXME - do we know which tables we expect?
            for table_name in REQUIRED_TWO_ZONE_TABLES:
                assert table_name in self.table_info, \
                    f"'{table_name}' file name not listed in {LOS_SETTINGS_NAME}.tables {self.table_info}"

        # read time_periods setting
        self.skim_time_periods = config.setting('skim_time_periods')

    def load_all_tables(self):

        if self.zone_system in [TWO_ZONE, THREE_ZONE]:

            # maz_to_taz
            file_name = self.table_info.get('maz_to_taz')
            assert file_name is not None, f"file_names not found for 'maz_to_taz' in {LOS_SETTINGS_NAME}.tables"
            file_path = config.data_file_path(file_name, mandatory=True)
            self.maz_to_taz_df = pd.read_csv(file_path)

            self.maz_to_maz_cardinality = self.maz_to_taz_df.MAZ.max() + 1

            # maz_to_maz_df
            file_names = self.table_info.get('maz_to_maz')
            assert file_names is not None, f"file_names not found for 'maz_to_maz' in {LOS_SETTINGS_NAME}.tables"
            file_names = [file_names] if isinstance(file_names, str) else file_names
            for file_name in file_names:
                table_file_path = config.data_file_path(file_name, mandatory=True)
                df = pd.read_csv(table_file_path)

                df['i'] = df.OMAZ * self.maz_to_maz_cardinality + df.DMAZ
                df.set_index('i', drop=True, inplace=True, verify_integrity=True)
                logger.debug(f"loading maz_to_maz table {file_name} with {len(df)} rows")

                # FIXME - don't really need these columns, but if we do want them,
                #  we would need to merge them in since files may have different numbers of rows
                df.drop(columns=['OMAZ', 'DMAZ'], inplace=True)

                if self.maz_to_maz_df is None:
                    self.maz_to_maz_df = df
                else:
                    self.maz_to_maz_df = pd.concat([self.maz_to_maz_df, df], axis=1)

        if self.zone_system == THREE_ZONE:
            # THREE_ZONE tables here...
            pass

    def load_skim_info(self, skim_tag, omx_file_names):
        """
        Read omx files for skim <skim_tag> (e.g. 'TAZ') and build skim_info dict
        """

        # we could just do nothing and return if already loaded, but for now tidier to track this
        assert skim_tag not in self.skim_info
        return load_skim_info(skim_tag, omx_file_names, self.skim_time_periods)

    def create_skim_dict(self, skim_tag):
        return create_skim_dict(skim_tag, self.skim_info[skim_tag])

    def load_all_skims(self):
        for skim_tag in self.skim_info.keys():
            self.skim_dicts[skim_tag] = self.create_skim_dict(skim_tag)

    def load_shared_data(self, shared_data_buffers):
        for skim_tag in self.skim_info.keys():
            load_skims(self.skim_info[skim_tag], shared_data_buffers[skim_tag])

        # FIXME - should also load tables - if we knew how to share data
        # for table_name in self.table_info:
        #     do something

    def allocate_shared_skim_buffers(self):

        assert not self.skim_buffers

        for skim_tag in self.skim_info.keys():
            self.skim_buffers[skim_tag] = allocate_skim_buffer(self.skim_info[skim_tag], shared=True)

        return self.skim_buffers

    def get_skim_dict(self, skim_tag):
        assert skim_tag in self.skim_dicts
        return self.skim_dicts[skim_tag]

    def get_table(self, table_name):
        assert table_name in self.tables, f"get_table: table '{table_name}' not loaded"
        return self.tables.get(table_name)

    def get_mazpairs(self, omaz, dmaz, attribute):

        # # this is slower
        # s = pd.merge(pd.DataFrame({'OMAZ': omaz, 'DMAZ': dmaz}),
        #              self.maz_to_maz_df,
        #              how="left")[attribute]

        # synthetic index method i : omaz_dmaz
        i = np.asanyarray(omaz) * self.maz_to_maz_cardinality + np.asanyarray(dmaz)
        s = util.quick_loc_df(i, self.maz_to_maz_df, attribute)

        # FIXME - no point in returning series? unless maz and tap have same index?
        return np.asanyarray(s)