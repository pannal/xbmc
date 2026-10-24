#!/usr/bin/env python3
"""Compile the actual header and inspect files written by it on the host.

Uses real files/mmap/threads, deterministic clocks and injected syscall failures.
This verifies writer/scoping/ownership, not Kodi scheduling, GL or target timings.
"""
import argparse
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile

HEADER = 'xbmc/utils/GUIFrameTiming.h'
CAPACITY = 32768
FILE_BYTES = 64 + 264 * CAPACITY

CPP = r'''
#include "utils/GUIFrameTiming.h"
#include <atomic>
#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <iostream>
#include <memory>
#include <new>
#include <sys/mman.h>
#include <thread>
#include <time.h>
#include <unistd.h>
using namespace GUI_FRAME_TIMING;
static const char* fault = "";
static bool failClock = false;
static std::atomic<unsigned> clocks{0};
static uint64_t wall = 0, cpu = 0;
static bool trackHeap = false;
static unsigned heapCalls = 0;
__attribute__((noinline)) void* operator new(std::size_t size)
{
 if (trackHeap) ++heapCalls;
 void* pointer=std::malloc(size ? size : 1);
 if (!pointer) throw std::bad_alloc();
 return pointer;
}
__attribute__((noinline)) void operator delete(void* pointer) noexcept { std::free(pointer); }
__attribute__((noinline)) void operator delete(void* pointer,std::size_t) noexcept { std::free(pointer); }
static void Check(bool value, const char* message)
{ if (!value) { std::cerr << message << '\n'; std::exit(1); } }
extern "C" int __real_posix_fallocate(int,off_t,off_t);
extern "C" int __wrap_posix_fallocate(int fd,off_t offset,off_t length)
{ return std::strcmp(fault,"allocate") == 0 ? ENOSPC : __real_posix_fallocate(fd,offset,length); }
extern "C" int __real_fchmod(int,mode_t);
extern "C" int __wrap_fchmod(int fd,mode_t mode)
{ if (std::strcmp(fault,"chmod") == 0) { errno=EPERM; return -1; } return __real_fchmod(fd,mode); }
extern "C" void* __real_mmap(void*,size_t,int,int,int,off_t);
extern "C" void* __wrap_mmap(void* addr,size_t length,int prot,int flags,int fd,off_t offset)
{ if (std::strcmp(fault,"mmap") == 0) { errno=ENOMEM; return MAP_FAILED; } return __real_mmap(addr,length,prot,flags,fd,offset); }
extern "C" int __wrap_clock_gettime(clockid_t id,timespec* value)
{
 ++clocks;
 if (failClock) { errno=EIO; return -1; }
 const uint64_t n = id==CLOCK_MONOTONIC ? (wall+=100) : (cpu+=10);
 value->tv_sec=static_cast<time_t>(n/1000000000);
 value->tv_nsec=static_cast<long>(n%1000000000);
 errno=EAGAIN; // Successful observation must not leak an errno change either.
 return 0;
}
void CrossTU();
static void IgnoredWorker()
{
 const unsigned before=clocks.load();
 std::thread worker([] {
   errno=ERANGE;
   Session rejected;
#ifdef HAS_LIBAMCODEC
   Check(rejected.Result()==InitResult::Rejected,"worker took ownership");
#endif
   { Frame frame; Scope scope(Region::Process); Count(Counter::FontLookup); }
   Check(errno==ERANGE,"worker changed errno");
 });
 worker.join();
 Check(clocks.load()==before,"worker used observation clocks");
}
int main(int argc,char** argv)
{
 Check(argc==3,"mode and output path required");
 const std::string mode=argv[1]; fault=argv[1];
 if (mode=="disabled") unsetenv("P3I_GUI_TIMING_PATH");
 else setenv("P3I_GUI_TIMING_PATH",argv[2],1);
 errno=EDOM;
 {
   Session session;
   Check(errno==EDOM,"initialization changed errno");
   if (mode=="disabled" || mode=="noop")
   {
     Check(session.Result()==InitResult::Disabled,"capture enabled without supported opt-in");
     { Frame frame; Scope scope(Region::Render); Count(Counter::FontLookup); }
     Check(clocks==0,"disabled clocks used");
   }
   else if (mode=="allocate" || mode=="chmod" || mode=="mmap" || mode=="rejected")
   {
     Check(session.Result()==InitResult::Failed,"failed initialization accepted");
     { Frame frame; Scope scope(Region::Render); Count(Counter::FontLookup); }
     Check(clocks==0,"failed initialization clocks used");
   }
   else
   {
     Check(session.Result()==InitResult::Started,"capture failed to start");
     Count(Counter::FontLookup); // Outside a frame: ignored.
     IgnoredWorker();
     if (mode=="bounded")
     {
       for (unsigned i=0;i<CAPACITY+3;++i)
       { Frame frame; Count(Counter::FontLookup); }
       Check(clocks==CAPACITY*4,"capture did not stop clocks at capacity");
     }
     else if (mode=="clock")
     { failClock=true; Frame frame; Scope scope(Region::Process); }
     else if (mode=="clock-end")
     { Frame frame; failClock=true; }
     else if (mode=="unbalanced")
     {
       std::unique_ptr<Scope> late;
       { Frame frame; late=std::make_unique<Scope>(Region::Process); }
       late.reset();
     }
     else
     {
       {
         Frame frame;
         Check(errno==EDOM,"frame start changed errno");
         trackHeap=true;
         {
           Scope process(Region::Process);
           { Scope move(Region::FrameMove); Count(Counter::FontLookup); Count(Counter::FontHit); }
         }
         trackHeap=false;
         Check(heapCalls==0,"active scopes allocated heap storage");
         { Frame nested; Scope outer(Region::RenderPass); Scope inner(Region::RenderPass); }
         CrossTU();
         IgnoredWorker();
         errno=EBUSY;
         { Scope render(Region::Render); errno=EINTR; }
         Check(errno==EINTR,"scope end changed errno");
       }
       Check(errno==EINTR,"frame end changed errno");
       trackHeap=true;
       { Frame frame; Count(Counter::FontLookup); Count(Counter::FontMiss); }
       trackHeap=false;
       Check(heapCalls==0,"active frame or counters allocated heap storage");
       Count(Counter::FontMiss); // Outside a frame: ignored.
     }
   }
   errno=E2BIG;
 }
 Check(errno==E2BIG,"session shutdown changed errno");
 return 0;
}
'''

