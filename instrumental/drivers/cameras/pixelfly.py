# -*- coding: utf-8 -*-
# Copyright 2015-2016 Nate Bogdanowicz
"""
Driver for PCO Pixelfly cameras.
"""
from future.utils import PY2

import atexit
import os.path
from time import clock
import numpy as np
from scipy.interpolate import interp1d
import win32event

from nicelib import NiceLib, NiceObjectDef, load_lib

from ._pixelfly import errortext
from . import Camera
from .. import InstrumentTypeError, _ParamDict
from ..util import check_units
from ...errors import Error, TimeoutError
from ... import Q_, u

if PY2:
    memoryview = buffer  # Needed b/c np.frombuffer is broken on memoryviews in PY2

__all__ = ['Pixelfly']

# Developed using version 2.1.0.29 of pf_cam.dll
info = load_lib('pixelfly', __package__)
ffi = info._ffi


class NicePixelfly(NiceLib):
    _info = info

    def _ret(code):
        if code != 0:
            pbuf = errortext.ffi.new('char[]', 1024)
            errortext.lib.PCO_GetErrorText(errortext.ffi.cast('unsigned int', code), pbuf,
                                           len(pbuf))
            err_message = errortext.ffi.string(pbuf)
            e = Error('({}) {}'.format(code, err_message))
            e.err_code = code
            raise e

    INITBOARD = ('in', 'out')  # Second arg should really be 'inout'
    CHECK_BOARD_AVAILABILITY = ('in')

    Board = NiceObjectDef(init='INITBOARD', attrs=dict(
        CLOSEBOARD = ('inout'),
        START_CAMERA = ('in'),
        STOP_CAMERA = ('in'),
        TRIGGER_CAMERA = ('in'),
        SETMODE = ('in', 'in', 'in', 'in', 'in', 'in', 'in', 'in', 'in', 'in'),
        SET_EXPOSURE = ('in', 'in'),
        GETMODE = ('in', 'out', 'out', 'out', 'out', 'out', 'out', 'out', 'out', 'out'),
        GETSIZES = ('in', 'out', 'out', 'out', 'out', 'out'),
        GETBOARDVAL = ('in', 'in', 'inout'),  # TODO: deal with void pointer properly
        READVERSION = ('in', 'in', 'buf', 'len=64'),
        READTEMPERATURE = ('in', 'out'),
        WRRDORION = ('in', 'in', 'out'),
        SETORIONINT = ('in', 'in', 'in', 'in', 'in'),  # TODO: NiceLib needs something like bufin
        GETORIONINT = ('in', 'in', 'in', 'buf', 'len'),
        READEEPROM = ('in', 'in', 'in', 'out'),
        WRITEEEPROM = ('in', 'in', 'in', 'in'),
        SETTIMEOUTS = ('in', 'in', 'in', 'in'),
        #SET_TIMEOUT_VALUES = ('in', 'arr', 'len'),  # TODO: len is in bytes
        SETDRIVER_EVENT = ('in', 'in', 'inout'),
        PCC_GET_VERSION = ('in', 'out', 'out'),
        READ_IMAGE = ('in', 'in', 'len', 'arr', 'in'),  # TODO: Check this
        ALLOCATE_BUFFER_EX = ('in', 'inout', 'in', 'inout', 'inout'),
        FREE_BUFFER = ('in', 'in'),
        SETBUFFER_EVENT = ('in', 'in', 'inout'),
        CLEARBUFFER_EVENT = ('in', 'in', 'inout'),
        PCC_RESETEVENT = ('in', 'in'),
        ADD_BUFFER_TO_LIST = ('in', 'in', 'in', 'in', 'in'),
        REMOVE_BUFFER_FROM_LIST = ('in', 'in'),
        ADD_BUFFER = ('in', 'in', 'in', 'in', 'in'),
        REMOVE_BUFFER = ('in', 'in'),
        REMOVE_ALL_BUFFERS_FROM_LIST = ('in'),
        PCC_WAITFORBUFFER = ('in', 'in', 'inout', 'in'),
        GETBUFFER_STATUS = ('in', 'in', 'in', 'arr', 'len=4:byte'),
    ))


# Load QE curves
data_dir = os.path.join(os.path.dirname(__file__), '_pixelfly')
def load_qe_curve(fname):
    data = np.loadtxt(os.path.join(data_dir, fname))
    return interp1d(data[:, 0], data[:, 1]*0.01, bounds_error=False, fill_value=0.,
                    assume_sorted=True)


