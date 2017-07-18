import os.path
from jinja2 import Template
from collections import OrderedDict
from functools import reduce
import operator
import numpy as np

class PyCode:
    def __init__(self):
        self._lines = []
        self._indent = ''

    def indent(self, num=1):
        self._indent += num * '    '

    def dedent(self, num=1):
        self._indent = self._indent[(4*num):]

    def put(self, s):
        if len(s) == 0:
            self._lines.append('')
        else:
            self._lines.append(self._indent + s)

    def putf(self, s, **kwargs):
        self.put(s.format(**kwargs))

    def declare_local_variable(self, type, name, init=None):
        if init is not None:
            self.putf('cdef {type} {name} = {init}', type=type, name=name, init=init)
        else:
            self.putf('cdef {type} {name}', type=type, name=name)

    def for_loop(self, idx, upper):
        self.putf('for {idx} in range({upper}):', idx=idx, upper=upper)
        self.indent()

    def end_loop(self):
        self.dedent()

    def result(self):
        return '\n'.join(self._lines)


class AsmGenerator:
    def __init__(self, classname, code, dim, numderiv=0, vec=False):
        self.classname = classname
        self.code = code
        self.dim = dim
        self.vec = vec
        self.numderiv = numderiv
        self.vars = OrderedDict()
        self.assignments = []   # assignment statements for vars used in expr
        self.exprs = []         # expressions to be added to the result
        # variables provided during initialization (geo_XXX)
        self.init_det = False
        self.init_jac = False
        self.init_jacinv = False
        self.init_weights = False       # geo_weights = Gauss weights * abs(geo_det)
        # variables provided pointwise during assembling
        self.need_val = False           # basis function values (u, v)
        self.need_grad = False          # gradient in parameter domain (gu, gv)
        self.need_phys_grad = False     # gradient in physical domain (gradu, gradv)
        self.need_phys_weights = False  # W = Gauss weight * abs(geo_det)
        # misc
        self._JacInvT = None

    def register_scalar_field(self, name):
        self.vars[name] = {
            'type': 'scalar',
            'numdims': 0
        }

    def register_vector_field(self, name):
        self.vars[name] = {
            'type': 'vector',
            'numdims': 1
        }

    def register_matrix_field(self, name, symmetric=False):
        self.vars[name] = {
            'type': 'matrix',
            'numdims': 2,
            'symmetric': symmetric
        }
        return LiteralMatrixExpr([
            [self.mat_entry(name, i, j) for j in range(self.dim)]
            for i in range(self.dim)
        ])

    def indent(self, num=1):
        self.code.indent(num)

    def dedent(self, num=1):
        self.code.dedent(num)

    def put(self, s):
        self.code.put(s)

    def putf(self, s, **kwargs):
        env = dict(self.env)
        env.update(kwargs)
        self.code.putf(s, **env)

    def add(self, expr):
        assert not self.vec, 'add() is only for scalar assemblers; use add_at()'
        assert not expr.vecsize, 'cannot add vector to scalar result'
        self.exprs.append(expr)

    def add_at(self, idx, expr):
        assert self.vec, 'add_at() is only for vector assemblers; use add()'
        assert not expr.vecsize, 'cannot add vector to scalar result'
        self.exprs.append((idx, expr))

    def dimrep(self, s, sep=', '):
        return sep.join([s.format(k, **self.env) for k in range(self.dim)])

    def extend_dim(self, i):
        # ex.: i = 1  ->  'None,:,None'
        slices = self.dim * ['None']
        slices[i] = ':'
        return ','.join(slices)

    def tensorprod(self, var):
        return ' * '.join(['{0}[{1}][{2}]'.format(var, k, self.extend_dim(k))
            for k in range(self.dim)])

    def gen_pderiv(self, var, D, idx='i'):
        """Generate code for computing parametric partial derivative of `var` of order `D=(Dx1, ..., Dxd)`"""
        D = tuple(reversed(D))  # x is last axis
        assert len(D) == self.dim
        assert all(0 <= d <= self.numderiv for d in D)
        factors = [
                "VD{var}{k}[{nderiv}*{idx}{k}+{ofs}]".format(
                    var = var,
                    idx = idx,
                    k   = k,
                    ofs = D[k],
                    **self.env
                )
                for k in range(self.dim)]
        return '(' + ' * '.join(factors) + ')'

    def partial_deriv(self, var, D):
        """Parametric partial derivative of `var` of order `D=(Dx1, ..., Dxd)`"""
        return PartialDerivExpr(var, D, self)

    def basisval(self, var):
        return self.partial_deriv(var, self.dim * (0,))

    def gradient(self, var, dims=None, additional_derivs=None):
        if dims is None:
            dims = range(self.dim)
        if additional_derivs is None:
            additional_derivs = self.dim * [0]

        entries = []
        for k in dims:
            D = list(additional_derivs)
            D[k] += 1
            entries.append(self.partial_deriv(var, D))
        return LiteralVectorExpr(entries)

    def declare_index(self, name, init=None):
        self.code.declare_local_variable('size_t', name, init)

    def declare_scalar(self, name, init=None):
        self.code.declare_local_variable('double', name, init)

    def declare_pointer(self, name, init=None):
        self.code.declare_local_variable('double*', name, init)

    def declare_vec(self, name, size=None):
        if size is None:
            size = self.dim
        self.putf('cdef double {name}[{size}]', name=name, size=size)

    def mat_entry(self, name, i, j):
        sym = self.vars[name]['symmetric']
        if sym and i > j:
            i, j = j, i
        idx = i*self.dim + j
        return LiteralExpr('{name}[{idx}]'.format(name=name, idx=idx))

    def vec_entry(self, name, i):
        return '{name}[{i}]'.format(name=name, i=i)

    def gen_assign(self, out, expr):
        if expr.vecsize:
            for k in range(expr.vecsize):
                self.put(self.vec_entry(out, k) + ' = ' + expr[k].gencode())
        elif expr.matsize:
            assert False, 'matrix assignment not implemented'
        else:
            self.put(out + ' = ' + expr.gencode())

    def let(self, varname, expr):
        #self.assignments.append((varname, expr))
        self.gen_assign(varname, expr)
        return LiteralExpr(varname, vecsize=expr.vecsize)

    def add_matvecvec(self, A, x, y):
        Ax = matmul(A, x)
        for k in range(self.dim):
            self.add(Ax[k] * y[k])

    def generate_kernel(self):
        # function definition
        self.put('@cython.boundscheck(False)')
        self.put('@cython.wraparound(False)')
        self.put('@cython.initializedcheck(False)')
        self.put('@staticmethod')
        rettype = 'void' if self.vec else 'double'
        self.putf('cdef {rettype} combine(', rettype=rettype)
        self.indent(2)

        # parameters
        for name, var in self.vars.items():
            self.putf('double[{X}:1] _{name},',
                    X=', '.join((self.dim + var['numdims']) * ':'),
                    name=name)

        self.put(self.dimrep('double* VDu{}') + ',')
        self.put(self.dimrep('double* VDv{}') + ',')
        if self.vec:    # for vector assemblers, result is passed as a pointer
            self.put('double result[]')
        self.dedent()
        self.put(') nogil:')

        # get input size from an arbitrary field variable
        first_var = list(self.vars)[0]
        for k in range(self.dim):
            self.declare_index(
                    'n%d' % k,
                    '_{var}.shape[{k}]'.format(k=k, var=first_var)
            )
        self.put('')

        # local variables
        for k in range(self.dim):
            self.declare_index('i%d' % k)

        if self.need_val:
            self.declare_scalar('u')
            self.declare_scalar('v')
        if self.need_grad:
            self.declare_vec('gu')
            self.declare_vec('gv')
        if self.need_phys_grad:
            self.declare_vec('gradu')
            self.declare_vec('gradv')

        if not self.vec:    # for vector assemblers, result is passed as a pointer
            self.declare_scalar('result', '0.0')

        # temp storage for field variables
        for name, var in self.vars.items():
            if var['type'] == 'scalar':
                self.declare_scalar(name)
            elif var['type'] == 'matrix':
                self.declare_pointer(name)

        self.declare_temp_variables()

        self.put('')

        # main loop over all Gauss points
        for k in range(self.dim):
            self.code.for_loop('i%d' % k, 'n%d' % k)

        self.env['I'] = self.dimrep('i{}')

        # generate assignments for field variables
        for name, var in self.vars.items():
            if var['type'] == 'scalar':
                self.putf('{name} = _{name}[{I}]', name=name)
            elif var['type'] == 'matrix':
                self.putf('{name} = &_{name}[{I}, 0, 0]', name=name)
        self.put('')

        if self.need_val:
            self.gen_assign('u', self.basisval('u'))
            self.gen_assign('v', self.basisval('v'))
            self.put('')
        if self.need_grad:
            self.gen_assign('gu', self.gradient('u'))
            self.put('')
            self.gen_assign('gv', self.gradient('v'))
            self.put('')
            if self.need_phys_grad:
                self.gen_assign('gradu', matmul(self.JacInvT, self.gu))
                self.put('')
                self.gen_assign('gradv', matmul(self.JacInvT, self.gv))
                self.put('')

        # generate deferred assignment statements
        for varname, expr in self.assignments:
            self.gen_assign(varname, expr)
        self.assignments = None     # disallow further assignments

        # if not yet done, compute expressions for the bilinear form a(u,v)
        self.generate_biform()

        # generate code for all expressions in the bilinear form
        if self.vec:
            for idx, expr in self.exprs:
                self.put(('result[%d] += ' % idx) + expr.gencode())
        else:
            for expr in self.exprs:
                self.put('result += ' + expr.gencode())

        # end main loop
        for _ in range(self.dim):
            self.code.end_loop()

        if not self.vec:
            self.put('return result')
        self.dedent()

    def generate_assemble_impl(self):
        src = gen_assemble_impl_header(dim=self.dim, vec=self.vec)
        for line in src.splitlines():
            self.put(line)

        self.indent(1)

        self.put('')
        if self.vec:
            self.putf('{classname}{dim}D.combine(', classname=self.classname)
        else:
            self.putf('return {classname}{dim}D.combine(', classname=self.classname)
        self.indent(2)
        for name in self.vars:
            self.putf('self.{name} [ {idx} ],', name=name,
                    idx=self.dimrep('g_sta[{0}]:g_end[{0}]'))
        # a_ij = a(phi_j, phi_i)  -- pass values for j first
        self.put(self.dimrep('values_j[{0}]') + ',')
        self.put(self.dimrep('values_i[{0}]') + ',')
        if self.vec:
            self.put('result')
        self.dedent(2)
        self.put(')')
        self.dedent()

    def generate_init(self):
        self.put('def __init__(self, kvs, geo):')
        self.indent()
        for line in \
