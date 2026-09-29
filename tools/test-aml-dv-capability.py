#!/usr/bin/env python3
"""Execute fresh DV capability parsing and both production VSVDB calculations.

Sysfs and settings are recording substitutes; no CE/driver coverage is claimed.
"""
import argparse
from pathlib import Path
import re
import runpy

ROOT = Path(__file__).resolve().parents[1]
fixture = runpy.run_path(str(ROOT / 'tools/test-aml-native-continuation.py'))
function, run = fixture['function'], fixture['run']

PREFIX = r'''
#include <cassert>
#include <cstring>
#include <cstdio>
#include <iomanip>
#include <iostream>
#include <map>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
struct CLog {template<class... T> static void Log(T&&...) {}};
constexpr int LOGINFO=0,LOGERROR=1,LOGDEBUG=2;
@CAP@
std::map<std::string,std::optional<std::string>> files;
int reads=0;
struct CSysfsPath {
 std::string path;
 explicit CSysfsPath(const char* p):path(p){}
 bool Exists(){return files.count(path);}
 template<class T> std::optional<T> Get(){++reads;return files.at(path);}
};
@READ@
enum DV_TYPE {DV_TYPE_DISPLAY_LED,DV_TYPE_PLAYER_LED_LLDV};
struct CSettings {@IDS@};
struct Settings {
 std::map<std::string,int> values;
 std::string payload;
 int GetInt(const char* key){return values[key];}
 bool GetBool(const char* key){return GetInt(key)!=0;}
 void SetInt(const char* key,int v){values[key]=v;}
 void SetString(const char*,const std::string& s){payload=s;}
 std::string m_dvCustomVsvdb;
 Settings* GetAdvancedSettings(){return this;}
} storage;
Settings* settings(){return &storage;}
struct CServiceBroker {static Settings* GetSettingsComponent(){return &storage;}};
@CALCULATIONS@
@ROUTE@
'''
TESTS = r'''
const std::string dvPath="/sys/devices/virtual/amhdmitx/amhdmitx0/dv_cap";
const std::string edidPath="/sys/class/amhdmitx/amhdmitx0/edid";
std::string valid(int version){
 return "VSVDB Version: V"+std::to_string(version)+"\nLength: 6\nVSVDB: 0123456789abcd\n"
        "TmaxM: 19nti\nTmaxQ: 10pqi\nRx: 11 rxi\nRy: 12 ryi\nGx: 13 gxi\nGy: 14 gyi\nBx: 2 bxi\nBy: 3 byi\n";
}
void defaults(bool inject=false,bool overrideEdid=false,int cs=3){
 storage.values.clear();storage.payload.clear();
 storage.values[CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS]=cs;
 storage.values[CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM]=1000;
 storage.values[CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_INJECT]=inject;
 storage.values[CSettings::SETTING_COREELEC_AMLOGIC_DV_OVERRIDE_EDID]=overrideEdid;
}
void Empty(const AMLDVCapability& cap,AMLDVCapability::Status status){
 assert(cap.status==status&&!cap.Valid());
 assert(cap.dv_ver_i==0&&cap.dv_max_v1_i==0&&cap.dv_max_v2_i==0&&cap.dv_rx_i==0&&cap.dv_by_i==0&&cap.dv_vsvdb_s.empty());
}
int main(){
 files[edidPath]="Rx Manufacturer Name: LG\nOther: value\n";
 files[dvPath]=valid(1);auto first=aml_read_dv_cap();
 assert(first.Valid()&&first.edid_pnpid=="LG"&&first.dv_ver_i==1&&first.dv_by_i==3);
 assert(reads==2&&first.dv_vsvdb_s=="0123456789abcd");
 defaults();const int before=reads;CalculateVSVDBPayload(first);
 assert(reads==before&&storage.payload=="2726014D1A1C5B");
 assert(storage.GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM)==1050);
 files[dvPath]=valid(2);files[edidPath]="Rx Manufacturer Name: SONY\nOther: value\n";
 auto second=aml_read_dv_cap();assert(second.Valid()&&second.edid_pnpid=="SONY");
 assert(first.edid_pnpid=="LG"&&first.dv_ver_i==1); // retained result never mutates
 defaults();CalculateVSVDBPayload_2(second);
 assert(storage.payload=="4903521A1C5A63");
 assert(storage.GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM)==447);
 // Missing and invalid reads do not inherit the old sink, even after a late field failure.
 files.erase(dvPath);files.erase(edidPath);auto missing=aml_read_dv_cap();
 Empty(missing,AMLDVCapability::Status::UNAVAILABLE);assert(missing.edid_pnpid.empty());
 files[dvPath]=std::nullopt;Empty(aml_read_dv_cap(),AMLDVCapability::Status::UNAVAILABLE);
 files[dvPath]="The Rx don't support DolbyVision";auto unsupported=aml_read_dv_cap();
 Empty(unsupported,AMLDVCapability::Status::UNSUPPORTED);
 auto largeIndex=valid(1);largeIndex.replace(largeIndex.find("19nti"),5,"128nti");
 auto shortPayload=valid(2);shortPayload.erase(shortPayload.find("0123456789abcd"),7);
 auto negativeIndex=valid(2);negativeIndex.replace(negativeIndex.find("10pqi"),5,"-1pqi");
 for(const auto& bad:std::vector<std::string>{"",valid(2).substr(0,valid(2).find("By: ")),
       largeIndex,negativeIndex,shortPayload}){
   files[dvPath]=bad;auto invalid=aml_read_dv_cap();Empty(invalid,AMLDVCapability::Status::INVALID);
   defaults();CalculateVSVDBPayload_2(invalid);const auto fallback=storage.payload;
   assert(storage.GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_CS)==1);
   defaults();CalculateVSVDBPayload_2(unsupported);assert(storage.payload==fallback);
 }
 files[dvPath]=valid(0);
 auto v0=valid(0);v0.replace(v0.find("10pqi"),5,"1000pqi");v0.replace(v0.find("11 rxi"),6,"1000 rxi");
 files[dvPath]=v0;auto legacy=aml_read_dv_cap();assert(legacy.Valid()&&legacy.dv_max_v2_i==1000&&legacy.dv_rx_i==1000);
 // Manual settings/override do not depend on any old EDID, and routing remains unchanged.
 for(int cs:{0,1,2,10}){
   defaults(true,true,cs);CalculateVSVDBPayload_2(second);const auto manual=storage.payload;
   defaults(true,true,cs);CalculateVSVDBPayload_2(missing);assert(storage.payload==manual);
   defaults(true,true,cs);set_vsvdb_payload_ver(DV_TYPE_DISPLAY_LED,1000,2000,second);assert(storage.payload==manual);
 }
 defaults(true,false);CalculateVSVDBPayload_2(first);assert(storage.GetInt(CSettings::SETTING_COREELEC_AMLOGIC_DV_VSVDB_MAX_LUM)==1000);
 defaults();set_vsvdb_payload_ver(DV_TYPE_PLAYER_LED_LLDV,1000,2000,first);assert(storage.payload=="2726014D1A1C5B");
 std::cout<<"PASS: fresh DV result, failures, immutable sink replacement, V1/V2 payloads and override policy\n";
}
'''

