#!/usr/bin/env python3
"""Unit tests for meet-recorder's pure functions.

No audio and no faster-whisper import are involved. The CLI is loaded by path
because the script has no .py extension.
"""

import importlib.machinery
import importlib.util
import os
import re
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CLI_PATH = os.path.join(HERE, "..", "meet-recorder")

_spec = importlib.util.spec_from_loader(
    "meet_recorder", importlib.machinery.SourceFileLoader("meet_recorder", CLI_PATH))
mr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr)


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

    def test_end_clamped_and_newlines_collapsed(self):
        vtt = mr.segments_to_vtt([seg(5, 4, "line one\n\nline two")])
        self.assertIn("00:00:05.000 --> 00:00:05.000", vtt)
        self.assertIn("<v Others>line one line two</v>", vtt)

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

    def test_roundtrip_with_format_segment(self):
        line = mr.format_segment(3725.7, 3730.2, "text", "Others")
        self.assertEqual(mr.parse_md_line(line),
                         seg(3725.0, 3730.0, "text", "Others"))


class TestWriteTranscripts(unittest.TestCase):
    def test_md_txt_unchanged_plus_vtt_and_segments(self):
        import json
        segs = [(0.25, 4.75, " Hi there ", "Others"), (5.5, 7.125, "Yo", "Me")]
        cfg = mr.load_config("/nonexistent/path.ini")
        with tempfile.TemporaryDirectory() as d:
            mr.write_transcripts(d, segs, 1_700_000_000, 65.4, "Sync", cfg)
            body = "[00:00 -> 00:04] Others: Hi there\n[00:05 -> 00:07] Me: Yo\n"
            self.assertEqual(open(os.path.join(d, "transcript.txt")).read(), body)
            iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(1_700_000_000))
            self.assertEqual(open(os.path.join(d, "transcript.md")).read(),
                             f"# Sync {iso}\n\nDuration: 01:05\n\n" + body)
            self.assertEqual(json.load(open(os.path.join(d, "segments.json")))[1],
                             {"start": 5.5, "end": 7.125, "speaker": "Me", "text": "Yo"})
            vtt = open(os.path.join(d, "transcript.vtt")).read()
            self.assertIn("00:00:05.500 --> 00:00:07.125\n<v Me>Yo</v>", vtt)


class TestExport(unittest.TestCase):
    def test_backfill_from_md_and_missing(self):
        import contextlib, io, json
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "transcript.md"), "w") as f:
                f.write("# Meeting x\n\nDuration: 00:10\n\n"
                        "[00:05 -> 00:09] Me: b\n[00:00 -> 00:04] a\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mr.cmd_export([d]), 0)
                first = open(os.path.join(d, "transcript.vtt")).read()
                self.assertEqual(mr.cmd_export([d]), 0)  # idempotent
            self.assertEqual(open(os.path.join(d, "transcript.vtt")).read(), first)
            self.assertLess(first.index(">a<"), first.index(">b<"))
            segs = json.load(open(os.path.join(d, "segments.json")))
            self.assertEqual(len(segs), 2)
            empty = os.path.join(d, "empty")
            os.mkdir(empty)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(mr.cmd_export([empty]), 1)


if __name__ == "__main__":
    unittest.main()