"""assert geo.dim == {dim}, "Geometry has wrong dimension"
self.base_init(kvs)

gaussgrid, gaussweights = make_tensor_quadrature([kv.mesh for kv in kvs], self.nqp)

self.C = compute_values_derivs(kvs, gaussgrid, derivs={maxderiv})""".splitlines():
            self.putf(line)
        self.put('')

        # dependencies
        if self.init_weights: self.init_det = True
        if self.init_jacinv or self.init_det: self.init_jac = True

        # compute Jacobian
        if self.init_jac:
            self.put('geo_jac = geo.grid_jacobian(gaussgrid)')

        # determinant and/or inverse of Jacobian
        if self.init_det and self.init_jacinv:
            self.put('geo_det, geo_jacinv = det_and_inv(geo_jac)')
        elif self.init_det:
            self.put('geo_det = determinants(geo_jac)')
        elif self.init_jacinv:
            self.put('geo_jacinv = inverses(geo_jac)')

        if self.init_weights:
            self.put('geo_weights = %s * np.abs(geo_det)' % self.tensorprod('gaussweights'))

        if self.need_phys_grad:
            self.put("self.JacInvT = np.asarray(np.swapaxes(geo_jacinv, -2, -1), order='C')")

        if self.need_phys_weights:  # store weights as field variable
            self.put('self.W = geo_weights')

        self.initialize_fields()
        self.put('')
        self.dedent()

    def declare_temp_variables(self):
        pass

    def initialize_fields(self):
        pass

    def generate_biform(self):
        pass

    @property
    def W(self):
        self.need_phys_weights = True
        return LiteralExpr('W')

    @property
    def u(self):
        self.need_val = True
        return LiteralExpr('u')

    @property
    def v(self):
        self.need_val = True
        return LiteralExpr('v')

    @property
    def gu(self):
        self.need_grad = True
        return LiteralExpr('gu', vecsize=self.dim)

    @property
    def gv(self):
        self.need_grad = True
        return LiteralExpr('gv', vecsize=self.dim)

    @property
    def gradu(self):
        self.need_phys_grad = True
        return LiteralExpr('gradu', vecsize=self.dim)

    @property
    def gradv(self):
        self.need_phys_grad = True
        return LiteralExpr('gradv', vecsize=self.dim)

    @property
    def JacInvT(self):
        if not self._JacInvT:
            self.need_phys_grad = True      # TODO: may not need this, only JacInvT itself!
            self._JacInvT = self.register_matrix_field('JacInvT')
        return self._JacInvT

    def generate(self):
        if self.need_phys_grad:
            self._JacInvT = self.register_matrix_field('JacInvT')   # TODO: duplicated code
            self.init_jacinv = True
            self.need_grad = True

        if self.need_phys_weights:
            self.register_scalar_field('W')
            self.init_weights = True

        if self.need_grad:
            self.numderiv = max(self.numderiv, 1) # ensure 1st derivatives

        self.env = {
            'dim': self.dim,
            'nderiv': self.numderiv+1, # includes 0-th derivative
            'maxderiv': self.numderiv,
        }

        baseclass = 'BaseVectorAssembler' if self.vec else 'BaseAssembler'
        self.putf('cdef class {classname}{dim}D({base}{dim}D):',
                classname=self.classname, base=baseclass)
        self.indent()

        # declare instance variables
        self.put('cdef vector[double[:, :, ::1]] C       # 1D basis values. Indices: basis function, mesh point, derivative')
        # declare field variables
        for name, var in self.vars.items():
            self.putf('cdef double[{X}:1] {name}',
                    X=', '.join((self.dim + var['numdims']) * ':'),
                    name=name)
        self.put('')

        # generate methods
        self.generate_init()
        self.generate_kernel()
        self.generate_assemble_impl()
        self.dedent()
        self.put('')


