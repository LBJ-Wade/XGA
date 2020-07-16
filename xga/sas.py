#  This code is a part of XMM: Generate and Analyse (XGA), a module designed for the XMM Cluster Survey (XCS).
#  Last modified by David J Turner (david.turner@sussex.ac.uk) 17/07/2020, 00:03. Copyright (c) David J Turner

import os
import warnings
from multiprocessing.dummy import Pool
from shutil import rmtree
from subprocess import Popen, PIPE
from typing import List, Tuple

import numpy as np
from astropy.units import Quantity, deg
from tqdm import tqdm

from xga import OUTPUT, COMPUTE_MODE, NUM_CORES
from xga.exceptions import SASNotFoundError, SASInputInvalid, NoProductAvailableError
from xga.imagetools import data_limits
from xga.products import BaseProduct, Image, ExpMap, Spectrum, PSFGrid
from xga.sources import BaseSource, ExtendedSource, GalaxyCluster
from xga.utils import energy_to_channel, xmm_sky

if "SAS_DIR" not in os.environ:
    raise SASNotFoundError("SAS_DIR environment variable is not set, "
                           "unable to verify SAS is present on system")
else:
    # This way, the user can just import the SAS_VERSION from this utils code
    sas_out, sas_err = Popen("sas --version", stdout=PIPE, stderr=PIPE, shell=True).communicate()
    SAS_VERSION = sas_out.decode("UTF-8").strip("]\n").split('-')[-1]


def execute_cmd(cmd: str, p_type: str, p_path: list, extra_info: dict, src: str) -> Tuple[BaseProduct, str]:
    """
    This function is called for the local compute option, and runs the passed command in a Popen shell.
    It then creates an appropriate product object, and passes it back to the callback function of the Pool
    it was called from.
    :param str cmd: SAS command to be executed on the command line.
    :param str p_type: The product type that will be produced by this command.
    :param str p_path: The final output path of the product.
    :param dict extra_info: Any extra information required to define the product object.
    :param str src: A string representation of the source object that this product is associated with.
    :return: The product object, and the string representation of the associated source object.
    :rtype: Tuple[BaseProduct, str]
    """
    out, err = Popen(cmd, shell=True, stdout=PIPE, stderr=PIPE).communicate()
    out = out.decode("UTF-8")
    err = err.decode("UTF-8")

    if p_type == "image":
        # Maybe let the user decide not to raise errors detected in stderr
        prod = Image(p_path[0], extra_info["obs_id"], extra_info["instrument"], out, err, cmd,
                     extra_info["lo_en"], extra_info["hi_en"])
        if "psf_corr" in extra_info and extra_info["psf_corr"]:
            prod.psf_corrected = True
            prod.psf_bins = extra_info["psf_bins"]
            prod.psf_model = extra_info["psf_model"]
            prod.psf_iterations = extra_info["psf_iter"]
            prod.psf_algorithm = extra_info["psf_algo"]
    elif p_type == "expmap":
        prod = ExpMap(p_path[0], extra_info["obs_id"], extra_info["instrument"], out, err, cmd,
                      extra_info["lo_en"], extra_info["hi_en"])
    elif p_type == "ccf":
        # ccf files may not be destined to spend life as product objects, but that doesn't mean
        # I can't take momentarily advantage of the error parsing I built into the product classes
        prod = BaseProduct(p_path[0], "", "", out, err, cmd)
        prod = None
    elif p_type == "spectrum":
        prod = Spectrum(p_path[0], extra_info["rmf_path"], extra_info["arf_path"], extra_info["b_spec_path"],
                        extra_info["b_rmf_path"], extra_info["b_arf_path"], extra_info["reg_type"],
                        extra_info["obs_id"], extra_info["instrument"], out, err, cmd)
    elif p_type == "psf":
        prod = PSFGrid(extra_info["files"], extra_info["chunks_per_side"], extra_info["model"],
                       extra_info["x_bounds"], extra_info["y_bounds"], extra_info["obs_id"],
                       extra_info["instrument"], out, err, cmd)
    else:
        raise NotImplementedError("Not implemented yet")

    return prod, src


