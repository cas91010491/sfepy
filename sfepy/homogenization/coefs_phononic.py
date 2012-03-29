import time

import numpy as nm
import numpy.linalg as nla
import scipy as sc

from sfepy.base.base import output, get_default, dict_to_struct, assert_, Struct
from sfepy.base.plotutils import plt
from sfepy.solvers import eig
from sfepy.base.progressbar import MyBar
from sfepy.linalg import norm_l2_along_axis
from sfepy.fem.evaluate import eval_equations
from sfepy.homogenization.coefs_base import MiniAppBase, CorrMiniApp
from sfepy.homogenization.utils import coor_to_sym

def compute_eigenmomenta(em_equation, var_name, problem, eig_vectors,
                         transform=None, progress_bar=None):
    """
    Compute the eigenmomenta corresponding to given eigenvectors.
    """
    n_dof, n_eigs = eig_vectors.shape

    equations, variables = problem.create_evaluable(em_equation)
    var = variables[var_name]

    n_c = var.n_components
    eigenmomenta = nm.empty((n_eigs, n_c), dtype=nm.float64)

    if progress_bar is not None:
        progress_bar.init(n_eigs - 1)

    for ii in xrange(n_eigs):
        if progress_bar is not None:
            progress_bar.update(ii)

        else:
            if (ii % 100) == 0:
                output('%d of %d (%f%%)' % (ii, n_eigs,
                                            100. * ii / (n_eigs - 1)))

        if transform is None:
            vec_phi, is_zero = eig_vectors[:,ii], False

        else:
            vec_phi, is_zero = transform(eig_vectors[:,ii], (n_dof / n_c, n_c))

        if is_zero:
            eigenmomenta[ii, :] = 0.0

        else:
            var.data_from_any(vec_phi.copy())

            val = eval_equations(equations, variables)

            eigenmomenta[ii, :] = val

    return eigenmomenta

def cut_freq_range(freq_range, eigs, valid, freq_margins, eig_range,
                   fixed_eig_range, feps):
    """
    Cut off masked resonance frequencies. Margins are preserved, like no
    resonances were cut.

    Returns
    -------
    freq_range : array
        The new range of frequencies.
    freq_range_margins : array
        The range of frequencies with prepended/appended margins equal to
        `fixed_eig_range` if it is not None.
    """
    n_eigs = eigs.shape[0]

    output('masked resonance frequencies in range:')
    valid_slice = slice(*eig_range)
    output(nm.where(valid[valid_slice] == False)[0])

    if fixed_eig_range is None:
        min_freq, max_freq = freq_range[0], freq_range[-1]
        margins = freq_margins * (max_freq - min_freq)
        prev_eig = min_freq - margins[0]
        next_eig = max_freq + margins[1]

        if eig_range[0] > 0:
            prev_eig = max(nm.sqrt(eigs[eig_range[0] - 1]) + feps, prev_eig)

        if eig_range[1] < n_eigs:
            next_eig = min(nm.sqrt(eigs[eig_range[1]]) - feps, next_eig)

        prev_eig = max(feps, prev_eig)
        next_eig = max(feps, next_eig, prev_eig + feps)

    else:
        prev_eig, next_eig = fixed_eig_range

    freq_range = freq_range[valid[valid_slice]]
    freq_range_margins = nm.r_[prev_eig, freq_range, next_eig]

    return freq_range, freq_range_margins

def split_chunks(indx):
    """Split index vector to chunks of consecutive numbers."""
    if not len(indx): return []

    delta = nm.ediff1d(indx, to_end=2)
    ir = nm.where(delta > 1)[0]

    chunks = []
    ic0 = 0
    for ic in ir:
        chunk = indx[ic0:ic+1]
        ic0 = ic + 1
        chunks.append(chunk)
    return chunks

