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
inline std::string Path(std::string p) {
 const std::string prefix="special://profile/";
 if(p.rfind(prefix,0)==0) return std::string(std::getenv("CACHE_TEST_PROFILE"))+"/"+p.substr(prefix.size());
 return p;
}
class CFile {
 std::fstream file; std::string path;
public:
 static int Stat(const std::string& p,struct stat* s) {return ::stat(Path(p).c_str(),s);}
 static bool Rename(const std::string& a,const std::string& b) {return ::rename(Path(a).c_str(),Path(b).c_str())==0;}
 static bool Delete(const std::string& p) {return ::unlink(Path(p).c_str())==0;}
 bool Open(const std::string& p) {path=Path(p);file.open(path,std::ios::in|std::ios::binary);return file.is_open();}
 bool OpenForWrite(const std::string& p,bool) {path=Path(p);file.open(path,std::ios::out|std::ios::binary|std::ios::trunc);return file.is_open();}
 int64_t GetLength() {struct stat s{};return ::stat(path.c_str(),&s)==0?s.st_size:-1;}
 ssize_t Read(void* p,size_t n) {file.read(static_cast<char*>(p),n);return file.gcount();}
 ssize_t Write(const void* p,size_t n) {file.write(static_cast<const char*>(p),n);return file?static_cast<ssize_t>(n):-1;}
 void Close() {file.close();}
};
}
'''
DIRECTORY_STUB = r'''
#pragma once
#include "File.h"
#include <filesystem>
namespace XFILE {class CDirectory {public: static bool Create(const std::string& p) {
 std::error_code e; std::filesystem::create_directories(Path(p),e);return !e;}};}
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
#include <cassert>
#include <filesystem>
#include <fstream>
#include <cstdlib>
#include <iostream>
#include <utime.h>
using namespace ACTIVE_AREA_CACHE;
void Write(const std::string& p,const std::string& s) {std::ofstream(p,std::ios::binary)<<s;}
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
 for(auto const& p:std::filesystem::directory_iterator(directory)) {
  assert(p.path().extension()==".dat");entry=p.path().string();
  std::ifstream in(entry);std::string data((std::istreambuf_iterator<char>(in)),{});
  assert(data.find(media)==std::string::npos); // source paths are not persisted
 }
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

LIFECYCLE = r'''
#include "utils/ActiveAreaCache.h"
#include "filesystem/File.h"
#include <atomic>
#include <cassert>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
enum {DV_DETECT_FAILED=0,DV_DETECT_SKIP_NON16X9=1,DV_DETECT_SKIP_IMAX=2,
 DV_DETECT_OK=3,DV_DETECT_SKIPPED=4,DV_DETECT_INACTIVE=5,DV_DETECT_RUNNING=6,
 LOGDEBUG=0,LOGERROR=1};
namespace PERFORMANCE_CORES {bool allowed=true;bool ApplyDetectionCurrentThread(){return allowed;}}
struct DetectResult {uint16_t top,bottom,left,right;bool highConfidence=false;};
struct DetectSource {
 std::string path;int streamIndex=0,codecHint=173,width=1920,height=1080;
 bool nativeDV=false,allowProbe=true;
 std::shared_ptr<std::atomic<bool>> superseded=std::make_shared<std::atomic<bool>>(false);
 std::shared_ptr<const DetectResult> result,subtitleResult,retained,retainedSubtitle;
};
struct CAMLNativeWorker {
 struct Run {bool Cancelled() const {return false;}};
 void Stop() {} bool Closed() const {return false;}
 template<class F,class T>void Start(F f,T) {f(Run{});}
} s_detectWorker;
std::mutex s_detectSourceMutex;
std::shared_ptr<DetectSource> s_detectSource;
std::atomic<bool> s_detectThrottleActive{false},s_detectCacheStarved{false},s_detectStable{false};
std::atomic<int> s_detectState{0};
struct CSettings {enum {SETTING_SUBTITLES_CACHEACTIVEAREA,SETTING_SUBTITLES_DETECTACTIVEAREA};};
struct Settings {
 bool cache=false,enabled=true;
 bool GetBool(int id)const{return id==CSettings::SETTING_SUBTITLES_CACHEACTIVEAREA?cache:enabled;}
} setting;
Settings* settings(){return &setting;}
bool aml_dv_detect_active_area_enabled(){return setting.enabled;}
void detect_clear_injection(bool){}
struct CLog {template<class... T>static void Log(T...) {}};
int scans=0;
void detect_publish_subtitle(const CAMLNativeWorker::Run&,const std::shared_ptr<DetectSource>& source,DetectResult result){
 source->subtitleResult=std::make_shared<DetectResult>(result);
}
void detect_publish(const CAMLNativeWorker::Run&,const std::shared_ptr<DetectSource>& source,uint16_t t,uint16_t b,uint16_t l,uint16_t r,bool highConfidence=false){
 source->result=std::make_shared<DetectResult>(DetectResult{t,b,l,r,highConfidence});s_detectState=3;
}
void DetectActiveAreaFromFile(const std::shared_ptr<DetectSource>& source,const CAMLNativeWorker::Run& run){
 ++scans;detect_publish_subtitle(run,source,{20,20,0,0});s_detectState=2;
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
 std::cout<<"PASS: production startup disabled-cache I/O bypass, persistent miss/save, blind hit without scanning, strict/subtitle separation, selected-stream/file invalidation\n";
}
'''

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--cxx', default='g++')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='active-area-cache-test-') as directory:
        work = pathlib.Path(directory)
        counted_file = FILE_STUB.replace('namespace XFILE {', 'namespace XFILE {\ninline int statCalls=0,openCalls=0,writeCalls=0;')
        counted_file = counted_file.replace('return ::stat(Path(p)', '++statCalls;return ::stat(Path(p)')
        counted_file = counted_file.replace('path=Path(p);file.open(path,std::ios::in', '++openCalls;path=Path(p);file.open(path,std::ios::in')
        counted_file = counted_file.replace('path=Path(p);file.open(path,std::ios::out', '++writeCalls;path=Path(p);file.open(path,std::ios::out')
        for name, text in [('filesystem/File.h', counted_file), ('filesystem/Directory.h', DIRECTORY_STUB), ('utils/Digest.h', DIGEST_STUB)]:
            path = work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        (work / 'test.cpp').write_text(TEST)
        argv = [args.cxx, '-std=c++17', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-I' + str(work), '-I' + str(ROOT / 'xbmc'), str(ROOT / 'xbmc/utils/ActiveAreaCache.cpp'), str(work / 'test.cpp'), '-lcrypto', '-o', str(work / 'test')]
        subprocess.run(argv, check=True)
        # LeakSanitizer cannot inspect threads under the tool's ptrace sandbox.
        subprocess.run([str(work / 'test'), str(work)], check=True,
                       env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, timeout=30)
        extract = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']
        startup = extract((ROOT / 'xbmc/utils/AMLUtils.cpp').read_text(), 'void aml_dv_detect_active_area_start()')
        (work / 'lifecycle.cpp').write_text(LIFECYCLE + startup + LIFECYCLE_TEST)
        lifecycle_argv = [str(work / 'lifecycle.cpp') if value == str(work / 'test.cpp') else
                          str(work / 'lifecycle') if value == str(work / 'test') else value for value in argv]
        subprocess.run(lifecycle_argv, check=True)
        subprocess.run([str(work / 'lifecycle'), str(work)], check=True,
                       env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'}, timeout=30)

if __name__ == '__main__':
    main()
