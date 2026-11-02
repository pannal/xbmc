#!/usr/bin/env python3
"""Verify actual TrueHD parser preservation for declared Evolution streams.

Synthetic frames exercise headers/buffering, not audio decoding or HMAC validity.
Optional original raw TrueHD files must remain byte-identical with defeat On/Off.
No authentication key is needed or used: the invariant is preserving source bytes.
"""
import argparse
import ctypes.util
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
UTILS = ROOT / 'xbmc/cores/AudioEngine/Utils'
HARNESS = r'''
#include <algorithm>
#include <cassert>
#include <cstring>
#include <fstream>
#include <iostream>
#include <iterator>
#include <vector>
#include "AEStreamInfo.h"
using Bytes = std::vector<uint8_t>;
unsigned checks=0;
bool fullOnly=false;
void check(bool value, const char* why) {
  ++checks;
  if (!value) { std::cerr << "CHECK FAILED: " << why << "\n"; std::abort(); }
}
unsigned majorSize(const Bytes& f) {
  return 28 + ((f[29]&1) ? 2+(f[30]>>4)*2 : 0);
}
void writeCrc(Bytes& f) {
  AVCRC table[1024]; av_crc_init(table,0,16,0x2d,sizeof(table));
  unsigned n=majorSize(f);
  uint16_t crc=av_crc(table,0,f.data()+4,n-4);
  crc ^= (f[n+1]<<8)|f[n]; f[n+2]=crc; f[n+3]=crc>>8;
}
void validCrc(const Bytes& f) {
  auto copy=f; writeCrc(copy);
  auto n=majorSize(f); check(copy[n+2]==f[n+2]&&copy[n+3]==f[n+3],"major CRC");
}
Bytes major(bool evo,unsigned dn,unsigned substreams=1) {
  Bytes f(96,0x55); std::fill(f.begin(),f.begin()+36,0);
  f[1]=f.size()/2; f[4]=0xf8;f[5]=0x72;f[6]=0x6f;f[7]=0xba;
  f[11]=1;f[12]=0xb7;f[13]=0x52;f[14]=evo?0x10:0;
  f[20]=substreams<<4;f[21]=0x80;f[29]=1;f[30]=0x10|(dn>>1);f[31]=(dn&1)<<7;
  f[31]|=35<<1;f[32]=13<<4;writeCrc(f);return f;
}
Bytes minor(unsigned substreams=1,bool extra=false) {
  Bytes f(64,0);f[1]=f.size()/2;unsigned at=4;
  for(unsigned n=0;n<substreams;n++) {
    f[at++]=extra?0x80:0;f[at++]=n+1;
    if(extra) {f[at++]=0x32+n;f[at++]=0x75-n;}
  }
  unsigned parity=0;for(unsigned n=0;n<at;n++)parity^=f[n];
  f[0]=((15^((parity>>4)^(parity&15)))&15)<<4;
  return f;
}
Bytes emit(CAEStreamParser& p,const Bytes& source,unsigned fragment,bool requested) {
  uint8_t* out=nullptr;unsigned capacity=0;Bytes result;size_t at=0;
  while(at<source.size()) {
    p.SetDefeatTrueHDDialNorm(requested); // The codec reapplies this every packet.
    unsigned size=std::min<size_t>(fragment,source.size()-at);
    unsigned outputSize=capacity;
    int consumed=p.AddData(const_cast<uint8_t*>(source.data()+at),size,&out,&outputSize);
    check(consumed==int(size),"fragment consumption");at+=consumed;
    if(outputSize) {
      check(at==source.size()&&result.empty(),"one complete output only");
      result.assign(out,out+outputSize);capacity=outputSize;
    }
  }
  delete[]out;
  if(result.size()!=source.size()) std::cerr << "fragment=" << fragment << " requested=" << requested << " source=" << source.size() << " out=" << result.size() << " buffered=" << p.GetBufferSize() << " sync=" << p.IsValid() << " frame=" << p.GetFrameSize() << "\n";
  check(result.size()==source.size(),"complete frame");
  check(p.GetBufferSize()==0&&p.GetSyncLostCount()==0,"no lost bytes/sync");
  return result;
}
void synthetic() {
  for(bool requested:{false,true})for(bool evo:{false,true})
  for(unsigned fragment:{96u,1u,2u,7u,31u,32u,35u,36u,37u})
  for(unsigned dn:{1u,27u,31u})for(unsigned substreams:{1u,4u})for(bool extra:{false,true}) {
    if(fullOnly && fragment!=96) continue;
    CAEStreamParser p;p.SetSyncFamily(CAEStreamParser::SyncFamily::TrueHD);
    for(int repeat=0;repeat<3;repeat++) {
      auto input=major(evo,dn,substreams);auto result=emit(p,input,fragment,requested);validCrc(result);
      const bool changed=requested&&!evo&&dn!=31;
      check((result!=input)==changed,"Evolution preservation / legacy rewrite");
      if(changed) {
        auto expected=input;expected[30]|=15;expected[31]|=128;writeCrc(expected);
        check(result==expected,"only intended dialnorm and major CRC bytes");
      }
      auto info=p.GetStreamInfo();check(info.m_hasDialNorm,"metadata present");
      check(info.m_dialNormOriginal==int(dn)-31,"original normalization");
      check(info.m_dialNormApplied==(requested&&!evo?0:int(dn)-31),"effective normalization");
      auto following=minor(substreams,extra);
      check(emit(p,following,fragment,requested)==following,"minor frame unchanged");
      check(p.GetStreamInfo().m_dialNormApplied==info.m_dialNormApplied,"minor metadata retained");
    }
    p.Reset();check(!p.GetStreamInfo().m_hasDialNorm,"reset forgets presentation");
    auto next=major(!evo,27,substreams);auto result=emit(p,next,fragment,requested);
    check((result!=next)==(requested&&evo),"reset/new declaration does not alter requested option");
  }
}
void media(const char* path) {
  std::ifstream stream(path,std::ios::binary);
  Bytes raw((std::istreambuf_iterator<char>(stream)),{});check(raw.size()>=8,"media input");
  for(bool requested:{false,true}) {
    CAEStreamParser p;p.SetSyncFamily(CAEStreamParser::SyncFamily::TrueHD);
    size_t at=0,count=0,majors=0;
    while(at<raw.size()) {
      check(at+8<=raw.size(),"AU header bounds");
      unsigned size=(((raw[at]&15)<<8)|raw[at+1])*2;
      check(size>=8&&at+size<=raw.size(),"AU bounds");
      Bytes input(raw.begin()+at,raw.begin()+at+size);
      auto result=emit(p,input,size,requested);
      check(result==input,"real Evolution stream must be original from AU zero");
      if(!std::memcmp(input.data()+4,"\xf8\x72\x6f\xba",4)) {
        check(input[14]&0x10,"declared Evolution media required");validCrc(result);++majors;
      }
      auto info=p.GetStreamInfo();check(info.m_dialNormApplied==info.m_dialNormOriginal,"real applied normalization");
      at+=size;++count;
    }
    std::cout<<path<<" requested="<<requested<<" AUs="<<count<<" majors="<<majors<<" bytes="<<at<<" unchanged\n";
  }
}
int main(int argc,char**argv) {
  int first=1;
  if(argc>1&&!std::strcmp(argv[1],"--full-only")){fullOnly=true;first=2;}
  synthetic();for(int i=first;i<argc;i++)media(argv[i]);
  std::cout<<checks<<" checks passed\n";
}
'''


