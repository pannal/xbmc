#!/usr/bin/env python3
"""Execute connected production subtitle detection, geometry, style and rasterization.

FFmpeg/VFS decoding, saved-setting access, font/track bootstrap, GL upload/draw and
sysfs are boundaries. Actual libass rasterization, glyph atlas packing/vertices,
owned result, detector publication/getter, active geometry and conversion execute.
Use the CE libass host archive with --ass-include/--ass-library; a host font is
required. This does not establish device GPU/HDMI or reporter-file acceptance.
"""
import argparse
import ctypes.util
import os
import re
from pathlib import Path
import runpy
import shutil
import signal
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FUNCTION = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
VIDEO = 'xbmc/cores/VideoPlayer/'
LIBASS = VIDEO + 'DVDSubtitles/DVDSubtitlesLibass.cpp'
RENDERER = VIDEO + 'VideoRenderers/OverlayRenderer.cpp'
RENDERHEADER = VIDEO + 'VideoRenderers/OverlayRenderer.h'
GLES = VIDEO + 'VideoRenderers/OverlayRendererGLES.cpp'
GLESHEADER = VIDEO + 'VideoRenderers/OverlayRendererGLES.h'
AML = 'xbmc/utils/AMLUtils.cpp'
RESULTHEADER = VIDEO + 'DVDSubtitles/DVDSubtitlesLibassRenderResult.h'


def source(path, baseline=None):
    if baseline:
        return subprocess.check_output(['git', 'show', baseline + ':' + path], cwd=ROOT, text=True)
    return (ROOT / path).read_text()


def generate(baseline=None):
    # Numeric states cross the persistent-cache and report boundaries too.
    actual = {name: int(value) for name, value in re.findall(
        r'(DV_DETECT_\w+)\s*=\s*(\d+)', source('xbmc/utils/AMLUtils.h', baseline))}
    fixture = {name: int(value) for name, value in re.findall(
        r'(DV_DETECT_\w+)\s*=\s*(\d+)', PREFIX)}
    assert fixture == actual, (fixture, actual)
    aml, libass, renderer = (source(path, baseline) for path in (AML, LIBASS, RENDERER))
    header, gles, gles_header = (source(path, baseline) for path in (RENDERHEADER, GLES, GLESHEADER))
    fields = FUNCTION(header, 'class COverlay\n')
    fields = fields[fields.index('    enum EType'):fields.index('  protected:')]
    code = (('' if baseline else '#define FIXTURE_CURRENT 1\n') + PREFIX).replace('@STATE@', FUNCTION(header, 'struct SRenderState') + ';')
    code = code.replace('@FIELDS@', fields).replace('@GEOMETRY@', FUNCTION(header, 'struct SRenderGeometry') + ';')
    code = code.replace('@VERTEX@', FUNCTION(gles_header, 'struct VERTEX') + ';')
    code = code.replace('@PAGE@', FUNCTION(gles_header, 'struct Page') + ';')
    code += '\nvoid aml_subtitle_active_area_invalidate(' + ('' if baseline else 'bool preserveGeometry=false') + ');\n'
    if 'bool allowSubtitleEstimate' in aml:
        declaration = source('xbmc/utils/AMLUtils.h', baseline).split('bool aml_subtitle_detect_active_area_get(', 1)[1].split(';', 1)[0]
        code += '\nbool aml_subtitle_detect_active_area_get(' + declaration + ';\n'
    code += '\n' + aml[aml.index('static CAMLNativeWorker s_detectWorker;'):aml.index('/* Mid-read cache guard.')]
    for sig in ['static void detect_publish(', 'void aml_dv_detect_set_file(', 'static void detect_clear_injection(',
                'void aml_dv_detect_active_area_start()', 'void aml_dv_detect_active_area_stop()']:
        code += '\n' + FUNCTION(aml, sig)
        if sig == 'static void detect_publish(' and 'static void detect_publish_subtitle(' in aml:
            code += '\n' + FUNCTION(aml, 'static void detect_publish_subtitle(')
    code += '\nvoid Invalidate(bool keep=false){aml_subtitle_active_area_invalidate(' + ('' if baseline else 'keep') + ');' + ('(void)keep;' if baseline else '') + '}\n'
    for sig in ['bool RenderOptsEqual(', 'CLibassRenderResult::CLibassRenderResult(',
                'std::shared_ptr<const CLibassRenderResult> CDVDSubtitlesLibass::RenderImage(',
                'bool CDVDSubtitlesLibass::IsDynamicEvent(', 'void CDVDSubtitlesLibass::UpdateRenderCache(',
                'void CDVDSubtitlesLibass::InvalidateRenderCache(', 'void CDVDSubtitlesLibass::ApplyStyle(',
                'void CDVDSubtitlesLibass::ConfigureAssOverride(', 'int CDVDSubtitlesLibass::GetPlayResY()']:
        if sig.startswith('CLibass') and 'int ActiveAreaTextOffset(' in libass:
            code += '\n' + FUNCTION(libass, 'int ActiveAreaTextOffset(')
        code += '\n' + FUNCTION(libass, sig)
    code += '\nCLibassRenderResult::~CLibassRenderResult() = default;\n'
    util = source(VIDEO + 'VideoRenderers/OverlayRendererUtil.cpp', baseline)
    code += '\nnamespace OVERLAY {\n' + FUNCTION(util, 'bool convert_quads(') + '\n}\n'
    for sig in ['COverlayGlyphGLES::COverlayGlyphGLES(', 'COverlayGlyphGLES::~COverlayGlyphGLES()',
                'bool COverlayGlyphGLES::IsValid() const', 'std::shared_ptr<COverlay> COverlay::Create(ASS_Image*']:
        code += '\n' + FUNCTION(gles, sig)
    for sig in ['COverlay::COverlay()', 'std::shared_ptr<COverlay> COverlay::Create(const CLibassRenderResult&',
                'void CRenderer::SetActivePicture(', 'void CRenderer::SetActiveAreaOffsets(',
                'CRenderer::SRenderGeometry CRenderer::PrepareRenderGeometry(',
                'SRenderState CRenderer::CalculateRenderState(', 'std::shared_ptr<COverlay> CRenderer::ConvertLibass(']:
        code += '\n' + FUNCTION(renderer, sig)
    manager = source(VIDEO + 'VideoRenderers/RenderManager.cpp', baseline)
    if 'CRect CRenderManager::CalcPictureActiveArea(' in manager:
        code += '\n' + FUNCTION(manager, 'CRect CRenderManager::CalcPictureActiveArea(')
    code += '\n' + FUNCTION(manager, 'CRect CRenderManager::CalcOverlayActiveArea(')
    if not baseline:
        # Real call-site contracts; execution below covers the called policies.
        player = source(VIDEO + 'VideoPlayer.cpp')
        codec = source(VIDEO + 'DVDCodecs/Video/AMLCodec.cpp')
        assert 'aml_subtitle_active_area_invalidate(preserveSubtitleGeometry);' in FUNCTION(player, 'void CVideoPlayer::FlushBuffers(')
        assert 'deferred.preserveSubtitleGeometry' in player
        assert 'bool preserveSubtitleGeometry = false' in source(VIDEO + 'VideoPlayer.h')
        assert 'aml_subtitle_active_area_invalidate(operation == Lifecycle::RESET);' in FUNCTION(codec, 'bool CAMLCodec::BeginLifecycle(')
    if not baseline:
        owner = source(VIDEO + 'VideoPlayerVideo.cpp')
        output = FUNCTION(owner, 'CVideoPlayerVideo::EOutputState CVideoPlayerVideo::OutputPicture(')
        assert output.index('m_renderManager.Configure(') < output.index('UpdateSubtitleProbe(*pPicture);')
        assert 'StopSubtitleProbe();' in FUNCTION(owner, 'void CVideoPlayerVideo::CloseStream(')
        code += SOFTWARE_ADAPTERS
        for signature in ('void CVideoPlayerVideo::UpdateSubtitleProbe(', 'void CVideoPlayerVideo::ResetSubtitleProbe()', 'void CVideoPlayerVideo::StopSubtitleProbe()'):
            code += '\n' + FUNCTION(owner, signature)
    code += '\nvoid DetectActiveAreaFromFile(const std::shared_ptr<DetectSource>& value,const CAMLNativeWorker::Run& run){++scans;const uint16_t border=value->width==1920?132:263;detect_publish(run,value,border,border,0,0);}\n'
    return code + TESTS


