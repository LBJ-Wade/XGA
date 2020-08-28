#  This code is a part of XMM: Generate and Analyse (XGA), a module designed for the XMM Cluster Survey (XCS).
#  Last modified by David J Turner (david.turner@sussex.ac.uk) 28/08/2020, 17:45. Copyright (c) David J Turner

from multiprocessing.dummy import Pool
from typing import List, Tuple

import numpy as np
from astropy.units import Quantity, pix
from tqdm import tqdm

from xga.exceptions import NoRegionsError
from xga.imagetools.profile import radial_brightness
from xga.sas import evselect_spectrum
from xga.sources import GalaxyCluster
from xga.utils import NUM_CORES, COMPUTE_MODE
from xga.xspec.fakeit import cluster_cr_conv


def radial_data_stack(sources: List[GalaxyCluster], scale_radius: str = "r200", use_peak: bool = True,
                      radii: np.ndarray = np.linspace(0, 1, 20), lo_en: Quantity = Quantity(0.5, 'keV'),
                      hi_en: Quantity = Quantity(2.0, 'keV'), custom_temps: Quantity = None,
                      psf_corr: bool = False, psf_model: str = "ELLBETA", psf_bins: int = 4,
                      psf_algo: str = "rl", psf_iter: int = 15, num_cores: int = NUM_CORES) \
        -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Creates and scales radial brightness profiles for a set of galaxy clusters so that they can be combined
    and compared, like for like. This particular function does not fit models, and outputs a mean brightness
    profile, as well as the scaled stack data and covariance matrices. This is based on the method in
    https://doi.org/10.1093/mnras/stv1366, though modified to work with profiles rather than 2D images.
    :param List[GalaxyCluster] sources: The source objects that will contribute to the stacked brightness profile.
    :param str scale_radius: The overdensity radius to scale the cluster radii by, all GalaxyCluster objects must
    have an entry for this radius.
    :param bool use_peak: Controls whether the peak position is used as the centre of the brightness profile
    for each GalaxyCluster object.
    :param ndarray radii: The radii (in units of scale_radius) at which to measure and stack surface brightness.
    :param Quantity lo_en: The lower energy limit of the data that goes into the stacked profiles.
    :param Quantity hi_en: The upper energy limit of the data that goes into the stacked profiles.
    :param Quantity custom_temps: Temperatures at which to calculate conversion factors for each cluster
    in sources, they will overwrite any temperatures measured by XGA. A single temperature can be passed to be used
    for all clusters in sources. If None, appropriate temperatures will be retrieved from the source objects.
    :param bool psf_corr: If True, PSF corrected ratemaps will be used to make the brightness profile stack.
    :param str psf_model: If PSF corrected, the PSF model used.
    :param int psf_bins: If PSF corrected, the number of bins per side.
    :param str psf_algo: If PSF corrected, the algorithm used.
    :param int psf_iter: If PSF corrected, the number of algorithm iterations.
    :param int num_cores: The number of cores to use when calculating the brightness profiles, the default is 90%
    of available cores.
    :return: This function returns the average profile, the scaled brightness profiles with the cluster
    changing along the y direction and the bin changing along the x direction, an array of the radii at which the
    brightness was measured (in units of scale_radius), and finally the covariance matrix and normalised
    covariance matrix.
    :rtype: Tuple[ndarray, ndarray, ndarray, ndarray, ndarray]
    """
    def construct_profile(src: GalaxyCluster, src_id: int, lower: Quantity, upper: Quantity) -> Tuple[Quantity, int]:
        """
        Constructs a brightness profile for the given galaxy cluster, and interpolates to find values
        at the requested radii in units of scale_radius.
        :param GalaxyCluster src: The GalaxyCluster to construct a profile for.
        :param int src_id: An identifier that enables the constructed profile to be placed
        correctly in the results array.
        :param Quantity lower: The lower energy limit to use.
        :param Quantity upper: The higher energy limit to use.
        :return: The profile and the cluster identifier.
        :rtype: Tuple[Quantity, int]
        """
        # The storage key is different based on whether the user wishes to generate profiles from PSF corrected
        #  ratemaps or not.
        if not psf_corr:
            storage_key = "bound_{l}-{u}".format(l=lower.value, u=upper.value)
        else:
            storage_key = "bound_{l}-{u}_{m}_{n}_{a}{i}".format(l=lower.value, u=upper.value, m=psf_model,
                                                                n=psf_bins, a=psf_algo, i=psf_iter)

        # Retrieving the relevant ratemap object, as well as masks
        rt = [rt[-1] for rt in src.get_products("combined_ratemap", just_obj=False) if storage_key in rt][0]
        source_mask, background_mask = src.get_mask(scale_radius)

        # The user can choose to use the original user passed coordinates, or the X-ray centroid
        if use_peak:
            pix_peak = rt.coord_conv(src.peak, pix)
        else:
            pix_peak = rt.coord_conv(src.ra_dec, pix)

        rad = Quantity(src.get_source_region(scale_radius)[0].to_pixel(rt.radec_wcs).radius, pix)
        brightness, cen_rad, bck = radial_brightness(rt, source_mask, background_mask, pix_peak,
                                                     rad, src.redshift, pix, src.cosmo)

        # Subtracting the background in the simplest way possible
        brightness -= bck
        # If a value goes below zero currently setting it to 0, this is BAD
        brightness[brightness < 0] = 0

        # Calculates the value of pixel radii in terms of the scale radii
        scaled_radii = (cen_rad / rad).value

        # Interpolating brightness profile values at the radii passed by the user
        interp_brightness = np.interp(radii, scaled_radii, brightness)

        return interp_brightness, src_id

    # This function isn't split out to be submitted to HPC jobs, unlike SAS tasks, so I make sure the num
    #  of cores is set to 1 to minimise resource usage.
    if COMPUTE_MODE != "local":
        num_cores = 1

    # Checking that all the sources are GalaxyClusters
    if not all([isinstance(s, GalaxyCluster) for s in sources]):
        raise TypeError("Currently only GalaxyCluster source objects may be analysed in this way.")

    # Checking that every single GalaxyCluster object was supplied with the scale radius chosen by the user
    if scale_radius.lower() == "r200":
        rad_check = [s.r200 is not None for s in sources]
    elif scale_radius.lower() == "r500":
        rad_check = [s.r500 is not None for s in sources]
    elif scale_radius.lower() == "r2500":
        rad_check = [s.r2500 is not None for s in sources]
    else:
        raise ValueError("{0} is not an acceptable overdensity radius, please use r200, r500, or "
                         "r2500.".format(scale_radius))

    if not all(rad_check):
        raise NoRegionsError("Some GalaxyCluster objects are missing the {} region".format(scale_radius))

    sb = np.zeros((len(sources), len(radii)))
    # Sets up a multiprocessing pool
    with tqdm(total=len(sources), desc="Generating Brightness Profiles") as onwards, Pool(num_cores) as pool:
        def callback(results):
            nonlocal sb
            nonlocal onwards
            b, s_id = results
            sb[s_id, :] = b
            onwards.update(1)

        def err_callback(err):
            onwards.update()
            raise err

        for s_ind, s in enumerate(sources):
            pool.apply_async(construct_profile, callback=callback, error_callback=err_callback,
                             args=(s, s_ind, lo_en, hi_en))
        pool.close()
        pool.join()
        onwards.close()

    # Now, we have all the brightness values at common radii (in units of R200 so scaled properly), now we have
    #  to weight the SB values so they are directly comparable. This accounts for redshift, nH, and sort-of for
    #  the temperature of each cluster.
    # First must make sure we have generated spectra for all the clusters, as we need the ARFs and RMFs to
    #  simulate spectra and calculate conversion values
    evselect_spectrum(sources, scale_radius)  # Use our standard setting for spectra generation

    # Calculate all the conversion factors
    if custom_temps is not None:
        cluster_cr_conv(sources, scale_radius, custom_temps)
    else:
        temps = Quantity([source.get_temperature(scale_radius, "tbabs*apec")[0] for source in sources], 'keV')
        cluster_cr_conv(sources, scale_radius, temps)

    combined_factors = []
    # Now to generate a combined conversion factor
    for source in sources:
        combined_factors.append(source.combined_conv_factor(scale_radius, lo_en, hi_en).value)

    combined_factors = np.array(combined_factors)
    # Multiplies each cluster profile by the matching conversion factor to go from countrate to luminosity
    scaled_brightness = (sb.T * combined_factors).T

    # Finds the highest value in the profile of each cluster
    max_brs = np.max(scaled_brightness, axis=1)
    # Finds the mean of the maximum values and calculates scaling factors so that the maximum
    #  value in each profile is now equal to the average
    scale_factors = max_brs.mean() / max_brs
    # Applied the rescaling factors
    scaled_brightness = (scaled_brightness.T * scale_factors).T

    # Calculates normalised and the usual covariance matrices
    norm_cov = np.corrcoef(scaled_brightness, rowvar=False)
    cov = np.cov(scaled_brightness, rowvar=False)

    average_profile = np.mean(scaled_brightness, axis=0)
    return average_profile, scaled_brightness, radii, cov, norm_cov

