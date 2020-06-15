#  This code is a part of XMM: Generate and Analyse (XGA), a module designed for the XMM Cluster Survey (XCS).
#  Last modified by David J Turner (david.turner@sussex.ac.uk) 03/05/2020, 12:14. Copyright (c) David J Turner

import os
from configparser import ConfigParser
from subprocess import Popen, PIPE
from typing import List

import pandas as pd
import pkg_resources
from astropy.io import fits
from astropy.units import Quantity, def_unit
from astropy.wcs import WCS
from fitsio.header import FITSHDR
from numpy import nan, floor, ogrid, ndarray, arctan2, pi
from tqdm import tqdm
import json

from xga.exceptions import XGAConfigError, HeasoftError, SASNotFoundError

# Got to make sure we're able to import the PyXspec module.
# Currently raises an error, but perhaps later on I'll relax this to a warning.
try:
    # TODO The check for the pyXSPEC module is no longer necessary, I'll be using command line xspec,
    #  so will have to check for that instead.
    import xspec
except ModuleNotFoundError:
    raise HeasoftError("Unable to import PyXspec, you have to make sure to set a PYTHON environment "
                       "variable before installing HEASOFT/XSPEC.")

# TODO Maybe check that Heasoft is correctly configured, with all the files to make CCFs
# This one I'm less likely to relax to a warnings
if "SAS_DIR" not in os.environ:
    raise SASNotFoundError("SAS_DIR environment variable is not set, "
                           "unable to verify SAS is present on system")
else:
    # This way, the user can just import the SAS_VERSION from this utils code
    out, err = Popen("sas --version", stdout=PIPE, stderr=PIPE, shell=True).communicate()
    SAS_VERSION = out.decode("UTF-8").strip("]\n").split('-')[-1]

# If XDG_CONFIG_HOME is set, then use that, otherwise use this default config path
CONFIG_PATH = os.environ.get('XDG_CONFIG_HOME', os.path.join(os.path.expanduser('~'), '.config', 'xga'))
# The path to the census file, which documents all available ObsIDs and their pointings
CENSUS_FILE = os.path.join(CONFIG_PATH, 'census.csv')
# XGA config file path
CONFIG_FILE = os.path.join(CONFIG_PATH, 'xga.cfg')
# Section of the config file for setting up the XGA module
XGA_CONFIG = {"xga_save_path": "/this/is/required/xga_output/",
              "compute_mode": "local"}
# Will have to make it clear in the documentation what is allowed here, and which can be left out
# TODO Figure out how on earth to deal with separate exp1 and exp2 etc events lists/images.
#  For now just ignore them I guess?
# TODO I am assuming here that there is just one event list per observation, may only be safe for XCS?
XMM_FILES = {"root_xmm_dir": "/this/is/required/xmm_obs/data/",
             "clean_pn_evts": "/this/is/required/{obs_id}/pn_exp1_clean_evts.fits",
             "clean_mos1_evts": "/this/is/required/{obs_id}/mos1_exp1_clean_evts.fits",
             "clean_mos2_evts": "/this/is/required/{obs_id}/mos2_exp1_clean_evts.fits",
             "attitude_file": "/this/is/required/{obs_id}/attitude.fits",
             "odf_path": "/this/is/required/{obs_id}/odf/",
             "lo_en": ['0.50', '2.00'],
             "hi_en": ['2.00', '10.00'],
             "pn_image": "/this/is/optional/{obs_id}/{obs_id}-{lo_en}-{hi_en}keV-pn_merged_img.fits",
             "mos1_image": "/this/is/optional/{obs_id}/{obs_id}-{lo_en}-{hi_en}keV-mos1_merged_img.fits",
             "mos2_image": "/this/is/optional/{obs_id}/{obs_id}-{lo_en}-{hi_en}keV-mos2_merged_img.fits",
             "pn_expmap": "/this/is/optional/{obs_id}/{obs_id}-{lo_en}-{hi_en}keV-pn_merged_img.fits",
             "mos1_expmap": "/this/is/optional/{obs_id}/{obs_id}-{lo_en}-{hi_en}keV-mos1_merged_expmap.fits",
             "mos2_expmap": "/this/is/optional/{obs_id}/{obs_id}-{lo_en}-{hi_en}keV-mos2_merged_expmap.fits",
             "region_file": "/this/is/optional/xmm_obs/regions/{obs_id}/regions.reg"}
