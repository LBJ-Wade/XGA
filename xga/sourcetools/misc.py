#  This code is a part of XMM: Generate and Analyse (XGA), a module designed for the XMM Cluster Survey (XCS).
#  Last modified by David J Turner (david.turner@sussex.ac.uk) 04/01/2021, 20:04. Copyright (c) David J Turner

from subprocess import Popen, PIPE

from astropy.coordinates import SkyCoord
from astropy.cosmology import Planck15
from astropy.units import Quantity
from numpy import array, ndarray, pi

from ..exceptions import HeasoftError


def nh_lookup(coord_pair: Quantity) -> ndarray:
    """
    Uses HEASOFT to lookup hydrogen column density for given coordinates.

    :param Quantity coord_pair: An astropy quantity with RA and DEC of interest.
    :return : Average and weighted average nH values (in units of cm^-2)
    :rtype: ndarray
    """
    # Apparently minimal type-checking is the Python way, but for some reason this heasoft command fails if
    # integers are passed, so I'll convert them, let them TypeError if people pass weird types.
    pos_deg = coord_pair.to("deg")
    src_ra = float(pos_deg.value[0])
    src_dec = float(pos_deg.value[1])

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


def name_to_coord(name: str):
    """
    I'd like it to be known that I hate papers and resources that either only give the name of an object
    or its sexagesimal coordinates - however it happens upsettingly often so here we are. This function will
    take a standard format name (e.g. XMMXCS J041853.9+555333.7) and return RA and DEC in degrees.

    :param name:
    """
    raise NotImplementedError("I started this and will finish it at some point, but I got bored.")
    if " " in name:
        survey, coord_str = name.split(" ")
        coord_str = coord_str[1:]
    elif "J" in name:
        survey, coord_str = name.sdplit("J")
    else:
        num_search = [d.isdigit() for d in name].index(True)
        survey = name[:num_search]
        coord_str = name[num_search:]

    if "+" in coord_str:
        ra, dec = coord_str.split("+")
    elif "-" in coord_str:
        ra, dec = coord_str.split("-")
        dec = "-" + dec
    else:
        raise ValueError("There doesn't seem to be a + or - in the object name.")


def coord_to_name(coord_pair: Quantity, survey: str = None) -> str:
    """
    This was originally just written in the init of BaseSource, but I figured I should split it out
    into its own function really. This will take a coordinate pair, and optional survey name, and spit
    out an object name in the standard format.

    :return: Source name based on coordinates.
    :rtype: str
    """
    s = SkyCoord(ra=coord_pair[0], dec=coord_pair[1])
    crd_str = s.to_string("hmsdms").replace("h", "").replace("m", "").replace("s", "").replace("d", "")
    ra_str, dec_str = crd_str.split(" ")
    # A bug popped up where a conversion ended up with no decimal point and the return part got
    #  really upset - so this adds one if there isn't one
    if "." not in ra_str:
        ra_str += "."
    if "." not in dec_str:
        dec_str += "."

    if survey is None:
        name = "J" + ra_str[:ra_str.index(".") + 2] + dec_str[:dec_str.index(".") + 2]
    else:
        name = survey + "J" + ra_str[:ra_str.index(".") + 2] + dec_str[:dec_str.index(".") + 2]

    return name












