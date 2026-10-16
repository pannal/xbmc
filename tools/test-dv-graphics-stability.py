#!/usr/bin/env python3
"""Execute SDR graphics setting helpers and real deferred/native dispatch.

Extracts production functions; reuses existing native-admission fixtures without
changing them. Sysfs/settings are recording substitutes. Host ASan/UBSan checks
do not establish CE compiler acceptance, kernel behavior or display luminance.
"""
import argparse
import ast
import itertools
import os
from pathlib import Path
import re
import runpy
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
native = runpy.run_path(str(ROOT / 'tools/test-aml-native-settings.py'))
continuation = runpy.run_path(str(ROOT / 'tools/test-aml-native-continuation.py'))
function, run = native['function'], native['run']
ID = 'coreelec.amlogic.dolbyvision.graphics.stable'
KEY = 'SETTING_COREELEC_AMLOGIC_DV_GRAPHICS_STABLE'


RECORDING = r'''
bool graphicsNode = false;
int graphicsExists = 0;
struct CSysfsPath {
 std::string path;
 explicit CSysfsPath(const char* value):path(value){}
 bool Exists(){
  assert(!inCallback && session && !session->AcquireDecoder());
  assert(path=="/sys/module/amdolby_vision/parameters/dv_graphics_stable");
  ++graphicsExists;return graphicsNode;
 }
 void Set(bool value){assert(graphicsNode);effect("graphics",value);}
};
@HELPERS@
using SettingConstPtr=std::shared_ptr<const CSetting>;
#define HAS_LIBAMCODEC
@CONDITION@
#undef HAS_LIBAMCODEC
@NO_AML_CONDITION@
'''

EXTRA_TESTS = r'''
void graphics_helper_policy(){
 CDolbyVisionAML dv;CAMLSession s;init(dv,s);
 s_dvGraphicsStableAvailable=false;graphicsExists=0;graphicsNode=true;
 assert(!aml_dv_graphics_stability_supported());
 assert(!DVGraphicsStableAvailable("","",nullptr,nullptr));
 assert(!DVGraphicsStableAvailableWithoutAML("","",nullptr,nullptr));
 assert(graphicsExists==0); // All settings properties are cached CPU reads.
 for(bool node:{false,true})for(bool value:{false,true}){
  graphicsNode=node;settings()->Put(KEY(GRAPHICS_STABLE),value);
  auto intent=storage.values;const int previous=graphicsExists;effects.clear();
  auto request=CAMLSession::FenceNative();assert(CAMLSession::TryBeginNative(request));
  aml_dv_apply_graphics_stability();assert(CAMLSession::EndNative(request));
  assert(graphicsExists==previous+1 && storage.values==intent);
  assert(aml_dv_graphics_stability_supported()==node);
  assert(DVGraphicsStableAvailable("","",nullptr,nullptr)==node);
  assert(!DVGraphicsStableAvailableWithoutAML("","",nullptr,nullptr));
  const std::vector<std::pair<std::string,int>> expected=node?
   std::vector<std::pair<std::string,int>>{{"graphics",value}}:
   std::vector<std::pair<std::string,int>>{};
  assert(snapshot()==expected);
 }
 // Removal is observed by the next admitted apply, never from GUI readers.
 assert(aml_dv_graphics_stability_supported());graphicsNode=false;
 const int previous=graphicsExists;
 assert(DVGraphicsStableAvailable("","",nullptr,nullptr)&&graphicsExists==previous);
 auto request=CAMLSession::FenceNative();assert(CAMLSession::TryBeginNative(request));
 effects.clear();aml_dv_apply_graphics_stability();assert(CAMLSession::EndNative(request));
 assert(!aml_dv_graphics_stability_supported()&&snapshot().empty());
 assert(settings()->GetBool(KEY(GRAPHICS_STABLE))); // Missing API preserves intent.
 drain(dv);
}
void graphics_deferred_policy(){
 for(bool node:{false,true})for(bool value:{false,true}){
  CDolbyVisionAML dv;CAMLSession s;init(dv,s);graphicsNode=node;
  enabled=false;playing=false;settings()->Put(KEY(TV_PRESET),TV_PRESET_AUTO);
  auto permit=std::make_unique<CAMLSession::Permit>(s.AcquireDecoder());assert(*permit);
  // Queue the opposite intent, then change storage before native admission.
  settings()->Put(KEY(GRAPHICS_STABLE),value);
  changed(KEY(GRAPHICS_STABLE),!value);
  until([&]{return !s.AcquireDecoder();});assert(snapshot().empty());
  settings()->Put(KEY(GRAPHICS_STABLE),value);
  // Marker completes the batch even when the graphics API is absent.
  changed(CSettings::SETTING_COREELEC_AUDIO_DDR_PRIORITY,1);
  std::promise<void> done;auto completed=done.get_future();
  effectHook=[&](const std::string& name,int){if(name=="ddr")done.set_value();};
  permit.reset();assert(completed.wait_for(3s)==std::future_status::ready);drain(dv);
  std::vector<std::pair<std::string,int>> expected;
  if(node)expected.emplace_back("graphics",value);
  expected.emplace_back("ddr",1);
  assert(snapshot()==expected && !dv.m_deferredWork.Failure());
  assert(settings()->GetInt(KEY(TV_PRESET))==TV_PRESET_AUTO);
  assert(settings()->GetBool(KEY(GRAPHICS_STABLE))==value);
  assert(aml_dv_graphics_stability_supported()==node);
 }
 // Retirement revokes the queued intent without touching sysfs.
 CDolbyVisionAML dv;CAMLSession s;init(dv,s);graphicsNode=true;
 auto permit=s.AcquireDecoder();assert(permit);
 const int previous=graphicsExists;
 changed(KEY(GRAPHICS_STABLE),true);until([&]{return !s.AcquireDecoder();});
 drain(dv);assert(snapshot().empty() && graphicsExists==previous);
}
'''


