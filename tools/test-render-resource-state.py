#!/usr/bin/env python3
"""Host checks of render-target provenance and GLES texture namespace ownership.

Uses the real TextureResources/RenderResource headers and extracts production
base token methods and GLES admission/drain/close methods. The primary-context
query is a controlled stub; glDeleteTextures records calls. Surface/route changes
are simulated by the real invalidation method, not actual EGL lifecycle calls.
No driver, GPU completion, shared contexts or device playback is validated here.
Runs ASan/UBSan; all generated sources and executables are temporary.
"""
import os
from pathlib import Path
import re
import runpy
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
function = runpy.run_path(str(ROOT / 'tools/test-render-slot-publication.py'))['function']


def main():
    base = (ROOT / 'xbmc/rendering/RenderSystem.h').read_text()
    gles = (ROOT / 'xbmc/rendering/gles/RenderSystemGLES.cpp').read_text()
    methods = '\n'.join(function(base, signature) for signature in [
        'RenderTargetToken CaptureRenderTarget()',
        'bool IsRenderTargetCurrent(', 'void InvalidateRenderTarget()'])
    fields = '\n'.join(re.search(pattern, base).group() for pattern in [
        r'const std::shared_ptr<const uint8_t> m_renderTargetIdentity[^\n]+;',
        r'uint64_t m_renderTargetGeneration[^\n]+;'])
    source = PRELUDE.replace('@BASE_METHODS@', methods).replace('@BASE_FIELDS@', fields)
    for signature in ['bool CRenderSystemGLES::CanRender()',
                      'bool CRenderSystemGLES::IsTextureContextCurrent(',
                      'void CRenderSystemGLES::DrainTextureResources()',
                      'void CRenderSystemGLES::CloseTextureResources()']:
        source += '\n' + function(gles, signature)
    source += TESTS
    with tempfile.TemporaryDirectory(prefix='render-resource-state-') as temporary:
        out = Path(temporary)
        (out / 'test.cpp').write_text(source)
        subprocess.run([os.environ.get('CXX', 'g++'), '-std=c++17', '-Wall', '-Wextra',
                        '-Werror', '-pthread', '-fsanitize=address,undefined',
                        '-fno-omit-frame-pointer', '-I', str(ROOT / 'xbmc'),
                        str(out / 'test.cpp'), '-o', str(out / 'test')], check=True)
        subprocess.run([str(out / 'test')], check=True)
    print('Render resource state: PASS (production tokens/registry/GLES admission and deletion; '
          'ASan/UBSan; recording GL and stub context)')


PRELUDE = r'''
#include "rendering/RenderResource.h"
#include "rendering/gles/TextureResources.h"
#include <algorithm>
#include <cassert>
#include <memory>
#include <new>
#include <thread>
#include <type_traits>
#include <vector>
using GLuint = uint32_t;
struct Delete {GLuint name; int context; std::thread::id thread;};
std::vector<Delete> deletions;
const auto owner = std::this_thread::get_id();
int currentContext = 1;
void glDeleteTextures(int count, const GLuint* names)
{
  assert(std::this_thread::get_id() == owner);
  assert(currentContext != 0);
  for (int i = 0; i < count; ++i)
    deletions.push_back({names[i], currentContext, std::this_thread::get_id()});
}
class CRenderSystemBase
{
public:
  virtual ~CRenderSystemBase() = default;
  virtual bool CanRender() const {return m_bRenderCreated;}
  bool m_bRenderCreated{false};
  @BASE_METHODS@
  @BASE_FIELDS@
};
class CRenderSystemGLES : public CRenderSystemBase
{
public:
  bool IsPrimaryContextCurrent() const {return currentContext == context;}
  bool CanRender() const override;
  bool IsTextureContextCurrent(const std::shared_ptr<CGLESTextureResources>&) const;
  void DrainTextureResources();
  void CloseTextureResources();
  int context{1};
  std::shared_ptr<CGLESTextureResources> m_textureResources;
};
void expectDeletes(std::initializer_list<GLuint> expected, int context)
{
  assert(deletions.size() == expected.size());
  std::vector<GLuint> names;
  for (const auto& deletion : deletions)
  {
    assert(deletion.context == context && deletion.thread == owner);
    names.push_back(deletion.name);
  }
  std::sort(names.begin(), names.end());
  std::vector<GLuint> sorted(expected);
  std::sort(sorted.begin(), sorted.end());
  assert(names == sorted);
  deletions.clear();
}
'''

