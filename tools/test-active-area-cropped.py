#!/usr/bin/env python3
"""Known coded crops bypass the connected detector startup before any work.

Real source/configuration, worker, parent hold/progress and scanner I/O gates;
actual hardware open/output/reset call sites and complete software probe owner methods.
Transport, sample decode and GUI/clock endpoints are recording fixtures. Explicit
DAR is required: decoded display dimensions alone may be a square-pixel fallback.
"""
import argparse
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
ADMISSION = runpy.run_path(str(ROOT/'tools/test-active-area-admission.py'))
function, run = ADMISSION['function'], ADMISSION['run']


def source():
    code = ADMISSION['source']()
    code = code.replace(function(code, 'int main()'), '')
    code = code.replace('static CAMLNativeWorker s_detectWorker;', r'''
int workerStarts=0;
struct RecordingWorker:CAMLNativeWorker {
 bool Start(std::function<void(const Run&)> action,std::shared_ptr<std::atomic<bool>> selected={}){
  ++workerStarts;return CAMLNativeWorker::Start(std::move(action),std::move(selected));
 }
};
static RecordingWorker s_detectWorker;
''')
    code = code.replace('inline bool ReadIdentity(Identity&){return true;}',
        'inline int identities=0,loads=0;inline bool ReadIdentity(Identity&){++identities;return true;}')
    code = code.replace('inline bool Load(const Identity&,Record& r){',
        'inline bool Load(const Identity&,Record& r){++loads;')
    pending=function(code,'bool aml_subtitle_active_area_startup_progress(const std::shared_ptr<const void>& selected,')
    code=code.replace(pending, 'std::function<void()> afterProgress;\n'+pending.replace('return ::aml_subtitle_active_area_startup_progress(selected,n,total);','const bool result=::aml_subtitle_active_area_startup_progress(selected,n,total);if(afterProgress)afterProgress();return result;'))
    aml = (ROOT/'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp').read_text()
    header = (ROOT/'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.h').read_text()
    assert 'CDVDStreamInfo  &m_hints;' in header and ', m_hints(hints)' in aml
    # Keep the actual caller-owned hints alive across output and reset. A fresh
    # local hints object per call hides hardware writes to source geometry.
    code += r'''
struct Hardware {
 struct Hints{int width,height;double aspect;struct{int dv_profile=8;}dovi;std::shared_ptr<const void> subtitleProbeSource;};
 Hints hints;Hints& m_hints=hints;
 struct{double dar=0.0;void SetVideoDAR(double value){dar=value;}}m_processInfo;
 Hardware(int w,int h,double dar,const std::shared_ptr<const void>& selected):hints{w,h,dar,{},selected}{}
'''
    for name, variable in [('Open','hints'),('Reset','m_hints')]:
        start = aml.index('  if (aml_subtitle_active_area_configure('+variable+'.width,')
        end = aml.index('aml_dv_detect_active_area_start();',start)+len('aml_dv_detect_active_area_start();')
        statement = aml[start:end]
        assert variable+'.aspect' in statement
        code += '\nvoid '+name+'(bool nativeDV){\n'+statement+'\n}\n'
    code += 'void Picture(int ratio){struct{int ratio_control;}vi{ratio};\n'
    code += function(aml,'    if (vi.ratio_control)')+'\n}\n};\n'
    for name in ('Open','Reset'):
        code += 'void Hardware'+name+'(int w,int h,double dar,bool nativeDV,const std::shared_ptr<const void>& selected){Hardware hardware(w,h,dar,selected);hardware.'+name+'(nativeDV); }\n'
    owner = (ROOT/'xbmc/cores/VideoPlayer/VideoPlayerVideo.cpp').read_text()
    code += r'''
namespace SOFTWARE {
enum class StreamHdrType {HDR_TYPE_NONE,HDR_TYPE_DOLBYVISION};
struct VideoPicture{unsigned int iWidth,iHeight;int iDisplayWidth,iDisplayHeight;};
struct CDVDVideoCodec{virtual ~CDVDVideoCodec()=default;};
struct CDVDVideoCodecFFmpeg:CDVDVideoCodec{void*GetHWAccel(){return nullptr;}};
struct CVideoPlayerVideo {
 struct{std::shared_ptr<const void> subtitleProbeSource;StreamHdrType hdrType=StreamHdrType::HDR_TYPE_NONE;double aspect=0.0;}m_hints;
 std::unique_ptr<CDVDVideoCodec>m_pVideoCodec=std::make_unique<CDVDVideoCodecFFmpeg>();
 std::shared_ptr<const void>m_subtitleProbeSource;unsigned int m_subtitleProbeWidth=0,m_subtitleProbeHeight=0;
 double m_subtitleProbeAspect=0.0;
 void UpdateSubtitleProbe(const VideoPicture&);void ResetSubtitleProbe();void StopSubtitleProbe();
};
'''
    code += '\n'.join(function(owner, s) for s in (
        'void CVideoPlayerVideo::UpdateSubtitleProbe(',
        'void CVideoPlayerVideo::ResetSubtitleProbe()',
        'void CVideoPlayerVideo::StopSubtitleProbe()'))+'\n}\n'
    amlutils=(ROOT/'xbmc/utils/AMLUtils.cpp').read_text()
    code+='\n'+function(amlutils,'bool aml_dv_auto_letterbox_get(')
    return code+TEST


