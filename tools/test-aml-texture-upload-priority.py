#!/usr/bin/env python3
"""Exercise actual EGL context creation with recorded priority attributes.

Also the second, high-priority upload context: created only when the driver
grants HIGH, bound only while the window system flags it, retired when it
cannot be bound and on destruction.

Extends the existing recording driver and retains its lifecycle cases. Priorities
are simulated hints; this does not establish driver scheduling or performance.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
spec = importlib.util.spec_from_file_location('upload_hardening', HERE / 'test-aml-texture-upload-hardening.py')
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)
d = h.donor
PATHS = [d.EGL_SOURCE, d.EGL_HEADER, d.AML_SOURCE, d.TEXTURE_SOURCE, h.PUBLICATION]
BASELINE = '42e507705412e947efd3d1df9790c46bdc94aab8'

STATE = r'''
struct ContextRequest
{
 EGLConfig config;
 EGLContext share;
 std::map<EGLint,EGLint> attributes;
};
std::map<EGLContext,ContextRequest> requests;
std::map<EGLContext,unsigned> queries;
bool forceEqualPriority=false,failUploadQuery=false,failUploadCreate=false;
bool denyHigh=false,failHighBind=false,highRequiresPbuffer=false;
bool mediumSurfacelessOnly=false,denyRendererHigh=false,failRendererQuery=false;
bool singlePbuffer=false;
std::atomic_bool* frameEligibilityOnPbuffer=nullptr;
bool RequestedHighUpload(EGLContext context)
{
 const auto r=requests.find(context);
 if(r==requests.end()||r->second.share==EGL_NO_CONTEXT)return false;
 const auto i=r->second.attributes.find(EGL_CONTEXT_PRIORITY_LEVEL_IMG);
 return i!=r->second.attributes.end()&&i->second==EGL_CONTEXT_PRIORITY_HIGH_IMG;
}
'''

TESTS = r'''
static EGLint Attribute(EGLContext context,EGLint key,EGLint fallback)
{
 const auto& attrs=fixture::requests.at(context).attributes;
 const auto i=attrs.find(key);return i==attrs.end()?fallback:i->second;
}
static void Priority(bool extension,bool explicitPriority,bool overridePriority,
                     bool failQuery,bool failCreate)
{
 Reset(extension?std::set<std::string>{"EGL_IMG_context_priority","EGL_KHR_surfaceless_context"}:
                 std::set<std::string>{"EGL_KHR_surfaceless_context"},false);
 fixture::forceEqualPriority=overridePriority;
 fixture::failUploadQuery=failQuery;fixture::failUploadCreate=failCreate;
 CWinSystemAmlogicGLESContext win;g_winSystem=&win;
 CEGLAttributesVec attrs;
 attrs.Add({{0x3098,2},{EGL_CONTEXT_FLAGS_KHR,EGL_CONTEXT_OPENGL_DEBUG_BIT_KHR}});
 if(explicitPriority)attrs.Add({{EGL_CONTEXT_PRIORITY_LEVEL_IMG,0x3103}});
 Check(win.m_pGLContext.CreateContext(attrs),"priority: primary creation remains usable");
 const auto render=win.m_pGLContext.m_eglContext,upload=win.m_pGLContext.m_eglUploadContext;
 Check(Attribute(render,EGL_CONTEXT_PRIORITY_LEVEL_IMG,EGL_CONTEXT_PRIORITY_MEDIUM_IMG)==
       (extension?EGL_CONTEXT_PRIORITY_HIGH_IMG:EGL_CONTEXT_PRIORITY_MEDIUM_IMG),
       "priority: renderer retains automatic HIGH only with extension");
 Check(fixture::queries[render]==(extension?1u:0u),"priority: primary query unchanged");
 if(failCreate)
 {
  Check(upload==EGL_NO_CONTEXT,"priority: failed upload creation retains fallback");
  CTexture texture;UploadOnLoader(texture);
  Check(!texture.uploaded,"priority: unavailable context skips worker upload");
  eglMakeCurrent(win.m_pGLContext.m_eglDisplay,EGL_NO_SURFACE,EGL_NO_SURFACE,render);
  texture.LoadToGPU();Check(texture.uploaded,"priority: render-thread fallback still works");
  eglMakeCurrent(win.m_pGLContext.m_eglDisplay,EGL_NO_SURFACE,EGL_NO_SURFACE,EGL_NO_CONTEXT);
 }
 else
 {
  Check(Attribute(upload,EGL_CONTEXT_PRIORITY_LEVEL_IMG,EGL_CONTEXT_PRIORITY_MEDIUM_IMG)==
        (explicitPriority?0x3103:EGL_CONTEXT_PRIORITY_MEDIUM_IMG),
        "priority: upload must not inherit automatic renderer HIGH");
  Check(fixture::requests.at(upload).share==render &&
        fixture::requests.at(upload).config==win.m_pGLContext.m_eglConfig,
        "priority: sharing and upload config preserved");
  for(auto context:{render,upload})
  {
   Check(Attribute(context,0x3098,1)==2,"priority: GLES version preserved");
   Check(Attribute(context,EGL_CONTEXT_FLAGS_KHR,0)==EGL_CONTEXT_OPENGL_DEBUG_BIT_KHR,
         "priority: caller context flags preserved");
  }
  Check(fixture::queries[upload]==(extension?1u:0u),"priority: query actual upload hint once");
  // Assignment/query failure is advisory and must not disable valid uploads.
  CTexture texture;UploadOnLoader(texture);
  Check(texture.uploaded && texture.synced,"priority: upload completion retained");
 }
 win.m_pGLContext.DestroyContext();
 Check(!LiveResources(),"priority: contexts retired");
 Violations("priority");
}
static void HighUpload(bool extension,bool denyHigh,bool failHighBind,
                       bool highRequiresPbuffer=false,bool frameEndsOnPbuffer=false)
{
 Reset(extension?std::set<std::string>{"EGL_IMG_context_priority","EGL_KHR_surfaceless_context"}:
                 std::set<std::string>{"EGL_KHR_surfaceless_context"},highRequiresPbuffer || failHighBind);
 fixture::denyHigh=denyHigh;fixture::failHighBind=failHighBind;
 fixture::highRequiresPbuffer=highRequiresPbuffer;
 fixture::mediumSurfacelessOnly=highRequiresPbuffer||failHighBind;
 CWinSystemAmlogicGLESContext win;g_winSystem=&win;
 auto& egl=win.m_pGLContext;
 if(frameEndsOnPbuffer)fixture::frameEligibilityOnPbuffer=&egl.m_textureUploadHighPriority;
 CEGLAttributesVec attrs;attrs.Add({{0x3098,2}});
 Check(egl.CreateContext(attrs),"high: primary creation remains usable");
 const auto render=egl.m_eglContext,upload=egl.m_eglUploadContext,high=egl.m_eglUploadContextHigh;
 const bool granted=extension&&!denyHigh;
 if(!granted)
  Check(high==EGL_NO_CONTEXT,"high: no high-priority upload context without a granted HIGH");
 else
 {
  Check(high!=EGL_NO_CONTEXT,"high: high-priority upload context created");
  Check(Attribute(high,EGL_CONTEXT_PRIORITY_LEVEL_IMG,EGL_CONTEXT_PRIORITY_MEDIUM_IMG)==EGL_CONTEXT_PRIORITY_HIGH_IMG,
        "high: requested at HIGH");
  Check(fixture::requests.at(high).share==render&&fixture::requests.at(high).config==egl.m_eglConfig,
        "high: shares with the renderer, upload config");
  Check(Attribute(high,0x3098,1)==2,"high: GLES version preserved");
 }
 Check(Attribute(upload,EGL_CONTEXT_PRIORITY_LEVEL_IMG,EGL_CONTEXT_PRIORITY_MEDIUM_IMG)==EGL_CONTEXT_PRIORITY_MEDIUM_IMG,
       "high: ordinary upload context stays medium");
 CTexture a;UploadOnLoader(a);
 Check(a.uploaded&&a.uploadContext==upload,"high: medium upload context while not flagged");
 egl.SetTextureUploadHighPriority(true);
 const bool expectHigh=granted&&!failHighBind;
 CTexture b;UploadOnLoader(b);
 Check(b.uploaded&&b.uploadContext==(expectHigh&&!frameEndsOnPbuffer?high:upload),
       "high: high-priority context bound while eligible, including pbuffer retry");
 if(granted&&failHighBind)
 {
  Check(egl.m_eglUploadContextHigh==EGL_NO_CONTEXT,"high: unbindable high context retired");
  Check(egl.HasTextureUploadContext()&&egl.m_eglUploadSurface==EGL_NO_SURFACE,
        "high: failed retry preserves surfaceless medium uploads");
  Check(fixture::pbufferCalls==1,"high: failed retry actually tried a pbuffer");
 }
 if(frameEndsOnPbuffer)
  Check(egl.m_eglUploadContextHigh==high,"high: clearing eligibility keeps the high context");
 egl.SetTextureUploadHighPriority(true);
 CTexture c;UploadOnLoader(c);
 Check(c.uploaded&&c.uploadContext==(expectHigh?high:upload),"high: a later eligible frame can use HIGH");
 egl.SetTextureUploadHighPriority(false);
 CTexture idle;UploadOnLoader(idle);
 Check(idle.uploaded&&idle.uploadContext==upload,"high: clearing eligibility returns to medium");
 Check(egl.m_eglUploadSurface==EGL_NO_SURFACE,"high: retry leaves the medium surface unchanged");
 egl.DestroyContext();
 Check(!LiveResources(),"high: contexts retired");
 Violations("high");
}
static void RendererGrant(bool queryFails)
{
 Reset({"EGL_IMG_context_priority","EGL_KHR_surfaceless_context"},true);
 fixture::denyRendererHigh=!queryFails;
 fixture::failRendererQuery=queryFails;
 CWinSystemAmlogicGLESContext win;g_winSystem=&win;
 auto& egl=win.m_pGLContext;
 CEGLAttributesVec attrs;attrs.Add({{0x3098,2}});
 Check(egl.CreateContext(attrs),"renderer grant: ordinary rendering is still available");
 Check(egl.m_eglUploadContextHigh==EGL_NO_CONTEXT && fixture::requests.size()==2,
       "renderer grant: only a confirmed HIGH renderer may have a HIGH upload context");
 egl.SetTextureUploadHighPriority(true);
 CTexture texture;UploadOnLoader(texture);
 Check(texture.uploaded&&texture.uploadContext==egl.m_eglUploadContext,
       "renderer grant: eligible frames retain medium uploads");
 egl.DestroyContext();
 Check(!LiveResources(),"renderer grant: contexts retired");
 Violations("renderer grant");
}
enum class HighPbufferOutcome { BindFails, FrameEnds, Binds };

static void SinglePbuffer(HighPbufferOutcome outcome, const char* fallbackWitness)
{
 Reset({"EGL_IMG_context_priority","EGL_KHR_surfaceless_context"},true);
 fixture::singlePbuffer=true;
 fixture::failSurfaceless=true;
 fixture::failHighBind=outcome==HighPbufferOutcome::BindFails;
 CWinSystemAmlogicGLESContext win;g_winSystem=&win;
 auto& egl=win.m_pGLContext;
 if(outcome==HighPbufferOutcome::FrameEnds)
  fixture::frameEligibilityOnPbuffer=&egl.m_textureUploadHighPriority;
 Check(egl.CreateContext({}),"one pbuffer: context creation succeeds");
 const auto medium=egl.m_eglUploadContext,high=egl.m_eglUploadContextHigh;
 Check(high!=EGL_NO_CONTEXT,"one pbuffer: HIGH context created");
 egl.SetTextureUploadHighPriority(true);
 CTexture first;UploadOnLoader(first);
 if(outcome==HighPbufferOutcome::Binds)
 {
  Check(first.uploaded&&first.uploadContext==high&&egl.m_eglUploadSurfaceHigh!=EGL_NO_SURFACE,
        "one pbuffer: HIGH used and kept the pbuffer while eligible");
  egl.SetTextureUploadHighPriority(false);
  CTexture fallback;UploadOnLoader(fallback);
  Check(fallback.uploaded&&fallback.uploadContext==medium,fallbackWitness);
 }
 else
  Check(first.uploaded&&first.uploadContext==medium,fallbackWitness);

 Check(egl.m_eglUploadSurface!=EGL_NO_SURFACE&&egl.m_eglUploadSurfaceHigh==EGL_NO_SURFACE,
       "one pbuffer: the medium context owns the only pbuffer");
 Check(fixture::pbufferCalls==2 && fixture::surfaces.size()==1,
       "one pbuffer: allocation tried once per context with one live surface");
 Check(egl.m_eglUploadContextHigh==(outcome==HighPbufferOutcome::BindFails?EGL_NO_CONTEXT:high),
       "one pbuffer: only a failed HIGH bind retires the context");
 egl.SetTextureUploadHighPriority(true);
 CTexture later;UploadOnLoader(later);
 Check(later.uploaded&&later.uploadContext==medium,
       "one pbuffer: subsequent eligible uploads still use medium");
 egl.DestroyContext();
 Check(!LiveResources(),"one pbuffer: contexts and surfaces retired");
 Violations("one pbuffer");
}

static void Highs()
{
 HighUpload(true,false,false);
 HighUpload(false,false,false);
 HighUpload(true,true,false);
 HighUpload(true,false,true);
 HighUpload(true,false,false,true);
 HighUpload(true,false,false,true,true);
 RendererGrant(false);
 RendererGrant(true);
 SinglePbuffer(HighPbufferOutcome::BindFails,"one pbuffer: failed HIGH bind preserves medium uploads");
 SinglePbuffer(HighPbufferOutcome::FrameEnds,"one pbuffer: clearing eligibility preserves medium uploads");
 SinglePbuffer(HighPbufferOutcome::Binds,"one pbuffer: medium can acquire a pbuffer after HIGH use");
}
static void Priorities()
{
 Priority(true,false,false,false,false);
 Priority(false,false,false,false,false);
 Priority(true,true,false,false,false);
 Priority(true,false,true,false,false);
 Priority(true,false,false,true,false);
 Priority(true,false,false,false,true);
}
'''


def generate(files):
    source = h.build_source(files)
    source = h.replace(source, 'bool pbuffer = true;', 'bool pbuffer = true;\n' + STATE)
    source = h.replace(source, 'EGLContext eglCreateContext(EGLDisplay, EGLConfig, EGLContext share, const EGLint*)',
                       'EGLContext eglCreateContext(EGLDisplay, EGLConfig config, EGLContext share, const EGLint* attributes)')
    source = h.replace(source, '  EGLContext context = reinterpret_cast<EGLContext>(fixture::next += 0x10);', r'''
  if(share!=EGL_NO_CONTEXT && fixture::failUploadCreate)return EGL_NO_CONTEXT;
  EGLContext context = reinterpret_cast<EGLContext>(fixture::next += 0x10);
  fixture::ContextRequest request{config,share,{}};
  if(attributes)for(const EGLint* p=attributes;*p!=EGL_NONE;p+=2)
    request.attributes.emplace(p[0],p[1]); // EGL fixture uses the first occurrence.
  fixture::requests.emplace(context,std::move(request));''')
    source = h.replace(source, 'EGLBoolean eglQueryContext(EGLDisplay, EGLContext, EGLint, EGLint* value)\n{\n  *value = EGL_CONTEXT_PRIORITY_HIGH_IMG;', r'''
EGLBoolean eglQueryContext(EGLDisplay, EGLContext context, EGLint, EGLint* value)
{
  ++fixture::queries[context];
  if(fixture::failUploadQuery && fixture::requests.at(context).share!=EGL_NO_CONTEXT)return EGL_FALSE;
  const auto& attrs=fixture::requests.at(context).attributes;
  const auto i=attrs.find(EGL_CONTEXT_PRIORITY_LEVEL_IMG);
  *value=fixture::forceEqualPriority?EGL_CONTEXT_PRIORITY_HIGH_IMG:
         (i==attrs.end()?EGL_CONTEXT_PRIORITY_MEDIUM_IMG:i->second);
  if(fixture::requests.at(context).share==EGL_NO_CONTEXT)
  {
    if(fixture::failRendererQuery)return EGL_FALSE;
    if(fixture::denyRendererHigh)*value=EGL_CONTEXT_PRIORITY_MEDIUM_IMG;
  }
  if(fixture::denyHigh&&fixture::RequestedHighUpload(context))*value=EGL_CONTEXT_PRIORITY_MEDIUM_IMG;''')
    source = h.replace(source, '  if (draw != read)\n    fixture::Violation("draw and read surfaces differ");', r'''
  if(fixture::RequestedHighUpload(context) &&
     (fixture::failHighBind || (fixture::highRequiresPbuffer && draw==EGL_NO_SURFACE)))
  {fixture::error=EGL_BAD_ACCESS;return EGL_FALSE;}
  const auto request=fixture::requests.find(context);
  if(fixture::mediumSurfacelessOnly && draw!=EGL_NO_SURFACE && request!=fixture::requests.end() &&
     request->second.share!=EGL_NO_CONTEXT && !fixture::RequestedHighUpload(context))
  {fixture::error=EGL_BAD_MATCH;return EGL_FALSE;}
  if (draw != read)
    fixture::Violation("draw and read surfaces differ");''')
    source = h.replace(source, '  fixture::extensions = std::move(extensions);', r'''
  fixture::requests.clear();fixture::queries.clear();
  fixture::forceEqualPriority=fixture::failUploadQuery=fixture::failUploadCreate=false;
  fixture::denyHigh=fixture::failHighBind=fixture::highRequiresPbuffer=false;
  fixture::mediumSurfacelessOnly=fixture::denyRendererHigh=fixture::failRendererQuery=false;
  fixture::singlePbuffer=false;
  fixture::frameEligibilityOnPbuffer=nullptr;
  fixture::extensions = std::move(extensions);''')
    source = h.replace(source, '  ++fixture::pbufferCalls;',
                       '  ++fixture::pbufferCalls;\n'
                       '  if(fixture::frameEligibilityOnPbuffer)fixture::frameEligibilityOnPbuffer->store(false);')
    source = h.replace(source, '  EGLSurface surface = reinterpret_cast<EGLSurface>(fixture::next += 0x10);', r'''
  if(fixture::singlePbuffer && !fixture::surfaces.empty())
  {
    fixture::error=0x3003; // EGL_BAD_ALLOC
    return EGL_NO_SURFACE;
  }
  EGLSurface surface = reinterpret_cast<EGLSurface>(fixture::next += 0x10);''')
    source = h.replace(source, 'int main(){try {', TESTS + '\nint main(){try {\n      Priorities();\n      Highs();')
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    files = {p: (ROOT / p).read_text() for p in PATHS}
    cases = {'actual': (files, None)}
    old = {p: subprocess.check_output(['git', 'show', f'{BASELINE}:{p}'], cwd=ROOT, text=True) for p in PATHS}
    cases['baseline'] = (old, 'upload must not inherit automatic renderer HIGH')
    mutations = {
        'shared-renderer-attributes': ('uploadContextAttribs.Get()', 'contextAttribs.Get()',
                                       'upload must not inherit automatic renderer HIGH'),
        'discard-upload-attributes': ('const CEGLAttributesVec uploadContextAttribs{contextAttribs};',
                                      'const CEGLAttributesVec uploadContextAttribs;', 'GLES version preserved'),
        'no-upload-query': ('if (m_eglUploadContext != EGL_NO_CONTEXT && supportsPriority)',
                            'if (false && m_eglUploadContext != EGL_NO_CONTEXT && supportsPriority)',
                            'query actual upload hint once'),
        'ignore-renderer-grant': ('if (m_eglUploadContext != EGL_NO_CONTEXT && rendererHigh)',
                                  'if (m_eglUploadContext != EGL_NO_CONTEXT && supportsPriority)',
                                  'renderer grant: only a confirmed HIGH renderer'),
        'overwrite-medium-surface': ('m_eglUploadSurfaceHigh = CreateTextureUploadPbuffer();',
                                     'm_eglUploadSurfaceHigh = m_eglUploadSurface = CreateTextureUploadPbuffer();',
                                     'failed retry preserves surfaceless medium uploads'),
        'hold-high-pbuffer': ('if ((retryMedium || retireHigh) && m_eglUploadSurfaceHigh != EGL_NO_SURFACE)',
                              'if (retireHigh && !retryMedium && m_eglUploadSurfaceHigh != EGL_NO_SURFACE)',
                              'one pbuffer: clearing eligibility preserves medium uploads'),
        'ignore-frame-eligibility': ('if (m_eglUploadContextHigh != EGL_NO_CONTEXT && allowHigh())',
                                     'if (m_eglUploadContextHigh != EGL_NO_CONTEXT)',
                                     'high: medium upload context while not flagged'),
        'skip-high-pbuffer-retry': ('bound = retry && bind(m_eglUploadContextHigh, m_eglUploadSurfaceHigh);', 'bound = false;',
                                    'including pbuffer retry'),
        'admit-after-frame-ended': ('      if (!retry || allowHigh())', '      if (true)', 'including pbuffer retry'),
        'keep-ungranted-high': ('         granted != EGL_CONTEXT_PRIORITY_HIGH_IMG))', '         false))',
                                'high: no high-priority upload context without a granted HIGH'),
        'leak-high-context': ('  if (m_eglUploadContextHigh != EGL_NO_CONTEXT)\n  {\n    eglDestroyContext(m_eglDisplay, m_eglUploadContextHigh);',
                              '  if (false)\n  {\n    eglDestroyContext(m_eglDisplay, m_eglUploadContextHigh);',
                              'high: contexts retired'),
        'keep-unbindable-high': ('#endif\n      eglDestroyContext(m_eglDisplay, m_eglUploadContextHigh);\n      m_eglUploadContextHigh = EGL_NO_CONTEXT;',
                                 '#endif', 'high: unbindable high context retired'),
    }
    for name, (before, after, witness) in mutations.items():
        changed = dict(files)
        changed[d.EGL_SOURCE] = h.replace(changed[d.EGL_SOURCE], before, after)
        cases[name] = (changed, witness)
    results = {}
    for name, (selected, witness) in cases.items():
        source = generate(selected)
        cpp, binary = args.output / (name + '.cpp'), args.output / name
        cpp.write_text(source)
        command = [os.environ.get('CXX', 'g++'), '-std=c++17', '-O1', '-g', '-Wall', '-Wextra', '-Werror',
                   '-Wno-unused-parameter', '-pthread', '-DHAS_LIBAMCODEC', str(cpp), '-o', str(binary)]
        if args.sanitize:
            command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
        subprocess.run(command, check=True)
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                             env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
        (args.output / (name + '.stdout')).write_text(run.stdout)
        (args.output / (name + '.stderr')).write_text(run.stderr)
        accepted = run.returncode == 0 if witness is None else run.returncode == 1 and witness in run.stderr
        results[name] = {'exit_code': run.returncode, 'expected_witness': witness, 'accepted': accepted,
                         'source_sha256': hashlib.sha256(source.encode()).hexdigest()}
        (args.output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
        if not accepted:
            raise RuntimeError(f'{name}: {run.stderr}')
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
