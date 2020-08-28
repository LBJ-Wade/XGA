#  This code is a part of XMM: Generate and Analyse (XGA), a module designed for the XMM Cluster Survey (XCS).
#  Last modified by David J Turner (david.turner@sussex.ac.uk) 24/08/2020, 11:36. Copyright (c) David J Turner

from subprocess import Popen, PIPE

from astropy.cosmology import Planck15
from astropy.units.quantity import Quantity
from numpy import array, ndarray, pi

from xga.exceptions import HeasoftError


def nh_lookup(src_ra: float, src_dec: float) -> ndarray:
    """
    Uses HEASOFT to lookup hydrogen column density for given coordinates.
    :param float src_ra: Right Ascension of object
    :param float src_dec: Declination of object
    :return : Average and weighted average nH values (in units of cm^-2)
    :rtype: ndarray
    """
    # Apparently minimal type-checking is the Python way, but for some reason this heasoft command fails if
    # integers are passed, so I'll convert them, let them TypeError if people pass weird types.
    src_ra = float(src_ra)
    src_dec = float(src_dec)

    heasoft_cmd = 'nh 2000 {ra} {dec}'.format(ra=src_ra, dec=src_dec)

    out, err = Popen(heasoft_cmd, stdout=PIPE, stderr=PIPE, shell=True).communicate()
    # Catch errors from stderr
    if err.decode("UTF-8") != '':
        # Going to assume top line of error most important, and strip out the error type from the string
        msg = err.decode("UTF-8").split('\n')[0].split(':')[-1].strip(' ')
        print(out.decode("UTF-8"))  # Sometimes this also has useful information
        raise HeasoftError(msg)

    heasoft_output = out.decode("utf-8")
    lines = heasoft_output.split('\n')
    try:
        average_nh = lines[-3].split(' ')[-1]
        weighed_av_nh = lines[-2].split(' ')[-1]
    except IndexError:
        raise HeasoftError("HEASOFT nH command output is not as expected")

    try:
        average_nh = float(average_nh)
        weighed_av_nh = float(weighed_av_nh)
    except ValueError:
        raise HeasoftError("HEASOFT nH command scraped output cannot be converted to float")

    # Returns both the average and weighted average nH values, as output by HEASOFT nH tool.
    nh_vals = Quantity(array([average_nh, weighed_av_nh]) / 10**22, "10^22 cm^-2")
    return nh_vals


def rad_to_ang(rad: Quantity, z: float, cosmo=Planck15) -> Quantity:
    """
    Converts radius in length units to radius on sky in degrees.
    :param Quantity rad: Radius for conversion.
    :param Cosmology cosmo: An instance of an astropy cosmology, the default is Planck15.
    :param float z: The _redshift of the source.
    :return: The radius in degrees.
    :rtype: Quantity
    """
    d_a = cosmo.angular_diameter_distance(z)
    ang_rad = (rad.to("Mpc") / d_a).to('').value * (180 / pi)
    return Quantity(ang_rad, 'deg')


def ang_to_rad(ang: Quantity, z: float, cosmo=Planck15) -> Quantity:
    """
    The counterpart to rad_to_ang, this converts from an angle to a radius in kpc.
    :param Quantity ang: Angle to be converted to radius.
    :param Cosmology cosmo: An instance of an astropy cosmology, the default is Planck15.
    :param float z: The _redshift of the source.
    :return: The radius in kpc.
    :rtype: Quantity
    """
    d_a = cosmo.angular_diameter_distance(z)
    rad = (ang.to("deg").value * (pi / 180) * d_a).to("kpc")
    return rad