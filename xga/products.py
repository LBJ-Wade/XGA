#  This code is a part of XMM: Generate and Analyse (XGA), a module designed for the XMM Cluster Survey (XCS).
#  Last modified by David J Turner (david.turner@sussex.ac.uk) 10/05/2020, 15:17. Copyright (c) David J Turner

import os
from typing import Tuple, List, Dict

import numpy as np
from astropy.io import fits
from astropy import wcs

from xga.exceptions import SASGenerationError, UnknownCommandlineError, FailedProductError
from xga.utils import SASERROR_LIST, SASWARNING_LIST


class BaseProduct:
    def __init__(self, path: str, stdout_str: str, stderr_str: str, gen_cmd: str, raise_properly: bool = True):
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
        self._sas_error, self._other_error = self.parse_stderr()
        self.og_cmd = gen_cmd

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

    def parse_stderr(self) -> Tuple[List[Dict], List]:
        """
        This method parses the stderr associated with the generation of a product into errors confirmed to have
        come from SAS, and other unidentifiable errors. The SAS errors are returned with the actual error
        name, the error message, and the SAS routine that caused the error.
        :return: A list of dictionaries containing parsed, confirmed SAS errors, and another list of
        unidentifiable errors that occured in the stderr.
        :rtype: Tuple[List[Dict], List]
        """
        # TODO Remove this when I know where the warnings are written to, then update the parser to deal with
        #  warnings as well.
        if "warning" in self.unprocessed_stderr:
            print("FOUND A WARNING IN STDERR")
            SASWARNING_LIST

        # Defined as empty as they are returned by this method
        parsed_sas_errs = []
        other_err_lines = []
        # err_str being "" is ideal, hopefully means that nothing has gone wrong
        if self.unprocessed_stderr != "":
            self.usable = False
            # Errors will be added to the error summary, then raised later
            # That way if people try except the error away the object will have been constructed properly
            err_lines = self.unprocessed_stderr.split('\n')  # Fingers crossed each line is a separate error
            # This is a crude way of looking for SAS error strings ONLY
            sas_err_lines = [line for line in err_lines if "** " in line and ": error" in line]
            for err in sas_err_lines:
                try:
                    # This tries to split out the SAS task that produced the error
                    originator = err.split("** ")[-1].split(":")[0]
                    # And this should split out the actual error name
                    err_ident = err.split(": error (")[-1].split(")")[0]
                    # Actual error message
                    err_body = err.split("({})".format(err_ident))[-1].strip("\n").strip(", ").strip(" ")
                    # Checking to see if the error identity is in the list of SAS errors
                    sas_err_match = [sas_err for sas_err in SASERROR_LIST if err_ident in sas_err]
                    if len(sas_err_match) != 1:
                        originator = ""
                        err_ident = ""
                        err_body = ""
                except IndexError:
                    originator = ""
                    err_ident = ""
                    err_body = ""

                parsed_sas_errs.append({"originator": originator, "name": err_ident,
                                        "message": err_body})
            # These are impossible to predict the form of, so they won't be parsed
            other_err_lines = [line for line in err_lines if line not in sas_err_lines]

        return parsed_sas_errs, other_err_lines

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
                raise SASGenerationError("{e} raised by {t} - {b}".format(e=error["name"], t=error["originator"],
                                                                          b=error["message"]))
            # This is for any unresolved errors.
            for error in self._other_error:
                raise UnknownCommandlineError("{}".format(error))


# TODO Probably add methods for coordinate transforms using the wcses (including radius)
class Image(BaseProduct):
    def __init__(self, path: str, stdout_str: str, stderr_str: str, gen_cmd: str, raise_properly: bool = True):
        """
        The initialisation method for the Image class.
        :param str path: The path to where the product file SHOULD be located.
        :param str stdout_str: The stdout from calling the terminal command.
        :param str stderr_str: The stderr from calling the terminal command.
        :param str gen_cmd: The command used to generate the product.
        :param bool raise_properly: Shall we actually raise the errors as Python errors?
        """
        super().__init__(path, stdout_str, stderr_str, gen_cmd, raise_properly)
        self._im_obj = None
        self._shape = None
        self._wcs_radec = None
        self._wcs_xmmXY = None
        self._wcs_xmmdetXdetY = None

    def _read_on_demand(self):
        """
        Internal method to read the image associated with this Image object into memory when it is requested by
        another method. Doing it on-demand saves on wasting memory.
        """
        if self._im_obj is None and self.usable:
            # Not all images produced by SAS are going to be needed all the time, so they will only be read in if
            # asked for.
            # Using read only mode because it still allows the user to make changes to the object in memory,
            # they just can't overwrite the original image.
            self._im_obj = fits.open(self.path, mode="readonly")
            # As the image must be loaded to know the shape, I've waited until here to set the _shape attribute
            self._shape = self._im_obj["PRIMARY"].data.shape
            # Will actually construct an image WCS as well because why not?
            # XMM images typically have two, both useful, so we'll find all available and store them
            wcses = wcs.find_all_wcs(self._im_obj["PRIMARY"].header)
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

        elif not self.usable:
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
        self._read_on_demand()
        return self._im_obj["PRIMARY"].data

    @data.setter
    def data(self, new_im_arr: np.ndarray):
        """
        Property setter for the image data. As the fits image is loaded in read-only mode,
        this won't alter the actual file (which is what I was going for), but it does allow
        user alterations to the image data they are analysing.
        :param np.ndarray new_im_arr: The new image data.
        """
        # Calling this ensures the image object is read into memory
        self._read_on_demand()

        # Have to make sure the input is of the right type, and the right shape
        if not isinstance(new_im_arr, np.ndarray):
            raise TypeError("You may only assign a numpy array to the data attribute.")
        elif new_im_arr.shape != self.shape:
            raise ValueError("You may only assign a numpy array to the data attribute if it "
                             "is the same shape as the original.")
        else:
            self._im_obj["PRIMARY"].data = new_im_arr

    # This one doesn't get a setter, as I require this WCS to not be none in the _read_on_demand method
    @property
    def radec_wcs(self) -> wcs.WCS:
        """
        Property getter for the WCS that converts back and forth between pixel values
        and RA-DEC coordinates. This one is the only WCS guaranteed to not-None.
        :return: The WCS object for RA and DEC.
        :rtype: wcs.WCS
        """
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
    
    # This absolutely doesn't get a setter considering its the main object with all the information in.
    @property
    def hdulist(self) -> fits.hdu.hdulist.HDUList:
        """
        Property getter allowing access to the astropy fits object created when the image was read in.
        :return: The result of the fits.open call on the product associated with an Image object.
        :rtype: fits.hdu.hdulist.HDUList
        """
        return self._im_obj


class ExpMap(Image):
    def __init__(self, path: str, stdout_str: str, stderr_str: str, gen_cmd: str, raise_properly: bool = True):
        super().__init__(path, stdout_str, stderr_str, gen_cmd, raise_properly)


class Spec(BaseProduct):
    def __init__(self, path: str, stdout_str: str, stderr_str: str, gen_cmd: str, raise_properly: bool = True):
        super().__init__(path, stdout_str, stderr_str, gen_cmd, raise_properly)


class AnnSpec(BaseProduct):
    def __init__(self, path: str, stdout_str: str, stderr_str: str, gen_cmd: str, raise_properly: bool = True):
        super().__init__(path, stdout_str, stderr_str, gen_cmd, raise_properly)






