import vtk
import os
import pickle
import time
import SimpleITK as sitk
import numpy as np
from vtk.util import vtkImageImportFromArray as vti
from vtk.util.numpy_support import (vtk_to_numpy, numpy_to_vtk) 
from meshpy.tet import (MeshInfo, build, Options)


class CellMech(object):
    """Quantifies deformation between objects in reference and deformed states.

    Object geometries are obtained by importing polygonal sufaces saved in the STL format. The
    user indicates a single reference directory containing the files and all corresponding
    deformed directories. The STL files must be named the same in each directory, so they are
    matched appropriately. In most cases, these will have been generated by pyCellAnalyst.Volume(),
    and by default will be named [image_dirname]_results.

    Parameters
    ----------
    ref_dir : str
        The directory containing the STL files corresponding to the reference (undeformed) state.
    def_dir : str
        The directory containing the STL files corresponding to the deformed state.
    rigidInitial : bool, optional
        If *True* do an initial rigid body transformation to align objects.
    deformable : bool, optional
        If *True* deformable image registration will be performed. This will call
        deformableRegistration(), which will calculate a displacement map between
        images reconstructed from reference and deformed surfaces.
    saveFEA : bool, optional
        If *True* will save nodes, elements, surface nodes, and displacement boundary conditions
        in a dictionary to *cell{:02d}.pkl*. This information can then be used to run finite element
        analysis in whatever software the user desires.
    deformableSettings : dict, optional
        Settings for deformable image registration with fields:
            * **Iterations:** (int, 200) The maximum number of iterations for algorithm.
            * **Maximum RMS:** (float, 0.01) Will terminate iterations if root-mean-square error is less than.
            * **Displacement Smoothing:** (float, 3.0) Variance for Gaussian smoothing of displacement field result.
            * **Precision:** (float, 0.01) The fraction of the object bounding box in each dimension spanned by 1 voxel. 
    display : bool, optional
        If *True* will display 3-D interactive rendering of displacement fields.

    Attributes
    ----------
    rsurfs : [,vtkPolyData,...]
        Polygonal surfaces of objects in reference state.
    dsurfs : [,vtkPolyData,...]
        Polygonal surfaces of objects in deformed state.
    rmeshes : [,vtkUnstructuredGrid,...]
        TETGEN generated tetrahedral meshes.
    rcentroids : [,ndarray(3, float),...]
        Volumetric centroids of objects in reference state.
    dcentroids : [,ndarray(3, float),...]
        Volumetric centroids of objects in deformed state.
    cell_strains : [,ndarray((3,3), float)
        Green-Lagrange strain tensors for object assuming uniform deformation.
    ecm_strain : ndarray((3,3), float)
        Green-Lagrange strain tensor for extracellular (extra-object) matrix assuming
        uniform deformation.
    rvols : [,float,...]
        Volumes of objects in reference state.
    dvols : [,float,...]
        Volumes of objects in deformed state.
    raxes : [,ndarray(3, float),...]
        Lengths of axes for ellipsoid with equivalent principal moments of inertia to object in reference state.
    daxes : [,ndarray(3, float),...]
        Lengths of axes for ellisoid with equivalent principal moments of inertia to object in deformed state.
    cell_fields : [,vtkUnstructuredGrid,...]
        Displacement vectors determined by deformable image registration interpolated to the vertices of object
        mesh in reference state.
    """

    def __init__(self,
                 ref_dir=None,
                 def_dir=None,
                 rigidInitial=True,
                 deformable=False,
                 saveFEA=False,
                 deformableSettings={'Iterations': 200,
                                     'Maximum RMS': 0.01,
                                     'Displacement Smoothing': 3.0,
                                     'Precision': 0.01},
                 display=False):

        if ref_dir is None:
            raise SystemExit(("You must indicate a directory containing "
                              "reference state STLs. Terminating..."))
        if def_dir is None:
            raise SystemExit(("You must indicate a directory containing "
                              "deformed state STLs. Terminating..."))
        self._ref_dir = ref_dir
        self._def_dir = def_dir
        self.rigidInitial = rigidInitial
        self.display = display
        self.deformable = deformable
        self.saveFEA = saveFEA
        self.deformableSettings = deformableSettings

        self.rsurfs = []
        self.dsurfs = []
        self.rmeshes = []
        self.rcentroids = []
        self.dcentroids = []
        self.cell_strains = []
        self.ecm_strain = None
        self.rvols = []
        self.dvols = []
        self.raxes = []
        self.daxes = []

        self.cell_fields = []

        self.rigidTransforms = []

        self._elements = []
        self._nodes = []
        self._snodes = []
        self._bcs = []

        self._readstls()
        if not(self.rsurfs):
            raise Exception(("No 3D surfaces detected. Currently 2D analysis "
                             "is not supported, so nothing was done."))
        self._getECMstrain()
        self._deform()

        if self.deformable:
            self.deformableRegistration()
        # no support for 2D FEA yet
        if self.saveFEA:
            for i, bc in enumerate(self._bcs):
                fea = {'nodes': self._nodes[i],
                       'elements': self._elements[i],
                       'surfaces': self._snodes[i],
                       'boundary conditions': bc}
                fid = open(str(os.path.normpath(
                    self._def_dir + os.sep + 'cellFEA{:02d}.pkl'
                    .format(i))), 'wb')
                pickle.dump(fea, fid)
                fid.close()
        print("Analysis of {:s} completed...".format(self._def_dir))

    def _readstls(self):
        """
        Read in STL files if self.surfaces is True
        """
        for fname in sorted(os.listdir(self._ref_dir)):
            if '.stl' in fname.lower():
                reader = vtk.vtkSTLReader()
                reader.SetFileName(
                    str(os.path.normpath(self._ref_dir + os.sep + fname)))
                reader.Update()
                triangles = vtk.vtkTriangleFilter()
                triangles.SetInputConnection(reader.GetOutputPort())
                triangles.Update()
                self.rsurfs.append(triangles.GetOutput())
                self._make3Dmesh(
                    str(os.path.normpath(self._ref_dir + os.sep + fname)),
                    'MATERIAL')

        for fname in sorted(os.listdir(self._def_dir)):
            if '.stl' in fname.lower():
                reader = vtk.vtkSTLReader()
                reader.SetFileName(
                    str(os.path.normpath(self._def_dir + os.sep + fname)))
                reader.Update()
                triangles = vtk.vtkTriangleFilter()
                triangles.SetInputConnection(reader.GetOutputPort())
                triangles.Update()
                self.dsurfs.append(triangles.GetOutput())
                self._make3Dmesh(
                    str(os.path.normpath(self._def_dir + os.sep + fname)),
                    'SPATIAL')

    def _deform(self):
        r"""
        Calculates the affine transform that best maps the reference polygonal
        surface to its corresponding deformed surface. This transform is calculated
        through an interactive closest point optimization, that seeks to minimize
        the sum of distances between the reference surface vertices and the current
        affine transformed surface.

        Assuming a uniform deformation, the non-translational elements of this affine
        transform compose the deformation gradient :math:`\mathbf{F}`. The Green-Lagrange
        strain tensor is then defined as

        :math:`\mathbf{E} = \frac{1}{2}(\mathbf{F}^T.\mathbf{F} - \mathbf{1})`,

        where :math:`\mathbf{1}` is the identity.

        Returns
        -------
        cell_strains
        """
        for i in xrange(len(self.rcentroids)):
            # volumetric strains
            self.vstrains.append(self.dvols[i] / self.rvols[i] - 1)

            ICP = vtk.vtkIterativeClosestPointTransform()
            rcopy = vtk.vtkPolyData()
            dcopy = vtk.vtkPolyData()
            rcopy.DeepCopy(self.rsurfs[i])
            dcopy.DeepCopy(self.dsurfs[i])
            ICP.SetSource(rcopy)
            ICP.SetTarget(dcopy)
            if self.rigidInitial:
                ICP.GetLandmarkTransform().SetModeToRigidBody()
                ICP.SetMaximumMeanDistance(0.001)
                ICP.SetCheckMeanDistance(1)
                ICP.SetMaximumNumberOfIterations(5000)
                ICP.StartByMatchingCentroidsOn()
                ICP.Update()
                trans = vtk.vtkTransform()
                trans.SetMatrix(ICP.GetMatrix())
                trans.Update()
                self.rigidTransforms.append(trans)
                rot = vtk.vtkTransformPolyDataFilter()
                rot.SetInputData(rcopy)
                rot.SetTransform(trans)
                rot.Update()
                ICP.GetLandmarkTransform().SetModeToAffine()
                ICP.SetSource(rot.GetOutput())
                ICP.Update()
            else:
                ICP.GetLandmarkTransform().SetModeToAffine()
                ICP.SetMaximumMeanDistance(0.001)
                ICP.SetCheckMeanDistance(1)
                ICP.SetMaximumNumberOfIterations(5000)
                ICP.StartByMatchingCentroidsOn()
                ICP.Update()

            F = np.zeros((3, 3), float)
            for j in xrange(3):
                for k in xrange(3):
                    F[j, k] = ICP.GetMatrix().GetElement(j, k)
            E = 0.5 * (np.dot(F.T, F) - np.eye(3))
            self.cell_strains.append(E)

    def deformableRegistration(self):
        """
        Performs deformable image registration on images reconstructed from polygonal surfaces at a user-specified precision.
        If **animate** parameter is *True*, this also spawns a VTK interative rendering that can animate the deformation. 

        Returns
        -------
        cell_fields
        """
        for r, mesh in enumerate(self.rmeshes):
            print("Performing deformable image registration for object {:d}"
                  .format(r + 1))
            rimg, dimg, rpoly = self._poly2img(r)
            origin = rimg.GetOrigin()
            rimg.SetOrigin((0, 0, 0))
            dimg.SetOrigin((0, 0, 0))

            steplength = np.min(dimg.GetSpacing()) * 5.0
            rimg = sitk.AntiAliasBinary(rimg)
            dimg = sitk.AntiAliasBinary(dimg)

            #peform the deformable registration
            register = sitk.FastSymmetricForcesDemonsRegistrationFilter()
            register.SetNumberOfIterations(
                self.deformableSettings['Iterations'])
            register.SetMaximumRMSError(self.deformableSettings['Maximum RMS'])
            register.SmoothDisplacementFieldOn()
            register.SetStandardDeviations(
                self.deformableSettings['Displacement Smoothing'])
            register.SmoothUpdateFieldOff()
            register.UseImageSpacingOn()
            register.SetMaximumUpdateStepLength(steplength)
            register.SetUseGradientType(0)
            disp_field = register.Execute(rimg, dimg)
            print("...Elapsed iterations: {:d}"
                  .format(register.GetElapsedIterations()))
            print("...Change in RMS error: {:6.3f}"
                  .format(register.GetRMSChange()))

            disp_field.SetOrigin(origin)

            #translate displacement field to VTK regular grid
            a = sitk.GetArrayFromImage(disp_field)
            disp = vtk.vtkImageData()
            disp.SetOrigin(disp_field.GetOrigin())
            disp.SetSpacing(disp_field.GetSpacing())
            disp.SetDimensions(disp_field.GetSize())
            arr = numpy_to_vtk(a.ravel(), deep=True, array_type=vtk.VTK_DOUBLE)
            arr.SetNumberOfComponents(3)
            arr.SetName("Displacement")
            disp.GetPointData().SetVectors(arr)

            #calculate the strain from displacement field
            getStrain = vtk.vtkCellDerivatives()
            getStrain.SetInputData(disp)
            getStrain.SetTensorModeToComputeStrain()
            getStrain.Update()
            #add the strain tensor to the displacement field structured grid
            strains = getStrain.GetOutput()
            c2p = vtk.vtkCellDataToPointData()
            c2p.PassCellDataOff()
            c2p.SetInputData(strains)
            c2p.Update()
            disp = c2p.GetOutput()

            #use VTK probe filter to interpolate displacements and strains
            #to 3D meshes of cells and save as UnstructuredGrid (.vtu)
            # to visualize in ParaView; this is a linear interpolation
            print("...Interpolating displacements to 3D mesh.")
            if self.rigidInitial:
                #transform 3D Mesh
                tf = vtk.vtkTransformFilter()
                tf.SetInputData(mesh)
                tf.SetTransform(self.rigidTransforms[r])
                tf.Update()
                mesh = tf.GetOutput()
            probe = vtk.vtkProbeFilter()
            probe.SetInputData(mesh)
            probe.SetSourceData(disp)
            probe.Update()
            field = probe.GetOutput()
            if self.display:
                probe2 = vtk.vtkProbeFilter()
                probe2.SetInputData(rpoly)
                probe2.SetSourceData(disp)
                probe2.Update()

            self.cell_fields.append(field)
            if self.saveFEA:
                idisp = field.GetPointData().GetVectors()
                bcs = np.zeros((len(self._snodes[r]), 3), float)
                for j, node in enumerate(self._snodes[r]):
                    d = idisp.GetTuple3(node - 1)
                    bcs[j, 0] = d[0]
                    bcs[j, 1] = d[1]
                    bcs[j, 2] = d[2]
                self._bcs.append(bcs)
            idWriter = vtk.vtkXMLUnstructuredGridWriter()
            idWriter.SetFileName(
                str(os.path.normpath(self._def_dir + os.sep +
                                     'cell{:04d}.vtu'.format(r + 1))))
            idWriter.SetInputData(self.cell_fields[r])
            idWriter.Write()
            if self.display:
                self.animate(probe2.GetOutput(), r)
        print("Registration completed.")

    def _getECMstrain(self):
        """
        Generates tetrahedrons from object centroids in the reference and deformed states.
        The highest quality tetrahedron (edge ratio closest to 1) is used to construct a
        linear system of equations,

        :math:`\|\mathbf{w}\|^2 - \|\mathbf{W}\|^2 = \mathbf{W}.\mathbf{E}.\mathbf{W}`,

        where, :math:`\mathbf{W}` are the reference tetrahedron edges (as vectors) and
        :math:`\mathbf{w}` are the deformed tetrahedron edges, to solve for Green-Lagrange
        strain, :math:`\mathbf{E}`.

        Returns
        -------
        ecm_strain
        """
        #get the ECM strain
        rc = np.array(self.rcentroids)
        dc = np.array(self.dcentroids)
        if rc.shape[0] < 4:
            print(("WARNING: There are less than 4 objects in the space; "
                   "therefore, tissue strain was not calculated."))
            return
        da = numpy_to_vtk(rc)
        p = vtk.vtkPoints()
        p.SetData(da)
        pd = vtk.vtkPolyData()
        pd.SetPoints(p)

        tet = vtk.vtkDelaunay3D()
        tet.SetInputData(pd)
        tet.Update()
        quality = vtk.vtkMeshQuality()
        quality.SetInputData(tet.GetOutput())
        quality.Update()
        mq = quality.GetOutput().GetCellData().GetArray("Quality")
        mq = vtk_to_numpy(mq)
        try:
            #tet with edge ratio closest to 1
            btet = np.argmin(abs(mq - 1.0))
        except:
            return
        idlist = tet.GetOutput().GetCell(btet).GetPointIds()
        P = np.zeros((4, 3), float)
        p = np.zeros((4, 3), float)
        for i in xrange(idlist.GetNumberOfIds()):
            P[i, :] = rc[idlist.GetId(i), :]
            p[i, :] = dc[idlist.GetId(i), :]
        X = np.array([P[1, :] - P[0, :],
                      P[2, :] - P[0, :],
                      P[3, :] - P[0, :],
                      P[3, :] - P[1, :],
                      P[3, :] - P[2, :],
                      P[2, :] - P[1, :]], float)

        x = np.array([p[1, :] - p[0, :],
                      p[2, :] - p[0, :],
                      p[3, :] - p[0, :],
                      p[3, :] - p[1, :],
                      p[3, :] - p[2, :],
                      p[2, :] - p[1, :]], float)

        #assemble the system
        dX = np.zeros((6, 6), float)
        ds = np.zeros((6, 1), float)
        for i in xrange(6):
            dX[i, 0] = 2 * X[i, 0] ** 2
            dX[i, 1] = 2 * X[i, 1] ** 2
            dX[i, 2] = 2 * X[i, 2] ** 2
            dX[i, 3] = 4 * X[i, 0] * X[i, 1]
            dX[i, 4] = 4 * X[i, 0] * X[i, 2]
            dX[i, 5] = 4 * X[i, 1] * X[i, 2]

            ds[i, 0] = np.linalg.norm(
                x[i, :]) ** 2 - np.linalg.norm(X[i, :]) ** 2

        E = np.linalg.solve(dX, ds)
        E = np.array([[E[0, 0], E[3, 0], E[4, 0]],
                      [E[3, 0], E[1, 0], E[5, 0]],
                      [E[4, 0], E[5, 0], E[2, 0]]], float)
        self.ecm_strain = E

    def _make3Dmesh(self, filename, frame):
        """
        Generates a 3-D tetrahedral mesh from a polygonal surface using TETGEN
        wrapped by MeshPy.
        These meshes are then used to determine the object's volume, centroid,
        and the axes of the ellipsoid that has equivalent principal moments of
        inertia.

        Parameters
        ----------
        filename : str
            The path and filename of the STL surface currently being analyzed. This
            is necessary since MeshPy has to read the STL from disk in its native format.
        frame : str
            Indicates the curent state, 'MATERIAL' or 'SPATIAL'.

        Returns
        -------
        **if frame=='MATERIAL'**
            * rmeshes
            * rcentroids
            * rvols
            * raxes
        **else**
            * dcentroids
            * dvols
            * daxes
        """
        s = MeshInfo()
        s.load_stl(filename)
        #use TETGEN to generate mesh
        #switches:
        # p -
        # q - refine mesh to improve quality
        #     1.2 minimum edge ratio
        #     minangle=15
        # Y - do not edit surface mesh
        # O - perform mesh optimization
        #     optlevel=9
        mesh = build(s, options=Options("pq1.2YO",
                                        optlevel=9))
        elements = list(mesh.elements)
        nodes = list(mesh.points)
        faces = np.array(mesh.faces)
        s_nodes = list(np.unique(np.ravel(faces)))

        ntmp = np.array(nodes, np.float64)
        arr = numpy_to_vtk(ntmp.ravel(), deep=True, array_type=vtk.VTK_DOUBLE)
        arr.SetNumberOfComponents(3)
        tetraPoints = vtk.vtkPoints()
        tetraPoints.SetData(arr)

        vtkMesh = vtk.vtkUnstructuredGrid()
        vtkMesh.Allocate(len(elements), len(elements))
        vtkMesh.SetPoints(tetraPoints)

        e = np.array(elements, np.uint32) - 1
        e = np.hstack((np.ones((e.shape[0], 1), np.uint32) * 4, e))

        arr = numpy_to_vtk(e.ravel(), deep=True,
                           array_type=vtk.VTK_ID_TYPE)

        tet = vtk.vtkCellArray()
        tet.SetCells(e.size / 5, arr)

        vtkMesh.SetCells(10, tet)

        n1 = ntmp[e[:, 1], :]
        n2 = ntmp[e[:, 2], :]
        n3 = ntmp[e[:, 3], :]
        n4 = ntmp[e[:, 4], :]
        tetraCents = (n1 + n2 + n3 + n4) / 4.0
        e1 = n4 - n1
        e2 = n3 - n1
        e3 = n2 - n1
        tetraVols = np.einsum('...j,...j',
                              e1, np.cross(e2, e3, axis=1)) / 6.0
        tetraVols = np.abs(tetraVols.ravel())

        totalVol = np.sum(tetraVols)
        centroid = np.sum(tetraVols[:, None] / totalVol * tetraCents, axis=0)
        tetraCents -= centroid

        I = np.r_[tetraCents[:, 1] ** 2 + tetraCents[:, 2] ** 2,
                  -tetraCents[:, 0] * tetraCents[:, 1],
                  -tetraCents[:, 0] * tetraCents[:, 2],
                  tetraCents[:, 0] ** 2 + tetraCents[:, 2] ** 2,
                  -tetraCents[:, 1] * tetraCents[:, 2],
                  tetraCents[:, 0] ** 2 + tetraCents[:, 1] ** 2]
        I = np.reshape(I, (tetraVols.size, 6), order='F')
        I *= tetraVols[:, None]
        I = np.sum(I, axis=0)
        I = np.array([[I[0], I[1], I[2]],
                      [I[1], I[3], I[4]],
                      [I[2], I[4], I[5]]])
        w, v = np.linalg.eigh(I)
        order = np.argsort(w)
        w = w[order]
        v = v[order]
        r_major = np.sqrt(5 * (w[1] + w[2] - w[0]) / (2 * totalVol))
        r_middle = np.sqrt(5 * (w[0] - w[1] + w[2]) / (2 * totalVol))
        r_minor = np.sqrt(5 * (w[0] + w[1] - w[2]) / (2 * totalVol))
        if frame == 'MATERIAL':
            self.rmeshes.append(vtkMesh)
            self._snodes.append(s_nodes)
            self._elements.append(elements)
            self._nodes.append(nodes)
            self.raxes.append([r_major, r_middle, r_minor])
            self.rcentroids.append(centroid)
            self.rvols.append(totalVol)
        else:
            self.daxes.append([r_major, r_middle, r_minor])
            self.dcentroids.append(centroid)
            self.dvols.append(totalVol)

    def _poly2img(self, ind):
        dim = int(np.ceil(1.0 / self.deformableSettings['Precision'])) + 10
        rpoly = vtk.vtkPolyData()
        rpoly.DeepCopy(self.rsurfs[ind])
        dpoly = self.dsurfs[ind]
        if self.rigidInitial:
            rot = vtk.vtkTransformPolyDataFilter()
            rot.SetInputData(rpoly)
            rot.SetTransform(self.rigidTransforms[ind])
            rot.Update()
            rpoly = rot.GetOutput()

        rbounds = np.zeros(6, np.float32)
        dbounds = np.copy(rbounds)

        rpoly.GetBounds(rbounds)
        dpoly.GetBounds(dbounds)

        spacing = np.zeros(3, np.float32)
        for i in xrange(3):
            rspan = rbounds[2 * i + 1] - rbounds[2 * i]
            dspan = dbounds[2 * i + 1] - dbounds[2 * i]
            spacing[i] = (np.max([rspan, dspan])
                          * self.deformableSettings['Precision'])

        imgs = []
        half = float(dim) / 2.0
        for i in xrange(2):
            arr = np.ones((dim, dim, dim), np.uint8)
            arr2img = vti.vtkImageImportFromArray()
            arr2img.SetDataSpacing(spacing)
            arr2img.SetDataExtent((0, dim - 1, 0, dim - 1, 0, dim - 1))
            arr2img.SetArray(arr)
            arr2img.Update()
            if i == 0:
                rimg = arr2img.GetOutput()
                rimg.SetOrigin((np.mean(rbounds[0:2]) -
                                half * spacing[0] + spacing[0] / 2,
                                np.mean(rbounds[2:4]) -
                                half * spacing[1] + spacing[1] / 2,
                                np.mean(rbounds[4:]) -
                                half * spacing[2] + spacing[2] / 2))
            else:
                dimg = arr2img.GetOutput()
                dimg.SetOrigin((np.mean(dbounds[0:2]) -
                                half * spacing[0] + spacing[0] / 2,
                                np.mean(dbounds[2:4]) -
                                half * spacing[1] + spacing[1] / 2,
                                np.mean(dbounds[4:]) -
                                half * spacing[2] + spacing[2] / 2))
        imgs = []
        for (pd, img) in [(rpoly, rimg), (dpoly, dimg)]:
            pol2stenc = vtk.vtkPolyDataToImageStencil()
            pol2stenc.SetInputData(pd)
            pol2stenc.SetOutputOrigin(img.GetOrigin())
            pol2stenc.SetOutputSpacing(img.GetSpacing())
            pol2stenc.SetOutputWholeExtent(img.GetExtent())
            pol2stenc.SetTolerance(0.0001)
            pol2stenc.Update()

            imgstenc = vtk.vtkImageStencil()
            imgstenc.SetInputData(img)
            imgstenc.SetStencilConnection(pol2stenc.GetOutputPort())
            imgstenc.ReverseStencilOff()
            imgstenc.SetBackgroundValue(0)
            imgstenc.Update()

            arr = vtk_to_numpy(imgstenc.GetOutput().GetPointData().GetArray(0))
            arr = arr.reshape(dim, dim, dim)
            itk_img = sitk.GetImageFromArray(arr)
            itk_img.SetSpacing(img.GetSpacing())
            itk_img.SetOrigin(img.GetOrigin())
            imgs.append(itk_img)
        return (imgs[0], imgs[1], rpoly)

    def animate(self, pd, ind):
        """
        Helper function called by **deformableRegistration** if **animate** is *True*.
        Spawns a window with an interactive 3-D rendering of the current analyzed object
        in its reference state. The displacements calculated from the deformable image
        registration can be applied to this object to animate the deformation by pressing
        the RIGHT-ARROW. Pressing the UP-ARROW will animate and also save the frames to
        disk.

        Parameters
        ----------
        pd : vtkPolyData
            The current analyzed object's reference geometry.
        ind : int
            The index of the current polydata in **rsurfs**. Necessary for naming directory created
            if animation frames are saved.
        """
        pd.GetPointData().SetActiveVectors("Displacement")

        class vtkTimerCallback():
            def __init__(self):
                self.timer_count = 0

            def execute(self, obj, event):
                if self.timer_count == 10:
                    self.timer_count = 0
                warpVector = vtk.vtkWarpVector()
                warpVector.SetInputData(pd)
                warpVector.SetScaleFactor(0.1 * (self.timer_count + 1))
                warpVector.Update()
                poly = warpVector.GetPolyDataOutput()
                getScalars = vtk.vtkExtractVectorComponents()
                getScalars.SetInputData(poly)
                getScalars.Update()

                vectorNorm = vtk.vtkVectorNorm()
                vectorNorm.SetInputData(poly)
                vectorNorm.Update()

                scalars = []
                scalars.append(
                    getScalars.GetVzComponent())
                scalars.append(
                    vectorNorm.GetOutput())
                scalars.append(
                    getScalars.GetVxComponent())
                scalars.append(
                    getScalars.GetVyComponent())

                names = ("Z", "Mag", "X", "Y")
                for k, a in enumerate(self.actors):
                    calc = vtk.vtkArrayCalculator()
                    scalars[k].GetPointData().GetScalars().SetName(names[k])
                    calc.SetInputData(scalars[k])
                    calc.AddScalarArrayName(names[k])
                    calc.SetResultArrayName(names[k])
                    calc.SetFunction(
                        "%s * 0.1 * %f" % (names[k], self.timer_count + 1))
                    calc.Update()
                    mapper = vtk.vtkPolyDataMapper()
                    mapper.SetInputData(calc.GetOutput())
                    mapper.SetScalarRange(calc.GetOutput().GetScalarRange())
                    mapper.SetScalarModeToUsePointData()
                    mapper.SetColorModeToMapScalars()
                    mapper.Update()
                    a.SetMapper(mapper)
                    cb.scalar_bars[k].SetLookupTable(mapper.GetLookupTable())

                iren = obj
                iren.GetRenderWindow().Render()
                time.sleep(0.3)

                if self.key == "Up":
                    try:
                        os.mkdir(self.directory)
                    except:
                        pass
                    w2i = vtk.vtkWindowToImageFilter()
                    w2i.SetInput(obj.GetRenderWindow())
                    w2i.Update()

                    png = vtk.vtkPNGWriter()
                    png.SetInputConnection(w2i.GetOutputPort())
                    png.SetFileName(self.directory + os.sep +
                                    "frame{:d}.png".format(self.timer_count))
                    png.Update()
                    png.Write()

                self.timer_count += 1

            def Keypress(self, obj, event):
                self.key = obj.GetKeySym()
                if self.key == "Right" or self.key == "Up":
                    for i in xrange(10):
                        obj.CreateOneShotTimer(1)

        renwin = vtk.vtkRenderWindow()

        iren = vtk.vtkRenderWindowInteractor()
        iren.SetRenderWindow(renwin)
        iren.Initialize()
        cb = vtkTimerCallback()

        xmins = (0, 0.5, 0, 0.5)
        xmaxs = (0.5, 1, 0.5, 1)
        ymins = (0, 0, 0.5, 0.5)
        ymaxs = (0.5, 0.5, 1, 1)
        titles = ('Z Displacement', 'Magnitude',
                  'X Displacement', 'Y Displacement')
        cb.actors = []
        cb.scalar_bars = []
        cb.directory = str(os.path.normpath(
            self._def_dir + os.sep + "animation{:0d}".format(ind + 1)))

        warpVector = vtk.vtkWarpVector()
        warpVector.SetInputData(pd)
        warpVector.Update()
        poly = warpVector.GetPolyDataOutput()

        getScalars = vtk.vtkExtractVectorComponents()
        getScalars.SetInputData(poly)
        getScalars.Update()

        vectorNorm = vtk.vtkVectorNorm()
        vectorNorm.SetInputData(poly)
        vectorNorm.Update()

        scalars = []
        scalars.append(
            getScalars.GetVzComponent())
        scalars.append(
            vectorNorm.GetOutput())
        scalars.append(
            getScalars.GetVxComponent())
        scalars.append(
            getScalars.GetVyComponent())
        bounds = np.zeros(6, np.float32)
        pd.GetBounds(bounds)
        length = np.min(bounds[1::2] - bounds[0:-1:2]) * 0.2
        bounds[1] = bounds[0] + length
        bounds[3] = bounds[2] + length
        bounds[5] = bounds[4] + length
        for j in xrange(4):
            mapper = vtk.vtkPolyDataMapper()
            mapper.SetInputData(scalars[j])
            mapper.SetScalarRange(scalars[j].GetScalarRange())
            mapper.SetScalarModeToUsePointData()
            mapper.SetColorModeToMapScalars()

            scalar_bar = vtk.vtkScalarBarActor()
            scalar_bar.SetLookupTable(mapper.GetLookupTable())
            scalar_bar.SetTitle(titles[j])
            scalar_bar.SetLabelFormat("%3.3f")
            cb.scalar_bars.append(scalar_bar)

            actor = vtk.vtkActor()
            actor.SetMapper(mapper)
            cb.actors.append(actor)

            renderer = vtk.vtkRenderer()
            renderer.SetBackground(0., 0., 0.)
            renwin.AddRenderer(renderer)
            if j == 0:
                camera = renderer.GetActiveCamera()
            else:
                renderer.SetActiveCamera(camera)

            triad = vtk.vtkCubeAxesActor()
            triad.SetCamera(camera)
            triad.SetFlyModeToStaticTriad()
            triad.SetBounds(bounds)
            triad.GetXAxesLinesProperty().SetColor(1.0, 0.0, 0.0)
            triad.GetYAxesLinesProperty().SetColor(0.0, 1.0, 0.0)
            triad.GetZAxesLinesProperty().SetColor(0.0, 0.0, 1.0)
            triad.GetXAxesLinesProperty().SetLineWidth(3.0)
            triad.GetYAxesLinesProperty().SetLineWidth(3.0)
            triad.GetZAxesLinesProperty().SetLineWidth(3.0)
            triad.XAxisLabelVisibilityOff()
            triad.YAxisLabelVisibilityOff()
            triad.ZAxisLabelVisibilityOff()
            triad.XAxisTickVisibilityOff()
            triad.YAxisTickVisibilityOff()
            triad.ZAxisTickVisibilityOff()
            triad.XAxisMinorTickVisibilityOff()
            triad.YAxisMinorTickVisibilityOff()
            triad.ZAxisMinorTickVisibilityOff()

            renderer.SetViewport(xmins[j], ymins[j],
                                 xmaxs[j], ymaxs[j])
            renderer.AddActor(actor)
            renderer.AddActor2D(scalar_bar)
            renderer.AddActor(triad)
            renderer.ResetCamera()
            renwin.Render()

        iren.AddObserver('TimerEvent', cb.execute)
        iren.AddObserver('KeyPressEvent', cb.Keypress)

        iren.Start()
