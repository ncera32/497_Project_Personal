# Import Libraries

import matplotlib.pyplot as plt
import os
import numpy as np
from mpi4py import MPI
from baseclasses import AeroProblem
from pygeo import DVConstraints, DVGeometryCST
from pyoptsparse import Optimization, OPT
from multipoint import multiPointSparse
from cmplxfoil import CMPLXFOIL, AnimateAirfoilOpt

import matplotlib as mpl

mpl.rcParams['lines.linewidth'] = 2
mpl.rc('xtick', labelsize=24) 
mpl.rc('ytick', labelsize=24) 
mpl.rc('axes', labelsize=24) 
mpl.rc('font', size=24)

# Specifying Parameters for Optimization

mycl = 0.5 #CL constraint
mycm = 0.03 # CM constraint PLACEHOLDER
alpha = 0.0 if mycl == 0.0 else 1.0 #Initial AoA (zero if CL is zero)
mach = 0.1 #Mach number
Re = 1e6 #Reynolds number
T = 288.15 #Standard sea level temperature (K)

# Creating processor sets

MP = multiPointSparse(MPI.COMM_WORLD)
MP.addProcessorSet("cruise", nMembers=1, memberSizes=MPI.COMM_WORLD.size)
MP.createCommunicators()

# Creating Output Directory

#CHECK
curDir = os.path.abspath(os.path.dirname(__file__))
outputDir = os.path.join(curDir, "output")

if not os.path.exists(outputDir):
    os.mkdir(outputDir)

# can do outputDir = "/Users/nickcera/Desktop/Aersp497-DesOpt/Project" ???
    
# CMPLXOIL solver setup
    
aeroOptions = {
    "writeSolution": True,
    "writeSliceFile": True,
    "writeCoordinates": True,
    "plotAirfoil": True,
    "outputDirectory": outputDir,
}

CFDSolver = CMPLXFOIL(os.path.join(curDir, "naca2415.dat.rtf"), options=aeroOptions)

#Set the aero-problem
ap = AeroProblem(
    name="fc",
    alpha=alpha if mycl != 0.0 else 0.0,
    mach=mach,
    reynolds=Re,
    reynoldsLength=1.0,
    T=T,
    areaRef=1.0,
    chordRef=1.0,
    evalFuncs=["cl", "cd", "cm"],
)
# Add angle of attack variable
if mycl != 0.0:
    ap.addDV("alpha", value=ap.alpha, lower=-10.0, upper=10.0, scale=1.0)

#Geometric parameterization
nCoeff = 4  # number of CST coefficients on each surface
DVGeo = DVGeometryCST(os.path.join(curDir, "naca2415.dat.rtf"), numCST=nCoeff)

DVGeo.addDV("upper_shape", dvType="upper", lowerBound=-0.1, upperBound=0.5)
DVGeo.addDV("lower_shape", dvType="lower", lowerBound=-0.5, upperBound=0.1)

# Add DVGeo object to CFD solver
CFDSolver.setDVGeo(DVGeo)

#Geometric constraints
DVCon = DVConstraints()
DVCon.setDVGeo(DVGeo)
DVCon.setSurface(CFDSolver.getTriangulatedMeshSurface())

# Thickness, volume, and leading edge radius constraints
le = 0.0001
wingtipSpacing = 0.1
leList = [[le, 0, wingtipSpacing], [le, 0, 1.0 - wingtipSpacing]]
teList = [[1.0 - le, 0, wingtipSpacing], [1.0 - le, 0, 1.0 - wingtipSpacing]]
DVCon.addVolumeConstraint(leList, teList, 2, 100, lower=0.85, scaled=True)
DVCon.addThicknessConstraints2D(leList, teList, 2, 100, lower=0.25, scaled=True)
le = 0.01
leList = [[le, 0, wingtipSpacing], [le, 0, 1.0 - wingtipSpacing]]
DVCon.addLERadiusConstraints(leList, 2, axis=[0, 1, 0], chordDir=[-1, 0, 0], lower=0.85, scaled=True)

fileName = os.path.join(outputDir, "constraints.dat")
DVCon.writeTecplot(fileName)

# Optimization callback functions

def cruiseFuncs(x):
    print(x)
    # Set design vars
    DVGeo.setDesignVars(x)
    ap.setDesignVars(x)
    # Run CFD
    CFDSolver(ap)
    # Evaluate functions
    funcs = {}
    DVCon.evalFunctions(funcs)
    CFDSolver.evalFunctions(ap, funcs)
    CFDSolver.checkSolutionFailure(ap, funcs)
    if MPI.COMM_WORLD.rank == 0:
        print("functions:")
        for key, val in funcs.items():
            if key == "DVCon1_thickness_constraints_0":
                continue
            print(f"    {key}: {val}")
    return funcs