tmpl_generic = Template(r'''
################################################################################
# {{DIM}}D Assemblers
################################################################################

cdef class BaseAssembler{{DIM}}D:
    cdef int nqp
    cdef size_t[{{DIM}}] ndofs
    cdef int[{{DIM}}] p
    cdef vector[ssize_t[:,::1]] meshsupp
    cdef list _asm_pool     # list of shared clones for multithreading

    cdef void base_init(self, kvs):
        assert len(kvs) == {{DIM}}, "Assembler requires two knot vectors"
        self.nqp = max([kv.p for kv in kvs]) + 1
        self.ndofs[:] = [kv.numdofs for kv in kvs]
        self.p[:]     = [kv.p for kv in kvs]
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
cdef object generic_assemble_core_{{DIM}}d(BaseAssembler{{DIM}}D asm, bidx, bint symmetric=False):
    cdef unsigned[:, ::1] {{ dimrepeat('bidx{}') }}
    cdef long {{ dimrepeat('mu{}') }}, {{ dimrepeat('MU{}') }}
    cdef double[{{ dimrepeat(':') }}:1] entries

    {{ dimrepeat('bidx{}') }} = bidx
    {{ dimrepeat('MU{}') }} = {{ dimrepeat('bidx{}.shape[0]') }}

    cdef size_t[::1] {{ dimrepeat('transp{}') }}
    if symmetric:
    {%- for k in range(DIM) %}
        transp{{k}} = get_transpose_idx_for_bidx(bidx{{k}})
    {%- endfor %}
    else:
        {{ dimrepeat('transp{}', sep=' = ') }} = None

    entries = np.zeros(({{ dimrepeat('MU{}') }}))

    cdef int num_threads = pyiga.get_max_threads()

    for mu0 in prange(MU0, num_threads=num_threads, nogil=True):
        _asm_core_{{DIM}}d_kernel(asm, symmetric,
            {{ dimrepeat('bidx{}') }},
            {{ dimrepeat('transp{}') }},
            entries,
            mu0)
    return entries

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.initializedcheck(False)
cdef void _asm_core_{{DIM}}d_kernel(
    BaseAssembler{{DIM}}D asm,
    bint symmetric,
    {{ dimrepeat('unsigned[:, ::1] bidx{}') }},
    {{ dimrepeat('size_t[::1] transp{}') }},
    double[{{ dimrepeat(':') }}:1] entries,
    long _mu0
) nogil:
    cdef size_t[{{DIM}}] i, j
    cdef int {{ dimrepeat('diag{}') }}
    cdef double entry
    cdef long {{ dimrepeat('mu{}') }}, {{ dimrepeat('MU{}') }}

    mu0 = _mu0
    {{ dimrepeat('MU{}') }} = {{ dimrepeat('bidx{}.shape[0]') }}

    i[0] = bidx0[mu0, 0]
    j[0] = bidx0[mu0, 1]

    if symmetric:
        diag0 = <int>j[0] - <int>i[0]
        if diag0 > 0:       # block is above diagonal?
            return
{% for k in range(1, DIM) %}
{{ indent(k)   }}for mu{{k}} in range(MU{{k}}):
{{ indent(k)   }}    i[{{k}}] = bidx{{k}}[mu{{k}}, 0]
{{ indent(k)   }}    j[{{k}}] = bidx{{k}}[mu{{k}}, 1]

{{ indent(k)   }}    if symmetric:
{{ indent(k)   }}        diag{{k}} = <int>j[{{k}}] - <int>i[{{k}}]
{{ indent(k)   }}        if {{ dimrepeat('diag{} == 0', sep=' and ', upper=k) }} and diag{{k}} > 0:
{{ indent(k)   }}            continue
{% endfor %}
{{ indent(DIM) }}entry = asm.assemble_impl(i, j)
{{ indent(DIM) }}entries[{{ dimrepeat('mu{}') }}] = entry

{{ indent(DIM) }}if symmetric:
{{ indent(DIM) }}    if {{ dimrepeat('diag{} != 0', sep=' or ') }}:     # are we off the diagonal?
{{ indent(DIM) }}        entries[ {{ dimrepeat('transp{0}[mu{0}]') }} ] = entry   # then also write into the transposed entry


cdef generic_assemble_{{DIM}}d_parallel(BaseAssembler{{DIM}}D asm, symmetric=False):
    mlb = MLBandedMatrix(
        tuple(asm.ndofs),
        tuple(asm.p)
    )
    X = generic_assemble_core_{{DIM}}d(asm, mlb.bidx, symmetric=symmetric)
    mlb.data = X
    return mlb.asmatrix()


# helper function for fast low-rank assembler
cdef double _entry_func_{{DIM}}d(size_t i, size_t j, void * data):
    return (<BaseAssembler{{DIM}}D>data).assemble(i, j)



cdef class BaseVectorAssembler{{DIM}}D:
    cdef int nqp
    cdef size_t[{{DIM}}] ndofs
    cdef vector[ssize_t[:,::1]] meshsupp

    cdef void base_init(self, kvs):
        assert len(kvs) == {{DIM}}, "Assembler requires two knot vectors"
        self.nqp = max([kv.p for kv in kvs]) + 1
        self.ndofs[:] = [kv.numdofs for kv in kvs]
        self.meshsupp = [kvs[k].mesh_support_idx_all() for k in range({{DIM}})]

    cdef BaseAssembler{{DIM}}D shared_clone(self):
        return self     # by default assume thread safety

    cdef inline size_t to_seq(self, size_t[{{DIM + 1}}] ii) nogil:
        return {{ to_seq(indices + [ 'ii[%d]' % DIM ], ndofs + [ DIM|string ]) }}

    @cython.boundscheck(False)
    @cython.wraparound(False)
    @cython.cdivision(True)
    cdef inline void from_seq(self, size_t i, size_t[{{DIM + 1}}] out) nogil:
        out[{{DIM}}] = i % {{DIM}}
        i /= {{DIM}}
        {%- for k in range(1, DIM)|reverse %}
        out[{{k}}] = i % self.ndofs[{{k}}]
        i /= self.ndofs[{{k}}]
        {%- endfor %}
        out[0] = i

    cdef void assemble_impl(self, size_t[{{DIM}}] i, size_t[{{DIM}}] j, double result[]) nogil:
        pass


@cython.boundscheck(False)
@cython.wraparound(False)
cdef object generic_assemble_core_vec_{{DIM}}d(BaseVectorAssembler{{DIM}}D asm, bidx, bint symmetric=False):
    cdef unsigned[:, ::1] {{ dimrepeat('bidx{}') }}
    cdef long {{ dimrepeat('mu{}') }}, {{ dimrepeat('MU{}') }}
    cdef double[{{ dimrepeat(':') }}, ::1] entries

    {{ dimrepeat('bidx{}') }} = bidx
    {{ dimrepeat('MU{}') }} = {{ dimrepeat('bidx{}.shape[0]') }}

    cdef size_t[::1] {{ dimrepeat('transp{}') }}
    if symmetric:
    {%- for k in range(DIM) %}
        transp{{k}} = get_transpose_idx_for_bidx(bidx{{k}})
    {%- endfor %}
    else:
        {{ dimrepeat('transp{}', sep=' = ') }} = None

    entries = np.zeros(({{ dimrepeat('MU{}') }}, {{DIM * DIM}}))

    cdef int num_threads = pyiga.get_max_threads()

    for mu0 in prange(MU0, num_threads=num_threads, nogil=True):
        _asm_core_vec_{{DIM}}d_kernel(asm, symmetric,
            {{ dimrepeat('bidx{}') }},
            {{ dimrepeat('transp{}') }},
            entries,
            mu0)
    return entries

@cython.boundscheck(False)
@cython.wraparound(False)
@cython.initializedcheck(False)
cdef void _asm_core_vec_{{DIM}}d_kernel(
    BaseVectorAssembler{{DIM}}D asm,
    bint symmetric,
    {{ dimrepeat('unsigned[:, ::1] bidx{}') }},
    {{ dimrepeat('size_t[::1] transp{}') }},
    double[{{ dimrepeat(':') }}, ::1] entries,
    long _mu0
) nogil:
    cdef size_t[{{DIM}}] i, j
    cdef int {{ dimrepeat('diag{}') }}
    cdef long {{ dimrepeat('mu{}') }}, {{ dimrepeat('MU{}') }}
    cdef int row, col

    mu0 = _mu0
    {{ dimrepeat('MU{}') }} = {{ dimrepeat('bidx{}.shape[0]') }}

    i[0] = bidx0[mu0, 0]
    j[0] = bidx0[mu0, 1]

    if symmetric:
        diag0 = <int>j[0] - <int>i[0]
        if diag0 > 0:       # block is above diagonal?
            return
{% for k in range(1, DIM) %}
{{ indent(k)   }}for mu{{k}} in range(MU{{k}}):
{{ indent(k)   }}    i[{{k}}] = bidx{{k}}[mu{{k}}, 0]
{{ indent(k)   }}    j[{{k}}] = bidx{{k}}[mu{{k}}, 1]

{{ indent(k)   }}    if symmetric:
{{ indent(k)   }}        diag{{k}} = <int>j[{{k}}] - <int>i[{{k}}]
{{ indent(k)   }}        if {{ dimrepeat('diag{} == 0', sep=' and ', upper=k) }} and diag{{k}} > 0:
{{ indent(k)   }}            continue
{% endfor %}
{{ indent(DIM) }}asm.assemble_impl(i, j, &entries[ {{ dimrepeat('mu{}') }}, 0 ])

{{ indent(DIM) }}if symmetric:
{{ indent(DIM) }}    if {{ dimrepeat('diag{} != 0', sep=' or ') }}:     # are we off the diagonal?
{{ indent(DIM) }}        for row in range({{DIM}}):
{{ indent(DIM) }}            for col in range({{DIM}}):
{{ indent(DIM) }}                entries[{{ dimrepeat('transp{0}[mu{0}]') }}, col*{{DIM}} + row] = entries[{{ dimrepeat('mu{}') }}, row*{{DIM}} + col]

''')

