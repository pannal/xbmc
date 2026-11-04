#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise the production image render dispatch with a recording renderer."""
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
def function(source, signature):
    begin = source.index(signature)
    end = source.index('{', begin) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[begin:end]

code = r'''
#include <cassert>
#include <memory>
#include <vector>
struct Settings {bool m_guiSinglePassCrossfade{true};Settings* GetAdvancedSettings(){return this;}};
static Settings settings;
struct CServiceBroker {static Settings* GetSettingsComponent(){return &settings;}};
struct CGUITexture {
 bool ready{true};int draws{0};
 bool ReadyToRender(){return ready;}
 void Render(){++draws;}
 int crossfades{0};bool crossfadeAccepted{true};
 bool lastDissolve{false};
 bool RenderCrossfade(CGUITexture&,bool dissolve){++crossfades;lastDissolve=dissolve;return crossfadeAccepted;}
};
struct CGUIControl {unsigned renders{0};void Render(){++renders;}};
struct CGUIImage:CGUIControl {
 struct CFadingTexture {std::unique_ptr<CGUITexture>m_texture{std::make_unique<CGUITexture>()};bool m_dissolve{false};};
 std::unique_ptr<CGUITexture>m_texture{std::make_unique<CGUITexture>()};
 std::vector<std::unique_ptr<CFadingTexture>> owners;
 std::vector<CFadingTexture*>m_fadingTextures;
 bool IsVisible(){return true;}void Render();
 void Old(){owners.emplace_back(std::make_unique<CFadingTexture>());m_fadingTextures.push_back(owners.back().get());}
};
'''
code += function((root/'xbmc/guilib/GUIImage.cpp').read_text(), 'void CGUIImage::Render()')
code += r'''
int main(){
 // One outgoing image: the pair is drawn once by the single pass.
 {CGUIImage i;i.Old();i.Render();
  assert(i.m_texture->crossfades==1&&i.m_texture->draws==0&&i.m_fadingTextures[0]->m_texture->draws==0&&i.renders==1);}
 // The outgoing image's history decides whether the pair may dissolve.
 for(bool dissolve:{false,true}){CGUIImage i;i.Old();i.m_fadingTextures[0]->m_dissolve=dissolve;i.Render();
  assert(i.m_texture->crossfades==1&&i.m_texture->lastDissolve==dissolve);}
 // A declined pair falls back to the separate draws exactly once.
 {CGUIImage i;i.Old();i.m_texture->crossfadeAccepted=false;i.Render();
  assert(i.m_texture->crossfades==1&&i.m_texture->draws==1&&i.m_fadingTextures[0]->m_texture->draws==1&&i.renders==1);}
 // Three layers never try the single pass.
 {CGUIImage i;i.Old();i.Old();i.Render();
  assert(i.m_texture->crossfades==0&&i.m_texture->draws==1&&i.m_fadingTextures[0]->m_texture->draws==1&&
         i.m_fadingTextures[1]->m_texture->draws==1);}
 // Either image not ready keeps the separate draws.
 {CGUIImage i;i.Old();i.m_texture->ready=false;i.Render();assert(i.m_texture->crossfades==0&&i.m_texture->draws==1);}
 {CGUIImage i;i.Old();i.m_fadingTextures[0]->m_texture->ready=false;i.Render();
  assert(i.m_texture->crossfades==0&&i.m_fadingTextures[0]->m_texture->draws==1);}
 // The setting disables the single pass.
 settings.m_guiSinglePassCrossfade=false;
 {CGUIImage i;i.Old();i.Render();assert(i.m_texture->crossfades==0&&i.m_texture->draws==1);}
 settings=Settings{};
 // Without a fade the image is drawn directly.
 {CGUIImage i;i.Render();assert(i.m_texture->crossfades==0&&i.m_texture->draws==1&&i.renders==1);}
}
'''
with tempfile.TemporaryDirectory(prefix='image-dispatch-') as temporary:
    path=Path(temporary);(path/'test.cpp').write_text(code)
    subprocess.run(['c++','-std=c++17','-O1','-g','-Wall','-Wextra','-Werror',
                    '-DTARGET_LINUX','-DHAS_LIBAMCODEC','-DHAS_GLES=2',
                    '-fsanitize=address,undefined','-fno-omit-frame-pointer',
                    str(path/'test.cpp'),'-o',str(path/'test')],check=True)
    subprocess.run([str(path/'test')],check=True)
print('PASS production image render dispatch: single-pass pair with the outgoing image history, declined pair, three layers, unready images, setting and direct draw')
