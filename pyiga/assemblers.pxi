# file generated by generate-assemblers.py

################################################################################
# 2D Assemblers
################################################################################

cdef class BaseAssembler2D:
    cdef int nqp
    cdef size_t[2] ndofs
    cdef int[2] p
    cdef vector[ssize_t[:,::1]] meshsupp
    cdef list _asm_pool     # list of shared clones for multithreading

    cdef void base_init(self, kvs):
        assert len(kvs) == 2, "Assembler requires two knot vectors"
        self.nqp = max([kv.p for kv in kvs]) + 1
        self.ndofs[:] = [kv.numdofs for kv in kvs]
        self.p[:]     = [kv.p for kv in kvs]
        self.meshsupp = [kvs[k].mesh_support_idx_all() for k in range(2)]
        self._asm_pool = []

    cdef _share_base(self, BaseAssembler2D asm):
        asm.nqp = self.nqp
        asm.ndofs[:] = self.ndofs[:]
        asm.meshsupp = self.meshsupp

    cdef BaseAssembler2D shared_clone(self):
        return self     # by default assume thread safety

    cdef inline size_t to_seq(self, size_t[2] ii) nogil:
        # by convention, the order of indices is (y,x)
        return (ii[0]) * self.ndofs[1] + ii[1]

    @cython.boundscheck(False)
    @cython.wraparound(False)
    @cython.cdivision(True)
    cdef inline void from_seq(self, size_t i, size_t[2] out) nogil:
        out[1] = i % self.ndofs[1]
        i /= self.ndofs[1]
        out[0] = i

    cdef double assemble_impl(self, size_t[2] i, size_t[2] j) nogil:
        return -9999.99  # Not implemented

    cpdef double assemble(self, size_t i, size_t j):
        cdef size_t[2] I, J
        with nogil:
            self.from_seq(i, I)
            self.from_seq(j, J)
            return self.assemble_impl(I, J)

    @cython.boundscheck(False)
    @cython.wraparound(False)
    cdef void multi_assemble_chunk(self, size_t[:,::1] idx_arr, double[::1] out) nogil:
        cdef size_t[2] I, J
        cdef size_t k

        for k in range(idx_arr.shape[0]):
            self.from_seq(idx_arr[k,0], I)
            self.from_seq(idx_arr[k,1], J)
            out[k] = self.assemble_impl(I, J)

    def multi_assemble(self, indices):
        """Assemble all entries given by `indices`.

        Args:
            indices: a sequence of `(i,j)` pairs or an `ndarray`
            of size `N x 2`.
        """
        cdef size_t[:,::1] idx_arr
        if isinstance(indices, np.ndarray):
            idx_arr = np.asarray(indices, order='C', dtype=np.uintp)
        else:   # possibly given as iterator
            idx_arr = np.array(list(indices), dtype=np.uintp)

        cdef double[::1] result = np.empty(idx_arr.shape[0])

        num_threads = pyiga.get_max_threads()
        if num_threads <= 1:
            self.multi_assemble_chunk(idx_arr, result)
        else:
            thread_pool = get_thread_pool()
            if not self._asm_pool:
                self._asm_pool = [self] + [self.shared_clone()
                        for i in range(1, thread_pool._max_workers)]

            results = thread_pool.map(_asm_chunk_2d,
                        self._asm_pool,
                        chunk_tasks(idx_arr, num_threads),
                        chunk_tasks(result, num_threads))
            list(results)   # wait for threads to finish
        return result

cpdef void _asm_chunk_2d(BaseAssembler2D asm, size_t[:,::1] idxchunk, double[::1] out):
    with nogil:
        asm.multi_assemble_chunk(idxchunk, out)


