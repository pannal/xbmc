#!/usr/bin/env python3
"""Exercise Kodi DV off/toggle/close against the vendor kernel policy/bypass branch.

Requires --kernel-source pointing at the paired linux-amlogic source. Executes
production Kodi functions and kernel policy/exit/HDR-admission branches with recording sysfs,
a controlled clock/vsync pump and hardware/settings adapters. Normal frame
processing and enable_dolby_vision register effects are modeled; no actual HDMI
packets, panel behavior or native library are tested. HDMI format staging and
application are modeled separately. --baseline REF must expose
the stale core and five-second close wait with the old Kodi source.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
extract = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
AML = 'xbmc/utils/AMLUtils.cpp'
KERNEL = 'drivers/amlogic/media/enhancement/amdolby_vision/amdolby_vision.c'

PRELUDE = r'''
#include <atomic>
#include <chrono>
#include <iostream>
#include <map>
#include <memory>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>
void check(bool ok, const char* text) { if (!ok) throw std::runtime_error(text); }
constexpr int LOGDEBUG=0, LOGINFO=1, LOGWARNING=2;
struct CLog { template<class... T> static void Log(T&&...) {} };
struct CStreamDetails { template<class T> static const char* DynamicRangeToString(T) { return ""; } };
enum class StreamHdrType { HDR_TYPE_NONE, HDR_TYPE_HDR10, HDR_TYPE_HDR10PLUS, HDR_TYPE_HLG, HDR_TYPE_DOLBYVISION };
enum DV_MODE { DV_MODE_OFF, DV_MODE_ON, DV_MODE_ON_DEMAND };
enum DV_TYPE { DV_TYPE_DISPLAY_LED, DV_TYPE_VS10_ONLY, DV_TYPE_PLAYER_LED_HDR2, DV_TYPE_PLAYER_LED_LLDV };
constexpr unsigned DOLBY_VISION_OUTPUT_MODE_IPT=0, DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL=1,
 DOLBY_VISION_OUTPUT_MODE_HDR10=2, DOLBY_VISION_OUTPUT_MODE_SDR10=3,
 DOLBY_VISION_OUTPUT_MODE_SDR8=4, DOLBY_VISION_OUTPUT_MODE_BYPASS=5;
constexpr int DOLBY_VISION_FOLLOW_SINK=0, DOLBY_VISION_FOLLOW_SOURCE=1,
 DOLBY_VISION_FORCE_OUTPUT_MODE=2, DOLBY_VISION_LL_DISABLE=0;
constexpr unsigned FLAG_TOGGLE_FRAME=0x80000000, FLAG_FORCE_RGB_OUTPUT=1,
 FLAG_FORCE_DOVI_LL=2, FLAG_MUTE=4;
constexpr int VD1_PATH=0, HDR_MODULE_ON=1, HDR_BY_DV_F_SRC=1, HDR_BY_DV_F_SINK=2,
 HDRP_BY_DV=4, FLAG_VSYNC_CNT=3, BYPASS_PROCESS=0, MAX_TRANSITION_DELAY=2,
 VIDEO_MUTE_ON_DV=1, VIDEO_MUTE_OFF=0;
enum signal_format_enum { FORMAT_INVALID, FORMAT_DOVI, FORMAT_DOVI_LL, FORMAT_HDR10,
 FORMAT_HDR10PLUS, FORMAT_HLG, FORMAT_SDR, FORMAT_MVC, FORMAT_CUVA, FORMAT_SDR_2020 };
struct vframe_s {};
struct vout_device_s { bool dv_info=true; };
struct vinfo_s { vout_device_s* vout_device; };
vout_device_s outputDevice;
vinfo_s outputInfo{&outputDevice};
const vinfo_s* get_current_vinfo() { return &outputInfo; }
bool sink_support_dolby_vision(const vinfo_s*, vframe_s*) { return true; }
bool sink_support_hdr(const vinfo_s*) { return true; }
bool is_meson_g12b_cpu() { return true; }
bool is_meson_g12a_cpu() { return false; }
int debug_dolby=0, efuse_mode=0, dolby_vision_efuse_bypass=0, cur_csc_type[1]={0xffff};
int hdrModule=0;
int get_hdr_module_status(int) { return hdrModule; }
void* p_funcs_stb=reinterpret_cast<void*>(1);
int dolby_vision_policy=DOLBY_VISION_FORCE_OUTPUT_MODE;
unsigned dolby_vision_mode=DOLBY_VISION_OUTPUT_MODE_BYPASS, dolby_vision_target_mode=5;
unsigned dolby_vision_hdr10_policy=HDR_BY_DV_F_SRC, dolby_vision_flags=0;
bool dolby_vision_enable=false, dolby_vision_on=false, dolby_vision_core1_on=false;
bool dolby_vision_wait_on=false, dolby_vision_on_in_uboot=false;
int dolby_vision_status=BYPASS_PROCESS, vsync_count=0, sdr_delay=0;
signal_format_enum dolby_vision_src_format=FORMAT_HDR10, last_dst_format=FORMAT_SDR;
bool videoPresent=true, vsyncRuns=true;
int disabledWithLiveCore=0, exits=0, packetRequests=0;
struct {unsigned video_width=3840,video_height=2160;} dovi_setting;
unsigned dolby_vision_mask=7;bool dovi_setting_video_flag=true;
void pr_dolby_dbg(const char*, ...) {}
void dolby_vision_set_toggle_flag(int) { dolby_vision_flags |= FLAG_TOGGLE_FRAME; }
int get_video_mute() { return VIDEO_MUTE_OFF; }
bool is_hdr10plus_frame(vframe_s*) { return dolby_vision_src_format==FORMAT_HDR10PLUS; }
bool is_hlg_frame(vframe_s*) { return dolby_vision_src_format==FORMAT_HLG; }
bool is_cuva_frame(vframe_s*) { return dolby_vision_src_format==FORMAT_CUVA; }
void send_hdmi_pkt(int,int,const vinfo_s*,vframe_s*) { ++packetRequests; }
void apply_stb_core_settings(bool,unsigned,int,unsigned,int) {}
void enable_dolby_vision(int enable) {
 check(enable==0,"only kernel bypass retirement is modeled");++exits;
 dolby_vision_on=dolby_vision_core1_on=dolby_vision_enable=false;
 dolby_vision_wait_on=dolby_vision_on_in_uboot=false;
 dolby_vision_status=BYPASS_PROCESS;
 dolby_vision_mode=dolby_vision_target_mode=DOLBY_VISION_OUTPUT_MODE_BYPASS;
}
@KERNEL@
constexpr int VD1_HDR=0, HDR_BYPASS=0;
void hdr_func(int,int,const vinfo_s*,void*) {}
int vpp_matrix_update(vframe_s*,const vinfo_s*,int,int) { return 0; }
int OrdinaryHDRPath() {
 // The actual amcsc VD1 gate returns before ordinary HDR/SDR processing when
 // DV still owns the pipeline, including when no video frame remains.
 bool dovi_on=false;vframe_s* vf=nullptr;vframe_s* vf_rpt=nullptr;
 const auto* vinfo=get_current_vinfo();int flags=0,vd_path=VD1_PATH;
 @HDR_GATE@
 return 1; // Reached the ordinary processing path; no packet/panel assertion.
}
int KernelStep() {
 // video.c: dolby_vision_proc runs its processing body only while enabled.
 if (!vsyncRuns || !is_dolby_vision_enable()) return 0;
 const auto* vinfo=get_current_vinfo();vframe_s frame;auto* vf=videoPresent?&frame:nullptr;
 int pps_state=0;
 int mode=dolby_vision_mode;
 if (dolby_vision_policy_process(&mode,videoPresent?dolby_vision_src_format:FORMAT_SDR,vf))
   dolby_vision_mode=mode;
 @BYPASS@
 // Adapter for an accepted normal frame: the enabled core remains active.
 dolby_vision_on=true;dolby_vision_core1_on=videoPresent;
 dolby_vision_status=1;
 dolby_vision_flags &= ~FLAG_TOGGLE_FRAME;
 return 0;
}
struct TestClock {
 using duration=std::chrono::microseconds;using rep=duration::rep;using period=duration::period;
 using time_point=std::chrono::time_point<TestClock>;static constexpr bool is_steady=true;
 static inline time_point current{};static time_point now() { return current; }
};
void usleep(int us) { TestClock::current+=std::chrono::microseconds(us);KernelStep(); }
std::map<std::string,std::string> nodes;
const std::string base="/sys/module/amdolby_vision/parameters/";
struct CSysfsPath {
 std::string path;explicit CSysfsPath(std::string p):path(std::move(p)){}
 template<class T> CSysfsPath(std::string p,T value):path(std::move(p)){if(Exists())Set(value);}
 bool Exists()const{return true;}
 template<class T> std::optional<T> Get()const {
  std::ostringstream out;
  if(path==base+"dolby_vision_mode")out<<dolby_vision_mode;
  else if(path==base+"dolby_vision_flags")out<<dolby_vision_flags;
  else if(path=="/sys/class/amdolby_vision/dv_video_on")out<<is_dolby_vision_video_on();
  else out<<(nodes.count(path)?nodes.at(path):"0");
  if constexpr(std::is_same_v<T,std::string>)return out.str();
  else {T value{};std::istringstream in(out.str());in>>value;return value;}
 }
 template<class T> void Set(T value){
  std::ostringstream out;out<<value;nodes[path]=out.str();
  if(path==base+"dolby_vision_policy")dolby_vision_policy=std::stoi(out.str());
  if(path==base+"dolby_vision_mode")dolby_vision_mode=std::stoul(out.str());
  if(path==base+"dolby_vision_flags")dolby_vision_flags=std::stoul(out.str());
  if(path==base+"dolby_vision_enable"){
   const bool enable=out.str()=="Y"||out.str()=="1";
   if(!enable&&dolby_vision_core1_on)++disabledWithLiveCore;
   dolby_vision_enable=enable;
  }
 }
};
struct CRenderManager {
 StreamHdrType m_hdrType_override=StreamHdrType::HDR_TYPE_NONE;
 bool m_bTriggerUpdateResolution=false;
 void TriggerUpdateResolutionHdr(StreamHdrType);
} renderer;
@TRIGGER@
StreamHdrType lastPublishedHDR=StreamHdrType::HDR_TYPE_NONE;
static void aml_dv_trigger_update_resolution(StreamHdrType type){
 renderer.TriggerUpdateResolutionHdr(type);
 lastPublishedHDR=renderer.m_hdrType_override;
 // The renderer consumes/reset its temporary override after applying it. Track
 // the last published target, not a supposedly permanent pending override.
 renderer.m_hdrType_override=StreamHdrType::HDR_TYPE_NONE;
 renderer.m_bTriggerUpdateResolution=false;
}
struct CDVCoreGuard {explicit CDVCoreGuard(const char*){}};
DV_MODE guiMode=DV_MODE_ON_DEMAND;DV_TYPE dvType=DV_TYPE_DISPLAY_LED;bool keepSDR=false;
using ColourFormat = std::pair<int, int>;
constexpr ColourFormat userColour{4, 3};
constexpr ColourFormat hdr2Colour{3, 3};
ColourFormat requestedColour=userColour, stagedColour=userColour, activeColour=userColour;
std::vector<ColourFormat> hdmiRefreshes;
struct AdvancedSettings {
 bool forceCS=false, limitCD=false;
 int savedCS=0, savedCD=0;
 bool GetForceCS() const { return forceCS; }
 bool GetLimitCD() const { return limitCD; }
 int GetForceCSPrevVal() const { return savedCS; }
 int GetLimitCDPrevVal() const { return savedCD; }
 void SetForceCS(bool value) { forceCS=value; }
 void SetLimitCD(bool value) { limitCD=value; }
 void SetForceCSPrevVal(int value) { savedCS=value; }
 void SetLimitCDPrevVal(int value) { savedCD=value; }
} advanced;
struct SettingsComponent {
 auto* GetAdvancedSettings() { return &advanced; }
};
struct DataCache {
 float GetVideoFps() const { return 23.976f; }
 StreamHdrType GetVideoHdrType() const {
  return dolby_vision_src_format==FORMAT_HDR10PLUS ? StreamHdrType::HDR_TYPE_HDR10PLUS :
                                                  StreamHdrType::HDR_TYPE_HDR10;
 }
};
struct CServiceBroker {
 static auto* GetSettingsComponent() { static SettingsComponent component; return &component; }
 static auto& GetDataCacheCore() { static DataCache cache; return cache; }
};
struct CSettings {
 static constexpr int SETTING_COREELEC_AMLOGIC_DV_TYPE=1,SETTING_COREELEC_AMLOGIC_DV_KEEP_SDR_STATE=2;
 static constexpr int SETTING_COREELEC_AMLOGIC_FORCE_CS=3,SETTING_COREELEC_AMLOGIC_LIMIT_CD=4;
 static constexpr int SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR=5;
 int GetInt(int key) const {
  if (key==SETTING_COREELEC_AMLOGIC_DV_TYPE) return dvType;
  if (key==SETTING_COREELEC_AMLOGIC_FORCE_CS) return requestedColour.first;
  if (key==SETTING_COREELEC_AMLOGIC_LIMIT_CD) return requestedColour.second;
  return 0;
 }
 bool GetBool(int) const { return keepSDR; }
 void SetInt(int key, int value) {
  if (key==SETTING_COREELEC_AMLOGIC_FORCE_CS) requestedColour.first=value;
  if (key==SETTING_COREELEC_AMLOGIC_LIMIT_CD) requestedColour.second=value;
  stagedColour=requestedColour; // The display settings callback stages attr.
 }
};
std::shared_ptr<CSettings> settings(){static auto s=std::make_shared<CSettings>();return s;}
struct CGUIDialogKaiToast {enum {Info};template<class... T>static void QueueNotification(T&&...){}};
struct {std::string Get(int){return {};}} g_localizeStrings;
struct RESOLUTION_INFO {};
struct CDisplaySettings {static CDisplaySettings& GetInstance(){static CDisplaySettings s;return s;}
 RESOLUTION_INFO GetResolutionInfo(int){return {};}int GetCurrentResolution(){return 0;}};
void write_resolution_ini(const RESOLUTION_INFO&) { stagedColour=requestedColour; }
bool vs10_conversion=false,vs10_conversion_reset_hdr10=false,aml_linux_force_422=false;
bool s_dvPlaybackActive=true,s_dvDiscHold=false,s_dvDiscDeferred=false;
std::atomic<bool>s_pm4kActive{false};std::atomic<int64_t>s_dvDiscDeferredSinceMs{0};
unsigned s_dvModeCached=5;std::atomic<unsigned>s_guiDvOutputMode{5};
std::shared_ptr<const void>s_dvPlaybackSession;
struct AMLDVOutputTransition{};std::shared_ptr<AMLDVOutputTransition>s_dvOutputTransition;
void aml_dv_dump_state(const char*){}
void aml_dv_backend_invalidate(const std::shared_ptr<const void>&){}
void aml_dv_detect_active_area_stop(){}
void aml_dv_auto_letterbox_watch_stop(){}
void aml_dv_reset_l5_signals(){}
void aml_dv_display_auto_now(){
 activeColour=nodes[base+"xbmc_aml_linux_force_422"]=="1" ? hdr2Colour : stagedColour;
 hdmiRefreshes.push_back(activeColour);
}
void aml_display_mode_round_trip(const char*){aml_dv_display_auto_now();}
std::string aml_dv_output_mode_to_string(unsigned){return {};}
void aml_reset_audio_from_vs10_change(){}
bool aml_is_dv_enable(){return dolby_vision_enable;}
DV_MODE aml_dv_mode(){return guiMode;}
int aml_dv_dolby_vision_mode(){return dolby_vision_mode;}
@OFF_DECLARATION@
void aml_kodi_set_cd_cs(int);
unsigned aml_dv_on(unsigned mode){
 if(mode==DOLBY_VISION_OUTPUT_MODE_IPT)mode=DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL;
 dolby_vision_enable=dolby_vision_on=true;dolby_vision_core1_on=videoPresent;
 dolby_vision_mode=mode;dolby_vision_policy=DOLBY_VISION_FORCE_OUTPUT_MODE;
 dolby_vision_status=1;last_dst_format=mode<=1?FORMAT_DOVI:FORMAT_HDR10;
 if(mode<=1)aml_dv_trigger_update_resolution(StreamHdrType::HDR_TYPE_DOLBYVISION);
 aml_kodi_set_cd_cs(1);
 aml_dv_display_auto_now();
 return mode;
}
@KODI@
void Reset(){
 nodes.clear();TestClock::current={};vsyncRuns=true;videoPresent=true;
 dolby_vision_enable=dolby_vision_on=dolby_vision_core1_on=false;
 dolby_vision_wait_on=dolby_vision_on_in_uboot=false;
 dolby_vision_mode=dolby_vision_target_mode=5;dolby_vision_flags=0;
 dolby_vision_policy=DOLBY_VISION_FORCE_OUTPUT_MODE;dolby_vision_status=BYPASS_PROCESS;
 sdr_delay=0;disabledWithLiveCore=exits=packetRequests=0;cur_csc_type[0]=0xffff;hdrModule=0;
 dolby_vision_src_format=FORMAT_HDR10;last_dst_format=FORMAT_SDR;
 guiMode=DV_MODE_ON_DEMAND;dvType=DV_TYPE_DISPLAY_LED;keepSDR=false;
 s_dvPlaybackActive=true;s_dvDiscHold=false;renderer={};vs10_conversion=false;
 vs10_conversion_reset_hdr10=false;
 lastPublishedHDR=StreamHdrType::HDR_TYPE_NONE;
 advanced={};
 requestedColour=stagedColour=activeColour=userColour;
 hdmiRefreshes.clear();
}
int Stop(){
 videoPresent=false;KernelStep();auto start=TestClock::now();
 aml_dv_wait_video_off(5);aml_dv_close();
 return std::chrono::duration_cast<std::chrono::milliseconds>(TestClock::now()-start).count();
}
int main(int argc,char** argv){try{
 bool baseline=argc>1&&std::string(argv[1])=="baseline";
 Reset();aml_dv_set_vs10_mode(0,StreamHdrType::HDR_TYPE_HDR10,true);
 aml_dv_set_vs10_mode(5,StreamHdrType::HDR_TYPE_HDR10,true);
 int stopMs=Stop();
 if(baseline){
  check(stopMs==5000,"parent reproduces exact five-second wait");
  check(dolby_vision_core1_on&&is_dolby_vision_on()&&!dolby_vision_enable,"parent leaves disabled live core");
  check(!OrdinaryHDRPath(),"parent excludes ordinary SDR restoration after stop");
  check(lastPublishedHDR==StreamHdrType::HDR_TYPE_DOLBYVISION,"parent leaves DV as last renderer target");
  std::cout<<"REPRODUCED: disabled live DV core; 5000ms stop; stale DV renderer target\n";return 0;
 }
 check(stopMs==0,"round-trip stop does not wait for stale DV core");
 check(!is_dolby_vision_on()&&!is_dolby_vision_video_on(),"SDR GUI path no longer owned by DV");
 check(OrdinaryHDRPath(),"ordinary SDR restoration is admitted after stop");
 check(lastPublishedHDR==StreamHdrType::HDR_TYPE_HDR10,"bypass restores source renderer target");
 check(!vs10_conversion,"bypass clears conversion bookkeeping");
 check(disabledWithLiveCore==0&&exits==1&&packetRequests>0,"kernel exit runs before userspace disable");
 for (const ColourFormat& original : {userColour, ColourFormat{4, 2}}) {
  Reset();
  guiMode=DV_MODE_ON;
  dvType=DV_TYPE_PLAYER_LED_HDR2;
  requestedColour=stagedColour=activeColour=original;
  for (int i=0; i<2; ++i) {
   aml_dv_set_vs10_mode(DOLBY_VISION_OUTPUT_MODE_HDR10,StreamHdrType::HDR_TYPE_HDR10,true);
   check(activeColour==hdr2Colour&&advanced.GetForceCS(),"active HDR2 forces 12-bit 422");
   hdmiRefreshes.clear();
   aml_dv_set_vs10_mode(DOLBY_VISION_OUTPUT_MODE_BYPASS,StreamHdrType::HDR_TYPE_HDR10,true);
   check(hdmiRefreshes==std::vector<ColourFormat>(2,original),
         "live bypass restores saved 444 and depth on the first existing refresh");
   check(!dolby_vision_enable&&!advanced.GetForceCS(),"live bypass releases only its colour override");
  }
  check(vs10_conversion_reset_hdr10,"always-on idle restoration remains pending for close");
 }
 Reset();
 dvType=DV_TYPE_PLAYER_LED_HDR2;
 dolby_vision_src_format=FORMAT_HDR10PLUS;
 aml_kodi_set_cd_cs(2);
 aml_dv_set_vs10_mode(DOLBY_VISION_OUTPUT_MODE_HDR10,StreamHdrType::HDR_TYPE_HDR10PLUS,true);
 aml_dv_set_vs10_mode(DOLBY_VISION_OUTPUT_MODE_BYPASS,StreamHdrType::HDR_TYPE_HDR10PLUS,true);
 check(activeColour==hdr2Colour&&advanced.GetLimitCD(),"native HDR10+ retains its 422 override");
 Reset();
 dvType=DV_TYPE_PLAYER_LED_HDR2;
 aml_dv_on(DOLBY_VISION_OUTPUT_MODE_HDR10);
 check(activeColour==hdr2Colour,"HDR2 startup applies 422");
 hdmiRefreshes.clear();
 aml_kodi_reset_cd_cs(); // Wrapper close runs before the codec's DV close.
 Stop();
 check(hdmiRefreshes==std::vector<ColourFormat>(2,userColour),
       "ordinary stop still restores 444 before refreshing HDMI");
 for(int source:{FORMAT_HDR10,FORMAT_DOVI,FORMAT_SDR,FORMAT_HLG})for(bool hdrActive:{false,true}){
  Reset();dolby_vision_src_format=static_cast<signal_format_enum>(source);
  if(hdrActive){cur_csc_type[0]=1;hdrModule=HDR_MODULE_ON;}
  for(int i=0;i<3;++i){aml_dv_on(0);aml_dv_off();
   check(!is_dolby_vision_on()&&!dolby_vision_core1_on,"all live sources retire cores");}
  check(disabledWithLiveCore==0,"repeated toggles never disable live cores");check(Stop()==0,"repeated stop");
 }
 Reset();check(Stop()==0&&!is_dolby_vision_on(),"plain HDR10 bypass stop");
 Reset();aml_dv_on(0);check(Stop()<5000&&!is_dolby_vision_on(),"DV stop drains and exits");
 Reset();guiMode=DV_MODE_ON;aml_dv_on(0);check(Stop()==0&&dolby_vision_enable,"always-on GUI retains DV");
 Reset();keepSDR=true;aml_dv_on(3);check(Stop()==0&&dolby_vision_enable,"keep-SDR retains its pipeline");
 Reset();s_dvDiscHold=true;aml_dv_on(0);check(Stop()==0&&dolby_vision_enable,"disc segment retains DV");
 Reset();dvType=DV_TYPE_VS10_ONLY;aml_dv_set_vs10_mode(0,StreamHdrType::HDR_TYPE_HDR10,true);
 check(!dolby_vision_enable,"VS10-only action still declined");
 Reset();aml_dv_set_vs10_mode(0,StreamHdrType::HDR_TYPE_HDR10,false);
 check(!dolby_vision_enable,"software conversion still declined");
 Reset();aml_dv_on(0);vsyncRuns=false;
 auto start=TestClock::now();aml_dv_wait_video_off(5);
 check(TestClock::now()-start==std::chrono::seconds(5),"genuine stuck drain keeps configured timeout");
 std::cout<<"PASS: DV teardown, HDR2 422/444 round trip, HDR10+ ownership, ordinary stop and held routes\n";
}catch(const std::exception&e){std::cerr<<"FAIL: "<<e.what()<<"\n";return 1;}}
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--kernel-source', type=Path, required=True)
    p.add_argument('--baseline')
    p.add_argument('--negative-controls', action='store_true')
    args = p.parse_args()
    aml = (subprocess.check_output(['git','show',args.baseline+':'+AML],cwd=ROOT,text=True)
           if args.baseline else (ROOT/AML).read_text())
    kernel = (args.kernel_source/KERNEL).read_text()
    video = (args.kernel_source/'drivers/amlogic/media/video_sink/video.c').read_text()
    assert 'if (is_dolby_vision_enable())' in extract(video,'static void dolby_vision_proc(')
    process = extract(kernel,'static int dv_process_internal(')
    bypass = extract(process,'if (dolby_vision_mode == DOLBY_VISION_OUTPUT_MODE_BYPASS)')
    csc = (args.kernel_source/'drivers/amlogic/media/enhancement/amvecm/amcsc.c').read_text()
    gate_start = csc.index('if (vd_path == VD1_PATH) {\n\t\tif (is_dolby_vision_on())')
    hdr_gate = extract(csc[gate_start:], 'if (is_dolby_vision_on())')
    policy = '\n'.join(extract(kernel,sig) for sig in [
        'static inline int mode_check(', 'static int dolby_vision_policy_process',
        'bool is_dolby_vision_on(void)', 'bool is_dolby_vision_video_on(void)',
        'bool is_dolby_vision_enable(void)'])
    kodi = '\n'.join(extract(aml,sig) for sig in [
        'static void aml_dv_toggle_frame(', 'void aml_dv_off(',
        'void aml_dv_set_vs10_mode(', 'void aml_dv_wait_video_off(', 'void aml_dv_close()',
        'void aml_kodi_set_cd_cs(', 'void aml_kodi_reset_cd_cs()'])
    off_declaration = 'void aml_dv_off(bool skip_hdmi_update=false);'
    if 'static void aml_restore_cd_cs(' in aml:
        kodi = extract(aml, 'static void aml_restore_cd_cs(') + '\n' + kodi
        off_declaration = 'void aml_dv_off(bool skip_hdmi_update=false,bool restore_vs10_colour=false);'
    # Time only is controlled: production wait conditions and timeouts are retained.
    kodi = kodi.replace('std::chrono::system_clock','TestClock')
    trigger = extract((ROOT/'xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.cpp').read_text(),
                      'void CRenderManager::TriggerUpdateResolutionHdr(')
    source = (PRELUDE.replace('@KERNEL@',policy).replace('@BYPASS@',bypass)
              .replace('@HDR_GATE@',hdr_gate).replace('@KODI@',kodi).replace('@TRIGGER@',trigger)
              .replace('@OFF_DECLARATION@',off_declaration))
    def run(source, negative=False):
        with tempfile.TemporaryDirectory(prefix='dv-bypass-stop-') as tmp:
            d=Path(tmp);(d/'test.cpp').write_text(source)
            subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror',
                            '-Wno-unused-parameter','-Wno-write-strings','-fsanitize=address,undefined',
                            '-fno-pie','-no-pie',str(d/'test.cpp'),'-o',str(d/'test')],check=True)
            r=subprocess.run([str(d/'test'),*(['baseline'] if args.baseline else [])],
                             capture_output=True,text=True,timeout=20,
                             env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'})
            if negative:check = r.returncode==1 and 'FAIL:' in r.stderr
            else:check = r.returncode==0
            assert check,r.stdout+r.stderr
            print(r.stdout.strip() if not negative else r.stderr.strip())
    run(source)
    if args.negative_controls:
        for name,old,new in [
            ('source policy instead of bypass','dolby_vision_policy", DOLBY_VISION_FORCE_OUTPUT_MODE);\n  if (modeChange)',
             'dolby_vision_policy", DOLBY_VISION_FOLLOW_SOURCE);\n  if (modeChange)'),
            ('missing renderer target','    aml_dv_trigger_update_resolution(hdrType);',''),
            ('missing explicit bypass mode','    dolby_vision_mode.Set(DOLBY_VISION_OUTPUT_MODE_BYPASS);',''),
            ('disable before kernel retirement','    dolby_vision_mode.Set(DOLBY_VISION_OUTPUT_MODE_BYPASS);',
             '    CSysfsPath("/sys/module/amdolby_vision/parameters/dolby_vision_enable", "N");\n    dolby_vision_mode.Set(DOLBY_VISION_OUTPUT_MODE_BYPASS);'),
            ('retain conversion bookkeeping','    vs10_conversion = false;\n    // The IPT action',
             '    // The IPT action'),
            ('retain stale chroma during refresh',
             'aml_dv_off(/*skip_hdmi_update=*/false, /*restore_vs10_colour=*/true);',
             'aml_dv_off();'),
        ]:
            assert old in source,name
            run(source.replace(old,new),True)
            print('REJECTED:',name)

if __name__=='__main__':main()
