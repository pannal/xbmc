#!/usr/bin/env python3
"""Compile the actual header and inspect files written by it on the host.

Uses real files/mmap/threads, deterministic clocks and injected syscall failures.
Checks ABI2 numeric decisions/tokens as well as writer/scoping/ownership. This
does not verify production hook placement, Kodi scheduling, GL or target timings.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shlex
import struct
import subprocess
import tempfile

HEADER = 'xbmc/utils/GUIFrameTiming.h'
CAPACITY = 32768
RECORD_BYTES = 496
FILE_BYTES = 64 + RECORD_BYTES * CAPACITY
COUNTERS = '''FontLookup FontHit FontMiss MessageNavStarted MessageNavCompleted
MessageNavHandled MessageNavCandidateChanged MessageNavTargetChanged
MessageNavUnknownSelection MessageNavLastAction MessageNavRequestedWindow
MessageNavBeforeWindow MessageNavBeforeControl MessageNavBeforeSelection
MessageNavAfterWindow MessageNavAfterControl MessageNavAfterSelection
WindowProcessCalls DirtyEmitted RenderCalls EffectiveBufferAge DirtyAlgorithm
MarkedBeforeAge MarkedAfterAge SolvedRegions RenderForced RenderDirty RenderClean
Idle40ms RenderAttempt PresentResult GuiRendered'''.split()

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
static thread_local bool trackHeap = false;
static thread_local unsigned heapCalls = 0;
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
static void CounterNoops()
{
 const unsigned before=clocks.load();
 const unsigned beforeHeap=heapCalls;
 const bool previousTracking=trackHeap;trackHeap=true;
 Check(ActiveFrame()==0,"inactive token was nonzero");
 Add(Counter::MessageNavStarted,7);Set(Counter::MessageNavLastAction,12);
 Add(static_cast<Counter>(COUNTER_COUNT),999);
 Set(static_cast<Counter>(UINT32_MAX),999);
 trackHeap=previousTracking;
 Check(heapCalls==beforeHeap,"inactive numeric operations allocated heap storage");
 Check(clocks==before,"inactive numeric operations used clocks");
}
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
   CounterNoops();
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
     CounterNoops();
     { Frame frame; Scope scope(Region::Render); Count(Counter::FontLookup); }
     Check(clocks==0,"disabled clocks used");
   }
   else if (mode=="allocate" || mode=="chmod" || mode=="mmap" || mode=="rejected")
   {
     Check(session.Result()==InitResult::Failed,"failed initialization accepted");
     CounterNoops();
     { Frame frame; Scope scope(Region::Render); Count(Counter::FontLookup); }
     Check(clocks==0,"failed initialization clocks used");
   }
   else
   {
     Check(session.Result()==InitResult::Started,"capture failed to start");
     CounterNoops();
     Count(Counter::FontLookup); // Outside a frame: ignored.
     IgnoredWorker();
     if (mode=="bounded")
     {
       for (unsigned i=0;i<CAPACITY+3;++i)
       { Frame frame; Count(Counter::FontLookup); }
       Check(clocks==CAPACITY*4,"capture did not stop clocks at capacity");
       CounterNoops();
     }
     else if (mode=="numeric")
     {
       {
         Frame frame;
         Check(ActiveFrame()==1,"first frame token incorrect");
         IgnoredWorker();
         const unsigned before=clocks.load();
         errno=EILSEQ;trackHeap=true;
         Add(Counter::MessageNavStarted,4);Add(Counter::MessageNavStarted,3);
         Set(Counter::MessageNavLastAction,99);Set(Counter::MessageNavLastAction,14);
         Set(Counter::MessageNavBeforeSelection,static_cast<uint64_t>(int64_t{-1}));
         Set(Counter::MessageNavAfterSelection,static_cast<uint64_t>(int64_t{-1}));
         Set(Counter::EffectiveBufferAge,static_cast<uint64_t>(int64_t{-2}));
         Add(Counter::DirtyEmitted,5);Set(Counter::DirtyEmitted,9);Add(Counter::DirtyEmitted,2);
         Count(Counter::FontLookup);
         Check(ActiveFrame()==1,"numeric operations changed token");
         trackHeap=false;
         Check(heapCalls==0,"numeric operations allocated heap storage");
         Check(clocks==before,"numeric operations used observation clocks");
         Check(errno==EILSEQ,"numeric operations changed errno");
       }
       CounterNoops();
       { Frame frame; Check(ActiveFrame()==2,"second frame token incorrect"); }
       CounterNoops();
       {
         Frame frame;Check(ActiveFrame()==3,"third frame token incorrect");
         const unsigned before=clocks.load();trackHeap=true;
         Set(Counter::GuiRendered,123);
         Add(static_cast<Counter>(COUNTER_COUNT),999);
         Set(static_cast<Counter>(COUNTER_COUNT),777);
         Add(static_cast<Counter>(UINT32_MAX),999);
         Set(static_cast<Counter>(UINT32_MAX),777);
         trackHeap=false;
         Check(heapCalls==0,"invalid numeric operations allocated heap storage");
         Check(clocks==before,"invalid numeric operations used observation clocks");
       }
       CounterNoops();
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
         { Frame nested; Check(ActiveFrame()==1,"nested frame changed token");
           Scope outer(Region::RenderPass); Scope inner(Region::RenderPass); }
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
   CounterNoops(); // Includes the disabled-after-failure/capacity states.
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
    assert header[:7] == (b'P3IGUI2\0',2,64,RECORD_BYTES,CAPACITY,8,len(COUNTERS)), header
    assert header[7] > 0 and header[8] > 0, header
    completed, flags = header[9:11]
    assert completed <= CAPACITY and len(data) == FILE_BYTES
    records = [struct.unpack_from('<5Q2I56Q',data,64+i*RECORD_BYTES) for i in range(completed)]
    for i, record in enumerate(records):
        assert record[0] == i+1 and record[3] >= record[1] and record[4] >= record[2], record
    assert data[64+completed*RECORD_BYTES:] == bytes(FILE_BYTES-64-completed*RECORD_BYTES) or flags & 6
    return completed, flags, records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--cxx',default=os.environ.get('CXX','c++'))
    parser.add_argument('--sanitize',action='store_true')
    parser.add_argument('--out',type=Path,help='retain generated sources, commands and receipts')
    args = parser.parse_args()
    source = (args.repo/HEADER).read_text()
    counter_enum=source.split('enum class Counter : uint32_t',1)[1].split('};',1)[0].split('{',1)[1]
    assert [name.strip() for name in counter_enum.split(',')]==COUNTERS+['Count']
    if args.out:
        args.out.mkdir(parents=True,exist_ok=False)
    context=contextlib.nullcontext(str(args.out)) if args.out else tempfile.TemporaryDirectory(prefix='gui-frame-timing-')
    with context as directory:
        work=Path(directory)
        receipts={'header_sha256':hashlib.sha256((args.repo/HEADER).read_bytes()).hexdigest(),
                  'sanitize':args.sanitize,'counter_names':COUNTERS,'compile':[],'runs':[]}
        def save():
            (work/'results.json').write_text(json.dumps(receipts,indent=2)+'\n')
        save()
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
            p=subprocess.run(command,capture_output=True,text=True)
            (work/(label+'-compile-stdout.txt')).write_text(p.stdout)
            (work/(label+'-compile-stderr.txt')).write_text(p.stderr)
            receipts['compile'].append({'label':label,'argv':command,'exit':p.returncode});save()
            assert p.returncode==0,(label,p.stdout,p.stderr)
            return binary

        def run(binary, mode, path, success=True):
            p=subprocess.run([str(binary),mode,str(path)],capture_output=True,text=True,
                             env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0'})
            label=binary.name+'-'+mode+'-'+path.stem
            (work/(label+'-stdout.txt')).write_text(p.stdout)
            (work/(label+'-stderr.txt')).write_text(p.stderr)
            receipts['runs'].append({'label':label,'argv':[str(binary),mode,str(path)],
                                    'exit':p.returncode,'ASAN_OPTIONS':'detect_leaks=0'});save()
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
        assert first[31:]==(2,1,1)+(0,)*29 and records[1][31:]==(1,0,1)+(0,)*29,records
        numeric=work/'numeric.bin';run(binary,'numeric',numeric)
        count,flags,rows=read(numeric)
        assert count==3 and flags==0 and all(row[5:7]==(0,0) for row in rows)
        expected=[0]*len(COUNTERS)
        for name,value in [('FontLookup',1),('MessageNavStarted',7),('MessageNavLastAction',14),
                           ('MessageNavBeforeSelection',2**64-1),('MessageNavAfterSelection',2**64-1),
                           ('EffectiveBufferAge',2**64-2),('DirtyEmitted',11)]:
            expected[COUNTERS.index(name)]=value
        assert rows[0][31:]==tuple(expected),('Add/Set or signed encoding',rows[0][31:])
        assert rows[1][31:]==(0,)*32,('counters leaked between frames',rows[1][31:])
        expected=[0]*32;expected[-1]=123
        assert rows[2][31:]==tuple(expected),('invalid counter modified valid fields',rows[2][31:])
        assert output.stat().st_mode & 0o777 == 0o600
        for mode in ('allocate','chmod','mmap'):
            path=work/(mode+'.bin');run(binary,mode,path)
            assert path.exists() and path.read_bytes()[:8] != b'P3IGUI2\0'
        for mode,flag in [('clock',2),('clock-end',2),('unbalanced',4)]:
            path=work/(mode+'.bin');run(binary,mode,path)
            count,flags,_=read(path);assert count==0 and flags==flag,(mode,count,flags)
        bounded=work/'bounded.bin';run(binary,'bounded',bounded)
        count,flags,records=read(bounded)
        assert count==CAPACITY and flags==1 and records[-1][31:]==(1,0,0)+(0,)*29
        original=output.read_bytes()
        run(binary,'rejected',output);assert output.read_bytes()==original
        link=work/'link.bin';link.symlink_to(output)
        run(binary,'rejected',link);assert output.read_bytes()==original and link.is_symlink()
        run(binary,'rejected',work/'absent-parent'/'capture.bin')
        run(binary,'rejected',Path('relative-capture.bin'))
        print('PASS actual header: opt-out/non-AML, exclusive0600, workers, cross-TU scopes, nested inclusive accounting, bounded32768, errno and injected errors')
        print('PASS ABI2 numeric counters: Add/Set, tokens, signed unknowns, zeroed frames, invalid IDs, owner/inactive/worker noops, zero heap and no numeric clocks')
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
        numeric_mutations={
            'add-overwrites': ('counters[static_cast<uint32_t>(counter)] += value;',
                               'counters[static_cast<uint32_t>(counter)] = value;'),
            'set-accumulates': ('counters[static_cast<uint32_t>(counter)] = value;',
                                'counters[static_cast<uint32_t>(counter)] += value;'),
            'wrong-token': ('return detail::current->frame;', 'return detail::current->frame + 1;'),
        }
        for name,(old,new) in numeric_mutations.items():
            assert source.count(old)==1,(name,source.count(old))
            mutant=compile(name,source.replace(old,new,1))
            path=work/(name+'.bin');p=run(mutant,'numeric',path,False)
            if name=='wrong-token':
                assert p.returncode==1 and 'first frame token incorrect' in p.stderr,p.stderr
            else:
                assert p.returncode==0,p.stderr
                _,_,rows=read(path)
                index=COUNTERS.index('MessageNavStarted' if name=='add-overwrites' else 'MessageNavLastAction')
                assert rows[0][31+index] != (7 if name=='add-overwrites' else 14),'broken numeric operation accepted'
            print('PASS negative control:',name)
        # Removing exclusive creation must demonstrably clobber an existing file.
        assert source.count('O_EXCL | ')==1
        mutant=compile('clobber',source.replace('O_EXCL | ','',1))
        path=work/'clobbered.bin';path.write_bytes(b'KEEP')
        run(mutant,'record',path)
        assert path.read_bytes()!=b'KEEP','missing exclusive open was not exercised'
        print('PASS negative control: missing O_EXCL would overwrite existing content')
        receipts['pass']=True;receipts['limits']=__doc__;save()
    print('Limits: host filesystem/threads and stub clocks; no Kodi, GPU, scanout or device-performance acceptance.')


if __name__=='__main__':
    main()
