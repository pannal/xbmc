#!/usr/bin/env python3
"""Exercise stop-time native GUI refresh against later output-mode decisions.

Production stop/retry methods; simulated application queue, cleanup, display
admission and output application. No HDMI receiver or decoder emulation.
"""
from pathlib import Path
import argparse
import os
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--utils-source', type=Path)
parser.add_argument('--gfx-source', type=Path)
args = parser.parse_args()
utils = (args.utils_source or ROOT / 'xbmc/utils/AMLUtils.cpp').read_text()
gfx = (args.gfx_source or ROOT / 'xbmc/windowing/GraphicContext.cpp').read_text()
production = function(utils, 'void aml_native_gui_playback_stopped()')
production += '\n' + function(gfx, 'void CGraphicContext::ProcessPendingVideoResolution()')
if 'void CGraphicContext::RequestVideoResolutionRefresh()' in gfx:
    production += '\n' + function(gfx, 'void CGraphicContext::RequestVideoResolutionRefresh()')

# The real EOF handler can queue this work before cleanup restores the GUI mode.
app = (ROOT / 'xbmc/application/Application.cpp').read_text()
start = app.index('case GUI_MSG_PLAYBACK_ENDED:')
end = app.index('case GUI_MSG_PLAYLISTPLAYER_STOPPED:', start)
eof = app[start:end]
assert eof.index('aml_native_gui_playback_stopped();') < eof.index('PlaybackCleanup();')
pre = r'''
#include <atomic>
#include <cstdio>
#include <optional>
#include <vector>
using RESOLUTION=int;
constexpr int TMSG_SETVIDEORESOLUTION=1;
static std::atomic<bool> s_heavyVideo{false},s_nativeGuiSuspended{true};
static std::vector<int> queued, applied;
static bool blocked=false;
static int failures=0, checks=0;
#define CHECK(x) do { checks++; if (!(x)) { \
    std::fprintf(stderr,"line %d: %s\n",__LINE__,#x); failures++; } } while (0)
struct CGraphicContext {
    struct ResolutionRequest { RESOLUTION resolution; bool forceUpdate; };
    std::optional<ResolutionRequest> m_pendingVideoResolution;
    bool m_videoResolutionRefreshPending=false;
    RESOLUTION m_Resolution=50;
    void RequestVideoResolutionRefresh();
    void ProcessPendingVideoResolution();
    RESOLUTION GetVideoResolution() const { return m_Resolution; }
    bool SetVideoResolutionInternal(RESOLUTION res,bool force) {
        m_pendingVideoResolution.reset();
        if (blocked) { m_pendingVideoResolution=ResolutionRequest{res,force}; return false; }
        m_Resolution=res;applied.push_back(res);
        return true;
    }
};
static CGraphicContext graphics;
struct Window { CGraphicContext& GetGfxContext(){return graphics;} };
struct Messenger {
    void PostMsg(int message,int res,int force) {
        CHECK(message==TMSG_SETVIDEORESOLUTION&&force==1);queued.push_back(res);
    }
};
struct CServiceBroker {
    static Window* GetWinSystem(){static Window w;return &w;}
    static Messenger* GetAppMessenger(){static Messenger m;return &m;}
};
'''
cases = r'''
static void reset() {
    graphics=CGraphicContext{};s_heavyVideo=false;s_nativeGuiSuspended=true;
    queued.clear();applied.clear();blocked=false;
}
static void deliver() {
    for (int res:queued) graphics.SetVideoResolutionInternal(res,true);
    queued.clear();graphics.ProcessPendingVideoResolution();
}
int main() {
    reset();
    aml_native_gui_playback_stopped();
    graphics.SetVideoResolutionInternal(60,false); // terminal cleanup restores UHD60
    applied.clear();deliver();
    CHECK(graphics.m_Resolution==60);
    CHECK(applied==std::vector<int>{60}); // no stale UHD50 write

    reset();
    graphics.m_pendingVideoResolution=CGraphicContext::ResolutionRequest{60,false};
    aml_native_gui_playback_stopped();deliver();
    CHECK(graphics.m_Resolution==60&&!graphics.m_pendingVideoResolution);
    CHECK(applied==std::vector<int>{60});

    reset();
    aml_native_gui_playback_stopped();
    graphics.m_pendingVideoResolution=CGraphicContext::ResolutionRequest{24,false};
    deliver();CHECK(graphics.m_Resolution==24);
    CHECK(applied==std::vector<int>{24});

    reset(); // On-start policy legitimately keeps the existing output mode.
    aml_native_gui_playback_stopped();deliver();
    CHECK(graphics.m_Resolution==50&&applied==std::vector<int>{50});
    applied.clear();deliver();CHECK(applied.empty());

    reset(); // A replacement decoder owns its heavy-video flag.
    s_heavyVideo=true;aml_native_gui_playback_stopped();deliver();
    CHECK(s_heavyVideo);

    reset();
    aml_native_gui_playback_stopped();aml_native_gui_playback_stopped();deliver();
    CHECK(applied==std::vector<int>{50});

    reset();blocked=true;
    aml_native_gui_playback_stopped();deliver();
    CHECK(applied.empty()&&graphics.m_pendingVideoResolution);
    blocked=false;deliver();
    CHECK(applied==std::vector<int>{50}&&!graphics.m_pendingVideoResolution);

    reset();s_nativeGuiSuspended=false;s_heavyVideo=true;
    aml_native_gui_playback_stopped();deliver();
    CHECK(applied.empty()&&s_heavyVideo);
    std::printf("Native GUI restore: %d checks, %d failures\n",checks,failures);
    return failures ? 1:0;
}
'''
with tempfile.TemporaryDirectory(prefix='aml-gui-restore-') as tmp:
    cpp=Path(tmp)/'test.cpp';exe=Path(tmp)/'test'
    cpp.write_text(pre+production+cases)
    subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror',
                    '-fsanitize=undefined','-fno-sanitize-recover=all',str(cpp),'-o',str(exe)],check=True)
    subprocess.run([str(exe)],check=True,timeout=15)
