#!/usr/bin/env python3

import dataclasses
import logging
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

import builder
import parsers
import patchdatabase
import utils


# ==================== Sanitize ====================
def get_cc_output(cc, file, flags, cc_timeout):
    cmd = [
        cc,
        file,
        "-c",
        "-o/dev/null",
        "-Wall",
        "-Wextra",
        "-Wpedantic",
        "-O1",
        "-Wno-builtin-declaration-mismatch",
    ]
    if flags:
        cmd.extend(flags.split())
    # Not using utils.run_cmd because of redirects
    result = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=cc_timeout
    )
    return result.returncode, result.stdout.decode("utf-8")


def check_compiler_warnings(
    clang: str, gcc: str, file: Path, flags: str, cc_timeout: int
) -> bool:
    clang_rc, clang_output = get_cc_output(clang, file, flags, cc_timeout)
    gcc_rc, gcc_output = get_cc_output(gcc, file, flags, cc_timeout)

    if clang_rc != 0 or gcc_rc != 0:
        return False

    warnings = [
        "conversions than data arguments",
        "incompatible redeclaration",
        "ordered comparison between pointer",
        "eliding middle term",
        "end of non-void function",
        "invalid in C99",
        "specifies type",
        "should return a value",
        "uninitialized",
        "incompatible pointer to",
        "incompatible integer to",
        "comparison of distinct pointer types",
        "type specifier missing",
        "uninitialized",
        "Wimplicit-int",
        "division by zero",
        "without a cast",
        "control reaches end",
        "return type defaults",
        "cast from pointer to integer",
        "useless type name in empty declaration",
        "no semicolon at end",
        "type defaults to",
        "too few arguments for format",
        "incompatible pointer",
        "ordered comparison of pointer with integer",
        "declaration does not declare anything",
        "expects type",
        "comparison of distinct pointer types",
        "pointer from integer",
        "incompatible implicit",
        "excess elements in struct initializer",
        "comparison between pointer and integer",
        "return type of ‘main’ is not ‘int’",
        "past the end of the array",
        "no return statement in function returning non-void",
    ]

    ws = [w for w in warnings if w in clang_output or w in gcc_output]
    if len(ws) > 0:
        logging.debug(f"Compiler warnings found: {ws}")
        return False

    return True


@contextmanager
def ccomp_env() -> Path:
    td = tempfile.TemporaryDirectory()
    tempfile.tempdir = td.name
    try:
        yield Path(td.name)
    finally:
        tempfile.tempdir = None


def verify_with_ccomp(
    ccomp: str, file: Path, flags: str, compcert_timeout: int
) -> bool:
    with ccomp_env() as tmpdir:
        cmd = [
            ccomp,
            str(file),
            "-interp",
            "-fall",
        ]
        if flags:
            cmd.extend(flags.split())
        res = True
        try:
            utils.run_cmd(
                cmd, additional_env={"TMPDIR": str(tmpdir)}, timeout=compcert_timeout
            )
            res = True
        except subprocess.CalledProcessError:
            res = False
        except subprocess.TimeoutExpired:
            res = False

        logging.debug(f"CComp returncode {res}")
        return res


def use_ub_sanitizers(
    clang: str, file: Path, flags: str, cc_timeout: int, exe_timeout: int
):
    cmd = [clang, str(file), "-O1", "-fsanitize=undefined,address"]
    if flags:
        cmd.extend(flags.split())

    with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as exe:
        exe.close()
        os.chmod(exe.name, 0o777)
        cmd.append(f"-o{exe.name}")
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=cc_timeout,
        )
        if result.returncode != 0:
            logging.debug(f"UB Sanitizer returncode {result.returncode}")
            if os.path.exists(exe.name):
                os.remove(exe.name)
            return False
        result = subprocess.run(
            exe.name,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=exe_timeout,
        )
        os.remove(exe.name)
        logging.debug(f"UB Sanitizer returncode {result.returncode}")
        return result.returncode == 0


