#!/usr/bin/env python3
"""Exercise the production persistent cache with real temporary filesystem I/O.

Cache persistence uses real temporary files with Kodi VFS/profile translation and
digest integration substituted. Production startup is exercised with synchronous
worker and scanner boundaries; cancellation concurrency is covered separately.
No media files are accessed.
"""
import argparse
import os
import pathlib
import runpy
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
FILE_STUB = r'''
#pragma once
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <string>
#include <sys/stat.h>
#include <unistd.h>
#define __stat64 stat
namespace XFILE {
inline std::string deniedDelete,switchOnReadPath,nextProfile;inline bool denyMarkerRename=false;
inline std::string Path(std::string p) {
 const std::string prefix="special://profile/";
 if(p.rfind(prefix,0)==0) return std::string(std::getenv("CACHE_TEST_PROFILE"))+"/"+p.substr(prefix.size());
 return p;
}
class CFile {
 std::fstream file; std::string path;
public:
 static int Stat(const std::string& p,struct stat* s) {return ::stat(Path(p).c_str(),s);}
 static bool Rename(const std::string& a,const std::string& b) {if(denyMarkerRename&&b.size()>=7&&b.substr(b.size()-7)=="version")return false;return ::rename(Path(a).c_str(),Path(b).c_str())==0;}
 static bool Delete(const std::string& p) {if(Path(p)==deniedDelete)return false;return ::unlink(Path(p).c_str())==0;}
 bool Open(const std::string& p) {path=Path(p);file.open(path,std::ios::in|std::ios::binary);return file.is_open();}
 bool OpenForWrite(const std::string& p,bool) {path=Path(p);file.open(path,std::ios::out|std::ios::binary|std::ios::trunc);return file.is_open();}
 int64_t GetLength() {struct stat s{};return ::stat(path.c_str(),&s)==0?s.st_size:-1;}
 ssize_t Read(void* p,size_t n) {file.read(static_cast<char*>(p),n);const auto got=file.gcount();if(path==switchOnReadPath&&!nextProfile.empty()){setenv("CACHE_TEST_PROFILE",nextProfile.c_str(),1);nextProfile.clear();}return got;}
 ssize_t Write(const void* p,size_t n) {file.write(static_cast<const char*>(p),n);return file?static_cast<ssize_t>(n):-1;}
 void Close() {file.close();}
};
}
'''
URL_STUB = r'''
#pragma once
#include <algorithm>
#include <cctype>
#include <string>
class CURL {
 std::string path;
public:
 explicit CURL(const std::string& p):path(p){}
 const std::string& Get()const{return path;}
 bool IsProtocol(const std::string& p)const{auto value=path.substr(0,path.find(":"));std::transform(value.begin(),value.end(),value.begin(),[](unsigned char c){return std::tolower(c);});return value==p;}
 std::string GetFileName()const{auto start=path.find("://");if(start==std::string::npos)return path;start=path.find('/',start+3);if(start==std::string::npos)return {};auto end=path.find_first_of("?#|",start);return path.substr(start+1,end==std::string::npos?end:end-start-1);}
 static std::string Decode(const std::string& p){std::string out;for(size_t n=0;n<p.size();++n){if(p[n]=='%'&&n+2<p.size()&&std::isxdigit(static_cast<unsigned char>(p[n+1]))&&std::isxdigit(static_cast<unsigned char>(p[n+2]))){out+=static_cast<char>(std::stoi(p.substr(n+1,2),nullptr,16));n+=2;}else out+=p[n];}return out;}
};
'''
HTTP_STUB = r'''
#pragma once
#include "File.h"
#include "URL.h"
#include <map>
namespace XFILE {
struct HttpIdentity {int64_t size=1000,modified=1234;std::string tag;};
inline std::map<std::string,HttpIdentity> httpResources;
inline int httpStats=0;
inline std::string lastHttpRequest;
struct Header {std::string tag;std::string GetValue(const std::string&)const{return tag;}};
class CCurlFile {Header header;public:
 int Stat(const CURL& url,struct stat* info){++httpStats;lastHttpRequest=url.Get();auto found=httpResources.find(url.Get());if(found==httpResources.end())return -1;*info={};info->st_mode=S_IFREG;info->st_size=found->second.size;info->st_mtime=found->second.modified;header.tag=found->second.tag;return 0;}
 const Header& GetHttpHeader()const{return header;}
};
}
'''