def detect_band_gaps(mass, freq_info, opts, gap_kind='normal', mtx_b=None):
    """
    Detect band gaps given solution to eigenproblem (eigs,
    eig_vectors). Only valid resonance frequencies (e.i. those for which
    corresponding eigenmomenta are above a given threshold) are taken into
    account.

    Notes
    -----
    - make feps relative to ]f0, f1[ size?
    """
    output('eigensolver:', opts.eigensolver)

    fm = freq_info.freq_range_margins
    min_freq, max_freq = fm[0], fm[-1]
    output('freq. range with margins: [%8.3f, %8.3f]'
           % (min_freq, max_freq))

    df = opts.freq_step * (max_freq - min_freq)

    fz_callback = get_callback(mass.evaluate, opts.eigensolver,
                               mtx_b=mtx_b, mode='find_zero')
    trace_callback = get_callback(mass.evaluate, opts.eigensolver,
                                  mtx_b=mtx_b, mode='trace')

    n_col = 1 + (mtx_b is not None)
    logs = [[] for ii in range(n_col + 1)]
    gaps = []

    for ii in xrange(freq_info.freq_range.shape[0] + 1):

        f0, f1 = fm[[ii, ii+1]]
        output('interval: ]%.8f, %.8f[...' % (f0, f1))

        f_delta = f1 - f0
        f_mid = 0.5 * (f0 + f1)
        if (f1 - f0) > (2.0 * opts.feps):
            num = min(1000, max(100, (f1 - f0) / df))
            a = nm.linspace(0., 1., num)
            log_freqs = f0 + opts.feps \
                        + 0.5 * (nm.sin((a - 0.5) * nm.pi) + 1.0) \
                        * (f1 - f0 - 2.0 * opts.feps)
            ## log_freqs = nm.linspace(f0 + opts.feps, f1 - opts.feps, num)
        else:
            log_freqs = nm.array([f_mid - 1e-8 * f_delta,
                                   f_mid + 1e-8 * f_delta])

        output('n_logged: %d' % log_freqs.shape[0])

        log_mevp = [[] for ii in range(n_col)]
        for f in log_freqs:
            for ii, data in enumerate(trace_callback(f)):
                log_mevp[ii].append(data)

        # Get log for the first and last f in log_freqs.
        lf0 = log_freqs[0]
        lf1 = log_freqs[-1]

        log0, log1 = log_mevp[0][0], log_mevp[0][-1]
        min_eig0 = log0[0]
        max_eig1 = log1[-1]
        if gap_kind == 'liquid':
            mevp = nm.array(log_mevp, dtype=nm.float64).squeeze()
            si = nm.where(mevp[:,0] < 0.0)[0]
            li = nm.where(mevp[:,-1] < 0.0)[0]
            wi = nm.setdiff1d(si, li)

            if si.shape[0] == 0: # No gaps.
                gap = ([2, lf0, log0[0]], [2, lf0, log0[-1]])
                gaps.append(gap)

            elif li.shape[0] == mevp.shape[0]: # Full interval strong gap.
                gap = ([1, lf1, log1[0]], [1, lf1, log1[-1]])
                gaps.append(gap)

            else:
                subgaps = []
                for chunk in split_chunks(li): # Strong gaps.
                    i0, i1 = chunk[0], chunk[-1]
                    fmin, fmax = log_freqs[i0], log_freqs[i1]
                    gap = ([1, fmin, mevp[i0,-1]], [1, fmax, mevp[i1,-1]])
                    subgaps.append(gap)

                for chunk in split_chunks(wi): # Weak gaps.
                    i0, i1 = chunk[0], chunk[-1]
                    fmin, fmax = log_freqs[i0], log_freqs[i1]
                    gap = ([0, fmin, mevp[i0,-1]], [2, fmax, mevp[i1,-1]])
                    subgaps.append(gap)
                gaps.append(subgaps)

        else:
            if min_eig0 > 0.0: # No gaps.
                gap = ([2, lf0, log0[0]], [2, lf0, log0[-1]])

            elif max_eig1 < 0.0: # Full interval strong gap.
                gap = ([1, lf1, log1[0]], [1, lf1, log1[-1]])

            else:
                llog_freqs = list(log_freqs)

                # Insert fmin, fmax into log.
                output('finding zero of the largest eig...')
                smax, fmax, vmax = find_zero(lf0, lf1, fz_callback,
                                              opts.feps, opts.zeps, 1)
                im = nm.searchsorted(log_freqs, fmax)
                llog_freqs.insert(im, fmax)
                for ii, data in enumerate(trace_callback(fmax)):
                    log_mevp[ii].insert(im, data)

                output('...done')
                if smax in [0, 2]:
                    output('finding zero of the smallest eig...')
                    # having fmax instead of f0 does not work if feps is large.
                    smin, fmin, vmin = find_zero(lf0, lf1, fz_callback,
                                                  opts.feps, opts.zeps, 0)
                    im = nm.searchsorted(log_freqs, fmin)
                    # +1 due to fmax already inserted before.
                    llog_freqs.insert(im+1, fmin)
                    for ii, data in enumerate(trace_callback(fmin)):
                        log_mevp[ii].insert(im+1, data)

                    output('...done')

                elif smax == 1:
                    smin = 1 # both are negative everywhere.
                    fmin, vmin = fmax, vmax

                gap = ([smin, fmin, vmin], [smax, fmax, vmax])

                log_freqs = nm.array(llog_freqs)

            output(gap[0])
            output(gap[1])

            gaps.append(gap)

        logs[0].append(log_freqs)
        for ii, data in enumerate(log_mevp):
            logs[ii+1].append(nm.array(data, dtype = nm.float64))

        output('...done')

    kinds = describe_gaps(gaps)

    slogs = Struct(freqs=logs[0], eigs=logs[1])
    if n_col == 2:
        slogs.eig_vectors = logs[2]

    return slogs, gaps, kinds

