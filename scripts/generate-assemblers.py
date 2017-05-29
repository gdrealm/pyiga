import os.path
from jinja2 import Template

tmpl_generic = Template(r'''
################################################################################
# {{DIM}}D Assemblers
################################################################################

cdef class BaseAssembler{{DIM}}D:
    cdef int nqp
    cdef size_t[{{DIM}}] ndofs
    cdef vector[ssize_t[:,::1]] meshsupp
    cdef list _asm_pool     # list of shared clones for multithreading

    cdef void base_init(self, kvs):
        assert len(kvs) == {{DIM}}, "Assembler requires two knot vectors"
        self.nqp = max([kv.p for kv in kvs]) + 1
        self.ndofs[:] = [kv.numdofs for kv in kvs]
        self.meshsupp = [kvs[k].mesh_support_idx_all() for k in range({{DIM}})]
        self._asm_pool = []

    cdef _share_base(self, BaseAssembler{{DIM}}D asm):
        asm.nqp = self.nqp
        asm.ndofs[:] = self.ndofs[:]
        asm.meshsupp = self.meshsupp

    cdef BaseAssembler{{DIM}}D shared_clone(self):
        return self     # by default assume thread safety

    cdef inline size_t to_seq(self, size_t[{{DIM}}] ii) nogil:
        # by convention, the order of indices is (y,x)
        return {{ to_seq(indices, ndofs) }}

    @cython.boundscheck(False)
    @cython.wraparound(False)
    @cython.cdivision(True)
    cdef inline void from_seq(self, size_t i, size_t[{{DIM}}] out) nogil:
        {%- for k in range(1, DIM)|reverse %}
        out[{{k}}] = i % self.ndofs[{{k}}]
        i /= self.ndofs[{{k}}]
        {%- endfor %}
        out[0] = i

    cdef double assemble_impl(self, size_t[{{DIM}}] i, size_t[{{DIM}}] j) nogil:
        return -9999.99  # Not implemented

    cpdef double assemble(self, size_t i, size_t j):
        cdef size_t[{{DIM}}] I, J
        with nogil:
            self.from_seq(i, I)
            self.from_seq(j, J)
            return self.assemble_impl(I, J)

    @cython.boundscheck(False)
    @cython.wraparound(False)
    cdef void multi_assemble_chunk(self, size_t[:,::1] idx_arr, double[::1] out) nogil:
        cdef size_t[{{DIM}}] I, J
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

            results = thread_pool.map(_asm_chunk_{{DIM}}d,
                        self._asm_pool,
                        chunk_tasks(idx_arr, num_threads),
                        chunk_tasks(result, num_threads))
            list(results)   # wait for threads to finish
        return result

cpdef void _asm_chunk_{{DIM}}d(BaseAssembler{{DIM}}D asm, size_t[:,::1] idxchunk, double[::1] out):
    with nogil:
        asm.multi_assemble_chunk(idxchunk, out)


@cython.boundscheck(False)
@cython.wraparound(False)
cdef object generic_assemble_{{DIM}}d(BaseAssembler{{DIM}}D asm, long chunk_start=-1, long chunk_end=-1):
    cdef size_t[{{DIM}}] i, j
    cdef size_t k, ii, jj
    cdef IntInterval intv

    cdef size_t[{{DIM}}] dof_start, dof_end, neigh_j_start, neigh_j_end
    cdef double entry
    cdef vector[double] entries
    cdef vector[size_t] entries_i, entries_j

    dof_start[:] = ({{ dimrepeat('0') }})
    dof_end[:] = asm.ndofs[:]

    if chunk_start >= 0:
        dof_start[0] = chunk_start
    if chunk_end >= 0:
        dof_end[0] = chunk_end

    i[:] = dof_start[:]
    with nogil:
        while True:         # loop over all i
            ii = asm.to_seq(i)

            for k in range({{DIM}}):
                intv = find_joint_support_functions(asm.meshsupp[k], i[k])
                neigh_j_start[k] = intv.a
                neigh_j_end[k] = intv.b
                j[k] = neigh_j_start[k]

            while True:     # loop j over all neighbors of i
                jj = asm.to_seq(j)
                if jj >= ii:
                    entry = asm.assemble_impl(i, j)

                    entries.push_back(entry)
                    entries_i.push_back(ii)
                    entries_j.push_back(jj)

                    if ii != jj:
                        entries.push_back(entry)
                        entries_i.push_back(jj)
                        entries_j.push_back(ii)

                if not next_lexicographic{{DIM}}(j, neigh_j_start, neigh_j_end):
                    break
            if not next_lexicographic{{DIM}}(i, dof_start, dof_end):
                break

    cdef size_t ne = entries.size()
    cdef size_t N = {{ dimrepeat("asm.ndofs[{}]", sep=' * ') }}
    return scipy.sparse.coo_matrix(
            (<double[:ne]> entries.data(),
                (<size_t[:ne]> entries_i.data(),
                 <size_t[:ne]> entries_j.data())),
            shape=(N,N)).tocsr()


cdef generic_assemble_{{DIM}}d_parallel(BaseAssembler{{DIM}}D asm):
    num_threads = pyiga.get_max_threads()
    if num_threads <= 1:
        return generic_assemble_{{DIM}}d(asm)
    def asm_chunk(rg):
        cdef BaseAssembler{{DIM}}D asm_clone = asm.shared_clone()
        return generic_assemble_{{DIM}}d(asm_clone, rg.start, rg.stop)
    results = get_thread_pool().map(asm_chunk, chunk_tasks(range_it(asm.ndofs[0]), 4*num_threads))
    return sum(results)


# helper function for fast low-rank assembler
cdef double _entry_func_{{DIM}}d(size_t i, size_t j, void * data):
    return (<BaseAssembler{{DIM}}D>data).assemble(i, j)
''')

