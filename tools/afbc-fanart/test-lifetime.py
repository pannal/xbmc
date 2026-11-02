#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise production deferred deletion methods with a recording GL fixture."""
from pathlib import Path
import subprocess
import tempfile
import os

ROOT = Path(__file__).resolve().parents[2]


def function(source, signature):
    start = source.index(signature)
    position = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[position] == '{') - (source[position] == '}')
        position += 1
    return source[start:position]


manager = (ROOT/'xbmc/guilib/TextureManager.cpp').read_text()
texture = (ROOT/'xbmc/guilib/TextureGL.cpp').read_text()
header = (ROOT/'xbmc/guilib/TextureManager.h').read_text()
entry = function(header, '  struct HardwareTexture') + ';'
source = r'''
#include <cassert>
#include <chrono>
#include <memory>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>
#define HAS_GLES 2
using GLuint=unsigned int;
using CCriticalSection=std::recursive_mutex;
static std::vector<unsigned int> deleted;
static void glDeleteTextures(int count,const unsigned int*names){for(int i=0;i<count;i++)deleted.push_back(names[i]);}
struct CTextureMap {};
class CGUITextureManager {public:
  void FreeUnusedTextures(unsigned int timeDelay=0);
  void ReleaseHwTexture(unsigned int texture,std::shared_ptr<void>backing={});
  std::vector<std::pair<CTextureMap*,std::chrono::time_point<std::chrono::steady_clock>>>m_unusedTextures;
  ENTRY
  std::vector<HardwareTexture>m_unusedHwTextures;
};
static CGUITextureManager manager;
struct Window {std::recursive_mutex mutex;std::recursive_mutex&GetGfxContext(){return mutex;}};
struct GUI {CGUITextureManager&GetTextureManager(){return manager;}};
struct CServiceBroker {
  static Window*GetWinSystem(){static Window w;return &w;}
  static GUI*GetGUI(){static GUI g;return &g;}
};
class CGLTexture {public:unsigned m_texture{0};std::shared_ptr<void>m_externalBacking;void DestroyTextureObject();};
'''.replace('ENTRY', entry)
source += '\n' + function(manager, 'void CGUITextureManager::FreeUnusedTextures(')
source += '\n' + function(manager, 'void CGUITextureManager::ReleaseHwTexture(')
source += '\n' + function(texture, 'void CGLTexture::DestroyTextureObject(')
source += r'''
int main(){
 const auto owner=std::this_thread::get_id();bool released=false;
 struct Backing {
   std::thread::id owner;bool&released;
   Backing(std::thread::id o,bool&r):owner(o),released(r){}
   ~Backing(){assert(std::this_thread::get_id()==owner);assert(deleted.size()==1&&deleted[0]==7);released=true;}
 };
 CGLTexture texture;texture.m_texture=7;
 texture.m_externalBacking=std::make_shared<Backing>(owner,released);
 std::weak_ptr<void>alive=texture.m_externalBacking;
 std::thread worker([&]{texture.DestroyTextureObject();texture.DestroyTextureObject();});worker.join();
 assert(texture.m_texture==0&&!texture.m_externalBacking);
 assert(!alive.expired()&&!released&&deleted.empty()&&manager.m_unusedHwTextures.size()==1);
 manager.FreeUnusedTextures();assert(released&&alive.expired()&&deleted.size()==1);
 manager.ReleaseHwTexture(9);manager.FreeUnusedTextures();assert(deleted.size()==2&&deleted[1]==9);
}
'''
out = Path(tempfile.mkdtemp(prefix='afbc-lifetime-'))
cpp = out/'test.cpp'
cpp.write_text(source)
binary = out/'test'
subprocess.run(['g++', '-std=c++17', '-O1', '-g', '-pthread', '-Wall', '-Wextra', '-Werror',
                '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie',
                str(cpp), '-o', str(binary)], check=True)
subprocess.run([str(binary)], check=True, env=dict(os.environ, ASAN_OPTIONS='detect_leaks=0'))
print('PASS worker retirement retains storage through GUI deletion; duplicate release and ordinary texture controls')
