#!/usr/bin/env python3
"""Connected public-result binding, request capture and playback consumers.

Production job manager, immutable binding registry, CFileItem capture, source
configuration/retirement, dispatcher and subtitle/native publication execute
together. Sample decoding, persistence, settings, sysfs and minimal CFileItem
storage are recording boundaries; physical renderer/HDMI behavior is not claimed.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
JOBS = runpy.run_path(str(ROOT / 'tools/test-detector-jobs.py'))
function, run = JOBS['function'], JOBS['run']


def source():
    aml = (ROOT / 'xbmc/utils/AMLUtils.cpp').read_text()
    item = (ROOT / 'xbmc/FileItem.cpp').read_text()
    # Setter now returns the exact playback generation; older extraction callers
    # may still search its former void signature while root updates those suites.
    def extract(text, signature):
        if signature == 'void aml_dv_detect_set_playback_file(' and signature not in text:
            signature = 'uint64_t aml_dv_detect_set_playback_file('
        return function(text, signature)
    JOBS['source'].__globals__['function'] = extract
    code = JOBS['source']().replace(JOBS['TEST'], '')
    if 'void aml_detector_end_playback(' not in code:
        code += '\n' + function(aml, 'void aml_detector_end_playback(')

    # The older native-worker fixture intentionally substitutes force ownership.
    # Restore the actual helper for this suite and record all parameter writes.
    old = 'static void detect_apply_l5_override_locked() {}'
    assert old in code
    code = code.replace(old, function(aml, 'static void detect_apply_l5_override_locked()\n{'))
    old = 'bool dvDetection=true;bool aml_dv_detect_active_area_enabled(){return dvDetection;}'
    assert old in code
    code = code.replace(old, '''
bool manual=false;std::array<uint16_t,4> manualOffsets{};
bool _l5_override_parse(uint16_t& t,uint16_t& b,uint16_t& l,uint16_t& r){
 if(!manual)return false;
 t=manualOffsets[0];b=manualOffsets[1];l=manualOffsets[2];r=manualOffsets[3];return true;
}
bool dvDetection=true;bool aml_dv_detect_active_area_enabled(){return dvDetection&&!manual;}
''')
    code = code.replace('bool aml_dv_l5_override_active(){return false;}',
                        'bool manual;bool aml_dv_l5_override_active(){return manual;}')
    code = code.replace('bool manual=false;std::array', 'std::array')
    old = function(code, 'struct CSysfsPath') + ';'
    code = code.replace(old, '''
std::mutex parameterMutex;std::map<std::string,int> parameters;std::string lastParameterPath;int lastParameterValue=0;
struct CSysfsPath{CSysfsPath(const char* path,int value){
 assert(session&&!session->AcquireDecoder());++writes;
 {std::lock_guard<std::mutex> guard(parameterMutex);parameters[path]=value;lastParameterPath=path;lastParameterValue=value;}
 if(writeHook)writeHook();
}};
bool wroteParameter(const std::string& name,int value){std::lock_guard<std::mutex> guard(parameterMutex);return lastParameterPath=="/sys/module/amdolby_vision/parameters/"+name&&lastParameterValue==value;}
int parameter(const std::string& name){std::lock_guard<std::mutex> guard(parameterMutex);return parameters["/sys/module/amdolby_vision/parameters/"+name];}
''')
    # Definitions are appended after their production declarations, avoiding
    # harness-specific reordering of the dispatcher/native publication logic.
    for signature in ('static void detect_clear_injection_locked(bool nativeSource)\n{',
                      'static bool detect_schedule_binding()\n{'):
        body = function(aml, signature)
        if body not in code:
            code += '\n' + body
    dispatcher = function(code, 'static bool detect_schedule_binding()\n{')
    instrumented, count = re.subn(r'(s_bindingRequested = false;\n\s*})',
        r'\1\n              binding_dispatch_step();', dispatcher, count=1)
    assert count == 1
    instrumented, count = re.subn(r'(source = s_detectSource;)',
        r'\1\n                  ++bindingDispatchSnapshots;', instrumented, count=1)
    assert count == 1
    marker = 'CLog::Log(LOGERROR, "DetectActiveArea: explicit result application failed");'
    assert instrumented.count(marker) == 1
    instrumented = instrumented.replace(marker, marker + '\n                ++bindingDispatchFailures;')
    instrumented, count = re.subn(r'(?m)^(\s*)// Publication can fail',
        r'\1binding_failure_step();\n\1// Publication can fail', instrumented, count=1)
    assert count == 1
    hook = r'''
std::atomic<bool> deferBindingDispatch{false},bindingDispatchEntered{false},releaseBindingDispatch{false};
std::atomic<int> bindingDispatchSnapshots{0},bindingDispatchFailures{0};
std::atomic<bool> deferFailureCleanup{false},failureCleanupEntered{false},releaseFailureCleanup{false};
void binding_failure_step(){
 if(!deferFailureCleanup.exchange(false))return;
 failureCleanupEntered=true;
 while(!releaseFailureCleanup.load())std::this_thread::sleep_for(1ms);
}
void binding_dispatch_step(){
 if(!deferBindingDispatch.exchange(false))return;
 bindingDispatchEntered=true;
 while(!releaseBindingDispatch.load())std::this_thread::sleep_for(1ms);
}
'''
    code = code.replace(dispatcher, hook + instrumented, 1)
    # Keep the actual scanner's final source-owned state propagation around
    # the recording I/O endpoint, so late-cleanup ownership is exercised too.
    finish = function(aml, 'static void detect_finish_scan_state(')
    recording = function(code, 'void DetectActiveAreaFromFile(')
    assert 'scanHook(source,run);' in recording
    if finish not in code:
        code = code.replace(recording, finish + '\n' + recording, 1)
    code = code.replace(recording, recording.replace(
        'scanHook(source,run);', 'scanHook(source,run);detect_finish_scan_state(source,run);'), 1)
    code += '\n' + function(aml, 'bool aml_dv_auto_letterbox_get(')
    # Assert actual CFileItem copy/reset wiring as well as executing capture.
    assert 'm_detectorBinding = item.m_detectorBinding;' in function(item, 'CFileItem& CFileItem::operator=')
    assert 'm_detectorBinding.reset();' in function(item, 'void CFileItem::Reset()')
    code += r'''
struct CFileItem {
 explicit CFileItem(std::string target):path(std::move(target)){}
 std::string path;std::map<std::string,CVariant> properties;
 std::shared_ptr<DETECTOR_PLAYBACK::Binding> m_detectorBinding;
 bool HasProperty(const std::string& k)const{return properties.count(k);}
 CVariant GetProperty(const std::string& k)const{auto p=properties.find(k);return p==properties.end()?CVariant():p->second;}
 void ClearProperty(const std::string& k){properties.erase(k);}
 void SetProperty(const std::string& k,const CVariant& value){properties[k]=value;}
 const std::string& GetDynPath()const{return path;}
 const auto& GetDetectorBinding()const{return m_detectorBinding;}
 void CaptureDetectorBinding();
};
'''
    code += '\n' + function(item, 'void CFileItem::CaptureDetectorBinding()')
    code = '#include <limits>\n#include <atomic>\nstd::atomic<bool> detectorFailLaunch{false};\n' + code
    player = (ROOT / 'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
    for signature in ('bool CVideoPlayer::CloseFile(', 'void CVideoPlayer::OnExit()'):
        assert 'aml_detector_end_playback(m_detectorPlaybackId);' in function(player, signature)

    return code + TEST


TEST = r'''
void autoStart(){CAMLNativeTransaction native;assert(native.TryBegin());aml_dv_detect_active_area_start();}
std::string startJob(const std::string& path="original.mkv"){
 auto value=aml_detector_start_job(path,-1,false);assert(value["status"].asString()=="accepted");return value["requestid"].asString();
}
CVariant completed(const std::string& id){CVariant value;until([&]{value=aml_detector_job_status(id);auto s=value["status"].asString();return s!="queued"&&s!="running"&&s!="cancelling";});s_explicitDetectorWorker.Stop();return value;}
void scan(const std::shared_ptr<DetectSource>& source,const CAMLNativeWorker::Run& state){
 source->scannedWidth=1920;source->scannedHeight=1080;source->scannedStream=2;source->scannedCodec=173;
 source->scannedAspect=16.0/9.0;source->uniqueStream=true;
 for(int n=1;n<=14;++n)source->completedPositions=n;
 detect_publish_subtitle(state,source,{133,134,0,0});detect_publish(state,source,132,132,0,0,true);
}
std::string bindingFor(const std::string& job,const std::string& target="original.mkv",int stream=-1,bool same=false){
 auto value=aml_detector_bind_job(job,target,stream,same);assert(value["status"].asString()=="ready");return value["bindingid"].asString();
}
std::shared_ptr<DETECTOR_PLAYBACK::Binding> capture(const std::string& id,const std::string& target){
 CFileItem original(target);original.SetProperty(DETECTOR_PLAYBACK::PROPERTY,id);
 original.CaptureDetectorBinding();assert(!original.HasProperty(DETECTOR_PLAYBACK::PROPERTY));
 assert(original.GetDetectorBinding());CFileItem request(original);
 original.m_detectorBinding.reset();assert(request.GetDetectorBinding());
 CFileItem second(target);second.SetProperty(DETECTOR_PLAYBACK::PROPERTY,id);second.CaptureDetectorBinding();assert(!second.GetDetectorBinding());
 return request.GetDetectorBinding();
}
std::shared_ptr<const void> openBound(const std::shared_ptr<DETECTOR_PLAYBACK::Binding>& binding,
 const std::string& target="original.mkv",int stream=2,int width=1920,int height=1080,bool native=true,double aspect=16.0/9.0){
 aml_dv_detect_set_playback_file(target,true,binding);aml_subtitle_active_area_startup_begin();
 const auto selected=aml_subtitle_active_area_source(stream,173);
 assert(aml_subtitle_active_area_configure(width,height,native,true,selected,aspect));
 return selected;
}
void applied(const std::shared_ptr<DETECTOR_PLAYBACK::Binding>& binding){
 until([&]{return binding->state==DETECTOR_PLAYBACK::State::APPLIED;});
}
void geometry(int top=132,int bottom=132,bool strict=true,int subtitleTop=133,int subtitleBottom=134){
 uint16_t t,b,l,r;
 assert(aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r,true));
 assert(t==(strict&&top==0&&bottom==0?0:subtitleTop)&&b==(strict&&top==0&&bottom==0?0:subtitleBottom)&&l==0&&r==0);
 assert(aml_dv_detect_active_area_override_get(1920,1080,t,b,l,r)==strict);
 if(strict)assert(t==top&&b==bottom&&l==0&&r==0);
}
void counters(int find,int identity,int store,int scans){assert(finds==find&&identities==identity&&stores==store&&entries==scans);}
int main(){
 (void)&detect_avio_read;(void)&detect_avio_seek;
 ready();CAMLSession sessionValue;init(sessionValue);settingsValue.wait=true;settingsValue.detect=false;dvDetection=false;
 settingsValue.cache=false;finds=identities=loads=stores=entries=0;scanHook=scan;
 const auto job=startJob();auto result=completed(job);
 assert(result["status"].asString()=="completed"&&!result["cachehit"].asBoolean());
 assert(result["strict"]["offsets"]["top"].asInteger()==132);
 assert(result["provenance"].asString()=="scanned"&&result["sampled"].asBoolean());
 counters(0,0,0,1);const auto id=bindingFor(job);auto request=capture(id,"original.mkv");
 auto selected=openBound(request);applied(request);geometry();
 assert(s_detectSource->boundUsable&&s_detectSource->policy->Blocked());
 assert(!aml_subtitle_active_area_startup_pending(selected));
 assert(request->subtitleAvailable&&request->nativeAvailable);
 assert(parameter("xbmc_force_l5_override")==1&&parameter("xbmc_override_l5_top")==132);
 autoStart();applied(request);counters(0,0,0,1); // no second scan/cache, despite automatic veto

 // Same-stream seek/reset and actual native-stop/0x0/reopen configuration.
 aml_subtitle_active_area_invalidate(true,true);aml_subtitle_active_area_reset(selected);
 assert(aml_subtitle_active_area_configure(1920,1080,true,true,selected,16.0/9.0));applied(request);geometry();
 {CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_detect_active_area_stop();}
 assert(aml_subtitle_active_area_configure(0,0,false,false,selected,0));
 assert(request->state==DETECTOR_PLAYBACK::State::ACCEPTED);
 assert(!request->subtitleAvailable&&!request->nativeAvailable&&!request->geometryAvailable);
 assert(aml_subtitle_active_area_configure(1920,1080,true,true,selected,16.0/9.0));applied(request);geometry();counters(0,0,0,1);
 assert(aml_subtitle_active_area_configure(1920,1080,true,false,selected,16.0/9.0));
 assert(request->state==DETECTOR_PLAYBACK::State::ACCEPTED&&!request->subtitleAvailable&&!request->nativeAvailable&&!request->geometryAvailable);
 assert(aml_subtitle_active_area_configure(1920,1080,true,true,selected,16.0/9.0));applied(request);geometry();

 // Bound data survives both public-handle release and source-job eviction.
 assert(aml_detector_release_binding(id)["status"].asString()=="released");
 for(int n=0;n<18;++n)completed(startJob("eviction-"+std::to_string(n)+".mkv"));
 assert(s_detectorJobs.size()==16);const auto entriesBeforeCache=entries.load();
 assert(aml_detector_job_status(job)["status"].asString()=="not_found");
 aml_subtitle_active_area_reset(selected);applied(request);geometry();

 // Genuine stream/geometry changes retire the previous result.
 const auto another=aml_subtitle_active_area_source(3,173);assert(another!=selected);
 assert(aml_subtitle_active_area_configure(1920,1080,true,true,another,16.0/9.0));
 assert(request->state==DETECTOR_PLAYBACK::State::STALE);uint16_t t,b,l,r;
 assert(!aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r,true));

 // A stale generation's late Stop must not retire a new playback.
 const auto oldGeneration=request->playback.load();
 const auto nextGeneration=aml_dv_detect_set_playback_file("independent.mkv",false,{});
 aml_detector_end_playback(oldGeneration);assert(s_detectSource->policy->id==nextGeneration&&!s_detectSource->superseded->load());

 // Validated cached data follows exactly the same capture/publication path.
 settingsValue.cache=true;cacheHit=true;saved.hasStrict=true;saved.highConfidence=true;saved.strict={132,132,0,0};
 saved.hasSubtitle=true;saved.subtitle={133,134,0,0};saved.displayAspect=16.0/9.0;saved.state=DV_DETECT_OK;saved.uniqueStream=true;
 auto cached=startJob("cached.mkv");result=completed(cached);assert(result["cachehit"].asBoolean()&&!result["sampled"].asBoolean());
 const auto cachedId=bindingFor(cached,"cached.mkv");auto cachedRequest=capture(cachedId,"cached.mkv");
 // The cache endpoint fixture supplies 1280x720 and actual job manager copies it.
 assert(cachedRequest->result->width==1280&&cachedRequest->result->height==720);
 const auto cachedSelection=openBound(cachedRequest,"cached.mkv",2,1280,720);applied(cachedRequest);
 assert(aml_subtitle_detect_active_area_get(1280,720,t,b,l,r,true)&&t==133);
 assert(aml_subtitle_active_area_configure(1920,1080,true,true,cachedSelection,16.0/9.0));
 assert(cachedRequest->state==DETECTOR_PLAYBACK::State::INCOMPATIBLE&&!cachedRequest->subtitleAvailable&&!cachedRequest->nativeAvailable&&!cachedRequest->geometryAvailable);
 assert(entries==entriesBeforeCache);settingsValue.cache=false;cacheHit=false;

 // Abandoned captured preparation becomes stale after its last request dies.
 const auto abandonedId=bindingFor(cached,"cached.mkv");
 {CFileItem abandoned("cached.mkv");abandoned.SetProperty(DETECTOR_PLAYBACK::PROPERTY,abandonedId);abandoned.CaptureDetectorBinding();assert(abandoned.GetDetectorBinding());}
 assert(aml_detector_binding_status(abandonedId)["status"].asString()=="stale");

 // Different URLs require explicit same-video association and target stream.
 auto remuxJob=startJob();completed(remuxJob);
 assert(aml_detector_bind_job(remuxJob,"remux://session",0,false)["status"].asString()=="association_required");
 assert(aml_detector_bind_job(remuxJob,"remux://session",-1,true)["status"].asString()=="association_required");
 const auto remuxId=bindingFor(remuxJob,"remux://session",0,true);auto remux=capture(remuxId,"remux://session");
 openBound(remux,"remux://session",0);applied(remux);geometry();
 const auto resizeId=bindingFor(remuxJob,"resized://session",0,true);auto resize=capture(resizeId,"resized://session");
 openBound(resize,"resized://session",0,1280,720);assert(resize->state==DETECTOR_PLAYBACK::State::INCOMPATIBLE);
 assert(!aml_subtitle_detect_active_area_get(1280,720,t,b,l,r,true));

 // Active apply is fenced by both current playback and selected-stream IDs.
 aml_dv_detect_set_playback_file("active.mkv",true,{});selected=aml_subtitle_active_area_source(2,173);
 assert(aml_subtitle_active_area_configure(1920,1080,true,true,selected,16.0/9.0));
 auto context=aml_detector_playback_status();const auto activeId=bindingFor(remuxJob,"active.mkv",2,true);
 const auto active=DETECTOR_PLAYBACK::Find(activeId);
 assert(aml_detector_apply_binding(activeId,"old",context["selectionid"].asString())["status"].asString()=="stale");
 assert(aml_detector_apply_binding(activeId,context["playbackid"].asString(),"old")["status"].asString()=="stale");
 auto accepted=aml_detector_apply_binding(activeId,context["playbackid"].asString(),context["selectionid"].asString());
 assert(accepted["status"].asString()=="accepted"||accepted["status"].asString()=="applied");applied(active);geometry();
 assert(aml_detector_apply_binding(activeId,context["playbackid"].asString(),context["selectionid"].asString())["status"].asString()=="already_used");
 const auto lateId=bindingFor(remuxJob,"active.mkv",2,true);
 aml_dv_detect_set_playback_file("new.mkv",false,{});
 assert(aml_detector_apply_binding(lateId,context["playbackid"].asString(),context["selectionid"].asString())["status"].asString()=="stale");
 assert(active->state==DETECTOR_PLAYBACK::State::STALE&&!s_detectSource->binding);

 // A dispatcher deferred on native ownership cannot publish into replacement.
 const auto pendingOldId=bindingFor(remuxJob,"pending-old.mkv",2,true);
 auto pendingOld=capture(pendingOldId,"pending-old.mkv");
 scanHook=[](const auto& source,const auto& state){scan(source,state);detect_publish(state,source,66,66,0,0,true);};
 const auto freshJob=startJob("fresh.mkv");completed(freshJob);
 const auto pendingNewId=bindingFor(freshJob,"pending-new.mkv",2,true);
 auto pendingNew=capture(pendingNewId,"pending-new.mkv");
 {CAMLNativeTransaction owner;assert(owner.TryBegin());
  openBound(pendingOld,"pending-old.mkv");
  const auto before=writes.load();openBound(pendingNew,"pending-new.mkv");
  assert(writes==before);assert(pendingOld->state==DETECTOR_PLAYBACK::State::STALE);
 }
 applied(pendingNew);assert(parameter("xbmc_override_l5_top")==66);

 // A consumed old dispatcher request must not cancel a replacement scan.
 settingsValue.detect=true;settingsValue.wait=false;settingsValue.cache=false;
 const auto queuedId=bindingFor(freshJob,"queued-old.mkv",2,true);auto queued=capture(queuedId,"queued-old.mkv");
 bindingDispatchEntered=false;releaseBindingDispatch=false;deferBindingDispatch=true;
 openBound(queued,"queued-old.mkv");until([]{return bindingDispatchEntered.load();});
 std::promise<void> newScanEntered;std::atomic<bool> releaseNewScan{false},newScanCancelled{false};
 scanHook=[&](const auto& source,const auto& state){
  assert(source->path=="unbound-new.mkv");newScanEntered.set_value();
  while(!releaseNewScan.load()){
   if(state.Cancelled()){newScanCancelled=true;return;}
   state.WaitFor(1ms);
  }
  detect_publish_subtitle(state,source,{44,44,0,0});detect_publish(state,source,42,42,0,0,true);
 };
 aml_dv_detect_set_playback_file("unbound-new.mkv",false,{});auto unbound=aml_subtitle_active_area_source(2,173);
 assert(aml_subtitle_active_area_configure(1920,1080,false,true,unbound,16.0/9.0));autoStart();
 assert(newScanEntered.get_future().wait_for(2s)==std::future_status::ready);
 const auto snapshots=bindingDispatchSnapshots.load();releaseBindingDispatch=true;
 until([&]{return bindingDispatchSnapshots>snapshots;});
 assert(!newScanCancelled&&s_detectSource->pending.load());releaseNewScan=true;
 until([]{return !s_detectSource->pending.load();});s_detectWorker.Stop();
 assert(!newScanCancelled&&aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r,true)&&t==44);
 // Apply must complete while the displaced automatic scanner is in blocked I/O.
 std::promise<void> oldReadEntered,releaseOldRead;auto oldReadRelease=releaseOldRead.get_future().share();
 scanHook=[&](const auto& source,const auto&){
  assert(source->path=="active-blocked.mkv");oldReadEntered.set_value();oldReadRelease.wait();
  source->scanState=DV_DETECT_FAILED; // its actual final propagation follows this boundary
 };
 aml_dv_detect_set_playback_file("active-blocked.mkv",false,{});auto oldSelection=aml_subtitle_active_area_source(2,173);
 assert(aml_subtitle_active_area_configure(1920,1080,true,true,oldSelection,16.0/9.0));autoStart();
 assert(oldReadEntered.get_future().wait_for(2s)==std::future_status::ready);const auto oldScan=s_detectSource;
 const auto whileBlocked=bindingFor(remuxJob,"active-blocked.mkv",2,true);
 const auto blockedResult=DETECTOR_PLAYBACK::Find(whileBlocked);context=aml_detector_playback_status();
 const auto beforeApply=std::chrono::steady_clock::now();
 accepted=aml_detector_apply_binding(whileBlocked,context["playbackid"].asString(),context["selectionid"].asString());
 assert(std::chrono::steady_clock::now()-beforeApply<500ms);
 assert(accepted["status"].asString()=="accepted"||accepted["status"].asString()=="applied");
 applied(blockedResult);geometry();assert(oldScan->pending.load());
 releaseOldRead.set_value();until([&]{return !oldScan->pending.load();});s_detectWorker.Stop();
 assert(s_detectState==DV_DETECT_OK);geometry();
 settingsValue.detect=false;settingsValue.wait=true;

 // Bounds/type checks reject malformed evidence before it can be captured.
 for(int bad=0;bad<5;++bad){DETECTOR_PLAYBACK::Result value=*pendingNew->result;
  if(bad==0)value.strict={540,540,0,0};
  if(bad==1)value.width=0;
  if(bad==2)value.displayAspect=std::numeric_limits<double>::quiet_NaN();
  if(bad==3)value.streamIndex=-1;
  if(bad==4){value.hasStrict=false;value.hasSubtitle=true;value.highConfidence=true;}
  assert(!value.Valid());
 }
 // Inconclusive or cancelled jobs cannot be made into usable bindings.
 scanHook=[](const auto& source,const auto&){source->scanState=DV_DETECT_SKIPPED;};
 const auto uncertain=startJob("unknown.mkv");completed(uncertain);
 assert(aml_detector_bind_job(uncertain,"unknown.mkv",-1,false)["status"].asString()=="unusable_result");
 std::promise<void> cancelEntered;scanHook=[&](const auto&,const auto& state){cancelEntered.set_value();while(!state.Cancelled())state.WaitFor(1ms);};
 const auto cancelled=startJob("cancelled.mkv");cancelEntered.get_future().wait();aml_detector_cancel_job(cancelled);
 assert(completed(cancelled)["status"].asString()=="cancelled");
 assert(aml_detector_bind_job(cancelled,"cancelled.mkv",-1,false)["status"].asString()=="unusable_result");

 // Authored nonzero L5 resists conservative evidence; invariant evidence wins.
 for(bool invariant:{false,true}){
  scanHook=[&](const auto& source,const auto& state){scan(source,state);detect_publish(state,source,132,132,0,0,invariant);};
  const auto authoredJob=startJob();completed(authoredJob);auto authored=capture(bindingFor(authoredJob),"original.mkv");
  {CAMLNativeTransaction owner;assert(owner.TryBegin());openBound(authored);
   cache.meta.has_level5_metadata=true;cache.meta.level5_active_area_top_offset=7;cache.meta.level5_active_area_bottom_offset=7;
  }
  applied(authored);
  assert(parameter("xbmc_force_l5_override")==int(invariant));
  assert(parameter("xbmc_detected_l5_top")==int(invariant?132:0));
 }

 // Full-frame zero is usable strict evidence; subtitle-only cannot become L5.
 scanHook=[](const auto& source,const auto& state){scan(source,state);detect_publish(state,source,0,0,0,0,true);};
 const auto zeroJob=startJob();completed(zeroJob);auto zero=capture(bindingFor(zeroJob),"original.mkv");openBound(zero);applied(zero);geometry(0,0);
 assert(parameter("xbmc_force_l5_override")==1&&parameter("xbmc_override_l5_top")==0);
 scanHook=[](const auto& source,const auto& state){scan(source,state);std::lock_guard<std::mutex> guard(s_detectSourceMutex);source->result.reset();source->scanState=DV_DETECT_SKIPPED;};
 const auto subJob=startJob();completed(subJob);auto subtitle=capture(bindingFor(subJob),"original.mkv");openBound(subtitle);applied(subtitle);geometry(0,0,false);
 assert(!subtitle->nativeAvailable&&parameter("xbmc_force_l5_override")==0);

 // Pre-crop endpoint supplies reliable probe geometry, no decoded samples.
 scanHook=[](const auto& source,const auto&){source->scannedWidth=3832;source->scannedHeight=1600;source->scannedStream=2;source->scannedCodec=173;
  source->scannedAspect=3832.0/1600.0;source->preCropped=true;source->scanState=DV_DETECT_SKIP_NON16X9;};
 const auto cropJob=startJob("crop.mkv");result=completed(cropJob);
 assert(result["status"].asString()=="completed"&&result["provenance"].asString()=="pre_cropped"&&!result["sampled"].asBoolean());
 assert(result["geometry"]["valid"].asBoolean()&&!result["strict"]["valid"].asBoolean()&&!result["subtitle"]["valid"].asBoolean());
 assert(result["geometry"]["offsets"]["top"].asInteger()==0&&result["padding"]["height"].asInteger()==2155&&result["padding"]["top"].asInteger()==277);
 auto crop=capture(bindingFor(cropJob,"crop.mkv"),"crop.mkv");aml_dv_set_active_area_geometry(3832,1600,true);
 openBound(crop,"crop.mkv",2,3832,1600,true,3832.0/1600.0);applied(crop);
 assert(crop->geometryAvailable&&!crop->subtitleAvailable&&!crop->nativeAvailable);
 assert(aml_dv_auto_letterbox_get(t,b,l,r)&&t==277&&b==277&&l==0&&r==0);
 assert(!aml_subtitle_detect_active_area_get(3832,1600,t,b,l,r,true));
 assert(parameter("xbmc_override_l5_top")==277&&parameter("xbmc_l5_override_additive")==1);

 // A failed native effect is terminal; its exception must not kill dispatcher.
 aml_dv_set_active_area_geometry(1920,1080,true);scanHook=scan;
 const auto effectJob=startJob();completed(effectJob);
 auto failedEffect=capture(bindingFor(effectJob),"original.mkv");
 std::atomic<bool> failEffect{true};writeHook=[&]{if(failEffect.exchange(false))throw std::runtime_error("injected native effect");};
 const auto failedHandlers=bindingDispatchFailures.load();selected=openBound(failedEffect);
 until([&]{return bindingDispatchFailures>failedHandlers;});
 assert(failedEffect->state==DETECTOR_PLAYBACK::State::FAILED);
 assert(aml_detector_binding_status(failedEffect->id)["status"].asString()=="failed");
 assert(!failedEffect->subtitleAvailable&&!failedEffect->nativeAvailable&&!failedEffect->geometryAvailable);
 assert(parameter("xbmc_force_l5_override")==0);
 for(const auto* side:{"top","bottom","left","right"}){
  assert(parameter(std::string("xbmc_detected_l5_")+side)==0);
  assert(parameter(std::string("xbmc_override_l5_")+side)==0);
 }
 assert(!aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r,true));
 aml_subtitle_active_area_reset(selected);
 assert(aml_subtitle_active_area_configure(1920,1080,true,true,selected,16.0/9.0));
 assert(failedEffect->state==DETECTOR_PLAYBACK::State::FAILED);
 writeHook={};
 auto recoveredEffect=capture(bindingFor(effectJob),"original.mkv");openBound(recoveredEffect);applied(recoveredEffect);geometry();
 assert(parameter("xbmc_override_l5_top")==132);

 // Late failures after force installation restore each existing native owner.
 for(int fallback=0;fallback<3;++fallback){
  manual=fallback==1;manualOffsets={88,88,0,0};const bool padding=fallback==2;
  aml_dv_set_active_area_geometry(padding?3832:1920,padding?1600:1080,true);
  const auto faultJob=padding?cropJob:effectJob;const std::string target=padding?"crop.mkv":"original.mkv";
  auto partial=capture(bindingFor(faultJob,target),target);std::atomic<bool> failAfterForce{true};std::atomic<int> throwAt{0};
  const int forced=padding?277:manual?88:132;const int restored=padding?277:manual?88:0;
  writeHook=[&]{if(wroteParameter("xbmc_force_l5_override",1)&&parameter("xbmc_override_l5_top")==forced&&failAfterForce.exchange(false)){
   throwAt=writes.load();throw std::runtime_error("injected partial native publication");
  }};
  const auto failures=bindingDispatchFailures.load();
  openBound(partial,target,2,padding?3832:1920,padding?1600:1080,true,padding?3832.0/1600.0:16.0/9.0);
  until([&]{return bindingDispatchFailures>failures;});
  assert(partial->state==DETECTOR_PLAYBACK::State::FAILED&&!failAfterForce);
  assert(!partial->subtitleAvailable&&!partial->nativeAvailable&&!partial->geometryAvailable);
  assert(parameter("xbmc_force_l5_override")==int(fallback!=0)&&parameter("xbmc_override_l5_top")==restored&&parameter("xbmc_override_l5_bottom")==restored);
  assert(parameter("xbmc_l5_override_additive")==int(padding));
  for(const auto* side:{"top","bottom","left","right"})assert(parameter(std::string("xbmc_detected_l5_")+side)==0);
  assert(writes>=throwAt+11);writeHook={};manual=false;
 }

 // Cleanup may itself fail; that terminal failure must not kill the dispatcher.
 aml_dv_set_active_area_geometry(1920,1080,true);auto twiceFailed=capture(bindingFor(effectJob),"original.mkv");
 std::atomic<int> failPhase{0};writeHook=[&]{
  if(failPhase==0&&wroteParameter("xbmc_force_l5_override",1)&&parameter("xbmc_override_l5_top")==132){failPhase=1;throw std::runtime_error("first failure");}
  if(failPhase==1&&wroteParameter("xbmc_detected_l5_top",0)){failPhase=2;throw std::runtime_error("cleanup failure");}
 };
 auto failures=bindingDispatchFailures.load();openBound(twiceFailed);until([&]{return bindingDispatchFailures>failures;});
 assert(failPhase==2&&twiceFailed->state==DETECTOR_PLAYBACK::State::FAILED&&!twiceFailed->nativeAvailable);
 writeHook={};auto afterCleanupFailure=capture(bindingFor(effectJob),"original.mkv");openBound(afterCleanupFailure);applied(afterCleanupFailure);geometry();

 // A delayed cleanup must not clear a replacement source's native parameters.
 auto replacedFailure=capture(bindingFor(effectJob),"original.mkv");std::atomic<bool> failReplaced{true};
 writeHook=[&]{if(wroteParameter("xbmc_force_l5_override",1)&&parameter("xbmc_override_l5_top")==132&&failReplaced.exchange(false))throw std::runtime_error("replaced failure");};
 failureCleanupEntered=false;releaseFailureCleanup=false;deferFailureCleanup=true;
 failures=bindingDispatchFailures.load();openBound(replacedFailure);until([]{return failureCleanupEntered.load();});
 aml_dv_detect_set_playback_file("replacement-owner.mkv",true,{});const auto replacementSelection=aml_subtitle_active_area_source(2,173);
 assert(aml_subtitle_active_area_configure(1920,1080,true,true,replacementSelection,16.0/9.0));
 manual=true;manualOffsets={88,88,0,0};
 {CAMLNativeTransaction owner;assert(owner.TryBegin());
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_top",91);
  CSysfsPath("/sys/module/amdolby_vision/parameters/xbmc_detected_l5_bottom",91);
  std::lock_guard<std::mutex> guard(s_detectSourceMutex);detect_apply_l5_override_locked();
 }
 const auto replacementWrites=writes.load();releaseFailureCleanup=true;until([&]{return bindingDispatchFailures>failures;});
 assert(writes==replacementWrites&&parameter("xbmc_detected_l5_top")==91&&parameter("xbmc_detected_l5_bottom")==91&&parameter("xbmc_override_l5_top")==88);
 writeHook={};manual=false;

 // Manual owner remains authoritative, even explicit native bound geometry.
 aml_dv_set_active_area_geometry(1920,1080,true);manual=true;manualOffsets={88,88,0,0};scanHook=scan;
 const auto manualJob=startJob();completed(manualJob);auto manualResult=capture(bindingFor(manualJob),"original.mkv");openBound(manualResult);applied(manualResult);
 assert(parameter("xbmc_override_l5_top")==88&&parameter("xbmc_force_l5_override")==1);
 manual=false;
 // Native decoder close suspends; explicit playback end retires exact owner.
 {CAMLNativeTransaction owner;assert(owner.TryBegin());aml_dv_detect_active_area_stop();}
 assert(manualResult->state!=DETECTOR_PLAYBACK::State::STALE);
 assert(aml_subtitle_active_area_configure(0,0,false,false,aml_subtitle_active_area_source(),0));
 const auto ended=manualResult->playback.load();aml_detector_end_playback(ended);
 assert(manualResult->state==DETECTOR_PLAYBACK::State::STALE);
 const auto scansBeforeEnded=entries.load();selected=aml_subtitle_active_area_source();
 assert(aml_subtitle_active_area_configure(0,0,false,false,selected,0));
 assert(aml_subtitle_active_area_configure(1920,1080,true,true,selected,16.0/9.0));autoStart();
 assert(aml_detector_playback_status()["status"].asString()=="inactive");
 assert(!aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r,true));
 assert(manualResult->state==DETECTOR_PLAYBACK::State::STALE&&entries==scansBeforeEnded);
 // End alone must also block a fresh automatic route with no one-shot veto.
 settingsValue.detect=true;settingsValue.wait=false;scanHook=scan;
 const auto endedAuto=aml_dv_detect_set_playback_file("ended-auto.mkv",false,{});
 selected=aml_subtitle_active_area_source(2,173);
 assert(aml_subtitle_active_area_configure(1920,1080,false,true,selected,16.0/9.0));
 aml_detector_end_playback(endedAuto);
 assert(aml_subtitle_active_area_configure(0,0,false,false,selected,0));
 assert(aml_subtitle_active_area_configure(1920,1080,false,true,selected,16.0/9.0));
 const auto beforeEndedAuto=entries.load();autoStart();until([]{return !s_detectSource->pending.load();});s_detectWorker.Stop();
 assert(entries==beforeEndedAuto&&aml_detector_playback_status()["status"].asString()=="inactive");
 assert(!s_detectSource->policy->skip&&s_detectSource->policy->ended);
 aml_dv_detect_set_playback_file("next.mkv",false,{});assert(!s_detectSource->binding);
 s_bindingWorker.Stop();
 std::cout<<"PASS: completed job/cache/pre-crop -> actual request capture -> source ownership -> real dispatcher/subtitle/native consumers; OFF-cache isolation, remux, reset, stale/stream/size rejection, eviction/release and precedence\n";
}
'''


LAUNCH_TEST = r"""
int main(){
 (void)&detect_avio_read;(void)&detect_avio_seek;
 ready();CAMLSession sessionValue;init(sessionValue);settingsValue.wait=true;settingsValue.detect=false;dvDetection=false;
 settingsValue.cache=false;scanHook=scan;
 const auto job=startJob();completed(job);auto first=capture(bindingFor(job),"original.mkv");
 assert(!s_bindingStarted);detectorFailLaunch=true;
 try{openBound(first);}catch(...){assert(false&&"dispatcher launch exception escaped");}
 assert(first->state==DETECTOR_PLAYBACK::State::FAILED&&!s_bindingStarted);
 assert(aml_detector_binding_status(first->id)["status"].asString()=="failed");
 assert(!aml_subtitle_active_area_startup_pending(aml_subtitle_active_area_source()));
 auto second=capture(bindingFor(job),"original.mkv");openBound(second);applied(second);geometry();
 assert(s_bindingStarted&&parameter("xbmc_override_l5_top")==132);s_bindingWorker.Stop();
 std::cout<<"PASS: actual one-shot thread-launch failure is terminal without exception leakage; next binding starts dispatcher and applies\n";
}
"""


def launch_source(code):
    return code.replace(function(code, 'int main()'), LAUNCH_TEST)


def launch_header():
    header = (ROOT / 'xbmc/utils/AMLNativeWorker.h').read_text()
    needle = 'm_thread = std::thread([run, action = std::move(action)]() mutable {'
    assert header.count(needle) == 1
    return '#include <stdexcept>\n' + header.replace(needle,
        'if(detectorFailLaunch.exchange(false))throw std::runtime_error("injected thread creation failure");\n      ' + needle)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    os.environ.setdefault('ASAN_OPTIONS', 'detect_leaks=0')
    code = source()
    if args.output:
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / 'playback.cpp').write_text(code)
        paths = ['xbmc/utils/DetectorPlayback.h', 'xbmc/utils/DetectorAdmission.h',
                 'xbmc/utils/AMLNativeWorker.h', 'xbmc/utils/AMLUtils.cpp',
                 'xbmc/FileItem.cpp', 'xbmc/FileItem.h',
                 'xbmc/cores/VideoPlayer/VideoPlayer.cpp',
                 'tools/test-detector-jobs.py', 'tools/test-active-area-admission.py',
                 'tools/test-active-area-startup.py', 'tools/test-aml-native-workers.py',
                 'tools/test-detector-playback.py']
        (args.output / 'sources.json').write_text(json.dumps({p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}, indent=2))
    run(code)
    launch = launch_source(code)
    injected_header = launch_header()
    if args.output:
        (args.output / 'launch.cpp').write_text(launch)
        (args.output / 'launch-worker.h').write_text(injected_header)
    run(launch, ('utils/AMLNativeWorker.h', injected_header))
    if args.negative_controls:
        for label, old, new in [
            ('suspended binding retains applied effects', '  binding->subtitleAvailable = false;\n  binding->nativeAvailable = false;\n  binding->geometryAvailable = false;', '  /* stale effect flags retained */'),
            ('suspended binding still says applied', '  if (binding->state == DETECTOR_PLAYBACK::State::APPLIED)\n    binding->state = DETECTOR_PLAYBACK::State::ACCEPTED;', '  /* stale applied state retained */'),
            ('native failure skips ownership cleanup', 'source->binding->state == DETECTOR_PLAYBACK::State::FAILED)\n                      detect_clear_injection_locked(true);', 'source->binding->state == DETECTOR_PLAYBACK::State::FAILED)\n                      (void)source;'),
            ('old failure cleanup clears replacement', 'if (source == s_detectSource && !source->superseded->load() &&\n                        source->binding->state == DETECTOR_PLAYBACK::State::FAILED)', 'if (source != nullptr)'),
            ('cleanup exception kills dispatcher', 'CLog::Log(LOGERROR, "DetectActiveArea: failed to restore native result ownership");', 'throw;'),
            ('ended playback resurrects', 's_detectSource->policy->ended = true;', '/* terminal generation lost */'),
            ('publication failure never becomes terminal', 'source->binding->state = DETECTOR_PLAYBACK::State::FAILED;', 'source->binding->state = DETECTOR_PLAYBACK::State::ACCEPTED;'),
            ('failed binding silently retries on reset', 'binding->state == DETECTOR_PLAYBACK::State::INCOMPATIBLE ||\n      binding->state == DETECTOR_PLAYBACK::State::FAILED', 'binding->state == DETECTOR_PLAYBACK::State::INCOMPATIBLE'),
            ('old binding dispatcher cancels new scan', 'binding_dispatch_step();\n', 'binding_dispatch_step();\n              s_detectWorker.Stop();\n'),
            ('late scan cleanup overwrites new owner', 'if (!run.Cancelled() && source == s_detectSource && !source->superseded->load())\n      s_detectState.store(source->scanState.load());', 'if ((void(run), source != nullptr))\n      s_detectState.store(source->scanState.load());'),
            ('bound result obeys automatic veto', '(!source->boundUsable && source->policy->Blocked())', 'source->policy->Blocked()'),
            ('geometry validation omitted', 'value.width != source->width || value.height != source->height ||', 'false ||'),
            ('playback fence omitted', 'playback != std::to_string(previous->policy->id) ||', '((void)playback, false) ||'),
            ('selection fence omitted', 'selection != std::to_string(previous->selectionId)', '(void(selection), false)'),
            ('copy binding lost', 'm_detectorBinding = DETECTOR_PLAYBACK::Consume(token, GetDynPath());', '(void)token;'),
        ]:
            assert old in code, label
            run(code.replace(old, new), negative=True)
            print('REJECTED:', label)
        scheduler = function(launch, 'static bool detect_schedule_binding()\n{')
        needle = '    catch (...)\n    {\n      return false;\n    }'
        assert scheduler.count(needle) == 1
        broken = launch.replace(scheduler, scheduler.replace(needle,
            '    catch (...)\n    {\n      throw;\n    }'))
        run(broken, ('utils/AMLNativeWorker.h', injected_header), negative=True)
        print('REJECTED: dispatcher thread-launch exception escapes')


if __name__ == '__main__':
    main()
