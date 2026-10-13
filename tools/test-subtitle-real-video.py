#!/usr/bin/env python3
"""Run the complete production detector on the supplied 9 MKV and matching SRT.

Every production seek/retry is decoded using linked host FFmpeg through production
custom-AVIO callbacks. CFile uses local stdio; playback cache, sysfs and GL are host
boundaries. Production measurement, consensus, publication/getter, overlay geometry,
libass rendering and glyph atlas/vertices execute under ASan/UBSan. The UTF-8 SRT
bootstrap reproduces the supplied file's timing, newline and italic conversion;
Kodi VFS/parser/font bootstrap and device acceptance remain unverified. No generated
pixels or subtitles participate. --baseline selects the detector/scan revision;
unchanged publication and rendering helpers remain current. It must reproduce
the failure. Supplemental observations never fill the primary consensus.
Requires ABI-matching host FFmpeg headers/libraries and a CE libass host archive.
"""
from pathlib import Path
import runpy,re,subprocess,ctypes.util,json,argparse,hashlib,os
ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('video',type=Path)
parser.add_argument('srt',type=Path)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--ffmpeg-include',type=Path,required=True)
parser.add_argument('--ffmpeg-library-dir',type=Path,help='Use matching static libavformat/libavcodec/libavutil archives from this directory')
parser.add_argument('--extra-library',action='append',default=[],help='Additional library name required by static FFmpeg, e.g. z or bz2')
parser.add_argument('--ass-include',type=Path,required=True)
parser.add_argument('--ass-library',required=True)
parser.add_argument('--font',type=Path,default=Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
parser.add_argument('--supplemental-scan',action='store_true',help='Observe separate real-film positions after acceptance; never fills primary consensus')
parser.add_argument('--supplemental-percentages',type=int,nargs=5,help='Five separately frozen validation positions; implies --supplemental-scan')
parser.add_argument('--baseline',help='Pre-change revision; must reproduce missing publication and out-of-picture text')
args=parser.parse_args();OUT=args.output.resolve();OUT.mkdir(parents=True,exist_ok=True)
if args.supplemental_percentages:
    assert len(set(args.supplemental_percentages))==5 and all(0<p<95 for p in args.supplemental_percentages)
    args.supplemental_scan=True
assert args.video.is_file() and args.srt.is_file()
raw=args.srt.read_bytes();text=raw.decode('utf-8-sig').replace('\r\n','\n')
assert args.video.stat().st_size==20037981114, 'Expected the supplied actual MKV'
assert hashlib.sha256(raw).hexdigest()=='b6d8668b216c4b46f01409f1c2bc399eb09b08be90db5d463378c342ae3e94e6', 'Expected the supplied matching SRT'
cues=re.findall(r'(\d+)\s*\n(\d\d:\d\d:\d\d,\d\d\d) --> (\d\d:\d\d:\d\d,\d\d\d)\s*\n(.*?)(?=\n\s*\n|\Z)',text,re.S)
assert len(cues)==373, 'Expected the supplied matching SRT'
def millis(value):
    h,m,s,ms=map(int,re.split('[:,]',value));return ((h*60+m)*60+s)*1000+ms
converted=[]
for number,start,end,text in cues:
    assert set(re.findall(r'<[^>]*>',text))<= {'<i>','</i>'}, 'Unsupported subtitle bootstrap tag'
    text=text.strip().replace('<i>',r'{\i1}').replace('</i>',r'{\i0}').replace('\n',r'\N')
    converted.append((int(number),millis(start),millis(end),text))
assert converted[0][1]==55930 and converted[-1][2]==4322066
assert all(start<end for _,start,end,_ in converted)
(OUT/'identity.json').write_text(json.dumps({'stat':{'size':args.video.stat().st_size,'mtime_ns':args.video.stat().st_mtime_ns},'srt_sha256':hashlib.sha256(raw).hexdigest(),'cue_count':len(cues),'multiline_cues':sum('\n' in cue[3] for cue in cues),'first_ms':converted[0][1],'last_ms':converted[-1][2],'ffprobe':json.loads(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration,size:stream=index,codec_name,codec_type,width,height,pix_fmt,time_base','-of','json',str(args.video)]))},indent=2))
T=runpy.run_path(str(ROOT/'tools/test-subtitle-text-integration.py'));F=T['FUNCTION']
a=T['source']('xbmc/utils/AMLUtils.cpp',args.baseline)
c=T['generate']().replace('int main(int argc,char** argv)','void retainedMain(int argc,char** argv)',1)
assert all(fragment in c for fragment in ['std::vector<uint8_t> submit(', ' std::vector<uint8_t> result(1920*1080,0);', '    if(x>=0&&x<1920&&y>=0&&y<1080)', 'return {overlay,owned,pixelBounds(pixels,1920,1080),std::move(pixels)};']), 'Glyph fixture changed; update the uncut-alpha instrumentation'
c=c.replace('std::vector<uint8_t> submit(', 'Bounds uncutBounds; size_t outsideAlpha=0;\nstd::vector<uint8_t> submit(',1)
c=c.replace(' std::vector<uint8_t> result(1920*1080,0);', ' uncutBounds=Bounds{}; outsideAlpha=0;\n std::vector<uint8_t> result(1920*1080,0);',1)
c=c.replace('    if(x>=0&&x<1920&&y>=0&&y<1080)', '    if(alpha){uncutBounds.left=std::min(uncutBounds.left,x);uncutBounds.top=std::min(uncutBounds.top,y);uncutBounds.right=std::max(uncutBounds.right,x+1);uncutBounds.bottom=std::max(uncutBounds.bottom,y+1);outsideAlpha+=(x<0||x>=1920||y<0||y>=1080); }\n    if(x>=0&&x<1920&&y>=0&&y<1080)',1)
c=c.replace('return {overlay,owned,pixelBounds(pixels,1920,1080),std::move(pixels)};', 'return {overlay,owned,uncutBounds,std::move(pixels)};')
stub=F(c,'void DetectActiveAreaFromFile(const std::shared_ptr<DetectSource>& value,')
c=c.replace(stub,'')
c=c.replace('void DetectActiveAreaFromFile(const std::shared_ptr<DetectSource>&,','static void DetectActiveAreaFromFile(const std::shared_ptr<DetectSource>&,')
c=c.replace('struct CLog{template<class... T> static void Log(T...){}template<class... T>static void LogF(T...){} };',r'''struct CLog{template<class... T> static void Log(int,const char* format,T... args){std::cout<<format;((std::cout<<"\t"<<args),...);std::cout<<std::endl;}template<class... T>static void LogF(T...){} };''')
c=c.replace('struct StringUtils{','struct StringUtils{static bool StartsWithNoCase(const std::string& s,const char* p){return s.rfind(p,0)==0;}')
c=c.replace('struct CServiceBroker {','struct CApplicationPlayer{bool IsPlaying(){return false;}int GetCacheLevel(){return 100;}}; struct AppComponents{template<class T> std::shared_ptr<T> GetComponent(){return {};}};\nstruct CServiceBroker {static AppComponents& GetAppComponents(){static AppComponents v;return v;}')
if 'SETTING_COREELEC_AMLOGIC_DV_LEVEL5=4,' in c:
    c=c.replace('SETTING_COREELEC_AMLOGIC_DV_LEVEL5=4,', 'SETTING_COREELEC_AMLOGIC_DV_LEVEL5=4,SETTING_COREELEC_AMLOGIC_DV_DETECT_THROTTLE=5,',1)
else:
    c=c.replace('SETTING_COREELEC_AMLOGIC_DV_LEVEL5=4;', 'SETTING_COREELEC_AMLOGIC_DV_LEVEL5=4,SETTING_COREELEC_AMLOGIC_DV_DETECT_THROTTLE=5;',1)
prefix=r'''
extern "C" {
#include <libavformat/avformat.h>
#include <libavcodec/avcodec.h>
#include <libavutil/frame.h>
}
#include <fstream>
namespace XFILE {
constexpr int READ_NO_CACHE=1;
struct CFile {FILE* fp=nullptr;~CFile(){if(fp)fclose(fp);}bool Open(const std::string& path,int){fp=fopen(path.c_str(),"rb");return fp;}
 int Read(uint8_t* dst,int n){return fread(dst,1,n,fp);}int64_t Seek(int64_t n,int w){return fseeko(fp,n,w)?-1:ftello(fp);}
 int64_t GetLength(){auto cur=ftello(fp);fseeko(fp,0,SEEK_END);auto n=ftello(fp);fseeko(fp,cur,SEEK_SET);return n;}
};}
static std::atomic<int64_t> s_detectLastCacheCheckMs{0};
static constexpr int kDetectCacheCriticalPct=40,kDetectCacheTargetPct=80,kDetectCacheFloorPct=50;
void observe(AVFrame* frame,int percent){
 std::cout<<"FRAME percent="<<percent<<" pts="<<frame->best_effort_timestamp<<" width="<<frame->width<<" height="<<frame->height<<" format="<<frame->format<<" stride="<<frame->linesize[0]<<std::endl;
 assert(frame->format==AV_PIX_FMT_YUV420P);std::string name="/tmp/nine-real-video-20261011/frame-"+std::to_string(percent)+"-"+std::to_string(frame->best_effort_timestamp)+".pgm";
 std::ofstream out(name,std::ios::binary);out<<"P5\n"<<frame->width<<" "<<frame->height<<"\n255\n";for(int y=0;y<frame->height;++y)out.write(reinterpret_cast<const char*>(frame->data[0]+y*frame->linesize[0]),frame->width);
}
'''
prefix=prefix.replace('/tmp/nine-real-video-20261011',str(OUT))
c+=prefix
for sig in ['static int64_t detect_steady_ms()','static int detect_interrupt_cb(','static int detect_avio_read(','static int64_t detect_avio_seek(','static bool detect_throttle_enabled()','static int detect_wait_for_cache(']:c+='\n'+F(a,sig)
c+='\n'+a[a.index('enum class DetectEdgeReason'):a.index('static void DetectActiveAreaFromFile(')]
c+='\n'+re.search(r'static const uint32_t s_commonAR\[\] = \{.*?\};',a,re.S).group()
scan=F(a,'static void DetectActiveAreaFromFile(')
scan=scan.replace('      /* Re-derive frame accessors','      observe(frame, measuredSeekPct);\n      /* Re-derive frame accessors')
scan=scan.replace('      samples_top[validSamples] = sTop;', '      for(int e=0;e<4;++e){const auto& m=subtitleMeasurements[e];std::cout<<"SUBEDGE "<<measuredSeekPct<<" "<<e<<" value="<<m.value<<" reason="<<detect_edge_reason(m.reason)<<" lane="<<m.lane<<" depth="<<m.depth<<" return="<<m.returnLevel<<" earliest="<<m.earliest<<" support="<<m.support<<" lanes="<<detect_edge_lanes(m)<<std::endl;}\n      samples_top[validSamples] = sTop;')
c+='\n'+scan
if args.supplemental_scan:
    independent='{13, 37, 58, 73, 86}' if 'const int numSeeks = 14;' in scan else '{2, 8, 22, 38, 52, 67, 90}'
    if args.supplemental_percentages:
        independent='{'+', '.join(map(str,args.supplemental_percentages))+'}'
    supplemental,n=re.subn(r'(const int seekPercents\[\] = )\{[^}]+\}',lambda m:m[1]+independent,scan,count=1)
    assert n==1, 'Cannot locate actual independent scan positions'
    count=len(args.supplemental_percentages) if args.supplemental_percentages else (5 if 'const int numSeeks = 14;' in scan else 7)
    supplemental,n=re.subn(r'const int numSeeks = (14|7);',f'const int numSeeks = {count};',supplemental,count=1)
    assert n==1, 'Cannot locate supplemental observation count'
    supplemental=supplemental.replace('DetectActiveAreaFromFile(', 'ObserveAdditionalFilmFrames(',1)
    c+='\n'+supplemental
render=r'''
struct FilmCue{int number;int64_t start,end;const char* text;};
static const FilmCue filmCues[]={@CUES@};
void renderFilm(const char* font,bool got){
#ifdef EXPECT_FAILURE
 assert(!got);
#else
 assert(got);assert(s_detectSource->subtitleResult&&s_detectSource->subtitleResult->top==20&&s_detectSource->subtitleResult->bottom==20);
#endif
 CRenderManager manager;manager.m_picture.iWidth=1920;manager.m_picture.iHeight=1080;manager.m_overlays.m_rs={0,0,1920,1080};int outside=0,drawn=0;
 for(const auto& event:filmCues){size_t completePixels=0;auto c=cue(font,event.text);auto* assEvent=&c->handler->m_track->events[0];assEvent->Start=event.start;assEvent->Duration=event.end-event.start;
  for(bool top:{false,true}){manager.m_overlays.m_subtitleAlign=top?SUBTITLES::Align::TOP_INSIDE:SUBTITLES::Align::BOTTOM_INSIDE;
   auto st=makeStyle(top,0);auto d=draw(manager,c,st,true,true,(event.start+event.end)*500.0);auto cached=draw(manager,c,st,true,false,(event.start+event.end)*500.0);assert(cached.canvas==d.canvas);
   const auto n=visibleGlyphPixels(*d.owned);if(!top)completePixels=n;else assert(n==completePixels);assert(outsideAlpha==0);
   bool in=d.bounds.top>=20&&d.bounds.bottom<=1060;outside+=!in;++drawn;
   std::cout<<"RENDER cue="<<event.number<<" pts_ms="<<(event.start+event.end)/2<<" top_align="<<top<<" bounds="<<d.bounds.left<<","<<d.bounds.top<<","<<d.bounds.right<<","<<d.bounds.bottom<<" alpha_pixels="<<n<<" outside_alpha="<<outsideAlpha<<" active="<<manager.m_overlays.m_activePicture.y1<<","<<manager.m_overlays.m_activePicture.y2<<" inside="<<in<<std::endl;
#ifndef EXPECT_FAILURE
   assert(in);rect(manager.m_overlays.m_activePicture,20,1060);
#endif
   if(!top&&(event.number==1||event.number==100||event.number==223||event.number==330)){
    std::ofstream out(std::string("@OUTPUT@/cue-")+std::to_string(event.number)+".pgm",std::ios::binary);out<<"P5\n1920 1080\n255\n";out.write(reinterpret_cast<const char*>(d.canvas.data()),d.canvas.size());
   }
  }
 }
 std::cout<<"RENDER_SUMMARY drawn="<<drawn<<" outside="<<outside<<std::endl;
#ifdef EXPECT_FAILURE
 assert(outside>0);
#else
 assert(outside==0);
#endif
}
'''
render=render.replace('@CUES@',','.join('{'+str(n)+','+str(start)+','+str(end)+','+json.dumps(text,ensure_ascii=False)+'}' for n,start,end,text in converted)).replace('@OUTPUT@',str(OUT))
c+=render
c+=r'''
void printResult(const char* label,const std::shared_ptr<const DetectResult>& r){std::cout<<label<<" ";if(r)std::cout<<r->top<<","<<r->bottom<<","<<r->left<<","<<r->right;else std::cout<<"none";std::cout<<std::endl;}
int main(int argc,char** argv){assert(argc==3);ready();aml_dv_detect_set_file(argv[1]);CVideoPlayerVideo owner;owner.m_hints.subtitleProbeSource=aml_subtitle_active_area_source();owner.m_hints.width=1920;owner.m_hints.height=1080;owner.m_pVideoCodec=std::make_unique<CDVDVideoCodecFFmpeg>();VideoPicture picture{1920,1080};owner.UpdateSubtitleProbe(picture);
 auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(120);while(s_detectState==DV_DETECT_RUNNING){assert(std::chrono::steady_clock::now()<deadline);std::this_thread::sleep_for(std::chrono::milliseconds(1));}s_detectWorker.Stop();
 CAMLNativeWorker::Run run(s_detectSource->superseded);
 printResult("STRICT",s_detectSource->result);printResult("SUBTITLE",s_detectSource->subtitleResult);uint16_t t,b,l,r;bool got=aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r,true);std::cout<<"CONSUMED "<<got<<" "<<t<<","<<b<<","<<l<<","<<r<<" state="<<s_detectState<<std::endl;
 assert(writes==0);renderFilm(argv[2],got);s_detectWorker.Stop();}
'''
if args.supplemental_scan:
    c=c.replace('renderFilm(argv[2],got);s_detectWorker.Stop();',
                'renderFilm(argv[2],got);std::cout<<"SUPPLEMENTAL_SCAN separate observations after primary acceptance"<<std::endl;ObserveAdditionalFilmFrames(s_detectSource,run);s_detectWorker.Stop();')
if args.baseline:c='#define EXPECT_FAILURE 1\n'+c
(OUT/'repro.cpp').write_text(c)
flags=['g++','-O1','-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie','-std=c++17','-Wall','-Wextra','-Werror','-Wno-misleading-indentation','-Wno-deprecated-declarations','-Wno-unused-variable','-pthread','-I',str(args.ffmpeg_include),'-I',str(args.ass_include),'-I',str(ROOT/'xbmc'),'-I',str(ROOT/T['VIDEO']/'VideoRenderers')]
def libraries(names):
    result=[]
    for name in names:
        library=ctypes.util.find_library(name)
        assert library, 'Missing host library: '+name
        result.append('-l:'+library)
    return result
ffmpeg=[str(args.ffmpeg_library_dir/('lib'+name+'.a')) for name in ['avformat','avcodec','avutil']] if args.ffmpeg_library_dir else libraries(['avformat','avcodec','avutil'])
subprocess.run(flags+[str(OUT/'repro.cpp'),str(ROOT/T['VIDEO']/'VideoRenderers/BitmapSubtitlePosition.cpp'),args.ass_library,*libraries(['freetype','harfbuzz','fribidi']),*ffmpeg,*libraries(args.extra_library),'-lm','-o',str(OUT/'repro')],check=True)

with (OUT/'result.log').open('w') as log:
    subprocess.run([str(OUT/'repro'),str(args.video),str(args.font)],stdout=log,stderr=subprocess.STDOUT,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'},timeout=180,check=True)
print('PASS:', 'actual-film pre-change failure reproduced' if args.baseline else 'actual-film scan and all supplied cues inside picture', OUT/'result.log')
