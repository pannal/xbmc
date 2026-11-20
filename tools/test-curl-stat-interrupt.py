#!/usr/bin/env python3
"""Run CCurlFile::Stat's progress callback with libcurl's multi interface.

Extract the callback from CurlFile.cpp and exercise HEAD and fallback GET
requests against local HTTP fixtures. The fixtures cover stalled requests,
the timeout with an empty interrupt, and the intentional stop after the first
GET body bytes. Caller interruption is recorded separately from that stop.

Needs libcurl headers and a library: --curl-include DIR (containing curl/) and
--curl-lib FILE, or a system libcurl development package.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'xbmc/filesystem/CurlFile.cpp'

HARNESS = r'''
#include <curl/curl.h>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <functional>
#include <stdexcept>
#include <string>
#include <thread>
using namespace std::chrono_literals;
using Clock=std::chrono::steady_clock;
static unsigned checks=0,cases=0;
static void Check(bool ok,const char*text){++checks;if(!ok)throw std::runtime_error(text);}
CALLBACK
// A local server: accepts, reads the request, then answers (data) or never answers (silent).
struct Server {
 int fd=-1;int port=0;std::thread t;std::atomic<bool> stop{false};bool answer;
 explicit Server(bool a):answer(a){
  fd=socket(AF_INET,SOCK_STREAM,0);int one=1;setsockopt(fd,SOL_SOCKET,SO_REUSEADDR,&one,sizeof one);
  sockaddr_in addr{};addr.sin_family=AF_INET;addr.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
  Check(bind(fd,(sockaddr*)&addr,sizeof addr)==0&&listen(fd,8)==0,"fixture: listen");
  socklen_t n=sizeof addr;getsockname(fd,(sockaddr*)&addr,&n);port=ntohs(addr.sin_port);
  t=std::thread([this]{
   int c=accept(fd,nullptr,nullptr);if(c<0)return;char buf[2048];(void)!read(c,buf,sizeof buf);
   if(answer){const char*r="HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: 100000\r\n\r\n";(void)!write(c,r,strlen(r));
    std::string body(4096,'x');for(int i=0;i<20&&!stop;++i){(void)!write(c,body.data(),body.size());std::this_thread::sleep_for(50ms);}}
   while(!stop){std::this_thread::sleep_for(10ms);}
   close(c);});
 }
 ~Server(){stop=true;shutdown(fd,SHUT_RDWR);close(fd);t.join();}
 std::string Url()const{return "http://127.0.0.1:"+std::to_string(port)+"/art.jpg";}
};
// As Stat: HEAD (or the GET fallback), CURLOPT_TIMEOUT, the progress callback with its data.
static CURLcode Request(const std::string&url,bool head,long timeout,StatProgress*progress){
 CURL*h=curl_easy_init();curl_easy_setopt(h,CURLOPT_URL,url.c_str());curl_easy_setopt(h,CURLOPT_TIMEOUT,timeout);
 if(head)curl_easy_setopt(h,CURLOPT_NOBODY,1L);
 curl_easy_setopt(h,CURLOPT_WRITEFUNCTION,+[](char*,size_t s,size_t n,void*){return s*n;});
 if(progress){curl_easy_setopt(h,CURLOPT_XFERINFOFUNCTION,stat_progress_callback);curl_easy_setopt(h,CURLOPT_XFERINFODATA,progress);curl_easy_setopt(h,CURLOPT_NOPROGRESS,0L);}
 CURLM* multi = curl_multi_init();
 CURLcode result = CURLE_FAILED_INIT;
 if (curl_multi_add_handle(multi, h) == CURLM_OK)
 {
  int running = 0;
  while (curl_multi_perform(multi, &running) == CURLM_OK)
  {
   int queued = 0;
   const auto* message = curl_multi_info_read(multi, &queued);
   if (message && message->msg == CURLMSG_DONE)
   {
    result = message->data.result;
    break;
   }
   if (!running || curl_multi_poll(multi, nullptr, 0, 1000, nullptr) != CURLM_OK)
    break;
  }
  curl_multi_remove_handle(multi, h);
 }
 curl_multi_cleanup(multi);
 curl_easy_cleanup(h);
 return result;
}
static void HeadStopsOnInterrupt(){Server s(false);std::atomic<bool> gone{false};std::function<bool()> interrupt=[&]{return gone.load();};
 StatProgress p{&interrupt,false};std::thread later([&]{std::this_thread::sleep_for(300ms);gone=true;});
 const auto start=Clock::now();const CURLcode r=Request(s.Url(),true,30,&p);const auto took=Clock::now()-start;later.join();
 Check(r==CURLE_ABORTED_BY_CALLBACK&&took<1500ms,"a waiting HEAD stops on the read interrupt");
 Check(p.interrupted,"the callback records caller interruption");++cases;}
static void EmptyInterruptWaits(){Server s(false);std::function<bool()> none;StatProgress p{&none,false};
 const auto start=Clock::now();const CURLcode r=Request(s.Url(),true,2,&p);const auto took=Clock::now()-start;
 Check(r==CURLE_OPERATION_TIMEDOUT&&took>=1900ms,"without an interrupt the HEAD waits for curl's timeout");++cases;}
static void FallbackStopsOnData(){Server s(true);std::function<bool()> none;StatProgress p{&none,true};
 const CURLcode r=Request(s.Url(),false,5,&p);Check(r==CURLE_ABORTED_BY_CALLBACK,"the GET fallback stops once data arrives");
 Check(!p.interrupted,"stopping after body bytes records no caller interruption");++cases;}
static void FallbackStopsOnInterrupt(){Server s(false);std::atomic<bool> gone{false};std::function<bool()> interrupt=[&]{return gone.load();};
 StatProgress p{&interrupt,true};std::thread later([&]{std::this_thread::sleep_for(300ms);gone=true;});
 const auto start=Clock::now();const CURLcode r=Request(s.Url(),false,30,&p);const auto took=Clock::now()-start;later.join();
 Check(r==CURLE_ABORTED_BY_CALLBACK&&took<1500ms,"the GET fallback stops on the read interrupt");++cases;}
int main(){try{curl_global_init(CURL_GLOBAL_DEFAULT);
 HeadStopsOnInterrupt();EmptyInterruptWaits();FallbackStopsOnData();FallbackStopsOnInterrupt();
 std::printf("PASS %u cases %u checks; actual Stat progress callback, real libcurl %s\n",cases,checks,curl_version());return 0;
}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def extract(text):
    start = text.index('struct StatProgress\n')
    m = re.search(r'extern "C" int stat_progress_callback\(.*?\n\}\n', text[start:], re.S)
    assert m, 'stat_progress_callback missing'
    return text[start:start + m.end()]


MUTATIONS = [
    ('interrupt-ignored', ('  if (progress->interrupt && *progress->interrupt && (*progress->interrupt)())\n'
                           '  {\n    progress->interrupted = true;\n    return 1;\n  }\n', ''),
     'a waiting HEAD stops on the read interrupt'),
    ('interrupt-unrecorded', ('    progress->interrupted = true;\n', ''),
     'the callback records caller interruption'),
    ('data-ignored', ('progress->abortOnData && dlnow > 0 ? 1 : 0', '0'), 'the GET fallback stops once data arrives'),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', '--out-dir', dest='out', type=Path)
    parser.add_argument('--curl-include', type=Path)
    parser.add_argument('--curl-lib')
    args = parser.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix='curl-stat-interrupt-'))
    out.mkdir(parents=True, exist_ok=True)
    text = (ROOT / SOURCE).read_text()
    callback = extract(text)
    records = []
    for name, mutation, witness in [('current', None, None), *MUTATIONS]:
        code = callback
        if mutation:
            assert code.count(mutation[0]) == 1, mutation[0]
            code = code.replace(mutation[0], mutation[1], 1)
        cpp = out / (name + '.cpp')
        cpp.write_text(HARNESS.replace('CALLBACK', code, 1))
        exe = out / name
        cmd = ['g++', '-std=c++17', '-O1', '-g', '-pthread', '-Wall', '-Werror', '-Wno-unused-parameter']
        if args.curl_include:
            cmd += ['-I', str(args.curl_include)]
        cmd += [str(cpp), '-o', str(exe), args.curl_lib or '-lcurl']
        c = subprocess.run(cmd, capture_output=True, text=True)
        (out / (name + '-compile.log')).write_text(c.stdout + c.stderr)
        assert c.returncode == 0, c.stderr
        r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=120)
        accepted = (r.returncode == 0 and not r.stderr) if witness is None else (
            r.returncode == 1 and r.stderr == 'FAIL ' + witness + '\n')
        records.append({'name': name, 'exit': r.returncode, 'witness': witness, 'accepted': accepted})
        (out / 'receipt.json').write_text(json.dumps({'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'source_sha256': hashlib.sha256(text.encode()).hexdigest(), 'results': records}, indent=2) + '\n')
        assert accepted, (name, r.stdout, r.stderr)
        print(r.stdout, end='', flush=True) if witness is None else print('PASS semantic control ' + name + ': ' + witness, flush=True)


if __name__ == '__main__':
    main()
