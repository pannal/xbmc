#!/usr/bin/env python3
"""Production SDR bitmap provenance, preparation snapshots and draw uniform routing.

Uses production constructors/conversion and shader bind functions with recording
GL/settings services. Shader pixels are covered by test-bitmap-subtitle-colour-shader.py.
No Kodi GUI or device acceptance is implied.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
resource = runpy.run_path(str(ROOT / 'tools/test-overlay-resource-inputs.py'))
geometry = runpy.run_path(str(ROOT / 'tools/test-overlay-render-geometry.py'))
function = geometry['function']


def snapshot_source():
    code = geometry['harness']()
    code = code.replace('int position=0,aspect=0;', 'int brightness=100,saturation=100,brightnessReads=0,saturationReads=0;\n  int sdrPeak=100,hdrPeak=100,hdrBrightness=100,hdrSaturation=100,hdrMode=0;bool hdrTonemap=false,correction=true;int colourReads[6]{};\n  int position=0,aspect=0;')
    code = code.replace('int GetInt(int id){', 'int GetInt(int id){if(id==5){++brightnessReads;return brightness;}if(id==6){++saturationReads;return saturation;}if(id>=7&&id<=12){++colourReads[id-7];switch(id){case 7:return sdrPeak;case 8:return hdrPeak;case 9:return hdrBrightness;case 10:return hdrSaturation;case 12:return hdrMode;}}')
    code = code.replace('SETTING_SUBTITLES_BITMAPMARGIN=4;', 'SETTING_SUBTITLES_BITMAPMARGIN=4,SETTING_SUBTITLES_BITMAPSDRBRIGHTNESS=5,SETTING_SUBTITLES_BITMAPSDRSATURATION=6,SETTING_SUBTITLES_BITMAPSDRPEAK=7,SETTING_SUBTITLES_PGSHDRTOSDR_PEAK=8,SETTING_SUBTITLES_PGSHDRTOSDR_BRIGHTNESS=9,SETTING_SUBTITLES_PGSHDRTOSDR_SATURATION=10,SETTING_SUBTITLES_PGSHDRTOSDR_TONEMAP=11,SETTING_SUBTITLES_PGSHDRTOSDR_MODE=12,SETTING_SUBTITLES_PGSHDRTOSDR=13;')
    code = code.replace('  double GetNumber(int)', '  bool GetBool(int id){if(id==13)return correction;++colourReads[4];return hdrTonemap;}\n  double GetNumber(int)')
    return code + r'''
int main(){
 CRenderer renderer;
 auto producer=std::make_shared<CDVDOverlayImage>();producer->m_canPosition=true;
 renderer.converted=std::make_shared<COverlay>();renderer.converted->m_isBitmapOverlay=true;
 renderer.converted->m_canPosition=true;
 CRenderer::OverlayBatch batch{{1,producer},{1,producer}};
 services.brightness=50;services.saturation=0;services.sdrPeak=25;services.hdrPeak=50;
 services.hdrBrightness=150;services.hdrSaturation=175;services.hdrTonemap=true;services.hdrMode=2;
 auto old=renderer.PrepareRenderItems(batch);
 assert(old.size()==2&&services.brightnessReads==1&&services.saturationReads==1);
 for(const auto& item:old)assert(item.state.sdrBrightness==0.5f&&item.state.sdrSaturation==0.0f);
 for(int reads:services.colourReads)assert(reads==1);
 for(const auto& item:old){const auto& c=item.state;assert(c.bitmapColourPrepared);
  assert(std::abs(c.sdrOutputPeak-std::pow(0.25f,1.0f/2.2f))<1e-6f);
  assert(std::abs(c.hdrOutputPeak-std::pow(0.5f,1.0f/2.3f))<1e-6f);
  assert(c.pqRefNits==20300.0f/150.0f&&c.pqSaturation==1.75f&&c.pqTonemap==1&&c.pqMode==2);}
 services.sdrPeak=50;services.hdrPeak=25;services.hdrBrightness=200;services.hdrSaturation=150;services.hdrTonemap=false;services.hdrMode=1;
 auto cached=old[0].overlay;services.brightness=150;services.saturation=200;
 auto fresh=renderer.PrepareRenderItems(batch);
 assert(fresh[0].overlay==cached&&fresh[1].overlay==cached);
 for(const auto& item:fresh)assert(item.state.sdrBrightness==1.5f&&item.state.sdrSaturation==2.0f);
 for(int reads:services.colourReads)assert(reads==2);
 for(const auto& item:fresh){const auto& c=item.state;
  assert(std::abs(c.sdrOutputPeak-std::pow(0.5f,1.0f/2.2f))<1e-6f);
  assert(std::abs(c.hdrOutputPeak-std::pow(0.25f,1.0f/2.3f))<1e-6f);
  assert(c.pqRefNits==101.5f&&c.pqSaturation==1.5f&&c.pqTonemap==0&&c.pqMode==1);}
 // Previously retained states cannot acquire a later slider value.
 for(const auto& item:old)assert(item.state.sdrBrightness==0.5f&&item.state.sdrSaturation==0.0f);
 int reads=services.brightnessReads;renderer.PrepareRenderItems({});assert(services.brightnessReads==reads);
 services.brightness=services.saturation=services.sdrPeak=services.hdrPeak=100;
 auto neutral=renderer.PrepareRenderItems(batch);
 assert(neutral[0].state.sdrBrightness==1.0f&&neutral[0].state.sdrSaturation==1.0f);
 assert(neutral[0].state.sdrOutputPeak==1&&neutral[0].state.hdrOutputPeak==1);
 for(const auto& item:old)assert(item.state.pqRefNits==20300.0f/150.0f&&item.state.pqTonemap==1);
 services.correction=false;services.hdrPeak=0;
 auto disabled=renderer.PrepareRenderItems(batch);assert(disabled[0].state.hdrOutputPeak==1);
 SRenderState direct{};assert(direct.sdrOutputPeak==1&&direct.hdrOutputPeak==1&&!direct.bitmapColourPrepared);assert(direct.sdrBrightness==1.0f&&direct.sdrSaturation==1.0f);
}
'''


def draw_source():
    code = resource['harness']()
    assert code.endswith(resource['TESTS'])
    code = code[:-len(resource['TESTS'])]
    renderer = (ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/OverlayRendererGLES.cpp').read_text()
    shader = (ROOT / 'xbmc/rendering/gles/GLESShader.cpp').read_text()
    header = (ROOT / 'xbmc/rendering/gles/GLESShader.h').read_text()
    fields = header[header.index('  GLint m_hTex0'):header.index('\n};')]
    extra = r'''
#include <map>
#include <string>
struct Matrix {std::array<float,16> a{1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1};const float* Get(){return a.data();}};
Matrix glMatrixProject,glMatrixModview;
struct TransformMatrix {float m[4][4]{{1,0,0,0},{0,1,0,0},{0,0,1,0},{0,0,0,1}};};
struct Settings {
 int GetInt(int id){if(id==1)return 100;if(id==2)return 150;if(id==3)return 175;return 0;}
 bool GetBool(int){return false;}
 Settings* GetSettings(){return this;}
} settings;
struct CSettings {static constexpr int SETTING_SUBTITLES_BITMAPZOOM=1,
 SETTING_SUBTITLES_PGSHDRTOSDR_BRIGHTNESS=2,SETTING_SUBTITLES_PGSHDRTOSDR_SATURATION=3,
 SETTING_SUBTITLES_PGSHDRTOSDR_TONEMAP=4,SETTING_SUBTITLES_PGSHDRTOSDR_MODE=5;};
struct Window {
 TransformMatrix matrix;Window& GetGfxContext(){return *this;}
 const TransformMatrix& GetGUIMatrix(){return matrix;}
 float GetGuiSdrPeakLuminance(){return 0.75f;}
} window;
std::map<std::string,int> locations;std::map<int,float> uniforms;
int glGetUniformLocation(int,const char* name){auto [it,inserted]=locations.emplace(name,locations.size()+1);(void)inserted;return it->second;}
int glGetAttribLocation(int,const char*){return 0;}
void glUseProgram(int){}void glUniform1i(int,int){}void glUniform4f(int,float,float,float,float){}
void glUniformMatrix4fv(int,int,int,const float*){}
void glUniform1f(int where,float value){uniforms[where]=value;}
struct CGLESShader {
 @SHADER_FIELDS@
 int ProgramHandle(){return 9;}void OnCompiledAndLinked();bool OnEnabled();
} guiShader;
enum class ShaderMethodGLES {SM_TEXTURE_NOBLEND,SM_TEXTURE_NOBLEND_PQ_TO_SDR};
constexpr int GL_BLEND=20,GL_ONE=21,GL_ONE_MINUS_SRC_ALPHA=22,GL_SRC_ALPHA=23,
 GL_CLAMP_TO_EDGE=24,GL_NEAREST=25,GL_LINEAR_MIPMAP_LINEAR=26,GL_CURRENT_PROGRAM=27,
 GL_FLOAT=28,GL_TRIANGLE_STRIP=29,GL_UNSIGNED_BYTE=30,GL_FALSE=0;
int draws=0;ShaderMethodGLES currentShader=ShaderMethodGLES::SM_TEXTURE_NOBLEND;
void glEnable(int){}void glDisable(int){}void glBlendFuncSeparate(int,int,int,int){}
void glVertexAttribPointer(int,int,int,int,int,const void*){}
void glEnableVertexAttribArray(int){}void glDisableVertexAttribArray(int){}
void glDrawElements(int,int,int,const void*){++draws;}
'''.replace('@SHADER_FIELDS@', fields)
    code = code.replace('struct CRenderSystemBase', extra + '\nstruct CRenderSystemBase', 1)
    code = code.replace(' bool current=true,ready=true;', r'''
 void EnableGUIShader(ShaderMethodGLES method){currentShader=method;assert(guiShader.OnEnabled());}
 void DisableGUIShader(){}
 void GetViewPort(CRect& r){r=CRect(0,0,1920,1080);}
 int GUIShaderGetPos(){return 0;}int GUIShaderGetCoord0(){return 1;}int GUIShaderGetDepth(){return 2;}
 int GUIShaderGetPma(){return guiShader.m_hPma;}
 int GUIShaderGetSdrBrightness(){return guiShader.m_hSdrBrightness;}
 int GUIShaderGetSdrSaturation(){return guiShader.m_hSdrSaturation;}
 int GUIShaderGetSubtitlePeak(){return guiShader.m_hSubtitlePeak;}
 bool current=true,ready=true;''')
    code = code.replace('static CRenderSystemBase* GetRenderSystem()', 'static Window* GetWinSystem(){return &window;}static Settings* GetSettingsComponent(){return &settings;}\n static CRenderSystemGLES* GetRenderSystem()')
    state = function((ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/OverlayRenderer.h').read_text(), 'struct SRenderState') + ';'
    code = code.replace('namespace OVERLAY {\n@QUADS@', 'namespace OVERLAY {\n' + state + '\n@QUADS@') if '@QUADS@' in code else code.replace('namespace OVERLAY {\nstruct SQuad', 'namespace OVERLAY {\n' + state + '\nstruct SQuad')
    # Actual source fixture expands SQuad without relying on formatting.
    if state not in code:
        code = code.replace('namespace OVERLAY {', 'namespace OVERLAY {\n' + state, 1)
    code = code.replace('bool m_rawPqMenu=false,', 'bool m_discMenuOverlay=false;\n bool m_rawPqMenu=false,', 1)
    code = code.replace('~COverlayTextureGLES();bool IsValid()const;', '~COverlayTextureGLES();bool IsValid()const;void Render(SRenderState&);')
    code += '\n' + function(shader, 'void CGLESShader::OnCompiledAndLinked()')
    code += '\n' + function(shader, 'bool CGLESShader::OnEnabled()')
    code += '\n' + function(renderer, 'void COverlayTextureGLES::Render(')
    codec = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayCodecFFmpeg.cpp').read_text()
    opened = function(codec, 'bool CDVDOverlayCodecFFmpeg::Open(')
    classify = opened[opened.index('  m_pgsHdrSource ='):opened.index('  // decoding')]
    pq = opened[opened.index('    m_pgsIsPqAuthored ='):opened.index('    const char* matrix')]
    publish = function(codec, 'std::shared_ptr<CDVDOverlay> CDVDOverlayCodecFFmpeg::GetOverlay()')
    provenance = publish[publish.index('    overlay->m_canPosition ='):publish.index('    overlay->iPTSStartTime')]
    code += r'''
constexpr int AV_CODEC_ID_HDMV_PGS_SUBTITLE=1,AV_CODEC_ID_DVD_SUBTITLE=2,
 AVCOL_PRI_BT2020=3,AVCOL_TRC_SMPTE2084=4;
struct Hints {int codec=1,colorPrimaries=0,colorTransferCharacteristic=0;struct {int dv_profile=0;}dovi;};
struct Classifier {
 bool m_pgsHdrSource=false,m_pgsIsPqAuthored=false;
 struct Context {int codec_id=1;}context;
 Context* m_pCodecContext=&context;
 void Open(const Hints& hints){context.codec_id=hints.codec;
 @CLASSIFY@
 // The setting read is a controlled input; the source condition remains production code.
 if(hints.codec==AV_CODEC_ID_HDMV_PGS_SUBTITLE){
 @PQ@
 }}
 std::shared_ptr<CDVDOverlayImage> Publish(){auto overlay=std::make_shared<CDVDOverlayImage>();
 @PROVENANCE@
 overlay->m_isHdrPq=m_pgsIsPqAuthored;overlay->m_isHdrPqSource=m_pgsHdrSource;return overlay;}
};
'''.replace('@CLASSIFY@', classify).replace('@PQ@', pq.replace('CSettings::SETTING_SUBTITLES_PGSHDRTOSDR', '0')).replace('@PROVENANCE@', provenance)
    assert 'overlay->m_isHdrPqSource = m_pgsHdrSource;' in publish
    return code + DRAW_TESTS


DRAW_TESTS = r'''
float value(const char* name){return uniforms.at(locations.at(name));}
int main(){
 guiShader.OnCompiledAndLinked();
 uniforms[guiShader.m_hSubtitlePeak]=0.3f;
 uniforms[guiShader.m_hSdrBrightness]=0.25f;uniforms[guiShader.m_hSdrSaturation]=0.4f;
 guiShader.OnEnabled();assert(value("m_subtitlePeak")==1);
 assert(value("m_sdrBrightness")==1&&value("m_sdrSaturation")==1);
 CDVDOverlayImage image;image.width=image.height=1;image.linesize=1;image.pixels={0};image.palette={0x80b45020};image.m_canPosition=true;
 CRect source(0,0,1920,1080);
 auto make=[&](const CDVDOverlayImage& o,bool raw=false){
  auto prepared=COverlayTextureGLES::PrepareImage(o,raw,renderSystem.CaptureRenderTarget());
  return std::make_unique<COverlayTextureGLES>(o,source,std::move(prepared));
 };
 auto sdr=make(image);assert(sdr->m_isSdrSubtitle);
 SRenderState state{0,0,1920,1080};state.sdrBrightness=0.5f;state.sdrSaturation=0;
 sdr->Render(state);assert(draws==1&&value("m_sdrBrightness")==0.5f&&value("m_sdrSaturation")==0);
 state.sdrOutputPeak=0.65f;sdr->Render(state);assert(value("m_subtitlePeak")==0.65f);
 auto sameTexture=sdr->m_texture;state.sdrBrightness=1.5f;state.sdrSaturation=2;
 sdr->Render(state);assert(sdr->m_texture==sameTexture&&value("m_sdrBrightness")==1.5f);
 // Every normal GUI bind and each following menu draw returns to neutral.
 guiShader.OnEnabled();assert(value("m_subtitlePeak")==1);assert(value("m_sdrBrightness")==1&&value("m_sdrSaturation")==1);
 CDVDOverlayImage menu=image;menu.SetDiscMenuOverlay(true);
 auto menuTexture=make(menu);assert(!menuTexture->m_isSdrSubtitle);
 sdr->Render(state);menuTexture->Render(state);assert(value("m_sdrBrightness")==1&&value("m_sdrSaturation")==1);
 auto unrelated=image;unrelated.m_canPosition=false;
 auto graphics=make(unrelated);assert(!graphics->m_isSdrSubtitle);
 sdr->Render(state);graphics->Render(state);assert(value("m_sdrBrightness")==1&&value("m_sdrSaturation")==1);
 auto hdr=image;hdr.m_isHdrPqSource=true;hdr.m_isHdrPq=true;
 auto hdrTexture=make(hdr);assert(!hdrTexture->m_isSdrSubtitle&&hdrTexture->m_isHdrSubtitle);
 hdrTexture->Render(state);assert(currentShader==ShaderMethodGLES::SM_TEXTURE_NOBLEND_PQ_TO_SDR);
 assert(value("m_pqRefNits")==20300.0f/150.0f&&value("m_pqSaturation")==1.75f);
 assert(value("m_sdrBrightness")==1&&value("m_sdrSaturation")==1);
 state.bitmapColourPrepared=true;state.pqRefNits=101.5f;state.pqSaturation=1.5f;state.pqTonemap=1;state.pqMode=2;state.hdrOutputPeak=0.7f;
 hdrTexture->Render(state);assert(value("m_subtitlePeak")==0.7f);
 assert(value("m_pqRefNits")==101.5f&&value("m_pqSaturation")==1.5f&&value("m_pqTonemap")==1&&value("m_pqMode")==2);
 auto hdrMenu=hdr;hdrMenu.SetDiscMenuOverlay(true);auto correctedMenu=make(hdrMenu);correctedMenu->m_discMenuOverlay=true;
 correctedMenu->Render(state);assert(value("m_subtitlePeak")==1&&value("m_pqRefNits")==20300.0f/150.0f&&value("m_pqSaturation")==1.75f);
 auto hdrOther=hdr;hdrOther.m_canPosition=false;auto otherCorrected=make(hdrOther);otherCorrected->Render(state);assert(value("m_subtitlePeak")==1);
 sdr->Render(state);menuTexture->Render(state);assert(value("m_subtitlePeak")==1);
 sdr->Render(state);graphics->Render(state);assert(value("m_subtitlePeak")==1);
 hdr.m_isHdrPq=false;auto noConversion=make(hdr);assert(!noConversion->m_isSdrSubtitle);
 noConversion->Render(state);assert(value("m_subtitlePeak")==1&&!noConversion->m_isHdrSubtitle);assert(currentShader==ShaderMethodGLES::SM_TEXTURE_NOBLEND&&value("m_sdrBrightness")==1);
 auto rawImage=image;rawImage.m_isPqMenuGraphics=true;
 auto raw=make(rawImage,true);assert(!raw->m_isSdrSubtitle&&raw->m_rawPqMenu);
 raw->Render(state);assert(value("m_sdrBrightness")==1);
 // Source classification persists through immutable publication and cut-out copies.
 auto published=image.GetPublishedRenderContent();auto copy=std::static_pointer_cast<const CDVDOverlayImage>(published);
 assert(copy->m_canPosition&&!copy->m_isHdrPqSource);
 CDVDOverlayImage cut(hdr,0,0,1,1);assert(cut.m_isHdrPqSource&&!cut.m_isHdrPq);
 auto cutTexture=make(cut);assert(!cutTexture->m_isSdrSubtitle);
 Classifier classifier;Hints hints;hints.dovi.dv_profile=7;classifier.Open(hints);
 auto dv=classifier.Publish();assert(dv->m_isHdrPqSource&&!dv->m_isHdrPq); // conversion disabled
 hints.dovi.dv_profile=0;hints.colorPrimaries=AVCOL_PRI_BT2020;hints.colorTransferCharacteristic=AVCOL_TRC_SMPTE2084;
 classifier.Open(hints);assert(classifier.Publish()->m_isHdrPqSource);
 hints.codec=AV_CODEC_ID_DVD_SUBTITLE;classifier.Open(hints);auto vob=classifier.Publish();
 assert(vob->m_canPosition&&!vob->m_isHdrPqSource&&!vob->m_isHdrPq);
 hints.codec=99;classifier.Open(hints);assert(!classifier.Publish()->m_canPosition&&!classifier.Publish()->m_isHdrPqSource);
}
'''


def run(code, negative=False, image_header=None):
    with tempfile.TemporaryDirectory(prefix='bitmap-colour-') as tmp:
        out = Path(tmp)
        (out / 'PlatformDefs.h').write_text('#pragma once\n#define PIXEL_ASHIFT 24\n#define PIXEL_RSHIFT 16\n#define PIXEL_GSHIFT 8\n#define PIXEL_BSHIFT 0\n')
        if image_header is not None:
            target = out / 'cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayImage.h'
            target.parent.mkdir(parents=True)
            target.write_text(image_header)
        (out / 'test.cpp').write_text(code)
        subprocess.run(['g++', '-std=c++17', '-DHAS_GLES=2', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-parameter', '-Wno-misleading-indentation',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
                        '-I', str(out), '-I', str(ROOT / 'xbmc'),
                        '-I', str(ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Overlay'), str(out / 'test.cpp'),
                        str(ROOT / 'xbmc/cores/VideoPlayer/VideoRenderers/BitmapSubtitlePosition.cpp'),
                        '-o', str(out / 'test')], check=True)
        r = subprocess.run([str(out / 'test')], capture_output=True, text=True, timeout=15,
                           env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
        if negative:
            assert r.returncode and 'Assertion' in r.stderr, r.stdout + r.stderr
        else:
            assert r.returncode == 0, r.stdout + r.stderr


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--negative-controls', action='store_true')
    args = p.parse_args()
    snapshots, draw = snapshot_source(), draw_source()
    run(snapshots); run(draw)
    print('PASS: production colour batch snapshots, cached cues, source/copy/provenance, uniform resets and HDR/menu routing')
    if args.negative_controls:
        for label, code, old, new in [
            ('disabled correction ceiling active', snapshots, 'hasBitmap && settings->GetBool(\n      CSettings::SETTING_SUBTITLES_PGSHDRTOSDR) ?', 'hasBitmap ?'),
            ('peak reset missing', draw, 'glUniform1f(m_hSubtitlePeak, 1.0f);', ''),
            ('SDR white snapshot discarded', snapshots, 'item.state.sdrOutputPeak = sdrOutputPeak;', 'item.state.sdrOutputPeak = 1.0f; (void)sdrOutputPeak;'),
            ('HDR ceiling snapshot discarded', snapshots, 'item.state.hdrOutputPeak = hdrOutputPeak;', 'item.state.hdrOutputPeak = 1.0f; (void)hdrOutputPeak;'),
            ('HDR snapshot ignored', draw, '!state.bitmapColourPrepared || m_discMenuOverlay', 'true'),
            ('HDR ceiling applied to menu', draw, 'm_isHdrSubtitle && !m_discMenuOverlay && !m_rawPqMenu', 'true'),
            ('snapshot discarded', snapshots, 'item.state.sdrBrightness = sdrBrightness;', 'item.state.sdrBrightness = 1.0f; (void)sdrBrightness;'),
            ('HDR conversion-disabled classified SDR', draw, '!o.m_isHdrPqSource &&', ''),
            ('menu eligible', draw, '!o.IsDiscMenuOverlay()', 'true'),
            ('brightness leaked to next GUI', draw, 'glUniform1f(m_hSdrBrightness, 1.0f);', ''),
            ('saturation leaked to next GUI', draw, 'glUniform1f(m_hSdrSaturation, 1.0f);', ''),
        ]:
            assert old in code, label
            run(code.replace(old, new, 1), True)
            print('REJECTED:', label)
        image = (ROOT / 'xbmc/cores/VideoPlayer/DVDCodecs/Overlay/DVDOverlayImage.h').read_text()
        run(draw, True, image.replace('m_isHdrPqSource = src.m_isHdrPqSource;', 'm_isHdrPqSource = false;'))
        print('REJECTED: source copy lost')


if __name__ == '__main__':
    main()