def native_code():
    aml = (ROOT / 'xbmc/utils/AMLUtils.cpp').read_text()
    conditions = (ROOT / 'xbmc/settings/SettingConditions.cpp').read_text()
    declaration = re.search(r'^static std::atomic<bool> s_dvGraphicsStableAvailable[^\n]+',
                            aml, re.M)
    assert declaration
    helpers = declaration.group() + '\n' + '\n'.join(function(aml, sig) for sig in (
        'bool aml_dv_graphics_stability_supported()',
        'void aml_dv_apply_graphics_stability()'))
    condition = function(conditions, 'bool DVGraphicsStableAvailable(')
    recording = (RECORDING.replace('@HELPERS@', helpers)
                 .replace('@CONDITION@', condition)
                 .replace('@NO_AML_CONDITION@', condition.replace(
                     'DVGraphicsStableAvailable(', 'DVGraphicsStableAvailableWithoutAML(', 1)))
    code = native['source']()
    # The shared fixture records unrelated hardware effects; replace its helper
    # stub with production code for this setting's actual sysfs/cache contract.
    if 'void aml_dv_apply_graphics_stability()' in code:
        code = code.replace(function(code, 'void aml_dv_apply_graphics_stability()'), '')
    anchor = 'void aml_apply_hdr10_metadata_limits(){effect("limits");}'
    assert code.count(anchor) == 1
    code = code.replace(anchor, recording + '\n' + anchor)
    anchor = 'int main(){preset_callback_policy();'
    assert code.count(anchor) == 1
    return code.replace(anchor, EXTRA_TESTS + '\nint main(){graphics_helper_policy();'
                        'graphics_deferred_policy();preset_callback_policy();')


