#!/usr/bin/env python3
"""Exercise actual EGL context creation with recorded priority attributes.

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
         (i==attrs.end()?EGL_CONTEXT_PRIORITY_MEDIUM_IMG:i->second);''')
    source = h.replace(source, '  fixture::extensions = std::move(extensions);', r'''
  fixture::requests.clear();fixture::queries.clear();
  fixture::forceEqualPriority=fixture::failUploadQuery=fixture::failUploadCreate=false;
  fixture::extensions = std::move(extensions);''')
    source = h.replace(source, 'int main(){try {', TESTS + '\nint main(){try {\n      Priorities();')
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
        'no-upload-query': ('if (m_eglUploadContext != EGL_NO_CONTEXT &&',
                            'if (false && m_eglUploadContext != EGL_NO_CONTEXT &&',
                            'query actual upload hint once'),
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
