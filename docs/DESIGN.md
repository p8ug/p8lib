# p8lib: design

`p8lib` fetches third-party Prog8 libraries and tells a build where they
are.  It does for Prog8 what `pkg-config` does for C: the compiler knows
nothing about packages, and the build asks `p8lib` for the directories to
put in `-srcdirs`.

This is a design, not a description of what `p8lib.py` does today.
"The first version" says what is built first, "Later" what is left out of
it, and "Open questions" what is not decided.

## Goals

- A developer shares a library by putting it on GitHub with one metadata
  file in the root of the repository.
- A program says which libraries it needs in one file, and anyone who
  checks the program out gets the same library sources.
- It works with every Prog8 compiler that has `-srcdirs`, with no change
  to any of them (see "Compilers").

## Not goals

- No `-l` option in a compiler.  It would tie builds to the one compiler
  that has it.  (See "Later" for how a compiler could join in without
  knowing about packages.)
- No building.  Libraries are sources; `p8lib` only fetches them and
  prints paths.
- No code from a library runs at install time: no scripts, no Makefile
  fragments, no hooks.

## Compilers

| Command | Compiler |
|---|---|
| `prog8c` | the official compiler, 12.3.5 (stable) |
| `prog8c-beta` | the official compiler, 14 (prerelease) |
| `prog8c-llvm` | the LLVM branch of the official compiler |
| `p8c` | p8p8 |

The design rests on four things a compiler does.  All four compilers do
them the same way (tried with `-target c64`):

- `-srcdirs a:b` is searched for imported modules.
- A module in `-srcdirs` is taken in place of a module of the same name in
  the compiler's own library, with no message.
- Two blocks of the same name without `%option merge` are an error.
- A subroutine in a `%option merge` block replaces one of the same name
  and signature in the block it merges into.  A different signature is a
  name conflict.

Nothing here needs version 14, so 12.3.5 is supported at no cost.

## A tool of its own

`p8lib` lives in its own repository and is installed by itself.  It is not
part of any compiler's distribution.

- It serves every compiler.  Shipped with one of them, it would look as if
  it were only for that one, and a library is worth publishing only when
  everyone can use it.
- It changes with the manifest format, not with a compiler.  A fix to
  `p8lib` should not wait for a compiler's release.
- It is Python and needs git and the network.  A compiler should not gain
  those as requirements for something it never calls.
- A neutral tool with a documented file format is something any of the
  compilers' projects can point to or adopt.

What a compiler's project does instead:

- shows the `p8lib srcdirs` build lines in its documentation;
- gets its target names, with their families and CPUs, into `p8lib`'s
  table of targets (see "Choosing the directories for a target").

Installing: `p8lib` is one Python file that needs only the standard
library.  `pipx install` or `uv tool install` from the repository puts it
on the PATH (`pyproject.toml` needs a `[project.scripts]` entry for
that); copying `p8lib.py` there works too.

The standard library reads TOML and does not write it.  So the lock,
which `p8lib` writes, is JSON, and the only thing `p8lib` does to a
`p8lib.toml` is add a line to it (see "Commands").

## How a build uses it

```
p8lib install
dirs=$(p8lib srcdirs -target c64) || exit 1
p8c -target c64 -srcdirs "src:$dirs" hello.p8
```

`p8lib srcdirs` prints one line: the directories for the given target,
absolute, joined with the platform's path separator (`:` or `;`).  Nothing
else goes to standard output; messages go to standard error.  The line is
empty when the program has no libraries.

It reads the project's `p8lib.toml`, found in the current directory or the
nearest one above it (`-manifest PATH` names another), and the
`p8lib.lock` beside it.

`srcdirs` never uses the network.  When a library is not installed, has
nothing for the target, or conflicts with another (see "Names"), it prints
what is wrong and what to run on standard error and exits with a non-zero
status.

Take the result in an assignment, as above.  The shell keeps the status of
an assignment and loses that of `"src:$(p8lib srcdirs ...)"` inside a
command, where a failure gives the compiler an empty path and the error
the user sees is a missing import.  In a Makefile the same two lines go in
the rule:

```make
hello.prg: hello.p8
	dirs=$$(p8lib srcdirs -target $(TARGET)) || exit 1; \
	p8c -target $(TARGET) -srcdirs "src:$$dirs" hello.p8
```

With `-own` the line starts with the project's own directories: its
`src/`, and what its own `[targets]` table adds for the target.  A program
that keeps its sources in `src/` then writes no directory of its own on
the build line, and a library builds its tests and demos the way a
program that uses it would:

```
dirs=$(p8lib srcdirs -target c64 -own) || exit 1
prog8c -target c64 -srcdirs "$dirs:demo" demo/main.p8
```

Options are written `-target` as the compilers write them; `--target` is
accepted as well, as the compilers accept it.

