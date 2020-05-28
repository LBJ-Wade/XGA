#  This code is a part of XMM: Generate and Analyse (XGA), a module designed for the XMM Cluster Survey (XCS).
#  Last modified by David J Turner (david.turner@sussex.ac.uk) 10/05/2020, 15:17. Copyright (c) David J Turner

import os
from typing import Tuple, List, Dict
import sys
from copy import deepcopy
import gc
import warnings
from tempfile import TemporaryFile
from time import sleep
import psutil

import numpy as np
from astropy.io import fits
from astropy import wcs
from astropy.units import Quantity, UnitBase, UnitsError, deg, pix

from xga.exceptions import SASGenerationError, UnknownCommandlineError, FailedProductError
from xga.utils import SASERROR_LIST, SASWARNING_LIST, xmm_sky, xmm_det
# TODO Maybe do a regions object but not fully decided on that yet


class BaseProduct:
    def __init__(self, path: str, obs_id: str, instrument: str, stdout_str: str, stderr_str: str,
                 gen_cmd: str, raise_properly: bool = True):
        """
        The initialisation method for the BaseProduct class.
        :param str path: The path to where the product file SHOULD be located.
        :param str stdout_str: The stdout from calling the terminal command.
        :param str stderr_str: The stderr from calling the terminal command.
        :param str gen_cmd: The command used to generate the product.
        :param bool raise_properly: Shall we actually raise the errors as Python errors?
        """
        # So this flag indicates whether we think this data product can be used for analysis
        self.usable = True
        # Hopefully uses the path setter method
        self.path = path
        # Saving this in attributes for future reference
        self.unprocessed_stdout = stdout_str
        self.unprocessed_stderr = stderr_str
        self._sas_error, self._sas_warn, self._other_error = self.parse_stderr()
        self._obs_id = obs_id
        self._inst = instrument
        self.og_cmd = gen_cmd
        self._energy_bounds = (None, None)
        self._prod_type = None

        self.raise_errors(raise_properly)

    @property
    def path(self) -> str:
        """
        Property getter for the attribute containing the path to the product.
        :return: The product path.
        :rtype: str
        """
        return self._path

    @path.setter
    def path(self, prod_path: str):
        """
        Property setter for the attribute containing the path to the product.
        :param str prod_path: The product path.
        """
        if not os.path.exists(prod_path):
            prod_path = None
            # We won't be able to make use of this product if it isn't where we think it is
            self.usable = False
        self._path = prod_path

    def parse_stderr(self) -> Tuple[List[Dict], List[Dict], List]:
        """
        This method parses the stderr associated with the generation of a product into errors confirmed to have
        come from SAS, and other unidentifiable errors. The SAS errors are returned with the actual error
        name, the error message, and the SAS routine that caused the error.
        :return: A list of dictionaries containing parsed, confirmed SAS errors, another containing SAS warnings,
        and another list of unidentifiable errors that occured in the stderr.
        :rtype: Tuple[List[Dict], List[Dict], List]
        """
        def find_sas(split_stderr: list, err_type: str) -> Tuple[dict, List[str]]:
            """
            Function to search for and parse SAS errors and warnings.
            :param list split_stderr: The stderr string split on line endings.
            :param str err_type: Should this look for errors or warnings?
            :return: Returns the dictionary of parsed errors/warnings, as well as all lines
            with SAS errors/warnings in.
            :rtype: Tuple[dict, List[str]]
            """
            parsed_sas = []
            # This is a crude way of looking for SAS error/warning strings ONLY
            sas_lines = [line for line in split_stderr if "** " in line and ": {}".format(err_type) in line]
            for err in sas_lines:
                try:
                    # This tries to split out the SAS task that produced the error
                    originator = err.split("** ")[-1].split(":")[0]
                    # And this should split out the actual error name
                    err_ident = err.split(": {} (".format(err_type))[-1].split(")")[0]
                    # Actual error message
                    err_body = err.split("({})".format(err_ident))[-1].strip("\n").strip(", ").strip(" ")

                    if err_type == "error":
                        # Checking to see if the error identity is in the list of SAS errors
                        sas_err_match = [sas_err for sas_err in SASERROR_LIST if err_ident in sas_err]
                    elif err_type == "warning":
                        # Checking to see if the error identity is in the list of SAS warnings
                        sas_err_match = [sas_err for sas_err in SASWARNING_LIST if err_ident in sas_err]

                    if len(sas_err_match) != 1:
                        originator = ""
                        err_ident = ""
                        err_body = ""
                except IndexError:
                    originator = ""
                    err_ident = ""
                    err_body = ""

                parsed_sas.append({"originator": originator, "name": err_ident, "message": err_body})
            return parsed_sas, sas_lines

        # Defined as empty as they are returned by this method
        parsed_sas_errs = []
        parsed_sas_warns = []
        other_err_lines = []
        # err_str being "" is ideal, hopefully means that nothing has gone wrong
        if self.unprocessed_stderr != "":
            # Errors will be added to the error summary, then raised later
            # That way if people try except the error away the object will have been constructed properly
            err_lines = self.unprocessed_stderr.split('\n')  # Fingers crossed each line is a separate error
            parsed_sas_errs, sas_err_lines = find_sas(err_lines, "error")
            parsed_sas_warns, sas_warn_lines = find_sas(err_lines, "warning")

            # These are impossible to predict the form of, so they won't be parsed
            other_err_lines = [line for line in err_lines if line not in sas_err_lines
                               and line not in sas_warn_lines and line != ""]
        return parsed_sas_errs, parsed_sas_warns, other_err_lines

    @property
    def sas_error(self) -> List[Dict]:
        """
        Property getter for the confirmed SAS errors associated with a product.
        :return: The list of confirmed SAS errors.
        :rtype: List[Dict]
        """
        return self._sas_error

    def raise_errors(self, raise_flag: bool):
        """
        Method to raise the errors parsed from std_err string.
        :param raise_flag: Should this function actually raise the error properly.
        """
        if raise_flag:
            # I know this won't ever get to the later errors, I might change how this works later
            for error in self._sas_error:
                self.usable = False  # Just to make sure this object isn't used if the user uses try, except
                raise SASGenerationError("{e} raised by {t} - {b}".format(e=error["name"], t=error["originator"],
                                                                          b=error["message"]))
            # This is for any unresolved errors.
            for error in self._other_error:
                if "warning" not in error:
                    raise UnknownCommandlineError("{}".format(error))

    @property
    def obs_id(self) -> str:
        """
        Property getter for the ObsID of this image. Admittedly this information is implicit in the location
        this object is stored in a source object, but I think it worth storing directly as a property as well.
        :return: The XMM ObsID of this image.
        :rtype: str
        """
        return self._obs_id

    @property
    def instrument(self) -> str:
        """
        Property getter for the instrument used to take this image. Admittedly this information is implicit
        in the location this object is stored in a source object, but I think it worth storing
        directly as a property as well.
        :return: The XMM instrument used to take this image.
        :rtype: str
        """
        return self._inst

    # This is a fundamental property of the generated product, so I won't allow it be changed.
    @property
    def energy_bounds(self) -> Tuple[Quantity, Quantity]:
        """
        Getter method for the energy_bounds property, which returns the rest frame energy band that this
        product was generated in.
        :return: Tuple containing the lower and upper energy limits as Astropy quantities.
        :rtype: Tuple[Quantity, Quantity]
        """
        return self._energy_bounds

    @property
    def type(self) -> str:
        """
        Property getter for the string identifier for the type of product this object is, mostly useful for
        internal methods of source objects.
        :return: The string identifier for this type of object.
        :rtype: str
        """
        return self._prod_type

    @property
    def errors(self) -> List[dict]:
        """
        Property getter for SAS errors detected during the generation of a product.
        :return: A list of dictionaries of parsed errors.
        :rtype: List[dict]
        """
        return self._sas_error

    @property
    def warnings(self) -> List[dict]:
        """
        Property getter for SAS warnings detected during the generation of a product.
        :return: A list of dictionaries of parsed errors.
        :rtype: List[dict]
        """
        return self._sas_error


