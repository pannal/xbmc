#!/usr/bin/env python3
"""Connect native chapter targeting to parent transport and real AML output gates.

Uses production-method fixtures for BD/demux, parent queue/flush/packet controls,
and full AML GetPicture/OutputPicture/start admission. Libbluray, encoded input,
clock/services and renderer are recording host boundaries. Tables are synthetic;
no disc/BD-J VM/hardware output is exercised.
"""
import argparse
import json
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VP = ROOT / 'xbmc/cores/VideoPlayer'
extract = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def harness(original_demux=None, original_output=None):
    parent = (VP / 'VideoPlayer.cpp').read_text()
    bd = runpy.run_path(str(ROOT / 'tools/test-bd-chapter-clock.py'))['harness'](parent)
    bd = bd[:bd.rindex('int main(){')]
    if original_demux:
        current=extract(bd,'bool CDVDDemuxFFmpeg::SeekChapter(')
        old=extract(original_demux.read_text(),'bool CDVDDemuxFFmpeg::SeekChapter(')
        # Signature-only fixture adapter: original seek body is unchanged.
        old=old.replace('int chapter, double* startpts)', 'int chapter, double* startpts, double*)',1)
        bd=bd.replace(current,old,1)
    transport = runpy.run_path(str(ROOT / 'tools/test-video-preroll-target.py'))['harness']()
    transport = transport[:transport.rindex('int main()')]
    aml = runpy.run_path(str(ROOT / 'tools/test-aml-preroll.py'))
    native = aml['PREFIX']
    native += '\n' + extract((VP / 'DVDCodecs/Video/AMLCodec.h').read_text(), 'void SetPrerollTarget(').replace(
        'void SetPrerollTarget(', 'void CAMLCodec::SetPrerollTarget(')
    native += '\n' + aml['method']((original_output or VP / 'DVDCodecs/Video/AMLCodec.cpp').read_text())
    video = (VP / 'VideoPlayerVideo.cpp').read_text()
    native += '\n' + aml['ADMISSION'].replace('@START@', extract(video, 'if (m_syncState == IDVDStreamPlayer::SYNC_STARTING &&')).replace(
        '@OUTPUT@', extract(video, 'CVideoPlayerVideo::EOutputState CVideoPlayerVideo::OutputPicture(').replace('#if defined(HAS_LIBAMCODEC)', '#if 1'))
    # Keep real native sentinel values across fixture boundaries. Undefine the
    # TimingConstants macros after its global include so fixture declarations
    # keep their ordinary names; global conversion functions remain actual.
    bd = bd.replace('DVD_NOPTS_VALUE=-1.e30', 'DVD_NOPTS_VALUE=static_cast<double>(0xFFF0000000000000ULL)')
    transport = transport.replace('DVD_NOPTS_VALUE=-1;', 'DVD_NOPTS_VALUE=static_cast<double>(0xFFF0000000000000ULL);')
    native = 'constexpr double DVD_TIME_BASE=1000000,DVD_NOPTS_VALUE=static_cast<double>(0xFFF0000000000000ULL);\nconstexpr int DVD_PLAYSPEED_NORMAL=1000,DVD_PLAYSPEED_PAUSE=0;\n' + native
    # Actual wrapper setter bridges packet target installation into actual AML
    # storage. Native input/decode arrival remains a controlled asynchronous queue.
    old = 'void SetPrerollTarget(double target) { prerollTarget=target; }'
    assert transport.count(old) == 1
    transport = transport.replace(old, 'Native::CAMLCodec native; Native::CAMLCodec* m_Codec=&native;\n  unsigned references=0;void SetPrerollTarget(double target);')
    wrapper = extract((VP / 'DVDCodecs/Video/DVDVideoCodecAmlogic.cpp').read_text(),
                      'void CDVDVideoCodecAmlogic::SetPrerollTarget(')
    transport += '\n' + wrapper.replace('CDVDVideoCodecAmlogic::', 'FakeCodec::')
    marker='delivered.push_back(std::static_pointer_cast<CDVDMsgDemuxerPacket>(pMsg)->id);'
    assert transport.count(marker)==1
    transport=transport.replace(marker,marker+'++m_pVideoCodec->references;')
    includes = set()
    def scoped(name, text):
        def move(match):
            includes.add(match.group());return ''
        text=re.sub(r'^#include[^\n]+',move,text,flags=re.MULTILINE)
        return '\nnamespace '+name+' {\n'+text+'\n}\n'
    sections = scoped('Native',native)+scoped('BD',bd)+scoped('Transport',transport)
    globals_='\n'.join(sorted(includes))+'\n#undef DVD_TIME_BASE\n#undef DVD_NOPTS_VALUE\n#undef DVD_PLAYSPEED_NORMAL\n#undef DVD_PLAYSPEED_PAUSE\n'
    return globals_+sections+TESTS


