#!/usr/bin/env python3
"""Unit tests for meet-recorder's pure functions.

No audio and no faster-whisper import are involved. The CLI is loaded by path
because the script has no .py extension.
"""

import importlib.machinery
import importlib.util
import contextlib
import html
import io
import json
import os
import random
import re
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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
        # strip becomes volatile on tick 3, settled 2 ticks -> keep at tick 4;
        # later slides settle 2 ticks after they appear
        self.assertEqual(run_keeps(frames), [12.0, 36.0, 66.0, 96.0])

    def test_text_change_while_strip_moves_is_new(self):
        s = Screen()
        line = (10, 20, 100, 25)                           # one new text line
        frames = [s.frame(0) for _ in range(10)] + \
                 [s.frame(0, lines=[line]) for _ in range(10)]
        self.assertEqual(run_keeps(frames), [12.0, 36.0])

    def test_caret_sized_change_is_not_new(self):
        s = Screen()
        frames = [s.frame(0) for _ in range(10)] + \
                 [s.frame(0, lines=[(10, 20, 13, 25)]) for _ in range(10)]
        self.assertEqual(run_keeps(frames), [12.0])

    def test_full_window_video_keeps_nothing(self):
        s = Screen()
        self.assertEqual(run_keeps([s.frame(0, video=True) for _ in range(40)]), [])

    def test_mostly_video_keeps_nothing(self):
        s = Screen()       # everything but the top 10 % is live video (> 85 %)
        frames = [s.frame(0, top_rows=TH // 10) for _ in range(40)]
        self.assertEqual(run_keeps(frames), [])

    def test_cell_that_stops_being_volatile_is_eligible_again(self):
        s = Screen()
        moving = [s.frame(0) for _ in range(10)]              # keep at 12 s
        frozen = [s.frame(0, strip="static") for _ in range(8)]
        # a change that lives only in the (former) strip region
        changed = [s.frame(0, strip="static", lines=[(0, 62, 120, 70)])
                   for _ in range(8)]
        times = run_keeps(moving + frozen + changed)
        self.assertEqual(times[0], 12.0)
        # the frozen strip itself differs from the kept frame once it calms
        # down (a camera turned off is a real layout change) ...
        self.assertEqual(len([t for t in times if 30 <= t < 54]), 1)
        # ... and a later change inside the region is seen again
        self.assertEqual(len([t for t in times if t >= 54]), 1)

    def test_while_volatile_the_strip_is_ignored(self):
        s = Screen()
        frames = [s.frame(0) for _ in range(30)]
        self.assertEqual(run_keeps(frames), [12.0])

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


class TestCollectScreens(unittest.TestCase):
    def test_moves_frames_and_skips_torn_or_missing(self):
        with tempfile.TemporaryDirectory() as d:
            raw, out = os.path.join(d, "raw"), os.path.join(d, "out")
            os.makedirs(os.path.join(raw, "screens"))
            os.makedirs(out)
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
            self.assertTrue(mr.write_moments(d, self.SEGS).endswith("moments.md"))


FRAMES = [{"t": 30.0, "file": "00030.0s.jpg"}, {"t": 100.0, "file": "00100.0s.jpg"},
          {"t": 200.0, "file": "00200.0s.jpg"}]


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
            self.assertEqual(mr.cmd_link_screens([d]), 0)
            self.assertEqual(mr.cmd_link_screens([d]), 0)
            self.assertEqual(Path(d, "summary.md").read_text(),
                             "- [ ] Do it [01:45] — [screen 01:40](screens/00100.0s.jpg)\n")


if __name__ == "__main__":
    unittest.main()