def sanitize(
    gcc: str,
    clang: str,
    ccomp: str,
    file: Path,
    flags: str,
    cc_timeout=8,
    exe_timeout=2,
    compcert_timeout=16,
):
    # Taking advantage of shortciruit logic...
    return (
        check_compiler_warnings(gcc, clang, file, flags, cc_timeout)
        and use_ub_sanitizers(clang, file, flags, cc_timeout, exe_timeout)
        and verify_with_ccomp(ccomp, file, flags, compcert_timeout)
    )


# ==================== Checker ====================


def annotate_program_with_static(annotator, file, include_paths):
    cmd = [annotator, file]
    for path in include_paths:
        cmd.append(f"--extra-arg=-isystem{path}")
    try:
        utils.run_cmd(cmd)
    except subprocess.CalledProcessError as e:
        raise Exception("Static annotator failed to annotate {file}! {e}")


class Checker:
    def __init__(self, config: utils.NestedNamespace, bldr: builder.Builder):
        self.config = config
        self.builder = bldr
        return

    def is_interesting_wrt_marker(self, case: utils.ReduceCase) -> bool:
        # Checks if the bad_setting does include the marker and
        # all the good settings do not.

        marker_prefix = utils.get_marker_prefix(case.marker)
        found_in_bad = builder.find_alive_markers(
            case.code, case.bad_setting, marker_prefix, self.builder
        )
        uninteresting = False
        if case.marker not in found_in_bad:
            uninteresting = True
        for good_setting in case.good_settings:
            found_in_good = builder.find_alive_markers(
                case.code, good_setting, marker_prefix, self.builder
            )
            if case.marker in found_in_good:
                uninteresting = True
                break
        return not uninteresting

    def is_interesting_wrt_ccc(self, case: utils.ReduceCase) -> bool:
        # Checks if there is a callchain between main and the marker
        with tempfile.NamedTemporaryFile(suffix=".c") as tf:
            with open(tf.name, "w") as f:
                f.write(case.code)

            # TODO: Handle include_paths better
            include_paths = utils.find_include_paths(
                self.config.llvm.sane_version,
                tf.name,
                f"-I{self.config.csmith.include_path}",
            )
            cmd = [self.config.ccc, tf.name, "--from=main", f"--to={case.marker}"]

            for path in include_paths:
                cmd.append(f"--extra-arg=-isystem{path}")
            try:
                result = utils.run_cmd(cmd, timeout=8)
                return (
                    f"call chain exists between main -> {case.marker}".strip()
                    == result.strip()
                )
            except subprocess.CalledProcessError:
                logging.debug("CCC failed")
                return False
            except subprocess.TimeoutExpired:
                logging.debug("CCC timed out")
                return False

    def is_interesting_with_static_globals(self, case: utils.ReduceCase) -> bool:
        # TODO: Why do we do this?

        with tempfile.NamedTemporaryFile(suffix=".c") as tf:
            with open(tf.name, "w") as new_cfile:
                print(case.code, file=new_cfile)

            # TODO: Handle include_paths better
            include_paths = utils.find_include_paths(
                self.config.llvm.sane_version,
                tf.name,
                f"-I{self.config.csmith.include_path}",
            )
            annotate_program_with_static(
                self.config.static_annotator, tf.name, include_paths
            )

            with open(tf.name, "r") as annotated_file:
                static_code = annotated_file.read()

            asm_bad = builder.get_asm_str(static_code, case.bad_setting, self.builder)
            uninteresting = False
            if case.marker not in asm_bad:
                uninteresting = True
            for good_setting in case.good_settings:
                asm_good = builder.get_asm_str(static_code, good_setting, self.builder)
                if case.marker in asm_good:
                    uninteresting = True
                    break
            return not uninteresting

    def is_interesting_with_empty_marker_bodies(self, case: utils.ReduceCase):

        marker_prefix = utils.get_marker_prefix(case.marker)
        p = re.compile(f"void {marker_prefix}(.*)\(void\);")
        empty_body_code = ""
        for line in case.code.split("\n"):
            m = p.match(line)
            if m:
                empty_body_code += f"\nvoid {marker_prefix}{m.group(1)}(void){{}}"
            else:
                empty_body_code += f"\n{line}"

        with tempfile.NamedTemporaryFile(suffix=".c") as tf:
            with open(tf.name, "w") as f:
                f.write(empty_body_code)

            return sanitize(
                self.config.gcc.sane_version,
                self.config.llvm.sane_version,
                self.config.ccomp,
                Path(tf.name),
                f"-I{self.config.csmith.include_path}",
            )

    def is_interesting(self, case: utils.ReduceCase):
        # TODO: Optimization potential. Less calls to clang etc.
        # when tests are combined.

        # Taking advantage of shortciruit logic
        return (
            self.is_interesting_wrt_marker(case)
            and self.is_interesting_wrt_ccc(case)
            and self.is_interesting_with_static_globals(case)
            and self.is_interesting_with_empty_marker_bodies(case)
        )


