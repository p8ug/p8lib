"""Tests of p8lib with libraries made on the spot.

Run from the repository:  python3 -m unittest discover -s tests -v

Nothing here uses the network: git repositories are local directories, and
the store is a temporary directory (P8LIB_HOME).  The tests at the end run
whichever Prog8 compilers are on the PATH.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import p8lib  # noqa: E402

GIT = ["git", "-c", "user.name=p8lib test", "-c", "user.email=test@example.invalid",
       "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false"]


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p8lib-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"
        self.prog = self.tmp / "prog"
        self.prog.mkdir()

    # -- running p8lib

    def run_p8lib(self, *args, cwd=None):
        env = dict(os.environ, P8LIB_HOME=str(self.store))
        return subprocess.run([sys.executable, str(REPO / "p8lib.py"), *args],
                              cwd=cwd or self.prog, env=env, capture_output=True, text=True)

    def ok(self, *args, **kwargs):
        result = self.run_p8lib(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def fails(self, *args, expect, **kwargs):
        result = self.run_p8lib(*args, **kwargs)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "", "nothing goes to standard output on failure")
        self.assertIn(expect, result.stderr)
        return result

    def srcdirs(self, *args):
        line = self.ok("srcdirs", *args).stdout
        self.assertTrue(line.endswith("\n") and line.count("\n") == 1, repr(line))
        return [Path(p) for p in line.strip().split(os.pathsep) if p]

    # -- making programs and libraries

    def program(self, dependencies):
        (self.prog / "p8lib.toml").write_text("[dependencies]\n" + dependencies)

    def library(self, name, extra="", files=None, directory=None):
        """A library directory with p8lib.toml and src/<name>.p8."""
        root = self.tmp / "libs" / (directory or name)
        (root / "src").mkdir(parents=True)
        (root / "p8lib.toml").write_text(
            f'[package]\nname = "{name}"\ndescription = "the {name} library"\n{extra}')
        files = files or {}
        files.setdefault(f"src/{name}.p8", f"{name} {{\n    sub answer() -> ubyte {{ return 42 }}\n}}\n")
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        return root

    def commit(self, root, message="change", tag=None):
        if not (root / ".git").exists():
            subprocess.run([*GIT, "init", "-q", "-b", "main", str(root)], check=True)
        subprocess.run([*GIT, "-C", str(root), "add", "-A"], check=True)
        subprocess.run([*GIT, "-C", str(root), "commit", "-q", "-m", message], check=True)
        if tag:
            subprocess.run([*GIT, "-C", str(root), "tag", "-a", tag, "-m", tag], check=True)
        return subprocess.run([*GIT, "-C", str(root), "rev-parse", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()

    def path_dep(self, name, root):
        return f'{name} = {{ path = "{root.as_posix()}" }}\n'

    def git_dep(self, name, root, **ref):
        more = "".join(f', {k} = "{v}"' for k, v in ref.items())
        return f'{name} = {{ git = "{root.as_uri()}"{more} }}\n'

    def lock(self):
        return json.loads((self.prog / "p8lib.lock").read_text())["libraries"]


class TargetKeys(unittest.TestCase):
    def test_name_family_cpu_default(self):
        self.assertEqual(p8lib.target_keys("coco2ram"), ["coco2ram", "coco2", "coco", "6809", "default"])
        self.assertEqual(p8lib.target_keys("c64"), ["c64", "6502", "default"])
        self.assertEqual(p8lib.target_keys("cx16"), ["cx16", "65c02", "6502", "default"])
        self.assertEqual(p8lib.target_keys("mega65"), ["mega65", "45gs02", "65c02", "6502", "default"])

    def test_llvm_targets_are_one_family_without_cpu(self):
        for target in ("llvm16", "llvm32", "llvm64"):
            self.assertEqual(p8lib.target_keys(target), [target, "llvm", "default"])

    def test_cpu_option(self):
        self.assertEqual(p8lib.target_keys("w65c816sxb", cpu="65816"),
                         ["w65c816sxb", "65816", "65c02", "6502", "default"])
        self.assertEqual(p8lib.target_keys("mega65", cpu="6502"), ["mega65", "6502", "default"])
        with self.assertRaises(p8lib.P8Error):
            p8lib.target_keys("c64", cpu="8080")

    def test_as_option_and_custom_target_file(self):
        self.assertEqual(p8lib.target_keys("mymachine", as_name="cx16"),
                         ["mymachine", "cx16", "65c02", "6502", "default"])
        self.assertEqual(p8lib.target_keys("targets/mymachine.properties", as_name="c64"),
                         ["mymachine", "c64", "6502", "default"])
        with self.assertRaises(p8lib.P8Error):
            p8lib.target_keys("mymachine")

    def test_every_target_has_a_known_cpu_or_none(self):
        for target in set(p8lib.PARENT) | set(p8lib.CPU):
            cpu = p8lib.cpu_of(target)
            self.assertTrue(cpu is None or cpu in p8lib.CPU_FALLBACK, target)


class Shorthand(unittest.TestCase):
    def test_owner_repo_at_tag_in_a_manifest(self):
        plain = p8lib.parse_dep("libmem", "gillham/prog8malloc")
        self.assertEqual((plain.url, plain.ref), ("https://github.com/gillham/prog8malloc", {}))
        short = p8lib.parse_dep("libmem", "gillham/prog8malloc@v1.0")
        table = p8lib.parse_dep("libmem", {"git": "https://github.com/gillham/prog8malloc", "tag": "v1.0"})
        self.assertEqual(short, table)
        for bad in ("gillham/prog8malloc@", "gillham/prog8malloc@-x", "gillham/prog8malloc@a b",
                    "gillham/prog8malloc@a@b", "prog8malloc@v1.0"):
            with self.subTest(bad=bad), self.assertRaises(p8lib.P8Error):
                p8lib.parse_dep("libmem", bad)

    def test_the_line_install_writes(self):
        line = lambda spec, **ref: p8lib.dependency_line("libmem", spec, ref)[0]
        self.assertEqual(line("gillham/prog8malloc"), 'libmem = "gillham/prog8malloc"')
        self.assertEqual(line("gillham/prog8malloc@v1.0"), 'libmem = "gillham/prog8malloc@v1.0"')
        self.assertEqual(line("gillham/prog8malloc", tag="v1.0"), 'libmem = "gillham/prog8malloc@v1.0"')
        self.assertEqual(line("gillham/prog8malloc", branch="main"),
                         'libmem = { git = "https://github.com/gillham/prog8malloc", branch = "main" }')
        self.assertEqual(line("https://example.org/x.git", tag="v1.0"),
                         'libmem = { git = "https://example.org/x.git", tag = "v1.0" }')
        with self.assertRaises(p8lib.P8Error):
            line("gillham/prog8malloc@v1.0", tag="v2.0")
        for spec, ref in (("gillham/prog8malloc@v1.0", {}), ("gillham/prog8malloc", {"tag": "v1.0"})):
            written, dep = p8lib.dependency_line("libmem", spec, ref)
            self.assertEqual(dep.ref, {"tag": "v1.0"})


class Scanner(unittest.TestCase):
    def blocks(self, text):
        return [(b.name, b.merge, b.subs) for b in p8lib.scan_blocks(text)]

    def test_blocks_and_what_hides_them(self):
        text = """