class Image(BaseProduct):
    def __init__(self, path: str, obs_id: str, instrument: str, stdout_str: str, stderr_str: str,
                 gen_cmd: str, lo_en: Quantity, hi_en: Quantity, raise_properly: bool = True):
        """
        The initialisation method for the Image class.
        :param str path: The path to where the product file SHOULD be located.
        :param str stdout_str: The stdout from calling the terminal command.
        :param str stderr_str: The stderr from calling the terminal command.
        :param str gen_cmd: The command used to generate the product.
        :param Quantity lo_en: The lower energy bound used to generate this product.
        :param Quantity hi_en: The upper energy bound used to generate this product.
        :param bool raise_properly: Shall we actually raise the errors as Python errors?
        """
        super().__init__(path, obs_id, instrument, stdout_str, stderr_str, gen_cmd, raise_properly)
        self._shape = None
        self._wcs_radec = None
        self._wcs_xmmXY = None
        self._wcs_xmmdetXdetY = None
        self._energy_bounds = (lo_en, hi_en)
        self._prod_type = "image"
        self._data = None
        self._header = None

    def _read_on_demand(self):
        """
        Internal method to read the image associated with this Image object into memory when it is requested by
        another method. Doing it on-demand saves on wasting memory.
        """
        # Usable flag to check that nothing went wrong in the image generation
        if self.usable:
            # Not all images produced by SAS are going to be needed all the time, so they will only be read in if
            # asked for.
            # Using read only mode because it still allows the user to make changes to the object in memory,
            # they just can't overwrite the original image.

            with fits.open(self.path, mode="readonly", memmap=False) as temp_im:
                data = temp_im[0].data

                data = data.newbyteorder().byteswap()
                data = data.astype("float32")

                if data.min() < 0:
                    # TODO PERHAPS RESTORE THIS?
                    # warnings.warn("You are loading an {} with elements that are < 0, "
                    #               "they will be set to 0.".format(self._prod_type))
                    data[data < 0] = 0
                # XMM exposure maps are float 32 by default, though admittedly images could be opened as int
                new_data = np.empty_like(data)
                np.copyto(new_data, data)

                self._data = new_data
                del temp_im[0].data
                del data
                header = temp_im[0].header
                self._header = header
                del header

            temp_im.close(verbose=True)
            del temp_im
            gc.collect()

            # data = fits.getdata(self.path)
            # if data.min() < 0:
            #     warnings.warn("You are loading an {} with elements that are < 0, "
            #                   "they will be set to 0.".format(self._prod_type))
            #     data[data < 0] = 0
            # self._data = data
            # self._header = fits.getheader(self.path)

            # As the image must be loaded to know the shape, I've waited until here to set the _shape attribute
            self._shape = self._data.shape
            # Will actually construct an image WCS as well because why not?
            # XMM images typically have two, both useful, so we'll find all available and store them
            wcses = wcs.find_all_wcs(self._header)

            # Just iterating through and assigning to the relevant attributes
            for w in wcses:
                axes = [ax.lower() for ax in w.axis_type_names]
                if "ra" in axes and "dec" in axes:
                    self._wcs_radec = w
                elif "x" in axes and "y" in axes:
                    self._wcs_xmmXY = w
                elif "detx" in axes and "dety" in axes:
                    self._wcs_xmmdetXdetY = w
                else:
                    raise ValueError("This type of WCS is not recognised!")

            # I'll only strongly require that the pixel-RADEC WCS is found
            if self._wcs_radec is None:
                raise FailedProductError("SAS has generated this image without a WCS capable of "
                                         "going from pixels to RA-DEC.")

        else:
            raise FailedProductError("SAS failed to generate this product successfully, so you cannot "
                                     "access data from it. Check the usable attribute next time")

    @property
    def shape(self) -> Tuple[int, int]:
        """
        Property getter for the resolution of the image. Standard XGA settings will make this 512x512.
        :return: The shape of the numpy array describing the image.
        :rtype: Tuple[int, int]
        """
        # This has to be run first, to check the image is loaded, otherwise how can we know the shape?
        # This if is here rather than in the method as some other properties of this class don't need the
        # image object, just some products derived from it.
        if self._data is None:
            self._read_on_demand()
        # There will not be a setter for this property, no-one is allowed to change the shape of the image.
        return self._shape

    @property
    def data(self) -> np.ndarray:
        """
        Property getter for the actual image data, in the form of a numpy array. Doesn't include
        any of the other stuff you get in a fits image, thats found in the hdulist property.
        :return: A numpy array of shape self.shape containing the image data.
        :rtype: np.ndarray
        """
        # Calling this ensures the image object is read into memory
        if self._data is None:
            self._read_on_demand()
        return self._data

    @data.setter
    def data(self, new_im_arr: np.ndarray):
        """
        Property setter for the image data. As the fits image is loaded in read-only mode,
        this won't alter the actual file (which is what I was going for), but it does allow
        user alterations to the image data they are analysing.
        :param np.ndarray new_im_arr: The new image data.
        """
        # Calling this ensures the image object is read into memory
        if self._data is None:
            self._read_on_demand()

        # Have to make sure the input is of the right type, and the right shape
        if not isinstance(new_im_arr, np.ndarray):
            raise TypeError("You may only assign a numpy array to the data attribute.")
        elif new_im_arr.shape != self._shape:
            raise ValueError("You may only assign a numpy array to the data attribute if it "
                             "is the same shape as the original.")
        else:
            self._data = new_im_arr

    # This one doesn't get a setter, as I require this WCS to not be none in the _read_on_demand method
    @property
    def radec_wcs(self) -> wcs.WCS:
        """
        Property getter for the WCS that converts back and forth between pixel values
        and RA-DEC coordinates. This one is the only WCS guaranteed to not-None.
        :return: The WCS object for RA and DEC.
        :rtype: wcs.WCS
        """
        # If this WCS is None then _read_on_demand definitely hasn't run, this one MUST be set
        if self._wcs_radec is None:
            self._read_on_demand()
        return self._wcs_radec

    # These two however, can be none, so the user should be allowed to set add WCS-es to those
    # that don't have them. Will be good for the coordinate transform methods
    @property
    def skyxy_wcs(self):
        """
        Property getter for the WCS that converts back and forth between pixel values
        and XMM XY Sky coordinates.
        :return: The WCS object for XMM X and Y sky coordinates.
        :rtype: wcs.WCS
        """
        # Deliberately checking the radec WCS, as the skyXY WCS is allowed to be None after the
        # read_on_demand call
        if self._wcs_radec is None:
            self._read_on_demand()
        return self._wcs_xmmXY

    @skyxy_wcs.setter
    def skyxy_wcs(self, input_wcs: wcs.WCS):
        """
        Property setter for the WCS that converts back and forth between pixel values
        and XMM XY Sky coordinates. This WCS is not guaranteed to be set from the image,
        so it is possible to add your own.
        :param wcs.WCS input_wcs: The user supplied WCS object to assign to skyxy_wcs property.
        """
        if not isinstance(input_wcs, wcs.WCS):
            # Obviously don't want people assigning non-WCS objects as this will be used internally
            TypeError("Can't assign a non-WCS object to this WCS property.")
        else:
            # Fetching the WCS axis names and lowering them for comparison
            axes = [w.lower() for w in input_wcs.axis_type_names]
            # Checking if the right names are present
            if "x" not in axes or "y" not in axes:
                raise ValueError("This WCS does not have the XY axes expected for the skyxy_wcs property.")
            else:
                self._wcs_xmmXY = input_wcs

    @property
    def detxy_wcs(self):
        """
        Property getter for the WCS that converts back and forth between pixel values
        and XMM DETXY detector coordinates.
        :return: The WCS object for XMM DETX and DETY detector coordinates.
        :rtype: wcs.WCS
        """
        # Deliberately checking the radec WCS, as the DETXY WCS is allowed to be None after the
        # read_on_demand call
        if self._wcs_radec is None:
            self._read_on_demand()
        return self._wcs_xmmdetXdetY

    @detxy_wcs.setter
    def detxy_wcs(self, input_wcs: wcs.WCS):
        """
        Property setter for the WCS that converts back and forth between pixel values
        and XMM DETXY detector coordinates. This WCS is not guaranteed to be set from the image,
        so it is possible to add your own.
        :param wcs.WCS input_wcs: The user supplied WCS object to assign to detxy_wcs property.
        """
        if not isinstance(input_wcs, wcs.WCS):
            # Obviously don't want people assigning non-WCS objects as this will be used internally
            TypeError("Can't assign a non-WCS object to this WCS property.")
        else:
            # Fetching the WCS axis names and lowering them for comparison
            axes = [w.lower() for w in input_wcs.axis_type_names]
            # Checking if the right names are present
            if "detx" not in axes or "dety" not in axes:
                raise ValueError("This WCS does not have the DETX DETY axes expected for the detxy_wcs property.")
            else:
                self._wcs_xmmdetXdetY = input_wcs
    
    # This absolutely doesn't get a setter considering its the main object with all the information in
    @property
    def header(self) -> fits.header.Header:
        """
        Property getter allowing access to the astropy fits header object created when the image was read in.
        :return: The header of the primary data table of the image that was read in.
        :rtype: fits.header.Header
        """
        return self._header

    def coord_conv(self, coord_pair: Quantity, output_unit: UnitBase) -> Quantity:
        """
        This will use the loaded WCSes, and astropy coordinates (including custom ones defined for this module),
        to perform common coordinate conversions for this product object.
        :param coord_pair: The input coordinate quantity to convert, in units of either deg,
        pix, xmm_sky, or xmm_det (xmm_sky and xmm_det are defined for this module).
        :param output_unit: The astropy unit to convert to, can be either deg, pix, xmm_sky, or
        xmm_det (xmm_sky and xmm_det are defined for this module).
        :return: The converted coordinates.
        :rtype: Quantity
        """
        allowed_units = ["deg", "xmm_sky", "xmm_det", "pix"]
        input_unit = coord_pair.unit.name
        out_name = output_unit.name

        # First off do some type checking
        if not isinstance(coord_pair, Quantity):
            raise TypeError("Please pass an astropy Quantity for the coord_pair.")
        # The coordinate pair must have two elements, no more no less
        elif coord_pair.shape != (2,):
            raise ValueError("Please supply x and y coordinates in one object.")
        # I know the proper way with astropy units is to do .to() but its easier with WCS this way
        elif input_unit not in allowed_units:
            raise UnitsError("Those coordinate units are not supported by this method, "
                             "please use one of these: {}".format(", ".join(allowed_units)))
        elif out_name not in allowed_units:
            raise UnitsError("That output unit is not supported by this method, "
                             "please use one of these: {}".format(", ".join(allowed_units)))

        # Check for presence of the right WCS
        if (input_unit == "xmm_sky" or out_name == "xmm_sky") and self.skyxy_wcs is None:
            raise ValueError("There is no XMM Sky XY WCS associated with this product.")
        elif (input_unit == "xmm_det" or out_name == "xmm_det") and self.detxy_wcs is None:
            raise ValueError("There is no XMM Detector XY WCS associated with this product.")

        # Now to do the actual conversion, which will include checking that the correct WCS is loaded
        # These go between degrees and pixels
        if input_unit == "deg" and out_name == "pix":
            # The second argument all_world2pix defines the origin, for numpy coords it should be 0
            out_coord = Quantity(self.radec_wcs.all_world2pix(*coord_pair, 0), output_unit).astype(int)
        elif input_unit == "pix" and out_name == "deg":
            out_coord = Quantity(self.radec_wcs.all_pix2world(*coord_pair, 0), output_unit)

        # These go between degrees and XMM sky XY coordinates
        elif input_unit == "deg" and out_name == "xmm_sky":
            interim = self.radec_wcs.all_world2pix(*coord_pair, 0)
            out_coord = Quantity(self.skyxy_wcs.all_pix2world(*interim, 0), xmm_sky)
        elif input_unit == "xmm_sky" and out_name == "deg":
            interim = self.skyxy_wcs.all_world2pix(*coord_pair, 0)
            out_coord = Quantity(self.radec_wcs.all_pix2world(*interim, 0), deg)

        # These go between XMM sky XY and pixel coordinates
        elif input_unit == "xmm_sky" and out_name == "pix":
            out_coord = Quantity(self.skyxy_wcs.all_world2pix(*coord_pair, 0), output_unit).astype(int)
        elif input_unit == "pix" and out_name == "xmm_sky":
            out_coord = Quantity(self.skyxy_wcs.all_pix2world(*coord_pair, 0), output_unit)

        # These go between degrees and XMM Det XY coordinates
        elif input_unit == "deg" and out_name == "xmm_det":
            interim = self.radec_wcs.all_world2pix(*coord_pair, 0)
            out_coord = Quantity(self.detxy_wcs.all_pix2world(*interim, 0), xmm_sky)
        elif input_unit == "xmm_det" and out_name == "deg":
            interim = self.detxy_wcs.all_world2pix(*coord_pair, 0)
            out_coord = Quantity(self.radec_wcs.all_pix2world(*interim, 0), deg)

        # These go between XMM det XY and pixel coordinates
        elif input_unit == "xmm_det" and out_name == "pix":
            out_coord = Quantity(self.detxy_wcs.all_world2pix(*coord_pair, 0), output_unit).astype(int)
        elif input_unit == "pix" and out_name == "xmm_det":
            out_coord = Quantity(self.detxy_wcs.all_pix2world(*coord_pair, 0), output_unit)

        # It is possible to convert between XMM coordinates and pixel and supply coordinates
        # outside the range covered by an image, but we can at least catch the error
        if out_name == "pix" and any(coord < 0 for coord in out_coord):
            raise ValueError("Pixel coordinates cannot be less than 0.")
        return out_coord