PREFIX = r'''
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <iostream>
#include <limits>
#include <map>
#include <map>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <ass/ass.h>
#include "utils/AMLNativeWorker.h"
#include "utils/Geometry.h"
#include "cores/VideoPlayer/DVDSubtitles/DVDSubtitlesLibassRenderResult.h"
#include "cores/VideoPlayer/DVDSubtitles/SubtitlesStyle.h"
#include "cores/VideoPlayer/VideoRenderers/BitmapSubtitlePosition.h"
#include "cores/VideoPlayer/VideoRenderers/OverlayRendererUtil.h"
using namespace KODI;
using namespace KODI::SUBTITLES::STYLE;
using namespace UTILS;
using CCriticalSection=std::recursive_mutex;
#define DVD_TIME_TO_MSEC(x) ((x)/1000)
constexpr int LOGERROR=1,LOGDEBUG=2,LOGWARNING=3,LOGINFO=4,ASS_NO_ID=-1,NATIVE=1,ADAPTED=0;
constexpr int ASS_BORDER_STYLE_OUTLINE=1,ASS_BORDER_STYLE_BOX=3,ASS_BORDER_STYLE_SQUARE_BOX=4;
namespace KODI::SUBTITLES {
constexpr const char* FONT_DEFAULT_FAMILYNAME="DEFAULT";
enum class Align{MANUAL,BOTTOM_INSIDE,BOTTOM_OUTSIDE,TOP_INSIDE,TOP_OUTSIDE};
enum class HorizontalAlign{LEFT,CENTER,RIGHT};
}
struct CLog{template<class... T> static void Log(T...){}template<class... T>static void LogF(T...){} };
struct StringUtils{static void ToLower(std::string& s){for(char& c:s)c=static_cast<char>(std::tolower(static_cast<unsigned char>(c)));}};
COLOR::Color ConvColor(COLOR::Color c,int opacity=100){return (c<<8)|static_cast<unsigned int>(255*(100-opacity)/100);}
struct Metadata {
 bool has_level5_metadata=false,level5_detected=false;
 uint16_t level5_active_area_top_offset=0,level5_active_area_bottom_offset=0,
          level5_active_area_left_offset=0,level5_active_area_right_offset=0;
};
struct Cache {
 Metadata meta;double pts=-1;int clears=0;
 Metadata GetVideoDoViFrameMetadata(){return meta;}
 Metadata GetVideoDoViFrameMetadata(double requested){return requested==pts?meta:Metadata{};}
 void ClearVideoDoViFrameMetadata(){meta={};pts=-1;++clears;}
} cache;
struct CSettings{static constexpr int SETTING_SUBTITLES_DETECTACTIVEAREA=1,
 SETTING_COREELEC_AMLOGIC_DV_RESTRICT_SUBS_USER_POS=2,SETTING_SUBTITLES_BITMAPZOOM=3,
 SETTING_COREELEC_AMLOGIC_DV_LEVEL5=4;};
struct Settings{
 bool detect=true,applyMargin=false;int zoom=100;
 bool GetBool(int id){return id==1?detect:applyMargin;}int GetInt(int){return zoom;}
} settingsValue;
Settings* settings(){return &settingsValue;}
struct RESOLUTION_INFO{int iSubtitles=1080,iHeight=1080;float fPixelRatio=1;struct{int top=0;}Overscan;};
struct Window {
 RESOLUTION_INFO info;int stereo=0;float textMargin=1;
 Window& GetGfxContext(){return *this;}RESOLUTION_INFO GetResInfo(){return info;}
 int GetStereoMode(){return stereo;}int GetVideoResolution(){return 0;}
 void SetResInfo(int,const RESOLUTION_INFO& value){info=value;}
} window;
struct SettingsComponent{Settings*GetSettings(){return &settingsValue;}};
using GLint=int;using GLuint=unsigned int;using GLfloat=float;using GLubyte=unsigned char;
constexpr int GL_MAX_TEXTURE_SIZE=1,GL_TEXTURE_2D=2;
struct Atlas {int w=0,h=0;std::vector<uint8_t> bytes;};
std::map<GLuint,Atlas> textures;GLuint nextTexture=1,boundTexture=0;
void glGetIntegerv(int,int* p){*p=2048;}
void glGenTextures(int,GLuint* p){*p=nextTexture++;}
void glBindTexture(int,GLuint value){boundTexture=value;}
void LoadTexture(int,int w,int h,int stride,float* u,float* v,bool,const void* pixels){
 assert(boundTexture&&w>0&&h>0);*u=*v=1;auto& atlas=textures[boundTexture];atlas.w=w;atlas.h=h;
 auto* bytes=static_cast<const uint8_t*>(pixels);for(int row=0;row<h;++row)atlas.bytes.insert(atlas.bytes.end(),bytes+row*stride,bytes+row*stride+w);
}
struct CGLESTextureResources{void Register(GLuint){}void Retire(GLuint value){textures.erase(value);}};
struct RenderSystem{virtual ~RenderSystem()=default;};
struct CRenderSystemGLES:RenderSystem {
 std::shared_ptr<CGLESTextureResources> resources=std::make_shared<CGLESTextureResources>();
 bool CanRender(){return true;}int CaptureRenderTarget(){return 1;}
 bool IsRenderTargetCurrent(int value){return value==1;}
 auto GetTextureResources(){return resources;}
 bool IsTextureContextCurrent(const std::shared_ptr<CGLESTextureResources>& value){return value==resources;}
} renderSystem;
struct CServiceBroker {
 static Cache&GetDataCacheCore(){return cache;}static Window*GetWinSystem(){return &window;}
 static SettingsComponent*GetSettingsComponent(){static SettingsComponent value;return &value;}
 static RenderSystem*GetRenderSystem(){return &renderSystem;}
};
constexpr int RENDER_STEREO_MODE_OFF=0,DV_DETECT_FAILED=0,DV_DETECT_RUNNING=6,DV_DETECT_SKIPPED=4,
 DV_DETECT_OK=3,DV_DETECT_SKIP_NON16X9=1,DV_DETECT_SKIP_IMAX=2,DV_DETECT_INACTIVE=5;
std::atomic<bool> s_detectStable{false},s_detectThrottleActive{false},s_detectCacheStarved{false};
std::atomic<int> s_detectState{DV_DETECT_FAILED},writes{0},scans{0};
std::atomic<uint16_t> s_detectedTop{0},s_detectedBottom{0},s_detectedLeft{0},s_detectedRight{0};
bool dvDetection=true;bool aml_dv_detect_active_area_enabled(){return dvDetection;}
std::map<std::string,int> sysfsValues;
struct CSysfsPath{CSysfsPath(const char* path,int value){sysfsValues[path]=value;++writes;}};
bool overrideActive=false,phantom=false;uint16_t overrideTop=0,overrideBottom=0;
bool aml_dv_get_l5_override(uint16_t& t,uint16_t& b,uint16_t&,uint16_t&){t=overrideTop;b=overrideBottom;return overrideActive;}
bool _l5_override_parse(uint16_t& t,uint16_t& b,uint16_t& l,uint16_t& r){return aml_dv_get_l5_override(t,b,l,r);}
// Automatic cropped-canvas synthesis is a separate geometry fixture boundary.
bool _auto_letterbox_geometry(uint16_t&,uint16_t&,uint16_t&,uint16_t&){return false;}
std::atomic<bool> s_autoLbAdditive{false};
#ifdef FIXTURE_CURRENT
constexpr int NativeClearWrites=4;
#else
constexpr int NativeClearWrites=4;
#endif
bool aml_dv_auto_letterbox_active(){return phantom;}
bool aml_dv_auto_letterbox_additive(){return !phantom;}
struct DetectSource;
void DetectActiveAreaFromFile(const std::shared_ptr<DetectSource>&,const CAMLNativeWorker::Run&);
class CDVDSubtitlesLibass {
public:
 bool IsAdapted()const{return m_subtitleType==ADAPTED;}
 ASS_Library* m_library=nullptr;ASS_Renderer* m_renderer=nullptr;ASS_Track* m_track=nullptr;
 CCriticalSection m_section;int m_subtitleType=ADAPTED,m_currentDefaultStyleId=ASS_NO_ID,m_defaultKodiStyleId=0;
 std::string m_defaultFontFamilyName="DejaVu Sans";
 std::shared_ptr<const CLibassRenderResult> m_lastResult;
 std::shared_ptr<const style> m_lastStyle;int m_lastImageYOffset=0;
 renderOpts m_lastOpts{};bool m_renderCacheValid=false;int64_t m_cacheValidFrom=0,m_cacheValidUntil=0;
 ~CDVDSubtitlesLibass(){if(m_track)ass_free_track(m_track);if(m_renderer)ass_renderer_done(m_renderer);if(m_library)ass_library_done(m_library);}
 void Init(const char*,const std::string&);
 void ApplyStyle(const std::shared_ptr<const style>&,renderOpts);
 void ConfigureAssOverride(const std::shared_ptr<const style>&,ASS_Style*);
 std::shared_ptr<const CLibassRenderResult> RenderImage(double,renderOpts,bool,std::shared_ptr<const style>);
 bool IsDynamicEvent(const ASS_Event*)const;void UpdateRenderCache(int64_t);void InvalidateRenderCache();int GetPlayResY();
};
struct CDVDOverlayLibass:std::enable_shared_from_this<CDVDOverlayLibass>{
 std::shared_ptr<CDVDSubtitlesLibass> handler;bool forced=false,textAlign=false;
 auto GetLibassHandler()const{return handler;}bool IsForcedMargins()const{return forced;}bool IsTextAlignEnabled()const{return textAlign;}
};
namespace OVERLAY {
@STATE@
class COverlay {
public:
@FIELDS@
 COverlay();virtual ~COverlay()=default;virtual bool IsValid()const{return true;}
 virtual void Render(SRenderState&){}
 static std::shared_ptr<COverlay>Create(ASS_Image*,float,float);
 static std::shared_ptr<COverlay>Create(const CLibassRenderResult&,float,float);
 static const ASS_Image*Images(const CLibassRenderResult& value){return value.m_images.get();}
 static std::vector<uint8_t>Bytes(const CLibassRenderResult& value){std::vector<uint8_t> out;
  for(auto* image=value.m_images.get();image;image=image->next)for(int y=0;y<image->h;++y)out.insert(out.end(),image->bitmap+y*image->stride,image->bitmap+y*image->stride+image->w);return out;}
};
class COverlayGlyphGLES:public COverlay{
public:
@VERTEX@
@PAGE@
 std::shared_ptr<CGLESTextureResources> m_textureResources;std::vector<Page>m_pages;bool m_uploadFailed=false;
 COverlayGlyphGLES(ASS_Image*,float,float);~COverlayGlyphGLES()override;bool IsValid()const override;
};
struct CRenderer {
@GEOMETRY@
 CCriticalSection m_section;CRect m_rs{0,0,3840,2160},m_rd{0,0,1920,1080},m_rv{0,0,1920,1080},m_activePicture;
 bool m_restrictToActivePicture=false,m_isSettingsChanged=false,m_activeAreaApplyUserPos=false;
 int m_activeAreaTopOffset=0,m_activeAreaBottomOffset=0,m_subtitlePosition=1080,m_subtitlePosResInfo=1080,m_subtitleVerticalMargin=0;
 enum{POSRESINFO_SAVE_CHANGES=-2};
 SUBTITLES::Align m_subtitleAlign=SUBTITLES::Align::BOTTOM_INSIDE;
 SUBTITLES::HorizontalAlign m_subtitleHorizontalAlign=SUBTITLES::HorizontalAlign::CENTER;
 std::string m_stereomode;
 std::map<std::shared_ptr<const CDVDOverlayLibass>,std::shared_ptr<COverlay>,std::owner_less<std::shared_ptr<const CDVDOverlayLibass>>>m_textureCache;
 void ResetSubtitlePosition(){m_subtitlePosResInfo=window.info.iSubtitles;m_subtitlePosition=window.info.iSubtitles;}
 void SetActivePicture(const CRect&,bool,bool);void SetActiveAreaOffsets(int,int,bool);
 SRenderGeometry PrepareRenderGeometry(const COverlay&,float=-1.0f)const;static SRenderState CalculateRenderState(const SRenderGeometry&);
 std::shared_ptr<COverlay>ConvertLibass(const CDVDOverlayLibass&,double,bool,std::shared_ptr<const style>);
};
}
using namespace OVERLAY;
int OVERLAY::GetStereoscopicDepth(bool,int){return 0;}
enum class StreamHdrType{HDR_TYPE_NONE,HDR_TYPE_DOLBYVISION};
struct CRenderManager{
 struct Picture{int iWidth=3840,iHeight=2160;std::string stereoMode;StreamHdrType hdrType=StreamHdrType::HDR_TYPE_NONE;}m_picture;
 CRenderer m_overlays;CRect CalcOverlayActiveArea(CRect&,CRect&,CRect&,bool,double);
 CRect CalcPictureActiveArea(const CRect&,const CRect&,const CRect&,double,bool,bool=false);
};
'''