def cruiseFuncsSens(x, funcs):
    funcsSens = {}
    DVCon.evalFunctionsSens(funcsSens)
    CFDSolver.evalFunctionsSens(ap, funcsSens)
    CFDSolver.checkAdjointFailure(ap, funcsSens)
    print("function sensitivities:")
    evalFunc = ["fc_cd", "fc_cl", "fc_cm", "fail"]
    for var in evalFunc:
        print(f"    {var}: {funcsSens[var]}")
    return funcsSens


# TRYING TO COLLECT METRIC HISTORY IN ARRAYS
obj_vals_SLSQP = []
cl_con_vals_SLSQP = []
cm_con_vals_SLSQP = []

def objCon(funcs, printOK):
    # Assemble the objective and any additional constraints:
    funcs["obj"] = funcs[ap["cd"]]
    funcs["cl_con_" + ap.name] = funcs[ap["cl"]] - mycl
    funcs["cm_con_" + ap.name] = funcs[ap["cm"]] - mycm

    obj_vals_SLSQP.append(funcs["obj"])
    cl_con_vals_SLSQP.append(funcs["cl_con_" + ap.name])
    cm_con_vals_SLSQP.append(funcs["cm_con_" + ap.name])

    if printOK:
        print("funcs in obj:", funcs)
    return funcs

# Optimization problem

# Create optimization problem
optProb = Optimization("opt", MP.obj)

# Add objective
optProb.addObj("obj", scale=1e4)

# Add variables from the AeroProblem
ap.addVariablesPyOpt(optProb)

# Add DVGeo variables
DVGeo.addVariablesPyOpt(optProb)

# Add constraints
DVCon.addConstraintsPyOpt(optProb)

# Add cl constraint
optProb.addCon("cl_con_" + ap.name, lower=0.0, upper=0.0, scale=1.0)

# Add cm constraint
optProb.addCon("cm_con_" + ap.name, lower=0.0, upper=0.0, scale=1.0)

# Enforce first upper and lower CST coefficients to add to zero
# to maintain continuity at the leading edge
jac = np.zeros((1, nCoeff), dtype=float)
jac[0, 0] = 1.0
optProb.addCon(
    "first_cst_coeff_match",
    lower=0.0,
    upper=0.0,
    linear=True,
    wrt=["upper_shape", "lower_shape"],
    jac={"upper_shape": jac, "lower_shape": jac},
)

# The MP object needs the 'obj' and 'sens' function for each proc set,
# the optimization problem and what the objcon function is:
MP.setProcSetObjFunc("cruise", cruiseFuncs)
MP.setProcSetSensFunc("cruise", cruiseFuncsSens)
MP.setObjCon(objCon)
MP.setOptProb(optProb)
optProb.printSparsity()
optProb.getDVConIndex()

# Run optimization

# Run optimization
optOptions = {"IFILE": os.path.join(outputDir, "SLSQP.out")}
opt = OPT("SLSQP", options=optOptions)
sol = opt(optProb, MP.sens, storeHistory=os.path.join(outputDir, "opt.hst"))
if MPI.COMM_WORLD.rank == 0:
    print(sol)

# Postprocessing 
    
# Save the final figure
CFDSolver.airfoilAxs[1].legend(["Original", "Optimized"], labelcolor="linecolor")
CFDSolver.airfoilFig.savefig(os.path.join(outputDir, "OptFoil.pdf"))


# Animate the optimization
#AnimateAirfoilOpt(outputDir, "fc").animate(
#    outputFileName=os.path.join(outputDir, "OptFoil"), fps=10, dpi=300
#)

#removed extra_args=["-vcodec", "libx264"] from above

# Plotting
fig = plt.figure(figsize = (12,12))
iterations_slsqp = np.arange(len(obj_vals_SLSQP))
ax1 = fig.add_subplot(221)
ax1.plot(iterations_slsqp, obj_vals_SLSQP, marker='.', lw=2, color='r')
ax1.set_xlabel('iterations')
ax1.set_ylabel('Cd value')
plt.tight_layout()

iterations_slsqp = np.arange(len(cl_con_vals_SLSQP))
ax2 = fig.add_subplot(222)
ax2.plot(iterations_slsqp, cl_con_vals_SLSQP, marker='.', lw=2, color='r')
ax2.set_xlabel('iterations')
ax2.set_ylabel('CL constraint violation')
plt.tight_layout()

iterations_slsqp = np.arange(len(cm_con_vals_SLSQP))
ax3 = fig.add_subplot(223)
ax3.plot(iterations_slsqp, cm_con_vals_SLSQP, marker='.', lw=2, color='r')
ax3.set_xlabel('iterations')
ax3.set_ylabel('CM constraint violation')
plt.tight_layout()

 
