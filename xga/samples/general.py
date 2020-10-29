#  This code is a part of XMM: Generate and Analyse (XGA), a module designed for the XMM Cluster Survey (XCS).
#  Last modified by David J Turner (david.turner@sussex.ac.uk) 29/10/2020, 11:07. Copyright (c) David J Turner

import numpy as np
from astropy.cosmology import Planck15
from astropy.units import Quantity, Unit
from tqdm import tqdm

from .base import BaseSample
from ..imagetools.psf import rl_psf
from ..sources.general import PointSource


class PointSample(BaseSample):
    def __init__(self, ra: np.ndarray, dec: np.ndarray, redshift: np.ndarray = None, name: np.ndarray = None,
                 point_radius: Quantity = None, use_peak=False, peak_lo_en=Quantity(0.5, "keV"),
                 peak_hi_en=Quantity(2.0, "keV"), back_inn_rad_factor=1.05, back_out_rad_factor=1.5,
                 cosmology=Planck15, load_fits=False, no_prog_bar: bool = False, psf_corr: bool = False):

        # I don't like having this here, but it does avoid a circular import problem
        from xga.sas import evselect_image, eexpmap, emosaic

        # Using the super defines BaseSources and stores them in the self._sources dictionary
        super().__init__(ra, dec, redshift, name, cosmology, load_products=True, load_fits=False,
                         no_prog_bar=no_prog_bar)

        print("Pre-generating necessary products")
        evselect_image(self, peak_lo_en, peak_hi_en)
        eexpmap(self, peak_lo_en, peak_hi_en)
        emosaic(self, "image", peak_lo_en, peak_hi_en)
        emosaic(self, "expmap", peak_lo_en, peak_hi_en)

        del self._sources
        self._sources = {}

        self._point_radii = []
        dec_lb = tqdm(desc="Setting up Point Sources", total=len(ra), disable=no_prog_bar)
        for ind, rd in enumerate(self._ra_decs):
            r, d = rd
            z = self._redshifts[ind]
            n = self._names[ind]
            if point_radius is not None:
                pr = point_radius[self._accepted_inds[ind]]
            else:
                pr = None

            self._sources[n] = PointSource(r, d, z, n, pr, use_peak, peak_lo_en, peak_hi_en, back_inn_rad_factor,
                                           back_out_rad_factor, cosmology, True, load_fits)
            pr = self._sources[n].point_radius
            self._point_radii.append(pr.value)
            dec_lb.update(1)
        dec_lb.close()

        # I'm not worried about pr never having existed - declaration of a sample will fail
        #  if not data is passed.
        self._pr_unit = pr.unit

        # I don't offer the user choices as to the configuration for PSF correction at the moment
        if psf_corr:
            rl_psf(self, lo_en=peak_lo_en, hi_en=peak_hi_en)

    @property
    def point_radii(self) -> Quantity:
        """
        Property getter for the radii of the regions used for analysis of the point sources in this sample.
        :return: A non-scalar Quantity of the point source radii used for analysis of the point sources in
        this sample.
        :rtype: Quantity
        """
        return Quantity(self._point_radii, self._pr_unit)

    @property
    def point_radii_unit(self) -> Unit:
        """
        Property getter for the unit which the point radii values are stored in.
        :return: The unit that the point radii are stored in.
        :rtype: Unit
        """
        return self._pr_unit

    def _del_data(self, key: int):
        """
        Specific to the PointSample class, this deletes the extra data stored during the initialisation
        of this type of sample.
        :param int key: The index or name of the source to delete.
        """
        del self._point_radii[key]