# List of XMM products supported by XGA that are allowed to be energy bound
ENERGY_BOUND_PRODUCTS = ["image", "expmap", "reg_image", "reg_expmap", "psfmap"]
# List of all XMM products supported by XGA
ALLOWED_PRODUCTS = ["spectrum", "grp_spec", "regions", "events"] + ENERGY_BOUND_PRODUCTS
XMM_INST = ["pn", "mos1", "mos2"]

# Here we read in files that list the errors and warnings in SAS
errors = pd.read_csv(pkg_resources.resource_filename(__name__, "files/sas_errors.csv"), header="infer")
warnings = pd.read_csv(pkg_resources.resource_filename(__name__, "files/sas_warnings.csv"), header="infer")
# Just the names of the errors in two handy constants
SASERROR_LIST = errors["ErrName"].values
SASWARNING_LIST = warnings["WarnName"].values

# XSPEC file extraction (and base fit) scripts
XGA_EXTRACT = pkg_resources.resource_filename(__name__, "xspec_scripts/xga_extract.tcl")
BASE_XSPEC_SCRIPT = pkg_resources.resource_filename(__name__, "xspec_scripts/general_xspec_fit.xcm")
# Useful jsons of all XSPEC models, their required parameters, and those parameter's units
with open(pkg_resources.resource_filename(__name__, "files/xspec_model_pars.json5"), 'r') as filey:
    MODEL_PARS = json.load(filey)
with open(pkg_resources.resource_filename(__name__, "files/xspec_model_units.json5"), 'r') as filey:
    MODEL_UNITS = json.load(filey)


def xmm_obs_id_test(test_string: str) -> bool:
    """
    Crude function to try and determine if a string follows the pattern of an XMM ObsID
    :param str test_string: The string we wish to test.
    :return: Whether the string is probably an XMM ObsID or not.
    :rtype: bool
    """
    probably_xmm = False
    # XMM ObsIDs are ten characters long, and making sure there is no . that might indicate a file extension.
    if len(test_string) == 10 and '.' not in test_string:
        try:
            # To our constant pain, XMM ObsIDs can convert to integers, so if this works then its likely
            # an XMM ObsID.
            int(test_string)
            probably_xmm = True
        except ValueError:
            pass
    return probably_xmm


def observation_census(config: ConfigParser) -> pd.DataFrame:
    """
    A function to initialise or update the file that stores which observations are available in the user
    specified XMM data directory, and what their pointing coordinates are.
    CURRENTLY THIS WILL NOT UPDATE TO DEAL WITH OBSID FOLDERS THAT HAVE BEEN DELETED.
    :param config: The XGA configuration object.
    :return: ObsIDs and pointing coordinates of available XMM observations.
    :rtype: pd.DataFrame
    """
    # The census lives in the XGA config folder, and CENSUS_FILE stores the path to it.
    # If it exists, it is read in, otherwise empty lists are initialised to be appended to.
    if os.path.exists(CENSUS_FILE):
        with open(CENSUS_FILE, 'r') as census:
            obs_lookup = census.readlines()  # Reads the lines of the files
            # This is just ObsIDs, needed to see which ObsIDs have already been processed.
            obs_lookup_obs = [entry.split(',')[0] for entry in obs_lookup[1:]]
    else:
        obs_lookup = ["ObsID,RA_PNT,DEC_PNT\n"]
        obs_lookup_obs = []

    # Need to find out which observations are available, crude way of making sure they are ObsID directories
    # This also checks that I haven't run them before
    obs_census = [entry for entry in os.listdir(config["XMM_FILES"]["root_xmm_dir"]) if xmm_obs_id_test(entry)
                  and entry not in obs_lookup_obs]
    if len(obs_census) != 0:
        census_progress = tqdm(desc="Assembling list of ObsID pointings", total=len(obs_census))
        for obs in obs_census:
            ra_pnt = ''
            dec_pnt = ''
            # Prepared to check all three events files, but if one succeeds the rest are
            # skipped for efficiency
            for key in ["clean_pn_evts", "clean_mos1_evts", "clean_mos2_evts"]:
                evt_path = config["XMM_FILES"][key].format(obs_id=obs)
                if os.path.exists(evt_path) and ra_pnt == '' and dec_pnt == '':
                    with fits.open(evt_path, mode='readonly') as evts:
                        try:
                            ra_pnt = evts[0].header["RA_PNT"]
                            dec_pnt = evts[0].header["DEC_PNT"]
                        except KeyError:
                            pass
                    break
                    # If this part has run successfully there's no need to open the other images
            # Format to write to the census.csv that lives in the config directory.
            obs_lookup.append("{o},{r},{d}\n".format(o=obs, r=ra_pnt, d=dec_pnt))
            census_progress.update(1)
        census_progress.close()
        with open(CENSUS_FILE, 'w') as census:
            census.writelines(obs_lookup)

    # I do the stripping and splitting to make it a 3 column array, needed to be lines to write to file
    obs_lookup = pd.DataFrame(data=[entry.strip('\n').split(',') for entry in obs_lookup[1:]],
                              columns=obs_lookup[0].strip("\n").split(','), dtype=str)
    obs_lookup["RA_PNT"] = obs_lookup["RA_PNT"].replace('', nan).astype(float)
    obs_lookup["DEC_PNT"] = obs_lookup["DEC_PNT"].replace('', nan).astype(float)
    return obs_lookup


