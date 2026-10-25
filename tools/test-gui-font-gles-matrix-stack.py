#!/usr/bin/env python3
"""Run actual GLES font submission against the actual modelview stack.

The pinned d49327ba12 baseline and an old-Pop semantic control must corrupt an
external owner's saved modelview. Current LastEnd must preserve both text-draw
transforms and caller-owned stacks. GL/window/font buffers are host substitutes;
this does not execute a GUI, driver, device or physical presentation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'd49327ba12d6b654951d4f4d9fc0a003865affd6'
FONT = 'xbmc/guilib/GUIFontTTFGLES.cpp'


def function(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda match: ' ' * len(match.group()), source)
    opening = masked.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


def old_pop_control(last_end):
    """Reinsert the old unmatched Pop at the end of the translated-entry loop."""
    loop = function(last_end, 'for (size_t i = 0; i < m_vertexTrans.size(); i++)')
    assert 'glMatrixModview.Pop()' not in loop, 'candidate still consumes an unowned stack'
    insertion = loop.rfind('}')
    altered = loop[:insertion] + '  glMatrixModview.Pop();\n    ' + loop[insertion:]
    return last_end.replace(loop, altered, 1)


PRELUDE = r'''
#include "rendering/MatrixGL.h"
#include "utils/Geometry.h"
#include <algorithm>
#include <array>
#include <vector>
#include <memory>
#include <cstdio>
#include <cmath>
#include <cassert>
#include <cstddef>
#include <string>
using GLvoid=void;using GLsizei=int;
constexpr int GL_ELEMENT_ARRAY_BUFFER=1,GL_ARRAY_BUFFER=2,GL_FALSE=0,GL_TRUE=1;
constexpr int GL_FLOAT=3,GL_UNSIGNED_BYTE=4,GL_TRIANGLES=5,GL_UNSIGNED_SHORT=6;
constexpr int GL_SCISSOR_TEST=7,GL_TEXTURE0=8,GL_BLEND=9,GL_DEPTH_BUFFER_BIT=10;
constexpr std::size_t ELEMENT_ARRAY_MAX_CHAR_INDEX=1024;
using Matrix=std::array<float,16>;
static std::vector<Matrix> matrixUploads,drawMatrices;
static std::vector<std::size_t> drawIndices;
static std::vector<std::array<float,4>> shaderClips;
static Matrix currentUniform{};
void glEnableVertexAttribArray(int){}void glDisableVertexAttribArray(int){}
void glBindBuffer(int,unsigned){}void glUniform1f(int,float){}
void glVertexAttribPointer(int,int,int,int,std::size_t,const void*){}
void glDisable(int){}void glEnable(int){}void glActiveTexture(int){}void glClear(int){}
void glUniform4fv(int location,int,const float* value)
{
  if(location==4)shaderClips.push_back({value[0],value[1],value[2],value[3]});
}
void glUniformMatrix4fv(int,int,int,const float* value)
{
  std::copy(value,value+16,currentUniform.begin());matrixUploads.push_back(currentUniform);
}
void glDrawElements(int,std::size_t count,int,const void*)
{
  drawMatrices.push_back(currentUniform);drawIndices.push_back(count);
}
struct TransformMatrix{float m[3][4]{{1,0,0,0},{0,1,0,0},{0,0,1,0}};};
struct CGraphicContext
{
  TransformMatrix gui;
  CRect StereoCorrection(const CRect&r){return r;}
  CRect GetScissors(){return {0,0,2000,2000};}
  float GetGUIScaleX(){return 1;}float GetGUIScaleY(){return 1;}
  float ScaleFinalXCoord(float x,float){return x;}
  float ScaleFinalYCoord(float,float y){return y;}
  const TransformMatrix& GetGUIMatrix(){return gui;}
  float GetTransformDepth(){return .5f;}
};
struct CWinSystemBase
{
  CGraphicContext gfx;CGraphicContext&GetGfxContext(){return gfx;}
};
struct CRenderSystemBase{virtual~CRenderSystemBase()=default;};
struct CRenderSystemGLES:CRenderSystemBase
{
  bool m_bRenderCreated=true,hardwareClip=true;
  CRect lastScissor;
  int GUIShaderGetPos(){return 1;}int GUIShaderGetCol(){return 2;}
  int GUIShaderGetCoord0(){return 3;}int GUIShaderGetClip(){return 4;}
  int GUIShaderGetCoordStep(){return 5;}int GUIShaderGetMatrix(){return 6;}
  int GUIShaderGetDepth(){return 7;}
  CRect ClipRectToScissorRect(const CRect&r){return hardwareClip?r:CRect();}
  void SetScissors(const CRect&r){lastScissor=r;}void ResetScissors(){}
  void DisableGUIShader(){}void CaptureStateBlock();void ApplyStateBlock();
};
static CWinSystemBase window;static CRenderSystemGLES renderer;
struct CServiceBroker
{
  static CWinSystemBase*GetWinSystem(){return &window;}
  static CRenderSystemBase*GetRenderSystem(){return &renderer;}
};
struct VertexBuffer{unsigned bufferHandle=1;std::size_t size=1;};
struct Translation
{
  VertexBuffer*m_vertexBuffer;CRect m_clip{0,0,2000,2000};
  float m_translateX=0,m_translateY=0,m_offsetX=0,m_offsetY=0;
};
struct SVertex{float x,y,z;unsigned char r,g,b,a;float u,v;};
class CGUIFontTTFGLES
{
public:
  void LastEnd();void CreateStaticVertexBuffers(){}
  std::vector<int>m_vertex;std::vector<Translation>m_vertexTrans;
  unsigned m_elementArrayHandle=1;bool m_scissorClip=true;
  unsigned m_textureWidth=1024,m_textureHeight=1024;
};
CMatrixGLStack glMatrixModview,glMatrixProject,glMatrixTexture;
'''

MAIN = r'''
static int failures=0,checks=0,cases=0;
static void Check(bool yes,const std::string& why)
{
  ++checks;if(!yes){++failures;if(failures<=24)std::fprintf(stderr,"FAIL: %s\n",why.c_str());}
}
static CMatrixGL Translated(float x,float y)
{
  CMatrixGL m;m.LoadIdentity();m.Translatef(x,y,0);return m;
}
static Matrix Copy(const CMatrixGL&m)
{
  Matrix v;const float*p=m;std::copy(p,p+16,v.begin());return v;
}
static bool Equal(const CMatrixGL&m,const CMatrixGL&n){return Copy(m)==Copy(n);}
static void ClearObservations()
{
  matrixUploads.clear();drawMatrices.clear();drawIndices.clear();shaderClips.clear();
}
static void Prepare()
{
  glMatrixModview.Clear();glMatrixProject.Clear();glMatrixTexture.Clear();
  glMatrixModview->LoadIdentity();glMatrixProject->LoadIdentity();glMatrixTexture->LoadIdentity();
  renderer.hardwareClip=true;ClearObservations();
}
static void Run(const std::string& name,int owners,bool shaderClip,int mode,bool large,bool fractional)
{
  ++cases;Prepare();
  const CMatrixGL outer=Translated(100,200),middle=Translated(40,50),caller=Translated(20,30);
  if(owners)
  {
    glMatrixModview.Get()=outer;renderer.CaptureStateBlock();
    if(owners==2){glMatrixModview.Get()=middle;renderer.CaptureStateBlock();}
  }
  glMatrixModview.Get()=caller;
  VertexBuffer one,two,zero;two.bufferHandle=2;zero.bufferHandle=0;
  if(large)one.size=2*ELEMENT_ARRAY_MAX_CHAR_INDEX+3;
  CGUIFontTTFGLES font;font.m_scissorClip=!shaderClip;renderer.hardwareClip=!shaderClip;
  const float x1=fractional?3.25f:3.0f,y1=fractional?4.75f:4.0f;
  if(mode!=1) // empty entries
  {
    const CRect clip=mode==2?CRect(3000,3000,4000,4000):CRect(0,0,2000,2000);
    font.m_vertexTrans.push_back({&one,clip,x1,y1,.5f,.25f});
    if(mode==3) // ignored buffer and clipped entry between two active entries
    {
      font.m_vertexTrans.push_back({&zero,{0,0,2000,2000},0,0,0,0});
      font.m_vertexTrans.push_back({&two,{3000,3000,4000,4000},0,0,0,0});
    }
    font.m_vertexTrans.push_back({&two,clip,7,9,0,0});
  }
  std::vector<Matrix> expectedUploads,expectedDraws;
  std::vector<std::size_t> expectedIndices;
  std::vector<std::array<float,4>> expectedClips;
  // Closed-form translations independent of LastEnd's matrix-composition walk.
  for(const auto& entry:font.m_vertexTrans)
  {
    if(!entry.m_vertexBuffer->bufferHandle)continue;
    CRect clip=entry.m_clip;clip.Intersect(CRect(0,0,2000,2000));
    if(!shaderClip&&clip.IsEmpty())continue;
    const Matrix expected=Copy(Translated(20+entry.m_offsetX+std::round(entry.m_translateX),
                                        30+entry.m_offsetY+std::round(entry.m_translateY)));
    expectedUploads.push_back(expected);
    if(shaderClip)
      expectedClips.push_back({entry.m_clip.x1-entry.m_translateX-entry.m_offsetX,
                              entry.m_clip.y1-entry.m_translateY-entry.m_offsetY,
                              entry.m_clip.x2-entry.m_translateX-entry.m_offsetX,
                              entry.m_clip.y2-entry.m_translateY-entry.m_offsetY});
    for(std::size_t i=0;i<entry.m_vertexBuffer->size;i+=ELEMENT_ARRAY_MAX_CHAR_INDEX)
    {
      expectedDraws.push_back(expected);
      expectedIndices.push_back(6*std::min(entry.m_vertexBuffer->size-i,ELEMENT_ARRAY_MAX_CHAR_INDEX));
    }
  }
  for(int repeat=0;repeat<3;++repeat)
  {
    ClearObservations();font.LastEnd();
    Check(matrixUploads==expectedUploads,name+": text entries preserve caller modelview");
    Check(drawMatrices==expectedDraws,name+": every glyph chunk uses its entry transform");
    Check(drawIndices==expectedIndices,name+": glyph chunk/index counts unchanged");
    Check(shaderClips==expectedClips,name+": shader clip coordinates unchanged");
    Check(Equal(glMatrixModview.Get(),caller),name+": caller current modelview unchanged");
    if(!shaderClip&&!font.m_vertexTrans.empty())
      Check(renderer.lastScissor==CRect(0,0,2000,2000),name+": caller scissor restored");
  }
  if(owners)
  {
    // A later caller mutation makes consumed saved-stack depth observable.
    glMatrixModview.Get()=Translated(777,888);renderer.ApplyStateBlock();
    Check(Equal(glMatrixModview.Get(),owners==2?middle:outer),name+": caller saved modelview retained");
    if(owners==2)
    {
      glMatrixModview.Get()=Translated(999,666);renderer.ApplyStateBlock();
      Check(Equal(glMatrixModview.Get(),outer),name+": nested owner saved modelview retained");
    }
  }
  else
  {
    // An empty stack must remain empty after text submission.
    glMatrixModview.Get()=Translated(777,888);glMatrixModview.Pop();
    Check(Equal(glMatrixModview.Get(),Translated(777,888)),name+": no owner stack invented");
  }
}
int main()
{
  for(int owners:{0,1,2})for(bool shader:{false,true})
  {
    const std::string name="owners="+std::to_string(owners)+(shader?" shader":" scissor");
    Run(name,owners,shader,0,false,false);
    Run(name+" fractional",owners,shader,0,false,true);
    Run(name+" large",owners,shader,0,true,false);
    Run(name+" empty",owners,shader,1,false,false);
    Run(name+" skipped",owners,shader,3,false,false);
    if(!shader)Run(name+" all-clipped",owners,false,2,false,false);
  }
  std::printf("%s: %d cases, %d checks; actual LastEnd/MatrixGL stack and Capture/Apply; "
              "owned/no-owner/nested, empty/clipped, shader/scissor, glyph chunks\n",
              failures?"FAIL":"PASS",cases,checks);
  return failures?1:0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, default=Path('/tmp/gui-font-gles-matrix-stack'))
    parser.add_argument('--sanitize', action='store_true', help='enable AddressSanitizer and UBSan')
    args = parser.parse_args()
    root = args.root.resolve()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    include = out / 'include'
    include.mkdir(exist_ok=True)
    # MatrixGL.h itself is real. Only its platform GL scalar typedefs are substituted.
    (include / 'system_gl.h').write_text('#pragma once\nusing GLfloat=float;using GLint=int;\n')
    inputs = {path: (root / path).read_text() for path in [
        FONT, 'xbmc/guilib/GUIFontTTF.cpp', 'xbmc/guilib/GUIFontTTFGL.cpp',
        'xbmc/rendering/MatrixGL.cpp', 'xbmc/rendering/MatrixGL.h',
        'xbmc/rendering/gles/RenderSystemGLES.cpp']}
    last = function(inputs[FONT], 'void CGUIFontTTFGLES::LastEnd()')
    for path, signature in [
        (FONT, 'bool CGUIFontTTFGLES::FirstBegin()'),
        ('xbmc/guilib/GUIFontTTF.cpp', 'void CGUIFontTTF::Begin()'),
        ('xbmc/guilib/GUIFontTTF.cpp', 'void CGUIFontTTF::End()'),
        ('xbmc/guilib/GUIFontTTF.cpp', 'void CGUIFontTTF::DrawTextInternal(')]:
        assert 'glMatrixModview.Push' not in function(inputs[path], signature), signature
    desktop = function(inputs['xbmc/guilib/GUIFontTTFGL.cpp'], 'void CGUIFontTTFGL::LastEnd()')
    assert 'glMatrixModview.Pop' not in desktop, 'desktop stack ownership changed; revisit fixture'
    baseline_text = subprocess.run(
        ['git', 'show', BASELINE + ':' + FONT], cwd=root, check=True,
        capture_output=True, text=True).stdout
    baseline = function(baseline_text, 'void CGUIFontTTFGLES::LastEnd()')
    assert baseline.count('glMatrixModview.Pop();') == 1, 'pinned baseline is not the old defect'
    methods = [function(inputs['xbmc/rendering/MatrixGL.cpp'], signature) for signature in [
        'CMatrixGL::CMatrixGL(const TransformMatrix &src)', 'void CMatrixGL::LoadIdentity()',
        'void CMatrixGL::Translatef(', 'void CMatrixGL::Scalef(',
        'void CMatrixGL::MultMatrixf(', 'void CMatrixGLStack::Load()']]
    methods += [function(inputs['xbmc/rendering/gles/RenderSystemGLES.cpp'], signature)
                for signature in ['void CRenderSystemGLES::CaptureStateBlock()',
                                  'void CRenderSystemGLES::ApplyStateBlock()']]
    variants = [('current', last, 0), ('baseline-' + BASELINE[:10], baseline, 1),
                ('mutant-old-pop', old_pop_control(last), 1)]
    receipts = {}
    for name, body, expected in variants:
        source = '\n'.join([PRELUDE, *methods, body, MAIN])
        cpp = out / (name + '.cpp')
        cpp.write_text(source)
        exe = out / name
        command = ['g++', '-std=c++17', '-O1', '-Wall', '-Wextra', '-Werror',
                   '-Wno-unused-parameter']
        if args.sanitize:
            command += ['-fsanitize=address,undefined', '-fno-sanitize-recover=undefined']
        command += ['-I', str(include), '-I', str(root / 'xbmc'), str(cpp), '-o', str(exe)]
        compiled = subprocess.run(command, capture_output=True, text=True, timeout=120)
        assert compiled.returncode == 0, (name, compiled.stderr)
        env = os.environ.copy()
        env.setdefault('ASAN_OPTIONS', 'detect_leaks=0')
        result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, env=env)
        sanitized = any(s in result.stderr for s in ('AddressSanitizer', 'runtime error:'))
        assert result.returncode == expected and not sanitized, (
            name, result.returncode, result.stdout, result.stderr)
        if expected:
            assert ('FAIL: owners=1 scissor: text entries preserve caller modelview' in result.stderr and
                    'caller saved modelview retained' in result.stderr), (name, result.stderr)
        else:
            assert not result.stderr and result.stdout.startswith('PASS:'), result.stderr
        receipts[name] = {'exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr,
                          'compile_command': command}
        print(('Accepted ' if expected == 0 else 'Rejected ') + name + ': ' + result.stdout.strip(),
              flush=True)
    report = {'baseline': BASELINE, 'sanitize': args.sanitize,
              'source_sha256': {path: hashlib.sha256(text.encode()).hexdigest()
                                for path, text in inputs.items()}, 'variants': receipts}
    (out / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print('GUI font GLES matrix stack: PASS; baseline and old-Pop control fail semantically; '
          'host fixtures, no GUI/device/presentation verification')


if __name__ == '__main__':
    main()