@cython.boundscheck(False)
@cython.wraparound(False)
cpdef object generic_assemble_core_2d(BaseAssembler2D asm, bidx, bint symmetric=False):
    cdef unsigned[:, ::1] bidx0, bidx1
    cdef long mu0, mu1, MU0, MU1
    cdef double[:, ::1] entries

    bidx0, bidx1 = bidx
    MU0, MU1 = bidx0.shape[0], bidx1.shape[0]

    cdef size_t[::1] transp0, transp1
    if symmetric:
        transp0 = get_transpose_idx_for_bidx(bidx0)
        transp1 = get_transpose_idx_for_bidx(bidx1)
    else:
        transp0 = transp1 = None

    entries = np.zeros((MU0, MU1))

    cdef int num_threads = pyiga.get_max_threads()

    for mu0 in prange(MU0, num_threads=num_threads, nogil=True):
        _asm_core_2d_kernel(asm, symmetric,
            bidx0, bidx1,
            transp0, transp1,
            entries,
            mu0)
    return entries

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.initializedcheck(False)
cdef void _asm_core_2d_kernel(
    BaseAssembler2D asm,
    bint symmetric,
    unsigned[:, ::1] bidx0, unsigned[:, ::1] bidx1,
    size_t[::1] transp0, size_t[::1] transp1,
    double[:, ::1] entries,
    long _mu0
) nogil:
    cdef size_t[2] i, j
    cdef int diag0, diag1
    cdef double entry
    cdef long mu0, mu1, MU0, MU1

    mu0 = _mu0
    MU0, MU1 = bidx0.shape[0], bidx1.shape[0]

    i[0] = bidx0[mu0, 0]
    j[0] = bidx0[mu0, 1]

    if symmetric:
        diag0 = <int>j[0] - <int>i[0]
        if diag0 > 0:       # block is above diagonal?
            return

    for mu1 in range(MU1):
        i[1] = bidx1[mu1, 0]
        j[1] = bidx1[mu1, 1]

        if symmetric:
            diag1 = <int>j[1] - <int>i[1]
            if diag0 == 0 and diag1 > 0:
                continue

        entry = asm.assemble_impl(i, j)
        entries[mu0, mu1] = entry

        if symmetric:
            if diag0 != 0 or diag1 != 0:     # are we off the diagonal?
                entries[ transp0[mu0], transp1[mu1] ] = entry   # then also write into the transposed entry


cdef generic_assemble_2d_parallel(BaseAssembler2D asm, symmetric=False):
    mlb = MLBandedMatrix(
        tuple(asm.ndofs),
        tuple(asm.p)
    )
    X = generic_assemble_core_2d(asm, mlb.bidx, symmetric=symmetric)
    mlb.data = X
    return mlb.asmatrix()


# helper function for fast low-rank assembler
cdef double _entry_func_2d(size_t i, size_t j, void * data):
    return (<BaseAssembler2D>data).assemble(i, j)

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.initializedcheck(False)
cdef double combine_mass_2d(
        double[ :, ::1 ] J,
        double* Vu0, double* Vu1,
        double* Vv0, double* Vv1,
    ) nogil:
    cdef size_t n0 = J.shape[0]
    cdef size_t n1 = J.shape[1]

    cdef size_t i0, i1
    cdef double result = 0.0
    cdef double vu, vv

    for i0 in range(n0):
        for i1 in range(n1):
            vu = Vu0[i0] * Vu1[i1]
            vv = Vv0[i0] * Vv1[i1]

            result += vu * vv * J[i0, i1]

    return result

