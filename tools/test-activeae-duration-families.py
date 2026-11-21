#!/usr/bin/env python3
"""Production RAW duration and physical-delay accounting across codec families.

Reuses packet fixture and real buffer/stats methods, replacing its two-format
constant-duration stub with the actual CAEStreamInfo declaration/GetDuration.
This tests units and codec routing, not MAT bytes or decoder replay.
"""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "paused", ROOT / "tools/test-activeae-paused-packets.py")
paused = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paused)
AE = ROOT / "xbmc/cores/AudioEngine/Engines/ActiveAE"
UTIL = ROOT / "xbmc/cores/AudioEngine/Utils"


def harness(engine, sink):
    source = paused.harness(engine, sink).split("int main()")[0]
    original = paused.block(source, "struct CAEStreamInfo") + ";"
    info = paused.block((UTIL / "AEStreamInfo.h").read_text(), "class CAEStreamInfo")
    duration = paused.block((UTIL / "AEStreamInfo.cpp").read_text(),
                            "double CAEStreamInfo::GetDuration() const")
    source = source.replace(original, info + ";\n" + duration)
    source = source.replace("CAEStreamInfo m_streamInfo;", """CAEStreamInfo m_streamInfo=[]{
      CAEStreamInfo info;info.m_type=CAEStreamInfo::STREAM_TYPE_TRUEHD;
      info.m_sampleRate=48000;return info;}();""")
    return source + TESTS


