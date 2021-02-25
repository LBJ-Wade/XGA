#  This code is a part of XMM: Generate and Analyse (XGA), a module designed for the XMM Cluster Survey (XCS).
#  Last modified by David J Turner (david.turner@sussex.ac.uk) 25/02/2021, 13:50. Copyright (c) David J Turner
from ._version import get_versions

__version__ = get_versions()['version']

from .utils import xga_conf, CENSUS, OUTPUT, COMPUTE_MODE, NUM_CORES, XGA_EXTRACT, BASE_XSPEC_SCRIPT, MODEL_PARS, \
    MODEL_UNITS, ABUND_TABLES, XSPEC_FIT_METHOD, COUNTRATE_CONV_SCRIPT, NHC, BLACKLIST, HY_MASS, XGA_PRE_CHECK

del get_versions
