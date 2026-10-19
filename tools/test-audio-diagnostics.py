#!/usr/bin/env python3
"""Exercise actual diagnostic worker and player/ALSA bodies with controlled APIs.

Host boundaries are not decoder, kernel, scheduler or physical HDMI acceptance.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DIAG = ROOT / "xbmc/cores/AudioEngine/Utils/AudioDiagnostics.cpp"


def block(text, marker):
    start = text.index(marker)
    pos = text.index("{", start)
    depth, end = 1, pos + 1
    while depth:
        depth += (text[end] == "{") - (text[end] == "}")
        end += 1
    return text[start:end]


class Diagnostics(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.directory = Path(cls.temp.name)
        alsa = (ROOT / "xbmc/cores/AudioEngine/Sinks/AESinkALSA.cpp").read_text()
        player = (ROOT / "xbmc/cores/paplayer/PAPlayer.cpp").read_text()
        bodies = "\n".join(block(alsa, marker) for marker in (
            "void CAESinkALSA::GetDelay(", "unsigned int CAESinkALSA::AddPackets(",
            "void CAESinkALSA::HandleError(", "void CAESinkALSA::Drain(",
        ))
        program = r'''
#include <future>
#include <iostream>
#include <fstream>
#include <vector>
#include <climits>
#include <cerrno>
#include "@DIAG@"
using namespace std::chrono_literals;
using M = AudioDiagnostics::Metric;
std::vector<int> writes;
std::vector<int> resumes;
std::vector<std::string> calls;
int recovery = 0, pcmState = 1, delayResult = 0;
long delayFrames = 100;
using snd_pcm_sframes_t = long;
enum { SND_PCM_STATE_PREPARED = 1, LOGERROR, LOGINFO, AE_FMT_RAW };
struct CLog { template<class... T> static void Log(int, const char*, T...) {} };
int snd_pcm_writei(void*, void*, unsigned int count) {
  calls.push_back("write:"+std::to_string(count));
  if (writes.empty()) return count;
  int value = writes.front(); writes.erase(writes.begin()); return value;
}
const char* snd_strerror(int) {return "stub";}
int snd_pcm_recover(void*, int error, int) {calls.push_back("recover:"+std::to_string(error));return recovery;}
int snd_pcm_prepare(void*) {calls.push_back("prepare");return 0;}
int snd_pcm_resume(void*) {
  calls.push_back("resume");
  int value = resumes.front(); resumes.erase(resumes.begin()); return value;
}
int snd_pcm_state(void*) {calls.push_back("state");return pcmState;}
int snd_pcm_start(void*) {calls.push_back("start");pcmState=2;return 0;}
int snd_pcm_drain(void*) {calls.push_back("drain");return 0;}
int snd_pcm_delay(void*, long* frames) {calls.push_back("delay");*frames=delayFrames;return delayResult;}
long snd_pcm_forward(void*, long frames) {calls.push_back("forward:"+std::to_string(frames));return frames;}
namespace KODI::TIME { void Sleep(std::chrono::milliseconds) {calls.push_back("sleep");} }
struct AEDelayStatus { double delay = 0; void SetDelay(double value) {delay=value;} };
struct CAESinkALSA {
  void* m_pcm = this;
  struct {int m_frameSize=4;} m_format;
  double m_formatSampleRateMul=1.0/44100.0;
  bool m_fragmented=false;
  unsigned int m_originalPeriodSize=16;
  void GetDelay(AEDelayStatus&);
  unsigned int AddPackets(uint8_t**, unsigned int, unsigned int);
  void HandleError(const char*, int);
  void Drain();
};
@ALSA@
struct ICodec { int GetCacheLevel() const {return 25;} };
struct Decoder {
  unsigned int available=4;
  uint8_t buffer[256]{};
  ICodec codec;
  unsigned int GetDataSize(bool) {return available;}
  int GetStatus() {return 3;}
  void* GetData(unsigned int) {return buffer;}
  uint8_t* GetRawData(int& size) {size=0;return nullptr;}
  const ICodec* GetCodec() {return &codec;}
};
struct Stream {
  unsigned int space=256, accepted=2, added=0;
  unsigned int GetSpace() {return space;}
  unsigned int AddData(uint8_t**, int, unsigned int, void*) {++added;return accepted;}
};
struct PAPlayer {
  struct StreamInfo {
    Decoder m_decoder;
    Stream stream;
    Stream* m_stream=&stream;
    unsigned int m_bytesPerSample=2;
    bool m_started=true;
    unsigned int m_framesSent=0;
    struct {
      int m_dataFormat=0, m_sampleRate=44100;
      struct {int Count() const {return 2;}} m_channelLayout;
      struct {int m_sampleRate=44100;int GetDuration() const {return 20;}} m_streamInfo;
    } m_audioFormat;
  };
  struct {int m_cacheLevel=0;} m_playerGUIData;
  bool QueueData(StreamInfo*);
};
@PLAYER@
int main(int argc, char** argv) {
  std::string mode=argv[1], directory=argv[2];
  bool on=std::stoi(argv[3]);
  if(on) std::ofstream(directory+"/audio-diagnostic.enable") << "900\n";
  AudioDiagnostics::Start(directory);
  if(mode=="core") {
    AudioDiagnostics::Record({{M::PlayerReadySamples,0}});
    AudioDiagnostics::Record({{M::PlayerReadySamples,100}},true);
    AudioDiagnostics::Record({{M::AlsaWriteError,-EPIPE},{M::AlsaWriteUs,50000}});
    AudioDiagnostics::Record({{M::DecoderReadBytes,LLONG_MAX}});
    AudioDiagnostics::Record({{M::DecoderReadBytes,1}});
    AudioDiagnostics::Scaled(M::AeRatioPpm,std::numeric_limits<double>::quiet_NaN());
    AudioDiagnostics::Scaled(M::AeRatioPpm,std::numeric_limits<double>::max());
    AudioDiagnostics::Scaled(M::AeCacheUs,0.125);
    AudioDiagnostics::Event(std::string(300,'X'));
    for(int i=0;i<64;++i) AudioDiagnostics::Event("event\nCONTROL");
    // Another thread owns the mutex: submission must drop, never wait.
    std::promise<void> locked,release;
    auto future=release.get_future();
    std::thread holder([&] {
      std::lock_guard<std::mutex> lock(AudioDiagnostics::GetState().mutex);
      locked.set_value(); future.wait();
    });
    locked.get_future().wait();
    auto start=AudioDiagnostics::NowUs();
    AudioDiagnostics::Record({{M::AlsaZero,1}});
    auto elapsed=AudioDiagnostics::NowUs()-start;
    release.set_value();holder.join();
    std::cout << "contention_us=" << elapsed << '\n';
    size_t bytes=AudioDiagnostics::MAX_BYTES-256;
    FILE* file=std::tmpfile();
    if(AudioDiagnostics::Write(file,bytes,"overflow")) return 12;
    std::fclose(file);
    std::cout << "endpoint=" << AudioDiagnostics::SafeEndpoint("surround71:CARD=AMLAUGESOUND,DEV=0")
              << " private=" << AudioDiagnostics::SafeEndpoint("plug:/storage/private?token=SECRET") << '\n';
    if(!on) for(const auto& bank:AudioDiagnostics::GetState().values)
      for(const auto& value:bank) if(value.count) return 14;
  } else if(mode=="boundary") {
    // Model a producer preempted while holding the snapshot lock, across a
    // scheduled drain. Its timestamp must remain inside the drained window.
    std::unique_lock<std::mutex> lock(AudioDiagnostics::GetState().mutex);
    for(int i=0;i<1000;++i) {
      std::ifstream header(directory+"/audio-diagnostic.log");
      if(header.peek()!=std::char_traits<char>::eof()) break;
      std::this_thread::sleep_for(1ms);
    }
    std::this_thread::sleep_for(1200ms);
    AudioDiagnostics::GetState().values[0][static_cast<size_t>(M::AlsaWriteUs)].Add(
      123,AudioDiagnostics::NowUs());
    lock.unlock();
  } else if(mode=="deadline") {
    std::this_thread::sleep_for(1200ms);
    if(AudioDiagnostics::Enabled()) return 13;
  } else if(mode=="bench") {
    const auto start=AudioDiagnostics::NowUs();
    for(int i=0;i<200000;++i)
      AudioDiagnostics::Record({{M::AlsaWriteUs,i},{M::AlsaWantFrames,2205},{M::AlsaZero,0}});
    std::cout << "records=200000 elapsed_us=" << AudioDiagnostics::NowUs()-start << '\n';
  } else if(mode.compare(0,7,"player-")==0) {
    PAPlayer p; PAPlayer::StreamInfo si;
    if(mode=="player-empty") si.m_decoder.available=0;
    if(mode=="player-full") si.stream.space=0;
    if(mode=="player-short") si.stream.accepted=1;
    if(mode=="player-prefill") {si.m_started=false;si.m_decoder.available=0;}
    std::cout << p.QueueData(&si) << ':' << si.m_framesSent << ':' << si.stream.added;
  } else {
    CAESinkALSA sink;
    if(mode=="short") writes={17,47};
    if(mode=="recover") writes={-EPIPE};
    if(mode=="retry") {writes={-EPIPE,64};recovery=-EPIPE;}
    if(mode=="resume") {writes={-ESTRPIPE,64};recovery=-ESTRPIPE;resumes={-EAGAIN,-ENOSYS};}
    if(mode=="delay-error") {delayResult=-EIO;delayFrames=77;}
    if(mode=="delay-negative") delayFrames=-15;
    uint8_t storage[1024]{};uint8_t* data=storage;
    std::cout << sink.AddPackets(&data,64,0) << ':';
    AEDelayStatus status;sink.GetDelay(status);sink.Drain();
    std::cout << status.delay;
    for(const auto& call:calls) std::cout << '|' << call;
  }
  AudioDiagnostics::Stop();
}
'''
        template = program.replace("@DIAG@", str(DIAG))
        baseline_alsa = subprocess.check_output(
            ["git", "show", "2c644fa30d:xbmc/cores/AudioEngine/Sinks/AESinkALSA.cpp"], cwd=ROOT, text=True)
        baseline_player = subprocess.check_output(
            ["git", "show", "2c644fa30d:xbmc/cores/paplayer/PAPlayer.cpp"], cwd=ROOT, text=True)
        originals = "\n".join(block(baseline_alsa, marker) for marker in (
            "void CAESinkALSA::GetDelay(", "unsigned int CAESinkALSA::AddPackets(",
            "void CAESinkALSA::HandleError(", "void CAESinkALSA::Drain(",
        ))
        cls.binary = cls.directory / "fixture"
        cls.baseline = cls.directory / "baseline"
        for binary, alsa_body, player_source in ((cls.binary, bodies, player),
                                                  (cls.baseline, originals, baseline_player)):
            program = template.replace("@ALSA@", alsa_body)
            program = program.replace("@PLAYER@", block(player_source, "bool PAPlayer::QueueData("))
            source = binary.with_suffix(".cpp")
            source.write_text(program)
            subprocess.run(["g++", "-std=c++17", "-DTARGET_LINUX", "-O2", "-pthread",
                        "-fsanitize=undefined", "-fno-sanitize-recover=undefined", "-Wall", "-Wextra",
                        "-Werror", "-Wno-unused-parameter", str(source), "-o", str(binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def run_case(self, case, on=True, marker=None, baseline=False):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            if marker is not None:
                (directory / "audio-diagnostic.enable").write_text(marker)
            result = subprocess.run([str(self.baseline if baseline else self.binary), case, temporary, str(int(on))],
                                    capture_output=True, text=True, timeout=5, check=True)
            path = directory / "audio-diagnostic.log"
            return result.stdout, path.read_text() if path.exists() else ""

    def test_off_has_no_output(self):
        _, log = self.run_case("core", False)
        self.assertEqual(log, "")

    def test_aggregates_preparation_contention_and_storage_bounds(self):
        output, log = self.run_case("core")
        self.assertIn("other_redacted", output)
        self.assertIn("CARD=AMLAUGESOUND,DEV=0", output)
        self.assertIn("player.ready_samples,1,0,0,0,", log)
        self.assertIn("prefill.player.ready_samples,1,100,100,100,", log)
        self.assertIn("alsa.write_errno,1,-32,-32,-32,", log)
        self.assertNotIn("event\nCONTROL", log)
        self.assertIn("event_CONTROL", log)
        self.assertIn("truncated=1", log)
        self.assertNotIn(",ae.resample_ratio_ppm,", log)
        self.assertIn("ae.stream_cache_us,1,125000", log)
        rows = [line.split(",") for line in log.splitlines() if ",decoder.read_bytes," in line]
        self.assertEqual(rows[0][-1], "1")  # saturation explicit, no signed overflow
        import re
        self.assertGreaterEqual(sum(map(int, re.findall(r"dropped=(\d+)", log))), 36)
        self.assertIn("END ", log)
        self.assertLessEqual(len(log.encode()), 8 * 1024 * 1024)

    def test_window_boundaries_contain_their_observations(self):
        _, log = self.run_case("boundary")
        for line in log.splitlines():
            if not line[:1].isdigit():
                continue
            values = line.split(",")
            start, end, max_at, last_at = map(int, (values[0], values[1], values[7], values[9]))
            self.assertLessEqual(start, max_at)
            self.assertLessEqual(max_at, end)
            self.assertLessEqual(start, last_at)
            self.assertLessEqual(last_at, end)

    def test_invalid_markers_and_automatic_deadline(self):
        for marker in ("0", "901", "-1", "900 extra", "invalid", "4294968196"):
            with self.subTest(marker=marker):
                self.assertEqual(self.run_case("normal", False, marker)[1], "")
        # Fixture does not overwrite a supplied marker when on=False.
        _, log = self.run_case("deadline", False, "1\n")
        self.assertIn("reason=deadline", log)

    def test_symlinks_and_special_markers_are_not_followed(self):
        for kind in ("marker_symlink", "output_symlink", "marker_fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                secret = directory / "private"
                secret.write_text("PRIVATE")
                marker = directory / "audio-diagnostic.enable"
                output = directory / "audio-diagnostic.log"
                if kind == "marker_symlink":
                    marker.symlink_to(secret)
                elif kind == "marker_fifo":
                    import os
                    os.mkfifo(marker)
                else:
                    marker.write_text("1\n")
                    output.symlink_to(secret)
                subprocess.run([str(self.binary), "core", temporary, "0"],
                               check=True, capture_output=True, timeout=3)
                self.assertEqual(secret.read_text(), "PRIVATE")
                self.assertTrue(output.is_symlink() or not output.exists())

    def test_alsa_observations_preserve_calls_and_returns(self):
        for case in ("normal", "short", "recover", "retry", "resume", "delay-error", "delay-negative"):
            with self.subTest(case=case):
                off, _ = self.run_case(case, False)
                on, log = self.run_case(case)
                self.assertEqual(on, off)
                original, _ = self.run_case(case, False, baseline=True)
                self.assertEqual(off, original)
                self.assertIn("alsa.write_us", log)
                if case == "recover":
                    self.assertTrue(on.startswith("0:"))  # preserve existing recovery-success behavior
                    self.assertIn("alsa.recover_result,1,0", log)
                if case == "delay-error":
                    self.assertIn("alsa.delay_result,1,-5", log)
                    self.assertNotIn(",alsa.delay_frames,", log)  # unavailable, never healthy zero
                if case == "delay-negative":
                    self.assertIn("alsa.delay_frames,1,-15", log)
                    self.assertIn("alsa.forward_result,1,15", log)
                if case == "short":
                    self.assertIn("alsa.short_frames,2,47", log)
                if case == "resume":
                    self.assertIn("alsa.resume_result,2,", log)

    def test_player_starvation_backpressure_and_short_acceptance(self):
        for case, metric in (("player-empty", "player.no_data_with_space,1,1"),
                             ("player-full", "player.no_space,1,1"),
                             ("player-short", "player.short_frames,1,1"),
                             ("player-prefill", "prefill.player.no_data_with_space,1,1")):
            with self.subTest(case=case):
                off, _ = self.run_case(case, False)
                on, log = self.run_case(case)
                self.assertEqual(on, off)
                original, _ = self.run_case(case, False, baseline=True)
                self.assertEqual(off, original)
                self.assertIn(metric, log)

    def test_submission_cost_is_measured(self):
        for on in (False, True):
            output, _ = self.run_case("bench", on)
            print(f"diagnostics_{'on' if on else 'off'}: {output.strip()}")


class Collector(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.dont_write_bytecode = True
        spec = importlib.util.spec_from_file_location("audio_collector", ROOT / "tools/Linux/audio-diagnostic.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_recording_completion_is_not_current_coverage(self):
        header = ("AUDIO_DIAG schema=1 clock=CLOCK_MONOTONIC mono_us=1000000 "
                  "wall_unix_ms=1000 duration_s=900 capture_start_us=1000000 max_bytes=8388608\n")
        current = header + "WINDOW seq=1 start_us=1000000 end_us=4000000 dropped=2\n"
        active = self.module.diagnostic_coverage(current, 2000000, 5000000, 2000)
        self.assertEqual(active["coverage"], "current")
        self.assertFalse(active["recording_complete"])
        self.assertEqual(active["end_lag_us"], 1000000)
        self.assertEqual(active["overlapping_dropped_submissions"], 2)
        preflight = self.module.diagnostic_coverage(current, 5000000, 5000000, 5000)
        self.assertEqual(preflight["coverage"], "current")
        completed = current + "END mono_us=4000100 reason=deadline\n"
        stale = self.module.diagnostic_coverage(completed, 10000000, 20000000, 10000)
        self.assertEqual(stale["coverage"], "none")
        self.assertEqual(stale["reason"], "recording_ended_before_collection")
        self.assertTrue(stale["recording_complete"])
        partial = self.module.diagnostic_coverage(completed, 2000000, 10000000, 2000)
        self.assertEqual(partial["coverage"], "partial")
        self.assertTrue(partial["recording_complete"])
        stopped_near_end = self.module.diagnostic_coverage(completed, 2000000, 5000000, 2000)
        self.assertEqual(stopped_near_end["coverage"], "partial")
        self.assertEqual(stopped_near_end["reason"], "recording_ended_during_collection")
        self.assertEqual(self.module.diagnostic_coverage(current, 5000000, 6000000, 5000)["coverage"], "none")

    def test_invalid_trace_and_clock_relationship_are_unavailable(self):
        header = ("AUDIO_DIAG schema=1 clock=CLOCK_MONOTONIC mono_us=1000000 "
                  "wall_unix_ms=1000 duration_s=900 capture_start_us=1000000 max_bytes=8388608\n")
        window = "WINDOW seq=1 start_us=1000000 end_us=2000000 dropped=0\n"
        for text in ("", header, header + "WINDOW broken\n", header + window +
                     "WINDOW seq=3 start_us=3000000 end_us=4000000 dropped=0\n",
                     header + window + "END BROKEN\n", header + window +
                     "END mono_us=1000001 reason=deadline\n",
                     header + window + "END mono_us=2000001 reason=deadline\n" + window):
            self.assertEqual(self.module.diagnostic_coverage(text, 1000000, 4000000, 1000)["coverage"], "unavailable")
        foreign = self.module.diagnostic_coverage(header + window, 1000000, 2000000, 100000)
        self.assertEqual(foreign["reason"], "clock_relationship_unverified")
        self.assertEqual(foreign["coverage"], "unavailable")

    def test_existing_acr_messages_keep_only_approved_fields(self):
        for n in (4707, 4704, 4459, 6272, 8918):
            event = self.module.kernel_event(f"6,12,34000,-;hdmitx: hw: aud_n_para = {n}")
            self.assertEqual((event["kind"], event["n"]), ("hdmi_acr_n", n))
        selected = self.module.kernel_event("6,12,34000,-;hdmitx: fs = 2, cd = 4, tmds_clk = 297000")
        self.assertEqual((selected["fs_enum"], selected["depth_enum"], selected["lookup_clock_khz"]), (2, 4, 297000))
        source = self.module.kernel_event("6,12,34000,-;hdmitx: hw: hdmitx tx_aud_src = 1, audio_param->channel_num = 1, hdev->aud_output_ch = 2")
        self.assertEqual((source["kind"], source["tx_aud_src"]), ("hdmi_audio_source", 1))
        private = self.module.kernel_event("6,12,34000,-;hdmitx: hw: aud_n_para = 4707 PRIVATE token=SECRET")
        self.assertNotIn("n", private)
        self.assertNotIn("SECRET", json.dumps(private))

    def test_private_data_and_mark_validation(self):
        clean = self.module.redact("hdmi smb://user:pass@192.168.0.2/music 192.168.0.2 00:11:22:33:44:55")
        for secret in ("user", "pass", "192.168", "00:11"):
            self.assertNotIn(secret, clean)
        self.assertIsNone(self.module.mark("token=PRIVATE"))
        self.assertEqual(self.module.mark("55 yes")["automatic_recovery"], "yes")
        self.assertEqual(self.module.mark("\n")["track_position"], "unknown")
        self.assertTrue(self.module.AUDIO.search("snd_soc: playback FIFO underrun"))
        for message in ("alsa: cannot open /home/nissel/Music/Private Album.flac",
                        "hdmi: receiver serial=PRIVATE123",
                        "hdmi audio username=PRIVATE 2001:db8::1234 token=SECRET"):
            exported = json.dumps(self.module.kernel_event("6,12,34000,-;" + message))
            for secret in ("PRIVATE", "SECRET", "/home", "2001:db8", "nissel"):
                self.assertNotIn(secret, exported)
        event = self.module.kernel_event("3,12,34000,-;snd_soc: FIFO underrun rate=44100 errno=-32")
        self.assertEqual(event["kind"], "fifo_underrun")
        self.assertEqual(event["rate_hz"], 44100)
        self.assertEqual(event["errno"], -32)

    def test_partial_input_is_bounded_and_does_not_extend_collection(self):
        reader = self.module.MarkInput()
        self.assertEqual(reader.feed(b"55"), [])
        self.assertEqual(reader.feed(b" yes\n")[0]["track_position"], "55")
        reader.feed(b"x" * 10000)
        self.assertLessEqual(len(reader.buffer), 80)
        self.assertEqual(reader.feed(b"\n55 no\n")[0]["automatic_recovery"], "no")
        with tempfile.TemporaryDirectory() as temporary:
            # Actual collect loop, real selector/pipe, synthetic config and no
            # /dev/kmsg access. Keep the input writer open without a newline.
            code = r'''
import importlib.util, os, pathlib, sys
sys.dont_write_bytecode=True
spec=importlib.util.spec_from_file_location('collector',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.TEMP=pathlib.Path(sys.argv[2])
m.configuration=lambda: {'nodes':{}}
original_open=os.open
def synthetic_open(path,*args,**kwargs):
    if str(path)=='/dev/kmsg': raise FileNotFoundError(2,'synthetic unavailable')
    return original_open(path,*args,**kwargs)
m.os.open=synthetic_open
# Completed earlier recording: collection must warn and metadata must say no
# current coverage, even though the old recording has its END footer.
t=m.time.monotonic_ns()//1000; w=m.time.time_ns()//1000000
(m.TEMP/'audio-diagnostic.log').write_text(
 f'AUDIO_DIAG schema=1 clock=CLOCK_MONOTONIC mono_us={t-20000000} wall_unix_ms={w-20000} duration_s=10 capture_start_us={t-20000000} max_bytes=8388608\n'
 f'WINDOW seq=1 start_us={t-20000000} end_us={t-10000000} dropped=0\n'
 f'END mono_us={t-10000000} reason=deadline\n')
m.collect('native',0.15)
'''
            child = subprocess.Popen([sys.executable, "-c", code,
                                      str(ROOT / "tools/Linux/audio-diagnostic.py"), temporary],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                child.stdin.write(b"55")
                child.stdin.flush()
                child.wait(timeout=2)
                self.assertEqual(child.returncode, 0, child.stderr.read().decode())
                self.assertEqual(len(list(Path(temporary).glob("*.zip"))), 1)
                output = child.stdout.read().decode()
                self.assertIn("Kodi diagnostic preflight: none", output)
                self.assertIn("Kodi diagnostic coverage: none", output)
                import zipfile
                with zipfile.ZipFile(next(Path(temporary).glob("*.zip"))) as archive:
                    metadata = json.loads(archive.read("capture.json"))
                self.assertTrue(metadata["diagnostic"]["recording_complete"])
                self.assertEqual(metadata["diagnostic"]["coverage"], "none")
                self.assertLess(metadata["collection_start_us"], metadata["collection_end_us"])
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                child.stdin.close()
                child.stdout.close()
                child.stderr.close()

    def test_closed_output_restores_input_flags(self):
        with tempfile.TemporaryDirectory() as temporary:
            code = r'''
import builtins, importlib.util, os, pathlib, sys
sys.dont_write_bytecode=True
spec=importlib.util.spec_from_file_location('collector',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.TEMP=pathlib.Path(sys.argv[2]);m.configuration=lambda: {'nodes':{}}
original_open=os.open
def synthetic_open(path,*args,**kwargs):
    if str(path)=='/dev/kmsg': raise FileNotFoundError(2,'synthetic unavailable')
    return original_open(path,*args,**kwargs)
m.os.open=synthetic_open
before=os.get_blocking(sys.stdin.fileno())
def closed_output(*args,**kwargs): raise BrokenPipeError('synthetic closed output')
builtins.print=closed_output
try: m.collect('native',0.05)
except BrokenPipeError: pass
assert os.get_blocking(sys.stdin.fileno())==before
'''
            result = subprocess.run([sys.executable, "-c", code,
                                     str(ROOT / "tools/Linux/audio-diagnostic.py"), temporary],
                                    input="", capture_output=True, text=True, timeout=2)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_allowlist_unavailable_and_budgets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            node = root / "proc/asound/card0/pcm3p/sub0"
            node.mkdir(parents=True)
            (node / "hw_params").write_text("rate: 44100 (44100/1)\n")
            (node / "status").write_text("PRIVATE_STATUS_MUST_NOT_BE_READ")
            frac = root / "sys/class/amhdmitx/amhdmitx0/frac_rate_policy"
            frac.parent.mkdir(parents=True)
            frac.write_text("0\n")
            result = self.module.configuration(root)
            self.assertNotIn("PRIVATE", json.dumps(result))
            self.assertTrue(result["nodes"]["proc/asound/card0/pcm3p/sub0/hw_params"]["available"])
            self.assertFalse(result["audio_clocks"]["available"])
            self.assertEqual(result["nodes"]["sys/class/amhdmitx/amhdmitx0/frac_rate_policy"]["text"], "0\n")
            path = root / "bounded"
            output = self.module.BoundedFile(path, 1024)
            for _ in range(100):
                output.write("x" * 100)
            output.close()
            self.assertLessEqual(path.stat().st_size, 1024)
            self.assertTrue(output.truncated)
            self.assertIn("unavailable", path.read_text())
            private = root / "private"
            private.write_text("PRIVATE_PASSWORD")
            owned = root / "audio-diagnostic.log"
            owned.symlink_to(private)
            self.assertFalse(self.module.read_node(owned, owned=True)["available"])
            import os
            fifo = root / "would-block"
            os.mkfifo(fifo)
            fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
            try:
                self.assertEqual(self.module.read_node(fifo)["reason"], "would_block")
            finally:
                os.close(fd)


if __name__ == "__main__":
    unittest.main()