def sas_call(sas_func):
    """
    This is used as a decorator for functions that produce SAS command strings. Depending on the
    system that XGA is running on (and whether the user requests parallel execution), the method of
    executing the SAS command will change. This supports both simple multi-threading and submission
    with the Sun Grid Engine.
    :return:
    """
    def wrapper(*args, **kwargs):
        # The first argument of all of these SAS functions will be the source object (or a list of),
        # so rather than return them from the sas function I'll just access them like this.
        if isinstance(args[0], BaseSource):
            sources = [args[0]]
        elif isinstance(args[0], list):
            sources = args[0]
        else:
            raise TypeError("Please pass a source object, or a list of source objects.")
        src_lookup = [repr(src) for src in sources]

        # This is the output from whatever function this is a decorator for
        cmd_list, to_stack, to_execute, cores, p_type, paths, extra_info = sas_func(*args, **kwargs)

        all_run = []  # Combined command list for all sources
        all_type = []  # Combined expected type list for all sources
        all_path = []  # Combined expected path list for all sources
        all_extras = []  # Combined extra information list for all sources
        source_rep = []  # For repr calls of each source object, needed for assigning products to sources
        for ind in range(len(cmd_list)):
            source: BaseSource = sources[ind]
            if len(cmd_list[ind]) > 0:
                # If there are commands to add to a source queue, then do it
                source.update_queue(cmd_list[ind], p_type[ind], paths[ind], extra_info[ind], to_stack)

            # If we do want to execute the commands this time round, we read them out for all sources
            # and add them to these master lists
            if to_execute:
                to_run, expected_type, expected_path, extras = source.get_queue()
                all_run += to_run
                all_type += expected_type
                all_path += expected_path
                all_extras += extras
                source_rep += [repr(source)] * len(to_run)

        # This is what the returned products get stored in before they're assigned to sources
        results = {s: [] for s in src_lookup}
        if to_execute and COMPUTE_MODE == "local" and len(all_run) > 0:
            # Will run the commands locally in a pool
            raised_errors = []
            prod_type_str = ", ".join(set(all_type))
            with tqdm(total=len(all_run), desc="Generating products of type(s) " + prod_type_str) as gen, \
                    Pool(cores) as pool:
                def callback(results_in: Tuple[BaseProduct, str]):
                    """
                    Callback function for the apply_async pool method, gets called when a task finishes
                    and something is returned.
                    :param Tuple[BaseProduct, str] results_in: Results of the command call.
                    """
                    nonlocal gen  # The progress bar will need updating
                    nonlocal results  # The dictionary the command call results are added to
                    if results_in[0] is None:
                        gen.update(1)
                        return
                    else:
                        prod_obj, rel_src = results_in
                        results[rel_src].append(prod_obj)
                        gen.update(1)

                def err_callback(err):
                    """
                    The callback function for errors that occur inside a task running in the pool.
                    :param err: An error that occurred inside a task.
                    """
                    nonlocal raised_errors
                    nonlocal gen

                    if err is not None:
                        # Rather than throwing an error straight away I append them all to a list for later.
                        raised_errors.append(err)
                    gen.update(1)

                for cmd_ind, cmd in enumerate(all_run):
                    # These are just the relevant entries in all these lists for the current command
                    # Just defined like this to save on line length for apply_async call.
                    exp_type = all_type[cmd_ind]
                    exp_path = all_path[cmd_ind]
                    ext = all_extras[cmd_ind]
                    src = source_rep[cmd_ind]
                    pool.apply_async(execute_cmd, args=(str(cmd), str(exp_type), exp_path, ext, src),
                                     error_callback=err_callback, callback=callback)
                pool.close()  # No more tasks can be added to the pool
                pool.join()  # Joins the pool, the code will only move on once the pool is empty.

                for error in raised_errors:
                    raise error

        elif to_execute and COMPUTE_MODE == "sge" and len(all_run) > 0:
            # This section will run the code on an HPC that uses the Sun Grid Engine for job submission.
            raise NotImplementedError("How did you even get here?")

        elif to_execute and COMPUTE_MODE == "slurm" and len(all_run) > 0:
            # This section will run the code on an HPC that uses slurm for job submission.
            raise NotImplementedError("How did you even get here?")

        elif to_execute and len(all_run) == 0:
            # It is possible to call a wrapped SAS function and find that the products already exist.
            # print("All requested products already exist")
            pass

        # Now we assign products to source objects
        for entry in results:
            # Made this lookup list earlier, using string representations of source objects.
            # Finds the ind of the list of sources that we should add this set of products to
            ind = src_lookup.index(entry)
            for product in results[entry]:
                # For each product produced for this source, we add it to the storage hierarchy
                sources[ind].update_products(product)

        # If only one source was passed, turn it back into a source object rather than a source
        # object in a list.
        if len(sources) == 1:
            sources = sources[0]
        return sources
    return wrapper


# TODO Perhaps remove the option to add to the SAS expression
@sas_call
def evselect_image(sources: List[BaseSource], lo_en: Quantity, hi_en: Quantity,
                   add_expr: str = "", num_cores: int = NUM_CORES):
    """
    A convenient Python wrapper for a configuration of the SAS evselect command that makes images.
    Images will be generated for every observation associated with every source passed to this function.
    If images in the requested energy band are already associated with the source,
    they will not be generated again
    :param List[BaseSource] sources: A single source object, or a list of source objects.
    :param Quantity lo_en: The lower energy limit for the image, in astropy energy units.
    :param Quantity hi_en: The upper energy limit for the image, in astropy energy units.
    :param str add_expr: A string to be added to the SAS expression keyword
    :param int num_cores: The number of cores to use (if running locally), default is set to
    90% of available.
    """
    stack = False  # This tells the sas_call routine that this command won't be part of a stack
    execute = True  # This should be executed immediately
    # This function supports passing both individual sources and sets of sources
    if isinstance(sources, BaseSource):
        sources = [sources]

    # Don't do much value checking in this module, but this one is so fundamental that I will do it
    if lo_en > hi_en:
        raise ValueError("lo_en cannot be greater than hi_en")
    else:
        # Calls a useful little function that takes an astropy energy quantity to the XMM channels
        # required by SAS commands
        lo_chan = energy_to_channel(lo_en)
        hi_chan = energy_to_channel(hi_en)

    expr = " && ".join([e for e in ["expression='(PI in [{l}:{u}])".format(l=lo_chan, u=hi_chan),
                                    add_expr] if e != ""]) + "'"
    # These lists are to contain the lists of commands/paths/etc for each of the individual sources passed
    # to this function
    sources_cmds = []
    sources_paths = []
    sources_extras = []
    sources_types = []
    for source in sources:
        cmds = []
        final_paths = []
        extra_info = []
        # Check which event lists are associated with each individual source
        for pack in source.get_products("events", just_obj=False):
            obs_id = pack[0]
            inst = pack[1]

            if not os.path.exists(OUTPUT + obs_id):
                os.mkdir(OUTPUT + obs_id)

            en_id = "bound_{l}-{u}".format(l=lo_en.value, u=hi_en.value)
            exists = [match for match in source.get_products("image", obs_id, inst, just_obj=False)
                      if en_id in match]
            if len(exists) == 1 and exists[0][-1].usable:
                continue

            evt_list = pack[-1]
            dest_dir = OUTPUT + "{o}/{i}_{l}-{u}_temp/".format(o=obs_id, i=inst, l=lo_en.value, u=hi_en.value)
            im = "{o}_{i}_{l}-{u}keVimg.fits".format(o=obs_id, i=inst, l=lo_en.value, u=hi_en.value)

            # If something got interrupted and the temp directory still exists, this will remove it
            if os.path.exists(dest_dir):
                rmtree(dest_dir)

            os.makedirs(dest_dir)
            cmds.append("cd {d};evselect table={e} imageset={i} xcolumn=X ycolumn=Y ximagebinsize=87 "
                        "yimagebinsize=87 squarepixels=yes ximagesize=512 yimagesize=512 imagebinning=binSize "
                        "ximagemin=3649 ximagemax=48106 withxranges=yes yimagemin=3649 yimagemax=48106 "
                        "withyranges=yes {ex}; mv * ../; cd ..; rm -r {d}".format(d=dest_dir, e=evt_list.path,
                                                                                  i=im, ex=expr))

            # This is the products final resting place, if it exists at the end of this command
            final_paths.append(os.path.join(OUTPUT, obs_id, im))
            extra_info.append({"lo_en": lo_en, "hi_en": hi_en, "obs_id": obs_id, "instrument": inst})
        sources_cmds.append(np.array(cmds))
        sources_paths.append(np.array(final_paths))
        # This contains any other information that will be needed to instantiate the class
        # once the SAS cmd has run
        sources_extras.append(np.array(extra_info))
        sources_types.append(np.full(sources_cmds[-1].shape, fill_value="image"))

    # I only return num_cores here so it has a reason to be passed to this function, really
    # it could just be picked up in the decorator.
    return sources_cmds, stack, execute, num_cores, sources_types, sources_paths, sources_extras


