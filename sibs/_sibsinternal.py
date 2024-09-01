from dataclasses import dataclass, field
import distutils.ccompiler
import os
import hashlib
import shutil
import pickle
import subprocess
import sys
from ._version import *
import glob

# OPTIONS, these can be set by the cmdline
sibsopt_nohcache = False
sibsopt_nohashdir = False
sibsopt_cflags = ""
sibsopt_ccflags = ""
sibsopt_ldflags = ""
sibsopt_cxxflags = ""
sibsopt_cxxlflags = ""
sibsopt_cclflags = ""
sibsopt_arflags = ""
sibsopt_showcommands = False
# sibsopt_nobuild = False
# sibsopt_nocopy = False
# sibsopt_nolink = False
# sibsopt_nocompile = False


cmakeloader = """
if (POLICY CMP0026)
  cmake_policy(SET CMP0026 OLD)
endif()

set(Project_Directories "${CMAKE_SOURCE_DIR}")
get_property(stack DIRECTORY ${CMAKE_SOURCE_DIR} PROPERTY SUBDIRECTORIES)
while(stack)
    list(POP_BACK stack directory)
    list(APPEND Project_Directories ${directory})
    get_property(subdirs DIRECTORY ${directory} PROPERTY SUBDIRECTORIES)
    if(subdirs)
        list(APPEND stack ${subdirs})
    endif()
endwhile()

foreach(dir ${Project_Directories})
  get_property(targetlist DIRECTORY ${dir} PROPERTY BUILDSYSTEM_TARGETS)
  #get_property(targetloc TARGET ${target} PROPERTY LOCATION)
  foreach(targ ${targetlist})
    get_property(targloc TARGET ${targ} PROPERTY LOCATION)
    get_property(targtyp TARGET ${targ} PROPERTY TYPE)
    get_property(targinc TARGET ${targ} PROPERTY INCLUDE_DIRECTORIES)
    message(STATUS "_SIBSUNIT_(${targtyp})(${targ})(${targloc})(${targinc})_SIBSEND_")
  endforeach()
endforeach()
"""

dynprefix = distutils.ccompiler.new_compiler().shared_lib_extension
staticprefix = distutils.ccompiler.new_compiler().static_lib_extension
execprefix = distutils.ccompiler.new_compiler().exe_extension
if execprefix == None or execprefix == "":
    execprefix = ""

defaultobj = "$CXX -c $SRC -o $OUT"
defaultstatic = "$AR -rcs $OUT $SRC"
defaultexec = "$CXXL $SRC -o $OUT -Wl,-rpath='${ORIGIN}'"
defaultdyn = "$CXXL -shared -fPIC $SRC -o $OUT"

@dataclass
class BuildUnit:
    name: str
    out_type: str
    dat: dict[str, str]
    incstr: str = ""
    # objects dynamic and static are lists of files that are generated by this unit, and also include dependencies
    objects: list[str] = field(default_factory=lambda: [])
    dynamic: list[str] = field(default_factory=lambda: [])
    static: list[str] = field(default_factory=lambda: [])
    thisoutput: str = "" # output of THIS unit, not including dependencies
    skip: bool = False
    directory: str = "."
    done: bool = False
    dolink: bool = False
    docompile: bool = False
    changed: bool = True
    cmakebuilddir: str = ""
    cmaketarget: str = ""
    prefix: str = ""

class CmakeUnitLoad:
    def __init__(self, unitstr: str, directory: str):
        out_type_st = unitstr.find('(')+1
        out_type_en = unitstr.find(')', out_type_st)
        out_type = unitstr[out_type_st:out_type_en].strip().upper()
        name_st = unitstr.find('(', out_type_en)+1
        name_en = unitstr.find(')', name_st)
        self.name = unitstr[name_st:name_en].strip()
        out_st = unitstr.find('(', name_en)+1
        out_en = unitstr.find(')', out_st)
        self.out = unitstr[out_st:out_en].strip()
        inc_st = unitstr.find('(', out_en)+1
        inc_en = unitstr.find(')', inc_st)
        self.inc = unitstr[inc_st:inc_en].strip()
        if out_type == "EXECUTABLE":
            self.out_type = "EXEC"
        elif out_type == "STATIC_LIBRARY":
            self.out_type = "STATIC"
        elif out_type == "SHARED_LIBRARY":
            self.out_type = "DYN"
        elif out_type == "UTILITY":
            self.out_type = "UNKNOWN" # we just ignore it
        else:
            print(f"Unknown type '{out_type}'")
            self.out_type = "UNKNOWN"