TESTS=r'''
struct Connected {
  BD::Rig r;BD::Format format;Transport::CVideoPlayerVideo video;Transport::Parent parent{video};
  double start=BD::DVD_NOPTS_VALUE,titleTime=BD::DVD_NOPTS_VALUE,floor=0;
  Connected(){
    r.demux.m_pFormatContext=&format;
    r.bd->SetMarks({0,100,300,450});
    r.bd->marks[1].start=100*90000ULL+22860; //100.254s, keep source precision
    r.bd->marks[2].start=300*90000ULL+11250;r.bd->marks[2].clip_ref=1;
    r.bd->marks[3].start=450*90000ULL+45000;r.bd->marks[3].clip_ref=1;
    r.bd->clips={{0,4200*90000ULL,4500*90000ULL},{300*90000ULL,100*90000ULL,400*90000ULL}};
    r.bd->title.clip_count=2;r.bd->title.clips=r.bd->clips.data();
  }
  Native::CAMLCodec& Codec(){return video.m_pVideoCodec->native;}
  void Seek(int chapter,double expected){
    assert(r.demux.SeekChapter(chapter,&start,&titleTime) && "native_accepted_request");
    parent.m_offset_pts=50000000.;Transport::seek(parent,start);
    const auto rawTicks=static_cast<int64_t>(std::llround(expected/1000000.*90000.));
    floor=r.demux.ConvertTimestamp(rawTicks,90000,1)-parent.m_offset_pts;
  }
  void Input(double pts){Transport::packet(parent,pts,pts);video.Step();}
  bool Output(double pts,Native::CVideoPlayerVideo& output){
    Native::VideoPicture picture;Codec().incoming={{static_cast<uint64_t>(pts),7,false}};
    assert(Codec().GetPicture(picture)==Native::CDVDVideoCodec::VC_PICTURE);
    const int before=output.m_renderManager.published;output.Accept(picture);
    return output.m_renderManager.published>before;
  }
};
int main(){
  {
    Connected c;c.Seek(2,4300254000.);c.Input(c.floor-2000000.);
    Native::CVideoPlayerVideo output;
    const bool presented=c.Output(c.floor-2000000.,output);
    std::cout<<"returned="<<c.start<<" transported="<<c.Codec().m_prerollTarget
             <<" frame="<<c.floor-2000000.<<" presented="<<presented<<std::endl;
    assert(c.video.m_pVideoCodec->references==1 && "preroll_reference_input_reaches_decoder");
    assert(!presented && "native_chapter_preroll_not_presented");
    assert(c.Codec().m_prerollTarget==c.floor && "chapter_target_matches_decoded_domain");
    assert(c.titleTime==100254. && "native_title_time_preserves_fraction");
    // Later input crosses first; earlier delayed output must still be excluded.
    c.Input(c.floor+100000.);
    assert(!c.Output(c.floor-41708.,output) && "late_preroll_stays_excluded");
    assert(c.Output(c.floor,output) && "equal_target_admitted");
    assert(!c.Output(c.floor-1.,output) && "one_microsecond_early_stays_excluded");
    assert(c.Output(c.floor+41708.,output) && output.m_messageParent.starts==1);
    // Actual native check-in assigns double PTS into uint64 microseconds.
    c.Codec().SetPrerollTarget(c.floor+0.25);
    assert(c.Output(c.floor,output) && "equal_native_checkin_timestamp_is_eligible");
    c.Codec().SetPrerollTarget(c.floor);
    assert(!c.Output(Native::DVD_NOPTS_VALUE,output) && "unknown_output_cannot_satisfy_accurate_target");
    assert(c.Codec().m_prerollTarget==c.floor);
    // Paused access-unit reference output and reverse scanning retain policy.
    c.Codec().m_speed=0;assert(!c.Output(c.floor-2000000.,output));
    c.Codec().m_speed=-1000;assert(c.Output(c.floor-2000000.,output));
    c.Codec().m_speed=1000;
    // True flush clears the old native floor before installing the new clip.
    c.Seek(3,100125000.);assert(c.titleTime==300125. && "new_clip_title_time_independent_of_origin");assert(c.Codec().m_prerollTarget==Native::DVD_NOPTS_VALUE);
    c.Input(c.floor-1000000.);assert(!c.Output(c.floor-1000000.,output));
    assert(c.Output(c.floor,output) && "new_clip_origin_target");
    c.Seek(2,4300254000.);c.Input(c.floor-1000000.);
    assert(!c.Output(c.floor-1000000.,output) && "backward_chapter_replaces_target");
    // Local recovery/resync do not clear a durable seek floor.
    c.video.SendMessage(Transport::message(Transport::CDVDMsg::GENERAL_RESET));c.video.Step();
    assert(c.Codec().m_prerollTarget==c.floor);c.video.m_pVideoCodec->ready=true;c.video.Step();
    c.video.SendMessage(std::make_shared<Transport::CDVDMsgDouble>(Transport::CDVDMsg::GENERAL_RESYNC,c.floor));c.video.Step();
    assert(c.Codec().m_prerollTarget==c.floor);
    // Later continuity normalization changes packets, never the established floor.
    c.parent.realContinuity=true;c.parent.m_CurrentAudio.id=-1;
    c.parent.m_CurrentVideo.dts=c.parent.m_CurrentVideo.lastdts=c.floor-2000000.;
    c.Input(c.floor+200000000.);assert(c.Codec().m_prerollTarget==c.floor);
    assert(!c.Output(c.floor-1000000.,output));
  }
  // Identical title markers with different origins, including an origin drop.
  for(uint64_t origin:{0ULL,100ULL,4200ULL,8000ULL}){
    Connected c;c.r.bd->clips[0].in_time=origin*90000;c.r.bd->clips[0].out_time=(origin+300)*90000;
    c.Seek(2,(static_cast<double>(origin)+100.254)*1000000.);
    c.Input(c.floor-1000000.);Native::CVideoPlayerVideo output;
    assert(!c.Output(c.floor-1000000.,output));assert(c.Output(c.floor,output));
  }
  // Use the actual ConvertTimestamp transport rule; native AVC can normalize.
  {Connected c;c.r.demux.m_checkTransportStream=true;c.r.demux.m_startTime=4000;
   c.Seek(2,4300254000.);assert(c.start==c.r.demux.ConvertTimestamp(387022860,90000,1) && "transport_target_converted");c.Input(c.floor-1000000.);Native::CVideoPlayerVideo output;
   assert(!c.Output(c.floor-1000000.,output));assert(c.Output(c.floor,output));}
  // Native MVC uses the same raw source clock; preserve its separate seek calls.
  {Connected c;c.r.bd->m_bMVCPlayback=true;c.Seek(2,4300254000.);
   assert(c.r.bd->GetChapterPos(2)==100 && "ui_marker_stays_title_seconds");}
  // Non-native BD, exposed providers (DVD/addon style), and the default API
  // retain legacy chapter-position meaning; no raw target is invented.
  {Connected c;c.r.bd->m_navmode=false;double start=0;assert(c.r.demux.SeekChapter(2,&start));assert(start==100000000.);}
  {BD::CDVDDemuxFFmpeg d;d.m_pInput=std::make_shared<BD::Exposed>();double start=0;
   assert(d.SeekChapter(3,&start) && start==200000000. && "generic_provider_target_unchanged");}
  // Source invalidation after acceptance still flushes, without stale timing
  // or a new title's chapter-position fallback. Each test starts fresh.
  for(int change=0;change<5;++change){
    Connected c;
    c.r.bd->native.duringSeek=[&]{
      if(change==0)++c.r.bd->m_titleGeneration;
      if(change==1)std::atomic_store(&c.r.bd->m_session,std::make_shared<BD::Session>());
      if(change==2)c.r.bd->m_aborted=true;
      if(change==3)c.r.bd->m_navmode=false;
      if(change==4)c.r.demux.m_pInput=std::make_shared<BD::Exposed>();
    };
    double start=42,time=42;assert(c.r.demux.SeekChapter(2,&start,&time));
    assert(time==BD::DVD_NOPTS_VALUE && "replacement_has_no_stale_title_time");
    assert(start==BD::DVD_NOPTS_VALUE && c.r.demux.flushes==1 && "accepted_replacement_has_no_stale_target");
    Transport::seek(c.parent,start);c.Input(1000.);
    assert(c.Codec().m_prerollTarget==Native::DVD_NOPTS_VALUE);
  }
  // Bad metadata is rejected before navigation. Reused result objects must
  // not retain a preceding source target on failure.
  for(int bad=0;bad<6;++bad){
    Connected c;auto& b=*c.r.bd;
    if(bad==0)b.marks[1].clip_ref=99;
    if(bad==1)b.marks[1].start=0,b.clips[0].start_time=1;
    if(bad==2)b.clips[0].out_time=b.clips[0].in_time-1;
    if(bad==3)b.clips[0].in_time=UINT64_MAX-1,b.clips[0].out_time=UINT64_MAX;
    if(bad==4)b.title.clips=nullptr;
    if(bad==5)b.m_aborted=true;
    BD::CDVDInputStream::IChapter::SeekResult result;result.domain=decltype(result.domain)::SOURCE_90KHZ;result.timestamp=123;
    assert(!b.SeekChapterWithResult(2,result) && b.native.requests.empty() && "invalid_metadata_never_navigates");
    assert(result.domain==decltype(result.domain)::UNAVAILABLE && result.timestamp==0 && "failure_does_not_reuse_target");
  }
  // UO failure keeps its existing check, with no native request or target.
  {Connected c;c.r.bd->m_uoMask=BD::BLURAY_UO_CHAPTER_SEARCH;double start=19;
   assert(!c.r.demux.SeekChapter(2,&start) && c.r.bd->native.requests.empty());}
}
'''