@sas_call
def cifbuild(sources: List[BaseSource], num_cores: int = NUM_CORES):
    """
    A wrapper for the XMM cifbuild command, which will be run before many of the more complex
    SAS commands, to check that a CIF compatible with the local version of SAS is available.
    :param List[BaseSource] sources: A single source object, or a list of source objects.
    :param int num_cores: The number of cores to use (if running locally), default is set to
    90% of available.
    """
    # This function supports passing both individual sources and sets of sources
    if isinstance(sources, BaseSource):
        sources = [sources]

    # This string contains the bash code to run cifbuild
    cif_cmd = "cd {d}; export SAS_ODF={odf}; cifbuild calindexset=ccf.cif; unset SAS_ODF"

    sources_cmds = []
    sources_paths = []
    sources_extras = []
    sources_types = []
    for source in sources:
        cmds = []
        final_paths = []
        extra_info = []
        for obs_id in source.obs_ids:
            odf_path = source.get_odf_path(obs_id)

            if not os.path.exists(OUTPUT + obs_id):
                os.mkdir(OUTPUT + obs_id)

            dest_dir = "{out}{obs}/".format(out=OUTPUT, obs=obs_id)
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir)
            final_path = dest_dir + "ccf.cif"
            if not os.path.exists(final_path):
                cmds.append(cif_cmd.format(d=dest_dir, odf=odf_path))
                final_paths.append(final_path)
                extra_info.append({})  # This doesn't need any extra information

        sources_cmds.append(np.array(cmds))
        sources_paths.append(np.array(final_paths))
        sources_extras.append(np.array(extra_info))
        sources_types.append(np.full(sources_cmds[-1].shape, fill_value="ccf"))

    stack = False  # This tells the sas_call routine that this command won't be part of a stack
    execute = True  # This should be executed immediately

    return sources_cmds, stack, execute, num_cores, sources_types, sources_paths, sources_extras