cdef class MassAssembler2D(BaseAssembler2D):
    cdef vector[double[:, :, ::1]] C       # 1D basis values. Indices: basis function, mesh point, derivative(0)
    cdef double[:, ::1] weights

    def __init__(self, kvs, geo):
        assert geo.dim == 2, "Geometry has wrong dimension"
        self.base_init(kvs)

        gauss = [make_iterated_quadrature(np.unique(kv.kv), self.nqp) for kv in kvs]
        gaussgrid = [g[0] for g in gauss]
        gaussweights = [g[1] for g in gauss]

        colloc = [bspline.collocation_derivs(kvs[k], gaussgrid[k], derivs=0) for k in range(2)]
        for k in range(2):
            colloc[k] = tuple(X.T.A for X in colloc[k])
        self.C = [np.stack(Cs, axis=-1) for Cs in colloc]

        geo_jac    = geo.grid_jacobian(gaussgrid)
        geo_det    = determinants(geo_jac)
        self.weights = gaussweights[0][:,None] * gaussweights[1][None,:] * np.abs(geo_det)

    @cython.boundscheck(False)
    @cython.wraparound(False)
    @cython.initializedcheck(False)
    cdef double assemble_impl(self, size_t[2] i, size_t[2] j) nogil:
        cdef int k
        cdef IntInterval intv
        cdef size_t g_sta[2]
        cdef size_t g_end[2]
        cdef (double*) values_i[2]
        cdef (double*) values_j[2]

        for k in range(2):
            intv = intersect_intervals(make_intv(self.meshsupp[k][i[k],0], self.meshsupp[k][i[k],1]),
                                       make_intv(self.meshsupp[k][j[k],0], self.meshsupp[k][j[k],1]))
            if intv.a >= intv.b:
                return 0.0      # no intersection of support
            g_sta[k] = self.nqp * intv.a    # start of Gauss nodes
            g_end[k] = self.nqp * intv.b    # end of Gauss nodes

            values_i[k] = &self.C[k][ i[k], g_sta[k], 0 ]
            values_j[k] = &self.C[k][ j[k], g_sta[k], 0 ]

        return combine_mass_2d(
                self.weights [ g_sta[0]:g_end[0], g_sta[1]:g_end[1] ],
                values_i[0], values_i[1],
                values_j[0], values_j[1]
        )

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.initializedcheck(False)
cdef double combine_stiff_2d(
        double[:, :, :, ::1] B,
        double* VDu0, double* VDu1,
        double* VDv0, double* VDv1,
    ) nogil:
    cdef size_t n0 = B.shape[0]
    cdef size_t n1 = B.shape[1]

    cdef size_t i0, i1
    cdef double gu[2]
    cdef double gv[2]
    cdef double result = 0.0
    cdef double *Bptr


    for i0 in range(n0):
        for i1 in range(n1):

            Bptr = &B[i0, i1, 0, 0]

            gu[0] = VDu0[2*i0+0] * VDu1[2*i1+1]
            gu[1] = VDu0[2*i0+1] * VDu1[2*i1+0]

            gv[0] = VDv0[2*i0+0] * VDv1[2*i1+1]
            gv[1] = VDv0[2*i0+1] * VDv1[2*i1+0]


            result += (Bptr[0+0]*gu[0] + Bptr[0+1]*gu[1]) * gv[0]
            result += (Bptr[2+0]*gu[0] + Bptr[2+1]*gu[1]) * gv[1]

    return result


cdef class StiffnessAssembler2D(BaseAssembler2D):
    cdef vector[double[:, :, ::1]] C            # 1D basis values. Indices: basis function, mesh point, derivative
    cdef double[:, :, :, ::1] B   # transformation matrix. Indices: DIM x mesh point, i, j

    def __init__(self, kvs, geo):
        assert geo.dim == 2, "Geometry has wrong dimension"
        self.base_init(kvs)

        gauss = [make_iterated_quadrature(np.unique(kv.kv), self.nqp) for kv in kvs]
        gaussgrid = [g[0] for g in gauss]
        gaussweights = [g[1] for g in gauss]

        colloc = [bspline.collocation_derivs(kvs[k], gaussgrid[k], derivs=1) for k in range(2)]
        for k in range(2):
            colloc[k] = tuple(X.T.A for X in colloc[k])
        self.C = [np.stack(Cs, axis=-1) for Cs in colloc]

        geo_jac = geo.grid_jacobian(gaussgrid)
        geo_det, geo_jacinv = det_and_inv(geo_jac)
        weights = gaussweights[0][:,None] * gaussweights[1][None,:] * np.abs(geo_det)
        self.B = matmatT_2x2(geo_jacinv) * weights[ :, :, None, None ]

    @cython.boundscheck(False)
    @cython.wraparound(False)
    @cython.initializedcheck(False)
    cdef double assemble_impl(self, size_t[2] i, size_t[2] j) nogil:
        cdef int k
        cdef IntInterval intv
        cdef size_t g_sta[2]
        cdef size_t g_end[2]
        cdef (double*) values_i[2]
        cdef (double*) values_j[2]

        for k in range(2):
            intv = intersect_intervals(make_intv(self.meshsupp[k][i[k],0], self.meshsupp[k][i[k],1]),
                                       make_intv(self.meshsupp[k][j[k],0], self.meshsupp[k][j[k],1]))
            if intv.a >= intv.b:
                return 0.0      # no intersection of support
            g_sta[k] = self.nqp * intv.a    # start of Gauss nodes
            g_end[k] = self.nqp * intv.b    # end of Gauss nodes

            values_i[k] = &self.C[k][ i[k], g_sta[k], 0 ]
            values_j[k] = &self.C[k][ j[k], g_sta[k], 0 ]

        return combine_stiff_2d(
                self.B [ g_sta[0]:g_end[0], g_sta[1]:g_end[1] ],
                values_i[0], values_i[1],
                values_j[0], values_j[1]
        )
