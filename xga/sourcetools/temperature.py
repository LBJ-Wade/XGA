#  This code is a part of XMM: Generate and Analyse (XGA), a module designed for the XMM Cluster Survey (XCS).
#  Last modified by David J Turner (david.turner@sussex.ac.uk) 01/02/2021, 16:44. Copyright (c) David J Turner

from typing import Tuple, Union, List
from warnings import warn

import numpy as np
from astropy.units import Quantity

from .. import NUM_CORES
from ..imagetools.misc import pix_deg_scale
from ..imagetools.profile import annular_mask
from ..samples import BaseSample
from ..sas import region_setup
from ..sources import BaseSource
from ..xspec.fit import single_temp_apec_profile


def _snr_bins(source: BaseSource, outer_rad: Quantity, min_snr: float, min_width: Quantity, lo_en: Quantity,
              hi_en: Quantity, obs_id: str = None, inst: str = None, psf_corr: bool = False, psf_model: str = "ELLBETA",
              psf_bins: int = 4, psf_algo: str = "rl", psf_iter: int = 15,
              allow_negative: bool = False, exp_corr: bool = True) -> Tuple[Quantity, np.ndarray, int]:
    """
    An internal function that will find the radii required to create annuli with a certain minimum signal to noise
    and minimum annulus width.

    :param BaseSource source: The source object which needs annuli generating for it.
    :param Quantity outer_rad: The outermost radius of the source region we will generate annuli within.
    :param float min_snr: The minimum signal to noise which is allowable in a given annulus.
    :param Quantity min_width: The minimum allowable width of the annuli. This can be set to try and avoid
        PSF effects.
    :param Quantity lo_en: The lower energy bound of the ratemap to use for the signal to noise calculations.
    :param Quantity hi_en: The upper energy bound of the ratemap to use for the signal to noise calculations.
    :param str obs_id: An ObsID of a specific ratemap to use for the SNR calculations. Default is None, which
            means the combined ratemap will be used. Please note that inst must also be set to use this option.
    :param str inst: The instrument of a specific ratemap to use for the SNR calculations. Default is None, which
        means the combined ratemap will be used.
    :param bool psf_corr: Sets whether you wish to use a PSF corrected ratemap or not.
    :param str psf_model: If the ratemap you want to use is PSF corrected, this is the PSF model used.
    :param int psf_bins: If the ratemap you want to use is PSF corrected, this is the number of PSFs per
        side in the PSF grid.
    :param str psf_algo: If the ratemap you want to use is PSF corrected, this is the algorithm used.
    :param int psf_iter: If the ratemap you want to use is PSF corrected, this is the number of iterations.
    :param bool allow_negative: Should pixels in the background subtracted count map be allowed to go below
        zero, which results in a lower signal to noise (and can result in a negative signal to noise).
    :param bool exp_corr: Should signal to noises be measured with exposure time correction, default is True. I
            recommend that this be true for combined observations, as exposure time could change quite dramatically
            across the combined product.
    :return: The radii of the requested annuli, the final snr values, and the original maximum number
        based on min_width.
    :rtype: Tuple[Quantity, np.ndarray, int]
    """
    # Parsing the ObsID and instrument options, see if they want to use a specific ratemap
    if all([obs_id is None, inst is None]):
        # Here the user hasn't set ObsID or instrument, so we use the combined data
        rt = source.get_combined_ratemaps(lo_en, hi_en, psf_corr, psf_model, psf_bins, psf_algo, psf_iter)
        interloper_mask = source.get_interloper_mask()
    elif all([obs_id is not None, inst is not None]):
        # Both ObsID and instrument have been set by the user
        rt = source.get_ratemaps(obs_id, inst, lo_en, hi_en, psf_corr, psf_model, psf_bins, psf_algo, psf_iter)
        interloper_mask = source.get_interloper_mask(obs_id)

    # Just making sure our relevant distances are in the same units, so that we can convert to pixels
    outer_rad = source.convert_radius(outer_rad, 'deg')
    min_width = source.convert_radius(min_width, 'deg')

    # Using the ratemap to get a conversion factor from pixels to degrees, though we will use it
    #  the other way around
    pix_to_deg = pix_deg_scale(source.default_coord, rt.radec_wcs)

    # Making sure to go up to the whole number, pixels have to be integer of course and I think its
    #  better to err on the side of caution here and make things slightly wider than requested
    outer_rad = int(np.ceil(outer_rad/pix_to_deg).value)
    min_width = int(np.ceil(min_width/pix_to_deg).value)

    # The maximum possible number of annuli, based on the input outer radius and minimum width
    # We have already made sure that the outer radius and minimum width allowed are integers by using
    #  np.ceil, so we know max_ann is going to be a whole number of annuli
    max_ann = int(outer_rad/min_width)

    # These are the initial bins, with imposed minimum width, I have to add one to max_ann because linspace wants the
    #  total number of values to generate, and while there are max_ann annuli, there are max_ann+1 radial boundaries
    init_rads = np.linspace(0, outer_rad, max_ann+1).astype(int)

    # Converts the source's default analysis coordinates to pixels
    pix_centre = rt.coord_conv(source.default_coord, 'pix')
    # Sets up a mask to correct for interlopers and weird edge effects
    corr_mask = interloper_mask*rt.edge_mask

    # Setting up our own background region
    back_inn_rad = np.array([np.ceil(source.background_radius_factors[0] * outer_rad)]).astype(int)
    back_out_rad = np.array([np.ceil(source.background_radius_factors[1] * outer_rad)]).astype(int)

    # Using my annular mask function to make a nice background region, which will be corrected for instrumental
    #  stuff and interlopers in a second
    back_mask = annular_mask(pix_centre, back_inn_rad, back_out_rad, rt.shape) * corr_mask

    # Generates the requested annular masks, making sure to apply the correcting mask
    ann_masks = annular_mask(pix_centre, init_rads[:-1], init_rads[1:], rt.shape)*corr_mask[..., None]

    # This will be modified by the loop until it describes annuli which all have an acceptable signal to noise
    cur_rads = init_rads.copy()
    acceptable = False
    while not acceptable:
        # How many annuli are there at this point in the loop?
        cur_num_ann = ann_masks.shape[2]

        # Just a list for the snrs to live in
        snrs = []
        for i in range(cur_num_ann):
            # We're calling the signal to noise calculation method of the ratemap for all of our annuli
            snrs.append(rt.signal_to_noise(ann_masks[:, :, i], back_mask, exp_corr, allow_negative))
        # Becomes a numpy array because they're nicer to work with
        snrs = np.array(snrs)
        # We find any indices of the array (== annuli) where the signal to noise is not above our minimum
        bad_snrs = np.where(snrs < min_snr)[0]

        # If there are no annuli below our signal to noise threshold then all is good and joyous and we accept
        #  the current radii
        if len(bad_snrs) == 0:
            acceptable = True
        # We work from the outside of the bad list inwards, and if the outermost bad bin is the one right on the
        #  end of the SNR profile, then we merge that leftwards into the N-1th annuli
        elif len(bad_snrs) != 0 and bad_snrs[-1] == cur_num_ann-1:
            cur_rads = np.delete(cur_rads, -2)
            ann_masks = annular_mask(pix_centre, cur_rads[:-1], cur_rads[1:], rt.shape) * corr_mask[..., None]
        # Otherwise if the outermost bad annulus is NOT right at the end of the profile, we merge to the right
        else:
            cur_rads = np.delete(cur_rads, bad_snrs[-1])
            ann_masks = annular_mask(pix_centre, cur_rads[:-1], cur_rads[1:], rt.shape) * corr_mask[..., None]

        if ann_masks.shape[2] == 4 and not acceptable:
            warn("The requested annuli for {s} cannot be created, the data quality is too low. As such a set "
                 "of four annuli will be returned".format(s=source.name))
            break

    # Now of course, pixels must become a more useful unit again
    final_rads = (Quantity(cur_rads, 'pix') * pix_to_deg).to("arcsec")

    return final_rads, snrs, max_ann


