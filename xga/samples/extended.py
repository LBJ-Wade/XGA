#  This code is a part of XMM: Generate and Analyse (XGA), a module designed for the XMM Cluster Survey (XCS).
#  Last modified by David J Turner (david.turner@sussex.ac.uk) 17/02/2021, 08:44. Copyright (c) David J Turner

from typing import Union

import numpy as np
from astropy.cosmology import Planck15
from astropy.units import Quantity
from tqdm import tqdm

from .base import BaseSample
from ..exceptions import PeakConvergenceFailedError, ModelNotAssociatedError, ParameterNotAssociatedError
from ..relations.fit import *
from ..sources.extended import GalaxyCluster


# Names are required for the ClusterSample because they'll be used to access specific cluster objects
class ClusterSample(BaseSample):
    """
    A sample class to be used for declaring and analysing populations of galaxy clusters, with many cluster-science
    specific functions, such as the ability to create common scaling relations.
    """
    def __init__(self, ra: np.ndarray, dec: np.ndarray, redshift: np.ndarray, name: np.ndarray, r200: Quantity = None,
                 r500: Quantity = None, r2500: Quantity = None, richness: np.ndarray = None,
                 richness_err: np.ndarray = None, wl_mass: Quantity = None, wl_mass_err: Quantity = None,
                 custom_region_radius: Quantity = None, use_peak: bool = True,
                 peak_lo_en: Quantity = Quantity(0.5, "keV"), peak_hi_en: Quantity = Quantity(2.0, "keV"),
                 back_inn_rad_factor: float = 1.05, back_out_rad_factor: float = 1.5, cosmology=Planck15,
                 load_fits: bool = False, clean_obs: bool = True, clean_obs_reg: str = "r200",
                 clean_obs_threshold: float = 0.3, no_prog_bar: bool = False, psf_corr: bool = False):

        # I don't like having this here, but it does avoid a circular import problem
        from xga.sas import evselect_image, eexpmap, emosaic

        # Using the super defines BaseSources and stores them in the self._sources dictionary
        super().__init__(ra, dec, redshift, name, cosmology, load_products=True, load_fits=False,
                         no_prog_bar=no_prog_bar)

        # This part is super useful - it is much quicker to use the base sources to generate all
        #  necessary ratemaps, as we can do it in parallel for the entire sample, rather than one at a time as
        #  might be necessary for peak finding in the cluster init.
        # TODO Make this logging rather than just printing
        print("Pre-generating necessary products")
        evselect_image(self, peak_lo_en, peak_hi_en)
        eexpmap(self, peak_lo_en, peak_hi_en)
        emosaic(self, "image", peak_lo_en, peak_hi_en)
        emosaic(self, "expmap", peak_lo_en, peak_hi_en)

        # Now that we've made those images the BaseSource objects aren't required anymore, we're about
        #  to define GalaxyClusters
        del self._sources
        self._sources = {}

        dec_lb = tqdm(desc="Setting up Galaxy Clusters", total=len(self.names), disable=no_prog_bar)
        for ind, r in enumerate(ra):
            # Just splitting out relevant values for this particular cluster so the object declaration isn't
            #  super ugly.
            d = dec[ind]
            z = redshift[ind]
            # The replace is there because source declaration removes spaces from any passed names,
            n = name[ind].replace(' ', '')
            # Declaring the BaseSample higher up weeds out those objects that aren't in any XMM observations
            #  So we want to check that the current object name is in the list of objects that have data
            if n in self.names:
                # I know this code is a bit ugly, but oh well
                if r200 is not None:
                    r2 = r200[ind]
                else:
                    r2 = None
                if r500 is not None:
                    r5 = r500[ind]
                else:
                    r5 = None
                if r2500 is not None:
                    r25 = r2500[ind]
                else:
                    r25 = None
                if custom_region_radius is not None:
                    cr = custom_region_radius[ind]
                else:
                    cr = None

                # Here we check the options that are allowed to be None
                if richness is not None:
                    lam = richness[ind]
                    lam_err = richness_err[ind]
                else:
                    lam = None
                    lam_err = None

                if wl_mass is not None:
                    wlm = wl_mass[ind]
                    wlm_err = wl_mass_err[ind]
                else:
                    wlm = None
                    wlm_err = None

                # Will definitely load products (the True in this call), because I just made sure I generated a
                #  bunch to make GalaxyCluster declaration quicker
                try:
                    self._sources[n] = GalaxyCluster(r, d, z, n, r2, r5, r25, lam, lam_err, wlm, wlm_err, cr,
                                                     use_peak, peak_lo_en, peak_hi_en, back_inn_rad_factor,
                                                     back_out_rad_factor, cosmology, True, load_fits, clean_obs,
                                                     clean_obs_reg, clean_obs_threshold, False)
                except PeakConvergenceFailedError:
                    warn("The peak finding algorithm has not converged for {}, using user "
                         "supplied coordinates".format(n))
                    self._sources[n] = GalaxyCluster(r, d, z, n, r2, r5, r25, lam, lam_err, wlm, wlm_err, cr, False,
                                                     peak_lo_en, peak_hi_en, back_inn_rad_factor, back_out_rad_factor,
                                                     cosmology, True, load_fits, clean_obs, clean_obs_reg,
                                                     clean_obs_threshold, False)

            dec_lb.update(1)
        dec_lb.close()

        # And again I ask XGA to generate the merged images and exposure maps, in case any sources have been
        #  cleaned and had data removed
        if clean_obs:
            emosaic(self, "image", peak_lo_en, peak_hi_en)
            emosaic(self, "expmap", peak_lo_en, peak_hi_en)

        # TODO Reconsider if this is even necessary, the data that has been removed should by definition
        #  not really include the peak
        # Updates with new peaks
        if clean_obs and use_peak:
            for n in self.names:
                # If the source in question has had data removed
                if self._sources[n].disassociated:
                    try:
                        en_key = "bound_{0}-{1}".format(peak_lo_en.to("keV").value,
                                                        peak_hi_en.to("keV").value)
                        rt = self._sources[n].get_products("combined_ratemap", extra_key=en_key)[0]
                        peak = self._sources[n].find_peak(rt)
                        self._sources[n].peak = peak[0]
                    except PeakConvergenceFailedError:
                        pass

        # I don't offer the user choices as to the configuration for PSF correction at the moment
        if psf_corr:
            from ..imagetools.psf import rl_psf
            rl_psf(self, lo_en=peak_lo_en, hi_en=peak_hi_en)

    @property
    def r200_snr(self) -> np.ndarray:
        """
        Fetches and returns the R200 signal to noises from the constituent sources.

        :return: The signal to noise ration calculated at the R200.
        :rtype: np.ndarray
        """
        snrs = []
        for s in self:
            try:
                snrs.append(s.get_snr("r200"))
            except ValueError:
                snrs.append(None)
        return np.array(snrs)

    @property
    def r500_snr(self) -> np.ndarray:
        """
        Fetches and returns the R500 signal to noises from the constituent sources.

        :return: The signal to noise ration calculated at the R500.
        :rtype: np.ndarray
        """
        snrs = []
        for s in self:
            try:
                snrs.append(s.get_snr("r500"))
            except ValueError:
                snrs.append(None)
        return np.array(snrs)

    @property
    def r2500_snr(self) -> np.ndarray:
        """
        Fetches and returns the R2500 signal to noises from the constituent sources.

        :return: The signal to noise ration calculated at the R2500.
        :rtype: np.ndarray
        """
        snrs = []
        for s in self:
            try:
                snrs.append(s.get_snr("r2500"))
            except ValueError:
                snrs.append(None)
        return np.array(snrs)

    @property
    def richness(self) -> Quantity:
        """
        Provides the richnesses of the clusters in this sample, if they were passed in on definition.

        :return: A unitless Quantity object of the richnesses and their error(s).
        :rtype: Quantity
        """
        rs = []
        for gcs in self._sources.values():
            rs.append(gcs.richness.value)

        rs = np.array(rs)

        # We're going to throw an error if all the richnesses are NaN, because obviously something is wrong
        check_rs = rs[~np.isnan(rs)]
        if len(check_rs) == 0:
            raise ValueError("All richnesses appear to be NaN.")

        return Quantity(rs)

    @property
    def wl_mass(self) -> Quantity:
        """
        Provides the weak lensing masses of the clusters in this sample, if they were passed in on definition.

        :return: A Quantity object of the WL masses and their error(s), in whatever units they were when
        they were passed in originally.
        :rtype: Quantity
        """
        wlm = []
        for gcs in self._sources.values():
            wlm.append(gcs.weak_lensing_mass.value)
            wlm_unit = gcs.weak_lensing_mass.unit

        wlm = np.array(wlm)

        # We're going to throw an error if all the weak lensing masses are NaN, because obviously something is wrong
        check_wlm = wlm[~np.isnan(wlm)]
        if len(check_wlm) == 0:
            raise ValueError("All weak lensing masses appear to be NaN.")

        return Quantity(wlm, wlm_unit)

    @property
    def r200(self) -> Quantity:
        """
        Returns all the R200 values passed in on declaration, but in units of kpc.

        :return: A quantity of R200 values.
        :rtype: Quantity
        """
        rads = []
        for gcs in self._sources.values():
            rad = gcs.get_radius('r200', 'kpc')
            if rad is None:
                rads.append(np.NaN)
            else:
                rads.append(rad.value)

        rads = np.array(rads)
        check_rads = rads[~np.isnan(rads)]
        if len(check_rads) == 0:
            raise ValueError("All R200 values appear to be NaN.")

        return Quantity(rads, 'kpc')

    @property
    def r500(self) -> Quantity:
        """
        Returns all the R500 values passed in on declaration, but in units of kpc.

        :return: A quantity of R500 values.
        :rtype: Quantity
        """
        rads = []
        for gcs in self._sources.values():
            rad = gcs.get_radius('r500', 'kpc')
            if rad is None:
                rads.append(np.NaN)
            else:
                rads.append(rad.value)

        rads = np.array(rads)
        check_rads = rads[~np.isnan(rads)]
        if len(check_rads) == 0:
            raise ValueError("All R500 values appear to be NaN.")

        return Quantity(rads, 'kpc')

    @property
    def r2500(self) -> Quantity:
        """
        Returns all the R2500 values passed in on declaration, but in units of kpc.

        :return: A quantity of R2500 values.
        :rtype: Quantity
        """
        rads = []
        for gcs in self._sources.values():
            rad = gcs.get_radius('r2500', 'kpc')
            if rad is None:
                rads.append(np.NaN)
            else:
                rads.append(rad.value)

        rads = np.array(rads)
        check_rads = rads[~np.isnan(rads)]
        if len(check_rads) == 0:
            raise ValueError("All R2500 values appear to be NaN.")

        return Quantity(rads, 'kpc')

    def Tx(self, model: str = 'tbabs*apec', outer_radius: Union[str, Quantity] = 'r500',
           inner_radius: Union[str, Quantity] = Quantity(0, 'arcsec'), group_spec: bool = True, min_counts: int = 5,
           min_sn: float = None, over_sample: float = None):
        """
        A get method for temperatures measured for the constituent clusters of this sample. An error will be
        thrown if temperatures haven't been measured for the given region (the default is R_500) and model (default
        is the tbabs*apec model which single_temp_apec fits to cluster spectra). Any clusters for which temperature
        fits failed will return NaN temperatures.

        :param str model: The name of the fitted model that you're requesting the results from (e.g. tbabs*apec).
        :param str/Quantity outer_radius: The name or value of the outer radius that was used for the generation of
            the spectra which were fitted to produce the desired result (for instance 'r200' would be acceptable
            for a GalaxyCluster, or Quantity(1000, 'kpc')). If 'region' is chosen (to use the regions in
            region files), then any inner radius will be ignored. You may also pass a quantity containing radius
            values, with one value for each source in this sample. The default for this method is r500.
        :param str/Quantity inner_radius: The name or value of the inner radius that was used for the generation of
            the spectra which were fitted to produce the desired result (for instance 'r500' would be acceptable
            for a GalaxyCluster, or Quantity(300, 'kpc')). By default this is zero arcseconds, resulting in a
            circular spectrum. You may also pass a quantity containing radius values, with one value for each
            source in this sample.
        :param bool group_spec: Whether the spectra that were fitted for the desired result were grouped.
        :param float min_counts: The minimum counts per channel, if the spectra that were fitted for the
            desired result were grouped by minimum counts.
        :param float min_sn: The minimum signal to noise per channel, if the spectra that were fitted for the
            desired result were grouped by minimum signal to noise.
        :param float over_sample: The level of oversampling applied on the spectra that were fitted.
        :return: An Nx3 array Quantity where N is the number of clusters. First column is the temperature, second
            column is the -err, and 3rd column is the +err. If a fit failed then that entry will be NaN.
        :rtype: Quantity
        """
        # Has to be here to prevent circular import unfortunately
        from ..sas.spec import region_setup

        if outer_radius != 'region':
            # This just parses the input inner and outer radii into something predictable
            inn_rads, out_rads = region_setup(self, outer_radius, inner_radius, True, '')[1:]
        else:
            raise NotImplementedError("Sorry region fitting is currently well supported")

        temps = []
        for src_ind, gcs in enumerate(self._sources.values()):
            try:
                # Fetch the temperature from a given cluster using the dedicated method
                gcs_temp = gcs.get_temperature(model, out_rads[src_ind], inn_rads[src_ind], group_spec, min_counts,
                                               min_sn, over_sample).value

                # If the measured temperature is 64keV I know that's a failure condition of the XSPEC fit,
                #  so its set to NaN
                if gcs_temp[0] > 30:
                    gcs_temp = np.array([np.NaN, np.NaN, np.NaN])
                    warn("A temperature of {m}keV was measured for {s}, anything over 30keV considered a failed "
                         "fit by XGA".format(s=gcs.name, m=gcs_temp))
                temps.append(gcs_temp)

            except (ValueError, ModelNotAssociatedError, ParameterNotAssociatedError) as err:
                # If any of the possible errors are thrown, we print the error as a warning and replace
                #  that entry with a NaN
                warn(str(err))
                temps.append(np.array([np.NaN, np.NaN, np.NaN]))

        # Turn the list of 3 element arrays into an Nx3 array which is then turned into an astropy Quantity
        temps = Quantity(np.array(temps), 'keV')

        # We're going to throw an error if all the temperatures are NaN, because obviously something is wrong
        check_temps = temps[~np.isnan(temps)]
        if len(check_temps) == 0:
            raise ValueError("All temperatures appear to be NaN.")

        return temps

    def gas_mass(self, rad_name: str, dens_tech: str = 'inv_abel_model', conf_level: int = 90) -> Quantity:
        """
        A get method for gas masses measured for the constituent clusters of this sample.

        :param str rad_name: The name of the radius (e.g. r500) to calculate the gas mass within.
        :param str dens_tech: The technique used to generate the density profile, default is 'inv_abel_model',
            which is the superior of the two I have implemented as of 03/12/20.
        :param int conf_level: The desired confidence level of the uncertainties.
        :return: An Nx3 array Quantity where N is the number of clusters. First column is the gas mass, second
            column is the -err, and 3rd column is the +err. If a fit failed then that entry will be NaN.
        :rtype: Quantity
        """
        gms = []

        raise NotImplementedError("This function is currently broken due to a change in how profiles are stored"
                                  " within XGA sources, hopefully not for long though!")

        # Iterate through all of our Galaxy Clusters
        for gcs in self._sources.values():
            dens_profs = gcs.get_products('combined_gas_density_profile')
            if len(dens_profs) == 0:
                # If no dens_prof has been run or something goes wrong then NaNs are added
                gms.append([np.NaN, np.NaN, np.NaN])
                warn("{s} doesn't have a density profile associated, please look at "
                     "sourcetools.density.".format(s=gcs.name))
            elif len(dens_profs) != 0:
                # This is because I store the profile products in a really dumb way which I'm going to need to
                #  correct - but for now this will do
                dens_prof = dens_profs[0][0]
                # Use the density profiles gas mass method to calculate the one we want
                gm = dens_prof.gas_mass(dens_tech, gcs.get_radius(rad_name, 'kpc'), conf_level)[0].value
                gms.append(gm)

            if len(dens_profs) > 1:
                warn("{s} has multiple density profiles associated with it, and until I upgrade XGA I can't"
                     " really tell them apart so I'm just taking the first one! I will fix this".format(s=gcs.name))

        gms = np.array(gms)

        # We're going to throw an error if all the gas masses are NaN, because obviously something is wrong
        check_gms = gms[~np.isnan(gms)]
        if len(check_gms) == 0:
            raise ValueError("All gas masses appear to be NaN.")

        return Quantity(gms, 'Msun')

    def gm_richness(self, rad_name: str, x_norm: Quantity = Quantity(60), y_norm: Quantity = Quantity(1e+12, 'solMass'),
                    fit_method: str = 'odr', start_pars: list = None, dens_tech: str = 'inv_abel_model') \
            -> ScalingRelation:
        """
        This generates a Gas Mass vs Richness scaling relation for this sample of Galaxy Clusters.

        :param str rad_name: The name of the radius (e.g. r500) to get values for.
        :param Quantity x_norm: Quantity to normalise the x data by.
        :param Quantity y_norm: Quantity to normalise the y data by.
        :param str fit_method: The name of the fit method to use to generate the scaling relation.
        :param list start_pars: The start parameters for the fit run.
        :param str dens_tech: The technique used to generate the density profile, default is 'inv_abel_model',
            which is the superior of the two I have implemented as of 03/12/20.
        :return: The XGA ScalingRelation object generated for this sample.
        :rtype: ScalingRelation
        """
        # Just make sure fit method is lower case
        fit_method = fit_method.lower()

        # Read out the richness values into variables just for convenience sake
        r_data = self.richness[:, 0]
        r_errs = self.richness[:, 1]

        # Read out the mass values, and multiply by the inverse e function for each cluster
        gm_vals = self.gas_mass(rad_name, dens_tech, conf_level=90) * self.cosmo.inv_efunc(self.redshifts)[..., None]
        gm_data = gm_vals[:, 0]
        gm_err = gm_vals[:, 1:]

        if rad_name in ['r200', 'r500', 'r2500']:
            rn = rad_name[1:]
        else:
            rn = rad_name

        y_name = "E(z)$^{-1}$M$_{g," + rn + "}$"
        if fit_method == 'curve_fit':
            scale_rel = scaling_relation_curve_fit(power_law, gm_data, gm_err, r_data, r_errs, y_norm, x_norm,
                                                   start_pars=start_pars, y_name=y_name, x_name=r"$\lambda$")
        elif fit_method == 'odr':
            scale_rel = scaling_relation_odr(power_law, gm_data, gm_err, r_data, r_errs, y_norm, x_norm,
                                             start_pars=start_pars, y_name=y_name, x_name=r"$\lambda$")
        elif fit_method == 'lira':
            scale_rel = scaling_relation_lira(gm_data, gm_err, r_data, r_errs, y_norm, x_norm,
                                              y_name=y_name, x_name=r"$\lambda$")
        elif fit_method == 'emcee':
            scaling_relation_emcee()
        else:
            raise ValueError('{e} is not a valid fitting method, please choose one of these: '
                             '{a}'.format(e=fit_method, a=' '.join(ALLOWED_FIT_METHODS)))

        return scale_rel

    # I don't allow the user to supply an inner radius here because I cannot think of a reason why you'd want to
    #  make a scaling relation with a core excised temperature.
    def gm_Tx(self, outer_radius: str, x_norm: Quantity = Quantity(4, 'keV'),
              y_norm: Quantity = Quantity(1e+12, 'solMass'), fit_method: str = 'odr', start_pars: list = None,
              dens_tech: str = 'inv_abel_model', model: str = 'tbabs*apec', group_spec: bool = True,
              min_counts: int = 5, min_sn: float = None, over_sample: float = None) -> ScalingRelation:
        """
        This generates a Gas Mass vs Tx scaling relation for this sample of Galaxy Clusters.

        :param str outer_radius: The name of the radius (e.g. r500) to get values for.
        :param Quantity x_norm: Quantity to normalise the x data by.
        :param Quantity y_norm: Quantity to normalise the y data by.
        :param str fit_method: The name of the fit method to use to generate the scaling relation.
        :param list start_pars: The start parameters for the fit run.
        :param str dens_tech: The technique used to generate the density profile, default is 'inv_abel_model',
            which is the superior of the two I have implemented as of 03/12/20.
        :param str model: The name of the model that the temperatures were measured with.
        :param bool group_spec: Whether the spectra that were fitted for the Tx values were grouped.
        :param float min_counts: The minimum counts per channel, if the spectra that were fitted for the
            Tx values were grouped by minimum counts.
        :param float min_sn: The minimum signal to noise per channel, if the spectra that were fitted for the
            Tx values were grouped by minimum signal to noise.
        :param float over_sample: The level of oversampling applied on the spectra that were fitted.
        :return: The XGA ScalingRelation object generated for this sample.
        :rtype: ScalingRelation
        """
        # Just make sure fit method is lower case
        fit_method = fit_method.lower()

        # Read out the temperature values into variables just for convenience sake
        t_vals = self.Tx(model, outer_radius, Quantity(0, 'deg'), group_spec, min_counts, min_sn, over_sample)
        t_data = t_vals[:, 0]
        t_errs = t_vals[:, 1:]

        # Read out the mass values, and multiply by the inverse e function for each cluster
        gm_vals = self.gas_mass(outer_radius, dens_tech, conf_level=90) * self.cosmo.inv_efunc(self.redshifts)[..., None]
        gm_data = gm_vals[:, 0]
        gm_err = gm_vals[:, 1:]

        if outer_radius in ['r200', 'r500', 'r2500']:
            rn = outer_radius[1:]
        else:
            rn = outer_radius

        x_name = r"T$_{\rm{x}," + rn + '}$'
        y_name = r"E(z)$^{-1}$M$_{\rm{g}," + rn + "}$"
        if fit_method == 'curve_fit':
            scale_rel = scaling_relation_curve_fit(power_law, gm_data, gm_err, t_data, t_errs, y_norm, x_norm,
                                                   start_pars=start_pars, y_name=y_name, x_name=x_name)
        elif fit_method == 'odr':
            scale_rel = scaling_relation_odr(power_law, gm_data, gm_err, t_data, t_errs, y_norm, x_norm,
                                             start_pars=start_pars, y_name=y_name, x_name=x_name)
        elif fit_method == 'lira':
            scale_rel = scaling_relation_lira(gm_data, gm_err, t_data, t_errs, y_norm, x_norm,
                                              y_name=y_name, x_name=x_name)
        elif fit_method == 'emcee':
            scaling_relation_emcee()
        else:
            raise ValueError('{e} is not a valid fitting method, please choose one of these: '
                             '{a}'.format(e=fit_method, a=' '.join(ALLOWED_FIT_METHODS)))

        return scale_rel

    def Lx_richness(self, outer_radius: str = 'r500', x_norm: Quantity = Quantity(60),
                    y_norm: Quantity = Quantity(1e+44, 'erg/s'), fit_method: str = 'odr', start_pars: list = None,
                    model: str = 'tbabs*apec', lo_en: Quantity = Quantity(0.5, 'keV'),
                    hi_en: Quantity = Quantity(2.0, 'keV'), inner_radius: Union[str, Quantity] = Quantity(0, 'arcsec'),
                    group_spec: bool = True, min_counts: int = 5, min_sn: float = None,
                    over_sample: float = None) -> ScalingRelation:
        """
        This generates a Lx vs richness scaling relation for this sample of Galaxy Clusters. If you have run fits
        to find core excised luminosity, and wish to use it in this scaling relation, then please don't forget
        to supply an inner_radius to the method call.

        :param str outer_radius: The name of the radius (e.g. r500) to get values for.
        :param Quantity x_norm: Quantity to normalise the x data by.
        :param Quantity y_norm: Quantity to normalise the y data by.
        :param str fit_method: The name of the fit method to use to generate the scaling relation.
        :param list start_pars: The start parameters for the fit run.
        :param str model: The name of the model that the luminosities were measured with.
        :param Quantity lo_en: The lower energy limit for the desired luminosity measurement.
        :param Quantity hi_en: The upper energy limit for the desired luminosity measurement.
        :param str/Quantity inner_radius: The name or value of the inner radius that was used for the generation of
            the spectra which were fitted to produce the desired result (for instance 'r500' would be acceptable
            for a GalaxyCluster, or Quantity(300, 'kpc')). By default this is zero arcseconds, resulting in a
            circular spectrum. You may also pass a quantity containing radius values, with one value for each
            source in this sample.
        :param bool group_spec: Whether the spectra that were fitted for the desired result were grouped.
        :param float min_counts: The minimum counts per channel, if the spectra that were fitted for the
            desired result were grouped by minimum counts.
        :param float min_sn: The minimum signal to noise per channel, if the spectra that were fitted for the
            desired result were grouped by minimum signal to noise.
        :param float over_sample: The level of oversampling applied on the spectra that were fitted.
        :return: The XGA ScalingRelation object generated for this sample.
        :rtype: ScalingRelation
        """
        # Just make sure fit method is lower case
        fit_method = fit_method.lower()

        # Read out the richness values into variables just for convenience sake
        r_data = self.richness[:, 0]
        r_errs = self.richness[:, 1]

        # Read out the luminosity values, and multiply by the inverse e function for each cluster
        lx_vals = self.Lx(model, outer_radius, inner_radius, lo_en, hi_en, group_spec, min_counts, min_sn,
                          over_sample) * self.cosmo.inv_efunc(self.redshifts)[..., None]
        lx_data = lx_vals[:, 0]
        lx_err = lx_vals[:, 1:]

        if outer_radius in ['r200', 'r500', 'r2500']:
            rn = outer_radius[1:]
        else:
            raise ValueError("As this is a method for a whole population, please use a named radius such as "
                             "r200, r500, or r2500.")
        y_name = "E(z)$^{-1}$L$_{x," + rn + ',' + str(lo_en.value) + '-' + str(hi_en.value) + "}$"
        if fit_method == 'curve_fit':
            scale_rel = scaling_relation_curve_fit(power_law, lx_data, lx_err, r_data, r_errs, y_norm, x_norm,
                                                   start_pars=start_pars, y_name=y_name,
                                                   x_name=r"$\lambda$")
        elif fit_method == 'odr':
            scale_rel = scaling_relation_odr(power_law, lx_data, lx_err, r_data, r_errs, y_norm, x_norm,
                                             start_pars=start_pars, y_name=y_name, x_name=r"$\lambda$")
        elif fit_method == 'lira':
            scale_rel = scaling_relation_lira(lx_data, lx_err, r_data, r_errs, y_norm, x_norm,
                                              y_name=y_name, x_name=r"$\lambda$")
        elif fit_method == 'emcee':
            scaling_relation_emcee()
        else:
            raise ValueError('{e} is not a valid fitting method, please choose one of these: '
                             '{a}'.format(e=fit_method, a=' '.join(ALLOWED_FIT_METHODS)))

        return scale_rel

    def Lx_Tx(self, outer_radius: str = 'r500', x_norm: Quantity = Quantity(4, 'keV'),
              y_norm: Quantity = Quantity(1e+44, 'erg/s'), fit_method: str = 'odr', start_pars: list = None,
              model: str = 'tbabs*apec', lo_en: Quantity = Quantity(0.5, 'keV'), hi_en: Quantity = Quantity(2.0, 'keV'),
              tx_inner_radius: Union[str, Quantity] = Quantity(0, 'arcsec'),
              lx_inner_radius: Union[str, Quantity] = Quantity(0, 'arcsec'), group_spec: bool = True,
              min_counts: int = 5, min_sn: float = None, over_sample: float = None) -> ScalingRelation:
        """
        This generates a Lx vs Tx scaling relation for this sample of Galaxy Clusters. If you have run fits
        to find core excised luminosity, and wish to use it in this scaling relation, then you can specify the inner
        radius of those spectra using lx_inner_radius, as well as ensuring that you use the temperature
        fit you want by setting tx_inner_radius.

        :param str outer_radius: The name of the radius (e.g. r500) to get values for.
        :param Quantity x_norm: Quantity to normalise the x data by.
        :param Quantity y_norm: Quantity to normalise the y data by.
        :param str fit_method: The name of the fit method to use to generate the scaling relation.
        :param list start_pars: The start parameters for the fit run.
        :param str model: The name of the model that the luminosities and temperatures were measured with.
        :param Quantity lo_en: The lower energy limit for the desired luminosity measurement.
        :param Quantity hi_en: The upper energy limit for the desired luminosity measurement.
        :param str/Quantity tx_inner_radius: The name or value of the inner radius that was used for the generation of
            the spectra which were fitted to produce the temperature (for instance 'r500' would be acceptable
            for a GalaxyCluster, or Quantity(300, 'kpc')). By default this is zero arcseconds, resulting in a
            circular spectrum. You may also pass a quantity containing radius values, with one value for each
            source in this sample.
        :param str/Quantity lx_inner_radius: The name or value of the inner radius that was used for the generation of
            the spectra which were fitted to produce the Lx. The same rules as tx_inner_radius apply, and this option
            is particularly useful if you have measured core-excised luminosity an wish to use it in a scaling relation.
        :param bool group_spec: Whether the spectra that were fitted for the desired result were grouped.
        :param float min_counts: The minimum counts per channel, if the spectra that were fitted for the
            desired result were grouped by minimum counts.
        :param float min_sn: The minimum signal to noise per channel, if the spectra that were fitted for the
            desired result were grouped by minimum signal to noise.
        :param float over_sample: The level of oversampling applied on the spectra that were fitted.
        :return: The XGA ScalingRelation object generated for this sample.
        :rtype: ScalingRelation
        """
        # Just make sure fit method is lower case
        fit_method = fit_method.lower()

        # Read out the luminosity values, and multiply by the inverse e function for each cluster
        lx_vals = self.Lx(model, outer_radius, lx_inner_radius, lo_en, hi_en, group_spec, min_counts, min_sn,
                          over_sample) * self.cosmo.inv_efunc(self.redshifts)[..., None]
        lx_data = lx_vals[:, 0]
        lx_err = lx_vals[:, 1:]

        # Read out the temperature values into variables just for convenience sake
        t_vals = self.Tx(model, outer_radius, tx_inner_radius, group_spec, min_counts, min_sn, over_sample)
        t_data = t_vals[:, 0]
        t_errs = t_vals[:, 1:]

        if outer_radius in ['r200', 'r500', 'r2500']:
            rn = outer_radius[1:]
        else:
            raise ValueError("As this is a method for a whole population, please use a named radius such as "
                             "r200, r500, or r2500.")

        if lx_inner_radius.value != 0:
            lx_rn = "Core-Excised " + rn
        else:
            lx_rn = rn

        x_name = r"T$_{x," + rn + '}$'
        y_name = "E(z)$^{-1}$L$_{x," + lx_rn + ',' + str(lo_en.value) + '-' + str(hi_en.value) + "}$"
        if fit_method == 'curve_fit':
            scale_rel = scaling_relation_curve_fit(power_law, lx_data, lx_err, t_data, t_errs, y_norm, x_norm,
                                                   start_pars=start_pars, y_name=y_name,
                                                   x_name=x_name)
        elif fit_method == 'odr':
            scale_rel = scaling_relation_odr(power_law, lx_data, lx_err, t_data, t_errs, y_norm, x_norm,
                                             start_pars=start_pars, y_name=y_name, x_name=x_name)
        elif fit_method == 'lira':
            scale_rel = scaling_relation_lira(lx_data, lx_err, t_data, t_errs, y_norm, x_norm,
                                              y_name=y_name, x_name=x_name)
        elif fit_method == 'emcee':
            scaling_relation_emcee()
        else:
            raise ValueError('{e} is not a valid fitting method, please choose one of these: '
                             '{a}'.format(e=fit_method, a=' '.join(ALLOWED_FIT_METHODS)))

        return scale_rel




