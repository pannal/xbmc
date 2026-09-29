#!/usr/bin/env python3
"""Execute retained original-player actions and production VS10 policy with host gates.

Message transport, decoder information and native effects are recording substitutes.
No full Kodi scheduler, CE or device claim.
"""
import argparse
from pathlib import Path
import runpy

ROOT=Path(__file__).resolve().parents[1]
fixture=runpy.run_path(str(ROOT/'tools/test-aml-native-continuation.py'))
function,run=fixture['function'],fixture['run']
PREFIX=r'''
#include "cores/VideoPlayer/PlayerNativeAction.h"
#include <cassert>
#include <deque>
#include <functional>
#include <future>
#include <iostream>
#include <vector>
using namespace std::chrono_literals;
enum class StreamHdrType{HDR_TYPE_NONE,HDR_TYPE_HDR10,HDR_TYPE_DOLBYVISION};
constexpr int ACTION_VS10_ORIGINAL=1,ACTION_VS10_SDR=2,ACTION_VS10_HDR10=3,ACTION_VS10_DV=4;
constexpr unsigned DOLBY_VISION_OUTPUT_MODE_IPT=1,DOLBY_VISION_OUTPUT_MODE_HDR10=2,DOLBY_VISION_OUTPUT_MODE_SDR10=3,DOLBY_VISION_OUTPUT_MODE_BYPASS=4;
enum DV_TYPE{DV_TYPE_DISPLAY_LED,DV_TYPE_VS10_ONLY};
struct CSettings{static constexpr int SETTING_COREELEC_AMLOGIC_DV_TYPE=1;};
struct Settings{int type=DV_TYPE_DISPLAY_LED;int GetInt(int){return type;}}config;
Settings* settings(){return &config;}
constexpr int LOGINFO=0;
struct CLog{template<class...T>static void Log(T...){}};
struct Localize{int Get(int v){return v;}}g_localizeStrings;
struct CGUIDialogKaiToast{enum{Info};static inline int notices=0;static void QueueNotification(int,int,int){++notices;}};
CAMLSession* session=nullptr;
std::thread::id nativeOwner;
std::function<void()> nativeHook;
std::vector<unsigned int> modes;
int resets=0,dumps=0;
bool enabled=true,vs10_conversion=false,vs10_conversion_reset_hdr10=false;
unsigned int existingMode=DOLBY_VISION_OUTPUT_MODE_IPT;
void aml_dv_dump_state(const char*){assert(session&&!session->AcquireDecoder());nativeOwner=std::this_thread::get_id();++dumps;if(nativeHook)nativeHook();}
int aml_dv_output_mode_to_string(unsigned int v){return v;}
struct CSysfsPath{explicit CSysfsPath(const char*){}template<class T>std::optional<T> Get(){return existingMode;}};
void aml_dv_on(unsigned int v){modes.push_back(v);existingMode=v;}
bool aml_is_dv_enable(){return enabled;}
void aml_dv_off(){modes.push_back(DOLBY_VISION_OUTPUT_MODE_BYPASS);existingMode=DOLBY_VISION_OUTPUT_MODE_BYPASS;}
void aml_reset_audio_from_vs10_change(){++resets;}
@UTILITY@
struct CDVDMsg{enum Message{PLAYER_VS10_ACTION,GENERAL_GUI_ACTION};explicit CDVDMsg(Message v):type(v){}virtual ~CDVDMsg()=default;bool IsType(Message v){return type==v;}Message type;};
template<class T>struct CDVDMsgType:CDVDMsg{CDVDMsgType(Message v,T value):CDVDMsg(v),m_value(std::move(value)){}T m_value;};
struct Messenger{std::mutex mutex;std::deque<std::shared_ptr<CDVDMsg>> messages;void Put(std::shared_ptr<CDVDMsg> m){std::lock_guard<std::mutex> lock(mutex);messages.push_back(m);}};
struct CAction{int id;int GetID()const{return id;}};
struct IDVDStreamPlayer{enum{SYNC_STARTING,SYNC_INSYNC};};
struct ProcessInfo{bool hw=true;int reads=0;bool IsVideoHwDecoder(){++reads;return hw;}};
struct CVideoPlayer{
 CPlayerNativeAction<StreamHdrType> m_vs10Action;
 Messenger m_messenger;
 std::unique_ptr<ProcessInfo> m_processInfo=std::make_unique<ProcessInfo>();
 struct{int syncState=IDVDStreamPlayer::SYNC_INSYNC;}m_CurrentVideo;
 bool pendingLifecycle=false,m_displayLost=false;
 bool ParentLifecyclePending(){return pendingLifecycle;}
 void QueueVS10Action(int);void ContinueVS10Action();
 bool OnAction(const CAction& action){switch(action.GetID()){@CASES@ default:return false;}}
 void Dispatch(){while(!m_messenger.messages.empty()){auto pMsg=m_messenger.messages.front();m_messenger.messages.pop_front();@DISPATCH@}}
};
@METHODS@
void ready(){auto d=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(d));assert(CAMLSession::EndDisplay(d,CAMLSession::DisplayPhase::READY));}
void open(CAMLSession& s){auto r=s.Fence();assert(s.BeginMutation(r));assert(s.Complete(r,true));}
void init(CVideoPlayer& p,CAMLSession& s,StreamHdrType hdr=StreamHdrType::HDR_TYPE_HDR10){ready();open(s);session=&s;p.m_vs10Action.BeginStream(hdr);modes.clear();resets=dumps=CGUIDialogKaiToast::notices=0;nativeHook={};config.type=DV_TYPE_DISPLAY_LED;existingMode=DOLBY_VISION_OUTPUT_MODE_IPT;enabled=true;vs10_conversion=false;vs10_conversion_reset_hdr10=false;}
void action(CVideoPlayer& p,int id){assert(p.OnAction({id}));p.Dispatch();p.ContinueVS10Action();}
'''
TESTS=r'''
void original_owner_and_supersession(){
 CVideoPlayer p;CAMLSession s;init(p,s);auto permit=std::make_unique<CAMLSession::Permit>(s.AcquireDecoder());
 std::thread producer([&]{assert(p.OnAction({ACTION_VS10_SDR}));});producer.join();
 p.Dispatch();p.ContinueVS10Action();assert(modes.empty()&&dumps==0&&!s.AcquireDecoder());
 assert(p.OnAction({ACTION_VS10_HDR10}));p.Dispatch();
 permit.reset();p.ContinueVS10Action();assert((modes==std::vector<unsigned>{DOLBY_VISION_OUTPUT_MODE_HDR10}));
 assert(nativeOwner==std::this_thread::get_id()&&resets==1);p.ContinueVS10Action();assert(resets==1);
}
void stream_and_player_identity(){
 CVideoPlayer original,newer;CAMLSession s;init(original,s);
 original.QueueVS10Action(ACTION_VS10_SDR);auto old=original.m_vs10Action.Capture(ACTION_VS10_DV);
 original.m_vs10Action.BeginStream(StreamHdrType::HDR_TYPE_DOLBYVISION);
 original.Dispatch();original.ContinueVS10Action();assert(dumps==0);
 newer.m_vs10Action.BeginStream(StreamHdrType::HDR_TYPE_HDR10);newer.m_vs10Action.Queue(old);newer.ContinueVS10Action();assert(dumps==0);
 auto permit=std::make_unique<CAMLSession::Permit>(s.AcquireDecoder());action(original,ACTION_VS10_SDR);
 original.m_vs10Action.Invalidate();permit.reset();original.ContinueVS10Action();assert(dumps==0&&s.AcquireDecoder());
 assert(original.OnAction({ACTION_VS10_DV}));assert(original.m_messenger.messages.empty());
}
void foreign_intent_preserves_pending(){
 CVideoPlayer original,newer;CAMLSession s;init(original,s);
 auto foreign=original.m_vs10Action.Capture(ACTION_VS10_DV);
 newer.m_vs10Action.BeginStream(StreamHdrType::HDR_TYPE_HDR10);
 auto permit=std::make_unique<CAMLSession::Permit>(s.AcquireDecoder());
 action(newer,ACTION_VS10_HDR10);assert(dumps==0);
 newer.m_vs10Action.Queue(foreign);permit.reset();newer.ContinueVS10Action();
 assert((modes==std::vector<unsigned>{DOLBY_VISION_OUTPUT_MODE_HDR10})&&resets==1);
}
void suspend_for_lifecycle(){
 CVideoPlayer p;CAMLSession s;init(p,s);auto permit=std::make_unique<CAMLSession::Permit>(s.AcquireDecoder());action(p,ACTION_VS10_SDR);
 assert(!s.AcquireDecoder());p.pendingLifecycle=true;p.ContinueVS10Action();assert(s.AcquireDecoder());
 permit.reset();p.ContinueVS10Action();assert(dumps==0);p.pendingLifecycle=false;
 p.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_STARTING;p.ContinueVS10Action();assert(dumps==0&&s.AcquireDecoder());
 p.m_CurrentVideo.syncState=IDVDStreamPlayer::SYNC_INSYNC;p.m_displayLost=true;p.ContinueVS10Action();assert(dumps==0&&s.AcquireDecoder());
 p.m_displayLost=false;p.ContinueVS10Action();assert(resets==1&&modes.back()==DOLBY_VISION_OUTPUT_MODE_SDR10);
}
void admitted_completion_and_exception(){
 CVideoPlayer p;CAMLSession s;init(p,s);
 bool once=false;nativeHook=[&]{if(!once){once=true;p.m_vs10Action.EndStream();assert(!p.m_vs10Action.Capture(ACTION_VS10_DV).stream);}};
 action(p,ACTION_VS10_ORIGINAL);assert(resets==1&&modes.back()==DOLBY_VISION_OUTPUT_MODE_BYPASS&&vs10_conversion_reset_hdr10);
 p.ContinueVS10Action();assert(resets==1&&s.AcquireDecoder());
 p.m_vs10Action.BeginStream(StreamHdrType::HDR_TYPE_HDR10);nativeHook=[] {throw 1;};
 try{action(p,ACTION_VS10_DV);assert(false);}catch(int){}
 assert(s.AcquireDecoder());nativeHook={};p.ContinueVS10Action();assert(resets==1);
 action(p,ACTION_VS10_HDR10);assert(resets==2);
}
void policy(){
 {CVideoPlayer p;CAMLSession s;init(p,s,StreamHdrType::HDR_TYPE_DOLBYVISION);action(p,ACTION_VS10_ORIGINAL);assert(modes.back()==DOLBY_VISION_OUTPUT_MODE_IPT&&!vs10_conversion);}
 {CVideoPlayer p;CAMLSession s;init(p,s);action(p,ACTION_VS10_SDR);assert((modes==std::vector<unsigned>{DOLBY_VISION_OUTPUT_MODE_HDR10,DOLBY_VISION_OUTPUT_MODE_SDR10}));assert(vs10_conversion);}
 {CVideoPlayer p;CAMLSession s;init(p,s);p.m_processInfo->hw=false;action(p,ACTION_VS10_DV);assert(modes.empty()&&resets==0&&CGUIDialogKaiToast::notices==1);action(p,ACTION_VS10_ORIGINAL);assert(modes.back()==DOLBY_VISION_OUTPUT_MODE_BYPASS&&resets==1);}
 {CVideoPlayer p;CAMLSession s;init(p,s);config.type=DV_TYPE_VS10_ONLY;action(p,ACTION_VS10_HDR10);assert(modes.empty()&&resets==0);}
 {CVideoPlayer p;CAMLSession s;init(p,s);auto display=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(display));action(p,ACTION_VS10_DV);assert(dumps==0);assert(CAMLSession::EndDisplay(display,CAMLSession::DisplayPhase::READY));p.ContinueVS10Action();assert(modes.back()==DOLBY_VISION_OUTPUT_MODE_IPT);}
}
int main(){original_owner_and_supersession();stream_and_player_identity();foreign_intent_preserves_pending();suspend_for_lifecycle();admitted_completion_and_exception();policy();std::cout<<"PASS: original-player VS10 intents, native admission and policy (ASan/UBSan)\n";}
'''

