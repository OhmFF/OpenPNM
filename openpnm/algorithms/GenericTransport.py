import numpy as np
import scipy.sparse as sprs
import scipy.sparse.csgraph as spgr
from scipy.spatial import ConvexHull
from scipy.spatial import cKDTree
from openpnm.topotools import iscoplanar
from openpnm.topotools import issymmetric
from openpnm.algorithms import GenericAlgorithm
from openpnm.utils import logging
import inspect
# Check if petsc4py is available
import importlib
if (importlib.util.find_spec('petsc4py') is not None):
    from openpnm.utils.petsc import PetscSparseLinearSolver as sls
logger = logging.getLogger(__name__)


class GenericTransport(GenericAlgorithm):
    r"""
    This class implements steady-state linear transport calculations

    Parameters
    ----------
    network : OpenPNM Network object
        The Network with which this algorithm is associated

    project : OpenPNM Project object, optional
        A Project can be specified instead of ``network``

    Notes
    -----

    The following table shows the methods that are accessible to the user
    for settig up the simulation.

    +---------------------+---------------------------------------------------+
    | Methods             | Description                                       |
    +=====================+===================================================+
    | ``set_value_BC``    | Applies constant value boundary conditions to the |
    |                     | specified pores                                   |
    +---------------------+---------------------------------------------------+
    | ``set_rate_BC``     | Applies constant rate boundary conditions to the  |
    |                     | specified pores                                   |
    +---------------------+---------------------------------------------------+
    | ``remove_BC``       | Removes all boundary conditions from the          |
    |                     | specified pores                                   |
    +---------------------+---------------------------------------------------+
    | ``rate``            | Calculates the total rate of transfer through the |
    |                     | given pores or throats                            |
    +---------------------+---------------------------------------------------+
    | ``setup``           | A shortcut for applying values in the ``settings``|
    |                     | attribute.                                        |
    +---------------------+---------------------------------------------------+
    | ``results``         | Returns the results of the calcualtion as a       |
    |                     | ``dict`` with the data stored under the 'quantity'|
    |                     | specified in the ``settings``                     |
    +---------------------+---------------------------------------------------+

    In addition to the above methods there are also the following attributes:

    +---------------------+---------------------------------------------------+
    | Attribute           | Description                                       |
    +=====================+===================================================+
    | ``A``               | Retrieves the coefficient matrix                  |
    +---------------------+---------------------------------------------------+
    | ``b``               | Retrieves the RHS matrix                          |
    +---------------------+---------------------------------------------------+

    This class contains quite a few hidden methods (preceeded by an
    underscore) that are called internally.  Since these are critical to the
    functioning of this algorithm they are worth outlining even though the
    user does not call them directly:

    +-----------------------+-------------------------------------------------+
    | Method or Attribute   | Description                                     |
    +=======================+=================================================+
    | ``_build_A``          | Builds the **A** matrix based on the            |
    |                       | 'conductance' specified in ``settings``         |
    +-----------------------+-------------------------------------------------+
    | ``_build_b``          | Builds the **b** matrix                         |
    +-----------------------+-------------------------------------------------+
    | ``_apply_BCs``        | Applies the given BCs by adjust the **A** and   |
    |                       | **b** matrices                                  |
    +-----------------------+-------------------------------------------------+
    | ``_calc_eff_prop``    | Finds the effective property (e.g. permeability |
    |                       | coefficient) based on the given BCs             |
    +-----------------------+-------------------------------------------------+
    | ``_solve``            | Runs the algorithm using the solver specified   |
    |                       | in the ``settings``                             |
    +-----------------------+-------------------------------------------------+
    | ``_get_domain_area``  | Attempts to estimate the area of the inlet pores|
    |                       | if not specified by user                        |
    +-----------------------+-------------------------------------------------+
    | ``_get_domain_length``| Attempts to estimate the length between the     |
    |                       | inlet and outlet faces if not specified by the  |
    |                       | user                                            |
    +-----------------------+-------------------------------------------------+


    """

    def __init__(self, project=None, network=None, phase=None, settings={},
                 **kwargs):
        # Set some default settings
        def_set = {'phase': None,
                   'conductance': None,
                   'quantity': None,
                   'solver': 'spsolve',
                   'gui': {'setup':        {'quantity': '',
                                            'conductance': ''},
                           'set_rate_BC':  {'pores': None,
                                            'values': None},
                           'set_value_BC': {'pores': None,
                                            'values': None},
                           'remove_BC':    {'pores': None}
                           }
                   }
        self.settings.update(def_set)
        self.settings.update(settings)

        self.setup(phase=phase, **settings)
        # If network given, get project, otherwise let parent class create it
        if network is not None:
            project = network.project
        super().__init__(project=project, **kwargs)
        # Create some instance attributes
        self._A = None
        self._pure_A = None
        self._b = None
        self._pure_b = None
        self['pore.bc_rate'] = np.nan
        self['pore.bc_value'] = np.nan

    def setup(self, phase=None, quantity='', conductance='', **kwargs):
        r"""
        This method takes several arguments that are essential to running the
        algorithm and adds them to the settings.

        Notes
        -----
        This generic version should be subclassed, and the arguments given
        suitable default names.
        """
        if phase:
            self.settings['phase'] = phase.name
        if quantity:
            self.settings['quantity'] = quantity
        if conductance:
            self.settings['conductance'] = conductance
        self.settings.update(kwargs)

    def set_value_BC(self, pores, values):
        r"""
        Apply constant value boundary conditons to the specified pore
        locations. These are sometimes referred to as Dirichlet conditions.

        Parameters
        ----------
        pores : array_like
            The pore indices where the condition should be applied

        values : scalar or array_like
            The value to of the boundary condition.  If a scalar is supplied
            it is assigne to all locations, and if a vector is applied it
            corresponds directy to the locations given in ``pores``.

        Notes
        -----
        The definition of ``quantity`` is specified in the algorithm's
        ``settings``, e.g. ``alg.settings['quentity'] = 'pore.pressure'``.
        """
        self._set_BC(pores=pores, bctype='value', bcvalues=values,
                     mode='merge')

    def set_rate_BC(self, pores, values):
        r"""
        Apply constant rate boundary conditons to the specified pore
        locations. This is similar to a Neumann boundary condition, but is
        slightly different since it's the conductance multiplied by the
        gradient, while Neumann conditions specify just the gradient.

        Parameters
        ----------
        pores : array_like
            The pore indices where the condition should be applied

        values : scalar or array_like
            The value to of the boundary condition.  If a scalar is supplied
            it is assigne to all locations, and if a vector is applied it
            corresponds directy to the locations given in ``pores``.

        Notes
        -----
        The definition of ``quantity`` is specified in the algorithm's
        ``settings``, e.g. ``alg.settings['quentity'] = 'pore.pressure'``.
        """
        self._set_BC(pores=pores, bctype='rate', bcvalues=values, mode='merge')

    def _set_BC(self, pores, bctype, bcvalues=None, mode='merge'):
        r"""
        Apply boundary conditions to specified pores

        Parameters
        ----------
        pores : array_like
            The pores where the boundary conditions should be applied

        bctype : string
            Specifies the type or the name of boundary condition to apply. The
            types can be one one of the following:

            - *'value'* : Specify the value of the quantity in each location
            - *'rate'* : Specify the flow rate into each location

        bcvalues : int or array_like
            The boundary value to apply, such as concentration or rate.  If
            a single value is given, it's assumed to apply to all locations.
            Different values can be applied to all pores in the form of an
            array of the same length as ``pores``.

        mode : string, optional
            Controls how the conditions are applied.  Options are:

            *'merge'*: (Default) Adds supplied boundary conditions to already
            existing conditions.

            *'overwrite'*: Deletes all boundary condition on object then add
            the given ones

        Notes
        -----
        It is not possible to have multiple boundary conditions for a
        specified location in one algorithm. Use ``remove_BCs`` to
        clear existing BCs before applying new ones or ``mode='overwrite'``
        which removes all existing BC's before applying the new ones.

        """
        # Hijack the parse_mode function to verify bctype argument
        bctype = self._parse_mode(bctype, allowed=['value', 'rate'],
                                  single=True)
        mode = self._parse_mode(mode, allowed=['merge', 'overwrite', 'remove'],
                                single=True)
        pores = self._parse_indices(pores)

        values = np.array(bcvalues)
        if values.size > 1 and values.size != pores.size:
            raise Exception('The number of boundary values must match the ' +
                            'number of locations')

        # Store boundary values
        if ('pore.bc_'+bctype not in self.keys()) or (mode == 'overwrite'):
            self['pore.bc_'+bctype] = np.nan
        self['pore.bc_'+bctype][pores] = values

    def remove_BC(self, pores=None):
        r"""
        Removes all boundary conditions from the specified pores

        Parameters
        ----------
        pores : array_like
            The pores from which boundary conditions are to be removed.  If no
            pores are specified, then BCs are removed from all pores. No error
            is thrown if the provided pores do not have any BCs assigned.
        """
        if pores is None:
            pores = self.Ps
        if 'pore.bc_value' in self.keys():
            self['pore.bc_value'][pores] = np.nan
        if 'pore.rate' in self.keys():
            self['pore.bc_rate'][pores] = np.nan

    def _build_A(self, force=False):
        r"""
        Builds the coefficient matrix based on conductances between pores.
        The conductance to use is specified in the algorithm's ``settings``
        under ``conductance``.  In subclasses (e.g. ``FickianDiffusion``)
        this is set by default, though it can be overwritten.

        Parameters
        ----------
        force : Boolean (default is ``False``)
            If set to ``True`` then the A matrix is built from new.  If
            ``False`` (the default), a cached version of A is returned.  The
            cached version is *clean* in the sense that no boundary conditions
            or sources terms have been added to it.
        """
        if force:
            self._pure_A = None
        if self._pure_A is None:
            network = self.project.network
            phase = self.project.phases()[self.settings['phase']]
            g = phase[self.settings['conductance']]
            am = network.create_adjacency_matrix(weights=g, fmt='coo')
            self._pure_A = spgr.laplacian(am)
        self.A = self._pure_A.copy()

    def _build_b(self, force=False):
        r"""
        Builds the RHS matrix, without applying any boundary conditions or
        source terms. This method is trivial an basically creates a column
        vector of 0's.

        Parameters
        ----------
        force : Boolean (default is ``False``)
            If set to ``True`` then the b matrix is built from new.  If
            ``False`` (the default), a cached version of b is returned.  The
            cached version is *clean* in the sense that no boundary conditions
            or sources terms have been added to it.
        """
        if force:
            self._pure_b = None
        if self._pure_b is None:
            b = np.zeros(shape=(self.Np, ), dtype=float)  # Create vector of 0s
            self._pure_b = b
        self.b = self._pure_b.copy()

    def _get_A(self):
        if self._A is None:
            self._build_A(force=True)
        return self._A

    def _set_A(self, A):
        self._A = A

    A = property(fget=_get_A, fset=_set_A)

    def _get_b(self):
        if self._b is None:
            self._build_b(force=True)
        return self._b

    def _set_b(self, b):
        self._b = b

    b = property(fget=_get_b, fset=_set_b)

    def _apply_BCs(self):
        r"""
        Applies all the boundary conditions that have been specified, by
        adding values to the *A* and *b* matrices.

        """
        if 'pore.bc_rate' in self.keys():
            # Update b
            ind = np.isfinite(self['pore.bc_rate'])
            self.b[ind] = self['pore.bc_rate'][ind]
        if 'pore.bc_value' in self.keys():
            f = np.amax(np.absolute(self.A.data))
            # Update b
            ind = np.isfinite(self['pore.bc_value'])
            self.b[ind] = f*self['pore.bc_value'][ind]
            # Update A
            # Find all entries on rows associated with value bc
            P_bc = self.toindices(np.isfinite(self['pore.bc_value']))
            indrow = np.in1d(self.A.row, P_bc)
            self.A.data[indrow] = 0  # Remove entries from A for all BC rows
            datadiag = self.A.diagonal()  # Add diagonal entries back into A
            datadiag[P_bc] = f*np.ones_like(P_bc, dtype=float)
            self.A.setdiag(datadiag)
            self.A.eliminate_zeros()  # Remove 0 entries

    def run(self):
        r"""
        Builds the A and b matrices, and calls the solver specified in the
        ``settings`` attribute.

        Parameters
        ----------
        x : ND-array
            Initial guess of unknown variable

        Returns
        -------
        Nothing is returned...the solution is stored on the objecxt under
        ``pore.quantity`` where *quantity* is specified in the ``settings``
        attribute.

        """
        logger.info('―'*80)
        logger.info('Running GenericTransport')
        self._run_generic()

    def _run_generic(self):
        self._apply_BCs()
        x_new = self._solve()
        self[self.settings['quantity']] = x_new

    def _solve(self, A=None, b=None):
        r"""
        Sends the A and b matrices to the specified solver, and solves for *x*
        given the boundary conditions, and source terms based on the present
        value of *x*.  This method does NOT iterate to solve for non-linear
        source terms or march time steps.

        Parameters
        ----------
        A : sparse matrix
            The coefficient matrix in sparse format. If not specified, then
            it uses  the ``A`` matrix attached to the object.

        b : ND-array
            The RHS matrix in any format.  If not specified, then it uses
            the ``b`` matrix attached to the object.

        Notes
        -----
        The solver used here is specified in the ``settings`` attribute of the
        algorithm.

        """
        if A is None:
            A = self.A
            if A is None:
                raise Exception('The A matrix has not been built yet')
        if b is None:
            b = self.b
            if b is None:
                raise Exception('The b matrix has not been built yet')

        if self.settings['solver'] == 'petsc':
            # Check if petsc is available
            petsc = importlib.util.find_spec('petsc4py')
            if not petsc:
                raise Exception('petsc is not installed')
            if not self.settings['petsc_solver']:
                self.settings['petsc_solver'] = 'cg'
            if not self.settings['petsc_precondioner']:
                self.settings['petsc_precondioner'] = 'jacobi'
            if not self.settings['petsc_atol']:
                self.settings['petsc_atol'] = 1e-06
            if not self.settings['petsc_rtol']:
                self.settings['petsc_rtol'] = 1e-06
            if not self.settings['petsc_max_it']:
                self.settings['petsc_max_it'] = 1000
            # Define the petsc linear system converting the scipy objects
            ls = sls(A=A.tocsr(), b=b)
            ls.settings.update({'solver': self.settings['petsc_solver'],
                                'preconditioner':
                                    self.settings['petsc_precondioner'],
                                'atol': self.settings['petsc_atol'],
                                'rtol': self.settings['petsc_rtol'],
                                'max_it': self.settings['petsc_max_it']})
            x = sls.solve(ls)
            del(ls)  # Clean
        else:
            A = A.tocsr()
            A.indices = A.indices.astype(np.int64)
            A.indptr = A.indptr.astype(np.int64)
            solver = getattr(sprs.linalg, self.settings['solver'])
            if 'tol' in inspect.getfullargspec(solver)[0]:
                # If an iterative solver is used, set tol
                norm_A = sprs.linalg.norm(self._A)
                norm_b = np.linalg.norm(self._b)
                tol = min(norm_A, norm_b)*1e-06
                x = solver(A=A, b=b, tol=tol)
            else:
                sym = issymmetric(A)
                if (sym and self.settings['solver'] == 'spsolve_triangular'):
                    solver = getattr(sprs.linalg, self.settings['solver'])
                    x = solver(A=sprs.tril(A), b=b)
                else:
                    x = solver(A=A, b=b)
        if type(x) == tuple:
            x = x[0]
        return x

    def results(self):
        r"""
        Fetches the calculated quantity from the algorithm and returns it as
        an array.
        """
        quantity = self.settings['quantity']
        d = {quantity: self[quantity]}
        return d

    def rate(self, pores=[], throats=[], mode='group'):
        r"""
        Calculates the net rate of material moving into a given set of pores or
        throats

        Parameters
        ----------
        pores : array_like
            The pores for which the rate should be calculated

        throats : array_like
            The throats through which the rate should be calculated

        mode : string, optional
            Controls how to return the rate.  Options are:

            *'group'*: (default) Returns the cumulative rate of material
            moving into the given set of pores

            *'single'* : Calculates the rate for each pore individually

        Returns
        -------
        If ``pores`` are specified, then the returned values indicate the
        net rate of material exiting the pore or pores.  Thus a positive
        rate indicates material is leaving the pores, and negative values
        mean material is entering.

        If ``throats`` are specified the rate is calculated in the direction of
        the gradient, thus is always positive.

        If ``mode`` is 'single' then the cumulative rate through the given
        pores (or throats) are returned as a vector, if ``mode`` is 'group'
        then the individual rates are summed and returned as a scalar.

        """
        pores = self._parse_indices(pores)
        throats = self._parse_indices(throats)

        network = self.project.network
        phase = self.project.phases()[self.settings['phase']]
        g = phase[self.settings['conductance']]
        quantity = self[self.settings['quantity']]

        P12 = network['throat.conns']
        X12 = quantity[P12]
        f = (-1)**np.argsort(X12, axis=1)[:, 1]
        Dx = np.abs(np.diff(X12, axis=1).squeeze())
        Qt = -f*g*Dx

        if len(throats) and len(pores):
            raise Exception('Must specify either pores or throats, not both')
        elif len(throats):
            R = np.absolute(Qt[throats])
            if mode == 'group':
                R = np.sum(R)
        elif len(pores):
            Qp = np.zeros((self.Np, ))
            np.add.at(Qp, P12[:, 0], -Qt)
            np.add.at(Qp, P12[:, 1], Qt)
            R = Qp[pores]
            if mode == 'group':
                R = np.sum(R)
        return np.array(R, ndmin=1)

    def _calc_eff_prop(self, inlets=None, outlets=None,
                       domain_area=None, domain_length=None):
        r"""
        Calculate the effective transport through the network

        Parameters
        ----------
        inlets : array_like
            The pores where the inlet boundary conditions were applied.  If
            not given an attempt is made to infer them from the algorithm.

        outlets : array_like
            The pores where the outlet boundary conditions were applied.  If
            not given an attempt is made to infer them from the algorithm.

        domain_area : scalar
            The area of the inlet and/or outlet face (which shold match)

        domain_length : scalar
            The length of the domain between the inlet and outlet faces

        Returns
        -------
        The effective transport property through the network

        """
        if self.settings['quantity'] not in self.keys():
            raise Exception('The algorithm has not been run yet. Cannot ' +
                            'calculate effective property.')

        Ps = np.isfinite(self['pore.bc_value'])
        BCs = np.unique(self['pore.bc_value'][Ps])
        Dx = np.abs(np.diff(BCs))
        if inlets is None:
            inlets = self._get_inlets()
        flow = self.rate(pores=inlets)
        # Fetch area and length of domain
        if domain_area is None:
            domain_area = self._get_domain_area(inlets=inlets,
                                                outlets=outlets)
        if domain_length is None:
            domain_length = self._get_domain_length(inlets=inlets,
                                                    outlets=outlets)
        D = np.sum(flow)*domain_length/domain_area/Dx
        return D

    def _get_inlets(self):
        # Determine boundary conditions by analyzing algorithm object
        Ps = np.isfinite(self['pore.bc_value'])
        BCs = np.unique(self['pore.bc_value'][Ps])
        inlets = np.where(self['pore.bc_value'] == np.amax(BCs))[0]
        return inlets

    def _get_outlets(self):
        # Determine boundary conditions by analyzing algorithm object
        Ps = np.isfinite(self['pore.bc_value'])
        BCs = np.unique(self['pore.bc_value'][Ps])
        outlets = np.where(self['pore.bc_value'] == np.amin(BCs))[0]
        return outlets

    def _get_domain_area(self, inlets=None, outlets=None):
        logger.warning('Attempting to estimate inlet area...will be low')
        network = self.project.network
        # Abort if network is not 3D
        if np.sum(np.ptp(network['pore.coords'], axis=0) == 0) > 0:
            raise Exception('The network is not 3D, specify area manually')
        if inlets is None:
            inlets = self._get_inlets()
        if outlets is None:
            outlets = self._get_outlets()
        inlets = network['pore.coords'][inlets]
        outlets = network['pore.coords'][outlets]
        if not iscoplanar(inlets):
            logger.error('Detected inlet pores are not coplanar')
        if not iscoplanar(outlets):
            logger.error('Detected outlet pores are not coplanar')
        Nin = np.ptp(inlets, axis=0) > 0
        if Nin.all():
            logger.warning('Detected inlets are not oriented along a ' +
                           'principle axis')
        Nout = np.ptp(outlets, axis=0) > 0
        if Nout.all():
            logger.warning('Detected outlets are not oriented along a ' +
                           'principle axis')
        hull_in = ConvexHull(points=inlets[:, Nin])
        hull_out = ConvexHull(points=outlets[:, Nout])
        if hull_in.volume != hull_out.volume:
            logger.error('Inlet and outlet faces are different area')
        area = hull_in.volume  # In 2D volume=area, area=perimeter
        return area

    def _get_domain_length(self, inlets=None, outlets=None):
        logger.warning('Attempting to estimate domain length... ' +
                       'could be low if boundary pores were not added')
        network = self.project.network
        if inlets is None:
            inlets = self._get_inlets()
        if outlets is None:
            outlets = self._get_outlets()
        inlets = network['pore.coords'][inlets]
        outlets = network['pore.coords'][outlets]
        if not iscoplanar(inlets):
            logger.error('Detected inlet pores are not coplanar')
        if not iscoplanar(outlets):
            logger.error('Detected inlet pores are not coplanar')
        tree = cKDTree(data=inlets)
        Ls = np.unique(np.around(tree.query(x=outlets)[0], decimals=5))
        if np.size(Ls) != 1:
            logger.error('A unique value of length could not be found')
        length = Ls[0]
        return length
