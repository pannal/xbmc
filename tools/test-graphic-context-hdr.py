#!/usr/bin/env python3
"""Check actual CGraphicContext initialization without reading an invalid HDR enum.

Compiles the complete production class declaration, actual out-of-line defaulted
constructor/destructor and inline HDR setter/getter. Unrelated lock, geometry,
resolution and transform types are small boundaries; this is not the Kodi ABI.
Exact CGraphicContext objects are placement-constructed in poisoned and reused
storage using both default- and value-initialization. HDR object representation
is copied to unsigned-char arrays before any typed getter is allowed. No device,
full GPU library, driver, display or no-signal cure is verified.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('hdr_method_extract',ROOT/'tools/test-aml-texture-upload-context.py')
EX=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(EX)
HEADER='xbmc/windowing/GraphicContext.h';CPP='xbmc/windowing/GraphicContext.cpp';ENUM='xbmc/cores/VideoPlayer/Interface/StreamInfo.h';STEREO='xbmc/rendering/RenderSystemTypes.h'
PRELUDE=r'''
#include <array>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <map>
#include <memory>
#include <new>
#include <optional>
#include <stack>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>
// Opaque complete boundary types. All CGraphicContext members and NSDMIs below
// are the production declaration; none of these boundaries accesses HDR state.
struct CCriticalSection {};
struct CPoint {float x=0,y=0;};
struct CRect {float x1=0,y1=0,x2=0,y2=0;};
struct TransformMatrix {void Reset(){}};
enum RESOLUTION {RES_INVALID=-1};
struct RESOLUTION_INFO {int opaque=0;};
struct OVERSCAN {};
namespace UTILS {namespace COLOR {using Color=uint32_t;}}
@ENUM@
@STEREO@
@CLASS@
@LIFETIME@
// Access the original protected member via a member pointer. Never instantiate
// Access: value-initializing a derived implicit constructor could pre-zero its
// base and conceal the production out-of-line user-provided constructor defect.
struct Access : CGraphicContext {static auto HdrMember(){return &Access::m_hdrType;}};
using Bytes=std::array<unsigned char,sizeof(StreamHdrType)>;
static Bytes Representation(CGraphicContext&value){Bytes bytes{};auto member=Access::HdrMember();std::memcpy(bytes.data(),&(value.*member),bytes.size());return bytes;}
static Bytes DefinedRepresentation(StreamHdrType value){Bytes bytes{};std::memcpy(bytes.data(),&value,bytes.size());return bytes;}
static void Need(bool value,const char*message){if(!value)throw std::runtime_error(message);}
static const std::array<StreamHdrType,5>valid={StreamHdrType::HDR_TYPE_NONE,StreamHdrType::HDR_TYPE_HDR10,StreamHdrType::HDR_TYPE_HDR10PLUS,StreamHdrType::HDR_TYPE_DOLBYVISION,StreamHdrType::HDR_TYPE_HLG};
static bool reusedFirst=false;
struct Destroy {CGraphicContext*value;~Destroy(){value->~CGraphicContext();}};
static void Fresh(bool valueInit,unsigned char poison){
 alignas(CGraphicContext) unsigned char storage[sizeof(CGraphicContext)];std::memset(storage,poison,sizeof(storage));
 auto*context=valueInit?::new(static_cast<void*>(storage)) CGraphicContext(): ::new(static_cast<void*>(storage)) CGraphicContext;
 Destroy guard{context};
 // Representation-only failure boundary: baseline must stop here before Get.
 Need(Representation(*context)==DefinedRepresentation(StreamHdrType::HDR_TYPE_NONE),"poisoned construction initializes HDR representation to NONE before typed read");
 Need(context->GetHDRType()==StreamHdrType::HDR_TYPE_NONE,"defined construction getter starts at NONE");
 for(auto state:valid){context->SetHDRType(state);Need(Representation(*context)==DefinedRepresentation(state),"valid HDR setter preserves requested state");Need(context->GetHDRType()==state,"valid HDR getter round-trips playback state");}
}
static void Reuse(bool valueInit){
 alignas(CGraphicContext) unsigned char storage[sizeof(CGraphicContext)];std::memset(storage,0x5a,sizeof(storage));
 auto*first=::new(static_cast<void*>(storage)) CGraphicContext;
 // Assign a valid value before any typed read, including on the missing-NSDMI
 // counterfactual. This creates real retired HDR state for the next lifetime.
 first->SetHDRType(StreamHdrType::HDR_TYPE_HLG);
 Need(Representation(*first)==DefinedRepresentation(StreamHdrType::HDR_TYPE_HLG),"reuse seed uses valid HDR setter");first->~CGraphicContext();
 auto*second=valueInit?::new(static_cast<void*>(storage)) CGraphicContext(): ::new(static_cast<void*>(storage)) CGraphicContext;
 Destroy guard{second};
 Need(Representation(*second)==DefinedRepresentation(StreamHdrType::HDR_TYPE_NONE),"reused construction resets prior HDR representation before typed read");
 Need(second->GetHDRType()==StreamHdrType::HDR_TYPE_NONE,"reused construction getter starts at NONE");
 second->SetHDRType(StreamHdrType::HDR_TYPE_DOLBYVISION);Need(second->GetHDRType()==StreamHdrType::HDR_TYPE_DOLBYVISION,"reused context still accepts valid HDR updates");
}
int main(int argc,char**argv){try{
 reusedFirst=argc>1&&std::string(argv[1])=="reuse";
 if(reusedFirst){Reuse(false);Reuse(true);}
 else{for(bool valueInit:{false,true})for(unsigned char poison:{0xa5,0x5a,0x6d})Fresh(valueInit,poison);Reuse(false);Reuse(true);}
 std::puts("PASS actual graphics context: poisoned/default/value construction, storage reuse and all valid HDR updates");return 0;
}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''

def generated(header,cpp,enum,stereo):
    declaration=re.sub(r'^\s*#(?:include|pragma)[^\n]*\n','',header,flags=re.M)
    lifetime=[]
    for signature in ('CGraphicContext::CGraphicContext()', 'CGraphicContext::~CGraphicContext()'):
        body=re.search(re.escape(signature)+r'\s*=\s*default;',cpp)
        assert body,'Actual lifetime method no longer out-of-line defaulted: '+signature
        lifetime.append(body.group())
    enum_body=EX.function(enum,'enum class StreamHdrType')+';'
    return PRELUDE.replace('@ENUM@',enum_body).replace('@STEREO@',re.sub(r'^\s*#pragma[^\n]*\n','',stereo,flags=re.M)).replace('@CLASS@',declaration).replace('@LIFETIME@','\n'.join(lifetime))

def variants(header,expect_baseline):
    if expect_baseline:return []
    match=re.search(r'StreamHdrType\s+m_hdrType\s*(?:=\s*StreamHdrType::HDR_TYPE_NONE|\{\s*StreamHdrType::HDR_TYPE_NONE\s*\})\s*;',header)
    assert match,'Expected the actual HDR_TYPE_NONE NSDMI before controls'
    def replace(new):return header[:match.start()]+new+header[match.end():]
    setter=EX.function(header,'  void SetHDRType(')
    assert setter.count('m_hdrType = hdrType;')==1
    return [('missing-initializer-poison',replace('StreamHdrType m_hdrType;'),'poisoned construction initializes HDR representation to NONE before typed read',None),
            ('missing-initializer-reuse',replace('StreamHdrType m_hdrType;'),'reused construction resets prior HDR representation before typed read','reuse'),
            ('wrong-valid-initializer',replace('StreamHdrType m_hdrType{StreamHdrType::HDR_TYPE_HDR10};'),'poisoned construction initializes HDR representation to NONE before typed read',None),
            ('setter-discards-playback-update',header.replace(setter,setter.replace('m_hdrType = hdrType;','m_hdrType = StreamHdrType::HDR_TYPE_NONE; (void)hdrType;'),1),'valid HDR setter preserves requested state',None)]

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output','--out',dest='out',type=Path);parser.add_argument('--sanitize',action='store_true');parser.add_argument('--expect-baseline-failure',action='store_true');args=parser.parse_args()
    out=args.out or Path(tempfile.mkdtemp(prefix='graphic-context-hdr-'));out.mkdir(parents=True,exist_ok=True)
    files={p:(ROOT/p).read_text() for p in (HEADER,CPP,ENUM,STEREO)};script_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest();records=[]
    baseline='poisoned construction initializes HDR representation to NONE before typed read' if args.expect_baseline_failure else None
    rows=[('actual',files[HEADER],baseline,None),*variants(files[HEADER],args.expect_baseline_failure)]
    for name,header,witness,mode in rows:
        source=generated(header,files[CPP],files[ENUM],files[STEREO]);path=out/(name+'.cpp');binary=out/name;path.write_text(source)
        command=['g++','-std=c++17','-O1','-g','-Wall','-Wextra','-Werror','-fno-omit-frame-pointer']
        if args.sanitize:command+=['-fsanitize=address,undefined','-fno-sanitize-recover=undefined','-fno-pie','-no-pie']
        command+=[str(path),'-o',str(binary)];c=subprocess.run(command,capture_output=True,text=True);(out/(name+'-compile.log')).write_text(c.stdout+c.stderr);assert c.returncode==0,c.stderr
        cmd=[str(binary)]+([mode] if mode else []);r=subprocess.run(cmd,capture_output=True,text=True,timeout=15,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0','UBSAN_OPTIONS':'halt_on_error=1:print_stacktrace=1'})
        (out/(name+'-stdout.txt')).write_text(r.stdout);(out/(name+'-stderr.txt')).write_text(r.stderr)
        accepted=(r.returncode==0 and not r.stderr) if witness is None else (r.returncode==1 and r.stderr=='FAIL '+witness+'\n' and not any(t in r.stdout+r.stderr for t in ('AddressSanitizer','UndefinedBehaviorSanitizer','runtime error:','DEADLYSIGNAL')))
        records.append({'name':name,'compile_argv':command,'compile_exit':c.returncode,'run_argv':cmd,'exit':r.returncode,'witness':witness,'accepted':accepted,'generated_sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
        (out/'receipt.json').write_text(json.dumps({'script_sha256':script_hash,'source_sha256':{p:hashlib.sha256(s.encode()).hexdigest() for p,s in files.items()},'baseline_expected':args.expect_baseline_failure,'sanitize':args.sanitize,'results':records},indent=2)+'\n')
        assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest()==script_hash,'Test changed during run'
        assert accepted,(name,r.returncode,r.stdout,r.stderr)
        print(r.stdout.strip() if witness is None else 'PASS expected semantic failure '+name+': '+witness,flush=True)
if __name__=='__main__':main()
