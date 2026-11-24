#!/usr/bin/env python3
"""Unit tests for meet-recorder's pure functions.

No audio and no faster-whisper import are involved. The CLI is loaded by path
because the script has no .py extension.
"""

import importlib.machinery
import importlib.util
import contextlib
import datetime
import errno
import html
import io
import json
import os
import random
import re
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REAL_POPEN = subprocess.Popen    # before any test patches it
CLI_PATH = os.path.join(HERE, "..", "meet-recorder")

_spec = importlib.util.spec_from_loader(
    "meet_recorder", importlib.machinery.SourceFileLoader("meet_recorder", CLI_PATH))
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)


class TestPrunePlan(unittest.TestCase):
    TODAY = datetime.date(2026, 11, 24)

    def test_prunes_past_boundary_keeps_inside(self):
        prune, keep = mr.prune_plan(["2026-09-10", "2026-10-11"],
                                    self.TODAY, 45)
        self.assertEqual(prune, ["2026-09-10"])
        self.assertEqual(keep, ["2026-10-11"])

    def test_exact_boundary_age_is_pruned(self):
        # 2026-10-10 is exactly 45 days before TODAY
        prune, keep = mr.prune_plan(["2026-10-10"], self.TODAY, 45)
        self.assertEqual(prune, ["2026-10-10"])

    def test_today_and_yesterday_kept(self):
        prune, keep = mr.prune_plan(["2026-11-24", "2026-11-23"],
                                    self.TODAY, 45)
        self.assertEqual(prune, [])
        self.assertEqual(keep, ["2026-11-23", "2026-11-24"])

    def test_unparseable_names_kept(self):
        prune, keep = mr.prune_plan(["not-a-date", "2026-13-01"],
                                    self.TODAY, 1)
        self.assertEqual(prune, [])
        self.assertEqual(keep, ["2026-13-01", "not-a-date"])

    def test_sorted_output(self):
        prune, _keep = mr.prune_plan(["2026-09-12", "2026-09-05"],
                                     self.TODAY, 1)
        self.assertEqual(prune, ["2026-09-05", "2026-09-12"])


class TestPruneCmd(unittest.TestCase):
    def _meeting(self, base, date, sub="104529-meeting"):
        folder = os.path.join(base, date, sub)
        os.makedirs(folder)
        with open(os.path.join(folder, "transcript.md"), "w") as f:
            f.write("# meeting\nwords\n")
        with open(os.path.join(folder, "transcript.txt"), "w") as f:
            f.write("[00:00 -> 00:05] words\n")
        with open(os.path.join(folder, "meeting.m4a"), "wb") as f:
            f.write(b"audio")
        with open(os.path.join(folder, "summary.md"), "w") as f:
            f.write("summary\n")
        return folder

    def _run_prune(self, base, args):
        cfg_path = os.path.join(os.path.dirname(base), "config.ini")
        with open(cfg_path, "w") as f:
            f.write(f"[paths]\noutput_dir = {base}\n")
        with mock.patch.object(mr, "CONFIG_FILE", cfg_path):
            return mr.cmd_prune(args)

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "Recordings")
            folder = self._meeting(base, "2026-09-01")
            rc = self._run_prune(base, ["--days", "45"])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isdir(folder))
            self.assertFalse(os.path.exists(os.path.join(base, ".archive")))

    def test_apply_archives_text_and_deletes_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "Recordings")
            folder = self._meeting(base, "2026-09-01")
            rc = self._run_prune(base, ["--days", "45", "--apply"])
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(folder))
            tar_path = os.path.join(base, ".archive",
                                    "2026-09-01_104529-meeting.tar.gz")
            self.assertTrue(os.path.isfile(tar_path))
            with tarfile.open(tar_path) as tar:
                self.assertEqual(sorted(tar.getnames()),
                                 ["summary.md", "transcript.md",
                                  "transcript.txt"])

    def test_apply_removes_empty_date_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "Recordings")
            self._meeting(base, "2026-09-01")
            self._run_prune(base, ["--days", "45", "--apply"])
            self.assertFalse(os.path.exists(os.path.join(base, "2026-09-01")))

    def test_unidentified_subdir_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "Recordings")
            self._meeting(base, "2026-09-01")
            mystery = os.path.join(base, "2026-09-01", "my-stuff")
            os.makedirs(mystery)
            with open(os.path.join(mystery, "note.txt"), "w") as f:
                f.write("keep me\n")
            rc = self._run_prune(base, ["--days", "45", "--apply"])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(os.path.join(mystery, "note.txt")))
            self.assertTrue(os.path.isdir(os.path.join(base, "2026-09-01")))

    def test_symlink_date_dir_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "Recordings")
            real = os.path.join(tmp, "real-2026-09-01")
            self._meeting(base, "2026-09-01")
            os.rename(os.path.join(base, "2026-09-01"), real)
            os.symlink(real, os.path.join(base, "2026-09-01"))
            rc = self._run_prune(base, ["--days", "45", "--apply"])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(os.path.join(
                real, "104529-meeting", "transcript.md")))

    def test_days_must_be_positive(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "Recordings")
            self._meeting(base, "2026-09-01")
            self.assertEqual(self._run_prune(base, ["--days", "0"]), 2)
            self.assertEqual(self._run_prune(base, ["--days", "x"]), 2)
            self.assertTrue(os.path.isdir(os.path.join(base, "2026-09-01")))

    def test_nothing_old_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "Recordings")
            folder = self._meeting(base, "2026-11-23")
            self.assertEqual(
                self._run_prune(base, ["--days", "45", "--apply"]), 0)
            self.assertTrue(os.path.isdir(folder))