@sas_call
def eexpmap(sources: List[BaseSource], lo_en: Quantity, hi_en: Quantity, num_cores: int = NUM_CORES):
    """
    A convenient Python wrapper for the SAS eexpmap command.
    Expmaps will be generated for every observation associated with every source passed to this function.
    If expmaps in the requested energy band are already associated with the source,
    they will not be generated again
    :param List[BaseSource] sources: A single source object, or a list of source objects.
    :param Quantity lo_en: The lower energy limit for the expmap, in astropy energy units.
    :param Quantity hi_en: The upper energy limit for the expmap, in astropy energy units.
    :param int num_cores: The number of cores to use (if running locally), default is set to
    90% of available.
    """
    # I know that a lot of this code is the same as the evselect_image code, but its 1am so please don't
    # judge me too much.

    # This function supports passing both individual sources and sets of sources
    if isinstance(sources, BaseSource):
        sources = [sources]

    # Don't do much value checking in this module, but this one is so fundamental that I will do it
    if lo_en > hi_en:
        raise ValueError("lo_en cannot be greater than hi_en")
    else:
        # Calls a useful little function that takes an astropy energy quantity to the XMM channels
        # required by SAS commands
        lo_chan = energy_to_channel(lo_en)
        hi_chan = energy_to_channel(hi_en)

    # These are crucial, to generate an exposure map one must have a ccf.cif calibration file, and a reference
    # image. If they do not already exist, these commands should generate them.
    cifbuild(sources)
    sources = evselect_image(sources, lo_en, hi_en)
    # This is necessary because the decorator will reduce a one element list of source objects to a single
    # source object. Useful for the user, not so much here where the code expects an iterable.
    if not isinstance(sources, list):
        sources = [sources]

    # These lists are to contain the lists of commands/paths/etc for each of the individual sources passed
    # to this function
    sources_cmds = []
    sources_paths = []
    sources_extras = []
    sources_types = []
    for source in sources:
        cmds = []
        final_paths = []
        extra_info = []
        # Check which event lists are associated with each individual source
        for pack in source.get_products("events", just_obj=False):
            obs_id = pack[0]
            inst = pack[1]

            if not os.path.exists(OUTPUT + obs_id):
                os.mkdir(OUTPUT + obs_id)

            en_id = "bound_{l}-{u}".format(l=lo_en.value, u=hi_en.value)
            exists = [match for match in source.get_products("expmap", obs_id, inst, just_obj=False)
                      if en_id in match]
            if len(exists) == 1 and exists[0][-1].usable:
                continue
            # Generating an exposure map requires a reference image.
            ref_im = [match for match in source.get_products("image", obs_id, inst, just_obj=False)
                      if en_id in match][0][-1]
            # It also requires an attitude file
            att = source.get_att_file(obs_id)
            # Set up the paths and names of files
            evt_list = pack[-1]
            dest_dir = OUTPUT + "{o}/{i}_{l}-{u}_temp/".format(o=obs_id, i=inst, l=lo_en.value, u=hi_en.value)
            exp_map = "{o}_{i}_{l}-{u}keVexpmap.fits".format(o=obs_id, i=inst, l=lo_en.value, u=hi_en.value)

            # If something got interrupted and the temp directory still exists, this will remove it
            if os.path.exists(dest_dir):
                rmtree(dest_dir)

            os.makedirs(dest_dir)
            # TODO Maybe support det coords
            cmds.append("cd {d}; cp ../ccf.cif .; export SAS_CCF={ccf}; eexpmap eventset={e} "
                        "imageset={im} expimageset={eim} withdetcoords=no withvignetting=yes "
                        "attitudeset={att} pimin={l} pimax={u}; mv * ../; cd ..; "
                        "rm -r {d}".format(e=evt_list.path, im=ref_im.path, eim=exp_map, att=att, l=lo_chan,
                                           u=hi_chan, d=dest_dir, ccf=dest_dir + "ccf.cif"))

            # This is the products final resting place, if it exists at the end of this command
            final_paths.append(os.path.join(OUTPUT, obs_id, exp_map))
            extra_info.append({"lo_en": lo_en, "hi_en": hi_en, "obs_id": obs_id, "instrument": inst})
        sources_cmds.append(np.array(cmds))
        sources_paths.append(np.array(final_paths))
        # This contains any other information that will be needed to instantiate the class
        # once the SAS cmd has run
        sources_extras.append(np.array(extra_info))
        sources_types.append(np.full(sources_cmds[-1].shape, fill_value="expmap"))

    stack = False  # This tells the sas_call routine that this command won't be part of a stack
    execute = True  # This should be executed immediately
    # I only return num_cores here so it has a reason to be passed to this function, really
    # it could just be picked up in the decorator.
    return sources_cmds, stack, execute, num_cores, sources_types, sources_paths, sources_extras