firstpath = os.getcwd()

class HashCache:
    # key pair file that stores hashes as bytes
    def __init__(self):
        self.hcache = {}
        # we will load and store the bytes ourselves
    
    def write(self):
        if not os.path.exists(os.path.join(firstpath, "build")):
            os.makedirs(os.path.join(firstpath, "build"))
        
        with open(os.path.join(firstpath, "build/sibs.hcache"), "wb+") as f:
            pickle.dump(self.hcache, f)

    
    def read(self):
        if os.path.exists(os.path.join(firstpath, "build/sibs.hcache")):
            with open(os.path.join(firstpath, "build/sibs.hcache"), "rb") as f:
                self.hcache = pickle.load(f)

    def setbytes(self, key: str, value: bytes):
        self.hcache[key] = value
        self.write()

    def sethash(self, file: str):
        with open(file, "rb") as f:
            self.setbytes(file, hashlib.sha256(f.read()).digest())
    
    def gethash(self, file: str) -> bytes:
        if file in self.hcache:
            return self.hcache[file]
        else:
            return None

def strhash(x):
    return hashlib.sha256(x.encode()).hexdigest()

def hashdirs(path: str) -> list[bytes]:
    # for every file in the directory (recursively) hash it
    # if a file is under .git or .sibscmakebuild, ignore it
    files = os.listdir(path)
    files = [f for f in files if not f.startswith('.')]
    hashes = []
    for f in files:
        if os.path.isdir(os.path.join(path, f)):
            hashes += hashdirs(os.path.join(path, f))
        else:
            with open(os.path.join(path, f), "rb") as f:
                   hashes.append(hashlib.sha256(f.read()).digest())
    return hashes

def hashdir(path: str) -> bytes:
    hashes = hashdirs(path)
    # we sort them so that the order is consistent
    hashes.sort()
    out = hashlib.sha256(b''.join(hashes)).digest()
    return out
    

if not sibsopt_nohcache:
    hashcache = HashCache()
    hashcache.read()

def getdirectdeps(units: list[BuildUnit], unit: BuildUnit) -> list[BuildUnit]:
    deps = []
    if 'DEPS' not in unit.dat:
        return []
    for dep in unit.dat['DEPS'].split('\n'):
        dep = dep.strip()
        if dep == "":
            continue
        dp = None
        for un in units:
            if dep == un.name:
                dp = un
                break
            if dep == un.directory:
                dp = un
                break
            if un.prefix + dep == un.name:
                dp = un
                break
            if dep.endswith(un.name) and un.prefix in dep:
                dp = un
                break
        
        if dp == None:
            print(f"Dependency '{dep}' not found!")
            print("Available dependencies:")
            for un in units:
                print(f"    {un.name}")
            exit(1)
        deps.append(dp)
    
    return deps


def getdeps(units: list[BuildUnit], unit: BuildUnit) -> list[BuildUnit]:
    deps = []
    if 'DEPS' not in unit.dat:
        return []
    for dep in unit.dat['DEPS'].split('\n'):
        dep = dep.strip()
        if dep == "":
            continue
        dp = None
        for un in units:
            if dep == un.name:
                dp = un
                break
            if dep == un.directory:
                dp = un
                break
            if un.prefix + dep == un.name:
                dp = un
                break
            if dep.endswith(un.name) and un.prefix in dep:
                dp = un
                break
        if dp == None:
            print(f"Dependency '{dep}' not found!")
            print("Available dependencies:")
            for un in units:
                print(f"    {un.name}")
            exit(1)
        deps.append(dp)
        if 'DEPS' in dp.dat:
            deps += getdeps(units, dp)
    return deps