## Layout of a library

```
prog8regex/
    p8lib.toml
    src/
        regex.p8
        c64/regex_platform.p8
        cx16/regex_platform.p8
```

`src/` holds what is the same everywhere.  A directory under it holds what
differs for a target; the library's main module imports
`regex_platform`, and `-srcdirs` decides which file that is.

## Names

The name in `[package]` is the library's identity.  Everything else
follows it:

- It is a Prog8 identifier in lower case: `regex`, `strings2`.
- The key a manifest lists the library under is that name.  `p8lib
  install` refuses `rx = "gillham/prog8regex"` when the library there
  calls itself `regex`.
- Two libraries of the same name are the same library.  A program cannot
  have both, wherever they come from.
- The repository's name is free.

Prog8 has one namespace for modules and one for blocks, shared by the
program, every library and the compiler's own library.  So:

- Every file in a library is the name, or the name, `_` and more:
  `regex.p8`, `regex_platform.p8`, `regex_tables.p8`.
- Every block a library declares is named the same way: `regex { }`,
  `regex_platform { }`.
- A library's name is not the name of a module in a compiler's library
  (`strings`, `math`, `textio`).  The compilers would take the library's
  file in place of their own without a message.  `p8lib` has the list.
- `platform.p8` and the block `platform` are left to the program.

A subroutine cannot have the name of the block it is in (the official
compiler refuses it, for 64tass).  So a library is not named after its
main routine: the one that offers `malloc()` is `libmem`, and a program
calls `libmem.malloc()`.

The compilers stop at two blocks of one name, so that mistake cannot go
unseen.  The prefix is there so that it does not happen, and so that
someone reading `regex_tables.x` knows where it comes from.

### Merging into a block of someone else

A library can add to a block it does not own, or replace a subroutine in
it, with `%option merge`:

```
txt {
    %option merge
    asmsub print(str text @AY) clobbers(A,Y) { ... }    ; replaces txt.print
    sub print_centered(str text) { ... }                ; adds to txt
}
```

When two libraries replace the same subroutine, the compilers take the one
that is imported last.  The official compilers print an INFO line for each
replacement; `p8c` prints nothing.  Which code runs then depends on the
order of two `%import` lines, and that is not something to find out in a
debugger.

So a library says in its manifest what it defines in blocks it does not
own:

```toml
[merges]
txt = ["print", "print_centered"]
```

- Two libraries of one program that list the same name in the same block
  are a conflict, whether they add it or replace it.  `p8lib install` and
  `p8lib srcdirs` name both libraries and stop.  The program drops one of
  them.
- `p8lib list` shows what each library merges, so that a program's author
  sees that `txt.print` is no longer the compiler's.

A library whose purpose is not the replacement should keep its routine in
its own block (`fastprint.print`) and leave the patching to the program.
A program's own merges are its own business: `p8lib` does not look at the
program's sources.

### What checks the names

`p8lib check`, for a library's author, and `p8lib install` and `p8lib
srcdirs`, for every library of a program, check:

- the library's name, and that it is not a module of a compiler's library;
- the names of the `.p8` files in `src/` and in the directories of
  `[targets]`;
- the names of the blocks in those files;
- that a block of someone else is only merged into, that `[merges]` lists
  it, and that it lists every subroutine the library defines there;
- that two libraries do not list the same name in `[merges]`.

A library that breaks a rule is refused.

For the blocks `p8lib` reads the sources, without parsing them: it blanks
comments, strings and inline assembly, counts braces, and takes the name
in front of each brace that opens at the top of a file.  Inside a block
it sees `%option merge` and the names after `sub`, `asmsub` and `extsub`.

`p8lib srcdirs` also refuses when two of the directories it would print
hold a module of the same name, because the compiler would silently take
the first.

It does not see variables and constants.  One that a library adds to a
block of someone else can be listed in `[merges]`, and then two libraries
that add the same one are told so, but nothing finds one that is not
listed.

## p8lib.toml

One file name for both roles.  A program's file has only
`[dependencies]`; a library's file also describes the library.

A program:

```toml
[dependencies]
regex = "gillham/prog8regex"
libmem = "gillham/prog8malloc@v1.0"
strings2 = { git = "https://example.org/strings2.git", tag = "v1.2" }
mylib = { path = "../mylib" }
```

A library:

```toml
[package]
name = "regex"
description = "Regular expressions"
license = "MIT"
prog8 = "12"

[targets]
c64  = ["src/c64"]
cx16 = ["src/cx16"]
6502 = ["src/6502"]        # any other target with a 6502 or a CPU that runs its code