OTHER = r'''
#include "utils/GUIFrameTiming.h"
void CrossTU()
{
 GUI_FRAME_TIMING::Scope scope(GUI_FRAME_TIMING::Region::PublishBounds);
 GUI_FRAME_TIMING::Count(GUI_FRAME_TIMING::Counter::FontLookup);
 GUI_FRAME_TIMING::Count(GUI_FRAME_TIMING::Counter::FontMiss);
}
'''


def read(path):
    data = path.read_bytes()
    header = struct.unpack_from('<8s10I2Q', data)
    assert header[:7] == (b'P3IGUI1\0',1,64,264,CAPACITY,8,3), header
    assert header[7] > 0 and header[8] > 0, header
    completed, flags = header[9:11]
    assert completed <= CAPACITY and len(data) == FILE_BYTES
    records = [struct.unpack_from('<5Q2I27Q',data,64+i*264) for i in range(completed)]
    for i, record in enumerate(records):
        assert record[0] == i+1 and record[3] >= record[1] and record[4] >= record[2], record
    assert data[64+completed*264:] == bytes(FILE_BYTES-64-completed*264) or flags & 6
    return completed, flags, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--cxx',default=os.environ.get('CXX','c++'))
    parser.add_argument('--sanitize',action='store_true')
    args = parser.parse_args()
    source = (args.repo/HEADER).read_text()
    with tempfile.TemporaryDirectory(prefix='gui-frame-timing-') as directory:
        work=Path(directory)
        (work/'main.cpp').write_text(CPP)
        (work/'other.cpp').write_text(OTHER)

        def compile(label, text=None, aml=True):
            include=args.repo/'xbmc'
            if text is not None:
                include=work/label
                (include/'utils').mkdir(parents=True)
                (include/'utils/GUIFrameTiming.h').write_text(text)
            binary=work/label
            if text is not None: binary=work/(label+'-bin')
            command=shlex.split(args.cxx)+['-std=c++17','-O1','-g','-Wall','-Wextra','-Werror',
                '-pthread','-I'+str(include),str(work/'main.cpp'),str(work/'other.cpp'),'-o',str(binary),
                '-Wl,--wrap=clock_gettime,--wrap=mmap,--wrap=posix_fallocate,--wrap=fchmod']
            if aml: command+=['-DHAS_LIBAMCODEC=1']
            if args.sanitize:
                command+=['-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie']
            subprocess.run(command,check=True)
            return binary

        def run(binary, mode, path, success=True):
            p=subprocess.run([str(binary),mode,str(path)],capture_output=True,text=True,
                             env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'})
            if success: assert p.returncode==0,(mode,p.stdout,p.stderr)
            return p

        binary=compile('actual')
        run(binary,'disabled',work/'disabled.bin')
        assert not (work/'disabled.bin').exists()
        run(compile('non-aml',aml=False),'noop',work/'noop.bin')
        assert not (work/'noop.bin').exists()
        output=work/'record.bin'
        run(binary,'record',output)
        count,flags,records=read(output)
        assert count==2 and flags==0
        first=records[0]
        assert first[5:7]==(24,1),first  # NestedFrame | NestedRegion, one nested frame.
        assert first[7:13]==(1,300,30,1,100,10),first  # Inclusive Process and FrameMove.
        assert first[25:28]==(2,400,40),first  # Inclusive recursive RenderPass.
        assert first[22:25]==(1,100,10),first  # Separate TU shares recorder state.
        assert first[31:]==(2,1,1) and records[1][31:]==(1,0,1),records
        assert output.stat().st_mode & 0o777 == 0o600
        for mode in ('allocate','chmod','mmap'):
            path=work/(mode+'.bin');run(binary,mode,path)
            assert path.exists() and path.read_bytes()[:8] != b'P3IGUI1\0'
        for mode,flag in [('clock',2),('clock-end',2),('unbalanced',4)]:
            path=work/(mode+'.bin');run(binary,mode,path)
            count,flags,_=read(path);assert count==0 and flags==flag,(mode,count,flags)
        bounded=work/'bounded.bin';run(binary,'bounded',bounded)
        count,flags,records=read(bounded)
        assert count==CAPACITY and flags==1 and records[-1][31:]==(1,0,0)
        original=output.read_bytes()
        run(binary,'rejected',output);assert output.read_bytes()==original
        link=work/'link.bin';link.symlink_to(output)
        run(binary,'rejected',link);assert output.read_bytes()==original and link.is_symlink()
        run(binary,'rejected',work/'absent-parent'/'capture.bin')
        run(binary,'rejected',Path('relative-capture.bin'))
        print('PASS actual header: opt-out/non-AML, exclusive0600, workers, cross-TU scopes, nested inclusive accounting, bounded32768, errno and injected errors')
        mutations = {
            'worker-owner': (' && owner;', ';'),
            'inclusive-total': ('duration.wallNs += wall - m_wall;', 'duration.wallNs = wall - m_wall;'),
        }
        for name,(old,new) in mutations.items():
            assert source.count(old)==1,(name,source.count(old))
            mutant=compile(name,source.replace(old,new,1))
            path=work/(name+'.bin');p=run(mutant,'record',path,False)
            if name=='worker-owner':
                assert p.returncode!=0 and 'worker used observation clocks' in p.stderr,p.stderr
            else:
                assert p.returncode==0,p.stderr
                _,_,rows=read(path)
                assert rows[0][25:28] != (2,400,40),'broken inclusive accounting accepted'
            print('PASS negative control:',name)
        # Removing exclusive creation must demonstrably clobber an existing file.
        assert source.count('O_EXCL | ')==1
        mutant=compile('clobber',source.replace('O_EXCL | ','',1))
        path=work/'clobbered.bin';path.write_bytes(b'KEEP')
        run(mutant,'record',path)
        assert path.read_bytes()!=b'KEEP','missing exclusive open was not exercised'
        print('PASS negative control: missing O_EXCL would overwrite existing content')
    print('Limits: host filesystem/threads and stub clocks; no Kodi, GPU, scanout or device-performance acceptance.')


if __name__=='__main__':
    main()
