#!/usr/bin/env python3
"""Actual detector job manager, worker, result sinks, admission and deadlines.

The scanner/VFS/cache boundaries record calls. Full scanner and persistence are
covered by the sampling/I/O and cache suites; no device acceptance is claimed.
"""
import argparse
import os
from pathlib import Path
import runpy
ROOT = Path(__file__).resolve().parents[1]
ADMISSION = runpy.run_path(str(ROOT/'tools/test-active-area-admission.py'))
function, run = ADMISSION['function'], ADMISSION['run']


def source():
    aml = (ROOT/'xbmc/utils/AMLUtils.cpp').read_text()
    code = ADMISSION['source']().replace(ADMISSION['TEST'], '')
    code = '#include "utils/ScopedLogSilence.h"\n#pragma GCC diagnostic push\n#pragma GCC diagnostic ignored "-Wunused-parameter"\n#include "utils/Variant.cpp"\n#pragma GCC diagnostic pop\n#include <map>\n'+code
    code = code.replace('bool aml_detector_retire_jobs(){return true;}', 'bool aml_detector_retire_jobs();')
    code = code.replace('SETTING_COREELEC_AMLOGIC_DV_DETECT_ACTIVE_AREA=7;', 'SETTING_COREELEC_AMLOGIC_DV_DETECT_ACTIVE_AREA=7,SETTING_SUBTITLES_EXCLUDEHTTPACTIVEAREA=8,SETTING_SUBTITLES_ACTIVEAREATIMEOUT=9;')
    code = code.replace('struct Settings {', 'struct Settings {int seconds=1;bool excludeHTTP=false;int GetInt(int){return seconds;}')
    code = code.replace('return id==5?wait:', 'return id==8?excludeHTTP:id==5?wait:')
    code = code.replace('inline bool ReadIdentity(Identity&){return true;}', 'inline bool ReadIdentity(Identity&){++identities;return true;}')
    code = code.replace('inline bool Load(const Identity&,Record& r){r.hasSubtitle=true;r.subtitle={20,20,0,0};r.state=DV_DETECT_SKIPPED;return true;}', 'inline bool Load(const Identity&,Record&){++loads;return false;}')
    code = code.replace('inline bool Store(const Identity&,const Record&){assert(false);return false;}', 'inline bool Store(const Identity&,const Record& r){++stores;saved=r;return true;}')
    index = code.index('namespace ACTIVE_AREA_CACHE {')
    cache = r'''
std::atomic<int> finds{0},identities{0},loads{0},stores{0};
std::atomic<bool> cacheHit{false};ACTIVE_AREA_CACHE::Record saved;
namespace ACTIVE_AREA_CACHE {
inline bool ReadContentIdentity(Identity&){assert(CScopedLogSilence::Active());++identities;return true;}
inline bool Find(const std::string&,int index,Identity& i,Record& r){
 assert(CScopedLogSilence::Active());++finds;if(!cacheHit)return false;i.width=1280;i.height=720;i.codec=173;i.streamIndex=index<0?2:index;r=saved;return true;
}
}
'''
    code = code[:index]+cache+code[index:]
    scanner = function(code,'void DetectActiveAreaFromFile(')
    code = code.replace(scanner, r'''
void DetectActiveAreaFromFile(const std::shared_ptr<DetectSource>& source,const CAMLNativeWorker::Run& run){
 if(source->standalone){assert(CScopedLogSilence::Active());}
 ++entries;scanHook(source,run);
}
''', 1)
    # Actual path classifier is exercised separately. This boundary records only
    # the explicit-job exclusion wiring rather than reimplementing URIUtils.
    code += 'struct URIUtils{static bool IsHTTP(const std::string& path,bool){return path=="HTTP-input";}};\n'
    code += function(aml,'void aml_dv_detect_set_playback_file(')+'\n'
    start = aml.index('namespace\n{\nenum class DetectorJobState')
    end = aml.index('\n\nbool aml_dv_retire_background_work()',start)
    return code+aml[start:end]+TEST