class TestFmtTs(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(mr.fmt_ts(0), "00:00")

    def test_rounds_down(self):
        self.assertEqual(mr.fmt_ts(65.9), "01:05")

    def test_over_an_hour(self):
        self.assertEqual(mr.fmt_ts(3661), "61:01")


class TestFormatSegment(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(
            mr.format_segment(0, 5, "hello"), "[00:00 -> 00:05] hello")

    def test_strips_text(self):
        self.assertEqual(
            mr.format_segment(61, 125, "  padded  "),
            "[01:01 -> 02:05] padded")


class TestSessionDirName(unittest.TestCase):
    def test_shape_default_slug(self):
        name = mr.session_dir_name(1_700_000_000)
        self.assertRegex(name, r"^\d{6}-meeting$")

    def test_matches_localtime(self):
        ts = 1_700_000_000
        expected = time.strftime("%H%M%S", time.localtime(ts)) + "-meeting"
        self.assertEqual(mr.session_dir_name(ts), expected)

    def test_custom_slug(self):
        name = mr.session_dir_name(1_700_000_000, slug="standup")
        self.assertRegex(name, r"^\d{6}-standup$")


class TestConfig(unittest.TestCase):
    def test_missing_file_is_defaults(self):
        cfg = mr.load_config("/nonexistent/path/does/not/exist.ini")
        self.assertEqual(cfg.get("audio", "sys_volume"), "1.0")
        self.assertEqual(cfg.get("audio", "mic_volume"), "2.0")
        self.assertEqual(cfg.getint("audio", "sample_rate"), 16000)
        self.assertEqual(cfg.get("whisper", "model"), "small")
        self.assertEqual(cfg.getint("whisper", "beam_size"), 5)
        self.assertEqual(cfg.get("paths", "output_dir"), "~/Recordings")

    def test_partial_file_merges(self):
        with tempfile.NamedTemporaryFile(
                "w", suffix=".ini", delete=False) as f:
            f.write("[audio]\nmic_volume = 3.5\n[whisper]\nmodel = small\n")
            path = f.name
        try:
            cfg = mr.load_config(path)
            # Overridden values.
            self.assertEqual(cfg.get("audio", "mic_volume"), "3.5")
            self.assertEqual(cfg.get("whisper", "model"), "small")
            # Untouched values fall back to defaults.
            self.assertEqual(cfg.get("audio", "sys_volume"), "1.0")
            self.assertEqual(cfg.getint("audio", "sample_rate"), 16000)
            self.assertEqual(cfg.getint("whisper", "beam_size"), 5)
            self.assertEqual(cfg.get("paths", "output_dir"), "~/Recordings")
        finally:
            os.remove(path)


class TestFfmpegMixCmd(unittest.TestCase):
    def test_exact_argv(self):
        cmd = mr.build_ffmpeg_mix_cmd(
            "sys.wav", "mic.wav", "meeting.m4a",
            sys_volume=1.0, mic_volume=2.0, sample_rate=16000)
        self.assertEqual(cmd, [
            "ffmpeg", "-y",
            "-i", "sys.wav",
            "-i", "mic.wav",
            "-filter_complex",
            "[0:a]volume=1.0[a];[1:a]volume=2.0[b];"
            "[a][b]amix=inputs=2:duration=longest:dropout_transition=2,"
            "aresample=16000",
            "-c:a", "aac", "-b:a", "96k",
            "meeting.m4a",
        ])

    def test_config_values_flow_through(self):
        cmd = mr.build_ffmpeg_mix_cmd(
            "/a/sys.wav", "/a/mic.wav", "/a/meeting.m4a",
            sys_volume=0.8, mic_volume=1.5, sample_rate=48000)
        self.assertIn(
            "[0:a]volume=0.8[a];[1:a]volume=1.5[b];"
            "[a][b]amix=inputs=2:duration=longest:dropout_transition=2,"
            "aresample=48000",
            cmd)


class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(mr.slugify("Weekly Sync: Ops & Sales!"), "weekly-sync-ops-sales")

    def test_empty_and_length(self):
        self.assertEqual(mr.slugify("***"), "")
        self.assertLessEqual(len(mr.slugify("a b " * 50)), 40)
        self.assertFalse(mr.slugify("a b " * 50).endswith("-"))


class TestCallStreams(unittest.TestCase):
    def so(self, **props):
        return {"properties": props}

    def test_matches_teams_binary_or_name(self):
        outs = [self.so(**{"application.process.binary": "teams-for-linux",
                           "application.name": "Chromium input"}),
                self.so(**{"application.name": "Teams for Linux"}),
                self.so(**{"application.process.binary": "chromium"})]
        self.assertEqual(len(mr.call_streams(outs, ["teams"])), 2)

    def test_ignores_own_recorder_and_others(self):
        outs = [self.so(**{"application.name": "pw-record",
                           "node.name": "pw-record teams"}),
                self.so(**{"application.process.binary": "superwhisper"})]
        self.assertEqual(mr.call_streams(outs, ["teams"]), [])

    def test_empty(self):
        self.assertEqual(mr.call_streams([], ["teams"]), [])
        self.assertEqual(mr.call_streams(None, ["teams"]), [])


class TestPickMeetingTitle(unittest.TestCase):
    def test_prefers_meeting_like(self):
        t = ["Chat | Tanner | Microsoft Teams",
             "Meeting with Tanner | Microsoft Teams"]
        self.assertEqual(mr.pick_meeting_title(t), "Meeting with Tanner")

    def test_skips_generic_tabs(self):
        self.assertEqual(mr.pick_meeting_title(["Chat | X | Microsoft Teams"]), "")
        self.assertEqual(mr.pick_meeting_title(["Q4 Budget Review | Microsoft Teams"]),
                         "Q4 Budget Review")
        self.assertEqual(mr.pick_meeting_title([]), "")


class TestSpeakers(unittest.TestCase):
    def test_format_with_speaker(self):
        self.assertEqual(mr.format_segment(0, 5, " hi ", "Me"), "[00:00 -> 00:05] Me: hi")

    def test_merge_orders_and_labels(self):
        out = mr.merge_speakers([(5, 7, "Sounds good to me")],
                                [(0, 4, "Can we ship on Friday?"), (8, 9, "Great")])
        self.assertEqual([s[3] for s in out], ["Others", "Me", "Others"])

    def test_drops_speaker_echo(self):
        others = [(10, 14, "The quarterly numbers look strong this year")]
        me = [(10.5, 14.5, "the quarterly numbers look strong this here"),
              (15, 17, "I disagree about the forecast")]
        out = mr.merge_speakers(me, others)
        self.assertEqual([(s[3], s[2]) for s in out if s[3] == "Me"],
                         [("Me", "I disagree about the forecast")])

    def test_same_words_far_apart_are_kept(self):
        out = mr.merge_speakers([(100, 102, "yes that works")],
                                [(0, 2, "yes that works")])
        self.assertEqual(len(out), 2)


class TestAppDevice(unittest.TestCase):
    DEVS = [{"index": 1, "name": "hdmi"}, {"index": 2, "name": "headset"}]

    def stream(self, sink):
        return {"sink": sink, "properties": {"application.process.binary": "teams-for-linux"}}

    def test_follows_default_when_app_uses_it(self):
        self.assertEqual(mr.app_device([self.stream(1)], self.DEVS, ["teams"], "sink", "hdmi"), "")

    def test_pins_when_app_uses_other_device(self):
        self.assertEqual(mr.app_device([self.stream(2)], self.DEVS, ["teams"], "sink", "hdmi"), "headset")

    def test_no_app_stream(self):
        self.assertEqual(mr.app_device([], self.DEVS, ["teams"], "sink", "hdmi"), "")


class TestMicPlan(unittest.TestCase):
    """plan_mic / is_real_mic (fix round 7: AEC only when provably safe)."""
    MIC = {"name": "alsa_input.webcam", "monitor_of_sink": "n/a",
           "properties": {"device.class": "sound"}}
    HS = {"name": "bluez_input.hs", "properties": {}}
    MON = {"name": "alsa_output.spk.monitor", "monitor_of_sink": "alsa_output.spk",
           "properties": {"device.class": "monitor"}}
    OLD_AEC = {"name": "echo-cancel-source",
               "properties": {"node.link-group": "echo-cancel-7"}}
    SOURCES = [MIC, HS, MON, OLD_AEC]

    def test_setting_normalised(self):
        for v, want in (("", "raw"), (None, "raw"), ("  AUTO ", "auto"),
                        ("Raw", "raw"), (" my.Node ", "my.Node")):
            self.assertEqual(mr.mic_source_setting(v), want)

    def test_is_real_mic(self):
        self.assertTrue(mr.is_real_mic(self.MIC))
        self.assertTrue(mr.is_real_mic(self.HS))
        for s in (self.MON, self.OLD_AEC, {"name": ""}, {},
                  {"name": "x.monitor"}, {"name": "meet-recorder.aec-mic.1"},
                  {"name": "y", "monitor_of_sink": "spk"}):
            self.assertFalse(mr.is_real_mic(s), s)

    def test_auto_real_default_mic_adds_aec_raw_unchanged(self):
        # fix round 8: the raw recorder is exactly the pre-AEC one (follows
        # the default like the call app); AEC is only an extra track
        aec, target, note = mr.plan_mic("auto", self.SOURCES,
                                        "alsa_input.webcam")
        self.assertEqual((aec, target), (True, ""))
        self.assertIn("echo-cancelled alsa_input.webcam", note)

    def test_auto_pinned_mic_aec_on_that_mic(self):
        # the canceller captures the same mic the call uses
        self.assertEqual(mr.plan_mic("auto", self.SOURCES, "bluez_input.hs",
                                     pin_mic="bluez_input.hs")[:2],
                         (True, "bluez_input.hs"))

    def test_auto_skips_aec_when_call_plays_to_non_default_sink(self):
        for pin_mic in ("", "bluez_input.hs"):
            aec, target, note = mr.plan_mic("auto", self.SOURCES,
                                            pin_mic or "alsa_input.webcam",
                                            pin_sink="bluez_output.hs",
                                            pin_mic=pin_mic)
            self.assertEqual((aec, target), (False, pin_mic))
            self.assertIn("non-default sink", note)

    def test_auto_skips_aec_without_a_real_mic(self):
        for mic in ("alsa_output.spk.monitor", "echo-cancel-source", "gone",
                    ""):
            aec, target, note = mr.plan_mic("auto", self.SOURCES, mic)
            self.assertEqual((aec, target), (False, ""), mic)
            self.assertTrue(note.startswith("raw mic"))
        self.assertFalse(mr.plan_mic("auto", None, "alsa_input.webcam")[0])

    def test_raw_and_explicit(self):
        self.assertEqual(mr.plan_mic("raw", self.SOURCES, "alsa_input.webcam")[:2],
                         (False, ""))
        self.assertEqual(mr.plan_mic("raw", self.SOURCES, "bluez_input.hs",
                                     pin_mic="bluez_input.hs")[:2],
                         (False, "bluez_input.hs"))
        self.assertEqual(mr.plan_mic("bluez_input.hs", self.SOURCES, "x")[:2],
                         (False, "bluez_input.hs"))
        # missing -> previous behaviour, never a failure
        self.assertEqual(mr.plan_mic("gone", self.SOURCES, "x", pin_mic="hs")[:2],
                         (False, "hs"))


class TestAecConf(unittest.TestCase):
    """The generated per-recording config keeps the safety rules."""
    NAMES = mr.aec_names("1795530225-abc123")

    def conf(self, mic="alsa_input.webcam"):
        return re.sub(r"#.*", "", mr.aec_conf_text(self.NAMES, mic))

    def module_block(self, text):
        return text[text.index("libpipewire-module-echo-cancel"):]

    def test_names_are_per_session(self):
        self.assertEqual(self.NAMES["mic"],
                         "meet-recorder.aec-mic.1795530225-abc123")
        self.assertEqual(len(set(self.NAMES.values())), 4)

    def test_module_never_mandatory(self):
        # conf.c load_module(): without nofail a failed module returns
        # -errno and the process does not start.
        block = self.module_block(self.conf())
        self.assertIn("flags = [ ifexists nofail ]", block)

    def test_output_is_private(self):
        text = self.conf()
        src = text[text.index("source.props"):]
        self.assertIn(f'media.class = "{mr.AEC_MEDIA_CLASS}"', src)
        self.assertIn("node.autoconnect = false", src)
        self.assertIn(f'node.name = "{self.NAMES["mic"]}"', src)
        # pipewire-pulse lists exactly these classes; WirePlumber defaults
        # and fallback targets use Audio/* device classes only.
        for listed in ("Audio/Source", "Audio/Duplex", "Audio/Source/Virtual",
                       "Stream/Output/Audio"):
            self.assertNotEqual(mr.AEC_MEDIA_CLASS, listed)
        self.assertTrue(mr.AEC_MEDIA_CLASS.startswith("Stream/"))
        self.assertNotIn("filter.smart", text)
        # a client, never a second daemon/server
        self.assertNotIn("core.daemon", text)

    def test_capture_pins_the_raw_mic_and_quotes_it(self):
        text = self.conf('we"ird\\mic')
        cap = text[text.index("capture.props"):text.index("sink.props")]
        self.assertIn('target.object = "we\\"ird\\\\mic"', cap)
        self.assertIn("node.passive = false", cap)
        ref = text[text.index("sink.props"):text.index("source.props")]
        self.assertNotIn("target.object", ref)   # follows the default sink

    def test_balanced(self):
        text = self.conf()
        self.assertEqual(text.count("{"), text.count("}"))
        self.assertEqual(text.count("["), text.count("]"))
        self.assertIn("monitor.mode = true", text)
        self.assertIn("library.name = aec/libspa-aec-webrtc", text)

    def test_env_scrubbed(self):
        env = mr.aec_env({"PATH": "/bin", "PIPEWIRE_CONFIG_NAME": "x.conf",
                          "PIPEWIRE_AUTOCONNECT": "1",
                          "PIPEWIRE_CONFIG_PREFIX": "p",
                          # applied after the configured stream props
                          # (Codex round 7): could unhide our output
                          "PIPEWIRE_PROPS": '{ media.class = Audio/Source }',
                          "PIPEWIRE_NODE": "x", "PIPEWIRE_LATENCY": "64/48000",
                          "PIPEWIRE_REMOTE": "pipewire-0",
                          "XDG_RUNTIME_DIR": "/run/user/1000"})
        self.assertEqual(env, {"PATH": "/bin", "PIPEWIRE_REMOTE": "pipewire-0",
                               "XDG_RUNTIME_DIR": "/run/user/1000"})

    def test_no_daemon_drop_in_shipped(self):
        # The daemon never loads the canceller (fix round 7).
        self.assertFalse(Path(HERE, "..", "pipewire",
                              "50-meet-recorder-aec.conf").exists())
        inst = Path(HERE, "..", "install-local.sh").read_text()
        self.assertNotIn("install -D -m 644 pipewire/", inst)
        self.assertIn("rm -f ~/.config/pipewire/pipewire.conf.d/"
                      "50-meet-recorder-aec.conf", inst)


class TestAecGraphState(unittest.TestCase):
    NAMES = mr.aec_names("s1")

    def node(self, nid, name, cls):
        return {"id": nid, "type": "PipeWire:Interface:Node",
                "info": {"props": {"node.name": name, "media.class": cls}}}

    def link(self, out, inp):
        return {"id": 900 + out + inp, "type": "PipeWire:Interface:Link",
                "info": {"output-node-id": out, "input-node-id": inp}}

    def dump(self, cls=None, links=(), rec=True):
        d = [self.node(10, self.NAMES["mic"], cls or mr.AEC_MEDIA_CLASS),
             self.node(30, "alsa_input.webcam", "Audio/Source")]
        if rec:
            d.append(self.node(20, self.NAMES["recorder"], "Stream/Input/Audio"))
        return d + [self.link(o, i) for o, i in links] + ["junk", None]

    def test_states(self):
        st = lambda d: mr.aec_graph_state(d, self.NAMES)
        self.assertEqual(st([]), "missing")
        self.assertEqual(st(None), "missing")
        self.assertEqual(st(self.dump(rec=False)), "ready")
        self.assertEqual(st(self.dump()), "ready")
        self.assertEqual(st(self.dump(links=[(10, 20)])), "linked")
        # not private (module ignored our media.class) -> never used
        self.assertEqual(st(self.dump(cls="Audio/Source")), "unsafe")
        # linked to anything but our recorder -> never used
        self.assertEqual(st(self.dump(links=[(10, 30)], rec=False)), "unsafe")
        self.assertEqual(st(self.dump(links=[(10, 20), (10, 30)])), "unsafe")
        # links INTO it (its capture side) are fine
        self.assertEqual(st(self.dump(links=[(30, 10), (10, 20)])), "linked")


class TestPickMicTrack(unittest.TestCase):
    """Fix round 8: AEC starts after the primaries (aec_delay); it is used
    only when complete and it kept the user's voice."""

    def prof(self, seconds=600, rms=None, level=900):
        n = int(seconds)
        r = [level] * n if rms is None else rms
        return {"seconds": float(seconds), "peaks": list(r), "rms": list(r)}

    def test_complete_aec_wins_with_raw_head(self):
        aec, raw = self.prof(598.8), self.prof(600)
        choice, offset, _ = mr.pick_mic_track(aec, raw, delay=1.1)
        self.assertEqual(choice, "aec")
        self.assertAlmostEqual(offset, 1.2)       # measured, within tolerance
        # quiet AEC while the far side talks (echo removed) is fine: those
        # seconds are not the user's own voice
        sys_ = self.prof(600, [3000] * 500 + [0] * 100)
        aec = self.prof(600, [12] * 500 + [900] * 100)
        self.assertEqual(mr.pick_mic_track(aec, raw, sys=sys_)[0], "aec")
        # an AEC track that never carried any voice proves nothing -> raw
        self.assertEqual(mr.pick_mic_track(self.prof(600, level=12), raw,
                                           sys=sys_)[0], "raw")

    def test_missing_tracks(self):
        self.assertEqual(mr.pick_mic_track(None, self.prof())[0], "raw")
        self.assertEqual(mr.pick_mic_track(None, None)[0], "raw")
        self.assertEqual(mr.pick_mic_track(self.prof(), None)[0], "raw")

    def test_unknown_format_is_raw(self):
        self.assertEqual(mr.pick_mic_track(self.prof() | {"rms": None},
                                           self.prof())[0], "raw")
        self.assertEqual(mr.pick_mic_track(self.prof(),
                                           self.prof() | {"rms": None})[0],
                         "raw")

    def test_length_must_match_the_delay(self):
        raw = self.prof(600)
        # expected 599.0 s (delay 1.0), got 598.4: a 0.6 s gap
        self.assertIn("gap", mr.pick_mic_track(self.prof(598.4), raw, 1.0)[2])
        self.assertEqual(mr.pick_mic_track(self.prof(598.6), raw, 1.0)[0], "aec")
        self.assertEqual(mr.pick_mic_track(self.prof(360), raw)[0], "raw")
        # longer than expected: unexplained -> raw
        choice, _, why = mr.pick_mic_track(self.prof(600), raw, 2.0)
        self.assertEqual(choice, "raw")
        self.assertIn("longer", why)
        self.assertEqual(mr.pick_mic_track(self.prof(601), raw, 0.0)[0], "raw")

    def test_implausible_delay_is_raw(self):
        choice, _, why = mr.pick_mic_track(self.prof(590), self.prof(600), 10.0)
        self.assertEqual(choice, "raw")
        self.assertIn("late", why)

    def test_wholly_silent_aec_with_raw_speech_is_raw(self):
        # Codex round 7: a minute of digital silence against 4 s of speech
        # was accepted. Any speech-level raw signal rejects a silent AEC.
        raw = [20] * 60
        raw[30:34] = [900] * 4
        self.assertEqual(mr.pick_mic_track(self.prof(60, level=0),
                                           self.prof(60, raw))[0], "raw")
        self.assertEqual(mr.pick_mic_track(self.prof(level=2), self.prof())[0],
                         "raw")
        # both silent: nobody spoke, nothing lost
        self.assertEqual(mr.pick_mic_track(self.prof(level=0),
                                           self.prof(level=0))[0], "aec")

    def test_lost_voice_in_over_20_percent_of_speech_windows(self):
        raw = self.prof(100)                # 10 speech windows, far side quiet
        aec = [900] * 100
        aec[0:20] = [5] * 20                # 2 of 10 windows lost: allowed
        self.assertEqual(mr.pick_mic_track(self.prof(100, aec), raw)[0], "aec")
        aec[20:30] = [5] * 10               # 3 of 10: > 20 % -> raw
        choice, _, why = mr.pick_mic_track(self.prof(100, aec), raw)
        self.assertEqual(choice, "raw")
        self.assertIn("3 of 10", why)
        # 20 dB under the raw mic counts as near-silent too
        aec = [900] * 100
        aec[0:30] = [80] * 30
        self.assertEqual(mr.pick_mic_track(self.prof(100, aec), raw)[0], "raw")

    def test_echo_only_windows_are_not_evidence(self):
        # raw at speech level only while the far side talks (echo): AEC is
        # right to remove it
        raw = self.prof(100)
        sys_ = self.prof(100, level=3000)
        aec = self.prof(100, [900] * 10 + [10] * 90)
        self.assertEqual(mr.pick_mic_track(aec, raw, sys=sys_)[0], "aec")
        # without the speaker track the same windows count -> raw
        self.assertEqual(mr.pick_mic_track(aec, raw)[0], "raw")

    def test_voice_check_aligned_by_offset(self):
        # AEC started 3 s late: AEC second i - 3 is raw second i; the first
        # 3 raw seconds come from the raw track itself
        raw = [900] * 60
        self.assertIsNone(mr.aec_voice_error([900] * 57, raw, None, 3))
        self.assertIsNotNone(mr.aec_voice_error(
            [5] * 30 + [900] * 27, raw, None, 3))
        self.assertEqual(mr.pick_mic_track(
            {"seconds": 57.0, "rms": [900] * 57, "peaks": []},
            {"seconds": 60.0, "rms": raw, "peaks": []}, 3.0)[:2], ("aec", 3.0))


class TestAecRoute(unittest.TestCase):
    """aec_route_error: AEC invalid once the call's mic or sink diverged."""
    SINKS = [{"index": 1, "name": "spk"}, {"index": 2, "name": "bt.sink"}]
    SOURCES = [{"index": 5, "name": "webcam"}, {"index": 6, "name": "bt.mic"}]

    def teams(self, key, idx):
        return [{key: idx, "properties": {"application.name": "Teams"}}]

    def err(self, mic="webcam", follows=True, dsink="spk", dsrc="webcam",
            sink_idx=None, src_idx=None):
        si = self.teams("sink", sink_idx) if sink_idx else []
        so = self.teams("source", src_idx) if src_idx else []
        return mr.aec_route_error(mic, follows, dsink, dsrc, si, self.SINKS,
                                  so, self.SOURCES, ["teams"])

    def test_same_route_is_fine(self):
        self.assertIsNone(self.err(sink_idx=1, src_idx=5))
        self.assertIsNone(self.err())                     # no call streams

    def test_default_mic_switch_invalidates(self):
        # Codex round 7: Teams + raw follow a new default mic, the
        # canceller (node.link-group) stays on the old one
        self.assertIn("call mic is now bt.mic",
                      self.err(dsrc="bt.mic", src_idx=6))
        self.assertIn("bt.mic", self.err(dsrc="bt.mic"))   # call over, default moved

    def test_call_app_on_another_sink_invalidates(self):
        self.assertIn("bt.sink", self.err(sink_idx=2))

    def test_pinned_mic_without_streams_is_not_judged(self):
        self.assertIsNone(self.err(mic="bt.mic", follows=False))
        self.assertIsNone(self.err(mic="bt.mic", follows=False, src_idx=6))
        self.assertIsNotNone(self.err(mic="bt.mic", follows=False, src_idx=5))

    def test_unreadable_defaults_invalidate(self):
        self.assertIsNotNone(self.err(dsink=""))
        self.assertIsNotNone(self.err(dsrc=""))


class TestWavStatsAndChoice(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.d)

    def wav(self, name, samples, rate=16000):
        import array
        import wave
        path = os.path.join(self.d, name)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            a = array.array("h", samples)
            if sys.byteorder == "big":
                a.byteswap()
            w.writeframes(a.tobytes())
        return path

    def test_stats_and_profile(self):
        p = self.wav("a.wav", [0, 5, -300, 7] * 4000 + [0, 9] * 4000)
        self.assertEqual(mr.wav_stats(p), {"seconds": 1.5, "peak": 300})
        prof = mr.wav_profile(p)
        self.assertEqual(prof["peaks"], [300, 9])
        self.assertAlmostEqual(prof["rms"][0],
                               ((25 + 90000 + 49) / 4) ** 0.5, places=6)
        self.assertAlmostEqual(prof["rms"][1], (81 / 2) ** 0.5, places=6)
        self.assertIsNone(mr.wav_stats(os.path.join(self.d, "missing.wav")))
        self.assertIsNone(mr.wav_stats(self.wav("tiny.wav", [1] * 10)))
        junk = os.path.join(self.d, "junk.wav")
        Path(junk).write_bytes(b"x" * 4096)
        self.assertIsNone(mr.wav_profile(junk))

    def test_splice_puts_raw_head_before_aec(self):
        raw = self.wav("raw.wav", [1] * 16000 + [2] * 16000)
        aec = self.wav("aec.wav", [7] * 8000)
        out = mr.splice_mic(raw, aec, 0.25, os.path.join(self.d, "out.wav"))
        import wave
        with wave.open(out) as w:
            self.assertEqual((w.getframerate(), w.getnframes()), (16000, 12000))
            data = w.readframes(12000)
        self.assertEqual(data[:2], b"\x01\x00")
        self.assertEqual(data[7998:8002], b"\x01\x00\x07\x00")
        other = self.wav("other.wav", [7] * 8000, rate=8000)
        with self.assertRaises(ValueError):
            mr.splice_mic(raw, other, 0.25, os.path.join(self.d, "x.wav"))

    def test_choose_mic_wav(self):
        live = self.wav("mic.wav", [0, 1000, -1000, 3] * 60000)    # 15 s
        silent = self.wav("mic-aec.wav", [0] * 240000)
        state = {"mic_wav": live, "mic_aec_wav": silent, "session_dir": self.d,
                 "mic_echo_cancel": True}
        self.assertEqual(mr.choose_mic_wav(state), (live, False))
        good = self.wav("aec2.wav", [0, 900, -900, 3] * 60000)
        wav, echo = mr.choose_mic_wav(dict(state, mic_aec_wav=good))
        self.assertEqual((os.path.basename(wav), echo),
                         ("mic-aec-aligned.wav", True))
        # a diverged route never uses AEC, however good the track
        wav, echo, why = mr.mic_choice(dict(state, mic_aec_wav=good,
                                            aec_diverged="call mic is now x"))
        self.assertEqual((wav, echo), (live, False))
        self.assertIn("route changed", why)
        # no AEC / sessions from before this feature
        self.assertEqual(mr.choose_mic_wav({"mic_wav": silent}),
                         (silent, False))
        # fix-round-7 layout (AEC in mic_wav, raw in mic_raw_wav): raw
        self.assertEqual(mr.choose_mic_wav({"mic_wav": good,
                                            "mic_raw_wav": live,
                                            "mic_echo_cancel": True}),
                         (live, False))

    def finalize(self, aec_samples, delay=0.0, raw_samples=None):
        sess = os.path.join(self.d, "raw")
        os.mkdir(sess)
        noise = [0, 900, -900, 40] * 8000                 # 2 s
        Path(sess, "aec.conf").write_text("x")
        Path(sess, "aec.log").write_text("")
        state = {"start": 1795530225, "source": "teams", "title": "",
                 "session_dir": sess,
                 "sys_wav": self.wav("raw/sys.wav", noise),
                 "mic_wav": self.wav("raw/mic.wav", raw_samples or noise),
                 "mic_echo_cancel": True, "aec_delay": delay}
        if aec_samples is not None:
            state["mic_aec_wav"] = self.wav("raw/mic-aec.wav", aec_samples)
        cfg = mr.load_config(os.devnull)
        cfg.set("paths", "output_dir", os.path.join(self.d, "out"))
        folder = mr.finalize_audio(state, cfg)
        return folder, mr.read_json(os.path.join(folder, "meta.json")), sess

    def reset(self):
        __import__("shutil").rmtree(self.d)
        os.mkdir(self.d)

    @unittest.skipUnless(__import__("shutil").which("ffprobe"), "no ffmpeg")
    def test_finalize_meta_and_cleanup(self):
        # AEC linked 0.5 s after the primaries: used behind a raw head
        folder, meta, sess = self.finalize([0, 900, -900, 5] * 6000, 0.5)
        self.assertIs(meta["mic_echo_cancel"], True)
        self.assertEqual(meta["mic_track_reason"], "echo-cancelled track complete")
        self.assertEqual(meta["tracks"], ["sys.wav", "mic-aec-aligned.wav"])
        me = mr.media_seconds(os.path.join(folder, "me.m4a"))
        self.assertAlmostEqual(me, 2.0, delta=0.1)   # 0.5 s raw + 1.5 s AEC
        self.assertEqual(os.listdir(sess), [])      # WAVs + aec.conf/log gone
        self.reset()
        _, meta, sess = self.finalize([0] * 32000)  # AEC never heard anything
        self.assertIs(meta["mic_echo_cancel"], False)
        self.assertEqual(meta["tracks"], ["sys.wav", "mic.wav"])
        self.assertIn("silent", meta["mic_track_reason"])
        self.assertEqual(os.listdir(sess), [])

    @unittest.skipUnless(__import__("shutil").which("ffprobe"), "no ffmpeg")
    def test_unverified_aec_encode_falls_back_to_raw(self):
        calls = []
        real = mr.encoded_ok

        def flaky(path, secs):
            calls.append(os.path.basename(path))
            return False if len(calls) == 1 else real(path, secs)
        with mock.patch.object(mr, "encoded_ok", flaky):
            folder, meta, sess = self.finalize([0, 900, -900, 5] * 8000)
        self.assertIs(meta["mic_echo_cancel"], False)
        self.assertEqual(meta["tracks"], ["sys.wav", "mic.wav"])
        self.assertIn("encode failed", meta["mic_track_reason"])
        # the raw fallback is verified the same way (all three outputs)
        self.assertEqual(calls, ["meeting.m4a", "meeting.m4a", "me.m4a",
                                 "others.m4a"])
        self.assertTrue(os.path.exists(os.path.join(folder, "me.m4a")))
        self.assertNotIn("audio_unverified", meta)

    @unittest.skipUnless(__import__("shutil").which("ffprobe"), "no ffmpeg")
    def test_unverified_raw_encode_keeps_the_wavs(self):
        with mock.patch.object(mr, "encoded_ok", lambda path, secs: False):
            folder, meta, sess = self.finalize(None)
        self.assertEqual(meta["audio_unverified"], "meeting.m4a did not verify")
        self.assertEqual(meta["wavs_kept_in"], sess)
        self.assertEqual(sorted(os.listdir(sess)),
                         ["aec.conf", "aec.log", "mic.wav", "sys.wav"])

    def test_wavs_kept_when_encoding_fails(self):
        def boom(cmd, **kw):
            if cmd[0] == "ffmpeg":
                raise subprocess.CalledProcessError(1, cmd)
            return subprocess.CompletedProcess(cmd, 1, "", "")
        with mock.patch.object(mr.subprocess, "run", boom):
            with self.assertRaises(subprocess.CalledProcessError):
                self.finalize([0, 900, -900, 5] * 8000)
        self.assertEqual(sorted(os.listdir(os.path.join(self.d, "raw"))),
                         ["aec.conf", "aec.log", "mic-aec-aligned.wav",
                          "mic-aec.wav", "mic.wav", "sys.wav"])


class TestWhisperOptions(unittest.TestCase):
    def cfg(self, text=""):
        with tempfile.NamedTemporaryFile("w", suffix=".ini",
                                         delete=False) as f:
            f.write(text)
        self.addCleanup(os.remove, f.name)
        return mr.load_config(f.name)

    def test_defaults(self):
        self.assertEqual(mr.whisper_options(self.cfg()), {
            "beam_size": 5, "vad_filter": True, "initial_prompt": None,
            "condition_on_previous_text": True})
        # AEC is opt-in until it has passed a real call (fix round 8)
        self.assertEqual(self.cfg().get("audio", "mic_source"), "raw")

    def test_overrides(self):
        opts = mr.whisper_options(self.cfg(
            "[whisper]\nbeam_size = 8\ncondition_on_previous_text = no\n"
            "initial_prompt = Tanner, FocusTime, 100% uptime, SER9\n"))
        self.assertEqual(opts["beam_size"], 8)
        self.assertFalse(opts["condition_on_previous_text"])
        # read raw: a literal % is fine
        self.assertEqual(opts["initial_prompt"],
                         "Tanner, FocusTime, 100% uptime, SER9")
        self.assertIsNone(mr.whisper_options(self.cfg(
            "[whisper]\ninitial_prompt =   \n"))["initial_prompt"])

    def test_bad_values_raise(self):
        for text in ("[whisper]\nbeam_size = 0\n",
                     "[whisper]\nbeam_size = x\n",
                     "[whisper]\ncondition_on_previous_text = maybe\n"):
            with self.assertRaises(ValueError, msg=text):
                mr.whisper_options(self.cfg(text))

    def test_transcribe_passes_options(self):
        calls = []

        class Model:
            def __init__(self, *a, **k):
                pass

            def transcribe(self, path, **kw):
                calls.append((path, kw))
                return iter([]), mock.Mock(duration=1.0)

        fake = mock.Mock(WhisperModel=Model)
        cfg = self.cfg("[whisper]\ninitial_prompt = Omarchy\n")
        with mock.patch.dict(sys.modules, {"faster_whisper": fake}):
            mr.transcribe(["me.m4a", "others.m4a"], cfg)
        self.assertEqual([p for p, _ in calls], ["me.m4a", "others.m4a"])
        for _, kw in calls:
            self.assertEqual(kw["initial_prompt"], "Omarchy")
            self.assertEqual(kw["beam_size"], 5)
            self.assertTrue(kw["condition_on_previous_text"])


class TestVttTimestamp(unittest.TestCase):
    def test_zero_and_hours_always_present(self):
        self.assertEqual(mr.fmt_vtt_ts(0), "00:00:00.000")
        self.assertEqual(mr.fmt_vtt_ts(65.5), "00:01:05.500")

    def test_hour_rollover(self):
        self.assertEqual(mr.fmt_vtt_ts(3599.999), "00:59:59.999")
        self.assertEqual(mr.fmt_vtt_ts(3600), "01:00:00.000")
        self.assertEqual(mr.fmt_vtt_ts(3725.25), "01:02:05.250")

    def test_ms_rounding(self):
        self.assertEqual(mr.fmt_vtt_ts(1.2345), "00:00:01.234")
        self.assertEqual(mr.fmt_vtt_ts(1.2346), "00:00:01.235")
        self.assertEqual(mr.fmt_vtt_ts(59.9996), "00:01:00.000")

    def test_never_negative(self):
        self.assertEqual(mr.fmt_vtt_ts(-0.3), "00:00:00.000")


def seg(start, end, text, speaker=""):
    return {"start": start, "end": end, "speaker": speaker, "text": text}


def parse_vtt(vtt):
    """Minimal WebVTT consumer: [(start_s, end_s, voice, decoded_text)]."""
    blocks = vtt.split("\n\n")
    assert blocks[0] == "WEBVTT", blocks[0]
    cues = []
    for block in blocks[1:]:
        if not block.strip():
            continue
        lines = block.strip("\n").split("\n")
        assert len(lines) == 3, lines
        m = re.fullmatch(r"(\d\d):(\d\d):(\d\d)\.(\d{3}) --> "
                         r"(\d\d):(\d\d):(\d\d)\.(\d{3})", lines[1])
        assert m, lines[1]
        t = [int(x) for x in m.groups()]
        start = t[0] * 3600 + t[1] * 60 + t[2] + t[3] / 1000
        end = t[4] * 3600 + t[5] * 60 + t[6] + t[7] / 1000
        v = re.fullmatch(r"<v ([^>]+)>(.*)</v>", lines[2])
        assert v, lines[2]
        assert "-->" not in lines[2]
        cues.append((start, end, v.group(1), html.unescape(v.group(2))))
    return cues


class TestSegmentsToVtt(unittest.TestCase):
    def test_exact_output(self):
        vtt = mr.segments_to_vtt([seg(0.0, 2.5, "Hi", "Others"),
                                  seg(3.0, 4.0, "Hello", "Me")])
        self.assertEqual(vtt,
                         "WEBVTT\n\n"
                         "1\n00:00:00.000 --> 00:00:02.500\n<v Others>Hi</v>\n\n"
                         "2\n00:00:03.000 --> 00:00:04.000\n<v Me>Hello</v>\n")

    def test_escaping_and_arrow(self):
        vtt = mr.segments_to_vtt([seg(0, 1, "a & b <tag> --> c")])
        self.assertIn("a &amp; b &lt;tag&gt; --&gt; c", vtt)
        body = vtt.split("\n", 2)[2]
        cue_text = [l for l in body.splitlines() if l.startswith("<v ")]
        self.assertTrue(all("-->" not in l for l in cue_text))

    def test_empty_text_skipped_and_numbering(self):
        vtt = mr.segments_to_vtt([seg(0, 1, "  "), seg(1, 2, "\n"),
                                  seg(2, 3, "kept")])
        self.assertEqual(vtt.count("<v "), 1)
        self.assertIn("1\n00:00:02.000 --> 00:00:03.000\n", vtt)

    def test_empty_list(self):
        self.assertEqual(mr.segments_to_vtt([]), "WEBVTT\n\n")

    def test_sorted_by_start(self):
        vtt = mr.segments_to_vtt([seg(10, 11, "second"), seg(2, 3, "first")])
        self.assertLess(vtt.index("first"), vtt.index("second"))
        self.assertIn("1\n00:00:02.000", vtt)

    def test_newlines_collapsed(self):
        vtt = mr.segments_to_vtt([seg(5, 6, "line one\n\nline two")])
        self.assertIn("<v Others>line one line two</v>", vtt)

    def test_end_always_after_start(self):
        # reversed, zero-length, same-millisecond, legacy same-second
        vtt = mr.segments_to_vtt([seg(5, 4, "rev"), seg(7, 7, "zero"),
                                  seg(9.0001, 9.0002, "same ms"),
                                  seg(11.0, 11.0, "legacy")])
        self.assertIn("00:00:05.000 --> 00:00:05.001\n<v Others>rev", vtt)
        self.assertIn("00:00:07.000 --> 00:00:07.001\n<v Others>zero", vtt)
        self.assertIn("00:00:09.000 --> 00:00:09.001\n<v Others>same ms", vtt)
        self.assertIn("00:00:11.000 --> 00:00:11.001\n<v Others>legacy", vtt)
        for start, end, _, _ in parse_vtt(vtt):
            self.assertGreater(end, start)

    def test_blank_speaker_names_fall_back(self):
        vtt = mr.segments_to_vtt([seg(0, 1, "a", "Me"), seg(1, 2, "b", "")],
                                 me="  ", others="")
        self.assertIn("<v Me>a</v>", vtt)
        self.assertIn("<v Others>b</v>", vtt)
        self.assertNotIn("<v >", vtt)

    def test_rejects_malformed_segments(self):
        for bad in ([{}], [{"start": 0, "end": 1}],
                    [{"start": "0", "end": 1, "text": "x"}],
                    [{"start": float("nan"), "end": 1, "text": "x"}],
                    [{"start": 0, "end": float("inf"), "text": "x"}],
                    [{"start": True, "end": 1, "text": "x"}],
                    [{"start": 0, "end": 1, "text": "x", "speaker": 3}],
                    ["nope"], {"start": 0}):
            with self.assertRaises(ValueError, msg=repr(bad)):
                mr.segments_to_vtt(bad)

    def test_absurd_numbers_are_value_errors(self):
        for v in (1e308, -1e308, 10 ** 400, 1e7 + 1):
            with self.assertRaises(ValueError, msg=repr(v)):
                mr.segments_to_vtt([{"start": 0, "end": v, "text": "x"}])
        # the largest accepted value still renders
        self.assertIn("2777:46:40.000", mr.segments_to_vtt([seg(0, 1e7, "x")]))

    def test_voice_names_escaped_once(self):
        vtt = mr.segments_to_vtt([seg(0, 1, "a", "Me"), seg(1, 2, "b", "Others")],
                                 me="R&D <Ops>", others="A & B")
        self.assertIn("<v R&amp;D &lt;Ops&gt;>a</v>", vtt)
        self.assertIn("<v A &amp; B>b</v>", vtt)
        self.assertEqual([html.unescape(v) for _, _, v, _ in parse_vtt(vtt)],
                         ["R&D <Ops>", "A & B"])

    def test_same_ms_starts_keep_spoken_order(self):
        # 1.0001 and 1.0004 both round to 1.000; the later one ends sooner
        vtt = mr.segments_to_vtt([seg(1.0004, 1.2, "second"),
                                  seg(1.0001, 5.0, "first")])
        self.assertLess(vtt.index("first"), vtt.index("second"))

    def test_speaker_name_mapping(self):
        vtt = mr.segments_to_vtt([seg(0, 1, "a", "Me"), seg(1, 2, "b", "Others"),
                                  seg(2, 3, "c", "")],
                                 me="Ser Nine", others="Tanner")
        self.assertIn("<v Ser Nine>a</v>", vtt)
        self.assertIn("<v Tanner>b</v>", vtt)
        self.assertIn("<v Tanner>c</v>", vtt)  # unlabelled -> others

    def test_config_default_speakers(self):
        cfg = mr.load_config("/nonexistent/path.ini")
        self.assertEqual(cfg.get("speakers", "me"), "Me")
        self.assertEqual(cfg.get("speakers", "others"), "Others")


class TestSegmentDicts(unittest.TestCase):
    def test_tuples_keep_float_precision(self):
        out = mr.segment_dicts([(0.123456, 1.5, " hi ", "Me"), (2, 3, "x")])
        self.assertEqual(out, [
            {"start": 0.123456, "end": 1.5, "speaker": "Me", "text": "hi"},
            {"start": 2.0, "end": 3.0, "speaker": "", "text": "x"}])


class TestParseMdLine(unittest.TestCase):
    def test_labelled(self):
        self.assertEqual(mr.parse_md_line("[01:05 -> 01:09] Me: hello there\n"),
                         seg(65.0, 69.0, "hello there", "Me"))

    def test_minutes_over_59(self):
        d = mr.parse_md_line("[61:01 -> 125:30] Others: long call")
        self.assertEqual((d["start"], d["end"], d["speaker"]),
                         (3661.0, 7530.0, "Others"))

    def test_unlabelled(self):
        self.assertEqual(mr.parse_md_line("[00:00 -> 00:05] Note: not a speaker"),
                         seg(0.0, 5.0, "Note: not a speaker", ""))

    def test_skips_headers_and_blanks(self):
        for line in ("# Meeting 2026-11-23T16:51:50", "", "Duration: 44:47",
                     "_No speech detected._"):
            self.assertIsNone(mr.parse_md_line(line))

    def test_rejects_seconds_over_59(self):
        self.assertIsNone(mr.parse_md_line("[00:60 -> 01:00] x"))
        self.assertIsNone(mr.parse_md_line("[00:00 -> 00:99] x"))

    def test_empty_labelled_utterance(self):
        line = mr.format_segment(1, 2, "  ", "Me")
        self.assertEqual(line, "[00:01 -> 00:02] Me: ")
        self.assertEqual(mr.parse_md_line(line + "\n"), seg(1.0, 2.0, "", "Me"))

    def test_labels_false_keeps_text_whole(self):
        self.assertEqual(
            mr.parse_md_line("[00:00 -> 00:05] Me: said literally", labels=False),
            seg(0.0, 5.0, "Me: said literally", ""))

    def test_roundtrip_with_format_segment(self):
        line = mr.format_segment(3725.7, 3730.2, "text", "Others")
        self.assertEqual(mr.parse_md_line(line),
                         seg(3725.0, 3730.0, "text", "Others"))


class TestParseMdTranscript(unittest.TestCase):
    def test_continuation_lines_kept(self):
        line = mr.format_segment(0, 4, "first\n\nsecond", "Others")
        md = ("# Meeting x\n\nDuration: 00:10\n\n" + line
              + "\n[00:05 -> 00:06] Me: next\n")
        segs = mr.parse_md_transcript(md)
        self.assertEqual(segs, [seg(0.0, 4.0, "first\n\nsecond", "Others"),
                                seg(5.0, 6.0, "next", "Me")])
        self.assertEqual(parse_vtt(mr.segments_to_vtt(segs))[0][3], "first second")

    def test_no_speech_marker_is_empty(self):
        self.assertEqual(mr.parse_md_transcript(
            "# Meeting x\n\nDuration: 00:03\n\n_No speech detected._\n"), [])

    def test_zero_rows_is_invalid(self):
        for text in ("", "# Some notes\n\nhello world\n", "[00:61 -> 01:00] x\n"):
            with self.assertRaises(ValueError, msg=repr(text)):
                mr.parse_md_transcript(text)

    def test_no_speech_marker_only_as_recorder_body(self):
        for text in ("_No speech detected._\n",
                     "# Meeting x\n\n_No speech detected._\n",
                     "Duration: 00:03\n\n_No speech detected._"):
            self.assertEqual(mr.parse_md_transcript(text), [], msg=repr(text))
        for text in ("# Notes\n\nsome unrelated text\n\n_No speech detected._\n",
                     "_No speech detected._\nand then more\n",
                     "# A\n# B\n_No speech detected._\n",
                     "Lorem\n_No speech detected._\nipsum\n"):
            with self.assertRaises(ValueError, msg=repr(text)):
                mr.parse_md_transcript(text)

    def test_bad_seconds_row_is_an_error_not_text(self):
        md = "# M\n\n[00:01 -> 00:02] Me: fine\n[00:03 -> 00:75] Others: bad\n"
        with self.assertRaisesRegex(ValueError, "line 4"):
            mr.parse_md_transcript(md)


def make_cfg(d, body="[speakers]\nme = Ser Nine\nothers = Tanner\n"):
    """A config file under d, independent of the user's own config.ini."""
    path = os.path.join(d, "config.ini")
    Path(path).write_text(body)
    return path


class TestSpeakerNamesConfig(unittest.TestCase):
    def test_percent_and_blank(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = mr.load_config(make_cfg(d, "[speakers]\nme = 100% Me\nothers =\n"))
            self.assertEqual(mr.speaker_names(cfg), ("100% Me", "Others"))

    def test_defaults(self):
        cfg = mr.load_config("/nonexistent/path.ini")
        self.assertEqual(mr.speaker_names(cfg), ("Me", "Others"))


class TestWriteTranscripts(unittest.TestCase):
    def test_md_txt_unchanged_plus_vtt_and_segments(self):
        segs = [(0.25, 4.75, " Hi there ", "Others"), (5.5, 7.125, "Yo", "Me")]
        cfg = mr.load_config("/nonexistent/path.ini")
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(
                mr.write_transcripts(d, segs, 1_700_000_000, 65.4, "Sync", cfg))
            body = "[00:00 -> 00:04] Others: Hi there\n[00:05 -> 00:07] Me: Yo\n"
            self.assertEqual(Path(d, "transcript.txt").read_text(), body)
            iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(1_700_000_000))
            self.assertEqual(Path(d, "transcript.md").read_text(),
                             f"# Sync {iso}\n\nDuration: 01:05\n\n" + body)
            self.assertEqual(json.loads(Path(d, "segments.json").read_text())[1],
                             {"start": 5.5, "end": 7.125, "speaker": "Me", "text": "Yo"})
            vtt = Path(d, "transcript.vtt").read_text(encoding="utf-8")
            self.assertIn("00:00:05.500 --> 00:00:07.125\n<v Me>Yo</v>", vtt)

    def test_export_failure_keeps_legacy_transcripts(self):
        cfg = mr.load_config("/nonexistent/path.ini")
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "transcript.vtt"))  # cannot be replaced
            err = mr.write_transcripts(d, [(0, 1, "hi", "Me")], 1_700_000_000,
                                       1, "", cfg)
            self.assertTrue(err)
            self.assertEqual(Path(d, "transcript.txt").read_text(),
                             "[00:00 -> 00:01] Me: hi\n")
            self.assertTrue(Path(d, "transcript.md").read_text().endswith(
                "[00:00 -> 00:01] Me: hi\n"))
            self.assertEqual([p for p in os.listdir(d) if p.endswith(".tmp")], [])

    def test_failed_segments_refresh_never_resurrects_old_transcript(self):
        cfg = mr.load_config("/nonexistent/path.ini")
        real = mr.write_json

        def failing(path, data, private=False):
            if path.endswith("segments.json"):
                raise OSError(28, "No space left on device")
            return real(path, data, private)

        with tempfile.TemporaryDirectory() as d, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(mr.write_transcripts(
                d, [(0, 1, "old", "Me")], 1_700_000_000, 1, "", cfg))
            with mock.patch.object(mr, "write_json", failing):
                err = mr.write_transcripts(d, [(0, 1, "new", "Me")],
                                           1_700_000_000, 1, "", cfg)
            self.assertIn("No space left", err)
            self.assertFalse(os.path.exists(os.path.join(d, "segments.json")))
            self.assertEqual(mr.cmd_export([d]), 0)       # the retry
            vtt = parse_vtt(Path(d, "transcript.vtt").read_text(encoding="utf-8"))
            self.assertEqual([c[3] for c in vtt], ["new"])

    def test_cache_older_than_md_is_ignored(self):
        with tempfile.TemporaryDirectory() as d, \
                contextlib.redirect_stdout(io.StringIO()):
            Path(d, "segments.json").write_text(json.dumps([seg(0, 1, "old", "Me")]))
            Path(d, "transcript.md").write_text("# M\n\n[00:00 -> 00:01] Me: new\n")
            os.utime(os.path.join(d, "segments.json"), (1_000_000, 1_000_000))
            self.assertEqual(mr.cmd_export([d]), 0)
            self.assertEqual(json.loads(Path(d, "segments.json").read_text())[0]["text"],
                             "new")
            self.assertEqual(mr.load_folder_segments(d)[0]["text"], "new")