macros = r"""
{%- macro init_basis_vals(numderivs) -%}
        assert geo.dim == {{DIM}}, "Geometry has wrong dimension"
        self.base_init(kvs)

        gauss = [make_iterated_quadrature(np.unique(kv.kv), self.nqp) for kv in kvs]
        gaussgrid = [g[0] for g in gauss]
        gaussweights = [g[1] for g in gauss]

        colloc = [bspline.collocation_derivs(kvs[k], gaussgrid[k], derivs={{numderivs}}) for k in range({{DIM}})]
        for k in range({{DIM}}):
            colloc[k] = tuple(X.T.A for X in colloc[k])
        self.C = [np.stack(Cs, axis=-1) for Cs in colloc]
{%- endmacro %}

{%- macro assemble_impl_header() -%}
    @cython.boundscheck(False)
    @cython.wraparound(False)
    @cython.initializedcheck(False)
    cdef double assemble_impl(self, size_t[{{DIM}}] i, size_t[{{DIM}}] j) nogil:
        cdef int k
        cdef IntInterval intv
        cdef size_t g_sta[{{DIM}}]
        cdef size_t g_end[{{DIM}}]
        cdef (double*) values_i[{{DIM}}]
        cdef (double*) values_j[{{DIM}}]

        for k in range({{DIM}}):
            intv = intersect_intervals(make_intv(self.meshsupp[k][i[k],0], self.meshsupp[k][i[k],1]),
                                       make_intv(self.meshsupp[k][j[k],0], self.meshsupp[k][j[k],1]))
            if intv.a >= intv.b:
                return 0.0      # no intersection of support
            g_sta[k] = self.nqp * intv.a    # start of Gauss nodes
            g_end[k] = self.nqp * intv.b    # end of Gauss nodes

            values_i[k] = &self.C[k][ i[k], g_sta[k], 0 ]
            values_j[k] = &self.C[k][ j[k], g_sta[k], 0 ]
{% endmacro %}

{%- macro make_grad(output, var, idx) -%}
{% for k in range(DIM) %}
{{output}}[{{k}}] = {{ grad_comp(var, idx, k) }}
{%- endfor %}
{% endmacro %}
"""

tmpl_mass_asm = Template(macros + r"""
@cython.boundscheck(False)
@cython.wraparound(False)
@cython.initializedcheck(False)
cdef double combine_mass_{{DIM}}d(
        double[ {{dimrepeat(':')}}:1 ] J,
        {{ dimrepeat('double* Vu{}') }},
        {{ dimrepeat('double* Vv{}') }},
    ) nogil:

{%- for k in range(DIM) %}
    cdef size_t n{{k}} = J.shape[{{k}}]
{%- endfor %}

    cdef size_t {{ dimrepeat('i{}') }}
    cdef double result = 0.0
    cdef double vu, vv
{% for k in range(DIM) %}
    {{ indent(k) }}for i{{k}} in range(n{{k}}):
{%- endfor %}
    {{ indent(DIM) }}vu = {{ dimrepeat('Vu{0}[i{0}]', sep=' * ') }}
    {{ indent(DIM) }}vv = {{ dimrepeat('Vv{0}[i{0}]', sep=' * ') }}

    {{ indent(DIM) }}result += vu * vv * J[{{ dimrepeat('i{}') }}]

    return result

cdef class MassAssembler{{DIM}}D(BaseAssembler{{DIM}}D):
    cdef vector[double[:, :, ::1]] C       # 1D basis values. Indices: basis function, mesh point, derivative(0)
    cdef double[{{dimrepeat(':')}}:1] weights

    def __init__(self, kvs, geo):
        {{ init_basis_vals(numderivs=0) }}

        geo_jac    = geo.grid_jacobian(gaussgrid)
        geo_det    = determinants(geo_jac)
        self.weights = {{ tensorprod('gaussweights') }} * np.abs(geo_det)

    {{ assemble_impl_header() }}
        return combine_mass_{{DIM}}d(
                self.weights [ {{ dimrepeat("g_sta[{0}]:g_end[{0}]") }} ],
                {{ dimrepeat("values_i[{}]") }},
                {{ dimrepeat("values_j[{}]") }}
        )
""")