def source():
    aml=(ROOT/'xbmc/utils/AMLUtils.cpp').read_text()
    dv=(ROOT/'xbmc/windowing/amlogic/DolbyVisionAML.cpp').read_text()
    header=(ROOT/'xbmc/utils/AMLUtils.h').read_text()
    cap=function(header,'struct AMLDVCapability')+';'
    calculations=dv[dv.index('static double colour_space_data'):dv.index('static bool force_modes()')]
    route=function(aml,'void set_vsvdb_payload_ver(')
    reader=function(aml,'AMLDVCapability aml_read_dv_cap()')
    ids=sorted(set(re.findall(r'CSettings::(SETTING_\w+)',calculations+route)))
    code=PREFIX.replace('@IDS@','\n'.join(f'static constexpr const char* {i}="{i}";' for i in ids))
    for key,value in [('CAP',cap),('READ',reader),('CALCULATIONS',calculations),('ROUTE',route)]:
        code=code.replace('@'+key+'@',value)
    # The admitted aml_dv_on operation must reuse the same result for colorimetry and payload.
    on=function(aml,'unsigned int aml_dv_on(')
    assert on.count('aml_read_dv_cap()')==1
    assert 'cap.Valid() && cap.dv_ver_i == 2' in on
    assert 'set_vsvdb_payload_ver(dv_type, max_lum_nits_value, source_max_pq, cap);' in on
    assert 'xbmc_dv_cap::' not in aml+dv
    return code+TESTS

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--negative-controls',action='store_true');args=parser.parse_args()
    code=source();run(code)
    if args.negative_controls:
        for label,old,new in [
          ('partial parse publication','result.status = AMLDVCapability::Status::INVALID;','result = candidate; result.status = AMLDVCapability::Status::INVALID;'),
          ('missing sink reuses old result','AMLDVCapability result;','static AMLDVCapability result;'),
          ('accept out-of-range LUT index','value < 0 || value > maximum','value < 0 || (false && value > maximum)'),
          ('V2 uses wrong sink index','max_lum_idx = cap.dv_max_v2_i;','max_lum_idx = 0;'),
        ]:
            assert old in code,label
            run(code.replace(old,new),negative=True);print('REJECTED:',label)

if __name__=='__main__':main()