def gen_assemble_impl_header(dim, vec=False):
    if vec:
        funcdecl = 'cdef void assemble_impl(self, size_t[{dim}] i, size_t[{dim}] j, double result[]) nogil:'.format(dim=dim)
    else:
        funcdecl = 'cdef double assemble_impl(self, size_t[{dim}] i, size_t[{dim}] j) nogil:'.format(dim=dim)
    zeroret = '' if vec else '0.0'  # for vector assemblers, result[] is 0-initialized

    return r"""
@cython.boundscheck(False)
@cython.wraparound(False)
@cython.initializedcheck(False)
{funcdecl}
    cdef int k
    cdef IntInterval intv
    cdef size_t g_sta[{dim}]
    cdef size_t g_end[{dim}]
    cdef (double*) values_i[{dim}]
    cdef (double*) values_j[{dim}]

    for k in range({dim}):
        intv = intersect_intervals(make_intv(self.meshsupp[k][i[k],0], self.meshsupp[k][i[k],1]),
                                   make_intv(self.meshsupp[k][j[k],0], self.meshsupp[k][j[k],1]))
        if intv.a >= intv.b:
            return {zeroret}      # no intersection of support
        g_sta[k] = self.nqp * intv.a    # start of Gauss nodes
        g_end[k] = self.nqp * intv.b    # end of Gauss nodes

        values_i[k] = &self.C[k][ i[k], g_sta[k], 0 ]
        values_j[k] = &self.C[k][ j[k], g_sta[k], 0 ]
""".format(
        dim=dim,
        funcdecl=funcdecl,
        zeroret=zeroret
    )

