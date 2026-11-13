"""Completion of the reserve flags and of claude-as --enroll-native, in Bash and zsh.

Bash calls each completion function as bash-completion does and prints
COMPREPLY. zsh autoloads each completion file with stand-ins for _describe,
which print the tag, the group and every candidate before zsh's own matching
against the word, and for _default. A pty case drives the real zsh completion
system through compinit. The installed cases complete through the links
install.sh makes in a temporary HOME, with the real launchers.

Completion is offline: the launchers are asked only --list-accounts (bash) or
--account-labels (zsh), which read account metadata and never log in or query
quota, and fake codex and claude record any start. Every process gets a
minimal environment, so a caller's kit configuration, XDG directories and
shell start-up files never reach it.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ("codex-as", "claude-as")
FLAGS = ["--reserve-5h", "--reserve-weekly"]
# What a dash word in argument 1 offers: the own options, then (claude-as) native enrollment and the reserve flags.
OPTIONS = {
    "codex-as": ["--help", "--version", "--list", "--info", "--labels", "--show-config", *FLAGS],
    "claude-as": ["--help", "--version", "--list", "--info", "--labels", "--label", "--show-config",
                  "--enroll-native", *FLAGS],
}
FLAG_DESCRIPTIONS = {"--reserve-5h": "--reserve-5h:keep this percentage of the 5-hour limit",
                     "--reserve-weekly": "--reserve-weekly:keep this percentage of the weekly limit"}
DESCRIBED = {
    "codex-as": ["[options|codex-as options|sorted]", "--help:show this help", "--version:show the kit version",
                 "--list:list the account names", "--info:show the last-seen details of an account",
                 "--labels:list the accounts with their emails", "--show-config:show the configuration in effect",
                 *FLAG_DESCRIPTIONS.values()],
    "claude-as": ["[options|claude-as options|sorted]", "--help:show this help", "--version:show the kit version",
                  "--list:list the account names", "--info:show the last-seen details of an account",
                  "--labels:list the accounts with their labels", "--label:set or clear the label of an account",
                  "--show-config:show the configuration in effect",
                  "--enroll-native:enroll an account through native Claude login", *FLAG_DESCRIPTIONS.values()],
}
# Reserve values: off and every tenth percentage for an empty word, off and every percentage once one is typed.
SHORT = ["off", *(str(n) for n in range(0, 101, 10))]
FULL = ["off", *(str(n) for n in range(101))]
VALUES = "[reserve-values|percentage to keep, or off|unsorted]"
OFF = "off:disable the check of this window"
# Account names as both launchers list them (sorted), and the labels zsh describes them by.
ACCOUNTS = {"codex-as": ["A", "B", "alpha"], "claude-as": ["vault", "wide", "work"]}
LABELS = {"codex-as": ("A", "email", "alice@example.com"), "claude-as": ("work", "label", "Work: billing")}
DESCRIBED_ACCOUNTS = {"codex-as": ["[accounts|Codex accounts|sorted]", "A:alice@example.com", "B", "alpha"],
                      "claude-as": ["[accounts|Claude accounts|sorted]", "vault", "wide", "work:Work: billing"]}
# The first word after NAME: the fixed suggestions, which zsh describes without a tag.
FIRST_NATIVE = {"codex-as": ["exec", "resume", "fork", "--help", "--version"],
                "claude-as": ["--help", "--version", "--print", "--continue", "--resume"]}
DESCRIBED_FIRST_NATIVE = {
    "codex-as": ["[|Codex arguments|sorted]", "exec:execute a prompt", "resume:resume a shared session",
                 "fork:fork a session", "--help:show CLI help", "--version:show CLI version"],
    "claude-as": ["[|Claude arguments|sorted]", "--help:show CLI help", "--version:show CLI version",
                  "--print:print a response", "--continue:continue last session", "--resume:resume a session"],
}
NATIVE = {"codex-as": "codex", "claude-as": "claude"}

# Stand-ins zsh's completion system would provide: each prints what it was given.
ZSH_STANDINS = r'''
_describe() {
    local opt tag= order=sorted OPTIND=1
    while getopts oOt:12JVx opt; do
        case $opt in
            t) tag=$OPTARG ;;
            V) order=unsorted ;;
        esac
    done
    shift $((OPTIND - 1))
    print -r -- "[$tag|$1|$order]"
    print -rl -- "${(@P)2}"
}
_default() { print -r -- "[default]"; }
'''
# Native completion functions that report how they were called; bash also passes the command,
# the word being completed and the one before it.
NATIVE_STANDINS = {
    "bash": '_{0}() {{ local IFS="|"; COMPREPLY=("native:${{COMP_WORDS[*]}}:$COMP_CWORD:$*:$COMP_LINE:$COMP_POINT"); }}\n',
    "zsh": '_{0}() {{ print -r -- "native:${{(j:|:)words}}:$CURRENT"; }}\n',
}
LAUNCHER_STUB = r'''#!/bin/sh
# A launcher that answers the two metadata queries completion may use, and records every call.
printf '%s\n' "$*" >>"$LAUNCHER_LOG"
case ${0##*/}:$* in
    codex-as:--list-accounts) printf '%s\n' A B alpha ;;
    codex-as:--account-labels) printf 'A\talice@example.com\nB\nalpha\n' ;;
    claude-as:--list-accounts) printf '%s\n' vault wide work ;;
    claude-as:--account-labels) printf 'vault\nwide\nwork\tWork: billing\n' ;;
    *) exit 1 ;;
esac
'''
NATIVE_FAKE = '#!/bin/sh\nprintf \'%s %s\\n\' "${0##*/}" "$*" >>"$NATIVE_LOG"\n'


def write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def snapshot(top):
    """Every path below TOP: type, mode, link text and content hash, without following links."""
    result = {}
    for directory, names, files in os.walk(top):
        for name in (*names, *files):
            path = Path(directory, name)
            info = path.lstat()
            entry = [stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode)]
            if path.is_symlink():
                entry.append(os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                entry.append(hashlib.sha256(path.read_bytes()).hexdigest())
            result[str(path.relative_to(top))] = entry
    return result


def run(command, env, cwd, timeout=20):
    """Run COMMAND in a session of its own and end the whole session on a timeout."""
    with subprocess.Popen([str(part) for part in command], env=env, cwd=cwd, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def native_call(tool, words, cword=None):
    """What a bash native stand-in reports for WORDS after the kit words, the word at CWORD
    (default: the last one) being completed and the line written with single spaces."""
    words = [NATIVE[tool], *words]
    cword = len(words) - 1 if cword is None else cword
    line = " ".join(words)
    point = len(" ".join(words[:cword + 1]))
    return f"native:{'|'.join(words)}:{cword}:{NATIVE[tool]}|{words[cword]}|{words[cword - 1]}:{line}:{point}"




class Fixture:
    """A temporary HOME, fake natives that record any start and the helpers that complete in SHELL."""

    shell = None

    def setUp(self):
        self.addCleanup(os.umask, os.umask(0o022))
        self.tmp = tempfile.TemporaryDirectory(prefix="kit-reserve-completion-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir(mode=0o700)
        self.fakes = self.base / "fake bin"
        for name in NATIVE.values():
            write(self.fakes / name, NATIVE_FAKE, 0o700)
        self.natives = self.base / "natives.log"
        self.env = {"HOME": str(self.home), "LC_ALL": "C", "NATIVE_LOG": str(self.natives),
                    "PATH": os.pathsep.join((str(self.fakes), os.environ.get("PATH", "/usr/bin:/bin")))}

    def complete(self, tool, words, prelude="", cword=None):
        """What the completion of TOOL offers after TOOL WORDS, the word at index CWORD of WORDS
        (default: the last one) being completed with the cursor at its end."""
        cword = len(words) - 1 if cword is None else cword
        if self.shell == "bash":
            point = len(" ".join((tool, *words[:cword + 1])))
            script = (f'{prelude}source "$1"; shift; COMP_WORDS=("$@"); COMP_CWORD={cword + 1}; '
                      f'COMP_LINE="${{COMP_WORDS[*]}}"; COMP_POINT={point}; _{tool.replace("-", "_")}; '
                      '[ "${#COMPREPLY[@]}" -eq 0 ] || printf "%s\\n" "${COMPREPLY[@]}"')
            command = ["bash", "--noprofile", "--norc", "-c", script, "bash", self.bash_file(tool), tool, *words]
        else:
            script = (ZSH_STANDINS + prelude + 'fpath=("$1" $fpath); autoload -Uz "_$2"; '
                      f'words=("${{(@)argv[2,-1]}}"); CURRENT={cword + 2}; "_$2"')
            command = ["zsh", "-f", "-c", script, "zsh", self.zsh_dir(), tool, *words]
        return run(command, self.shell_env(), self.base)

    def assertOffers(self, tool, words, expected, prelude="", cword=None):
        result = self.complete(tool, words, prelude, cword)
        # A zsh completion function returns 1 when it offers nothing.
        code = 1 if self.shell == "zsh" and not expected else 0
        self.assertEqual((result.returncode, result.stdout.splitlines(), result.stderr), (code, expected, ""),
                         f"{self.shell} {tool} {words}")

    def assertOffline(self, listed=False):
        """No native CLI ran; subclasses add what their launchers show. LISTED: accounts were listed."""
        self.assertFalse(self.natives.exists(), "completion started a native CLI")


class Behaviour:
    """The completion cases; each concrete class runs them in one shell against its own files and launchers."""

    def test_argument_1_offers_the_reserve_flags_and_native_enrollment_after_the_own_options(self):
        for tool in TOOLS:
            with self.subTest(tool=tool):
                if self.shell == "bash":
                    self.assertOffers(tool, ["-"], OPTIONS[tool])
                    self.assertOffers(tool, ["--r"], FLAGS)
                    self.assertOffers(tool, ["--reserve-w"], ["--reserve-weekly"])
                    self.assertOffers(tool, ["--reserve-5h"], ["--reserve-5h"])
                    self.assertOffers(tool, ["--e"], ["--enroll-native"] if tool == "claude-as" else [])
                else:
                    # zsh matches the candidates against the word itself.
                    for word in ("-", "--", "--r", "--e"):
                        self.assertOffers(tool, [word], DESCRIBED[tool])
        self.assertOffline()

    def test_each_flag_offers_its_values_in_either_order(self):
        for tool in TOOLS:
            for words in (["--reserve-5h"], ["--reserve-weekly"], ["--reserve-5h", "10", "--reserve-weekly"],
                          ["--reserve-weekly", "off", "--reserve-5h"], ["--reserve-weekly", "12.5", "--reserve-5h"]):
                with self.subTest(tool=tool, words=words):
                    if self.shell == "bash":
                        self.assertOffers(tool, [*words, ""], SHORT)
                        self.assertOffers(tool, [*words, "1"], ["1", *(str(n) for n in range(10, 20)), "100"])
                        self.assertOffers(tool, [*words, "5"], ["5", *(str(n) for n in range(50, 60))])
                        self.assertOffers(tool, [*words, "o"], ["off"])
                        self.assertOffers(tool, [*words, "100"], ["100"])
                        for word in ("101", "x"):
                            self.assertOffers(tool, [*words, word], [])
                    else:
                        self.assertOffers(tool, [*words, ""], [VALUES, OFF, *SHORT[1:]])
                        self.assertOffers(tool, [*words, "1"], [VALUES, OFF, *FULL[1:]])
                    # A dash word is no value: a flag where the value goes would be taken for it.
                    self.assertOffers(tool, [*words, "-"], [])
        self.assertOffline()

    def test_account_names_and_the_other_flag_follow_the_flags(self):
        for tool in TOOLS:
            for words in (["--reserve-5h", "10"], ["--reserve-weekly", "off"],
                          ["--reserve-5h", "0", "--reserve-weekly", "100"], ["--reserve-weekly", "40", "--reserve-5h", "10"],
                          ["--reserve-5h", "off", "--reserve-weekly", "off"], ["--reserve-5h", "12.5"], ["--reserve-5h", "007"]):
                with self.subTest(tool=tool, words=words):
                    left = [flag for flag in FLAGS if flag not in words]
                    if self.shell == "bash":
                        self.assertOffers(tool, [*words, ""], ACCOUNTS[tool])
                        prefix, matches = ("a", ["alpha"]) if tool == "codex-as" else ("w", ["wide", "work"])
                        self.assertOffers(tool, [*words, prefix], matches)
                        self.assertOffers(tool, [*words, "-"], left)
                        self.assertOffers(tool, [*words, "--reserve-"], left)
                        # Own options and native enrollment count only as argument 1.
                        for word in ("--i", "--h", "--e"):
                            self.assertOffers(tool, [*words, word], [])
                    else:
                        self.assertOffers(tool, [*words, ""], DESCRIBED_ACCOUNTS[tool])
                        group = f"[options|{tool} options|sorted]"
                        self.assertOffers(tool, [*words, "-"], [group, *(FLAG_DESCRIPTIONS[f] for f in left)] if left else [])
        self.assertOffline(listed=True)

    def test_missing_or_invalid_values_duplicates_and_late_options_offer_nothing(self):
        cases = [
            # A value missing: the next word was taken for it.
            ["--reserve-5h", "--reserve-weekly", ""], ["--reserve-5h", "--reserve-weekly", "10", ""],
            ["--reserve-5h", "A", ""], ["--reserve-5h", "A", "exec", ""], ["--reserve-weekly", "", ""],
            # Values outside 0..100 or off, and bash's word break at = (there is no --reserve-5h=V form).
            ["--reserve-5h", "101", ""], ["--reserve-5h", "-5", ""], ["--reserve-5h", "OFF", ""],
            ["--reserve-5h", "1e1", ""], ["--reserve-5h", "=", ""], ["--reserve-5h", "=", "10", ""], ["--reserve-5h=10", ""],
            # Each flag once.
            ["--reserve-5h", "10", "--reserve-5h", ""], ["--reserve-5h", "10", "--reserve-5h", "20", ""],
            ["--reserve-5h", "10", "--reserve-weekly", "5", "--reserve-weekly", ""],
            # Own options and native enrollment only as argument 1, and nothing after an own option.
            ["--reserve-5h", "10", "--info", ""], ["--reserve-5h", "10", "--help", ""], ["--reserve-5h", "10", "-x", ""],
            ["--reserve-5h", "10", "--enroll-native", ""], ["--info", "--reserve-5h", ""],
            ["--list", "--reserve-5h", "10", ""],
        ]
        for tool in TOOLS:
            for words in cases:
                with self.subTest(tool=tool, words=words):
                    self.assertOffers(tool, words, [])
        self.assertOffline()

    def test_native_completion_gets_the_words_after_name(self):
        cases = [
            ("codex-as", ["--reserve-5h", "10", "A"], ["exec", ""]),
            ("codex-as", ["--reserve-weekly", "40", "--reserve-5h", "off", "alpha"], ["--model", "m", ""]),
            ("codex-as", ["--reserve-weekly", "0", "B"], ["exec", "--mo"]),
            ("claude-as", ["--reserve-5h", "10", "work"], ["--model", ""]),
            ("claude-as", ["--reserve-weekly", "100", "--reserve-5h", "5", "vault"], ["--model", "opus", "--per"]),
            # NAME may look like a value.
            ("codex-as", ["--reserve-5h", "10", "10"], ["exec", ""]),
            # Without flags, as before.
            ("codex-as", ["A"], ["exec", ""]),
            ("claude-as", ["work"], ["--model", ""]),
        ]
        for tool, kit, native in cases:
            with self.subTest(tool=tool, words=kit + native):
                if self.shell == "bash":
                    expected = native_call(tool, native)
                else:
                    expected = f"native:{'|'.join((NATIVE[tool], *native))}:{len(native) + 1}"
                self.assertOffers(tool, kit + native, [expected], NATIVE_STANDINS[self.shell].format(NATIVE[tool]))
        with self.subTest(cursor="in a word before the last one"):
            words = ["--reserve-5h", "10", "A", "exec", "--mo", "--x"]
            expected = (native_call("codex-as", words[3:], cword=2) if self.shell == "bash"
                        else "native:codex|exec|--mo|--x:3")
            self.assertOffers("codex-as", words, [expected], NATIVE_STANDINS[self.shell].format("codex"), cword=4)
        self.assertOffline()

    def test_the_first_word_after_name_keeps_its_fixed_suggestions(self):
        for tool in TOOLS:
            name = ACCOUNTS[tool][0]
            prelude = NATIVE_STANDINS[self.shell].format(NATIVE[tool])
            for kit in ([name], ["--reserve-5h", "10", name], ["--reserve-weekly", "off", "--reserve-5h", "20", name]):
                with self.subTest(tool=tool, words=kit):
                    if self.shell == "bash":
                        self.assertOffers(tool, [*kit, ""], FIRST_NATIVE[tool], prelude)
                        self.assertOffers(tool, [*kit, "--v"], ["--version"], prelude)
                    else:
                        self.assertOffers(tool, [*kit, ""], DESCRIBED_FIRST_NATIVE[tool], prelude)
        self.assertOffline()

    def test_native_completion_is_never_called_after_invalid_kit_words(self):
        cases = [
            ("codex-as", ["--reserve-5h", "10", "--info", "A", "x", ""]),
            ("codex-as", ["--reserve-5h", "--reserve-weekly", "A", "x", ""]),
            ("codex-as", ["--reserve-5h", "10", "--reserve-5h", "20", "A", "x", ""]),
            ("codex-as", ["--reserve-5h", "A", "exec", "x", ""]),
            ("codex-as", ["--reserve-5h", "10", "-x", "A", "x", ""]),
            ("codex-as", ["--reserve-5h", "200", "A", "x", ""]),
            ("claude-as", ["--enroll-native", "work", "x", ""]),
            ("claude-as", ["--enroll-native", "work", "x", "y", ""]),
            ("claude-as", ["--reserve-5h", "10", "--enroll-native", "work", "x", ""]),
            ("claude-as", ["--reserve-5h", "10", "--label", "work", "x", ""]),
        ]
        for tool, words in cases:
            with self.subTest(tool=tool, words=words):
                self.assertOffers(tool, words, [], NATIVE_STANDINS[self.shell].format(NATIVE[tool]))
        self.assertOffline()

    def test_enroll_native_completes_one_account_name(self):
        if self.shell == "bash":
            self.assertOffers("claude-as", ["--enroll-native", ""], ACCOUNTS["claude-as"])
            self.assertOffers("claude-as", ["--enroll-native", "wo"], ["work"])
        else:
            self.assertOffers("claude-as", ["--enroll-native", ""], DESCRIBED_ACCOUNTS["claude-as"])
        # Nothing after the name; codex-as has no native enrollment.
        self.assertOffers("claude-as", ["--enroll-native", "work", ""], [])
        self.assertOffers("claude-as", ["--enroll-native", "work", "-"], [])
        self.assertOffers("codex-as", ["--enroll-native", ""], [])
        self.assertOffline(listed=True)


class Repository(Fixture):
    """The checkout's completion files, with launchers that only answer the metadata queries and record each call."""

    def setUp(self):
        super().setUp()
        launchers = self.base / "launchers"
        for tool in TOOLS:
            write(launchers / tool, LAUNCHER_STUB, 0o700)
        self.env["PATH"] = os.pathsep.join((str(launchers), self.env["PATH"]))
        self.log = self.base / "launcher.log"

    def bash_file(self, tool):
        return ROOT / "completions/bash" / tool

    def zsh_dir(self):
        return ROOT / "completions/zsh"

    def shell_env(self):
        return self.env | {"LAUNCHER_LOG": str(self.log)}

    def assertOffline(self, listed=False):
        super().assertOffline(listed)
        # Bash asks --list-accounts and zsh --account-labels, the 0.6 spellings every release accepts;
        # both read account metadata only.
        query = "--list-accounts" if self.shell == "bash" else "--account-labels"
        calls = set(self.log.read_text().splitlines()) if self.log.exists() else set()
        self.assertEqual(calls, {query} if listed else calls & {query}, "completion asked a launcher something else")