################################################################################
# 3D Assemblers
################################################################################

cdef class BaseAssembler3D:
    cdef int nqp
    cdef size_t[3] ndofs
    cdef int[3] p
    cdef vector[ssize_t[:,::1]] meshsupp
    cdef list _asm_pool     # list of shared clones for multithreading

    cdef void base_init(self, kvs):
        assert len(kvs) == 3, "Assembler requires two knot vectors"
        self.nqp = max([kv.p for kv in kvs]) + 1
        self.ndofs[:] = [kv.numdofs for kv in kvs]
        self.p[:]     = [kv.p for kv in kvs]
        self.meshsupp = [kvs[k].mesh_support_idx_all() for k in range(3)]
        self._asm_pool = []

    cdef _share_base(self, BaseAssembler3D asm):
        asm.nqp = self.nqp
        asm.ndofs[:] = self.ndofs[:]
        asm.meshsupp = self.meshsupp

    cdef BaseAssembler3D shared_clone(self):
        return self     # by default assume thread safety

    cdef inline size_t to_seq(self, size_t[3] ii) nogil:
        # by convention, the order of indices is (y,x)
        return ((ii[0]) * self.ndofs[1] + ii[1]) * self.ndofs[2] + ii[2]

    @cython.boundscheck(False)
    @cython.wraparound(False)
    @cython.cdivision(True)
    cdef inline void from_seq(self, size_t i, size_t[3] out) nogil:
        out[2] = i % self.ndofs[2]
        i /= self.ndofs[2]
        out[1] = i % self.ndofs[1]
        i /= self.ndofs[1]
        out[0] = i

    cdef double assemble_impl(self, size_t[3] i, size_t[3] j) nogil:
        return -9999.99  # Not implemented

    cpdef double assemble(self, size_t i, size_t j):
        cdef size_t[3] I, J
        with nogil:
            self.from_seq(i, I)
            self.from_seq(j, J)
            return self.assemble_impl(I, J)

    @cython.boundscheck(False)
    @cython.wraparound(False)
    cdef void multi_assemble_chunk(self, size_t[:,::1] idx_arr, double[::1] out) nogil:
        cdef size_t[3] I, J
        cdef size_t k

        for k in range(idx_arr.shape[0]):
            self.from_seq(idx_arr[k,0], I)
            self.from_seq(idx_arr[k,1], J)
            out[k] = self.assemble_impl(I, J)

    def multi_assemble(self, indices):
        """Assemble all entries given by `indices`.

        Args:
            indices: a sequence of `(i,j)` pairs or an `ndarray`
            of size `N x 2`.
        """
        cdef size_t[:,::1] idx_arr
        if isinstance(indices, np.ndarray):
            idx_arr = np.asarray(indices, order='C', dtype=np.uintp)
        else:   # possibly given as iterator
            idx_arr = np.array(list(indices), dtype=np.uintp)

        cdef double[::1] result = np.empty(idx_arr.shape[0])

        num_threads = pyiga.get_max_threads()
        if num_threads <= 1:
            self.multi_assemble_chunk(idx_arr, result)
        else:
            thread_pool = get_thread_pool()
            if not self._asm_pool:
                self._asm_pool = [self] + [self.shared_clone()
                        for i in range(1, thread_pool._max_workers)]

            results = thread_pool.map(_asm_chunk_3d,
                        self._asm_pool,
                        chunk_tasks(idx_arr, num_threads),
                        chunk_tasks(result, num_threads))
            list(results)   # wait for threads to finish
        return result