class Expr:
    def __add__(self, other): return OperExpr('+', self, other)
    def __sub__(self, other): return OperExpr('-', self, other)
    def __mul__(self, other): return OperExpr('*', self, other)
    def __div__(self, other): return OperExpr('/', self, other)
    def __getitem__(self, i):
        if isinstance(i, slice) or isinstance(i, range):
            return SliceExpr(self, i)
        else:
            return IndexExpr(self, i)

class LiteralExpr(Expr):
    def __init__(self, s, vecsize=None):
        self.s = s
        self.vecsize = vecsize
        self.matsize = None

    def gencode(self):
        return self.s

class LiteralVectorExpr(Expr):
    def __init__(self, entries):
        self.vecsize = len(entries)
        self.matsize = None
        self.entries = entries

    def __getitem__(self, i):
        if i < 0: i += self.vecsize
        assert 0 <= i < self.vecsize, 'index out of bounds'
        return self.entries[i]

class LiteralMatrixExpr(Expr):
    def __init__(self, entries):
        self.vecsize = None
        self.matsize = len(entries), len(entries[0])
        self.entries = np.array(entries, dtype=object)

    def __getitem__(self, ij):
        if isinstance(ij[0], range) and isinstance(ij[1], range):
            # hackish support for double range slicing
            ij = np.ix_(*ij)
        result = self.entries[ij]
        if isinstance(result, np.ndarray):
            return LiteralMatrixExpr(result)
        else:
            return result