def getused(units: list[BuildUnit], unit_name: str) -> list[BuildUnit]:
    # get all units that use this one
    used = []
    for unit in units:
        if unit.skip:
            continue
        for dep in getdirectdeps(units, unit):
            if dep.name == unit_name:
                used.append(unit)
    return used



def filedigest(path: str) -> bytes:
    return hashlib.sha256(open(path, "rb").read()).digest()

def loadunits(path: str, prefix: str = "") -> tuple[list[BuildUnit], list[str]]:
    # go into path
    if prefix != "":
        prefix = prefix+"_"
    originalpath = os.getcwd()
    os.chdir(path)

    units: list[BuildUnit] = []
    with open("sibs.txt", "r") as f:
        inplines: list[str] = f.readlines()

    commands = []
    
    currentunit = None
    level = 0
    asdname = ""
    data = ""
    cmakemode = False
    cmakename = ""
    cmakeprojs = []
    sibsmode = False
    sibsname = ""
    sibsdirs = []
    gitmode = False
    buildcmds = False
    confcmds = False
    for lineus in inplines:
        line = lineus.strip()
        if line.startswith("#"):
            continue
        # remove everything after a # on the line (unless it is \#)
        cmrm_hf = line.find("#")
        if cmrm_hf != -1:
            if line[cmrm_hf-1] != "\\":
                line = line[:cmrm_hf]
            else:
                # remove JUST the \\
                line = line[:cmrm_hf-1] + line[cmrm_hf:]

        if line.startswith("BUILDCMDS") and level == 0:
            # BUILDCMDS {
            #     echo hi
            # }
            level += 1
            buildcmds = True
            continue
        if buildcmds and level == 1:
            if line.endswith('}'):
                level -= 1
                buildcmds = False
                continue
            else:
                commands.append(line.strip())
                continue

        if line.startswith("CONFCMDS") and level == 0:
            # CONFCMDS {
            #     echo hi
            # }
            level += 1
            confcmds = True
            continue
        if confcmds and level == 1:
            if line.endswith('}'):
                level -= 1
                confcmds = False
                continue
            else:
                os.system(line.strip()) 
                continue



        
        if line.startswith("CMAKE") and level == 0:
            # Cmake units are special
            # their unit name ('cmakeproj' in this example) is NOT a valid unit
            # all 'targets' in the cmake project are imported starting with this name
            # so if the cmake project contains a target called mytestlib a valid unit would be cmakeproj_mytestlib
            # if you put multiple projects, they will all be imported under the same name
            nmst = line.find('(')+1
            nmen = line.find(')')
            name = line[nmst:nmen].strip()
            level += 1
            cmakemode = True
            cmakename = name
            cmakeprojs = []

            continue
        if cmakemode and level == 1:
            if line.endswith('}'):
                cmakemode = False
                level -= 1
                for p in cmakeprojs:
                    print(f"CMAKE {p}")
                    # we add the cmake loader to the end of the p CMakeLists.txt
                    # then run cmake on it, and then remove the loader
                    with open(p+"/CMakeLists.txt", "r") as f:
                        cmakelines = f.readlines()
                    with open(p+"/CMakeLists.txt", "w") as f:
                        for ln in cmakelines:
                            f.write(ln)
                        f.write('\n# SIBSLOADER_START\n')
                        f.write(cmakeloader)
                        f.write('\n# SIBSLOADER_END\n')
                    builddir = os.path.join(p, ".sibscmakebuild")
                    os.makedirs(builddir, exist_ok=True)
                    # os.makedirs(os.path.join(firstpath, "build/cmake/"), exist_ok=True)
                    # os.system(f"cmake -B {builddir} {p} > "+os.path.join(firstpath, "build/cmake/cmake.log"))
                    # use subprocess to capture the output
                    a = subprocess.run(["cmake", "-B", builddir, p], capture_output=True)
                    if a.returncode != 0:
                        print(f"Error: cmake failed for {p}:")
                        print(a.stderr.decode())
                        exit(1)

                    with open(p+"/CMakeLists.txt", "w") as f:
                        for ln in cmakelines:
                            f.write(ln)

                    cmakelines = a.stdout.decode().split('\n')
                    sibsunits = []
                    for lnus in cmakelines:
                        ln = lnus.strip()
                        if "_SIBSUNIT_" in ln:
                            st = ln.find("_SIBSUNIT_")
                            en = ln.find("_SIBSEND_")
                            sibsunits.append(ln[st+len("_SIBSUNIT_"):en])
                    # now we need to load these units into actual BuildUnits

                    for su in sibsunits:
                        su = su.strip()
                        if su == "":
                            continue
                        cu = CmakeUnitLoad(su, p)
                        # now we need to convert this to a BuildUnit
                        if cu.out_type == "UNKNOWN":
                            continue

                        bu = BuildUnit(prefix+cmakename+"_"+cu.name, cu.out_type, {})
                        bu.dat['DEPS'] = ""
                        bu.skip = True
                        # some includes will have special things in them:
                        # $<BUILD_INTERFACE:...> replaces with ...
                        # $<INSTALL_INTERFACE:...> replaces with ...
                        # these are converted to absolite IF they aren't already
                        # relative to the cmake directory
                        incstr = ""
                        for incn in cu.inc.split(';'):
                            inc = incn.strip()
                            if inc.startswith("$<BUILD_INTERFACE:"):
                                inc = inc[len("$<BUILD_INTERFACE:"):-1]
                            if inc.startswith("$<INSTALL_INTERFACE:"):
                                inc = inc[len("$<INSTALL_INTERFACE:"):-1]
                            if not os.path.isabs(inc):
                                inc = os.path.join(os.getcwd(), p, inc)
                            incstr += f" -I {inc}"

                        bu.incstr = incstr
                        if cu.out_type == "STATIC":
                            bu.static.append(cu.out)
                        elif cu.out_type == "DYN":
                            bu.dynamic.append(cu.out)
                        bu.thisoutput = cu.out
                        # we need to make builddir relative to the original path
                        builddir = os.path.abspath(builddir)
                        # commands.append("cmake --build "+builddir+" --target "+cu.name)
                        bu.cmakebuilddir = builddir
                        bu.cmaketarget = cu.name

                        bu.directory = os.path.abspath(p)
                        bu.prefix = prefix
                        units.append(bu)
            else:
                cmakeprojs.append(line.strip())
                continue
        
        if line.startswith("SIBS") and level == 0:
            # exact same as CMAKE syntax SIBS(name) {
            #     toimport dir
            # }
            # creates units named name_xxx for each unit in the directories build config
            nmst = line.find('(')+1
            nmen = line.find(')')
            name = line[nmst:nmen].strip()
            level += 1
            sibsmode = True
            sibsname = name
            sibsdirs = []
            continue
        if sibsmode and level == 1:
            if line.endswith('}'):
                sibsmode = False
                level -= 1
                for d in sibsdirs:
                    # way simpler than cmake
                    print(f"SIBS {d}")
                    a,b = loadunits(d, prefix=prefix+sibsname)
                    units += a
                    commands += b
            else:
                sibsdirs.append(line.strip())
                continue

        if line.startswith("GIT") and level == 0:
            # GIT {
            #     https://github.com/glfw/glfw/git lib/glfw
            #     URL DIR [TAG]
            # }
            level += 1
            gitmode = True
            continue
        if gitmode and level == 1:
            if line.endswith('}'):
                gitmode = False
                level -= 1
                continue
            else:
                # we need to split the line into two parts
                # the first part is the url
                # the second part is the directory
                stp = line.strip().split()
                url = stp[0]
                directory = stp[1]
                directory = directory.replace("$BUILDDIR", os.path.join(firstpath, "build"))
                if len(stp) > 2:
                    tag = stp[2]
                else:
                    tag = "HEAD"
                # git doesn't import any units, just clones the repo
                # if it already exists and is correct, just git pull
                print(f"GIT {directory}")
                if os.path.exists(directory):
                    # set origin to the url
                    a = subprocess.run(["git", "remote", "set-url", "origin", url], cwd=directory, capture_output=True)
                    if a.returncode != 0:
                        print(f"Error: git remote set-url failed for {directory}:")
                        print(a.stderr.decode())
                        exit(1)
                    a = subprocess.run(["git", "fetch", "origin", tag], cwd=directory, capture_output=True)
                    if a.returncode != 0:
                        print(f"Error: git fetch failed for {directory}:")
                        print(a.stderr.decode())
                        exit(1)
                    a = subprocess.run(["git", "reset", "--hard", tag], cwd=directory, capture_output=True)
                    if a.returncode != 0:
                        print(f"Error: git reset failed for {directory}:")
                        print(a.stderr.decode())
                        exit(1)
                    # a = subprocess.run(["git", "clean", "-f", "-d"], cwd=directory, capture_output=True)
                    # if a.returncode != 0:
                    #     print(f"Error: git clean failed for {directory}:")
                    #     print(a.stderr.decode())
                    #     exit(1)
                
                    a = subprocess.run(["git", "pull", "origin", tag], cwd=directory, capture_output=True)
                    if a.returncode != 0:
                        print(f"Error: git pull failed for {directory}:")
                        print(a.stderr.decode())
                        exit(1)

                else:
                    if tag == "HEAD":
                        tag = "master"
                    a = subprocess.run(["git", "clone", url, directory, "--branch", tag], capture_output=True)
                    if a.returncode != 0:
                        print(f"Error: git clone failed for {directory}:")
                        print(a.stderr.decode())

                    
                        exit(1)
                    continue
        
        if line.startswith("UNIT") and level == 0:
            if line[4:].strip()[0] != '(':
                print("No '(' after UNIT!")
                exit(1)
            tyst = line.find('(')+1
            tyen = line.find(')')
            ty = line[tyst:tyen].strip().upper()
            name = line[tyen+1:line.find('{', tyen)].strip()
            if ty not in ['OBJ', 'OBJECT', 'EXEC', 'EXECUTABLE', 'DYN', 'DYNAMIC', 'STATIC', 'SHARED']:
                print(f"Invalid type: '{ty}'")
            if ty == 'OBJECT': ty = 'OBJ'
            if ty == 'EXECUTABLE': ty = 'EXEC'
            if ty == 'DYNAMIC': ty = 'DYN'
            if ty == 'SHARED': ty = 'DYN'
            level += 1
            currentunit = BuildUnit(prefix+name, ty, {})
            # relative to original path, not cwwd
            currentunit.directory = os.path.relpath(os.getcwd(), firstpath)
            continue

        if currentunit != None and level == 1:
            if line.endswith('}'):
                currentunit.prefix = prefix
                units.append(currentunit)
                currentunit = None
                level -= 1
            else:
                asdname = line[:line.find('{')].strip().upper()
                level += 1
            continue
        if currentunit != None and level == 2:
            if line.endswith('}'):
                level -= 1
                currentunit.dat[asdname] = data
                data = ""
            else:
                data += line + "\n"

    print(f"Configuring done ({len(units)} units), Optimizing...")

    toremove = []
    if not os.path.exists(firstpath+"/build"):
        os.makedirs(firstpath+"/build")
    unusedlog = firstpath+"/build/unused.txt"
    for unit in units:
        if unit.skip:
            # this is a cmake unit, we need to add the command
            # cmake units only need to be built if they are used
            used = getused(units, unit.name)
            if len(used) == 0:
                toremove.append(unit)
                with open(unusedlog, "a") as f:
                    f.write(f"{unit.name}\n")
                continue
            
            # hash all files in the cmake directory (excluting the files under .git and .sibscmakebuild)
            # if the hash changes, we need to recompile
            hashd = hashdir(unit.directory)
            unit.changed = False
            unit.directory = os.path.relpath(unit.directory, firstpath)
            unit.directory = os.path.normpath(unit.directory)
            if not sibsopt_nohcache and not sibsopt_nohashdir:
                if unit.directory in hashcache.hcache:
                    if hashd != hashcache.gethash(unit.directory):
                        unit.changed = True
                        hashcache.setbytes(unit.directory, hashd)
                else:
                    hashcache.setbytes(unit.directory, hashd)
                    unit.changed = True
            else:
                unit.changed = True
            if not unit.changed:
                # check if output exists
                if not os.path.exists(unit.thisoutput):
                    unit.changed = True
            
            if not unit.changed:
                print(f"Skipping cmake unit '{unit.name}' (no changes)")

            if unit.cmakebuilddir != "" and unit.changed:
                commands.append("cmake --build "+unit.cmakebuilddir+" --target "+unit.cmaketarget)
                continue
        
        if 'SOURCES' in unit.dat:
            # change sources that contain "**" or "*" to glob
            newsources = ""
            for source in unit.dat['SOURCES'].split('\n'):
                source = source.strip().replace("$BUILDDIR", os.path.join(firstpath, "build"))
                if source.strip() == "":
                    continue
                if source.find("**") != -1 or source.find("*") != -1:
                    newsources += "\n".join(glob.glob(source.strip(), recursive=True))+"\n"
                else:
                    newsources += source.strip()+"\n"
            unit.dat['SOURCES'] = newsources

        # only if this is our unit, not imported from another
        if unit.directory != os.path.relpath(os.getcwd(), firstpath):
            continue
        if 'INCLUDE' in unit.dat:
            includes = unit.dat['INCLUDE'].split('\n')
            for inc in includes:
                if inc.strip() == "":
                    continue
                # if inc is a relative path, make it absolute
                if not os.path.isabs(inc):
                    inc = os.path.join(unit.directory, inc)
                unit.incstr += f" -I {inc}"
        if unit.skip:
            continue
        # create the outputs
        if unit.out_type == 'DYN':
            out = "build/"+unit.name+dynprefix
            unit.dynamic.append(out)
            unit.thisoutput = out
            if 'LINK' not in unit.dat:
                unit.dat['LINK'] = defaultdyn
            unit.dolink = True
        elif unit.out_type == 'STATIC':
            out = "build/"+unit.name+staticprefix
            unit.static.append(out)
            unit.thisoutput = out
            if 'LINK' not in unit.dat:
                unit.dat['LINK'] = defaultstatic
            unit.dolink = True
        elif unit.out_type == 'EXEC':
            out = "build/"+unit.name+execprefix
            unit.thisoutput = out
            if 'LINK' not in unit.dat:
                unit.dat['LINK'] = defaultexec
            unit.dolink = True
        
        if 'SOURCES' in unit.dat:
            unit.docompile = True
            if 'COMPILE' not in unit.dat:
                unit.dat['COMPILE'] = defaultobj
            sources = unit.dat['SOURCES'].split('\n')
            unit.changed = False
            newsources = ""
            for source in sources:
                if source.strip() == "":
                    continue
                src = os.path.normpath(os.path.join(unit.directory, source.strip()))
                srchash = strhash(unit.name+":"+src)
                if not sibsopt_nohcache:
                    if not unit.changed:
                        if srchash not in hashcache.hcache:
                            hashcache.setbytes(srchash, filedigest(source.strip()))
                            unit.changed = True
                        else:
                            if hashcache.gethash(srchash) != filedigest(source.strip()):
                                hashcache.setbytes(srchash, filedigest(source.strip()))
                                unit.changed = True
                else:
                    unit.changed = True
                if source.strip().endswith(".h") or source.strip().endswith(".hpp"):
                    continue
                newsources += source.strip()+"\n"
                # we need to make the source relative to the unit directory
                # we also need to make the soure path normalized (remove .. and .)
                # also make it so that it is always the exact same string for the same source ex:
                # "src/main.c" and "src\\main.c" are the same, the second will be converted to "src/main.c"
                src = os.path.normpath(os.path.join(unit.directory, source.strip()))
                srchash = strhash(unit.name+":"+src)
                # add to unit.objects
                unit.objects.append("build/obj/"+srchash+".o")
                
            unit.dat['SOURCES'] = newsources

            if not unit.changed:
                # check hash of build file
                path = os.path.join(unit.directory, "sibs.txt")
                path = os.path.normpath(path)
                if not sibsopt_nohcache:
                    if path not in hashcache.hcache:
                        hashcache.setbytes(path, filedigest("sibs.txt"))
                        unit.changed = True
                    else:
                        if hashcache.gethash(path) != filedigest("sibs.txt"):
                            hashcache.setbytes(path, filedigest("sibs.txt"))
                            unit.changed = True
                else:
                    unit.changed = True

                        
    for unit in toremove:
        units.remove(unit)

    print(f"Optimizing done ({len(units)} units)")

    os.chdir(originalpath)
    return (units, commands)


