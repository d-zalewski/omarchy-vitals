"""Tier 1 - sound.

Audio breaks in two distinct ways after a kernel change: the card stops being
detected (obvious), or it works but stutters under load (subtle, and the same
underlying problem as scheduler jitter). Both are covered here.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core import Fail, Info, Ok, Skip, Warn, check

ASOUND = Path("/proc/asound")


@check(tier=1, name="sound_card", desc="sound hardware detected")
def sound_card(ctx):
    if not ASOUND.exists():
        return Fail("no /proc/asound - ALSA not available in this kernel")
    cards_file = ASOUND / "cards"
    text = cards_file.read_text() if cards_file.exists() else ""
    cards = [l for l in text.splitlines() if re.match(r"\s*\d+\s+\[", l)]
    if not cards:
        return Fail("no sound cards detected", sound_cards=0)
    names = [re.sub(r"\s+", " ", c).strip()[:40] for c in cards]
    return Ok(f"{len(cards)} card(s): {'; '.join(names)}", sound_cards=len(cards))


@check(tier=1, name="sound_driver", desc="audio driver bound")
def sound_driver(ctx):
    out = ctx.run(["lspci", "-k"]).stdout
    drivers, cur = [], False
    for line in out.splitlines():
        if re.search(r"(Audio device|Multimedia audio)", line):
            cur = True
        elif cur and "Kernel driver in use:" in line:
            drivers.append(line.split(":", 1)[1].strip())
            cur = False
        elif line and not line.startswith("\t"):
            cur = False
    if not drivers:
        # USB audio has no PCI entry; fall back to module presence.
        mods = ctx.run(["lsmod"]).stdout
        if "snd_usb_audio" in mods:
            return Ok("USB audio driver loaded (snd_usb_audio)")
        return Warn("no PCI audio driver bound")
    return Ok(f"driver(s): {', '.join(sorted(set(drivers)))}")


@check(tier=1, name="pipewire", desc="audio server running")
def pipewire(ctx):
    running = []
    for proc in ("pipewire", "wireplumber", "pipewire-pulse"):
        if ctx.run(["pgrep", "-x", proc]).returncode == 0:
            running.append(proc)
    if not running:
        if ctx.run(["pgrep", "-x", "pulseaudio"]).returncode == 0:
            return Ok("pulseaudio running")
        return Warn("no audio server running (pipewire/pulseaudio)")
    missing = {"pipewire", "wireplumber"} - set(running)
    if missing:
        # wireplumber is the session manager; without it devices never appear.
        return Warn(f"running: {', '.join(running)} - missing {', '.join(missing)}")
    return Ok(f"running: {', '.join(running)}")


@check(tier=1, name="audio_sinks", desc="playback/capture devices available")
def audio_sinks(ctx):
    if not ctx.have("wpctl"):
        return Skip("wpctl not available (pipewire-utils)")
    r = ctx.run_in_session(["wpctl", "status"], timeout=30)
    if r.returncode != 0:
        return Warn(f"wpctl failed: {(r.stderr or r.stdout).strip()[:70]}")
    out = r.stdout
    sinks = srcs = 0
    section = None
    for line in out.splitlines():
        if "Sinks:" in line:
            section = "sink"
        elif "Sources:" in line:
            section = "source"
        elif re.match(r"\s*[├└│]?\s*\*?\s*\d+\.\s", line):
            if section == "sink":
                sinks += 1
            elif section == "source":
                srcs += 1
        elif line.strip().endswith(":") and "Filters" not in line:
            section = None
    if sinks == 0:
        return Fail("no audio output devices (sinks) - sound will not work",
                    audio_sinks=0, audio_sources=srcs)
    return Ok(f"{sinks} sink(s), {srcs} source(s)",
              audio_sinks=sinks, audio_sources=srcs)


@check(tier=1, name="audio_default_sink", desc="a default output is selected")
def audio_default_sink(ctx):
    if not ctx.have("wpctl"):
        return Skip("wpctl not available")
    r = ctx.run_in_session(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], timeout=20)
    if r.returncode != 0:
        return Warn("no default audio sink selected - nothing would play")
    vol = r.stdout.strip()
    muted = "MUTED" in vol.upper()
    if muted:
        return Info(f"default sink present but muted ({vol})")
    return Ok(f"default sink usable ({vol})")


@check(tier=1, name="audio_xruns", desc="buffer underruns (audible dropouts)")
def audio_xruns(ctx):
    n = ctx.count_matches(ctx.journal_all, r"xrun")
    if n == 0:
        return Ok("no xruns logged", audio_xruns=0)
    # xruns are jitter you can hear; correlate with tier 2 latency numbers.
    return Warn(f"{n} xrun mention(s) - audible dropouts, compare tier 2 latency",
                audio_xruns=n)


@check(tier=1, name="audio_playback", desc="actually plays sound to the device",
       requires=["speaker-test"], disruptive=True, est_seconds=6)
def audio_playback(ctx):
    """Push real audio through the stack.

    Marked disruptive because it makes an audible noise. It is the only check
    that proves the whole path works rather than merely enumerating devices.
    """
    r = ctx.run_in_session(
        ["speaker-test", "-t", "sine", "-f", "440", "-l", "1", "-s", "1"],
        timeout=30)
    out = r.stdout + r.stderr
    if r.returncode == 0 and ("Playback" in out or "sine" in out.lower()):
        return Ok("test tone played through default device")
    return Fail(f"playback failed: {out.strip().splitlines()[-1][:70] if out.strip() else 'no output'}")