TESTS = r'''
int main(){
  using Info=CAEStreamInfo;
  struct Case {Info::DataType type;unsigned rate,dtsSamples;double ms;};
  const Case cases[]={
    {Info::STREAM_TYPE_TRUEHD,48000,0,20},
    {Info::STREAM_TYPE_TRUEHD,96000,0,20},
    {Info::STREAM_TYPE_TRUEHD,192000,0,20},
    {Info::STREAM_TYPE_TRUEHD,44100,0,21.7687074829932},
    {Info::STREAM_TYPE_TRUEHD,88200,0,21.7687074829932},
    {Info::STREAM_TYPE_TRUEHD,176400,0,21.7687074829932},
    {Info::STREAM_TYPE_AC3,48000,0,32},
    {Info::STREAM_TYPE_EAC3,48000,0,32},
    {Info::STREAM_TYPE_DTS_512,48000,0,10.6666666666667},
    {Info::STREAM_TYPE_DTS_1024,48000,0,21.3333333333333},
    {Info::STREAM_TYPE_DTS_2048,48000,0,42.6666666666667},
    {Info::STREAM_TYPE_DTSHD,48000,0,10.6666666666667},
    {Info::STREAM_TYPE_DTSHD_CORE,48000,0,10.6666666666667},
    {Info::STREAM_TYPE_DTSHD_MA,48000,0,10.6666666666667},
    {Info::STREAM_TYPE_DTSHD_MA,96000,2048,21.3333333333333},
  };
  for(const auto& test:cases){
    CActiveAEStream stream;
    auto& buffers=stream.buffers;
    auto& info=buffers.m_inputFormat.m_streamInfo;
    info.m_type=test.type;info.m_sampleRate=test.rate;info.m_dtsSamplesPerFrame=test.dtsSamples;
    assert(std::abs(info.GetDuration()-test.ms)<.000001);
    std::deque<CSampleBuffer*>* queues[6]={&buffers.m_inputSamples,&buffers.m_outputSamples,
      &buffers.resample.m_inputSamples,&buffers.resample.m_outputSamples,
      &buffers.atempo.m_inputSamples,&buffers.atempo.m_outputSamples};
    CSampleBuffer samples[18]{};
    for(int q=0;q<6;++q){
      for(int part=0;part<3;++part){
        auto& sample=samples[q*3+part];sample.id=q*3+part;
        // compressed-byte magnitude and carrier rate must not become PCM time
        sample.pkt->nb_samples=part==0?(q%2?61440:131072):0;
        sample.pkt->config.sample_rate=q%2?192000:44100;
        sample.pkt->pause_burst_ms=part==1?7:0;
        queues[q]->push_back(&sample);
      }
    }
    const double queued=6*(test.ms/1000+.007);
    assert(std::abs(buffers.GetDelay()-queued)<.000001);
    CEngineStats stats;stats.m_sinkFormat=buffers.m_inputFormat;
    stats.m_sinkDelay.delay=.123;stats.m_sinkLatency=.017;
    stats.m_bufferedSamples=2;
    stats.UpdateStream(&stream);
    stream.m_bufferedTime=.011; // received after the stats snapshot
    stats.m_streamStats.front().m_resampleRatio=1.25;
    AEDelayStatus status;stats.GetDelay(status,&stream);
    assert(std::abs(status.delay-(.140+2*test.ms/1000+(queued+.011)*1.25))<.000001);
    buffers.Flush();
    for(auto& sample:samples)assert(sample.returns==1);
    // Dropped RAW payload contributes zero, but physical sink delay survives.
    stats.m_bufferedSamples=0;stream.m_bufferedTime=0;stats.UpdateStream(&stream);
    stats.GetDelay(status,&stream);assert(std::abs(status.delay-.140)<.000001);
    stats.Reset(192000,false); // acknowledged FLUSH clears counts and old measured delay
    stats.GetDelay(status,&stream);assert(std::abs(status.delay-.017)<.000001);
  }
  // Representative PCM path retains sample/rate durations through all six queues.
  CActiveAEStream stream;stream.buffers.m_inputFormat.m_dataFormat=AE_FMT_FLOAT;
  CSampleBuffer samples[6]{};
  std::deque<CSampleBuffer*>* queues[6]={&stream.buffers.m_inputSamples,&stream.buffers.m_outputSamples,
    &stream.buffers.resample.m_inputSamples,&stream.buffers.resample.m_outputSamples,
    &stream.buffers.atempo.m_inputSamples,&stream.buffers.atempo.m_outputSamples};
  for(int i=0;i<6;++i){
    samples[i].pkt->nb_samples=i%2?480:441;
    samples[i].pkt->config.sample_rate=i%2?48000:44100;
    queues[i]->push_back(&samples[i]);
  }
  assert(std::abs(stream.buffers.GetDelay()-.060)<.000001);
  stream.buffers.Flush();for(auto& sample:samples)assert(sample.returns==1);
  std::cout<<"PASS production duration families: 6 TrueHD rates, AC3/EAC3, DTS512/1024/2048/HD/MA, six queues, pause/drop, physical sink delay, PCM and flush ownership\n";
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    engine, sink = (AE / "ActiveAE.cpp").read_text(), (AE / "ActiveAESink.cpp").read_text()
    source = harness(engine, sink)
    variants = [("production", source)]
    if args.negative_controls:
        for label, old, new in (
            ("raw-bytes-as-pcm-time", "const double packetSeconds = m_inputFormat.m_streamInfo.GetDuration() / 1000.0;",
             "const double packetSeconds = 61440.0 / 192000;"),
            ("lost-physical-delay", "status = m_sinkDelay;", "status.delay = 0;"),
            ("lost-atempo-delay", "&m_atempoBuffers->m_outputSamples}", "}"),
            ("wrong-mat-rate-family", "rate = 176400;", "rate = 192000;"),
            ("pause-milliseconds-as-seconds", "buffer->pkt->pause_burst_ms / 1000.0", "buffer->pkt->pause_burst_ms"),
        ):
            assert old in source, label
            variants.append((label, source.replace(old, new)))
    with tempfile.TemporaryDirectory(prefix="ae-duration-") as temp:
        for label, text in variants:
            cpp, exe = Path(temp) / (label + ".cpp"), Path(temp) / label
            cpp.write_text(text)
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-Wno-missing-field-initializers", "-fsanitize=address,undefined",
                            "-fno-pie", "-no-pie", str(cpp), "-o", str(exe)], check=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True)
            if label == "production":
                print(result.stdout, end="")
                if result.returncode:
                    print(result.stderr, end="")
                result.check_returncode()
            else:
                assert result.returncode and "Assertion" in result.stderr, (label, result.stderr)
                print("REJECTED:", label)


if __name__ == "__main__":
    main()