def to_list(str_rep_list: str) -> list:
    """
    Convenience function to change a string representation of a Python list into an actual list object.
    :param str str_rep_list: String that represents a Python list. e.g. "['0.5', '2.0']"
    :return: The parsed representative string.
    :rtype: list
    """
    in_parts = str_rep_list.strip("[").strip("]").split(',')
    real_list = [part.strip(' ').strip("'").strip('"') for part in in_parts if part != '' and part != ' ']
    return real_list


def energy_to_channel(energy: Quantity) -> int:
    """
    Converts an astropy energy quantity into an XMM channel.
    :param energy:
    """
    energy = energy.to("eV").value
    chan = int(energy)
    return chan


def dict_search(key: str, var: dict) -> list:
    """
    This simple function was very lightly modified from a stackoverflow answer, and is an
    efficient method of searching through a nested dictionary structure for specfic keys
    (and yielding the values associated with them). In this case will extract all of a
    specific product type for a given source.
    :param key: The key in the dictionary to search for and extract values.
    :param var: The variable to search, likely to be either a dictionary or a string.
    :return list[list]: Returns information on keys and values
    """

    # Check that the input is actually a dictionary
    if hasattr(var, 'items'):
        for k, v in var.items():
            if k == key:
                yield v
            # Here is where we dive deeper, recursively searching lower dictionary levels.
            if isinstance(v, dict):
                for result in dict_search(key, v):
                    # We yield a string of the result and the key, as we'll need to return the
                    # ObsID and Instrument information from these product searches as well.
                    # This will mean the output is an unpleasantly nested list, but we can solve that.
                    yield [str(k), result]


# TODO Rewrite this to be vectorised? Masks get returned as a 512 x 512 x N array?
# TODO Then move it to image tools
def annular_mask(cen_x: int, cen_y: int, inn_rad: int, out_rad: int, len_x: int, len_y: int,
                 start_ang: Quantity = Quantity(0, 'deg'), stop_ang: Quantity = Quantity(360, 'deg')) -> ndarray:
    """
    A hopefully handy little function to generate annular (or circular) masks in the form of numpy arrays.
    It produces the mask for a given shape of image, centered at supplied coordinates, and with inner and
    outer radii supplied by the user also. Angular limits can also be supplied to give the mask an annular
    dependence.
    :param int cen_x: Numpy array x-coordinate of the center for this mask.
    :param int cen_y: Numpy array y-coordinate of the center for this mask.
    :param int inn_rad: Pixel radius for the inner part of the annular mask.
    :param int out_rad: Pixel radius for the outer part of the annular mask.
    :param Quantity start_ang: Lower angular limit for the mask.
    :param Quantity stop_ang: Upper angular limit for the mask.
    :param int len_x: Length in the x direction of the array/image this mask is for.
    :param int len_y: Length in the y direction of the array/image this mask is for.
    :return: The generated mask array.
    :rtype: ndarray
    """
    # Making use of the astropy units module, check that we are being pass actual angle values
    if start_ang.unit not in ['deg', 'rad']:
        raise ValueError("start_angle unit type {} is not an accepted angle unit, "
                         "please use deg or rad.".format(start_ang.unit))
    elif stop_ang.unit not in ['deg', 'rad']:
        raise ValueError("stop_angle unit type {} is not an accepted angle unit, "
                         "please use deg or rad.".format(stop_ang.unit))
    # Enforcing some common sense rules on the angles
    elif start_ang >= stop_ang:
        raise ValueError("start_ang cannot be greater than or equal to stop_ang.")
    elif start_ang > Quantity(360, 'deg') or stop_ang > Quantity(360, 'deg'):
        raise ValueError("start_ang and stop_ang cannot be greater than 360 degrees.")
    elif stop_ang < Quantity(0, 'deg'):
        raise ValueError("stop_ang cannot be less than 0 degrees.")
    else:
        # Don't want to pass astropy objects to numpy functions, but do need the angles in radians
        start_ang = start_ang.to('rad').value
        stop_ang = stop_ang.to('rad').value

    # This sets up the cartesian coordinate grid of x and y values
    arr_y, arr_x = ogrid[:len_y, :len_x]

    # Go to polar coordinates
    rec_x = arr_x - cen_x
    rec_y = arr_y - cen_y
    # Leave this as r**2 to avoid square rooting and involving floats
    arr_r_squared = rec_x**2 + rec_y**2

    # arctan2 does just perform arctan on two values, but then uses the signs of those values to
    # decide the quadrant of the output
    arr_theta = (arctan2(rec_x, rec_y) - start_ang) % (2*pi)  # Normalising to 0-2pi range

    # This applies common sense rules to inner and out radii, also slightly changes the mask term
    # if inn_rad == out_rad
    if inn_rad > out_rad:
        raise ValueError("inn_rad value cannot be greater than out_rad")
    # If the user sets inner radius to 0, they'll expect a circular mask that includes the central pixel
    elif inn_rad < out_rad and inn_rad == 0:
        rad_mask = arr_r_squared <= out_rad ** 2
    elif inn_rad < out_rad and inn_rad != 0:
        rad_mask = (arr_r_squared <= out_rad**2) & (arr_r_squared > inn_rad**2)
    elif inn_rad == out_rad:
        rad_mask = arr_r_squared == inn_rad**2

    ang_mask = arr_theta <= (stop_ang - start_ang)
    ann_mask = rad_mask * ang_mask
    # Just ensures that the central pixel is included if a non-default angle range is used.
    if inn_rad == 0:
        ann_mask[cen_y, cen_x] = True

    return ann_mask


