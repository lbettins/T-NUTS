import os
import copy
import uuid
import numpy as np
import subprocess
import time
from ape.common import get_electronic_energy as get_e_elect
from ape.InternalCoordinates import getXYZ

from tnuts.common import evolve_dihedral_by, get_energy_gradient, log_trajectory
from tnuts.mode import dicts_to_NModes

class Geometry:
    def __init__(self, samp_obj, internal, syms):
        self.count = 0

        self.samp_obj = samp_obj
        self.n_rotors = samp_obj.n_rotors

        self.internal0 = copy.deepcopy(samp_obj.torsion_internal)
        self.geom0 = copy.deepcopy(self.internal0.cart_coords)

        self.internal = internal
        self.geom = self.internal.cart_coords

        # Current configuration
        self.xcur = np.zeros(self.n_rotors)

        # Output energy/grad file
        self.dict = {}

        # Number of dihedrals and reference (equilibrium) dihedrals
        self.scans = [np.array(samp_obj.rotors_dict[n+1]['scan'])\
                for n in range(self.n_rotors)]
        scan_indices = internal.B_indices[-self.n_rotors:]
        self.dihedrals0 = np.array([self.internal.calc_dihedral(self.internal0.c3d, self.scans[i]-1) for i in range(self.n_rotors)])

        # Indices of dihedrals in B matrix
        self.torsion_inds = [len(internal.B_indices) - self.n_rotors +\
                scan_indices.index([ind-1 for ind in scan]) for scan in self.scans]
        B = internal.B
        Bt_inv = np.linalg.pinv(B.dot(B.T)).dot(B)
        nrow = B.shape[0]
        # Displacement identity vector for all torsions
        self.qk = np.zeros(nrow, dtype=int)
        self.signs = np.ones(self.n_rotors)
        for i,ind in enumerate(self.torsion_inds):
            self.qk[ind] = 1
            if internal.prim_coords[ind] > 0:
                self.qk[ind] *= -1
                self.signs[i] *= -1
        self.symmetry_numbers = np.atleast_1d(syms)
        self.L = 2*np.pi/self.symmetry_numbers
        self.center_mod = lambda x: \
                ((x.transpose() % self.L) \
                - self.L*((x.transpose() % self.L) // ((self.L)/2))).transpose()

        # Bookkeeping
        if not os.path.exists(samp_obj.output_directory):
            os.makedirs(samp_obj.output_directory)
        self.path = os.path.join(samp_obj.output_directory, 'nuts_out', samp_obj.label)
        if not os.path.exists(self.path):
            os.makedirs(self.path)
        n = 0
        traj_log = os.path.join(samp_obj.output_directory, 'nuts_out', 'trajectories_{}.txt')
        while os.path.exists(traj_log.format(n)):
            n += 1
        self.traj_log = traj_log.format(n)

        # Timing
        self.qchem_runtime = 0.
        self.coord_runtime = 0.

        # Define args and kwargs for running jobs
        if not samp_obj.is_QM_MM_INTERFACE:
            self.kwargs = dict(charge=samp_obj.charge,
                multiplicity=samp_obj.spin_multiplicity,
                level_of_theory=samp_obj.level_of_theory, basis=samp_obj.basis,
                unrestricted=samp_obj.unrestricted)
        else:
            self.kwargs = dict(charge=samp_obj.charge,
                multiplicity=samp_obj.spin_multiplicity,
                level_of_theory=samp_obj.level_of_theory, basis=samp_obj.basis,
                unrestricted=samp_obj.unrestricted,
                is_QM_MM_INTERFACE=samp_obj.is_QM_MM_INTERFACE,
                QM_USER_CONNECT=samp_obj.QM_USER_CONNECT,
                QM_ATOMS=samp_obj.QM_ATOMS,
                force_field_params=samp_obj.force_field_params,
                fixed_molecule_string=samp_obj.fixed_molecule_string,
                opt=samp_obj.opt,
                number_of_fixed_atoms=samp_obj.number_of_fixed_atoms)

    def reset(self):
        self.internal = copy.deepcopy(self.internal0)
        self.geom = self.internal.cart_coords
        self.xcur = np.zeros(self.n_rotors)

    def hard_reset(self):
        self.reset()
        self.clear_dict()

    def clear_dict(self):
        self.dict = {}

    def check_dihedral(self, target):
        dihedrals = np.array([self.internal.calc_dihedral(self.internal.c3d, self.scans[i]-1) for i in range(self.n_rotors)])
        net = self.center_mod(dihedrals - self.dihedrals0)
        self.xcur = net
        err = self.center_mod(net - target)
        return net, err

    def transform_geometry_to(self, x, incr_step=np.pi/12, err_thresh=1e-5, cart_rms_thresh=1E-9):
        step = np.zeros(len(self.qk))
        tic = time.perf_counter()
        cur_dihedral, error = self.check_dihedral(x)
        while (np.abs(error) > err_thresh).any():
            for err,ind in zip(error,self.torsion_inds):
                step[ind] = self.qk[ind]*min(np.abs(err), np.abs(incr_step))*np.sign(err)
            try:
                self.internal.transform_int_step(step, cart_rms_thresh=cart_rms_thresh)
            except:
                self.hard_reset()
                return self.transform_geometry_to(x)
            cur_dihedral, error = self.check_dihedral(x)
        toc = time.perf_counter()
        print("CONVERGED", 'Time:', toc-tic, "seconds")
        return getXYZ(self.samp_obj.symbols, self.internal.cart_coords)

    def get_geometry_at(self, x):
        """
        Δx = x - xi
        """
        x = np.atleast_1d(x)
        delta_x = x - self.xcur
        #if (delta_x > np.pi/36).any():
        #    x %= 2*np.pi/self.symmetry_numbers
        step = np.zeros(len(self.qk))
        for i,ind in enumerate(self.torsion_inds):
            step[ind] = self.qk[ind] * delta_x[i]

        def transform_geom(dq, internal, max_step=np.pi/36):
            """
            Internal coordinate transformation needs to be incremental
            """
            step_wide = np.array([-1 if dq_i > max_step else 1 if dq_i < -max_step\
                else 0 for dq_i in dq])
            if step_wide.any():
                ddq = max_step*step_wide
                #print((dq+ddq)[-self.n_rotors:])
                evolve_dihedral_by(ddq, internal, cart_rms_thresh=1e-15)
                return transform_geom(dq+ddq, internal)
            else:
                return evolve_dihedral_by(dq, internal)

        xyz = transform_geom(step, self.internal, np.pi/36)
        #if xyz is not self.geom:
        #    self.geom = xyz
        self.xcur = x
        return getXYZ(self.samp_obj.symbols, self.internal.cart_coords)

    def get_energy_grad_at(self, x, which="energy"):
        """
        Returns E, gradient. Stores both values to dict with positional key.
        """
        #roundx = 2*np.around(x/2,2)
        try:
        # IF the gradient or energy has already been calculated for the position,
        # find the desired value and return it immediately
            egrad = self.dict[tuple(x)]
            if which == "energy":
                log_trajectory(self.traj_log,x,egrad[0],egrad[1])
                return egrad[0]
            else:
                self.clear_dict()
                return egrad[1]
        # OTHERWISE we need to calculate it below
        except KeyError:
            pass
         
        #Get the energy/gradient from QChem
        tic = time.perf_counter()
        prev_xyz = getXYZ(self.samp_obj.symbols, self.internal.cart_coords)
        prev_x = tuple(self.xcur)
        #xyz = self.get_geometry_at(tuple(roundx))
        #xyz = self.get_geometry_at(x)
        xyz = self.transform_geometry_to(x)
        toc = time.perf_counter()
        self.coord_runtime += toc-tic

        file_name = '{}_{}'.format(self.count, uuid.uuid4().hex)
        args = (self.path, file_name, self.samp_obj.ncpus)

        # Get and time energy/gradient
        tic = time.perf_counter()
        E,grad = get_energy_gradient(xyz,*args,**self.kwargs)
        toc = time.perf_counter()
        self.qchem_runtime += toc-tic

        # if the job succeeds, proceed normally
        if grad is not None and E is not None:
            E -= self.samp_obj.e_elect  # All in Hartree
            if E > 1.:
                print("Energy is:", E)
                print("Errors in coordinate transformations:")
                print("Previous:", prev_x, "\n",
                    prev_xyz, "\n"
                    "Current:", tuple(x), "\n",
                    xyz)
                self.hard_reset()
                return self.get_energy_grad_at(x,which=which)

            B = self.internal.B_prim
            Bt_inv = np.linalg.pinv(B.dot(B.T)).dot(B)
    
            grad = Bt_inv.dot(grad)[self.torsion_inds]
            grad *= self.signs  # In Hartree per rad

            #E += ((x-roundx)*grad).sum()

            log_trajectory(self.traj_log,x,E,grad)
            subprocess.Popen(['rm {input_path}/{file_name}.q.out'.format(input_path=self.path,
                file_name=file_name)], shell=True)
            self.Ecur = E
            self.gradcur = grad
            # Set the energy/gradient in memory and return
            self.dict[tuple(x)] = (E, grad)
        # or else reset internals and try again
        else:
            if E is not None:
                print("Energy is", E)
            print("Errors in coordinate transformations:")
            print("Previous:", prev_x, "\n",
                    prev_xyz, "\n"
                    "Current:", tuple(x), "\n",
                    xyz)
            self.hard_reset()
            return self.get_energy_grad_at(x,which=which)
            
        self.count += 1
        if (self.count % 100) == 0:
            self.reset()
        elif (self.count % 200) == 0:
            self.hard_reset()
        if which == "energy":
            return E
        else:
            return grad

if __name__ == '__main__':
    directory = '/Users/lancebettinson/Documents/entropy/um-vt/MeOOH'
    freq_file = os.path.join(directory,'MeOOH.out')
    label = 'MeOOH'
    from ape.sampling import SamplingJob
    samp_obj = SamplingJob(label,freq_file,output_directory=directory,
            protocol='TNUTS')
    samp_obj.parse()
    xyz_dict, energy_dict, mode_dict = samp_obj.sampling()
    modes = dicts_to_NModes(mode_dict, energy_dict, xyz_dict,
                samp_obj=samp_obj)
    syms = np.array([mode.get_symmetry_number() for mode in modes])
    geom = Geometry(samp_obj, samp_obj.torsion_internal, syms)
    
    x = 0./syms
    #xyz = geom.get_geometry_at(x)
    #with open(os.path.join(os.path.expandvars("$SCRATCH"),"geom_evol_test.txt"),'a') as f:
    #    for i in range(100):
    #        f.write(str(7)+'\n')
    #        f.write("#Position"+str(geom.xcur)+'\n')
    #        f.write(xyz+'\n')
    #        #print(geom.xcur,'\n',xyz,'\n')
    #        if i < 50:
    #            xyz = geom.get_geometry_at(x+i*np.array([2*np.pi/50,4*np.pi/50]))
    #        elif i > 50:
    #            xyz = geom.get_geometry_at(x-i*np.array([4*np.pi/50,2*np.pi/50]))
    #        elif i == 50:
    #            xyz = geom.get_geometry_at(geom.xcur)


    tic1 = time.perf_counter()
    xyz = geom.get_geometry_at( 0*np.pi/syms )
    grad = geom.get_energy_grad_at( geom.xcur )
    toc1 = time.perf_counter()
    print(geom.xcur)
    
    tic2 = time.perf_counter()
    xyz1 = geom.get_geometry_at( np.pi/syms )
    grad = geom.get_energy_grad_at( geom.xcur, which="grad" )
    toc2 = time.perf_counter()
    print(geom.xcur)
    
    tic3 = time.perf_counter()
    xyz2 = geom.get_geometry_at( np.pi/syms+0.01 )
    grad = geom.get_energy_grad_at( geom.xcur, which="grad" )
    toc3 = time.perf_counter()

    tic4 = time.perf_counter()
    grad = geom.get_energy_grad_at( 0*np.pi/syms, which="grad" )
    toc4 = time.perf_counter()
    print(geom.xcur)
    print(xyz2+'\n')
    print(xyz1+'\n')
    print(xyz+'\n')
    print(f"Geometry 0 calculation: {toc1 - tic1:0.4f} seconds")
    print(f"Maximally perturbed: {toc2 - tic2:0.4f} seconds")
    print(f"Small pertubation from max: {toc3 - tic3:0.4f} seconds")
    print(f"Reading from dictionary: {toc4 - tic4:0.4f} seconds")
    print(f"QChem runtime: {geom.qchem_runtime:0.4f} seconds")
    print(f"Coord runtime: {geom.coord_runtime:0.4f} seconds")

    #print(geom.signs)
    #print(geom.dict)

