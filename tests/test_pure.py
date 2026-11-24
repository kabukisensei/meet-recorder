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
import re
import tempfile
import time
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