def find_all_wcs(hdr: FITSHDR) -> List[WCS]:
    """
    A play on the function of the same name in astropy.io.fits, except this one will take a fitsio header object
    as an argument, and construct astropy wcs objects. Very simply looks for different WCS entries in the
    header, and uses their critical values to construct astropy WCS objects.
    :return: A list of astropy WCS objects extracted from the input header.
    :rtype: List[WCS]
    """
    wcs_search = [k.split("CTYPE")[-1][-1] for k in hdr.keys() if "CTYPE" in k]
    wcs_nums = [w for w in wcs_search if w.isdigit()]
    wcs_not_nums = [w for w in wcs_search if not w.isdigit()]
    if len(wcs_nums) != 2 and len(wcs_nums) != 0:
        raise KeyError("There are an odd number of CTYPEs with no extra key ")
    elif len(wcs_nums) == 2:
        wcs_keys = [""] + list(set(wcs_not_nums))
    elif len(wcs_nums) == 0:
        wcs_keys = list(set(wcs_not_nums))

    wcses = []
    for key in wcs_keys:
        w = WCS(naxis=2)
        w.wcs.crpix = [hdr["CRPIX1{}".format(key)], hdr["CRPIX2{}".format(key)]]
        w.wcs.cdelt = [hdr["CDELT1{}".format(key)], hdr["CDELT2{}".format(key)]]
        w.wcs.crval = [hdr["CRVAL1{}".format(key)], hdr["CRVAL2{}".format(key)]]
        w.wcs.ctype = [hdr["CTYPE1{}".format(key)], hdr["CTYPE2{}".format(key)]]
        wcses.append(w)

    return wcses


if not os.path.exists(CONFIG_PATH):
    os.makedirs(CONFIG_PATH)

# If first XGA run, creates default config file
if not os.path.exists(CONFIG_FILE):
    xga_default = ConfigParser()
    xga_default.add_section("XGA_SETUP")
    xga_default["XGA_SETUP"] = XGA_CONFIG
    xga_default.add_section("XMM_FILES")
    xga_default["XMM_FILES"] = XMM_FILES
    with open(CONFIG_FILE, 'w') as new_cfg:
        xga_default.write(new_cfg)

    # First time run triggers this message
    raise XGAConfigError("As this is the first time you've used XGA, "
                         "please configure {} to match your setup".format(CONFIG_FILE))