def docompile(units: list[BuildUnit], unit: BuildUnit) -> list[str]:
    commands = []
    if not unit.docompile:
        return []
    if 'COMPILE' not in unit.dat:
        return []
    if 'SOURCES' not in unit.dat:
        return []
    sources = unit.dat['SOURCES'].split('\n')
    compiles = unit.dat['COMPILE'].split('\n')
    deps = getdeps(units, unit)
    depinc = ""
    needa_compile = False
    if unit.changed:
        needa_compile = True
    depobjs = []
    for dep in deps:
        if dep.out_type == 'OBJ':
            depobjs += dep.objects
        elif dep.out_type != 'DYN' and dep.out_type != 'STATIC':
            print(f"Invalid dependency '{dep.name}' (type '{dep.out_type}') for unit '{unit.name}'")
        depinc += dep.incstr
        if dep.changed:
                needa_compile = True
    if not needa_compile:
        return []
    for source in sources:
        if source.strip() == "":
            continue
        src = os.path.normpath(os.path.join(unit.directory, source.strip()))
        srchash = strhash(unit.name+":"+src)
        out = f"build/obj/{srchash}.o"
        if out in unit.objects:
            for command in compiles:
                if command.strip() == "":
                    continue
                commands.append(command.replace("$SRC", src).replace("$OUT", out)+depinc+unit.incstr)
        else:
            print(f"Error: source '{src}' not configured for unit '{unit.name}'")
            exit(1)
    
    return commands

