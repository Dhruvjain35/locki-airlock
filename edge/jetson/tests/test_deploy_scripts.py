"""Deploy-script tests: bash syntax, board detection against fixture roots, and installer / service / deploy /
inventory runs. No hardware, no network, no real sudo, nothing installed.

    cd edge/jetson && python3 -m unittest tests.test_deploy_scripts -v

Every script runs with EDGE_ROOT=<fixture root>, a fake uname and a fake python3 (EDGE_UNAME / EDGE_PYTHON3), and a
PATH whose sudo, apt-get, dpkg, usermod, git, cmake, make, systemctl, rsync, ssh, ... are shims that only append
their argv to a log. Scripts that write files run from a throwaway copy of the repo layout.
stdlib only and Python 3.6 compatible: `unittest discover` runs this file on the Jetson too.
"""
import os
import pwd
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
JETSON_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(JETSON_DIR))
SCRIPTS = os.path.join(REPO, "scripts")
FIXTURES = os.path.join(HERE, "fixtures", "jetson_roots")
NEW_SCRIPTS = ("edge_jetson_inventory.sh", "edge_jetson_detect.sh", "edge_jetson_install.sh",
               "edge_deploy.sh", "edge_service.sh")
BASH = shutil.which("bash") or "/bin/bash"
KEYS = ("ARCH", "BOARD_MODEL", "L4T_MAJOR", "L4T_MINOR", "JETPACK_FAMILY", "UBUNTU", "PYTHON3", "PLATFORM")
FORBIDDEN_IN_LOG = re.compile(r"upgrade|dist-upgrade|full-upgrade|do-release-upgrade|nvidia-jetpack|python3\.1\d")
# Commands that could change a machine. All of them are shims here; any call shows up in the log.
LOGGED = ("sudo", "apt-get", "apt", "dpkg", "usermod", "git", "cmake", "make", "systemctl", "rsync", "ssh", "scp",
          "pip", "pip3", "python3", "nvpmodel", "ldconfig")

LOG_SHIM = r"""#!/bin/sh
# logging shim: records argv, changes nothing
{ printf '%s' "${0##*/}"; for a in "$@"; do printf ' %s' "$a"; done; printf '\n'; } >> "$EDGE_TEST_LOG"
if [ -n "${EDGE_TEST_ARGV:-}" ]; then   # exact argv, one per line: proves word boundaries
  { printf '%s\n' "${0##*/}"; for a in "$@"; do printf '%s\n' "$a"; done; printf '@@END@@\n'; } >> "$EDGE_TEST_ARGV"
fi
if [ "${0##*/}" = sudo ] && [ "$*" = "make install" ] && [ -n "${EDGE_TEST_STATE:-}" ]; then
  : > "$EDGE_TEST_STATE/jetson_inference_installed"
fi
exit 0
"""

DPKG_QUERY_SHIM = r"""#!/bin/sh
# read-only query shim: a package counts as installed only if it is listed in FAKE_DPKG_INSTALLED
pkg=""
for a in "$@"; do case "$a" in -*) ;; *) pkg="$a" ;; esac; done
case " ${FAKE_DPKG_INSTALLED:-} " in *" $pkg "*) printf 'install ok installed|1.0-fake'; exit 0 ;; esac
echo "dpkg-query: no packages found matching $pkg" >&2
exit 1
"""

APT_CACHE_SHIM = r"""#!/bin/sh
pkg="${2:-}"
echo "$pkg:"
echo "  Installed: (none)"
case "$pkg" in
  libopencv-python) echo "  Candidate: ${FAKE_LIBOPENCV_PYTHON_CANDIDATE:-(none)}" ;;
  *) echo "  Candidate: ${FAKE_JETPACK_CANDIDATE:-(none)}" ;;
esac
"""

FAKE_UNAME = r"""#!/bin/sh
case "${1:-}" in
  -m) echo "${FAKE_UNAME_M:-aarch64}" ;;
  -r) echo "${FAKE_UNAME_R:-4.9.337-tegra}" ;;
  *) echo "${FAKE_UNAME_S:-Linux}" ;;
esac
"""

