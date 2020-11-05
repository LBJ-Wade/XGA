#  This code is a part of XMM: Generate and Analyse (XGA), a module designed for the XMM Cluster Survey (XCS).
#  Last modified by David J Turner (david.turner@sussex.ac.uk) 05/11/2020, 13:24. Copyright (c) David J Turner
from typing import Tuple

import numpy as np
from astropy.units import Quantity, UnitConversionError
from scipy.integrate import trapz, cumtrapz

from ..products.base import BaseProfile1D


class SurfaceBrightness1D(BaseProfile1D):
    def __init__(self, radii: Quantity, values: Quantity, source_name: str, obs_id: str, inst: str,
                 lo_en: Quantity, hi_en: Quantity, radii_err: Quantity = None, values_err: Quantity = None,
                 background: Quantity = None):
        """
        A subclass of BaseProfile1D, designed to store and analyse surface brightness radial profiles
        of Galaxy Clusters. Allows for the viewing, fitting of the profile.
        :param Quantity radii: The radii at which surface brightness has been measured.
        :param Quantity values: The surface brightnesses that have been measured.
        :param str source_name: The name of the source this profile is associated with.
        :param str obs_id: The observation which this profile was generated from.
        :param str inst: The instrument which this profile was generated from.
        :param Quantity lo_en: The lower energy bound of the ratemap that this profile was generated from.
        :param Quantity hi_en: The upper energy bound of the ratemap that this profile was generated from.
        :param Quantity radii_err: Uncertainties on the radii.
        :param Quantity values_err: Uncertainties on the values.
        :param Quantity background: The background brightness value.
        """
        super().__init__(radii, values, source_name, obs_id, inst, radii_err, values_err)

        if type(background) != Quantity:
            raise TypeError("The background variables must be an astropy quantity.")

        # Set the internal type attribute to brightness profile
        self._prof_type = "brightness"

        # Setting the energy bounds
        self._energy_bounds = (lo_en, hi_en)

        # Check that the background passed by the user is the same unit as values
        if background is not None and background.unit == values.unit:
            self._background = background
        elif background is not None and background.unit != values.unit:
            raise UnitConversionError("The background unit must be the same as the values unit.")
        # If no background is passed then the internal background attribute stays at 0 as it was set in
        #  BaseProfile1D


class GasMass1D(BaseProfile1D):
    def __init__(self, radii: Quantity, values: Quantity, source_name: str, obs_id: str, inst: str,
                 radii_err: Quantity = None, values_err: Quantity = None):
        """
        A subclass of BaseProfile1D, designed to store and analyse gas mass radial profiles of Galaxy
        Clusters.
        :param Quantity radii: The radii at which gas mass has been measured.
        :param Quantity values: The gas mass that have been measured.
        :param str source_name: The name of the source this profile is associated with.
        :param str obs_id: The observation which this profile was generated from.
        :param str inst: The instrument which this profile was generated from.
        :param Quantity radii_err: Uncertainties on the radii.
        :param Quantity values_err: Uncertainties on the values.
        """
        super().__init__(radii, values, source_name, obs_id, inst, radii_err, values_err)
        self._prof_type = "gas_mass"

        # As this will more often than not be generated from GasDensity1D, we have to allow an
        #  external realisation to be added
        self._allowed_real_types = ["gas_dens_prof"]