@sas_call
def emosaic(sources: List[BaseSource], to_mosaic: str, lo_en: Quantity, hi_en: Quantity, psf_corr: bool = False,
            psf_model: str = "ELLBETA", psf_bins: int = 4, psf_algo: str = "rl", psf_iter: int = 15,
            num_cores: int = NUM_CORES):
    """
    A convenient Python wrapper for the SAS emosaic command. Every image associated with the source,
    that is in the energy band specified by the user, will be added together.
    :param List[BaseSource] sources: A single source object, or a list of source objects.
    :param str to_mosaic: The data type to produce a mosaic for, can be either image or expmap.
    :param Quantity lo_en: The lower energy limit for the combined image, in astropy energy units.
    :param Quantity hi_en: The upper energy limit for the combined image, in astropy energy units.
    :param bool psf_corr: If True, PSF corrected images will be mosaiced.
    :param str psf_model: If PSF corrected, the PSF model used.
    :param int psf_bins: If PSF corrected, the number of bins per side.
    :param str psf_algo: If PSF corrected, the algorithm used.
    :param int psf_iter: If PSF corrected, the number of algorithm iterations.
    :param int num_cores: The number of cores to use (if running locally), default is set to
    90% of available.
    """
    # This function supports passing both individual sources and sets of sources
    if isinstance(sources, BaseSource):
        sources = [sources]

    if to_mosaic not in ["image", "expmap"]:
        raise ValueError("The only valid choices for to_mosaic are image and expmap.")
    # Don't do much value checking in this module, but this one is so fundamental that I will do it
    elif lo_en > hi_en:
        raise ValueError("lo_en cannot be greater than hi_en")

    # To make a mosaic we need to have the individual products in the first place
    if to_mosaic == "image":
        sources = evselect_image(sources, lo_en, hi_en)
        for_name = "img"
    elif to_mosaic == "expmap":
        sources = eexpmap(sources, lo_en, hi_en)
        for_name = "expmap"

    # This is necessary because the decorator will reduce a one element list of source objects to a single
    # source object. Useful for the user, not so much here where the code expects an iterable.
    if not isinstance(sources, list):
        sources = [sources]

    mosaic_cmd = "cd {d}; emosaic imagesets='{ims}' mosaicedset={mim}"

    sources_cmds = []
    sources_paths = []
    sources_extras = []
    sources_types = []
    for source in sources:
        en_id = "bound_{l}-{u}".format(l=lo_en.value, u=hi_en.value)
        # If we're mosaicing PSF corrected images, we need to
        if psf_corr and to_mosaic == "expmap":
            raise ValueError("There can be no PSF corrected expmaps to mosaic, it doesn't make sense.")
        elif psf_corr:
            en_id += "_" + psf_model + "_" + str(psf_bins) + "_" + psf_algo + str(psf_iter)

        # Checking if the combined product already exists
        exists = [match for match in source.get_products("combined_{}".format(to_mosaic), just_obj=False)
                  if en_id in match]
        if len(exists) == 1 and exists[0][-1].usable:
            continue

        # This fetches all image objects with the passed energy bounds
        matches = [[match[0], match[-1]] for match in source.get_products(to_mosaic, just_obj=False)
                   if en_id in match]
        paths = [product[1].path for product in matches if product[1].usable]
        obs_ids = [product[0] for product in matches if product[1].usable]
        obs_ids_set = []
        for obs_id in obs_ids:
            if obs_id not in obs_ids_set:
                obs_ids_set.append(obs_id)

            if not os.path.exists(OUTPUT + obs_id):
                os.mkdir(OUTPUT + obs_id)

        # The problem I have here is that merged images don't belong to a particular ObsID, so where do they
        # go in the xga_output folder? I've arbitrarily decided to save it in the folder of the first ObsID
        # associated with a given source.
        dest_dir = OUTPUT + "{o}/".format(o=obs_ids_set[0])
        if not psf_corr:
            mosaic = "{os}_{l}-{u}keVmerged_{t}.fits".format(os="_".join(obs_ids_set), l=lo_en.value, u=hi_en.value,
                                                             t=for_name)
        else:
            mosaic = "{os}_{b}bin_{it}iter_{m}mod_{a}algo_{l}-{u}keVpsfcorr_merged_img." \
                     "fits".format(os="_".join(obs_ids_set), l=lo_en.value, u=hi_en.value, b=psf_bins, it=psf_iter,
                                   a=psf_algo, m=psf_model)

        sources_cmds.append(np.array([mosaic_cmd.format(ims=" ".join(paths), mim=mosaic, d=dest_dir)]))
        sources_paths.append(np.array([dest_dir + mosaic]))
        # This contains any other information that will be needed to instantiate the class
        # once the SAS cmd has run
        # The 'combined' values for obs and inst here are crucial, they will tell the source object that the final
        # product is assigned to that these are merged products - combinations of all available data
        sources_extras.append(np.array([{"lo_en": lo_en, "hi_en": hi_en, "obs_id": "combined",
                                         "instrument": "combined", "psf_corr": psf_corr, "psf_algo": psf_algo,
                                         "psf_model": psf_model, "psf_iter": psf_iter, "psf_bins": psf_bins}]))
        sources_types.append(np.full(sources_cmds[-1].shape, fill_value=to_mosaic))

    stack = False  # This tells the sas_call routine that this command won't be part of a stack
    execute = True  # This should be executed immediately
    # I only return num_cores here so it has a reason to be passed to this function, really
    # it could just be picked up in the decorator.
    return sources_cmds, stack, execute, num_cores, sources_types, sources_paths, sources_extras