DIRECTORY_STUB = r'''
#pragma once
#include "File.h"
#include <filesystem>
#include "FileItem.h"
namespace XFILE {inline int enumerations=0;inline bool denyEnumeration=false;class CDirectory {public: static bool Create(const std::string& p) {
 std::error_code e; std::filesystem::create_directories(Path(p),e);return !e;}
static bool GetDirectory(const std::string& path,CFileItemList& items,const std::string&,int){
 ++enumerations;if(denyEnumeration)return false;
 std::error_code error;
 for(const auto& entry:std::filesystem::directory_iterator(Path(path),error)){
  auto item=std::make_shared<CFileItem>();item->path=path+entry.path().filename().string();item->m_bIsFolder=entry.is_directory();items.items.push_back(item);
 }
 return !error;
}
};constexpr int DIR_FLAG_NO_FILE_DIRS=1,DIR_FLAG_NO_FILE_INFO=2,DIR_FLAG_BYPASS_CACHE=4;}

'''
DIGEST_STUB = r'''
#pragma once
#include <openssl/sha.h>
#include <string>
namespace KODI::UTILITY {class CDigest {public:enum class Type {SHA256};
 static std::string Calculate(Type,const std::string& value) {
 unsigned char result[SHA256_DIGEST_LENGTH];SHA256(reinterpret_cast<const unsigned char*>(value.data()),value.size(),result);
 const char* hex="0123456789abcdef";std::string out;
 for(auto c:result){out+=hex[c>>4];out+=hex[c&15];}return out;
 }};}
'''
TEST = r'''
#include "utils/ActiveAreaCache.h"
#include <algorithm>
#include <cassert>
#include <filesystem>
#include <fstream>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <utime.h>
#include <vector>
using namespace ACTIVE_AREA_CACHE;
void Write(const std::string& p,const std::string& s) {std::ofstream(p,std::ios::binary)<<s;}
std::string CacheEntry(const std::vector<std::filesystem::path>& entries,const Identity& expected) {
 std::string match;
 for(const auto& entry:entries) {
  std::ifstream input(entry);int version,stream,codec,width,height;bool native;std::string key;
  assert(input>>version>>key>>stream>>codec>>width>>height>>native);
  if(stream==expected.streamIndex&&codec==expected.codec&&width==expected.width&&
     height==expected.height&&native==expected.nativeDV) {
   assert(match.empty());match=entry.string();
  }
 }
 return match;
}
int main(int argc,char** argv) {
 assert(argc==2);const std::string root=argv[1];setenv("CACHE_TEST_PROFILE",root.c_str(),1);
 const auto media=root+"/fake-video.mkv";Write(media,"test file identity only");
 Identity id;id.path=media;id.streamIndex=0;id.codec=173;id.width=1920;id.height=1080;
 assert(ReadIdentity(id));Record loaded,record;record.state=3;record.hasStrict=true;record.hasSubtitle=true;
 record.strict={20,20,0,0};record.subtitle={40,40,0,0};
 assert(!Load(id,loaded));assert(Store(id,record));assert(Load(id,loaded));
 assert(loaded.hasStrict&&loaded.strict==record.strict&&loaded.subtitle==record.subtitle);
 assert(Store(id,record)); // atomic replacement preserves a readable record
 assert(Load(id,loaded));
 assert(!loaded.highConfidence);record.highConfidence=true;
 assert(Store(id,record));assert(Load(id,loaded));assert(loaded.highConfidence);
 record.displayAspect=16.0/9.0;assert(Store(id,record));assert(Load(id,loaded));
 assert(loaded.displayAspect==record.displayAspect);
 // Geometry-only persistence is distinct from sampled strict/subtitle evidence.
 Identity crop=id;crop.width=3832;crop.height=1600;assert(ReadIdentity(crop));
 Record geometry;geometry.state=1;geometry.preCropped=true;geometry.uniqueStream=true;
 geometry.displayAspect=3832.0/1600.0;
 assert(Store(crop,geometry));assert(Load(crop,loaded));
 assert(loaded.preCropped&&!loaded.hasStrict&&!loaded.hasSubtitle&&!loaded.highConfidence);
 assert(loaded.displayAspect==geometry.displayAspect&&loaded.uniqueStream);
 auto badGeometry=geometry;badGeometry.displayAspect=0.0;assert(!Store(crop,badGeometry));
 badGeometry=geometry;badGeometry.displayAspect=2.0;assert(!Store(crop,badGeometry));
 badGeometry=geometry;badGeometry.hasStrict=true;assert(!Store(crop,badGeometry));
 badGeometry=geometry;badGeometry.highConfidence=true;assert(!Store(crop,badGeometry));
 auto full=crop;full.width=1920;full.height=1080;
 badGeometry=geometry;badGeometry.displayAspect=16.0/9.0;assert(!Store(full,badGeometry));

 for(int field=0;field<9;field++) {
  auto different=id;
  switch(field) {case 0:different.size++;break;case 1:different.modified++;break;
   case 2:different.created++;break;case 3:different.inode++;break;case 4:different.streamIndex++;break;
   case 5:different.codec++;break;case 6:different.width++;break;case 7:different.height++;break;
   case 8:different.nativeDV=true;break;}
  assert(!Load(different,loaded));
 }
 auto unavailable=id;unavailable.streamIndex=-1;assert(!ReadIdentity(unavailable));
 unavailable=id;unavailable.path=root+"/missing";assert(!ReadIdentity(unavailable));
 unavailable=id;unavailable.path=root;assert(!ReadIdentity(unavailable));
 auto bad=record;bad.strict={600,600,0,0};assert(!Store(id,bad));
 bad=record;bad.subtitle={0,0,1000,1000};assert(!Store(id,bad));
 bad=record;bad.state=0;assert(!Store(id,bad));bad={};assert(!Store(id,bad));
 Record text;text.state=2;text.hasSubtitle=true;text.subtitle={120,120,160,160};
 assert(Store(id,text));assert(Load(id,loaded));assert(!loaded.hasStrict&&loaded.hasSubtitle&&loaded.state==2);
 assert(!loaded.highConfidence);text.highConfidence=true;assert(!Store(id,text));text.highConfidence=false;
 auto directory=root+"/active-area-cache";
 std::string entry;
 std::vector<std::filesystem::path> entries;
 for(auto const& p:std::filesystem::directory_iterator(directory)) {
  if(p.path().filename()=="version")continue;
  assert(p.path().extension()==".dat");entries.push_back(p.path());
  std::ifstream in(p.path());std::string data((std::istreambuf_iterator<char>(in)),{});
  assert(data.find(media)==std::string::npos); // source paths are not persisted
 }
 assert(entries.size()==2);
 // The corruption target must be the original record in either iteration order.
 std::sort(entries.begin(),entries.end());entry=CacheEntry(entries,id);
 const auto cropEntry=CacheEntry(entries,crop);assert(!cropEntry.empty()&&cropEntry!=entry);
 std::reverse(entries.begin(),entries.end());
 assert(CacheEntry(entries,id)==entry&&CacheEntry(entries,crop)==cropEntry);
 assert(!entry.empty());
 Write(entry,"1 123 truncated");assert(!Load(id,loaded));
 Write(entry,std::string(513,'x'));assert(!Load(id,loaded));
 assert(Store(id,text));std::ifstream in(entry);std::string data((std::istreambuf_iterator<char>(in)),{});in.close();
 data[0]='9';Write(entry,data);assert(!Load(id,loaded));
 data[0]='1';Write(entry,data);assert(!Load(id,loaded)); // old confidence-free format cannot seed an override
 Write(media,"changed file size");assert(!Store(id,text));auto changed=id;assert(ReadIdentity(changed));assert(!Load(changed,loaded));
 // A same-size replacement still invalidates through timestamp/inode identity.
 Write(media,"test file identity only");assert(ReadIdentity(id));assert(Store(id,record));
 const auto replacement=media+".replacement";Write(replacement,"test file identity only");
 std::filesystem::rename(replacement,media);changed=id;assert(ReadIdentity(changed));assert(!(changed==id));assert(!Load(changed,loaded));
 struct utimbuf dates{};utime(media.c_str(),&dates);assert(!ReadIdentity(changed));
 std::cout<<"PASS: persistence, replacement, metadata/stream isolation, withheld-native state, malformed records, unavailable identity\n";
}
'''