class TestAtomicWritePermissions(unittest.TestCase):
    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_new_file_honours_umask(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.umask(0o077)
            try:
                mr.write_text_atomic(os.path.join(d, "a.vtt"), "x")
                mr.write_json(os.path.join(d, "b.json"), [])
            finally:
                os.umask(old)
            self.assertEqual(self.mode(os.path.join(d, "a.vtt")), 0o600)
            self.assertEqual(self.mode(os.path.join(d, "b.json")), 0o600)
            old = os.umask(0o022)
            try:
                mr.write_text_atomic(os.path.join(d, "c.vtt"), "x")
            finally:
                os.umask(old)
            self.assertEqual(self.mode(os.path.join(d, "c.vtt")), 0o644)

    def test_replacing_keeps_existing_mode(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "transcript.vtt")
            Path(path).write_text("old")
            os.chmod(path, 0o600)
            old = os.umask(0o022)
            try:
                mr.write_text_atomic(path, "new")
            finally:
                os.umask(old)
            self.assertEqual((self.mode(path), Path(path).read_text()), (0o600, "new"))

    def test_private_is_0600_even_with_open_umask(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "screens.json")
            Path(path).write_text("[]")
            os.chmod(path, 0o644)
            old = os.umask(0)
            try:
                mr.write_json(path, [], private=True)
            finally:
                os.umask(old)
            self.assertEqual(self.mode(path), 0o600)
            self.assertEqual([p for p in os.listdir(d) if p.endswith(".tmp")], [])


class TestExport(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self._old_cfg = mr.CONFIG_FILE
        mr.CONFIG_FILE = make_cfg(self.d)   # not the user's config.ini
        self.out, self.err = io.StringIO(), io.StringIO()
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(contextlib.redirect_stdout(self.out))
        self._stack.enter_context(contextlib.redirect_stderr(self.err))

    def tearDown(self):
        self._stack.close()
        mr.CONFIG_FILE = self._old_cfg
        self._tmp.cleanup()

    def folder(self, name, files):
        path = os.path.join(self.d, name)
        os.mkdir(path)
        for fname, text in files.items():
            Path(path, fname).write_text(text, encoding="utf-8")
        return path

    def vtt(self, folder):
        return parse_vtt(Path(folder, "transcript.vtt").read_text(encoding="utf-8"))

    def test_backfill_from_md_and_missing(self):
        d = self.folder("a", {"transcript.md": "# Meeting x\n\nDuration: 00:10\n\n"
                              "[00:05 -> 00:09] Me: b\n[00:00 -> 00:04] a\n"})
        self.assertEqual(mr.cmd_export([d]), 0)
        first = Path(d, "transcript.vtt").read_text(encoding="utf-8")
        self.assertEqual(mr.cmd_export([d]), 0)  # idempotent
        self.assertEqual(Path(d, "transcript.vtt").read_text(encoding="utf-8"), first)
        self.assertEqual([(c[2], c[3]) for c in parse_vtt(first)],
                         [("Tanner", "a"), ("Ser Nine", "b")])  # test config names
        self.assertEqual(len(json.loads(Path(d, "segments.json").read_text())), 2)
        self.assertEqual(mr.cmd_export([self.folder("empty", {})]), 1)

    def test_mixed_batch_continues_after_failure(self):
        bad = self.folder("bad", {"segments.json": "[{}]"})
        notes = self.folder("notes", {"transcript.md": "# just notes\n\nhello\n"})
        missing = os.path.join(self.d, "missing")
        good = self.folder("good", {"segments.json": json.dumps(
            [seg(1.5, 2.25, "fine", "Me")])})
        self.assertEqual(mr.cmd_export([bad, notes, missing, good]), 1)
        err = self.err.getvalue()
        for f in (bad, notes, missing):
            self.assertIn(f + ": export failed", err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(self.out.getvalue().split(),
                         [os.path.join(good, "transcript.vtt")])
        self.assertEqual(self.vtt(good), [(1.5, 2.25, "Ser Nine", "fine")])
        self.assertFalse(os.path.exists(os.path.join(notes, "segments.json")))
        self.assertFalse(os.path.exists(os.path.join(bad, "transcript.vtt")))

    def test_absurd_numbers_fail_one_folder_not_the_batch(self):
        huge = self.folder("huge", {"segments.json": '[{"start": 0, "end": 1e308, '
                                    '"text": "x"}]'})
        digits = self.folder("digits", {"segments.json": '[{"start": 0, "end": 1'
                                        + "0" * 400 + ', "text": "x"}]'})
        good = self.folder("ok", {"segments.json": json.dumps([seg(0, 1, "fine")])})
        self.assertEqual(mr.cmd_export([huge, digits, good]), 1)
        self.assertIn(huge + ": export failed", self.err.getvalue())
        self.assertIn(digits + ": export failed", self.err.getvalue())
        self.assertEqual(self.vtt(good)[0][3], "fine")

    def test_truncated_cache_recovers_from_md(self):
        d = self.folder("torn", {"segments.json": '[{"start": 0, "en',
                                 "transcript.md": "# M\n\n[00:00 -> 00:02] Me: hi\n"})
        os.utime(os.path.join(d, "transcript.md"), (1_000_000, 1_000_000))
        self.assertEqual(mr.cmd_export([d]), 0)
        self.assertEqual(self.vtt(d)[0][3], "hi")
        self.assertEqual(len(json.loads(Path(d, "segments.json").read_text())), 1)

    def test_bad_segments_keep_existing_vtt(self):
        d = self.folder("keep", {"segments.json": "[{}]",
                                 "transcript.vtt": "WEBVTT\n\nold\n"})
        self.assertEqual(mr.cmd_export([d]), 1)
        self.assertEqual(Path(d, "transcript.vtt").read_text(), "WEBVTT\n\nold\n")

    def test_no_speech_md_exports_empty(self):
        d = self.folder("quiet", {"transcript.md": "# Meeting x\n\nDuration: 00:02"
                                  "\n\n_No speech detected._\n"})
        self.assertEqual(mr.cmd_export([d]), 0)
        self.assertEqual(Path(d, "transcript.vtt").read_text(), "WEBVTT\n\n")

    def test_empty_cached_segments_retry_from_md(self):
        d = self.folder("retry", {"segments.json": "[]", "transcript.md":
                                  "# M\n\n[00:00 -> 00:02] Me: hello\n"})
        self.assertEqual(mr.cmd_export([d]), 0)
        self.assertEqual(len(self.vtt(d)), 1)

    def test_meta_speaker_labels_controls_md_labels(self):
        md = "# M\n\n[00:00 -> 00:02] Me: hello\n"
        single = self.folder("single", {"transcript.md": md,
                                        "meta.json": json.dumps({"source": "manual"})})
        labelled = self.folder("labelled", {"transcript.md": md, "meta.json":
                                            json.dumps({"speaker_labels": True})})
        nometa = self.folder("nometa", {"transcript.md": md})
        self.assertEqual(mr.cmd_export([single, labelled, nometa]), 0)
        self.assertEqual(self.vtt(single)[0][2:], ("Tanner", "Me: hello"))
        self.assertEqual(self.vtt(labelled)[0][2:], ("Ser Nine", "hello"))
        self.assertEqual(self.vtt(nometa)[0][2:], ("Ser Nine", "hello"))  # heuristic


class TestVttConsumerContract(unittest.TestCase):
    def test_decoded_cues(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = mr.load_config(make_cfg(d))
            path = mr.write_vtt(d, [
                seg(3.25, 4.0, "second"),
                seg(0.5, 2.0, "Tom & Jerry <b>\n\nnext --> line", "Me"),
                seg(2.5, 3.0, " \n\t ", "Others"),        # whitespace-only: skipped
                seg(3661.5, 3662.0, "héllo ünïcode", "Others"),
            ], cfg)
            raw = Path(path).read_bytes()
        text = raw.decode("utf-8")  # valid UTF-8 regardless of locale
        self.assertEqual(parse_vtt(text), [
            (0.5, 2.0, "Ser Nine", "Tom & Jerry <b> next --> line"),
            (3.25, 4.0, "Tanner", "second"),
            (3661.5, 3662.0, "Tanner", "héllo ünïcode"),
        ])
        ids = [b.split("\n")[0] for b in text.split("\n\n")[1:] if b.strip()]
        self.assertEqual(ids, ["1", "2", "3"])


def ppm(w, h, fill=0, patch=None, header=None):
    """Synthetic P6 bytes: all channels = fill, optional (x, y, value) pixels."""
    px = bytearray([fill] * (w * h * 3))
    for x, y, v in patch or []:
        i = (y * w + x) * 3
        px[i:i + 3] = bytes([v, v, v])
    return (header or f"P6\n{w} {h}\n255\n".encode()) + bytes(px)


class TestThumbDiff(unittest.TestCase):
    def test_parse_with_comment(self):
        w, h, mx, px = mr.parse_ppm(ppm(2, 1, 7, header=b"P6\n# grim\n2 1\n255\n"))
        self.assertEqual((w, h, mx, px), (2, 1, 255, bytes([7] * 6)))

    def test_identical_is_zero_and_full_flip_is_one(self):
        a = mr.parse_ppm(ppm(4, 4, 0))
        self.assertEqual(mr.thumb_diff(a, mr.parse_ppm(ppm(4, 4, 0))), 0.0)
        self.assertEqual(mr.thumb_diff(a, mr.parse_ppm(ppm(4, 4, 255))), 1.0)

    def test_partial_change_is_mean_abs_fraction(self):
        a = mr.parse_ppm(ppm(10, 10, 0))
        b = mr.parse_ppm(ppm(10, 10, 0, patch=[(0, 0, 255), (5, 5, 51)]))
        self.assertAlmostEqual(mr.thumb_diff(a, b), (255 + 51) / (100 * 255))

    def test_size_change_is_total_difference(self):
        self.assertEqual(mr.thumb_diff(mr.parse_ppm(ppm(4, 4)),
                                       mr.parse_ppm(ppm(4, 5))), 1.0)

    def test_rejects_bad_input(self):
        for data in (b"P3\n1 1\n255\n0 0 0", b"P6\n2 2\n255\n\x00",
                     b"P6\nx y\n255\n", b""):
            with self.assertRaises(ValueError, msg=repr(data)):
                mr.parse_ppm(data)


P = mr.screen_params(mr.load_config("/nonexistent/path.ini"))   # the defaults
TW, TH = 120, 72          # synthetic thumbnail; 12x12 grid -> 10x6 px cells
STRIP_Y = int(TH * 0.8)   # bottom 20 %: the live camera strip


class Screen:
    """Synthetic call window: a slide (top) + optional camera strip (bottom)."""

    def __init__(self, seed=1):
        self.rng = random.Random(seed)

    def frame(self, slide, strip="random", lines=(), video=False, top_rows=None):
        px = bytearray(TW * TH * 3)
        for y in range(TH):
            for x in range(TW):
                i = (y * TW + x) * 3
                if video:
                    continue
                if y >= STRIP_Y and strip != "static":
                    continue
                v = (x * (slide + 1) * 3 + y * 5 + slide * 40) % 200
                if y >= STRIP_Y:
                    v = 90 if strip == "static" else v
                px[i:i + 3] = bytes([v, v, v])
        for x0, y0, x1, y1 in lines:      # 'text': white blocks
            for y in range(y0, y1):
                for x in range(x0, x1):
                    i = (y * TW + x) * 3
                    px[i:i + 3] = b"\xff\xff\xff"
        noise_from = 0 if video else (STRIP_Y if strip == "random" else None)
        if top_rows is not None:
            noise_from = top_rows
        if noise_from is not None:
            a = noise_from * TW * 3
            px[a:] = self.rng.randbytes(len(px) - a)
        return (TW, TH, 255, bytes(px))


def run_keeps(frames, params=P, interval=3.0):
    """Drive cell_diffs + screen_keep_step exactly like screens-loop does;
    frames are parsed-PPM tuples (None = window missing / failed sample).
    Returns the kept tick times."""
    state, prev, kept, times = dict(mr.SCREEN_KEEP_START), None, None, []
    for i, f in enumerate(frames):
        t = i * interval
        if f is None:
            prev = None
            continue
        d_prev, d_kept = mr.cell_diffs(prev, f), mr.cell_diffs(kept, f)
        prev = f
        state, keep = mr.screen_keep_step(state, d_prev, d_kept, t, params)
        if keep:
            kept = f
            times.append(t)
    return times


class TestCellDiffs(unittest.TestCase):
    def test_per_cell_values(self):
        a = mr.parse_ppm(ppm(24, 24, 0))
        b = mr.parse_ppm(ppm(24, 24, 0, patch=[(0, 0, 255)]))      # cell 0 only
        d = mr.cell_diffs(a, b)
        self.assertEqual(len(d), 144)
        self.assertAlmostEqual(d[0], 1 / 4)                        # 1 of 2x2 px
        self.assertEqual(sum(d[1:]), 0)
        self.assertIsNone(mr.cell_diffs(a, mr.parse_ppm(ppm(24, 25))))
        self.assertIsNone(mr.cell_diffs(None, a))

    def test_tiny_image_uses_fewer_cells(self):
        d = mr.cell_diffs(mr.parse_ppm(ppm(4, 3)), mr.parse_ppm(ppm(4, 3, 255)))
        self.assertEqual(d, [1.0] * 12)


class TestKeepRule(unittest.TestCase):
    """Volatile-mask keep rule on synthetic windows (default parameters)."""

    def test_static_slides_with_live_strip_kept_once_each(self):
        s = Screen()
        frames = [s.frame(k) for k in (0, 1, 2, 3) for _ in range(10)]
        # strip volatile on tick 2 (moved on ticks 1 and 2), settled on
        # ticks 2 and 3 -> keep at tick 3; later slides 2 ticks after they appear
        self.assertEqual(run_keeps(frames), [9.0, 36.0, 66.0, 96.0])

    def test_caret_sized_change_is_not_new(self):
        s = Screen()
        frames = [s.frame(0) for _ in range(10)] + \
                 [s.frame(0, lines=[(10, 20, 13, 25)]) for _ in range(10)]
        self.assertEqual(run_keeps(frames), [9.0])

    def test_full_window_video_keeps_nothing(self):
        s = Screen()
        self.assertEqual(run_keeps([s.frame(0, video=True) for _ in range(40)]), [])

    def test_mostly_video_keeps_nothing(self):
        s = Screen()       # everything but the top 10 % is live video (> 85 %)
        frames = [s.frame(0, top_rows=TH // 10) for _ in range(40)]
        self.assertEqual(run_keeps(frames), [])

    def test_strip_holding_still_for_a_tick_does_not_delay_warm_up(self):
        # measured live: a random strip cell can repeat itself for one tick;
        # the first slide (shown 15 s from the start) must still be kept
        s = Screen()
        frames = [s.frame(0) for _ in range(5)]
        frames[2] = frames[1]
        frames += [s.frame(1) for _ in range(5)]
        self.assertEqual(run_keeps(frames), [9.0, 21.0])

    def test_single_text_line_is_too_small(self):
        # fix round 8: a new line of text (~9 cells, 6 %) is a small change,
        # like a name label; only a large coherent region is a new frame
        s = Screen()
        line = (10, 20, 100, 25)
        frames = [s.frame(0) for _ in range(10)] + \
                 [s.frame(0, lines=[line]) for _ in range(10)]
        self.assertEqual(run_keeps(frames), [9.0])

    def test_large_block_change_while_strip_moves_is_new(self):
        s = Screen()
        block = (10, 6, 110, 40)          # ~half the window, one region
        frames = [s.frame(0) for _ in range(10)] + \
                 [s.frame(0, lines=[block]) for _ in range(10)]
        self.assertEqual(run_keeps(frames), [9.0, 36.0])

    def test_two_tick_build_of_a_large_block_is_kept_once_settled(self):
        # a big change caught on 2 ticks is a transition (burst), not video
        s = Screen()
        frames = ([s.frame(0) for _ in range(10)]
                  + [s.frame(0, lines=[(10, 6, 110, 24)])]
                  + [s.frame(0, lines=[(10, 6, 110, 48)]) for _ in range(10)])
        self.assertEqual(run_keeps(frames), [9.0, 39.0])

    def test_cell_that_stops_being_volatile_is_eligible_again(self):
        s = Screen()
        live = TH * 6 // 10                  # bottom 40 %: live video
        moving = [s.frame(0, top_rows=live) for _ in range(10)]
        frozen = [s.frame(0, strip="static") for _ in range(8)]
        # a large change that lives only in the (former) video region
        changed = [s.frame(0, strip="static", lines=[(0, 44, 120, 70)])
                   for _ in range(8)]
        times = run_keeps(moving + frozen + changed)
        self.assertEqual(times[0], 9.0)
        # the frozen region itself differs from the kept frame once it calms
        # down (a camera turned off is a real layout change) ...
        self.assertEqual(len([t for t in times if 30 <= t < 54]), 1)
        # ... and a later change inside the region is seen again
        self.assertEqual(len([t for t in times if t >= 54]), 1)

    def test_gallery_churn_keeps_only_the_initial_frame(self):
        # Teams gallery: static avatar tiles; every few ticks a speaking
        # ring moves to another tile, a name label changes, tiles reflow by
        # a cell -- small scattered changes, each settling for a few ticks
        rng = random.Random(7)
        s = Screen()
        frames = []
        for k in range(40):
            spots = [(x, y, x + rng.randint(6, 14), y + rng.randint(3, 6))
                     for x, y in ((rng.randrange(0, 105), rng.randrange(0, 66))
                                  for _ in range(rng.randint(2, 5)))]
            frames += [s.frame(1, strip="static", lines=spots)] * 3
        self.assertEqual(run_keeps(frames), [6.0])

    def test_region_is_one_block_not_scattered_cells(self):
        n, p = 144, P
        calm = range(n)
        # nine 2x2 clumps 3 cells apart: 25 % of the cells changed, but
        # the largest region is 4 cells (speaking rings in separate tiles)
        scattered = [0.5 if divmod(i, 12)[0] % 4 < 2
                     and divmod(i, 12)[1] % 4 < 2 else 0.0 for i in range(n)]
        self.assertEqual(sum(1 for x in scattered if x), 36)
        self.assertEqual(mr.largest_region(
            [i for i, x in enumerate(scattered) if x], n), 4)
        self.assertFalse(mr.region_is_new(scattered, calm, p))
        block = [0.5 if divmod(i, 12)[0] < 3 else 0.0 for i in range(n)]
        self.assertTrue(mr.region_is_new(block, calm, p))          # 25 %
        # text lines with one blank row between them are one region
        lines = [0.5 if divmod(i, 12)[0] in (0, 2, 4, 6) and
                 divmod(i, 12)[1] < 9 else 0.0 for i in range(n)]
        self.assertEqual(mr.largest_region(
            [i for i, x in enumerate(lines) if x], n), 36)
        self.assertTrue(mr.region_is_new(lines, calm, p))
        # masked (volatile) cells never count
        self.assertFalse(mr.region_is_new(block, range(18, n), p))

    # Operator's numbers for every consecutive pair of KEPT frames on two
    # real calls on 2026-11-24 (.work/frame-stats.txt: 12x12 grid, changed
    # = cell mean-abs-diff > 0.02): (changed fraction, largest connected
    # fraction). Call 1 was gallery/video tiles in a 338-598 px wide
    # window; call 2 (1783 px) had shared content.
    REAL_CALL_1 = (
        (0.16, 0.125), (0.201, 0.181), (0.139, 0.111), (0.167, 0.146),
        (0.125, 0.097), (0.188, 0.167), (0.201, 0.174), (0.958, 0.958),
        (0.944, 0.944), (0.257, 0.222), (0.868, 0.576), (0.118, 0.097),
        (0.118, 0.097), (0.417, 0.292), (0.188, 0.16), (0.153, 0.125),
        (0.194, 0.167), (0.382, 0.257), (0.167, 0.132), (0.188, 0.167),
        (0.188, 0.167), (0.188, 0.16), (0.188, 0.167), (0.146, 0.125),
        (0.188, 0.16), (0.181, 0.16), (0.167, 0.132), (0.194, 0.167),
        (0.181, 0.16), (0.174, 0.16), (0.181, 0.16), (0.153, 0.132),
        (0.09, 0.076), (0.146, 0.111), (0.181, 0.132), (0.181, 0.16),
        (0.181, 0.16), (0.861, 0.854))
    REAL_CALL_2 = (
        (0.472, 0.472), (0.354, 0.354), (0.326, 0.326), (0.861, 0.861),
        (0.326, 0.215), (0.361, 0.319), (0.153, 0.062), (0.889, 0.889),
        (0.507, 0.417), (0.076, 0.056), (0.069, 0.069), (0.049, 0.035),
        (0.701, 0.701), (0.806, 0.806), (0.167, 0.062), (0.229, 0.208),
        (0.208, 0.139), (0.243, 0.181), (0.257, 0.167), (0.924, 0.924),
        (0.076, 0.049), (0.681, 0.681), (0.076, 0.056), (0.465, 0.41),
        (0.312, 0.236), (0.535, 0.472), (0.854, 0.854), (0.854, 0.854),
        (0.049, 0.014), (0.854, 0.854), (0.931, 0.931), (0.236, 0.097),
        (0.924, 0.924), (0.208, 0.174), (0.618, 0.528), (0.472, 0.403),
        (0.465, 0.396), (0.069, 0.042), (0.069, 0.069), (0.924, 0.924),
        (0.215, 0.153), (0.243, 0.16), (0.924, 0.924), (0.083, 0.062),
        (0.049, 0.028), (0.083, 0.035), (0.076, 0.062), (0.076, 0.076),
        (0.924, 0.924), (0.924, 0.924), (0.062, 0.042), (0.042, 0.014),
        (0.924, 0.924), (0.917, 0.917))

    @staticmethod
    def real_layout(changed, largest, n=144, cols=12):
        """d_kept for one measured pair: the largest region as one compact
        block (full rows from the top), the rest as 2x2 clumps at least 3
        cells from everything else (separate regions). None if it does not
        fit the grid."""
        a, rest = round(largest * n), round(changed * n) - round(largest * n)
        d = [0.0] * n
        for i in range(a):
            d[i] = 0.5
        row = -(-a // cols) + 2                  # 2 blank rows below the block
        clump = max(1, min(4, a))
        while rest > 0:
            if row + 1 >= n // cols:
                return None
            for c0 in range(0, cols - 1, 4):
                cells = [(row + dr) * cols + c0 + dc
                         for dr in (0, 1) for dc in (0, 1)][:min(clump, rest)]
                for i in cells:
                    d[i] = 0.5
                rest -= len(cells)
                if rest <= 0:
                    break
            row += 4
        return d

    def test_real_call_numbers(self):
        for table, want in ((self.REAL_CALL_1, 6), (self.REAL_CALL_2, 27)):
            got = used = 0
            for changed, largest in table:
                d = self.real_layout(changed, largest)
                if d is None:                    # cannot model this pair
                    self.assertGreaterEqual(largest, 0.25)
                    got += 1
                    continue
                used += 1
                cells = [i for i, x in enumerate(d) if x]
                self.assertEqual(mr.largest_region(cells, 144),
                                 round(largest * 144), (changed, largest))
                got += mr.region_is_new(d, range(144), P)
            self.assertGreater(used, len(table) * 0.8)
            # + the initial frame of each call: 7 of 39 and 28 of 55
            self.assertEqual(got, want)
        # and call 1's window was never >= min_width: no frames at all
        for w in (338, 574, 598):
            self.assertTrue(mr.screen_too_narrow({"size": [w, 680]}, P))
        self.assertFalse(mr.screen_too_narrow({"size": [1783, 1378]}, P))
        self.assertTrue(mr.screen_too_narrow({}, P))

    @staticmethod
    def deck(s, trans_ticks=2, mode="text", ticks_per_slide=5):
        """Operator's live test (/tmp/mr-slides.py): 6 slides x 15 s (5 ticks
        at 3 s) over a moving strip in the bottom 20 %. A change spans
        trans_ticks ticks: the first trans_ticks - 1 show a fade (blend of
        old and new), so the SAME cells move on every transition tick (the
        Codex model: 2 moving + 3 calm ticks per slide)."""
        def slide(k):
            if mode == "gradient":
                return s.frame(k)
            ind = (7 * k) % 20 * 2                  # indent changes per slide
            gap = 8 + 4 * (k % 2)
            lines = [(2 + ind, 4, 52 + ind, 9)] + [
                (6 + ind, 14 + i * gap, 46 + ind + 5 * i, 18 + i * gap)
                for i in range(3 + k % 2)]
            return s.frame(0, lines=lines)
        frames, strip_at = [], STRIP_Y * TW * 3
        for k in range(6):
            for j in range(ticks_per_slide):
                f = slide(k)
                if k and j < trans_ticks - 1:
                    a = (j + 1) / trans_ticks            # fade progress
                    old = slide(k - 1)[3]
                    mix = bytes(int(o + (n - o) * a)
                                for o, n in zip(old[:strip_at], f[3][:strip_at]))
                    f = f[:3] + (mix + f[3][strip_at:],)
                frames.append(f)
        return frames

    def assert_one_per_slide(self, times, label):
        self.assertEqual(len(times), 6, (label, times))
        for k, t in enumerate(times):               # each inside its slide
            self.assertTrue(15 * k <= t < 15 * (k + 1), (label, times))

    def test_operator_deck_keeps_every_slide(self):
        # the live blocker: after the first keep, 2-tick transitions used to
        # mask the whole slide for good -> 1 frame instead of 6
        for mode in ("text", "gradient"):
            for trans in (1, 2, 3):
                self.assert_one_per_slide(
                    run_keeps(self.deck(Screen(), trans, mode)), (mode, trans))

    def test_share_starting_with_live_strip_is_masked_after_the_burst(self):
        # a still window, then a share + live strip appear at once (a burst
        # that includes the strip): the strip's first 2 ticks are exempt,
        # then it is masked and the slide is kept, not suppressed forever
        s = Screen()
        frames = ([s.frame(5, strip="static") for _ in range(5)]
                  + [s.frame(0) for _ in range(10)])
        times = run_keeps(frames)
        self.assertEqual(times[0], 6.0)
        self.assertEqual(len(times), 2)
        self.assertTrue(15 < times[1] <= 30, times)

    def test_video_starting_after_a_still_picture_keeps_nothing_more(self):
        s = Screen()
        frames = ([s.frame(5, strip="static") for _ in range(5)]
                  + [s.frame(0, video=True) for _ in range(30)])
        self.assertEqual(run_keeps(frames), [6.0])

    def test_constant_strip_alone_keeps_only_the_first_frame(self):
        s = Screen()
        self.assertEqual(run_keeps([s.frame(0) for _ in range(40)]), [9.0])

    def test_while_volatile_the_strip_is_ignored(self):
        s = Screen()
        frames = [s.frame(0) for _ in range(30)]
        self.assertEqual(run_keeps(frames), [9.0])

    def test_min_gap_delays_keep(self):
        s = Screen()
        frames = [s.frame(0, strip="static")] * 2 + [s.frame(1, strip="static")] * 10
        p = dict(P, settle_ticks=1, min_gap_seconds=10)
        self.assertEqual(run_keeps(frames, p), [3.0, 15.0])

    def test_max_frames(self):
        frames = [mr.parse_ppm(ppm(24, 24, k * 20)) for k in range(10) for _ in range(5)]
        self.assertEqual(len(run_keeps(frames, dict(P, max_frames=3))), 3)
        self.assertEqual(len(run_keeps(frames)), 10)

    def test_missing_window_resets_settling(self):
        f = mr.parse_ppm(ppm(24, 24, 9))
        self.assertEqual(run_keeps([f, f, None, f, f, f]), [15.0])

    def test_pure(self):
        s = dict(mr.SCREEN_KEEP_START)
        mr.screen_keep_step(s, [0.0] * 144, None, 0, P)
        self.assertEqual(s, mr.SCREEN_KEEP_START)

    def test_loop_cost_is_small(self):
        # real thumbnails are ~250x140; two cell_diffs per tick every 3 s
        a = (250, 140, 255, random.Random(1).randbytes(250 * 140 * 3))
        b = (250, 140, 255, random.Random(2).randbytes(250 * 140 * 3))
        t0 = time.process_time()
        for _ in range(10):
            mr.cell_diffs(a, b)
        per_tick = (time.process_time() - t0) / 10 * 2
        self.assertLess(per_tick / 3.0, 0.05)       # < 5 % of a core


class TestScreenHelpers(unittest.TestCase):
    def test_file_names_sort_by_time(self):
        names = [mr.screen_file_name(t) for t in (5.04, 61.0, 123.4, 3725.25)]
        self.assertEqual(names[2], "00123.4s.jpg")
        self.assertEqual(names, sorted(names))

    def test_pick_window_prefers_meeting_title(self):
        clients = [
            {"class": "teams-for-linux", "title": "Chat | Microsoft Teams",
             "stableId": "a", "size": [2000, 1000]},
            {"class": "teams-for-linux", "title": "Weekly sync | Microsoft Teams",
             "stableId": "b", "size": [400, 300]},
            {"class": "firefox", "title": "Meeting notes", "stableId": "c"}]
        self.assertEqual(mr.pick_screen_window(clients, "teams-for-linux")["stableId"], "b")
        self.assertEqual(mr.pick_screen_window(clients[:1], "teams-for-linux")["stableId"], "a")
        self.assertIsNone(mr.pick_screen_window(clients, "zoom"))
        self.assertIsNone(mr.pick_screen_window([], "teams-for-linux"))
        unmapped = [dict(clients[0], mapped=False)]
        self.assertIsNone(mr.pick_screen_window(unmapped, "teams-for-linux"))

    def test_jpeg_size(self):
        sof = b"\xff\xc0\x00\x11\x08" + (720).to_bytes(2, "big") + (1280).to_bytes(2, "big")
        data = b"\xff\xd8" + b"\xff\xe0\x00\x04ab" + sof + b"\x00" * 12
        self.assertEqual(mr.jpeg_size(data), (1280, 720))
        self.assertEqual(mr.jpeg_size(b"nope"), (0, 0))

    def test_screen_config_defaults_and_bad_values(self):
        p = mr.screen_params(mr.load_config("/nonexistent/path.ini"))
        self.assertEqual((p["window_class"], p["interval_seconds"], p["settle_ticks"],
                          p["min_gap_seconds"], p["max_frames"], p["jpeg_quality"]),
                         ("teams-for-linux", 3.0, 2, 10.0, 300, 80))
        with tempfile.TemporaryDirectory() as d:
            cfg = mr.load_config(make_cfg(d, "[screens]\nenabled = maybe\n"
                                          "settle_ticks = lots\n"))
            self.assertTrue(mr.screens_enabled(cfg))
            self.assertEqual(mr.screen_params(cfg)["settle_ticks"], 2)


@contextlib.contextmanager
def collect_sandbox(d, cfg="[screens]\nmin_free_mb = 0\n"):
    """(raw, out) for collect_screens: raw is a staging dir under a temp
    CACHE_DIR, out a recording folder (meta.json); config is a temp file."""
    cache, out = os.path.join(d, "cache"), os.path.join(d, "out")
    os.makedirs(cache, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    if not os.path.exists(os.path.join(out, "meta.json")):
        Path(out, "meta.json").write_text("{}")
    with mock.patch.object(mr, "CACHE_DIR", cache), \
            mock.patch.object(mr, "CONFIG_FILE", make_cfg(d, cfg)):
        yield os.path.join(cache, "1700000000-abcdef"), out


class TestCollectScreens(unittest.TestCase):
    def test_moves_frames_and_skips_torn_or_missing(self):
        with tempfile.TemporaryDirectory() as d, collect_sandbox(d) as (raw, out):
            os.makedirs(os.path.join(raw, "screens"))
            for name in ("00012.0s.jpg", "00003.0s.jpg"):
                Path(raw, "screens", name).write_bytes(b"jpg")
            Path(raw, "screens.jsonl").write_text(
                '{"t": 12.0, "file": "00012.0s.jpg", "window_title": "B", "w": 1, "h": 1}\n'
                '{"t": 3.0, "file": "00003.0s.jpg", "window_title": "A", "w": 1, "h": 1}\n'
                '{"t": 20.0, "file": "00020.0s.jpg"}\n'          # image missing
                '{"t": 30.0, "fi')                               # torn write
            self.assertEqual(mr.collect_screens(raw, out), 2)
            frames = json.loads(Path(out, "screens.json").read_text())
            self.assertEqual([f["file"] for f in frames], ["00003.0s.jpg", "00012.0s.jpg"])
            self.assertEqual(sorted(os.listdir(os.path.join(out, "screens"))),
                             ["00003.0s.jpg", "00012.0s.jpg"])

    def test_no_frames_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(mr.collect_screens(os.path.join(d, "nope"), d), 0)
            self.assertEqual(os.listdir(d), [])

    def staged(self, raw, names=("00003.0s.jpg",)):
        make_frames(raw, names)
        Path(raw, "screens.jsonl").write_text("".join(
            json.dumps({"t": float(n[:7]), "file": n}) + "\n" for n in names))

    def test_unreadable_manifest_is_not_empty(self):
        # Codex: a PermissionError reading screens.jsonl made the collector
        # see "no frames" and rmtree the only copy
        with tempfile.TemporaryDirectory() as d, collect_sandbox(d) as (raw, out), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.staged(raw)
            real_open = open

            def deny(path, *a, **k):
                if str(path).endswith("screens.jsonl"):
                    raise PermissionError(13, "denied")
                return real_open(path, *a, **k)
            with mock.patch("builtins.open", deny):
                self.assertEqual(mr.collect_screens(raw, out), 0)
            self.assertTrue(os.path.isfile(os.path.join(raw, "screens", "00003.0s.jpg")))
            self.assertIn("denied", err.getvalue())
            self.assertEqual(mr.collect_screens(raw, out), 1)      # retry works
            self.assertFalse(os.path.exists(raw))

    def test_swapped_or_foreign_paths_delete_nothing(self):
        with tempfile.TemporaryDirectory() as d, collect_sandbox(d) as (raw, out), \
                contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            self.staged(raw)
            make_frames(out, ["00001.0s.jpg"])        # a collected recording
            Path(out, "screens.jsonl").write_text(     # even with a manifest
                json.dumps({"t": 1.0, "file": "00001.0s.jpg"}) + "\n")
            before = sorted(os.listdir(out))
            self.assertEqual(mr.collect_screens(out, raw), 0)        # swapped
            self.assertEqual(mr.cmd_collect_screens([out, raw]), 2)
            other = os.path.join(d, "elsewhere")
            self.staged(other)
            self.assertEqual(mr.collect_screens(other, out), 0)      # not staging
            self.assertEqual(sorted(os.listdir(out)), before)
            self.assertTrue(os.path.isdir(other) and os.path.isdir(raw))
            os.remove(os.path.join(out, "meta.json"))                # not a recording
            self.assertEqual(mr.collect_screens(raw, out), 0)
            self.assertTrue(os.path.isdir(raw))

    def test_leftovers_keep_staging(self):
        with tempfile.TemporaryDirectory() as d, collect_sandbox(d) as (raw, out), \
                contextlib.redirect_stderr(io.StringIO()):
            self.staged(raw)
            make_frames(raw, ["00009.0s.jpg"])          # frame not in manifest
            Path(raw, "mic.wav").write_bytes(b"RIFF")    # WAV not removed
            self.assertEqual(mr.collect_screens(raw, out), 1)
            self.assertTrue(os.path.isfile(os.path.join(raw, "mic.wav")))
            self.assertTrue(os.path.isfile(os.path.join(raw, "screens", "00009.0s.jpg")))

    def test_staging_without_frames_is_removed(self):
        # screens disabled / nothing kept: the WAVs are gone after the mix
        with tempfile.TemporaryDirectory() as d, collect_sandbox(d) as (raw, out):
            os.makedirs(raw)
            Path(raw, "screens.log").write_text("no window\n")
            self.assertEqual(mr.collect_screens(raw, out), 0)
            self.assertFalse(os.path.exists(raw))
            self.assertEqual(Path(out, "screens.log").read_text(), "no window\n")

    def test_output_reserve_stops_copying_and_keeps_staging(self):
        with tempfile.TemporaryDirectory() as d, \
                collect_sandbox(d, "[screens]\nmin_free_mb = 1e12\n") as (raw, out), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.staged(raw)
            self.assertEqual(mr.collect_screens(raw, out), 0)
            self.assertIn("nearly full", err.getvalue())
            self.assertTrue(os.path.isdir(raw))

    def test_absurd_manifest_time_is_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as d, collect_sandbox(d) as (raw, out):
            make_frames(raw, ["00003.0s.jpg"])
            Path(raw, "screens.jsonl").write_text(
                '{"t": 1' + "0" * 400 + ', "file": "00003.0s.jpg"}\n')
            self.assertEqual(mr.read_screens_jsonl(raw), [])


class TestMoments(unittest.TestCase):
    SEGS = [seg(0.0, 10.0, "intro"), seg(55.0, 59.0, "before window", "Me"),
            seg(60.0, 75.0, "overlaps start", "Others"),
            seg(100.0, 104.5, "right at the frame", "Me"),
            seg(119.9, 121.0, "just inside after"), seg(120.5, 130.0, "after window")]

    def test_window_is_30s_before_to_15s_after(self):
        got = [d["text"] for d in mr.moment_segments(self.SEGS, 105.0)]
        self.assertEqual(got, ["overlaps start", "right at the frame",
                               "just inside after"])

    def test_sorted_and_empty(self):
        segs = [seg(10, 11, "b"), seg(5, 6, "a")]
        self.assertEqual([d["text"] for d in mr.moment_segments(segs, 8)], ["a", "b"])
        self.assertEqual(mr.moment_segments([], 8), [])
        self.assertEqual(mr.moment_segments(segs, 500), [])

    def test_render(self):
        frames = [{"t": 105.0, "file": "00105.0s.jpg", "window_title": "Q4 review"},
                  {"t": 600.0, "file": "00600.0s.jpg", "window_title": ""}]
        md = mr.render_moments(frames, self.SEGS)
        self.assertIn("## 01:45 — Q4 review\n\n![screen 01:45](screens/00105.0s.jpg)\n", md)
        self.assertIn("- [01:40 -> 01:44] Me: right at the frame\n", md)
        self.assertIn("## 10:00 — Screen\n", md)
        self.assertIn("_No transcript lines around this moment._", md)

    def test_write_moments_only_with_frames(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(mr.write_moments(d, self.SEGS))
            Path(d, "screens.json").write_text(json.dumps(
                [{"t": 105.0, "file": "00105.0s.jpg", "window_title": "X"}]))
            self.assertIsNone(mr.write_moments(d, self.SEGS))   # image missing
            make_frames(d, ["00105.0s.jpg"])
            self.assertTrue(mr.write_moments(d, self.SEGS).endswith("moments.md"))


FRAMES = [{"t": 30.0, "file": "00030.0s.jpg"}, {"t": 100.0, "file": "00100.0s.jpg"},
          {"t": 200.0, "file": "00200.0s.jpg"}]


def make_frames(folder, names, sub="screens"):
    os.makedirs(os.path.join(folder, sub), exist_ok=True)
    for name in names:
        Path(folder, sub, name).write_bytes(b"\xff\xd8 jpeg " + name.encode())


class TestLinkScreens(unittest.TestCase):
    def test_pick_frame(self):
        self.assertEqual(mr.pick_frame(FRAMES, 110)["t"], 100.0)   # latest at/before
        self.assertEqual(mr.pick_frame(FRAMES, 100)["t"], 100.0)   # exactly at t
        self.assertEqual(mr.pick_frame(FRAMES, 190)["t"], 100.0)   # 90 s back, inclusive
        self.assertEqual(mr.pick_frame(FRAMES, 191)["t"], 200.0)   # else nearest after
        self.assertEqual(mr.pick_frame(FRAMES, 20)["t"], 30.0)
        self.assertIsNone(mr.pick_frame(FRAMES, 10))               # 20 s before first
        self.assertIsNone(mr.pick_frame([], 100))

    def test_links_and_idempotent(self):
        text = ("# Summary\n\nWe talked [01:00 -> 01:05] about it.\n\n"
                "- [ ] Move ticket to DevOps (Tanner) [01:50]\n"
                "- Decided to ship Friday [00:20]\n"
                "- Unrelated [55:00]\r\n"
                "- Long call [1:02:03]\n")
        frames = FRAMES + [{"t": 3700.0, "file": "03700.0s.jpg"}]
        once = mr.link_screens(text, frames)
        self.assertIn("(Tanner) [01:50] — [screen 01:40](screens/00100.0s.jpg)\n", once)
        self.assertIn("ship Friday [00:20] — [screen 00:30](screens/00030.0s.jpg)\n", once)
        self.assertIn("- Unrelated [55:00]\r\n", once)              # no frame in window
        self.assertIn("[1:02:03] — [screen 61:40](screens/03700.0s.jpg)", once)
        self.assertIn("We talked [01:00 -> 01:05] about it.\n", once)  # ranges ignored
        self.assertEqual(mr.link_screens(once, frames), once)

    def test_no_frames_is_noop(self):
        text = "- [ ] Something [01:50]\n"
        self.assertEqual(mr.link_screens(text, []), text)

    def test_cmd_link_screens(self):
        with tempfile.TemporaryDirectory() as d, \
                contextlib.redirect_stdout(io.StringIO()):
            Path(d, "summary.md").write_text("- [ ] Do it [01:45]\n")
            Path(d, "screens.json").write_text(json.dumps(FRAMES))
            make_frames(d, [f["file"] for f in FRAMES])
            self.assertEqual(mr.cmd_link_screens([d]), 0)
            self.assertEqual(mr.cmd_link_screens([d]), 0)
            self.assertEqual(Path(d, "summary.md").read_text(),
                             "- [ ] Do it [01:45] — [screen 01:40](screens/00100.0s.jpg)\n")


class TestLinkScreensTags(unittest.TestCase):
    def test_only_terminal_tags(self):
        text = ("- See [01:45](#time) in the doc\n"          # a link, not a tag
                "- Mid-line [01:45] mention only\n"
                "- Tag with full stop [01:45].\n"
                "- [01:45](screens/x) stays as is\n")
        out = mr.link_screens(text, FRAMES)
        lines = out.splitlines()
        self.assertEqual(lines[0], "- See [01:45](#time) in the doc")
        self.assertEqual(lines[1], "- Mid-line [01:45] mention only")
        self.assertEqual(lines[2], "- Tag with full stop [01:45]. — "
                                   "[screen 01:40](screens/00100.0s.jpg)")
        self.assertEqual(lines[3], "- [01:45](screens/x) stays as is")
        self.assertEqual(mr.link_screens(out, FRAMES), out)

    def test_read_frames_filters_bad_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "screens.json").write_text(json.dumps([
                {"t": 1.0, "file": "00001.0s.jpg"},             # exists
                {"t": 2.0, "file": "00002.0s.jpg"},             # missing
                {"t": 3.0, "file": "../../etc/passwd"},
                {"t": 4.0, "file": "00004.0s.png"},
                {"t": float("nan"), "file": "00005.0s.jpg"},
                {"t": True, "file": "00006.0s.jpg"},
                {"t": "7", "file": "00007.0s.jpg"}]))
            make_frames(d, ["00001.0s.jpg", "00004.0s.png", "00005.0s.jpg",
                            "00006.0s.jpg", "00007.0s.jpg"])
            self.assertEqual([f["file"] for f in mr.read_frames(d)], ["00001.0s.jpg"])


class StateDirMixin:
    """Points the module's state/config paths at a temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        state = os.path.join(self.d, "state")
        self._saved = {k: getattr(mr, k) for k in (
            "STATE_DIR", "STATE_FILE", "LOCK_FILE", "LAST_FILE", "JOBS_DIR",
            "WATCH_FILE", "CONFIG_FILE")}
        mr.STATE_DIR = state
        mr.STATE_FILE = os.path.join(state, "session.json")
        mr.LOCK_FILE = os.path.join(state, "lock")
        mr.LAST_FILE = os.path.join(state, "last.json")
        mr.JOBS_DIR = os.path.join(state, "transcribing")
        mr.WATCH_FILE = os.path.join(state, "watch.json")
        mr.CONFIG_FILE = make_cfg(self.d, "[whisper]\nmodel = medium\n")
        self.children = []
        self._out = contextlib.ExitStack()
        self._out.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.err = self._out.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def tearDown(self):
        self._out.close()
        for p in self.children:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, 9)
                except OSError:
                    p.kill()
            p.wait()
        for k, v in self._saved.items():
            setattr(mr, k, v)
        self._tmp.cleanup()

    def child(self, cmd="sleep 60", group=False):
        p = subprocess.Popen(["sh", "-c", cmd], start_new_session=group)
        self.children.append(p)
        time.sleep(0.05)
        return p, mr.proc_start(p.pid)


class TestProcessIdentity(StateDirMixin, unittest.TestCase):
    def test_zombie_and_reuse_are_not_alive(self):
        p, start = self.child("exit 0")
        deadline = time.time() + 5
        while mr.pid_alive(p.pid) and time.time() < deadline:
            time.sleep(0.02)
        self.assertFalse(mr.pid_alive(p.pid))     # a zombie (not reaped yet)
        q, qstart = self.child()
        self.assertTrue(mr.pid_alive(q.pid, qstart))
        self.assertFalse(mr.pid_alive(q.pid, qstart + 1))   # "reused" pid

    def test_wrong_identity_is_never_signalled(self):
        p, start = self.child()
        mr.stop_procs([(p.pid, start + 1)], timeout=0.3)
        self.assertIsNone(p.poll())
        mr.stop_procs([(p.pid, start)], timeout=2)
        self.assertIsNotNone(p.wait(timeout=2))

    def test_stubborn_worker_is_killed_and_its_group_never_signalled(self):
        # a worker that ignores SIGTERM, plus another process in its group:
        # stop SIGKILLs the verified worker only -- no group enumeration
        # (a reused group id must never authorize a signal; fix round 5)
        pidfile = os.path.join(self.d, "member.pid")
        p, start = self.child(f"trap '' TERM; sleep 60 & echo $! > {pidfile}; wait",
                              group=True)
        time.sleep(0.1)
        member = int(Path(pidfile).read_text())
        self.addCleanup(lambda: mr.pid_alive(member) and os.kill(member, 9))
        t0 = time.monotonic()
        mr.stop_procs([], (p.pid, start), timeout=0.5)
        self.assertLess(time.monotonic() - t0, 3)
        self.assertIsNotNone(p.wait(timeout=2))
        self.assertTrue(mr.pid_alive(member))     # not ours to signal by group
        self.assertFalse(hasattr(mr, "group_members"))

    def test_worker_without_saved_start_is_never_signalled(self):
        # Codex (round 5): pid_alive(pid, None) let a bare (possibly
        # reused) pid be SIGTERMed / SIGKILLed
        p, start = self.child()
        mr.stop_procs([], (p.pid, None), timeout=0.3)
        self.assertIsNone(p.poll())
        self.assertIn("not signalling screens worker", self.err.getvalue())
        mr.write_state({"id": "x", "pids": [], "screens_pid": p.pid})
        with mock.patch.object(mr, "finish_session", lambda *a, **k: 0):
            mr.cmd_stop()
        self.assertIsNone(p.poll())

    def test_reused_worker_pid_is_not_signalled(self):
        p, start = self.child(group=True)
        mr.stop_procs([], (p.pid, start + 1), timeout=0.3)
        self.assertIsNone(p.poll())

    def test_stop_survives_signal_errors_and_still_finalizes(self):
        p, start = self.child()
        mr.write_state({"id": "x", "pids": [p.pid],
                        "pid_starts": {str(p.pid): start},
                        "screens_pid": p.pid, "screens_start": start})
        calls = []
        with mock.patch.object(os, "killpg", side_effect=PermissionError(1, "no")), \
                mock.patch.object(mr.signal, "pidfd_send_signal",
                                  side_effect=PermissionError(1, "no")), \
                mock.patch.object(mr, "finish_session",
                                  lambda st, cfg, **kw: calls.append(st["id"]) or 0):
            self.assertEqual(mr.cmd_stop(), 0)
        self.assertEqual(calls, ["x"])
        self.assertIsNone(mr.read_state())
        self.assertIn("cannot signal", self.err.getvalue())

    def test_watcher_recovers_dead_session_and_ends_worker(self):
        rec, rstart = self.child("exit 0")
        rec.wait()
        worker, wstart = self.child(group=True)
        mr.write_state({"id": "dead", "pids": [rec.pid],
                        "pid_starts": {str(rec.pid): rstart},
                        "screens_pid": worker.pid, "screens_start": wstart})
        calls = []
        with mock.patch.object(mr, "finish_session",
                               lambda st, cfg, **kw: calls.append((st["id"], kw))):
            mr.recover_if_stale()
        self.assertEqual(calls, [("dead", {"recovered": True})])
        self.assertIsNone(mr.read_state())
        self.assertIsNotNone(worker.wait(timeout=3))

    def test_live_session_is_not_recovered(self):
        p, start = self.child()
        mr.write_state({"id": "live", "pids": [p.pid],
                        "pid_starts": {str(p.pid): start}})
        with mock.patch.object(mr, "finish_session", side_effect=AssertionError):
            mr.recover_if_stale()
        self.assertEqual(mr.read_state()["id"], "live")

    def test_screens_session_ok(self):
        raw = os.path.join(self.d, "raw")
        os.mkdir(raw)
        p, start = self.child()
        good = {"id": "s1", "session_dir": raw, "pids": [p.pid],
                "pid_starts": {str(p.pid): start}}
        mr.write_state(good)
        self.assertTrue(mr.screens_session_ok(raw, "s1"))
        self.assertFalse(mr.screens_session_ok(raw, "s2"))        # newer session
        mr.write_state(dict(good, pid_starts={str(p.pid): start + 1}))
        self.assertFalse(mr.screens_session_ok(raw, "s1"))        # recorders gone
        mr.clear_state()
        self.assertFalse(mr.screens_session_ok(raw, "s1"))

    def test_state_is_private_and_atomic(self):
        old = os.umask(0)
        try:
            mr.write_state({"id": "x"})
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(mr.STATE_FILE).st_mode), 0o600)
        Path(mr.STATE_FILE).write_text('{"id": "x", "pi')       # torn
        self.assertIsNone(mr.read_state())


class TestLifecycleLock(StateDirMixin, unittest.TestCase):
    def test_exclusive_across_processes_and_reentrant(self):
        with mr.lifecycle_lock():
            with mr.lifecycle_lock():                 # re-entrant in-process
                code = ("import fcntl, os, sys\n"
                        "fd = os.open(sys.argv[1], os.O_RDWR)\n"
                        "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                        "except BlockingIOError:\n    sys.exit(7)\n")
                rc = subprocess.run([sys.executable, "-I", "-c", code,
                                     mr.LOCK_FILE]).returncode
                self.assertEqual(rc, 7)
        rc = subprocess.run([sys.executable, "-I", "-c", code, mr.LOCK_FILE]).returncode
        self.assertEqual(rc, 0)

    def test_times_out(self):
        os.makedirs(mr.STATE_DIR, exist_ok=True)
        holder = subprocess.Popen([sys.executable, "-I", "-c",
                                   "import fcntl, os, sys, time\n"
                                   "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
                                   "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                                   "print('held', flush=True)\ntime.sleep(30)\n",
                                   mr.LOCK_FILE], stdout=subprocess.PIPE)
        self.children.append(holder)
        holder.stdout.readline()
        with self.assertRaises(TimeoutError):
            with mr.lifecycle_lock(timeout=0.3):
                pass
        with mock.patch.object(mr, "LOCK_TIMEOUT", 0.3):
            self.assertEqual(mr.cmd_stop(), mr.BUSY)  # reports busy, no crash
        self.assertIn("busy", self.err.getvalue())
        holder.kill()
        holder.stdout.close()

    def test_start_publishes_one_complete_state_under_the_lock(self):
        real_popen = subprocess.Popen
        mr.CACHE_DIR, saved_cache = os.path.join(self.d, "cache"), mr.CACHE_DIR
        self.addCleanup(setattr, mr, "CACHE_DIR", saved_cache)
        seen = {}

        def fake_popen(argv, **kw):
            if argv[0] == "pw-record":           # stand-in recorder
                p = real_popen(["sleep", "60"], **kw)
                self.children.append(p)
                return p
            return real_popen(argv, **kw)

        def fake_spawn(sess_dir, session_id, origin):
            seen["state_during_spawn"] = mr.read_state()
            seen["args"] = (sess_dir, session_id, origin)
            seen["mode"] = stat.S_IMODE(os.stat(sess_dir).st_mode)
            code = ("import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR)\n"
                    "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                    "except BlockingIOError:\n    sys.exit(7)\n")
            seen["lock_rc"] = subprocess.run(
                [sys.executable, "-I", "-c", code, mr.LOCK_FILE]).returncode
            w, start = self.child(group=True)
            return w.pid, start

        writes = []
        real_write = mr.write_state
        with mock.patch.object(mr.subprocess, "Popen", fake_popen), \
                mock.patch.object(mr.subprocess, "check_output",
                                  lambda *a, **k: "default\n"), \
                mock.patch.object(mr, "run_json", lambda *a, **k: []), \
                mock.patch.object(mr, "notify", lambda *a: None), \
                mock.patch.object(mr, "spawn_screens", fake_spawn), \
                mock.patch.object(mr, "write_state",
                                  lambda s: writes.append(dict(s)) or real_write(s)):
            self.assertEqual(mr.cmd_start(), 0)
        self.assertIsNone(seen["state_during_spawn"])     # nothing half-published
        self.assertEqual(seen["lock_rc"], 7)              # a stop would have waited
        self.assertEqual(seen["mode"], 0o700)
        self.assertEqual(len(writes), 1)
        st = writes[0]
        self.assertEqual((st["session_dir"], st["id"], st["origin"]), seen["args"])
        self.assertTrue(st["id"] in st["session_dir"])
        self.assertTrue(mr.session_live(st))
        self.assertIsInstance(st["screens_pid"], int)
        self.assertEqual(mr.cmd_start(), 1)               # already recording

    def lock_is_free(self):
        code = ("import fcntl, os, sys\nfd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
                "try:\n    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "except BlockingIOError:\n    sys.exit(7)\n")
        return subprocess.run([sys.executable, "-I", "-c", code,
                               mr.LOCK_FILE]).returncode == 0

    def test_watcher_never_stops_a_replacement_session(self):
        # Codex: the watcher decided to stop Teams session A outside the
        # lock; meanwhile a manual session B replaced it
        p, start = self.child()
        mr.write_state({"id": "B", "source": "manual", "pids": [p.pid],
                        "pid_starts": {str(p.pid): start}})
        with mock.patch.object(mr, "finish_session", side_effect=AssertionError):
            self.assertEqual(mr.cmd_stop(expect_id="A", expect_source="teams"), 1)
        self.assertEqual(mr.read_state()["id"], "B")
        self.assertIsNone(p.poll())
        mr.write_state({"id": "A", "source": "manual", "pids": [p.pid],
                        "pid_starts": {str(p.pid): start}})
        with mock.patch.object(mr, "finish_session", side_effect=AssertionError):
            self.assertEqual(mr.cmd_stop(expect_id="A", expect_source="teams"), 1)
        self.assertIsNone(p.poll())                    # same id, other source

    def test_stop_and_toggle_mix_with_the_lock_released(self):
        for cmd in (mr.cmd_stop, mr.cmd_toggle):
            p, start = self.child()
            mr.write_state({"id": "x", "pids": [p.pid],
                            "pid_starts": {str(p.pid): start}})
            seen = []
            with mock.patch.object(mr, "finish_session",
                                   lambda st, cfg, **kw: seen.append(
                                       self.lock_is_free()) or 0):
                self.assertEqual(cmd(), 0)
            self.assertEqual(seen, [True], cmd.__name__)

    def test_recovery_mixes_with_the_lock_released(self):
        rec, rstart = self.child("exit 0")
        rec.wait()
        mr.write_state({"id": "dead", "pids": [rec.pid],
                        "pid_starts": {str(rec.pid): rstart}})
        seen = []
        with mock.patch.object(mr, "finish_session",
                               lambda st, cfg, **kw: seen.append(self.lock_is_free())):
            mr.recover_if_stale()
        self.assertEqual(seen, [True])

    def start_env(self, spawn=None, cfg_body=None, sources=(), argvs=None,
                  die=()):
        """Patches for cmd_start: fake pw-record (sleep) and pactl; returns
        the list of spawned recorder Popens. sources: `pactl list sources`;
        argvs collects the pw-record argvs; die: output basenames whose
        recorder exits at once."""
        real_popen = subprocess.Popen
        mr.CACHE_DIR, saved_cache = os.path.join(self.d, "cache"), mr.CACHE_DIR
        self.addCleanup(setattr, mr, "CACHE_DIR", saved_cache)
        if cfg_body is not None:
            Path(mr.CONFIG_FILE).write_text(cfg_body)
        spawned = []

        def fake_popen(argv, **kw):
            if argv[0] == "pw-record":
                if argvs is not None:
                    argvs.append(argv)
                dies = os.path.basename(argv[-1]) in die
                p = real_popen(["true"] if dies else ["sleep", "60"], **kw)
                self.children.append(p)
                spawned.append(p)
                return p
            return real_popen(argv, **kw)

        def fake_run_json(argv, *a, **k):
            return list(sources) if argv[-1] == "sources" else []
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(mr.subprocess, "Popen", fake_popen))
        stack.enter_context(mock.patch.object(mr.subprocess, "check_output",
                                              lambda *a, **k: "default\n"))
        stack.enter_context(mock.patch.object(mr, "run_json", fake_run_json))
        stack.enter_context(mock.patch.object(mr, "notify", lambda *a: None))
        if spawn is not None:
            stack.enter_context(mock.patch.object(mr, "spawn_screens", spawn))
        self.addCleanup(stack.close)
        return spawned

    def test_bad_config_fails_start_before_any_spawn(self):
        spawned = self.start_env(cfg_body="[audio]\nsample_rate = lots\n")
        self.assertEqual(mr.cmd_start(), 1)
        self.assertEqual((spawned, mr.read_state()), ([], None))

    def test_malformed_screens_enabled_does_not_strand_recorders(self):
        # Codex: `[screens] enabled = %` raised after both recorders spawned
        spawned = self.start_env(spawn=lambda *a: None,
                                 cfg_body="[screens]\nenabled = %\n")
        self.assertEqual(mr.cmd_start(), 0)
        self.assertEqual(len(spawned), 2)
        self.assertTrue(mr.session_live(mr.read_state()))

    # check_output answers "default" for both get-default-sink/-source
    MIC = {"name": "default", "monitor_of_sink": "n/a", "properties": {}}
    AUTO = "[audio]\nmic_source = auto\n"

    def aec_env(self, graph=("ready", "linked"), cfg_body=AUTO, **kw):
        """start_env plus a fake AEC helper (`pipewire -c`): records its
        argv/env and scripts wait_aec_graph's answers. Default config
        opts in to AEC (mic_source = auto)."""
        helpers, waits = [], list(graph)
        spawned = self.start_env(cfg_body=cfg_body, **kw)
        fake_popen = mr.subprocess.Popen

        def popen(argv, **k):
            if argv[0] == "pipewire":
                helpers.append((argv, k.get("env"),
                                Path(argv[2]).read_text()))
                p = REAL_POPEN(["sleep", "60"], **k)
                self.children.append(p)
                spawned.append(p)
                return p
            return fake_popen(argv, **k)
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(mr.subprocess, "Popen", popen))
        stack.enter_context(mock.patch.object(
            mr, "wait_aec_graph",
            lambda *a, **k: waits.pop(0) if waits else "missing"))
        self.addCleanup(stack.close)
        return spawned, helpers

    def test_primaries_start_first_then_optional_aec(self):
        argvs = []
        spawned, helpers = self.aec_env(spawn=lambda *a: None,
                                        sources=[self.MIC], argvs=argvs)
        self.assertEqual(mr.cmd_start(), 0)
        st = mr.read_state()
        names = mr.aec_names(st["id"])
        self.assertTrue(st["mic_echo_cancel"])
        self.assertEqual(len(spawned), 4)        # 2 primaries + helper + AEC rec
        self.assertEqual(len(st["pids"]), 3)
        # Codex round 7: the primaries never wait for the canceller. They
        # are the first two processes, exactly the pre-AEC recorders.
        sys_argv, raw_argv, aec_argv = argvs
        self.assertIn("stream.capture.sink", " ".join(sys_argv))
        self.assertEqual(raw_argv[-1], st["mic_wav"])
        self.assertNotIn("--target", raw_argv)    # follows the default mic
        self.assertEqual(spawned[2].pid, st["aec_pid"])   # helper after them
        # the canceller: its own process, our generated config, clean env
        (argv, env, conf), = helpers
        self.assertEqual(argv, ["pipewire", "-c",
                                os.path.join(st["session_dir"], "aec.conf")])
        self.assertNotIn("PIPEWIRE_CONFIG_NAME", env)
        self.assertIn('target.object = "default"', conf)
        self.assertIsNotNone(st["aec_start"])
        self.assertNotIn(st["aec_pid"], st["pids"])
        self.assertGreaterEqual(st["aec_delay"], 0.0)
        self.assertEqual((st["aec_mic"], st["aec_follows_default"]),
                         ("default", True))
        self.assertEqual(aec_argv[-3:], ["--target", names["mic"],
                                         st["mic_aec_wav"]])
        self.assertIn("node.dont-fallback = true", " ".join(aec_argv))
        self.assertIn(names["recorder"], " ".join(aec_argv))
        # stop: recorders first, then the helper
        order = []
        real_send = mr.send_signal
        with mock.patch.object(mr, "send_signal",
                               lambda p, s: order.append((p[0], s))
                               or real_send(p, s)):
            mr.stop_procs(mr.state_procs(st), None, timeout=2,
                          helpers=[mr.aec_proc(st)])
        self.assertEqual(order[-1], (st["aec_pid"], signal.SIGTERM))
        self.assertTrue(all(s == signal.SIGINT for _, s in order[:-1]))
        self.assertFalse(mr.pid_alive(*mr.aec_proc(st)))

    def test_aec_budget_bounds_the_whole_setup(self):
        # one deadline for node + link waits, and each pw-dump is bounded
        # by the time left (Codex: 4 s + 3 s waits, 5 s probes)
        seen = []

        def slow_dump(argv, default, env=None, timeout=5.0):
            seen.append(timeout)
            time.sleep(0.05)
            return []
        p = mock.Mock()
        p.poll.return_value = None
        with mock.patch.object(mr, "run_json", slow_dump):
            t0 = time.monotonic()
            st = mr.wait_aec_graph(mr.aec_names("x"), {"ready"},
                                   time.monotonic() + 0.4, [p])
        self.assertEqual(st, "missing")
        self.assertLess(time.monotonic() - t0, 0.7)
        self.assertTrue(all(t <= 0.4 for t in seen))
        self.assertLessEqual(mr.AEC_BUDGET, 3.0)

    def test_aec_unavailable_records_raw_like_before(self):
        for graph in (("missing",), ("unsafe",), ("exited",),
                      ("ready", "ready"), ("ready", "unsafe")):
            argvs = []
            mr.clear_state()
            spawned, helpers = self.aec_env(graph=graph, spawn=lambda *a: None,
                                            sources=[self.MIC], argvs=argvs)
            self.assertEqual(mr.cmd_start(), 0, graph)
            st = mr.read_state()
            self.assertFalse(st["mic_echo_cancel"], graph)
            self.assertNotIn("mic_aec_wav", st)
            self.assertNotIn("aec_pid", st)
            self.assertEqual(len(st["pids"]), 2, graph)
            self.assertIn("echo canceller not used", st["mic_note"])
            sys_argv, raw_argv = argvs[:2]
            self.assertNotIn("--target", raw_argv)    # follows the default
            self.assertEqual(raw_argv[-1], st["mic_wav"])
            # the primaries run on; everything the AEC attempt started is gone
            for p in spawned[:2]:
                self.assertIsNone(p.poll())
            for p in spawned[2:]:
                self.assertIsNotNone(p.wait(timeout=3))
            self.assertFalse(os.path.exists(os.path.join(st["session_dir"],
                                                         "mic-aec.wav")))
            mr.stop_procs(mr.state_procs(st), None, timeout=1)

    def test_a_dead_primary_keeps_the_other_and_skips_aec_without_mic(self):
        # Codex round 7: a raw-recorder failure discarded usable capture
        for die, alive in (("mic.wav", "sys.wav"), ("sys.wav", "mic.wav")):
            argvs = []
            mr.clear_state()
            spawned, helpers = self.aec_env(spawn=lambda *a: None,
                                            sources=[self.MIC], argvs=argvs,
                                            die=(die,))
            self.assertEqual(mr.cmd_start(), 0, die)
            st = mr.read_state()
            self.assertTrue(mr.session_live(st))
            self.assertIn("failed to start", st["start_note"])
            # no raw mic -> no AEC either (it needs the raw track)
            self.assertEqual(bool(helpers), die == "sys.wav", die)
            self.assertEqual(len(st["pids"]), 1 + bool(helpers))
            mr.stop_procs(mr.state_procs(st), None, timeout=1,
                          helpers=[mr.aec_proc(st)])

    def test_both_primaries_dead_is_a_retry_not_handled(self):
        spawned, helpers = self.aec_env(spawn=lambda *a: None,
                                        sources=[self.MIC],
                                        die=("mic.wav", "sys.wav"))
        notes = []
        with mock.patch.object(mr, "notify", lambda *a: notes.append(a)):
            self.assertEqual(mr.cmd_start(source="teams"), mr.RETRY)
        self.assertEqual(helpers, [])
        self.assertIsNone(mr.read_state())
        self.assertEqual(os.listdir(mr.CACHE_DIR), [])
        self.assertEqual(notes, [])            # the watcher notifies at the end
        # the watcher keeps retrying (bounded) instead of marking it handled
        real_start, real_sleep = mr.cmd_start, time.sleep

        def start_with_real_sleep(**kw):     # watch_polls patches sleep
            with mock.patch.object(mr.time, "sleep", real_sleep):
                return real_start(**kw)
        start, notes = self.watch_polls(15, start_with_real_sleep)
        self.assertEqual(start.call_count, mr.START_RETRIES)
        self.assertEqual(len(notes), 1)

    def test_route_divergence_at_stop_and_from_the_watcher(self):
        mr.write_state({"id": "s", "pids": [], "mic_echo_cancel": True,
                        "aec_mic": "default", "aec_follows_default": True})
        with mock.patch.object(mr, "probe_aec_route",
                               lambda st, apps: "call mic is now usb"), \
                mock.patch.object(mr, "finish_session",
                                  lambda st, cfg, **kw: self.assertEqual(
                                      st["aec_diverged"],
                                      "call mic is now usb") or 0):
            self.assertEqual(mr.cmd_stop(), 0)
        # watcher: periodic check marks only the same session
        mr.write_state({"id": "s2", "pids": [], "mic_echo_cancel": True})
        mr.mark_aec_diverged("other", "x")
        self.assertNotIn("aec_diverged", mr.read_state())
        mr.mark_aec_diverged("s2", "call app now plays to bt")
        self.assertEqual(mr.read_state()["aec_diverged"],
                         "call app now plays to bt")
        mr.mark_aec_diverged("s2", "later reason")        # first one wins
        self.assertEqual(mr.read_state()["aec_diverged"],
                         "call app now plays to bt")

    def test_probe_aec_route_reads_pactl_bounded(self):
        calls = []

        def check_output(argv, **k):
            calls.append(k["timeout"])
            return {"get-default-sink": "spk", "get-default-source": "usb"
                    }[argv[1]] + "\n"
        with mock.patch.object(mr.subprocess, "check_output", check_output), \
                mock.patch.object(mr, "run_json",
                                  lambda argv, d, timeout=5: calls.append(
                                      timeout) or []):
            why = mr.probe_aec_route({"aec_mic": "webcam",
                                      "aec_follows_default": True}, ["teams"])
        self.assertIn("call mic is now usb", why)
        self.assertTrue(all(t <= 2.0 for t in calls))
        with mock.patch.object(mr.subprocess, "check_output",
                               side_effect=subprocess.TimeoutExpired("p", 2)), \
                mock.patch.object(mr, "run_json", lambda *a, **k: []):
            self.assertIn("cannot read", mr.probe_aec_route({}, ["teams"]))

    def test_no_aec_for_raw_setting_pinned_sink_or_no_real_mic(self):
        cases = (([self.MIC], "[audio]\nmic_source = raw\n", None),
                 ([self.MIC], "", None),             # default is raw now
                 ([], self.AUTO, None),
                 ([dict(self.MIC, monitor_of_sink="spk")], self.AUTO, None),
                 ([self.MIC], self.AUTO, "bluez_output.hs"))
        for sources, body, pin_sink in cases:
            argvs = []
            mr.clear_state()
            spawned, helpers = self.aec_env(spawn=lambda *a: None,
                                            sources=sources, argvs=argvs,
                                            cfg_body=body)
            with mock.patch.object(mr, "app_device",
                                   lambda s, d, a, key, default:
                                   pin_sink if key == "sink" and pin_sink
                                   else ""):
                self.assertEqual(mr.cmd_start(), 0)
            st = mr.read_state()
            self.assertEqual(helpers, [])               # never started
            self.assertFalse(st["mic_echo_cancel"])
            self.assertEqual(len(argvs), 2)
            self.assertNotIn("--target", argvs[1])
            mr.stop_procs(mr.state_procs(st), None, timeout=1)

    def test_explicit_mic_source_is_recorded(self):
        argvs = []
        _, helpers = self.aec_env(spawn=lambda *a: None, argvs=argvs,
                                  sources=[self.MIC, {"name": "usb.mic"}],
                                  cfg_body="[audio]\nmic_source = usb.mic\n")
        self.assertEqual(mr.cmd_start(), 0)
        st = mr.read_state()
        self.assertEqual(helpers, [])
        self.assertEqual(argvs[1][-3:], ["--target", "usb.mic", st["mic_wav"]])
        mr.stop_procs(mr.state_procs(st), None, timeout=1)

    def test_rollback_stops_aec_helper(self):
        def bad_spawn(*a):
            raise RuntimeError("boom")
        spawned, helpers = self.aec_env(spawn=bad_spawn, sources=[self.MIC])
        with mock.patch.object(mr, "stop_helpers",
                               wraps=mr.stop_helpers) as sh:
            self.assertEqual(mr.cmd_start(), 1)
        self.assertEqual(len(helpers), 1)
        self.assertEqual(len(sh.call_args.args[0]), 1)
        for p in spawned:
            self.assertIsNotNone(p.wait(timeout=3))
        self.assertIsNone(mr.read_state())
        self.assertEqual(os.listdir(mr.CACHE_DIR), [])

    def test_failure_after_spawning_leaves_no_orphan(self):
        for exc in (RuntimeError("boom"), KeyboardInterrupt()):
            def bad_spawn(*a, exc=exc):
                raise exc
            spawned = self.start_env(spawn=bad_spawn)
            if isinstance(exc, KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    mr.cmd_start()
            else:
                self.assertEqual(mr.cmd_start(), 1)
            self.assertEqual(len(spawned), 2)
            for p in spawned:
                self.assertIsNotNone(p.wait(timeout=3))
            self.assertIsNone(mr.read_state())
            self.assertEqual(os.listdir(mr.CACHE_DIR), [])
            self.assertTrue(self.lock_is_free())

    def test_watcher_retries_a_busy_start(self):
        Path(mr.CONFIG_FILE).write_text(
            "[auto]\nstart_after_seconds = 0\npoll_seconds = 0.5\n")
        rcs, ticks = [mr.BUSY, 0], []

        def fake_start(**kw):
            return rcs.pop(0) if rcs else 0

        class Done(Exception):
            pass

        def fake_sleep(s):
            ticks.append(s)
            if len(ticks) >= 3:
                raise Done
        with mock.patch.object(mr, "call_active", lambda cfg: True), \
                mock.patch.object(mr, "cmd_start", side_effect=fake_start) as start, \
                mock.patch.object(mr.time, "sleep", fake_sleep):
            with self.assertRaises(Done):
                mr.cmd_watch()
        self.assertEqual(start.call_count, 2)          # busy -> tried again

    def watch_polls(self, n, start_side_effect):
        """Run cmd_watch for n in-call polls with cmd_start mocked."""
        Path(mr.CONFIG_FILE).write_text(
            "[auto]\nstart_after_seconds = 0\npoll_seconds = 0.5\n")
        ticks, notes = [], []

        class Done(Exception):
            pass

        def fake_sleep(s):
            ticks.append(s)
            if len(ticks) >= n:
                raise Done
        with mock.patch.object(mr, "call_active", lambda cfg: True), \
                mock.patch.object(mr, "cmd_start",
                                  side_effect=start_side_effect) as start, \
                mock.patch.object(mr, "notify", lambda *a: notes.append(a)), \
                mock.patch.object(mr.time, "sleep", fake_sleep):
            with self.assertRaises(Done):
                mr.cmd_watch()
        return start, notes

    def test_watcher_retries_a_timed_out_probe(self):
        # Codex: one pactl timeout made start return 1 and the watcher
        # skipped the rest of the call. The real start is used: the first
        # probe times out, the second attempt records.
        self.start_env(spawn=lambda *a: None)
        probes = [subprocess.TimeoutExpired(["pactl"], 5)]

        def check_output(*a, **k):
            if probes:
                raise probes.pop(0)
            return "default\n"
        real_start, rcs = mr.cmd_start, []
        with mock.patch.object(mr.subprocess, "check_output", check_output):
            start, notes = self.watch_polls(
                3, lambda **kw: rcs.append(real_start(**kw)) or rcs[-1])
        self.assertEqual(rcs, [mr.RETRY, 0])          # retried, then recorded
        self.assertEqual(start.call_count, 2)         # live -> no third start
        self.assertTrue(mr.session_live(mr.read_state()))
        self.assertFalse([n for n in notes if "could not" in n[0]])

    def test_watcher_gives_up_after_bounded_retries(self):
        start, notes = self.watch_polls(15, lambda **kw: mr.RETRY)
        self.assertEqual(start.call_count, mr.START_RETRIES)
        self.assertEqual(len(notes), 1)
        self.assertIn("could not start", notes[0][0])

    def test_failed_start_still_finalizes_the_recovered_session(self):
        # Codex: recovery cleared the crashed session, then the new staging
        # dir failed (ENOSPC) and the old recording was never mixed
        for cmd in (mr.cmd_start, mr.cmd_toggle):
            self.start_env()
            rec, rstart = self.child("exit 0")
            rec.wait()
            mr.write_state({"id": "old", "pids": [rec.pid],
                            "pid_starts": {str(rec.pid): rstart}})
            calls = []
            with mock.patch.object(mr, "new_session_dir",
                                   side_effect=OSError(errno.ENOSPC, "full")), \
                    mock.patch.object(mr, "finish_session",
                                      lambda st, cfg, **kw: calls.append(
                                          (st["id"], kw)) or 0):
                self.assertEqual(cmd(), 1, cmd.__name__)
            self.assertEqual(calls, [("old", {"recovered": True})], cmd.__name__)
            self.assertIsNone(mr.read_state())
            # and when the new start raises instead of failing cleanly
            mr.write_state({"id": "old2", "pids": [rec.pid],
                            "pid_starts": {str(rec.pid): rstart}})
            calls.clear()
            with mock.patch.object(mr, "start_settings",
                                   side_effect=KeyboardInterrupt), \
                    mock.patch.object(mr, "finish_session",
                                      lambda st, cfg, **kw: calls.append(st["id"])):
                with self.assertRaises(KeyboardInterrupt):
                    cmd()
            self.assertEqual(calls, ["old2"], cmd.__name__)

    def test_start_never_adopts_or_deletes_an_existing_dir(self):
        # Codex (round 5): staging accepted an existing path and a failed
        # start rmtree()d it. Creation is exclusive: a taken name is
        # skipped, and only the dir this attempt made is rolled back.
        self.start_env(die=("sys.wav",))
        os.makedirs(mr.CACHE_DIR)
        taken = os.path.join(mr.CACHE_DIR, "100-000001")
        os.mkdir(taken)
        Path(taken, "keep").write_text("not ours")
        with mock.patch.object(mr.time, "time", lambda: 100.0), \
                mock.patch.object(mr.os, "urandom",
                                  side_effect=[b"\0\0\1", b"\0\0\2"]):
            self.assertEqual(mr.new_session_dir(),
                             ("100-000002", os.path.join(mr.CACHE_DIR, "100-000002")))
        with mock.patch.object(mr.os, "urandom", side_effect=[b"\0\0\1"] * 5), \
                mock.patch.object(mr.time, "time", lambda: 100.0):
            self.assertRaises(FileExistsError, mr.new_session_dir)
        with mock.patch.object(mr.os, "urandom", side_effect=[b"\0\0\1"] * 5), \
                mock.patch.object(mr.time, "time", lambda: 100.0):
            self.assertEqual(mr.cmd_start(), 1)       # no free name: fails
        self.assertEqual(Path(taken, "keep").read_text(), "not ours")
        # speaker recorder dies: the mic alone is recorded (fix round 8)
        self.assertEqual(mr.cmd_start(), 0)
        st = mr.read_state()
        self.assertEqual(st["start_note"], "speaker recorder failed to start")
        mr.stop_procs(mr.state_procs(st), None, timeout=1)
        mr.clear_state()
        # both die: RETRY, and only the dir this attempt made is removed
        self.start_env(die=("sys.wav", "mic.wav"))
        before = set(os.listdir(mr.CACHE_DIR))
        self.assertEqual(mr.cmd_start(), mr.RETRY)
        self.assertEqual(set(os.listdir(mr.CACHE_DIR)), before)
        self.assertEqual(Path(taken, "keep").read_text(), "not ours")

    def test_undecodable_probe_output_is_a_bounded_retry(self):
        # Codex (round 5): UnicodeDecodeError from pactl crashed cmd_watch
        self.start_env()
        bad = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with mock.patch.object(mr.subprocess, "check_output", side_effect=bad):
            self.assertEqual(mr.cmd_start(source="teams"), mr.RETRY)
            real_start = mr.cmd_start
            start, notes = self.watch_polls(15, lambda **kw: real_start(**kw))
        self.assertEqual(start.call_count, mr.START_RETRIES)
        self.assertEqual(len(notes), 1)
        self.assertIsNone(mr.read_state())

    def test_retry_window_is_checked_before_launching(self):
        # Codex (round 5): a retry was launched at 61 s
        clock, launched = [0.0], []

        def fake_time():
            clock[0] += 7.0
            return clock[0]

        def fake_start(**kw):
            launched.append(clock[0])             # this poll's `now`
            return mr.RETRY
        with mock.patch.object(mr.time, "time", fake_time):
            start, notes = self.watch_polls(12, fake_start)
        self.assertTrue(launched)
        self.assertLess(max(launched) - launched[0], mr.START_RETRY_SECONDS)
        self.assertLess(start.call_count, mr.START_RETRIES)   # time ran out
        self.assertEqual(len(notes), 1)

    def test_auto_toggle_writes_only_its_key(self):
        self.assertEqual(mr.cmd_auto(["off"]), 0)
        text = Path(mr.CONFIG_FILE).read_text()
        self.assertIn("[auto]\nenabled = false", text)
        self.assertIn("model = medium", text)
        self.assertNotIn("[screens]", text)


class TestDiskAndPrivacy(unittest.TestCase):
    P = {"max_mb": 1.0, "min_free_mb": 10.0}

    def test_frame_budget(self):
        self.assertIsNone(mr.frame_budget_error(0, 50e6, 400_000, self.P))
        self.assertIn("budget", mr.frame_budget_error(800_000, 50e6, 400_000, self.P))
        self.assertIn("disk", mr.frame_budget_error(0, 10.2e6, 400_000, self.P))

    def test_append_private_mode_and_short_write_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "screens.jsonl")
            old = os.umask(0)
            try:
                mr.append_private(path, '{"a": 1}\n')
            finally:
                os.umask(old)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            real = os.write
            with mock.patch.object(mr.os, "write",
                                   lambda fd, b: real(fd, b[:5])):
                with self.assertRaises(OSError):
                    mr.append_private(path, '{"b": 2}\n')
            self.assertEqual(Path(path).read_text(), '{"a": 1}\n')

    def raw_session(self, d, names):
        """A staging dir under a temp CACHE_DIR + a recording folder d/out
        (meta.json), as collect_screens requires; config from a temp file."""
        cache = os.path.join(d, "cache")
        self.enterContext(mock.patch.object(mr, "CACHE_DIR", cache))
        self.enterContext(mock.patch.object(
            mr, "CONFIG_FILE", make_cfg(d, "[screens]\nmin_free_mb = 0\n")))
        out = os.path.join(d, "out")
        os.makedirs(out, exist_ok=True)
        if not os.path.exists(os.path.join(out, "meta.json")):
            Path(out, "meta.json").write_text("{}")
        raw = os.path.join(cache, "1700000000-abcdef")
        make_frames(raw, names)
        Path(raw, "screens.jsonl").write_text("".join(
            json.dumps({"t": float(n[:7]), "file": n, "window_title": "W"}) + "\n"
            for n in names))
        return raw

    def test_collect_is_private_and_verified(self):
        with tempfile.TemporaryDirectory() as d:
            raw = self.raw_session(d, ["00003.0s.jpg", "00012.0s.jpg"])
            out = os.path.join(d, "out")
            os.makedirs(out, exist_ok=True)
            Path(out, "meta.json").write_text(json.dumps(
                {"transcript_status": "done", "speaker_labels": True}))
            old = os.umask(0)
            try:
                self.assertEqual(mr.collect_screens(raw, out), 2)
            finally:
                os.umask(old)
            mode = lambda *p: stat.S_IMODE(os.stat(os.path.join(out, *p)).st_mode)
            self.assertEqual(mode("screens"), 0o700)
            self.assertEqual(mode("screens", "00003.0s.jpg"), 0o600)
            self.assertEqual(mode("screens.json"), 0o600)
            self.assertFalse(os.path.exists(raw))            # all copied
            meta = json.loads(Path(out, "meta.json").read_text())
            self.assertEqual(meta, {"transcript_status": "done",
                                    "speaker_labels": True, "screens": 2})

    def test_collect_partial_failure_keeps_raw_for_retry(self):
        with tempfile.TemporaryDirectory() as d, \
                contextlib.redirect_stderr(io.StringIO()):
            raw = self.raw_session(d, ["00003.0s.jpg", "00012.0s.jpg"])
            os.chmod(os.path.join(raw, "screens", "00012.0s.jpg"), 0)
            out = os.path.join(d, "out")
            os.makedirs(out, exist_ok=True)
            if os.access(os.path.join(raw, "screens", "00012.0s.jpg"), os.R_OK):
                self.skipTest("running as root")
            self.assertEqual(mr.collect_screens(raw, out), 1)
            frames = json.loads(Path(out, "screens.json").read_text())
            self.assertEqual([f["file"] for f in frames], ["00003.0s.jpg"])
            self.assertTrue(os.path.isdir(raw))
            os.chmod(os.path.join(raw, "screens", "00012.0s.jpg"), 0o600)
            self.assertEqual(mr.collect_screens(raw, out), 2)   # the retry
            self.assertFalse(os.path.exists(raw))

    def test_moments_built_by_whichever_finishes_last(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            os.makedirs(out, exist_ok=True)
            # transcription finished first: no frames yet -> no moments
            Path(out, "segments.json").write_text(json.dumps([seg(10, 12, "hello")]))
            Path(out, "transcript.md").write_text("# M\n\n[00:10 -> 00:12] hello\n")
            os.utime(os.path.join(out, "transcript.md"), (1_000_000, 1_000_000))
            self.assertIsNone(mr.moments_if_ready(out, [seg(10, 12, "hello")]))
            raw = self.raw_session(d, ["00012.0s.jpg"])
            mr.collect_screens(raw, out)            # collection finishes last
            self.assertIn("hello", Path(out, "moments.md").read_text())

    def test_collect_before_transcript_skips_moments(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "out")
            os.makedirs(out, exist_ok=True)
            mr.collect_screens(self.raw_session(d, ["00012.0s.jpg"]), out)
            self.assertFalse(os.path.exists(os.path.join(out, "moments.md")))


class TestScreensWorkerProcess(unittest.TestCase):
    """The real screens-loop process against fake hyprctl / grim."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.bin = os.path.join(self.d, "bin")
        os.mkdir(self.bin)
        self.script("hyprctl", "echo '[{\"class\": \"mr-test\", \"title\": \"T\", "
                    "\"mapped\": true, \"stableId\": \"abc\", \"size\": [1600, 900]}]'")
        self.cfg = make_cfg(self.d, "[screens]\nwindow_class = mr-test\n"
                                    "interval_seconds = 0.5\n")
        self.state_dir = os.path.join(self.d, ".local", "state", "meet-recorder")
        os.makedirs(self.state_dir)
        self.raw = os.path.join(self.d, "raw")
        os.mkdir(self.raw)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            try:
                os.killpg(p.pid, 9)
            except OSError:
                pass
            p.wait()
        self._tmp.cleanup()

    def script(self, name, body):
        path = os.path.join(self.bin, name)
        Path(path).write_text("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def start(self):
        rec = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.procs.append(rec)
        time.sleep(0.05)
        Path(self.state_dir, "session.json").write_text(json.dumps({
            "id": "sid1", "session_dir": self.raw, "pids": [rec.pid],
            "pid_starts": {str(rec.pid): mr.proc_start(rec.pid)}}))
        env = dict(os.environ, HOME=self.d, MEET_RECORDER_CONFIG=self.cfg,
                   PATH=self.bin + ":/usr/bin:/bin")
        w = subprocess.Popen([sys.executable, CLI_PATH, "screens-loop", self.raw,
                              "sid1", repr(time.monotonic())], env=env,
                             start_new_session=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.procs.append(w)
        return rec, w

    def wait_for(self, cond, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def group_size(self, pgid):
        n = 0
        for name in os.listdir("/proc"):
            if name.isdigit():
                st = mr.proc_stat(name)
                n += bool(st and st[1] == pgid and st[0] not in "ZX")
        return n

    def test_stop_while_grim_hangs_leaves_nothing_behind(self):
        self.script("grim", "exec sleep 100")
        _, w = self.start()
        self.assertTrue(self.wait_for(lambda: self.group_size(w.pid) >= 2))
        t0 = time.monotonic()
        mr.stop_procs([], (w.pid, mr.proc_start(w.pid)))
        self.assertLess(time.monotonic() - t0, 2.0)   # no 5 s escalation needed
        self.assertEqual(self.group_size(w.pid), 0)
        self.assertEqual(oct(stat.S_IMODE(os.stat(os.path.join(self.raw, "screens"))
                                          .st_mode)), oct(0o700))

    def test_grim_dies_with_a_killed_worker(self):
        # no group signalling any more: a SIGKILLed worker's grim must still
        # not linger (PR_SET_PDEATHSIG)
        self.script("grim", "exec sleep 100")
        _, w = self.start()
        self.assertTrue(self.wait_for(lambda: self.group_size(w.pid) >= 2))
        os.kill(w.pid, signal.SIGKILL)
        w.wait()
        self.assertTrue(self.wait_for(lambda: self.group_size(w.pid) == 0))

    def test_stop_signal_during_spawn_kills_the_new_child(self):
        runner = mr.CaptureRunner(None)
        real_popen = subprocess.Popen

        def popen_then_signal(*a, **kw):
            p = real_popen(*a, **kw)
            runner.kill()             # the handler ran before self.proc = p
            return p
        t0 = time.monotonic()
        with mock.patch.object(mr.subprocess, "Popen", popen_then_signal):
            with self.assertRaises(RuntimeError):
                runner.run(["sleep", "60"], timeout=10)
        self.assertLess(time.monotonic() - t0, 2)

    def test_narrow_window_is_skipped_and_logged_once(self):
        # fix round 8: a 338-598 px tile made unreadable frames
        self.script("hyprctl", "echo '[{\"class\": \"mr-test\", \"title\": \"T\", "
                    "\"mapped\": true, \"stableId\": \"abc\", \"size\": [500, 400]}]'")
        marker = os.path.join(self.d, "grim-called")
        self.script("grim", f"touch {marker}; exit 1")
        _, w = self.start()
        time.sleep(2.0)                                # ~4 ticks at 0.5 s
        mr.stop_procs([], (w.pid, mr.proc_start(w.pid)))
        self.assertFalse(os.path.exists(marker))
        log = Path(self.raw, "screens.log").read_text()
        self.assertEqual(log.count("min_width"), 1, log)

    def test_exits_by_itself_when_recorders_die(self):
        self.script("grim", "printf 'P6\\n2 2\\n255\\n'; head -c 12 /dev/zero")
        rec, w = self.start()
        time.sleep(1.0)
        self.assertIsNone(w.poll())                    # running while recording
        rec.kill()
        rec.wait()
        self.assertTrue(self.wait_for(lambda: w.poll() is not None, timeout=8))


class TestSnap(unittest.TestCase):
    """Task 9: `meet-recorder snap` against fake hyprctl / grim /
    notify-send on PATH (real CLI process, temp HOME)."""
    # minimal JPEG: SOI + SOF0 (512 high x 768 wide) + EOI
    JPEG = r"\377\330\377\300\000\021\010\002\000\003\000\003\001\042\000\377\331"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.bin = os.path.join(self.d, "bin")
        os.mkdir(self.bin)
        self.notes = os.path.join(self.d, "notes.log")
        self.script("hyprctl", """case "$1" in
  clients) echo '[{"class": "teams-for-linux", "title": "Weekly sync | Microsoft Teams", "mapped": true, "stableId": "call1", "size": [1783, 1378], "address": "0x1"}, {"class": "foot", "title": "notes", "mapped": true, "stableId": "foot1", "size": [800, 600], "address": "0x2"}]';;
  activewindow) echo '{"class": "foot", "title": "notes", "address": "0x2", "size": [800, 600]}';;
esac""")
        self.script("grim", f"echo \"$@\" >> {self.d}/grim.log; printf '{self.JPEG}'")
        self.script("notify-send",
                    f"for a in \"$@\"; do printf '%s\\n' \"$a\"; done >> {self.notes}; "
                    f"echo --- >> {self.notes}")
        self.cfg = make_cfg(self.d, "[screens]\nmax_mb = 300\nmin_free_mb = 0\n")
        self.state_dir = os.path.join(self.d, ".local", "state", "meet-recorder")
        os.makedirs(self.state_dir)
        self.raw = os.path.join(self.d, "raw")
        os.mkdir(self.raw, 0o700)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            p.kill()
            p.wait()
        self._tmp.cleanup()

    def script(self, name, body):
        path = os.path.join(self.bin, name)
        Path(path).write_text("#!/bin/sh\n" + body + "\n")
        os.chmod(path, 0o755)

    def record(self, ago=65.0):
        rec = subprocess.Popen(["sleep", "60"])
        self.procs.append(rec)
        time.sleep(0.05)
        Path(self.state_dir, "session.json").write_text(json.dumps({
            "id": "sid1", "session_dir": self.raw, "pids": [rec.pid],
            "pid_starts": {str(rec.pid): mr.proc_start(rec.pid)},
            "start": int(time.time() - ago), "origin": time.monotonic() - ago,
            "title": "Weekly sync"}))

    def cli(self, *args):
        env = dict(os.environ, HOME=self.d, MEET_RECORDER_CONFIG=self.cfg,
                   PATH=self.bin + ":/usr/bin:/bin")
        return subprocess.run([sys.executable, CLI_PATH, *args], env=env,
                              capture_output=True, text=True, timeout=30)

    def notifications(self):
        try:
            text = Path(self.notes).read_text()
        except FileNotFoundError:
            return []
        return [n.strip("\n").split("\n") for n in text.split("---\n") if n.strip()]

    def manifest(self):
        return [json.loads(l) for l in
                Path(self.raw, "screens.jsonl").read_text().splitlines()]

    def test_snap_while_recording_saves_frame_manifest_and_count(self):
        self.record()
        t0 = time.monotonic()
        r = self.cli("snap")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.monotonic() - t0, 5)
        path = r.stdout.strip()
        self.assertEqual(os.path.dirname(path), os.path.join(self.raw, "screens"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        (entry,) = self.manifest()
        self.assertEqual(set(entry), {"t", "file", "window_title", "w", "h", "manual"})
        self.assertIs(entry["manual"], True)
        self.assertEqual((entry["w"], entry["h"]), (768, 512))
        self.assertEqual(entry["window_title"], "Weekly sync")
        self.assertAlmostEqual(entry["t"], 65.0, delta=3)
        self.assertIn("-T call1 -t jpeg", Path(self.d, "grim.log").read_text())
        (note,) = self.notifications()
        self.assertEqual(note[:6], ["-a", "Meeting snap", "-c", "meet-recorder.snap",
                                    "-t", "2500"])
        self.assertIn(path, note)
        self.assertIn(f"string:image-path:{path}", note)
        self.assertNotIn("critical", note)
        self.assertEqual(note[-2:], ["Screen saved",
                                     f"{mr.fmt_ts(entry['t'])} · Weekly sync"])
        # a second snap at once: never replaces the first, count goes up
        self.assertEqual(self.cli("snap").returncode, 0)
        files = [e["file"] for e in self.manifest()]
        self.assertEqual(len(set(files)), 2)
        st = json.loads(self.cli("status", "--json").stdout)
        self.assertEqual(st["snaps"], 2)
        self.assertAlmostEqual(st["last_snap_unix"], time.time(), delta=5)

    def test_focused_window(self):
        self.record()
        r = self.cli("snap", "--window", "focused")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-T foot1", Path(self.d, "grim.log").read_text())
        self.assertEqual(self.manifest()[0]["window_title"], "notes")
        self.assertEqual(self.cli("snap", "--window", "bogus").returncode, 2)

    def test_not_recording_fails_visibly(self):
        r = self.cli("snap")
        self.assertEqual(r.returncode, 1)
        (note,) = self.notifications()
        self.assertIn("-u", note)
        self.assertIn("critical", note)
        self.assertEqual(note[-2][:len("Screen NOT saved")], "Screen NOT saved")
        self.assertIn("not recording", note[-2])
        self.assertFalse(os.path.exists(os.path.join(self.d, "grim.log")))
        self.assertEqual(json.loads(self.cli("status", "--json").stdout)["snaps"], 0)

    def test_budget_is_respected(self):
        Path(self.cfg).write_text("[screens]\nmax_mb = 0.00001\n")
        self.record()
        r = self.cli("snap")
        self.assertEqual(r.returncode, 1)
        self.assertIn("budget", self.notifications()[0][-2])
        self.assertFalse(os.path.exists(os.path.join(self.raw, "screens.jsonl")))

    def test_capture_failure_is_reported(self):
        self.script("grim", "echo nope >&2; exit 1")
        self.record()
        self.assertEqual(self.cli("snap").returncode, 1)
        note = self.notifications()[0]
        self.assertIn("critical", note)
        self.assertTrue(note[-2].startswith("Screen NOT saved — grim failed"))


class TestManualFramesDownstream(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.saved = mr.CACHE_DIR
        mr.CACHE_DIR = os.path.join(self.d, "cache")
        os.makedirs(mr.CACHE_DIR)

    def tearDown(self):
        mr.CACHE_DIR = self.saved
        self._tmp.cleanup()

    def test_manual_frames_survive_collect_and_are_marked(self):
        raw = os.path.join(mr.CACHE_DIR, "s1")
        os.makedirs(os.path.join(raw, "screens"))
        folder = os.path.join(self.d, "out")
        os.makedirs(folder)
        Path(folder, "meta.json").write_text("{}")
        lines = []
        # an automatic frame and a manual one 0.1 s later (same picture):
        # both are kept, nothing is deduplicated
        for t, manual in ((10.0, False), (10.1, True)):
            name = mr.screen_file_name(t)
            Path(raw, "screens", name).write_bytes(b"\xff\xd8same")
            e = {"t": t, "file": name, "window_title": "Deck", "w": 1, "h": 1}
            if manual:
                e["manual"] = True
            lines.append(json.dumps(e))
        Path(raw, "screens.jsonl").write_text("\n".join(lines) + "\n")
        with mock.patch.object(mr, "CONFIG_FILE",
                               make_cfg(self.d, "[screens]\nmin_free_mb = 0\n")):
            self.assertEqual(mr.collect_screens(raw, folder), 2)
        frames = mr.read_frames(folder)
        self.assertEqual([f.get("manual") for f in frames], [None, True])
        self.assertFalse(os.path.exists(raw))
        md = mr.render_moments(frames, [])
        self.assertIn("## 00:10 — Deck — 📌 marked by you", md)
        self.assertEqual(md.count("📌"), 1)

    def test_link_screens_prefers_a_manual_frame_in_the_window(self):
        frames = [{"t": 50.0, "file": "00050.0s.jpg", "manual": True},
                  {"t": 95.0, "file": "00095.0s.jpg"},
                  {"t": 300.0, "file": "00300.0s.jpg"},
                  {"t": 305.0, "file": "00305.0s.jpg", "manual": True}]
        self.assertEqual(mr.pick_frame(frames, 100)["t"], 50.0)     # not 95
        self.assertEqual(mr.pick_frame(frames, 299)["t"], 305.0)   # after, within 15 s
        self.assertEqual(mr.pick_frame(frames, 180)["t"], 95.0)    # no manual near
        text = mr.link_screens("- Ship it [01:40]\n", frames)
        self.assertIn("(screens/00050.0s.jpg)", text)
        self.assertEqual(mr.link_screens(text, frames), text)        # idempotent


class TestSnapHotkeyInstaller(unittest.TestCase):
    SCRIPT = os.path.join(HERE, "..", "install-hypr-bind.sh")
    ANCHOR = 'o.bind("SUPER + SHIFT + R", "Meeting recorder", "meet-recorder toggle")'

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.file = os.path.join(self.d, "bindings.lua")
        self.orig = ("-- my bindings\n"
                     'o.bind("SUPER + RETURN", "Terminal", { launch = "foot" })\n'
                     f"{self.ANCHOR}\n"
                     'o.bind("SUPER + B", "Browser", { launch = "chromium" })\n')
        Path(self.file).write_text(self.orig)
        self.binds = os.path.join(self.d, "binds.json")
        self.errors = os.path.join(self.d, "errors.txt")
        self.calls = os.path.join(self.d, "calls.log")
        Path(self.binds).write_text(json.dumps([
            {"modmask": 65, "key": "R", "description": "Meeting recorder"},
            {"modmask": 68, "key": "R", "description": "Set reminder"}]))
        Path(self.errors).write_text("\n\n")
        self.hyprctl = os.path.join(self.d, "hyprctl")
        Path(self.hyprctl).write_text(
            "#!/bin/sh\n"
            f"echo \"$@\" >> {self.calls}\n"
            "case \"$1\" in\n"
            f"  binds) cat {self.binds};;\n"
            f"  configerrors) cat {self.errors};;\n"
            "esac\n")
        os.chmod(self.hyprctl, 0o755)

    def tearDown(self):
        self._tmp.cleanup()

    def run_it(self):
        return subprocess.run(["bash", self.SCRIPT, self.file], capture_output=True,
                              text=True, env=dict(os.environ, HYPRCTL=self.hyprctl),
                              timeout=30)

    def reloads(self):
        try:
            return Path(self.calls).read_text().split().count("reload")
        except FileNotFoundError:
            return 0

    def test_inserts_once_after_the_toggle_and_reloads_only_on_change(self):
        r = self.run_it()
        self.assertEqual(r.returncode, 0, r.stderr)
        want = 'o.bind("SUPER + ALT + R", "Meeting snap", "meet-recorder snap")'
        lines = Path(self.file).read_text().splitlines()
        self.assertEqual(lines[lines.index(self.ANCHOR) + 1], want)
        self.assertEqual(self.reloads(), 1)
        self.assertEqual(self.run_it().returncode, 0)                # again
        self.assertEqual(Path(self.file).read_text().count("meet-recorder snap"), 1)
        self.assertEqual(self.reloads(), 1)                          # unchanged
        self.assertEqual(Path(self.file).read_text(),
                         self.orig.replace(self.ANCHOR, self.ANCHOR + "\n" + want))
        self.assertEqual(sorted(os.listdir(self.d)),
                         sorted(["bindings.lua", "binds.json", "errors.txt",
                                 "calls.log", "hyprctl"]))           # no leftovers

    def test_falls_back_when_super_alt_r_is_taken(self):
        Path(self.binds).write_text(json.dumps([{"modmask": 72, "key": "r"}]))
        self.assertEqual(self.run_it().returncode, 0)
        self.assertIn('o.bind("SUPER + SHIFT + U", "Meeting snap", "meet-recorder snap")',
                      Path(self.file).read_text())

    def test_config_errors_revert_the_line(self):
        Path(self.errors).write_text("bindings.lua:4: bad bind\n")
        r = self.run_it()
        self.assertEqual(r.returncode, 0)
        self.assertIn("reverted", r.stderr)
        self.assertEqual(Path(self.file).read_text(), self.orig)
        self.assertEqual(self.reloads(), 2)                          # add + revert

    def test_missing_anchor_changes_nothing(self):
        Path(self.file).write_text("-- nothing here\n")
        r = self.run_it()
        self.assertEqual(r.returncode, 0)
        self.assertIn("not found", r.stderr)
        self.assertEqual(Path(self.file).read_text(), "-- nothing here\n")
        self.assertEqual(self.reloads(), 0)

    def test_install_local_calls_it(self):
        inst = Path(HERE, "..", "install-local.sh").read_text()
        self.assertIn("./install-hypr-bind.sh", inst)
        self.assertTrue(os.access(self.SCRIPT, os.X_OK))


class TestLatestFolder(unittest.TestCase):
    def test_newest_with_transcript_any_source(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(mr.newest_transcript_folder(d))
            self.assertIsNone(mr.newest_transcript_folder(os.path.join(d, "nope")))
            for rel, has in (("2026-11-22/090000-meeting", True),
                             ("2026-11-23/101502-teams-weekly-ops", True),
                             ("2026-11-23/120000-manual", True),
                             ("2026-11-23/130000-manual", False)):   # pending
                os.makedirs(os.path.join(d, rel))
                if has:
                    Path(d, rel, "transcript.md").write_text("x")
            self.assertEqual(mr.newest_transcript_folder(d),
                             os.path.join(d, "2026-11-23/120000-manual"))

    def test_summarize_without_recordings(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "rec"))
            cfg = make_cfg(d, f"[paths]\noutput_dir = {d}/rec\n")
            env = dict(os.environ, HOME=d, MEET_RECORDER_CONFIG=cfg,
                       PATH="/usr/bin:/bin")
            out = subprocess.run(["bash", os.path.join(HERE, "..",
                                                      "meet-recorder-summarize")],
                                 env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 1)
            self.assertIn("no recording found", out.stderr)


if __name__ == "__main__":
    unittest.main()