SOFTWARE_ADAPTERS = r'''
struct VideoPicture{unsigned int iWidth=3840,iHeight=2160;};
struct CDVDVideoCodec{virtual ~CDVDVideoCodec()=default;};
struct CDVDVideoCodecFFmpeg:CDVDVideoCodec{bool accel=false;void*GetHWAccel(){return accel?this:nullptr;}};
struct CVideoPlayerVideo{
 struct Hints{std::shared_ptr<const void> subtitleProbeSource;StreamHdrType hdrType=StreamHdrType::HDR_TYPE_NONE;int width=3840,height=2160;}m_hints;
 struct ProcessInfo{bool hw=false;int reads=0;bool IsVideoHwDecoder(){++reads;return hw;}}m_processInfo;
 std::unique_ptr<CDVDVideoCodec>m_pVideoCodec;std::shared_ptr<const void>m_subtitleProbeSource;
 unsigned int m_subtitleProbeWidth=0,m_subtitleProbeHeight=0;
 void UpdateSubtitleProbe(const VideoPicture&);void ResetSubtitleProbe();void StopSubtitleProbe();
};
'''

TESTS = r'''
void CDVDSubtitlesLibass::Init(const char* font,const std::string& cue){
 m_library=ass_library_init();assert(m_library);m_renderer=ass_renderer_init(m_library);assert(m_renderer);
 ass_set_fonts(m_renderer,font,"DejaVu Sans",ASS_FONTPROVIDER_NONE,nullptr,1);
 std::string script=R"ASS([Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 360
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,28,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,1,0,2,10,10,5,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:10.00,Default,,0,0,0,,)ASS" +cue+"\n";
 m_track=ass_read_memory(m_library,script.data(),script.size(),nullptr);assert(m_track&&m_track->n_events==1);
 m_defaultKodiStyleId=m_track->events[0].Style;
}
struct Bounds{int left=100000,top=100000,right=-100000,bottom=-100000;};
struct Draw{std::shared_ptr<COverlay> overlay;std::shared_ptr<const CLibassRenderResult> owned;Bounds bounds;std::vector<uint8_t> canvas;};
Bounds pixelBounds(const std::vector<uint8_t>& image,int width,int height){Bounds out;
 for(int y=0;y<height;++y)for(int x=0;x<width;++x)if(image[y*width+x]){
  out.left=std::min(out.left,x);out.top=std::min(out.top,y);out.right=std::max(out.right,x+1);out.bottom=std::max(out.bottom,y+1);}
 assert(out.right>out.left&&out.bottom>out.top);return out;}
std::vector<uint8_t> submit(const COverlayGlyphGLES& glyph,const SRenderState& state){
 // GPU boundary: use the production atlas and production normalized vertices.
 // A host alpha canvas lets assertions inspect final nontransparent pixels.
 std::vector<uint8_t> result(1920*1080,0);
 for(const auto& page:glyph.m_pages){const auto& atlas=textures.at(page.texture);
  for(size_t k=0;k<page.vertex.size();k+=4){const auto& a=page.vertex[k];const auto& d=page.vertex[k+3];
   int left=static_cast<int>(std::lround(state.x+a.x*state.width));
   int top=static_cast<int>(std::lround(state.y+a.y*state.height));
   int right=static_cast<int>(std::lround(state.x+d.x*state.width));
   int bottom=static_cast<int>(std::lround(state.y+d.y*state.height));
   int u=static_cast<int>(std::lround(a.u*atlas.w)),v=static_cast<int>(std::lround(a.v*atlas.h));
   assert(right-left>0&&bottom-top>0);assert(u>=0&&v>=0&&u+right-left<=atlas.w&&v+bottom-top<=atlas.h);
   for(int y=top;y<bottom;++y)for(int x=left;x<right;++x){
    int alpha=atlas.bytes[(v+y-top)*atlas.w+u+x-left]*a.a/255;
    if(x>=0&&x<1920&&y>=0&&y<1080)result[y*1920+x]=std::max(result[y*1920+x],static_cast<uint8_t>(alpha));}
  }
 }
 return result;
}
void near(float a,float b){assert(std::abs(a-b)<0.01f);}
void rect(CRect area,float top,float bottom){near(area.y1,top);near(area.y2,bottom);}
void inside(const Bounds& value,const CRect& area){assert(value.top>=area.y1&&value.bottom<=area.y2);}
std::shared_ptr<const style> makeStyle(bool top=false,int margin=10,int shadow=20){
 style s{};s.fontName="DejaVu Sans";s.fontSize=28;s.alignment=top?FontAlign::TOP_CENTER:FontAlign::SUB_CENTER;
 s.marginVertical=margin;s.shadowSize=shadow;s.fontBorderSize=20;return std::make_shared<const style>(s);
}
Draw draw(CRenderManager& manager,const std::shared_ptr<CDVDOverlayLibass>& cue,const std::shared_ptr<const style>& st,bool restrict=true,bool forceStyle=false,double pts=500000){
 auto& r=manager.m_overlays;manager.CalcOverlayActiveArea(r.m_rs,r.m_rd,r.m_rv,restrict,pts);
 bool update=forceStyle||r.m_isSettingsChanged;r.m_isSettingsChanged=false;
 auto overlay=r.ConvertLibass(*cue,pts,update,st);assert(overlay);
 auto glyph=std::dynamic_pointer_cast<COverlayGlyphGLES>(overlay);assert(glyph&&glyph->IsValid());
 auto state=r.CalculateRenderState(r.PrepareRenderGeometry(*glyph));
 near(state.width,1920);near(state.height,1080);near(state.x,0);near(state.y,0);
 auto owned=cue->handler->m_lastResult;assert(owned);auto pixels=submit(*glyph,state);
 return {overlay,owned,pixelBounds(pixels,1920,1080),std::move(pixels)};
}
void awaitPublication(){
 auto deadline=std::chrono::steady_clock::now()+std::chrono::seconds(3);
 for(;;){{std::lock_guard<std::mutex> lock(s_detectSourceMutex);if(s_detectSource->result&&!s_detectSource->superseded->load())return;}
  assert(std::chrono::steady_clock::now()<deadline);std::this_thread::sleep_for(std::chrono::milliseconds(1));}
}
void ready(){auto request=CAMLSession::FenceDisplay();assert(CAMLSession::TryBeginDisplay(request));assert(CAMLSession::EndDisplay(request,CAMLSession::DisplayPhase::READY));}
void select(bool native=false,int width=3840,int height=2160,bool probe=true){
 aml_dv_detect_set_file("fixture-same-path");assert(aml_subtitle_active_area_configure(width,height,native,probe,aml_subtitle_active_area_source()));
}
void publish(uint16_t border=263){CAMLNativeWorker::Run run(s_detectSource->superseded);detect_publish(run,s_detectSource,border,border,0,0);}
bool get(int width=3840,int height=2160){uint16_t t,b,l,r;bool found=aml_subtitle_detect_active_area_get(width,height,t,b,l,r);if(found)assert(t==263&&b==263&&l==0&&r==0);return found;}
std::shared_ptr<CDVDOverlayLibass> cue(const char* font,const std::string& text){auto result=std::make_shared<CDVDOverlayLibass>();result->handler=std::make_shared<CDVDSubtitlesLibass>();result->handler->Init(font,text);return result;}
size_t visibleGlyphPixels(const CLibassRenderResult& result){
 size_t count=0;for(auto* image=COverlay::Images(result);image;image=image->next){
  if((image->color&255)==255)continue;
  for(int y=0;y<image->h;++y)for(int x=0;x<image->w;++x)count+=image->bitmap[y*image->stride+x]!=0;
 }return count;
}
void textEnvelopePrecedence(){
 select(false,1920,1080);CAMLNativeWorker::Run run(s_detectSource->superseded);
 detect_publish_subtitle(run,s_detectSource,DetectResult{42,42,500,500});
 detect_publish(run,s_detectSource,42,42,0,0);
 uint16_t t,b,l,r;
 assert(aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r,false)&&t==42&&l==0&&r==0);
 assert(aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r,true)&&t==42&&l==500&&r==500);
 detect_publish(run,s_detectSource,0,0,0,0);
 assert(aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r,true)&&t==0&&b==0&&l==0&&r==0);
 std::cout<<"PASS: native positive-axis crop preserves text envelope; strict full-frame remains authoritative\n";
}
void fullFrameConfinement(const char* font){
 select(false,1920,1080);CRenderManager manager;manager.m_picture.iWidth=1920;manager.m_picture.iHeight=1080;
 manager.m_overlays.m_rs={0,0,1920,1080};settingsValue.applyMargin=true;
 auto text=cue(font,"Going deeply below\\NYoung glyphs descend: gypq");
 for(int margin:{0,1,3}){
  manager.m_overlays.m_subtitleAlign=SUBTITLES::Align::BOTTOM_INSIDE;
  auto bottom=draw(manager,text,makeStyle(false,margin),true,true);
  manager.m_overlays.m_subtitleAlign=SUBTITLES::Align::TOP_INSIDE;
  auto top=draw(manager,text,makeStyle(true,margin),true,true);
  // Matching uncut raster pixel counts detect libass canvas clipping before
  // atlas submission; a clipped host canvas alone cannot establish a fit.
  assert(visibleGlyphPixels(*bottom.owned)==visibleGlyphPixels(*top.owned));
  const int padding=static_cast<int>(std::ceil(margin*1080.0/720.0));
  assert(bottom.bounds.bottom<=1080-padding&&top.bounds.top>=padding);
 }
 settingsValue.applyMargin=false;
}
void discovery(const char* font){
 select();int before=writes.load();CRenderManager manager;auto c=cue(font,"Going deeply below\\NYoung glyphs descend: gypq");auto st=makeStyle();
 auto normal=draw(manager,c,st,true,true);assert(normal.bounds.bottom>1000);assert(!get());
 publish();assert(get()&&writes==before);auto confined=draw(manager,c,st);
 rect(manager.m_overlays.m_activePicture,131.5f,948.5f);
 inside(confined.bounds,manager.m_overlays.m_activePicture);
 assert(manager.m_overlays.m_activeAreaTopOffset==132&&manager.m_overlays.m_activeAreaBottomOffset==132);assert(confined.bounds.bottom<normal.bounds.bottom-100);
 assert(confined.owned!=normal.owned&&confined.overlay!=normal.overlay);
 // Compare correction against the SAME raw libass rasterization. A style
 // reconfiguration can change subpixel phase; integer owned translation itself
 // must never change bytes, dimensions, colour or interline spacing.
 int rawChanges=0;const ASS_Image* raw=ass_render_frame(c->handler->m_renderer,c->handler->m_track,500,&rawChanges);
 assert(raw);CLibassRenderResult rawOwned(raw);
 assert(COverlay::Bytes(*confined.owned)==COverlay::Bytes(rawOwned));
 auto* a=COverlay::Images(rawOwned);auto* b=COverlay::Images(*confined.owned);
 int delta=b->dst_y-a->dst_y;assert(delta<=0);
 for(;a&&b;a=a->next,b=b->next){assert(b->dst_y-a->dst_y==delta&&b->dst_x==a->dst_x&&b->w==a->w&&b->h==a->h&&b->color==a->color);}
 assert(!a&&!b);
 for(int n=0;n<10;++n){auto stable=draw(manager,c,st);assert(stable.owned==confined.owned&&stable.overlay==confined.overlay&&stable.canvas==confined.canvas);}
 // Full-frame restriction now also preserves complete glyphs and its margin
 // policy; disabling it restores the independent unrestricted style route.
 auto unrestricted=draw(manager,c,st,false);assert(unrestricted.canvas!=normal.canvas);
 auto confinedAgain=draw(manager,c,st);inside(confinedAgain.bounds,manager.m_overlays.m_activePicture);
 // Style object/options must invalidate a retained handler even if the renderer
 // gives updateStyle=false (e.g. another handler consumed its global flag).
 auto replacement=makeStyle(false,25,20);auto changed=draw(manager,c,replacement);
 assert(c->handler->m_track->styles[c->handler->m_defaultKodiStyleId].MarginV!=0);
 assert(c->handler->m_lastStyle==replacement);
 settingsValue.applyMargin=true;auto padded=draw(manager,c,replacement);
 const double pad=25.0*(1080-264)/720;assert(padded.bounds.bottom<=std::floor(1080-132-pad));
 assert(padded.bounds.bottom<changed.bounds.bottom);inside(padded.bounds,manager.m_overlays.m_activePicture);
 settingsValue.applyMargin=false;auto unpadded=draw(manager,c,replacement);assert(unpadded.canvas==changed.canvas);
 auto topStyle=makeStyle(true);manager.m_overlays.m_subtitleAlign=SUBTITLES::Align::TOP_INSIDE;
 auto top=draw(manager,c,topStyle);inside(top.bounds,manager.m_overlays.m_activePicture);assert(top.bounds.top<200);
 settingsValue.applyMargin=true;auto topPadded=draw(manager,c,topStyle);assert(topPadded.bounds.top>top.bounds.top);
 settingsValue.applyMargin=false;
 // Forced margins / authored positioning are deliberate exclusions; neither
 // confinement nor style refresh may turn them into an adapted-dialogue crop.
 c->forced=true;auto forcedNormal=draw(manager,c,topStyle,false);auto forced=draw(manager,c,topStyle);assert(forced.canvas==forcedNormal.canvas);assert(c->handler->m_lastOpts.marginsMode==MarginsMode::DISABLED);assert(forced.bounds.top<131.5f);c->forced=false;
 manager.m_picture.stereoMode="left_right";auto stereo=draw(manager,c,topStyle);assert(!manager.m_overlays.m_restrictToActivePicture&&stereo.bounds.top<131.5f);manager.m_picture.stereoMode.clear();
 auto restored=draw(manager,c,topStyle);inside(restored.bounds,manager.m_overlays.m_activePicture);
 settingsValue.detect=false;auto disabled=draw(manager,c,topStyle);assert(!get()&&disabled.bounds.top<131.5f);settingsValue.detect=true;
 auto enabled=draw(manager,c,topStyle);inside(enabled.bounds,manager.m_overlays.m_activePicture);
 // A fresh independent handler observes options without a renderer style flag.
 auto second=cue(font,"Second handler gypq");auto secondResult=draw(manager,second,topStyle,true,false,2500000);inside(secondResult.bounds,manager.m_overlays.m_activePicture);
 auto later=draw(manager,second,topStyle,true,false,4500000);assert(later.canvas==secondResult.canvas);
#ifdef FIXTURE_CURRENT
 auto huge=cue(font,"gypq\\Ngypq\\Ngypq\\Ngypq\\Ngypq\\Ngypq\\Ngypq\\Ngypq\\Ngypq\\Ngypq\\Ngypq\\Ngypq");
 style large=*st;large.fontSize=55;auto hugeStyle=std::make_shared<const style>(large);
 manager.m_overlays.m_subtitleAlign=SUBTITLES::Align::BOTTOM_INSIDE;
 auto oversized=draw(manager,huge,hugeStyle);
 assert(oversized.bounds.bottom-oversized.bounds.top>816);
 assert(oversized.bounds.top<132&&oversized.bounds.bottom>948);
 assert(std::abs((oversized.bounds.top+oversized.bounds.bottom)/2.0-540)<25);
 int changes=0;auto* hugeRaw=ass_render_frame(huge->handler->m_renderer,huge->handler->m_track,500,&changes);assert(hugeRaw);
 CLibassRenderResult hugeOriginal(hugeRaw);assert(COverlay::Bytes(hugeOriginal)==COverlay::Bytes(*oversized.owned));
#endif
 // A later external playback with a new selection never inherits prior results.
 select();assert(!get());auto next=cue(font,"Tijd om te vertrekken.\\N- Hoezo? gypq");manager.m_overlays.m_subtitleAlign=SUBTITLES::Align::BOTTOM_INSIDE;
 auto nextUnrestricted=draw(manager,next,st,true,false,3500000);assert(nextUnrestricted.bounds.bottom>1000);
 publish();auto nextConfined=draw(manager,next,st,true,false,3500000);inside(nextConfined.bounds,manager.m_overlays.m_activePicture);
 std::cout<<"PASS: same-PTS paused discovery, nonzero-alpha glyph bounds, translation bytes/spacing, stable cache, margins/top/forced/stereo and multiple handlers\n";
}
void lifecycle(const char* font){
 select();publish();auto selected=aml_subtitle_active_area_source();auto oldSource=s_detectSource;
 CAMLNativeWorker::Run oldRun(oldSource->superseded);
 cache.meta.has_level5_metadata=true;cache.pts=500000;
 Invalidate(true);assert(oldRun.Cancelled()&&get()&&!cache.meta.has_level5_metadata);
 CRenderManager manager;auto c=cue(font,"Seek retained geometry gypq");auto st=makeStyle();auto during=draw(manager,c,st,true,true);inside(during.bounds,manager.m_overlays.m_activePicture);
 detect_publish(oldRun,oldSource,400,400,0,0);assert(!oldSource->result&&get());
 assert(aml_subtitle_active_area_configure(3840,2160,false,true,selected));assert(get());
 auto scansBefore=scans.load();aml_dv_detect_active_area_start();awaitPublication();s_detectWorker.Stop();assert(scans==scansBefore&&get());
 auto after=draw(manager,c,st);assert(after.canvas==during.canvas);
 // Repeated seek before fresh owner configuration keeps the accepted result.
 Invalidate(true);Invalidate(true);assert(get());assert(aml_subtitle_active_area_configure(3840,2160,false,true,selected));assert(get());
 // Incomplete scans cannot be retained, or cancelled stale results promoted.
 Invalidate();assert(!get());assert(aml_subtitle_active_area_configure(3840,2160,false,true,selected));
 auto incomplete=s_detectSource;CAMLNativeWorker::Run incompleteRun(incomplete->superseded);Invalidate(true);assert(!get());detect_publish(incompleteRun,incomplete,263,263,0,0);assert(!incomplete->result&&!get());
 // Same path means a new playback identity, never a reusable geometry token.
 select();publish();auto priorSelection=aml_subtitle_active_area_source();auto prior=s_detectSource;CAMLNativeWorker::Run priorRun(prior->superseded);
 aml_dv_detect_set_file("fixture-same-path");assert(!get());assert(!aml_subtitle_active_area_configure(3840,2160,false,true,priorSelection));
 detect_publish(priorRun,prior,263,263,0,0);assert(!get());
 for(int change=0;change<4;++change){
  select();publish();auto current=aml_subtitle_active_area_source();Invalidate(true);assert(get());
  int width=change==0?1920:3840,height=change==0?1080:2160;
  bool native=change==1,probe=change!=2;
  if(change==3)Invalidate();
  assert(aml_subtitle_active_area_configure(width,height,native,probe,current));assert(!get(width,height));
 }
 select();publish();assert(!get(1920,1080));aml_dv_detect_active_area_stop();assert(!get());
 // Cropped geometry is visible renderer padding without a probe.
 select(false,3840,1634);auto scansBeforeCrop=scans.load();aml_dv_detect_active_area_start();s_detectWorker.Stop();assert(scans==scansBeforeCrop&&!get(3840,1634));
 manager.m_picture.iHeight=1634;manager.m_overlays.m_rs={0,0,3840,1634};manager.m_overlays.m_rd={0,131.5f,1920,948.5f};auto cropped=draw(manager,c,st);inside(cropped.bounds,manager.m_overlays.m_activePicture);
 std::cout<<"PASS: pure seek/reset retained geometry, old-run rejection, same-path replacement, geometry/native/eligibility/destructive lifecycle and cropped fallback\n";
}
void nativeExemption(const char* font){
 select();publish();CRenderManager manager;auto c=cue(font,"Native authored gypq");c->handler->m_subtitleType=NATIVE;
 auto st=makeStyle();auto ordinary=draw(manager,c,st,false,true);auto restricted=draw(manager,c,st);
 assert(restricted.canvas==ordinary.canvas&&restricted.bounds.bottom>948.5f);
 assert(c->handler->m_lastImageYOffset==0);
 std::cout<<"PASS: native authored ASS retains unrestricted pixels and is excluded from adapted text translation\n";
}
void native(){
 select(true);cache.meta={};publish();assert(get());auto nativeWrites=writes.load();assert(nativeWrites>=4);
 CRenderManager manager;manager.m_picture.hdrType=StreamHdrType::HDR_TYPE_DOLBYVISION;auto& r=manager.m_overlays;
 cache.pts=500000;cache.meta.has_level5_metadata=true;cache.meta.level5_active_area_top_offset=cache.meta.level5_active_area_bottom_offset=300;
 rect(manager.CalcOverlayActiveArea(r.m_rs,r.m_rd,r.m_rv,true,500000),150,930);
#ifdef FIXTURE_CURRENT
 // Understated authored metadata remains the native/public rectangle while
 // adapted text uses its independent detected confinement. Native writes do
 // not change. Detection can protect text while native injection is disabled.
 cache.meta.level5_active_area_top_offset=cache.meta.level5_active_area_bottom_offset=20;
 rect(manager.CalcPictureActiveArea(r.m_rs,r.m_rd,r.m_rv,500000,false,false),10,1070);
 rect(manager.CalcOverlayActiveArea(r.m_rs,r.m_rd,r.m_rv,true,500000),131.5f,948.5f);
 dvDetection=false;uint16_t dt,db,dl,dr;
 assert(!aml_subtitle_detect_active_area_get(3840,2160,dt,db,dl,dr,false));
 assert(aml_subtitle_detect_active_area_get(3840,2160,dt,db,dl,dr,true)&&dt==263);
 rect(manager.CalcOverlayActiveArea(r.m_rs,r.m_rd,r.m_rv,true,500000),131.5f,948.5f);
 dvDetection=true;assert(writes==nativeWrites);
#endif
 cache.meta.level5_active_area_top_offset=cache.meta.level5_active_area_bottom_offset=300;
 overrideActive=true;overrideTop=overrideBottom=0;rect(manager.CalcOverlayActiveArea(r.m_rs,r.m_rd,r.m_rv,true,500000),0,1080);overrideActive=false;
 // Authored frame metadata prevents detected native sysfs replacement.
 publish();assert(writes==nativeWrites);
 Invalidate(true);assert(get()&&!cache.meta.has_level5_metadata);auto current=aml_subtitle_active_area_source();assert(aml_subtitle_active_area_configure(3840,2160,true,true,current));
 cache.meta.has_level5_metadata=true;cache.meta.level5_active_area_top_offset=cache.meta.level5_active_area_bottom_offset=300;
 aml_dv_detect_active_area_start();awaitPublication();s_detectWorker.Stop();assert(writes==nativeWrites+NativeClearWrites); // startup clears only; authored metadata blocks injection
 dvDetection=false;assert(!get());
#ifdef FIXTURE_CURRENT
 select(true);cache.meta={};auto textOnlyScans=scans.load(),textOnlyWrites=writes.load();
 aml_dv_detect_active_area_start();awaitPublication();s_detectWorker.Stop();
 assert(scans==textOnlyScans+1&&writes==textOnlyWrites+NativeClearWrites&&!s_detectInjected.load()); // startup clears old driver state; no native injection
 uint16_t st,sb,sl,sr;assert(aml_subtitle_detect_active_area_get(3840,2160,st,sb,sl,sr,true)&&st==263);
 assert(!aml_subtitle_detect_active_area_get(3840,2160,st,sb,sl,sr,false));
#endif
 dvDetection=true;
 select(false);publish();auto genericWrites=writes.load();Invalidate(true);current=aml_subtitle_active_area_source();assert(aml_subtitle_active_area_configure(3840,2160,false,true,current));aml_dv_detect_active_area_start();awaitPublication();s_detectWorker.Stop();assert(writes==genericWrites&&get());
 // Converted output type does not turn original HDR10 into native geometry.
 manager.m_picture.hdrType=StreamHdrType::HDR_TYPE_DOLBYVISION;cache.meta.has_level5_metadata=true;cache.meta.level5_active_area_top_offset=cache.meta.level5_active_area_bottom_offset=300;
 rect(manager.CalcOverlayActiveArea(r.m_rs,r.m_rd,r.m_rv,true,500000),131.5f,948.5f);assert(writes==genericWrites);
 std::cout<<"PASS: native authored/manual precedence, metadata freshness, original HDR-to-VS10 identity and separate native writes\n";
}
#ifdef FIXTURE_CURRENT
void softwareRoute(const char* font){
 aml_dv_detect_set_file("software-vc1-synthetic-bars");CVideoPlayerVideo owner;
 owner.m_hints.subtitleProbeSource=aml_subtitle_active_area_source();owner.m_hints.width=640;owner.m_hints.height=480;
 owner.m_processInfo.hw=true;owner.m_pVideoCodec=std::make_unique<CDVDVideoCodecFFmpeg>();
 VideoPicture picture{1920,1080};auto writesBefore=writes.load(),scansBefore=scans.load();
 CRenderManager manager;manager.m_picture.iWidth=1920;manager.m_picture.iHeight=1080;manager.m_overlays.m_rs={0,0,1920,1080};
 auto c=cue(font,"Tijd om te vertrekken.\\N- Hoezo? gypq");auto st=makeStyle();auto normal=draw(manager,c,st,true,true);assert(normal.bounds.bottom>1000);
 owner.UpdateSubtitleProbe(picture);awaitPublication();s_detectWorker.Stop();
 assert(s_detectSource->width==1920&&s_detectSource->height==1080&&!s_detectSource->nativeDV);
 uint16_t t,b,l,r;assert(aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r)&&t==132&&b==132);
 auto active=draw(manager,c,st);inside(active.bounds,manager.m_overlays.m_activePicture);rect(manager.m_overlays.m_activePicture,132,948);
 assert(writes==writesBefore&&scans==scansBefore+1&&owner.m_processInfo.reads==0);
 owner.UpdateSubtitleProbe(picture);assert(scans==scansBefore+1);
 auto old=s_detectSource;CAMLNativeWorker::Run oldRun(old->superseded);owner.ResetSubtitleProbe();assert(oldRun.Cancelled());
 assert(aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r));auto retained=draw(manager,c,st);assert(retained.canvas==active.canvas);
 owner.UpdateSubtitleProbe(picture);awaitPublication();s_detectWorker.Stop();assert(scans==scansBefore+1);
 assert(aml_subtitle_detect_active_area_get(1920,1080,t,b,l,r));
 // Actual decoded dimensions rotate the lifetime independently of stale hints.
 picture={3840,2160};owner.UpdateSubtitleProbe(picture);awaitPublication();s_detectWorker.Stop();assert(get());
 assert(s_detectSource->width==3840&&s_detectSource->height==2160);
 auto* installed=dynamic_cast<CDVDVideoCodecFFmpeg*>(owner.m_pVideoCodec.get());assert(installed);
 installed->accel=true;owner.UpdateSubtitleProbe(picture);assert(!get()&&!owner.m_subtitleProbeSource);
 installed->accel=false;owner.UpdateSubtitleProbe(picture);awaitPublication();s_detectWorker.Stop();assert(get());
 select();publish();owner.ResetSubtitleProbe();assert(get());owner.StopSubtitleProbe();assert(get()); // stale owner cannot erase next selection
 for(int exclusion=0;exclusion<4;++exclusion){
  aml_dv_detect_set_file("excluded-owner");CVideoPlayerVideo other;other.m_hints.subtitleProbeSource=aml_subtitle_active_area_source();
  if(exclusion==0){auto codec=std::make_unique<CDVDVideoCodecFFmpeg>();codec->accel=true;other.m_pVideoCodec=std::move(codec);}
  if(exclusion==1)other.m_pVideoCodec=std::make_unique<CDVDVideoCodec>();
  if(exclusion==2){other.m_pVideoCodec=std::make_unique<CDVDVideoCodecFFmpeg>();other.m_hints.hdrType=StreamHdrType::HDR_TYPE_DOLBYVISION;}
  auto oldSelection=s_detectSource;other.UpdateSubtitleProbe(picture);assert(s_detectSource==oldSelection&&!get());
 }
 // Settings OFF admits no decoding work; a fresh reset after enabling uses the
 // same production owner helper rather than implying a live setting callback.
 aml_dv_detect_set_file("disabled-software");CVideoPlayerVideo disabled;disabled.m_pVideoCodec=std::make_unique<CDVDVideoCodecFFmpeg>();disabled.m_hints.subtitleProbeSource=aml_subtitle_active_area_source();
 settingsValue.detect=false;auto beforeDisabled=scans.load();disabled.UpdateSubtitleProbe(picture);assert(scans==beforeDisabled&&!get());
 settingsValue.detect=true;disabled.ResetSubtitleProbe();disabled.UpdateSubtitleProbe(picture);awaitPublication();s_detectWorker.Stop();assert(scans==beforeDisabled+1&&get());
 auto staleSelection=disabled.m_hints.subtitleProbeSource;select();publish();disabled.UpdateSubtitleProbe(picture);assert(get()&&s_detectSource->selection!=staleSelection);
 disabled.StopSubtitleProbe();assert(get());
 // A current owner close uses destructive stop/configuration, never retention.
 CVideoPlayerVideo closing;closing.m_pVideoCodec=std::make_unique<CDVDVideoCodecFFmpeg>();closing.m_hints.subtitleProbeSource=aml_subtitle_active_area_source();closing.UpdateSubtitleProbe(picture);awaitPublication();s_detectWorker.Stop();closing.StopSubtitleProbe();assert(!get());
 std::cout<<"PASS: actual software FFmpeg admission, decoded geometry, installed/HW/native guards, source-safe reset/close and SRT raster bounds\n";
}
#endif
#ifdef FIXTURE_CURRENT

#endif
void ownedTranslation(){
 // Independent exact byte/coordinate oracle for the actual owned-header offset.
 unsigned char pixels[]={1,2,3,4};ASS_Image raw{};raw.w=2;raw.h=2;raw.stride=2;raw.bitmap=pixels;raw.color=0xffffff00;raw.dst_x=5;raw.dst_y=10;
 CLibassRenderResult owned(&raw);auto* output=COverlay::Images(owned);assert(output->dst_y==10&&output->dst_x==5);assert(COverlay::Bytes(owned)==std::vector<uint8_t>({1,2,3,4}));
#ifdef FIXTURE_CURRENT
 CLibassRenderResult shifted(&raw,&owned,-3);assert(COverlay::Images(shifted)->dst_y==7);assert(COverlay::Bytes(shifted)==COverlay::Bytes(owned));
 renderOpts opts{};opts.frameHeight=1080;opts.marginsMode=MarginsMode::INSIDE_ACTIVE_AREA;opts.activeAreaTopMargin=opts.activeAreaBottomMargin=132;auto st=makeStyle(false,0);
 raw.dst_y=942;raw.h=10;assert(ActiveAreaTextOffset(&raw,opts,*st)==-4);
 raw.dst_y=128;assert(ActiveAreaTextOffset(&raw,opts,*st)==4);
 raw.dst_y=0;raw.h=900;assert(ActiveAreaTextOffset(&raw,opts,*st)==90); // oversized: centre without resize/clip
 opts.frameHeight=std::numeric_limits<float>::infinity();assert(ActiveAreaTextOffset(&raw,opts,*st)==0);
 opts.frameHeight=100;assert(ActiveAreaTextOffset(&raw,opts,*st)==0);
 opts.frameHeight=1080;assert(ActiveAreaTextOffset(nullptr,opts,*st)==0);
 opts.frameHeight=std::numeric_limits<float>::quiet_NaN();assert(ActiveAreaTextOffset(&raw,opts,*st)==0);
 opts.frameHeight=1080;raw.w=raw.h=0;assert(ActiveAreaTextOffset(&raw,opts,*st)==0);
#endif
}
int main(int argc,char** argv){assert(argc==2);assert(ass_library_version()==LIBASS_VERSION);assert(LIBASS_VERSION>=0x01704000);
 ready();ownedTranslation();
#ifdef FIXTURE_CURRENT
 textEnvelopePrecedence();fullFrameConfinement(argv[1]);
 softwareRoute(argv[1]);
#endif
 discovery(argv[1]);lifecycle(argv[1]);nativeExemption(argv[1]);native();s_detectWorker.Stop();
 std::cout<<"PASS: connected production detector→geometry→text conversion→CE libass→owned glyph atlas/vertices→host alpha canvas\n";
}
'''


