"""
Copyright 2015 Enzo Busseti, 2017 Robin Verschueren

This file is part of CVXPY.

CVXPY is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

CVXPY is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with CVXPY.  If not, see <http://www.gnu.org/licenses/>.
"""

import numpy as np
import scipy as sp
import cvxpy.settings as s
from cvxpy.constraints import PSD, SOC, NonPos, Zero, ExpCone
from cvxpy.reductions.inverse_data import InverseData
from cvxpy.utilities.coeff_extractor import CoeffExtractor
from cvxpy.reductions.solution import Solution
from .conic_solver import ConicSolver


def vectorized_lower_tri_to_mat(v, dim):
    """
    :param v: a list of length (dim * (dim + 1) / 2)
    :param dim: the number of rows (equivalently, columns) in the output array.
    :return: Return the symmetric 2D array defined by taking "v" to
      specify its lower triangular entries.
    """
    rows, cols, vals = [], [], []
    running_idx = 0
    for j in range(dim):
        rows += [j + k for k in range(dim - j)]
        cols += [j] * (dim - j)
        vals += v[running_idx:(running_idx + dim - j)]
        running_idx += dim - j
    A = sp.sparse.coo_matrix((vals, (rows, cols)), shape=(dim, dim)).toarray()
    d = np.diag(np.diag(A))
    A = A + A.T - d
    return A


def psd_coeff_offset(problem, c):
    """
    Returns an array "A" and vector "b" such that the given constraint is
      equivalent to "A * x <=_{PSD} b".

    :param problem: the cvxpy Problem in which "c" arises.
    :param c: a cvxpy Constraint defining a linear matrix inequality
      "B + \sum_j A[j] * x[j] >=_{PSD} 0".
    :return: (A, b) such that "c" holds at "x" iff "A * x <=_{PSD} b"
      (where the PSD cone is reshaped into a subset of R^N with N = dim ** 2).

    Note: It is desirable to change this mosek interface so that PSD constraints
    are represented by a vector in R^N with N = (dim * (dim + 1) / 2).
    This is possible because arguments to a linear matrix inequality
    are necessarily symmetric. For now we use N = dim ** 2,
    because it simplifies implementation and only
    makes a modest difference in the size of the problem seen by mosek.
    """
    extractor = CoeffExtractor(InverseData(problem))
    A_vec, b_vec = extractor.affine(c.expr)
    A = -A_vec
    b = b_vec
    dim = c.expr.shape[0]
    return A, b, dim