def get_callback(mass, method, mtx_b=None, mode='trace'):
    """
    Return callback to solve band gaps or dispersion eigenproblem P.

    Notes
    -----
    Find zero callbacks return:
      eigenvalues

    Trace callbacks return:
      (eigenvalues,)
    or
      (eigenvalues, eigenvectors) (in full (dispoersion) mode)

    If `mtx_b` is None, the problem P is
      M w = \lambda w,
    otherwise it is
      omega^2 M w = \eta B w"""

    def find_zero_callback(f):
        meigs = eig(mass(f), eigenvectors=False, method=method)
        return meigs

    def find_zero_full_callback(f):
        meigs = eig((f**2) * mass(f), mtx_b=mtx_b,
                    eigenvectors=False, method=method)
        return meigs

    def trace_callback(f):
        meigs = eig(mass(f), eigenvectors=False, method=method)
        return meigs,

    def trace_full_callback(f):
        meigs, mvecs = eig((f**2) * mass(f), mtx_b=mtx_b,
                           eigenvectors=True, method=method)

        return meigs, mvecs

    if mtx_b is not None:
        mode += '_full'

    return eval(mode + '_callback')

def find_zero(f0, f1, callback, feps, zeps, mode):
    """
    For f \in ]f0, f1[ find frequency f for which either the smallest (`mode` =
    0) or the largest (`mode` = 1) eigenvalue of problem P given by `callback`
    is zero.

    Returns
    -------
    flag : 0, 1, or 2
        The flag, see Notes below.
    frequency : float
        The found frequency.
    eigenvalue : float
        The eigenvalue corresponding to the found frequency.

    Notes
    -----
    Meaning of the return value combinations:

    =====  ======  ========
    mode    flag    meaning
    =====  ======  ========
    0, 1    0       eigenvalue -> 0 for f \in ]f0, f1[
    0       1       f -> f1, smallest eigenvalue < 0
    0       2       f -> f0, smallest eigenvalue > 0 and -> -\infty
    1       1       f -> f1, largest eigenvalue < 0 and  -> +\infty
    1       2       f -> f0, largest eigenvalue > 0
    =====  ======  ========
    """
    fm, fp = f0, f1
    ieig = {0 : 0, 1 : -1}[mode]
    while 1:
        f = 0.5 * (fm + fp)
        meigs = callback(f)

        val = meigs[ieig]
        ## print f, f0, f1, fm, fp, val
        ## print '%.16e' % f, '%.16e' % fm, '%.16e' % fp, '%.16e' % val

        if (abs(val) < zeps) or ((fp - fm) < (abs(fm) * nm.finfo(float).eps)):
            return 0, f, val

        if mode == 0:
            if (f - f0) < feps:
                return 2, f0, val

            elif (f1 - f) < feps:
                return 1, f1, val

        elif mode == 1:
            if (f1 - f) < feps:
                return 1, f1, val

            elif (f - f0) < feps:
                return 2, f0, val

        if val > 0.0:
            fp = f

        else:
            fm = f

def describe_gaps(gaps):
    kinds = []
    for ii, gap in enumerate(gaps):
        if isinstance(gap, list):
            subkinds = []
            for gmin, gmax in gap:
                if (gmin[0] == 2) and (gmax[0] == 2):
                    kind = ('p', 'propagation zone')
                elif (gmin[0] == 1) and (gmax[0] == 1):
                    kind = ('is', 'inner strong band gap')
                elif (gmin[0] == 0) and (gmax[0] == 2):
                    kind = ('iw', 'inner weak band gap')
                subkinds.append(kind)
            kinds.append(subkinds)

        else:
            gmin, gmax = gap

            if (gmin[0] == 2) and (gmax[0] == 2):
                kind = ('p', 'propagation zone')
            elif (gmin[0] == 1) and (gmax[0] == 2):
                kind = ('w', 'full weak band gap')
            elif (gmin[0] == 0) and (gmax[0] == 2):
                kind = ('wp', 'weak band gap + propagation zone')
            elif (gmin[0] == 1) and (gmax[0] == 1):
                kind = ('s', 'full strong band gap (due to end of freq.'
                        ' range or too large thresholds)')
            elif (gmin[0] == 1) and (gmax[0] == 0):
                kind = ('sw', 'strong band gap + weak band gap')
            elif (gmin[0] == 0) and (gmax[0] == 0):
                kind = ('swp', 'strong band gap + weak band gap +'
                        ' propagation zone')
            else:
                msg = 'impossible band gap combination: %d, %d' % (gmin, gmax)
                raise ValueError(msg)
            kinds.append(kind)

    return kinds