class Installed(Fixture):
    """A copy of the checkout installed with install.sh into a temporary HOME that holds fabricated accounts."""

    def setUp(self):
        super().setUp()
        kit = self.base / "kit"
        for part in ("bin", "lib", "completions", "helpers", "docs"):
            shutil.copytree(ROOT / part, kit / part)
        shutil.copy2(ROOT / "install.sh", kit / "install.sh")
        secrets = self.home / ".secrets"
        secrets.mkdir(mode=0o700)
        for name in ACCOUNTS["codex-as"]:
            payload = base64.urlsafe_b64encode(json.dumps({"sub": "fixture-" + name}).encode()).decode().rstrip("=")
            write(secrets / f"codex-{name}.json", json.dumps({
                "auth_mode": "chatgpt", "OPENAI_API_KEY": None, "last_refresh": "2026-10-01T00:00:00Z",
                "tokens": {"id_token": f"fake.{payload}.fake", "access_token": "fabricated-access-" + name,
                           "refresh_token": "fabricated-refresh-" + name, "account_id": name}}))
        for name in ACCOUNTS["claude-as"]:
            write(secrets / f"claude-{name}", "sk-ant-oat01-" + "fabricated-completion-" * 3 + "\n")
        state = self.home / ".local/state/coding-agent-kit/accounts"
        state.mkdir(parents=True, mode=0o700)
        for tool, (name, field, label) in LABELS.items():
            write(state / f"{tool.removesuffix('-as')}-{name}.json", json.dumps({field: label}))
        result = run(["sh", kit / "install.sh"], self.env, self.base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.zsh_site = self.home / ".local/share/zsh/site-functions"
        self.assertIn(f'fpath=("{self.zsh_site}" $fpath)', result.stdout)
        self.env["PATH"] = os.pathsep.join((str(self.home / ".local/bin"), self.env["PATH"]))
        self.before = snapshot(self.home)

    def bash_file(self, tool):
        return self.home / ".local/share/bash-completion/completions" / tool

    def zsh_dir(self):
        return self.zsh_site

    def shell_env(self):
        return self.env

    def assertOffline(self, listed=False):
        super().assertOffline(listed)
        self.assertEqual(snapshot(self.home), self.before, "completion wrote below HOME")


@unittest.skipUnless(shutil.which("bash"), "bash not installed")
class RepositoryBash(Repository, Behaviour, unittest.TestCase):
    shell = "bash"


@unittest.skipUnless(shutil.which("zsh"), "zsh not installed")
class RepositoryZsh(Repository, Behaviour, unittest.TestCase):
    shell = "zsh"


@unittest.skipUnless(shutil.which("bash"), "bash not installed")
class InstalledBash(Installed, Behaviour, unittest.TestCase):
    """Every case through the links install.sh made and the real launchers."""

    shell = "bash"


@unittest.skipUnless(shutil.which("zsh"), "zsh not installed")
class InstalledZsh(Installed, Behaviour, unittest.TestCase):
    """Every case through the links install.sh made and the real launchers."""

    shell = "zsh"


# Drives an interactive zsh in a pty: compinit with the installed fpath, then for each argument the
# line and a TAB, then a sentinel command; prints the line as the TAB left it (echoed input and
# what completion inserted, then the sentinel command typed after it) and the lines of its listing.
ZPTY_DRIVER = r'''
zmodload zsh/zpty || exit 3
zpty z 'zsh -f -i'
zpty -r z out '*READY> ' || exit 4
zpty -w z "unset zle_bracketed_paste; fpath=(${(q)1} \$fpath); autoload -Uz compinit; compinit -u -D; LISTMAX=1000; COLUMNS=500; setopt list_rows_first; print -r -- IN''IT"
zpty -r z out '*INIT*READY> ' || exit 5
shift
for line; do
    zpty -w -n z "$line"$'\t'
    zpty -w z $'\C-u'"print -r -- DO''NE"
    zpty -r z out '*DONE*READY> ' || exit 6
    lines=("${(@f)${out//$'\r'/}}")
    print -rl -- "== $line" "-> $lines[1]"
    # The listing: the lines after the echoed input, up to the redrawn prompt or the sentinel.
    for entry in "${(@)lines[2,-1]}"; do
        [[ $entry == 'READY> '* || $entry == DONE ]] && break
        print -r -- "$entry"
    done
done
zpty -d z
'''


@unittest.skipUnless(shutil.which("zsh"), "zsh not installed")
class RealZshCompletion(Installed, unittest.TestCase):
    """The installed zsh completions under the real completion system, started with compinit."""

    shell = "zsh"

    maxDiff = None

    def test_real_completion_system_offers_values_accounts_and_flags(self):
        if run(["zsh", "-f", "-c", "zmodload zsh/zpty"], self.env, self.base).returncode:
            self.skipTest("zsh has no zpty module")
        driver = write(self.base / "driver.zsh", ZPTY_DRIVER)
        lines = ["codex-as --reserve-5h ", "codex-as --reserve-weekly 40 --reserve-5h 10 ", "claude-as --reserve-5h off ",
                 "claude-as --enroll-native ", "claude-as --", "codex-as --reserve-5h 10 --"]
        result = run(["zsh", "-f", driver, self.zsh_site, *lines],
                     self.env | {"TERM": "dumb", "PS1": "READY> "}, self.base, timeout=60)
        self.assertEqual((result.returncode, result.stderr), (0, ""), result.stdout)
        listings, echoes, current = {}, {}, None
        for line in result.stdout.splitlines():
            if line.startswith("== "):
                current = listings.setdefault(line[3:], [])
            elif line.startswith("-> "):
                echoes[next(reversed(listings))] = line[3:]
            else:
                # Candidates fill rows; WORD... -- DESCRIPTION rows describe theirs.
                current.extend(line.split(" -- ")[0].split())
        # Values in their own order; zsh orders accounts and options itself (described ones first).
        self.assertEqual(listings.pop("codex-as --reserve-5h "), SHORT)
        self.assertEqual({line: sorted(listing) for line, listing in listings.items()}, {
            "codex-as --reserve-weekly 40 --reserve-5h 10 ": ACCOUNTS["codex-as"],
            "claude-as --reserve-5h off ": ACCOUNTS["claude-as"],
            "claude-as --enroll-native ": ACCOUNTS["claude-as"],
            "claude-as --": sorted(OPTIONS["claude-as"]),
            # One flag left: completed in place, without a listing.
            "codex-as --reserve-5h 10 --": [],
        })
        self.assertTrue(echoes["codex-as --reserve-5h 10 --"].startswith("codex-as --reserve-5h 10 --reserve-weekly "),
                        echoes)
        self.assertOffline(listed=True)


if __name__ == "__main__":
    unittest.main()
