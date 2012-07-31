import numpy as nm

from sfepy.base.testing import TestCommon

rsels = {
    '2_4' : 'nodes in (y > 0.9) & (y < 1.1)',
    '3_8' : 'nodes in (z > 0.9) & (z < 1.1)',
}

eps = 1e-5

shifts = {
    '2_4' : nm.array([[0.0, 1.0], [0.0, eps]], dtype=nm.float64),
    '3_8' : nm.array([[0.0, 0.0, 1.0], [0.0, 0.0, eps]], dtype=nm.float64),
}

rots = {
    '2_4' : None,
    '3_8' : None,
}

class Test(TestCommon):

    @staticmethod
    def from_conf(conf, options):
        from sfepy.fem.geometry_element import GeometryElement

        gels = {}
        for key in ['2_3', '2_4', '3_4', '3_8']:
            gel = GeometryElement(key)
            gel.create_surface_facet()
            gels[key] = gel

        return Test(conf=conf, options=options, gels=gels)

    def test_continuity(self):
        import sfepy
        from sfepy.base.base import Struct
        from sfepy.fem import Mesh, Domain, Field, FieldVariable, Integral
        from sfepy.fem.global_interp import get_ref_coors

        ok = True
        integral = Integral('i', order=5)

        for geom in ['2_4', '3_8']:
            mesh0 = Mesh.from_file('meshes/elements/%s_2.mesh' % geom,
                                   prefix_dir=sfepy.data_dir)
            gel = self.gels[geom]

            perms = gel.get_conn_permutations()

            qps, _ = integral.get_qp(gel.surface_facet.name)
            zz = nm.zeros_like(qps[:, :1])
            qps = nm.hstack(([qps] + [zz]))

            rot = rots[geom]
            if rot is not None:
                pass

            shift = shifts[geom]

            bads = []

            for ir, pr in enumerate(perms):
                for ic, pc in enumerate(perms):
                    self.report('ir: %d, ic: %d' % (ir, ic))

                    mesh = mesh0.copy()
                    conn = mesh.conns[0]
                    conn[0, :] = conn[0, pr]
                    conn[1, :] = conn[1, pc]

                    cache = Struct(mesh=mesh)

                    domain = Domain('domain', mesh)
                    omega = domain.create_region('Omega', 'all')
                    region = domain.create_region('Facet', rsels[geom])
                    field = Field.from_args('f', nm.float64, shape=1,
                                            region=omega, approx_order=5,
                                            poly_space_base='lobatto')
                    var = FieldVariable('u', 'unknown', field, 1)
                    self.report('# dofs: %d' % var.n_dof)

                    vec = nm.empty(var.n_dof, dtype=var.dtype)
                    ap = field.aps[0]
                    ps = ap.interp.poly_spaces['v']

                    dofs = field.get_dofs_in_region_group(region, 0,
                                                          merge=False)
                    edofs, fdofs = nm.unique(dofs[1]), nm.unique(dofs[2])

                    rcoors = nm.ascontiguousarray(qps
                                                  + shift[:1, :] - shift[1:, :])
                    ccoors = nm.ascontiguousarray(qps
                                                  + shift[:1, :] + shift[1:, :])

                    for ip in nm.r_[edofs, fdofs]:
                        vec.fill(0.0)
                        vec[ip] = 1.0

                        evec = vec[ap.econn]

                        rrc, rcells, rstatus = get_ref_coors(field, rcoors,
                                                             cache=cache)
                        crc, ccells, cstatus = get_ref_coors(field, ccoors,
                                                             cache=cache)

                        rbf = ps.eval_base(rrc, ori=ap.ori[:1])
                        cbf = ps.eval_base(crc, ori=ap.ori[1:])

                        rvals = nm.dot(rbf, evec[0])
                        cvals = nm.dot(cbf, evec[1])

                        _ok = nm.allclose(rvals, cvals, atol=1e-14, rtol=0.0)
                        self.report('dof %d: %s' % (ip, _ok))
                        if not _ok:
                            bads.append([geom, ir, ic, ip])

                        ok = ok and _ok

        if not ok:
            self.report('continuity errors:\n', bads)
            self.report('%d in total!' % len(bads))

        return ok