class Pixelfly(Camera):
    DEFAULT_KWDS = Camera.DEFAULT_KWDS.copy()
    DEFAULT_KWDS.update(trig='software', gain='low')
    _open_cameras = []

    _qe_high = load_qe_curve('QEHigh.tsv')
    _qe_low = load_qe_curve('QELow.tsv')
    _qe_vga = load_qe_curve('VGA.tsv')

    def __init__(self, board_num=0):
        self._dev = NicePixelfly.Board(board_num)
        self._cam_started = False
        self._mode_set = False
        self._mem_set_up = False
        self._partial_sequence = []
        self._capture_started = False

        # For saving
        self._param_dict = _ParamDict("<Pixelfly '{}'>".format(board_num))
        self._param_dict.module = 'cameras.pixelfly'
        self._param_dict['module'] = 'cameras.pixelfly'
        self._param_dict['pixelfly_board_num'] = board_num

        self._bufsizes = []
        self._bufnums = []
        self._bufptrs = []
        self._buf_events = []
        self._nbufs = 0
        self._buf_i = 0

        try:
            self.set_mode()
        except:
            self._dev.CLOSEBOARD()
            raise

        self._open_cameras.append(self)
        self._last_kwds = {}

    @staticmethod
    def _list_boards():
        board_nums = []

        for board_num in range(4):
            try:
                board = NicePixelfly.Board(board_num)
            except Exception:
                pass
            else:
                board.CLOSEBOARD()
                board_nums.append(board_num)

        return board_nums

    def close(self):
        """ Clean up memory and close the camera."""
        if self._cam_started:
            self._dev.STOP_CAMERA()

        self._dev.REMOVE_ALL_BUFFERS_FROM_LIST()
        for bufnum in self._bufnums:
            self._dev.FREE_BUFFER(bufnum)
        self._bufsizes, self._bufnums, self._bufptrs = [], [], []
        self._nbufs = 0

        self._dev.CLOSEBOARD()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def set_mode(self, shutter='single', trig='software', exposure='10ms',
                 hbin=1, vbin=1, gain='low', depth=12):
        """ Set the mode of the camera.

        Parameters
        ----------
        shutter : str
            One of 'single', 'double', or 'video'.
        trig : str
            One of 'software' or 'hardware'.
        exposure : Quantity or str
            Exposure time. Up to 65.6 ms with microsecond resolution.
        hbin : int
            Horizontal binning factor. Either 1 or 2.
        vbin : int
            Vertical binning factor. Either 1 or 2.
        gain : str
            Gain of camera. Either 'low' or 'high'.
        depth : int
            Bit depth of each pixel. Either 8 or 12.
        """
        # Normalize all the parameters
        shutter_map = {'single': 0x10, 'double': 0x20, 'video': 0x30}
        mode = shutter_map[shutter] + (1 if trig == 'software' else 0)
        exp_units = 'ms' if shutter == 'video' else 'us'
        exptime = int(Q_(exposure).to(exp_units).magnitude)

        # Check exptime bounds to avoid cryptic errors
        if shutter == 'video':
            if not (1 <= exptime <= 65535):
                raise Error("Invalid exposure time {}. Exposure must be between 1 us and "
                            "65.535 ms when in video shutter mode".format(exposure))
        else:
            if not (5 <= exptime <= 65535):
                raise Error("Invalid exposure time {}. Exposure must be between 5 us and "
                            "65.535 ms when in single shutter mode".format(exposure))

        hbin_val = 0 if hbin == 1 else 1
        vbin_val = 0 if vbin == 1 else 1
        gain_val = 0 if gain == 'low' else 1
        bit_pix = 12 if depth == 12 else 8

        self._shutter = shutter

        # Camera must be stopped before SETMODE is called
        if self._cam_started:
            self._dev.STOP_CAMERA()

        self._dev.SETMODE(mode, 0, exptime, hbin_val, vbin_val, gain_val, 0, bit_pix, 0)

        self._load_sizes()
        self._allocate_buffers()
        self.color_mode = 'mono16'

    def start_live_video(self, **kwds):
        self._handle_kwds(kwds)
        self._last_kwds = kwds

        #self.set_mode('video')
        self.set_mode(exposure=kwds['exposure_time'], hbin=kwds['hbin'], vbin=kwds['vbin'],
                      shutter='video', trig=kwds.pop('trig', 'software'),
                      gain=kwds['gain'])

        # Set software ROI; must be after call to set_mode()
        self._width = kwds['width']
        self._height = kwds['height']

        self._trigger()

    def stop_live_video(self):
        self.set_mode()

    @check_units(timeout='?ms')
    def wait_for_frame(self, timeout=None):
        """wait_for_frame(self, timeout=None')"""
        timeout = win32event.INFINITE if timeout is None else max(0, timeout.m_as('ms'))

        buf_i = (self._buf_i) % self._nbufs  # Most recently triggered buffer
        ret = win32event.WaitForSingleObject(int(self._buf_events[buf_i]), int(timeout))

        if ret != win32event.WAIT_OBJECT_0:
            return False  # Object is not signaled

        win32event.ResetEvent(int(self._buf_events[buf_i]))
        self._dev.PCC_RESETEVENT(buf_i)
        status = self._dev.GETBUFFER_STATUS(self._bufnums[buf_i], 0)

        #if px.PCC_BUF_STAT_ERROR(ptr):
        if status[0] & 0xF000:
            uptr = ffi.cast('DWORD *', status)
            raise Exception("Buffer error 0x{:08X} 0x{:08X} 0x{:08X} 0x{:08X}".format(
                            uptr[0], uptr[1], uptr[2], uptr[3]))

        if self._shutter == 'video':
            self._dev.ADD_BUFFER_TO_LIST(self._bufnums[self._buf_i], self._frame_size(), 0, 0)
        self._buf_i = (self._buf_i + 1) % self._nbufs

        return True

    def _frame_size(self):
        return self._binned_width * self._binned_height * (self.bit_depth//8 + 1)

    def _allocate_buffers(self, nbufs=None):
        if nbufs is None:
            if self._nbufs > 1:
                nbufs = self._nbufs
            elif self._shutter == 'video':
                nbufs = 2
            else:
                nbufs = 1

        frame_size = self._frame_size()
        bufnr_p = ffi.new('int *')

        # Remove and free all existing buffers
        self._dev.REMOVE_ALL_BUFFERS_FROM_LIST()
        for bufnum in self._bufnums:
            self._dev.FREE_BUFFER(bufnum)
        self._bufnums = []
        self._bufsizes = []
        self._buf_events = []
        self._bufptrs = []

        # Create new buffers
        for i in range(nbufs):
            adr = ffi.new('void **')
            bufnr_p[0] = -1  # Allocate new buffer
            event_p = ffi.new('HANDLE *', ffi.NULL)

            self._dev.ALLOCATE_BUFFER_EX(bufnr_p, frame_size, event_p, adr)

            self._bufnums.append(bufnr_p[0])
            self._bufsizes.append(frame_size)
            self._buf_events.append(ffi.cast('unsigned int', event_p[0]))
            self._bufptrs.append(ffi.cast('void *', adr[0]))
        self._nbufs = nbufs

        self._dev.START_CAMERA()
        self._cam_started = True
        self._mem_set_up = True

    def _trigger(self):
        frame_size = self._frame_size()

        for i in range(self._nbufs):
            self._dev.ADD_BUFFER_TO_LIST(self._bufnums[i], frame_size, 0, 0)
        self._buf_i = 0
        self._capture_started = True

        self._dev.TRIGGER_CAMERA()

    def latest_frame(self, copy=True):
        buf_i = (self._buf_i - 1) % self._nbufs
        if copy:
            buf = memoryview(ffi.buffer(self._bufptrs[buf_i], self._frame_size())[:])
        else:
            buf = memoryview(ffi.buffer(self._bufptrs[buf_i], self._frame_size()))

        arr = self._array_from_buffer(buf)

        # Software ROI
        kwds = self._last_kwds
        return arr[kwds['top']:kwds['bot'], kwds['left']:kwds['right']]

    def start_capture(self, **kwds):
        self._handle_kwds(kwds)
        self._last_kwds = kwds

        #if kwds['n_frames'] > 1:
        #    raise Error("Pixelfly camera does not support multi-image capture sequences")
        self._nbufs = kwds.get('n_frames', 1)

        self.set_mode(exposure=kwds['exposure_time'], hbin=kwds['hbin'], vbin=kwds['vbin'],
                      shutter=kwds.pop('shutter', 'single'), trig=kwds.pop('trig', 'software'),
                      gain=kwds['gain'])

        # Set software ROI; must be after call to set_mode()
        self._width = kwds['width']
        self._height = kwds['height']

        self._trigger()

    def cancel_capture(self):
        """Cancels a capture sequence, cleaning up and stopping the camera"""
        pass

    @check_units(timeout='?ms')
    def get_captured_image(self, timeout='1s', copy=True, **kwds):
        self._handle_kwds(kwds)  # Should get rid of this duplication somehow...
        image_arrs = []

        if not self._capture_started:
            raise Error("No capture initiated. You must first call start_capture()")

        start_time = clock() * u.s
        while self._buf_i < self._nbufs:
            if timeout is None:
                frame_ready = self.wait_for_frame(timeout=None)
            else:
                elapsed_time = clock() * u.s - start_time
                frame_ready = self.wait_for_frame(timeout - elapsed_time)

            if not frame_ready:
                self._partial_sequence.extend(image_arrs)  # Save for later
                raise TimeoutError

            if copy:
                buf = memoryview(ffi.buffer(self._bufptrs[self._buf_i], self._frame_size())[:])
            else:
                buf = memoryview(ffi.buffer(self._bufptrs[self._buf_i], self._frame_size()))

            array = self._array_from_buffer(buf)

            if kwds['fix_hotpixels']:
                array = self._correct_hot_pixels(array)

            # Software ROI
            kwds = self._last_kwds
            array = array[kwds['top']:kwds['bot'], kwds['left']:kwds['right']]

            image_arrs.append(array)
            self._buf_i += 1

            # FIXME: HACK -- remove me
            if self._buf_i < self._nbufs:
                self._dev.TRIGGER_CAMERA()

        image_arrs = self._partial_sequence + image_arrs
        self._partial_sequence = []

        if len(image_arrs) == 1:
            return image_arrs[0]
        else:
            return tuple(image_arrs)

    def _array_from_buffer(self, buf):
        dtype = np.uint8 if self.bit_depth <= 8 else np.uint16
        if self._shutter != 'double':
            arr = np.frombuffer(buf, dtype)
            return arr.reshape((self._binned_height, self._binned_width))
        else:
            px_per_frame = self._binned_width*self._binned_height
            byte_per_px = self.bit_depth/8 + 1
            arr1 = np.frombuffer(buf, dtype, px_per_frame, 0)
            arr2 = np.frombuffer(buf, dtype, px_per_frame, px_per_frame*byte_per_px)
            return (arr1.reshape((self._binned_height, self._binned_width)),
                    arr2.reshape((self._binned_height, self._binned_width)))

    def grab_image(self, timeout='1s', copy=True, **kwds):
        self.start_capture(**kwds)
        return self.get_captured_image(timeout=timeout, copy=copy, **kwds)

    def _load_sizes(self):
        ccdx, ccdy, actualx, actualy, bit_pix = self._dev.GETSIZES()
        self._max_width = ccdx
        self._max_height = ccdy
        self._binned_width = actualx
        self._binned_height = actualy
        self._bit_depth = bit_pix

        self._width = self._binned_width
        self._height = self._binned_height

        if self._shutter == 'double':
            self._height = self._height / 2  # Give the height of *each* image individually

    def _version(self, typ):
        return self._dev.READVERSION(typ)

    @check_units(wavlen='nm')
    def quantum_efficiency(self, wavlen, high_gain=False):
        """quantum_efficiency(self, wavlen, high_gain=False)

        Fractional quantum efficiency of the sensor at a given wavelength
        """
        if self.max_width == 640:
            curve = self._qe_vga
        elif self.max_width == 1392:
            curve = self._qe_high if high_gain else self._qe_low
        else:
            raise Error("Unrecognized pixelfly model")
        return float(curve(wavlen.m_as('nm')))

    @property
    def temperature(self):
        """ The temperature of the CCD. """
        temp_C = self._dev.READTEMPERATURE()
        return Q_(temp_C, 'degC')

    width = property(lambda self: self._width)
    height = property(lambda self: self._height)
    max_width = property(lambda self: self._max_width)
    max_height = property(lambda self: self._max_height)
    bit_depth = property(lambda self: self._bit_depth)


def list_instruments():
    board_nums = Pixelfly._list_boards()
    cams = []

    for board_num in board_nums:
        params = _ParamDict("<Pixelfly '{}'>".format(board_num))
        params.module = 'cameras.pixelfly'
        params['pixelfly_board_num'] = board_num
        cams.append(params)
    return cams


def _instrument(params):
    if 'pixelfly_board_num' in params:
        cam = Pixelfly(params['pixelfly_board_num'])
    elif params.module == 'cameras.pixelfly':
        cam = Pixelfly()
    else:
        raise InstrumentTypeError()
    return cam


@atexit.register
def _cleanup():
    for cam in Pixelfly._open_cameras:
        try:
            cam.close()
        except:
            pass


def close_all():
    board = NicePixelfly.Board(0)
    board.STOP_CAMERA()
    board.CLOSEBOARD()