def min_snr_proj_temp_prof(sources: Union[BaseSource, BaseSample], outer_radii: Union[Quantity, List[Quantity]],
                           min_snr: float = 20, min_width: Quantity = Quantity(20, 'arcsec'), use_combined: bool = True,
                           use_worst: bool = False, lo_en: Quantity = Quantity(0.5, 'keV'),
                           hi_en: Quantity = Quantity(2, 'keV'), psf_corr: bool = False, psf_model: str = "ELLBETA",
                           psf_bins: int = 4, psf_algo: str = "rl", psf_iter: int = 15, allow_negative: bool = False,
                           exp_corr: bool = True, group_spec: bool = True, min_counts: int = 5, min_sn: float = None,
                           over_sample: float = None, one_rmf: bool = True, num_cores: int = NUM_CORES):
    """
    This is a convenience function that allows you to quickly and easily start measuring projected
    temperature profiles of galaxy clusters, deciding on the annular bins using signal to noise measurements
    from photometric products. This function calls single_temp_apec_profile, but doesn't expose all of the more
    in depth variables, so if you want more control then use single_temp_apec_profile directly. The projected
    temperature profiles which are generated are added to their source's storage structure.

    :param sources:
    :param str/Quantity outer_radii: The name or value of the outer radius to use for the generation of
        the spectrum (for instance 'r200' would be acceptable for a GalaxyCluster, or Quantity(1000, 'kpc')). If
        'region' is chosen (to use the regions in region files), then any inner radius will be ignored. If you are
        generating for multiple sources then you can also pass a Quantity with one entry per source.
    :param float min_snr: The minimum signal to noise which is allowable in a given annulus.
    :param Quantity min_width: The minimum allowable width of an annulus. The default is set to 20 arcseconds to try
        and avoid PSF effects.
    :param bool use_combined: If True then the combined RateMap will be used for signal to noise annulus
        calculations, this is overridden by use_worst.
    :param bool use_worst: If True then the worst observation of the cluster (ranked by global signal to noise) will
        be used for signal to noise annulus calculations.
    :param Quantity lo_en: The lower energy bound of the ratemap to use for the signal to noise calculations.
    :param Quantity hi_en: The upper energy bound of the ratemap to use for the signal to noise calculations.
    :param bool psf_corr: Sets whether you wish to use a PSF corrected ratemap or not.
    :param str psf_model: If the ratemap you want to use is PSF corrected, this is the PSF model used.
    :param int psf_bins: If the ratemap you want to use is PSF corrected, this is the number of PSFs per
        side in the PSF grid.
    :param str psf_algo: If the ratemap you want to use is PSF corrected, this is the algorithm used.
    :param int psf_iter: If the ratemap you want to use is PSF corrected, this is the number of iterations.
    :param bool allow_negative: Should pixels in the background subtracted count map be allowed to go below
        zero, which results in a lower signal to noise (and can result in a negative signal to noise).
    :param bool exp_corr: Should signal to noises be measured with exposure time correction, default is True. I
            recommend that this be true for combined observations, as exposure time could change quite dramatically
            across the combined product.
    :param bool group_spec: A boolean flag that sets whether generated spectra are grouped or not.
    :param float min_counts: If generating a grouped spectrum, this is the minimum number of counts per channel.
        To disable minimum counts set this parameter to None.
    :param float min_sn: If generating a grouped spectrum, this is the minimum signal to noise in each channel.
        To disable minimum signal to noise set this parameter to None.
    :param float over_sample: The minimum energy resolution for each group, set to None to disable. e.g. if
        over_sample=3 then the minimum width of a group is 1/3 of the resolution FWHM at that energy.
    :param bool one_rmf: This flag tells the method whether it should only generate one RMF for a particular
        ObsID-instrument combination - this is much faster in some circumstances, however the RMF does depend
        slightly on position on the detector.
    :param int num_cores: The number of cores to use (if running locally), default is set to 90% of available.
    """

    if outer_radii != 'region':
        inn_rad_vals, out_rad_vals = region_setup(sources, outer_radii, Quantity(0, 'arcsec'), True, '')[1:]
    else:
        raise NotImplementedError("I don't currently support fitting region spectra")

    if all([use_combined, use_worst]):
        warn("You have passed both use_combined and use_worst as True. use_worst overrides use_combined, so the "
             "worst observation for each source will be used to decide on the annuli.")
        use_combined = False
    elif all([not use_combined, not use_worst]):
        warn("You have passed both use_combined and use_worst as False. One of them must be True, so here we default"
             " to using the combined data to decide on the annuli.")
        use_combined = True

    all_rads = []
    for src_ind, src in enumerate(sources):
        if use_combined:
            # This is the simplest option, we just use the combined ratemap to decide on the annuli with minimum SNR
            rads, snrs, ma = _snr_bins(src, out_rad_vals[src_ind], min_snr, min_width, lo_en, hi_en, psf_corr=psf_corr,
                                       psf_model=psf_model, psf_bins=psf_bins, psf_algo=psf_algo, psf_iter=psf_iter,
                                       allow_negative=allow_negative, exp_corr=exp_corr)
        else:
            # This way is slightly more complicated, but here we use the worst observation (ranked by global
            #  signal to noise).
            # The return for this function is ranked worst to best, so we grab the first row (which is an ObsID and
            #  instrument), then call _snr_bins with that one
            lowest_ranked = src.snr_ranking(out_rad_vals[src_ind], lo_en, hi_en, allow_negative)[0][0, :]
            rads, snrs, ma = _snr_bins(src, out_rad_vals[src_ind], min_snr, min_width, lo_en, hi_en, lowest_ranked[0],
                                       lowest_ranked[1], psf_corr, psf_model, psf_bins, psf_algo, psf_iter,
                                       allow_negative, exp_corr)

        # Shoves the annuli we've decided upon into a list for single_temp_apec_profile to use
        all_rads.append(rads)

    single_temp_apec_profile(sources, all_rads, group_spec=group_spec, min_counts=min_counts, min_sn=min_sn,
                             over_sample=over_sample, one_rmf=one_rmf, num_cores=num_cores)


def onion_deproj_temp_prof():
    raise NotImplementedError("I'll begin work on this soon")