def source():
    player=(ROOT/'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
    utility=function((ROOT/'xbmc/utils/AMLUtils.cpp').read_text(),'void aml_dv_set_vs10_mode(')
    methods='\n'.join(function(player,sig) for sig in ('void CVideoPlayer::QueueVS10Action(', 'void CVideoPlayer::ContinueVS10Action('))
    onaction=function(player,'bool CVideoPlayer::OnAction(')
    cases=onaction[onaction.index('    case ACTION_VS10_ORIGINAL:'):onaction.index('    case ACTION_TOGGLE_VIDEO_FREERUN_MODE:')]
    messages=function(player,'void CVideoPlayer::HandleMessages()')
    dispatch=messages[messages.index('    else if (pMsg->IsType(CDVDMsg::PLAYER_VS10_ACTION))'):messages.index('    else if (pMsg->IsType(CDVDMsg::GENERAL_GUI_ACTION))')].replace('else if','if',1)
    process=function(player,'void CVideoPlayer::Process()')
    assert 'while (!m_bAbortRequest)\n  {\n    ContinueVS10Action();' in process
    for sig,call in [('bool CVideoPlayer::OpenFile(', 'm_vs10Action.Invalidate();'),('bool CVideoPlayer::CloseFile(', 'm_vs10Action.Invalidate();'),('void CVideoPlayer::OnExit()', 'm_vs10Action.EndStream();'),('bool CVideoPlayer::CloseStream(', 'm_vs10Action.EndStream();'),('void CVideoPlayer::FlushBuffers(', 'm_vs10Action.Suspend();')]:
        assert call in function(player,sig),(sig,call)
    opening=function(player,'bool CVideoPlayer::OpenStream(')
    assert opening.index('m_vs10Action.EndStream();')<opening.index('res = OpenVideoStream(hint, reset);')
    assert opening.index('if (res)')<opening.index('m_vs10Action.BeginStream(hint.hdrType);')
    assert 'GetDataCacheCore' not in methods+utility+cases
    return PREFIX.replace('@UTILITY@',utility).replace('@CASES@',cases).replace('@DISPATCH@',dispatch).replace('@METHODS@',methods)+TESTS

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    code=source();run(code)
    if args.negative_controls:
        path='cores/VideoPlayer/PlayerNativeAction.h';original=(ROOT/'xbmc'/path).read_text()
        for label,old,new in [
          ('accept foreign stream','!intent.stream || intent.stream != m_stream','!intent.stream'),
          ('ignore close invalidation','if (m_pending->stream != m_stream)','if (false)'),
          ('bypass admission','if (!m_native->TryBegin())','if (false && !m_native->TryBegin())'),
          ('keep fence during flush','void Suspend() { m_native.reset(); }','void Suspend() {}'),
        ]:
            assert old in original
            run(code,(path,original.replace(old,new)),True);print('REJECTED:',label)
        for label,old,new in [
          ('ignore software decoder','!hardwareDecoder)','((void)hardwareDecoder, false))'),
          ('apply during startup','m_CurrentVideo.syncState != IDVDStreamPlayer::SYNC_INSYNC','false'),
          ('lose HDR10 transition','aml_dv_on(DOLBY_VISION_OUTPUT_MODE_HDR10);','(void)0;'),
        ]:
            assert old in code
            run(code.replace(old,new),negative=True);print('REJECTED:',label)

if __name__=='__main__':main()
