#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Check production upload admission with projected GUI bounds and origins."""
from pathlib import Path
import subprocess
import tempfile
root=Path(__file__).resolve().parents[2]
source=(root/'xbmc/guilib/GUITexture.cpp').read_text()
a=source.index('void CGUITexture::RequestUploadAhead(');b=source.index('\n}\n',a)+3
code=r'''
#include "utils/Geometry.h"
#include <algorithm>
#include <cassert>
#include <string>
#include <optional>
#include <vector>
struct Gfx {
 float scale{2},x{0},y{0};unsigned projections{0};
 int GetWidth(){return 3840;}int GetHeight(){return 2160;}
 CRect GenerateAABB(const CRect& r){++projections;return {(r.x1+x)*scale,(r.y1+y)*scale,(r.x2+x)*scale,(r.y2+y)*scale};}
};
struct Window {Gfx gfx;Gfx& GetGfxContext(){return gfx;}};
struct Advanced {bool m_guiAsyncTextureUpload{true};Advanced*GetAdvancedSettings(){return this;}};
struct Manager {
 struct Request {std::string path;bool background,prefetch;std::optional<unsigned>frame;};std::vector<Request>requests;
 void RequestImageUpload(const std::string& path,bool background=false,bool prefetch=false,std::optional<unsigned>frame={}){requests.push_back({path,background,prefetch,frame});}
};
struct GUI {Manager manager;Manager&GetLargeTextureManager(){return manager;}};
static Window window;static Advanced advanced;static GUI gui;
struct CServiceBroker {
 static Window*GetWinSystem(){return &window;}static Advanced*GetSettingsComponent(){return &advanced;}
 static GUI*GetGUI(){return &gui;}
};
struct CGUITexture {
 enum {NO,LARGE,NORMAL};int m_isAllocated{LARGE};bool m_visible{true},background{false};
 struct Array{unsigned count{0};unsigned size(){return count;}}m_texture;
 struct Info{std::string filename{"poster"};}m_info;
 float m_posX{0},m_posY{0},m_width{290},m_height{415};
 bool IsBackgroundRequest(){return background;}void RequestUploadAhead(unsigned);
};
'''+source[a:b]+r'''
int main(){
 auto run=[](CGUITexture& t,int kind){gui.manager.requests.clear();t.RequestUploadAhead(42);
  if(kind<0){assert(gui.manager.requests.empty());return;}
  assert(gui.manager.requests.size()==1);const auto& r=gui.manager.requests.front();
  assert(r.path=="poster"&&!r.background&&r.prefetch==bool(kind)&&r.frame==42);};
 CGUITexture t;run(t,0); // On-screen demand starts before Render.
 t.m_posX=-300;run(t,1);t.m_posX=-1000;run(t,-1);
 t.m_posX=0;t.m_posY=1085;run(t,1);t.m_posY=1800;run(t,-1);
 t.m_posY=0;window.gfx.y=-425;run(t,1);window.gfx.y=-1500;run(t,-1);
 window.gfx=Gfx{};window.gfx.x=100;t.m_posX=-150;run(t,0); // Origin changes classification.
 window.gfx=Gfx{};t.m_posX=0;
 t.background=true;run(t,-1);t.background=false;
 t.m_visible=false;run(t,-1);t.m_visible=true;
 t.m_texture.count=1;run(t,-1);t.m_texture.count=0;
 t.m_isAllocated=CGUITexture::NORMAL;run(t,-1);t.m_isAllocated=CGUITexture::LARGE;
 t.m_width=0;run(t,-1);t.m_width=290;
 const auto projections=window.gfx.projections;advanced.m_guiAsyncTextureUpload=false;
 run(t,-1);assert(window.gfx.projections==projections); // Async-off skips projection too.
}
'''
with tempfile.TemporaryDirectory(prefix='async-upload-prefetch-') as temporary:
 p=Path(temporary);(p/'test.cpp').write_text(code)
 subprocess.run(['c++','-std=c++17','-O1','-g','-Wall','-Wextra','-Werror',
                 '-fsanitize=address,undefined','-fno-omit-frame-pointer','-I'+str(root/'xbmc'),
                 str(p/'test.cpp'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True)
print('PASS production prefetch admission: visible, near/far edges, origins, scaling, hidden/ready/background bypass and async-off cost')
