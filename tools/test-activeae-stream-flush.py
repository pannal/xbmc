#!/usr/bin/env python3
"""Whole production SFlushStream resets paused packet/servo/tempo ownership.

Reuses packet fixture and real buffer flush/stat/engine barrier methods. Limiter,
sync-error, stream-port purge and resampler ratio setters are recording boundaries.
No actual decoder, resampler, tempo algorithm or worker thread is claimed here.
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


def harness(engine, sink):
    source = paused.harness(engine, sink).split("int main()")[0]
    source = source.replace(" double GetRR(){return 1;}", """
 double ratio=1;int ratioResets=0;
 double GetRR(){return ratio;}
 void SetRR(double value,double){ratio=value;++ratioResets;}
""")
    source = source.replace(
        "struct {double GetLastError(int64_t& time){time=0;return 0;}} m_syncError;",
        """struct {
          int flushes=0;double error=23;
          void Flush(){++flushes;error=0;}
          double GetLastError(int64_t& time){time=0;return error;}
        }m_syncError;""")
    source = source.replace("struct CActiveAEStream {\n", """struct CActiveAEStream {
  double m_lastPts=100,m_lastPtsJump=200,m_resampleIntegral=30;
  std::chrono::milliseconds m_errorInterval{7};
  struct Port{int purges=0;void Purge(){++purges;}}port;
  Port* m_streamPort=&port;
  struct Limiter{int resets=0;void Reset(){++resets;}}m_limiter;
  int freeResets=0;void ResetFreeBuffers(){++freeResets;}
""")
    source = source.replace("  bool FlushEngine();", """
  struct {double atempoThreshold=2;}m_settings;
  void SFlushStream(CActiveAEStream*);
  bool FlushEngine();""")
    return source + paused.block(engine, "void CActiveAE::SFlushStream(") + TESTS


TESTS = r'''
int main(){
  for(bool shared:{false,true})for(bool raw:{false,true}){
    CActiveAE engine;CActiveAEStream stream,peer;
    if(shared)engine.m_streams.push_back(&peer);
    if(!raw)engine.m_mode=CActiveAE::MODE_PCM;
    stream.m_paused=true;stream.m_bufferedTime=.3;
    stream.buffers.ratio=1.1;
    CSampleBuffer samples[8]{};
    std::deque<CSampleBuffer*>* queues[]={
      &stream.buffers.m_inputSamples,&stream.buffers.m_outputSamples,
      &stream.buffers.resample.m_inputSamples,&stream.buffers.resample.m_outputSamples,
      &stream.buffers.atempo.m_inputSamples,&stream.buffers.atempo.m_outputSamples,
      &stream.m_processingSamples};
    for(int i=0;i<7;++i)queues[i]->push_back(&samples[i]);
    engine.sink.m_dataPort.Queue(CSinkDataProtocol::SAMPLE,&samples[7],true);
    engine.SFlushStream(&stream);
    for(int i=0;i<7;++i)assert(samples[i].returns==1);
    assert(samples[7].returns==(shared?0:1));
    assert(stream.port.purges==1&&stream.freeResets==1&&stream.m_limiter.resets==1);
    assert(stream.m_bufferedTime==0&&!stream.m_paused);
    assert(stream.m_syncState==CAESyncInfo::SYNC_START);
    assert(stream.m_syncError.error==0&&stream.m_syncError.flushes==1);
    assert(stream.m_lastPts==0&&stream.m_lastPtsJump==0);
    assert(stream.m_errorInterval==1000ms&&stream.m_resampleIntegral==0);
    assert(stream.buffers.ratio==1&&stream.buffers.ratioResets==1);
    assert(engine.m_stats.updates==1);
    if(shared){assert(engine.m_stats.resets==0);assert(engine.FlushEngine());}
    else assert(engine.m_stats.resets==1);
    assert(samples[7].returns==1);
  }
  std::cout<<"PASS whole SFlushStream: paused RAW/PCM, six processing/tempo queues, owned samples, single/shared streams, timestamp/error/ratio/limiter reset\n";
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-controls", action="store_true")
    args = parser.parse_args()
    engine, sink = (AE / "ActiveAE.cpp").read_text(), (AE / "ActiveAESink.cpp").read_text()
    variants = [("production", engine)]
    if args.negative_controls:
        for label, old, new in (
            ("stale-servo-integral", "stream->m_resampleIntegral = 0.0;", ""),
            ("stale-timestamp", "stream->m_lastPts = 0.0;", ""),
            ("stale-atempo-ratio", "stream->m_processingBuffers->SetRR(1.0, m_settings.atempoThreshold);", ""),
            ("paused-packet-leak", "stream->m_processingBuffers->Flush();", ""),
        ):
            assert old in engine, label
            variants.append((label, engine.replace(old, new)))
    with tempfile.TemporaryDirectory(prefix="ae-stream-flush-") as temp:
        for label, impl in variants:
            cpp, exe = Path(temp) / (label + ".cpp"), Path(temp) / label
            cpp.write_text(harness(impl, sink))
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