class MOSEK(ConicSolver):
    """An interface for the Mosek solver.
    """
    # Solver capabilities.
    #   Note that "SUPPORTED_CONSTRAINTS" does not include the exponential cone by default.
    #   CVXPY will check for exponential cone support when "import_solver( ... )"
    # or "accepts( ... )" is called.
    MIP_CAPABLE = True
    SUPPORTED_CONSTRAINTS = ConicSolver.SUPPORTED_CONSTRAINTS + [SOC, PSD]
    EXP_CONE_ORDER = [2, 1, 0]
    """
    The cvxpy standard for the exponential cone is:
        K_e = closure{(x,y,z) |  y >= z * exp(x/z), z>0}.
    Whenever a solver uses this convention, EXP_CONE_ORDER should be [0, 1, 2].

    MOSEK uses the convention:
        K_e = closure{(x,y,z) | x >= y * exp(z/y), x,y >= 0}.
    with this convention, EXP_CONE_ORDER should be [1, 2, 0]... right?

    Well for whatever reason, NO. After trying all 6 possible values for "EXP_CONE_ORDER",
    the only one that passes our units tests is EXP_CONE_ORDER = [2, 1, 0]. However "hackish",
    trying all 6 possibilities during development is really not a problem.
    We recommend doing the same to add exponential cone support for other solvers.
    """

    def import_solver(self):
        """Imports the solver (updates the set of supported constraints, if applicable).
        """
        import mosek
        mosek  # For flake8
        if hasattr(mosek.conetype, 'pexp') and ExpCone not in MOSEK.SUPPORTED_CONSTRAINTS:
            MOSEK.SUPPORTED_CONSTRAINTS.append(ExpCone)

    def name(self):
        """The name of the solver.
        """
        return s.MOSEK

    def accepts(self, problem):
        """Can Mosek solve the problem?
        """
        # TODO check if is matrix stuffed.
        self.import_solver()
        if not problem.objective.args[0].is_affine():
            return False
        for constr in problem.constraints:
            if type(constr) not in MOSEK.SUPPORTED_CONSTRAINTS:
                return False
            for arg in constr.args:
                if not arg.is_affine():
                    return False
        return True

    def block_format(self, problem, constraints, exp_cone_order=None):
        """
        :param problem: the cvxpy Problem we are preparing for mosek.
        :param constraints: a list of Constraint objects for which coefficient
          and offset data ("A", "b" respectively) is needed.
        :param exp_cone_order: a parameter that is only used when a Constraint
           object describes membership in the exponential cone.

        :return: a large matrix "coeff" and a vector of constants "offset" such
          that every Constraint in "constraints" holds at x \in R^n iff
          "coeff * x <=_K offset", where K is a product of cones supported by mosek
          (the zero cone, the nonnegative orthant, the second order cone,
          and the exponential cone). The nature of K is inferred
          later by accessing the data in "lengths" and "ids".

        Notes:

            (1) In practice, this is only called with one type of constraint at a time
            (i.e. all linear equations, or all exponential cone membership constraints,
            all linear inequalities, etc...).

            (2) This function cannot be used with linear matrix inequalities.
            It will throw an error if any Constraint
            c \in constraints defines an LMI.
        """
        if not constraints:
            return None, None
        matrices, offsets, lengths, ids = [], [], [], []
        for con in constraints:
            coeff, offset = self.format_constr(problem, con, exp_cone_order)
            matrices.append(coeff)
            offsets.append(offset)
            lengths.append(offset.size)
            ids.append(con.id)
        coeff = sp.sparse.vstack(matrices).tocsc()
        offset = np.hstack(offsets)
        return coeff, offset, lengths, ids

    def apply(self, problem):
        """Returns a new problem and data for inverting the new solution.

        Returns
        -------
        tuple
            (dict of arguments needed for the solver, inverse data)
        """
        data = dict()
        inv_data = {self.VAR_ID: problem.variables()[0].id,
                    'suc_slacks': [], 'y_slacks': [], 'snx_slacks': [], 'psd_dims': []}

        # Get integrality constraint information
        var = problem.variables()[0]
        data[s.BOOL_IDX] = [int(t[0]) for t in var.boolean_idx]
        data[s.INT_IDX] = [int(t[0]) for t in var.integer_idx]
        inv_data['integer_variables'] = len(data[s.BOOL_IDX]) + len(data[s.INT_IDX]) > 0

        # Parse the coefficient vector from the objective.
        c, constant = self.get_coeff_offset(problem.objective.args[0])
        data[s.C] = c.ravel()
        inv_data['n0'] = len(data[s.C])
        data[s.OBJ_OFFSET] = constant[0]
        data[s.DIMS] = {s.SOC_DIM: [], s.EXP_DIM: [], s.PSD_DIM: [], s.LEQ_DIM: 0, s.EQ_DIM: 0}
        inv_data[s.OBJ_OFFSET] = constant[0]
        As = list()
        bs = list()

        # Linear inequalities
        leq_constr = [ci for ci in problem.constraints if type(ci) == NonPos]
        if len(leq_constr) > 0:
            A, b, lengths, ids = self.block_format(problem, leq_constr)  # A, b : A * x <= b
            inv_data['suc_slacks'] += [(ids[k], lengths[k]) for k in range(len(lengths))]
            data[s.DIMS][s.LEQ_DIM] = sum(lengths)
            As.append(A)
            bs.append(b)

        # Linear equations
        eq_constr = [ci for ci in problem.constraints if type(ci) == Zero]
        if len(eq_constr) > 0:
            A, b, lengths, ids = self.block_format(problem, eq_constr)  # A, b : A * x == b.
            inv_data['y_slacks'] += [(ids[k], lengths[k]) for k in range(len(lengths))]
            data[s.DIMS][s.EQ_DIM] = sum(lengths)
            As.append(A)
            bs.append(b)

        # Second order cone
        soc_constr = [ci for ci in problem.constraints if type(ci) == SOC]
        data[s.DIMS][s.SOC_DIM] = [dim for ci in soc_constr for dim in ci.cone_sizes()]
        if len(soc_constr) > 0:
            A, b, lengths, ids = self.block_format(problem, soc_constr)  # A * x <=_{soc} b.
            inv_data['snx_slacks'] += [(ids[k], lengths[k]) for k in range(len(lengths))]
            As.append(A)
            bs.append(b)

        # Exponential cone
        exp_constr = [ci for ci in problem.constraints if type(ci) == ExpCone]
        if len(exp_constr) > 0:
            # A * x <=_{EXP} b.
            A, b, lengths, ids = self.block_format(problem, exp_constr,
                                                   MOSEK.EXP_CONE_ORDER)
            data[s.DIMS][s.EXP_DIM] = lengths
            inv_data['snx_slacks'] += [(ids[k], lengths[k]) for k in range(len(lengths))]
            As.append(A)
            bs.append(b)

        # PSD constraints
        psd_constr = [ci for ci in problem.constraints if type(ci) == PSD]
        if len(psd_constr) > 0:
            data[s.DIMS][s.PSD_DIM] = list()
            for c in psd_constr:
                A_vec, b_vec, dim = psd_coeff_offset(problem, c)
                inv_data['psd_dims'].append((c.id, dim))
                data[s.DIMS][s.PSD_DIM].append(dim)
                As.append(A_vec)
                bs.append(b_vec)

        data[s.G] = sp.sparse.vstack(tuple(As))
        data[s.H] = np.hstack(tuple(bs))

        return data, inv_data

    def solve_via_data(self, data, warm_start, verbose, solver_opts, solver_cache=None):
        import mosek
        with mosek.Env() as env:
            with env.Task(0, 0) as task:
                kwargs = sorted(solver_opts.keys())
                if "mosek_params" in kwargs:
                    self._handle_mosek_params(task, solver_opts["mosek_params"])
                    kwargs.remove("mosek_params")
                if kwargs:
                    raise ValueError("Invalid keyword-argument '%s'" % kwargs[0])

                if verbose:
                    # Define a stream printer to grab output from MOSEK
                    def streamprinter(text):
                        import sys
                        sys.stdout.write(text)
                        sys.stdout.flush()

                    env.set_Stream(mosek.streamtype.log, streamprinter)
                    task.set_Stream(mosek.streamtype.log, streamprinter)
                    task.putintparam(mosek.iparam.infeas_report_auto, mosek.onoffkey.on)

                # Check if the cvxpy standard form has zero variables.
                # If so, return a trivial solution.
                # This is necessary because MOSEK will crash if handed a problem
                # with zero variables.
                if len(data[s.C]) == 0:
                    return {s.STATUS: s.OPTIMAL, s.PRIMAL: [],
                            s.VALUE: data[s.OFFSET], s.EQ_DUAL: [], s.INEQ_DUAL: []}

                # The following lines recover problem parameters, and define helper constants.
                #
                #   The problem's constraint set is "G * z <=_K h."
                # The rows in (G, h) are formatted in order of
                #   (1) linear inequalities, (2) linear equations,
                # (3) soc constraints, (4) exponential cone
                #   constraints, and (5) vectorized linear matrix inequalities.
                # The parameter "dims" indicates the
                #   exact dimensions of each of these cones.
                # The problem's objective is "min c.T * z".
                #
                #   MOSEK's standard form requires that we replace generalized
                # inequalities with slack variables and linear equations.
                # The parameter "n" is the size of the column-vector variable after adding
                #   slacks for SOC and EXP constraints.
                # To be consistent with MOSEK documentation, subsequent comments
                #   refer to this variable as "x".

                G, h = data[s.G], data[s.H]
                dims = data[s.DIMS]
                c = data[s.C]
                n0 = len(c)
                n = n0 + sum(dims[s.SOC_DIM]) + sum(dims[s.EXP_DIM])
                psd_total_dims = sum([el ** 2 for el in dims[s.PSD_DIM]])
                m = len(h)
                num_bool = len(data[s.BOOL_IDX])
                num_int = len(data[s.INT_IDX])

                # Define variables, cone constraints, and integrality constraints.
                #
                #   From a book-keeping perspective, it is best to think of "x" as a block vector.
                # The first block is equal to "z" from "G * z <=_K h",
                # the second block is slacks for SOC constraints, and third block
                #   is slacks for EXP cone constraints.
                # Once we declare "x" in the MOSEK model, we add the necessary
                #   cone membership constraints for slack variables.
                # The last step is to add integrality constraints.
                #
                #   Note that the API call for PSD variables contains the word "bar".
                # MOSEK documentation consistently, uses "bar" as a sort of flag,
                # indicating that a function deals with PSD variables.

                task.appendvars(n)
                task.putvarboundlist(np.arange(n, dtype=int),
                                     [mosek.boundkey.fr] * n, np.zeros(n), np.zeros(n))
                if psd_total_dims > 0:
                    task.appendbarvars(dims[s.PSD_DIM])
                running_idx = n0
                for size_cone in dims[s.SOC_DIM]:
                    task.appendcone(mosek.conetype.quad,
                                    0.0,  # unused
                                    np.arange(running_idx, running_idx + size_cone))
                    running_idx += size_cone
                for k in range(sum(dims[s.EXP_DIM]) // 3):
                    task.appendcone(mosek.conetype.pexp,
                                    0.0,  # unused
                                    np.arange(running_idx, running_idx + 3))
                    running_idx += 3
                if num_bool + num_int > 0:
                    task.putvartypelist(data[s.BOOL_IDX], [mosek.variabletype.type_int] * num_bool)
                    task.putvarboundlist(data[s.BOOL_IDX],
                                         [mosek.boundkey.ra] * num_bool,
                                         [0] * num_bool, [1] * num_bool)
                    task.putvartypelist(data[s.INT_IDX], [mosek.variabletype.type_int] * num_int)

                # Define linear inequality and equality constraints.
                #
                #   Mosek will see a total of m linear expressions,
                #   which must define linear inequalities and
                #   equalities. The variable x contributes to these linear expressions
                #   by standard matrix-vector multiplication;
                #   the matrix in question is referred to as "A" in the mosek documentation.
                #   The PSD variables have a different means of contributing to the
                #   linear expressions.
                #   Specifically, a PSD variable Xj contributes "+tr( \bar{A}_{ij} * Xj )"
                #   to the i-th linear expression, where \bar{A}_{ij} is specified
                #   by a call to putbaraij.
                #
                #   The following code has three phases. (1) Build the matrix A.
                #   (2) Specify the \bar{A}_{ij} for PSD variables.
                #   (3) Specify the right-hand sides of the m linear (in)equalities seen by mosek.
                #
                #   Remark : The parameter G defines the first n0 columns of A.
                #   The remaining columns of A are for SOC and EXP slack variables.
                #   We can actually add all of these slack variables at once
                #   by specifying a giant identity matrix in the appropriate position.

                task.appendcons(m)
                row, col, vals = sp.sparse.find(G)
                task.putaijlist(row.tolist(), col.tolist(), vals.tolist())
                total_soc_exp_slacks = sum(dims[s.SOC_DIM]) + sum(dims[s.EXP_DIM])
                if total_soc_exp_slacks > 0:
                    i = dims[s.LEQ_DIM] + dims[s.EQ_DIM]  # constraint index in {0, ..., m - 1}
                    j = len(c)  # index of the first slack variable in the block vector "x".
                    rows = np.arange(i, i + total_soc_exp_slacks).tolist()
                    cols = np.arange(j, j + total_soc_exp_slacks).tolist()
                    task.putaijlist(rows, cols, [1] * total_soc_exp_slacks)

                # constraint index; start of LMIs.
                i = dims[s.LEQ_DIM] + dims[s.EQ_DIM] + total_soc_exp_slacks
                for j, dim in enumerate(dims[s.PSD_DIM]):  # SDP slack variable "Xj"
                    for row_idx in range(dim):
                        for col_idx in range(dim):
                            val = 1. if row_idx == col_idx else 0.5
                            row = max(row_idx, col_idx)
                            col = min(row_idx, col_idx)
                            mat = task.appendsparsesymmat(dim, [row], [col], [val])
                            task.putbaraij(i, j, [mat], [1.0])
                            i += 1

                num_eq = len(h) - dims[s.LEQ_DIM]
                type_constraint = [mosek.boundkey.up] * dims[s.LEQ_DIM] + \
                                  [mosek.boundkey.fx] * num_eq
                task.putconboundlist(np.arange(m, dtype=int), type_constraint, h, h)

                # Define the objective, and optimize the mosek task.

                task.putclist(np.arange(len(c)), c)
                task.putobjsense(mosek.objsense.minimize)
                task.optimize()

                if verbose:
                    task.solutionsummary(mosek.streamtype.msg)

                return task

    def invert(self, task, inverse_data):
        """
        Use information contained within "task" and "inverse_data" to properly
        define a cvxpy Solution object.

        :param task: the mosek Task object generated during solve_via_data.
        :param inverse_data: data recorded during "apply", in preparation for
          constructing the mosek model.

        :return: a cvxpy Solution object, instantiated with the following fields:

            (1) status - the mosek status code.
            (2) opt_val - the optimal objective function value
            (after translation by a possible constant).
            (3) primal_vars - a dictionary with a single element: "x", represented as a list.
            (4) dual_vars - a dictionary with as many elements as
            constraints in the cvxpy standard form problem.
                The elements of the dictionary are either scalars, or numpy arrays.
        """
        import mosek
        # Map of MOSEK status to CVXPY status.
        # taken from:
        # http://docs.mosek.com/7.0/pythonapi/Solution_status_keys.html
        if inverse_data['integer_variables']:
            sol = mosek.soltype.itg
        else:
            sol = mosek.soltype.itr  # the solution found via interior point method

        STATUS_MAP = {mosek.solsta.optimal: s.OPTIMAL,
                      mosek.solsta.integer_optimal: s.OPTIMAL,
                      mosek.solsta.prim_infeas_cer: s.INFEASIBLE,
                      mosek.solsta.dual_infeas_cer: s.UNBOUNDED,
                      mosek.solsta.near_optimal: s.OPTIMAL_INACCURATE,
                      mosek.solsta.near_integer_optimal: s.OPTIMAL_INACCURATE,
                      mosek.solsta.near_prim_infeas_cer: s.INFEASIBLE_INACCURATE,
                      mosek.solsta.near_dual_infeas_cer: s.UNBOUNDED_INACCURATE,
                      mosek.solsta.unknown: s.SOLVER_ERROR}

        task.getprosta(sol)  # mosek "problem status"; unused.
        solution_status = task.getsolsta(sol)

        status = STATUS_MAP[solution_status]
        if status in s.SOLUTION_PRESENT:

            # get objective value
            opt_val = task.getprimalobj(sol) + inverse_data[s.OBJ_OFFSET]

            # get cvxpy variable value
            x = [0.] * inverse_data['n0']
            task.getxxslice(sol, 0, inverse_data['n0'], x)
            primal_vars = {inverse_data[self.VAR_ID]: x}

            # Dual variables.
            #
            #   A cvxpy "Constraint" object views itself as "affine_expression( vars ) \in K".
            #   As such, the appropriate dual variable from the perspective of
            #   a Constraint object should be in K^*. However, we need to think
            #   about where in the mosek formulation that dual variable can be found.
            #   One can verify that for a given Constraint, the slack in "A * x <=_K b"
            #   is equal to the value "affine_expression( vars )." (This is due
            #   to the implementation of format_constr in ConicSolver,
            #   and format_constr's subsequent use in this class.)
            #   As a result, the appropriate dual variable for a Constraint
            #   represented as "A * x + s == b, s \in K" for the mosek formulation
            #   is the conic dual variable to "s \in K" (rather than the Lagrange
            #   multiplier on the system of linear equations "A * x + s == b").
            #
            #   The methods for getting dual variables in the mosek API are a little strange.
            #   They have no return value, and instead they store the desired result
            #   in the last argument provided to the function in question.

            dual_vars = dict()

            # Dual variables for the inequality constraints
            suc_len = sum([ell for _, ell in inverse_data['suc_slacks']])
            suc = [0.] * suc_len
            task.getsucslice(sol, 0, suc_len, suc)
            dual_vars.update(MOSEK.parse_dual_vars(suc, inverse_data['suc_slacks']))

            # Dual variables for the original equality constraints
            y_len = sum([ell for _, ell in inverse_data['y_slacks']])
            y = [0.] * y_len
            task.getyslice(sol, suc_len, suc_len + y_len, y)
            dual_vars.update(MOSEK.parse_dual_vars(y, inverse_data['y_slacks']))

            # Dual variables for SOC and EXP constraints
            snx_len = sum([ell for _, ell in inverse_data['snx_slacks']])
            snx = np.zeros(snx_len)
            task.getsnxslice(sol, inverse_data['n0'], inverse_data['n0'] + snx_len, snx)
            dual_vars.update(MOSEK.parse_dual_vars(snx, inverse_data['snx_slacks']))

            # Dual variables for PSD constraints
            for j, (id, dim) in enumerate(inverse_data['psd_dims']):
                sj = [0.] * (dim * (dim + 1) // 2)
                task.getbarsj(sol, j, sj)
                dual_vars[id] = vectorized_lower_tri_to_mat(sj, dim)

        else:
            if status == s.INFEASIBLE:
                opt_val = np.inf
            elif status == s.UNBOUNDED:
                opt_val = -np.inf
            else:
                opt_val = None
            primal_vars = None
            dual_vars = None

        return Solution(status, opt_val, primal_vars, dual_vars, attr={})

    @staticmethod
    def parse_dual_vars(dual_var, constr_id_to_constr_dim):
        """

        :param dual_var: a list of numbers returned by some 'get dual variable'
          function in mosek's Optimzer API.
        :param constr_id_to_constr_dim: a list of tuples (id, dim).
          The entry "id" is the index of the cvxpy Constraint
          object to which the next "dim" entries of the dual variable belong.

        :return: a dictionary keyed by cvxpy Constraint object indicies,
          with either scalar or numpy array values.
        """
        dual_vars = dict()
        running_idx = 0
        for id, dim in constr_id_to_constr_dim:
            if dim == 1:
                dual_vars[id] = dual_var[running_idx]  # a scalar
            else:
                dual_vars[id] = np.array(dual_var[running_idx:(running_idx + dim)])
            running_idx += dim
        return dual_vars

    @staticmethod
    def _handle_mosek_params(task, params):
        if params is None:
            return

        import mosek

        def _handle_str_param(param, value):
            if param.startswith("MSK_DPAR_"):
                task.putnadouparam(param, value)
            elif param.startswith("MSK_IPAR_"):
                task.putnaintparam(param, value)
            elif param.startswith("MSK_SPAR_"):
                task.putnastrparam(param, value)
            else:
                raise ValueError("Invalid MOSEK parameter '%s'." % param)

        def _handle_enum_param(param, value):
            if isinstance(param, mosek.dparam):
                task.putdouparam(param, value)
            elif isinstance(param, mosek.iparam):
                task.putintparam(param, value)
            elif isinstance(param, mosek.sparam):
                task.putstrparam(param, value)
            else:
                raise ValueError("Invalid MOSEK parameter '%s'." % param)

        for param, value in params.items():
            if isinstance(param, str):
                _handle_str_param(param.strip(), value)
            else:
                _handle_enum_param(param, value)
