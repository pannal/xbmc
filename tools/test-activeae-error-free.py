#!/usr/bin/env python3
"""Exercise inline FreeStream delivery through ActiveAE error-state dispatch.

Production sender, state inheritance, error transition, free handler, pause
failure/flush methods and DiscardStream execute with controlled actor/transport
boundaries. Unrelated engine state handlers are excluded from this host harness.
"""
import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
AE = ROOT / "xbmc/cores/AudioEngine/Engines/ActiveAE"
spec = importlib.util.spec_from_file_location(
    "paused_packets", ROOT / "tools/test-activeae-paused-packets.py")
paused = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paused)
block = paused.block


def harness(engine, sink):
    source = paused.harness(engine, sink)
    source = source[:source.index("int main()")]
    header = (AE / "ActiveAE.h").read_text()
    enums = block(engine, "enum AE_STATES") + ";\n" + re.search(
        r"int AE_parentStates\[\].*?;", engine, re.S).group()
    free = re.search(r"case CActiveAEDataProtocol::FREESTREAM:(.*?)"
                     r"case CActiveAEDataProtocol::FREESOUND:", engine, re.S).group()
    free = free[:free.index("case CActiveAEDataProtocol::FREESOUND:")]
    machine = block(engine, "void CActiveAE::StateMachine(")
    # Keep the production dispatch loop and selected state bodies verbatim.
    prefix = machine[:machine.index("    case AE_TOP:")]
    error = machine[machine.index("    case AE_TOP_ERROR:"):
                    machine.index("    case AE_TOP_UNCONFIGURED:")]
    play = machine[machine.index("    case AE_TOP_CONFIGURED_PLAY:"):]
    play = play[:play.index("          if (RunStages())")]
    dispatch = prefix + """
    case AE_TOP:
      if (port == &m_dataPort)
      {
        switch (signal)
        {
        @FREE@
        default:
          break;
        }
      }
      assert(false && "unexpected top-level event");
      return;
""" + error + play + """
          assert(false && "unexpected non-error play timeout");
          return;
        default:
          break;
        }
      }
      break;
    default:
      assert(false && "unexpected state");
      return;
    }
  }
}
"""
    source = source.replace("#include <memory>", "#include <memory>\n#include <list>\n#include <functional>")
    source = source.replace("constexpr int LOGERROR = 1, LOGINFO = 2;", """
constexpr int LOGERROR = 1, LOGINFO = 2, LOGDEBUG = 0;
struct CActiveAEDataProtocol {enum {FREESTREAM=101, ACC=1};};
struct CActiveAEControlProtocol {enum {ACC=1, TIMEOUT=102};};
@ENUMS@
struct CActiveAEStream;
using IAEStream=CActiveAEStream;
@PAYLOAD@
""")
    source = source.replace("  void SendOutMessage(int signal, void*, size_t) {\n",
                            "  bool paused=false;\n"
                            "  void SendOutMessage(int signal, void* data, size_t) {\n"
                            "    if(signal==CSinkControlProtocol::PAUSED)paused=*static_cast<bool*>(data);\n")
    source = source.replace("struct Port {\n", """struct Port {
  std::function<void(int, Port*, Message*)> dispatch;
  Message reply{};
  bool SendOutMessageSync(int signal, Message** result, std::chrono::seconds,
                          void* payload, size_t size) {
    assert(signal==CActiveAEDataProtocol::FREESTREAM && size==sizeof(MsgStreamFree));
    MsgStreamFree inlinePayload;
    std::memcpy(&inlinePayload,payload,size);
    reply.signal=signal;
    reply.data=reinterpret_cast<unsigned char*>(&inlinePayload);
    dispatch(signal,this,&reply);
    *result=&reply;
    return true;
  }
""")
    source = source.replace("void Message::Reply(int value, void* buffer, size_t) {",
                            "using Protocol=Port;\nvoid Message::Reply(int value, void* buffer, size_t) {")
    source = source.replace("struct CActiveAEStreamBuffers:Pool {", """
struct CActiveAEStreamBuffers:Pool {
  std::unique_ptr<Pool> GetResampleBuffers(){return std::make_unique<Pool>();}
  std::unique_ptr<Pool> GetAtempoBuffers(){return std::make_unique<Pool>();}
""")
    source = source.replace("struct CActiveAEStream {\n", """static int destroyed=0;
struct CActiveAEStream {
  virtual ~CActiveAEStream(){++destroyed;}
  std::unique_ptr<Pool> m_inputBuffers;
""")
    source = source.replace("struct CEngineStats {", "struct CEngineStats {\n"
                            "  void RemoveStream(unsigned int);\n")
    source = source.replace("#include <cassert>", "#include <cassert>\n#include <algorithm>")
    source = source.replace("  CActiveAEStream placeholder;\n  std::list<CActiveAEStream*> m_streams{&placeholder};", """
  std::list<CActiveAEStream*> m_streams;
  void AbandonPausePreservation();
  std::vector<std::unique_ptr<Pool>> m_discardBufferPools;
  Port m_dataPort,m_controlPort;
  CActiveAE(){
    m_dataPort.dispatch=[this](int signal,Port* port,Message* msg){
      StateMachine(signal,port,msg);
    };
  }
  void StateMachine(int,Protocol*,Message*);
  bool FreeStream(IAEStream*,bool);
  void DiscardStream(CActiveAEStream*);
  void ClearDiscardedBuffers(){m_discardBufferPools.clear();}
  void LoadSettings(){}
""")
    source = source.replace("  static constexpr int AE_TOP_CONFIGURED_PLAY=2;\n", "")
    source += "\n" + dispatch.replace("@FREE@", free)
    source += "\n" + block(engine, "void CActiveAE::AbandonPausePreservation(")
    source += "\n" + block(engine, "void CActiveAE::DiscardStream(")
    source += "\n" + block(engine, "void CEngineStats::RemoveStream(")
    source += "\n" + block(engine, "bool CActiveAE::FreeStream(")
    source += r'''
int main(){
  for(bool pauseTimeout:{false,true})for(bool finish:{false,true}){
    CActiveAE engine;
    auto* stream=new CActiveAEStream;
    engine.m_streams.push_back(stream);
    engine.m_state=AE_TOP_CONFIGURED_PLAY;
    CSampleBuffer pending{1}, processing{2};
    stream->m_processingSamples.push_back(&processing);
    engine.sink.m_dataPort.Queue(CSinkDataProtocol::SAMPLE,&pending,true);
    engine.m_sink.m_controlPort.timeout=pauseTimeout;
    engine.m_sink.m_controlPort.error=!pauseTimeout;
    engine.m_sink.m_controlPort.flushTimeout=true;
    engine.Pause(stream);
    assert(stream->m_paused && engine.m_extError && engine.m_sinkFlushPending);
    assert(engine.m_sink.m_controlPort.paused && pending.returns==0);
    engine.StateMachine(CActiveAEControlProtocol::TIMEOUT,nullptr,nullptr);
    assert(engine.m_state==AE_TOP_ERROR && engine.m_streams.front()==stream);
    int deleted=destroyed;
    // Free's production sender copies an inline MsgStreamFree; no resume/recovery first.
    bool acknowledged=engine.FreeStream(stream,finish);
    assert(acknowledged && engine.m_dataPort.reply.signal==CActiveAEDataProtocol::ACC);
    assert(engine.m_streams.empty() && destroyed==deleted+1);
    assert(engine.m_stats.m_streamStats.empty());
    assert(processing.returns==1 && !engine.m_sink.m_controlPort.paused);
    assert(engine.m_state==AE_TOP_ERROR && engine.configured==0 && engine.submitted==0);
    assert(engine.m_sinkFlushPending && pending.returns==0);
    // Its already queued timed-out pause/flush still owns the sink packet.
    engine.m_sink.m_controlPort.flushTimeout=false;
    assert(engine.FlushEngine() && pending.returns==1 && !engine.m_sinkFlushPending);
  }
  std::cout<<"production inline FreeStream/error dispatch, destruction, ACK and pause release passed\n";
}
'''
    return source.replace("@ENUMS@", enums).replace(
        "@PAYLOAD@", block(header, "struct MsgStreamFree") + ";")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--negative-control", action="store_true")
    args = parser.parse_args()
    engine = (AE / "ActiveAE.cpp").read_text()
    sink = (AE / "ActiveAESink.cpp").read_text()
    variants = [("production", engine)]
    if args.negative_control:
        variants.extend((
            ("pointer-to-pointer-payload", engine.replace(
                "msgStreamFree = reinterpret_cast<MsgStreamFree*>(msg->data);",
                "msgStreamFree = *(MsgStreamFree**)msg->data;")),
            ("ack-without-removal", engine.replace(
                "DiscardStream(msgStreamFree->stream);", "(void)msgStreamFree;")),
            ("missing-pause-release", engine.replace("bool paused = false;", "bool paused = true;")),
        ))
    with tempfile.TemporaryDirectory(prefix="ae-error-free-") as directory:
        root = Path(directory)
        for name, impl in variants:
            if name != "production":
                assert impl != engine, name
            cpp, binary = root / (name + ".cpp"), root / name
            cpp.write_text(harness(impl, sink))
            subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                            "-fsanitize=address,undefined", "-fno-pie", "-no-pie",
                            str(cpp), "-o", str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            if name == "production":
                print(result.stdout, end="")
                if result.returncode:
                    print(result.stderr, end="")
                result.check_returncode()
            else:
                assert result.returncode != 0 and (
                    "Assertion" in result.stderr or "runtime error:" in result.stderr), (
                        name, result.stderr)
                print(name + ": rejected")


if __name__ == "__main__":
    main()