@sas_call
def psfgen(sources: List[BaseSource], bins: int = 4, psf_model: str = "ELLBETA", num_cores: int = NUM_CORES):
    """
    A wrapper for the psfgen SAS task. Used to generate XGA PSF objects, which in turn can be used to correct
    XGA images/ratemaps for optical effects. By default we use the ELLBETA model reported in Read et al. 2011
    (doi:10.1051/0004-6361/201117525), and generate a grid of binsxbins PSFs that can be used
    to correct for the PSF over an entire image. The energy dependence of the PSF is assumed to be minimal, and the
    resultant PSF object will be paired up with an image that matches it's ObsID and instrument.
    :param List[BaseSource] sources: A single source object, or a list of source objects.
    :param int bins: The image coordinate space will be divided into a grid of size binsxbins, PSFs will be
    generated at the central coordinates of the grid chunks.
    :param str psf_model: Which model to use when generating the PSF, default is ELLBETA, the best available.
    :param int num_cores: The number of cores to use (if running locally), default is set to
    90% of available.
    """
    stack = False  # This tells the sas_call routine that this command won't be part of a stack
    execute = True  # This should be executed immediately

    psf_model = psf_model.upper()
    allowed_models = ["ELLBETA", "LOW", "MEDIUM", "EXTENDED", "HIGH"]
    if psf_model not in allowed_models:
        raise SASInputInvalid("{0} is not a valid PSF model. Allowed models are "
                              "{1}".format(psf_model, ", ".join(allowed_models)))
    elif bins > 10:
        raise ValueError("While I appreciate your desire for fine binning, I think {0}x{0} bins would"
                         " probably take too long...".format(bins))

    # Need a valid CIF for this task, so run cifbuild first.from
    cifbuild(sources)

    # This is necessary because the decorator will reduce a one element list of source objects to a single
    # source object. Useful for the user, not so much here where the code expects an iterable.
    if not isinstance(sources, list):
        sources = [sources]

    # These lists are to contain the lists of commands/paths/etc for each of the individual sources passed
    # to this function
    sources_cmds = []
    sources_paths = []
    sources_extras = []
    sources_types = []
    for source in sources:
        cmds = []
        final_paths = []
        extra_info = []
        # Check which event lists are associated with each individual source
        for pack in source.get_products("events", just_obj=False):
            obs_id = pack[0]
            inst = pack[1]

            if not os.path.exists(OUTPUT + obs_id):
                os.mkdir(OUTPUT + obs_id)

            # This looks for any image for this ObsID, instrument combo - it does assume that whatever
            #  it finds will be the same resolution as any images in other energy bands that XGA will
            #  create in the future.
            images = source.get_products("image", obs_id, inst, just_obj=True)

            if len(images) == 0:
                raise NoProductAvailableError("There is no image available for {o} {i}, please generate "
                                              "images before PSFs".format(o=obs_id, i=inst))

            # Checking if the Image products are the same shape that XGA makes
            res_match = [im for im in images if im.shape == (512, 512)]
            if len(res_match) == 0:
                raise NoProductAvailableError("There is an image associated with {o} {i}, but it doesn't"
                                              " appear to be at the resolution XGA uses - this is not "
                                              "supported yet.")
            else:
                image = res_match[0]

            # Here we try and find if this PSF configuration has already been run and has been
            #  associated with the source. If so then don't do it again.
            psfs = source.get_products("psf", obs_id, inst, extra_key=psf_model + "_" + str(bins))
            if len(psfs) != 0:
                continue

            # This part is where we decide on the RA DEC coordinates for the centres of each
            #  PSF in our grid
            # This function gives us x and y limits for where there is data in an image, they are used as start
            #  and end coordinates for our bins so the PSFs are more focused on where there is actually data.
            x_lims, y_lims = data_limits(image)
            # Simple calculation to calculate step size in pixels, so how long each chunk will be in
            #  x and y directions
            x_step = (x_lims[1] - x_lims[0]) / bins
            y_step = (y_lims[1] - y_lims[0]) / bins

            # These are the x and y bin centre coordinates - when converted to RA and DEC this is where the
            #  PSF is generated at.
            x_cen_coords = np.arange(*x_lims, x_step) + (x_step / 2)
            y_cen_coords = np.arange(*y_lims, y_step) + (y_step / 2)

            # Get all combinations of the central coordinates using meshgrid, then turn them into
            #  an N row, 2 column numpy array of pixel coordinates for easy conversion to RA-DEC.
            pix_mesh = np.meshgrid(x_cen_coords, y_cen_coords)
            pix_coords = Quantity(np.stack([pix_mesh[0].ravel(), pix_mesh[1].ravel()]).T, 'pix')

            # But I also want to know the boundaries of the bins so I can easily select which parts of
            #  the image belong with each PSF in the grid
            x_boundaries = np.linspace(*x_lims, bins+1)
            y_boundaries = np.linspace(*y_lims, bins+1)

            # These two arrays give the x and y boundaries of the bins in the same order as the pix_coords array
            x_bound_coords = np.tile(np.stack([x_boundaries[0: -1].ravel(), x_boundaries[1:].ravel()]).T,
                                     (bins, 1))
            x_bound_coords = x_bound_coords.round(0).astype(int)

            y_bound_coords = np.repeat(np.stack([y_boundaries[0: -1].ravel(), y_boundaries[1:].ravel()]).T,
                                       bins, 0)
            y_bound_coords = y_bound_coords.round(0).astype(int)

            ra_dec_coords = image.coord_conv(pix_coords, deg)

            dest_dir = OUTPUT + "{o}/{i}_temp/".format(o=obs_id, i=inst)
            psf = "{o}_{i}_{b}bin_{m}mod_{ra}_{dec}_psf.fits"

            # The change directory and SAS setup commands
            init_cmd = "cd {d}; cp ../ccf.cif .; export SAS_CCF={ccf}; ".format(d=dest_dir,
                                                                                ccf=dest_dir + "ccf.cif")

            # If something got interrupted and the temp directory still exists, this will remove it
            if os.path.exists(dest_dir):
                rmtree(dest_dir)

            os.makedirs(dest_dir)

            psf_files = []
            total_cmd = init_cmd
            for pair_ind in range(ra_dec_coords.shape[0]):
                # The coordinates at which this PSF will be generated
                ra, dec = ra_dec_coords[pair_ind, :].value

                psf_file = psf.format(o=obs_id, i=inst, b=bins, ra=ra, dec=dec, m=psf_model)
                psf_files.append(os.path.join(OUTPUT, obs_id, psf_file))
                # Going with xsize and ysize as 400 pixels, I think its enough and quite a bit faster than 1000
                total_cmd += "psfgen image={i} coordtype=EQPOS level={m} energy=1000 xsize=400 ysize=400 x={ra} " \
                             "y={dec} output={p}; ".format(i=image.path, m=psf_model, ra=ra, dec=dec, p=psf_file)

            total_cmd += "mv * ../; cd ..; rm -r {d}".format(d=dest_dir)
            cmds.append(total_cmd)
            # This is the products final resting place, if it exists at the end of this command
            # In this case it just checks for the final PSF in the grid, all other files in the grid
            # get stored in extra info.
            final_paths.append(os.path.join(OUTPUT, obs_id, psf_file))
            extra_info.append({"obs_id": obs_id, "instrument": inst, "model": psf_model, "chunks_per_side": bins,
                               "files": psf_files, "x_bounds": x_bound_coords, "y_bounds": y_bound_coords})

        sources_cmds.append(np.array(cmds))
        sources_paths.append(np.array(final_paths))
        # This contains any other information that will be needed to instantiate the class
        # once the SAS cmd has run
        sources_extras.append(np.array(extra_info))
        sources_types.append(np.full(sources_cmds[-1].shape, fill_value="psf"))

    # I only return num_cores here so it has a reason to be passed to this function, really
    # it could just be picked up in the decorator.
    return sources_cmds, stack, execute, num_cores, sources_types, sources_paths, sources_extras