HTTP_TEST = r'''
#include "utils/ActiveAreaCache.h"
#include "filesystem/CurlFile.h"
#include <cassert>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
using namespace ACTIVE_AREA_CACHE;
Identity Identify(const std::string& path,XFILE::HttpIdentity metadata={}){
 XFILE::httpResources[path]=metadata;Identity id;id.path=path;
 id.streamIndex=0;id.codec=173;id.width=1920;id.height=1080;id.nativeDV=true;
 const auto before=XFILE::httpStats;assert(ReadIdentity(id));
 assert(XFILE::httpStats==before+1&&XFILE::lastHttpRequest==path&&id.path==path);
 return id;
}
int main(int argc,char** argv){
 assert(argc==2);const auto profile=std::string(argv[1])+"/http-profile";
 std::filesystem::create_directories(profile);setenv("CACHE_TEST_PROFILE",profile.c_str(),1);
 const auto privateRecords=[&]{
  for(const auto& entry:std::filesystem::directory_iterator(profile+"/active-area-cache")){
   std::ifstream input(entry.path());const std::string bytes((std::istreambuf_iterator<char>(input)),{});
   assert(bytes.find("synthetic-secret")==std::string::npos);
   assert(bytes.find("fake.example")==std::string::npos);
   assert(bytes.find("synthetic-private-validator")==std::string::npos);
   assert(entry.path().filename()=="version"||entry.path().filename().string().size()==133);
  }
 };
 // Synthetic URLs: retained bytes, order, duplicates, escaped values, headers and
 // credentials must remain part of identity. Only the two proven session names
 // may disappear from the key; requests and revalidation retain the whole URL.
 const std::string endpoint="https://fake.example:32400/library/parts/123/456/file.mkv";
 const std::string stable="?X-Plex-Token=synthetic-secret&a=%2f&a=/&profile=original";
 const std::string tail="|User-Agent=synthetic%20client&seekable=1";
 const auto first=endpoint+stable+"&X-Plex-Session-Id=first&X-Plex-Session-Identifier=one"+tail;
 const auto second=endpoint+stable+"&X-Plex-Session-Id=second&X-Plex-Session-Identifier=two"+tail;
 Record record,loaded;record.state=3;record.hasStrict=true;record.highConfidence=true;
 record.strict={16,16,0,0};record.hasSubtitle=true;record.subtitle={40,40,0,0};
 const XFILE::HttpIdentity strong{1000,0,"\"synthetic-private-validator\""};
 auto id=Identify(first,strong);assert(id.modified==0&&!id.validator.empty());
 assert(id.validator!=strong.tag&&id.validator.size()==64);
 const int beforeStore=XFILE::httpStats;assert(Store(id,record));
 assert(XFILE::httpStats==beforeStore+1&&XFILE::lastHttpRequest==first);
 privateRecords(); // Check while the strong-validator record still exists.
 auto next=Identify(second,strong);assert(Load(next,loaded));
 assert(loaded.highConfidence&&loaded.strict==record.strict&&loaded.subtitle==record.subtitle);
 // A percent-encoded spelling of an exact session name remains non-content;
 // leading, trailing and duplicated session fields likewise do not alter it.
 const auto encoded=endpoint+stable+"&%58-Plex-Session-Id=three&X-Plex-Session-Identifier=four"+tail;
 assert(Load(Identify(encoded,strong),loaded));
 const auto duplicates=endpoint+"?X-Plex-Session-Id=x&"+stable.substr(1)+
   "&X-Plex-Session-Id=y&X-Plex-Session-Identifier=z"+tail;
 assert(Load(Identify(duplicates,strong),loaded));
 for(const auto& changed: {
   std::string("http://fake.example:32400/library/parts/123/456/file.mkv")+stable+tail,
   std::string("https://other.example:32400/library/parts/123/456/file.mkv")+stable+tail,
   endpoint+"?X-Plex-Token=other&a=%2f&a=/&profile=original"+tail,
   endpoint+"?X-Plex-Token=synthetic-secret&a=/&a=%2f&profile=original"+tail,
   endpoint+"?X-Plex-Token=synthetic-secret&a=%2F&a=/&profile=original"+tail,
   endpoint+"?X-Plex-Token=synthetic-secret&a=/&profile=original"+tail,
   endpoint+stable+"&"+tail, // Preserve even a retained empty query segment.
   endpoint+stable+"&x-plex-session-id=case-sensitive"+tail,
   endpoint+stable+"&X-Plex-Session-Id-extra=content"+tail,
   endpoint+stable+"&X-Plex-Client-Identifier=different-client"+tail,
   endpoint+stable+"&X-Plex-Session=not-a-proven-session-field"+tail,
   endpoint+"?X-Plex-Token=synthetic-secret&a=%2f&a=/&profile=transcoded"+tail,
   endpoint+stable+"|User-Agent=other&seekable=1",
   endpoint+stable+tail+"#different-fragment",
   std::string("https://fake.example:32400/library/parts/124/456/file.mkv")+stable+tail,
   std::string("https://fake.example:32400/library/parts/123/457/file.mkv")+stable+tail,
   std::string("https://fake.example:32400/library/parts/123/456/other.mkv")+stable+tail})
  assert(!Load(Identify(changed,strong),loaded));
 for(int field=0;field<5;++field){auto changed=next;
  switch(field){case 0:++changed.streamIndex;break;case 1:++changed.codec;break;
   case 2:++changed.width;break;case 3:++changed.height;break;case 4:changed.nativeDV=false;break;}
  assert(!Load(changed,loaded));
 }
 // Normalization is deliberately unavailable outside an original Plex part.
 for(const auto& path:{std::string("https://fake.example/video/:/transcode/universal/start.m3u8"),
   std::string("https://fake.example/library/parts/abc/456/file.mkv"),
   std::string("https://fake.example/library/parts/123/abc/file.mkv"),
   std::string("https://fake.example/library/parts//456/file.mkv"),
   std::string("https://fake.example/library/parts/123/456"),
   std::string("https://fake.example/other/library/parts/123/456/file.mkv")}){
  auto original=Identify(path+"?X-Plex-Session-Id=a",strong);assert(Store(original,record));
  assert(!Load(Identify(path+"?X-Plex-Session-Id=b",strong),loaded));
 }
 const auto suffixFirst=endpoint+"|User-Agent=x?X-Plex-Session-Id=";
 assert(Store(Identify(suffixFirst+"a",strong),record));
 assert(!Load(Identify(suffixFirst+"b",strong),loaded));
 // Neither metadata nor validators may be borrowed from the endpoint/session.
 auto changed=Identify(second,{1001,0,strong.tag});assert(!Load(changed,loaded));
 changed=Identify(second,{1000,0,"\"replacement-validator\""});assert(!Load(changed,loaded));
 XFILE::httpResources[first]={1000,0,"\"replacement-validator\""};
 assert(!Store(id,record)); // The original identity is rechecked at commit time.
 Identify(first,strong);assert(Store(id,record));
 XFILE::httpResources.erase(first);assert(!Store(id,record));
 auto missing=id;assert(!ReadIdentity(missing));
 for(const auto& invalid: {std::string(),std::string("W/\"weak\""),std::string("unquoted"),
   std::string("\"unterminated"),std::string("\"inner\"quote\""),std::string(" \"spaces\""),
   std::string("\"bad space\""),std::string("\"bad\nline\""),std::string("\"bad\x7f\""),
   std::string("\"good\" trailing")}){
  XFILE::httpResources[first]={1000,0,invalid};auto candidate=id;
  assert(!ReadIdentity(candidate)&&candidate.validator.empty());
 }
 // A usable Last-Modified does not require an ETag. Strong validators need
 // known positive length; neither a weak tag nor a URL timestamp suffices.
 auto dated=Identify(first,{1000,1234,"W/\"weak\""});assert(dated.validator.empty());
 assert(Store(dated,record));assert(Load(Identify(second,{1000,1234,"W/\"weak\""}),loaded));
 assert(!Load(Identify(second,{1000,1235,""}),loaded));
 assert(!Load(Identify(second,{1001,1234,""}),loaded));
 assert(!Identify(first,{1000,0,"\"\""}).validator.empty()); // Empty opaque ETag is valid.
 for(const auto& metadata:{XFILE::HttpIdentity{0,1234,strong.tag},XFILE::HttpIdentity{-1,1234,strong.tag},
   XFILE::HttpIdentity{1000,0,""},XFILE::HttpIdentity{1000,-1,"W/\"weak\""}}){
  XFILE::httpResources[first]=metadata;auto candidate=id;assert(!ReadIdentity(candidate));
 }
 // A local filename resembling a Plex URL query remains byte-exact.
 const auto local=profile+"/local?X-Plex-Session-Id=";
 std::ofstream(local+"a")<<"synthetic local file";std::ofstream(local+"b")<<"synthetic local file";
 Identity localId;localId.path=local+"a";localId.streamIndex=0;localId.codec=173;localId.width=1920;localId.height=1080;
 assert(ReadIdentity(localId)&&Store(localId,record));localId.path=local+"b";
 assert(ReadIdentity(localId)&&!Load(localId,loaded));
 privateRecords();
 std::cout<<"PASS: HTTP Plex session reuse, exact retained identity and original requests, validated Last-Modified/strong ETag eligibility, metadata invalidation and private persistence\n";
}
'''

