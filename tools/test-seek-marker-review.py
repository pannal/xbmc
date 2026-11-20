#!/usr/bin/env python3
"""Production-path combination regressions for SEEK-1 and SEEK-2.

These are ordinary failing regressions, not expected failures. Reuses the
marker/lifecycle harness, real queue, full speed/seek branches and state
publication. Device, demux and scheduling remain controlled host adapters.
"""
import argparse
from pathlib import Path
import runpy
import tempfile

ROOT = Path(__file__).resolve().parents[1]
marker = runpy.run_path(str(ROOT / "tools/test-seek-timeline-marker.py"))
f = marker["f"]
replace = marker["replace"]


def source():
    code = marker["harness"]().split("int main() {")[0]
    parent = (ROOT / "xbmc/cores/VideoPlayer/VideoPlayer.cpp").read_text()
    messages = (ROOT / "xbmc/cores/VideoPlayer/DVDMessage.h").read_text()
    code = replace(code, "PLAYER_SEEK_CHAPTER, PLAYER_VIDEO_RECOVERY",
                   "PLAYER_SEEK_CHAPTER, PLAYER_SETSPEED, PLAYER_VIDEO_RECOVERY")
    code = replace(code, "struct CVideoPlayerAudio {",
                   f.block(messages, "class CDVDMsgPlayerSetSpeed :") + ";\n" +
                   f.block(messages, "class CDVDMsgPlayerSeekChapter") + ";\nstruct CVideoPlayerAudio {")
    code = replace(code, "  void LogSyncTransition(const char*, double) {}",
                   "  double GetCurrentPts(){return 90000000;}\n"
                   "  void SetSpeed(int speed){m_speed=speed;}\n"
                   "  void LogSyncTransition(const char*, double) {}")
    code = replace(code, "constexpr int DVDSTREAM_TYPE_DVD=1,DVDSTATE_SEEK=1;",
                   "constexpr int DVDSTREAM_TYPE_DVD=1,DVDSTREAM_TYPE_PVRMANAGER=2,DVDSTATE_SEEK=1;")
    code = replace(code, "\n  std::shared_ptr<CDVDInputStream> m_pInputStream",
                   "\n  struct CInputStreamPVRBase:CDVDInputStream{void Pause(bool){}};"
                   "\n  std::shared_ptr<CDVDInputStream> m_pInputStream")
    code = replace(code, "    uint64_t epoch=0; bool accepts=true; int level=20;",
                   "    void SetSpeed(int) {}\n    uint64_t epoch=0; bool accepts=true; int level=20;")
    code = replace(code, "    void SetStateSeeking(bool active)", """
    void SetTempo(float) {} void SetSpeed(float) {} void SetFrameAdvance(bool) {}
    void SetStateSeeking(bool active)""")
    code = replace(code, "    double absolute=6000000;", "    void SetSpeed(int) {}\n    double absolute=6000000;")
    code = replace(code, "struct IPlayerCallback {void OnPlayBackSeek",
                   "struct IPlayerCallback {void OnPlayBackSpeedChanged(int) {} void OnPlayBackSeek")
    code = replace(code, "  void DrainControls();void Publish(State);", """
  int GetChapter(){return m_State.chapter;}int GetChapterCount(){return 12;}
  void SynchronizeDemuxer(){}int SeekChapter(int);
  void DrainControls();void Publish(State);""")
    code += f.block(parent, "int CVideoPlayer::SeekChapter(").replace("CVideoPlayer::", "Parent::")
    speed = f.body(parent, "else if (pMsg->IsType(CDVDMsg::PLAYER_SETSPEED))")
    code = replace(code, "    if(pMsg->IsType(CDVDMsg::PLAYER_SEEK)){",
                   "    if(pMsg->IsType(CDVDMsg::PLAYER_SETSPEED)){" + speed +
                   "}\n    else if(pMsg->IsType(CDVDMsg::PLAYER_SEEK)){")
    return code


RETIRE = r"""
int main() {
  Trial t;
  t.Arrow(true);t.Arrow(true);t.Commit();t.player.DrainControls();
  assert(cache.GetSeekTarget()==130000);
  t.Arrow(true);
  assert(app->handler.GetSeekTime()==140.0);
  t.FinishFlush();
  t.player.m_clock.value=120000000;
  t.player.Sample(120000,true);
  assert(!cache.GetSeekTarget()); // old inaccurate seek really completed
  @ASSERTION@
}
"""

