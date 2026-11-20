#!/usr/bin/env python3
"""Execute the actual CTextureCache::CacheImage with interruptible callers.

CacheImage, OnCachingComplete and SetCachedObserver run unchanged against a
fixture texture cache (database, cached-path lookup) and a fixture cache job
that blocks until released or interrupted. Requests for one image run on real
threads. Covers: the own fill gets the caller's read interrupt; a waiting caller
stops on its interrupt; a caller that waited for a request stopped on its
interrupt fetches the image itself, while a request that failed on its own is
not repeated; the record of a stopped request ends with its waiters; every
cached image is reported, outside the cache's locks. No database, decoder or
network.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('cache_extract', ROOT / 'tools/test-aml-texture-upload-context.py')
EXTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACT)
SOURCE = 'xbmc/TextureCache.cpp'
SIGNATURES = ('bool CTextureCache::StartCacheImage(', 'void CTextureCache::EndFill(',
              'std::string CTextureCache::CacheImage(const std::string& image,',
              'void CTextureCache::SetCachedObserver(', 'void CTextureCache::OnCachingComplete(')

PRELUDE = r'''
#include <atomic>
#include <chrono>
#include <cstdint>
#include <condition_variable>
#include <cstdio>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <stdexcept>
#include <string>
#include <thread>
using namespace std::chrono_literals;
static unsigned checks=0,cases=0;
static void Check(bool ok,const char*text){++checks;if(!ok)throw std::runtime_error(text);}
constexpr int LOGDEBUG=0;
struct CLog {template<class...T>static void Log(int,const char*,T&&...){}};
static thread_local int heldLocks=0;
struct CCriticalSection {std::recursive_mutex m;void lock(){m.lock();++heldLocks;}void unlock(){--heldLocks;m.unlock();}};
struct CEvent {
 std::mutex m;std::condition_variable cv;bool set=false;
 static std::atomic<bool> hold;
 bool Wait(std::chrono::milliseconds d){if(hold){while(hold)std::this_thread::sleep_for(1ms);return false;}
  std::unique_lock<std::mutex>l(m);const bool r=cv.wait_for(l,d,[&]{return set;});set=false;return r;}
 void Set(){{std::lock_guard<std::mutex>l(m);set=true;}cv.notify_all();}
};
std::atomic<bool> CEvent::hold{false};
struct CTexture {static std::unique_ptr<CTexture> LoadFromFile(const std::string&,int,int){return std::make_unique<CTexture>();}};
struct CTextureUtils {static std::string UnwrapImageURL(const std::string&s){return s;}};
struct CTextureDetails {std::string file;bool hashRevalidated=false,updateable=false;};
// The network: a fill reads until released (then succeeds unless fillOk is false) or interrupted.
struct Net {std::atomic<int> fills{0},started{0},interruptible{0};std::atomic<bool> release{false},fillOk{true};};
static Net* net=nullptr;
struct CTextureCacheJob {
 std::string m_url;CTextureDetails m_details;std::function<bool()> m_readInterrupt;uint64_t m_fill=0;
 explicit CTextureCacheJob(const std::string&u):m_url(u){}
 bool DoWork();bool ShouldCancel(unsigned,unsigned)const{return false;}
 bool CacheTexture(std::unique_ptr<CTexture>*t=nullptr){
  ++net->fills;if(m_readInterrupt)++net->interruptible;++net->started;
  const auto end=std::chrono::steady_clock::now()+3s;
  while(!net->release){if(m_readInterrupt&&m_readInterrupt())return false;if(std::chrono::steady_clock::now()>end)return false;std::this_thread::sleep_for(1ms);}
  if(!net->fillOk)return false;
  m_details.file="cached/"+m_url;if(t)*t=std::make_unique<CTexture>();return true;}
};
class CTextureCache {
public:
 CCriticalSection m_processingSection;std::set<std::string> m_processinglist;CEvent m_completeEvent;
 std::map<std::string,uint64_t> m_fillIDs;uint64_t m_lastFillID=0;std::map<uint64_t,unsigned int> m_waiting;std::set<uint64_t> m_interrupted;
 CCriticalSection m_observerSection;std::function<void(const std::string&)> m_cachedObserver;
 std::mutex dbLock;std::set<std::string> db;
 std::string CacheImage(const std::string& image,std::unique_ptr<CTexture>* texture=nullptr,CTextureDetails* details=nullptr,const std::function<bool()>& interrupt={});
 void SetCachedObserver(std::function<void(const std::string& url)> observer);
 bool StartCacheImage(const std::string& image,uint64_t* fill=nullptr);
 void EndFill(const std::string& url,uint64_t fill);
 void OnCachingComplete(bool success,CTextureCacheJob* job,bool interrupted=false);
 std::function<void()> afterMiss;
 void AddCachedTexture(const std::string&url,const CTextureDetails&){std::lock_guard<std::mutex>l(dbLock);db.insert(url);}
 void SetCachedTextureValid(const std::string&url,bool){std::lock_guard<std::mutex>l(dbLock);db.insert(url);}
 std::string GetCachedImage(const std::string&url,CTextureDetails&,bool){
  std::string r;{std::lock_guard<std::mutex>l(dbLock);r=db.count(url)?"cached/"+url:"";}
  if(r.empty()&&afterMiss){auto f=std::move(afterMiss);afterMiss=nullptr;f();}return r;}
 static std::string GetCachedPath(const std::string&f){return f;}
 std::string CheckCachedImage(const std::string&url,bool&recache){recache=false;std::lock_guard<std::mutex>l(dbLock);return db.count(url)?"cached/"+url:"";}
};
struct CServiceBroker {static std::shared_ptr<CTextureCache> cache;static std::shared_ptr<CTextureCache> GetTextureCache(){return cache;}};
std::shared_ptr<CTextureCache> CServiceBroker::cache;
'''

TESTS = r'''
using Clock=std::chrono::steady_clock;
template<class P> static bool Within(P p,std::chrono::milliseconds d){const auto end=Clock::now()+d;while(!p()){if(Clock::now()>end)return false;std::this_thread::sleep_for(1ms);}return true;}
static void Wait(bool ok,const char*text){Check(ok,text);}
// A request for "u" on its own thread.
struct Request {
 std::thread t;std::string result;std::atomic<bool> done{false},stop{false};
 Request(CTextureCache&c,bool interruptible){t=std::thread([this,&c,interruptible]{
  std::unique_ptr<CTexture> tex;std::function<bool()> i;if(interruptible)i=[this]{return stop.load();};
  result=c.CacheImage("u",&tex,nullptr,i);done=true;});}
 ~Request(){net->release=true;stop=true;if(t.joinable())t.join();}
 void Join(){t.join();}
};
static void OwnFill(){Net n;net=&n;n.release=true;CTextureCache c;std::atomic<int> reported{0},underLock{0};
 c.SetCachedObserver([&](const std::string&url){if(url=="u")++reported;if(heldLocks)++underLock;});
 Request r(c,true);r.Join();
 Check(r.result=="cached/u"&&n.fills==1,"own fill caches the image");
 Check(n.interruptible==1,"own fill reads with the caller's interrupt");
 Check(reported==1,"every cached image is reported");Check(underLock==0,"the report comes without the cache's locks");++cases;}
static void WaiterStops(){Net n;net=&n;CTextureCache c;Request owner(c,false);Wait(Within([&]{return n.started.load()==1;},2s),"fixture: owner did not start");
 Request waiter(c,true);Wait(Within([&]{std::lock_guard<CCriticalSection>l(c.m_processingSection);return !c.m_waiting.empty();},2s),"fixture: waiter did not wait");
 waiter.stop=true;Check(Within([&]{return waiter.done.load();},1s),"a waiting caller stops on its interrupt");
 Check(waiter.result.empty()&&n.fills==1,"an interrupted waiter returns nothing");++cases;}
static void StoppedOwner(bool interruptibleWaiter){Net n;net=&n;CTextureCache c;Request owner(c,true);Wait(Within([&]{return n.started.load()==1;},2s),"fixture: owner did not start");
 Request waiter(c,interruptibleWaiter);Wait(Within([&]{std::lock_guard<CCriticalSection>l(c.m_processingSection);return !c.m_waiting.empty();},2s),"fixture: waiter did not wait");
 owner.stop=true;Wait(Within([&]{return owner.done.load();},2s),"fixture: owner did not stop");
 Check(owner.result.empty(),"a stopped request returns nothing");
 Wait(Within([&]{return n.started.load()==2;},3s),"a caller waiting for a stopped fill fetches the image itself");
 n.release=true;Wait(Within([&]{return waiter.done.load();},3s),"fixture: waiter did not end");
 Check(waiter.result=="cached/u"&&n.fills==2,"a caller waiting for a stopped fill fetches the image itself");
 std::lock_guard<CCriticalSection>l(c.m_processingSection);
 Check(c.m_interrupted.empty()&&c.m_waiting.empty(),"the record of a stopped fill ends with its waiters");++cases;}
static void FailureShared(){Net n;net=&n;n.fillOk=false;CTextureCache c;Request owner(c,false);Wait(Within([&]{return n.started.load()==1;},2s),"fixture: owner did not start");
 Request waiter(c,true);Wait(Within([&]{std::lock_guard<CCriticalSection>l(c.m_processingSection);return !c.m_waiting.empty();},2s),"fixture: waiter did not wait");
 n.release=true;Wait(Within([&]{return waiter.done.load()&&owner.done.load();},3s),"fixture: requests did not end");
 Check(waiter.result.empty()&&n.fills==1,"a fill that failed on its own is not repeated by its waiters");++cases;}
static void WaiterUsesResult(){Net n;net=&n;CTextureCache c;Request owner(c,true);Wait(Within([&]{return n.started.load()==1;},2s),"fixture: owner did not start");
 Request waiter(c,false);Wait(Within([&]{std::lock_guard<CCriticalSection>l(c.m_processingSection);return !c.m_waiting.empty();},2s),"fixture: waiter did not wait");
 n.release=true;Wait(Within([&]{return waiter.done.load();},3s),"fixture: waiter did not end");
 Check(waiter.result=="cached/u"&&n.fills==1,"a waiter uses the other request's result");++cases;}
static void StoppedAlone(){Net n;net=&n;CTextureCache c;std::atomic<int> reported{0};c.SetCachedObserver([&](const std::string&){++reported;});
 Request owner(c,true);Wait(Within([&]{return n.started.load()==1;},2s),"fixture: owner did not start");
 owner.stop=true;owner.Join();
 std::lock_guard<CCriticalSection>l(c.m_processingSection);
 Check(c.m_interrupted.empty(),"a stopped fill without waiters leaves no record");
 Check(reported==0,"a fill that caches nothing is not reported");++cases;}
static void StaleRecord(){Net n;net=&n;CTextureCache c;
 // A caller still registered for an earlier fill that was stopped.
 Request first(c,true);Wait(Within([&]{return n.started.load()==1;},2s),"fixture: first fill did not start");
 uint64_t earlier=0;{std::lock_guard<CCriticalSection>l(c.m_processingSection);earlier=c.m_fillIDs.at("u");++c.m_waiting[earlier];}
 first.stop=true;first.Join();
 {std::lock_guard<CCriticalSection>l(c.m_processingSection);Check(c.m_interrupted.count(earlier)==1,"fixture: earlier fill recorded");}
 // A later fill that fails on its own, with its own waiter.
 n.fillOk=false;Request owner(c,false);Wait(Within([&]{return n.started.load()==2;},2s),"fixture: later fill did not start");
 Request waiter(c,false);Wait(Within([&]{std::lock_guard<CCriticalSection>l(c.m_processingSection);return c.m_waiting.size()==2;},2s),"fixture: waiter did not wait");
 n.release=true;Wait(Within([&]{return waiter.done.load()&&owner.done.load();},3s),"fixture: requests did not end");
 Check(waiter.result.empty()&&n.fills==2,"a fill that failed on its own is not retried for an earlier stopped fill");
 {std::lock_guard<CCriticalSection>l(c.m_processingSection);c.m_waiting.erase(earlier);c.m_interrupted.erase(earlier);}++cases;}
static void DeclinedBackground(){Net n;net=&n;CTextureCache c;Request owner(c,false);Wait(Within([&]{return n.started.load()==1;},2s),"fixture: owner did not start");
 Request waiter(c,true);Wait(Within([&]{std::lock_guard<CCriticalSection>l(c.m_processingSection);return !c.m_waiting.empty();},2s),"fixture: waiter did not wait");
 // A background cache job for the same image, as CTextureCacheJob::DoWork: declined, then completed.
 {CTextureCacheJob job("u");if(!c.StartCacheImage("u",&job.m_fill))c.OnCachingComplete(false,&job);}
 Check(!Within([&]{return waiter.done.load();},200ms),"a declined background fill leaves the running fill alone");
 n.release=true;Wait(Within([&]{return waiter.done.load();},3s),"fixture: waiter did not end");
 Check(waiter.result=="cached/u"&&n.fills==1,"a declined background fill leaves the running fill alone");++cases;}
static void RetryLooksAgain(){Net n;net=&n;CTextureCache c;Request owner(c,true);Wait(Within([&]{return n.started.load()==1;},2s),"fixture: owner did not start");
 Request waiter(c,false);Wait(Within([&]{std::lock_guard<CCriticalSection>l(c.m_processingSection);return !c.m_waiting.empty();},2s),"fixture: waiter did not wait");
 // After the waiter's miss, another caller of the stopped fill caches the image; a fetch now would fail.
 n.fillOk=false;c.afterMiss=[&]{std::lock_guard<std::mutex>l(c.dbLock);c.db.insert("u");};
 owner.stop=true;Wait(Within([&]{return owner.done.load();},2s),"fixture: owner did not stop");n.release=true;
 Wait(Within([&]{return waiter.done.load();},3s),"fixture: waiter did not end");
 Check(waiter.result=="cached/u"&&n.fills==1,"a retry finds an image cached since its last look");++cases;}
static void WaitPerFill(){Net n;net=&n;CTextureCache c;Request owner(c,true);Wait(Within([&]{return n.started.load()==1;},2s),"fixture: owner did not start");
 // The waiter sleeps until let go, as a scheduler may keep it.
 CEvent::hold=true;
 Request waiter(c,false);Wait(Within([&]{std::lock_guard<CCriticalSection>l(c.m_processingSection);return !c.m_waiting.empty();},2s),"fixture: waiter did not wait");
 owner.stop=true;owner.Join();
 // A later fill starts before the waiter wakes, and fails on its own.
 n.fillOk=false;Request later(c,false);Wait(Within([&]{return n.started.load()==2;},2s),"fixture: later fill did not start");
 // Let the waiter go while the later fill runs; it counts in for that fill unless it waits on by image.
 uint64_t laterFill=0;{std::lock_guard<CCriticalSection>l(c.m_processingSection);laterFill=c.m_fillIDs.at("u");}
 CEvent::hold=false;
 Within([&]{std::lock_guard<CCriticalSection>l(c.m_processingSection);return c.m_waiting.count(laterFill)>0;},500ms);
 n.release=true;Wait(Within([&]{return waiter.done.load()&&later.done.load();},3s),"fixture: requests did not end");
 Check(waiter.result.empty()&&n.fills==2,"a waiter does not wait through a later fill and retry its failure");++cases;}
static void BackgroundEndsFill(){Net n;net=&n;auto c=std::make_shared<CTextureCache>();CServiceBroker::cache=c;
 // A background cache job whose completion callback was cancelled, as a skin unload does: only DoWork runs.
 CTextureCacheJob job("u");std::thread t([&]{job.DoWork();});
 Wait(Within([&]{return n.started.load()==1;},2s),"fixture: background fill did not start");
 Request waiter(*c,true);Wait(Within([&]{std::lock_guard<CCriticalSection>l(c->m_processingSection);return !c->m_waiting.empty();},2s),"fixture: waiter did not wait");
 n.release=true;t.join();
 Wait(Within([&]{return waiter.done.load();},2s),"a background fill ends without its completion callback");
 Check(waiter.result=="cached/u"&&n.fills==1,"a background fill ends without its completion callback");
 CServiceBroker::cache=nullptr;++cases;}
int main(){try{
 OwnFill();WaiterStops();StoppedOwner(false);StoppedOwner(true);FailureShared();WaiterUsesResult();StoppedAlone();StaleRecord();DeclinedBackground();RetryLooksAgain();WaitPerFill();BackgroundEndsFill();
 std::printf("PASS %u cases %u checks; actual CacheImage/OnCachingComplete, cache/job fixtures, real threads\n",cases,checks);return 0;
}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def source(text, mutation=None):
    functions = [EXTRACT.function(text, s) for s in SIGNATURES]
    functions.append(EXTRACT.function((ROOT / 'xbmc/TextureCacheJob.cpp').read_text(), 'bool CTextureCacheJob::DoWork()'))
    body = '\n'.join(functions)
    if mutation:
        old, new = mutation
        assert body.count(old) == 1, old
        body = body.replace(old, new, 1)
    for comment in ('/*= nullptr*/', '/*= {}*/', '/* = false */', '/* = nullptr */'):
        body = body.replace(comment, '')
    return PRELUDE + body + '\n' + TESTS


MUTATIONS = [
    ('no-own-interrupt', ('      job.m_readInterrupt = interrupt;\n', ''), "own fill reads with the caller's interrupt"),
    ('wait-ignores-interrupt', ('      if (interrupt && interrupt())\n      {\n        stopped = true;\n        break;\n      }\n', ''),
     'a waiting caller stops on its interrupt'),
    ('stop-not-recorded', ('OnCachingComplete(success, &job, !success && interrupt && interrupt());',
                           'OnCachingComplete(success, &job, false);'),
     'a caller waiting for a stopped fill fetches the image itself'),
    ('refetch-any-failure', ('    if (!interrupted || attempt >= MAX_REFETCHES)', '    if ((void)interrupted, attempt >= MAX_REFETCHES)'),
     'a fill that failed on its own is not repeated by its waiters'),
    ('record-without-waiters', (' && m_waiting.find(job->m_fill) != m_waiting.end()', ''),
     'a stopped fill without waiters leaves no record'),
    ('record-kept', ('        m_interrupted.erase(fill);\n', ''), 'the record of a stopped fill ends with its waiters'),
    ('record-per-image', ('      interrupted = m_interrupted.find(fill) != m_interrupted.end();',
                          '      interrupted = !m_interrupted.empty();'),
     'a fill that failed on its own is not retried for an earlier stopped fill'),
    ('release-any-fill', ('  if (fill == 0 || current == m_fillIDs.end() || current->second != fill)\n    return;\n',
                          '  if ((void)fill, current == m_fillIDs.end())\n    return;\n'),
     'a declined background fill leaves the running fill alone'),
    ('retry-trusts-miss', ('      if (attempt > 0)\n      {\n        // Another caller', '      if (false)\n      {\n        // Another caller'),
     'a retry finds an image cached since its last look'),
    ('wait-per-image', ('        const auto current = m_fillIDs.find(url);\n        if (current == m_fillIDs.end() || current->second != fill)\n          break;\n',
                        '        if (m_processinglist.find(url) == m_processinglist.end())\n          break;\n'),
     'a waiter does not wait through a later fill and retry its failure'),
    ('background-needs-callback', ('    cache->OnCachingComplete(success, this);\n', ''),
     'a background fill ends without its completion callback'),
    ('report-under-lock', ('      observer = m_cachedObserver;\n    }\n    if (observer)\n      observer(job->m_url);\n',
                           '      observer = m_cachedObserver;\n      if (observer)\n        observer(job->m_url);\n    }\n'),
     "the report comes without the cache's locks"),
    ('no-report', ('    if (observer)\n      observer(job->m_url);\n', ''), 'every cached image is reported'),
    ('report-failures', ('  if (success)\n  {\n    std::function', '  if (true)\n  {\n    std::function'),
     'a fill that caches nothing is not reported'),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', '--out-dir', dest='out', type=Path)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix='texture-cache-interrupt-'))
    out.mkdir(parents=True, exist_ok=True)
    text = (ROOT / SOURCE).read_text()
    env = {**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0', 'UBSAN_OPTIONS': 'halt_on_error=1:print_stacktrace=1'}
    records = []
    for name, mutation, witness in [('current', None, None), *MUTATIONS]:
        cpp = out / (name + '.cpp')
        cpp.write_text(source(text, mutation))
        exe = out / name
        cmd = ['g++', '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-pthread']
        if args.sanitize:
            cmd += ['-fsanitize=address,undefined', '-fno-sanitize-recover=undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
        cmd += [str(cpp), '-o', str(exe)]
        c = subprocess.run(cmd, capture_output=True, text=True)
        (out / (name + '-compile.log')).write_text(c.stdout + c.stderr)
        assert c.returncode == 0, c.stderr
        r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120, env=env)
        sanitizer = any(x in r.stdout + r.stderr for x in ('AddressSanitizer', 'UndefinedBehaviorSanitizer', 'runtime error:'))
        accepted = (r.returncode == 0 and not r.stderr) if witness is None else (
            r.returncode == 1 and r.stderr == 'FAIL ' + witness + '\n' and not sanitizer)
        records.append({'name': name, 'exit': r.returncode, 'witness': witness, 'accepted': accepted,
                        'generated_sha256': hashlib.sha256(cpp.read_bytes()).hexdigest()})
        (out / 'receipt.json').write_text(json.dumps({'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'source_sha256': hashlib.sha256(text.encode()).hexdigest(), 'sanitize': args.sanitize, 'results': records}, indent=2) + '\n')
        assert accepted, (name, r.stdout, r.stderr)
        print(r.stdout, end='', flush=True) if witness is None else print('PASS semantic control ' + name + ': ' + witness, flush=True)


if __name__ == '__main__':
    main()