def setup_code():
    code = continuation['source']()
    if 'void aml_dv_apply_graphics_stability()' in code:
        code = code.replace(function(code, 'void aml_dv_apply_graphics_stability()'), '')
    anchor = 'void aml_dv_enable_new_backend(){}'
    assert code.count(anchor) == 1
    code = code.replace(anchor, anchor + '\nint graphicsApplies=0;'
                        '\nvoid aml_dv_apply_graphics_stability(){++graphicsApplies;}')
    tests = r'''
void graphics_setup_policy(){
 ready();supported=false;values.overrideEdid=false;
 int previous=graphicsApplies;
 CDolbyVisionAML absent;assert(absent.Setup());
 assert(graphicsApplies==previous+1&&!absent.m_registered);
 assert(values.callbacks.count(CSettings::SETTING_COREELEC_AMLOGIC_DV_GRAPHICS_STABLE));
 supported=true;previous=graphicsApplies;
 startupHook=[&]{assert(graphicsApplies==previous+1);};
 CDolbyVisionAML present;assert(present.Setup());
 assert(graphicsApplies==previous+1&&present.m_registered);
}
'''
    anchor = 'int main(){display_nesting();'
    assert code.count(anchor) == 1
    return code.replace(anchor, tests + '\nint main(){graphics_setup_policy();display_nesting();')


def entries(path):
    result, fields, key = {}, {}, None
    for line in path.read_text().splitlines() + ['']:
        if not line:
            if fields.get('msgctxt', '').startswith('#'):
                assert fields['msgctxt'] not in result, (path, fields['msgctxt'])
                result[fields['msgctxt']] = fields
            fields, key = {}, None
        elif line.startswith(('msgctxt ', 'msgid ', 'msgstr ')):
            key, value = line.split(' ', 1)
            fields[key] = ast.literal_eval(value)
        elif line.startswith('"') and key:
            fields[key] += ast.literal_eval(line)
    return result


def evaluate(node, properties, values):
    if node.tag in ('and', 'dependency'):
        return all(evaluate(child, properties, values) for child in node)
    if node.tag == 'or':
        return any(evaluate(child, properties, values) for child in node)
    assert node.tag == 'condition'
    if node.get('on') == 'property':
        return properties[node.get('name')]
    actual, target = values[node.get('setting')], int(node.text)
    assert node.get('operator') in ('is', '!is')
    return (actual == target) == (node.get('operator') == 'is')


def settings_check():
    tree = ET.parse(ROOT / 'system/settings/settings.xml')
    identifiers = [s.get('id') for s in tree.findall('.//setting')]
    assert len(identifiers) == len(set(identifiers))
    node = tree.find(f'.//category[@id="coreelec"]/group/setting[@id="{ID}"]')
    assert node is not None and node.get('type') == 'boolean'
    assert node.findtext('default') == 'false' and node.findtext('visible') == 'false'
    assert node.findtext('requirement') == 'HAVE_AMCODEC' and node.findtext('level') == '2'
    assert node.get('parent') == 'coreelec.amlogic.dolbyvision.mode'
    assert node.find('control').attrib == {'type': 'toggle'}
    dependencies = node.findall('dependencies/dependency')
    assert dependencies and all(d.get('type') == 'visible' for d in dependencies)
    cases = 0
    for capability, backend, mode, output_type, vp, output in itertools.product(
            (False, True), (False, True), range(3), range(5), range(8), (0, 1, 2, 3, 4, 5, 99)):
        properties = {'dvgraphicsstableavailable': capability, 'dvnewbackendavailable': backend}
        values = dict(zip(('mode', 'type', 'video.processor', 'vs10.dv'),
                          (mode, output_type, vp, output)))
        values = {'coreelec.amlogic.dolbyvision.' + key: value for key, value in values.items()}
        actual = all(evaluate(d, properties, values) for d in dependencies)
        expected = capability and backend and mode != 2 and output_type == 0 and vp == 0 and output in (0, 1)
        assert actual == expected, (properties, values, actual, expected)
        cases += 1
    en = entries(ROOT / 'addons/resource.language.en_gb/resources/strings.po')
    de = entries(ROOT / 'addons/resource.language.de_de/resources/strings.po')
    for field in ('label', 'help'):
        entry = '#' + node.get(field)
        assert en[entry]['msgid'] == de[entry]['msgid'] and de[entry]['msgstr']
    help_text = en['#' + node.get('help')]['msgid']
    assert 'Use only if' in help_text and 'unwanted' in help_text and 'SDR-only' in help_text
    assert 'native graphics tone mapping' in help_text and 'Leave disabled' in help_text
    assert 'budget' not in help_text.lower() and 'believed' not in help_text.lower()
    german_help = de['#' + node.get('help')]['msgstr'].lower()
    assert 'budget' not in german_help and 'vermutlich' not in german_help
    print(f'PASS: {cases} XML visibility combinations, default off, native outputs 0/1, unique settings and EN/DE')


