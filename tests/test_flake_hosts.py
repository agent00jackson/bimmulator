"""Evaluate host package composition without building QEMU or loading firmware."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FlakeHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        nix = os.environ.get("IDRIVE_NIX") or shutil.which("nix")
        if not nix:
            raise unittest.SkipTest("Nix is required to evaluate the two-host package matrix")
        # Explicit path: includes new source files in an uncommitted checkout.
        reference = json.dumps("path:" + str(ROOT))
        expression = '''let
          flake = builtins.getFlake REFERENCE;
          describe = system: let
            packages = flake.packages.${system};
            shell = flake.devShells.${system}.default;
          in {
            names = builtins.attrNames packages;
            omapFlags = packages.qemu-omap5.configureFlags;
            kombiFlags = packages.qemu-capricorn-m.configureFlags;
            omapInstall = packages.qemu-omap5.postInstall;
            kombiInstall = packages.qemu-capricorn-m.postInstall;
            server = shell.IDRIVE_ANGLE_SERVER;
            lzo = shell.IDRIVE_LZO_LIBRARY;
            launcher = packages.idrive6.text;
            graphicsInstall = (packages.mesa-egl-rpc-server or packages.angle-egl-rpc-server).installPhase;
          };
        in {
          systems = builtins.attrNames flake.packages;
          darwin = describe "aarch64-darwin";
          linux = describe "x86_64-linux";
        }'''.replace("REFERENCE", reference)
        result = subprocess.run(
            [nix, "--extra-experimental-features", "nix-command flakes", "eval", "--impure",
             "--json", "--expr", expression], cwd=ROOT, capture_output=True, text=True, timeout=300,
        )
        if result.returncode:
            raise AssertionError("host matrix evaluation failed:\n" + result.stderr)
        cls.matrix = json.loads(result.stdout)

    def test_exported_host_and_graphics_package_matrix(self):
        self.assertEqual(self.matrix["systems"], ["aarch64-darwin", "x86_64-linux"])
        for host, expected, absent in (("darwin", "angle-egl-rpc-server", "mesa-egl-rpc-server"),
                                      ("linux", "mesa-egl-rpc-server", "angle-egl-rpc-server")):
            with self.subTest(host=host):
                record = self.matrix[host]
                self.assertIn(expected, record["names"])
                self.assertNotIn(absent, record["names"])
                self.assertTrue(record["server"].endswith("/bin/" + expected))
                self.assertIn(record["server"], record["launcher"])

    def test_guest_targets_and_native_qemu_kvm_alias_targets(self):
        for host, native in (("darwin", "aarch64"), ("linux", "x86_64")):
            for machine, guest in (("omap", "arm"), ("kombi", "mips64el")):
                with self.subTest(host=host, machine=machine):
                    record = self.matrix[host]
                    self.assertIn(f"--target-list={guest}-softmmu,{native}-softmmu", record[machine + "Flags"])
                    self.assertIn(f"/bin/qemu-system-{native}", record[machine + "Install"])

    def test_linux_native_gui_and_platform_lzo_runtime(self):
        self.assertIn("--enable-gtk", self.matrix["linux"]["omapFlags"])
        self.assertNotIn("--enable-gtk", self.matrix["darwin"]["omapFlags"])
        self.assertIn("--enable-cocoa", self.matrix["darwin"]["omapFlags"])
        for host, suffix in (("darwin", ".dylib"), ("linux", ".so")):
            with self.subTest(host=host):
                record = self.matrix[host]
                self.assertTrue(record["lzo"].endswith("/lib/liblzo2" + suffix))
                self.assertIn(record["lzo"], record["launcher"])

    def test_linux_server_composes_software_mesa_runtime(self):
        install = self.matrix["linux"]["graphicsInstall"]
        self.assertIn("--set LIBGL_ALWAYS_SOFTWARE 1", install)
        self.assertIn("--set LIBGL_DRIVERS_PATH", install)
        self.assertIn("/lib/dri", install)
        self.assertIn("--set __EGL_VENDOR_LIBRARY_FILENAMES", install)
        self.assertIn("/share/glvnd/egl_vendor.d/50_mesa.json", install)
        self.assertNotIn("wrapProgram", self.matrix["darwin"]["graphicsInstall"])