FAKE_PYTHON = r"""#!/bin/sh
# fake interpreter: answers the probes the scripts make; argv goes to EDGE_TEST_PYLOG
printf '%s\n' "$*" >> "${EDGE_TEST_PYLOG:-/dev/null}"
case "$*" in
  *"-m unittest"*) echo "Ran 1 test in 0.001s"; echo; echo "OK (fake python3)"; exit 0 ;;
  *"-m venv"*)
    for last in "$@"; do :; done
    mkdir -p "$last/bin" && cp "$0" "$last/bin/python"
    case "$*" in
      *--system-site-packages*) echo "include-system-site-packages = true" > "$last/pyvenv.cfg" ;;
      *) echo "include-system-site-packages = false" > "$last/pyvenv.cfg" ;;
    esac
    exit 0 ;;
  *jetson*inference*)
    if [ "${FAKE_JI:-0}" = 1 ] || [ -e "${EDGE_TEST_STATE:-/nonexistent}/jetson_inference_installed" ]; then
      echo importable; exit 0
    fi
    echo "ModuleNotFoundError: No module named 'jetson_inference'" >&2; exit 1 ;;
  *cv2*)
    if [ "${FAKE_CV2:-1}" = 1 ]; then echo "${FAKE_CV2_VERSION:-4.1.1}"; exit 0; fi
    echo "ModuleNotFoundError: No module named 'cv2'" >&2; exit 1 ;;
  *numpy*) echo 1.13.3; exit 0 ;;
  *version_info*) echo "${FAKE_PY_VERSION:-3.6}"; exit 0 ;;
  *--version*) echo "Python ${FAKE_PY_VERSION:-3.6}.9"; exit 0 ;;
esac
exit 0
"""


SHIMS = {}   # created once per test run (setUpModule): exec'ing freshly written files is slow on macOS


def _write(path, text, mode=0o755):
    with open(path, "w") as f:
        f.write(text)
    os.chmod(path, mode)
    return path


def setUpModule():
    root = tempfile.mkdtemp(prefix="locki-deploy-shims-")
    SHIMS["root"] = root
    SHIMS["bin"] = os.path.join(root, "bin")
    SHIMS["fakes"] = os.path.join(root, "fakes")
    os.makedirs(SHIMS["bin"])
    os.makedirs(SHIMS["fakes"])
    for name in LOGGED:
        _write(os.path.join(SHIMS["bin"], name), LOG_SHIM)
    _write(os.path.join(SHIMS["bin"], "dpkg-query"), DPKG_QUERY_SHIM)
    _write(os.path.join(SHIMS["bin"], "apt-cache"), APT_CACHE_SHIM)
    SHIMS["uname"] = _write(os.path.join(SHIMS["fakes"], "uname"), FAKE_UNAME)
    SHIMS["python3"] = _write(os.path.join(SHIMS["fakes"], "python3"), FAKE_PYTHON)


def tearDownModule():
    if SHIMS.get("root"):
        shutil.rmtree(SHIMS["root"], True)


def table_fields(markdown):
    """First column of every summary-table row (header and separator excluded)."""
    fields = []
    for line in markdown.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split(" | ")]
        if line.startswith("| ") and len(cells) == 3 and cells[0] not in ("Field", "---"):
            fields.append(cells[0])
    return fields


