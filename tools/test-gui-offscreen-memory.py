#!/usr/bin/env python3
"""Run complete production container/list-item FreeMemory methods with owned layouts.

Compare resource release/order and retained layouts against the installed-source
baseline. Layout groups own real unique_ptr payloads; graphics/backend resources,
rendering, concurrent writers and device performance are outside this fixture.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile

BASELINE = 'bfa0b0f91d06971e0d47d1aa1e44837ef01cdf98'
CONTAINER = 'xbmc/guilib/GUIBaseContainer.cpp'
ITEM = 'xbmc/guilib/GUIListItem.cpp'
HEADER = 'xbmc/guilib/GUIListItem.h'
LAYOUT = 'xbmc/guilib/GUIListItemLayout.cpp'


def method(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda m: ' ' * len(m.group()), source)
    brace = masked.index('{', start)
    end, depth = brace + 1, 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


FIXTURE = r'''
#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <iostream>
#include <memory>
#include <set>
#include <stdexcept>
#include <string>
#include <tuple>
#include <vector>
static unsigned checks=0,cases=0,calls=0;
static uint64_t digest=1469598103934665603ULL;
static std::vector<std::tuple<int,int,bool>> events;
static std::set<int> live;
static std::function<void(int)> callback;
static void Check(bool value,const char* description)
{++checks;if(!value){std::cerr<<"FAIL "<<description<<'\n';std::exit(1);}}
static void Hash(uint64_t value){digest^=value;digest*=1099511628211ULL;}
struct Resource
{
 int id;
 explicit Resource(int value):id(value){Check(live.insert(id).second,"resource ID unique");}
 ~Resource(){Check(live.erase(id)==1,"resource released exactly once");events.emplace_back(2,id,false);}
};
struct Group
{
 int id;std::unique_ptr<Resource> resource;
 explicit Group(int value):id(value),resource(std::make_unique<Resource>(value)){}
 void FreeResources(bool immediately)
 {
  events.emplace_back(1,id,immediately);
  if(callback)callback(id);
  resource.reset();
 }
};
class CGUIListItemLayout;
class CGUIListItem
{
public:
 int id;
 std::unique_ptr<CGUIListItemLayout> m_layout,m_focusedLayout;
 explicit CGUIListItem(int value);
 ~CGUIListItem();
 @HAS_LAYOUTS@
 void FreeMemory(bool immediately=false){++calls;FreeMemoryBody(immediately);}
 void FreeMemoryBody(bool immediately);
};
// HasLayouts above is compiled while its pointee is still incomplete.
class CGUIListItemLayout
{
public:
 int id;Group m_group;
 explicit CGUIListItemLayout(int value):id(value),m_group(value){}
 ~CGUIListItemLayout()
 {Check(!m_group.resource,"FreeResources precedes layout destruction");events.emplace_back(3,id,false);}
 void FreeResources(bool immediately);
};
CGUIListItem::CGUIListItem(int value):id(value){}
CGUIListItem::~CGUIListItem(){FreeMemory();}
@ITEM_METHOD@
@LAYOUT_METHOD@
struct CGUIBaseContainer
{
 std::vector<std::shared_ptr<CGUIListItem>> m_items;
 bool cached=false;int lastStart=0,lastEnd=0;
 explicit CGUIBaseContainer(int count)
 {for(int i=0;i<count;++i)m_items.push_back(std::make_shared<CGUIListItem>(i));}
 void FreeMemory(int keepStart,int keepEnd);
};
@CONTAINER_METHOD@
static int Mask(const CGUIListItem& item)
{return (item.m_layout?1:0)|(item.m_focusedLayout?2:0);}
static void Attach(CGUIListItem& item,unsigned mask,int generation=0)
{
 Check(!item.m_layout&&!item.m_focusedLayout,"fixture attaches to empty owner");
 if(mask&1)item.m_layout=std::make_unique<CGUIListItemLayout>(generation*100000+item.id*10+1);
 if(mask&2)item.m_focusedLayout=std::make_unique<CGUIListItemLayout>(generation*100000+item.id*10+2);
}
static bool Outside(int index,int start,int end)
{return start<end?(index<start||index>end):(index>end&&index<start);}
static void HashState(const CGUIBaseContainer& c)
{
 for(const auto& item:c.m_items)
 {Hash(item->id);Hash(item->m_layout?item->m_layout->id:0);Hash(item->m_focusedLayout?item->m_focusedLayout->id:0);}
 for(const auto& event:events){Hash(std::get<0>(event));Hash(std::get<1>(event));Hash(std::get<2>(event));}
 Hash(live.size());events.clear();
}
static unsigned Sweep(CGUIBaseContainer& c,int start,int end,bool optimized)
{
 std::set<CGUIListItem*> removed;
 std::vector<int> masks;unsigned scanned=0;
 for(size_t i=0;i<c.m_items.size();++i)
 {
  const auto& item=c.m_items[i];masks.push_back(Mask(*item));
  if(Outside(static_cast<int>(i),start,end))
  {++scanned;if(Mask(*item))removed.insert(item.get());}
 }
 calls=0;c.FreeMemory(start,end);
 Check(calls==(optimized?removed.size():scanned),"only populated offscreen owners need calls");
 for(size_t i=0;i<c.m_items.size();++i)
  Check(Mask(*c.m_items[i])==(removed.count(c.m_items[i].get())?0:masks[i]),"inclusive/wrapped/clamped keep ownership matches oracle");
 for(const auto& event:events)if(std::get<0>(event)==1)Check(!std::get<2>(event),"container preserves deferred release argument");
 HashState(c);return scanned;
}
static void FinishCase()
{Check(live.empty(),"all fixture resources released");for(const auto& e:events){Hash(std::get<0>(e));Hash(std::get<1>(e));Hash(std::get<2>(e));}events.clear();++cases;}
static void RangeCase(int count,int start,int end,unsigned seed,bool alias,bool optimized)
{
 {
  CGUIBaseContainer c(count);
  if(alias&&count>3)c.m_items[3]=c.m_items[0];
  std::set<CGUIListItem*> initialized;
  for(const auto& item:c.m_items)if(initialized.insert(item.get()).second)Attach(*item,(item->id+seed)%4);
  Sweep(c,start,end,optimized);Sweep(c,start,end,optimized);
 }
 FinishCase();
}
static void RecreatedAndWarm(bool optimized)
{
 {
  CGUIBaseContainer c(522);
  for(int i=8;i<=20;++i)Attach(*c.m_items[i],3);
  unsigned sum=0;
  for(unsigned frame=0;frame<10;++frame){Sweep(c,8,20,optimized);sum+=calls;}
  Check(sum==(optimized?0u:5090u),"522-item ten-frame warm empty call count");
  // Another owner can create fresh layouts outside an unchanged keep range.
  Attach(*c.m_items[500],2,1);Sweep(c,8,20,optimized);
  Check(!c.m_items[500]->m_focusedLayout,"focused-only layout from another owner freed next frame");
  Attach(*c.m_items[519],1,2);Sweep(c,8,20,optimized);
  Check(!c.m_items[519]->m_layout,"new regular layout freed under identical range");
  std::cout<<"warm_frames=10 offscreen_per_frame=509 empty_FreeMemory_calls="<<sum<<'\n';
 }
 FinishCase();
}
static void Reentrant(bool optimized)
{
 {
  CGUIBaseContainer c(10);Attach(*c.m_items[1],1,3);bool fired=false;
  callback=[&](int id)
  {
   if(id==300011&&!fired)
   {fired=true;Attach(*c.m_items[0],2,4);Attach(*c.m_items[8],1,5);}
  };
  calls=0;c.FreeMemory(-2,-1);
  Check(fired&&Mask(*c.m_items[0])==2&&Mask(*c.m_items[8])==0,"same scan sees later reentrant owner, earlier owner waits next frame");
  Check(calls==(optimized?2u:10u),"reentrant call count");HashState(c);
  callback={};Sweep(c,-2,-1,optimized);
 }
 FinishCase();
}
static void DirectImmediate()
{
 {
  CGUIListItem item(1000);Attach(item,3,6);events.clear();item.FreeMemory(true);
  Check(!Mask(item),"direct immediate frees both layouts");
  Check(events.size()==6&&events[0]==std::make_tuple(1,610001,true)&&events[3]==std::make_tuple(1,610002,true),"normal-before-focused release/destruction and immediate flag retained");
 }
 FinishCase();
}
int main(int argc,char** argv)
{
 Check(argc==2,"optimized mode required");const bool optimized=std::stoi(argv[1])!=0;
 for(int count=0;count<=12;++count)for(int start=-3;start<=count+3;++start)
  for(int end=-3;end<=count+3;++end)for(unsigned seed=0;seed<4;++seed)
   RangeCase(count,start,end,seed,false,optimized);
 for(int start=-2;start<=8;++start)for(int end=-2;end<=8;++end)
  for(unsigned seed=0;seed<4;++seed)RangeCase(6,start,end,seed,true,optimized);
 RecreatedAndWarm(optimized);Reentrant(optimized);DirectImmediate();
 std::cout<<"cases="<<cases<<" checks="<<checks<<" digest="<<digest<<'\n';
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--baseline-ref', default=BASELINE)
    parser.add_argument('--cxx', default=os.environ.get('CXX', 'c++'))
    parser.add_argument('--sanitize', action='store_true')
    parser.add_argument('--out', type=Path, help='new directory retaining fixtures, commands and results')
    args = parser.parse_args()
    read = lambda path: (args.repo / path).read_text()
    old = lambda path: subprocess.check_output(['git', 'show', args.baseline_ref + ':' + path], cwd=args.repo, text=True)
    container = method(read(CONTAINER), 'void CGUIBaseContainer::FreeMemory(')
    item = method(read(ITEM), 'void CGUIListItem::FreeMemory(')
    layout = method(read(LAYOUT), 'void CGUIListItemLayout::FreeResources(')
    baseline_container = method(old(CONTAINER), 'void CGUIBaseContainer::FreeMemory(')
    baseline_item = method(old(ITEM), 'void CGUIListItem::FreeMemory(')
    baseline_layout = method(old(LAYOUT), 'void CGUIListItemLayout::FreeResources(')
    predicate = method(read(HEADER), 'bool HasLayouts(')
    assert container.count('->HasLayouts()') == 3, 'three offscreen loops must use the predicate'
    assert item == baseline_item and layout == baseline_layout, 'resource release methods must remain unchanged'
    if args.out:
        args.out.mkdir(parents=True, exist_ok=False)
    context = contextlib.nullcontext(str(args.out)) if args.out else tempfile.TemporaryDirectory(prefix='gui-offscreen-memory-')
    with context as directory:
        work = Path(directory)
        receipts = {'baseline_ref': args.baseline_ref, 'sanitize': args.sanitize,
                    'source_sha256': {path: hashlib.sha256(read(path).encode()).hexdigest()
                                      for path in (CONTAINER, ITEM, HEADER, LAYOUT)}, 'runs': []}
        def run(label, candidate_container, candidate_item, candidate_predicate, optimized):
            source = FIXTURE.replace('@HAS_LAYOUTS@', candidate_predicate).replace('@ITEM_METHOD@', candidate_item.replace('CGUIListItem::FreeMemory(', 'CGUIListItem::FreeMemoryBody(', 1)).replace('@LAYOUT_METHOD@', layout).replace('@CONTAINER_METHOD@', candidate_container)
            path = work / (label + '.cpp'); path.write_text(source);binary = path.with_suffix('')
            command = shlex.split(args.cxx) + ['-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror', str(path), '-o', str(binary)]
            if args.sanitize: command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
            subprocess.run(command, check=True)
            # LSan is unavailable under this environment's ptrace; ownership is
            # checked explicitly. Preserve an explicit caller sanitizer policy.
            sanitizer = os.environ.get('ASAN_OPTIONS', 'detect_leaks=0')
            invocation = [str(binary), str(int(optimized))]
            result = subprocess.run(invocation, capture_output=True, text=True,
                                    env={**os.environ, 'ASAN_OPTIONS': sanitizer})
            path.with_suffix('.stdout').write_text(result.stdout)
            path.with_suffix('.stderr').write_text(result.stderr)
            receipts['runs'].append({'label': label, 'compile': command, 'run': invocation,
                                     'ASAN_OPTIONS': sanitizer, 'returncode': result.returncode})
            (work / 'results.json').write_text(json.dumps(receipts, indent=2) + '\n')
            return result
        original = run('baseline', baseline_container, baseline_item, predicate, False)
        actual = run('current', container, item, predicate, True)
        assert original.returncode == actual.returncode == 0, (original.stdout, original.stderr, actual.stdout, actual.stderr)
        expected = original.stdout.split('digest=')[1].strip()
        assert actual.stdout.split('digest=')[1].strip() == expected, ('resource behavior differs', original.stdout, actual.stdout)
        print('PASS baseline:', original.stdout.strip())
        print('PASS current:', actual.stdout.strip())
        mutations = {
            'focused-only-skipped': (container, item, predicate.replace('m_layout || m_focusedLayout', 'm_layout != nullptr')),
            'both-layouts-required': (container, item, predicate.replace('m_layout || m_focusedLayout', 'm_layout && m_focusedLayout')),
            'missed-offscreen-loop': (container.replace('if (m_items[i]->HasLayouts())', 'if (true)', 1), item, predicate),
            'keep-boundary-shift': (container.replace('keepEnd + 1', 'keepEnd + 2'), item, predicate),
            'focused-reset-omitted': (container, item.replace('m_focusedLayout.reset();', ''), predicate),
            'resource-release-omitted': (container, item.replace('m_layout->FreeResources(immediately);', ''), predicate),
            'same-range-cache': (container.replace('{', '{\n  if (cached && lastStart == keepStart && lastEnd == keepEnd) return;\n  cached = true; lastStart = keepStart; lastEnd = keepEnd;', 1), item, predicate),
        }
        for name, (changed_container, changed_item, changed_predicate) in mutations.items():
            assert (changed_container, changed_item, changed_predicate) != (container, item, predicate), ('ineffective mutation', name)
            result = run(name, changed_container, changed_item, changed_predicate, True)
            semantic_failure = result.returncode == 1 and result.stderr.startswith('FAIL ')
            digest_failure = result.returncode == 0 and 'digest=' in result.stdout and result.stdout.split('digest=')[1].strip() != expected
            assert semantic_failure or digest_failure, ('mutation survived or failed without a semantic witness', name, result.returncode, result.stdout, result.stderr)
            print('Rejected mutation:', name)
        receipts['pass'] = True
        (work / 'results.json').write_text(json.dumps(receipts, indent=2) + '\n')
    print('Limits: production FreeMemory/predicate bodies with owned layout/group payload fixtures; full scan retained. No actual graphics resources, concurrent writers, target runtime or measured frame-time gain.')


if __name__ == '__main__':
    main()