class SimpleEVP(CorrMiniApp):
    """
    Simple eigenvalue problem.
    """

    def process_options(self):
        get = self.options.get

        return Struct(eigensolver=get('eigensolver', 'eig.sgscipy'),
                      elasticity_contrast=get('elasticity_contrast', 1.0),
                      scale_epsilon=get('scale_epsilon', 1.0),
                      save_eig_vectors=get('save_eig_vectors', (0, 0)))

    def __call__(self, problem=None, data=None):
        problem = get_default(problem, self.problem)
        opts = self.app_options

        problem.set_equations(self.equations)

        problem.select_bcs(ebc_names=self.ebcs, epbc_names=self.epbcs,
                           lcbc_names=self.get_default_attr('lcbcs', []))

        problem.update_materials(problem.ts)

        self.init_solvers(problem)

        #variables = problem.get_variables()

        mtx_a = problem.evaluate(self.equations['lhs'], mode='weak',
                                 auto_init=True, dw_mode='matrix')

        mtx_m = problem.evaluate(self.equations['rhs'], mode='weak',
                                 dw_mode='matrix')

        output('computing resonance frequencies...')
        tt = [0]

        if isinstance(mtx_a, sc.sparse.spmatrix):
            mtx_a = mtx_a.toarray()
        if isinstance(mtx_m, sc.sparse.spmatrix):
            mtx_m = mtx_m.toarray()

        eigs, mtx_s_phi = eig(mtx_a, mtx_m, return_time=tt,
                              method=opts.eigensolver)
        eigs[eigs<0.0] = 0.0
        output('...done in %.2f s' % tt[0])
        output('original eigenfrequencies:')
        output(eigs)
        opts = self.app_options
        epsilon2 = opts.scale_epsilon * opts.scale_epsilon
        eigs_rescaled = (opts.elasticity_contrast / epsilon2)  * eigs
        output('rescaled eigenfrequencies:')
        output(eigs_rescaled)
        output('number of eigenfrequencies: %d' % eigs.shape[0])

        try:
            assert_(nm.isfinite(eigs).all())
        except ValueError:
            from sfepy.base.base import debug; debug()

        n_eigs = eigs.shape[0]

        variables = problem.get_variables()

        mtx_phi = nm.empty((variables.di.ptr[-1], mtx_s_phi.shape[1]),
                           dtype=nm.float64)

        make_full = variables.make_full_vec
        for ii in xrange(n_eigs):
            mtx_phi[:,ii] = make_full(mtx_s_phi[:,ii])

        self.save(eigs, mtx_phi, problem)

        evp = Struct(name='evp', eigs=eigs, eigs_rescaled=eigs_rescaled,
                     eig_vectors=mtx_phi)

        return evp

    def save(self, eigs, mtx_phi, problem):
        save = self.app_options.save_eig_vectors

        n_eigs = eigs.shape[0]

        out = {}
        state = problem.create_state()
        for ii in xrange(n_eigs):
            if (ii >= save[0]) and (ii < (n_eigs - save[1])): continue
            state.set_full(mtx_phi[:,ii], force=True)
            aux = state.create_output_dict()
            for name, val in aux.iteritems():
                out[name+'%03d' % ii] = val

        if self.post_process_hook is not None:
            out = self.post_process_hook(out, problem, mtx_phi)

        problem.domain.mesh.write(self.save_name + '.vtk', io='auto', out=out)

        fd = open(self.save_name + '_eigs.txt', 'w')
        eigs.tofile(fd, ' ')
        fd.close()

class DensityVolumeInfo(MiniAppBase):
    """
    Determine densities of regions specified in `region_to_material`, and
    compute average density based on region volumes.
    """

    def __call__(self, volume=None, problem=None, data=None):
        problem = get_default(problem, self.problem)

        vf = data[self.requires[0]]

        average_density = 0.0
        total_volume = 0.0
        volumes = {}
        densities = {}
        for region_name, aux in self.region_to_material.iteritems():
            vol = vf['volume_' + region_name]

            mat_name, item_name = aux
            conf = problem.conf.get_item_by_name('materials', mat_name)
            density = conf.values[item_name]

            output('region %s: volume %f, density %f' % (region_name,
                                                         vol, density))

            volumes[region_name] = vol
            densities[region_name] = density

            average_density += vol * density
            total_volume += vol

        true_volume = self._get_volume(volume)
        assert_(abs(total_volume - true_volume) / true_volume < 1e-14)

        output('total volume:', true_volume)

        average_density /= true_volume

        return Struct(name='density_volume_info',
                      average_density=average_density,
                      total_volume=total_volume,
                      volumes=volumes,
                      densities=densities)