# TODO Add an option to generate core-excised spectra.
@sas_call
def evselect_spectrum(sources: List[BaseSource], reg_type: str, group_spec: bool = True, min_counts: int = 5,
                      min_sn: float = None, over_sample: float = None, one_rmf: bool = True,
                      num_cores: int = NUM_CORES):
    """
    A wrapper for all of the SAS processes necessary to generate an XMM spectrum that can be analysed
    in XSPEC. Every observation associated with this source, and every instrument associated with that
    observation, will have a spectrum generated using the specified region type as as boundary. It is possible
    to generate both grouped and ungrouped spectra using this function, with the degree of grouping set
    by the min_counts, min_sn, and oversample parameters.
    :param List[BaseSource] sources: A single source object, or a list of source objects.
    :param str reg_type: Tells the method what region source you want to use, for instance r500 or r200.
    :param bool group_spec: A boolean flag that sets whether generated spectra are grouped or not.
    :param float min_counts: If generating a grouped spectrum, this is the minimum number of counts per channel.
    To disable minimum counts set this parameter to None.
    :param float min_sn: If generating a grouped spectrum, this is the minimum signal to noise in each channel.
    To disable minimum signal to noise set this parameter to None.
    :param float over_sample: The minimum energy resolution for each group, set to None to disable.
    :param bool one_rmf: This flag tells the method whether it should only generate one RMF for a particular
    ObsID-instrument combination - this is much faster in some circumstances, however the RMF does depend
    slightly on position on the detector.
    :param int num_cores: The number of cores to use (if running locally), default is set to
    90% of available.
    """
    allowed_bounds = ["region", "r2500", "r500", "r200", "custom"]
    # This function supports passing both individual sources and sets of sources
    if isinstance(sources, BaseSource):
        sources = [sources]

    if not all([type(src) != BaseSource for src in sources]):
        raise TypeError("You cannot generate spectra from a BaseSource object, really you shouldn't be using "
                        "them at all, they are mostly useful as a superclass.")
    elif not all([src.detected for src in sources]):
        warnings.warn("Not all of these sources have been detected, the spectra generated may not be helpful.")
    elif reg_type not in allowed_bounds:
        raise ValueError("The only valid choices for reg_type are:\n {}".format(", ".join(allowed_bounds)))
    elif reg_type in ["r2500", "r500", "r200"] and not all([type(src) == GalaxyCluster for src in sources]):
        raise TypeError("You cannot use ExtendedSource classes with {}, "
                        "they have no overdensity radii.".format(reg_type))

    # Have to make sure that all observations have an up to date cif file.
    cifbuild(sources)

    # Define the various SAS commands that need to be populated, for a useful spectrum you also need ARF/RMF
    spec_cmd = "cd {d}; cp ../ccf.cif .; export SAS_CCF={ccf}; evselect table={e} withspectrumset=yes " \
               "spectrumset={s} energycolumn=PI spectralbinsize=5 withspecranges=yes specchannelmin=0 " \
               "specchannelmax={u} {ex}"

    rmf_cmd = "rmfgen rmfset={r} spectrumset='{s}' detmaptype=flat extendedsource={es}"

    # Don't need to run backscale separately, as this arfgen call will do it automatically
    arf_cmd = "arfgen spectrumset='{s}' arfset={a} withrmfset=yes rmfset='{r}' badpixlocation={e} " \
              "extendedsource={es} detmaptype=flat setbackscale=yes"

    # If the user wants to group spectra, then we'll need this template command:
    grp_cmd = "specgroup spectrumset={s} overwrite=yes backgndset={b} arfset={a} rmfset={r} addfilenames=no"

    stack = False  # This tells the sas_call routine that this command won't be part of a stack
    execute = True  # This should be executed immediately

    sources_cmds = []
    sources_paths = []
    sources_extras = []
    sources_types = []
    for source in sources:
        # rmfgen and arfgen both take arguments that describe if something is an extended source or not,
        #  so we check the source type
        if isinstance(source, (ExtendedSource, GalaxyCluster)):
            ex_src = "yes"
        else:
            ex_src = "no"
        cmds = []
        final_paths = []
        extra_info = []
        # Check which event lists are associated with each individual source
        for pack in source.get_products("events", just_obj=False):
            obs_id = pack[0]
            inst = pack[1]

            if not os.path.exists(OUTPUT + obs_id):
                os.mkdir(OUTPUT + obs_id)

            # Got to check if this spectrum already exists
            exists = [match for match in source.get_products("spectrum", obs_id, inst, just_obj=False)
                      if reg_type in match]
            if len(exists) == 1 and exists[0][-1].usable:
                continue

            # If there is no match to a region, the source region returned by this method will be None,
            #  and if the user wants to generate spectra from region files, we have to ignore that observations
            if reg_type == "region" and source.get_source_region("region", obs_id)[0] is None:
                continue

            # This method returns a SAS expression for the source and background regions - excluding interlopers
            reg, b_reg = source.get_sas_region(reg_type, obs_id, inst, xmm_sky)

            # Some settings depend on the instrument, XCS uses different patterns for different instruments
            if "pn" in inst:
                # Also the upper channel limit is different for EPN and EMOS detectors
                spec_lim = 20479
                expr = "expression='#XMMEA_EP && (PATTERN <= 4) && (FLAG .eq. 0) && {s}'".format(s=reg)
                b_expr = "expression='#XMMEA_EP && (PATTERN <= 4) && (FLAG .eq. 0) && {s}'".format(s=b_reg)
            elif "mos" in inst:
                spec_lim = 11999
                expr = "expression='#XMMEA_EM && (PATTERN <= 12) && (FLAG .eq. 0) && {s}'".format(s=reg)
                b_expr = "expression='#XMMEA_EM && (PATTERN <= 12) && (FLAG .eq. 0) && {s}'".format(s=b_reg)
            else:
                raise ValueError("You somehow have an illegal value for the instrument name...")

            # Some of the SAS tasks have issues with filenames with a '+' in them for some reason, so this
            #  replaces any + symbols that may be in the source name with another character
            source_name = source.name.replace("+", "x")

            # Just grabs the event list object
            evt_list = pack[-1]
            # Sets up the file names of the output files
            dest_dir = OUTPUT + "{o}/{i}_{n}_temp/".format(o=obs_id, i=inst, n=source_name)
            spec = "{o}_{i}_{n}_{bt}_spec.fits".format(o=obs_id, i=inst, n=source_name, bt=reg_type)
            b_spec = "{o}_{i}_{n}_{bt}_backspec.fits".format(o=obs_id, i=inst, n=source_name, bt=reg_type)
            arf = "{o}_{i}_{n}_{bt}.arf".format(o=obs_id, i=inst, n=source_name, bt=reg_type)
            b_arf = "{o}_{i}_{n}_{bt}_back.arf".format(o=obs_id, i=inst, n=source_name, bt=reg_type)
            ccf = dest_dir + "ccf.cif"

            # Fills out the evselect command to make the main and background spectra
            s_cmd_str = spec_cmd.format(d=dest_dir, ccf=ccf, e=evt_list.path, s=spec, u=spec_lim, ex=expr)
            sb_cmd_str = spec_cmd.format(d=dest_dir, ccf=ccf, e=evt_list.path, s=b_spec, u=spec_lim, ex=b_expr)

            # This chunk adds rmfgen commands depending on whether we're using a universal RMF or
            #  an individual one for each spectrum. Also adds arfgen commands on the end, as they depend on
            #  the rmf.
            if one_rmf:
                rmf = "{o}_{i}_{n}_{bt}.rmf".format(o=obs_id, i=inst, n=source_name, bt="universal")
                b_rmf = rmf
            else:
                rmf = "{o}_{i}_{n}_{bt}.rmf".format(o=obs_id, i=inst, n=source_name, bt=reg_type)
                b_rmf = "{o}_{i}_{n}_{bt}_back.rmf".format(o=obs_id, i=inst, n=source_name, bt=reg_type)

            if one_rmf and not os.path.exists(dest_dir + rmf):
                cmd_str = ";".join([s_cmd_str, rmf_cmd.format(r=rmf, s=spec, es=ex_src),
                                    arf_cmd.format(s=spec, a=arf, r=rmf, e=evt_list.path, es=ex_src), sb_cmd_str,
                                    arf_cmd.format(s=b_spec, a=b_arf, r=b_rmf, e=evt_list.path, es=ex_src)])
            elif not one_rmf and not os.path.exists(dest_dir + rmf):
                cmd_str = ";".join([s_cmd_str, rmf_cmd.format(r=rmf, s=spec, es=ex_src),
                                    arf_cmd.format(s=spec, a=arf, r=rmf, e=evt_list.path, es=ex_src)]) + ";"
                cmd_str += ";".join([sb_cmd_str, rmf_cmd.format(r=b_rmf, s=b_spec, es=ex_src),
                                    arf_cmd.format(s=b_spec, a=b_arf, r=b_rmf, e=evt_list.path, es=ex_src)])
            else:
                cmd_str = ";".join([s_cmd_str, arf_cmd.format(s=spec, a=arf, r=rmf, e=evt_list.path,
                                                              es=ex_src)]) + ";"
                cmd_str += ";".join([sb_cmd_str, arf_cmd.format(s=b_spec, a=b_arf, r=b_rmf, e=evt_list.path,
                                                                es=ex_src)])

            # If the user wants to produce grouped spectra, then this if statement is triggered and adds a specgroup
            #  command at the end. The groupspec command will replace the ungrouped spectrum.
            if group_spec:
                new_grp = grp_cmd.format(s=spec, b=b_spec, r=rmf, a=arf)
                if min_counts is not None:
                    new_grp += " mincounts={mc}".format(mc=min_counts)
                if min_sn is not None:
                    new_grp += " minSN={msn}".format(msn=min_sn)
                if over_sample is not None:
                    new_grp += " oversample={os}".format(os=over_sample)
                cmd_str += "; " + new_grp

            # Adds clean up commands to move all generated files and remove temporary directory
            cmd_str += "; mv * ../; cd ..; rm -r {d}".format(d=dest_dir)
            cmds.append(cmd_str)  # Adds the full command to the set
            # If something got interrupted and the temp directory still exists, this will remove it
            if os.path.exists(dest_dir):
                rmtree(dest_dir)
            # Makes sure the whole path to the temporary directory is created
            os.makedirs(dest_dir)

            final_paths.append(os.path.join(OUTPUT, obs_id, spec))
            extra_info.append({"reg_type": reg_type, "rmf_path": os.path.join(OUTPUT, obs_id, rmf),
                               "arf_path": os.path.join(OUTPUT, obs_id, arf),
                               "b_spec_path": os.path.join(OUTPUT, obs_id, b_spec),
                               "b_rmf_path": os.path.join(OUTPUT, obs_id, b_rmf),
                               "b_arf_path": os.path.join(OUTPUT, obs_id, b_arf),
                               "obs_id": obs_id, "instrument": inst})

        sources_cmds.append(np.array(cmds))
        sources_paths.append(np.array(final_paths))
        # This contains any other information that will be needed to instantiate the class
        #  once the SAS cmd has run
        sources_extras.append(np.array(extra_info))
        sources_types.append(np.full(sources_cmds[-1].shape, fill_value="spectrum"))

    return sources_cmds, stack, execute, num_cores, sources_types, sources_paths, sources_extras


def evselect_annular_spectrum():
    raise NotImplementedError("Haven't quite got around to doing this bit yet")