def dolink(units: list[BuildUnit], unit: BuildUnit) -> list[str]:
    commands = []
    if not unit.dolink:
        return []
    if 'LINK' not in unit.dat:
        return []
    
    objs = unit.objects
    depobjs = []
    deplibs = []
    needa_link = False
    if unit.changed:
        needa_link = True
    for dep in getdeps(units, unit):
        if dep.changed:
            needa_link = True
        if dep.out_type == 'OBJ':
            depobjs += dep.objects
        elif dep.out_type == 'DYN':
            deplibs += dep.dynamic
        elif dep.out_type == 'STATIC':
            deplibs += dep.static
        else:
            print(f"Invalid dependency '{dep.name}' (type '{dep.out_type}')")
            exit(1)
    if not needa_link:
        return []
    for command in unit.dat['LINK'].split('\n'):
        if command.strip() == "":
            continue
        commands.append(command.replace('$SRC', ' '.join(objs+depobjs+deplibs)).replace('$OUT', unit.thisoutput))
    
    return commands

def compilecmd(cmd: str) -> str:
    # we need to replace the cxx cxxl, cc, ccl, and ar commands
    # TODO: we should make this more robust for other compilers
    # cmd = cmd.replace("$CXXL", "g++")
    # cmd = cmd.replace("$CXX", "g++")
    # cmd = cmd.replace("$CCL", "gcc")
    # cmd = cmd.replace("$CC", "gcc")
    # cmd = cmd.replace("$AR", "ar")
    # if the command contains CC or CXX add cflags
    # if the command contains CC add ccflags
    # if the command contains CXX add cxxflags
    # if the command contains CCL or CXXL add ldflags
    # if the command contains CCL add cclflags
    # if the command contains CXXL add cxxlflags
    # if the command contains AR add arflags
    cmd = cmd.replace("$CXXL", f"g++ {sibsopt_ldflags} {sibsopt_cxxlflags}")
    cmd = cmd.replace("$CXX", f"g++ {sibsopt_cflags} {sibsopt_cxxflags}")
    cmd = cmd.replace("$CCL", f"gcc {sibsopt_ldflags} {sibsopt_cclflags}")
    cmd = cmd.replace("$CC", f"gcc {sibsopt_cflags} {sibsopt_ccflags}")
    cmd = cmd.replace("$AR", f"ar {sibsopt_arflags}")

    return cmd