class ShimCase(unittest.TestCase):
    def setUp(self):
        if not SHIMS:                                    # a runner that skipped setUpModule
            setUpModule()
            self.addCleanup(tearDownModule)
        self.tmp = tempfile.mkdtemp(prefix="locki-deploy-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = SHIMS["bin"]
        self.fake_uname = SHIMS["uname"]
        self.fake_python = SHIMS["python3"]
        self.home = self._mkdir("home")
        self.state = self._mkdir("state")
        self.log = os.path.join(self.tmp, "calls.log")
        self.pylog = os.path.join(self.tmp, "python.log")
        self.argv = os.path.join(self.tmp, "argv.log")
        for path in (self.log, self.pylog, self.argv):
            open(path, "w").close()

    def _mkdir(self, name):
        path = os.path.join(self.tmp, name)
        os.makedirs(path)
        return path

    _write = staticmethod(_write)

    def env(self, root="nano_r32_7_4", uname_s="Linux", uname_m="aarch64", py="3.6", **extra):
        e = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.home,
            "LC_ALL": "C",
            "LANG": "C",
            "EDGE_ROOT": root if os.path.isabs(root) else os.path.join(FIXTURES, root),
            "EDGE_UNAME": self.fake_uname,
            "EDGE_PYTHON3": self.fake_python,
            "EDGE_TEST_LOG": self.log,
            "EDGE_TEST_PYLOG": self.pylog,
            "EDGE_TEST_ARGV": self.argv,
            "EDGE_TEST_STATE": self.state,
            "FAKE_UNAME_S": uname_s,
            "FAKE_UNAME_M": uname_m,
            "FAKE_PY_VERSION": py,
        }
        if "TMPDIR" in os.environ:
            e["TMPDIR"] = os.environ["TMPDIR"]
        e.update(dict((k, str(v)) for k, v in extra.items()))
        return e

    def run_script(self, script, args=(), scripts_dir=SCRIPTS, **env_kw):
        p = subprocess.run([BASH, os.path.join(scripts_dir, script)] + list(args), env=self.env(**env_kw),
                           cwd=self.tmp, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, timeout=300)
        return p.returncode, p.stdout

    def calls(self):
        with open(self.log) as f:
            return f.read()

    def argvs(self, tool):
        """Exact argv lists (program name dropped) of every logged call to `tool`."""
        with open(self.argv) as f:
            records = [r.split("\n") for r in f.read().split("@@END@@\n") if r]
        return [r[1:-1] for r in records if r[0] == tool]

    def pycalls(self):
        with open(self.pylog) as f:
            return f.read()

    def repo_copy(self):
        """Minimal repo layout with copies of the scripts, so nothing in the real checkout is ever written."""
        root = tempfile.mkdtemp(prefix="repo-", dir=self.tmp)
        os.makedirs(os.path.join(root, "scripts"))
        for name in NEW_SCRIPTS:
            shutil.copy(os.path.join(SCRIPTS, name), os.path.join(root, "scripts", name))
        os.makedirs(os.path.join(root, "edge", "jetson", "tests"))
        os.makedirs(os.path.join(root, "edge", "jetson", "locki_edge"))
        open(os.path.join(root, "edge", "jetson", "locki_edge", "runtime.py"), "w").close()
        return root

    def l4t_root(self, first_line, ubuntu="18.04"):
        """Ad-hoc fixture root with a custom nv_tegra_release first line."""
        root = tempfile.mkdtemp(prefix="root-", dir=self.tmp)
        os.makedirs(os.path.join(root, "etc"))
        with open(os.path.join(root, "etc", "nv_tegra_release"), "w") as f:
            f.write(first_line + "\n")
        with open(os.path.join(root, "etc", "os-release"), "w") as f:
            f.write('ID=ubuntu\nVERSION_ID="%s"\nPRETTY_NAME="Ubuntu %s"\n' % (ubuntu, ubuntu))
        return root