LIFECYCLE = r'''
#include "utils/ActiveAreaCache.h"
#include "utils/DetectorAdmission.h"
#include "filesystem/File.h"
#include "filesystem/CurlFile.h"
#include <atomic>
#include <cassert>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
enum {DV_DETECT_FAILED=0,DV_DETECT_SKIP_NON16X9=1,DV_DETECT_SKIP_IMAX=2,
 DV_DETECT_OK=3,DV_DETECT_SKIPPED=4,DV_DETECT_INACTIVE=5,DV_DETECT_RUNNING=6,DV_DETECT_TIMED_OUT=7,
 LOGDEBUG=0,LOGERROR=1,LOGINFO=2};
namespace PERFORMANCE_CORES {bool allowed=true;bool ApplyDetectionCurrentThread(){return allowed;}}
struct DetectResult {uint16_t top,bottom,left,right;bool highConfidence=false;};
struct DetectSource {
 std::string path;int streamIndex=0,codecHint=173,width=1920,height=1080;
 bool nativeDV=false,allowProbe=true,codedCrop=false,boundUsable=false;double displayAspect=0.0;
 std::shared_ptr<DETECTOR_ADMISSION::PlaybackPolicy> policy=std::make_shared<DETECTOR_ADMISSION::PlaybackPolicy>();
 std::atomic<bool> timedOut{false},uniqueStream{false};
 std::atomic<int> scanState{0};
 std::atomic<bool> pending{false};bool startupScan=false;
 std::shared_ptr<std::atomic<bool>> startupWait=std::make_shared<std::atomic<bool>>(false),startupAdmission=std::make_shared<std::atomic<bool>>(false);
 std::atomic<int> completedPositions{0};
 std::shared_ptr<std::atomic<bool>> superseded=std::make_shared<std::atomic<bool>>(false);
 std::shared_ptr<const DetectResult> result,subtitleResult,retained,retainedSubtitle;
};
bool workerCancelled=false;
struct CAMLNativeWorker {
 struct Run {bool Cancelled() const {return workerCancelled;}};
 void Stop() {} bool Closed() const {return false;}
 template<class F,class T>void Start(F f,T) {f(Run{});}
} s_detectWorker;
std::mutex s_detectSourceMutex;
std::shared_ptr<DetectSource> s_detectSource;
std::atomic<bool> s_detectThrottleActive{false},s_detectCacheStarved{false},s_detectStable{false};
std::atomic<int> s_detectState{0};
struct CSettings {enum {SETTING_SUBTITLES_CACHEACTIVEAREA,SETTING_SUBTITLES_DETECTACTIVEAREA};};
struct Settings {
 bool cache=false,enabled=true,dvEnabled=true;
 bool GetBool(int id)const{return id==CSettings::SETTING_SUBTITLES_CACHEACTIVEAREA?cache:enabled;}
} setting;
Settings* settings(){return &setting;}
bool aml_dv_detect_active_area_enabled(){return setting.dvEnabled;}
void detect_clear_injection(bool){}
void detect_clear_injection_locked(bool){}
void detect_binding_failed_locked(const std::shared_ptr<DetectSource>&){assert(false);}
bool detect_install_binding_locked(const std::shared_ptr<DetectSource>&){assert(false);return false;}
bool detect_schedule_binding(){assert(false);return false;}
std::shared_ptr<const void> detect_reserve_scanner(){static int token;return std::shared_ptr<const void>(&token,[](const void*){});}
struct CLog {template<class... T>static void Log(T...) {}};
int scans=0,scanMode=0;
void detect_publish_subtitle(const CAMLNativeWorker::Run&,const std::shared_ptr<DetectSource>& source,DetectResult result){
 source->subtitleResult=std::make_shared<DetectResult>(result);
}
void detect_publish(const CAMLNativeWorker::Run&,const std::shared_ptr<DetectSource>& source,uint16_t t,uint16_t b,uint16_t l,uint16_t r,bool highConfidence=false){
 source->result=std::make_shared<DetectResult>(DetectResult{t,b,l,r,highConfidence});s_detectState=3;
}
void DetectActiveAreaFromFile(const std::shared_ptr<DetectSource>& source,const CAMLNativeWorker::Run& run){
 assert(source->pending.load());
 ++scans;source->completedPositions.store(14);
 if(scanMode==2){s_detectState=DV_DETECT_FAILED;return;}
 detect_publish_subtitle(run,source,{20,20,0,0});s_detectState=2;
 if(scanMode==1||scanMode==3||scanMode==4)
  detect_publish(run,source,16,16,0,0,true);
 if(scanMode==3)workerCancelled=true;
 if(scanMode==4)XFILE::httpResources.at(source->path).tag="\"changed-during-scan\"";
}
'''

