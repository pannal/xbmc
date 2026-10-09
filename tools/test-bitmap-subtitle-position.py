#!/usr/bin/env python3
"""Production bitmap grouping/render preparation, crop-aware geometry and exact L5.

Settings/conversion/GPU are recording substitutes. Executes production helpers,
render capture/preparation/final overlap and active-area resolution under ASan/UBSan.
No CE image, decoding, GUI or HDMI acceptance is implied.
"""
import argparse
from pathlib import Path
import os
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / 'tools/test-overlay-render-geometry.py'))
function = fixture['function']


def source():
    code = '#include <iostream>\n' + fixture['harness']()
    code = code.replace('  double GetNumber(int)', '  bool GetBool(int){return false;}\n  double GetNumber(int)')
    code = code.replace('SETTING_SUBTITLES_BITMAPMARGIN=4;', 'SETTING_SUBTITLES_BITMAPMARGIN=4,SETTING_COREELEC_AMLOGIC_DV_RESTRICT_SUBS_USER_POS=5;')
    code = code.replace('struct CServiceBroker {', '''struct Metadata {
  bool has_level5_metadata=false,level5_detected=false;
  uint16_t level5_active_area_top_offset=0,level5_active_area_bottom_offset=0,
    level5_active_area_left_offset=0,level5_active_area_right_offset=0;
};
struct Cache {Metadata meta;double requested=0;Metadata GetVideoDoViFrameMetadata(double pts){requested=pts;return meta;}} cache;
struct CServiceBroker {
  static Cache& GetDataCacheCore(){return cache;}''')
    code = code.replace('  void RenderPqMenu(const OverlayBatch&);', '''  bool m_isSettingsChanged=false,m_activeAreaApplyUserPos=false;
  void SetActivePicture(const CRect&,bool,bool);
  void SetActiveAreaOffsets(int,int,bool);
  void RenderPqMenu(const OverlayBatch&);''')
    renderer = (ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/OverlayRenderer.cpp').read_text()
    for sig in ['void CRenderer::SetActivePicture(', 'void CRenderer::SetActiveAreaOffsets(']:
        code += function(renderer, sig) + '\n'
    code += r'''
enum class StreamHdrType {HDR_TYPE_NONE,HDR_TYPE_DOLBYVISION};
bool nativeDV=true,phantom=false,detected=false,overrideActive=false;
uint16_t detectedTop=140,detectedBottom=140,detectedLeft=0,detectedRight=0;
bool aml_subtitle_native_dv(){return nativeDV;}
bool aml_dv_auto_letterbox_active(){return phantom;}
bool aml_dv_auto_letterbox_additive(){return !phantom;}
bool aml_dv_get_l5_override(uint16_t&,uint16_t&,uint16_t&,uint16_t&){return overrideActive;}
bool aml_subtitle_detect_active_area_get(int,int,uint16_t& t,uint16_t& b,uint16_t& l,uint16_t& r){
  if(!detected)return false;t=detectedTop;b=detectedBottom;l=detectedLeft;r=detectedRight;return true;
}
struct CRenderManager {
  struct Picture {int iWidth=1920,iHeight=1080;std::string stereoMode;StreamHdrType hdrType=StreamHdrType::HDR_TYPE_DOLBYVISION;} m_picture;
  CRenderer m_overlays;
  CRect CalcOverlayActiveArea(CRect&,CRect&,CRect&,bool,double);
};
'''
    manager = (ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.cpp').read_text()
    code += function(manager, 'bool TextSubtitleNeedsSignal(') + '\n'
    code += function(manager, 'CRect CRenderManager::CalcOverlayActiveArea(')
    return code + TESTS


TESTS = r'''
void near(float x,float y){assert(std::abs(x-y)<0.02f);}
void rect(CRect r,float l,float t,float rr,float b){near(r.x1,l);near(r.y1,t);near(r.x2,rr);near(r.y2,b);}
int main(){
  const CRect screen(0,0,1920,1080),active(0,140,1920,940);
  rect(GetActivePictureArea(screen,screen,screen,screen,active),0,140,1920,940);
  rect(GetActivePictureArea({0,100,1920,980},screen,screen,screen,active),0,49.0909f,1920,1030.9091f);
  rect(GetActivePictureArea({240,0,1680,1080},screen,screen,screen,{400,0,1520,1080}),213.3333f,0,1706.6666f,1080);
  rect(GetActivePictureArea(screen,{0,140,1920,940},screen,screen,{}),0,140,1920,940);
  rect(GetActivePictureArea(screen,screen,screen,screen,{-1,0,1920,1080}),0,0,1920,1080);
  rect(GetActivePictureArea(screen,screen,screen,screen,{0,900,1920,800}),0,0,1920,1080);
  rect(GetActivePictureArea(screen,{0,-100,1920,1180},screen,screen,active),0,65.9259f,1920,1014.0741f);
  assert(GetActivePictureArea({},screen,screen,screen,active).IsEmpty());
  rect(GetBitmapSubtitleArea(screen,screen,active,1.78f),0,140,1920,940);
  rect(GetBitmapSubtitleArea(screen,screen,screen,2.4f),0,140,1920,940);

  // Native visible-text policy cannot infer horizontal clearance from a baseline.
  assert(TextSubtitleNeedsSignal(false,screen,screen,900));
  assert(TextSubtitleNeedsSignal(false,{240,0,1680,1080},screen,900));
  assert(!TextSubtitleNeedsSignal(false,active,screen,900));
  assert(TextSubtitleNeedsSignal(false,active,screen,950));
  assert(!TextSubtitleNeedsSignal(true,screen,screen,900));

  // Shared metadata/detection policy executes the actual manager resolver.
  CRenderManager manager;CRect src=screen,dst=screen,view=screen;
  cache.meta.has_level5_metadata=true; // zero authored L5 must not hide valid detection
  detected=true;
  rect(manager.CalcOverlayActiveArea(src,dst,view,true,123),0,140,1920,940);
  assert(cache.requested==123&&manager.m_overlays.m_restrictToActivePicture);
  cache.meta.level5_active_area_top_offset=180;cache.meta.level5_active_area_bottom_offset=180;
  rect(manager.CalcOverlayActiveArea(src,dst,view,true,124),0,180,1920,900);
  overrideActive=true; // explicit 0,0,0,0 remains authoritative
  rect(manager.CalcOverlayActiveArea(src,dst,view,true,125),0,0,1920,1080);
  overrideActive=false;phantom=true;
  dst=active;
  rect(manager.CalcOverlayActiveArea(src,dst,view,true,125),0,140,1920,940);
  phantom=false;nativeDV=false;manager.m_picture.hdrType=StreamHdrType::HDR_TYPE_NONE;
  detected=false;dst=active;
  rect(manager.CalcOverlayActiveArea(src,dst,view,true,126),0,140,1920,940);
  detected=true;dst=screen;
  rect(manager.CalcOverlayActiveArea(src,dst,view,false,127),0,140,1920,940);
  assert(!manager.m_overlays.m_restrictToActivePicture);
  services.stereo=1;
  rect(manager.CalcOverlayActiveArea(src,dst,view,true,128),0,0,1920,1080);
  assert(!manager.m_overlays.m_restrictToActivePicture);services.stereo=0;

  // Actual grouped render preparation with distinct converted regions.
  CRenderer r;r.m_activePicture=active;
  auto low=std::make_shared<CDVDOverlayImage>(),high=std::make_shared<CDVDOverlayImage>();
  low->m_canPosition=high->m_canPosition=true;low->bForced=true;
  auto first=std::make_shared<COverlay>(),second=std::make_shared<COverlay>();
  for(auto o:{first,second}){
    o->m_isBitmapOverlay=o->m_canPosition=true;o->m_pos=COverlay::POSITION_RELATIVE;
    o->m_align=COverlay::ALIGN_VIDEO;o->m_x=0.5f;o->m_width=400.0f/1920.0f;o->m_height=40.0f/1080.0f;
  }
  first->m_y=870.0f/1080.0f;second->m_y=930.0f/1080.0f;
  r.conversion=[&](const CDVDOverlay& o){return &o==high->GetPublishedRenderContent().get()?first:second;};
  CRenderer::OverlayBatch batch{{1,high},{1,low}};
  services={};services.position=0;
  auto original=r.PrepareRenderItems(batch);
  rect(original[0].bounds,760,850,1160,890);rect(original[1].bounds,760,910,1160,950);
  assert(CRenderer::HasImageSubOutsideActiveArea(original,active));
  for(int mode:{1,2,3,4,5}){
    services.position=mode;auto items=r.PrepareRenderItems(batch);
    near(items[1].bounds.y1-items[0].bounds.y1,60);
    near(items[0].state.width,400);near(items[1].state.height,40);
    if(mode==1||mode==2)assert(!CRenderer::HasImageSubOutsideActiveArea(items,active));
    if(mode==3||mode==4)assert(CRenderer::HasImageSubOutsideActiveArea(items,active));
  }
  // Margin scales with view height and author padding is retained for picture edges.
  services.position=1;services.margin=1;auto picture=r.PrepareRenderItems(batch);
  near(picture[1].bounds.y2,940-130.0f*800.0f/1080.0f);
  services.position=3;auto screenItems=r.PrepareRenderItems(batch);near(screenItems[1].bounds.y2,1069.2f);
  r.m_restrictToActivePicture=true;auto confined=r.PrepareRenderItems(batch);near(confined[1].bounds.y2,929.2f);
  assert(!CRenderer::HasImageSubOutsideActiveArea(confined,active));
  services.position=5;services.offset=-50;confined=r.PrepareRenderItems(batch);
  near(confined[1].bounds.y2,929.2f);
  // Whole-block zoom scales gaps and widths, not just each image independently.
  services.position=1;services.zoom=150;auto zoomed=r.PrepareRenderItems(batch);
  near(zoomed[1].bounds.y1-zoomed[0].bounds.y1,90);near(zoomed[0].bounds.Width(),600);
  // Oversized blocks keep layout and remain visible to overlap signaling.
  r.m_activePicture={0,500,1920,550};auto oversized=r.PrepareRenderItems(batch);
  near(oversized[1].bounds.y1-oversized[0].bounds.y1,90);near(oversized[0].bounds.Height(),60);
  assert(CRenderer::HasImageSubOutsideActiveArea(oversized,r.m_activePicture));
  near((oversized[0].bounds.y1+oversized[1].bounds.y2)*0.5f,525);
  r.m_activePicture=screen;services.zoom=100;services.position=0;services.aspect=240;
  auto hiddenAspect=r.PrepareRenderItems(batch);near(hiddenAspect[0].bounds.y1,850);near(hiddenAspect[1].bounds.y2,950);
  services.aspect=0;services.position=3;r.m_restrictToActivePicture=false;
  r.m_rv=r.m_rd={100,100,3940,2260};r.m_activePicture={100,380,3940,1980};
  auto uhd=r.PrepareRenderItems(batch);near(uhd[1].bounds.y2,2238.4f);near(uhd[0].bounds.Width(),800);
  r.m_rv=r.m_rd=screen;
  r.m_activePicture=active;r.m_restrictToActivePicture=false;services.zoom=100;services.position=4;
  // Forced stereo, source stereo, menus and unknown graphic provenance are excluded.
  services.stereo=1;auto stereo=r.PrepareRenderItems(batch);near(stereo[0].bounds.y1,850);
  services.stereo=0;r.m_stereomode="left_right";stereo=r.PrepareRenderItems(batch);near(stereo[0].bounds.y1,850);
  r.m_stereomode.clear();first->m_canPosition=second->m_canPosition=false;
  auto unrelated=r.PrepareRenderItems(batch);near(unrelated[0].bounds.y1,850);
  first->m_canPosition=second->m_canPosition=true;first->m_discMenuOverlay=true;second->m_discMenuOverlay=true;
  auto menus=r.PrepareRenderItems(batch);near(menus[0].bounds.y1,850);
  assert(!CRenderer::HasImageSubOutsideActiveArea(menus,active));
  first->m_discMenuOverlay=second->m_discMenuOverlay=false;
  // Retained final states/bounds drive both overlap and actual draw after settings mutate.
  services.position=3;auto retained=r.PrepareRenderItems(batch);services.position=1;r.m_activePicture=screen;
  assert(CRenderer::HasImageSubOutsideActiveArea(retained,active));
  int draws=0;first->draw=[&](SRenderState& state){near(state.y,retained[0].state.y);++draws;};
  second->draw=[&](SRenderState& state){near(state.y,retained[1].state.y);++draws;};
  const int before=r.conversions;r.RenderPrepared(retained,batch);assert(draws==2&&r.conversions==before);
  // Real copied/cut-out decoder provenance survives immutable publication.
  CDVDOverlayImage image;image.m_canPosition=true;image.width=image.height=image.linesize=2;image.pixels={1,1,1,1};image.palette={0,0xff000000};
  CDVDOverlayImage cut(image,0,0,1,1);assert(cut.m_canPosition);
  auto published=std::static_pointer_cast<const CDVDOverlayImage>(image.GetPublishedRenderContent());assert(published->m_canPosition);
  std::cout<<"PASS: production bitmap placement, shared geometry, grouping/zoom/margins/overlap/menu/stereo and publication\n";
}
'''


def run(code, helper=None, negative=False):
    with tempfile.TemporaryDirectory(prefix='bitmap-subtitle-test-') as tmp:
        out = Path(tmp)
        (out / 'PlatformDefs.h').write_text('#pragma once\n#define PIXEL_ASHIFT 24\n')
        (out / 'test.cpp').write_text(code)
        source_helper = ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/BitmapSubtitlePosition.cpp'
        if helper is not None:
            source_helper = out / 'BitmapSubtitlePosition.cpp'
            source_helper.write_text(helper)
        subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                        '-Wno-misleading-indentation', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-fno-pie', '-no-pie', '-I', str(out), '-I', str(ROOT / 'xbmc'),
                        '-I', str(ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers'),
                        str(out / 'test.cpp'), str(source_helper), '-o', str(out / 'test')], check=True)
        result = subprocess.run([str(out / 'test')], text=True, capture_output=True,
                                env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, timeout=15)
        if negative:
            assert result.returncode and 'Assertion' in result.stderr, result.stdout + result.stderr
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout.strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    code = source()
    run(code)
    if args.negative_controls:
        helper = (ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/BitmapSubtitlePosition.cpp').read_text()
        for label, old, new in [
            ('crop ignored', '(crop.y1 - source.y1)', 'crop.y1'),
            ('padding lost', 'const float bottomPadding = std::max(margin,', 'const float bottomPadding = std::min(margin,'),
            ('oversized per-region fallback', 'if (last - first > high - low)', 'if (false && last - first > high - low)'),
        ]:
            assert old in helper
            run(code, helper.replace(old, new), True)
            print('REJECTED:', label)
        for label, old, new in [
            ('zero L5 hides detection', '(metadata.level5_active_area_top_offset || metadata.level5_active_area_bottom_offset ||\n         metadata.level5_active_area_left_offset || metadata.level5_active_area_right_offset)', 'true'),
            ('full-frame text signal cleared', 'return !verticalBars || baseline <= active.y1 || baseline >= active.y2;', '(void)verticalBars; return baseline <= active.y1 || baseline >= active.y2;'),
            ('overlap assumes restriction', 'if (active.IsEmpty())', 'if (true || active.IsEmpty())'),
            ('stereo placement admitted', 'overlay->m_canPosition && !overlay->m_discMenuOverlay && !stereo', 'overlay->m_canPosition && !overlay->m_discMenuOverlay && (stereo || !stereo)'),
        ]:
            assert old in code
            run(code.replace(old, new), negative=True)
            print('REJECTED:', label)


if __name__ == '__main__':
    main()
