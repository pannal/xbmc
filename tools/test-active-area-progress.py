#!/usr/bin/env python3
"""Connect the production progress dialog, player retirement and AML OSD publisher.

The complete real dialog .cpp/.h and the extracted real player Finish method and
AML OSD method run together. The GUI base fixture models its close animation as
an explicit FinishClose step; assertions check the matching production base
ordering. Labels, window visibility and sysfs are recording boundaries. This is
host lifecycle coverage, not a skin/GPU, HDMI or paused-frame driver reproduction.
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-aml-osd-lifetime.py'))['function']

GUI_STUB = r'''
#pragma once
#include <cassert>
#include <map>
#include <mutex>
#include <string>
#include <vector>
using CCriticalSection=std::mutex;
using CDirtyRegionList=std::vector<int>;
constexpr int WINDOW_DIALOG_EXT_PROGRESS=10151,WINDOW_HOME=10000,WINDOW_VIDEO_MENU=12901;
constexpr int GUI_MSG_WINDOW_INIT=1,GUI_MSG_WINDOW_DEINIT=2,LOAD_ON_GUI_INIT=1;
enum class DialogModalityType {MODELESS};
struct CGUIMessage {int type;explicit CGUIMessage(int value):type(value){}int GetMessage(){return type;}};
struct Property {std::string value;std::string asString()const{return value;}};
class CGUIWindow {
public:
 std::map<std::string,std::string> properties;
 void SetProperty(const char* name,const char* value){properties[name]=value;}
 Property GetProperty(const char* name)const{auto it=properties.find(name);return {it==properties.end()?"":it->second};}
};
class CGUIDialog:public CGUIWindow {
public:
 bool m_active=false,visible=false,closing=false;
 int m_loadType=0;std::map<int,std::string> labels;int renderedPercentage=-1;
 CGUIDialog(int,const char*,DialogModalityType){}
 virtual ~CGUIDialog()=default;
 virtual bool OnMessage(CGUIMessage& msg){if(msg.type==GUI_MSG_WINDOW_DEINIT){m_active=false;visible=false;closing=false;}return true;}
 virtual void Process(unsigned int,CDirtyRegionList&){}
 void Open(){if(!m_active){m_active=true;visible=true;CGUIMessage msg(GUI_MSG_WINDOW_INIT);OnMessage(msg);}}
 void Close(bool force,int=0,bool=true,bool=false){if(!m_active)return;if(!force){closing=true;return;}CGUIMessage msg(GUI_MSG_WINDOW_DEINIT);OnMessage(msg);}
 void FinishClose(){assert(closing);Close(true);}
};
#define SET_CONTROL_LABEL(id,value) labels[id]=value
#define SET_CONTROL_VISIBLE(id) do{}while(false)
#define CONTROL_SELECT_ITEM(id,value) renderedPercentage=value
'''
PRELUDE = r'''
#include "dialogs/GUIDialogExtendedProgressBar.h"
#include <atomic>
#include <iostream>
CGUIDialogExtendedProgressBar progress;
CGUIWindow home;
struct WindowManager {
 bool genericDialogs=false,videoMenu=false;
 CGUIWindow* GetWindow(int id){if(id==WINDOW_DIALOG_EXT_PROGRESS)return &progress;assert(id==WINDOW_HOME);return &home;}
 bool HasVisibleDialog()const{return genericDialogs;}
 bool IsWindowVisible(int id)const{if(id==WINDOW_DIALOG_EXT_PROGRESS)return progress.visible;assert(id==WINDOW_VIDEO_MENU);return videoMenu;}
}wm;
struct GUI {WindowManager& GetWindowManager(){return wm;}}gui;
struct Cache {bool avChange=false;bool GetAVChangeExtended()const{return avChange;}}cache;
struct CServiceBroker {static GUI* GetGUI(){return &gui;}static Cache& GetDataCacheCore(){return cache;}};
std::atomic_bool s_pm4kActive{false},s_discMenuVisible{false};
int s_lastOsd=-1,emittedOsd=-1,osdWrites=0;
struct CSysfsPath {CSysfsPath(const char* path,int value){assert(std::string(path)=="/sys/module/amdolby_vision/parameters/dolby_vision_xbmc_osd");emittedOsd=value;++osdWrites;}};
void aml_dv_dump_state(const char*){}
struct CVideoPlayer {CGUIDialogProgressBarHandle* m_activeAreaProgress=nullptr;void FinishActiveAreaProgress();};
'''
TEST = r'''
void Frame(unsigned int now){CDirtyRegionList dirty;progress.Process(now,dirty);aml_dv_set_xbmc_osd();}
void Expect(int value){aml_dv_set_xbmc_osd();assert(emittedOsd==value);}
void Empty(){assert(!progress.visible);assert(progress.GetProperty("activeareastartup").asString().empty());Expect(0);}
void Complete(CVideoPlayer& player,unsigned int now){
 auto* handle=player.m_activeAreaProgress;assert(handle&&!handle->IsFinished());
 player.FinishActiveAreaProgress();assert(!player.m_activeAreaProgress&&handle->IsFinished());
 // No GUI update has run: the last detector frame is still displayed.
 assert(progress.visible&&!progress.GetProperty("activeareastartup").asString().empty());Expect(1);
 Frame(now);assert(progress.visible&&progress.closing);
 // Finished handles have been deleted, but the rendered closing frame still
 // needs zero-L5 OSD signaling, including when no new video frame is produced.
 assert(!progress.GetProperty("activeareastartup").asString().empty());Expect(1);
 const int writes=osdWrites;Expect(1);assert(osdWrites==writes);
 progress.FinishClose();Empty();
}
int main(){
 // Native and PM4K both publish the native detector signal; PM4K's own OSD
 // explicitly remains off throughout these paused/closing GUI frame steps.
 for(bool pm4k:{false,true}){
  s_pm4kActive=pm4k;home.SetProperty("script.plex.osd_active","0");Empty();
  CVideoPlayer player;player.m_activeAreaProgress=progress.GetHandle("L5",true);
  auto* handle=player.m_activeAreaProgress;assert(handle->IsActiveAreaStartup());
  handle->SetText("6/14");handle->SetProgress(6,14);Frame(100);
  assert(progress.labels[30]=="L5"&&progress.labels[31]=="6/14");
  assert(progress.renderedPercentage==42);Expect(1);
  Complete(player,101);
  // Stop/cancel uses the same real retirement method before any further
  // detector progress. Its last visible frame has the same lifetime guarantee.
  player.m_activeAreaProgress=progress.GetHandle("cancelled detection",true);
  Frame(102);Expect(1);Complete(player,103);
 }
 // A generic job is NOT a detector, even in the shared visible dialog.
 s_pm4kActive=true;auto* ordinary=progress.GetHandle("download");Frame(100);
 assert(!ordinary->IsActiveAreaStartup());Expect(0);
 CVideoPlayer player;player.m_activeAreaProgress=progress.GetHandle("L5",true);
 Frame(101);assert(progress.labels[30]=="download");Expect(0);
 Frame(2101);assert(progress.labels[30]=="L5");Expect(1);
 Frame(4101);assert(progress.labels[30]=="download");Expect(0);
 Frame(6101);assert(progress.labels[30]=="L5");Expect(1);
 // Completing the detector selects the surviving generic job immediately:
 // the signal belongs to what is rendered, not another/finished handle.
 player.FinishActiveAreaProgress();Frame(6102);
 assert(progress.visible&&!progress.closing&&progress.labels[30]=="download");
 assert(progress.GetProperty("activeareastartup").asString().empty());Expect(0);
 ordinary->MarkFinished();Frame(6103);assert(progress.closing);progress.FinishClose();Empty();
 // A generic handle can later be replaced by a detector, and vice versa.
 ordinary=progress.GetHandle("download");player.m_activeAreaProgress=progress.GetHandle("L5",true);
 Frame(100);Expect(0);ordinary->MarkFinished();Frame(101);
 assert(progress.labels[30]=="L5");Expect(1);Complete(player,102);
 // Other OSD sources retain their own independent lifetime.
 home.SetProperty("script.plex.osd_active","1");Expect(1);
 home.SetProperty("script.plex.osd_active","0");Expect(0);
 s_discMenuVisible=true;Expect(1);s_discMenuVisible=false;Expect(0);
 wm.genericDialogs=true;Expect(0); // PM4K still controls ordinary dialogs.
 s_pm4kActive=false;Expect(1);wm.genericDialogs=false;Expect(0);
 wm.videoMenu=true;Expect(1);wm.videoMenu=false;Expect(0);
 home.SetProperty("skin.codeclogosonscreen","1");Expect(1);
 home.SetProperty("skin.codeclogosonscreen","0");Expect(0);
 cache.avChange=true;Expect(1);home.SetProperty("skin.codeclogososd","1");Expect(0);
 std::cout<<"PASS: connected real progress/current handle -> player finish/cancel -> rendered close/deinit -> native/PM4K OSD sysfs publication\n";
}
'''


def sources():
    dialog = (ROOT / 'xbmc/dialogs/GUIDialogExtendedProgressBar.cpp').read_text()
    header = (ROOT / 'xbmc/dialogs/GUIDialogExtendedProgressBar.h').read_text()
    aml = (ROOT / 'xbmc/utils/AMLUtils.cpp').read_text()
    player = (ROOT / 'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
    window = (ROOT / 'xbmc/guilib/GUIWindow.cpp').read_text()
    after = function(window, 'void CGUIWindow::AfterRender()')
    close = function(window, 'void CGUIWindow::Close_Internal(')
    assert 'if (m_closing && !CGUIControlGroup::IsAnimating(ANIM_TYPE_WINDOW_CLOSE))' in after
    assert 'Close(true);' in after
    assert close.index('if (!forceClose && HasAnimation(ANIM_TYPE_WINDOW_CLOSE))') < close.index('CGUIMessage msg(GUI_MSG_WINDOW_DEINIT')
    assert 'm_closing = true;' in close and 'return;' in close[:close.index('CGUIMessage msg(GUI_MSG_WINDOW_DEINIT')]
    # Pause does not stop the real GUI's OSD publication path.
    manager = function((ROOT / 'xbmc/guilib/GUIWindowManager.cpp').read_text(),
                       'void CGUIWindowManager::AfterRender()')
    assert 'aml_dv_set_xbmc_osd();' in manager
    osd = function(aml, 'void aml_dv_set_xbmc_osd()')
    finish = function(player, 'void CVideoPlayer::FinishActiveAreaProgress()')
    return header, dialog, PRELUDE + osd + '\n' + finish + '\n' + TEST


def run(header, dialog, code, negative=False):
    with tempfile.TemporaryDirectory(prefix='active-area-progress-') as directory:
        out=Path(directory)
        for name,content in {
            'guilib/GUIDialog.h':GUI_STUB,
            'guilib/GUIMessage.h':'#include "guilib/GUIDialog.h"\n',
            'guilib/GUIProgressControl.h':'',
            'utils/Variant.h':'',
            'utils/TimeUtils.h':'#pragma once\nstruct CTimeUtils{static unsigned int GetFrameTime(){return 0;}};\n',
            'dialogs/GUIDialogExtendedProgressBar.h':header,
            'dialogs/GUIDialogExtendedProgressBar.cpp':dialog,
            'fixture.cpp':code,
        }.items():
            path=out/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
        compiled=subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-O1','-g','-Wall','-Wextra','-Werror',
                                 '-Wno-unused-parameter','-pthread','-fsanitize=address,undefined',
                                 '-fno-omit-frame-pointer','-fno-pie','-no-pie',
                                 '-fno-sanitize-recover=undefined','-I'+str(out),
                                 str(out/'dialogs/GUIDialogExtendedProgressBar.cpp'),str(out/'fixture.cpp'),
                                 '-o',str(out/'fixture')],capture_output=True,text=True,timeout=60)
        assert compiled.returncode==0,compiled.stderr
        result=subprocess.run([str(out/'fixture')],capture_output=True,text=True,timeout=15,
                              env=dict(os.environ,ASAN_OPTIONS='detect_leaks=0'))
        if negative:
            assert result.returncode!=0 and 'Assertion' in result.stderr, (
                'negative control must fail a behavioral assertion', result.returncode,
                result.stdout, result.stderr)
        else:
            assert result.returncode==0,result.stdout+result.stderr
            print(result.stdout.strip(),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--negative-controls',action='store_true')
    args=parser.parse_args();header,dialog,code=sources();run(header,dialog,code)
    if args.negative_controls:
        controls=[
            ('player clears signal before rendered frame retires','code',
             'm_activeAreaProgress->MarkFinished();','m_activeAreaProgress->MarkFinished();progress.SetProperty("activeareastartup", "");'),
            ('missing detector marker','dialog',
             'activeAreaStartup ? "1" : ""','activeAreaStartup ? "" : ""'),
            ('wrong current handle treated as detector','dialog',
             'activeAreaStartup = handle->IsActiveAreaStartup();','activeAreaStartup = true;'),
            ('closing frame loses marker early','dialog',
             'Close(false, 0, true, false);','SetProperty("activeareastartup", "");Close(false, 0, true, false);'),
            ('deinit leaves stale detector marker','dialog',
             'SetProperty("activeareastartup", "");','/* deinit signal leaked */'),
            ('PM4K ignores native detector progress','code',
             'osd_active || detectorProgressVisible ||','osd_active || false ||'),
        ]
        for label,target,old,new in controls:
            original=dialog if target=='dialog' else code
            assert old in original,label
            changed=original.replace(old,new)
            # Keep negative controls compiling: an ignored signal remains read.
            if label=='PM4K ignores native detector progress':
                changed=changed.replace('int val = (osd_active', '(void)detectorProgressVisible;int val = (osd_active')
            run(header,changed if target=='dialog' else dialog,changed if target=='code' else code,negative=True)
            print('REJECTED:',label,flush=True)
    print('Host fixtures; real animation renderer, skin brightness and paused-frame kernel/HDMI remain device verification.',flush=True)


if __name__=='__main__':main()