class TestScriptSyntax(unittest.TestCase):
    def test_bash_n_every_new_script(self):
        for name in NEW_SCRIPTS:
            path = os.path.join(SCRIPTS, name)
            self.assertTrue(os.path.isfile(path), path)
            p = subprocess.run([BASH, "-n", path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               universal_newlines=True)
            self.assertEqual(p.returncode, 0, "%s: %s" % (name, p.stdout))
            with open(path) as f:
                self.assertEqual(f.readline().strip(), "#!/usr/bin/env bash", name)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_shellcheck(self):
        p = subprocess.run(["shellcheck", "-x", "-S", "warning"] + list(NEW_SCRIPTS), cwd=SCRIPTS,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_no_upgrade_flash_pip_gpio_or_serial_in_code(self):
        bad = re.compile(r"do-release-upgrade|dist-upgrade|full-upgrade|\bapt(-get)?\s+upgrade|pip3?\s+install|"
                         r"flash\.sh|initrd_flash|\bdd\s+if=|\bmkfs|/sys/class/gpio|Jetson\.GPIO|"
                         r"/dev/tty(USB|ACM|THS)|nvpmodel\s+-m")
        for name in NEW_SCRIPTS:
            with open(os.path.join(SCRIPTS, name)) as f:
                for n, line in enumerate(f, 1):
                    code = line.strip()
                    if code.startswith("#"):
                        continue
                    self.assertIsNone(bad.search(code), "%s:%d: %s" % (name, n, code))


class TestDetect(ShimCase):
    def detect(self, **kw):
        rc, out = self.run_script("edge_jetson_detect.sh", **kw)
        self.assertEqual(rc, 0, out)
        facts = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        self.assertEqual(sorted(facts), sorted(KEYS), out)
        self.assertEqual(self.calls(), "", "detection must not run any logged command")
        return facts

    def test_original_nano_r32_is_jetpack4(self):
        self.assertEqual(self.detect(root="nano_r32_7_4", py="3.6"), {
            "ARCH": "aarch64", "BOARD_MODEL": "NVIDIA Jetson Nano Developer Kit", "L4T_MAJOR": "32",
            "L4T_MINOR": "7.4", "JETPACK_FAMILY": "4", "UBUNTU": "18.04", "PYTHON3": "3.6", "PLATFORM": "jetson"})

    def test_orin_nano_r36_is_jetpack6(self):
        self.assertEqual(self.detect(root="orin_nano_r36_4_0", py="3.10"), {
            "ARCH": "aarch64", "BOARD_MODEL": "NVIDIA Jetson Orin Nano Developer Kit", "L4T_MAJOR": "36",
            "L4T_MINOR": "4.0", "JETPACK_FAMILY": "6", "UBUNTU": "22.04", "PYTHON3": "3.10", "PLATFORM": "jetson"})

    def test_r35_is_jetpack5(self):
        f = self.detect(root="jetpack5_r35_3_1", py="3.8")
        self.assertEqual((f["L4T_MAJOR"], f["L4T_MINOR"], f["JETPACK_FAMILY"], f["UBUNTU"], f["PYTHON3"],
                          f["PLATFORM"]), ("35", "3.1", "5", "20.04", "3.8", "jetson"))

    def test_no_nv_tegra_release_is_not_jetson(self):
        f = self.detect(root="not_jetson", py="3.10")
        self.assertEqual((f["L4T_MAJOR"], f["L4T_MINOR"], f["JETPACK_FAMILY"], f["PLATFORM"], f["UBUNTU"],
                          f["BOARD_MODEL"]),
                         ("unknown", "unknown", "unknown", "not_jetson", "22.04", "Raspberry Pi 4 Model B Rev 1.4"))

    def test_darwin_is_macos(self):
        f = self.detect(root="nano_r32_7_4", uname_s="Darwin", uname_m="arm64")
        self.assertEqual((f["PLATFORM"], f["ARCH"]), ("macos", "arm64"))

    def test_unmapped_l4t_major_is_unknown_family(self):
        root = self.l4t_root("# R34 (release), REVISION: 1.1, GCID: 1, BOARD: t186ref, EABI: aarch64, DATE: x")
        f = self.detect(root=root)
        self.assertEqual((f["L4T_MAJOR"], f["L4T_MINOR"], f["JETPACK_FAMILY"], f["PLATFORM"]),
                         ("34", "1.1", "unknown", "jetson"))

    def test_missing_interpreter_and_missing_root_report_unknown(self):
        f = self.detect(root=os.path.join(self.tmp, "does-not-exist"), EDGE_PYTHON3="/nonexistent/python3")
        self.assertEqual((f["PYTHON3"], f["BOARD_MODEL"], f["UBUNTU"], f["PLATFORM"]),
                         ("unknown", "unknown", "unknown", "not_jetson"))

    def test_library_sources_cleanly_under_strict_mode(self):
        lib = os.path.join(SCRIPTS, "edge_jetson_detect.sh")
        code = ('set -euo pipefail; before="$-"; . "%s"; edge_detect; '
                'echo "family=$JETPACK_FAMILY minor=$L4T_MINOR platform=$PLATFORM"; '
                '[ "$before" = "$-" ] && echo options-unchanged' % lib)
        for root, want in (("nano_r32_7_4", "family=4 minor=7.4 platform=jetson"),
                           (os.path.join(self.tmp, "nowhere"), "family=unknown minor=unknown platform=not_jetson")):
            p = subprocess.run([BASH, "-c", code], env=self.env(root=root), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout)
            self.assertEqual(p.stdout.split(), want.split() + ["options-unchanged"], p.stdout)


class TestInstaller(ShimCase):
    def install(self, *args, **kw):
        repo = self.repo_copy()
        rc, out = self.run_script("edge_jetson_install.sh", args, scripts_dir=os.path.join(repo, "scripts"), **kw)
        return rc, out, repo

    def assert_refused(self, rc, out):
        self.assertEqual(rc, 2, out)
        self.assertIn("REFUSED", out)
        self.assertEqual(self.calls(), "", "a refused run must not invoke anything:\n" + out)

    def test_dry_run_invokes_nothing_on_every_family(self):
        for root, py in (("nano_r32_7_4", "3.6"), ("jetpack5_r35_3_1", "3.8"), ("orin_nano_r36_4_0", "3.10")):
            rc, out, _ = self.install(root=root, py=py)
            self.assertEqual(rc, 0, out)
            self.assertIn("DRY RUN: nothing changed", out)
            self.assertIn("would run: sudo apt-get install -y --no-install-recommends python3-numpy", out)
            self.assertEqual(self.calls(), "", "%s dry run invoked a logged command:\n%s" % (root, out))
            self.assertNotIn("-m unittest", self.pycalls())

    def test_dry_run_with_every_opt_in_still_invokes_nothing(self):
        rc, out, _ = self.install("--with-detectnet", "--with-jetpack-meta", "--venv",
                                  root="nano_r32_7_4", FAKE_JETPACK_CANDIDATE="4.6.4-b39")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls(), "", out)
        for want in ("git clone --recursive --depth=1 https://github.com/dusty-nv/jetson-inference", "cmake ../",
                     "make -j", "sudo make install", "sudo ldconfig", "30-60+ minutes",
                     "would run: sudo apt-get install -y --no-install-recommends nvidia-jetpack",
                     "skipped on JetPack 4.x"):
            self.assertIn(want, out)

    def test_yes_on_jetpack4_installs_only_the_matching_set(self):
        rc, out, repo = self.install("--yes", root="nano_r32_7_4", py="3.6")
        self.assertEqual(rc, 0, out)
        log = self.calls()
        self.assertIsNone(FORBIDDEN_IN_LOG.search(log), log)
        self.assertIn("sudo apt-get update", log)
        self.assertIn("sudo apt-get install -y --no-install-recommends python3-numpy v4l-utils alsa-utils rsync", log)
        self.assertNotIn("python3-opencv", log)          # the JetPack cv2 is importable: left alone
        for never in ("git ", "cmake", "make ", "venv", "pip"):
            self.assertNotIn(never, log)
        groups = subprocess.run(["id", "-nG"], stdout=subprocess.PIPE, universal_newlines=True).stdout.split()
        if "video" in groups and "audio" in groups:
            self.assertNotIn("usermod", log)
        else:
            self.assertIn("sudo usermod -aG", log)
        self.assertIn("-m unittest discover -s edge/jetson/tests", self.pycalls())   # from the repo root
        self.assertTrue(os.path.isfile(os.path.join(repo, "hardware", "jetson_inventory.md")), out)
        self.assertIn("DONE", out)

    def test_yes_is_idempotent_when_everything_is_present(self):
        rc, out, _ = self.install("--yes", root="nano_r32_7_4",
                                  FAKE_DPKG_INSTALLED="python3-numpy v4l-utils alsa-utils rsync")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("apt-get", self.calls())
        self.assertIn("all installed: skip", out)

    def test_missing_cv2_installs_python3_opencv_then_rechecks(self):
        rc, out, _ = self.install("--yes", root="nano_r32_7_4", FAKE_CV2=0)
        self.assertIn("sudo apt-get install -y --no-install-recommends python3-opencv", self.calls())
        self.assertEqual(rc, 1, out)                     # the fake cv2 never appears: reported, exit 1
        self.assertIn("cv2 still not importable", out)

    def test_missing_cv2_prefers_nvidia_libopencv_python(self):
        rc, out, _ = self.install("--yes", root="nano_r32_7_4", FAKE_CV2=0,
                                  FAKE_LIBOPENCV_PYTHON_CANDIDATE="4.1.1-2-gd5a58aa75")
        calls = self.calls()
        self.assertIn("sudo apt-get install -y --no-install-recommends libopencv-python", calls)
        self.assertNotIn("python3-opencv", calls)

    def test_refuses_not_jetson(self):
        self.assert_refused(*self.install("--yes", root="not_jetson")[:2])

    def test_refuses_macos(self):
        self.assert_refused(*self.install("--yes", root="nano_r32_7_4", uname_s="Darwin", uname_m="arm64")[:2])

    def test_refuses_non_aarch64(self):
        self.assert_refused(*self.install("--yes", root="nano_r32_7_4", uname_m="x86_64")[:2])

    def test_refuses_unknown_l4t_family(self):
        root = self.l4t_root("# R34 (release), REVISION: 1.1, GCID: 1, BOARD: t186ref, EABI: aarch64, DATE: x")
        self.assert_refused(*self.install("--yes", root=root)[:2])

    def test_jetpack_meta_refused_when_apt_offers_another_family(self):
        rc, out, _ = self.install("--yes", "--with-jetpack-meta", root="nano_r32_7_4",
                                  FAKE_JETPACK_CANDIDATE="6.2+b77")
        self.assert_refused(rc, out)
        self.assertIn("mismatched JetPack", out)

    def test_jetpack_meta_refused_without_apt_candidate(self):
        self.assert_refused(*self.install("--yes", "--with-jetpack-meta", root="nano_r32_7_4")[:2])

    def test_jetpack_meta_installs_only_on_explicit_flag_and_matching_candidate(self):
        rc, out, _ = self.install("--yes", "--with-jetpack-meta", root="nano_r32_7_4",
                                  FAKE_JETPACK_CANDIDATE="4.6.4-b39")
        self.assertEqual(rc, 0, out)
        self.assertIn("sudo apt-get install -y --no-install-recommends nvidia-jetpack", self.calls())

    def test_detectnet_runs_the_official_build_steps_in_order(self):
        rc, out, _ = self.install("--yes", "--with-detectnet", root="nano_r32_7_4")
        self.assertEqual(rc, 0, out)
        log = self.calls()
        ji = os.path.join(self.home, "jetson-inference")
        steps = ["sudo apt-get install -y --no-install-recommends git cmake libpython3-dev python3-numpy",
                 "git clone --recursive --depth=1 https://github.com/dusty-nv/jetson-inference " + ji,
                 "cmake ../", "make -j", "sudo make install", "sudo ldconfig"]
        positions = [log.find(s) for s in steps]
        self.assertNotIn(-1, positions, log)
        self.assertEqual(positions, sorted(positions), log)
        self.assertIn("30-60+ minutes", out)
        self.assertIsNone(re.search(r"upgrade|nvidia-jetpack|python3\.1\d", log), log)

    def test_orin_venv_uses_system_site_packages(self):
        rc, out, repo = self.install("--yes", "--venv", root="orin_nano_r36_4_0", py="3.10")
        self.assertEqual(rc, 0, out)
        self.assertIn("-m venv --system-site-packages " + os.path.join(repo, "edge", "jetson", ".venv"),
                      self.pycalls())
        self.assertIsNone(re.search(r"upgrade|nvidia-jetpack", self.calls()), self.calls())

    def test_existing_venv_without_system_site_packages_is_refused(self):
        repo = self.repo_copy()
        venv = os.path.join(repo, "edge", "jetson", ".venv")
        os.makedirs(os.path.join(venv, "bin"))
        self._write(os.path.join(venv, "bin", "python"), FAKE_PYTHON)
        with open(os.path.join(venv, "pyvenv.cfg"), "w") as f:
            f.write("include-system-site-packages = false\n")
        rc, out = self.run_script("edge_jetson_install.sh", ("--yes", "--venv"),
                                  scripts_dir=os.path.join(repo, "scripts"), root="orin_nano_r36_4_0", py="3.10")
        self.assert_refused(rc, out)

    def test_unknown_option_is_rejected(self):
        rc, out, _ = self.install("--force")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.calls(), "")


