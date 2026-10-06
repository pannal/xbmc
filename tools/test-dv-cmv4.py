#!/usr/bin/env python3
"""Compile production CMv4 converter and live decoder settings methods with host stubs.

Exercises serialized RPU output, input cache invalidation and callback-to-decode
propagation. libdovi, settings, cache and native output are stubbed: this does not
validate actual RPU parsing, Kodi threading, GUI visibility or hardware output.
"""
import argparse
import ast
import pathlib
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]


def function(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def po_entries(text):
    result, key, field, values = {}, None, None, {}
    for line in text.splitlines() + ['']:
        if not line.strip():
            if key is not None:
                assert key not in result, f'duplicate PO context {key}'
                result[key] = values
            key, field, values = None, None, {}
        elif line.startswith(('msgctxt ', 'msgid ', 'msgstr ')):
            field, value = line.split(' ', 1)
            if field == 'msgctxt':
                key = ast.literal_eval(value)
            else:
                values[field] = ast.literal_eval(value)
        elif line.startswith('"') and field in values:
            values[field] += ast.literal_eval(line)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=pathlib.Path, default=ROOT)
    parser.add_argument('--overlay', type=pathlib.Path)
    parser.add_argument('--mutations', action='store_true')
    args = parser.parse_args()

    def read(path):
        p = args.overlay / path if args.overlay else args.root / path
        if not p.exists():
            p = args.root / path
        return p.read_text()

    header = read('xbmc/utils/BitstreamConverter.h')
    converter = read('xbmc/utils/BitstreamConverter.cpp')
    codec = read('xbmc/cores/VideoPlayer/DVDCodecs/Video/DVDVideoCodecAmlogic.cpp')
    settings_h = read('xbmc/settings/Settings.h')
    pq = read('xbmc/utils/HDR10PlusConvert.cpp')
    xml = ET.fromstring(read('system/settings/settings.xml'))
    ids = [s.get('id') for s in xml.findall('.//setting')]
    assert len(ids) == len(set(ids)), 'duplicate setting ID'
    controls = {s.get('id'): s for s in xml.findall('.//setting')}
    prefix = 'coreelec.amlogic.dolbyvision.cmv40.'
    assert controls[prefix + 'append'].findtext('default') == '3'
    assert [o.text for o in controls[prefix + 'append'].findall('./constraints/options/option')] == ['0', '1', '2', '3', '4']
    assert controls[prefix + 'smart.threshold'].findtext('default') == '20'
    trigger = controls[prefix + 'auto.trigger']
    assert trigger.findtext('default') == '0'
    assert [o.text for o in trigger.findall('./constraints/options/option')] == ['0', '1', '2', '3', '4']
    assert any(d.get('setting') == prefix + 'append' and d.text == '4' for d in trigger.findall('./dependencies/dependency'))
    english = po_entries(read('addons/resource.language.en_gb/resources/strings.po'))
    german = po_entries(read('addons/resource.language.de_de/resources/strings.po'))
    for control in [controls[prefix + 'append'], controls[prefix + 'smart.threshold'], trigger]:
        labels = [control.get('label'), control.get('help')] + [o.get('label') for o in control.findall('./constraints/options/option')]
        for label in labels:
            key = '#' + label
            assert english[key]['msgid'] == german[key]['msgid'], key
            assert german[key]['msgstr'], key
    assert 'Level 1' in english['#60333']['msgid']
    assert 'source peak' in english['#60334']['msgid']
    registered = codec[codec.index('RegisterCallback(this,'):codec.index('});', codec.index('RegisterCallback(this,'))]
    for suffix in ['APPEND', 'SMART_THRESHOLD', 'AUTO_TRIGGER', 'STRIP']:
        assert 'SETTING_COREELEC_AMLOGIC_DV_CMV40_' + suffix in registered
    assert 'SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR' in registered
    # Evaluate the actual XML dependency tree with the cached capability value.
    def evaluate(node, values, available):
        if node.tag == 'or':
            return any(evaluate(child, values, available) for child in node)
        if node.tag == 'and':
            return all(evaluate(child, values, available) for child in node)
        if node.get('name') == 'dvnewbackendavailable':
            return available
        equal = str(values[node.get('setting')]) == (node.text or '').strip()
        return not equal if node.get('operator') == '!is' else equal

    for available, enabled in [(a, e) for a in [False, True] for e in [False, True]]:
        for type_value in [0, 1, 2, 3, 4]:
            for mode in [0, 1, 2]:
                for vp in [0, 1]:
                    for output in [0, 1, 2, 3, 5]:
                        for append in [0, 1, 2, 3, 4]:
                            values = {'coreelec.amlogic.dolbyvision.type': type_value,
                                      'coreelec.amlogic.dolbyvision.mode': mode,
                                      'coreelec.amlogic.dolbyvision.video.processor': vp,
                                      'coreelec.amlogic.dolbyvision.vs10.dv': output,
                                      prefix + 'append': append,
                                      'coreelec.amlogic.dolbyvision.new.backend': 'true' if enabled else 'false'}
                            route_allowed = (mode != 2 and vp == 0 and
                                             (type_value == 0 or available and enabled and
                                              type_value in [1, 2, 4] and output in [0, 1]))
                            for suffix, mode_allowed in [('append', True), ('strip', True),
                                                         ('smart.threshold', append == 3),
                                                         ('auto.trigger', append == 4)]:
                                control = controls[prefix + suffix]
                                shown = all(evaluate(d[0], values, available) if len(d)
                                            else evaluate(d, values, available)
                                            for d in control.findall('./dependencies/dependency'))
                                assert shown == (route_allowed and mode_allowed), (suffix, values, available)
    conditions = read('xbmc/settings/SettingConditions.cpp')
    assert 'm_complexConditions.emplace("dvnewbackendavailable", DVNewBackendAvailable)' in conditions
    condition = function(conditions, 'bool DVNewBackendAvailable(')
    assert 'CSysfsPath' not in condition and 'aml_dv_new_backend_available()' in condition
    aml = read('xbmc/utils/AMLUtils.cpp')
    # Paired kernel ABI is unsigned decimal 0/1 (not bool parameter Y/N).
    # Check Kodi's contract without making the fixture depend on another repo.
    availability = function(aml, 'bool aml_support_dolby_vision(')
    assert 'new_backend.Get<std::string>()' in availability
    assert 'DVBackendState::ParseAvailability(*capabilityText)' in availability
    assert 'available && *available == 1' in availability
    cached_availability = function(aml, 'bool aml_dv_new_backend_available(')
    assert 'CSysfsPath' not in cached_availability
    opening = function(aml, 'void aml_dv_open(')
    closing = function(aml, 'void aml_dv_close(')
    open_guard = re.search(r'^  CDVCoreGuard dvlock\(__FUNCTION__\);', opening, re.M).start()
    close_guard = re.search(r'^  CDVCoreGuard dvlock\(__FUNCTION__\);', closing, re.M).start()
    assert open_guard < opening.index('CSysfsPath native_source')
    assert close_guard < closing.index('CSysfsPath native_source')
    assert closing.index('native_source.Set(0u)') < closing.index('DV_MODE_ON', close_guard)
    assert 'm_originalSourceHdrType.store(hints.hdrType)' in codec
    assert 'new CAMLCodec(m_processInfo, m_hints, m_originalSourceHdrType.load())' in codec
    native_codec = read('xbmc/cores/VideoPlayer/DVDCodecs/Video/AMLCodec.cpp')
    assert 'aml_dv_open(hints.hdrType, hints.bitdepth, hints.colorPrimaries, false, m_dvSession, m_originalSourceHdrType)' in native_codec
    visibility = read('xbmc/windowing/amlogic/DolbyVisionAML.cpp')
    assert 'set_visible(CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_AUTO_TRIGGER, show)' in visibility

    enums = re.search(r'enum DOVICMv40Mode : int\s*\{.*?\};', header, re.S).group()
    constants = '\n'.join(re.findall(r'constexpr double ST2084_.*?;', pq))
    setters = '\n'.join(function(header, 'void              ' + name + '(') for name in ['SetAppendCMv40', 'SetSmartBypassDisplayNits', 'SetSmartBypassThresholdPct', 'SetCMv40AutoTrigger', 'SetStripCMv40'])
    invalidation = function(header, 'void InvalidateDoViCache(')
    settings_constants = '\n'.join('static constexpr const char* ' + name + ' = "' + value + '";' for name, value in re.findall(r'(SETTING_[A-Z0-9_]+)\s*=\s*"([^"]+)"', settings_h) if value.startswith('coreelec.amlogic.dolbyvision.'))
    production = function(converter, 'bool CachedRpuInputMatches(')
    production += function(converter, 'inline bool AppendCMv40(')
    production += function(converter, 'inline bool StripCMv40(')
    process = function(converter, 'void CBitstreamConverter::ProcessDoViRpu(')
    apply = function(codec, 'void CDVDVideoCodecAmlogic::ApplyDynamicDoViSettings(')
    callbacks = function(codec, 'void CDVDVideoCodecAmlogic::UpdateAppendCMv40SettingCache(')
    callbacks += function(codec, 'void CDVDVideoCodecAmlogic::UpdateStripCMv40SettingCache(')
    callbacks += function(codec, 'void CDVDVideoCodecAmlogic::OnSettingChanged(')
    assert 'CSysfsPath' not in callbacks

    source = r'''
#define HAVE_LIBDOVI
#include <algorithm>
#include <atomic>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <string>
#include <vector>
constexpr int LOGINFO=1, LOGDEBUG=2, HEVC_NAL_UNSPEC62=62;
#define __MODULE_NAME__ "host"
struct CLog { template<class... T> static void Log(T...) {} };
enum DOVIMode {MODE_NONE=0};
enum DOVIELType {TYPE_NONE=0,TYPE_FEL=1};
''' + enums + constants + function(pq, 'int max_pq_to_nits(') + r'''
struct DoviRpuDataHeader {};
struct Level1 { uint16_t max_pq=3079; };
struct DoviVdrDmData {
  uint16_t source_max_pq=3079;
  struct {void* level254=nullptr; struct {size_t len=1;} level2; Level1* level1=nullptr;} dm_data;
};
struct DoviRpuOpaque {DoviRpuDataHeader header; DoviVdrDmData dm; Level1 level1; std::vector<uint8_t> bytes;};
struct DoviData {const uint8_t* data; size_t len;};
int parses=0, additions=0, removals=0;
bool parseFailure=false, headerFailure=false, dataFailure=false, appendFailure=false;
DoviRpuOpaque* dovi_parse_unspec62_nalu(uint8_t* buf,int len) {
  ++parses;if(parseFailure)return nullptr;assert(len==7);
  auto* p=new DoviRpuOpaque;p->bytes.assign(buf,buf+len);
  p->dm.source_max_pq=(buf[0]<<8)|buf[1];p->level1.max_pq=(buf[2]<<8)|buf[3];
  p->dm.dm_data.level1=buf[6]?&p->level1:nullptr;p->dm.dm_data.level2.len=buf[4];
  p->dm.dm_data.level254=buf[5]?p:nullptr;return p;
}
const DoviRpuDataHeader* dovi_rpu_get_header(DoviRpuOpaque* p) {return headerFailure?nullptr:&p->header;}
const DoviVdrDmData* dovi_rpu_get_vdr_dm_data(DoviRpuOpaque* p) {return dataFailure?nullptr:&p->dm;}
int dovi_rpu_add_cmv40_safe_default_metadata(DoviRpuOpaque* p) {++additions;if(appendFailure)return -1;p->bytes[5]=1;return 1;}
int dovi_rpu_remove_cmv40_metadata(DoviRpuOpaque* p) {++removals;p->bytes[5]=0;return 0;}
const DoviData* dovi_write_unspec62_nalu(DoviRpuOpaque* p) {
  auto* b=new uint8_t[p->bytes.size()];std::copy(p->bytes.begin(),p->bytes.end(),b);return new DoviData{b,p->bytes.size()};
}
void dovi_data_free(const DoviData* p) {delete[] p->data;delete p;}
void dovi_rpu_free_header(const DoviRpuDataHeader*) {}
void dovi_rpu_free_vdr_dm_data(const DoviVdrDmData*) {}
void dovi_rpu_free(DoviRpuOpaque* p) {delete p;}
struct DOVIFrameMetadata {double pts=0;};
struct DOVIStreamMetadata {std::string meta_version="2.9";};
struct Cache {
 DOVIStreamMetadata meta;
 void SetVideoDoViFrameMetadata(const DOVIFrameMetadata&) {}
 DOVIStreamMetadata GetVideoDoViStreamMetadata() {return meta;}
 void SetVideoDoViStreamMetadata(const DOVIStreamMetadata& m) {meta=m;}
};
struct Hints {DOVIELType dovi_el_type=TYPE_NONE;int dovi=0;bool is_dual_track=false;};
void ConvertDoVi(DOVIMode,bool,DoviRpuOpaque*,const DoviRpuDataHeader*,const DoviVdrDmData*,Hints&,Cache&,uint8_t*&,int32_t&,const DoviData*&) {assert(false);}
template<class... T> void PopulateDoviRpuInfo(T...) {}
bool IsValidPtsForInjection(double) {return false;}
bool AppendPtsToDoviRpuNalu(std::vector<uint8_t>&,uint64_t) {assert(false);return false;}
''' + production + r'''
struct CBitstreamConverter {
 DOVIMode m_convert_dovi=MODE_NONE;DOVICMv40Mode m_append_cmv40=CMV40_NONE,m_smart_last_effective=CMV40_SMART;
 bool m_first_frame=false,m_strip_cmv40=false,m_is_dt_dl=false,m_l5_override_active=false;
 int m_smart_display_nits=0,m_smart_threshold_pct=20,m_cmv40_auto_trigger=0;
 uint16_t m_l5_override_top=0,m_l5_override_bottom=0,m_l5_override_left=0,m_l5_override_right=0;
 Hints m_hints;Cache m_dataCacheCore;DOVIFrameMetadata m_cached_dovi_frame_metadata;
 std::vector<uint8_t> m_cached_dovi_rpu_in_nal,m_cached_dovi_rpu_out_nal;
 void ProcessDoViRpu(uint8_t*,int32_t,uint8_t**,int*,double);
 void SetLevel5Override(bool,uint16_t,uint16_t,uint16_t,uint16_t) {}
 static void BitstreamAllocAndCopy(uint8_t** out,int* size,const uint8_t*,uint32_t,const uint8_t* input,uint32_t len,uint8_t) {
  *out=static_cast<uint8_t*>(std::malloc(len));assert(*out);*size=len;std::copy(input,input+len,*out);
 }
''' + setters + invalidation + r'''
};
''' + process + r'''
struct CSettings {
''' + settings_constants + r'''
};
struct Settings {
 std::map<std::string,int> values;
 int GetInt(const std::string& k) {return values[k];}
 bool GetBool(const std::string& k) {return values[k]!=0;}
};
struct SettingsComponent {std::shared_ptr<Settings> settings=std::make_shared<Settings>();std::shared_ptr<Settings> GetSettings() {return settings;}};
SettingsComponent component;
struct CServiceBroker {static SettingsComponent* GetSettingsComponent() {return &component;}};
struct CSetting {std::string id;const std::string& GetId() const {return id;}};
enum class StreamHdrType {HDR_TYPE_NONE=0,HDR_TYPE_DOLBYVISION=1,HDR_TYPE_HDR10PLUS=2};
constexpr int DV_TYPE_DISPLAY_LED=0,DV_TYPE_PLAYER_LED_LLDV=1,DV_TYPE_PLAYER_LED_HDR=2,DV_TYPE_VS10_ONLY=3,DV_TYPE_PLAYER_LED_HDR2=4,DV_MODE_OFF=2;
constexpr int DOLBY_VISION_OUTPUT_MODE_IPT=0,DOLBY_VISION_OUTPUT_MODE_IPT_TUNNEL=1;
bool newBackendAvailable=false;
bool aml_dv_new_backend_available() {return newBackendAvailable;}
struct CDVDVideoCodecAmlogic {
 CBitstreamConverter* m_bitstream=nullptr;
 std::atomic<StreamHdrType> m_originalSourceHdrType{StreamHdrType::HDR_TYPE_DOLBYVISION};
 std::atomic<int> m_appendCMv40ModeSetting{0},m_smartDisplayNits{0},m_smartThresholdPct{20},m_cmv40AutoTriggerSetting{0};
 std::atomic<bool> m_stripCMv40Setting{false};
 DOVICMv40Mode m_appendCMv40ModeApplied=CMV40_NONE;bool m_stripCMv40Applied=false;
 std::atomic<bool> m_level5OverrideActiveSetting{false};std::atomic<uint64_t> m_level5OverrideValuesSetting{0};
 bool m_level5OverrideActiveApplied=false;uint64_t m_level5OverrideValuesApplied=0;
 void UpdateAppendCMv40SettingCache();void UpdateStripCMv40SettingCache();void ApplyDynamicDoViSettings();
 void UpdateLevel5OverrideSettingCache() {}
 void OnSettingChanged(const std::shared_ptr<const CSetting>&);
};
''' + callbacks + apply + r'''
std::vector<uint8_t> rpu(int source,int frame=3079,bool l2=true,bool native=false,bool l1=true) {
 return {static_cast<uint8_t>(source>>8),static_cast<uint8_t>(source),static_cast<uint8_t>(frame>>8),static_cast<uint8_t>(frame),static_cast<uint8_t>(l2),static_cast<uint8_t>(native),static_cast<uint8_t>(l1)};
}
std::vector<uint8_t> convert(CBitstreamConverter& c,std::vector<uint8_t> in) {
 uint8_t* out=nullptr;int size=0;c.ProcessDoViRpu(in.data(),in.size(),&out,&size,1.0);
 std::vector<uint8_t> result(out,out+size);std::free(out);assert(result.size()==in.size());return result;
}
void expect(CBitstreamConverter& c,const std::vector<uint8_t>& in,bool metadata) {assert(convert(c,in)[5]==metadata);}
void changed(CDVDVideoCodecAmlogic& codec,const char* id,int value) {
 component.settings->values[id]=value;codec.OnSettingChanged(std::make_shared<CSetting>(CSetting{id}));codec.ApplyDynamicDoViSettings();
}
int main() {
 static_assert(CMV40_NONE==0 && CMV40_NO_L2==1 && CMV40_ALWAYS==2 && CMV40_SMART==3 && CMV40_SOURCE_AUTO==4);
 CBitstreamConverter c;c.SetAppendCMv40(CMV40_SOURCE_AUTO);c.SetSmartBypassDisplayNits(1000);
 for(int trigger=0;trigger<=4;++trigger) {
  c.SetCMv40AutoTrigger(trigger);
  const int boundary[]={3079,3079,3388,3696,4095};
  expect(c,rpu(boundary[trigger]),true);
  if(trigger<4)expect(c,rpu(boundary[trigger]+5),false);
 }
 // Source metadata drives Auto; frame L1 drives Smart independently.
 c.SetCMv40AutoTrigger(1);expect(c,rpu(3388,2055),false);expect(c,rpu(2055,4095),true);
 expect(c,rpu(4095,4095,false),true);
 for(int invalid:{0,5000})expect(c,rpu(invalid),true);
 c.SetCMv40AutoTrigger(0);c.SetSmartBypassDisplayNits(0);expect(c,rpu(4095),true);
 c.SetCMv40AutoTrigger(99);expect(c,rpu(4095),true);
 // Authored CMv4 is retained and strip wins even if append was requested.
 int adds=additions;expect(c,rpu(3079,3079,true,true),true);assert(additions==adds);
 c.SetStripCMv40(true);expect(c,rpu(3079,3079,true,true),false);assert(removals>0 && additions==adds);
 c.SetStripCMv40(false);
 // Missing parser/header/data and mutation failure preserve original NAL.
 for(int kind=0;kind<4;++kind) {
  c.InvalidateDoViCache();parseFailure=kind==0;headerFailure=kind==1;dataFailure=kind==2;appendFailure=kind==3;
  auto in=rpu(3079);assert(convert(c,in)==in);
 }
 parseFailure=headerFailure=dataFailure=appendFailure=false;
 // Identical input reuses serialized output. Unchanged setters retain cache.
 c.SetAppendCMv40(CMV40_SOURCE_AUTO);c.SetCMv40AutoTrigger(1);c.SetSmartBypassDisplayNits(1000);
 expect(c,rpu(3388),false);int parsed=parses;
 c.SetCMv40AutoTrigger(1);c.SetSmartBypassDisplayNits(1000);c.SetSmartBypassThresholdPct(20);
 expect(c,rpu(3388),false);assert(parses==parsed);
 c.SetCMv40AutoTrigger(2);expect(c,rpu(3388),true);assert(parses==parsed+1);
 c.SetCMv40AutoTrigger(0);expect(c,rpu(3388),false);
 c.SetSmartBypassDisplayNits(2000);expect(c,rpu(3388),true);
 c.SetAppendCMv40(CMV40_SMART);c.SetSmartBypassDisplayNits(1000);c.SetSmartBypassThresholdPct(0);
 expect(c,rpu(3079,3100),false);c.SetSmartBypassThresholdPct(20);expect(c,rpu(3079,3100),true);
 // Original controls and Smart fallback remain effective.
 c.SetAppendCMv40(CMV40_NONE);expect(c,rpu(3079),false);
 c.SetAppendCMv40(CMV40_NO_L2);expect(c,rpu(3079),false);expect(c,rpu(3079,3079,false),true);
 c.SetAppendCMv40(CMV40_ALWAYS);expect(c,rpu(4095),true);
 c.SetAppendCMv40(CMV40_SMART);c.SetSmartBypassDisplayNits(0);expect(c,rpu(4095),true);
 expect(c,rpu(4095,4095,false),true);
 // Real callbacks update settings atomics; real Apply propagates same-mode
 // threshold, display and Auto trigger changes to the cached-input converter.
 CDVDVideoCodecAmlogic codec;codec.m_bitstream=&c;
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_NEW_BACKEND,1);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_SMART_THRESHOLD,0);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM,1000);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND,3);
 expect(c,rpu(3079,3100),false);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_SMART_THRESHOLD,20);expect(c,rpu(3079,3100),true);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM,100);expect(c,rpu(3079,3100),false);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_APPEND,4);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_AUTO_TRIGGER,1);expect(c,rpu(3388),false);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_AUTO_TRIGGER,2);expect(c,rpu(3388),true);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE,1);expect(c,rpu(3388),false);
 newBackendAvailable=true;
 for(int type:{1,2,4}) {
  changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE,type);expect(c,rpu(3388),true);
  changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_NEW_BACKEND,0);expect(c,rpu(3388),false);
  changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_NEW_BACKEND,1);expect(c,rpu(3388),true);
  changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_DV,3);expect(c,rpu(3388),false);
  changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_VS10_DV,0);expect(c,rpu(3388),true);
 }
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE,3);expect(c,rpu(3388),false);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE,1);expect(c,rpu(3388),true);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE,2);expect(c,rpu(3388),false);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_MODE,1);expect(c,rpu(3388),true);
 codec.m_originalSourceHdrType.store(StreamHdrType::HDR_TYPE_HDR10PLUS);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE,1);expect(c,rpu(3388),false);
 codec.m_originalSourceHdrType.store(StreamHdrType::HDR_TYPE_DOLBYVISION);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE,1);expect(c,rpu(3388),true);
 newBackendAvailable=false;
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE,1);expect(c,rpu(3388),false);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_TYPE,0);expect(c,rpu(3388),true);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR,1);expect(c,rpu(3388),false);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_VIDEO_PROCESSOR,0);expect(c,rpu(3388),true);
 changed(codec,CSettings::SETTING_COREELEC_AMLOGIC_DV_CMV40_STRIP,1);expect(c,rpu(3388,3079,true,true),false);
 parsed=parses;codec.ApplyDynamicDoViSettings();expect(c,rpu(3388,3079,true,true),false);assert(parses==parsed);
 std::cout<<"CMv4 production-method regression checks passed ("<<parses<<" parses)\n";
}
'''
    with tempfile.TemporaryDirectory(prefix='dv-cmv4-') as tmp:
        path = pathlib.Path(tmp)

        def run(text, name, success=True):
            cpp = path / (name + '.cpp')
            exe = path / name
            cpp.write_text(text)
            subprocess.run(['g++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-Wdouble-promotion', '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie', '-I' + str(args.root / 'xbmc'), str(cpp), '-o', str(exe)], check=True)
            result = subprocess.run([str(exe)], capture_output=True, text=True)
            if success:
                assert result.returncode == 0, result.stdout + result.stderr
                print(result.stdout.strip())
            else:
                assert result.returncode != 0, name + ' mutation escaped'
                print(name + ' mutation rejected')

        run(source, 'production')
        # Compile the real cached capability/condition and provenance sysfs
        # blocks; only the kernel boundary is stubbed. A converted effective
        # hdrType must never override the captured original source.
        availability = function(aml, 'bool aml_support_dolby_vision(')
        open_start = opening.index('  CSysfsPath native_source')
        open_end = opening.index('  aml_dv_dump_state("dv_open/pre")', open_start)
        close_start = closing.index('  CSysfsPath native_source')
        close_end = closing.index('  aml_dv_dump_state("dv_close/pre")', close_start)
        capability_source = r'''
#define HAS_LIBAMCODEC
#include <atomic>
#include <cassert>
#include <cstdint>
#include <iostream>
#include <map>
#include <optional>
#include <string>
#include <type_traits>
#include "utils/DVBackendState.h"
constexpr int LOGDEBUG=1;
struct CLog {template<class... T> static void Log(T...) {}};
using SettingConstPtr=const void*;
struct CSysfsPath {
 static std::map<std::string,unsigned int> nodes;
 static int reads;
 std::string path;
 explicit CSysfsPath(const char* name):path(name) {}
 bool Exists() const {return nodes.count(path);}
 template<class T> std::optional<T> Get() const {
  ++reads;if constexpr(std::is_same_v<T,std::string>)return nodes.count(path)?std::optional<T>(std::to_string(nodes[path])):std::nullopt;
  else return static_cast<T>(nodes[path]);
 }
 template<class T> void Set(T value) {nodes[path]=value;}
};
std::map<std::string,unsigned int> CSysfsPath::nodes;
int CSysfsPath::reads=0;
static std::atomic<bool> s_dvNewBackendAvailable{false};
static std::atomic<int> s_dvNewBackendAvailability{-1};
''' + cached_availability + availability + condition + r'''
enum class StreamHdrType {HDR_TYPE_NONE,HDR_TYPE_DOLBYVISION,HDR_TYPE_HDR10PLUS};
void open(StreamHdrType hdrType,StreamHdrType originalSourceHdrType,bool swDecoded) {
 (void)hdrType;
 (void)originalSourceHdrType;
''' + opening[open_start:open_end] + r'''
}
void close() {
''' + closing[close_start:close_end] + r'''
}
int main() {
 assert(!aml_dv_new_backend_available());assert(!DVNewBackendAvailable("","",nullptr,nullptr));
 auto& nodes=CSysfsPath::nodes;
 nodes["/sys/class/amdolby_vision/support_info"]=7;
 nodes["/sys/module/amdolby_vision/parameters/dv_new_backend_available"]=1;
 assert(aml_support_dolby_vision());assert(aml_dv_new_backend_available());
 int reads=CSysfsPath::reads;
 for(int i=0;i<100;++i)assert(DVNewBackendAvailable("","",nullptr,nullptr));
 assert(CSysfsPath::reads==reads);
 // The loader contract is sampled once; later cache reads never access native state.
 assert(aml_support_dolby_vision());assert(CSysfsPath::reads==reads);
 const char* name="/sys/module/amdolby_vision/parameters/xbmc_dv_source_native";nodes[name]=0;
 open(StreamHdrType::HDR_TYPE_DOLBYVISION,StreamHdrType::HDR_TYPE_HDR10PLUS,false);assert(nodes[name]==0);
 open(StreamHdrType::HDR_TYPE_DOLBYVISION,StreamHdrType::HDR_TYPE_DOLBYVISION,false);assert(nodes[name]==1);
 close();assert(nodes[name]==0);
 open(StreamHdrType::HDR_TYPE_DOLBYVISION,StreamHdrType::HDR_TYPE_DOLBYVISION,true);assert(nodes[name]==0);
 open(StreamHdrType::HDR_TYPE_NONE,StreamHdrType::HDR_TYPE_NONE,false);assert(nodes[name]==0);
 nodes.erase(name);open(StreamHdrType::HDR_TYPE_DOLBYVISION,StreamHdrType::HDR_TYPE_DOLBYVISION,false);assert(!nodes.count(name));
 std::cout<<"Cached capability condition and original-source provenance passed\n";
}
'''
        run(capability_source, 'capability-provenance')
        # Separate programs retain the production one-time initialization guard.
        for name, setup in [('missing-new-node', 'nodes.erase("/sys/module/amdolby_vision/parameters/dv_new_backend_available");'),
                            ('legacy-only', 'nodes["/sys/module/amdolby_vision/parameters/dv_new_backend_available"]=0;'),
                            ('missing-original', 'nodes["/sys/class/amdolby_vision/support_info"]=0;')]:
            unavailable = capability_source.replace('assert(aml_support_dolby_vision());assert(aml_dv_new_backend_available());',
                setup + 'aml_support_dolby_vision();assert(!aml_dv_new_backend_available());return 0;')
            run(unavailable, name)
        if args.mutations:
            converted = capability_source.replace('originalSourceHdrType == StreamHdrType::HDR_TYPE_DOLBYVISION', 'hdrType == StreamHdrType::HDR_TYPE_DOLBYVISION')
            assert converted != capability_source
            run(converted, 'converted-source-provenance', False)
        if args.mutations:
            mutated = source.replace('if (m_smart_display_nits != nits) InvalidateDoViCache();', '')
            assert mutated != source
            run(mutated, 'missing-display-cache-invalidation', False)
            # Reproduce the original same-mode propagation omission.
            mutated_apply = apply.replace('m_bitstream->SetSmartBypassThresholdPct(m_smartThresholdPct.load());', 'if (mode != m_appendCMv40ModeApplied) m_bitstream->SetSmartBypassThresholdPct(m_smartThresholdPct.load());')
            assert mutated_apply != apply
            run(source.replace(apply, mutated_apply), 'original-same-mode-headroom', False)
            mutated = source.replace('max_pq_to_nits(sourcePq) > threshold', 'max_pq_to_nits(sourcePq) >= threshold')
            assert mutated != source
            run(mutated, 'exclusive-source-boundary', False)
    print('XML, callbacks, visibility registration and English/German controls passed')


if __name__ == '__main__':
    main()
