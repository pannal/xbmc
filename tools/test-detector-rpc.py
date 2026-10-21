#!/usr/bin/env python3
"""Connected production JSON-RPC detector schema, dispatch and operation wrappers.

Scanner/binding/share-policy endpoints record exact arguments and side effects;
this does not substitute for the separate production binding/scanner lifecycle
suites. Runs AML and unsupported variants with ASan/UBSan, plus optional compiled
semantic faults. No network, device access or old task artifacts are required.

Example: tools/test-detector-rpc.py --negative-controls --dependency-include /path/to/include
         --schema-builder /path/to/JsonSchemaBuilder --output /tmp/detector-rpc-check
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[1]
SCHEMA=ROOT/'xbmc/interfaces/json-rpc/schema'


def function(source,signature):
    start=source.index(signature);opening=source.index('{',start);depth=1;end=opening+1
    while depth:
        depth+=(source[end]=='{')-(source[end]=='}');end+=1
    return source[start:end]


def discover_dependencies(explicit):
    candidates=list(explicit or [])
    if not explicit:
        candidates.extend((Path.home()/'CoreELEC-build').glob('build.CoreELEC-*/toolchain/*/sysroot/usr/include'))
        candidates.append(Path('/usr/include'))
    found={}
    for name,header in [('rapidjson','rapidjson.h'),('fmt','format.h')]:
        found[name]=next((root/name for root in candidates if (root/name/header).is_file()),None)
        if found[name] is None:
            raise SystemExit('Missing '+name+' headers; use --dependency-include with their include root')
    return found


def discover_builder(explicit):
    if explicit:return explicit
    if os.environ.get('JSON_SCHEMA_BUILDER'):return Path(os.environ['JSON_SCHEMA_BUILDER'])
    executable=shutil.which('JsonSchemaBuilder')
    if executable:return Path(executable)
    candidates=list((Path.home()/'CoreELEC-build').glob('build.CoreELEC-*/toolchain/bin/JsonSchemaBuilder'))
    if candidates:return candidates[0]
    raise SystemExit('Missing JsonSchemaBuilder; use --schema-builder')


def fixture(OUT,dependencies,builder):
    def put(name,value):
        target=OUT/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(value)
    # Copy third-party trees only: adding a cross sysroot include root directly
    # would accidentally compile against its ARM C-library headers on the host.
    for name,path in dependencies.items():
        shutil.copytree(path,OUT/name,dirs_exist_ok=True)
    subprocess.run([str(builder)]+[str(SCHEMA/name) for name in
                   ('version.txt','license.txt','methods.json','types.json','notifications.json')],
                   cwd=OUT,check=True,stdout=subprocess.DEVNULL)
    for name in ('JSONRPC.h','JSONServiceDescription.h','JSONRPCUtils.h','ITransportLayer.h','IClient.h','DetectorOperations.h'):
     put(name,(ROOT/'xbmc/interfaces/json-rpc'/name).read_text())
    for name in ('JSONVariantParser.h','JSONVariantWriter.h','Variant.h'):
     put('utils/'+name,(ROOT/'xbmc/utils'/name).read_text())
    put('utils/log.h',r'''
    #pragma once
    #include <string>
    #include <vector>
    #include "utils/ScopedLogSilence.h"
    #include <fmt/format.h>
    inline std::vector<std::string> recordedLogs;
    constexpr int LOGDEBUG=0,LOGINFO=1,LOGWARNING=2,LOGERROR=3,LOGJSONRPC=4;
    struct CLog{template<class... T>static void Log(int,const char* f,T&&...args){if(CScopedLogSilence::Active())return;recordedLogs.push_back(fmt::format(fmt::runtime(f),std::forward<T>(args)...));}template<class...T>static void Log(int,int,const char*f,T&&...args){Log(0,f,std::forward<T>(args)...);}};
    ''')
    put('utils/StringUtils.h',r'''
    #pragma once
    #define FMT_HEADER_ONLY 1
    #include <fmt/format.h>
    #include <string>
    #include <algorithm>
    #include <cctype>
    struct StringUtils{
     static bool StartsWithNoCase(std::string s,std::string prefix){ToLower(s);ToLower(prefix);return s.rfind(prefix,0)==0;}
     static void ToLower(std::string& s){std::transform(s.begin(),s.end(),s.begin(),[](unsigned char c){return std::tolower(c);});}
     template<class... T>static std::string Format(const std::string& f,T&&...args){return fmt::format(fmt::runtime(f),std::forward<T>(args)...);}
    };
    ''')
    put('utils/FileUtils.h',r'''
    #pragma once
    #include <string>
    #include <cassert>
    #include "utils/log.h"
    inline int sharingChecks=0;
    struct CFileUtils{static bool RemoteAccessAllowed(const std::string& p){assert(CScopedLogSilence::Active());CLog::Log(1,"sensitive share {}",p);++sharingChecks;return p.rfind("shared://",0)==0;}};
    ''')
    utils=(ROOT/'xbmc/interfaces/json-rpc/JSONUtils.h').read_text()
    utils=re.sub(r'^#include "playlists/SmartPlayList.h"\n|^#include "utils/SortUtils.h"\n','',utils,flags=re.M)
    utils=utils.replace(function(utils,'    static bool ParseSorting('),'')
    utils=utils.replace(function(utils,'    static bool GetXspFiltering('),'')
    put('JSONUtils.h',utils)
    # Entire real schema validator and registration/introspection implementation,
    # with only unrelated operation registrations omitted from this host binary.
    schema=(ROOT/'xbmc/interfaces/json-rpc/JSONServiceDescription.cpp').read_text()
    schema=re.sub(r'^#include "(?!JSONServiceDescription.h|JSONRPC.h|ServiceDescription.h|utils/)([^"]+)"\n','',schema,flags=re.M)
    a=schema.index('JsonRpcMethodMap CJSONServiceDescription::m_methodMaps[] = {');b=schema.index('\n};',a)+3
    m= schema[a:b];entries=[line for line in m.splitlines() if '"P3I.Detector.' in line or '"JSONRPC.Introspect"' in line]
    assert len(entries)==12
    schema=schema[:a]+'JsonRpcMethodMap CJSONServiceDescription::m_methodMaps[] = {\n'+'\n'.join(entries)+'\n};'+schema[b:]
    schema='#include <cmath>\n#include "DetectorOperations.h"\n'+schema
    put('schema.cpp',schema)
    rpc=(ROOT/'xbmc/interfaces/json-rpc/JSONRPC.cpp').read_text()
    methods='\n'.join(function(rpc,sig) for sig in ['JSONRPC_STATUS CJSONRPC::Introspect(', 'std::string CJSONRPC::MethodCall(', 'bool CJSONRPC::HandleMethodCall(', 'inline bool CJSONRPC::IsProperJSONRPC(', 'inline void CJSONRPC::BuildResponse('])
    # The application service is only the output-compactness preference boundary.
    prelude=r'''
    #include "JSONRPC.h"
    #include "ServiceDescription.h"
    #include "utils/JSONVariantParser.h"
    #include "utils/JSONVariantWriter.h"
    #include "utils/StringUtils.h"
    #include "utils/log.h"
    struct Settings{bool m_jsonOutputCompact=true;Settings*GetAdvancedSettings(){return this;}};
    struct CServiceBroker{static Settings*GetSettingsComponent(){static Settings value;return &value;}};
    using namespace JSONRPC;
    '''
    if 'JSONRPCPrivacy' in rpc:
     prelude+='#include "utils/JSONRPCPrivacy.h"\n'
    put('dispatch.cpp',prelude+function(rpc,'CVariant DetectorLogRequest(')+'\n'+methods)
    put('operations.cpp',(ROOT/'xbmc/interfaces/json-rpc/DetectorOperations.cpp').read_text())
    put('main.cpp',MAIN)

MAIN = r'''
#include "JSONRPC.h"
#include "DetectorOperations.h"
#include "ServiceDescription.h"
#include "utils/JSONVariantParser.h"
#include "utils/JSONVariantWriter.h"
#include "utils/DetectorAdmission.h"
#include "utils/FileUtils.h"
#include "utils/log.h"
#include <cassert>
#include <iostream>
using namespace JSONRPC;
struct Transport:ITransportLayer{
 bool local=true;int caps=Response;
 bool PrepareDownload(const char*,CVariant&,std::string&)override{return false;}
 bool Download(const char*,CVariant&)override{return false;}
 int GetCapabilities()override{return caps;}
 bool IsLocal()const override{return local;}
}transport;
struct Client:IClient{int permission=OPERATION_PERMISSION_ALL;int GetPermissionFlags()override{return permission;}int GetAnnouncementFlags()override{return 0;}bool SetAnnouncementFlags(int)override{return false;}}client;
int bindings=0,bindingReads=0,playbackReads=0,applications=0,releases=0;
std::string receivedTarget,receivedBinding,receivedPlayback,receivedSelection,receivedJob;int targetStream=0;bool sameVideo=false;
int starts=0;std::string received,receivedID;int selected=0;bool allow=false,busy=false;
CVariant aml_detector_start_job(const std::string& path,int index,bool http){assert(CScopedLogSilence::Active());CLog::Log(1,"sensitive endpoint {}",path);++starts;received=path;selected=index;allow=http;CVariant r;r["version"]=1;r["status"]=busy?"busy":"running";if(!busy)r["requestid"]="test-1";return r;}
CVariant aml_detector_job_status(const std::string&id){receivedID=id;CVariant r;r["version"]=1;r["status"]="completed";r["requestid"]=id;r["progress"]["completed"]=14;r["subtitle"]["valid"]=true;r["subtitle"]["top"]=0;return r;}
CVariant aml_detector_cancel_job(const std::string&id){receivedID=id;CVariant r;r["version"]=1;r["status"]="running";r["cancelrequested"]=true;return r;}
CVariant aml_detector_bind_job(const std::string&id,const std::string&target,int stream,bool same){
 assert(CScopedLogSilence::Active());CLog::Log(1,"sensitive bind target {}",target);++bindings;
 receivedJob=id;receivedTarget=target;targetStream=stream;sameVideo=same;
 CVariant r;r["version"]=1;r["status"]="accepted";r["bindingid"]="binding-1";return r;
}
CVariant aml_detector_binding_status(const std::string&id){++bindingReads;receivedBinding=id;CVariant r;r["version"]=1;r["status"]="applied";r["bindingid"]=id;r["subtitleavailable"]=true;r["nativeavailable"]=false;return r;}
CVariant aml_detector_playback_status(){++playbackReads;CVariant r;r["version"]=1;r["status"]="playing";r["playbackid"]="playback-1";r["selectionid"]="selection-2";return r;}
CVariant aml_detector_apply_binding(const std::string&id,const std::string&playback,const std::string&selection){++applications;receivedBinding=id;receivedPlayback=playback;receivedSelection=selection;CVariant r;r["version"]=1;r["status"]="accepted";return r;}
CVariant aml_detector_release_binding(const std::string&id){++releases;receivedBinding=id;CVariant r;r["version"]=1;r["status"]="released";return r;}
CVariant call(const std::string& method,CVariant params=CVariant(CVariant::VariantTypeObject)){
 CVariant input;input["jsonrpc"]="2.0";input["id"]=1;input["method"]=method;input["params"]=params;std::string text;assert(CJSONVariantWriter::Write(input,text,true));
 auto output=CJSONRPC::MethodCall(text,&transport,&client);CVariant result;assert(CJSONVariantParser::Parse(output,result));return result;
}
CVariant path(const std::string&s){CVariant r;r["path"]=s;return r;}
CVariant binding(const std::string&id="binding-1"){CVariant r;r["bindingid"]=id;return r;}
CVariant bindParams(const std::string&target){auto r=path(target);r["requestid"]="test-1";return r;}
CVariant applyParams(){auto r=binding();r["playbackid"]="playback-1";r["selectionid"]="selection-2";return r;}
void error(const CVariant&r,int code){assert(r["error"]["code"].asInteger()==code);}
int main(){
 for(auto* type:JSONRPC_SERVICE_TYPES)CJSONServiceDescription::AddType(type);
 for(auto* method:JSONRPC_SERVICE_METHODS){CVariant parsed;assert(CJSONVariantParser::Parse("{"+std::string(method)+"}",parsed));auto name=parsed.begin_map()->first;if(name.rfind("P3I.Detector.",0)==0||name=="JSONRPC.Introspect")assert(CJSONServiceDescription::AddBuiltinMethod(method));}
 CJSONServiceDescription::ResolveReferences();
 auto cap=call("P3I.Detector.GetCapabilities");assert(cap["result"]["version"].asInteger()==1);
#if defined(HAS_LIBAMCODEC)
 assert(cap["result"]["supported"].asBoolean());
 assert(cap["result"]["geometryresults"].asBoolean()&&cap["result"]["resultbinding"].asInteger()==1);
 assert(cap["result"]["bindingproperty"].asString()==DETECTOR_PLAYBACK::PROPERTY&&cap["result"]["maxbindings"].asInteger()==64);
#else
 assert(!cap["result"]["supported"].asBoolean());
 assert(!cap["result"]["geometryresults"].asBoolean()&&cap["result"]["resultbinding"].asInteger()==0);
#endif
 CVariant filter;filter["filter"]["id"]="P3I.Detector";filter["filter"]["type"]="namespace";auto intro=call("JSONRPC.Introspect",filter);
 assert(intro["result"]["methods"].size()==11);
 for(const auto* name:{"Start","GetStatus","Cancel","SetSkipNext","ClearSkipNext","Bind","GetBinding","GetPlayback","Apply","ReleaseBinding","GetCapabilities"})
  assert(intro["result"]["methods"].isMember(std::string("P3I.Detector.")+name));
 const std::string complicated=u8"https://person:secret@host.example/影片,a\"b.mkv?x=one,two|User-Agent=Kodi%20API&X-Plex-Token=fake_test_only";
 auto result=call("P3I.Detector.Start",path(complicated));
#if defined(HAS_LIBAMCODEC)
 assert(result["result"]["status"].asString()=="running"&&starts==1&&received==complicated&&selected==-1&&!allow&&sharingChecks==0);
 auto options=path("/local/file.mkv");options["options"]["streamindex"]=3;options["options"]["allowhttp"]=true;
 result=call("P3I.Detector.Start",options);assert(selected==3&&allow&&starts==2);
 busy=true;result=call("P3I.Detector.Start",path("/another.mkv"));assert(result["result"]["status"].asString()=="busy"&&!result["result"].isMember("requestid"));busy=false;
 transport.local=false;const int before=starts;result=call("P3I.Detector.Start",path("/private/file.mkv"));assert(result["result"]["status"].asString()=="path_not_shared"&&starts==before&&sharingChecks==1);
 result=call("P3I.Detector.Start",path("shared://video/file.mkv"));assert(result["result"]["status"].asString()=="running"&&starts==before+1&&sharingChecks==2);transport.local=true;
 CVariant id;id["requestid"]="test-1";result=call("P3I.Detector.GetStatus",id);assert(receivedID=="test-1"&&result["result"]["progress"]["completed"].asInteger()==14&&result["result"]["subtitle"]["valid"].asBoolean()&&result["result"]["subtitle"]["top"].asInteger()==0);
 result=call("P3I.Detector.Cancel",id);assert(result["result"]["cancelrequested"].asBoolean());
 // The actual schema supplies default source association and selected stream.
 auto bindingResult=call("P3I.Detector.Bind",bindParams(complicated));
 assert(bindingResult["result"]["status"].asString()=="accepted"&&bindingResult["result"]["bindingid"].asString()=="binding-1");
 assert(bindings==1&&receivedJob=="test-1"&&receivedTarget==complicated&&targetStream==-1&&!sameVideo);
 auto explicitBind=bindParams(complicated);explicitBind["options"]["association"]="same_video";explicitBind["options"]["streamindex"]=3;
 call("P3I.Detector.Bind",explicitBind);assert(bindings==2&&receivedTarget==complicated&&receivedJob=="test-1"&&targetStream==3&&sameVideo);
 explicitBind["options"]["association"]="source";explicitBind["options"]["streamindex"]=2147483647;
 call("P3I.Detector.Bind",explicitBind);assert(!sameVideo&&targetStream==2147483647);
 const int bindBefore=bindings,shareBefore=sharingChecks;
 transport.local=false;result=call("P3I.Detector.Bind",bindParams(complicated));
 assert(result["result"]["status"].asString()=="path_not_shared"&&bindings==bindBefore&&sharingChecks==shareBefore+1);
 result=call("P3I.Detector.Bind",bindParams("shared://video,target/影片.mkv"));
 assert(result["result"]["status"].asString()=="accepted"&&bindings==bindBefore+1&&sharingChecks==shareBefore+2);
 assert(receivedTarget=="shared://video,target/影片.mkv"&&!sameVideo&&targetStream==-1);transport.local=true;
 // IDs remain structured, and outputs are forwarded without converting accepted
 // application into a falsely applied state in the wrapper.
 const std::string opaque=u8"binding,é\"-1";
 result=call("P3I.Detector.GetBinding",binding(opaque));
 assert(bindingReads==1&&receivedBinding==opaque&&result["result"]["status"].asString()=="applied"&&result["result"]["subtitleavailable"].asBoolean());
 result=call("P3I.Detector.GetPlayback");assert(playbackReads==1&&result["result"]["playbackid"].asString()=="playback-1"&&result["result"]["selectionid"].asString()=="selection-2");
 auto application=applyParams();application["bindingid"]=opaque;application["playbackid"]=u8"play,é\"";application["selectionid"]="selection,\"-2";
 result=call("P3I.Detector.Apply",application);
 assert(applications==1&&receivedBinding==opaque&&receivedPlayback==u8"play,é\""&&receivedSelection=="selection,\"-2"&&result["result"]["status"].asString()=="accepted");
 result=call("P3I.Detector.ReleaseBinding",binding(opaque));assert(releases==1&&receivedBinding==opaque&&result["result"]["status"].asString()=="released");
 auto armed=call("P3I.Detector.SetSkipNext");assert(armed["result"]["status"].asString()=="armed"&&DETECTOR_ADMISSION::Value()=="1");
 CVariant token;token["token"]=armed["result"]["token"];
 auto newer=call("P3I.Detector.SetSkipNext");result=call("P3I.Detector.ClearSkipNext",token);assert(result["result"]["status"].asString()=="unchanged"&&DETECTOR_ADMISSION::Value()=="1");
 token["token"]=newer["result"]["token"];result=call("P3I.Detector.ClearSkipNext",token);assert(result["result"]["status"].asString()=="cleared"&&DETECTOR_ADMISSION::Value().empty());
 armed=call("P3I.Detector.SetSkipNext");assert(DETECTOR_ADMISSION::Capture());token["token"]=armed["result"]["token"];assert(call("P3I.Detector.ClearSkipNext",token)["result"]["status"].asString()=="unchanged");
 token["token"]="01";error(call("P3I.Detector.ClearSkipNext",token),InvalidParams);
#else
 assert(result["result"]["status"].asString()=="unsupported"&&starts==0&&sharingChecks==0);
 result=call("P3I.Detector.SetSkipNext");assert(result["result"]["status"].asString()=="unsupported"&&DETECTOR_ADMISSION::Value().empty());
 CVariant id;id["requestid"]="test-1";assert(call("P3I.Detector.GetStatus",id)["result"]["status"].asString()=="unsupported");assert(call("P3I.Detector.Cancel",id)["result"]["status"].asString()=="unsupported");
 assert(call("P3I.Detector.Bind",bindParams(complicated))["result"]["status"].asString()=="unsupported");
 assert(call("P3I.Detector.GetBinding",binding())["result"]["status"].asString()=="unsupported");
 assert(call("P3I.Detector.GetPlayback")["result"]["status"].asString()=="unsupported");
 assert(call("P3I.Detector.Apply",applyParams())["result"]["status"].asString()=="unsupported");
 assert(call("P3I.Detector.ReleaseBinding",binding())["result"]["status"].asString()=="unsupported");
 assert(bindings==0&&bindingReads==0&&playbackReads==0&&applications==0&&releases==0&&sharingChecks==0);

#endif
 client.permission=ReadData;error(call("P3I.Detector.Start",path("/file.mkv")),BadPermission);error(call("P3I.Detector.Cancel"),BadPermission);error(call("P3I.Detector.SetSkipNext"),BadPermission);assert(call("P3I.Detector.GetCapabilities")["result"].isObject());client.permission=OPERATION_PERMISSION_ALL;
 error(call("P3I.Detector.Start",path("")),InvalidParams);error(call("P3I.Detector.Start"),InvalidParams);
 auto invalid=path("/file.mkv");invalid["options"]["streamindex"]=-2;error(call("P3I.Detector.Start",invalid),InvalidParams);
 invalid["options"]["streamindex"]=int64_t(4294967295LL);error(call("P3I.Detector.Start",invalid),InvalidParams);
 invalid["options"]["streamindex"]=0;invalid["options"]["allowhttp"]="yes";error(call("P3I.Detector.Start",invalid),InvalidParams);
 invalid=path("/file.mkv");invalid["options"]["stream_index"]=0;error(call("P3I.Detector.Start",invalid),InvalidParams);
 invalid=path("/file.mkv");invalid["options"]["allow_http"]=true;error(call("P3I.Detector.Start",invalid),InvalidParams);
 invalid=path("/file.mkv");invalid["surprise"]=true;error(call("P3I.Detector.Start",invalid),InvalidParams);
 // New operations keep their explicit permissions on every optional build.
 client.permission=ReadData;
 error(call("P3I.Detector.Bind",bindParams("/file.mkv")),BadPermission);
 error(call("P3I.Detector.Apply",applyParams()),BadPermission);
 error(call("P3I.Detector.ReleaseBinding",binding()),BadPermission);
 assert(call("P3I.Detector.GetBinding",binding())["result"].isObject());
 assert(call("P3I.Detector.GetPlayback")["result"].isObject());
 client.permission=ControlPlayback;
 error(call("P3I.Detector.Bind",bindParams("/file.mkv")),BadPermission);
 error(call("P3I.Detector.GetBinding",binding()),BadPermission);
 error(call("P3I.Detector.GetPlayback"),BadPermission);
 assert(call("P3I.Detector.Apply",applyParams())["result"].isObject());
 assert(call("P3I.Detector.ReleaseBinding",binding())["result"].isObject());
 client.permission=ReadData|ControlPlayback;assert(call("P3I.Detector.Bind",bindParams("/file.mkv"))["result"].isObject());
 client.permission=OPERATION_PERMISSION_ALL;
 // Binding options use the canonical association enum, and refuse typos/types.
 error(call("P3I.Detector.Bind"),InvalidParams);
 for(const auto* association:{"samevideo","same-video","unknown","","SOURCE"}){
  auto p=bindParams("/file.mkv");p["options"]["association"]=association;error(call("P3I.Detector.Bind",p),InvalidParams);
 }
 auto badBind=bindParams("/file.mkv");badBind["options"]["association"]=true;error(call("P3I.Detector.Bind",badBind),InvalidParams);
 badBind=bindParams("/file.mkv");badBind["options"]["streamindex"]=-2;error(call("P3I.Detector.Bind",badBind),InvalidParams);
 badBind["options"]["streamindex"]=int64_t(4294967295LL);error(call("P3I.Detector.Bind",badBind),InvalidParams);
 badBind=bindParams("/file.mkv");badBind["options"]["stream_index"]=0;error(call("P3I.Detector.Bind",badBind),InvalidParams);
 badBind=bindParams("/file.mkv");badBind["extra"]=true;error(call("P3I.Detector.Bind",badBind),InvalidParams);
 badBind=bindParams("");error(call("P3I.Detector.Bind",badBind),InvalidParams);
 for(const auto* method:{"P3I.Detector.GetBinding","P3I.Detector.ReleaseBinding"}){
  error(call(method),InvalidParams);error(call(method,binding("")),InvalidParams);error(call(method,binding(std::string(33,'x'))),InvalidParams);
  auto p=binding();p["bindingid"]=3;error(call(method,p),InvalidParams);p=binding();p["extra"]=true;error(call(method,p),InvalidParams);
 }
 for(const auto* field:{"requestid","path"}){
  auto p=bindParams("/file.mkv");p[field]="";error(call("P3I.Detector.Bind",p),InvalidParams);
  p=bindParams("/file.mkv");p[field]=true;error(call("P3I.Detector.Bind",p),InvalidParams);
 }
 badBind=bindParams("/file.mkv");badBind["requestid"]=std::string(33,'x');error(call("P3I.Detector.Bind",badBind),InvalidParams);
 badBind=bindParams(std::string(32769,'x'));error(call("P3I.Detector.Bind",badBind),InvalidParams);
 for(const auto* field:{"bindingid","playbackid","selectionid"}){
  for(const auto& value:{std::string(""),std::string(33,'x')}){auto p=applyParams();p[field]=value;error(call("P3I.Detector.Apply",p),InvalidParams);}
  auto p=applyParams();p[field]=true;error(call("P3I.Detector.Apply",p),InvalidParams);
 }
 error(call("P3I.Detector.Apply",binding()),InvalidParams);
 auto p=applyParams();p["extra"]=true;error(call("P3I.Detector.Apply",p),InvalidParams);
 p=CVariant(CVariant::VariantTypeObject);p["extra"]=true;error(call("P3I.Detector.GetPlayback",p),InvalidParams);
 error(call("P3I.Detector.Missing"),MethodNotFound);
 // Exercise the actual dispatch logging for valid/batch/malformed envelopes
 // and syntax errors. The scanner already received the exact credential URL.
 CVariant req;req["jsonrpc"]="2.0";req["id"]="fake_test_only";req["method"]="p3i.detector.Start";req["params"]=path(complicated);
 CVariant batch(CVariant::VariantTypeArray);batch.append(req);std::string text;CJSONVariantWriter::Write(batch,text,true);CJSONRPC::MethodCall(text,&transport,&client);
 req["jsonrpc"]="invalid";CJSONVariantWriter::Write(req,text,true);CJSONRPC::MethodCall(text,&transport,&client);
 CJSONRPC::MethodCall(text+" BROKEN fake_test_only",&transport,&client);
 req["jsonrpc"]="2.0";req["method"]="P3I.Detector.Bind";req["params"]=bindParams(complicated);
 CJSONVariantWriter::Write(req,text,true);CJSONRPC::MethodCall(text,&transport,&client);
 req["params"]["options"]["association"]="invalid_fake_test_only";
 CJSONVariantWriter::Write(req,text,true);CJSONRPC::MethodCall(text,&transport,&client);
 for(const auto& log:recordedLogs){assert(log.find("fake_test_only")==std::string::npos&&log.find("host.example")==std::string::npos&&log.find("person:secret")==std::string::npos&&log.find("影片")==std::string::npos);}
 transport.caps=0;error(call("P3I.Detector.GetCapabilities"),MethodNotFound);
 std::cout<<"PASS: actual JSON parse/write + MethodCall/HandleMethodCall + schema CheckCall/defaults/introspection + registered detector wrappers; AML="
#if defined(HAS_LIBAMCODEC)
 <<1
#else
 <<0
#endif
 <<"; scanner/sharing/services recording boundaries\n";
 CJSONServiceDescription::Cleanup();
}
'''


def build_run(out,name,argv,negative=False):
    (out/(name+'-argv.json')).write_text(json.dumps(argv,indent=2))
    with (out/(name+'-compile.log')).open('w') as log:
        compiled=subprocess.run(argv,stdout=log,stderr=subprocess.STDOUT)
    assert compiled.returncode==0,(name,(out/(name+'-compile.log')).read_text())
    result=subprocess.run([str(out/name)],capture_output=True,text=True,timeout=45,
                          env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'})
    (out/(name+'-run.log')).write_text(result.stdout+result.stderr)
    if negative:
        assert result.returncode!=0 and 'Assertion' in result.stderr,(name,result.stdout,result.stderr)
        print('REJECTED:',name,flush=True)
    else:
        assert result.returncode==0,(name,result.stdout,result.stderr)
        print(result.stdout.strip(),flush=True)
    return {'case':name,'negative':negative,'exit':result.returncode}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dependency-include',type=Path,action='append')
    parser.add_argument('--schema-builder',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--negative-controls',action='store_true')
    args=parser.parse_args();dependencies=discover_dependencies(args.dependency_include);builder=discover_builder(args.schema_builder).resolve()
    with tempfile.TemporaryDirectory(prefix='detector-rpc-') as temporary:
        out=args.output or Path(temporary);out.mkdir(parents=True,exist_ok=True)
        sources=[ROOT/'xbmc/interfaces/json-rpc'/name for name in
                 ('JSONServiceDescription.cpp','JSONRPC.cpp','DetectorOperations.cpp','DetectorOperations.h')]
        sources.extend(SCHEMA/name for name in ('methods.json','types.json','notifications.json'))
        hashes={str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
        fixture(out,dependencies,builder)
        receipts=[];aml_argv=None
        for aml in (False,True):
            name='aml' if aml else 'nonaml'
            argv=[os.environ.get('CXX','g++'),'-std=c++17','-O1','-g','-Wall','-Wextra',
                  '-Wno-unused-parameter','-Wno-unused-variable','-Wno-unused-function',
                  '-pthread','-fsanitize=address,undefined','-fno-pie','-no-pie','-DFMT_HEADER_ONLY=1',
                  '-DTARGET_LINUX','-I'+str(out),'-I'+str(ROOT/'xbmc')]
            if aml:argv.append('-DHAS_LIBAMCODEC')
            argv.extend(str(out/file) for file in ('schema.cpp','dispatch.cpp','operations.cpp','main.cpp'))
            argv.extend(str(ROOT/'xbmc/utils'/file) for file in ('Variant.cpp','JSONVariantParser.cpp','JSONVariantWriter.cpp'))
            argv.extend(['-o',str(out/name)])
            receipts.append(build_run(out,name,argv))
            if aml:aml_argv=argv
        if args.negative_controls:
            variants=[
                ('wrapper-scope-omitted','operations.cpp','const CScopedLogSilence sensitiveInput;','/* scope omitted */'),
                ('sharing-bypass','operations.cpp','!transport ||\n           (!transport->IsLocal() && !CFileUtils::RemoteAccessAllowed(params["path"].asString()))','false'),
                ('privacy-bypass','dispatch.cpp','CJSONVariantWriter::Write(DetectorLogRequest(inputroot), safeRequest, true);','CJSONVariantWriter::Write(inputroot, safeRequest, true);'),
                ('bind-sharing-bypass','operations.cpp','!transport || (!transport->IsLocal() &&\n           !CFileUtils::RemoteAccessAllowed(params["path"].asString()))','false'),
                ('bind-association-ignored','operations.cpp','params["options"]["association"].asString() == "same_video"','false'),
                ('apply-selection-lost','operations.cpp','params["selectionid"].asString()','params["playbackid"].asString()'),
            ]
            for name,target,old,new in variants:
                original=(out/target).read_text();assert old in original,name
                mutant=out/(name+'.cpp');mutant.write_text(original.replace(old,new,1))
                argv=[str(mutant) if arg==str(out/target) else arg for arg in aml_argv]
                argv=[arg for arg in argv if arg!='-fsanitize=address,undefined']
                argv[argv.index('-o')+1]=str(out/name)
                receipts.append(build_run(out,name,argv,negative=True))
        (out/'verification.json').write_text(json.dumps({'sources':hashes,'cases':receipts,
            'fixture_sha256':{name:hashlib.sha256((out/name).read_bytes()).hexdigest() for name in
                ('ServiceDescription.h','schema.cpp','dispatch.cpp','operations.cpp','main.cpp')}},indent=2))


if __name__=='__main__':
    main()
