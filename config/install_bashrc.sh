#!/usr/bin/env bash
# Copyright (c) 2025 Ryan Lush <ryan.lush@gmail.com>
# Part of hailo-tracker. Free for personal, educational, and open-source use;
# commercial use requires written permission -- ryan.lush@gmail.com

#
# install_bashrc.sh — put bashrc.pi in place as ~/.bashrc.
#
#   ./config/install_bashrc.sh              show what would change
#   ./config/install_bashrc.sh --apply      install it, keeping a timestamped backup
#   ./config/install_bashrc.sh --file=X     install a specific file
#
# Why a versioned file rather than editing ~/.bashrc directly: ~/.bashrc is not
# in the repo, so every alias in it is one reflash away from being gone -- and a
# Pi that has been reimaged is exactly when you least want to be reconstructing
# your shell from memory. One file, in git, installed here.
#
# Your machine-specific bits belong in ~/.bash_aliases, which this never
# touches and bashrc.pi sources at the end.

# ── refuse to be sourced ──────────────────────────────────────────────────
# This script calls exit() on error. When a script is SOURCED, exit terminates
# the calling shell -- over ssh that drops the connection, which is a hard way
# to find out you typed `source` instead of `./`. It also breaks $0: sourced,
# $0 is "-bash", so `dirname "$0"` fails with "invalid option -- b".
#
# `(return 0 2>/dev/null)` succeeds only inside a sourced file, which is the
# portable way to detect it.
if (return 0 2>/dev/null); then
    printf 'This is a script, not something to source. Run it:\n    %s%s\n' \
        "" "${BASH_SOURCE[0]}" >&2
    return 1
fi

set -u

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m==> %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m==> %s\033[0m\n' "$*" >&2; exit 1; }

# ── refuse to run under sudo ──────────────────────────────────────────────
# This script writes $HOME/.bashrc and needs no privilege to do it. Under sudo,
# Ubuntu resets HOME to /root -- so it installs root's shell instead of yours,
# and the project layer then looks for the repo under /root, finds nothing, and
# says so only as "a new shell did not expose bhelp/hts". Catch it here, where
# the message can be specific, rather than there, where it cannot.
if [ "${EUID:-$(id -u)}" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
    warn "running under sudo: \$HOME is $HOME, so this would install ${SUDO_USER}'s"
    warn "shell into root's home. Nothing here needs root."
    die  "re-run as yourself:  ./config/install_bashrc.sh --apply"
fi

# Per-machine source files. The suffix names the target, so a Mac copy can sit
# beside the Pi's without either overwriting the other:
#
#     bashrc.pi     the Raspberry Pi
#     bashrc.mac    a Mac, if you add one later
#
# The twin of bashrc.pi is bashrc.bone in the balance_bot repo. They are the
# same file apart from the hardware section; fix one, fix the other.
#
# Picked automatically from the OS; override with --file.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
case "$(uname -s)" in
    Darwin) DEFAULT_SRC="$REPO/bashrc.mac" ;;
    *)      DEFAULT_SRC="$REPO/bashrc.pi" ;;
esac
# Fall back to whichever exists, so this still works on a machine with only one.
[ -f "$DEFAULT_SRC" ] || DEFAULT_SRC="$REPO/bashrc.pi"
[ -f "$DEFAULT_SRC" ] || DEFAULT_SRC="$REPO/bashrc.mac"

SRC="$DEFAULT_SRC"
DST="$HOME/.bashrc"
APPLY=0
for arg in "${@:-}"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        --file=*) SRC="${arg#--file=}" ;;
        "") ;;
        *) printf "unknown option: %s\n" "$arg" >&2; exit 1 ;;
    esac
done

[ -f "$SRC" ] || die "missing $SRC  (expected the machine's bashrc in $REPO)"
say "source: $SRC"

# A Pi-only file on something that is not a Pi will define aliases for hardware
# that is not there. Not fatal -- say so and carry on.
if [ "$(basename "$SRC")" = "bashrc.pi" ]; then
    if ! grep -qi 'raspberry pi' /proc/device-tree/model /proc/cpuinfo 2>/dev/null; then
        warn "this does not look like a Raspberry Pi -- the hardware section"
        warn "(vcgencmd, pinctrl, rpicam, hailortcli) will be dead weight here"
    fi
fi

# Never install something that will not parse -- a broken ~/.bashrc greets you
# with a syntax error on every new shell, including the one you would use to fix it.
bash -n "$SRC" || die "$SRC has a syntax error; refusing to install"
say "syntax check passed"

# ── collision check ──────────────────────────────────────────────────────
# bashrc.pi sources config/tracker_aliases.sh and then ~/.bash_aliases, in that
# order. Whatever is sourced LAST wins, silently -- which is exactly how the
# BeagleBone's shell ended up with an `sbot` that stopped one service while the
# file you would read to find out said it stopped three.
#
# So: list every name bashrc.pi defines, list every name the other files
# define, and report the overlap. Names, not contents, because a collision is a
# collision even when the two definitions happen to agree today.
names_in() {
    [ -f "$1" ] || return 0
    grep -hoE '^[[:space:]]*alias[[:space:]]+[A-Za-z0-9_.]+=' "$1" 2>/dev/null |
        sed 's/.*alias[[:space:]]*//; s/=$//'
    grep -hoE '^[A-Za-z0-9_]+\(\)' "$1" 2>/dev/null | sed 's/()//'
}