[dependencies]
strings2 = "someone/prog8strings2"
```

- `"owner/repo"` means that repository on GitHub, over https, and
  `"owner/repo@v1.0"` its tag `v1.0`.  The table form gives a git URL and optionally a `tag`, a `branch` or a `rev` (the
  full commit).  With none of the three it is the head of the default
  branch at the time the lock is written.
- `path` is a directory on this machine, for working on a library and a
  program together.  It is not cloned and not locked, and only the
  program's own manifest may use it.  It stands for every request for that
  name: a library that wants `strings2` from GitHub gets the program's
  directory.
- `src/` is always included; `[targets]` lists what is added to it.  A
  library's sources are always in `src/`, which leaves the root of the
  repository to its Makefile, its tests and its examples.
- `prog8` is the oldest version of the language the library is written
  for, in the official compiler's numbering.  `p8lib list` shows it, and
  `p8lib update` says so when it moves a library to a commit with another
  value.  Nothing checks it: `p8lib` does not know which compiler will
  run.  A library that drops an old compiler keeps a tag for it (`v1.0`
  for Prog8 12, `v2.0` for 14), and a program that stays with the old
  compiler pins the tag.

### Dependencies of libraries

A library's own dependencies are fetched too, so a program lists only what
it imports itself.  The result is one flat set of libraries, one of each
name, and the lock records all of them.

When the program and a library, or two libraries, ask for the same name
from different repositories or at different commits, `p8lib install`
stops and names who asks for what.  There is no rule that picks one.  A
`p8lib.lock` in a library's repository is not read.

### What a manifest may say

A library's manifest is input from a stranger.

- A directory in `[targets]` is relative, has no `..` in it, and is
  inside the library's clone after symbolic links are followed.  Anything
  else is refused, and `srcdirs` never prints a directory outside the
  clone of the library it belongs to.
- A git URL is `https://`.  Others are refused unless the program's own
  manifest lists the dependency with that URL.
- Names are checked against the rule in "Names" before they are used in a
  path or on a command line.  git is run with `--` before the URL, without
  submodules.

### Choosing the directories for a target

Target names differ between compilers and come in families (`coco2`,
`coco2ram`, `coco3ram`; `trs80`, `trs80m4`).  A library usually has one
directory for a family or for a CPU.  The lookup tries these keys of
`[targets]` in order and takes the first that is there:

1. the target's name,
2. its family (`coco2ram` -> `coco2` -> `coco`),
3. its CPU, then the CPUs that one falls back to,
4. `default`.

A library with no `[targets]` table is the same everywhere and gives only
`src/`.  A library that has the table and no key that matches does not
support the target, and `srcdirs` says so.

The first key decides alone.  A library with code for the C64 on top of
code for every 6502 writes both: `c64 = ["src/6502", "src/c64"]`.

A CPU falls back to one whose code it runs:

| CPU | then |
|---|---|
| `65816` | `65c02`, `6502` |
| `45gs02` | `65c02`, `6502` |
| `65c02` | `6502` |
| `6502`, `z80`, `6809`, `68000` | nothing |

The fallback is right for code that keeps to the documented instructions
and wrong for some that does not.  A 65C02 does not have the undocumented
instructions of the NMOS 6502, and a 65816 does not have the 65C02's
`RMB`, `SMB`, `BBR` and `BBS`.  The value `false` in place of a list says
that the library is not for that key, and ends the lookup there:

```toml
[targets]
6502  = ["src/6502"]
65c02 = false              # uses undocumented 6502 instructions
```

The 65816 and the 45GS02 come to `65c02` before `6502`, so this is "only
the 6502".  `65816 = false` beside a `65c02` entry leaves out only the
65816.  `false` works for any key: `c128 = false`.

`default` is for a library that needs a directory on one target and
nothing more on the others:

```toml
[targets]
cx16    = ["src/cx16"]
default = []
```

`llvm16`, `llvm32` and `llvm64` are the family `llvm` and have no CPU: a
library writes `llvm = [...]` for all three.  (`llvm16` will stand for a
6502, a Z80 or a 6809 in `prog8c-llvm`.  It has no `%asm` and no `asmsub`
today, so a library has nothing to tell the CPUs apart for.)

`p8lib` has a table of the known targets with their families and CPUs,
taken from p8p8's target files, which cover the most machines.  Two
options correct it for one call:

- `-as NAME` for a target it does not know (a custom target file): the
  known target to treat it as.
- `-cpu NAME` for a CPU other than the table's.  `p8c -cpu` builds a
  target for another CPU, and the compilers do not agree everywhere:
  `mega65` is a 45GS02 for `p8c` and a 6502 for the others.

## Where things are kept

| What | Where |
|---|---|
| the program's needs | `p8lib.toml`, in the program's repository |
| the resolved commits | `p8lib.lock`, beside it, committed |
| the clones | `~/.local/share/p8lib/` (`$XDG_DATA_HOME/p8lib`; `%LOCALAPPDATA%\p8lib` on Windows; `$P8LIB_HOME` in place of any of them), one directory per repository and commit |