cpdef void _asm_chunk_3d(BaseAssembler3D asm, size_t[:,::1] idxchunk, double[::1] out):
    with nogil:
        asm.multi_assemble_chunk(idxchunk, out)


@cython.boundscheck(False)
@cython.wraparound(False)
cpdef object generic_assemble_core_3d(BaseAssembler3D asm, bidx, bint symmetric=False):
    cdef unsigned[:, ::1] bidx0, bidx1, bidx2
    cdef long mu0, mu1, mu2, MU0, MU1, MU2
    cdef double[:, :, ::1] entries

    bidx0, bidx1, bidx2 = bidx
    MU0, MU1, MU2 = bidx0.shape[0], bidx1.shape[0], bidx2.shape[0]

    cdef size_t[::1] transp0, transp1, transp2
    if symmetric:
        transp0 = get_transpose_idx_for_bidx(bidx0)
        transp1 = get_transpose_idx_for_bidx(bidx1)
        transp2 = get_transpose_idx_for_bidx(bidx2)
    else:
        transp0 = transp1 = transp2 = None

    entries = np.zeros((MU0, MU1, MU2))

    cdef int num_threads = pyiga.get_max_threads()

    for mu0 in prange(MU0, num_threads=num_threads, nogil=True):
        _asm_core_3d_kernel(asm, symmetric,
            bidx0, bidx1, bidx2,
            transp0, transp1, transp2,
            entries,
            mu0)
    return entries

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.initializedcheck(False)
cdef void _asm_core_3d_kernel(
    BaseAssembler3D asm,
    bint symmetric,
    unsigned[:, ::1] bidx0, unsigned[:, ::1] bidx1, unsigned[:, ::1] bidx2,
    size_t[::1] transp0, size_t[::1] transp1, size_t[::1] transp2,
    double[:, :, ::1] entries,
    long _mu0
) nogil:
    cdef size_t[3] i, j
    cdef int diag0, diag1, diag2
    cdef double entry
    cdef long mu0, mu1, mu2, MU0, MU1, MU2

    mu0 = _mu0
    MU0, MU1, MU2 = bidx0.shape[0], bidx1.shape[0], bidx2.shape[0]

    i[0] = bidx0[mu0, 0]
    j[0] = bidx0[mu0, 1]

    if symmetric:
        diag0 = <int>j[0] - <int>i[0]
        if diag0 > 0:       # block is above diagonal?
            return

    for mu1 in range(MU1):
        i[1] = bidx1[mu1, 0]
        j[1] = bidx1[mu1, 1]

        if symmetric:
            diag1 = <int>j[1] - <int>i[1]
            if diag0 == 0 and diag1 > 0:
                continue

        for mu2 in range(MU2):
            i[2] = bidx2[mu2, 0]
            j[2] = bidx2[mu2, 1]

            if symmetric:
                diag2 = <int>j[2] - <int>i[2]
                if diag0 == 0 and diag1 == 0 and diag2 > 0:
                    continue

            entry = asm.assemble_impl(i, j)
            entries[mu0, mu1, mu2] = entry

            if symmetric:
                if diag0 != 0 or diag1 != 0 or diag2 != 0:     # are we off the diagonal?
                    entries[ transp0[mu0], transp1[mu1], transp2[mu2] ] = entry   # then also write into the transposed entry


