"""Firmware-free integration checks for a built host EGL server (protocol 3).

Set IDRIVE_TEST_EGL_SERVER to opt in; no VM or existing server is used.
"""
import ctypes
import mmap
import os
import platform
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
import unittest


HEADER = struct.Struct(">4sHH6I")


def words(*values):
    return struct.pack(">" + "I" * len(values), *values)


class EGLServerFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = os.environ.get("IDRIVE_TEST_EGL_SERVER")
        if not binary:
            raise unittest.SkipTest("set IDRIVE_TEST_EGL_SERVER to a built host EGL RPC server")
        cls.binary = str(Path(binary).resolve(strict=True))

    def start_server(self, arguments, extra_environment=None):
        self.log = tempfile.TemporaryFile(mode="w+b")
        self.addCleanup(self.log.close)
        environment = os.environ.copy()
        # The Mesa backend must work without X11/Wayland, even for GUI QEMU runs.
        environment.pop("DISPLAY", None)
        environment.pop("WAYLAND_DISPLAY", None)
        environment["LIBGL_ALWAYS_SOFTWARE"] = "1"
        environment.update(extra_environment or {})
        self.process = subprocess.Popen(
            [self.binary, *arguments], env=environment,
            stdout=self.log, stderr=self.log,
        )
        self.addCleanup(self.stop_server)
        self.sequence = 0

    def setUp(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        self.start_server(["--port", str(self.port)])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            try:
                with self.connect():
                    return
            except OSError:
                time.sleep(0.02)
        self.log.seek(0)
        self.fail("host EGL server did not start:\n" + self.log.read().decode(errors="replace"))

    def stop_server(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def connect(self):
        return socket.create_connection(("127.0.0.1", self.port), timeout=5)

    def receive(self, connection, size):
        data = bytearray()
        while len(data) < size:
            block = connection.recv(size - len(data))
            self.assertTrue(block, "unexpected EOF from host EGL server")
            data.extend(block)
        return bytes(data)

    def request(self, connection, opcode, a=0, b=0, c=0, payload=b"", *,
                magic=b"I6ER", version=3, length=None, sequence=None):
        self.sequence += 1
        sequence = self.sequence if sequence is None else sequence
        connection.sendall(HEADER.pack(magic, version, opcode, 1, a, b, c, sequence,
                                       len(payload) if length is None else length) + payload)
        reply = HEADER.unpack(self.receive(connection, HEADER.size))
        self.assertEqual(reply[:2], (b"I6EA", 3))
        self.assertEqual(reply[-1], sequence)
        self.assertLessEqual(reply[-2], 16 * 1024 * 1024)
        return reply, self.receive(connection, reply[-2])

    def ok(self, connection, opcode, *args, **kwargs):
        reply, payload = self.request(connection, opcode, *args, **kwargs)
        self.assertEqual(reply[2:4], (0, 0), f"RPC opcode {opcode} failed: {reply}")
        return reply[4:7], payload

    def context(self, connection, *, window=False):
        self.ok(connection, 1)  # INITIALIZE
        # RGBA8, depth24, ES2, pbuffer. This also exercises the baseline's
        # window-to-pbuffer config adaptation used by the QNX Screen bridge.
        attrs = words(0x3033, 4, 0x3040, 4, 0x3024, 8, 0x3023, 8,
                      0x3022, 8, 0x3021, 8, 0x3025, 24, 0x3038)
        values, configs = self.ok(connection, 5, b=1, payload=attrs)
        self.assertGreater(values[0], 0)
        config, = struct.unpack(">I", configs)
        if window:
            values, _ = self.ok(connection, 20, config, 2, 2)  # CREATE_WINDOW_SURFACE
        else:
            values, _ = self.ok(connection, 7, config, payload=words(0x3057, 2, 0x3056, 2, 0x3038))
        surface = values[0]
        values, _ = self.ok(connection, 8, config, payload=words(0x3098, 2, 0x3038))
        self.ok(connection, 9, surface, surface, values[0])
        return surface


class EGLServerTests(EGLServerFixture):
    def test_initialize_and_terminate(self):
        with self.connect() as connection:
            values, _ = self.ok(connection, 1)
            self.assertGreaterEqual(values[0], 1)
            _, vendor = self.ok(connection, 3, 0x3053)  # EGL_VENDOR
            self.assertTrue(vendor.rstrip(b"\0"))
            self.ok(connection, 2)
            self.assertEqual(connection.recv(1), b"")

    def test_pbuffer_clear_and_readback(self):
        with self.connect() as connection:
            self.context(connection)
            self.ok(connection, 17, payload=struct.pack(">4f", 1, 0, 0, 1))
            self.ok(connection, 18, 0x4000)  # GL_COLOR_BUFFER_BIT
            _, pixels = self.ok(connection, 19, payload=words(0, 0, 2, 2, 0x1908, 0x1401))
            self.assertEqual(pixels, b"\xff\0\0\xff" * 4)
            self.ok(connection, 2)

    def test_gl_requires_current_context_and_reports_invalid_enum(self):
        with self.connect() as connection:
            reply, _ = self.request(connection, 18, 0x4000)
            self.assertEqual(reply[2:4], (1, 0x0502))  # GL_INVALID_OPERATION
            self.context(connection)
            reply, _ = self.request(connection, 23, 0xffffffff)  # GL_ENABLE
            self.assertEqual(reply[2:4], (1, 0x0500))  # GL_INVALID_ENUM
            self.ok(connection, 18, 0x4000)  # The previous error must not leak.
            self.ok(connection, 2)

    def test_bad_attributes_and_unknown_opcode(self):
        with self.connect() as connection:
            for opcode, payload in ((5, words(0x3033)), (0xffff, b"")):
                with self.subTest(opcode=opcode):
                    reply, _ = self.request(connection, opcode, payload=payload)
                    self.assertEqual(reply[2:4], (1, 0x300c))  # EGL_BAD_PARAMETER
            self.ok(connection, 1)
            self.ok(connection, 2)

    def test_invalid_headers_are_rejected_and_connection_closed(self):
        for options in ({"magic": b"BAD!"}, {"version": 4}, {"sequence": 0},
                        {"length": 16 * 1024 * 1024 + 1}):
            with self.subTest(options=options), self.connect() as connection:
                reply, payload = self.request(connection, 1, **options)
                self.assertEqual(reply[2:4], (1, 0x300c))
                self.assertEqual(payload, b"")
                self.assertEqual(connection.recv(1), b"")
        with self.connect() as connection:
            self.ok(connection, 1)
            self.ok(connection, 2)

    def test_truncated_frame_does_not_stop_listener(self):
        for frame in (b"I6ER", HEADER.pack(b"I6ER", 3, 5, 1, 0, 1, 0, 1, 4) + b"\0"):
            with self.subTest(frame=frame), self.connect() as connection:
                connection.sendall(frame)
                connection.shutdown(socket.SHUT_WR)
                self.assertEqual(connection.recv(1), b"")
        with self.connect() as connection:
            self.ok(connection, 1)
            self.ok(connection, 2)


class EGLShaderMapTests(EGLServerFixture):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="idrive-shader-test-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.marker = b"synthetic host-test shader identifier, not native firmware"
        self.native = root / "synthetic.shader"
        self.source = root / "synthetic.vert"
        self.native.write_bytes(self.marker)
        self.source.write_text("#version 300 es\nvoid main() { gl_Position = vec4(0.0, 0.0, 0.0, 1.0); }\n")
        super().setUp()

    def start_server(self, arguments, extra_environment=None):
        super().start_server([*arguments, "--shader-map", str(self.native), str(self.source)],
                             extra_environment)

    def test_essl300_map_promotes_es2_request_and_compiles_synthetic_shader(self):
        with self.connect() as connection:
            self.context(connection)  # Requests ES2, as the baseline QNX provider does.
            _, version = self.ok(connection, 14, 0x1f02)  # GL_VERSION
            self.assertTrue(version.startswith(b"OpenGL ES 3."), version)
            values, _ = self.ok(connection, 30, 0x8b31)  # GL_VERTEX_SHADER
            shader = values[0]
            self.ok(connection, 33, 1, 0, len(self.marker), payload=words(shader) + self.marker)
            values, _ = self.ok(connection, 40, shader, 0x8b81)  # GL_COMPILE_STATUS
            self.assertEqual(values[0], 1)
            self.ok(connection, 2)
        self.log.seek(0)
        self.assertIn(b"ANGLE_CONTEXT_GLES3_PROMOTION reason=essl300-shader-map", self.log.read())


@unittest.skipUnless(platform.system() == "Linux" and platform.machine() == "x86_64",
                     "mailbox fixture uses x86_64 aligned native-word memory ordering")
class EGLMailboxTests(EGLServerFixture):
    """Exercise the release transport with real shared mappings, not TCP."""
    MAILBOX_BYTES = 17 * 1024 * 1024
    FRAME_OFFSET = 64
    IDLE, REQUEST, RESPONSE, FAILED = 0, 2, 4, 5

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="idrive-mailbox-test-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        mailbox = root / "mailbox"
        self.ram = root / "guest-ram"
        with mailbox.open("w+b") as stream:
            stream.truncate(self.MAILBOX_BYTES)
            self.mapping = mmap.mmap(stream.fileno(), self.MAILBOX_BYTES)
        self.addCleanup(self.mapping.close)
        self.ram.write_bytes(b"\xa5" * 4096)
        self.start_server(["--mailbox-file", str(mailbox), "--guest-ram-file", str(self.ram)], {
            "IDRIVE_ANGLE_EGL_BATCH": "1",
            "IDRIVE_ANGLE_EGL_IMMEDIATE_POST": "1",
            "IDRIVE_ANGLE_EGL_FAST_WAIT": "1",
        })
        self.wait_word(0, 0x424d3649)  # READY magic, published after features.
        self.assertEqual(self.word(4), self.IDLE)
        self.assertEqual(self.word(8), 1 | 2 | 8 | 16)

    def word(self, offset, value=None):
        # Native aligned 32-bit loads/stores are atomic on x86_64. Its store
        # ordering publishes frame bytes before REQUEST, and reading RESPONSE
        # observes the server's preceding frame writes. Do not use this fixture
        # as an implementation of the guest's atomics on other architectures.
        word = ctypes.c_uint32.from_buffer(self.mapping, offset)
        if value is not None:
            word.value = value
        return word.value

    def wait_word(self, offset, expected):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.word(offset) == expected:
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.001)
        self.log.seek(0)
        self.fail(f"mailbox word {offset} did not reach {expected}:\n" +
                  self.log.read().decode(errors="replace"))

    def publish(self, frame):
        self.assertEqual(self.word(4), self.IDLE)
        self.assertLessEqual(len(frame), self.MAILBOX_BYTES - self.FRAME_OFFSET)
        self.mapping[self.FRAME_OFFSET:self.FRAME_OFFSET + len(frame)] = frame
        self.word(4, self.REQUEST)

    def request(self, connection, opcode, a=0, b=0, c=0, payload=b""):
        self.sequence += 1
        self.publish(HEADER.pack(b"I6ER", 3, opcode, 1, a, b, c, self.sequence, len(payload)) + payload)
        self.wait_word(4, self.RESPONSE)
        reply = HEADER.unpack_from(self.mapping, self.FRAME_OFFSET)
        self.assertEqual(reply[:2], (b"I6EA", 3))
        self.assertEqual(reply[-1], self.sequence)
        self.assertLessEqual(reply[-2], 16 * 1024 * 1024)
        start = self.FRAME_OFFSET + HEADER.size
        data = self.mapping[start:start + reply[-2]]
        self.word(4, self.IDLE)
        return reply, data

    def test_window_swap_writes_shared_ram_and_rejects_out_of_bounds(self):
        surface = self.context(None, window=True)
        self.ok(None, 17, payload=struct.pack(">4f", 1, 0, 0, 1))
        self.ok(None, 18, 0x4000)
        _, pixels = self.ok(None, 19, payload=words(0, 0, 2, 2, 0x1908, 0x1401))
        self.assertEqual(pixels, b"\xff\0\0\xff" * 4)
        # QNX Screen presents to shared physical RAM, not an RPC pixel payload.
        # Check BGRA conversion, row padding, offset and guard bytes on disk.
        values, data = self.ok(None, 21, surface, 2, 2, payload=words(12, 8, 0x80000040, 0))
        self.assertEqual(values[2], 24)
        self.assertEqual(data, b"")
        frame = (b"\0\0\xff\xff" * 2 + b"\0" * 4) * 2
        expected = b"\xa5" * 64 + frame + b"\xa5" * (4096 - 64 - len(frame))
        self.assertEqual(self.ram.read_bytes(), expected)
        for stride, address in ((7, 0x80000040), (12, 0x80000ff0), (12, 0x7fffffff)):
            with self.subTest(stride=stride, address=address):
                reply, _ = self.request(None, 21, surface, 2, 2, payload=words(stride, 8, address, 0))
                self.assertEqual(reply[2:4], (1, 0x300c))  # EGL_BAD_PARAMETER
                self.assertEqual(self.ram.read_bytes(), expected)
        self.ok(None, 2)

    def test_batch_clear_readback_and_error_recovery(self):
        reply, _ = self.request(None, 18, 0x4000)
        self.assertEqual(reply[2:4], (1, 0x0502))  # No current context.
        self.context(None)
        self.ok(None, 17, payload=struct.pack(">4f", 0, 1, 0, 1))
        viewport = HEADER.pack(b"I6ER", 3, 16, 1, 0, 0, 0, 101, 16) + words(0, 0, 2, 2)
        clear = HEADER.pack(b"I6ER", 3, 18, 1, 0x4000, 0, 0, 102, 0)
        values, _ = self.ok(None, 68, payload=viewport + clear)
        self.assertEqual(values[0], 0)
        _, pixels = self.ok(None, 19, payload=words(0, 0, 2, 2, 0x1908, 0x1401))
        self.assertEqual(pixels, b"\0\xff\0\xff" * 4)
        invalid_enum = HEADER.pack(b"I6ER", 3, 23, 1, 0xffffffff, 0, 0, 103, 0)
        values, _ = self.ok(None, 68, payload=invalid_enum)
        self.assertEqual(values[0], 0x0500)  # Batch reports the first GL error.
        wrong_client = HEADER.pack(b"I6ER", 3, 18, 2, 0x4000, 0, 0, 104, 0)
        reply, _ = self.request(None, 68, payload=wrong_client)
        self.assertEqual(reply[2:4], (1, 0x300c))
        values, _ = self.ok(None, 68, payload=clear)
        self.assertEqual(values[0], 0)  # Neither error poisoned the mailbox.
        self.ok(None, 2)

    def test_version_four_header_fails_mailbox_without_touching_ram(self):
        self.publish(HEADER.pack(b"I6ER", 4, 1, 1, 0, 0, 0, 1, 0))
        self.wait_word(4, self.FAILED)
        self.assertEqual(self.process.wait(timeout=5), 1)
        self.assertEqual(self.ram.read_bytes(), b"\xa5" * 4096)
        self.log.seek(0)
        self.assertIn(b"ANGLE_EGL_MAILBOX_FAIL stage=request-header", self.log.read())


if __name__ == "__main__":
    unittest.main()