class Eigenmomenta(MiniAppBase):
    """
    Eigenmomenta corresponding to eigenvectors.

    Parameters
    ----------
    var_name : str
        The name of the variable used in the integral.
    threshold : float
        The threshold under which an eigenmomentum is considered zero.
    threshold_is_relative : bool
        If True, the `threshold` is relative w.r.t. max. norm of eigenmomenta.
    transform : callable, optional
        Optional function for transforming the eigenvectors before computing
        the eigenmomenta.
    progress_bar : bool
        If True, use a progress bar to show computation progress.

    Returns
    -------
    eigenmomenta : Struct
        The resulting eigenmomenta. An eigenmomentum above threshold is marked
        by the attribute 'valid' set to True.
    """

    def process_options(self):
        options = dict_to_struct(self.options)
        get = options.get_default_attr

        return Struct(var_name=get('var_name', None,
                                   'missing "var_name" in options!'),
                      threshold=get('threshold', 1e-4),
                      threshold_is_relative=get('threshold_is_relative', True),
                      transform=get('transform', None),
                      progress_bar=get('progress_bar', True))

    def __call__(self, volume=None, problem=None, data=None):
        problem = get_default(problem, self.problem)
        opts = self.app_options

        evp, dv_info = [data[ii] for ii in self.requires]

        output('computing eigenmomenta...')
        if opts.progress_bar:
            progress_bar = MyBar('progress:')

        else:
            progress_bar = None

        if opts.transform is not None:
            fun = getattr(problem.conf.funmod, opts.transform[0])
            def wrap_transform(vec, shape):
                return fun(vec, shape, *opts.eig_vector_transform[1:])

        else:
            wrap_transform = None

        tt = time.clock()
        eigenmomenta = compute_eigenmomenta(self.expression, opts.var_name,
                                            problem, evp.eig_vectors,
                                            wrap_transform, progress_bar)
        output('...done in %.2f s' % (time.clock() - tt))

        n_eigs = evp.eigs.shape[0]

        mag = norm_l2_along_axis(eigenmomenta)

        if opts.threshold_is_relative:
            tol = opts.threshold * mag.max()
        else:
            tol = opts.threshold

        valid = nm.where(mag < tol, False, True)
        mask = nm.where(valid == False)[0]
        eigenmomenta[mask, :] = 0.0
        n_zeroed = mask.shape[0]

        output('%d of %d eigenmomenta zeroed (under %.2e)'\
                % (n_zeroed, n_eigs, tol))

        out = Struct(name='eigenmomenta', n_zeroed=n_zeroed,
                     eigenmomenta=eigenmomenta, valid=valid)
        return out

class AcousticMassTensor(MiniAppBase):
    """
    The acoustic mass tensor for a given frequency.

    Returns
    -------
    self : AcousticMassTensor instance
        This class instance whose `evaluate()` method computes for a given
        frequency the required tensor.

    Notes
    -----
    `eigenmomenta`, `eigs` should contain only valid resonances.
    """

    def __call__(self, volume=None, problem=None, data=None):
        evp, self.dv_info, ema = [data[ii] for ii in self.requires]

        self.eigs = evp.eigs[ema.valid]
        self.eigenmomenta = ema.eigenmomenta[ema.valid, :]

        return self

    def evaluate(self, freq):
        ema = self.eigenmomenta

        n_c = ema.shape[1]
        fmass = nm.zeros((n_c, n_c), dtype=nm.float64)

        num, denom = self.get_coefs(freq)
        if not nm.isfinite(denom).all():
            raise ValueError('frequency %e too close to resonance!' % freq)

        for ir in range(n_c):
            for ic in range(n_c):
                if ir <= ic:
                    val = nm.sum((num / denom) * (ema[:, ir] * ema[:, ic]))
                    fmass[ir, ic] += val
                else:
                    fmass[ir, ic] = fmass[ic, ir]

        eye = nm.eye(n_c, n_c, dtype=nm.float64)
        mtx_mass = (eye * self.dv_info.average_density) \
                   - (fmass / self.dv_info.total_volume)

        return mtx_mass

    def get_coefs(self, freq):
        """
        Get frequency-dependent coefficients.
        """
        f2 = freq*freq
        de = f2 - self.eigs
        return f2, de

class AcousticMassLiquidTensor(AcousticMassTensor):

    def get_coefs(self, freq):
        """
        Get frequency-dependent coefficients.
        """
        eigs = self.eigs

        f2 = freq*freq
        aux = (f2 - self.gamma * eigs)
        num = f2 * aux
        denom = aux*aux + f2*(self.eta*self.eta)*nm.power(eigs, 2.0)
        return num, denom

