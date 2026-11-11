"""Exercise the real Mesa builder with host-only executable fixtures; no VM/GL."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


BUILDER = Path(__file__).resolve().parents[1] / "tools/build-mesa-egl-rpc-server.sh"


class MesaBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mesa-builder-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.log = self.base / "compiler.json"
        self.pkg_log = self.base / "pkg-config.jsonl"
        # Absolute interpreters keep a deliberately isolated PATH usable.
        self.make_executable("uname", "#!/bin/sh\n"
                             'case "$1" in -s) echo "${TEST_OS:-Linux}";; '
                             '-m) echo "${TEST_ARCH:-x86_64}";; esac\n')
        self.cxx = self.make_executable("mock c++", f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
args = sys.argv[1:]
pathlib.Path(os.environ["TEST_CXX_LOG"]).write_text(json.dumps(args))
if os.environ.get("TEST_CXX_FAIL"):
    sys.exit(23)
pathlib.Path(args[args.index("-o") + 1]).write_text("mock binary, never executed\\n")
''')
        self.pkg = self.make_executable("pkg-config", f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
args = sys.argv[1:]
with pathlib.Path(os.environ["TEST_PKG_LOG"]).open("a") as log:
    log.write(json.dumps(args) + "\\n")
if os.environ.get("TEST_PKG_FAIL") == args[0]:
    sys.exit(1)
if args[0] == "--cflags":
    print("-I/mock/include -DMOCK_MESA=1")
elif args[0] == "--libs":
    print("-L/mock/lib -lEGL -lGLESv2")
''')
        for tool in ("dirname", "mkdir"):
            (self.bin / tool).symlink_to(shutil.which(tool))
        self.env = {
            "PATH": str(self.bin),
            "CXX": str(self.cxx),
            "PKG_CONFIG": str(self.pkg),
            "TEST_CXX_LOG": str(self.log),
            "TEST_PKG_LOG": str(self.pkg_log),
        }
        self.output = self.base / "output with spaces" / "mesa server"

    def make_executable(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)
        return path

    def run_builder(self, *args, builder=BUILDER, **env):
        return subprocess.run(
            ["/bin/bash", str(builder), *map(str, args)],
            env={**self.env, **env}, cwd=self.base,
            text=True, capture_output=True, timeout=10,
        )

    def assert_rejected(self, result, message):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stderr)
        self.assertFalse(self.log.exists())
        self.assertFalse(self.output.exists())

    def test_actual_compiler_argv_and_output_paths_with_spaces(self):
        result = self.run_builder(self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.output.is_file())
        self.assertIn(str(self.output), result.stdout)
        self.assertEqual(json.loads(self.log.read_text()), [
            "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror", "-DIDRIVE_USE_MESA",
            "-I/mock/include", "-DMOCK_MESA=1", f"-I{BUILDER.parent}",
            str(BUILDER.parent / "angle-egl-rpc-server.cpp"),
            "-L/mock/lib", "-lEGL", "-lGLESv2", "-o", str(self.output),
        ])
        self.assertEqual([json.loads(line) for line in self.pkg_log.read_text().splitlines()], [
            ["--exists", "egl", "glesv2"], ["--cflags", "egl", "glesv2"],
            ["--libs", "egl", "glesv2"],
        ])

    def test_default_output_is_local_to_builder_checkout(self):
        # Relocate the actual script to avoid writing into the source checkout.
        tools = self.base / "checkout with spaces" / "tools"
        tools.mkdir(parents=True)
        builder = tools / BUILDER.name
        builder.write_bytes(BUILDER.read_bytes())
        result = self.run_builder(builder=builder)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = tools.parent / "build/mesa-egl-rpc-server"
        self.assertTrue(output.is_file())
        self.assertEqual(json.loads(self.log.read_text())[-1], str(output))

    def test_default_cxx_and_pkg_config_commands(self):
        (self.bin / "c++").symlink_to(self.cxx)
        result = self.run_builder(self.output, CXX="", PKG_CONFIG="")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_existing_compiler_override_takes_precedence(self):
        result = self.run_builder(self.output, IDRIVE_HOST_CXX=str(self.cxx), CXX="missing-cxx")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_compiler(self):
        self.assert_rejected(self.run_builder(self.output, CXX="no-such-cxx"),
                             "Host C++ compiler is unavailable")

    def test_missing_pkg_config(self):
        self.assert_rejected(self.run_builder(self.output, PKG_CONFIG="no-such-pkg-config"),
                             "pkg-config is unavailable")

    def test_missing_dependencies(self):
        self.assert_rejected(self.run_builder(self.output, TEST_PKG_FAIL="--exists"),
                             "egl glesv2")

    def test_failed_flag_queries(self):
        for query, message in (("--cflags", "compiler flags"), ("--libs", "linker flags")):
            with self.subTest(query=query):
                self.assert_rejected(self.run_builder(self.output, TEST_PKG_FAIL=query), message)

    def test_unsupported_platforms(self):
        for env in ({"TEST_OS": "Darwin"}, {"TEST_ARCH": "aarch64"}):
            with self.subTest(env=env):
                self.assert_rejected(self.run_builder(self.output, **env), "Linux x86_64")

    def test_invalid_arguments(self):
        for args in (("",), (str(self.output), "extra")):
            with self.subTest(args=args):
                self.assert_rejected(self.run_builder(*args), "Usage:")

    def test_relative_output_path(self):
        output = Path("relative output/mesa server")
        result = self.run_builder(output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.base / output).is_file())
        self.assertEqual(json.loads(self.log.read_text())[-1], str(output))

    def test_output_directory_failure_stops_before_compilation(self):
        blocked = self.base / "not a directory"
        blocked.write_text("fixture")
        result = self.run_builder(blocked / "server")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertNotIn("Built host", result.stdout)

    def test_compiler_failure_is_propagated(self):
        result = self.run_builder(self.output, TEST_CXX_FAIL="1")
        self.assertEqual(result.returncode, 23)
        self.assertNotIn("Built host", result.stdout)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
