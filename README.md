# p8lib

`p8lib` fetches third-party [Prog8](https://github.com/irmen/prog8)
libraries and tells your build where they are.  It does for Prog8 what
`pkg-config` does for C: the compiler knows nothing about packages, and the
build asks `p8lib` for the directories to give to `-srcdirs`.

```
p8lib install gillham/prog8malloc
dirs=$(p8lib srcdirs -target c64) || exit 1
prog8c -target c64 -srcdirs "src:$dirs" main.p8
```

It works with any Prog8 compiler that has `-srcdirs`.  It is tried with
the official compiler (12 and 14), its LLVM branch, and p8p8.

The design, and the reasons for it, are in [docs/DESIGN.md](docs/DESIGN.md).

## Installing

`p8lib` is one Python file that needs Python 3.12 or later, git, and
nothing else.  Either of

```
uv tool install git+https://github.com/p8ug/p8lib
pipx install git+https://github.com/p8ug/p8lib
```

puts the `p8lib` command on your PATH.  To bring it up to date later:

```
uv tool upgrade p8lib
pipx install --force git+https://github.com/p8ug/p8lib
```

`pipx upgrade` does not look at a git repository, so with pipx the install
is repeated.

From a checkout of this repository, `uv tool install .` or `pipx install .`
does the same.  Copying `p8lib.py` to a directory on the PATH and running
it as `p8lib.py` works as well.

## Using libraries in a program

A program lists its libraries in a `p8lib.toml` in its repository:

```toml
[dependencies]
libmem = "gillham/prog8malloc"
```

`p8lib install owner/repo` writes that line for you, and makes the file
when there is none.  The name on the left is the library's own name, which
`p8lib` asks the library for; it is also what you `%import`.

```
p8lib install gillham/prog8malloc          # the newest commit
p8lib install gillham/prog8malloc@v1.0     # the tag v1.0
```

Then run `p8lib install` with no arguments whenever `p8lib.toml` has
changed, or after checking the program out somewhere new.  It fetches what
is missing and writes `p8lib.lock`, which records the exact commit of
every library.  Commit both files: everyone who builds the program then
gets the same library sources.

### Building

`p8lib srcdirs -target NAME` prints the directories of the program's
libraries for that target, joined with `:` (`;` on Windows).  It never
uses the network.

```
dirs=$(p8lib srcdirs -target cx16) || exit 1
prog8c -target cx16 -srcdirs "src:$dirs" main.p8
```

Take the result in a variable first, as above.  Written inside the
compiler's command line, a failure of `p8lib` would be lost and the
compiler would complain about a missing import instead.

In a Makefile:

```make
main.prg: main.p8
	dirs=$$(p8lib srcdirs -target $(TARGET)) || exit 1; \
	prog8c -target $(TARGET) -srcdirs "src:$$dirs" main.p8
```

With `-own`, the line starts with the program's own `src/` (and what a
`[targets]` table in the program's `p8lib.toml` adds for the target), so
the build line needs no directory of its own:

```
dirs=$(p8lib srcdirs -target cx16 -own) || exit 1
prog8c -target cx16 -srcdirs "$dirs" src/main.p8
```

For a custom target file, say which known target it is like:
`p8lib srcdirs -target mymachine -as cx16`.  For a CPU other than the
target's usual one, add `-cpu 65816`.

### Ways to name a library

```toml
[dependencies]
regex    = "gillham/prog8regex"                 # GitHub, default branch
libmem   = "gillham/prog8malloc@v1.0"           # GitHub, a tag
strings2 = { git = "https://example.org/strings2.git", branch = "main" }
other    = { git = "https://example.org/other.git", rev = "<full commit>" }
mylib    = { path = "../mylib" }                # a directory on this machine
```

A `path` is for working on a library and a program at the same time.  It
is not locked, and it takes the place of that library wherever another
library asks for it.

### Staying on a version

`p8lib install` never moves a library that is in the lock.  `p8lib update`
does: it takes each library to the newest commit that `p8lib.toml` allows,
and says when the new commit is written for another version of Prog8:

```
$ p8lib update
p8lib: libmem b30ee0f5d5 -> 963b786c7e
p8lib: libmem was written for Prog8 12, and is now for Prog8 14
```

A program that has to stay with an older compiler names the tag
(`"gillham/prog8malloc@v1.0"`).  `p8lib update libmem` updates one
library.

To remove a library, take its line out of `p8lib.toml` and run `p8lib
install`.

## Commands

| Command | What it does |
|---|---|
| `p8lib install` | fetches the libraries of `p8lib.toml` and brings `p8lib.lock` up to date |
| `p8lib install owner/repo[@tag]` | adds a library to `p8lib.toml` first; also takes a git URL, and `-tag`, `-branch` or `-rev` |
| `p8lib update [name ...]` | moves the lock to the newest commits the manifest allows |
| `p8lib list` | the libraries of the program: commit, Prog8 version, description, and what they merge into other blocks |
| `p8lib srcdirs -target T` | prints the directories for the compiler's `-srcdirs`; with `-own`, the project's own first |
| `p8lib check` | for a library's author: checks the library in the current directory |
| `p8lib init [name]` | for a library's author: writes a first `p8lib.toml` |
| `p8lib -version` | prints the version of `p8lib` |

Every command but `init` takes `-manifest PATH` to use a `p8lib.toml` other
than the one found from the current directory upwards.  Options can be written with
one dash or two.

## Writing a library

A library is a git repository with a `p8lib.toml` in its root and its
sources in `src/`:

```
prog8regex/
    p8lib.toml
    src/
        regex.p8
        c64/regex_platform.p8
        cx16/regex_platform.p8
    demo/               tests, examples, a Makefile: anywhere but src/
```

```toml
[package]
name = "regex"
description = "Regular expressions"
license = "MIT"
prog8 = "14"

[targets]
c64  = ["src/c64"]
cx16 = ["src/cx16"]

[dependencies]
strings2 = "someone/prog8strings2"
```

In an existing library, `p8lib init` writes this file from what it finds
and lists what is left to do.  `p8lib check` says whether the library
follows the rules.

### Names

Prog8 has one namespace for modules and one for blocks, shared by the
program, every library and the compiler's own library.  So everything in
a library carries the library's name:

- every `.p8` file in `src/` and in the target directories is `name.p8`
  or `name_something.p8`;
- every block is named the same way: `regex { }`, `regex_tables { }`;
- the name is not that of a module in a compiler's library (`strings`,
  `math`, `textio`, ...), which a file in `-srcdirs` would silently
  replace.