class AppliedLoadTensor(MiniAppBase):
    """
    The applied load tensor for a given frequency.

    Returns
    -------
    self : AppliedLoadTensor instance
        This class instance whose `evaluate()` method computes for a given
        frequency the required tensor.

    Notes
    -----
    `eigenmomenta`, `ueigenmomenta`, `eigs` should contain only valid
    resonances.
    """

    def __call__(self, volume=None, problem=None, data=None):
        evp, self.dv_info, ema, uema = [data[ii] for ii in self.requires]

        self.eigs = evp.eigs[ema.valid]
        self.eigenmomenta = ema.eigenmomenta[ema.valid, :]
        self.ueigenmomenta = uema.eigenmomenta[uema.valid, :]

        return self

    def evaluate(self, freq):
        ema, uema = self.eigenmomenta, self.ueigenmomenta

        n_c = ema.shape[1]
        fload = nm.zeros((n_c, n_c), dtype=nm.float64)

        de = (freq**2) - (self.eigs)
        if not nm.isfinite(de).all():
            raise ValueError('frequency %e too close to resonance!' % freq)

        for ir in range(n_c):
            for ic in range(n_c):
                val = nm.sum(ema[:, ir] * uema[:, ic] / de)
                fload[ir, ic] += (freq**2) * val

        eye = nm.eye((n_c, n_c), dtype=nm.float64)

        mtx_load = eye - (fload / self.dv_info.total_volume)

        return mtx_load

class BandGaps(MiniAppBase):

    def process_options(self):
        get = self.options.get

        freq_margins = get('freq_margins', (5, 5))
        # Given per cent.
        freq_margins = 0.01 * nm.array(freq_margins, dtype=nm.float64)

        # Given in per cent.
        freq_step = 0.01 * get('freq_step', 5)

        return Struct(eigensolver=get('eigensolver', 'eig.sgscipy'),
                      eig_range=get('eig_range', None),
                      freq_margins=freq_margins,
                      fixed_eig_range=get('fixed_eig_range', None),
                      freq_step=freq_step,

                      feps=get('feps', 1e-8),
                      zeps=get('zeps', 1e-8))

    def __call__(self, volume=None, problem=None, data=None):
        problem = get_default(problem, self.problem)
        opts = self.app_options

        evp, ema, mass = [data[ii] for ii in self.requires]

        eigs = evp.eigs

        self.fix_eig_range(eigs.shape[0])

        if opts.fixed_eig_range is not None:
            mine, maxe = opts.fixed_eig_range
            ii = nm.where((eigs > (mine**2.)) & (eigs < (maxe**2.)))[0]
            freq_range_initial = nm.sqrt(eigs[ii])
            opts.eig_range = (ii[0], ii[-1] + 1) # +1 as it is a slice.

        else:
            freq_range_initial = nm.sqrt(eigs[slice(*opts.eig_range)])

        output('initial freq. range     : [%8.3f, %8.3f]'
               % tuple(freq_range_initial[[0, -1]]))

        aux = cut_freq_range(freq_range_initial, eigs, ema.valid,
                             opts.freq_margins, opts.eig_range,
                             opts.fixed_eig_range,
                             opts.feps)
        freq_range, freq_range_margins = aux
        if len(freq_range):
            output('freq. range             : [%8.3f, %8.3f]'
                   % tuple(freq_range[[0, -1]]))

        else:
            # All masked.
            output('freq. range             : all masked!')

        freq_info = Struct(name='freq_info',
                           freq_range_initial=freq_range_initial,
                           freq_range=freq_range,
                           freq_range_margins=freq_range_margins)

        logs, gaps, kinds = detect_band_gaps(mass, freq_info, opts)

        bg = Struct(logs=logs, gaps=gaps, kinds=kinds,
                    valid=ema.valid, eig_range=slice(*opts.eig_range),
                    n_eigs=eigs.shape[0], n_zeroed=ema.n_zeroed,
                    freq_range_initial=freq_info.freq_range_initial,
                    freq_range=freq_info.freq_range,
                    freq_range_margins=freq_info.freq_range_margins,
                    opts=opts)

        return bg

    def fix_eig_range(self, n_eigs):
        eig_range = get_default(self.app_options.eig_range, (0, n_eigs))
        if eig_range[-1] < 0:
            eig_range[-1] += n_eigs + 1

        assert_(eig_range[0] < (eig_range[1] - 1))
        assert_(eig_range[1] <= n_eigs)
        self.app_options.eig_range = eig_range