class TestInventory(ShimCase):
    def inventory(self, **kw):
        repo = self.repo_copy()
        rc, out = self.run_script("edge_jetson_inventory.sh", scripts_dir=os.path.join(repo, "scripts"), **kw)
        return rc, out, os.path.join(repo, "hardware", "jetson_inventory.md")

    def test_writes_summary_table_and_raw_blocks(self):
        rc, out, path = self.inventory(root="nano_r32_7_4")
        self.assertEqual(rc, 0, out)
        with open(path) as f:
            md = f.read()
        for want in ("| Board model | NVIDIA Jetson Nano Developer Kit | VERIFIED |",
                     "| L4T version | R32.7.4 | DERIVED |",
                     "| JetPack family | 4.x (L4T R32 maps to JetPack 4) | DERIVED |",
                     "| OS | Ubuntu 18.04.6 LTS | VERIFIED |",
                     "| Architecture | aarch64 | VERIFIED |",
                     "| Python 3 | 3.6.9", "| OpenCV (cv2) | 4.1.1 | VERIFIED |",
                     "| jetson_inference | not importable | VERIFIED |",
                     "## Raw command output", "```text", "### `cat /etc/nv_tegra_release`"):
            self.assertIn(want, md)
        self.assertIn("| Field | Value | Status |", out)
        for line in self.calls().splitlines():          # read-only: at most the no-prompt power-mode query
            self.assertEqual(line, "sudo -n nvpmodel -q")

    def test_refuses_on_macos_and_writes_nothing(self):
        rc, out, path = self.inventory(root="nano_r32_7_4", uname_s="Darwin", uname_m="arm64")
        self.assertEqual(rc, 2, out)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.calls(), "")

    def test_committed_placeholder_has_the_generated_layout(self):
        placeholder = os.path.join(REPO, "hardware", "jetson_inventory.md")
        if not os.path.isfile(placeholder):
            self.skipTest("hardware/jetson_inventory.md not present")
        rc, out, path = self.inventory(root="nano_r32_7_4")
        self.assertEqual(rc, 0, out)
        with open(placeholder) as f:
            committed = table_fields(f.read())
        with open(path) as f:
            generated = table_fields(f.read())
        self.assertEqual(len(generated), 26, generated)
        self.assertEqual(committed, generated)


