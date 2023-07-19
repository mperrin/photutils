# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
This module provides models for doing PSF/PRF-fitting photometry.
"""

import copy
import io
import itertools
import os
import warnings
from functools import lru_cache
from itertools import product

import numpy as np
from astropy.io import fits, registry
from astropy.io.fits import HDUList
from astropy.io.fits.verify import VerifyWarning
from astropy.modeling import Fittable2DModel, Parameter
from astropy.nddata import NDData

__all__ = ['GriddedPSFModel']


class GriddedPSFModelRead(registry.UnifiedReadWrite):
    def __init__(self, instance, cls):
        super().__init__(instance, cls, "read", registry=None)
        # uses default global registry

    def __call__(self, *args, **kwargs):
        return self.registry.read(self._cls, *args, **kwargs)


class GriddedPSFModel(Fittable2DModel):
    """
    A fittable 2D model containing a grid PSF models defined at specific
    locations that are interpolated to evaluate a PSF at an arbitrary
    (x, y) position.

    Parameters
    ----------
    data : `~astropy.nddata.NDData`
        A `~astropy.nddata.NDData` object containing the grid of
        reference PSF arrays. The data attribute must contain a 3D
        `~numpy.ndarray` containing a stack of the 2D PSFs with a shape
        of ``(N_psf, PSF_ny, PSF_nx)``. The meta attribute must be
        `dict` containing the following:

            * ``'grid_xypos'``: A list of the (x, y) grid positions of
              each reference PSF. The order of positions should match the
              first axis of the 3D `~numpy.ndarray` of PSFs. In other
              words, ``grid_xypos[i]`` should be the (x, y) position of
              the reference PSF defined in ``data[i]``.

            * ``'oversampling'``: The integer oversampling factor of the
              PSF.

        The meta attribute may contain other properties such as the
        telescope, instrument, detector, and filter of the PSF.
    """

    flux = Parameter(description='Intensity scaling factor for the PSF '
                     'model.', default=1.0)
    x_0 = Parameter(description='x position in the output coordinate grid '
                    'where the model is evaluated.', default=0.0)
    y_0 = Parameter(description='y position in the output coordinate grid '
                    'where the model is evaluated.', default=0.0)

    read = registry.UnifiedReadWriteMethod(GriddedPSFModelRead)

    def __init__(self, data, *, flux=flux.default, x_0=x_0.default,
                 y_0=y_0.default, fill_value=0.0):

        self._data_input = self._validate_data(data)
        self.data = data.data
        self._meta = data.meta  # use _meta to avoid the meta descriptor
        self.grid_xypos = data.meta['grid_xypos']
        self.oversampling = data.meta['oversampling']
        self.fill_value = fill_value

        self._grid_xpos, self._grid_ypos = np.transpose(self.grid_xypos)
        self._xgrid = np.unique(self._grid_xpos)  # also sorts values
        self._ygrid = np.unique(self._grid_ypos)  # also sorts values
        self.meta['grid_shape'] = (len(self._ygrid), len(self._xgrid))
        if (len(list(itertools.product(self._xgrid, self._ygrid)))
                != len(self.grid_xypos)):
            raise ValueError('"grid_xypos" must form a regular grid.')

        self._xidx = np.arange(self.data.shape[2], dtype=float)
        self._yidx = np.arange(self.data.shape[1], dtype=float)

        # Here we avoid decorating the instance method with @lru_cache
        # to prevent memory leaks; we set maxsize=128 to prevent the
        # cache from growing too large.
        self._calc_interpolator = lru_cache(maxsize=128)(
            self._calc_interpolator_uncached)

        super().__init__(flux, x_0, y_0)

    @staticmethod
    def _validate_data(data):
        if not isinstance(data, NDData):
            raise TypeError('data must be an NDData instance.')

        if data.data.ndim != 3:
            raise ValueError('The NDData data attribute must be a 3D numpy '
                             'ndarray')

        if 'grid_xypos' not in data.meta:
            raise ValueError('"grid_xypos" must be in the nddata meta '
                             'dictionary.')
        if len(data.meta['grid_xypos']) != data.data.shape[0]:
            raise ValueError('The length of grid_xypos must match the number '
                             'of input PSFs.')

        if 'oversampling' not in data.meta:
            raise ValueError('"oversampling" must be in the nddata meta '
                             'dictionary.')
        if not np.isscalar(data.meta['oversampling']):
            raise ValueError('oversampling must be a scalar value')

        return data

    def __str__(self):
        cls_name = f'<{self.__class__.__module__}.{self.__class__.__name__}>'
        cls_info = []

        keys = ('STDPSF', 'instrument', 'detector', 'filter', 'grid_shape')
        for key in keys:
            if key in self.meta:
                name = key.capitalize() if key != 'STDPSF' else key
                cls_info.append((name, self.meta[key]))

        cls_info.extend([('Number of ePSFs', len(self.grid_xypos)),
                         ('ePSF shape (oversampled pixels)',
                          self.data.shape[1:]),
                         ('Oversampling', self.oversampling),
                         ])

        with np.printoptions(threshold=25, edgeitems=5):
            fmt = [f'{key}: {val}' for key, val in cls_info]

        return f'{cls_name}\n' + '\n'.join(fmt)

    def __repr__(self):
        return self.__str__()

    def copy(self):
        """
        Return a copy of this model.

        Note that the PSF grid data is not copied. Use the `deepcopy`
        method if you want to copy the PSF grid data.
        """
        return self.__class__(self._data_input, flux=self.flux.value,
                              x_0=self.x_0.value, y_0=self.y_0.value,
                              fill_value=self.fill_value)

    def deepcopy(self):
        """
        Return a deep copy of this model.
        """
        return copy.deepcopy(self)

    def clear_cache(self):
        """
        Clear the internal cache.
        """
        self._calc_interpolator.cache_clear()

    def _cache_info(self):
        """
        Return information about the internal cache.
        """
        return self._calc_interpolator.cache_info()

    @staticmethod
    def _find_start_idx(data, x):
        """
        Find the index of the lower bound where ``x`` should be inserted
        into ``a`` to maintain order.

        The index of the upper bound is the index of the lower bound
        plus 2.  Both bound indices must be within the array.

        Parameters
        ----------
        data : 1D `~numpy.ndarray`
            The 1D array to search.

        x : float
            The value to insert.

        Returns
        -------
        index : int
            The index of the lower bound.
        """
        idx = np.searchsorted(data, x)
        if idx == 0:
            idx0 = 0
        elif idx == len(data):  # pragma: no cover
            idx0 = idx - 2
        else:
            idx0 = idx - 1
        return idx0

    def _find_bounding_points(self, x, y):
        """
        Find the indices of the grid points that bound the input
        ``(x, y)`` position.

        Parameters
        ----------
        x, y : float
            The ``(x, y)`` position where the PSF is to be evaluated.
            The position must be inside the region defined by the grid
            of PSF positions.

        Returns
        -------
        indices : list of int
            A list of indices of the bounding grid points.
        """
        x0 = self._find_start_idx(self._xgrid, x)
        y0 = self._find_start_idx(self._ygrid, y)
        xypoints = list(itertools.product(self._xgrid[x0:x0 + 2],
                                          self._ygrid[y0:y0 + 2]))

        # find the grid_xypos indices of the reference xypoints
        indices = []
        for xx, yy in xypoints:
            indices.append(np.argsort(np.hypot(self._grid_xpos - xx,
                                               self._grid_ypos - yy))[0])

        return indices

    @staticmethod
    def _bilinear_interp(xyref, zref, xi, yi):
        """
        Perform bilinear interpolation of four 2D arrays located at
        points on a regular grid.

        Parameters
        ----------
        xyref : list of 4 (x, y) pairs
            A list of 4 ``(x, y)`` pairs that form a rectangle.

        zref : 3D `~numpy.ndarray`
            A 3D `~numpy.ndarray` of shape ``(4, nx, ny)``. The first
            axis corresponds to ``xyref``, i.e., ``refdata[0, :, :]`` is
            the 2D array located at ``xyref[0]``.

        xi, yi : float
            The ``(xi, yi)`` point at which to perform the
            interpolation.  The ``(xi, yi)`` point must lie within the
            rectangle defined by ``xyref``.

        Returns
        -------
        result : 2D `~numpy.ndarray`
            The 2D interpolated array.
        """
        xyref = [tuple(i) for i in xyref]
        idx = sorted(range(len(xyref)), key=xyref.__getitem__)
        xyref = sorted(xyref)  # sort by x, then y
        (x0, y0), (_x0, y1), (x1, _y0), (_x1, _y1) = xyref

        if x0 != _x0 or x1 != _x1 or y0 != _y0 or y1 != _y1:
            raise ValueError('The refxy points do not form a rectangle.')

        if not np.isscalar(xi):
            xi = xi[0]
        if not np.isscalar(yi):
            yi = yi[0]

        if not x0 <= xi <= x1 or not y0 <= yi <= y1:
            raise ValueError('The (x, y) input is not within the rectangle '
                             'defined by xyref.')

        data = np.asarray(zref)[idx]
        weights = np.array([(x1 - xi) * (y1 - yi), (x1 - xi) * (yi - y0),
                            (xi - x0) * (y1 - yi), (xi - x0) * (yi - y0)])
        norm = (x1 - x0) * (y1 - y0)

        return np.sum(data * weights[:, None, None], axis=0) / norm

    def _calc_interpolator_uncached(self, x_0, y_0):
        """
        Return the local interpolation function for the PSF model at
        (x_0, y_0).

        Note that the interpolator will be cached by _calc_interpolator.
        It can be cleared by calling the clear_cache method.
        """
        from scipy.interpolate import RectBivariateSpline

        if (x_0 < self._xgrid[0] or x_0 > self._xgrid[-1]
                or y_0 < self._ygrid[0] or y_0 > self._ygrid[-1]):
            # position is outside of the grid, so simply use the
            # closest reference PSF
            ref_index = np.argsort(np.hypot(self._grid_xpos - x_0,
                                            self._grid_ypos - y_0))[0]
            psf_image = self.data[ref_index, :, :]
        else:
            # find the four bounding reference PSFs and interpolate
            ref_indices = self._find_bounding_points(x_0, y_0)
            xyref = np.array(self.grid_xypos)[ref_indices]
            psfs = self.data[ref_indices, :, :]

            psf_image = self._bilinear_interp(xyref, psfs, x_0, y_0)

        interpolator = RectBivariateSpline(self._xidx, self._yidx,
                                           psf_image.T, kx=3, ky=3, s=0)

        return interpolator

    def evaluate(self, x, y, flux, x_0, y_0):
        """
        Evaluate the `GriddedPSFModel` for the input parameters.
        """
        # NOTE: the astropy base Model.__call__() method converts scalar
        # inputs to size-1 arrays before calling evaluate().
        if not np.isscalar(flux):
            flux = flux[0]
        if not np.isscalar(x_0):
            x_0 = x_0[0]
        if not np.isscalar(y_0):
            y_0 = y_0[0]

        # Calculate the local interpolation function for the PSF at
        # (x_0, y_0). Only the integer part of the position is input in
        # order to have effective caching.
        interpolator = self._calc_interpolator(int(x_0), int(y_0))

        # now evaluate the PSF at the (x_0, y_0) subpixel position on
        # the input (x, y) values
        xi = self.oversampling * (np.asarray(x, dtype=float) - x_0)
        yi = self.oversampling * (np.asarray(y, dtype=float) - y_0)

        # define origin at the PSF image center
        ny, nx = self.data.shape[1:]
        xi += (nx - 1) / 2
        yi += (ny - 1) / 2

        evaluated_model = flux * interpolator.ev(xi, yi)

        if self.fill_value is not None:
            # find indices of pixels that are outside the input pixel
            # grid and set these pixels to the fill_value
            invalid = (((xi < 0) | (xi > nx - 1))
                       | ((yi < 0) | (yi > ny - 1)))
            evaluated_model[invalid] = self.fill_value

        return evaluated_model


def stdpsf_reader(filename, oversampling=4, sci_exten=None, filter_name=None):
    """
    Generate a `~photutils.psf.GriddedPSFModel` from a STScI
    standard-format PSF file.
    """
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', VerifyWarning)
        with fits.open(filename, ignore_missing_end=True) as hdulist:
            header = hdulist[0].header
            data = hdulist[0].data

    npsfs = header['NAXIS3']
    nxpsfs = header['NXPSFS']
    nypsfs = header['NYPSFS']
    data_ny, data_nx = data.shape[1:]

    if 'IPSFX01' in header:
        xgrid = [header[f'IPSFX{i:02d}'] for i in range(1, nxpsfs + 1)]
        ygrid = [header[f'JPSFY{i:02d}'] for i in range(1, nypsfs + 1)]
    elif 'IPSFXA5' in header:
        xgrid = [int(n) for n in header['IPSFXA5'].split()] * 4
        ygrid = [int(n) for n in header['JPSFYA5'].split()] * 2
    else:
        raise ValueError('Unknown standard-format PSF file.')

    # STDPDF FITS positions are 1-indexed
    xgrid = np.array(xgrid) - 1
    ygrid = np.array(ygrid) - 1

    # (nypsfs, nxpsfs)
    # (6, 6)   # WFPC2, 4 det
    # (1, 1)   # ACS/HRC
    # (10, 9)  # ACS/WFC, 2 det
    # (3, 3)   # WFC3/IR
    # (8, 7)   # WFC3/UVIS, 2 det
    # (5, 5)   # NIRISS
    # (5, 5)   # NIRCam SW
    # (10, 20) # NIRCam SW, 8 det
    # (5, 5)   # NIRCam LW
    # (3, 3)   # MIRI
    if npsfs in (90, 56):  # ACS/WFC or WFC3/UVIS data (2 chips)
        if sci_exten is None:
            raise ValueError('sci_exten must be specified for ACS/WFC '
                             'and WFC3/UVIS PSFs.')
        if sci_exten not in (1, 2):
            raise ValueError('sci_exten must be 1 or 2.')

        # ACS/WFC1 and WFC3/UVIS1 chip1 (sci, 2) are above chip2 (sci, 1)
        # in y-pixel coordinates
        ygrid = ygrid.reshape((2, ygrid.shape[0] // 2))[sci_exten - 1]
        if sci_exten == 2:
            ygrid -= 2048

        data = data.reshape((2, npsfs // 2, data_ny, data_nx))[sci_exten - 1]
        nypsfs //= 2

    if npsfs == 36:
        raise NotImplementedError('WFPC2 PSFs not yet supported.')

    if npsfs == 200:
        raise NotImplementedError('NIRCam SW PSFs not yet supported.')

    # product iterates over the last input first
    xy_grid = [yx[::-1] for yx in product(ygrid, xgrid)]

    meta = {'grid_xypos': xy_grid,
            'oversampling': oversampling}

    # try to get metadata
    file_meta = _get_metadata(filename, npsfs, sci_exten)
    if file_meta is not None:
        meta.update(file_meta)

    nddata = NDData(data, meta=meta)

    return GriddedPSFModel(nddata)


def _get_metadata(filename, npsfs, sci_exten):
    if isinstance(filename, io.FileIO):
        filename = filename.name

    parts = os.path.basename(filename).strip('.fits').split('_')
    if len(parts) not in (3, 4):
        return None  # filename from astropy download_file

    detector, filter_name = parts[1:3]
    detector_map = {'WFPC2': ['HST/WFPC2', 'WFPC2'],
                    'ACSHRC': ['HST/ACS', 'HRC'],
                    'ACSWFC': ['HST/ACS', 'WFC'],
                    'WFC3UV': ['HST/WFC3', 'UVIS'],
                    'WFC3IR': ['HST/WFC3', 'IR'],
                    'NRCSW': ['JWST/NIRCam', 'NRCSW'],
                    'NRCA1': ['JWST/NIRCam', 'A1'],
                    'NRCA2': ['JWST/NIRCam', 'A2'],
                    'NRCA3': ['JWST/NIRCam', 'A3'],
                    'NRCA4': ['JWST/NIRCam', 'A4'],
                    'NRCB1': ['JWST/NIRCam', 'B1'],
                    'NRCB2': ['JWST/NIRCam', 'B2'],
                    'NRCB3': ['JWST/NIRCam', 'B3'],
                    'NRCB4': ['JWST/NIRCam', 'B4'],
                    'NRCAL': ['JWST/NIRCam', 'A5'],
                    'NRCBL': ['JWST/NIRCam', 'B5'],
                    'NIRISS': ['JWST/NIRISS', 'NIRISS'],
                    'MIRI': ['JWST/MIRI', 'MIRIM']}

    try:
        inst_det = detector_map[detector]
    except KeyError:
        raise ValueError(f'Unknown detector {detector}.')

    if inst_det[1] in ('WFC', 'UVIS'):
        chip = 2 if sci_exten == 1 else 1
        inst_det[1] = f'{inst_det[1]}{chip}'

    if inst_det[1] == 'WFPC2':
        wfpc2_map = {1: 'PC', 2: 'WF2', 3: 'WF3', 4: 'WF4'}
        inst_det[1] = wfpc2_map[sci_exten]

    if inst_det[1] == 'NRCSW':
        sw_map = {1: 'A1', 2: 'A2', 3: 'A3', 4: 'A4',
                  5: 'B1', 6: 'B2', 7: 'B3', 8: 'B4'}
        inst_det[1] = sw_map[sci_exten]

    meta = {'STDPSF': filename,
            'instrument': inst_det[0],
            'detector': inst_det[1],
            'filter': filter_name}

    return meta


def is_fits(origin, filepath, fileobj, *args, **kwargs):
    """
    Determine whether `origin` is a FITS file.

    Parameters
    ----------
    origin : str or readable file-like
        Path or file object containing a potential FITS file.

    Returns
    -------
    is_fits : bool
        Returns `True` if the given file is a FITS file.
    """
    if filepath is not None:
        extens = ('.fits', '.fits.gz', '.fit', '.fit.gz', '.fts', '.fts.gz')
        return filepath.lower().endswith(extens)
    return isinstance(args[0], HDUList)


with registry.delay_doc_updates(GriddedPSFModel):
    registry.register_reader('stdpsf', GriddedPSFModel, stdpsf_reader)
    registry.register_identifier('stdpsf', GriddedPSFModel, is_fits)