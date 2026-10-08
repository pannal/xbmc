#!/usr/bin/env python3
"""Advanced XML parsing and actual PreInit/activation boundaries.

Host substitutes replace services/renderers; no CE compilation or GUI/device proof.
"""
from collections import Counter
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
KEY = "coreelec.amlogic.independentpresenter"
CONSTANT = "SETTING_COREELEC_AMLOGIC_INDEPENDENT_PRESENTER"


HARNESS = r"""
#include <cassert>
#include <cctype>
#include <algorithm>
#include <string_view>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>
#include <cstdint>
#include <optional>
using CCriticalSection = std::recursive_mutex;
constexpr int LOGINFO=1, STATE_UNCONFIGURED=0, STATE_CONFIGURED=1;
constexpr int PRESENT_IDLE=0, RENDER_STEREO_MODE_OFF=0;

static int reads=0;
static std::vector<std::string> logs;
struct CLog { template<class... T> static void Log(int, const char* fmt, T... values) {
  std::ostringstream os; os << fmt; ((os << '|' << values), ...); logs.push_back(os.str());
}};
namespace PLAYBACK_DIAGNOSTICS {
uint64_t NowUs(){return 100;} uint64_t NextId(){return 200;}
struct EndDisplay {template<class F> void Record(uint64_t,const char*,F&&) {}};
[[maybe_unused]] inline EndDisplay endDisplay;
}
namespace fmt {template<class... T> std::string format(const char*,T&&...){return {};}}
struct CSysInfo {static const char* GetVersion(){return "test";} static const char* GetBuildDate(){return "today";}};
struct TiXmlNode {
 std::string name,value;std::vector<std::unique_ptr<TiXmlNode>> children;
 explicit TiXmlNode(std::string n="",std::string v=""):name(n),value(v){}
 TiXmlNode* Add(std::string n,std::string v=""){
  children.push_back(std::make_unique<TiXmlNode>(n,v));return children.back().get();}
 const TiXmlNode* FirstChild(const char* n=nullptr)const{
  for(const auto& c:children){if(!n||c->name==n)return c.get();}
  return nullptr;}
 const std::string& ValueStr()const{return value;}
};
struct StringUtils {static void ToLower(std::string& s){for(char& c:s)c=std::tolower(static_cast<unsigned char>(c));}};
struct XMLUtils {static bool GetBoolean(const TiXmlNode*,const char*,bool&);};
@BOOLEAN@
struct CAdvancedSettings {
 @DEFAULT@
 void Initialize(){@INITIALIZE@}
 void Parse(const TiXmlNode* root){
  const auto* pElement=root->FirstChild("video");if(pElement){@PARSE@}
 }
};
static CAdvancedSettings advanced;
struct Component {CAdvancedSettings* GetAdvancedSettings(){++reads;return &advanced;}};
struct Graphics {int stereo=0; int GetStereoMode(){return stereo;}};
static Graphics graphics;
struct Window {Graphics& GetGfxContext(){return graphics;}};
struct CServiceBroker {static Component* GetSettingsComponent(){static Component c;return &c;}
static Window* GetWinSystem(){static Window w;return &w;}};
struct Renderer {virtual ~Renderer()=default;};
struct CRendererAML : Renderer {int PresenterCodec(){return 1;}};
struct Queue {void Show(bool){}};
struct CAMLPresenterSession {std::shared_ptr<Queue> queue=std::make_shared<Queue>();
  template<class A,class B> CAMLPresenterSession(int,int,int,bool,A,B){}
};
struct Port {void UpdateClockSync(bool){}};
struct Cache {void SetRenderPts(double){}};
struct CRenderManager {
 CCriticalSection m_statelock,m_presentlock,m_datalock;
 int m_renderState=STATE_UNCONFIGURED,m_QueueSize=0,m_QueueSkip=0,m_presentstep=0;
 bool m_bRenderGUI=false,m_showVideo=true,m_processInfoLifetime=true;
 int m_dvdClock=0,m_diagnosticId=3,m_lifecycleGeneration=1;
 std::optional<int> m_appliedResolution;
 int DiagnosticId(){return m_diagnosticId;}
 struct Picture {std::string stereoMode;} m_picture;
 Renderer* m_pRenderer=nullptr;
 Port port; Port* m_playerPort=&port; Cache m_dataCacheCore;
 bool m_amlIndependentPresenter=false;
 std::shared_ptr<CAMLPresenterSession> m_amlPresenter;
 void ClearFrameSelection(){} void InvalidateReservations(){} void CreateRenderer(){}
 void UpdateLatencyTweak(){} bool UpdateAMLPresenter(){return true;} void LogAMLPresenter(const char*,bool){}
 void PreInitOnMain();
 void ConfigureSelection(){
#ifdef HAS_LIBAMCODEC
 @ACTIVATION@
#endif
 }
};
@METHODS@
int main(){
 assert(advanced.m_amlIndependentPresenter);
 TiXmlNode root("advancedsettings");auto* video=root.Add("video");
 advanced.Parse(&root);assert(advanced.m_amlIndependentPresenter); // absent means enabled
 auto* entry=video->Add("amlindependentpresenter");entry->Add("text","false");
 advanced.Parse(&root);assert(!advanced.m_amlIndependentPresenter);
 // Layered XML without a value preserves the preceding override.
 TiXmlNode empty("advancedsettings");empty.Add("video");advanced.Parse(&empty);
 assert(!advanced.m_amlIndependentPresenter);
 advanced.Initialize();assert(advanced.m_amlIndependentPresenter); // profile reset
 for(const char* value:{"false","off","0"}){
  entry->children.front()->value=value;advanced.Parse(&root);assert(!advanced.m_amlIndependentPresenter);
 }
 for(const char* value:{"true","on","yes"}){
  entry->children.front()->value=value;advanced.Parse(&root);assert(advanced.m_amlIndependentPresenter);
 }
 TiXmlNode obsolete("advancedsettings");obsolete.Add("coreelec")->Add("amlogic.independentpresenter")->Add("text","false");
 advanced.Parse(&obsolete);assert(advanced.m_amlIndependentPresenter); // old GUI encoding has no effect
 CRendererAML aml;Renderer software;
 CRenderManager manager;manager.m_pRenderer=&aml;
 advanced.m_amlIndependentPresenter=false;manager.PreInitOnMain();manager.ConfigureSelection();assert(!manager.m_amlPresenter);
 // Reconfiguration cannot switch the current playback snapshot.
 advanced.m_amlIndependentPresenter=true;manager.ConfigureSelection();assert(!manager.m_amlPresenter);
 manager.m_renderState=STATE_CONFIGURED;
 const int priorReads=reads;manager.PreInitOnMain();assert(reads==priorReads);
 manager.ConfigureSelection();assert(!manager.m_amlPresenter);
 // Completed stop/new playback reaches the unconfigured PreInit boundary.
 manager.m_renderState=STATE_UNCONFIGURED;manager.PreInitOnMain();
 manager.ConfigureSelection();
#ifdef HAS_LIBAMCODEC
 assert(manager.m_amlPresenter && manager.m_amlIndependentPresenter);
 advanced.m_amlIndependentPresenter=false;manager.m_amlPresenter.reset();manager.ConfigureSelection();assert(manager.m_amlPresenter);
 // Eligibility remains unchanged for software, stereo and missing original lifetime.
 manager.m_amlPresenter.reset();manager.m_pRenderer=&software;manager.ConfigureSelection();assert(!manager.m_amlPresenter);
 manager.m_pRenderer=&aml;manager.m_picture.stereoMode="split_vertical";manager.ConfigureSelection();assert(!manager.m_amlPresenter);
 manager.m_picture.stereoMode="mono";graphics.stereo=1;manager.ConfigureSelection();assert(!manager.m_amlPresenter);
 graphics.stereo=0;manager.m_processInfoLifetime=false;manager.ConfigureSelection();assert(!manager.m_amlPresenter);
 manager.m_processInfoLifetime=true;manager.ConfigureSelection();assert(manager.m_amlPresenter);
 manager.m_amlPresenter.reset();manager.PreInitOnMain();manager.ConfigureSelection();assert(!manager.m_amlPresenter);
#else
 assert(!manager.m_amlPresenter && reads==0);
#endif
}
"""