def main():
    global sibsopt_nohashdir
    global sibsopt_nohcache
    global sibsopt_cflags
    global sibsopt_ccflags
    global sibsopt_ldflags
    global sibsopt_cxxflags
    global sibsopt_cxxlflags
    global sibsopt_cclflags
    global sibsopt_arflags
    global sibsopt_showcommands
    charg = ""
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg.startswith("--"):
                if arg == "--nohashdir" or arg == "--nocmakepersist":
                    sibsopt_nohashdir = True
                elif arg == "--nohcache" or arg == "--nopersist":
                    sibsopt_nohcache = True
                elif arg.startswith("--cflags="):
                    sibsopt_cflags += " "+arg[len("--cflags="):]
                elif arg.startswith("--ccflags="):
                    sibsopt_ccflags += " "+arg[len("--ccflags="):]
                elif arg.startswith("--ldflags="):
                    sibsopt_ldflags += " "+arg[len("--ldflags="):]
                elif arg.startswith("--cxxflags="):
                    sibsopt_cxxflags += " "+arg[len("--cxxflags="):]
                elif arg.startswith("--cxxlflags="):
                    sibsopt_cxxlflags += " "+arg[len("--cxxlflags="):]
                elif arg.startswith("--cclflags="):
                    sibsopt_cclflags += " "+arg[len("--cclflags="):]
                elif arg.startswith("--arflags="):
                    sibsopt_arflags += " "+arg[len("--arflags="):]
                elif arg.startswith("--debug"):
                    sibsopt_cflags += " -g"
                    sibsopt_ldflags += " -g"
                elif arg.startswith("--showcommands"):
                    sibsopt_showcommands = True
                elif arg == "--help":
                    print("SIBS: Simply Integrated Build System")
                    print("Version: v"+sibsversion)
                    print("Usage:")
                    print("python -m sibs (directory) (--nocmakepersist/--nohashdir --nohcache/--nopersist --cflags=... --ccflags=... --ldflags=... --cxxflags=... --cxxlflags=... --cclflags=... --arflags=... --debug --help)")
                    print("Options:")
                    print("    --nocmakepersist/nohashdir: Cmake imported projects will not persist between builds, this will make them rebuild every time (very slow)")
                    print("    --nohcache/nopersist: Don't use the hash cache, this will make even local projects rebuild every time, no matter if there are changes (very slow)")
                    print("    --cflags=: Extra flags to pass to the C/C++ compiler")
                    print("    --ccflags=: Extra flags to pass to the C compiler")
                    print("    --cxxflags=: Extra flags to pass to the C++ compiler")
                    print("    --ldflags=: Extra flags to pass to the C/C++ linker")
                    print("    --cclflags=: Extra flags to pass to the C linker")
                    print("    --cxxlflags=: Extra flags to pass to the C++ linker")
                    print("    --arflags=: Extra flags to pass to the archiver")
                    print("    --debug: Adds -g to all compile commands")
                    print("    --showcommands: Shows the commands that will be executed")

                    print("    --help: Print this help message")
                    exit(0)

                else:
                    print(f"Unknown option '{arg}'")
            else:
                if charg != "":
                    print(f"Unknown option '{charg}'")
                charg = arg
  
    if charg == "":
        charg = "."

    if not os.path.exists(os.path.join(charg, "sibs.txt")):
        print("Error: no sibs.txt file found!")
        exit(1)



    units, cmds = loadunits(charg)

    print(f"Building unit commands...")
    
    for unit in units:
        if unit.skip:
            continue
        
        if unit.docompile:
            cmds += docompile(units, unit)
        if unit.dolink:
            cmds += dolink(units, unit)

    os.makedirs("build/obj/", exist_ok=True)
    os.makedirs("build/cmake/", exist_ok=True)

    print(f"Building unit commands done ({len(cmds)} commands)")
    for cmd in cmds:
        c = compilecmd(cmd)
        if sibsopt_showcommands:
            print(c)
        os.system(c)
    
    if len(cmds) == 0:
        print("Nothing to build!")
        exit(0)

    for unit in units:
        if unit.skip and unit.thisoutput != None and unit.thisoutput.strip() != "": # this means cmake
            # copy the cmake output to the build directory
            print(f"Copying {unit.thisoutput} to build/"+os.path.basename(unit.thisoutput))
            shutil.copyfile(unit.thisoutput, "build/"+os.path.basename(unit.thisoutput))