def run(source,out,name,expected=None):
    out.mkdir(parents=True,exist_ok=True)
    cpp=out/(name+'.cpp');binary=out/name;cpp.write_text(source)
    command=[os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
             '-pthread','-no-pie','-fsanitize=address,undefined','-fno-omit-frame-pointer',
             '-I'+str(ROOT/'xbmc'),str(cpp),'-o',str(binary)]
    subprocess.run(command,check=True)
    result=subprocess.run([str(binary)],capture_output=True,text=True)
    (out/(name+'.log')).write_text(result.stdout+result.stderr)
    if expected:
        assert result.returncode==-6 and expected in result.stderr and 'Assertion' in result.stderr,result.stderr
        assert not any(s in result.stderr for s in ['Sanitizer','runtime error:']),result.stderr
    else:
        assert result.returncode==0,result.stderr
    print(result.stdout,end='');print(name+': PASS'+(' (intended rejection)' if expected else ''),flush=True)
    return {'name':name,'returncode':result.returncode,'expected_assertion':expected}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expect-original-failure',action='store_true')
    parser.add_argument('--expect-unknown-failure',action='store_true')
    parser.add_argument('--expect-fraction-failure',action='store_true')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--original-demux',type=Path)
    parser.add_argument('--original-output',type=Path)
    parser.add_argument('--negative-controls',action='store_true')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='bd-preroll-') as temp:
        out=args.output or Path(temp)
        source=harness(args.original_demux,args.original_output)
        results=[run(source,out,'bd-preroll','native_chapter_preroll_not_presented' if args.expect_original_failure else 'unknown_output_cannot_satisfy_accurate_target' if args.expect_unknown_failure else 'equal_native_checkin_timestamp_is_eligible' if args.expect_fraction_failure else None)]
        if args.negative_controls:
            controls=[
              ('legacy-native-target','result.domain = CDVDInputStream::IChapter::SeekResult::Domain::SOURCE_90KHZ;','result.domain = CDVDInputStream::IChapter::SeekResult::Domain::CHAPTER_POSITION;','native_chapter_preroll_not_presented'),
              ('wrong-clip-origin','m_titleInfo->clips[chapter.clip_ref]','m_titleInfo->clips[0]','native_accepted_request'),
              ('truncate-authored-mark','timestamp = static_cast<int64_t>(clip.in_time + relative);','timestamp = static_cast<int64_t>((clip.in_time + relative)/90000*90000);','chapter_target_matches_decoded_domain'),
              ('bypass-demux-conversion','*startpts = ConvertTimestamp(target.timestamp, 90000, 1);','*startpts = DVD_SEC_TO_TIME(target.timestamp/90000.);','transport_target_converted'),
              ('stale-title-generation','m_titleGeneration == generation','(static_cast<void>(generation), true)','replacement_has_no_stale_title_time'),
              ('stale-session','std::atomic_load(&m_session) == session','true','replacement_has_no_stale_title_time'),
              ('ignore-retired-source','!m_aborted && m_bd == native','m_bd == native','replacement_has_no_stale_title_time'),
              ('stale-demux-input','if (m_pInput != input)','if (false)','replacement_has_no_stale_title_time'),
              ('borrow-replacement-timing','case CDVDInputStream::IChapter::SeekResult::Domain::UNAVAILABLE:\n          *startpts = DVD_NOPTS_VALUE;','case CDVDInputStream::IChapter::SeekResult::Domain::UNAVAILABLE:\n          *startpts = DVD_SEC_TO_TIME(static_cast<double>(ich->GetChapterPos(chapter)));','accepted_replacement_has_no_stale_target'),
              ('admit-unknown-output','(videoPicture.pts == DVD_NOPTS_VALUE || videoPicture.pts < std::trunc(m_prerollTarget))','(videoPicture.pts != DVD_NOPTS_VALUE && videoPicture.pts < std::trunc(m_prerollTarget))','unknown_output_cannot_satisfy_accurate_target'),
              ('fractional-native-mismatch','videoPicture.pts < std::trunc(m_prerollTarget)','videoPicture.pts < m_prerollTarget','equal_native_checkin_timestamp_is_eligible'),
              ('clear-floor-after-eligible','      VideoHoldDecoded();','      VideoHoldDecoded(); m_prerollTarget = DVD_NOPTS_VALUE;','one_microsecond_early_stays_excluded'),
              ('reverse-inherits-floor','m_speed >= DVD_PLAYSPEED_PAUSE &&','m_speed >= DVD_PLAYSPEED_PAUSE-1000 &&','c.Output(c.floor-2000000.,output)'),
              ('drop-reference-input','  m_VideoPlayerVideo->SendMessage(\n      std::make_shared<CDVDMsgDemuxerPacket>(pPacket, drop, m_videoPrerollTarget));','  if (drop) CDVDDemuxUtils::FreeDemuxPacket(pPacket); else m_VideoPlayerVideo->SendMessage(\n      std::make_shared<CDVDMsgDemuxerPacket>(pPacket, drop, m_videoPrerollTarget));','preroll_reference_input_reaches_decoder'),
            ]
            for name,old,new,expected in controls:
                assert source.count(old)==1,(name,source.count(old))
                results.append(run(source.replace(old,new,1),out,name,expected))
        (out/'results.json').write_text(json.dumps(results,indent=2)+'\n')


if __name__=='__main__':main()