The clones are shared between projects.  Two projects can use different
commits of one library, because the commit is part of the directory's
name.  A clone is made under a temporary name and renamed when it is
complete, so that a `srcdirs` running at the same time never sees half of
one.

`p8lib.lock` has one entry per library, the libraries of libraries
included: the name, the URL, what the manifest asked for and the commit.

## Commands

| Command | What it does |
|---|---|
| `p8lib install` | fetches everything `p8lib.lock` names; resolves and adds to the lock what the manifest names and the lock does not; takes out of the lock what the manifest no longer needs |
| `p8lib update [name]` | moves the lock to the newest commit the manifest allows |
| `p8lib list` | the libraries of the project, with commit, description and merges |
| `p8lib srcdirs -target T [-own]` | the directories, as above |
| `p8lib check` | the checks of "Names" on the library in the current directory, for its author |
| `p8lib init [name]` | writes a first `p8lib.toml` for the library in the current directory, for an author who brings an existing library to `p8lib`: the name from the one module in `src/` or from the directory, `[targets]` from the directories under `src/`.  It then says what `p8lib check` would refuse |

| `p8lib install owner/repo` | fetches the library, asks it its name, adds the line to `[dependencies]` of `p8lib.toml` (a new file when there is none), and installs.  A git URL in place of `owner/repo`, and `-tag`, `-branch` or `-rev`, as in the manifest |

`p8lib -version` prints the version of `p8lib`.

`p8lib install owner/repo` adds a line and changes nothing else in the
file, and writes it only when the program's libraries still resolve with
the new one.  The key it writes is the library's own name: `p8lib install
gillham/prog8malloc` adds `libmem = "gillham/prog8malloc"`, and `p8lib
install gillham/prog8malloc@v1.0` adds `libmem =
"gillham/prog8malloc@v1.0"`.  Everything
else is done by editing `p8lib.toml` and running `p8lib install`: a
library is removed by taking its line out.

Fetching is `git clone --depth 1` of the tag or branch.  For the commit
the lock names it is a fetch of that one commit, and a full clone when the
server refuses that.

## The first version

Everything above.  It is what a program with a hand-written manifest
needs to build with libraries from GitHub, on any of the four compilers.

`tests/test_p8lib.py` makes its libraries and git repositories on the
spot, uses no network, and gives `p8lib`'s answer to each compiler it
finds on the PATH:

```
python3 -m unittest discover -s tests
```

## Later

- **A catalog.**  A list that maps short names to repositories (`regex`
  -> `gillham/prog8regex`).  `p8lib install regex` would look the name up
  once and write the repository into the manifest, so that a build never
  depends on the catalog.  It decides what code is cloned, so it is
  fetched over https only.
- **More commands that edit the manifest.**  `p8lib remove name`, and
  `p8lib install regex` by a short name.  Taking a line out of a TOML
  file, or changing one, and keeping the comments is more than adding a
  line, and the standard library does not write TOML.
- **Naming libraries on the command line.**  `p8lib srcdirs -target c64
  regex strings2` with no manifest.  It needs an answer to which commit
  "the installed `regex`" is when the store holds several.
- **More of the program's own directories.**  `-own` gives `src/` and
  what `[targets]` adds to it.  A program with its sources somewhere else,
  or a library's `demo/`, is still written on the build line.
- **A compiler that reads the result.**  `p8lib` could write the resolved
  directories to a plain file, one per line, for each target.  A compiler
  could read that with no TOML parser, no git and no dependency logic, and
  every compiler could adopt the same file.
- **Cleaning the store.**  A command that removes clones no lock on this
  machine names.

## Open questions

- Two libraries that merge the same name.  The program can only drop one
  of them.  Should its manifest be able to say which one wins, and how
  would that reach the compiler, which goes by the order of imports?
- A conflict between libraries of libraries.  It is an error for now.
  With experience: does the program's own manifest decide, or the newest
  commit?
- The LLVM targets.  They are one family for now.  When `llvm16` has
  assembly for its CPUs, is the CPU given with `-cpu`, or is the triple
  (`mos-c64`, `wasm32-wasip1`) a key of `[targets]`?
- Compiler flags.  A library that needs options on the compiler's command
  line makes every build line depend on it; is there a case that needs
  it?
- Version ranges.  Tags and commits are enough to start with.  Ranges
  (`">=1.2"`) need a rule for what a version is.
- Whether the table of targets, families and CPUs lives in `p8lib` or is
  fetched from somewhere.
- Libraries that carry assembly are written for one assembler (64tass,
  lwasm, z88dk).  Keying on the target covers it as long as one target
  means one assembler in every compiler.