LIFECYCLE_TEST = r'''
int main(int argc,char** argv){
 assert(argc==2);const std::string root=argv[1];setenv("CACHE_TEST_PROFILE",root.c_str(),1);
 const auto media=root+"/startup-video.mkv";std::ofstream(media)<<"fake file identity only";
 s_detectSource=std::make_shared<DetectSource>();s_detectSource->path=media;
 // Explicitly disabled cache must not stat the media or open/create a record.
 const auto stats=XFILE::statCalls,opens=XFILE::openCalls,writes=XFILE::writeCalls;
 aml_dv_detect_active_area_start();assert(scans==1&&s_detectSource->subtitleResult);
 assert(XFILE::statCalls==stats&&XFILE::openCalls==opens&&XFILE::writeCalls==writes);
 // Enabled miss scans and saves; a fresh playback object then reuses blindly.
 setting.cache=true;aml_dv_detect_active_area_start();assert(scans==2);
 assert(XFILE::statCalls>stats&&XFILE::writeCalls>writes);
 s_detectSource=std::make_shared<DetectSource>();s_detectSource->path=media;
 aml_dv_detect_active_area_start();assert(scans==2&&s_detectState==2);
 assert(!s_detectSource->result&&s_detectSource->subtitleResult->top==20);
 // A strict saved result remains distinct from the adapted subtitle envelope.
 ACTIVE_AREA_CACHE::Identity id;id.path=media;id.streamIndex=0;id.codec=173;id.width=1920;id.height=1080;
 assert(ACTIVE_AREA_CACHE::ReadIdentity(id));ACTIVE_AREA_CACHE::Record record;
 record.state=3;record.hasStrict=true;record.strict={16,16,0,0};record.hasSubtitle=true;record.subtitle={40,40,0,0};
 assert(ACTIVE_AREA_CACHE::Store(id,record));aml_dv_detect_active_area_start();
 assert(scans==2&&s_detectState==3&&s_detectSource->result->top==16&&s_detectSource->subtitleResult->top==40);
 assert(!s_detectSource->result->highConfidence);
 record.highConfidence=true;assert(ACTIVE_AREA_CACHE::Store(id,record));
 aml_dv_detect_active_area_start();assert(scans==2&&s_detectSource->result->highConfidence);
 // Switching the selected stream or modifying the file forces a new scan.
 s_detectSource=std::make_shared<DetectSource>();s_detectSource->path=media;s_detectSource->streamIndex=1;
 aml_dv_detect_active_area_start();assert(scans==3);
 std::ofstream(media)<<"different file size";aml_dv_detect_active_area_start();assert(scans==4);
 // Off ignores even an existing record; disabled detection never accesses it.
 setting.cache=false;const auto offStats=XFILE::statCalls,offOpens=XFILE::openCalls,offWrites=XFILE::writeCalls;
 aml_dv_detect_active_area_start();assert(scans==5);
 assert(XFILE::statCalls==offStats&&XFILE::openCalls==offOpens&&XFILE::writeCalls==offWrites);
 setting.cache=true;setting.enabled=false;aml_dv_detect_active_area_start();assert(scans==5);
 assert(XFILE::statCalls==offStats&&XFILE::openCalls==offOpens&&XFILE::writeCalls==offWrites);
 // Failed CPU isolation must prevent decoding and must not create a cache record.
 setting.cache=false;setting.enabled=true;PERFORMANCE_CORES::allowed=false;
 aml_dv_detect_active_area_start();assert(scans==5&&s_detectState==DV_DETECT_SKIPPED);
 assert(!s_detectSource->result&&!s_detectSource->subtitleResult);
 assert(XFILE::statCalls==offStats&&XFILE::openCalls==offOpens&&XFILE::writeCalls==offWrites);
 assert(!s_detectSource->pending.load());
 // Connected regression: only native DV detection enabled, with the two
 // request-session fields changing exactly as in the reported Plex opens.
 PERFORMANCE_CORES::allowed=true;setting.enabled=false;setting.dvEnabled=true;
 setting.cache=true;scanMode=1;
 const std::string base="https://synthetic.example/library/parts/111/222/original.mkv";
 const auto first=base+"?X-Plex-Token=fake&X-Plex-Session-Id=one&X-Plex-Session-Identifier=first";
 const auto second=base+"?X-Plex-Token=fake&X-Plex-Session-Id=two&X-Plex-Session-Identifier=second";
 const XFILE::HttpIdentity metadata{1000,0,"\"native-dv-original\""};
 XFILE::httpResources[first]=metadata;XFILE::httpResources[second]=metadata;
 const auto open=[&](const std::string& path){
  s_detectSource=std::make_shared<DetectSource>();s_detectSource->path=path;
  s_detectSource->nativeDV=true;aml_dv_detect_active_area_start();
  assert(!s_detectSource->pending.load());
 };
 const int firstScan=scans;open(first);
 assert(scans==firstScan+1&&s_detectSource->completedPositions.load()==14);
 assert(s_detectState==DV_DETECT_OK&&s_detectSource->result->top==16&&s_detectSource->result->highConfidence);
 assert(s_detectSource->subtitleResult->top==20&&XFILE::lastHttpRequest==first);
 open(second);assert(scans==firstScan+1&&s_detectSource->completedPositions.load()==0);
 assert(s_detectState==DV_DETECT_OK&&s_detectSource->result->top==16&&s_detectSource->result->highConfidence);
 assert(s_detectSource->subtitleResult->top==20&&XFILE::lastHttpRequest==second);
 // A newer validator forces decoding even though the stable part is identical.
 XFILE::httpResources[second].tag="\"native-dv-replaced\"";
 open(second);assert(scans==firstScan+2&&s_detectSource->completedPositions.load()==14);
 // Cache OFF ignores an otherwise reusable HTTP entry and does no identity I/O.
 setting.cache=false;const auto httpBefore=XFILE::httpStats,opensBefore=XFILE::openCalls;
 const auto writesBefore=XFILE::writeCalls;
 open(second);assert(scans==firstScan+3);
 assert(XFILE::httpStats==httpBefore&&XFILE::openCalls==opensBefore&&XFILE::writeCalls==writesBefore);
 // Both detector enables OFF is a terminal no-I/O return, even for native DV.
 setting.cache=true;setting.dvEnabled=false;open(second);assert(scans==firstScan+3);
 assert(XFILE::httpStats==httpBefore&&XFILE::openCalls==opensBefore&&XFILE::writeCalls==writesBefore);
 setting.dvEnabled=true;
 // Failure, cancellation, source changes during decoding, and unavailable
 // identity must never seed a successful result for a subsequent open.
 for(int mode:{2,3,4,5,6}){
  const auto path=base+"?case="+std::to_string(mode);
  if(mode!=6)XFILE::httpResources[path]=mode==5?XFILE::HttpIdentity{1000,0,"W/\"weak\""}:metadata;
  const int initial=scans;scanMode=mode>=5?1:mode;open(path);
  assert(scans==initial+1);workerCancelled=false;
  ACTIVE_AREA_CACHE::Identity failed;failed.path=path;failed.streamIndex=0;failed.codec=173;
  failed.width=1920;failed.height=1080;failed.nativeDV=true;ACTIVE_AREA_CACHE::Record absent;
  const bool identified=ACTIVE_AREA_CACHE::ReadIdentity(failed);
  assert(mode>=5?!identified:identified&&!ACTIVE_AREA_CACHE::Load(failed,absent));
  scanMode=1;open(path);assert(scans==initial+2);
 }
 // Withheld-native/text-only acceptance remains text-only across reuse: it
 // must not be promoted into a high-confidence native L5 result by caching.
 const auto withheld=base+"?case=withheld";XFILE::httpResources[withheld]=metadata;
 scanMode=0;const int withheldScan=scans;open(withheld);
 assert(scans==withheldScan+1&&s_detectState==DV_DETECT_SKIP_IMAX);
 assert(!s_detectSource->result&&s_detectSource->subtitleResult->top==20);
 open(withheld);assert(scans==withheldScan+1&&s_detectSource->completedPositions.load()==0);
 assert(!s_detectSource->result&&s_detectState==DV_DETECT_SKIP_IMAX&&s_detectSource->subtitleResult->top==20);
 std::cout<<"PASS: production startup disabled-cache I/O bypass, persistent miss/save, blind hit without scanning, strict/subtitle separation, selected-stream/file invalidation\n";
 std::cout<<"PASS: native DV-only HTTP session reuse (14 positions then zero), independent enables, cancellation/failure/change rejection and withheld-native preservation\n";
}
'''