def wiring_check():
    aml = (ROOT / 'xbmc/utils/AMLUtils.cpp').read_text()
    dv = (ROOT / 'xbmc/windowing/amlogic/DolbyVisionAML.cpp').read_text()
    conditions = (ROOT / 'xbmc/settings/SettingConditions.cpp').read_text()
    settings = (ROOT / 'xbmc/settings/Settings.h').read_text()
    assert re.search(rf'{KEY}\s*=\s*"{re.escape(ID)}"', settings)
    assert 'm_complexConditions.emplace("dvgraphicsstableavailable", DVGraphicsStableAvailable)' in conditions
    assert KEY in function(dv, 'static void set_dv_settings_visible(')
    setup = function(dv, 'bool CDolbyVisionAML::Setup(')
    assert setup.index('native.TryBegin()') < setup.index('aml_dv_apply_graphics_stability();')
    assert setup.index('aml_dv_apply_graphics_stability();') < setup.index('if (!dv_supported && !override_edid)')
    for signature in ('unsigned int aml_dv_on(', 'void aml_dv_start()'):
        method = function(aml, signature)
        assert method.index('CDVCoreGuard dvlock') < method.index('aml_dv_apply_graphics_stability();')
    assert function(aml, 'unsigned int aml_dv_on(').index('aml_dv_apply_graphics_stability();') < function(
        aml, 'unsigned int aml_dv_on(').index('"/sys/module/amdolby_vision/parameters/dolby_vision_enable"')
    assert function(aml, 'void aml_dv_start()').index('aml_dv_apply_graphics_stability();') < function(
        aml, 'void aml_dv_start()').index('_dv_start_locked();')
    print('PASS: capability registration, admitted startup/engage/wake and group visibility wiring')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    # LeakSanitizer cannot run under the sandbox's ptrace supervision. Keep
    # AddressSanitizer and UndefinedBehaviorSanitizer active, as other fixtures do.
    os.environ.setdefault('ASAN_OPTIONS', 'detect_leaks=0')
    settings_check()
    wiring_check()
    code = native_code()
    run(code)
    print('PASS: production helper/property, missing API and saved intent, deferred freshness/preset/retirement')
    setup = setup_code()
    run(setup)
    print('PASS: production Setup applies and registers setting before unsupported-DV return and startup')
    if args.negative_controls:
        for label, old, new in (
            ('missing-driver write', '  if (supported)\n    control.Set(', '  if (true)\n    control.Set('),
            ('ignores saved off', 'control.Set(settings()->GetBool(CSettings::' + KEY + '));', 'control.Set(true);'),
            ('GUI performs sysfs read', 'return s_dvGraphicsStableAvailable.load(std::memory_order_acquire);', 'return CSysfsPath("/sys/module/amdolby_vision/parameters/dv_graphics_stable").Exists();'),
            ('synchronous callback effect', 'schedule_native_setting_apply(setting->GetId());', 'apply_native_setting(setting->GetId());'),
            ('bypass native admission', 'm_deferredWork.ScheduleNative(std::chrono::milliseconds(0)', 'm_deferredWork.Schedule(std::chrono::milliseconds(0)'),
            ('stale capability', 's_dvGraphicsStableAvailable.store(supported, std::memory_order_release);', '(void)supported;'),
            ('setting enters preset policy', 'setting->GetId() == CSettings::' + KEY + ' ||\n      ', ''),
        ):
            assert code.count(old) == 1, label
            run(code.replace(old, new, 1), negative=True)
            print('REJECTED:', label)
        old = '  aml_dv_apply_graphics_stability();'
        assert setup.count(old) == 1
        run(setup.replace(old, '  /* omitted startup setting */', 1), negative=True)
        print('REJECTED: omitted startup apply')


if __name__ == '__main__':
    main()
