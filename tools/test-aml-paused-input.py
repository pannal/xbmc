#!/usr/bin/env python3
"""Production input retry/continuation and pause admission with recording boundaries.

Uses real AddData, write_av_packet, lifecycle, wrapper continuation/early admission
and CanOutputPicture. Native write, converter, header/checkin, clock and queue
scheduling are controlled inputs. No decoder, HDMI or device-timing acceptance.
"""
import argparse
import runpy
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
base = runpy.run_path(str(ROOT / 'tools/test-aml-lifecycle.py'))
function = base['function']

TESTS = r'''
static std::vector<uint8_t> delivered;
static int calls=0, headers=0, checkins=0, conversions=0;
static std::function<int(uint8_t*,int)> deviceWrite;
int Dll::codec_write(void*,uint8_t* data,int size) {++calls; return deviceWrite(data,size);}
int check_in_pts(Private*,am_packet_t*) {++checkins;return PLAYER_SUCCESS;}
int write_header(Private*,am_packet_t*) {++headers;return PLAYER_SUCCESS;}
void dumpfile_write(Private*,uint8_t*,int) {}
int accept(uint8_t* data,int size) {delivered.insert(delivered.end(),data,data+size);return size;}
void clean() {delivered.clear();calls=headers=checkins=conversions=0;deviceWrite=accept;}

void partial(bool dual, bool twice) {
 clean(); CDVDVideoCodecAmlogic wrapper;assert(wrapper.m_Codec->OpenDecoder());
 wrapper.m_Codec->trace.clear();wrapper.m_inputPauseAllowed=true;
 uint8_t bytes[]{1,2,3,4,5,6}; DemuxPacket packet{bytes,6,42,43};
 if(dual)wrapper.m_packages.emplace_back(new uint8_t[1],1,false,42);
 wrapper.dual=dual;
 deviceWrite=[&](uint8_t* p,int){
   if(calls==1)return accept(p,2);
   wrapper.m_Codec->m_processInfo.speed=0;errno=EAGAIN;return -1;
 };
 assert(!wrapper.Feed(packet));
 assert(wrapper.m_Codec->HasPendingInput() && wrapper.m_pendingInput);
 assert(delivered==std::vector<uint8_t>({1,2}) && headers==1 && checkins==1 && conversions==1);
 assert(wrapper.m_Codec->trace.empty() && wrapper.m_packages.size()==(dual?1:0));
 // Repeated calls while paused neither touch converter storage nor write/reset.
 const int before=calls;
 assert(!wrapper.Feed(packet));assert(calls==before && conversions==1);
 if(twice){
   wrapper.m_Codec->m_processInfo.speed=1;
   deviceWrite=[&](uint8_t* p,int){
     if(calls==before+1)return accept(p,1);
     wrapper.m_Codec->m_processInfo.speed=0;errno=EAGAIN;return -1;
   };
   assert(!wrapper.Feed(packet));
 }
 wrapper.m_Codec->m_processInfo.speed=1;deviceWrite=accept;
 assert(wrapper.Feed(packet));
 assert(delivered==std::vector<uint8_t>(bytes,bytes+6));
 assert(headers==1 && checkins==1 && conversions==1);
 assert(!wrapper.m_pendingInput && !wrapper.m_Codec->HasPendingInput());
 assert(wrapper.m_Codec->trace.empty() && wrapper.m_packages.empty());
}
void cancel(bool close) {
 clean();CDVDVideoCodecAmlogic wrapper;assert(wrapper.m_Codec->OpenDecoder());
 wrapper.m_inputPauseAllowed=true;uint8_t bytes[]{8,9,10};DemuxPacket packet{bytes,3,42,43};
 deviceWrite=[&](uint8_t* p,int){if(calls==1)return accept(p,1);
   wrapper.m_Codec->m_processInfo.speed=0;errno=EAGAIN;return -1;};
 assert(!wrapper.Feed(packet));assert(wrapper.m_pendingInput);
 auto codec=wrapper.m_Codec;
 if(close)wrapper.Close();else wrapper.Reset();
 assert(!wrapper.m_pendingInput && !codec->HasPendingInput());
 if(!close){codec->m_processInfo.speed=1;deviceWrite=accept;
   uint8_t replacement[]{11,12};DemuxPacket next{replacement,2,100,101};
   assert(wrapper.Feed(next));assert(delivered==std::vector<uint8_t>({8,11,12}));}
}
void boundary(bool pause,bool complete,bool abort) {
 clean();CAMLCodec codec;assert(codec.OpenDecoder());codec.trace.clear();uint8_t data[]{1};
 deviceWrite=[&](uint8_t* p,int n){
   if(calls==100){
     if(pause)codec.m_processInfo.speed=0;
     if(abort)codec.m_abort=true;
     if(complete)return accept(p,n);
   }
   errno=EAGAIN;return -1;
 };
 const bool accepted=codec.AddData(data,1,0,0,true);
 assert(accepted==(complete&&!abort));assert(calls==100);
 assert(codec.LifecyclePending()==(!pause&&!complete&&!abort));
 if(pause&&!complete&&!abort)assert(codec.HasPendingInput());
}
void hardError() {
 clean();CAMLCodec codec;assert(codec.OpenDecoder());codec.trace.clear();uint8_t data[]{1,2,3};
 deviceWrite=[&](uint8_t* p,int){
   if(calls==1)return accept(p,1);
   codec.m_processInfo.speed=0;errno=EIO;return -1;
 };
 assert(!codec.AddData(data,3,0,0,true));
 assert(delivered==std::vector<uint8_t>({1}));
 assert(!codec.HasPendingInput() && codec.m_wrFailActive && "hard error cannot become pause continuation");
 codec.m_processInfo.speed=1;
 codec.m_tpWrFailLastReset-=std::chrono::milliseconds(300);
 assert(!codec.AddData(data,3,0,0,true));
 assert(codec.LifecyclePending() && "real failure recovery survives concurrent pause");
}
void landing(bool advance) {
 clean();CAMLCodec codec;assert(codec.OpenDecoder());codec.m_processInfo.speed=0;
 codec.m_speed=0;codec.m_processInfo.advance=advance;uint8_t data[]{3};
 assert(codec.AddData(data,1,0,0,advance)); // landing: allowInputPause=false
 assert(delivered==std::vector<uint8_t>({3}) && !codec.LifecyclePending());
}
void wrongIdentity() {
 clean();CDVDVideoCodecAmlogic wrapper;assert(wrapper.m_Codec->OpenDecoder());
 wrapper.m_inputPauseAllowed=true;wrapper.m_Codec->m_processInfo.speed=0;
 uint8_t data[]{1};DemuxPacket first{data,1,0,0},other{data,1,0,0};
 assert(!wrapper.Feed(first));assert(!wrapper.Feed(other));
 assert(!wrapper.m_pendingInput && !wrapper.m_Codec->HasPendingInput());
 assert(calls==0);
}
void admission() {
 CVideoPlayerVideo player;assert(player.CanOutputPicture());
 player.m_processInfo.speed=0;assert(!player.CanOutputPicture()); // before speed message
 player.m_speed=0;assert(!player.CanOutputPicture());
 player.m_processInfo.advance=true;assert(player.CanOutputPicture());
 player.m_processInfo.advance=false;
 for(int state:{IDVDStreamPlayer::SYNC_STARTING,IDVDStreamPlayer::SYNC_WAITSYNC}){
   player.m_syncState=state;assert(player.CanOutputPicture());
 }
 player.m_paused=true;assert(!player.CanOutputPicture());
 player.m_paused=false;player.m_syncState=IDVDStreamPlayer::SYNC_INSYNC;
 player.m_processInfo.speed=1;assert(!player.CanOutputPicture()); // speed message still pending
 player.m_speed=1000;assert(player.CanOutputPicture());
}
int main(){
 partial(false,false);partial(true,false);partial(true,true);
 cancel(false);cancel(true);hardError();wrongIdentity();landing(false);landing(true);admission();
 boundary(false,false,false);boundary(true,false,false);boundary(false,true,false);
 boundary(true,true,false);boundary(false,false,true);
 puts("PASS paused input: exact partial bytes/PTS/header, conversion pinning, dual-layer pop, seek/stop cancellation, write100 completion/pause/abort, startup/seek/frame advance and unpaused recovery");
}
'''