# But if the config file is found, some preprocessing and checks are applied
else:
    xga_conf = ConfigParser()
    # It would be nice to do configparser interpolation, but it wouldn't handle the lists of energy values
    xga_conf.read(CONFIG_FILE)
    keys_to_check = ["root_xmm_dir", "clean_pn_evts", "clean_mos1_evts", "clean_mos2_evts", "attitude_file",
                     "odf_path"]
    # Here I check that the installer has actually changed the three events file paths
    all_changed = all([xga_conf["XMM_FILES"][key] != XMM_FILES[key] for key in keys_to_check])
    if not all_changed:
        raise XGAConfigError("Some events file paths (or the root_xmm_dir) in the config have not "
                             "been changed from default")
    elif not os.path.exists(xga_conf["XMM_FILES"]["root_xmm_dir"]):
        raise FileNotFoundError("That root_xmm_dir does not appear to exist, "
                                "if it an SFTP mount check the connection.")

    # Now I do the same for the XGA_SETUP section
    keys_to_check = ["xga_save_path"]
    # Here I check that the installer has actually changed the three events file paths
    all_changed = all([xga_conf["XGA_SETUP"][key] != XGA_CONFIG[key] for key in keys_to_check])
    if not all_changed:
        raise XGAConfigError("You have not changed the xga_save_path value in the config file")
    elif not os.path.exists(xga_conf["XGA_SETUP"]["xga_save_path"]):
        # This is the folder where any files generated by XGA get written
        # Its taken as is from the config file, so it can be absolute, or relative to the project directory
        # Can also be overwritten at runtime by the user, so that's nice innit
        os.makedirs(xga_conf["XGA_SETUP"]["xga_save_path"])

    no_check = ["root_xmm_dir", "lo_en", "hi_en"]
    for key, value in xga_conf["XMM_FILES"].items():
        # Here we attempt to deal with files where people have defined their file paths
        # relative to the root_xmm_dir
        if key not in no_check and xga_conf["XMM_FILES"]["root_xmm_dir"] not in xga_conf["XMM_FILES"][key] \
                and xga_conf["XMM_FILES"][key][0] != '/':
            xga_conf["XMM_FILES"][key] = os.path.join(os.path.abspath(xga_conf["XMM_FILES"]["root_xmm_dir"]),
                                                      xga_conf["XMM_FILES"][key])

    # As it turns out, the ConfigParser class is a pain to work with, so we're converting to a dict here
    # Addressing works just the same
    xga_conf = {str(sect): dict(xga_conf[str(sect)]) for sect in xga_conf}
    try:
        xga_conf["XMM_FILES"]["lo_en"] = to_list(xga_conf["XMM_FILES"]["lo_en"])
        xga_conf["XMM_FILES"]["hi_en"] = to_list(xga_conf["XMM_FILES"]["hi_en"])
    except KeyError:
        raise KeyError("Entries have been removed from config file, "
                       "please leave all in place, even if they are empty")

    # Do a little pre-checking for the energy entries
    if len(xga_conf["XMM_FILES"]["lo_en"]) != len(xga_conf["XMM_FILES"]["hi_en"]):
        raise ValueError("lo_en and hi_en entries in the config "
                         "file do not parse to lists of the same length.")

    # Make sure that this is the absolute path
    xga_conf["XMM_FILES"]["root_xmm_dir"] = os.path.abspath(xga_conf["XMM_FILES"]["root_xmm_dir"]) + "/"
    # Read dataframe of ObsIDs and pointing coordinates into constant
    CENSUS = observation_census(xga_conf)
    OUTPUT = os.path.abspath(xga_conf["XGA_SETUP"]["xga_save_path"]) + "/"

    # These are the different ways the SAS runs can be partitioned out
    allowed_compute = ["local", "sge", "slurm"]
    COMPUTE_MODE = xga_conf["XGA_SETUP"]["compute_mode"].lower()
    if COMPUTE_MODE not in allowed_compute:
        raise ValueError("{0} is not a valid compute mode - "
                         "please choose from:\n {1}".format(xga_conf["XGA_SETUP"]["compute_mode"],
                                                            ", ".join(allowed_compute)))
    elif COMPUTE_MODE == "local":
        # Going to allow multi-core processing to use 90% of available cores by default, but
        # this can be over-ridden in individual SAS calls.
        NUM_CORES = max(int(floor(os.cpu_count() * 0.9)), 1)  # Makes sure that at least one core is used

    # TODO Remove this once I have figured out how to support HPCs
    elif COMPUTE_MODE in ["sge", "slurm"]:
        raise NotImplementedError("I don't support HPCs yet!")

    xmm_sky = def_unit("xmm_sky")
    xmm_det = def_unit("xmm_det")