def main():
    tree = ET.parse(ROOT / "system/settings/settings.xml")
    ids = [s.attrib["id"] for s in tree.iter("setting")]
    assert not [i for i,n in Counter(ids).items() if n != 1]
    assert KEY not in ids
    header=(ROOT/'xbmc/settings/Settings.h').read_text()
    assert CONSTANT not in header
    advanced_header=(ROOT/'xbmc/settings/AdvancedSettings.h').read_text()
    advanced_source=(ROOT/'xbmc/settings/AdvancedSettings.cpp').read_text()
    default=re.search(r'bool m_amlIndependentPresenter = true;',advanced_header).group()
    initialize=re.search(r'm_amlIndependentPresenter = true;',advanced_source).group()
    parse=re.search(r'XMLUtils::GetBoolean\(pElement, "amlindependentpresenter", m_amlIndependentPresenter\);',advanced_source).group()
    load=advanced_source.split('bool CAdvancedSettings::Load(',1)[1].split('constexpr CAEStreamInfo',1)[0]
    assert load.index('profileManager.GetUserDataItem("advancedsettings.xml")') < load.index('source=advancedsettings')
    render=(ROOT/'xbmc/cores/VideoPlayer/VideoRenderers/RenderManager.cpp').read_text()
    extract=runpy.run_path(str(ROOT/'tools/test-render-slot-publication.py'))['function']
    preinit=extract(render,'void CRenderManager::PreInitOnMain()')
    configure=extract(render,'bool CRenderManager::Configure()')
    activation=configure.split('    auto* aml = dynamic_cast<CRendererAML*>',1)[1].split('#endif',1)[0]
    activation='    auto* aml = dynamic_cast<CRendererAML*>'+activation
    assert 'const bool enabled = m_amlIndependentPresenter;' in activation
    assert 'GetSettings' not in activation and 'IsInMenu' not in activation
    assert render.count('m_amlIndependentPresenter =') == 1
    player=(ROOT/'xbmc/cores/VideoPlayer/VideoPlayer.cpp').read_text()
    opened=extract(player,'bool CVideoPlayer::OpenFile(')
    assert opened.index('if (IsRunning())') < opened.index('m_renderManager.PreInit()') < opened.index('  Create();')
    boolean=extract((ROOT/'xbmc/utils/XMLUtils.cpp').read_text(),
                    'bool XMLUtils::GetBoolean(const TiXmlNode* pRootNode, const char* strTag, bool& bBoolValue)')
    harness=(HARNESS.replace('@BOOLEAN@',boolean).replace('@DEFAULT@',default)
             .replace('@INITIALIZE@',initialize).replace('@PARSE@',parse)
             .replace('@ACTIVATION@',activation).replace('@METHODS@',preinit))
    with tempfile.TemporaryDirectory(prefix='presenter-setting-') as tmp:
        out=Path(tmp);(out/'test.cpp').write_text(harness)
        for target in [True,False]:
            defines=['-DHAS_LIBAMCODEC=1'] if target else []
            subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror',
                            '-fsanitize=address,undefined','-fno-omit-frame-pointer',*defines,
                            str(out/'test.cpp'),'-o',str(out/'test')],check=True)
            subprocess.run([str(out/'test')],check=True,timeout=20)
    print('PASS: dedicated advanced XML default/override/profile reset; production boolean parser, playback snapshot and activation/fallback boundaries (AML/non-AML; host ASan/UBSan)')


if __name__ == '__main__':
    main()
