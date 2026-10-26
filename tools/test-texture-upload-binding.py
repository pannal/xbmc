#!/usr/bin/env python3
"""Replay actual async upload/SyncGPU/GUI bind methods with owned GL objects.

The fixture distinguishes shared texture object generations from reusable numeric
names, and bindings in primary versus worker contexts. Normal upload completion
must finish its work, release the worker binding before EGL release/publication,
leave primary bindings untouched and permit explicit GUI rebinding and eventual
render-context deletion. Cancellation and failure tests preserve the original
context-guard semantics. Exceptional uploads/sync are not claimed to clear their
bindings: the reclamation contract tested here is normal completion only.

No actual EGL/GL driver, GPU pixel readback, worker scheduling or device memory
measurement is executed. Pixels are owned fixture tags and deletion follows an
explicit model of the shared-object binding lifetime. No source is rewritten.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

FILES = ('xbmc/guilib/Texture.cpp', 'xbmc/guilib/TextureGL.cpp')
RETENTION_WITNESS = 'worker texture reclaimed after render-context deletion'


def function(source, signature):
    start = source.index(signature)
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"',
                    lambda match: ' ' * len(match.group()), source)
    opening = masked.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (masked[end] == '{') - (masked[end] == '}')
        end += 1
    return source[start:end]


PRELUDE = r'''
#include <algorithm>
#include <array>
#include <atomic>
#include <cstdio>
#include <cstdint>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
#include <utility>
using GLuint=unsigned int;using GLenum=unsigned int;
constexpr unsigned GL_TEXTURE_2D=0x0de1,GL_TEXTURE0=0x84c0;
static unsigned long checks=0,cases=0;
static void Check(bool value,const char* reason){++checks;if(!value)throw std::runtime_error(reason);}
namespace Model {
constexpr int NONE=-1,PRIMARY=0,WORKERS=3,UNITS=4;
struct Object{unsigned name,generation;int pixels;bool finished=false;};
struct Context{unsigned active=0;std::array<std::shared_ptr<Object>,UNITS>bindings{};
 std::vector<std::shared_ptr<Object>>pending;};
struct Event{std::string kind;int context;unsigned generation;
 bool operator==(const Event&b)const{return kind==b.kind&&context==b.context&&generation==b.generation;}};
static std::array<Context,WORKERS+1>contexts;
static std::map<unsigned,std::shared_ptr<Object>>names;
static std::vector<unsigned>freeNames;
static std::vector<Event>events;
static int current=NONE;static unsigned nextName=1,nextGeneration=1;
static bool throwSync=false;
static void Reset(){contexts={};names.clear();freeNames.clear();events.clear();current=NONE;
 nextName=nextGeneration=1;throwSync=false;}
static std::shared_ptr<Object>Create(int pixels){unsigned name;
 if(freeNames.empty())name=nextName++;else{name=freeNames.back();freeNames.pop_back();}
 auto obj=std::make_shared<Object>(Object{name,nextGeneration++,pixels,false});names.emplace(name,obj);return obj;}
static unsigned BoundGeneration(int ctx,unsigned unit){auto&o=contexts.at(ctx).bindings.at(unit);return o?o->generation:0;}
static void DeleteOnPrimary(unsigned name){Check(current==PRIMARY,"deletion uses render context");
 auto found=names.find(name);Check(found!=names.end(),"delete live numeric name");auto obj=found->second;
 for(auto&binding:contexts[PRIMARY].bindings)if(binding==obj)binding.reset();
 names.erase(found);freeNames.push_back(name);events.push_back({"delete",PRIMARY,obj->generation});}
static void MarkPublication(unsigned name){auto obj=names.at(name);Check(current==NONE,"publication follows context release");
 Check(obj->finished,"upload finish precedes publication");events.push_back({"publish",current,obj->generation});}
}
void glActiveTexture(unsigned unit){Check(Model::current!=Model::NONE,"active texture has context");
 Check(unit>=GL_TEXTURE0&&unit<GL_TEXTURE0+Model::UNITS,"valid active unit");Model::contexts[Model::current].active=unit-GL_TEXTURE0;}
void glBindTexture(unsigned target,unsigned name){
 Check(Model::current!=Model::NONE,"unbind executes before EGL release");Check(target==GL_TEXTURE_2D,"2D texture target");
 auto&context=Model::contexts.at(Model::current);
 if(name){context.bindings[context.active]=Model::names.at(name);Model::events.push_back({"bind",Model::current,context.bindings[context.active]->generation});}
 else{unsigned generation=Model::BoundGeneration(Model::current,context.active);context.bindings[context.active].reset();Model::events.push_back({"unbind",Model::current,generation});}}
void glFinish(){Check(Model::current>Model::PRIMARY,"finish uses upload context");
 Model::events.push_back({"finish",Model::current,0});if(Model::throwSync)throw std::runtime_error("sync sentinel");
 for(auto&obj:Model::contexts[Model::current].pending){obj->finished=true;}
 Model::contexts[Model::current].pending.clear();}
// Fault control: issue the cleanup in the wrong shared context, then return to
// the worker so the primary-binding assertion can observe the actual damage.
static void WrongContextUnbind(){int worker=Model::current;Model::current=Model::PRIMARY;
 glBindTexture(GL_TEXTURE_2D,0);Model::current=worker;}
struct Window{
 bool bindResult=true,releaseResult=true,throwRelease=false;int worker=1;
 unsigned hasCalls=0,bindCalls=0,releaseCalls=0;bool leased=false;
 std::atomic_bool*cancelAfterBind=nullptr;
 bool HasContext(){++hasCalls;return Model::current==Model::PRIMARY;}
 bool BindTextureUploadContext(const std::atomic_bool*){++bindCalls;if(!bindResult)return false;
 Check(!leased&&Model::current==Model::NONE,"exclusive upload lease");leased=true;Model::current=worker;
 Model::events.push_back({"acquire",worker,0});if(cancelAfterBind)cancelAfterBind->store(true);return true;}
 bool UnbindTextureUploadContext(){++releaseCalls;Check(leased,"release owns lease");leased=false;
 Model::events.push_back({"release",Model::current,0});Model::current=Model::NONE;
 if(throwRelease)throw std::runtime_error("release sentinel");
 return releaseResult;}
};
using CWinSystemBase=Window;
static Window window;static bool windowPresent=true;
struct CServiceBroker{static Window*GetWinSystem(){return windowPresent?&window:nullptr;}};
class CTexture{public:virtual~CTexture()=default;
 void LoadToGPUAsync(const std::atomic_bool*cancelled=nullptr);virtual void LoadToGPU()=0;virtual void SyncGPU()=0;};
class CGLTexture:public CTexture{public:
 unsigned m_texture=0;int pixels=0;bool throwUpload=false;unsigned loadCalls=0;
 explicit CGLTexture(int data):pixels(data){}
 void LoadToGPU()override{++loadCalls;Check(Model::current>Model::PRIMARY,"upload uses worker context");
  if(!m_texture)m_texture=Model::Create(pixels)->name;
  glBindTexture(GL_TEXTURE_2D,m_texture);auto obj=Model::names.at(m_texture);
  Model::contexts[Model::current].pending.push_back(obj);Model::events.push_back({"upload",Model::current,obj->generation});
  if(throwUpload)throw std::runtime_error("upload sentinel");}
 void SyncGPU()override;void BindToUnit(unsigned int unit);
};
'''

TESTS = r'''
static void Reset(){Model::Reset();window=Window{};windowPresent=true;}
static std::size_t EventIndex(const char*kind){for(std::size_t i=0;i<Model::events.size();++i){
 if(Model::events[i].kind==kind)return i;}
 return Model::events.size();}
static void ModelSanity(){Reset();Model::current=1;auto old=Model::Create(11);unsigned name=old->name,generation=old->generation;
 std::weak_ptr<Model::Object>oldWeak=old;glBindTexture(GL_TEXTURE_2D,name);old.reset();
 Model::current=Model::PRIMARY;Model::DeleteOnPrimary(name);Check(!oldWeak.expired(),"model retains other-context binding");
 auto replacement=Model::Create(22);Check(replacement->name==name&&replacement->generation!=generation,"numeric reuse has new generation");
 std::weak_ptr<Model::Object>newWeak=replacement;glBindTexture(GL_TEXTURE_2D,name);
 Check(Model::contexts[0].bindings[0]->pixels==22&&Model::contexts[1].bindings[0]->pixels==11,"reuse keeps distinct object pixels");
 Model::current=1;glBindTexture(GL_TEXTURE_2D,0);Check(oldWeak.expired(),"model final binding releases old generation");
 Model::current=0;replacement.reset();Model::DeleteOnPrimary(name);Check(newWeak.expired(),"new generation separately reclaimed");++cases;}
static void NormalUploads(){Reset();auto primary=Model::Create(-1);primary->finished=true;Model::current=0;glBindTexture(GL_TEXTURE_2D,primary->name);
 const unsigned sentinel=primary->generation;primary.reset();unsigned previousName=0,previousGeneration=0;
 for(int i=0;i<24;++i){
  window.worker=1+i%Model::WORKERS;unsigned workerUnit=(i/Model::WORKERS)%Model::UNITS;
  Model::contexts[window.worker].active=workerUnit;Model::events.clear();Model::current=Model::NONE;
  CGLTexture texture(100+i);texture.LoadToGPUAsync();
  Check(window.releaseCalls==static_cast<unsigned>(i+1)&&!window.leased&&Model::current==Model::NONE,"normal lease released once");
  Check(Model::BoundGeneration(0,0)==sentinel,"primary bindings preserved");
  auto obj=Model::names.at(texture.m_texture);std::weak_ptr<Model::Object>weak=obj;
  unsigned generation=obj->generation;obj.reset();Model::MarkPublication(texture.m_texture);
  Check(EventIndex("upload")<EventIndex("finish")&&EventIndex("finish")<EventIndex("release")&&EventIndex("release")<EventIndex("publish"),"normal upload finish release publication order");
  if(previousName)Check(texture.m_texture==previousName&&generation!=previousGeneration,"repeated name reuse changes generation");
  previousName=texture.m_texture;previousGeneration=generation;
  Model::current=0;unsigned guiUnit=1+i%(Model::UNITS-1);texture.BindToUnit(guiUnit);
  auto drawn=Model::contexts[0].bindings[guiUnit];
  Check(drawn&&drawn->generation==generation&&drawn->pixels==100+i,"GUI explicit rebind selects published pixels");drawn.reset();
  Model::DeleteOnPrimary(texture.m_texture);
  Check(weak.expired(),"worker texture reclaimed after render-context deletion");
  Check(Model::BoundGeneration(0,0)==sentinel,"primary bindings preserved");++cases;
 }
}
static void EarlyReturns(){
 for(int kind=0;kind<5;++kind){Reset();CGLTexture texture(7);std::atomic_bool cancelled{false};
  if(kind==0)cancelled=true;
  if(kind==1)windowPresent=false;
  if(kind==2)Model::current=0;
  if(kind==3)window.bindResult=false;
  if(kind==4)window.cancelAfterBind=&cancelled;
  texture.LoadToGPUAsync(&cancelled);
  Check(texture.loadCalls==0&&texture.m_texture==0,"early exit performs no upload");
  Check(EventIndex("finish")==Model::events.size(),"early exit performs no finish");
  Check(window.releaseCalls==(kind==4?1u:0u)&&!window.leased,"only acquired cancellation lease released");
  Check(window.bindCalls==(kind>=3?1u:0u),"early return bind count");
  if(kind==0)Check(window.hasCalls==0,"pre-cancel avoids window inspection");
  ++cases;
 }
}
static void Failures(){
 // Normal upload reaches SyncGPU cleanup even when EGL release later fails.
 Reset();window.releaseResult=false;CGLTexture releaseFailure(9);bool caught=false;
 try{releaseFailure.LoadToGPUAsync();}catch(const std::runtime_error&e){caught=std::string(e.what())=="Texture upload context could not be released";}
 Check(caught&&window.releaseCalls==1&&!window.leased&&Model::current==Model::NONE,"normal release failure surfaces once");
 std::weak_ptr<Model::Object>weak=Model::names.at(releaseFailure.m_texture);Model::current=0;Model::DeleteOnPrimary(releaseFailure.m_texture);
 Check(weak.expired(),"normal sync cleanup survives release failure");++cases;
 for(bool upload:{false,true})for(bool badRelease:{false,true}){Reset();CGLTexture texture(31);texture.throwUpload=upload;Model::throwSync=!upload;window.throwRelease=badRelease;bool preserved=false;
  try{texture.LoadToGPUAsync();}catch(const std::runtime_error&e){preserved=std::string(e.what())==(upload?"upload sentinel":"sync sentinel");}
  Check(preserved&&window.releaseCalls==1&&!window.leased&&Model::current==Model::NONE,"exception guard preserves original failure and releases lease");
  Check(EventIndex("publish")==Model::events.size(),"exception never reaches publication");
  // No assertion of binding/object reclamation on exceptional paths: SyncGPU's
  // normal cleanup may not have executed. Context teardown clears the fixture.
  ++cases;
 }
}
int main(){try{ModelSanity();NormalUploads();EarlyReturns();Failures();
 std::printf("PASS %lu cases %lu checks; normal binding reclamation and GUI rebind; cancellation/exception guard limits explicit\n",cases,checks);return 0;
}catch(const std::exception&e){std::fprintf(stderr,"FAIL %s\n",e.what());return 1;}}
'''


def methods(sources):
    texture, gl = [sources[p] for p in FILES]
    return '\n\n'.join((function(texture, 'void CTexture::LoadToGPUAsync('),
                         function(gl, 'void CGLTexture::SyncGPU()'),
                         function(gl, 'void CGLTexture::BindToUnit(')))


def mutate_methods(code):
    unbind = 'glBindTexture(GL_TEXTURE_2D, 0);'
    assert code.count(unbind) == 1, 'Expected exactly one normal worker unbind'
    assert code.count('glFinish();') == 1
    yield 'worker-unbind-omitted', code.replace(unbind, '(void)0;', 1), RETENTION_WITNESS
    # Move cleanup beyond Release(), while leaving the real guard intact.
    late = code.replace(unbind, '(void)0;', 1)
    release_throw = 'throw std::runtime_error("Texture upload context could not be released");'
    assert late.count(release_throw) == 1
    late = late.replace(release_throw, release_throw + '\n  glBindTexture(GL_TEXTURE_2D, 0);', 1)
    yield 'unbind-after-egl-release', late, 'unbind executes before EGL release'
    yield 'cleanup-wrong-context', code.replace(unbind, 'WrongContextUnbind();', 1), 'primary bindings preserved'
    yield 'finish-omitted', code.replace('glFinish();', '(void)0;', 1), 'upload finish precedes publication'
    gui_bind = 'glBindTexture(GL_TEXTURE_2D, m_texture);'
    assert code.count(gui_bind) == 1
    yield 'gui-rebind-omitted', code.replace(gui_bind, '(void)0;', 1), 'GUI explicit rebind selects published pixels'


def semantic_failure(run, witness):
    return (run.returncode == 1 and run.stderr == 'FAIL ' + witness + '\n' and
            not any(x in run.stdout + run.stderr for x in (
                'AddressSanitizer', 'UndefinedBehaviorSanitizer', 'LeakSanitizer',
                'runtime error:', 'DEADLYSIGNAL', 'Sanitizer CHECK failed')))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--sanitize', action='store_true')
    parser.add_argument('--expect-baseline-failure', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    out = args.out or Path(tempfile.mkdtemp(prefix='texture-upload-binding-'))
    out.mkdir(parents=True, exist_ok=True)
    sources = {p: (root / p).read_text() for p in FILES}
    code = methods(sources)
    controls = [] if args.expect_baseline_failure else list(mutate_methods(code))
    records = []
    env = {**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0', 'UBSAN_OPTIONS': 'halt_on_error=1:print_stacktrace=1'}
    for name, body, witness in [('current', code, RETENTION_WITNESS if args.expect_baseline_failure else None), *controls]:
        cpp, exe = out / (name + '.cpp'), out / name
        cpp.write_text(PRELUDE + body + TESTS)
        command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                   '-Wno-unused-function', '-fno-omit-frame-pointer']
        if args.sanitize:
            command += ['-fsanitize=address,undefined', '-fno-sanitize-recover=undefined', '-fno-pie', '-no-pie']
        command += [str(cpp), '-o', str(exe)]
        built = subprocess.run(command, capture_output=True, text=True, timeout=120)
        (out / (name + '-compile.txt')).write_text(built.stdout + built.stderr)
        assert built.returncode == 0, name + ' compile failed; see ' + str(out)
        run = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=30)
        (out / (name + '-stdout.txt')).write_text(run.stdout)
        (out / (name + '-stderr.txt')).write_text(run.stderr)
        accepted = semantic_failure(run, witness) if witness else run.returncode == 0 and not run.stderr
        records.append({'name': name, 'compile_argv': command, 'compile_exit': built.returncode,
                        'generated_sha256': hashlib.sha256(cpp.read_bytes()).hexdigest(),
                        'exit': run.returncode, 'witness': witness, 'accepted': accepted})
        assert accepted, name + ' did not produce expected semantic result; see ' + str(out)
    receipt = {'source_hashes': {p: hashlib.sha256(sources[p].encode()).hexdigest() for p in FILES},
               'tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               'baseline_failure_expected': args.expect_baseline_failure,
               'sanitize': args.sanitize, 'limits': __doc__, 'results': records}
    (out / 'results.json').write_text(json.dumps(receipt, indent=2) + '\n')
    if args.expect_baseline_failure:
        print('Reproduced baseline: FAIL ' + RETENTION_WITNESS)
    else:
        print((out / 'current-stdout.txt').read_text(), end='')
        print('PASS %d strict semantic controls; receipts %s' % (len(controls), out))


if __name__ == '__main__':
    main()
