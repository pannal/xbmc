#!/usr/bin/env python3
"""Production preroll target transport, corrected timeline and flush lifetime.

Reuses the real queue/reset/flush/parent lifecycle fixture. Adds the complete
ProcessVideoData and CheckPlayerInit methods and actual packet message type/
constructor/destructor. Unrelated demux continuity effects are recording inputs;
CheckContinuity/UpdateCorrection also run against a delayed-preroll witness;
this is host scheduling, not player threads or hardware. Native GetPicture and
frame ownership have separate production-function suites.
"""
import argparse
from pathlib import Path
import runpy
import tempfile

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / 'tools/test-player-lifecycle.py'))
extract = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def harness():
    source = fixture['harness']().replace('int main() { timeout_recovery();', 'void adjacent_checks() { timeout_recovery();')
    message = (ROOT / 'xbmc/cores/VideoPlayer/DVDMessage.h').read_text()
    impl = (ROOT / 'xbmc/cores/VideoPlayer/DVDMessage.cpp').read_text()
    parent = (ROOT / 'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
    video = (ROOT / 'xbmc/cores/VideoPlayer/VideoPlayerVideo.cpp').read_text()
    # Retain the fixture's packet IDs for its existing queue checks, while using
    # the real target-bearing message and constructor for the new video path.
    declaration = extract(message, 'class CDVDMsgDemuxerPacket :') + ';'
    declaration = declaration.replace('  DemuxPacket* m_packet;', '''  int id=0;
  explicit CDVDMsgDemuxerPacket(int value):CDVDMsgDemuxerPacket(new DemuxPacket){id=value;}
  DemuxPacket* m_packet;''')
    old = extract(source, 'struct CDVDMsgDemuxerPacket :') + ';'
    source = source.replace(old, declaration)
    source = source.replace('struct DemuxPacket { int iSize=7; };', '''struct DemuxPacket { int iSize=7; double dts=0,pts=0,m_ptsOffsetCorrection=0; };
struct CDVDDemuxUtils {static void FreeDemuxPacket(DemuxPacket* p){delete p;}};''')
    constructors = '\n'.join(extract(impl, sig) for sig in (
        'CDVDMsgDemuxerPacket::CDVDMsgDemuxerPacket(', 'CDVDMsgDemuxerPacket::~CDVDMsgDemuxerPacket()'))
    source = source.replace('struct DVDMessageListItem {', constructors + '\nstruct DVDMessageListItem {')
    source = source.replace('struct CCurrentStream {', 'constexpr int STREAM_AUDIO=1,STREAM_VIDEO=2,AV_CODEC_ID_DTS=1,AV_PROFILE_DTS=2,AV_CODEC_ID_H264=3;\nstruct CDemuxStream {};\nstruct CCurrentStream {')
    source = source.replace('AV_SYNC_NONE,AV_SYNC_CONT,AV_SYNC_FORCE', 'AV_SYNC_NONE,AV_SYNC_CONT,AV_SYNC_FORCE,AV_SYNC_CHECK')
    source = source.replace('  int id=0,syncState=', '  int type=STREAM_VIDEO,player=2;\n  int id=0,syncState=')
    declarations = '''
  bool realContinuity=false;
  double correctionDelta=0;
  bool CheckNativeContinuity(CCurrentStream&,DemuxPacket*);
  void UpdateCorrection(DemuxPacket*,double);
  bool CheckPlayerInit(CCurrentStream&);
  void ProcessVideoData(CDemuxStream*,DemuxPacket*);
  void CheckStreamChanges(CCurrentStream&,CDemuxStream*) {}
  bool CheckSceneSkip(CCurrentStream&) {return false;}
  bool CheckContinuity(CCurrentStream& current,DemuxPacket* packet) {
    if(realContinuity)return CheckNativeContinuity(current,packet);
    m_offset_pts+=correctionDelta;
    packet->pts-=correctionDelta; packet->dts-=correctionDelta;
    correctionDelta=0; return false;
  }
  void UpdateTimestamps(CCurrentStream& current,DemuxPacket* packet) {current.dts=packet->dts;}
'''
    source = source.replace('  CVideoPlayerVideo* m_VideoPlayerVideo;', declarations + '\n  CVideoPlayerVideo* m_VideoPlayerVideo;')
    source=source.replace('  bool inited=false; unsigned packets=0;',
      '  bool inited=false; unsigned packets=0;\n  struct {int codec=0,profile=0;} hint;\n  double duration=40000;double dts_end() const{return dts+duration;}')
    source=source.replace('#include <algorithm>', '#include <algorithm>\n#include <cmath>\nconstexpr double DVD_MSEC_TO_TIME(double x){return x*1000;}')
    source += '\n' + extract(parent,'static void UpdateLimits(')
    source += '\n' + extract(parent,'void CVideoPlayer::UpdateCorrection(').replace('CVideoPlayer::','Parent::')
    source += '\n' + extract(parent,'bool CVideoPlayer::CheckContinuity(').replace('CVideoPlayer::CheckContinuity','Parent::CheckNativeContinuity')
    source += '\n' + '\n'.join(extract(parent, sig).replace('CVideoPlayer::', 'Parent::') for sig in (
        'bool CVideoPlayer::CheckPlayerInit(', 'void CVideoPlayer::ProcessVideoData('))
    source = source.replace('bool GetBool(int) const {return false;}', 'bool resetPts=false; bool GetBool(int) const {return resetPts;}')
    # Execute the exact target-update statements from the real packet handler.
    begin = video.index('      const auto& prerollTarget =')
    update = video[begin:video.index('\n\n', begin)]
    source = source.replace('delivered.push_back(std::static_pointer_cast<CDVDMsgDemuxerPacket>(pMsg)->id);',
                            '{' + update + '\n delivered.push_back(std::static_pointer_cast<CDVDMsgDemuxerPacket>(pMsg)->id);}')
    return source + TESTS


TESTS = r'''
static void packet(Parent& parent,double dts,double pts) {
  auto p=new DemuxPacket; p->dts=dts; p->pts=pts;
  parent.ProcessVideoData(nullptr,p);
}
static void seek(Parent& parent,double target,bool accurate=true) {
  parent.FlushBuffers(target,accurate,true);
  complete(*parent.m_VideoPlayerVideo);
  assert(parent.ContinueParentLifecycle());
  parent.m_VideoPlayerVideo->Step(); // queued general synchronization
}
int main() {
  adjacent_checks();
  CVideoPlayerVideo video; Parent parent(video);
  parent.m_offset_pts=100000000;
  seek(parent,3633254000);
  assert(parent.m_videoPrerollTarget==3533254000);
  assert(parent.m_CurrentVideo.startpts==3533254000 && parent.m_CurrentAudio.startpts==3533254000);
  packet(parent,3526373000,3526373000);
  video.Step(); assert(video.m_pVideoCodec->prerollTarget==3533254000);
  assert(!parent.m_CurrentVideo.inited);
  // Input passes the chapter target before old decoded pictures finish arriving.
  packet(parent,3533260000,3533300000); video.Step();
  assert(parent.m_CurrentVideo.inited && video.m_pVideoCodec->prerollTarget==3533254000);
  packet(parent,3533300000,3533340000); video.Step();
  assert(video.m_pVideoCodec->prerollTarget==3533254000);
  // Separate EL messages have no update; a retry keeps the same message metadata.
  auto el=std::make_shared<CDVDMsgDemuxerPacket>(new DemuxPacket,false);
  assert(!el->m_prerollTarget);
  video.SendMessage(el);video.Step();assert(video.m_pVideoCodec->prerollTarget==3533254000);
  video.SendMessage(el);video.Step();assert(video.m_pVideoCodec->prerollTarget==3533254000);
  // A decoder-local reset retains the floor while true queued flush completion clears it.
  video.SendMessage(message(CDVDMsg::GENERAL_RESET));video.Step();
  assert(video.m_pVideoCodec->prerollTarget==3533254000);
  video.m_pVideoCodec->ready=true;video.Step();assert(video.m_pVideoCodec->prerollTarget==3533254000);
  video.Flush(true);video.Step();assert(video.m_pVideoCodec->prerollTarget==3533254000);
  video.m_pVideoCodec->ready=true;video.Step();assert(video.m_pVideoCodec->prerollTarget==DVD_NOPTS_VALUE);
  // Backward/forward/paused seeks replace the normalized floor. Continuity
  // corrects new packets into the existing domain without moving that floor.
  for (double target : {4181510000.,1000000000.,8000000000.}) {
    parent.m_playSpeed=0;seek(parent,target);
    const double floor=target-parent.m_offset_pts;
    parent.correctionDelta=5000000;
    packet(parent,target-parent.m_offset_pts-1000000,target-parent.m_offset_pts-1000000);
    video.Step();
    assert(parent.m_CurrentVideo.startpts==floor);
    assert(video.m_pVideoCodec->prerollTarget==floor);
    assert(!parent.m_CurrentVideo.inited);
  }
  // Real continuity normalizes a new 1100s segment to 900.04s. Moving the
  // 906.881s floor by that new offset would admit already queued 900s preroll.
  parent.m_offset_pts=0;parent.realContinuity=true;seek(parent,906881000);
  parent.m_CurrentAudio.id=-1;parent.m_CurrentAudio.dts=DVD_NOPTS_VALUE;
  parent.m_CurrentVideo.dts=parent.m_CurrentVideo.lastdts=900000000;
  packet(parent,1100000000,1100000000);video.Step();
  assert(parent.m_offset_pts==199960000 && parent.m_CurrentVideo.dts==900040000);
  assert(video.m_pVideoCodec->prerollTarget==906881000 && !parent.m_CurrentVideo.inited);
  assert(900000000 < video.m_pVideoCodec->prerollTarget); // delayed old output stays preroll
  parent.realContinuity=false;
  // Existing 20s bounded relaxation can originate from audio. It must update
  // the durable normalized target and every subsequent video packet, not only its DROP bit.
  seek(parent,9000000000);
  parent.m_CurrentAudio.dts=9000000000-parent.m_offset_pts-21000000;
  assert(!parent.CheckPlayerInit(parent.m_CurrentAudio));
  assert(parent.m_videoPrerollTarget==8979000000-parent.m_offset_pts);
  packet(parent,8979000000-parent.m_offset_pts,8979000000-parent.m_offset_pts);video.Step();
  assert(video.m_pVideoCodec->prerollTarget==8979000000-parent.m_offset_pts);
  // Unknown DTS keeps input in preroll and does not manufacture a new target.
  seek(parent,1000000000);
  parent.m_CurrentVideo.dts=DVD_NOPTS_VALUE;
  assert(parent.CheckPlayerInit(parent.m_CurrentVideo));
  assert(parent.m_videoPrerollTarget==1000000000-parent.m_offset_pts);
  seek(parent,1000000000,false);
  packet(parent,1,1);video.Step();assert(video.m_pVideoCodec->prerollTarget==DVD_NOPTS_VALUE);
  // Input-stream/BD chapter fallback may not provide a timestamp. It must
  // clear the preceding known target even though the route requests accuracy.
  seek(parent,DVD_NOPTS_VALUE,true);
  packet(parent,1,1);video.Step();assert(video.m_pVideoCodec->prerollTarget==DVD_NOPTS_VALUE);
  // Existing external-subtitle reset decision precedes all target projection.
  CServiceBroker::GetSettingsComponent()->resetPts=true;
  parent.m_pSubtitleDemuxer=true;seek(parent,4181510000);
  assert(parent.m_offset_pts==0 && parent.m_CurrentAudio.startpts==4181510000);
  packet(parent,4176881000,4176881000);video.Step();
  assert(video.m_pVideoCodec->prerollTarget==4181510000);
}
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls',action='store_true')
    args=parser.parse_args()
    source=harness()
    with tempfile.TemporaryDirectory(prefix='video-preroll-target-') as tmp:
        out=Path(tmp)
        fixture['run'](source,out,'preroll-target')
        if args.negative_controls:
            controls=[
                ('lose-packet-target','m_pVideoCodec->SetPrerollTarget(*prerollTarget);','(void)prerollTarget;'),
                ('clear-target-before-reset-completes','m_pVideoCodec->Reset();','m_pVideoCodec->SetPrerollTarget(DVD_NOPTS_VALUE); m_pVideoCodec->Reset();'),
                ('lose-relaxed-target','m_videoPrerollTarget = current.dts;','m_videoPrerollTarget = current.dts + m_offset_pts;'),
                ('move-floor-on-continuity','std::make_shared<CDVDMsgDemuxerPacket>(pPacket, drop, m_videoPrerollTarget)', 'std::make_shared<CDVDMsgDemuxerPacket>(pPacket, drop, m_videoPrerollTarget-m_offset_pts)'),
                ('keep-target-after-flush','m_pVideoCodec->SetPrerollTarget(DVD_NOPTS_VALUE);','(void)m_pVideoCodec;'),
            ]
            for name,old,new in controls:
                assert old in source,name
                result=fixture['run'](source.replace(old,new),out,name,expect_failure=True)
                assert result.returncode == -6 and 'Assertion' in result.stderr, result.stderr
                assert 'Sanitizer' not in result.stderr and 'runtime error:' not in result.stderr


if __name__=='__main__':main()