cdef generic_assemble_3d_parallel(BaseAssembler3D asm, symmetric=False):
    mlb = MLBandedMatrix(
        tuple(asm.ndofs),
        tuple(asm.p)
    )
    X = generic_assemble_core_3d(asm, mlb.bidx, symmetric=symmetric)
    mlb.data = X
    return mlb.asmatrix()


# helper function for fast low-rank assembler
cdef double _entry_func_3d(size_t i, size_t j, void * data):
    return (<BaseAssembler3D>data).assemble(i, j)

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.initializedcheck(False)
cdef double combine_mass_3d(
        double[ :, :, ::1 ] J,
        double* Vu0, double* Vu1, double* Vu2,
        double* Vv0, double* Vv1, double* Vv2,
    ) nogil:
    cdef size_t n0 = J.shape[0]
    cdef size_t n1 = J.shape[1]
    cdef size_t n2 = J.shape[2]

    cdef size_t i0, i1, i2
    cdef double result = 0.0
    cdef double vu, vv

    for i0 in range(n0):
        for i1 in range(n1):
            for i2 in range(n2):
                vu = Vu0[i0] * Vu1[i1] * Vu2[i2]
                vv = Vv0[i0] * Vv1[i1] * Vv2[i2]

                result += vu * vv * J[i0, i1, i2]

    return result

cdef class MassAssembler3D(BaseAssembler3D):
    cdef vector[double[:, :, ::1]] C       # 1D basis values. Indices: basis function, mesh point, derivative(0)
    cdef double[:, :, ::1] weights

    def __init__(self, kvs, geo):
        assert geo.dim == 3, "Geometry has wrong dimension"
        self.base_init(kvs)

        gauss = [make_iterated_quadrature(np.unique(kv.kv), self.nqp) for kv in kvs]
        gaussgrid = [g[0] for g in gauss]
        gaussweights = [g[1] for g in gauss]

        colloc = [bspline.collocation_derivs(kvs[k], gaussgrid[k], derivs=0) for k in range(3)]
        for k in range(3):
            colloc[k] = tuple(X.T.A for X in colloc[k])
        self.C = [np.stack(Cs, axis=-1) for Cs in colloc]

        geo_jac    = geo.grid_jacobian(gaussgrid)
        geo_det    = determinants(geo_jac)
        self.weights = gaussweights[0][:,None,None] * gaussweights[1][None,:,None] * gaussweights[2][None,None,:] * np.abs(geo_det)

    @cython.boundscheck(False)
    @cython.wraparound(False)
    @cython.initializedcheck(False)
    cdef double assemble_impl(self, size_t[3] i, size_t[3] j) nogil:
        cdef int k
        cdef IntInterval intv
        cdef size_t g_sta[3]
        cdef size_t g_end[3]
        cdef (double*) values_i[3]
        cdef (double*) values_j[3]

        for k in range(3):
            intv = intersect_intervals(make_intv(self.meshsupp[k][i[k],0], self.meshsupp[k][i[k],1]),
                                       make_intv(self.meshsupp[k][j[k],0], self.meshsupp[k][j[k],1]))
            if intv.a >= intv.b:
                return 0.0      # no intersection of support
            g_sta[k] = self.nqp * intv.a    # start of Gauss nodes
            g_end[k] = self.nqp * intv.b    # end of Gauss nodes

            values_i[k] = &self.C[k][ i[k], g_sta[k], 0 ]
            values_j[k] = &self.C[k][ j[k], g_sta[k], 0 ]

        return combine_mass_3d(
                self.weights [ g_sta[0]:g_end[0], g_sta[1]:g_end[1], g_sta[2]:g_end[2] ],
                values_i[0], values_i[1], values_i[2],
                values_j[0], values_j[1], values_j[2]
        )

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.initializedcheck(False)
cdef double combine_stiff_3d(
        double[:, :, :, :, ::1] B,
        double* VDu0, double* VDu1, double* VDu2,
        double* VDv0, double* VDv1, double* VDv2,
    ) nogil:
    cdef size_t n0 = B.shape[0]
    cdef size_t n1 = B.shape[1]
    cdef size_t n2 = B.shape[2]

    cdef size_t i0, i1, i2
    cdef double gu[3]
    cdef double gv[3]
    cdef double result = 0.0
    cdef double *Bptr


    for i0 in range(n0):
        for i1 in range(n1):
            for i2 in range(n2):

                Bptr = &B[i0, i1, i2, 0, 0]

                gu[0] = VDu0[2*i0+0] * VDu1[2*i1+0] * VDu2[2*i2+1]
                gu[1] = VDu0[2*i0+0] * VDu1[2*i1+1] * VDu2[2*i2+0]
                gu[2] = VDu0[2*i0+1] * VDu1[2*i1+0] * VDu2[2*i2+0]

                gv[0] = VDv0[2*i0+0] * VDv1[2*i1+0] * VDv2[2*i2+1]
                gv[1] = VDv0[2*i0+0] * VDv1[2*i1+1] * VDv2[2*i2+0]
                gv[2] = VDv0[2*i0+1] * VDv1[2*i1+0] * VDv2[2*i2+0]


                result += (Bptr[0+0]*gu[0] + Bptr[0+1]*gu[1] + Bptr[0+2]*gu[2]) * gv[0]
                result += (Bptr[3+0]*gu[0] + Bptr[3+1]*gu[1] + Bptr[3+2]*gu[2]) * gv[1]
                result += (Bptr[6+0]*gu[0] + Bptr[6+1]*gu[1] + Bptr[6+2]*gu[2]) * gv[2]

    return result


