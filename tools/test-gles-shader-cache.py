#!/usr/bin/env python3
"""Exercise production GUI shader selection, compilation requests and teardown.

The shader and window objects record program identity, defines, failed compilation
and namespace deletion. GL/Mali compilation costs and device output are untested.
Uses ASan/UBSan; --mutations also rejects broken cache/lifecycle implementations.
"""
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def main():
    header = (ROOT / 'xbmc/rendering/gles/RenderSystemGLES.h').read_text()
    gles = (ROOT / 'xbmc/rendering/gles/RenderSystemGLES.cpp').read_text()
    enum = re.search(r'enum class ShaderMethodGLES\s*\{.*?\};', header, re.S).group()
    fields = '\n'.join(re.search(pattern, header).group() for pattern in [
        r'using ShaderSet = [^\n]+;', r'ShaderSet m_pShader;',
        r'std::array<ShaderSet, 8> m_shaderVariants;'])
    methods = '\n'.join(function(gles, signature) for signature in [
        'bool CRenderSystemGLES::BeginRender()',
        'void CRenderSystemGLES::EnableGUIShader(',
        'void CRenderSystemGLES::InitialiseShaders()',
        'void CRenderSystemGLES::ReleaseShaderSet(',
        'void CRenderSystemGLES::ReleaseShaders('])
    init = function(gles, 'bool CRenderSystemGLES::InitRenderSystem()')
    # Bootstrap snapshots both effective flags before selecting a shader set.
    for flag in ('m_transferPQ', 'm_limitedColorRange'):
        assert init.index(flag + ' =') < init.index('InitialiseShaders();')
    source = PRELUDE.replace('@ENUM@', enum).replace('@FIELDS@', fields)
    with tempfile.TemporaryDirectory(prefix='gles-shader-cache-') as temporary:
        out = Path(temporary)

        def run(body, name, should_pass=True):
            cpp = out / (name + '.cpp')
            binary = out / name
            cpp.write_text(source + body + TESTS)
            subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                            '-Werror', '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                            '-fno-pie', '-no-pie',
                            str(cpp), '-o', str(binary)], check=True)
            result = subprocess.run([str(binary)], capture_output=True, text=True,
                                    env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
            if should_pass:
                assert result.returncode == 0, result.stdout + result.stderr
            else:
                assert result.returncode != 0 and 'Assertion' in result.stderr, result.stderr

        run(methods, 'production')
        print('GLES shader cache: PASS (four legacy/eight tuned-state identity/defines/reuse, stable frames, '
              'menu suppression, frame colour snapshots/forwarding, failed-set retries, '
              'release/abandon; ASan/UBSan)')
        if '--mutations' in sys.argv:
            mutations = {
                'no-cache-hit': ('if (!m_shaderVariants[variant].empty())', 'if (false)'),
                'cache-failures': ('return shader.second != nullptr;', '(void)shader; return true;'),
                'lose-range-key': ('(m_limitedColorRange ? 1 : 0)', '0'),
                'no-invalidation': ('InvalidateRenderTarget();', ''),
                'leak-inactive': ('ReleaseShaderSet(shaders, abandon);', '(void)shaders;'),
                'delete-abandoned': ('shader.second->Abandon();', '(void)shader;'),
                'no-colour-snapshot': ('m_guiColour = CServiceBroker::GetWinSystem()->GetGuiColourAdjustment();', ''),
                'no-colour-forwarding': ('m_pShader[m_method]->SetGuiColourAdjustment(m_guiColour);', ''),
                'live-colour-at-bind': ('SetGuiColourAdjustment(m_guiColour)',
                                       'SetGuiColourAdjustment(CServiceBroker::GetWinSystem()->GetGuiColourAdjustment())'),
                'lose-colour-key': ('(m_guiColourEnabled ? 4 : 0)', '0'),
            }
            for name, (old, new) in mutations.items():
                assert old in methods
                run(methods.replace(old, new), name, False)
                print('Rejected mutation:', name)


PRELUDE = r'''
#include <algorithm>
#include <array>
#include <cassert>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>
@ENUM@
constexpr int LOGERROR=1;
struct CLog { template<class... T> static void Log(int,const char*,T&&...){} };
int currentContext=1, nextProgram=0, compiles=0, deletions=0, abandons=0, failAt=0;
std::map<int,int> livePrograms;
struct Bind {int program;std::pair<float,float> colour;};
std::vector<Bind> binds;
struct CGLESShader
{
  std::string prefix;
  int program=0;
  std::pair<float,float> colour{1.0f,1.0f};
  void SetGuiColourAdjustment(std::pair<float,float> value){colour=value;}
  void Enable(){assert(currentContext && livePrograms.at(program)==currentContext);binds.push_back({program,colour});}
  CGLESShader(const char*,const std::string& p):prefix(p){}
  CGLESShader(const char*,const char*,const std::string& p):prefix(p){}
  ~CGLESShader(){Free();}
  bool CompileAndLink()
  {
    assert(currentContext);
    program=++nextProgram;livePrograms.emplace(program,currentContext);
    return ++compiles!=failAt;
  }
  void Free()
  {
    if(!program)return;
    assert(currentContext && livePrograms.at(program)==currentContext);
    ++deletions;livePrograms.erase(program);program=0;
  }
  void Abandon()
  {
    if(!program)return;
    ++abandons;livePrograms.erase(program);program=0;
  }
};
struct Gfx {bool pq=false;bool IsTransferPQ(){return pq;}};
struct Win
{
  bool menu=false,limited=false;Gfx gfx;
  std::pair<float,float> colour{1.0f,1.0f};int colourReads=0;
  bool IsMenuCompositeActive(){return menu;}
  bool UseLimitedColor(){return limited;}
  std::pair<float,float> GetGuiColourAdjustment(){++colourReads;return colour;}
  Gfx& GetGfxContext(){return gfx;}
} win;
struct CServiceBroker{static Win* GetWinSystem(){return &win;}};
struct CRenderSystemGLES
{
  bool m_limitedColorRange=false,m_transferPQ=false,renderable=true,oes=false;
  std::pair<float,float> m_guiColour{1.0f,1.0f};
  bool m_guiColourEnabled=false;
  ShaderMethodGLES m_method=ShaderMethodGLES::SM_DEFAULT;
  int generation=1;
  bool CanRender(){return renderable && currentContext;}
  bool IsExtSupported(const char*){return oes;}
  void DrainTextureResources(){}
  void InvalidateRenderTarget(){++generation;}
  bool BeginRender();void InitialiseShaders();void ReleaseShaders(bool abandon=false);
  void EnableGUIShader(ShaderMethodGLES);
  @FIELDS@
  static void ReleaseShaderSet(ShaderSet&,bool);
};
using Ids=std::map<ShaderMethodGLES,int>;
Ids programs(const CRenderSystemGLES& r)
{
  Ids ids;
  for(const auto& shader:r.m_pShader)
    ids[shader.first]=shader.second?shader.second->program:0;
  return ids;
}
void select(unsigned int variant)
{
  win.menu=false;win.limited=(variant&1)!=0;win.gfx.pq=(variant&2)!=0;
  win.colour=(variant&4)?std::pair<float,float>{0.65f,1.4f}:std::pair<float,float>{1.0f,1.0f};
}
void checkDefines(const CRenderSystemGLES& r,unsigned int variant)
{
  for(const auto& shader:r.m_pShader)
  {
    assert(shader.second);
    const auto& prefix=shader.second->prefix;
    if(shader.first==ShaderMethodGLES::SM_TEXTURE_NOBLEND_PQ_TO_SDR)
      assert(prefix=="#define KODI_PQ_TO_SDR 1\n");
    else
    {
      assert((prefix.find("KODI_LIMITED_RANGE")!=std::string::npos)==((variant&1)!=0));
      assert((prefix.find("KODI_TRANSFER_PQ")!=std::string::npos)==((variant&2)!=0));
      assert((prefix.find("KODI_GUI_COLOUR")!=std::string::npos)==((variant&4)!=0));
    }
  }
}
void checkBind(CRenderSystemGLES& r,ShaderMethodGLES method,std::pair<float,float> expected)
{
  const auto previous=binds.size();const int reads=win.colourReads;
  r.EnableGUIShader(method);
  assert(r.m_method==method && binds.size()==previous+1);
  assert(binds.back().program==r.m_pShader.at(method)->program);
  assert(binds.back().colour==expected && win.colourReads==reads);
}
void checkFrameColour(bool oes)
{
  CRenderSystemGLES r;r.oes=oes;select(0);r.InitialiseShaders();
  const int count=oes?15:13,start=compiles-count,deleted=deletions;
  std::array<Ids,8> expected;
  for(unsigned int v=0;v<8;++v)
  {
    select(v);const int reads=win.colourReads;assert(r.BeginRender());
    assert(win.colourReads==reads+1);
    expected[v]=programs(r);checkDefines(r,v);
  }
  assert(compiles-start==8*count);
  // A warm shader set takes one pair per frame and forwards that immutable
  // pair to every method, including binds after the source settings change.
  const std::pair<float,float> first{0.65f,1.4f},second{1.6f,0.25f};
  for(int cycle=0;cycle<20;++cycle)
    for(unsigned int v=0;v<8;++v)
    {
      select(v);const auto captured=win.colour;const int reads=win.colourReads;
      assert(r.BeginRender() && win.colourReads==reads+1);
      assert(programs(r)==expected[v]);checkDefines(r,v);
      for(const auto& shader:r.m_pShader)checkBind(r,shader.first,captured);
      win.colour=(v&4)?second:first;
      for(const auto& shader:r.m_pShader)checkBind(r,shader.first,captured);
      assert(r.BeginRender() && win.colourReads==reads+2);
      assert(programs(r)==expected[v|4]);checkDefines(r,v|4);
      for(const auto& shader:r.m_pShader)checkBind(r,shader.first,win.colour);
      assert(compiles-start==8*count && deletions==deleted);
    }
  r.renderable=false;const int blockedReads=win.colourReads;
  assert(!r.BeginRender() && win.colourReads==blockedReads);r.renderable=true;
  r.ReleaseShaders();assert(livePrograms.empty() && deletions-deleted==8*count);
}
'''

TESTS = r'''
int main()
{
  for(bool oes:{false,true})
  {
    CRenderSystemGLES r;r.oes=oes;select(0);r.InitialiseShaders();
    const int count=oes?15:13,start=compiles-count,deleted=deletions;
    std::array<Ids,4> expected;
    for(unsigned int v=0;v<4;++v)
    {
      const int oldGeneration=r.generation;
      select(v);assert(r.BeginRender());
      assert(r.generation==oldGeneration+(v!=0));
      assert(r.m_pShader.size()==static_cast<size_t>(count));
      expected[v]=programs(r);checkDefines(r,v);
    }
    assert(compiles-start==4*count);
    for(int cycle=0;cycle<100;++cycle)
      for(unsigned int v=0;v<4;++v)
      {
        select(v);assert(r.BeginRender());assert(programs(r)==expected[v]);
        checkDefines(r,v);
      }
    for(int frame=0;frame<1000;++frame)assert(r.BeginRender());
    assert(compiles-start==4*count && deletions==deleted);
    win.menu=true;assert(r.BeginRender());assert(programs(r)==expected[0]);
    win.menu=false;assert(r.BeginRender());assert(programs(r)==expected[3]);
    currentContext=0;const int generation=r.generation;
    const int lostReads=win.colourReads;
    assert(!r.BeginRender());assert(r.generation==generation && win.colourReads==lostReads);
    currentContext=1;r.ReleaseShaders();
    assert(livePrograms.empty() && deletions-deleted==4*count);
    assert(r.m_pShader.empty());for(const auto& v:r.m_shaderVariants)assert(v.empty());
    // A replacement context must compile new program identities.
    currentContext=2;select(3);r.InitialiseShaders();
    assert(compiles-start==5*count && programs(r)!=expected[3]);
    currentContext=0;const int before=deletions;r.ReleaseShaders(true);
    assert(deletions==before && livePrograms.empty());currentContext=1;
  }
  // Every mandatory/optional compile failure is retried on reentry, not per frame.
  for(int failed=1;failed<=15;++failed)
  {
    CRenderSystemGLES r;r.oes=true;select(0);failAt=compiles+failed;
    r.InitialiseShaders();assert(std::count_if(r.m_pShader.begin(),r.m_pShader.end(),
      [](const auto& s){return !s.second;})==1);
    const int before=compiles;
    for(int frame=0;frame<1000;++frame)assert(r.BeginRender());
    assert(compiles==before);
    select(2);assert(r.BeginRender());assert(r.m_shaderVariants[0].empty());
    select(0);assert(r.BeginRender());assert(compiles==before+30);checkDefines(r,0);
    const auto recovered=programs(r);
    select(2);assert(r.BeginRender());select(0);assert(r.BeginRender());
    assert(programs(r)==recovered && compiles==before+30);
    r.ReleaseShaders();assert(livePrograms.empty());failAt=0;
  }
  // Context loss abandons active and inactive names without issuing GL deletes.
  CRenderSystemGLES r;select(0);r.InitialiseShaders();
  for(unsigned int v=1;v<4;++v){select(v);assert(r.BeginRender());}
  const int beforeDelete=deletions,beforeAbandon=abandons;
  currentContext=0;r.ReleaseShaders(true);
  assert(deletions==beforeDelete && abandons-beforeAbandon==52 && livePrograms.empty());
  currentContext=1;checkFrameColour(false);checkFrameColour(true);
}
'''


if __name__ == '__main__':
    main()