TEST = r'''
void ReadyParent(Parent& p,const std::shared_ptr<const void>& selected){
 p.m_CurrentVideo.id=0;p.m_CurrentVideo.hint.subtitleProbeSource=selected;
 p.m_State.streamsReady=true;p.m_activeAreaVideoReady=true;
 p.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_INSYNC;
 p.m_activeAreaWaitStart=std::chrono::steady_clock::now()-600ms;
}
void NoWork(Parent& p,int notificationsBefore){
 assert(!p.WaitForActiveAreaDetection());
 assert(!aml_subtitle_active_area_startup_held(p.m_CurrentVideo.hint.subtitleProbeSource));
 assert(p.m_clock.speed==DVD_PLAYSPEED_NORMAL&&!p.m_activeAreaProgress);
 assert(PLAYER_FIXTURE::notifications==notificationsBefore);
 assert(workerStarts==0&&entries==0&&reads==0&&seeks==0);
 assert(ACTIVE_AREA_CACHE::identities==0&&ACTIVE_AREA_CACHE::loads==0);
 assert(!s_detectSource->pending&&!s_detectSource->result&&!s_detectSource->subtitleResult);
 assert(!s_detectSource->retained&&!s_detectSource->retainedSubtitle);
 assert(s_detectState==DV_DETECT_SKIP_NON16X9);
}
void Counters(){workerStarts=entries=reads=seeks=0;ACTIVE_AREA_CACHE::identities=ACTIVE_AREA_CACHE::loads=0;}
int main(){
 ready();CAMLSession sessionValue;init(sessionValue);
 // Same exact geometry as both Silo episodes; independent native/subtitle
 // enables, cache ON/OFF and wait ON/OFF all obey one eligibility boundary.
 for(bool native:{false,true})for(int mode:{1,2,3})for(bool cache:{false,true})for(bool wait:{false,true}){
  if(!native&&mode==2)continue;
  settingsValue.detect=mode&1;dvDetection=mode&2;settingsValue.wait=wait;settingsValue.cache=cache;
  for(const auto& title:{"Silo-The-Drive","Silo-Who-Are-You"}){
   aml_dv_detect_set_file(title);aml_subtitle_active_area_startup_begin();
   const auto selected=aml_subtitle_active_area_source(0,173);Counters();
   CVideoPlayerVideo video;Parent parent(video);ReadyParent(parent,selected);
   const int shown=PLAYER_FIXTURE::notifications;
   // Query in the configure-to-start interval: no admission hold is allowed.
   assert(aml_subtitle_active_area_configure(3840,1606,native,true,selected,3840.0/1606.0));
   // A seek can rearm admission before the first parent query/start call.
   aml_subtitle_active_area_invalidate(true,true);
   NoWork(parent,shown);
   scanHook=[](const auto&,const auto&){assert(false&&"coded crop reached scanner");};
   {CAMLNativeTransaction owner;assert(owner.TryBegin());HardwareOpen(3840,1606,3840.0/1606.0,native,selected);}
   NoWork(parent,shown);
   // Immediate seek/reset must not rearm a crop's requested startup admission.
   aml_subtitle_active_area_invalidate(true,true);NoWork(parent,shown);
   {CAMLNativeTransaction owner;assert(owner.TryBegin());HardwareReset(3840,1606,3840.0/1606.0,native,selected);}
   NoWork(parent,shown);
   if(native){
    aml_dv_set_active_area_geometry(3840,1606,true);uint16_t t,b,l,r;
    assert(aml_dv_auto_letterbox_get(t,b,l,r)&&t==277&&b==277&&l==0&&r==0);
   }
  }
 }
 // Reported source dimensions/DAR, then a deliberately different hardware DAR.
 // ratio_control is a synthetic stimulus: the runtime log has no reset DAR.
 settingsValue.detect=true;dvDetection=true;settingsValue.cache=true;settingsValue.wait=true;
 aml_dv_detect_set_file("Spider-Man-reset");aml_subtitle_active_area_startup_begin();
 auto spider=aml_subtitle_active_area_source(0,173);Counters();
 CVideoPlayerVideo spiderVideo;Parent spiderParent(spiderVideo);ReadyParent(spiderParent,spider);
 const int spiderShown=PLAYER_FIXTURE::notifications;
 Hardware hardware(3832,1600,3832.0/1600.0,spider);
 {CAMLNativeTransaction owner;assert(owner.TryBegin());hardware.Open(true);}
 NoWork(spiderParent,spiderShown);
 for(int n=0;n<4;++n){
  hardware.Picture(27307);assert(hardware.m_processInfo.dar==65536.0/27307);
  aml_subtitle_active_area_invalidate(true,true);
  {CAMLNativeTransaction owner;assert(owner.TryBegin());hardware.Reset(true);}
  NoWork(spiderParent,spiderShown);
  assert(hardware.hints.aspect==3832.0/1600.0);
 }
 // Padding is geometry-generated: the fixture has no authored RPU L5 block.
 aml_dv_set_active_area_geometry(3832,1600,true);uint16_t st,sb,sl,sr;
 assert(aml_dv_auto_letterbox_get(st,sb,sl,sr)&&st==277&&sb==277&&sl==0&&sr==0);
 // Legitimate changes update the same live hints (as sequence parsing does).
 // An old cropped decision must not survive a source DAR/SAR or size change.
 settingsValue.wait=false;settingsValue.cache=false;scanHook=scan;
 for(int change=0;change<4;++change){
  hardware.hints.width=3832;hardware.hints.height=1600;
  hardware.hints.aspect=change==0?16.0/9.0:change==1?0.0:change==2?NAN:16.0/9.0;
  if(change==3){hardware.hints.width=1920;hardware.hints.height=1080;}
  hardware.Picture(27307);
  {CAMLNativeTransaction owner;assert(owner.TryBegin());hardware.Reset(true);}
  until([&]{return !s_detectSource->pending.load();});s_detectWorker.Stop();
  assert(!s_detectSource->codedCrop&&s_detectSource->completedPositions==14);
  assert(workerStarts==change+1&&entries==change+1&&reads==(change+1)*14);
 }
 // A different stream selection must be evaluated using its own hints.
 const auto replaced=aml_subtitle_active_area_source(1,173);
 Hardware replacement(1920,1080,16.0/9.0,replaced);
 {CAMLNativeTransaction owner;assert(owner.TryBegin());replacement.Open(true);}
 until([&]{return !s_detectSource->pending.load();});s_detectWorker.Stop();
 assert(s_detectSource->selection==replaced&&!s_detectSource->codedCrop&&workerStarts==5);
 // Reconfigure after the parent's pending snapshot but before pause. Current
 // eligibility must be rechecked atomically with applying/publishing the hold.
 settingsValue.detect=true;dvDetection=true;settingsValue.cache=true;settingsValue.wait=true;
 aml_dv_detect_set_file("same-selection-race");aml_subtitle_active_area_startup_begin();
 auto raced=aml_subtitle_active_area_source();assert(aml_subtitle_active_area_configure(1920,1080,false,true,raced,16.0/9.0));Counters();
 CVideoPlayerVideo raceVideo;Parent raceParent(raceVideo);ReadyParent(raceParent,raced);int raceShown=PLAYER_FIXTURE::notifications;
 PLAYER_FIXTURE::afterProgress=[&]{assert(aml_subtitle_active_area_configure(3840,1606,false,true,raced,3840.0/1606.0));};
 NoWork(raceParent,raceShown);PLAYER_FIXTURE::afterProgress={};
 // Representative square-pixel SDR/HDR scope and narrow/4:3 sources bypass.
 settingsValue.detect=true;dvDetection=true;settingsValue.cache=true;settingsValue.wait=true;
 for(const auto& size:{std::pair<int,int>{1920,800},{1280,544},{1440,1080},{640,480}}){
  aml_dv_detect_set_file("other-crop");aml_subtitle_active_area_startup_begin();const auto selected=aml_subtitle_active_area_source();Counters();
  CVideoPlayerVideo video;Parent parent(video);ReadyParent(parent,selected);int shown=PLAYER_FIXTURE::notifications;
  {CAMLNativeTransaction owner;assert(owner.TryBegin());HardwareOpen(size.first,size.second,double(size.first)/size.second,false,selected);}
  NoWork(parent,shown);
 }
 // A pre-existing accepted result from the old policy cannot survive crop
 // recognition, even with matching stream/dimensions and retained geometry.
 aml_dv_detect_set_file("old-result");const auto old=aml_subtitle_active_area_source();
 assert(aml_subtitle_active_area_configure(3840,1606,false,true,old));
 CAMLNativeWorker::Run oldRun(s_detectSource->superseded);
 detect_publish(oldRun,s_detectSource,10,10,0,0);detect_publish_subtitle(oldRun,s_detectSource,{20,20,0,0});
 aml_subtitle_active_area_invalidate(true,true);assert(s_detectSource->retained&&s_detectSource->retainedSubtitle);
 aml_subtitle_active_area_startup_begin();Counters();
 CVideoPlayerVideo oldVideo;Parent oldParent(oldVideo);ReadyParent(oldParent,old);int shown=PLAYER_FIXTURE::notifications;
 {CAMLNativeTransaction owner;assert(owner.TryBegin());HardwareReset(3840,1606,3840.0/1606.0,false,old);}
 NoWork(oldParent,shown);detect_publish(oldRun,s_detectSource,10,10,0,0);
 assert(!s_detectSource->result); // superseded callback cannot revive old geometry
 // Uncropped letterbox/fullscreen and non-square or missing aspect retain the
 // existing hold -> scan admission. Their geometry algorithm is not altered.
 settingsValue.cache=false;settingsValue.wait=true;
 struct Eligible{int w,h;double dar;};
 for(const auto& item:{Eligible{1920,1080,16.0/9.0},{3840,2160,16.0/9.0},
     {1440,1080,16.0/9.0},{720,576,4.0/3.0},{1920,800,0.0},
     {1920,800,-1.0},{1920,800,NAN},{1920,800,INFINITY}}){
  aml_dv_detect_set_file("eligible");aml_subtitle_active_area_startup_begin();auto selected=aml_subtitle_active_area_source();Counters();
  CVideoPlayerVideo video;Parent parent(video);ReadyParent(parent,selected);
  scanHook=scan;
  {CAMLNativeTransaction owner;assert(owner.TryBegin());HardwareOpen(item.w,item.h,item.dar,false,selected);}
  assert(!s_detectSource->codedCrop&&parent.WaitForActiveAreaDetection());
  s_detectWorker.Stop(); // completed when admitted, or cancellation is still safe
  // Run a deterministic full scan in ordinary background mode as well.
  settingsValue.wait=false;aml_subtitle_active_area_startup_begin();
  {CAMLNativeTransaction owner;assert(owner.TryBegin());HardwareReset(item.w,item.h,item.dar,false,selected);}
  until([&]{return !s_detectSource->pending.load();});s_detectWorker.Stop();
  assert(entries>=1&&reads>=14&&seeks>=14&&workerStarts==2);
  assert(s_detectSource->completedPositions==14);parent.WaitForActiveAreaDetection();settingsValue.wait=true;
 }
 // Actual software owner: known source aspect + current display geometry.
 // Changing only display/source aspect must bypass deduplication and renew
 // admission. Unknown source DAR cannot be inferred from synthesized display.
 settingsValue.wait=false;settingsValue.cache=false;settingsValue.detect=true;
 aml_dv_detect_set_file("software");auto selected=aml_subtitle_active_area_source();
 SOFTWARE::CVideoPlayerVideo software;software.m_hints.subtitleProbeSource=selected;
 software.m_hints.aspect=2.4;SOFTWARE::VideoPicture picture{1920,800,1920,800};Counters();
 software.UpdateSubtitleProbe(picture);s_detectWorker.Stop();assert(workerStarts==0&&s_detectSource->codedCrop);
 software.ResetSubtitleProbe();software.UpdateSubtitleProbe(picture);s_detectWorker.Stop();assert(workerStarts==0);
 // Anamorphic source, then stale DAR against a changed display, then missing DAR.
 for(int condition=0;condition<3;++condition){
  software.m_hints.aspect=2.4;picture={1920,800,1920,800};software.UpdateSubtitleProbe(picture);
  assert(s_detectSource->codedCrop);
  software.m_hints.aspect=condition==0?16.0/9.0:condition==1?2.4:0.0;
  picture.iDisplayWidth=condition==2?1920:1420;picture.iDisplayHeight=800;
  software.UpdateSubtitleProbe(picture);until([&]{return !s_detectSource->pending.load();});s_detectWorker.Stop();
  assert(workerStarts==condition+1&&!s_detectSource->codedCrop&&s_detectSource->completedPositions==14);
 }
 software.m_hints.aspect=2.4;picture={1920,800,1920,800};software.UpdateSubtitleProbe(picture);s_detectWorker.Stop();
 assert(s_detectSource->codedCrop&&workerStarts==3&&!s_detectSource->result);
 picture={1920,1080,1920,1080};software.UpdateSubtitleProbe(picture);
 until([&]{return !s_detectSource->pending.load();});s_detectWorker.Stop();assert(workerStarts==4&&!s_detectSource->codedCrop);
 software.StopSubtitleProbe();assert(!s_detectSource->result);
 std::cout<<"PASS: known crops bypass worker/cache/I/O/hold/toast at hardware open/output/reset and software geometry admission; retained/cache isolation, native padding and uncropped/anamorphic/unknown paths\n";
}
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls',action='store_true')
    args=parser.parse_args()
    code=source()
    run(code)
    if args.negative_controls:
        controls=[
            ('hardware DAR overwrites source evidence','m_processInfo.SetVideoDAR(65536.0 / vi.ratio_control);','m_hints.aspect = 65536.0 / vi.ratio_control; m_processInfo.SetVideoDAR(m_hints.aspect);'),
            ('stale pending snapshot pauses a crop','if (held && aml_subtitle_active_area_startup_try_hold(\n                  m_CurrentVideo.hint.subtitleProbeSource, [this] { SetCaching(CACHESTATE_INIT); }))','if (held && ([this] { SetCaching(CACHESTATE_INIT); return true; }()))'),
            ('crop worker bypass removed','if (source->codedCrop)\n  {\n    s_detectState.store(DV_DETECT_SKIP_NON16X9);','if (false)\n  {\n    s_detectState.store(DV_DETECT_SKIP_NON16X9);'),
            ('non-square DAR ignored','std::abs(displayAspect * height - width) > 1.0','false'),
            ('unknown DAR accepted','!std::isfinite(displayAspect) || displayAspect <= 0.0 ||\n      std::abs(displayAspect * height - width) > 1.0','(std::isfinite(displayAspect) && displayAspect > 0.0 && std::abs(displayAspect * height - width) > 1.0)'),
            ('retained geometry survives crop','allowProbe && !s_detectSource->codedCrop && !s_detectSource->policy->Blocked() && previous->width == width &&\n      previous->height == height && previous->displayAspect == displayAspect &&','allowProbe &&'),
            ('software aspect change ignored','m_subtitleProbeAspect == aspect','true'),
        ]
        for name,old,new in controls:
            assert old in code,name
            bad=code.replace(old,new,1)
            run(bad,negative=True)
            print('REJECTED:',name)


if __name__=='__main__':main()