class OperExpr(Expr):
    def __init__(self, oper, x, y):
        assert None == x.vecsize == y.vecsize and \
               None == x.matsize == y.matsize, 'expected scalars'
        self.vecsize = x.vecsize
        self.matsize = x.matsize
        self.oper = oper
        self.args = (x,y)

    def gencode(self):
        sep = ' ' + self.oper + ' '
        return '(' + sep.join(x.gencode() for x in self.args) + ')'

class IndexExpr(Expr):
    def __init__(self, x, i):
        self.vecsize = self.matsize = None
        assert x.vecsize, 'indexed expression is not a vector'
        self.x = x
        i = int(i)
        if i < 0: i += x.vecsize
        assert 0 <= i < x.vecsize, 'index out of range'
        self.i = i

    def gencode(self):
        return '{x}[{i}]'.format(x=self.x.gencode(), i=self.i)

class PartialDerivExpr(Expr):
    def __init__(self, var, D, asmgen):
        self.vecsize = self.matsize = None
        self.var = var
        self.D = D
        self.asmgen = asmgen

    def gencode(self):
        return self.asmgen.gen_pderiv(self.var, self.D)

def _indices_from_slice(sl, n):
    start = sl.start
    if start is None: start = 0
    if start < 0: start += n
    stop = sl.stop
    if stop is None: stop = n
    if stop < 0: stop += n
    step = sl.step
    if step is None: step = 1
    return tuple(range(start, stop, step))