def harness(baseline=False):
    codec=(ROOT/'xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp').read_text()
    wrapper=(ROOT/'xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp').read_text()
    video=(ROOT/'xbmc/cores/VideoPlayer/VideoPlayerVideo.cpp').read_text()
    h=base['harness']
    old=h.__globals__['TESTS'];h.__globals__['TESTS']=''
    try: source=h()
    finally:h.__globals__['TESTS']=old
    source=source.replace('size_t data_size{};', 'int data_size{};')
    source=source.replace('constexpr int PLAYER_SUCCESS=0,', 'constexpr int PLAYER_WR_FAILED=-1;\nconstexpr int AVERROR(int err){return -err;}\nconstexpr int PLAYER_SUCCESS=0,')
    source=source.replace('struct am_packet_t {','struct am_packet_t {\n  void* codec{};')
    source=source.replace('struct Private {','struct Dll;\nstruct Private {\n Dll* m_dll{};')
    source=source.replace(function(source,'int write_av_packet('),'int write_av_packet(Private*,am_packet_t*);')
    source=source.replace('struct Dll {','struct Dll {\n int codec_write(void*,uint8_t*,int);')
    source=source.replace('Private storage; Private* am_private{&storage};',
                          'Private storage; Private* am_private{&storage};\n CAMLCodec(){storage.m_dll=&dll;}\n bool HasPendingInput() const{return m_inputPending;}')
    source=source.replace('constexpr int AML_G12B=1;',
                          'struct DemuxPacket{uint8_t* pData;uint32_t iSize;double dts,pts;};\nconstexpr int AML_G12B=1;')
    source=source.replace('int codec{AV_CODEC_ID_H264};} m_hints;',
                          'int codec{AV_CODEC_ID_H264};bool ptsinvalid=false;} m_hints;')
    source=source.replace('std::optional<int> m_pendingInput;',r'''
 struct PendingInput{const DemuxPacket* packet;uint8_t* data;uint32_t size;bool dualLayer;};
 std::optional<PendingInput> m_pendingInput;bool m_inputPauseAllowed=false,dual=false;
 bool AddPreparedInput(const DemuxPacket&);
 bool Feed(const DemuxPacket&);
 std::vector<uint8_t> converted;
''')
    write=function(codec,'int write_av_packet(').replace('am_private_t','Private')
    declarations='int check_in_pts(Private*,am_packet_t*);\nint write_header(Private*,am_packet_t*);\nvoid dumpfile_write(Private*,uint8_t*,int);\n'
    prefix=wrapper[wrapper.index('  if (!ContinueLifecycle())',wrapper.index('bool CDVDVideoCodecAmlogic::AddData(')):]
    prefix=prefix[:prefix.index('  // More input after')]
    feed=r'''
extern int conversions;
bool CDVDVideoCodecAmlogic::Feed(const DemuxPacket& packet){
@PREFIX@
 ++conversions;
 // A conversion invalidates its previous output. An unintended second
 // conversion therefore corrupts the native suffix and the exact-byte oracle.
 converted.assign(packet.pData,packet.pData+packet.iSize);
 if(conversions>1)for(auto& byte:converted)byte=0xee;
 m_pendingInput=PendingInput{&packet,converted.data(),packet.iSize,dual};
 return AddPreparedInput(packet);
}
'''.replace('@PREFIX@',prefix)
    # cancel()->replacement deliberately performs a second independent conversion.
    feed=feed.replace('if(conversions>1)for(auto& byte:converted)byte=0xee;',
                       'if(m_Codec->HasPendingInput())for(auto& byte:converted)byte=0xee;')
    player=r'''
struct IDVDStreamPlayer{enum{SYNC_INSYNC,SYNC_STARTING,SYNC_WAITSYNC};};
struct CVideoPlayerVideo{
 int m_speed=1000,m_syncState=IDVDStreamPlayer::SYNC_INSYNC;bool m_paused=false;
 struct {float speed=1;bool advance=false;float GetNewSpeed()const{return speed;}
 bool IsFrameAdvance()const{return advance;}}m_processInfo;
 bool CanOutputPicture() const;
};
'''
    if baseline:
        oldcodec=subprocess.check_output(['git','show','67cd65cf93:xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp'],cwd=ROOT,text=True)
        oldadd=function(oldcodec,'bool CAMLCodec::AddData(').replace('double pts)','double pts, bool /*allowInputPause*/)')
        source=source.replace(function(source,'bool CAMLCodec::AddData('),oldadd)
    return source+declarations+write+player+function(video,'bool CVideoPlayerVideo::CanOutputPicture() const')+function(wrapper,'bool CDVDVideoCodecAmlogic::AddPreparedInput(')+feed+TESTS.replace('static int calls=0, headers=0, checkins=0, conversions=0;', 'static int calls=0, headers=0, checkins=0; int conversions=0;')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls',action='store_true')
    args=parser.parse_args()
    source=harness()
    base['compile_run'](source)
    if args.negative_controls:
        # Baseline retries reset rather than suspending the partial packet.
        base['compile_run'](harness(True),True);print('REJECTED: original AddData')
        mutants=[
            ('hide hard error behind pause','if (!write_failed && am_private->am_pkt.isvalid && pauseInput())','if (am_private->am_pkt.isvalid && pauseInput())'),
            ('reinitialize pending packet','if (!m_inputPending)','if (true)'),
            ('reconvert on resume','if (m_pendingInput && m_Codec && m_Codec->HasPendingInput())','if (false)'),
            ('discard paused cursor','m_inputPending = true;','m_inputPending = false;'),
            ('block paused landing','if (!allowInputPause ||','if ((allowInputPause && false) ||'),
            ('block frame advance','m_processInfo.IsFrameAdvance())','false)'),
            ('ignore requested pause','m_speed == DVD_PLAYSPEED_PAUSE || m_processInfo.GetNewSpeed() == 0.0f','m_speed == DVD_PLAYSPEED_PAUSE'),
            ('reset completed write100','if (loop == 100 && am_private->am_pkt.isvalid)','if (loop == 100)'),
        ]
        for name,old,new in mutants:
            assert old in source,name
            base['compile_run'](source.replace(old,new,1),True);print('REJECTED:',name)

if __name__=='__main__':main()