SPEED = r"""
int main() {
  Trial t;
  for(int speed:{8000,1000})
    assert(t.player.queue.Put(std::make_shared<CDVDMsgPlayerSetSpeed>(
        CDVDMsgPlayerSetSpeed::SpeedParams{speed,false}))==MSGQ_OK);
  t.player.SeekTime(130000); // admission publishes before queued FF is processed
  assert(cache.GetSeekTarget()==130000);
  assert(t.player.queue.GetUserTimeSeekCount()==1);
  t.player.DrainControls(); // complete, unmodified production speed and seek branches
  assert(t.player.demux.targets==std::vector<double>{130000});
  assert(cache.GetSeekTarget()==130000 && "SEEK-2 older FF erased newer admitted user intent");
  assert(app->handler.GetSeekTime()==130.0);
  t.FinishFlush();t.player.Sample(130000,true);
  assert(!cache.GetSeekTarget());
}
"""

COMBINATIONS = {
    "rejected-queue-keeps-older": r"""
int main(){
 Trial t;t.player.SeekTime(130000);
 t.player.queue.m_bInitialized=false;t.player.SeekTime(800000);
 assert(cache.GetSeekTarget()==130000 && app->handler.GetSeekTime()==130.0);
 t.player.queue.m_bInitialized=true;t.player.DrainControls();
 assert(t.player.demux.targets==std::vector<double>{130000});
 t.FinishFlush();t.player.Sample(130000,true);assert(!cache.GetSeekTarget());
}
""",
    "chapter-supersession": r"""
int main(){
 Trial t;t.player.m_State.chapter=2;t.player.SeekTime(130000);
 t.player.queue.m_bInitialized=false;t.player.SeekChapter(4);
 assert(cache.GetSeekTarget()==130000 && t.player.m_chapterSeekTarget==0);
 t.player.queue.m_bInitialized=true;t.player.SeekChapter(4);
 assert(!cache.GetSeekTarget() && t.player.m_chapterSeekTarget==4);
 t.player.SeekTime(700000);
 assert(cache.GetSeekTarget()==700000 && app->handler.GetSeekTime()==700.0);
 assert(t.player.queue.GetUserTimeSeekCount()==2);
}
""",
    "stack-accurate-overlap": r"""
int main(){
 Trial t;stack->regular=true;stack->start=300000;
 t.Arrow(true);t.Arrow(true);t.Commit();t.player.DrainControls();
 assert(app->handler.GetSeekTime()==430.0);
 t.Arrow(true);assert(app->handler.GetSeekTime()==440.0);
 t.FinishFlush();t.player.m_clock.value=130000000;t.player.Sample(130000,true);
 assert(app->handler.GetSeekTime()==440.0);
 t.Commit();t.player.DrainControls();
 assert(t.player.demux.targets.back()==140000); // part-local demux, stack-global GUI
 t.FinishFlush();t.player.Sample(140000,true);
 assert(app->handler.GetSeekTime()==440.0 && !cache.GetSeekTarget());
}
""",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=["retirement-preview", "retirement-commit", "queued-speed", *COMBINATIONS],
                        required=True)
    args = parser.parse_args()
    if args.case in COMBINATIONS:
        test = COMBINATIONS[args.case]
    elif args.case == "queued-speed":
        test = SPEED
    elif args.case == "retirement-preview":
        test = RETIRE.replace("@ASSERTION@", """
  assert(app->handler.GetSeekTime()==140.0 &&
         "SEEK-1 retiring inaccurate older target changed newer absolute preview");""")
    else:
        test = RETIRE.replace("@ASSERTION@", """
  t.Commit();t.player.DrainControls();
  assert(t.player.demux.targets.back()==140000 &&
         "SEEK-1 newer preview committed a different absolute destination");""")
    with tempfile.TemporaryDirectory(prefix="seek-marker-review-") as directory:
        f.run(source() + test, Path(directory), args.case)


if __name__ == "__main__":
    main()