def build(root, work, source, library, sanitize):
    work.mkdir()
    (work/'AEStreamInfo.cpp').write_text(source)
    shutil.copyfile(UTILS/'AEStreamInfo.h',work/'AEStreamInfo.h')
    (work/'main.cpp').write_text(HARNESS)
    command=['g++','-std=c++17','-O1','-g','-I'+str(work),'-I'+str(root),
             '-I'+str(UTILS),str(work/'AEStreamInfo.cpp'),str(work/'main.cpp'),
             '-Wl,-l:'+library,'-o',str(work/'test')]
    if sanitize:
        command[1:1]=['-fsanitize=address,undefined','-fno-omit-frame-pointer']
    subprocess.run(command,check=True,capture_output=True,text=True)
    return work/'test'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--media',type=Path,action='append',default=[])
    parser.add_argument('--baseline-ref',help='original source must fail the new preservation assertion')
    parser.add_argument('--sanitize',action='store_true')
    args=parser.parse_args()
    include=Path(os.environ.get('FFMPEG_INCLUDE','/usr/include/x86_64-linux-gnu'))
    library=os.environ.get('AVUTIL_LIBRARY') or ctypes.util.find_library('avutil')
    if not library:
        raise SystemExit('Host libavutil required')
    source=(UTILS/'AEStreamInfo.cpp').read_text()
    with tempfile.TemporaryDirectory(prefix='truehd-evolution-') as temporary:
        root=Path(temporary);(root/'utils').mkdir();(root/'libavutil').mkdir()
        (root/'utils/log.h').write_text('''#pragma once
constexpr int LOGINFO=1,LOGDEBUG=0,LOGERROR=2;
struct CLog {template<class...T> static void Log(int,const char*,const T&...) {}};
''')
        for name in ('crc.h','attributes.h'):
            shutil.copyfile(include/'libavutil'/name,root/'libavutil'/name)
        binary=build(root,root/'current',source,library,args.sanitize)
        for path in args.media:
            print(path,'sha256',hashlib.file_digest(path.open('rb'),'sha256').hexdigest(),flush=True)
        subprocess.run([str(binary),*[str(p.resolve()) for p in args.media]],check=True)
        variants={
            'removed-guard':source.replace('&& !hasEvolutionData &&','&&',1),
            'inverted-guard':source.replace('&& !hasEvolutionData &&','&& hasEvolutionData &&',1),
            'wrong-flag':source.replace('(data[14] & 0x10) != 0','(data[14] & 0x01) != 0',1),
            'late-only':source.replace('(data[14] & 0x10) != 0','((data[14] & 0x10) != 0) && m_hasSync',1),
            'discard-short-header':source.replace('m_needBytes = 32;', 'm_needBytes = 0;',1),
            'discard-extension':source.replace('m_needBytes = 4 + major_sync_size;', 'm_needBytes = 0;',1),
            'stale-minor-words':source.replace('if (left < static_cast<unsigned int>(p + 2))', 'if (false)',2),
        }
        if args.baseline_ref:
            variants['original-source']=subprocess.check_output(['git','show',
                args.baseline_ref+':xbmc/cores/AudioEngine/Utils/AEStreamInfo.cpp'],cwd=ROOT,text=True)
        for name,mutant in variants.items():
            assert mutant!=source,name
            binary=build(root,root/name,mutant,library,args.sanitize)
            result=subprocess.run([str(binary)]+(['--full-only'] if name=='original-source' else []),capture_output=True,text=True)
            assert result.returncode and 'CHECK FAILED:' in result.stderr,(name,result.stdout,result.stderr)
            print('Rejected',name,result.stderr.strip().splitlines()[-1],flush=True)


if __name__=='__main__':
    main()
