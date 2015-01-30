import os, re, string, warnings, platform, vtk, fnmatch
import numpy as np
from vtk.util import vtkImageExportToArray as vte
from vtk.util import vtkImageImportFromArray as vti
from sklearn import svm,preprocessing
import SimpleITK as sitk

class Volume(object):
    """
    DESCRIPTION
    This class will segment objects from 3-D images using user-specified routines. The intended purpose is for laser scanning fluorescence
    microscopy of chondrocytes and/or their surrounding matrices. Nevertheless, this can be generalized to any 3-D object using any
    imaging modality; however, it is likely the segmentation parameters will need to be adjusted. Therefore, in this case, the user should set
    segmentation='User' during Class instantiation, and call the segmentaion method with appropriate parameters.

    Attributes:
        cells        An image containing the segmented objects as integer labels. Has the same properties as the input image stack.
        volumes      List of the physical volumes of the segmented objects.
        centroids    List of centroids of segmented objects in physical space.
        surfaces     List containing VTK STL objects.
        dimensions   List containing the ellipsoid axis lengths of segmented objects.
        orientations List containing the basis vectors of ellipsoid axes. Same order as dimensions.
    """
    def __init__(self,vol_dir,output_dir=None,regions=None,pixel_dim=[0.411,0.411,0.6835],stain='Foreground',segmentation='Geodesic',smoothing_method='Curvature Diffusion',smoothing_parameters={},stretch=False,enhance_edge=False,display=True,handle_overlap=True,debug=False,fillholes=True):
        """
        INPUTS
        vol_dir              TYPE: string. This is required. Currently it is the path to a directory containing a stack of TIFF images. Other formats may be supported in the future.
        output_dir           TYPE: string. Directory to write STL surfaces to. If not specifed, will create a directory vol_dir+'_results'.
        regions              TYPE: list of form [[pixel coordinate x, y, z, box edge length x, y, z],[...]]. If not specified, assumes whole image region.
        pixel_dim            TYPE: [float, float, float]. The physical dimensions of the voxels in the image.
        stain                TYPE: string. Indicates if the object to be segmented is the foreground or the background.
        segmentation         TYPE: string. Execute indicated segmentation using default values.
                             'User'      The user will invoke the segmentation method by calling the function. This allows for parameter specification.
                             'Threshold' Thresholds based on a user-supplied percentage of the maximum voxel intensity. See thresholdSegmentation for other methods available if 'User' is indicated.
                             'Geodesic'  Uses a geodesic active contour levelset.
                             'EdgeFree'  Uses an edge free active contour model.
        smoothing_method     TYPE: string. Smoothing method to use on regions of interest.
        smoothing_parameters TYPE: dictionary. Change smoothing parameters of smoothing method from default by passing a dictionary with key and new value.
                             Dictionary Defaults by Method:
                             'Gaussian':            {'sigma': 0.5}
                             'Median':              {'radius': (1,1,1)}
                             'Curvature Diffusion': {'iterations': 10, 'conductance': 9}
                             'Gradient Diffusion':  {'iterations': 10, 'conductance': 9, 'time step': 0.01}
                             'Bilateral':           {'domainSigma': 1.5, 'rangeSigma': 5.0, 'samples': 100}
                             'Patch-based':         {'radius':4, 'iterations':10, 'patches':20, 'noise model': 'poisson'}
        stretch              TYPE: Boolean. Whether to do contrast stretching of 2D slices in regions of interest after to smoothing.
        enhance_edge         TYPE: Boolean. Whether to enhance the edges with Laplacian sharpening
        display              TYPE: Boolean. Spawn a window to render the cells or not.
        handle_overlap       TYPE: Boolean. If labelled objects overlap, employs Support Vector Machines to classify the shared voxels.
        debug                TYPE: Boolean. If True, the following images depending on the segmentation method will be output to the output_dir.
                             thresholdSegmentation:
                                 smoothed region of interest image as smoothed_[region id].nii e.g. smoothed_001.nii
                             edgeFreeSegmentation:
                                 All of the above plus: seed image for each region as seed_[region id].nii e.g. seed_001.nii
                             geodesicSegmentation:
                                 All of the above plus: edge map image for each region as edge_[region id].nii e.g. edge_001.nii 
        fillholes            TYPE: Boolean. If True, holes fully within the segmented object will be filled.
        """
        # check what OS we are running on
        op_sys = platform.platform()
        if 'Windows' in op_sys:
            self._path_dlm = '\\'
        elif 'Linux' in op_sys:
            self._path_dlm = '/'
        else:
            print('WARNING: This module is untested on your operating system.')
            self._path_dlm = '/'
        warnings.filterwarnings("ignore")

        self._vol_dir = vol_dir

        if output_dir is None:
            self._output_dir = vol_dir+'_results'
        else:
            self._output_dir = output_dir
        
        self._pixel_dim = pixel_dim
        self._stain = stain
        self.display = display
        self._img = None
        self._imgType = None
        self._imgTypeMax = None
        self.handle_overlap = handle_overlap
        self.smoothing_method = smoothing_method
        self.smoothing_parameters = smoothing_parameters
        self.stretch = stretch
        self.enhance_edge = enhance_edge
        self.debug = debug
        if self.debug:
            for p in ['seed*.nii','smoothed*.nii','edge*.nii']:
                files = fnmatch.filter(os.listdir(self._output_dir),p)
                for f in files:
                    os.remove(self._output_dir+self._path_dlm+f)
                
        self.fillholes = fillholes
        # read in the TIFF stack        
        self._parseStack()
        # define a blank image with the same size and spacing as image stack to add segmented cells to
        self.cells = sitk.Image(self._img.GetSize(),self._imgType)
        self.cells.SetSpacing(self._img.GetSpacing())
        self.cells.SetOrigin(self._img.GetOrigin())
        self.cells.SetDirection(self._img.GetDirection())

        self.surfaces = []
        # if regions are not specified, assume there is only one cell and default to whole image
        if regions is None:
            self._regions = [map(int,list(self._img.GetOrigin()))+list(self._img.GetSize())]
        else:
            self._regions = regions

        self.volumes = []
        self.centroids = []
        self.dimensions = []
        
        #Execute segmentation with default parameters unless specified as 'User'
        if segmentation=='Threshold':
            self.thresholdSegmentation()
        elif segmentation=='Entropy':
            self.entropySegmentation()
        elif segmentation=='Geodesic':
            self.geodesicSegmentation()
        elif segmentation=='EdgeFree':
            self.edgeFreeSegmentation()
        elif segmentation=='User':
            pass
        else:
            raise SystemExit('{:s} is not a supported segmentation method.'.format(segmentation))

        try:
            os.mkdir(self._output_dir)
        except:
            pass
        
        sitk.WriteImage(self._img,self._output_dir+self._path_dlm+'stack.nii')

    def _parseStack(self):
        reader = sitk.ImageFileReader()
        files = fnmatch.filter(sorted(os.listdir(self._vol_dir)),'*.tif')
        if len(files) > 1:
            counter = [re.search("[0-9]*\.tif",f).group() for f in files]
            for i,c in enumerate(counter):
                counter[i] = int(c.replace('.tif',''))
            files = np.array(files,dtype=object)
            sorter = np.argsort(counter)
            files = files[sorter]
            img = []
            for fname in files:
                reader.SetFileName(self._vol_dir+self._path_dlm+fname)
                im = reader.Execute()
                if 'vector' in string.lower(im.GetPixelIDTypeAsString()):
                    img.append(sitk.VectorMagnitude(im))
                else:
                    img.append(im)
            self._img = sitk.JoinSeries(img)
            print("\nImported 3D image stack ranging from {:s} to {:s}".format(files[0],files[-1]))
        else:
            print("\nImported 2D image {:s}".format(files[0]))
            reader.SetFileName(self._vol_dir+self._path_dlm+files[0])
            self._img = reader.Execute()
                
        self._imgType = self._img.GetPixelIDValue()
        if self._imgType == 1:
            self._imgTypeMax = 255
        elif self._imgType == 3:
            self._imgTypeMax = 65535
        elif self._imgType == 0:
            print 'WARNING: Given a 12-bit image; this has been converted to 16-bit.'
            self._imgType = 3
            self._imgTypeMax = 65535

        self._img = sitk.Cast(self._img,self._imgType)
        self._img.SetSpacing(self._pixel_dim)

    def equalize2D(self,img):
        maxt = self._getMinMax(img)[1]
        if self._img.GetDimension() == 3:
            size = img.GetSize()
            slices = []
            ucutoffs = np.zeros((size[2],),int)
            lcutoffs = np.zeros((size[2],),int)
            for i in xrange(size[2]):
                s = sitk.Extract(img,[size[0],size[1],0],[0,0,i])
                a = sitk.GetArrayFromImage(s)
                ucutoffs[i] = np.percentile(a,98)
                lcutoffs[i] = np.percentile(a,2)
                slices.append(s)
            u10 = np.percentile(ucutoffs,10)
            ucutoffs[ucutoffs < u10] = self._imgTypeMax
            for i,s in enumerate(slices):
                slices[i] = sitk.IntensityWindowing(s,lcutoffs[i],ucutoffs[i],0,self._imgTypeMax)
            newimg = sitk.JoinSeries(slices)
            newimg.SetOrigin(img.GetOrigin())
            newimg.SetSpacing(img.GetSpacing())
            newimg.SetDirection(img.GetDirection())
        else:
            a = sitk.GetArrayFromImage(img)
            ucutoff = np.percentile(a,98)
            lcutoff = np.percentile(a,2)
            newimg = sitk.IntensityWindowing(img,lcutoff,ucutoff,0,self._imgTypeMax)
        return sitk.Cast(newimg,self._imgType)

    def smoothRegion(self,img):
        img = sitk.Cast(img,sitk.sitkFloat32)

        if self.smoothing_method == 'None':
            pass
        elif self.smoothing_method == 'Gaussian':
            parameters = {'sigma':0.5}
            for p in self.smoothing_parameters.keys():
                try:
                    parameters[p] = self.smoothing_parameters[p]
                except:
                    raise SystemExit("{:s} is not a parameter of {:s}".format(p,self.smoothing_method))
            img = sitk.DiscreteGaussian(img,variance=parameters['sigma'])

        elif self.smoothing_method == 'Median':
            parameters = {'radius':(1,1,1)}
            for p in self.smoothing_parameters.keys():
                try:
                    parameters[p] = self.smoothing_parameters[p]
                except:
                    raise SystemExit("{:s} is not a parameter of {:s}".format(p,self.smoothing_method))
            img = sitk.Median(img,radius=parameters['radius'])

        elif self.smoothing_method == 'Curvature Diffusion':
            parameters = {'iterations': 10, 'conductance': 9}
            for p in self.smoothing_parameters.keys():
                try:
                    parameters[p] = self.smoothing_parameters[p]
                except:
                    raise SystemExit("{:s} is not a parameter of {:s}".format(p,self.smoothing_method))
            smooth = sitk.CurvatureAnisotropicDiffusionImageFilter()
            smooth.EstimateOptimalTimeStep(img)
            smooth.SetNumberOfIterations(parameters['iterations'])
            smooth.SetConductanceParameter(parameters['conductance'])
            img = smooth.Execute(img)

        elif self.smoothing_method == 'Gradient Diffusion':
            parameters = {'iterations': 10, 'conductance': 9, 'time step': 0.01}
            for p in self.smoothing_parameters.keys():
                try:
                    parameters[p] = self.smoothing_parameters[p]
                except:
                    raise SystemExit("{:s} is not a parameter of {:s}".format(p,self.smoothing_method))
            smooth = sitk.GradientAnisotropicDiffusionImageFilter()
            smooth.SetNumberOfIterations(parameters['iterations'])
            smooth.SetConductanceParameter(parameters['conductance'])
            smooth.SetTimeStep(parameters['time step'])
            img = smooth.Execute(img)

        elif self.smoothing_method == 'Bilateral':
            parameters = {'domainSigma': 1.5, 'rangeSigma': 10.0, 'samples': 100}
            for p in self.smoothing_parameters.keys():
                try:
                    parameters[p] = self.smoothing_parameters[p]
                except:
                    raise SystemExit("{:s} is not a parameter of {:s}".format(p,self.smoothing_method))
            img = sitk.Bilateral(img,domainSigma=parameters['domainSigma'],rangeSigma=parameters['rangeSigma'],numberOfRangeGaussianSamples=parameters['samples'])

        elif self.smoothing_method == 'Patch-based':
            parameters = {'radius':4, 'iterations':10, 'patches':20, 'noise model': 'poisson'}
            noise_models = {'nomodel': 0, 'gaussian': 1, 'rician': 2, 'poisson': 3}
            for p in self.smoothing_parameters.keys():
                try:
                    if p == 'noise model':
                        parameters[p] = noise_models[self.smoothing_parameters[p]]
                    else:
                        parameters[p] = self.smoothing_parameters[p]
                except:
                    raise SystemExit("{:s} is not a parameter of {:s}".format(p,self.smoothing_method))
            smooth = sitk.PatchBasedDenoisingImageFilter()
            smooth.KernelBandwidthEstimationOn()
            smooth.SetNoiseModel(parameters['noise model'])
            smooth.SetNoiseModelFidelityWeight(1.0)
            smooth.SetNumberOfSamplePatches(parameters['patches'])
            smooth.SetPatchRadius(parameters['radius'])
            smooth.SetNumberOfIterations(parameters['iterations'])
            img = smooth.Execute(img)

        else:
            raise SystemExit("ERROR: {:s} is not a supported smoothing method. Options are: 'None', 'Gaussian', 'Median', 'Curvature Diffusion', 'Gradient Diffusion', 'Bilateral', or 'Patch-based'.".format(self.smoothing_method))
        #enhance the edges
        if self.enhance_edge:
            img = sitk.LaplacianSharpening(img)
        # do contrast stretching on 2D slices if set to True
        if self.stretch:
            img = self.equalizeImage2D(sitk.Cast(img,self._imgType))        
        return sitk.Cast(img,sitk.sitkFloat32)
        
    def _getMinMax(self,img):
        mm = sitk.MinimumMaximumImageFilter()
        mm.Execute(img)
        return (mm.GetMinimum(),mm.GetMaximum())

    def _getLabelShape(self,img):
        ls = sitk.LabelShapeStatisticsImageFilter()
        ls.Execute(img)
        labels = ls.GetLabels()
        labelshape = {'volume': [],
                      'centroid': [],
                      'ellipsoid diameters': [],
                      'bounding box': []}
        for l in labels:
            labelshape['volume'].append(ls.GetPhysicalSize(l))
            labelshape['centroid'].append(ls.GetCentroid(l))
            labelshape['ellipsoid diameters'].append(ls.GetEquivalentEllipsoidDiameter(l))
            labelshape['bounding box'].append(ls.GetBoundingBox(l))
        return labelshape
        
    def thresholdSegmentation(self,method='Percentage',adaptive=True,ratio=0.4):
        """
        DESCRIPTION
        Segments image based on a specified percentage of the maximum voxel intensity in the specified region of interest.
        For the case of multiple objects in the region, saves only the object with the greatest volume.

        INPUTS
        method     TYPE: string. The thresholding method to use.
                   OPTIONS
                   'Percentage'  Threshold at percentage of the maximum voxel intensity.
                   'Otsu'
                   For more information on the following consult http://www.insight-journal.org/browse/publication/811 and cited original sources.
                   'Huang'       Fuzzy thresholding using Shannon entropy function.
                   'IsoData'     Also known as Ridler-Calvard.
                   'Li'
                   'MaxEntropy'
                   'KittlerIllingworth'
                   'Moments'
                   'Yen'
                   'RenyiEntropy'
                   'Shanbhag'
        ratio      TYPE: float. Percentage to threshold at if using 'Percentage' method.
        adaptive   TYPE: Boolean. Whether to adaptively adjust initial threshold until foreground does not touch the region boundaries. 
               
        """
        if method not in ['Percentage','Otsu','Huang','IsoData','Li','MaxEntropy','KittlerIllingworth','Moments','Yen','RenyiEntropy','Shanbhag']:
            raise SystemExit("{:s} is not a supported threshold method.".format(method))
        dimension = self._img.GetDimension()
        for i,region in enumerate(self._regions):
            if dimension == 3:
                roi = sitk.RegionOfInterest(self._img,region[3:],region[0:3])
            else:
                roi = sitk.RegionOfInterest(self._img,region[2:],region[0:2])
            simg = self.smoothRegion(roi)
            if self.debug:
                sitk.WriteImage(sitk.Cast(simg,self._imgType),self._output_dir+self._path_dlm+"smoothed_{:03d}.nii".format(i+1))
            print("\n------------------")
            print("Segmenting Cell {:d}".format(i+1))
            print("------------------\n")

            if method == 'Percentage':
                t = self._getMinMax(simg)[1]
                if self._stain == 'Foreground':
                    t *= ratio
                    seg = sitk.BinaryThreshold(simg,t,1e7)
                elif self._stain == 'Background':
                    t *= (1.0-ratio)
                    seg = sitk.BinaryThreshold(simg,0,t)
                else:
                    raise SystemExit("Unrecognized value for 'stain', {:s}. Options are 'Foreground' or 'Background'".format(self._stain))
                print("... Thresholded using {:s} method at a value of: {:d}".format(method,int(t)))

            elif method == 'Otsu':
                thres = sitk.OtsuThresholdImageFilter()

            elif method == 'Huang':
                thres = sitk.HuangThresholdImageFilter()

            elif method == 'IsoData':
                thres = sitk.IsoDataThresholdImageFilter()

            elif method == 'Li':
                thres = sitk.LiThresholdImageFilter()

            elif method == 'MaxEntropy':
                thres = sitk.MaximumEntropyThresholdImageFilter()

            elif method == 'KittlerIllingworth':
                thres = sitk.KittlerIllingworthThresholdImageFilter()

            elif method == 'Moments':
                thres = sitk.MomentsThresholdImageFilter()

            elif method == 'Yen':
                thres = sitk.YenThresholdImageFilter()

            elif method == 'RenyiEntropy':
                thres = sitk.RenyiEntropyThresholdImageFilter()

            elif method == 'Shanbhag':
                thres = sitk.ShanbhagThresholdImageFilter()

            else:
                raise SystemExit("Unrecognized value for 'stain', {:s}. Options are 'Foreground' or 'Background'".format(self._stain))

            if not(method=='Percentage'):
                thres.SetNumberOfHistogramBins((self._imgTypeMax+1)/2)
                if self._stain == 'Foreground':
                    thres.SetInsideValue(0)
                    thres.SetOutsideValue(1)
                elif self._stain == 'Background':
                    thres.SetInsideValue(1)
                    thres.SetOutsideValue(0)
                seg = thres.Execute(simg)
                t = thres.GetThreshold()
                print("... Threshold determined by {:s} method: {:d}".format(method,int(t)))

            if adaptive:
                newt = np.copy(t)
                if dimension == 3:
                    region_bnds = [(0,region[3]),(0,region[4])]
                else:
                    region_bnds = [(0,region[2]),(0,region[3])]
                while True:
                    #Opening (Erosion/Dilation) step to remove islands smaller than 1 voxels in radius)
                    seg = sitk.BinaryMorphologicalOpening(seg,1)
                    if self.fillholes:
                        seg = sitk.BinaryFillhole(seg!=0)
                    #Get connected regions
                    r = sitk.ConnectedComponent(seg)
                    labelstats = self._getLabelShape(r)
                    label = np.argmax(labelstats['volume'])+1
                    bb = labelstats['bounding box'][label-1]
                    if dimension == 3:
                        label_bounds = [(bb[0],bb[0]+bb[3]),(bb[1],bb[1]+bb[4])]
                    else:
                        label_bounds = [(bb[0],bb[0]+bb[2]),(bb[1],bb[1]+bb[3])]
                    if np.any(np.intersect1d(region_bnds[0],label_bounds[0])) or \
                       np.any(np.intersect1d(region_bnds[1],label_bounds[1])):
                        if self._stain == 'Foreground':
                            newt += 0.01*t
                            seg = sitk.BinaryThreshold(simg,int(newt),1e7)
                        elif self._stain == 'Background':
                            newt -= 0.01*t
                            seg = sitk.BinaryThreshold(simg,0,int(newt))
                    else:
                        break
                if not(newt == t):
                    print("... ... Adjusted the threshold to: {:d}".format(int(newt)))
            else:
                #Opening (Erosion/Dilation) step to remove islands smaller than 1 voxels in radius)
                seg = sitk.BinaryMorphologicalOpening(seg,1)
                if self.fillholes:
                    seg = sitk.BinaryFillhole(seg!=0)
                #Get connected regions
                r = sitk.ConnectedComponent(seg)
                labelstats = self._getLabelShape(r)
                label = np.argmax(labelstats['volume'])+1
                
            tmp = sitk.Image(self._img.GetSize(),self._imgType)
            tmp.SetSpacing(self._img.GetSpacing())
            tmp.SetOrigin(self._img.GetOrigin())
            tmp.SetDirection(self._img.GetDirection())
            resampler = sitk.ResampleImageFilter()
            resampler.SetReferenceImage(tmp)
            resampler.SetInterpolator(sitk.sitkNearestNeighbor)
            tmp = resampler.Execute((r==label)*(i+1))
            self.cells = sitk.Add(self.cells,sitk.Cast(tmp,self._imgType))
            #Test for overlap
            if self.handle_overlap:
                maxlabel = self._getMinMax(self.cells)[1]
                if maxlabel > (i+1):
                    self.cells = self._classifyShared(i,self.cells,False)

    def geodesicSegmentation(self,upsampling=2,seed_method='Percentage',ratio=0.7,canny_variance=(0.5,0.5,0.5),propagation=0.3,curvature=0.1,advection=1.0,rms=0.005,active_iterations=200):
        """
        DESCRIPTION
        Performs a segmentation using the SimpleITK implementation of the Geodesic Active Contour Levelset Segmentation method described in (Caselles et al. 1997.)
        Please also consult SimpleITK's documentation of GeodesicActiveContourLevelSetImageFilter.
        This method will establish initial levelsets by calling the entropySegmentation() method. 

        INPUTS
        upsampling           TYPE: integer. Resample image splitting original voxels into this many. NOTE - Resampling will always be performed to make voxels isotropic.
        seed_method          TYPE: string. Method used to determine seed image. Same as thresholdSegmentation method variable.
                             OPTIONS
                             'Percentage'  Threshold at percentage of the maximum voxel intensity.
                             'Otsu'
                             For more information on the following consult http://www.insight-journal.org/browse/publication/811 and cited original sources.
                             'Huang'       Fuzzy thresholding using Shannon entropy function.
                             'IsoData'     Also known as Ridler-Calvard.
                             'Li'
                             'MaxEntropy'
                             'KittlerIllingworth'
                             'Moments'     Finds threshold that produces binary that best matches 1st, 2nd, and 3rd moments of grayscale image.
                             'Yen'
                             'RenyiEntropy'
                             'Shanbhag'
        ratio                TYPE: float. The ratio to use with 'Percentage' seed method. This plays no role with other seed methods.

        canny_variance       TYPE: [float,float,float]. Variance for canny edge detection.

        propagation          TYPE: float. Weight for propagation term in active contour functional. Higher results in faster expansion.
        curvature            TYPE: float. Weight for curvature term in active contour functional. Higher results in smoother segmentation.
        advection            TYPE: float. Weight for advective term in active contour functional. Higher causes levelset to move toward edges.
        rms                  TYPE: float. The change in Root Mean Square at which iterations will terminate.
        active_iterations    TYPE: integer. The maximum number of iterations the active contour will conduct.
        """
        self.thresholdSegmentation(method=seed_method,ratio=ratio)
        dimension = self._img.GetDimension()
        newcells = sitk.Image(self.cells.GetSize(),self._imgType )
        newcells.SetSpacing(self.cells.GetSpacing())
        newcells.SetDirection(self.cells.GetDirection())
        newcells.SetOrigin(self.cells.GetOrigin())
        for i,region in enumerate(self._regions):
            print("\n-------------------------------------------")
            print("Evolving Geodesic Active Contour for Cell {:d}".format(i+1))
            print("-------------------------------------------")
            if dimension == 3:
                seed = sitk.RegionOfInterest(self.cells,region[3:],region[0:3])
                roi = sitk.RegionOfInterest(self._img,region[3:],region[0:3])
                #resample the Region of Interest to improve resolution of derivatives and give closer to isotropic voxels
                zratio = self._pixel_dim[2]/self._pixel_dim[0]
                newz = int(zratio*roi.GetSize()[2])*upsampling #adjust size in z to be close to isotropic and double the resolution
                newzspace = ( float(roi.GetSize()[2])/float(newz) )*self._pixel_dim[2]
                newx = roi.GetSize()[0]*upsampling
                newxspace = self._pixel_dim[0]/float(upsampling)
                newy = roi.GetSize()[1]*upsampling
                newyspace = self._pixel_dim[1]/float(upsampling)
                #Do the resampling
                refine = sitk.ResampleImageFilter()
                refine.SetInterpolator(sitk.sitkBSpline)
                refine.SetSize( (newx,newy,newz) )
                refine.SetOutputOrigin( roi.GetOrigin() )
                refine.SetOutputSpacing( (newxspace,newyspace,newzspace) )
                refine.SetOutputDirection( roi.GetDirection() )
                rimg = refine.Execute(roi)
            else:
                seed = sitk.RegionOfInterest(self.cells,region[2:],region[0:2])
                roi = sitk.RegionOfInterest(self._img,region[2:],region[0:2])
                #resample the Region of Interest to improve resolution of derivatives
                newx = roi.GetSize()[0]*upsampling
                newxspace = self._pixel_dim[0]/float(upsampling)
                newy = roi.GetSize()[1]*upsampling
                newyspace = self._pixel_dim[1]/float(upsampling)
                #Do the resampling
                refine = sitk.ResampleImageFilter()
                refine.SetInterpolator(sitk.sitkBSpline)
                refine.SetSize( (newx,newy) )
                refine.SetOutputOrigin( roi.GetOrigin() )
                refine.SetOutputSpacing( (newxspace,newyspace) )
                refine.SetOutputDirection( roi.GetDirection() )
                rimg = refine.Execute(roi)
            refine.SetInterpolator(sitk.sitkNearestNeighbor)
            seed = refine.Execute(seed)
            #smooth the perimeter of the binary seed
            seed = sitk.BinaryMorphologicalClosing(seed==(i+1),3)
            seed = sitk.AntiAliasBinary(seed)
            seed = sitk.BinaryThreshold(seed,0.5,1e7)
            #Smooth the resampled image
            simg = self.smoothRegion(rimg)
            #Get the image gradient magnitude
            canny = sitk.CannyEdgeDetection(sitk.RescaleIntensity(sitk.Cast(rimg,sitk.sitkFloat32),0,1),variance=canny_variance)
            canny = sitk.InvertIntensity(canny,1)
            canny = sitk.Cast(canny,sitk.sitkFloat32)
            if self.debug:
                sitk.WriteImage(sitk.Cast(simg,self._imgType),self._output_dir+self._path_dlm+"smoothed_{:03d}.nii".format(i+1))
                sitk.WriteImage(sitk.Cast(seed,self._imgType),self._output_dir+self._path_dlm+"seed_{:03d}.nii".format(i+1))
                sitk.WriteImage(sitk.Cast(canny,self._imgType),self._output_dir+self._path_dlm+"edge_{:03d}.nii".format(i+1))
            d = sitk.SignedMaurerDistanceMap(seed,insideIsPositive=False,squaredDistance=False,useImageSpacing=True)
            d = sitk.BinaryThreshold(d,-1e7,-0.5)
            d = sitk.Cast(d,canny.GetPixelIDValue() )*(-1.0)+0.5
            gd = sitk.GeodesicActiveContourLevelSetImageFilter()
            gd.SetMaximumRMSError(rms/float(upsampling))
            gd.SetNumberOfIterations(active_iterations)
            gd.SetPropagationScaling(propagation)
            gd.SetCurvatureScaling(curvature)
            gd.SetAdvectionScaling(advection)
            seg = gd.Execute(d,canny)
            print("... Geodesic Active Contour Segmentation Completed")
            print("... ... Elapsed Iterations: {:d}".format(gd.GetElapsedIterations()))
            print("... ... Change in RMS Error: {:.3e}".format(gd.GetRMSChange()))
            seg = sitk.BinaryThreshold(seg,-1e7,0)*(i+1)
            tmp = sitk.Image(self._img.GetSize(),self._imgType)
            tmp.SetSpacing(self._img.GetSpacing())
            tmp.SetOrigin(self._img.GetOrigin())
            tmp.SetDirection(self._img.GetDirection())
            resampler = sitk.ResampleImageFilter()
            resampler.SetReferenceImage(tmp)
            resampler.SetInterpolator(sitk.sitkNearestNeighbor)
            tmp = resampler.Execute(seg)
            newcells = sitk.Add(newcells,sitk.Cast(tmp,self._imgType))
            #Handle Overlap
            if self.handle_overlap:
                maxlabel = self._getMinMax(newcells)[1]
                if maxlabel > (i+1):
                    newcells = self._classifyShared(i,newcells,True)
        self.cells = newcells
            
    def edgeFreeSegmentation(self,upsampling=2,seed_method='Percentage',ratio=0.4,lambda1=1.0,lambda2=1.1,curvature=0.0,iterations=20):
        """
        DESCRIPTION
        Performs a segmentation using the SimpleITK implementation of the Active Contours Without Edges method described in (Chan and Vese. 2001.)
        Please also consult SimpleITK's documentation of ScalarChanAndVeseDenseLevelSetImageFilter.
        This method will establish initial levelsets by calling the entropySegmentation() method.

        INPUTS
        upsampling           TYPE: integer. Resample image splitting original voxels into this many. NOTE - Resampling will always be performed to make voxels isotropic.
        seed_method          TYPE: string. Method used to determine seed image. Same as thresholdSegmentation method variable.
                             OPTIONS
                             'Percentage'  Threshold at percentage of the maximum voxel intensity.
                             For more information on the following consult http://www.insight-journal.org/browse/publication/811 and cited original sources.
                             'Huang'       Fuzzy thresholding using Shannon entropy function.
                             'IsoData'     Also known as Ridler-Calvard.
                             'Li'
                             'MaxEntropy'
                             'KittlerIllingworth'
                             'Moments'     Finds threshold that produces binary that best matches 1st, 2nd, and 3rd moments of grayscale image.
                             'Yen'
                             'RenyiEntropy'
                             'Shanbhag'
        ratio                TYPE: float. The ratio to use with 'Percentage' seed method. This plays no role with other seed methods.

        lambda1              TYPE: float. Weight for internal levelset term.
        lambda2              TYPE: float. Weight for external levelset term.
        curvature            TYPE: float. Weight for curvature. Higher results in smoother levelsets, but less ability to capture fine features.
        iterations           TYPE: integer. The number of iterations the active contour will conduct.
        """
        self.thresholdSegmentation(method=seed_method,ratio=ratio)
        dimension = self._img.GetDimension()
        newcells = sitk.Image(self.cells.GetSize(),self._imgType)
        newcells.SetSpacing(self.cells.GetSpacing())
        newcells.SetDirection(self.cells.GetDirection())
        newcells.SetOrigin(self.cells.GetOrigin())
        for i,region in enumerate(self._regions):
            print("\n-------------------------------------------")
            print("Evolving Edge-free Active Contour for Cell {:d}".format(i+1))
            print("-------------------------------------------")
            if dimension == 3:
                seed = sitk.RegionOfInterest(self.cells,region[3:],region[0:3])
                roi = sitk.RegionOfInterest(self._img,region[3:],region[0:3])
                #resample the Region of Interest to improve resolution of derivatives and give closer to isotropic voxels
                zratio = self._pixel_dim[2]/self._pixel_dim[0]
                newz = int(zratio*roi.GetSize()[2])*upsampling #adjust size in z to be close to isotropic and double the resolution
                newzspace = ( float(roi.GetSize()[2])/float(newz) )*self._pixel_dim[2]
                newx = roi.GetSize()[0]*upsampling
                newxspace = self._pixel_dim[0]/float(upsampling)
                newy = roi.GetSize()[1]*upsampling
                newyspace = self._pixel_dim[1]/float(upsampling)
                #Do the resampling
                refine = sitk.ResampleImageFilter()
                refine.SetInterpolator(sitk.sitkBSpline)
                refine.SetSize( (newx,newy,newz) )
                refine.SetOutputOrigin( roi.GetOrigin() )
                refine.SetOutputSpacing( (newxspace,newyspace,newzspace) )
                refine.SetOutputDirection( roi.GetDirection() )
            else:
                seed = sitk.RegionOfInterest(self.cells,region[2:],region[0:2])
                roi = sitk.RegionOfInterest(self._img,region[2:],region[0:2])
                #resample the Region of Interest to improve resolution of derivatives
                newx = roi.GetSize()[0]*upsampling
                newxspace = self._pixel_dim[0]/float(upsampling)
                newy = roi.GetSize()[1]*upsampling
                newyspace = self._pixel_dim[1]/float(upsampling)
                #Do the resampling
                refine = sitk.ResampleImageFilter()
                refine.SetInterpolator(sitk.sitkBSpline)
                refine.SetSize( (newx,newy) )
                refine.SetOutputOrigin( roi.GetOrigin() )
                refine.SetOutputSpacing( (newxspace,newyspace) )
                refine.SetOutputDirection( roi.GetDirection() )
            rimg = refine.Execute(roi)
            refine.SetInterpolator(sitk.sitkNearestNeighbor)
            seed = refine.Execute(seed)
            if self.debug:
                sitk.WriteImage(sitk.Cast(seed,self._imgType),self._output_dir+self._path_dlm+"seed_{:03d}.nii".format(i+1))
            blur = sitk.DiscreteGaussian(sitk.Cast(seed==(i+1),sitk.sitkFloat32),variance=1.0)
            #intensity_weighted = sitk.Cast(rimg,sitk.sitkFloat32) + 2*blur*sitk.Cast(rimg,sitk.sitkFloat32)
            intensity_weighted = sitk.Cast(rimg,sitk.sitkFloat32)
            phi0 = sitk.SignedMaurerDistanceMap(seed==(i+1),insideIsPositive=False,squaredDistance=False,useImageSpacing=True)
            cv = sitk.ScalarChanAndVeseDenseLevelSetImageFilter()
            cv.SetNumberOfIterations(iterations)
            cv.UseImageSpacingOn()
            cv.SetHeavisideStepFunction(0)
            cv.SetLambda1(lambda1)
            cv.SetLambda2(lambda2)
            seg = cv.Execute(sitk.Cast(phi0,sitk.sitkFloat32),intensity_weighted)
            seg = sitk.BinaryThreshold(seg,1e-7,1e7)
            #Get connected regions
            seg = sitk.BinaryMorphologicalOpening(seg,upsampling)
            r = sitk.ConnectedComponent(seg)
            labelstats = self._getLabelShape(r)
            label = np.argmax(labelstats['volume'])+1
            seg = (r==label)*(i+1)
            tmp = sitk.Image(self._img.GetSize(),self._imgType)
            tmp.SetSpacing(self._img.GetSpacing())
            tmp.SetOrigin(self._img.GetOrigin())
            tmp.SetDirection(self._img.GetDirection())
            resampler = sitk.ResampleImageFilter()
            resampler.SetReferenceImage(tmp)
            resampler.SetInterpolator(sitk.sitkNearestNeighbor)
            tmp = resampler.Execute(seg)
            newcells = sitk.Add(newcells,sitk.Cast(tmp,self._imgType))
            #Handle Overlap
            if self.handle_overlap:
                maxlabel = self._getMinMax(newcells)[1]
                if maxlabel > (i+1):
                    newcells = self._classifyShared(i,newcells,True)
        self.cells = newcells

    def _classifyShared(self,i,cells,previous):
        #cells overlap so use SVM to classify shared voxels
        print("... ... ... WARNING: Segmentation overlapped a previous")
        print("... ... ... Using SVM to classify shared voxels")
        a = sitk.GetArrayFromImage(cells)
        ind2space = np.array(self._pixel_dim,float)[::-1]
        # we can use seeds from a previous segmentation as training
        # for geodesic and edge-free cases
        if previous:
            t = sitk.GetArrayFromImage(self.cells)
            p1 = np.argwhere(t==(i+1)) * ind2space
            print("\n")
        else:
            print("... ... ... The training data is often insufficient for this segmentation method.")
            print("... ... ... Please consider using Geodesic or EdgeFree options.\n")
            p1 = np.argwhere( a==(i+1) ) * ind2space
        g1 = np.array([i+1]*p1.shape[0],int)
        labels = np.unique(a)
        b = np.copy(a)
        for l in labels[labels>(i+1)]:
            if previous:
                p2 = np.argwhere( t==(l-i-1) ) * ind2space
            else:
                p2 = np.argwhere( a==(l-i-1) ) * ind2space
            unknown1 = np.argwhere(a==l) * ind2space
            unknown2 = np.argwhere(a==(i+1)) * ind2space
            unknown3 = np.argwhere(a==(l-i-1)) * ind2space
            unknown = np.vstack((unknown1,unknown2,unknown3))
            g2 = np.array([l-i-1]*p2.shape[0],int)
            X = np.vstack((p1,p2))
            scaler = preprocessing.StandardScaler().fit(X)
            y = np.hstack((g1,g2))
            clf = svm.SVC(kernel='rbf',degree=3,gamma=2,class_weight='auto')
            clf.fit(scaler.transform(X),y)
            classification = clf.predict(scaler.transform(unknown))
            b[a==l] = classification[0:unknown1.shape[0]]
            b[a==(i+1)] = classification[unknown1.shape[0]:unknown1.shape[0]+unknown2.shape[0]]
            b[a==(l-i-1)] = classification[unknown1.shape[0]+unknown2.shape[0]:]
        cells = sitk.Cast(sitk.GetImageFromArray(b),self._imgType)
        cells.SetSpacing(self._img.GetSpacing())
        cells.SetOrigin(self._img.GetOrigin())
        cells.SetDirection(self._img.GetDirection())
        return cells

    def writeSurfaces(self):
        if self._img.GetDimension() == 2:
            print("WARNING: A 2D image was processed, so there are no surfaces to write.")
            return
        #delete old surfaces
        old_surfaces = fnmatch.filter(os.listdir(self._output_dir),'*.stl')
        for f in old_surfaces:
            os.remove(self._output_dir+self._path_dlm+f)
        
        #create and write the STLs
        stl = vtk.vtkSTLWriter()
        for i,c in enumerate(self._regions):
            a = vti.vtkImageImportFromArray()
            a.SetDataSpacing([self._pixel_dim[0],self._pixel_dim[1],self._pixel_dim[2]])
            a.SetDataExtent([0,100,0,100,0,self._img.GetSize()[2]])
            n = sitk.GetArrayFromImage(self.cells==(i+1))
            a.SetArray(n)
            a.Update()

            thres=vtk.vtkImageThreshold()
            thres.SetInputData(a.GetOutput())
            thres.ThresholdByLower(0)
            thres.ThresholdByUpper(1e7)
            thres.Update()

            iso=vtk.vtkImageMarchingCubes()
            iso.SetInputConnection(thres.GetOutputPort())
            iso.SetValue(0,0)
            iso.Update()

            triangles = vtk.vtkGeometryFilter()
            triangles.SetInputConnection(iso.GetOutputPort())
            triangles.Update()

            smooth = vtk.vtkWindowedSincPolyDataFilter()
            smooth.SetNumberOfIterations(100)
            smooth.SetInputConnection(triangles.GetOutputPort())
            smooth.Update()
            self.surfaces.append(smooth.GetOutput())
            
            filename = 'cell{:02d}.stl'.format(i+1)
            stl.SetFileName(self._output_dir+self._path_dlm+filename)
            stl.SetInputData(self.surfaces[-1])
            stl.Write()
        if self.display:
            print('\n************************     Rendering Cells     *****************************')
            print('                               Please Note:                                   ')
            print('               The renderings displayed here are voxel-based.                 ') 
            print('The STL surfaces will be much smoother if viewed in software such as Paraview.')
            print('******************************************************************************')
            N = len(self.surfaces)
            colormap = vtk.vtkLookupTable()
            colormap.SetHueRange(0.9,0.1)
            colormap.SetTableRange(1,N)
            colormap.SetNumberOfColors(N)
            colormap.Build()
            skins = []
            for i,s in enumerate(self.surfaces):
                mapper = vtk.vtkPolyDataMapper()
                mapper.SetInputData(s)
                mapper.SetLookupTable(colormap)
                mapper.ScalarVisibilityOff()
                skin = vtk.vtkActor()
                skin.SetMapper(mapper)
                color = [0,0,0]
                colormap.GetColor(i+1,color)
                skin.GetProperty().SetColor(color)
                skins.append(skin)

            # Create a colorbar
            colorbar = vtk.vtkScalarBarActor()
            colorbar.SetLookupTable(colormap)
            colorbar.SetTitle("Cells")
            colorbar.SetNumberOfLabels(N)
            colorbar.SetLabelFormat("%3.0f")

            # Create the renderer, the render window, and the interactor. The renderer
            # draws into the render window, the interactor enables mouse- and 
            # keyboard-based interaction with the data within the render window.
            aRenderer = vtk.vtkRenderer()
            renWin = vtk.vtkRenderWindow()
            renWin.AddRenderer(aRenderer)
            iren = vtk.vtkRenderWindowInteractor()
            iren.SetRenderWindow(renWin)

            # It is convenient to create an initial view of the data. The FocalPoint
            # and Position form a vector direction. Later on (ResetCamera() method)
            # this vector is used to position the camera to look at the data in
            # this direction.
            aCamera = vtk.vtkCamera()
            aCamera.SetViewUp (0, 0, -1)
            aCamera.SetPosition (0, 1, 0)
            aCamera.SetFocalPoint (0, 0, 0)
            aCamera.ComputeViewPlaneNormal()
            
            # Actors are added to the renderer. An initial camera view is created.
            # The Dolly() method moves the camera towards the FocalPoint,
            # thereby enlarging the image.
            for skin in skins:
                aRenderer.AddActor(skin)
            aRenderer.AddActor(colorbar)
            aRenderer.SetActiveCamera(aCamera)
            aRenderer.ResetCamera ()
            aCamera.Dolly(1.5)

            bounds = thres.GetOutput().GetBounds()
            triad = vtk.vtkCubeAxesActor()
            l = 0.5*(bounds[5]-bounds[4])
            triad.SetBounds([bounds[0],bounds[0]+l,bounds[2],bounds[2]+l,bounds[4],bounds[4]+l])
            triad.SetCamera(aRenderer.GetActiveCamera())
            triad.SetFlyModeToStaticTriad()
            triad.GetXAxesLinesProperty().SetColor(1.0,0.0,0.0)
            triad.GetYAxesLinesProperty().SetColor(0.0,1.0,0.0)
            triad.GetZAxesLinesProperty().SetColor(0.0,0.0,1.0)
            triad.GetXAxesLinesProperty().SetLineWidth(5.0)
            triad.GetYAxesLinesProperty().SetLineWidth(5.0)
            triad.GetZAxesLinesProperty().SetLineWidth(5.0)
            triad.XAxisLabelVisibilityOff()
            triad.YAxisLabelVisibilityOff()
            triad.ZAxisLabelVisibilityOff()
            triad.XAxisTickVisibilityOff()
            triad.YAxisTickVisibilityOff()
            triad.ZAxisTickVisibilityOff()
            triad.XAxisMinorTickVisibilityOff()
            triad.YAxisMinorTickVisibilityOff()
            triad.ZAxisMinorTickVisibilityOff()
            aRenderer.AddActor(triad)
            # Set a background color for the renderer and set the size of the
            # render window (expressed in pixels).
            aRenderer.SetBackground(0.0,0.0,0.0)
            renWin.SetSize(800, 600)

            # Note that when camera movement occurs (as it does in the Dolly()
            # method), the clipping planes often need adjusting. Clipping planes
            # consist of two planes: near and far along the view direction. The 
            # near plane clips out objects in front of the plane the far plane
            # clips out objects behind the plane. This way only what is drawn
            # between the planes is actually rendered.
            aRenderer.ResetCameraClippingRange()

            im=vtk.vtkWindowToImageFilter()
            im.SetInput(renWin)

            iren.Initialize();
            iren.Start();

    def writeLabels(self):
        sitk.WriteImage(self.cells,self._output_dir+self._path_dlm+'labels.nii')

    def getDimensions(self):
        labelstats = self._getLabelShape(self.cells)
        self.volumes = labelstats['volume']
        self.centroids = labelstats['centroid']
        self.dimensions = labelstats['ellipsoid diameters']