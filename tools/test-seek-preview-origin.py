#!/usr/bin/env python3
"""Pending-derived previews preserve real relative-request flags and lifecycle."""
from pathlib import Path
import runpy
import tempfile

ROOT = Path(__file__).resolve().parents[1]
review = runpy.run_path(str(ROOT / "tools/test-seek-marker-review.py"))
f, replace = review["f"], review["replace"]


def source():
    code = review["source"]()
    parent = (ROOT / "xbmc/cores/VideoPlayer/VideoPlayer.cpp").read_text()
    application = (ROOT / "xbmc/application/ApplicationPlayer.cpp").read_text()
    code = replace(code, "struct Settings {", "bool fastSeek=false;\nstruct Settings {")
    code = replace(code, "bool GetBool(int) const {return false;}",
                   "bool GetBool(int id) const {return id==2 && fastSeek;}")
    code = replace(code, "SETTING_COREELEC_AMLOGIC_DV_AUDIO_SEAMLESSBRANCH};",
                   "SETTING_COREELEC_AMLOGIC_DV_AUDIO_SEAMLESSBRANCH,SETTING_COREELEC_AMLOGIC_FAST_SEEK};")
    old = "void SeekTime(int64_t time){CDVDMsgPlayerSeek::CMode m;m.time=time;QueueTimeSeek(m);}"
    code = replace(code, old, "void SeekTime(int64_t);")
    code += f.block(parent, "void CVideoPlayer::SeekTime(int64_t").replace("CVideoPlayer::", "Parent::")
    stub = "void SeekTimeRelative(int64_t time,std::optional<int64_t> origin){player->SeekTimeRelative(time,origin);}"
    code = replace(code, stub, """
 bool waiting=false;int64_t waitingTime=0;
 bool SeekWaitingItem(int64_t time,bool relative){assert(!relative);waitingTime=time;return waiting;}
 Parent* GetControllable(){return player;}
 void SeekTimeRelative(int64_t,std::optional<int64_t>);
""")
    code += f.block(application, "void CApplicationPlayer::SeekTimeRelative(int64_t iTime,")
    return code


TEST = r"""
int main(){
 for(bool cuts:{false,true})for(bool fast:{false,true})for(bool stacked:{false,true})
 for(bool forward:{false,true}){
  Trial t;fastSeek=fast;stack->regular=stacked;stack->start=300000;
  t.player.m_Edl.cuts=cuts;
  t.player.SeekTime(130000);t.player.DrainControls();
  const int origin=130000;
  t.Arrow(forward);const int destination=origin+(forward?10000:-10000);
  const double visible=destination/1000.0+(stacked?300:0);
  assert(app->handler.GetSeekTime()==visible);
  t.FinishFlush();t.player.m_clock.value=110000000;t.player.Sample(110000,true);
  assert(!cache.GetSeekTarget() && app->handler.GetSeekTime()==visible);
  assert(app->handler.GetSeekSize()==destination/1000-110);
  t.Commit();
  auto request=std::static_pointer_cast<CDVDMsgPlayerSeek>(t.player.m_pendingSeek);assert(request);
  assert(request->GetTime()==destination && !request->GetRelative());
  assert(request->GetAccurate()==(cuts&&!fast));
  assert(request->GetBackward()==(cuts||!forward));
  assert(request->GetSync() && request->GetRestore() && !request->GetTrickPlay());
  t.player.DrainControls();
  assert(t.player.demux.targets.back()==destination+(cuts?10000:0));
 }
 {Trial t;t.player.SeekTime(130000);t.Arrow(true);
  assert(app->handler.m_seekBase);app->handler.Reset();
  t.player.ClearSeekTarget();t.player.Sample(200000,true);
  assert(!app->handler.m_seekBase && app->handler.GetSeekTime()==200);
  t.Arrow(false);t.Commit();
  auto request=std::static_pointer_cast<CDVDMsgPlayerSeek>(t.player.m_pendingSeek);
  assert(request->GetTime()==-10000 && request->GetRelative());}
 {Trial t;app->waiting=true;
  app->SeekTimeRelative(10000,std::optional<int64_t>{130000});
  assert(app->waitingTime==140000 && !t.player.m_pendingSeek);
  app->waiting=false;}
 {Trial t;t.player.SeekTime(130000);t.Arrow(true);t.Arrow(false);
  assert(!app->handler.m_seekBase && !app->handler.m_requireSeek);}
 std::puts("PASS: real pending-origin application/player precision, EDL/stack, reset/cancel and waiting-item admission");
}
"""

if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="seek-preview-origin-") as directory:
        f.run(source() + TEST, Path(directory), "seek-preview-origin")