UPGRADE_TEST = r'''
#include "utils/ActiveAreaCache.h"
#include "filesystem/File.h"
#include "filesystem/Directory.h"
#include <cassert>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
using namespace ACTIVE_AREA_CACHE;
void Write(const std::string& p,const std::string& value){std::ofstream(p,std::ios::binary)<<value;}
std::string Read(const std::string& p){std::ifstream in(p,std::ios::binary);return std::string((std::istreambuf_iterator<char>(in)),{});}
int Version(const std::string& p){std::ifstream in(p);int version=-1;in>>version;return version;}
int main(int argc,char** argv){
 assert(argc==2);const auto profile=std::string(argv[1])+"/upgrade-profile";
 std::filesystem::create_directories(profile);setenv("CACHE_TEST_PROFILE",profile.c_str(),1);
 const auto media=profile+"/identity-only.mkv";Write(media,"fake file identity only");
 Identity id;id.path=media;id.streamIndex=0;id.codec=173;id.width=1920;id.height=1080;assert(ReadIdentity(id));
 Record record,loaded;record.state=3;record.hasStrict=true;record.strict={20,20,0,0};
 assert(Store(id,record));const auto cache=profile+"/active-area-cache/";
 const auto marker=cache+"version";const int current=Version(marker);assert(current>0);
 std::string kept;for(const auto& entry:std::filesystem::directory_iterator(cache))
  if(entry.path().extension()==".dat")kept=entry.path().string();
 assert(!kept.empty());const auto keptBytes=Read(kept);
 const auto old=cache+std::string(64,'a')+".dat";
 const auto corrupt=cache+std::string(64,'b')+".dat";
 const auto denied=cache+std::string(64,'c')+".dat";
 const auto folder=cache+std::string(64,'d')+".dat";
 const auto temporary=cache+std::string(64,'e')+".dat.tmp";
 const auto unrelated=cache+"notes.dat";
 const auto outside=profile+"/"+std::string(64,'f')+".dat";
 const auto obsolete=std::to_string(current-1)+" old geometry";
 Write(old,obsolete);Write(corrupt,"");Write(denied,obsolete);
 std::filesystem::create_directory(folder);Write(temporary,obsolete);Write(unrelated,obsolete);Write(outside,obsolete);
 Write(marker,std::to_string(current-1));XFILE::deniedDelete=denied;
 assert(Load(id,loaded)&&loaded.strict==record.strict);
 assert(!std::filesystem::exists(old)&&!std::filesystem::exists(corrupt));
 assert(std::filesystem::exists(denied)&&Version(marker)==current-1);
 assert(Read(kept)==keptBytes&&std::filesystem::is_directory(folder));
 assert(Read(unrelated)==obsolete&&Read(temporary)==obsolete&&Read(outside)==obsolete);
 XFILE::deniedDelete.clear();const int failedReads=XFILE::enumerations;
 assert(Load(id,loaded));assert(!std::filesystem::exists(denied));
 assert(Version(marker)==current&&XFILE::enumerations==failedReads+1);
 const int completedReads=XFILE::enumerations;
 assert(Load(id,loaded)&&XFILE::enumerations==completedReads); // normal hits do not rescan directory
 // Directory errors and a failed atomic marker replacement leave a retry.
 Write(marker,std::to_string(current-1));Write(old,obsolete);XFILE::denyEnumeration=true;
 assert(Load(id,loaded));assert(std::filesystem::exists(old)&&Version(marker)==current-1);
 XFILE::denyEnumeration=false;XFILE::denyMarkerRename=true;
 assert(Load(id,loaded));assert(!std::filesystem::exists(old)&&Version(marker)==current-1);
 assert(!std::filesystem::exists(marker+".tmp"));
 XFILE::denyMarkerRename=false;assert(Load(id,loaded));assert(Version(marker)==current);
 // Existing installs have no marker; the first updated use must clean them.
 std::filesystem::remove(marker);Write(old,obsolete);
 assert(Load(id,loaded)&&!std::filesystem::exists(old)&&Version(marker)==current);
 // A profile switch in this same process must not inherit another profile's marker.
 const auto second=std::string(argv[1])+"/second-profile";
 const auto secondCache=second+"/active-area-cache/";std::filesystem::create_directories(secondCache);
 const auto secondOld=secondCache+std::string(64,'a')+".dat";Write(secondOld,obsolete);
 Write(secondCache+"version",std::to_string(current-1));setenv("CACHE_TEST_PROFILE",second.c_str(),1);
 assert(!Load(id,loaded));assert(!std::filesystem::exists(secondOld)&&Version(secondCache+"version")==current);
 setenv("CACHE_TEST_PROFILE",profile.c_str(),1);assert(Load(id,loaded));
 assert(Read(kept)==keptBytes&&Read(unrelated)==obsolete&&Read(outside)==obsolete);
 // Real special:// directory enumeration returns aliases. Switch active
 // profiles after reading A's obsolete header but before deleting the record.
 // A single frozen URI must keep deletion, marker and cache read in A.
 Write(marker,std::to_string(current-1));Write(old,obsolete);
 const auto secondSame=secondCache+std::string(64,'a')+".dat";
 Write(secondSame,std::to_string(current)+" current geometry");
 const auto untouchedMarker=Read(secondCache+"version");
 XFILE::switchOnReadPath=old;XFILE::nextProfile=second;
 assert(Load(id,loaded));assert(std::string(std::getenv("CACHE_TEST_PROFILE"))==second);
 assert(!std::filesystem::exists(old)&&Read(secondSame)==std::to_string(current)+" current geometry");
 assert(Version(marker)==current&&Read(secondCache+"version")==untouchedMarker);
 assert(Read(kept)==keptBytes&&Read(unrelated)==obsolete);
 XFILE::switchOnReadPath.clear();

 std::cout<<"PASS: policy upgrade cleanup, current data retained, owned paths only, failures retry, marker migration and profile switching\n";
}
'''