def run(code, args, negative=False):
    with tempfile.TemporaryDirectory(prefix='subtitle-text-integration-') as temporary:
        out = Path(temporary)
        shutil.copytree(args.ass_include / 'ass', out / 'ass')
        # Baseline production owns its original constructor ABI/header.
        header = out / RESULTHEADER.removeprefix('xbmc/')
        header.parent.mkdir(parents=True)
        header.write_text(source(RESULTHEADER, args.baseline))
        (out / 'test.cpp').write_text(code)
        utility = out / 'BitmapSubtitlePosition.cpp'
        utility.write_text(source(VIDEO + 'VideoRenderers/BitmapSubtitlePosition.cpp', args.baseline))
        flags = [os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra', '-Werror',
                 '-Wno-misleading-indentation', '-pthread', '-fsanitize=address,undefined',
                 '-fno-omit-frame-pointer', '-fno-pie', '-no-pie', '-I', str(out), '-I', str(ROOT / 'xbmc'), '-I', str(ROOT / VIDEO / 'VideoRenderers')]
        subprocess.run(flags + [str(out / 'test.cpp'), str(utility), args.ass_library,
                               *[('-l:' + ctypes.util.find_library(name)) for name in ('freetype', 'harfbuzz', 'fribidi')], '-lm', '-o', str(out / 'test')], check=True)
        result = subprocess.run([str(out / 'test'), str(args.font)], capture_output=True, text=True,
                                env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, timeout=30)
        if negative:
            assert result.returncode == -signal.SIGABRT and 'Assertion' in result.stderr, result.stdout + result.stderr
            assert 'ERROR: AddressSanitizer' not in result.stderr and 'runtime error:' not in result.stderr, result.stderr
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout.strip())
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ass-include', type=Path, default=Path('/usr/include'))
    parser.add_argument('--ass-library', default='-lass')
    parser.add_argument('--font', type=Path, default=Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'))
    parser.add_argument('--negative-controls', action='store_true')
    parser.add_argument('--baseline', help='Original production Git revision; requires assertion failure after strict compilation')
    args = parser.parse_args()
    assert args.font.is_file() and (args.ass_include / 'ass/ass.h').is_file()
    code = generate(args.baseline)
    result = run(code, args, negative=bool(args.baseline))
    if args.baseline:
        assert "value.top>=area.y1&&value.bottom<=area.y2" in result.stderr, result.stderr
        print(result.stderr.splitlines()[-1])
        print('REJECTED original production baseline after strict compiler acceptance:', args.baseline)
    elif args.negative_controls:
        for label, old, new in [
            ('adapted collision positions not refreshed', 'ass_set_selective_style_override(m_renderer, style);\n    ass_set_selective_style_override_enabled(m_renderer, ASS_OVERRIDE_DEFAULT);', 'ass_set_selective_style_override_enabled(m_renderer, ASS_OVERRIDE_DEFAULT);'),
            ('owned image translation dropped', 'image.dst_y += verticalOffset;', '(void)verticalOffset;'),
            ('fractional active edge truncated', 'std::ceil(area.y1 - m_rv.y1)', '(area.y1 - m_rv.y1)'),
            ('handler retained stale style', 'm_lastStyle != subStyle || !RenderOptsEqual(opts, m_lastOpts)', 'false'),
            ('destructive lifecycle retains cache', 's_detectSource->retained.reset();', '(void)preserveGeometry;'),
            ('seek retained cache destroyed', 's_detectSource->retained = s_detectSource->result;', 's_detectSource->retained.reset();'),
            ('geometry identity ignored', 'previous->width == width && previous->height == height &&', 'true &&'),
            ('original native identity ignored', 'previous->nativeDV == nativeDV &&', 'true &&'),
            ('software to HW retained stale ownership', 'StopSubtitleProbe(); // format renegotiation may change the installed decoder route', '// fixture: retirement omitted'),
            ('software HW acceleration admitted', 'softwareCodec->GetHWAccel() ||', 'false ||'),
            ('software native hint admitted', 'm_hints.hdrType == StreamHdrType::HDR_TYPE_DOLBYVISION)', 'false)'),
            ('stale run publishes', 'run.Cancelled() || source != s_detectSource || source->superseded->load() ||', 'false || source != s_detectSource || false ||'),
        ]:
            assert old in code, label
            run(code.replace(old, new, 1), args, negative=True)
            print('REJECTED compiled runtime mutant:', label)


if __name__ == '__main__':
    main()