class TestService(ShimCase):
    def setUp(self):
        super().setUp()
        self.repo = self.repo_copy()
        self.conf = os.path.join(self.home, ".config", "locki-edge")
        self.env_file = os.path.join(self.conf, "airlock.env")

    def service(self, *args, **kw):
        kw.setdefault("root", "nano_r32_7_4")
        return self.run_script("edge_service.sh", args, scripts_dir=os.path.join(self.repo, "scripts"), **kw)

    def write_env(self, mode):
        if not os.path.isdir(self.conf):
            os.makedirs(self.conf)
        self._write(self.env_file, "AIRLOCK_EDGE_RUNTIME=jetson\n", mode)

    def test_refuses_without_env_file(self):
        rc, out = self.service("install", "--yes")
        self.assertEqual(rc, 2, out)
        self.assertIn("missing", out)
        self.assertEqual(self.calls(), "")

    def test_refuses_group_or_world_readable_env_file(self):
        for mode in (0o644, 0o640, 0o604):
            self.write_env(mode)
            rc, out = self.service("install", "--yes")
            self.assertEqual(rc, 2, "%o: %s" % (mode, out))
            self.assertIn("chmod 600", out)
        self.assertEqual(self.calls(), "")

    def test_refuses_loose_token_file(self):
        self.write_env(0o600)
        self._write(os.path.join(self.conf, "device_token"), "x\n", 0o644)
        rc, out = self.service("install", "--yes")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.calls(), "")

    def test_dry_run_prints_the_required_unit_and_invokes_nothing(self):
        self.write_env(0o600)
        rc, out = self.service("install")
        self.assertEqual(rc, 0, out)
        user = pwd.getpwuid(os.getuid()).pw_name
        for line in ("User=%s" % user, "SupplementaryGroups=video audio",
                     "WorkingDirectory=%s" % os.path.join(self.repo, "edge", "jetson"),
                     "ExecStart=/usr/bin/python3 -m locki_edge.runtime --env-file %s" % self.env_file,
                     "Restart=on-failure", "RestartSec=3", "KillSignal=SIGTERM", "TimeoutStopSec=15",
                     "NoNewPrivileges=yes", "ProtectSystem=full", "WantedBy=multi-user.target"):
            self.assertIn(line, out)
        self.assertIn("DRY RUN", out)
        self.assertEqual(self.calls(), "")

    def test_yes_writes_the_unit_but_never_starts_or_enables_it(self):
        self.write_env(0o600)
        rc, out = self.service("install", "--yes")
        self.assertEqual(rc, 0, out)
        log = self.calls()
        self.assertIn("sudo install -m 0644 ", log)
        self.assertIn(" /etc/systemd/system/locki-airlock-edge.service", log)
        self.assertIn("sudo systemctl daemon-reload", log)
        self.assertIsNone(re.search(r"systemctl\s+(start|enable|restart)\b", log), log)
        self.assertIn("sudo systemctl start locki-airlock-edge", out)   # printed for the operator, not run

    def test_refuses_on_macos(self):
        self.write_env(0o600)
        rc, out = self.service("install", "--yes", uname_s="Darwin", uname_m="arm64")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.calls(), "")

    def test_uninstall_without_yes_changes_nothing(self):
        rc, out = self.service("uninstall")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls(), "")