class ExpMap(Image):
    def __init__(self, path: str, obs_id: str, instrument: str, stdout_str: str, stderr_str: str,
                 gen_cmd: str, lo_en: Quantity, hi_en: Quantity, raise_properly: bool = True):
        super().__init__(path, obs_id, instrument, stdout_str, stderr_str, gen_cmd, lo_en, hi_en, raise_properly)
        self._prod_type = "expmap"

    def exp_time(self, at_coord: Quantity) -> float:
        """
        A simple method that converts the given coordinates to pixels, then finds the exposure time
        at those coordinates.
        :param Quantity at_coord: A pair of coordinates to find the exposure time for.
        :return: The exposure time at the supplied coordinates.
        :rtype: float
        """
        pix_coord = self.coord_conv(at_coord, pix).value
        exp = self.data[pix_coord[0], pix_coord[1]]
        return float(exp)


class EventList(BaseProduct):
    def __init__(self, path: str, obs_id: str, instrument: str, stdout_str: str, stderr_str: str,
                 gen_cmd: str, raise_properly: bool = True):
        super().__init__(path, obs_id, instrument, stdout_str, stderr_str, gen_cmd, raise_properly)
        self._prod_type = "events"


# TODO So these are 'stack' products, with each next step relying on the one before, so while they are a single
#  object they will get the paths of every file that should have been created.
class Spec(BaseProduct):
    def __init__(self, path: str, obs_id: str, instrument: str, stdout_str: str, stderr_str: str,
                 gen_cmd: str, raise_properly: bool = True):
        super().__init__(path, obs_id, instrument, stdout_str, stderr_str, gen_cmd, raise_properly)


class AnnSpec(BaseProduct):
    def __init__(self, path: str, obs_id: str, instrument: str, stdout_str: str, stderr_str: str,
                 gen_cmd: str, raise_properly: bool = True):
        super().__init__(path, obs_id, instrument, stdout_str, stderr_str, gen_cmd, raise_properly)


# Defining a dictionary to map from string product names to their associated classes
PROD_MAP = {"image": Image, "expmap": ExpMap, "events": EventList}