def copy_flag(
    frm: utils.CompilerSetting, to: list[utils.CompilerSetting]
) -> list[utils.CompilerSetting]:
    res: list[utils.CompilerSetting] = []
    for setting in to:
        cpy = dataclasses.replace(setting)
        cpy.additional_flags = frm.additional_flags
        res.append(cpy)
    return res


def override_bad(
    case: utils.ReduceCase, override_settings: list[utils.CompilerSetting]
) -> list[utils.ReduceCase]:
    res = []
    bsettings = copy_flag(case.bad_setting, override_settings)
    for s in bsettings:
        cpy = dataclasses.replace(case)
        cpy.bad_setting = s
        res.append(cpy)
    return res


def override_good(
    case: utils.ReduceCase, override_settings: list[utils.CompilerSetting]
) -> utils.ReduceCase:
    gsettings = copy_flag(case.good_settings[0], override_settings)
    cpy = dataclasses.replace(case)
    cpy.good_settings = gsettings
    return cpy


if __name__ == "__main__":
    config, args = utils.get_config_and_parser(parsers.checker_parser())

    patchdb = patchdatabase.PatchDB(config.patchdb)
    bldr = builder.Builder(config, patchdb, args.cores)
    chkr = Checker(config, bldr)

    bad_settings = []
    if args.bad_settings:
        bad_settings = utils.get_compiler_settings(
            config, args.bad_settings, args.bad_settings_default_opt_levels
        )

    good_settings = []
    if args.good_settings:
        good_settings = utils.get_compiler_settings(
            config, args.good_settings, args.good_settings_default_opt_levels
        )

    cases_to_test: list[utils.ReduceCase] = []
    check_marker: bool = False
    if args.bad_settings and args.good_settings:
        # Override all options defined in the case
        with open(args.file, "r") as f:
            code = f.read()

        cases_to_test = [
            utils.ReduceCase(code, "", bs, good_settings) for bs in bad_settings
        ]
        check_marker = True

    elif args.bad_settings and not args.good_settings:
        # TODO: Get flags from somewhere. For now,
        # take the ones from the first config.
        case = utils.ReduceCase.from_file(args.file, config)

        cases_to_test = override_bad(case, bad_settings)

    elif not args.bad_settings and args.good_settings:
        case = utils.ReduceCase.from_file(args.file, config)

        cases_to_test = [override_good(case, good_settings)]

    else:
        cases_to_test = [utils.ReduceCase.from_file(args.file, config)]

    if args.marker is not None:
        for c in cases_to_test:
            c.marker = args.marker
    elif check_marker:
        raise Exception("You need to specify a marker")

    if all(chkr.is_interesting(c) for c in cases_to_test):
        sys.exit(0)
    else:
        sys.exit(1)