class TestDeploy(ShimCase):
    EXCLUDES = ("--exclude=.venv/", "--exclude=__pycache__/", "--exclude=*.pyc", "--exclude=*.env",
                "--exclude=*token*")

    def deploy(self, *args, **kw):
        # A throwaway repo layout with every folder the script can send, so the rsync list does not depend on
        # which folders this checkout has (the shared repo has no hardware/: the device writes its own).
        repo = self.repo_copy()
        files = [("docs", "airlock", "edge", "README.md"), ("scripts", "airlock_not_edge.ts")]
        if kw.get("hardware", True):
            files.append(("hardware", "jetson_inventory.md"))
        for parts in files:
            if not os.path.isdir(os.path.join(repo, *parts[:-1])):
                os.makedirs(os.path.join(repo, *parts[:-1]))
            open(os.path.join(repo, *parts), "w").close()
        return self.run_script("edge_deploy.sh", args, scripts_dir=os.path.join(repo, "scripts"))

    def lines(self, tool):
        return [l for l in self.calls().splitlines() if l.startswith(tool + " ")]

    def installer_calls(self):
        return [l for l in self.lines("ssh") if "edge_jetson_install.sh" in l]

    def test_default_copies_and_tests_but_never_installs(self):
        rc, out = self.deploy("areeb@jetson.local")
        self.assertEqual(rc, 0, out)
        rsyncs = self.lines("rsync")
        self.assertEqual(len(self.argvs("rsync")), 4, rsyncs)
        for argv in self.argvs("rsync"):               # each exclude must be its own argument
            for ex in self.EXCLUDES:
                self.assertIn(ex, argv)
        # one rsync per code dir with trailing slashes, so --delete can only prune inside that dir
        code = [l for l in rsyncs if "--delete" in l]
        self.assertEqual(sorted(l.split()[-2:] for l in code), sorted(
            ["%s/" % p, "areeb@jetson.local:locki-airlock-edge/%s/" % p]
            for p in ("edge/jetson", "scripts", "docs/airlock/edge")), code)
        for line in rsyncs:
            self.assertNotIn(" -azR", line)              # -R + --delete also prunes implied parent dirs
        # scripts/ is shared with the rest of the repo: only edge_*.sh go, and only that rsync filters by name
        for argv in self.argvs("rsync"):
            if argv[-2] == "scripts/":
                self.assertLess(argv.index("--include=edge_*.sh"), argv.index("--exclude=*"), argv)
            else:
                self.assertNotIn("--exclude=*", argv)
        hardware = [l for l in rsyncs if "--ignore-existing" in l]
        self.assertEqual(len(hardware), 1, rsyncs)
        self.assertTrue(hardware[0].endswith(" hardware/ areeb@jetson.local:locki-airlock-edge/hardware/"),
                        hardware[0])
        self.assertNotIn("--delete", hardware[0])
        ssh = self.lines("ssh")
        self.assertTrue(any("bash scripts/edge_jetson_detect.sh" in l for l in ssh), ssh)
        # from the repo root: edge/jetson/tests also holds Sid's adapter tests, which import edge.jetson.*
        self.assertTrue(any("cd ~/locki-airlock-edge && /usr/bin/python3 -m unittest discover -s edge/jetson/tests" in l
                            for l in ssh), ssh)
        self.assertEqual(self.installer_calls(), [])

    def test_without_a_hardware_folder_nothing_is_sent_there(self):
        rc, out = self.deploy("areeb@jetson.local", hardware=False)
        self.assertEqual(rc, 0, out)
        self.assertEqual([argv[-2] for argv in self.argvs("rsync")], ["edge/jetson/", "scripts/", "docs/airlock/edge/"])

    def test_install_without_yes_is_a_remote_dry_run(self):
        rc, out = self.deploy("areeb@jetson.local", "--install")
        self.assertEqual(rc, 0, out)
        calls = self.installer_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertTrue(calls[0].endswith("bash scripts/edge_jetson_install.sh"), calls[0])

    def test_install_yes_and_opt_ins_pass_through(self):
        rc, out = self.deploy("areeb@jetson.local", "--install", "--yes", "--with-detectnet")
        self.assertEqual(rc, 0, out)
        calls = self.installer_calls()
        self.assertEqual(len(calls), 1, calls)
        self.assertTrue(calls[0].endswith("bash scripts/edge_jetson_install.sh --yes --with-detectnet"), calls[0])

    def test_yes_without_install_is_rejected(self):
        rc, out = self.deploy("areeb@jetson.local", "--yes")
        self.assertEqual(rc, 2, out)
        self.assertEqual(self.calls(), "")

    def test_bad_or_missing_target_is_rejected(self):
        for args in ((), ("areeb@jetson;reboot",), ("a@b", "c@d")):
            rc, out = self.deploy(*args)
            self.assertEqual(rc, 2, "%r: %s" % (args, out))
        self.assertEqual(self.calls(), "")


if __name__ == "__main__":
    unittest.main()