%import textio
%zeropage basicsafe
; not { a block
/* nor
   this { one */
regex {
    %option no_symbol_prefixing, ignore_unused
    str brace = "}{ ; not a comment"
    ubyte c = '{'
    sub match(str pattern) -> bool {
        if pattern[0] == '}' { return true }
        %asm {{
            lda  #'}'   ; } } }
            rts
        }}
        return false
    }
    asmsub fast(ubyte a @A) clobbers(X) {
        %asm {{ rts }}
    }
    extsub $ffd2 = chrout(ubyte c @A)
    ; sub commented_out() { }
}

regex_tables $c000
{
    ubyte[4] t = [1, 2, 3, 4]
}

txt {
    %option merge
    sub print_centered(str s) { }
}
"""
        self.assertEqual(self.blocks(text), [
            ("regex", False, ["match", "fast", "chrout"]),
            ("regex_tables", False, []),
            ("txt", True, ["print_centered"])])

    def test_a_file_without_blocks(self):
        self.assertEqual(self.blocks("%import shared_things\n; nothing else\n"), [])


class PathLibraries(Case):
    def test_version(self):
        self.assertEqual(self.ok("-version").stdout, f"p8lib {p8lib.__version__}\n")

    def test_blocks_are_named_after_the_library(self):
        lib = self.library("libmem", files={"src/libmem.p8": "mem {\n    sub malloc(uword size) { }\n}\n"})
        self.program(self.path_dep("libmem", lib))
        self.fails("install", expect="src/libmem.p8: block 'mem': a block of 'libmem' is named libmem or libmem_...")
        (lib / "src/libmem.p8").write_text("libmem {\n    sub malloc(uword size) { }\n}\nlibmem_pool {\n}\n")
        self.ok("install")
        self.ok("check", cwd=lib)

    def test_merges_must_be_listed(self):
        merge = "txt {\n    %option merge\n    sub print(str s) { }\n    sub print_fast(str s) { }\n}\n"
        lib = self.library("fastprint", files={"src/fastprint.p8": "fastprint { }\n" + merge})
        self.program(self.path_dep("fastprint", lib))
        self.fails("install", expect="merges into the block 'txt', which [merges] does not list")
        manifest = lib / "p8lib.toml"
        manifest.write_text(manifest.read_text() + '[merges]\ntxt = ["print"]\n')
        self.fails("install", expect="defines txt.print_fast, which [merges] does not list")
        manifest.write_text(manifest.read_text().replace('["print"]', '["print", "print_fast"]'))
        self.ok("install")
        # merging into its own block needs no listing
        (lib / "src/fastprint_more.p8").write_text("fastprint {\n    %option merge\n    sub more() { }\n}\n")
        self.ok("install")
        # and a block of someone else without merge is just a clash
        (lib / "src/fastprint_more.p8").write_text("txt {\n    sub more() { }\n}\n")
        self.fails("install", expect="block 'txt': a block of 'fastprint'")

    def test_own_directories(self):
        lib = self.library("regex", '[targets]\nc64 = ["src/c64"]\ndefault = []\n',
                           {"src/c64/regex_platform.p8": "regex_platform { }\n"})
        (self.prog / "src/c64").mkdir(parents=True)
        (self.prog / "src/main.p8").write_text("main { }\n")
        self.program(self.path_dep("regex", lib))
        self.assertEqual(self.srcdirs("-target", "c64"), [lib / "src", lib / "src/c64"])
        self.assertEqual(self.srcdirs("-target", "c64", "-own"), [self.prog / "src", lib / "src", lib / "src/c64"])
        (self.prog / "p8lib.toml").write_text(
            '[targets]\nc64 = ["src/c64"]\ndefault = []\n[dependencies]\n' + self.path_dep("regex", lib))
        self.assertEqual(self.srcdirs("-target", "c64", "-own"),
                         [self.prog / "src", self.prog / "src/c64", lib / "src", lib / "src/c64"])
        self.assertEqual(self.srcdirs("-target", "cx16", "--own"), [self.prog / "src", lib / "src"])
        # a library builds its own demo the same way, with no dependencies at all
        self.assertEqual(self.ok("srcdirs", "-target", "c64", "-own", cwd=lib).stdout,
                         os.pathsep.join([str(lib / "src"), str(lib / "src/c64")]) + "\n")
        shutil.rmtree(self.prog / "src")
        self.fails("srcdirs", "-target", "c64", "-own", expect="directory 'src' does not exist")

    def test_no_dependencies_gives_an_empty_line(self):
        self.program("")
        self.ok("install")
        self.assertEqual(self.srcdirs("-target", "c64"), [])
        self.assertFalse((self.prog / "p8lib.lock").exists())

    def test_no_manifest(self):
        self.fails("srcdirs", "-target", "c64", expect="no p8lib.toml")

    def test_target_directories(self):
        lib = self.library("regex", '[targets]\nc64 = ["src/c64"]\ncx16 = ["src/cx16"]\n6502 = ["src/6502"]\n', {
            "src/c64/regex_platform.p8": "regex_platform { }\n",
            "src/cx16/regex_platform.p8": "regex_platform { }\n",
            "src/6502/regex_platform.p8": "regex_platform { }\n"})
        self.program(self.path_dep("regex", lib))
        self.ok("install")
        self.assertEqual(self.srcdirs("-target", "c64"), [lib / "src", lib / "src/c64"])
        self.assertEqual(self.srcdirs("--target", "cx16"), [lib / "src", lib / "src/cx16"])
        self.assertEqual(self.srcdirs("-target", "pet32"), [lib / "src", lib / "src/6502"])
        self.assertEqual(self.srcdirs("-target", "f256"), [lib / "src", lib / "src/6502"], "65c02 falls back")
        self.assertEqual(self.srcdirs("-target", "mine", "-as", "c64"), [lib / "src", lib / "src/c64"])
        self.fails("srcdirs", "-target", "coco2", expect="has nothing for target coco2")
        self.fails("srcdirs", "-target", "mine", expect="unknown target")

    def test_manifest_option_and_search_upwards(self):
        lib = self.library("regex")
        self.program(self.path_dep("regex", lib))
        (self.prog / "deep/er").mkdir(parents=True)
        self.assertEqual(self.srcdirs("-target", "c64"), [lib / "src"])
        self.assertEqual(p8lib.os.pathsep.join([str(lib / "src")]) + "\n",
                         self.ok("srcdirs", "-target", "c64", cwd=self.prog / "deep/er").stdout)
        self.assertEqual(str(lib / "src") + "\n",
                         self.ok("srcdirs", "-target", "c64", "-manifest", str(self.prog), cwd=self.tmp).stdout)

    def test_false_excludes_a_cpu_and_what_falls_back_to_it(self):
        lib = self.library("nmos", '[targets]\n6502 = []\n65c02 = false\n')
        self.program(self.path_dep("nmos", lib))
        self.assertEqual(self.srcdirs("-target", "c64"), [lib / "src"])
        self.fails("srcdirs", "-target", "cx16", expect="'nmos' is not for 65c02")
        self.fails("srcdirs", "-target", "mega65", expect="'nmos' is not for 65c02")
        self.fails("srcdirs", "-target", "w65c816sxb", "-cpu", "65816", expect="'nmos' is not for 65c02")
        self.assertEqual(self.srcdirs("-target", "mega65", "-cpu", "6502"), [lib / "src"])

    def test_default(self):
        lib = self.library("gfx", '[targets]\ncx16 = ["src/cx16"]\ndefault = []\n',
                           {"src/cx16/gfx_vera.p8": "gfx_vera { }\n"})
        self.program(self.path_dep("gfx", lib))
        self.assertEqual(self.srcdirs("-target", "cx16"), [lib / "src", lib / "src/cx16"])
        self.assertEqual(self.srcdirs("-target", "llvm64"), [lib / "src"])

    def test_key_must_be_the_name(self):
        lib = self.library("regex")
        self.program(self.path_dep("rx", lib))
        self.fails("install", expect="calls itself 'regex'")

    def test_directories_must_stay_inside(self):
        for rel, expect in (("../other", "has .. in it"), ("/etc", "is not relative"),
                            ("src/nothere", "does not exist"), ("src\\\\c64", "backslash")):
            with self.subTest(rel=rel):
                lib = self.library("esc", f'[targets]\nc64 = ["{rel}"]\n', directory=f"esc{abs(hash(rel))}")
                self.program(self.path_dep("esc", lib))
                self.fails("install", expect=expect)
                self.fails("srcdirs", "-target", "c64", expect=expect)

    @unittest.skipIf(os.name == "nt", "symbolic links need rights on Windows")
    def test_symbolic_link_out_of_the_library(self):
        lib = self.library("esc", '[targets]\nc64 = ["src/out"]\n')
        (self.tmp / "elsewhere").mkdir()
        (lib / "src/out").symlink_to(self.tmp / "elsewhere")
        self.program(self.path_dep("esc", lib))
        self.fails("srcdirs", "-target", "c64", expect="leads outside the library")

    def test_file_names(self):
        lib = self.library("regex", files={"src/helpers.p8": "helpers { }\n"})
        self.program(self.path_dep("regex", lib))
        self.fails("install", expect="src/helpers.p8: a file of 'regex' is named")

    def test_name_of_a_compiler_module(self):
        lib = self.library("strings")
        self.program(self.path_dep("strings", lib))
        self.fails("install", expect="module in a compiler's library")

    def test_no_src_directory(self):
        lib = self.library("regex")
        shutil.rmtree(lib / "src")
        self.program(self.path_dep("regex", lib))
        self.fails("install", expect="keeps its sources in src/")

    def test_same_module_in_two_directories(self):
        lib = self.library("regex", '[targets]\nc64 = ["src/c64"]\n', {
            "src/regex_platform.p8": "regex_platform { }\n",
            "src/c64/regex_platform.p8": "regex_platform { }\n"})
        self.program(self.path_dep("regex", lib))
        self.fails("srcdirs", "-target", "c64", expect="'regex_platform' is there twice")

    def test_two_libraries_merge_the_same_name(self):
        one = self.library("fastprint", '[merges]\ntxt = ["print", "print_fast"]\n')
        two = self.library("colorprint", '[merges]\ntxt = ["print"]\n')
        self.program(self.path_dep("fastprint", one))
        self.assertIn("merges: txt.print, txt.print_fast", self.ok("list").stdout)
        self.program(self.path_dep("fastprint", one) + self.path_dep("colorprint", two))
        for command in (["install"], ["srcdirs", "-target", "c64"]):
            self.fails(*command, expect="'fastprint' and 'colorprint' both define txt.print")

    def test_merges_of_different_names_are_fine(self):
        one = self.library("fastprint", '[merges]\ntxt = ["print_fast"]\n')
        two = self.library("colorprint", '[merges]\ntxt = ["print_color"]\n')
        self.program(self.path_dep("fastprint", one) + self.path_dep("colorprint", two))
        self.assertEqual(self.srcdirs("-target", "c64"), [one / "src", two / "src"])

    def test_check(self):
        good = self.library("regex", '[targets]\nc64 = ["src/c64"]\nc65 = []\n',
                            {"src/c64/regex_platform.p8": "regex_platform { }\n"})
        result = self.ok("check", cwd=good)
        self.assertIn("follows the rules", result.stderr)
        self.assertIn("[targets] c65 is not a target", result.stderr)
        bad = self.library("bad", '[merges]\nbad_tables = ["x"]\n[dependencies]\nother = { path = "../other" }\n',
                           {"src/tables.p8": "tables { }\n"})
        result = self.fails("check", cwd=bad, expect="src/tables.p8")
        self.assertIn("library's own block", result.stderr)
        self.assertIn("a path works only on this machine", result.stderr)
        self.program("")
        self.fails("check", expect="has no [package]")


class Init(Case):
    def test_a_library_that_is_ready(self):
        root = self.tmp / "prog8regex"
        for rel in ("src/regex.p8", "src/c64/regex_platform.p8", "src/cx16/regex_platform.p8", "src/docs/notes.txt"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("")
        result = self.ok("init", cwd=root)
        self.assertEqual(result.stdout, "")
        manifest = p8lib.Manifest(root / "p8lib.toml")
        self.assertEqual(manifest.name, "regex")
        self.assertEqual(manifest.targets, {"c64": ["src/c64"], "cx16": ["src/cx16"]})
        self.assertNotIn(".p8:", result.stderr)
        self.ok("check", cwd=root)
        self.fails("init", cwd=root, expect="already")

    def test_a_library_that_needs_work(self):
        root = self.tmp / "prog8malloc"
        for rel in ("src/libmem.p8", "src/main.p8", "src/c64/malloc_platform.p8", "src/mymachine/malloc_platform.p8"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("")
        result = self.ok("init", cwd=root)
        self.assertEqual(p8lib.Manifest(root / "p8lib.toml").name, "malloc")
        self.assertIn("src/libmem.p8: a file of 'malloc' is named", result.stderr)
        self.assertIn("[targets] mymachine is not a target", result.stderr)
        (root / "p8lib.toml").unlink()
        self.ok("init", "libmem", cwd=root)
        self.assertEqual(p8lib.Manifest(root / "p8lib.toml").name, "libmem")
        self.fails("init", "Lib-Mem", cwd=self.prog, expect="is not a library name")


class GitLibraries(Case):
    def test_install_locks_the_commit_and_update_moves_it(self):
        lib = self.library("regex")
        first = self.commit(lib)
        self.program(self.git_dep("regex", lib))
        self.fails("srcdirs", "-target", "c64", expect='run "p8lib install"')
        self.ok("install")
        self.assertEqual(self.lock()["regex"]["commit"], first)
        clone = self.srcdirs("-target", "c64")[0]
        self.assertTrue(clone.is_relative_to(self.store), clone)
        self.assertTrue((clone / "regex.p8").is_file())
        self.assertIn(first[:10], self.ok("list").stdout)

        (lib / "src/regex_more.p8").write_text("regex_more { }\n")
        second = self.commit(lib)
        self.ok("install")
        self.assertEqual(self.lock()["regex"]["commit"], first, "install keeps the lock")
        self.assertEqual(self.srcdirs("-target", "c64"), [clone])

        self.assertIn("regex", self.ok("update").stderr)
        self.assertEqual(self.lock()["regex"]["commit"], second)

        # a release for another version of the language is said out loud
        manifest = lib / "p8lib.toml"
        manifest.write_text(manifest.read_text() + 'prog8 = "12"\n')
        self.commit(lib)
        self.assertIn("was written for Prog8 (not said), and is now for Prog8 12", self.ok("update").stderr)
        self.assertIn("prog8 12", self.ok("list").stdout)
        manifest.write_text(manifest.read_text().replace('prog8 = "12"', 'prog8 = "14"'))
        third = self.commit(lib)
        self.assertIn("regex was written for Prog8 12, and is now for Prog8 14", self.ok("update").stderr)
        self.assertIn("prog8 14", self.ok("list").stdout)
        (lib / "src/regex_evenmore.p8").write_text("regex_evenmore { }\n")
        self.commit(lib)
        self.assertNotIn("written for", self.ok("update").stderr)
        second = self.lock()["regex"]["commit"]
        self.assertNotEqual(second, third)
        newer = self.srcdirs("-target", "c64")[0]
        self.assertNotEqual(newer, clone)
        self.assertTrue((newer / "regex_more.p8").is_file())
        self.assertTrue(clone.is_dir(), "the old commit stays for other projects")

    def test_a_fresh_checkout_installs_what_the_lock_says(self):
        lib = self.library("regex")
        first = self.commit(lib)
        self.program(self.git_dep("regex", lib))
        self.ok("install")
        (lib / "src/regex_more.p8").write_text("regex_more { }\n")
        self.commit(lib)
        shutil.rmtree(self.store)                       # another machine
        self.fails("srcdirs", "-target", "c64", expect="'regex' is not installed")
        self.ok("install")
        self.assertEqual(self.lock()["regex"]["commit"], first)
        self.assertFalse((self.srcdirs("-target", "c64")[0] / "regex_more.p8").exists())

    def test_tag_branch_and_rev(self):
        lib = self.library("regex")
        tagged = self.commit(lib, tag="v1.0")
        (lib / "src/regex_more.p8").write_text("regex_more { }\n")
        head = self.commit(lib)
        subprocess.run([*GIT, "-C", str(lib), "branch", "stable", tagged], check=True)
        for ref, commit in (({"tag": "v1.0"}, tagged), ({"branch": "stable"}, tagged),
                            ({"branch": "main"}, head), ({"rev": tagged}, tagged), ({}, head)):
            with self.subTest(ref=ref):
                self.program(self.git_dep("regex", lib, **ref))
                self.ok("install")
                self.assertEqual(self.lock()["regex"], {"git": lib.as_uri(), "requested": ref, "commit": commit})
        self.program(self.git_dep("regex", lib, tag="v9"))
        self.fails("install", expect="no tag 'v9'")

    def test_changed_manifest_needs_install(self):
        lib = self.library("regex")
        self.commit(lib, tag="v1.0")
        self.program(self.git_dep("regex", lib))
        self.ok("install")
        self.program(self.git_dep("regex", lib, tag="v1.0"))
        self.fails("srcdirs", "-target", "c64", expect='run "p8lib install"')

    def test_removed_library_leaves_the_lock(self):
        lib = self.library("regex")
        self.commit(lib)
        self.program(self.git_dep("regex", lib))
        self.ok("install")
        self.program("")
        self.ok("install")
        self.assertEqual(self.lock(), {})

    def test_library_of_a_library(self):
        strings2 = self.library("strings2")
        self.commit(strings2)
        regex = self.library("regex", "[dependencies]\n" + self.git_dep("strings2", strings2))
        self.commit(regex)

        self.program(self.git_dep("regex", regex))
        self.fails("install", expect="Only https:// URLs are taken from a library")

        self.program(self.git_dep("regex", regex) + self.git_dep("strings2", strings2))
        self.ok("install")
        self.assertEqual(sorted(self.lock()), ["regex", "strings2"])
        dirs = self.srcdirs("-target", "c64")
        self.assertEqual([d.name for d in dirs], ["src", "src"])
        self.assertTrue((dirs[0] / "regex.p8").is_file() and (dirs[1] / "strings2.p8").is_file())

    def test_two_requests_that_do_not_agree(self):
        strings2 = self.library("strings2")
        self.commit(strings2, tag="v1")
        (strings2 / "src/strings2_more.p8").write_text("strings2_more { }\n")
        self.commit(strings2, tag="v2")
        regex = self.library("regex", "[dependencies]\n" + self.git_dep("strings2", strings2, tag="v1"))
        self.commit(regex)

        self.program(self.git_dep("regex", regex) + self.git_dep("strings2", strings2, tag="v2"))
        result = self.fails("install", expect="'strings2' is wanted in two ways")
        self.assertIn("the program wants", result.stderr)
        self.assertIn("'regex' wants", result.stderr)

        self.program(self.git_dep("regex", regex) + self.git_dep("strings2", strings2, tag="v1"))
        self.ok("install")

    def test_the_programs_path_stands_for_every_request(self):
        strings2 = self.library("strings2")
        self.commit(strings2)
        regex = self.library("regex", "[dependencies]\n" + self.git_dep("strings2", strings2))
        self.commit(regex)
        self.program(self.git_dep("regex", regex) + self.path_dep("strings2", strings2))
        self.ok("install")
        self.assertEqual(sorted(self.lock()), ["regex"])
        self.assertEqual(self.srcdirs("-target", "c64")[1], strings2 / "src")

    def test_a_library_may_not_use_a_path(self):
        regex = self.library("regex", '[dependencies]\nstrings2 = { path = "../strings2" }\n')
        self.library("strings2")
        self.commit(regex)
        self.program(self.git_dep("regex", regex))
        self.fails("install", expect="only the program's own p8lib.toml may")

    def test_install_a_library_by_name_writes_the_manifest(self):
        regex = self.library("regex", directory="prog8regex")
        self.commit(regex, tag="v1.0")
        strings2 = self.library("strings2")
        self.commit(strings2)
        manifest = self.prog / "p8lib.toml"

        result = self.ok("install", regex.as_uri())             # no manifest yet
        self.assertIn(f'regex = {{ git = "{regex.as_uri()}" }}', result.stderr)
        self.assertEqual(manifest.read_text(), f'[dependencies]\nregex = {{ git = "{regex.as_uri()}" }}\n')
        self.assertEqual(sorted(self.lock()), ["regex"])
        self.fails("install", regex.as_uri(), expect="'regex' is in")

        manifest.write_text("# my game\n[dependencies]\n# libraries\n"
                            f"{self.git_dep('regex', regex)}\n[other]\nkey = 1  # kept\n")
        self.ok("install", strings2.as_uri())
        self.assertEqual(manifest.read_text(),
                         "# my game\n[dependencies]\n# libraries\n"
                         f"{self.git_dep('regex', regex)}"
                         f'strings2 = {{ git = "{strings2.as_uri()}" }}\n'
                         "\n[other]\nkey = 1  # kept\n")
        self.assertEqual(sorted(self.lock()), ["regex", "strings2"])
        self.assertEqual(len(self.srcdirs("-target", "c64")), 2)

    def test_install_a_library_with_a_tag(self):
        regex = self.library("regex")
        tagged = self.commit(regex, tag="v1.0")
        (regex / "src/regex_more.p8").write_text("regex_more { }\n")
        self.commit(regex)
        self.ok("install", regex.as_uri(), "-tag", "v1.0")
        self.assertIn('tag = "v1.0"', (self.prog / "p8lib.toml").read_text())
        self.assertEqual(self.lock()["regex"]["commit"], tagged)
        self.fails("install", "-tag", "v1.0", expect="go with a library")

    def test_install_a_library_that_cannot_be_used_changes_nothing(self):
        bad = self.library("regex", files={"src/helpers.p8": "helpers { }\n"})
        self.commit(bad)
        plain = self.library("plain")
        (plain / "p8lib.toml").unlink()
        self.commit(plain)
        self.program("# untouched\n")
        before = (self.prog / "p8lib.toml").read_text()
        self.fails("install", bad.as_uri(), expect="src/helpers.p8")
        self.fails("install", plain.as_uri(), expect="not a p8lib library")
        self.fails("install", "not-a-library", expect="not owner/repo and not a URL")
        self.assertEqual((self.prog / "p8lib.toml").read_text(), before)
        self.assertFalse((self.prog / "p8lib.lock").exists())

    def test_install_a_library_into_a_manifest_it_cannot_edit(self):
        regex = self.library("regex")
        self.commit(regex)
        (self.prog / "p8lib.toml").write_text('dependencies = { }\n')
        self.fails("install", regex.as_uri(), expect="add this line by hand")
        self.assertEqual((self.prog / "p8lib.toml").read_text(), 'dependencies = { }\n')

    def test_url_that_looks_like_an_option(self):
        self.program('regex = { git = "--upload-pack=touch_pwned" }\n')
        self.fails("install", expect="may not start with '-'")

    def test_repository_without_manifest(self):
        lib = self.library("regex")
        (lib / "p8lib.toml").unlink()
        self.commit(lib)
        self.program(self.git_dep("regex", lib))
        self.fails("install", expect="there is no p8lib.toml")


COMPILERS = [c for c in ("prog8c", "prog8c-beta", "prog8c-llvm", "p8c") if shutil.which(c)]


class Compilers(Case):
    """The whole way: p8lib's answer, given to a real compiler."""

    def compile(self, compiler, target, dirs):
        (self.prog / "main.p8").write_text(
            "%import greet\n%import regex\n"
            "main {\n    sub start() {\n        cx16.r0L = greet.answer() + regex.answer() + regex_platform.id()\n    }\n}\n")
        srcdirs = os.pathsep.join(map(str, dirs))
        options = [] if compiler == "p8c" else ["-plaintext", "-quiet"]
        return subprocess.run([compiler, "-target", target, "-check", *options, "-srcdirs", srcdirs, "main.p8"],
                              cwd=self.prog, capture_output=True, text=True)

    @unittest.skipUnless(COMPILERS, "no Prog8 compiler on the PATH")
    def test_compilers_find_the_libraries(self):
        greet = self.library("greet")
        self.commit(greet, tag="v1")
        regex = self.library("regex", '[targets]\nc64 = ["src/c64"]\n6502 = ["src/6502"]\n', {
            "src/regex.p8": "%import regex_platform\nregex {\n    sub answer() -> ubyte { return 42 }\n}\n",
            "src/c64/regex_platform.p8": "regex_platform {\n    sub id() -> ubyte { return 64 }\n}\n",
            "src/6502/regex_platform.p8": "regex_platform {\n    sub id() -> ubyte { return 2 }\n}\n"})
        self.program(self.git_dep("greet", greet, tag="v1") + self.path_dep("regex", regex))
        self.ok("install")
        for compiler in COMPILERS:
            for target in ("c64", "pet32"):
                with self.subTest(compiler=compiler, target=target):
                    with_libs = self.compile(compiler, target, self.srcdirs("-target", target))
                    self.assertEqual(with_libs.returncode, 0, with_libs.stdout + with_libs.stderr)
                    without = self.compile(compiler, target, [self.prog])
                    self.assertNotEqual(without.returncode, 0, "the program needs the libraries")


if __name__ == "__main__":
    unittest.main()