class SliceExpr(Expr):
    def __init__(self, x, sl):
        assert x.vecsize, 'expression is not a vector'
        self.x = x
        if isinstance(sl, slice):
            self.indices = _indices_from_slice(sl, x.vecsize)
        else:   # range?
            self.indices = tuple(sl)
        for i in self.indices:
            assert 0 <= i < x.vecsize, 'slice index out of range'
        self.vecsize = len(self.indices)
        self.matsize = None

    def __getitem__(self, i):
        assert not isinstance(i, slice), 'slicing of slices not implemented'
        return self.x[self.indices[i]]

class MatMulExpr(Expr):
    def __init__(self, A, x):
        assert A.matsize[1] == x.vecsize, 'incompatible shapes'
        self.vecsize = A.matsize[0]
        self.matsize = None
        self.A = A
        self.x = x

    def __getitem__(self, i):
        if i < 0: i += self.vecsize
        assert 0 <= i < self.vecsize
        factors = [self.A[i, j] * self.x[j] for j in range(self.x.vecsize)]
        return reduce(operator.add, factors)

def inner(x, y):
    assert x.vecsize and y.vecsize, 'inner() requires vector expressions'
    assert x.vecsize == y.vecsize, 'incompatible sizes'
    return reduce(operator.add, (x[i] * y[i] for i in range(x.vecsize)))