tmpl_stiffness_asm = Template(macros + r"""
@cython.boundscheck(False)
@cython.wraparound(False)
@cython.initializedcheck(False)
cdef double combine_stiff_{{DIM}}d(
        double[{{ dimrepeat(':') }}, :, ::1] B,
        {{ dimrepeat('double* VDu{}') }},
        {{ dimrepeat('double* VDv{}') }},
    ) nogil:

{%- for k in range(DIM) %}
    cdef size_t n{{k}} = B.shape[{{k}}]
{%- endfor %}

    cdef size_t {{ dimrepeat('i{}') }}
    cdef double gu[{{DIM}}]
    cdef double gv[{{DIM}}]
    cdef double result = 0.0
    cdef double *Bptr

{% for k in range(DIM) %}
    {{ indent(k) }}for i{{k}} in range(n{{k}}):
{%- endfor %}

    {{ indent(DIM) }}Bptr = &B[{{ dimrepeat('i{}') }}, 0, 0]
{{ make_grad('gu', 'VDu', 'i') | indent(4*(DIM + 1)) }}
{{ make_grad('gv', 'VDv', 'i') | indent(4*(DIM + 1)) }}

{% for k in range(DIM) -%}
{% set K = (DIM*k)|string %}
    {{ indent(DIM) }}result += ({{ dimrepeat('Bptr[' + K + '+{0}]*gu[{0}]', sep=' + ') }}) * gv[{{k}}]
{%- endfor %}

    return result


cdef class StiffnessAssembler{{DIM}}D(BaseAssembler{{DIM}}D):
    cdef vector[double[:, :, ::1]] C            # 1D basis values. Indices: basis function, mesh point, derivative
    cdef double[{{dimrepeat(':')}}, :, ::1] B   # transformation matrix. Indices: DIM x mesh point, i, j

    def __init__(self, kvs, geo):
        {{ init_basis_vals(numderivs=1) }}

        geo_jac = geo.grid_jacobian(gaussgrid)
        geo_det, geo_jacinv = det_and_inv(geo_jac)
        weights = {{ tensorprod('gaussweights') }} * np.abs(geo_det)
        self.B = matmatT_{{DIM}}x{{DIM}}(geo_jacinv) * weights[ {{dimrepeat(":")}}, None, None ]

    {{ assemble_impl_header() }}
        return combine_stiff_{{DIM}}d(
                self.B [ {{ dimrepeat("g_sta[{0}]:g_end[{0}]") }} ],
                {{ dimrepeat("values_i[{}]") }},
                {{ dimrepeat("values_j[{}]") }}
        )
""")

def generate(dim):
    DIM = dim

    def dimrepeat(s, sep=', '):
        return sep.join([s.format(k) for k in range(DIM)])

    def to_seq(i, n):
        s = i[0]
        for k in range(1,DIM):
            s = '({0}) * {1} + {2}'.format(s, n[k], i[k])
        return s

    def extend_dim(i):
        # ex.: i = 1  ->  'None,:,None'
        slices = DIM * ['None']
        slices[i] = ':'
        return ','.join(slices)

    def tensorprod(var):
        return ' * '.join(['{0}[{1}][{2}]'.format(var, k, extend_dim(k)) for k in range(DIM)])

    def indent(num):
        return num * '    ';

    def grad_comp(var, idx, j):
        factors = [
                "{var}{k}[2*{idx}{k}+{ofs}]".format(
                    var = var,
                    idx = idx,
                    k   = k,
                    ofs = (1 if k + j + 1 == DIM else 0)
                )
                for k in range(DIM)]
        return ' * '.join(factors)

    # helper variables for to_seq
    indices = ['ii[%d]' % k for k in range(DIM)]
    ndofs   = ['self.ndofs[%d]' % k for k in range(DIM)]

    env = locals()
    return ''.join(tmpl.render(env)
            for tmpl in (tmpl_generic, tmpl_mass_asm, tmpl_stiffness_asm))

path = os.path.join(os.path.dirname(__file__), "..", "pyiga", "assemblers.pxi")
with open(path, 'w') as f:
    f.write('# file generated by generate-assemblers.py\n')
    f.write(generate(dim=2))
    f.write(generate(dim=3))