TESTS = r'''
int main()
{
  // Tokens retain an incarnation identity even when a renderer address is reused.
  std::aligned_storage_t<sizeof(CRenderSystemGLES), alignof(CRenderSystemGLES)> storage;
  auto* first = new (&storage) CRenderSystemGLES;
  first->m_bRenderCreated = true;
  first->m_textureResources = std::make_shared<CGLESTextureResources>();
  const auto old = first->CaptureRenderTarget();
  assert(first->IsRenderTargetCurrent(old));
  first->~CRenderSystemGLES();
  auto* second = new (&storage) CRenderSystemGLES;
  second->m_bRenderCreated = true;
  second->m_textureResources = std::make_shared<CGLESTextureResources>();
  const auto replacement = second->CaptureRenderTarget();
  assert(old.generation == replacement.generation);
  assert(old.identity != replacement.identity);
  assert(!second->IsRenderTargetCurrent(old));
  assert(second->IsRenderTargetCurrent(replacement));
  second->~CRenderSystemGLES();

  CRenderSystemGLES renderer;
  auto resources = std::make_shared<CGLESTextureResources>();
  auto unrelated = std::make_shared<CGLESTextureResources>();
  assert(!renderer.CanRender());
  renderer.m_bRenderCreated = true;
  assert(!renderer.CanRender());
  renderer.m_textureResources = resources;
  assert(renderer.CanRender());
  assert(renderer.IsTextureContextCurrent(resources));
  assert(!renderer.IsTextureContextCurrent(unrelated));
  assert(!renderer.IsTextureContextCurrent(nullptr));
  const auto original = renderer.CaptureRenderTarget();
  assert(!renderer.IsRenderTargetCurrent({}));
  renderer.m_bRenderCreated = false;
  assert(!renderer.IsRenderTargetCurrent(original));
  renderer.m_bRenderCreated = true;
  currentContext = 0;
  assert(!renderer.CanRender() && !renderer.IsRenderTargetCurrent(original));
  currentContext = 2;
  assert(!renderer.CanRender() && !renderer.IsTextureContextCurrent(resources));
  currentContext = 1;

  // Route/size A -> B -> A is simulated at its invalidation boundary: matching
  // geometry does not recover the previous generation. Textures keep namespace.
  resources->Register(11);
  renderer.InvalidateRenderTarget();
  const auto intermediate = renderer.CaptureRenderTarget();
  renderer.InvalidateRenderTarget();
  assert(!renderer.IsRenderTargetCurrent(original));
  assert(!renderer.IsRenderTargetCurrent(intermediate));
  assert(renderer.IsRenderTargetCurrent(renderer.CaptureRenderTarget()));
  assert(renderer.IsTextureContextCurrent(resources));
  assert(deletions.empty());

  // CPU release from a foreign thread must only enqueue. Foreign registration,
  // draining and closing cannot claim ownership or consume the owner's queue.
  resources->Register(12);
  resources->Register(12);
  resources->Register(0);
  std::thread releaser([&] {
    assert(!resources->IsOwner());
    assert(!renderer.CanRender());
    assert(!renderer.IsRenderTargetCurrent(renderer.CaptureRenderTarget()));
    assert(!renderer.IsTextureContextCurrent(resources));
    resources->Register(99);
    resources->Retire(11);
    resources->Retire(11);
    resources->Retire(0);
    resources->Retire(1234);
    assert(resources->TakeRetired().empty());
    assert(resources->Close().empty());
    renderer.DrainTextureResources();
  });
  releaser.join();
  assert(resources->IsOpen() && deletions.empty());
  currentContext = 0;
  renderer.DrainTextureResources();
  currentContext = 2;
  renderer.DrainTextureResources();
  assert(deletions.empty());
  currentContext = 1;
  renderer.m_bRenderCreated = false;
  renderer.DrainTextureResources();
  assert(deletions.empty());
  renderer.m_bRenderCreated = true;
  renderer.DrainTextureResources();
  expectDeletes({11}, 1);
  renderer.DrainTextureResources();
  assert(deletions.empty());

  // Close must gather both retired and still-live names, once each, even while
  // CPU consumers still retain the old registry. Readiness alone is irrelevant
  // to deleting names when their owning context remains current.
  resources->Register(13);
  resources->Retire(12);
  const auto preClose = renderer.CaptureRenderTarget();
  renderer.m_bRenderCreated = false;
  renderer.CloseTextureResources();
  expectDeletes({12, 13}, 1);
  assert(!resources->IsOpen());
  renderer.m_bRenderCreated = true;
  assert(!renderer.CanRender());
  assert(!renderer.IsRenderTargetCurrent(preClose));
  renderer.CloseTextureResources();
  assert(deletions.empty());

  // A new context may reuse the exact GLuint. Late old-context release cannot
  // enqueue a deletion in that namespace or delete the replacement name.
  currentContext = renderer.context = 2;
  auto fresh = std::make_shared<CGLESTextureResources>();
  renderer.m_textureResources = fresh;
  fresh->Register(13);
  assert(renderer.CanRender());
  assert(!renderer.IsTextureContextCurrent(resources));
  resources->Register(13);
  resources->Retire(13);
  renderer.DrainTextureResources();
  assert(deletions.empty());
  fresh->Retire(13);
  renderer.DrainTextureResources();
  expectDeletes({13}, 2);

  // A contextless close abandons names without any GL call. The same applies
  // when a different context is current; only EGL destruction may reclaim them.
  fresh->Register(21);
  fresh->Register(22);
  fresh->Retire(21);
  currentContext = 0;
  renderer.CloseTextureResources();
  assert(!fresh->IsOpen() && deletions.empty());
  currentContext = 2;
  renderer.DrainTextureResources();
  assert(deletions.empty());
  renderer.m_textureResources = std::make_shared<CGLESTextureResources>();
  renderer.m_textureResources->Register(31);
  currentContext = 3;
  renderer.CloseTextureResources();
  assert(!renderer.m_textureResources->IsOpen() && deletions.empty());
  renderer.m_textureResources.reset();
  renderer.CloseTextureResources();
  assert(deletions.empty());

  // Independent retire calls preserve every registered name under contention.
  currentContext = renderer.context = 4;
  renderer.m_textureResources = std::make_shared<CGLESTextureResources>();
  for (GLuint name = 100; name < 164; ++name)
    renderer.m_textureResources->Register(name);
  std::vector<std::thread> threads;
  for (GLuint index = 0; index < 4; ++index)
    threads.emplace_back([&, index] {
      for (GLuint name = 100 + index; name < 164; name += 4)
        renderer.m_textureResources->Retire(name);
    });
  for (auto& thread : threads)
    thread.join();
  assert(deletions.empty());
  renderer.DrainTextureResources();
  assert(deletions.size() == 64);
  std::set<GLuint> unique;
  for (const auto& deletion : deletions)
  {
    assert(deletion.context == 4 && deletion.thread == owner);
    assert(deletion.name >= 100 && deletion.name < 164);
    unique.insert(deletion.name);
  }
  assert(unique.size() == 64);
  deletions.clear();
  renderer.CloseTextureResources();
  assert(deletions.empty());
}
'''

if __name__ == '__main__':
    main()