def compute_cat( coefs, iw_dir, mode = 'simple' ):
    r"""Compute Christoffel acoustic tensor (cat) given the incident wave
    direction (unit vector).

    - if mode == 'simple', coefs.elastic is the elasticity tensor C and
    cat := \Gamma_{ik} = C_{ijkl} n_j n_l

    - if mode == 'piezo', coefs.elastic, .coupling, .dielectric are the
    elasticity, piezo-coupling and dielectric tensors C, G, D and
    cat := H_{ik} = \Gamma_{ik} + \frac{1}{\xi} \gamma_i \gamma_j, where
    \gamma_i = G_{kij} n_j n_k,
    \xi = D_{kl} n_k n_l
    """
    dim = iw_dir.shape[0]

    cat = nm.zeros( (dim, dim), dtype = nm.float64 )

    mtx_c = coefs.elastic
    for ii in range( dim ):
        for ij in range( dim ):
            ir = coor_to_sym( ii, ij, dim )
            for ik in range( dim ):
                for il in range( dim ):
                    ic = coor_to_sym( ik, il, dim )
                    cat[ii,ik] += mtx_c[ir,ic] * iw_dir[ij] * iw_dir[il]
#    print cat
    
    if mode =='piezo':
        xi = nm.dot( nm.dot( coefs.dielectric, iw_dir ), iw_dir )
#        print xi
        gamma = nm.zeros( (dim,), dtype = nm.float64 )
        mtx_g = coefs.coupling
        for ii in range( dim ):
            for ij in range( dim ):
                ir = coor_to_sym( ii, ij, dim )
                for ik in range( dim ):
                    gamma[ii] += mtx_g[ik,ir] * iw_dir[ij] * iw_dir[ik]
#        print gamma
        cat += nm.outer( gamma, gamma ) / xi
        
    return cat

def compute_polarization_angles( iw_dir, wave_vectors ):
    """Computes angle between incident wave direction `iw_dir` and wave
    vectors. Vector length does not matter (can use eigenvectors directly)."""
    pas = []

    iw_dir = iw_dir / nla.norm( iw_dir )
    idims = range( iw_dir.shape[0] )
    pi2 = 0.5 * nm.pi
    for vecs in wave_vectors:
        pa = nm.empty( vecs.shape[:-1], dtype = nm.float64 )
        for ir, vec in enumerate( vecs ):
            for ic in idims:
                vv = vec[:,ic]
                # Ensure the angle is in [0, pi/2].
                val = nm.arccos( nm.dot( iw_dir, vv ) / nla.norm( vv ) )
                if val > pi2:
                    val = nm.pi - val
                pa[ir,ic] = val

        pas.append( pa )

    return pas

def transform_plot_data( datas, plot_transform, funmod ):
    if plot_transform is not None:
        fun = getattr( funmod, plot_transform[0] )

    dmin, dmax = 1e+10, -1e+10
    tdatas = []
    for data in datas:
        tdata = data.copy()
        if plot_transform is not None:
            tdata = fun( tdata, *plot_transform[1:] )
        dmin = min( dmin, tdata.min() )
        dmax = max( dmax, tdata.max() )
        tdatas.append( tdata )
    dmin, dmax = min( dmax - 1e-8, dmin ), max( dmin + 1e-8, dmax )
    return (dmin, dmax), tdatas

def plot_eigs( fig_num, plot_rsc, plot_labels, valid, freq_range, plot_range,
               show = False, clear = False, new_axes = False ):
    """
    Plot resonance/eigen-frequencies.

    `valid` must correspond to `freq_range`

    resonances : red
    masked resonances: dotted red
    """
    if plt is None: return
    assert_( len( valid ) == len( freq_range ) )

    fig = plt.figure( fig_num )
    if clear:
        fig.clf()
    if new_axes:
        ax = fig.add_subplot( 111 )
    else:
        ax = fig.gca()

    l0 = l1 = None
    for ii, f in enumerate( freq_range ):
        if valid[ii]:
            l0 = ax.plot( [f, f], plot_range, **plot_rsc['resonance'] )[0]
        else:
            l1 = ax.plot( [f, f], plot_range, **plot_rsc['masked'] )[0]
 
    if l0:
        l0.set_label( plot_labels['resonance'] )
    if l1:
        l1.set_label( plot_labels['masked'] )

    if new_axes:
        ax.set_xlim( [freq_range[0], freq_range[-1]] )
        ax.set_ylim( plot_range )

    if show:
        plt.show()
    return fig 

