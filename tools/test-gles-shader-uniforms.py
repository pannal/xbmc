#!/usr/bin/env python3
"""Exercise production OnEnabled with real member declarations and GL recorders.

Checks active state resets, absent-uniform work, and clipping across all uniform
presence masks. No GL driver, compiled shader or device rendering is simulated.
"""
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']

PRELUDE = r'''
#include <array>
#include <cassert>
#include <cmath>
#include <string>
#include <utility>
#include <vector>
using GLint=int; using GLfloat=float;
constexpr int GL_FALSE=0;
#include "GLESShader.h"
struct Call {int location;std::vector<float> values;};
std::vector<Call> calls;
int ignored=0, peakReads=0;
void glUniform1f(int location,float value)
{if(location<0){++ignored;return;}calls.push_back({location,{value}});}
void glUniformMatrix4fv(int location,int count,int transpose,const float* value)
{assert(count==1&&transpose==GL_FALSE);if(location<0){++ignored;return;}
 calls.push_back({location,std::vector<float>(value,value+16)});}
struct Matrix {std::array<float,16> values{};const float* Get(){return values.data();}};
Matrix glMatrixProject,glMatrixModview;
struct TransformMatrix {float m[3][4]{};};
struct CRect {float x1=0,y1=0,x2=3840,y2=2160;};
struct Gfx {TransformMatrix matrix;const TransformMatrix& GetGUIMatrix(){return matrix;}};
struct Win {Gfx gfx;float peak=1.0f;Gfx& GetGfxContext(){return gfx;}
 float GetGuiSdrPeakLuminance(){++peakReads;return peak;}} win;
struct Render {void GetViewPort(CRect& rect){rect=CRect{};}} render;
struct CServiceBroker {static Win* GetWinSystem(){return &win;}
 static Render* GetRenderSystem(){return &render;}};
CGLESShader::CGLESShader(const char*,const std::string&){}
CGLESShader::CGLESShader(const char*,const char*,const std::string&){}
void CGLESShader::OnCompiledAndLinked(){}
void CGLESShader::Free(){}
struct Subject:CGLESShader
{
 Subject():CGLESShader("",""){}
 std::array<GLint*,12> locations(){return {&m_hProj,&m_hModel,&m_hBrightness,
 &m_hContrast,&m_hPma,&m_hSdrBrightness,&m_hSdrSaturation,&m_hSubtitlePeak,
 &m_hGuiTuning,&m_hGuiPeak,&m_hGuiSaturation,&m_sdrPeak};}
};
'''
TESTS = r'''
int main(int argc,char**)
{
 const bool requireNoUnused=argc>1;
 for(unsigned mask=0;mask<(1u<<12);++mask)
 {
  Subject shader;
  auto locations=shader.locations();
  for(unsigned i=0;i<locations.size();++i)*locations[i]=(mask&(1u<<i))?static_cast<int>(i):-1;
  for(int pass=0;pass<3;++pass)
  {
   calls.clear();ignored=0;peakReads=0;
   glMatrixProject.values.fill(0.0f);glMatrixModview.values.fill(0.0f);
   for(int i:{0,5,10,15})glMatrixModview.values[i]=1.0f;
   glMatrixProject.values[0]=2.0f;glMatrixProject.values[5]=3.0f;
   glMatrixProject.values[11]=1.0f;
   win.gfx.matrix=TransformMatrix{};
   win.gfx.matrix.m[0][0]=1.0f;win.gfx.matrix.m[1][1]=1.0f;
   win.gfx.matrix.m[2][3]=1.0f;
   if(pass==2)win.gfx.matrix.m[0][1]=0.5f;
   win.peak=1.1f+pass*0.1f;
   std::pair<float,float> colour{0.7f+pass*0.2f,0.8f+pass*0.3f};
   shader.SetGuiColourAdjustment(colour);
   assert(shader.OnEnabled());
   assert(shader.HardwareClipIsPossible()==(pass!=2));
   if(pass!=2){assert(shader.GetClipXFactor()==3840.0f);assert(shader.GetClipYFactor()==-3240.0f);}
   else {assert(shader.GetClipXFactor()==0.0f);assert(shader.GetClipYFactor()==0.0f);}
   const std::array<float,10> expected{0.0f,1.0f,0.0f,1.0f,1.0f,1.0f,1.0f,colour.first,colour.second,win.peak};
   unsigned n=0;
   for(unsigned i=0;i<12;++i)
   {
    if(!(mask&(1u<<i)))continue;
    assert(n<calls.size());const auto& call=calls[n++];
    assert(call.location==static_cast<int>(i));
    if(i<2){const auto& matrix=i==0?glMatrixProject.values:glMatrixModview.values;
      assert(call.values==std::vector<float>(matrix.begin(),matrix.end()));}
    else {assert(call.values.size()==1&&call.values[0]==expected[i-2]);}
   }
   assert(n==calls.size());
   if(requireNoUnused){assert(ignored==0);assert(peakReads==((mask&(1u<<11))?1:0));}
  }
 }
}
'''


def main():
    current = (ROOT / 'xbmc/rendering/gles/GLESShader.cpp').read_text()
    body = function(current, 'bool CGLESShader::OnEnabled()')
    with tempfile.TemporaryDirectory(prefix='gles-uniforms-') as temporary:
        out = Path(temporary)
        (out / 'guilib').mkdir()
        (out / 'guilib/Shader.h').write_text('namespace Shaders { struct CGLSLShaderProgram { '
            'virtual ~CGLSLShaderProgram()=default; virtual void OnCompiledAndLinked(){} '
            'virtual bool OnEnabled(){return true;} }; }')
        def run(method, name, performance=True, success=True):
            cpp = out / (name + '.cpp')
            binary = out / name
            cpp.write_text(PRELUDE + method + TESTS)
            subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                '-Werror', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                '-fno-pie', '-no-pie', '-I' + str(out),
                '-I' + str(ROOT / 'xbmc/rendering/gles'), str(cpp), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary)] + (['no-unused'] if performance else []),
                capture_output=True, text=True, env={**os.environ, 'ASAN_OPTIONS':'detect_leaks=0'})
            assert (result.returncode == 0) == success, result.stdout + result.stderr
            if not success:
                assert 'Assertion' in result.stderr, result.stderr
        run(body, 'production')
        # Restore eager calls: active output must match, unnecessary work must fail.
        import re
        eager = re.sub(r'  if \(m_\w+ >= 0\)\n    (glUniform[^\n]+)', r'  \1', body)
        assert eager != body
        run(eager, 'eager-semantics', performance=False)
        run(eager, 'eager-work', success=False)
        for field in ('m_hPma', 'm_hSubtitlePeak', 'm_hGuiPeak', 'm_sdrPeak'):
            mutant = body.replace('if (' + field + ' >= 0)', 'if (false)')
            assert mutant != body
            run(mutant, 'missing-' + field, success=False)
    print('GLES OnEnabled: PASS (4096 location masks, repeated binds/colour changes, '
          'active defaults and matrices, clipping, no inactive calls/settings reads; '
          'eager and missing-reset controls rejected; ASan/UBSan)')


if __name__ == '__main__':
    main()
