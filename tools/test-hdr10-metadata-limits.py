#!/usr/bin/env python3
"""Execute production outgoing HDR10 policy/packet writes and Kodi sysfs policy.

Use --kernel-root for the paired external kernel checkout. Recording sysfs and
register substitutes establish source/cap/reset semantics, not device HDMI output.
"""
import argparse
import ast
from pathlib import Path
import re
import runpy
import subprocess
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def run(code, language, negative=False):
    with tempfile.TemporaryDirectory(prefix='hdr10-limits-') as directory:
        path = Path(directory)
        source = path / ('test.c' if language == 'c' else 'test.cpp')
        source.write_text(code)
        command = ['gcc' if language == 'c' else 'g++',
                   '-std=gnu99' if language == 'c' else '-std=c++17',
                   '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined',
                   '-fno-omit-frame-pointer', '-fno-pie', '-no-pie', str(source),
                   '-o', str(path / 'test')]
        subprocess.run(command, check=True)
        result = subprocess.run([str(path / 'test')], text=True, capture_output=True, timeout=20)
        if negative:
            assert result.returncode and 'Assertion' in result.stderr, result.stdout + result.stderr
        else:
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout.strip())


KERNEL_PREFIX = r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
struct kernel_param {void *arg;};
struct kernel_param_ops {int (*set)(const char *,const struct kernel_param *);int (*get)(char *,const struct kernel_param *);};
int param_get_uint(char *buf,const struct kernel_param *kp){return sprintf(buf,"%u",*(unsigned int *)kp->arg);}
int kstrtouint(const char *s,unsigned int base,unsigned int *value){
 char *end;unsigned long v;if(*s=='-')return -EINVAL;errno=0;v=strtoul(s,&end,base);
 if(errno||v>0xffffffffUL||end==s||(*end && strcmp(end,"\n")))return -EINVAL;
 *value=v;return 0;
}
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define module_param_cb(name,ops,arg,mode) static const struct kernel_param name##_kp={arg};
#define MODULE_PARM_DESC(name,desc)
@DEFINES@
#include "@REG_HEADER@"
struct write {unsigned int addr,value,offset,len;};
struct write writes[256];unsigned int count;
void hdmitx_wr_reg(unsigned int addr,unsigned int value){assert(count<256);writes[count++]=(struct write){addr,value,0,0};}
void hdmitx_set_reg_bits(unsigned int addr,unsigned int value,unsigned int offset,unsigned int len){assert(count<256);writes[count++]=(struct write){addr,value,offset,len};}
@POLICY@
@PACKET@
'''
KERNEL_TESTS = r'''
unsigned int nits(const unsigned char *db,int offset){return db[offset]|(db[offset+1]<<8);}
void put(unsigned char *db,int offset,unsigned int value){db[offset]=value&255;db[offset+1]=value>>8;}
void caps(unsigned int lum,unsigned int cll){char text[20];sprintf(text,"%u",lum);assert(!hdr10_limit_ops.set(text,&xbmc_hdr10_max_lum_override_kp));sprintf(text,"%u",cll);assert(!hdr10_limit_ops.set(text,&xbmc_hdr10_max_cll_override_kp));}
void packet(unsigned char *db,unsigned char *hb,unsigned int lum,unsigned int cll){
 unsigned char original[26],header[3],expected[26];unsigned int i,v;
 memcpy(original,db,26);memcpy(header,hb,3);memcpy(expected,db,26);
 if(hb[0]==0x87&&hb[1]==1&&hb[2]==26&&db[0]==2&&db[1]==0){
  v=nits(db,18);if(lum&&v>lum)put(expected,18,lum);
  v=nits(db,22);if(cll&&v>cll)put(expected,22,cll);
 }
 caps(lum,cll);count=0;hdmitx_set_packet(HDMI_PACKET_DRM,db,hb);
 assert(count==30&&!memcmp(original,db,26)&&!memcmp(header,hb,3));
 assert(writes[0].addr==HDMITX_DWC_FC_DRM_HB01&&writes[0].value==hb[1]);
 assert(writes[1].addr==HDMITX_DWC_FC_DRM_HB02&&writes[1].value==hb[2]);
 for(i=0;i<26;i++)assert(writes[i+2].addr==HDMITX_DWC_FC_DRM_PB00+i&&writes[i+2].value==expected[i]);
 assert(writes[28].addr==HDMITX_DWC_FC_DATAUTO3&&writes[28].value==1&&writes[28].offset==6&&writes[28].len==1);
 assert(writes[29].addr==HDMITX_DWC_FC_PACKET_TX_EN&&writes[29].value==1&&writes[29].offset==7&&writes[29].len==1);
}
void bypass_vendor(unsigned int oui,unsigned int length){
 unsigned char db[27],hb[3]={0x81,1,0};struct write expected[256];unsigned int i,size;
 for(i=0;i<27;i++){db[i]=i+1;}db[0]=oui&255;db[1]=(oui>>8)&255;db[2]=(oui>>16)&255;hb[2]=length;
 caps(0,0);count=0;hdmitx_set_packet(HDMI_PACKET_VEND,db,hb);size=count;memcpy(expected,writes,sizeof(writes));
 caps(100,100);count=0;hdmitx_set_packet(HDMI_PACKET_VEND,db,hb);assert(count==size&&!memcmp(expected,writes,size*sizeof(struct write)));
}
int main(void){
 unsigned char db[26],hb[3]={0x87,1,26};unsigned int i,a,b,c,d,cases=0;
 unsigned int values[]={0,1,99,100,101,1000,4000,10000,65535},limits[]={0,100,1000,4000,10000};
 assert(!xbmc_hdr10_max_lum_override&&!xbmc_hdr10_max_cll_override);
 for(i=0;i<26;i++){db[i]=i*7;}db[0]=2;db[1]=0;
 for(a=0;a<9;a++)for(b=0;b<9;b++)for(c=0;c<5;c++)for(d=0;d<5;d++){
  put(db,18,values[a]);put(db,22,values[b]);packet(db,hb,limits[c],limits[d]);cases++;
 }
 /* Same original source can be capped, raised, disabled and replaced. */
 put(db,18,4000);put(db,22,6000);packet(db,hb,100,200);packet(db,hb,1000,2000);packet(db,hb,0,0);
 put(db,18,300);put(db,22,0);packet(db,hb,1000,2000);
 for(i=0;i<256;i++){db[0]=i;packet(db,hb,100,100);}db[0]=2;
 db[1]=1;packet(db,hb,100,100);db[1]=0;
 for(i=0;i<3;i++){unsigned char old=hb[i];hb[i]=0;packet(db,hb,100,100);hb[i]=old;}
 /* Both NULL recovery variants still disable DRM without payload writes. */
 for(i=0;i<3;i++){
  count=0;hdmitx_set_packet(HDMI_PACKET_DRM,i==0?db:NULL,i==1?hb:NULL);
  assert(count==2&&writes[0].addr==HDMITX_DWC_FC_DATAUTO3&&writes[0].value==0&&writes[0].offset==6&&writes[0].len==1);
  assert(writes[1].addr==HDMITX_DWC_FC_PACKET_TX_EN&&writes[1].value==0&&writes[1].offset==7&&writes[1].len==1);
 }
 bypass_vendor(DOVI_IEEEOUI,0x1b);bypass_vendor(HDR10PLUS_IEEEOUI,27);bypass_vendor(HDMI_IEEEOUI,5);
 /* Parameter changes alone must neither replay nor enable a packet. */
 count=0;caps(500,600);assert(count==0);
 assert(hdr10_limit_ops.set("10001",&xbmc_hdr10_max_lum_override_kp)==-EINVAL&&xbmc_hdr10_max_lum_override==500);
 assert(hdr10_limit_ops.set("-1",&xbmc_hdr10_max_cll_override_kp)==-EINVAL&&xbmc_hdr10_max_cll_override==600);
 assert(hdr10_limit_ops.set("bad",&xbmc_hdr10_max_lum_override_kp)==-EINVAL);
 {char text[20];assert(hdr10_limit_ops.get(text,&xbmc_hdr10_max_cll_override_kp)>0&&!strcmp(text,"600"));}
 printf("PASS: %u PQ payload combinations, source reuse/replacement, zeros, route bypass, NULL recovery and bounded parameters (ASan/UBSan)\n",cases);
 return 0;
}
'''
KODI_PREFIX = r'''
#include <algorithm>
#include <cassert>
#include <iostream>
#include <map>
#include <string>
#include <vector>
struct CSettings {@IDS@};
struct Settings {std::map<std::string,int> values;int GetInt(const std::string& s){return values.at(s);}bool GetBool(const std::string& s){return GetInt(s)!=0;}} storage;
Settings* settings(){return &storage;}
std::map<std::string,bool> nodes;
std::vector<std::pair<std::string,int>> writes;
struct CSysfsPath {
 std::string path;CSysfsPath(const std::string& s):path(s){}
 bool Exists(){return nodes[path];}
 template<class T>CSysfsPath(const std::string& s,T value):path(s){if(Exists())Set(value);}
 void Set(int v){assert(Exists());writes.emplace_back(path,v);}
};
struct HDRStaticMetadataInfo {int max_lum=4000,min_lum=50,max_cll=6000,max_fall=700;};
struct Cache {HDRStaticMetadataInfo hdr;HDRStaticMetadataInfo GetVideoHDRStaticMetadataInfo(){return hdr;}} cache;
struct CServiceBroker {static Cache& GetDataCacheCore(){return cache;}};
@METHODS@
int main(){
 const std::string lum="/sys/module/hdmitx20/parameters/xbmc_hdr10_max_lum_override",cll="/sys/module/hdmitx20/parameters/xbmc_hdr10_max_cll_override";
 const char *master=CSettings::SETTING_COREELEC_AMLOGIC_HDR10_LIMITER,*l=CSettings::SETTING_COREELEC_AMLOGIC_HDR10_MAX_LUMINANCE,*c=CSettings::SETTING_COREELEC_AMLOGIC_HDR10_MAX_CLL;
 for(bool a:{false,true})for(bool b:{false,true}){
  nodes[lum]=a;nodes[cll]=b;storage.values={{master,1},{l,1000},{c,2000}};writes.clear();
  assert(aml_hdr10_metadata_limits_supported()==(a&&b));aml_apply_hdr10_metadata_limits();assert(writes.size()==((a&&b)?2:0));
 }
 nodes[lum]=nodes[cll]=true;
 for(int enabled:{0,1})for(int lv:{-1,0,100,1000,10000,10001})for(int cv:{-1,0,100,1000,10000,10001}){
  storage.values={{master,enabled},{l,lv},{c,cv}};auto original=storage.values;writes.clear();aml_apply_hdr10_metadata_limits();
  const std::vector<std::pair<std::string,int>> expected={{lum,enabled?std::clamp(lv,0,10000):0},{cll,enabled?std::clamp(cv,0,10000):0}};
  assert(writes==expected&&storage.values==original);
 }
 // Conversion still receives the authored values after outgoing caps change.
 const std::string prefix="/sys/module/amdolby_vision/parameters/xbmc_dv_hdr10_";
 for(const auto* suffix:{"max_lum","min_lum","max_cll","max_fall"})nodes[prefix+suffix]=true;
 storage.values={{master,1},{l,100},{c,200}};aml_apply_hdr10_metadata_limits();writes.clear();aml_dv_send_hdr10_data();
 const std::vector<std::pair<std::string,int>> sourceExpected={{prefix+"max_lum",4000},{prefix+"min_lum",50},{prefix+"max_cll",6000},{prefix+"max_fall",700}};
 assert(writes==sourceExpected&&cache.hdr.max_lum==4000&&cache.hdr.max_cll==6000);
 std::cout<<"PASS: 72 Kodi cap/master combinations, missing/partial driver API, saved values and original DV source metadata (ASan/UBSan)\n";
}
'''


def kernel_code(root):
    folder = root / 'drivers/amlogic/media/vout/hdmitx/hdmi_tx_20'
    policy = re.sub(r'^#include[^\n]*\n', '', (folder / 'hdmi_tx_hdr10_limits.c').read_text(), flags=re.M)
    packet = function((folder / 'hw/hdmi_tx_hw.c').read_text(),
                      'static void hdmitx_set_packet(int type, unsigned char *DB, unsigned char *HB)\n{')
    headers = root / 'include/linux/amlogic/media/vout/hdmi_tx'
    defines = []
    for filename in ['hdmi_tx_module.h', 'hdmi_common.h']:
        defines.extend(re.findall(r'^#define (?:HDMI_PACKET_\w+|HDMI_AUDIO_INFO|HDMI_SOURCE_DESCRIPTION|\w+_IEEEOUI)\s+[^\n]+', (headers / filename).read_text(), re.M))
    defines.append('#define T3D_FRAME_PACKING 0')
    makefile = (folder / 'Makefile').read_text()
    assert 'hdmi_tx_calibration.o hdmi_tx_hdr10_limits.o' in makefile
    return (KERNEL_PREFIX.replace('@DEFINES@', '\n'.join(defines))
            .replace('@REG_HEADER@', str(folder / 'hw/hdmi_tx_reg.h'))
            .replace('@POLICY@', policy).replace('@PACKET@', packet) + KERNEL_TESTS)


def kodi_code():
    source = (ROOT / 'xbmc/utils/AMLUtils.cpp').read_text()
    methods = '\n'.join(function(source, sig) for sig in (
        'bool aml_hdr10_metadata_limits_supported()', 'void aml_apply_hdr10_metadata_limits()',
        'void aml_dv_send_hdr10_data()'))
    ids = sorted(set(re.findall(r'CSettings::(SETTING_\w+)', methods)))
    return KODI_PREFIX.replace('@IDS@', '\n'.join(
        f'static constexpr const char* {name}="{name}";' for name in ids)).replace('@METHODS@', methods)


def po_entries(path):
    entries, fields, key = {}, {}, None
    for line in path.read_text().splitlines() + ['']:
        if not line:
            if fields.get('msgctxt', '').startswith('#'):
                ident = fields['msgctxt']
                assert ident not in entries, (path, ident)
                entries[ident] = fields
            fields, key = {}, None
        elif line.startswith(('msgctxt ', 'msgid ', 'msgstr ')):
            key, value = line.split(' ', 1);fields[key] = ast.literal_eval(value)
        elif line.startswith('"') and key:
            fields[key] += ast.literal_eval(line)
    return entries


def settings_check():
    tree = ET.parse(ROOT / 'system/settings/settings.xml')
    all_ids = [s.get('id') for s in tree.findall('.//setting')]
    assert len(all_ids) == len(set(all_ids))
    en = po_entries(ROOT / 'addons/resource.language.en_gb/resources/strings.po')
    de = po_entries(ROOT / 'addons/resource.language.de_de/resources/strings.po')
    for suffix, default in [('limiter','false'),('max.luminance','0'),('max.cll','0')]:
        ident = 'coreelec.amlogic.hdr10.' + suffix
        node = tree.find(f'.//category[@id="coreelec"]/group[@id="1"]/setting[@id="{ident}"]')
        assert node is not None and node.findtext('requirement') == 'HAVE_AMCODEC'
        assert node.findtext('level') == '2' and node.findtext('default') == default
        assert node.findtext('visible') == 'false'
        if suffix != 'limiter':
            assert node.get('parent') == 'coreelec.amlogic.hdr10.limiter'
            assert [node.findtext('constraints/'+tag) for tag in ['minimum','step','maximum']] == ['0','100','10000']
            dep = node.find('dependencies/dependency')
            assert dep.get('type') == 'visible' and dep.get('setting') == node.get('parent') and dep.text == 'true'
        for field in ['label','help']:
            entry = '#'+node.get(field)
            assert en[entry]['msgid'] == de[entry]['msgid'] and de[entry]['msgstr']
    print('PASS: XML unique settings, native non-DV visibility, defaults/ranges/dependencies and exact EN/DE IDs')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--kernel-root', required=True, type=Path)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    kernel, kodi = kernel_code(args.kernel_root), kodi_code()
    run(kernel, 'c');run(kodi, 'c++');settings_check()
    if args.negative_controls:
        mutations = [
            ('ignore PQ gate', 'db[0] != 2 || db[1] != 0', 'db[1] != 0'),
            ('synthesize unknown zero', 'limit && value > limit', 'limit && (value > limit || !value)'),
            ('raise instead of cap', 'limit && value > limit', 'limit && value < limit'),
            ('cap minimum luminance', 'dst + 18', 'dst + 20'),
            ('cap MaxFALL', 'dst + 22', 'dst + 24'),
            ('mutate source metadata', 'memcpy(dst, db, 26);', 'memcpy(dst, db, 26); ((unsigned char *)db)[18] = 0;'),
            ('ignore version', 'hb[1] != 1 || ', ''),
            ('ignore payload length', 'hb[2] != 26 ||', ''),
            ('ignore descriptor', ' || db[1] != 0', ''),
            ('ignore cap off', 'limit && value > limit', 'value > limit'),
            ('write original payload', 'HDMITX_DWC_FC_DRM_PB00 + i, hdr10_db[i]', 'HDMITX_DWC_FC_DRM_PB00 + i, DB[i]'),
            ('break NULL disable', 'HDMITX_DWC_FC_DATAUTO3, 0, 6, 1', 'HDMITX_DWC_FC_DATAUTO3, 1, 6, 1'),
            ('accept excessive cap', 'limit > 10000', 'limit > 65535'),
        ]
        for label, old, new in mutations:
            assert old in kernel, label
            run(kernel.replace(old, new), 'c', negative=True);print('REJECTED:', label)
        for label, old, new in [
            ('ignore disabled master', 'const bool enabled = settings()->GetBool(CSettings::SETTING_COREELEC_AMLOGIC_HDR10_LIMITER);', 'const bool enabled = true;'),
            ('partial API writes', 'if (!aml_hdr10_metadata_limits_supported())', 'if (false)'),
            ('disable child cap clamps', '0, 10000', '0, 65535'),
            ('clamp DV source mastering maximum', 'hdrStaticMetadataInfo.max_lum);', 'std::min(hdrStaticMetadataInfo.max_lum, 100));'),
            ('clamp DV source MaxCLL', 'hdrStaticMetadataInfo.max_cll);', 'std::min(hdrStaticMetadataInfo.max_cll, 200));'),
        ]:
            assert old in kodi, label
            run(kodi.replace(old, new), 'c++', negative=True);print('REJECTED:', label)


if __name__ == '__main__':
    main()