FIND_TEST = r'''
#include "utils/ActiveAreaCache.h"
#include "filesystem/File.h"
#include "filesystem/CurlFile.h"
#include <cassert>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
using namespace ACTIVE_AREA_CACHE;
int main(int argc,char** argv){
 assert(argc==2);const auto profile=std::string(argv[1])+"/find-profile";
 std::filesystem::create_directories(profile);setenv("CACHE_TEST_PROFILE",profile.c_str(),1);
 const auto media=profile+"/selected.mkv";std::ofstream(media)<<"synthetic identity only";
 Identity id;id.path=media;id.streamIndex=2;id.codec=173;id.width=1920;id.height=1080;assert(ReadIdentity(id));
 Record saved,loaded;saved.hasStrict=true;saved.hasSubtitle=true;saved.state=3;saved.highConfidence=true;
 saved.strict={0,0,0,0};saved.subtitle={30,30,0,0};assert(Store(id,saved));Identity found;
 assert(!Find(media,-1,found,loaded)); // unspecified stream needs proven uniqueness
 assert(!Find(media,0,found,loaded));
 assert(Find(media,2,found,loaded)&&found==id&&loaded.hasStrict&&loaded.strict[0]==0&&loaded.highConfidence);
 saved.uniqueStream=true;assert(Store(id,saved));assert(Find(media,-1,found,loaded)&&loaded.uniqueStream);
 // Prefer metadata-independent sampling over a native-policy cache record.
 auto native=id;native.nativeDV=true;saved.strict={90,90,0,0};assert(Store(native,saved));
 assert(Find(media,-1,found,loaded)&&!found.nativeDV&&loaded.strict[0]==0&&loaded.subtitle[0]==30);
 // Text-only evidence stays text-only in discovery as in ordinary reuse.
 saved.hasStrict=false;saved.highConfidence=false;saved.state=2;assert(Store(id,saved));
 assert(Find(media,-1,found,loaded)&&!loaded.hasStrict&&loaded.hasSubtitle&&loaded.state==2);
 // Discovery revalidates original content; geometry is unavailable until probing.
 Identity frozen;frozen.path=media;assert(ReadContentIdentity(frozen)&&frozen.streamIndex==-1);
 std::ofstream(media)<<"changed size during decoder probing";
 frozen.streamIndex=2;frozen.codec=173;frozen.width=1920;frozen.height=1080;
 assert(!Store(frozen,saved)&&!Find(media,2,found,loaded));
 // Stable Plex identity retains validators and the exact current request URL.
 const std::string base="https://test.invalid/library/parts/12/34/original.mkv?X-Plex-Token=fake";
 const auto first=base+"&X-Plex-Session-Id=first",second=base+"&X-Plex-Session-Id=second";
 XFILE::httpResources[first]={1000,0,"\"strong\""};XFILE::httpResources[second]=XFILE::httpResources[first];
 id.path=first;assert(ReadIdentity(id)&&Store(id,saved));
 assert(Find(second,-1,found,loaded)&&found.path==second&&XFILE::lastHttpRequest==second);
 XFILE::httpResources[second].tag="\"replacement\"";assert(!Find(second,-1,found,loaded));
 XFILE::httpResources[second].tag="W/\"weak\"";assert(!Find(second,-1,found,loaded));
 std::cout<<"PASS: actual cache discovery, selected/unique streams, valid zero, strict/text separation, native preference, frozen content and stable HTTP validation\n";
}
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cxx', default='g++')
    parser.add_argument('--negative-controls',action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='active-area-cache-test-') as directory:
        work = pathlib.Path(directory)
        counted_file = FILE_STUB.replace('namespace XFILE {', 'namespace XFILE {\ninline int statCalls=0,openCalls=0,writeCalls=0;')
        counted_file = counted_file.replace('return ::stat(Path(p)', '++statCalls;return ::stat(Path(p)')
        counted_file = counted_file.replace('path=Path(p);file.open(path,std::ios::in', '++openCalls;path=Path(p);file.open(path,std::ios::in')
        counted_file = counted_file.replace('path=Path(p);file.open(path,std::ios::out', '++writeCalls;path=Path(p);file.open(path,std::ios::out')
        file_items = r'''#pragma once
#include <string>
#include <memory>
#include <vector>
struct CFileItem{std::string path;bool m_bIsFolder=false;const std::string& GetPath() const{return path;}};
struct CFileItemList{std::vector<std::shared_ptr<CFileItem>> items;const auto& GetList()const{return items;}};
'''
        uri = r'''#pragma once
#include <string>
struct URIUtils{static std::string GetFileName(const std::string& path){return path.substr(path.find_last_of("/\\")+1);}};
'''
        special = r'''#pragma once