class GasDensity1D(BaseProfile1D):
    def __init__(self, radii: Quantity, values: Quantity, source_name: str, obs_id: str, inst: str,
                 radii_err: Quantity = None, values_err: Quantity = None):
        """
        A subclass of BaseProfile1D, designed to store and analyse gas density radial profiles of Galaxy
        Clusters. Allows for the viewing, fitting of the profile, as well as measurement of gas masses,
        and generation of gas mass radial profiles.
        :param Quantity radii: The radii at which gas density has been measured.
        :param Quantity values: The gas densities that have been measured.
        :param str source_name: The name of the source this profile is associated with.
        :param str obs_id: The observation which this profile was generated from.
        :param str inst: The instrument which this profile was generated from.
        :param Quantity radii_err: Uncertainties on the radii.
        :param Quantity values_err: Uncertainties on the values.
        """
        super().__init__(radii, values, source_name, obs_id, inst, radii_err, values_err)

        # Actually imposing limits on what units are allowed for the radii and values for this - just
        #  to make things like the gas mass integration easier and more reliable. Also this is for mass
        #  density, not number density.
        if not radii.unit.is_equivalent("Mpc"):
            raise UnitConversionError("Radii unit cannot be converted to kpc")

        if not values.unit.is_equivalent("solMass / Mpc3"):
            raise UnitConversionError("Values unit cannot be converted to solMass / Mpc3")

        # These are the allowed realisation types (in addition to whatever density models there are
        self._allowed_real_types = ["inv_abel_model", "inv_abel_data"]

        # Setting the type
        self._prof_type = "gas_density"

        # Setting up a dictionary to store gas mass results in.
        self._gas_masses = {}

    def gas_mass(self, real_type: str, outer_rad: Quantity, conf_level: int = 90) -> Tuple[Quantity, Quantity]:
        """
        A method to calculate and return the gas mass (with uncertainties).
        :param str real_type: The realisation type to measure the mass from.
        :param Quantity outer_rad: The radius to measure the gas mass out to.
        :param int conf_level: The confidence level for the gas mass uncertainties.
        :return: A Quantity containing three values (mass, -err, +err), and another Quantity containing
        the entire mass distribution from the whole realisation.
        :rtype: Tuple[Quantity, Quantity]
        """
        if real_type not in self._realisations:
            raise ValueError("{r} is not an acceptable realisation type, this profile object currently has "
                             "realisations stored for".format(r=real_type,
                                                              a=", ".join(list(self._realisations.keys()))))
        if not outer_rad.unit.is_equivalent(self.radii_unit):
            raise UnitConversionError("The supplied outer radius cannot be converted to the radius unit"
                                      " of this profile ({u})".format(u=self.radii_unit.to_string()))
        else:
            outer_rad = outer_rad.to(self.radii_unit)

        run_int = True
        # Setting up storage structure if this particular configuration hasn't been run already
        # It goes realisation type - radius - confidence level
        if real_type not in self._gas_masses:
            self._gas_masses[real_type] = {}
            self._gas_masses[real_type][str(outer_rad.value)] = {}
            self._gas_masses[real_type][str(outer_rad.value)][str(conf_level)] = {"result": None,
                                                                                  "distribution": None}
        elif str(outer_rad.value) not in self._gas_masses[real_type]:
            self._gas_masses[real_type][str(outer_rad.value)] = {}
            self._gas_masses[real_type][str(outer_rad.value)][str(conf_level)] = {"result": None,
                                                                                  "distribution": None}
        elif str(conf_level) not in self._gas_masses[real_type][str(outer_rad.value)]:
            self._gas_masses[real_type][str(outer_rad.value)][str(conf_level)] = {"result": None,
                                                                                  "distribution": None}
        else:
            run_int = False

        if real_type not in self._good_model_fits and run_int:
            real_info = self._realisations[real_type]

            allowed_ind = np.where(real_info["mod_radii"] <= outer_rad)[0]
            trunc_rad = real_info["mod_radii"][allowed_ind].to("Mpc")
            trunc_real = real_info["mod_real"].to("solMass / Mpc3")[allowed_ind, :] * trunc_rad[..., None]**2

            gas_masses = Quantity(4*np.pi*trapz(trunc_real.value.T, trunc_rad.value), "solMass")

            upper = 50 + (conf_level / 2)
            lower = 50 - (conf_level / 2)

            gas_mass_mean = np.mean(gas_masses)
            gas_mass_lower = gas_mass_mean - np.percentile(gas_masses, lower)
            gas_mass_upper = np.percentile(gas_masses, upper) - gas_mass_mean
            storage = Quantity(np.array([gas_mass_mean.value, gas_mass_lower.value, gas_mass_upper.value]),
                               gas_mass_mean.unit)
            self._gas_masses[real_type][str(outer_rad.value)][str(conf_level)]["result"] = storage
            self._gas_masses[real_type][str(outer_rad.value)][str(conf_level)]["distribution"] = gas_masses

        elif real_type in self._good_model_fits and run_int:
            raise NotImplementedError("Cannot integrate models yet")

        results: Quantity = self._gas_masses[real_type][str(outer_rad.value)][str(conf_level)]['result']
        dist: Quantity = self._gas_masses[real_type][str(outer_rad.value)][str(conf_level)]["distribution"]
        return results, dist

    def gas_mass_profile(self, real_type: str, outer_rad: Quantity, conf_level: int = 90) -> GasMass1D:
        """
        A method to calculate and return a gas mass profile.
        :param str real_type: The realisation type to measure the mass profile from.
        :param Quantity outer_rad: The radius to measure the gas mass profile out to.
        :param int conf_level: The confidence level for the gas mass profile uncertainties.
        :return:
        :rtype:
        """
        # Run this for the checks it performs
        mass_res = self.gas_mass(real_type, outer_rad, conf_level)

        real_info = self._realisations[real_type]
        allowed_ind = np.where(real_info["mod_radii"] <= outer_rad)[0]
        trunc_rad = real_info["mod_radii"][allowed_ind].to("Mpc")
        trunc_real = real_info["mod_real"].to("solMass / Mpc3")[allowed_ind, :] * trunc_rad[..., None] ** 2
        gas_mass_real = Quantity(4 * np.pi * cumtrapz(trunc_real.value.T, trunc_rad.value), "solMass").T

        gas_mass_prof = np.mean(gas_mass_real, axis=1)
        # TODO Implement upper and lower bounds when BaseProfile1D supports non-gaussian errors
        gm_prof = GasMass1D(trunc_rad[1:], gas_mass_prof, self.src_name, self.obs_id, self.instrument)
        gm_prof.add_realisation("gas_dens_prof", trunc_rad[1:], gas_mass_real, conf_level)

        return gm_prof