cdef class StiffnessAssembler3D(BaseAssembler3D):
    cdef vector[double[:, :, ::1]] C            # 1D basis values. Indices: basis function, mesh point, derivative
    cdef double[:, :, :, :, ::1] B   # transformation matrix. Indices: DIM x mesh point, i, j

    def __init__(self, kvs, geo):
        assert geo.dim == 3, "Geometry has wrong dimension"
        self.base_init(kvs)

        gauss = [make_iterated_quadrature(np.unique(kv.kv), self.nqp) for kv in kvs]
        gaussgrid = [g[0] for g in gauss]
        gaussweights = [g[1] for g in gauss]

        colloc = [bspline.collocation_derivs(kvs[k], gaussgrid[k], derivs=1) for k in range(3)]
        for k in range(3):
            colloc[k] = tuple(X.T.A for X in colloc[k])
        self.C = [np.stack(Cs, axis=-1) for Cs in colloc]

        geo_jac = geo.grid_jacobian(gaussgrid)
        geo_det, geo_jacinv = det_and_inv(geo_jac)
        weights = gaussweights[0][:,None,None] * gaussweights[1][None,:,None] * gaussweights[2][None,None,:] * np.abs(geo_det)
        self.B = matmatT_3x3(geo_jacinv) * weights[ :, :, :, None, None ]

    @cython.boundscheck(False)
    @cython.wraparound(False)
    @cython.initializedcheck(False)
    cdef double assemble_impl(self, size_t[3] i, size_t[3] j) nogil:
        cdef int k
        cdef IntInterval intv
        cdef size_t g_sta[3]
        cdef size_t g_end[3]
        cdef (double*) values_i[3]
        cdef (double*) values_j[3]

        for k in range(3):
            intv = intersect_intervals(make_intv(self.meshsupp[k][i[k],0], self.meshsupp[k][i[k],1]),
                                       make_intv(self.meshsupp[k][j[k],0], self.meshsupp[k][j[k],1]))
            if intv.a >= intv.b:
                return 0.0      # no intersection of support
            g_sta[k] = self.nqp * intv.a    # start of Gauss nodes
            g_end[k] = self.nqp * intv.b    # end of Gauss nodes

            values_i[k] = &self.C[k][ i[k], g_sta[k], 0 ]
            values_j[k] = &self.C[k][ j[k], g_sta[k], 0 ]

        return combine_stiff_3d(
                self.B [ g_sta[0]:g_end[0], g_sta[1]:g_end[1], g_sta[2]:g_end[2] ],
                values_i[0], values_i[1], values_i[2],
                values_j[0], values_j[1], values_j[2]
        )