def matmul(A, x):
    return MatMulExpr(A, x)

class MassAsmGen(AsmGenerator):
    def __init__(self, code, dim):
        AsmGenerator.__init__(self, 'MassAssembler', code, dim)
        self.add(self.W * self.u * self.v)


class StiffnessAsmGen(AsmGenerator):
    def __init__(self, code, dim):
        AsmGenerator.__init__(self, 'StiffnessAssembler', code, dim)
        self.init_jacinv = self.init_weights = True

        B = self.register_matrix_field('B', symmetric=True)
        self.add_matvecvec(B, self.gu, self.gv)

    def initialize_fields(self):
        self.putf('self.B = matmatT(geo_jacinv) * geo_weights[{slices}, None, None]',
            slices=self.dimrep(':'))


class Heat_ST_AsmGen(AsmGenerator):
    def __init__(self, code, dim):
        AsmGenerator.__init__(self, 'Heat_ST_Assembler', code, dim)
        timederiv = self.gradu[-1] * self.v
        gradgrad = inner(self.gradu[:-1], self.gradv[:-1])
        self.add(self.W * (gradgrad + timederiv))


class Wave_ST_AsmGen(AsmGenerator):
    def __init__(self, code, dim):
        AsmGenerator.__init__(self, 'WaveAssembler_ST', code, dim, numderiv=2)
        self.need_phys_grad = True
        self.need_phys_weights = True

    def declare_temp_variables(self):
        self.declare_vec('dtgv', size=self.dim-1)
        self.declare_vec('dtgradv', size=self.dim-1)

    def generate_biform(self):
        Dt = (self.dim-1) * (0,) + (1,)
        utt_vt = self.partial_deriv('u', Dt[:-1] + (2,)) * self.partial_deriv('v', Dt)

        spacedims, timedim = range(self.dim-1), self.dim-1
        dtgv = self.let('dtgv', self.gradient('v',
                dims=spacedims,
                additional_derivs=Dt    # additional derivative in time direction
        ))

        dtgradv = self.let('dtgradv', matmul(self.JacInvT[spacedims, spacedims], dtgv))
        gradu_dtgradv = inner(self.gradu[:-1], dtgradv)

        self.add(self.W * (utt_vt + gradu_dtgradv))


class DivDivAsmGen(AsmGenerator):
    def __init__(self, code, dim):
        AsmGenerator.__init__(self, 'DivDivAssembler', code, dim, vec=dim**2)
        for i in range(self.dim):
            for j in range(self.dim):
                self.add_at(self.dim*i + j, self.W * self.gradu[j] * self.gradv[i])


def generate(dim):
    DIM = dim

    def dimrepeat(s, sep=', ', upper=DIM):
        return sep.join([s.format(k) for k in range(upper)])

    def to_seq(i, n):
        s = i[0]
        for k in range(1, len(i)):
            s = '({0}) * {1} + {2}'.format(s, n[k], i[k])
        return s

    def indent(num):
        return num * '    ';

    # helper variables for to_seq
    indices = ['ii[%d]' % k for k in range(DIM)]
    ndofs   = ['self.ndofs[%d]' % k for k in range(DIM)]

    code = PyCode()
    MassAsmGen(code, DIM).generate()
    StiffnessAsmGen(code, DIM).generate()
    Heat_ST_AsmGen(code, DIM).generate()
    Wave_ST_AsmGen(code, DIM).generate()
    DivDivAsmGen(code, DIM).generate()

    s = tmpl_generic.render(locals())
    s += code.result()
    return s

if __name__ == '__main__':
    path = os.path.join(os.path.dirname(__file__), "..", "pyiga", "assemblers.pxi")
    with open(path, 'w') as f:
        f.write('# file generated by generate-assemblers.py\n')
        f.write(generate(dim=2))
        f.write(generate(dim=3))