#include "File.h"
struct CSpecialProtocol{static std::string TranslatePath(const std::string& path){return XFILE::Path(path);}};
'''
        for name, text in [('URL.h',URL_STUB),('filesystem/CurlFile.h',HTTP_STUB),('filesystem/SpecialProtocol.h',special),('FileItem.h',file_items),('utils/URIUtils.h',uri),('filesystem/File.h', counted_file), ('filesystem/Directory.h', DIRECTORY_STUB), ('utils/Digest.h', DIGEST_STUB)]:
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (work / 'test.cpp').write_text(TEST)
        argv = [args.cxx, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-I' + str(work), '-I' + str(ROOT / 'xbmc'), str(ROOT / 'xbmc/utils/ActiveAreaCache.cpp'), str(work / 'test.cpp'), '-lcrypto', '-o', str(work / 'test')]
        subprocess.run(argv, check=True)
        # LeakSanitizer cannot inspect threads under the tool's ptrace sandbox.
        subprocess.run([str(work / 'test'), str(work)], check=True,
                       env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, timeout=30)
        if args.negative_controls:
            # Reintroduce the old last-entry target without depending on the
            # filesystem's directory order. Both distinct identities must fail
            # to resolve independently when the selector ignores its match.
            old=' return match;'
            assert TEST.count(old)==1
            broken=work/'wrong-target.cpp'
            broken.write_text(TEST.replace(old,' return entries.back().string();',1))
            broken_argv=[str(broken) if value==str(work/'test.cpp') else
                         str(work/'wrong-target') if value==str(work/'test') else value for value in argv]
            subprocess.run(broken_argv,check=True)
            target=work/'target-negative';target.mkdir()
            result=subprocess.run([str(work/'wrong-target'),str(target)],text=True,capture_output=True,
                                  env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'},timeout=30)
            assert result.returncode!=0 and 'Assertion' in result.stderr,(result.stdout,result.stderr)
            assert 'AddressSanitizer' not in result.stderr and 'runtime error:' not in result.stderr,result.stderr
            print('REJECTED: corruption target uses last directory entry')
        (work / 'http.cpp').write_text(HTTP_TEST)
        http_argv = [str(work/'http.cpp') if value==str(work/'test.cpp') else
                     str(work/'http') if value==str(work/'test') else value for value in argv]
        subprocess.run(http_argv,check=True)
        subprocess.run([str(work/'http'),str(work)],check=True,
                       env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'},timeout=30)
        (work / 'upgrade.cpp').write_text(UPGRADE_TEST)
        upgrade_argv = [str(work/'upgrade.cpp') if value==str(work/'test.cpp') else str(work/'upgrade') if value==str(work/'test') else value for value in argv]
        subprocess.run(upgrade_argv,check=True)
        subprocess.run([str(work/'upgrade'),str(work)],check=True,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'},timeout=30)
        (work / 'find.cpp').write_text(FIND_TEST)
        find_argv = [str(work/'find.cpp') if value==str(work/'test.cpp') else str(work/'find') if value==str(work/'test') else value for value in argv]
        subprocess.run(find_argv,check=True)
        subprocess.run([str(work/'find'),str(work)],check=True,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'},timeout=30)
        if args.negative_controls:
            actual=(ROOT/'xbmc/utils/ActiveAreaCache.cpp').read_text()
            for index,(label,old,new) in enumerate([
                ('unspecified stream ignores ambiguity', ': !candidate.uniqueStream)', ': false)'),
                ('discovery ignores changed content', 'if (!ReadIdentity(current) || !(current == stored))', 'if (!ReadIdentity(current))'),
            ]):
                assert old in actual,label
                broken=work/'broken-find-cache.cpp';broken.write_text(actual.replace(old,new,1))
                broken_argv=[str(broken) if value==str(ROOT/'xbmc/utils/ActiveAreaCache.cpp') else value for value in find_argv]
                broken_argv[1:1]=['-iquote',str(ROOT/'xbmc/utils')]
                subprocess.run(broken_argv,check=True)
                target=work/('find-negative-'+str(index));target.mkdir()
                result=subprocess.run([str(work/'find'),str(target)],text=True,capture_output=True,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'},timeout=30)
                assert result.returncode!=0 and 'Assertion' in result.stderr,(label,result.stdout,result.stderr)
                assert 'AddressSanitizer' not in result.stderr and 'runtime error:' not in result.stderr,result.stderr
                print('REJECTED:',label)

            for index,(label,old,new) in enumerate([
                ('HTTP session identity remains volatile',
                 'StablePath(identity.path) +', 'StablePath(identity.path) + identity.path +'),
                ('HTTP retained query fields discarded',
                 'if (name != "X-Plex-Session-Id" && name != "X-Plex-Session-Identifier")',
                 'if (false && name != "X-Plex-Session-Id" && name != "X-Plex-Session-Identifier")'),
                ('HTTP malformed ETag accepted',
                 'status == 0 && StrongETag(tag)', 'status == 0 && (StrongETag(tag) || true)'),
                ('HTTP changed validator ignored',
                 'validator == other.validator &&', '(validator == other.validator || true) &&'),
            ]):
                assert old in actual,label
                broken=work/'broken-http-cache.cpp';broken.write_text(actual.replace(old,new,1))
                broken_argv=[str(broken) if value==str(ROOT/'xbmc/utils/ActiveAreaCache.cpp') else value for value in http_argv]
                broken_argv[1:1]=['-iquote',str(ROOT/'xbmc/utils')]
                subprocess.run(broken_argv,check=True)
                target=work/('http-negative-'+str(index));target.mkdir()
                result=subprocess.run([str(work/'http'),str(target)],text=True,capture_output=True,
                                      env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'},timeout=30)
                assert result.returncode!=0 and 'Assertion' in result.stderr,(label,result.stdout,result.stderr)
                assert 'AddressSanitizer' not in result.stderr and 'runtime error:' not in result.stderr,result.stderr
                print('REJECTED:',label)
            owned_start=actual.index('    if (item->m_bIsFolder')
            owned_end=actual.index('      continue;',owned_start)+len('      continue;')
            ownership=actual[owned_start:owned_end]
            for label,old,new in [
                ('profile URI not frozen','CSpecialProtocol::TranslatePath(CACHE_DIRECTORY)','std::string(CACHE_DIRECTORY)'),
                ('cleanup omitted','if (ReadVersion(marker, version) && version == CACHE_VERSION)','if (true || (ReadVersion(marker, version) && version == CACHE_VERSION))'),
                ('current entries deleted','(!ReadVersion(item->GetPath(), version) || version != CACHE_VERSION)','(!ReadVersion(item->GetPath(), version) || version == CACHE_VERSION)'),
                ('failed cleanup stamped complete','if (!complete)','if (!complete && false)'),
                ('unrelated files removed',ownership,'    if (item->m_bIsFolder || name.empty() || (legacy && current))\n      continue;'),
                ('directory rescanned on every hit','if (ReadVersion(marker, version) && version == CACHE_VERSION)','if (false && ReadVersion(marker, version) && version == CACHE_VERSION)'),
            ]:
                assert old in actual,label
                broken=work/'broken-cache.cpp';broken.write_text(actual.replace(old,new) if label=='profile URI not frozen' else actual.replace(old,new,1))
                broken_argv=[str(broken) if value==str(ROOT/'xbmc/utils/ActiveAreaCache.cpp') else value for value in upgrade_argv]
                broken_argv[1:1]=['-iquote',str(ROOT/'xbmc/utils')]
                subprocess.run(broken_argv,check=True)
                target=work/('negative-'+str(len(label)));target.mkdir(exist_ok=True)
                result=subprocess.run([str(work/'upgrade'),str(target)],text=True,capture_output=True,env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'},timeout=30)
                assert result.returncode!=0 and 'Assertion' in result.stderr,(label,result.stdout,result.stderr)
                assert 'AddressSanitizer' not in result.stderr and 'runtime error:' not in result.stderr,result.stderr
                print('REJECTED:',label)
        extract = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
        startup = extract((ROOT / 'xbmc/utils/AMLUtils.cpp').read_text(), 'void aml_dv_detect_active_area_start()')
        (work / 'lifecycle.cpp').write_text(LIFECYCLE + startup + LIFECYCLE_TEST)
        lifecycle_argv = [str(work / 'lifecycle.cpp') if value == str(work / 'test.cpp') else
                          str(work / 'lifecycle') if value == str(work / 'test') else value for value in argv]
        subprocess.run(lifecycle_argv, check=True)
        subprocess.run([str(work / 'lifecycle'), str(work)], check=True,
                       env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, timeout=30)
        if args.negative_controls:
            for index,(label,broken_startup) in enumerate([
                ('native DV incorrectly gated by subtitle enable',startup.replace(
                    '(source->nativeDV && aml_dv_detect_active_area_enabled())',
                    '(false && source->nativeDV && aml_dv_detect_active_area_enabled())')),
                ('cancelled worker result persisted',startup.replace('!run.Cancelled()', 'true')),
            ]):
                assert broken_startup!=startup,label
                (work/'lifecycle.cpp').write_text(LIFECYCLE+broken_startup+LIFECYCLE_TEST)
                subprocess.run(lifecycle_argv,check=True)
                target=work/('worker-negative-'+str(index));target.mkdir()
                result=subprocess.run([str(work/'lifecycle'),str(target)],text=True,capture_output=True,
                                      env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'},timeout=30)
                assert result.returncode!=0 and 'Assertion' in result.stderr,(label,result.stdout,result.stderr)
                assert 'AddressSanitizer' not in result.stderr and 'runtime error:' not in result.stderr,result.stderr
                print('REJECTED:',label)

if __name__ == '__main__':
    main()