TEST = r'''
void autoStart(){CAMLNativeTransaction native;assert(native.TryBegin());aml_dv_detect_active_area_start();}
std::string start(const std::string& path="job",int stream=-1){
 auto reply=aml_detector_start_job(path,stream,false);assert(reply["status"].asString()=="accepted");return reply["requestid"].asString();
}
CVariant done(const std::string& id){
 CVariant result;
 until([&]{result=aml_detector_job_status(id);const auto status=result["status"].asString();return status!="queued"&&status!="running"&&status!="cancelling";});return result;
}
void normal(const std::shared_ptr<DetectSource>& source,const CAMLNativeWorker::Run& r){
 source->scannedWidth=1280;source->scannedHeight=720;source->scannedStream=2;source->scannedCodec=173;source->uniqueStream=true;
 for(int n=1;n<=14;++n)source->completedPositions=n;
 detect_publish_subtitle(r,source,{30,30,0,0});detect_publish(r,source,0,0,0,0,true);
}
struct Item {
 bool audio=false,video=true,game=false,playlist=false,smart=false;
 std::map<std::string,CVariant> props;
 bool HasProperty(const std::string& k)const{return props.count(k);}
 bool IsAudio()const{return audio;}bool IsVideo()const{return video;}bool IsGame()const{return game;}bool IsPicture()const{return false;}
 bool IsPlayList()const{return playlist;}bool IsSmartPlayList()const{return smart;}
 void SetProperty(const std::string& k,bool b){props[k]=b;}
};
int main(){
 (void)&detect_avio_read;(void)&detect_avio_seek;
 ready();CAMLSession sessionValue;init(sessionValue);settingsValue.wait=true;settingsValue.cache=true;
 // Generic non-PM4K publications, exact value, generations and queued snapshots.
 for(const auto& value:{"", "true", "01", " 1"}){DETECTOR_ADMISSION::Publish(value);Item i;DETECTOR_ADMISSION::CaptureItem(i);assert(!i.props.at(DETECTOR_ADMISSION::ITEM_SKIP).asBoolean());assert(DETECTOR_ADMISSION::Value()==value);}
 DETECTOR_ADMISSION::Publish("1");Item music;music.audio=true;music.video=false;DETECTOR_ADMISSION::CaptureItem(music);assert(!music.HasProperty(DETECTOR_ADMISSION::ITEM_SKIP));
 Item list;list.playlist=true;DETECTOR_ADMISSION::CaptureItem(list);assert(!list.HasProperty(DETECTOR_ADMISSION::ITEM_SKIP));
 Item first;DETECTOR_ADMISSION::CaptureItem(first);assert(first.props.at(DETECTOR_ADMISSION::ITEM_SKIP).asBoolean());assert(DETECTOR_ADMISSION::Value().empty());
 Item next;DETECTOR_ADMISSION::CaptureItem(next);auto token=DETECTOR_ADMISSION::Publish("1");DETECTOR_ADMISSION::CaptureItem(next);assert(!next.props.at(DETECTOR_ADMISSION::ITEM_SKIP).asBoolean());assert(DETECTOR_ADMISSION::Value()=="1");
 const auto newer=DETECTOR_ADMISSION::Publish("1");assert(!DETECTOR_ADMISSION::Cancel(token)&&DETECTOR_ADMISSION::Value()=="1");assert(DETECTOR_ADMISSION::Cancel(newer));
 // Request veto precedes all cache/worker/hold I/O and survives source routes.
 scanHook=normal;entries=0;
 aml_dv_detect_set_playback_file("automatic",true);aml_subtitle_active_area_startup_begin();
 auto selected=aml_subtitle_active_area_source(0,173);assert(aml_subtitle_active_area_configure(1920,1080,true,true,selected));
 const auto findsBefore=finds.load(),identityBefore=identities.load(),loadsBefore=loads.load();
 autoStart();assert(!s_detectSource->pending&&!aml_subtitle_active_area_startup_pending(selected));
 assert(!aml_subtitle_active_area_startup_try_hold(selected,[]{assert(false);}));
 aml_subtitle_active_area_reset(selected);assert(aml_subtitle_active_area_configure(1920,1080,true,true,selected));autoStart();
 selected=aml_subtitle_active_area_source(1,173);assert(aml_subtitle_active_area_configure(1920,1080,true,true,selected));autoStart();
 assert(entries==0&&finds==findsBefore&&identities==identityBefore&&loads==loadsBefore);
 settingsValue.wait=false;aml_dv_detect_set_playback_file("next",false);selected=aml_subtitle_active_area_source(0,173);assert(aml_subtitle_active_area_configure(1920,1080,false,true,selected));
 autoStart();until([]{return !s_detectSource->pending.load();});s_detectWorker.Stop();assert(entries==1);
 // HTTP policy is a snapshot; changes apply to new requests only.
 settingsValue.excludeHTTP=true;aml_dv_detect_set_playback_file("HTTP-input",false);assert(s_detectSource->policy->Blocked());settingsValue.excludeHTTP=false;assert(s_detectSource->policy->Blocked());
 aml_dv_detect_set_playback_file("HTTP-input",false);assert(!s_detectSource->policy->Blocked());
 // No player geometry/metadata/results or one-shot signal may be borrowed.
 select("active-player",true,1920,1080);const auto active=s_detectSource;const auto activeState=s_detectState.load();const auto beforeWrites=writes.load(),beforeWatch=watchWrites.load();
 settingsValue.detect=false;dvDetection=true;
 DETECTOR_ADMISSION::Publish("1");cache.meta.has_level5_metadata=true;cacheHit=false;entries=0;stores=0;
 auto id=start();auto result=done(id);s_explicitDetectorWorker.Stop();
 assert(result["status"].asString()=="completed"&&!result["cachehit"].asBoolean());assert(entries==1&&stores==1&&saved.hasStrict&&saved.hasSubtitle&&saved.uniqueStream);
 assert(result["strict"]["valid"].asBoolean()&&result["strict"]["offsets"]["top"].asInteger()==0);
 assert(result["strict"]["width"].asInteger()==1280&&result["strict"]["rectangle"]["x2"].asInteger()==1280);
 assert(result["strict"]["confidence"].asString()=="invariant"&&result["subtitle"]["confidence"].asString()=="placement_only");
 assert(s_detectSource==active&&s_detectState==activeState&&!active->result&&!active->subtitleResult);
 assert(writes==beforeWrites&&watchWrites==beforeWatch&&DETECTOR_ADMISSION::Value()=="1");
 // Shared cache ON returns a validated record without a scanner reservation.
 cacheHit=true;auto reserved=detect_reserve_scanner();assert(reserved);const auto priorEntries=entries.load();id=start("cached");result=done(id);s_explicitDetectorWorker.Stop();
 assert(result["status"].asString()=="completed"&&result["cachehit"].asBoolean()&&entries==priorEntries);
 assert(result["progress"]["completed"].asInteger()==0&&result["subtitle"]["offsets"]["top"].asInteger()==30);
 // A miss cannot cancel an automatic owner; an OFF cache does no identity I/O.
 cacheHit=false;id=start("busy");assert(done(id)["status"].asString()=="busy");s_explicitDetectorWorker.Stop();assert(entries==priorEntries);
 reserved.reset();settingsValue.cache=false;const auto offFinds=finds.load(),offIdentities=identities.load(),offStores=stores.load();
 id=start("off");assert(done(id)["status"].asString()=="completed");s_explicitDetectorWorker.Stop();assert(finds==offFinds&&identities==offIdentities&&stores==offStores);
 // HTTP exclusion rejects before lookup; deliberate override remains isolated.
 settingsValue.excludeHTTP=true;const auto beforeHTTP=finds.load();
 assert(aml_detector_start_job("HTTP-input",-1,false)["status"].asString()=="http_excluded"&&finds==beforeHTTP);
 auto http=aml_detector_start_job("HTTP-input",-1,true);assert(http["status"].asString()=="accepted");
 assert(done(http["requestid"].asString())["status"].asString()=="completed");s_explicitDetectorWorker.Stop();settingsValue.excludeHTTP=false;
 // Cancellation stays busy while synchronous backend I/O is still blocked.
 std::promise<void> entered,release;auto released=release.get_future();scanHook=[&](const auto& source,const CAMLNativeWorker::Run& r){entered.set_value();released.wait();normal(source,r);};
 id=start("blocked");entered.get_future().wait();result=aml_detector_cancel_job(id);assert(result["status"].asString()=="cancelling");assert(aml_detector_start_job("other",-1,false)["status"].asString()=="busy");
 release.set_value();assert(done(id)["status"].asString()=="cancelled");s_explicitDetectorWorker.Stop();assert(!s_detectorJobs.at(id)->source->result);
 // Real sleeping backend returns after deadline, then post-call guard aborts.
 scanHook=[&](const auto& source,const CAMLNativeWorker::Run& r){source->positionDeadline.Start(20ms);std::this_thread::sleep_for(40ms);XFILE::CFile file;DetectInput input{file,r,source.get()};assert(detect_input_interrupt_cb(&input));normal(source,r);};
 settingsValue.cache=true;const auto beforeTimeoutStores=stores.load();id=start("timeout");assert(done(id)["status"].asString()=="timed_out");s_explicitDetectorWorker.Stop();assert(stores==beforeTimeoutStores&&!s_detectorJobs.at(id)->source->result);
 // Timeout is sticky only for the exact current automatic playback.
 aml_dv_detect_set_playback_file("timeout-auto",false);selected=aml_subtitle_active_area_source(0,173);assert(aml_subtitle_active_area_configure(1920,1080,true,true,selected));
 auto old=s_detectSource;CAMLNativeWorker::Run oldRun(old->superseded);XFILE::CFile file;DetectInput input{file,oldRun,old.get()};old->positionDeadline.Start(0ms);assert(detect_input_interrupt_cb(&input));
 aml_subtitle_active_area_reset(selected);assert(aml_subtitle_active_area_configure(1920,1080,true,true,selected));autoStart();assert(s_detectState==DV_DETECT_TIMED_OUT&&!s_detectSource->pending);
 aml_dv_detect_set_playback_file("replacement",false);assert(detect_input_interrupt_cb(&input));assert(!s_detectSource->policy->Blocked());
 // Bounded results and shutdown cancel actual worker before services retire.
 scanHook=normal;settingsValue.cache=false;for(int n=0;n<20;++n){id=start();done(id);s_explicitDetectorWorker.Stop();}assert(s_detectorJobs.size()==16);assert(aml_detector_job_status("no-such-id")["status"].asString()=="not_found");
 std::promise<void> shutdownEntered;scanHook=[&](const auto&,const CAMLNativeWorker::Run& r){shutdownEntered.set_value();assert(!r.WaitFor(30s));};id=start("shutdown");shutdownEntered.get_future().wait();aml_detector_retire_jobs();until([]{return aml_detector_retire_jobs();});assert(done(id)["status"].asString()=="cancelled");assert(aml_detector_start_job("late",-1,false)["status"].asString()=="shutdown");
 std::cout<<"PASS: actual standalone jobs, shared cache hit/miss/OFF, result isolation, busy/cancel/shutdown, bounded records, generic ownership and sticky automatic admission\n";
}
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    os.environ.setdefault('ASAN_OPTIONS','detect_leaks=0')
    code=source();run(code)
    if args.negative_controls:
        for label,old,new in [
            ('worker logs credential-bearing I/O','      const CScopedLogSilence sensitiveInput;', '      /* scope missing */'),
            ('cache hit still scans','        job->cacheHit = true;\n        return;','        job->cacheHit = true;'),
            ('standalone ignores cancellation','if (!run.Cancelled() && !source->timedOut &&','if (!source->timedOut &&'),
            ('veto loses reset retention','s_detectSource->policy = previous->policy;','/* lost policy */'),
            ('cancel releases busy early','found->second->source->superseded->store(true);','{found->second->source->superseded->store(true);found->second->state=DetectorJobState::FINISHED;}'),
        ]:
            assert old in code,label
            run(code.replace(old,new),negative=True);print('REJECTED:',label)
if __name__=='__main__':main()
