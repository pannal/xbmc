#!/usr/bin/env python3
"""Exercise production Kodi L5 ownership and the supplied CE driver's ETSI substitution.

The two production function bodies execute together. Sysfs is an in-memory
parameter boundary; driver register packing is a byte-buffer boundary. This is
host verification of metadata precedence, not device/HDMI or RPU decoder proof.
"""
import argparse, hashlib, json, pathlib, runpy, subprocess, tempfile
ROOT=pathlib.Path(__file__).resolve().parents[1]
F=runpy.run_path(str(ROOT/'tools/test-render-slot-publication.py'))['function']
PREFIX=r'''
#include <algorithm>
#include <array>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <memory>
#include <mutex>
#include <string>
#include <vector>
using u16=uint16_t;using u32=uint32_t;
constexpr int LOGDEBUG=0,ETSI_META_OFFSET=71,CORE_META_LENGTH=2048,LEVEL_5_LENGTH=13;
bool xbmc_detect_active_area=true,xbmc_force_l5_override=false,xbmc_l5_override_additive=false;
bool xbmc_meta_level_5=true,xbmc_meta_level_5_osdst=false,xbmc_meta_level_5_subt=false;
bool dolby_vision_xbmc_osd=false,dolby_vision_subtitles=false,dump_enable=false;int debug_dolby=0;
u16 xbmc_detected_l5_top=42,xbmc_detected_l5_bottom=42,xbmc_detected_l5_left=0,xbmc_detected_l5_right=0;
u16 xbmc_override_l5_top=0,xbmc_override_l5_bottom=0,xbmc_override_l5_left=0,xbmc_override_l5_right=0;
unsigned char reversed_meta_buffer[CORE_META_LENGTH]{},combo_meta_buffer[CORE_META_LENGTH]{};
struct md_reg_ipcore3{std::vector<unsigned char> data;};
size_t reverse_dv_meta(unsigned char* out,md_reg_ipcore3*){memset(out,0,ETSI_META_OFFSET);return ETSI_META_OFFSET;}
void prepare_dv_meta(md_reg_ipcore3* out,unsigned char* data,size_t size){out->data.assign(data,data+size);}
u32 get_unaligned_be32(const unsigned char* p){return (u32(p[0])<<24)|(u32(p[1])<<16)|(u32(p[2])<<8)|p[3];}
void dump_buffer(const char*,unsigned char*,size_t){}
#define pr_err(...) ((void)0)
struct CLog{template<class... T>static void Log(T...) {}};
struct DetectResult{u16 top,bottom,left,right;bool highConfidence=false;};
struct DetectSource{bool nativeDV=true,allowProbe=true;std::shared_ptr<std::atomic<bool>> superseded=std::make_shared<std::atomic<bool>>(false);std::shared_ptr<DetectResult> result;};
auto s_detectSource=std::make_shared<DetectSource>();std::mutex s_detectSourceMutex;
bool nativeEnabled=true,manual=false,autoLetterbox=false;std::array<u16,4> manualOffsets{},autoOffsets{68,68,0,0};std::atomic<bool> s_autoLbAdditive{true};
bool _l5_override_parse(u16& t,u16& b,u16& l,u16& r){if(!manual)return false;t=manualOffsets[0];b=manualOffsets[1];l=manualOffsets[2];r=manualOffsets[3];return true;}
bool _auto_letterbox_geometry(u16& t,u16& b,u16& l,u16& r){if(!autoLetterbox)return false;t=autoOffsets[0];b=autoOffsets[1];l=autoOffsets[2];r=autoOffsets[3];return true;}
bool aml_dv_detect_active_area_enabled(){return nativeEnabled&&!manual;}
struct CSysfsPath{CSysfsPath(const char* path,int value){std::string name=path;name=name.substr(name.find_last_of('/')+1);
 if(name=="xbmc_force_l5_override")xbmc_force_l5_override=value;
 else if(name=="xbmc_l5_override_additive")xbmc_l5_override_additive=value;
 else if(name=="xbmc_override_l5_top")xbmc_override_l5_top=value;
 else if(name=="xbmc_override_l5_bottom")xbmc_override_l5_bottom=value;
 else if(name=="xbmc_override_l5_left")xbmc_override_l5_left=value;
 else if(name=="xbmc_override_l5_right")xbmc_override_l5_right=value;
 else assert(false);
}};
'''
TEST=r'''
std::array<u16,4> output(std::array<u16,4> authored,bool hasL5=true){
 std::vector<unsigned char> input(ETSI_META_OFFSET,0);input.insert(input.end(),{0,0,0,1,1,0});
 if(hasL5){unsigned char block[13];const bool osd=dolby_vision_xbmc_osd,subs=dolby_vision_subtitles;dolby_vision_xbmc_osd=dolby_vision_subtitles=false;
 build_level_5_data_from(block,authored[0],authored[1],authored[2],authored[3]);dolby_vision_xbmc_osd=osd;dolby_vision_subtitles=subs;input.insert(input.end(),block,block+13);}
 md_reg_ipcore3 core;source_meta_copy(input.data(),input.size(),&core);assert(core.data.size()>=ETSI_META_OFFSET);
 for(size_t p=ETSI_META_OFFSET;p+5<=core.data.size();){auto size=get_unaligned_be32(core.data.data()+p)+5;assert(p+size<=core.data.size());if(core.data[p+4]==5){assert(size==13);auto* z=core.data.data()+p;return {u16((z[9]<<8)|z[10]),u16((z[11]<<8)|z[12]),u16((z[5]<<8)|z[6]),u16((z[7]<<8)|z[8])};}p+=size;}
 assert(false);return {};
}
int main(){
 const std::array<u16,4> authored{7,7,1,2},zero{};
 assert(output(authored)==authored);assert(output(zero)==(std::array<u16,4>{42,42,0,0})); // legacy fills zeros only
 s_detectSource->result=std::make_shared<DetectResult>(DetectResult{132,132,0,0,false});aml_dv_apply_l5_override_sysfs();
 assert(!xbmc_force_l5_override&&output(authored)==authored);
 s_detectSource->result->highConfidence=true;aml_dv_apply_l5_override_sysfs();
 assert(xbmc_force_l5_override&&!xbmc_l5_override_additive);assert(output(authored)==(std::array<u16,4>{132,132,0,0}));
 assert(output(authored,false)==(std::array<u16,4>{132,132,0,0})); // missing L5 append uses same owner
 manual=true;manualOffsets=zero;aml_dv_apply_l5_override_sysfs();assert(xbmc_force_l5_override&&output(authored)==zero);
 manualOffsets={80,80,3,4};aml_dv_apply_l5_override_sysfs();assert(output(authored)==manualOffsets);
 manual=false;s_detectSource->result=std::make_shared<DetectResult>(DetectResult{0,0,0,0,true});aml_dv_apply_l5_override_sysfs();assert(xbmc_force_l5_override&&output(authored)==zero);
 s_detectSource->superseded->store(true);aml_dv_apply_l5_override_sysfs();assert(!xbmc_force_l5_override&&output(authored)==authored);
 s_detectSource->superseded->store(false);s_detectSource->result=std::make_shared<DetectResult>(DetectResult{132,132,0,0,true});
 nativeEnabled=false;aml_dv_apply_l5_override_sysfs();assert(!xbmc_force_l5_override&&output(authored)==authored);nativeEnabled=true;
 s_detectSource->nativeDV=false;aml_dv_apply_l5_override_sysfs();assert(!xbmc_force_l5_override);s_detectSource->nativeDV=true;
 s_detectSource->allowProbe=false;aml_dv_apply_l5_override_sysfs();assert(!xbmc_force_l5_override);s_detectSource->allowProbe=true;
 autoLetterbox=true;aml_dv_apply_l5_override_sysfs();assert(xbmc_l5_override_additive&&output(authored)==(std::array<u16,4>{75,75,1,2}));
 s_autoLbAdditive=false;aml_dv_apply_l5_override_sysfs();assert(output(authored)==autoOffsets);autoLetterbox=false;
 aml_dv_apply_l5_override_sysfs();xbmc_meta_level_5_osdst=true;dolby_vision_xbmc_osd=true;assert(output(authored)==zero);
 dolby_vision_xbmc_osd=false;xbmc_meta_level_5_subt=true;dolby_vision_subtitles=true;assert(output(authored)==zero);
 dolby_vision_subtitles=false;assert(output(authored)==(std::array<u16,4>{132,132,0,0}));
 s_detectSource->result.reset();aml_dv_apply_l5_override_sysfs();assert(!xbmc_force_l5_override&&output(authored)==authored);
 std::cout<<"PASS: actual Kodi owner + CE driver nonzero/zero/absent L5 replacement, manual precedence, confidence rejection, stale clearing, cropped composition and OSD/subtitle suppression\n";
}
'''
def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--kernel-source',type=pathlib.Path,required=True);p.add_argument('--output',type=pathlib.Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
 aml=(ROOT/'xbmc/utils/AMLUtils.cpp').read_text();kernel=a.kernel_source.read_text();code=PREFIX
 for signature in ['static void detect_apply_l5_override_locked()','void aml_dv_apply_l5_override_sysfs()']:code+='\n'+F(aml,signature)
 for signature in ['static inline void build_level_5_data_from(','static inline void build_level_5_data(','static inline void build_level_5_override_data(','static inline u16 l5_saturating_add(','static inline void build_level_5_additive_data(','static inline void build_level_5_data_select(','static inline bool is_level_5_all_zero(','static inline void source_meta_copy(']:code+='\n'+F(kernel,signature)
 cpp=a.output/'repro.cpp';cpp.write_text(code+'\n'+TEST);exe=a.output/'repro';argv=['g++','-std=c++17','-O1','-Wall','-Wextra','-Werror','-Wno-unused-variable','-Wno-unused-but-set-variable','-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie',str(cpp),'-o',str(exe)]
 with (a.output/'compile.log').open('w') as log:subprocess.run(argv,stdout=log,stderr=subprocess.STDOUT,check=True)
 import os
 with (a.output/'result.log').open('w') as log:subprocess.run([str(exe)],stdout=log,stderr=subprocess.STDOUT,check=True,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'})
 (a.output/'verification.json').write_text(json.dumps(dict(result='PASS',compiler_argv=argv,kernel_source=str(a.kernel_source),source_sha256={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in [ROOT/'xbmc/utils/AMLUtils.cpp',a.kernel_source,cpp]},driver_register_packing_stubbed=True,device_verified=False),indent=2));print((a.output/'result.log').read_text().strip())
if __name__=='__main__':main()
