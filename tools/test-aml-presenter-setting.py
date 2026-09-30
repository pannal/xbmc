#!/usr/bin/env python3
"""GUI definition/localization and actual setting-load/PreInit/activation boundaries.

Host substitutes replace services/renderers; no CE compilation or GUI/device proof.
"""
import ast
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


def catalog(path):
    entries = {}
    entry = {}
    field = None
    for line in path.read_text().splitlines() + [""]:
        if not line.strip():
            if "msgctxt" in entry:
                assert entry["msgctxt"] not in entries, (path, entry["msgctxt"])
                entries[entry["msgctxt"]] = entry
            entry, field = {}, None
        elif line.startswith(("msgctxt ", "msgid ", "msgstr ")):
            field, value = line.split(" ", 1)
            entry[field] = ast.literal_eval(value)
        elif line.startswith('"') and field:
            entry[field] += ast.literal_eval(line)
    return entries


HARNESS = r"""
#include <cassert>
#include <algorithm>
#include <string_view>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>
#include <cstdint>
using CCriticalSection = std::recursive_mutex;
constexpr int LOGINFO=1, STATE_UNCONFIGURED=0, STATE_CONFIGURED=1;
constexpr int PRESENT_IDLE=0, RENDER_STEREO_MODE_OFF=0;
static bool selected=false, xmlSuccess=true, subSuccess=true;
static int reads=0;
static std::vector<std::string> logs;
struct CLog { template<class... T> static void Log(int, const char* fmt, T... values) {
  std::ostringstream os; os << fmt; ((os << '|' << values), ...); logs.push_back(os.str());
}};
namespace PLAYBACK_DIAGNOSTICS {
uint64_t NowUs(){return 100;} uint64_t NextId(){return 200;}
}
struct CSysInfo {static const char* GetVersion(){return "test";} static const char* GetBuildDate(){return "today";}};
struct TiXmlNode {};
// Minimal in-memory XML tree substitute for the production override filter.
struct TiXmlElement : TiXmlNode {
 std::string name,id;TiXmlElement* parent=nullptr;
 std::vector<std::unique_ptr<TiXmlElement>> children;
 TiXmlElement(std::string n="",std::string i=""):name(n),id(i){}
 TiXmlElement(const TiXmlElement& other):name(other.name),id(other.id){
  for(const auto& c:other.children){children.push_back(std::make_unique<TiXmlElement>(*c));children.back()->parent=this;}
 }
 TiXmlElement* Add(std::string n,std::string i=""){
  children.push_back(std::make_unique<TiXmlElement>(n,i));children.back()->parent=this;return children.back().get();
 }
 TiXmlElement* FirstChildElement(const char* n){for(auto& c:children)if(c->name==n)return c.get();return nullptr;}
 TiXmlElement* NextSiblingElement(const char* n){bool seen=false;for(auto& c:parent->children){
  if(seen&&c->name==n)return c.get();
  if(c.get()==this)seen=true;}return nullptr;}
 const char* Attribute(const char* key){assert(std::string(key)=="id");return id.empty()?nullptr:id.c_str();}
 void RemoveChild(TiXmlElement* node){children.erase(std::remove_if(children.begin(),children.end(),
  [&](auto& c){return c.get()==node;}),children.end());}
};
static bool hiddenSuccess=true;
static std::unique_ptr<TiXmlElement> hidden;
struct CSettingsBase {
 static bool LoadValuesFromXml(const TiXmlElement*,bool&){return xmlSuccess;}
 static bool LoadHiddenValuesFromXml(const TiXmlElement* root){hidden=std::make_unique<TiXmlElement>(*root);return hiddenSuccess;}
};
struct CSettings : CSettingsBase {
  @CONSTANT@
  bool GetBool(const char* key){assert(std::string(key)==SETTING_COREELEC_AMLOGIC_INDEPENDENT_PRESENTER); ++reads; return selected;}
  bool Load(const TiXmlElement*,bool&);
  bool LoadHidden(const TiXmlElement*);
  bool Load(const TiXmlNode*){return subSuccess;}
};
static CSettings settings;
struct Component {CSettings* GetSettings(){return &settings;}};
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
 struct Picture {std::string stereoMode;} m_picture;
 Renderer* m_pRenderer=nullptr;
 Port port; Port* m_playerPort=&port; Cache m_dataCacheCore;
 bool m_amlIndependentPresenter=false;
 std::shared_ptr<CAMLPresenterSession> m_amlPresenter;
 void ClearFrameSelection(){} void InvalidateReservations(){} void CreateRenderer(){}
 void UpdateLatencyTweak(){} void UpdateAMLPresenter(){} void LogAMLPresenter(const char*,bool){}
 void PreInitOnMain();
 void ConfigureSelection(){
#ifdef HAS_LIBAMCODEC
 @ACTIVATION@
#endif
 }
};
@METHODS@
int main(){
 assert(!settings.LoadHidden(nullptr)&&!hidden);
 TiXmlElement overrides("advancedsettings");
 const char* key=CSettings::SETTING_COREELEC_AMLOGIC_INDEPENDENT_PRESENTER;
 overrides.Add("setting",key);overrides.Add("setting","unrelated.setting");
 overrides.Add("setting",key);overrides.Add("setting");
 auto* category=overrides.Add("coreelec");
 category->Add("amlogic.independentpresenter");category->Add("amlogic.fastseek");
 category->Add("amlogic.independentpresenter");
 overrides.Add("video")->Add("amlindependentpresenter"); // removed dedicated parser ignores this
 assert(settings.LoadHidden(&overrides));
 assert(overrides.children.size()==6&&category->children.size()==3); // no input mutation
 assert(hidden->children.size()==4);
 assert(hidden->FirstChildElement("setting")->id=="unrelated.setting");
 assert(hidden->FirstChildElement("setting")->NextSiblingElement("setting")->id.empty());
 assert(hidden->FirstChildElement("coreelec")->children.size()==1);
 assert(hidden->FirstChildElement("coreelec")->FirstChildElement("amlogic.fastseek"));
 hiddenSuccess=false;assert(!settings.LoadHidden(&overrides));hiddenSuccess=true;
 TiXmlElement xml;bool updated=false;
 assert(!settings.Load(nullptr,updated)&&logs.empty());
 xmlSuccess=false;assert(!settings.Load(&xml,updated)&&logs.empty());xmlSuccess=true;
 subSuccess=false;assert(!settings.Load(&xml,updated)&&logs.empty());subSuccess=true;
 for(bool enabled:{false,true}){
  selected=enabled;assert(settings.Load(&xml,updated));
  assert(logs.back().find("source=gui")!=std::string::npos);
#ifdef HAS_LIBAMCODEC
  assert(logs.back().find(enabled?"|1|test|":"|0|test|")!=std::string::npos);
#else
  assert(logs.back().find("|0|test|")!=std::string::npos);
#endif
 }
 CRendererAML aml;Renderer software;
 CRenderManager manager;manager.m_pRenderer=&aml;
 selected=false;manager.PreInitOnMain();manager.ConfigureSelection();assert(!manager.m_amlPresenter);
 // A GUI edit and a renderer reconfigure cannot switch the current selection.
 selected=true;manager.ConfigureSelection();assert(!manager.m_amlPresenter);
 manager.m_renderState=STATE_CONFIGURED;
 const int priorReads=reads;manager.PreInitOnMain();assert(reads==priorReads);
 manager.ConfigureSelection();assert(!manager.m_amlPresenter);
 // Completed stop/new playback reaches the unconfigured PreInit boundary.
 manager.m_renderState=STATE_UNCONFIGURED;manager.PreInitOnMain();
 manager.ConfigureSelection();
#ifdef HAS_LIBAMCODEC
 assert(manager.m_amlPresenter && manager.m_amlIndependentPresenter);
 selected=false;manager.m_amlPresenter.reset();manager.ConfigureSelection();assert(manager.m_amlPresenter);
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
    group = tree.find("./section[@id='system']/category[@id='coreelec']/group[@id='1']")
    setting = group.find(f"setting[@id='{KEY}']")
    assert setting is not None and setting.attrib['type'] == 'boolean'
    assert setting.findtext('requirement') == 'HAVE_AMCODEC'
    assert setting.findtext('level') == '3' and setting.findtext('default') == 'false'
    assert setting.find('control').attrib['type'] == 'toggle'
    en, de = [catalog(ROOT / f'addons/resource.language.{lang}/resources/strings.po')
              for lang in ['en_gb', 'de_de']]
    for field in ['label','help']:
        entry = '#' + setting.attrib[field]
        assert en[entry]['msgid'] == de[entry]['msgid'] and de[entry]['msgstr']
    assert 'stopping playback' in en['#14310']['msgid']
    conditions = (ROOT/'xbmc/settings/SettingConditions.cpp').read_text()
    assert '#ifdef HAS_LIBAMCODEC\n  m_simpleConditions.insert("have_amcodec");' in conditions
    header=(ROOT/'xbmc/settings/Settings.h').read_text()
    constant=re.search(r'static constexpr auto '+CONSTANT+r' = "'+re.escape(KEY)+r'";',header).group()
    for name in ['AdvancedSettings.cpp','AdvancedSettings.h']:
        source=(ROOT/'xbmc/settings'/name).read_text()
        assert 'amlindependentpresenter' not in source.lower()
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
    settings_source=(ROOT/'xbmc/settings/Settings.cpp').read_text()
    loaded=extract(settings_source,'bool CSettings::Load(const TiXmlElement* root, bool& updated)')
    hidden_method=extract(settings_source,'bool CSettings::LoadHidden(const TiXmlElement* root)')
    harness=HARNESS.replace('@CONSTANT@',constant).replace('@ACTIVATION@',activation).replace('@METHODS@',loaded+'\n'+preinit+'\n'+hidden_method)
    with tempfile.TemporaryDirectory(prefix='presenter-setting-') as tmp:
        out=Path(tmp);(out/'test.cpp').write_text(harness)
        for target in [True,False]:
            defines=['-DHAS_LIBAMCODEC=1'] if target else []
            subprocess.run([os.environ.get('CXX','g++'),'-std=c++17','-Wall','-Wextra','-Werror',
                            '-fsanitize=address,undefined','-fno-omit-frame-pointer',*defines,
                            str(out/'test.cpp'),'-o',str(out/'test')],check=True)
            subprocess.run([str(out/'test')],check=True,timeout=20)
    print('PASS: GUI XML/PO references and uniqueness; actual load/override exclusion, playback snapshot and activation/fallback boundaries (AML/non-AML; host ASan/UBSan)')


if __name__ == '__main__':
    main()
