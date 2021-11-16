#!/usr/bin/env python3

import grp
import json
import os
import shutil
import stat
from pathlib import Path

import utils

if __name__ == "__main__":
    print(
        "Have you installed the following programs/projects: llvm, clang, gcc, cmake, ccomp, csmith and creduce?"
    )
    print("Press enter to continue if you believe you have")
    input()

    not_found = []
    for p in ["clang", "gcc", "cmake", "ccomp", "csmith", "creduce"]:
        if not shutil.which(p):
            not_found.append(p)

    if not_found:
        print("Can't find", " ".join(not_found), " in $PATH.")

    if not Path("/usr/include/llvm/").exists():
        print("Can't find /usr/include/llvm/")
        not_found.append("kill")

    if not_found:
        exit(1)

    print("Creating default ~/.config/dce/config.json...")

    path = Path.home() / ".config/dce/config.json"
    if path.exists():
        print(f"{path} already exists! Aborting to prevent overriding data...")
        exit(1)

    config = {}
    # ====== GCC ======
    gcc = {}
    gcc["name"] = "gcc"
    gcc["main_branch"] = "master"

    # Git clone repo
    print("Cloning gcc to ./gcc ...")
    if not Path("./gcc").exists():
        utils.run_cmd("git clone git://gcc.gnu.org/git/gcc.git")
    gcc["repo"] = "./gcc"

    if shutil.which("gcc"):
        gcc["sane_version"] = "gcc"
    else:
        gcc["sane_version"] = "???"
        print(
            "gcc is not in $PATH, you have to specify the executable yourself in gcc.sane_version"
        )

    gcc["patches"] = [
        "./patches/" + patch
        for patch in os.listdir("./patches")
        if patch.startswith("gcc-")
    ]

    gcc["releases"] = [
        "trunk",
        "releases/gcc-11.2.0",
        "releases/gcc-11.1.0",
        "releases/gcc-10.3.0",
        "releases/gcc-10.2.0",
        "releases/gcc-10.1.0",
        "releases/gcc-9.4.0",
        "releases/gcc-9.3.0",
        "releases/gcc-9.2.0",
        "releases/gcc-9.1.0",
        "releases/gcc-8.5.0",
        "releases/gcc-8.4.0",
    ]
    config["gcc"] = gcc

    # ====== LLVM ======
    llvm = {}
    llvm["name"] = "llvm"
    llvm["main_branch"] = "main"

    # Git clone repo
    print("Cloning llvm to ./llvm-project ...")
    if not Path("./llvm-project").exists():
        utils.run_cmd("git clone https://github.com/llvm/llvm-project")
    llvm["repo"] = "./llvm-project"

    if shutil.which("clang"):
        llvm["sane_version"] = "clang"
    else:
        llvm["sane_version"] = "???"
        print(
            "clang is not in $PATH, you have to specify the executable yourself in llvm.sane_version"
        )

    llvm["patches"] = [
        "./patches/" + patch
        for patch in os.listdir("./patches")
        if patch.startswith("llvm-")
    ]

    llvm["releases"] = [
        "trunk",
        "llvmorg-13.0.0",
        "llvmorg-12.0.1",
        "llvmorg-12.0.0",
        "llvmorg-11.1.0",
        "llvmorg-11.0.1",
        "llvmorg-11.0.0",
        "llvmorg-11.0.0",
        "llvmorg-10.0.1",
        "llvmorg-10.0.0",
    ]

    config["llvm"] = llvm
    # ====== CSmith ======
    csmith = {}
    csmith["max_size"] = 50000
    csmith["min_size"] = 10000
    if shutil.which("csmith"):
        csmith["executable"] = "csmith"
        res = utils.run_cmd("csmith --version")
        # $ csmith --version
        # csmith 2.3.0
        # Git version: 30dccd7
        version = res.split("\n")[0].split()[1]
        csmith["include_path"] = "/usr/include/csmith-" + version
    else:
        print(
            "Can't find csmith in $PATH. You have to specify the executable and the include path yourself"
        )
        csmith["executable"] = "???"
        csmith["include_path"] = "???"
    config["csmith"] = csmith

    # ====== Cpp programs ======

    print("Compiling instrumenter...")
    os.makedirs("./dce_instrumenter/build", exist_ok=True)
    utils.run_cmd(
        "cmake .. -DLT_LLVM_INSTALL_DIR=/usr",
        working_dir=Path("./dce_instrumenter/build/"),
    )
    utils.run_cmd("make -j", working_dir=Path("./dce_instrumenter/build/"))
    config["dcei"] = "./dce_instrumenter/build/bin/dcei"
    config["static_annotator"] = "./dce_instrumenter/build/bin/static-annotator"

    print("Compiling callchain checker (ccc)...")
    os.makedirs("./callchain_checker/build", exist_ok=True)
    utils.run_cmd("cmake ..", working_dir=Path("./callchain_checker/build/"))
    utils.run_cmd("make -j", working_dir=Path("./callchain_checker/build/"))
    config["ccc"] = "./callchain_checker/build/bin/ccc"

    # ====== Rest ======
    config["patchdb"] = "./patches/patchdb.json"

    os.makedirs("logs", exist_ok=True)
    config["logdir"] = "./logs"

    config["cache_group"] = grp.getgrgid(os.getgid()).gr_name

    os.makedirs("compiler_cache", exist_ok=True)
    shutil.chown("compiler_cache", group=config["cache_group"])
    os.chmod("compiler_cache", 0o770 | stat.S_ISGID)
    config["cachedir"] = "./compiler_cache"

    config["creduce"] = "creduce"
    if not shutil.which("creduce"):
        print(
            "creduce was not found in $PATH. You have to specify the executable yourself"
        )
        config["creduce"] = "???"

    config["ccomp"] = "ccomp"
    if not shutil.which("ccomp"):
        print(
            "ccomp was not found in $PATH. You have to specify the executable yourself"
        )
        config["ccomp"] = "???"

    print("Saving config...")
    with open(path, "w") as f:
        json.dump(config, f, indent=4)

    print("Example command:")
    print(
        "Generate, reduce and bisect 10 cases for LLVM trunk O3 vs. LLVM 13.0.0 O3 and LLVM 12.0.1 O3 into directory ./llvmo3 with 64 processes"
    )
    print(
        "\t./bisector.py -g -a 10 -t llvm trunk 3 -ac llvm llvmorg-13.0.0 3 llvm llvmorg-12.0.1 3 -d ./llvmo3 -p 64"
    )
    print("Get additional output with `-ll info`")