MINE="$(mktemp)"; THEIRS="$(mktemp)"
trap 'rm -f "$MINE" "$THEIRS"' EXIT
names_in "$SRC" | sort -u > "$MINE"

# The two project files must never define the same name. This is the rule that
# makes the split safe -- see the header of config/tracker_aliases.sh.
HT_ALIASES="$REPO/config/tracker_aliases.sh"
if [ -f "$HT_ALIASES" ]; then
    HTN="$(mktemp)"
    names_in "$HT_ALIASES" | sort -u > "$HTN"
    OVERLAP="$(comm -12 "$MINE" "$HTN" | tr '\n' ' ')"
    rm -f "$HTN"
    if [ -n "${OVERLAP// /}" ]; then
        die "$(basename "$SRC") and config/tracker_aliases.sh both define: $OVERLAP
       That is the exact bug the split exists to prevent -- one file wins
       silently and the other becomes a lie. Remove the duplicates and re-run."
    fi
    say "no overlap between $(basename "$SRC") and tracker_aliases.sh"
fi

LEGACY=""
for cand in "$HOME/.bash_aliases" "$REPO/config/aliases.sh"; do
    [ -f "$cand" ] || continue
    LEGACY="$LEGACY $cand"
    names_in "$cand" >> "$THEIRS"
done

if [ -n "$LEGACY" ]; then
    sort -u "$THEIRS" -o "$THEIRS"
    CLASH="$(comm -12 "$MINE" "$THEIRS" | tr '\n' ' ')"
    if [ -n "${CLASH// /}" ]; then
        warn "these names are defined BOTH in $(basename "$SRC") and elsewhere:"
        echo "      $CLASH"
        echo "  Defined in:$LEGACY"
        echo "  Those are sourced AFTER bashrc, so they win -- and bhelp will"
        echo "  describe the definition that did NOT take effect."
        echo
        echo "  Either delete the duplicates from those files, or move the whole"
        echo "  file aside if it is fully superseded:"
        for l in $LEGACY; do echo "      mv $l $l.superseded"; done
        echo
    else
        say "no name collisions with$LEGACY"
    fi
fi

if [ -f "$DST" ] && cmp -s "$SRC" "$DST"; then
    say "$DST is already identical — nothing to do"
    exit 0
fi

if [ -f "$DST" ]; then
    say "differences against the current $DST:"
    diff -u "$DST" "$SRC" | sed -n '1,60p' || true
    echo
    n=$(diff "$DST" "$SRC" | grep -c '^[<>]' || true)
    say "$n changed line(s) in total"
else
    say "no existing $DST — this will create it"
fi

if [ "$APPLY" -eq 0 ]; then
    echo
    say "dry run — nothing written. Re-run with --apply"
    exit 0
fi

if [ -f "$DST" ]; then
    BAK="$DST.bak-$(date +%Y%m%d-%H%M%S)"
    cp -p "$DST" "$BAK" || die "could not back up $DST"
    say "backed up to $BAK"
fi

cp "$SRC" "$DST" || die "could not write $DST"
say "installed $DST"

# The shell has to find config/tracker_aliases.sh to load the project layer,
# and $HOME/hailo-tracker is only a guess -- one that fails silently, leaving a
# shell with no service aliases and nothing saying why. Record the real path.
if mkdir -p "$HOME/.config/hailo-tracker" 2>/dev/null &&
   printf '%s\n' "$REPO" > "$HOME/.config/hailo-tracker/repo_path" 2>/dev/null; then
    say "recorded repo path: $REPO"
else
    warn "could not record the repo path under ~/.config/hailo-tracker;"
    warn "the shell will fall back to \$HOME/hailo-tracker"
fi

# Verify the installed copy in a real shell rather than trusting the copy.
# Two checks, not one: the shell half and the project half fail for completely
# different reasons, and a single combined check cannot tell you which.
if bash -ic 'declare -F bhelp >/dev/null' 2>/dev/null; then
    say "verified: bhelp loads in a new shell"
else
    warn "installed, but a new shell has no bhelp -- check for an early"
    warn "'return' or an error in $DST"
fi

if bash -ic 'alias hts >/dev/null 2>&1' 2>/dev/null; then
    say "verified: the project layer loaded"
else
    warn "installed, but the project aliases did not load."
    warn "  looked for: $HT_ALIASES"
    warn "  the shell resolves the repo as \$HT_DIR, then the path recorded in"
    warn "  ~/.config/hailo-tracker/repo_path, then \$HOME/hailo-tracker"
fi

echo
say "Run 'exec bash' or open a new terminal, then:"
echo "     bhelp        project commands, grouped"
echo "     ahelp        every alias and function"