A subroutine cannot have the name of its block, so do not name a library
after its main routine: the library with `malloc()` is `libmem`.

Everything in `src/` can be imported by a program, so tests and demo
programs go in another directory.  Build them with the library's own
directories from `p8lib`, which also brings in the library's dependencies:

```
dirs=$(p8lib srcdirs -target c64 -own) || exit 1
prog8c -target c64 -srcdirs "$dirs:demo" demo/main.p8
```

### Targets

`src/` is always given to the compiler.  `[targets]` lists the directories
that are added for a target.  For a target, `p8lib` tries these keys in
order and takes the first that is there:

1. the target's name (`coco2ram`),
2. its family (`coco2`, then `coco`),
3. its CPU, and the CPUs whose code that one runs (`65816` or `45gs02`,
   then `65c02`, then `6502`),
4. `default`.

```toml
[targets]
c64     = ["src/6502", "src/c64"]   # the first key that matches decides alone
6502    = ["src/6502"]              # every other 6502, 65C02, 65816, 45GS02
llvm    = ["src/portable"]          # llvm16, llvm32, llvm64
65816   = false                     # not for this CPU
default = []                        # everything else: src/ alone
```

A library with no `[targets]` table is the same everywhere.  One that has
the table and no matching key does not support the target, and `p8lib
srcdirs` says so.

### Changing a block of someone else

A library that adds to or replaces something in a block it does not own,
with `%option merge`, lists it:

```toml
[merges]
txt = ["print", "print_centered"]
```

When two libraries replace the same routine, the compiler takes the one
imported last.  `p8lib` refuses a program whose libraries list the same
name, and `p8lib list` shows what each library merges.  `p8lib check`
refuses a merge into a block that is not listed, and a subroutine in it
that is not listed.

### Releases

`p8lib` fetches whatever commit a program asks for, so a release is a git
tag.  When a library stops supporting an older compiler, keep a tag for
the old one (`v1.0` for Prog8 12, `v2.0` for 14) and set `prog8` in each.

## Where things are kept

| What | Where |
|---|---|
| the program's needs | `p8lib.toml`, in the program's repository |
| the exact commits | `p8lib.lock`, beside it |
| the fetched libraries | `~/.local/share/p8lib` (`$XDG_DATA_HOME/p8lib`; `%LOCALAPPDATA%\p8lib` on Windows; `$P8LIB_HOME` in place of any of them) |

The fetched libraries are shared between programs, one directory per
repository and commit.  Nothing in a library is run when it is fetched.

## Tests

```
python3 -m unittest discover -s tests
```

The tests make their libraries and git repositories on the spot and use
no network.  One of them gives `p8lib`'s answer to each Prog8 compiler it
finds on the PATH (`prog8c`, `prog8c-beta`, `prog8c-llvm`, `p8c`).

## Not there yet

- a catalog of short names, for `p8lib install regex`;
- `p8lib remove`;
- finding variables and constants that a library adds to a block of
  someone else and does not list in `[merges]` (subroutines are found);
- cleaning old commits out of the store.
