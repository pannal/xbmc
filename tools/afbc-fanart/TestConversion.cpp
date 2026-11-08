// SPDX-License-Identifier: GPL-2.0-or-later
// Readback belongs only in this software-GLES test, never in production uploads.
#include "guilib/AMLAFBCTexture.cpp"

#include <cassert>
#include <iostream>
#include <vector>

std::vector<uint8_t> ReadTexture(GLuint texture, unsigned int w, unsigned int h)
{
  GLuint fbo = 0;
  glGenFramebuffers(1, &fbo);
  glBindFramebuffer(GL_FRAMEBUFFER, fbo);
  glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, texture, 0);
  assert(glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE);
  std::vector<uint8_t> pixels(static_cast<size_t>(w) * h * 4);
  glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, pixels.data());
  assert(glGetError() == GL_NO_ERROR);
  glBindFramebuffer(GL_FRAMEBUFFER, 0);
  glDeleteFramebuffers(1, &fbo);
  return pixels;
}

int main()
{
  assert(AllocationSize(1920, 1080) == 8552448);
  assert(AllocationSize(3840, 2160) == 33763328);
  assert(AllocationSize(3839, 2159) == AllocationSize(3840, 2160));
  assert(AllocationSize(4097, 2160) == 0);
  assert(!KnownLibrary("/definitely/not/a/driver"));
  assert(!RenderTexture(1, 2, 1, 1)); // A context is mandatory.

  EGLDisplay display = eglGetDisplay(EGL_DEFAULT_DISPLAY);
  assert(display != EGL_NO_DISPLAY && eglInitialize(display, nullptr, nullptr));
  assert(eglBindAPI(EGL_OPENGL_ES_API));
  const EGLint configAttributes[]{EGL_SURFACE_TYPE, EGL_PBUFFER_BIT, EGL_RENDERABLE_TYPE,
                                  EGL_OPENGL_ES2_BIT, EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8,
                                  EGL_BLUE_SIZE, 8, EGL_ALPHA_SIZE, 8, EGL_NONE};
  EGLConfig config{};
  EGLint count = 0;
  assert(eglChooseConfig(display, configAttributes, &config, 1, &count) && count == 1);
  const EGLint contextAttributes[]{EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE};
  EGLContext context = eglCreateContext(display, config, EGL_NO_CONTEXT, contextAttributes);
  const EGLint surfaceAttributes[]{EGL_WIDTH, 1, EGL_HEIGHT, 1, EGL_NONE};
  EGLSurface surface = eglCreatePbufferSurface(display, config, surfaceAttributes);
  assert(context != EGL_NO_CONTEXT && surface != EGL_NO_SURFACE);
  assert(eglMakeCurrent(display, surface, surface, context));
  GLuint program = 0, fbo = 0;
  for (const auto& size : {std::pair<unsigned int, unsigned int>{129, 127}, {299, 449}, {480, 720}, {512, 512}, {1920, 1080}, {3840, 2160}})
  {
    const auto [w, h] = size;
    std::vector<uint8_t> pixels(static_cast<size_t>(w) * h * 4);
    for (size_t i = 0; i < pixels.size(); ++i)
      pixels[i] = static_cast<uint8_t>(i * 13 + i / 113);
    GLuint textures[2];
    glGenTextures(2, textures);
    for (unsigned int i = 0; i < 2; ++i)
    {
      glBindTexture(GL_TEXTURE_2D, textures[i]);
      Parameters();
      glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE,
                    i ? nullptr : pixels.data());
    }
    assert(RenderTexture(textures[0], textures[1], w, h));
    assert(ReadTexture(textures[1], w, h) == pixels);
    const auto& cached = conversionResources.at(context);
    if (program)
      assert(program == cached.program && fbo == cached.fbo);
    program = cached.program;
    fbo = cached.fbo;
    glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    GLint attachment = -1;
    glGetFramebufferAttachmentParameteriv(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                                          GL_FRAMEBUFFER_ATTACHMENT_OBJECT_NAME, &attachment);
    assert(attachment == 0); // Converter cache does not retain the image.
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glBindTexture(GL_TEXTURE_2D, textures[0]);
    GLint filter = 0;
    glGetTexParameteriv(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, &filter);
    assert(filter == GL_LINEAR);
    assert(!RenderTexture(textures[0], 0, w, h)); // Incomplete FBO falls back.
    glGetTexParameteriv(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, &filter);
    assert(filter == GL_LINEAR);

    // Actual BGRA uploads must produce the same RGBA result as ordinary GUI sampling.
    const auto* extensions = reinterpret_cast<const char*>(glGetString(GL_EXTENSIONS));
    if (extensions && std::strstr(extensions, "GL_EXT_texture_format_BGRA8888"))
    {
      std::vector<uint8_t> bgra = pixels;
      for (size_t i = 0; i < bgra.size(); i += 4)
        std::swap(bgra[i], bgra[i + 2]);
      glBindTexture(GL_TEXTURE_2D, textures[0]);
      glTexImage2D(GL_TEXTURE_2D, 0, 0x80e1, w, h, 0, 0x80e1, GL_UNSIGNED_BYTE, bgra.data());
      assert(RenderTexture(textures[0], textures[1], w, h));
      assert(ReadTexture(textures[1], w, h) == pixels);
    }
    assert(glGetError() == GL_NO_ERROR);
    glDeleteTextures(2, textures);
    std::cout << "PASS single-draw conversion, RGBA/BGRA/alpha and cleanup " << w << 'x' << h << '\n';
  }
  std::string error;
  assert(!CAMLAFBCTexture::Create(1, 1, 1, error));
  assert(!error.empty()); // Host never enters the ARM private import path.
  assert(conversionResources.size() == 1 && CAMLAFBCTexture::HasConversionResources(context));
  CAMLAFBCTexture::ReleaseConversionResources(context);
  assert(conversionResources.empty() && !CAMLAFBCTexture::HasConversionResources(context));
  assert(!glIsProgram(program) && !glIsFramebuffer(fbo));

  // A context without a current bind must only forget metadata. It must not
  // issue GL deletion against the current, unrelated context's colliding names.
  EGLContext other = eglCreateContext(display, config, EGL_NO_CONTEXT, contextAttributes);
  assert(other != EGL_NO_CONTEXT);
  assert(eglMakeCurrent(display, surface, surface, context));
  assert(!RenderTexture(1, 0, 1, 1));
  program = conversionResources.at(context).program;
  fbo = conversionResources.at(context).fbo;
  assert(eglMakeCurrent(display, surface, surface, other));
  assert(!RenderTexture(1, 0, 1, 1));
  const GLuint otherProgram = conversionResources.at(other).program;
  CAMLAFBCTexture::ReleaseConversionResources(context);
  assert(!conversionResources.count(context) && glIsProgram(otherProgram));
  CAMLAFBCTexture::ReleaseConversionResources(other);
  assert(conversionResources.empty());
  assert(eglMakeCurrent(display, surface, surface, context));
  glDeleteProgram(program);glDeleteFramebuffers(1, &fbo);
  eglMakeCurrent(display, EGL_NO_SURFACE, EGL_NO_SURFACE, EGL_NO_CONTEXT);
  eglDestroyContext(display, other);
  eglDestroyContext(display, context);
  eglDestroySurface(display, surface);
  eglTerminate(display);
  std::cout << "PASS converter reuse and exact-context retirement\n";
}