def plot_logs( fig_num, plot_rsc, plot_labels,
               freqs, logs, valid, freq_range, plot_range, squared,
               draw_eigs = True, show_legend = True, show = False,
               clear = False, new_axes = False ):
    """
    Plot logs of min/middle/max eigs of M.
    """
    if plt is None: return

    fig = plt.figure( fig_num )
    if clear:
        fig.clf()
    if new_axes:
        ax = fig.add_subplot( 111 )
    else:
        ax = fig.gca()

    if draw_eigs:
        aux = plot_eigs( fig_num, plot_rsc, plot_labels, valid, freq_range,
                         plot_range )

    for ii, log in enumerate( logs ):
        l1 = ax.plot( freqs[ii], log[:,0], **plot_rsc['eig_min'] )
        l2 = ax.plot( freqs[ii], log[:,-1], **plot_rsc['eig_max'] )
        if log.shape[1] == 3:
            l3 = ax.plot( freqs[ii], log[:,1], **plot_rsc['eig_mid'] )
        else:
            l3 = None
            
    l1[0].set_label( plot_labels['eig_min'] )
    l2[0].set_label( plot_labels['eig_max'] )
    if l3:
        l3[0].set_label( plot_labels['eig_mid'] )

    fmin, fmax = freqs[0][0], freqs[-1][-1]
    ax.plot( [fmin, fmax], [0, 0], **plot_rsc['x_axis'] )

    if squared:
        ax.set_xlabel( r'$\lambda$, $\omega^2$' )
    else:
        ax.set_xlabel( r'$\sqrt{\lambda}$, $\omega$' )

    ax.set_ylabel( plot_labels['y_axis'] )

    if new_axes:
        ax.set_xlim( [fmin, fmax] )
        ax.set_ylim( plot_range )

    if show_legend:
        ax.legend()

    if show:
        plt.show()
    return fig


def plot_gap(ax, ii, f0, f1, kind, kind_desc, gmin, gmax, plot_range, plot_rsc):
    def draw_rect( ax, x, y, rsc ):
        ax.fill( nm.asarray( x )[[0,1,1,0]],
                 nm.asarray( y )[[0,0,1,1]],
                 **rsc )

    # Colors.
    strong = plot_rsc['strong_gap']
    weak = plot_rsc['weak_gap']
    propagation = plot_rsc['propagation']

    if kind == 'p':
        draw_rect( ax, (f0, f1), plot_range, propagation )
        info = [(f0, f1)]
    elif kind == 'w':
        draw_rect( ax, (f0, f1), plot_range, weak )
        info = [(f0, f1)]
    elif kind == 'wp':
        draw_rect( ax, (f0, gmin[1]), plot_range, weak )
        draw_rect( ax, (gmin[1], f1), plot_range, propagation )
        info = [(f0, gmin[1]), (gmin[1], f1)]
    elif kind == 's':
        draw_rect( ax, (f0, f1), plot_range, strong )
        info = [(f0, f1)]
    elif kind == 'sw':
        draw_rect( ax, (f0, gmax[1]), plot_range, strong )
        draw_rect( ax, (gmax[1], f1), plot_range, weak )
        info = [(f0, gmax[1]), (gmax[1], f1)]
    elif kind == 'swp':
        draw_rect( ax, (f0, gmax[1]), plot_range, strong )
        draw_rect( ax, (gmax[1], gmin[1]), plot_range, weak )
        draw_rect( ax, (gmin[1], f1), plot_range, propagation )
        info = [(f0, gmax[1]), (gmax[1], gmin[1]), (gmin[1], f1)]
    elif kind == 'is':
        draw_rect( ax, (gmin[1], gmax[1]), plot_range, strong )
        info = [(gmin[1], gmax[1])]
    elif kind == 'iw':
        draw_rect( ax, (gmin[1], gmax[1]), plot_range, weak )
        info = [(gmin[1], gmax[1])]
    else:
        output( 'impossible band gap combination:' )
        output( gmin, gmax )
        raise ValueError

    output( ii, gmin[0], gmax[0], '%.8f' % f0, '%.8f' % f1 )
    output( ' -> %s\n    %s' %(kind_desc, info) )

def plot_gaps( fig_num, plot_rsc, gaps, kinds, freq_range,
               plot_range, show = False, clear = False, new_axes = False ):
    """ """
    if plt is None: return

    fig = plt.figure( fig_num )
    if clear:
        fig.clf()
    if new_axes:
        ax = fig.add_subplot( 111 )
    else:
        ax = fig.gca()

    for ii in xrange( len( freq_range ) - 1 ):
        f0, f1 = freq_range[[ii, ii+1]]
        gap = gaps[ii]
        if isinstance(gap, list):
            for ig, (gmin, gmax) in enumerate(gap):
                kind, kind_desc = kinds[ii][ig]
                plot_gap(ax, ii, f0, f1, kind, kind_desc, gmin, gmax,
                         plot_range, plot_rsc)
        else:
            gmin, gmax = gap
            kind, kind_desc = kinds[ii]
            plot_gap(ax, ii, f0, f1, kind, kind_desc, gmin, gmax,
                     plot_range, plot_rsc)

    if new_axes:
        ax.set_xlim( [freq_range[0], freq_range[-1]] )
        ax.set_ylim( plot_range )

    if show:
        plt.show()
    return fig