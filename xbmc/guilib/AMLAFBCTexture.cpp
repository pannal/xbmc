/*
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#include "AMLAFBCTexture.h"

#include "AMLAFBCTexturePolicy.h"

#include <openssl/evp.h>

#include <array>
#include <atomic>
#include <cstring>
#include <dlfcn.h>
#include <fcntl.h>
#include <fstream>
#include <map>
#include <mutex>
#include <sys/ioctl.h>
#include <unistd.h>

namespace
{
// Preserve the extra capacity used in hardware validation, without a CPU mapping.
constexpr size_t ALLOCATION_PADDING = 65536;
constexpr uint32_t AFBC_RGBA8 = 0x360bb688;
constexpr uint32_t AFBC_RGBA8_WIDE_SPLIT = 0xf60bb688;
std::atomic_bool failed{false};
std::atomic_bool reportedUnsupported{false};
std::atomic_bool reportedKernel{false};

size_t Align(size_t value, size_t alignment)
{
  return (value + alignment - 1) & ~(alignment - 1);
}

size_t AllocationSize(unsigned int width, unsigned int height)
{
  if (!width || !height || width > 4096 || height > 4096)
    return 0;
  const size_t w = Align(width, 16), h = Align(height, 16);
  return Align(Align((w / 16) * (h / 16) * 16, 1024) + w * h * 4, 4096) + ALLOCATION_PADDING;
}

bool KnownLibrary(const char* path)
{
  std::ifstream input(path, std::ios::binary);
  std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> digest(EVP_MD_CTX_new(), EVP_MD_CTX_free);
  if (!input || !digest || EVP_DigestInit_ex(digest.get(), EVP_sha256(), nullptr) != 1)
    return false;
  std::array<char, 65536> buffer;
  while (input.read(buffer.data(), buffer.size()) || input.gcount())
    if (EVP_DigestUpdate(digest.get(), buffer.data(), input.gcount()) != 1)
      return false;
  if (!input.eof())
    return false;
  std::array<unsigned char, EVP_MAX_MD_SIZE> hash{};
  unsigned int length = 0;
  if (EVP_DigestFinal_ex(digest.get(), hash.data(), &length) != 1 || length != 32)
    return false;
  const char* hex = "0123456789abcdef";
  std::string text;
  for (unsigned int i = 0; i < length; ++i)
  {
    text += hex[hash[i] >> 4];
    text += hex[hash[i] & 15];
  }
  return text == "949b043bd373a7779d4338a87ecdd929e0430dfddcb092de7a4e6cda3a905397" ||
         text == "789adf85dc9c630bb049249bf7e28ed40380e919ae1f379a352167d3107ec445";
}

struct API
{
  uint32_t (*createMapping)(void*){nullptr};
  int (*destroyMapping)(uint32_t){nullptr};
  PFNEGLCREATEIMAGEKHRPROC createImage{nullptr};
  PFNEGLDESTROYIMAGEKHRPROC destroyImage{nullptr};
  PFNGLEGLIMAGETARGETTEXTURE2DOESPROC bindImage{nullptr};
  bool supported{false};
};

const API& Driver()
{
  static API api;
  static std::once_flag once;
  std::call_once(once, [] {
    if (sizeof(void*) != 4)
      return;
    void* create = dlsym(RTLD_DEFAULT, "egl_create_pixmap_ID_mapping");
    void* destroy = dlsym(RTLD_DEFAULT, "egl_destroy_pixmap_ID_mapping");
    Dl_info info{};
    Dl_info eglInfo{};
    // The registry and active EGL entry point must come from the same library.
    if (!create || !destroy || !dladdr(create, &info) || !info.dli_fname ||
        !dladdr(dlsym(RTLD_DEFAULT, "eglGetDisplay"), &eglInfo) ||
        info.dli_fbase != eglInfo.dli_fbase || !KnownLibrary(info.dli_fname))
      return;
    std::memcpy(&api.createMapping, &create, sizeof(create));
    std::memcpy(&api.destroyMapping, &destroy, sizeof(destroy));
    api.createImage = reinterpret_cast<PFNEGLCREATEIMAGEKHRPROC>(eglGetProcAddress("eglCreateImageKHR"));
    api.destroyImage = reinterpret_cast<PFNEGLDESTROYIMAGEKHRPROC>(eglGetProcAddress("eglDestroyImageKHR"));
    api.bindImage = reinterpret_cast<PFNGLEGLIMAGETARGETTEXTURE2DOESPROC>(eglGetProcAddress("glEGLImageTargetTexture2DOES"));
    api.supported = api.createImage && api.destroyImage && api.bindImage;
  });
  return api;
}

struct IonAllocation { size_t length, alignment; uint32_t heapMask, flags; int handle; };
struct IonFd { int handle, fd; };
struct IonHandle { int handle; };

void Parameters()
{
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
}

GLuint Compile(GLenum type, const char* text)
{
  GLuint shader = glCreateShader(type);
  glShaderSource(shader, 1, &text, nullptr);
  glCompileShader(shader);
  GLint ok = 0;
  glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
  if (!ok)
  {
    glDeleteShader(shader);
    return 0;
  }
  return shader;
}

// Owned by one upload-context generation. DestroyContext explicitly retires
// these before destroying EGL handles, so a recycled handle cannot reuse names.
struct ConversionResources
{
  GLuint program{0};
  GLuint fbo{0};
  GLint sampler{-1};

  void Delete()
  {
    glDeleteFramebuffers(1, &fbo);
    glDeleteProgram(program);
    fbo = program = 0;
  }

  bool Initialize()
  {
    if (program)
      return true;
    const char* vertex = "attribute vec2 position; varying highp vec2 uv;"
                         "void main(){uv=position*.5+.5;gl_Position=vec4(position,0.,1.);}";
    const char* fragment = "precision highp float; varying highp vec2 uv; uniform sampler2D image;"
                           "void main(){gl_FragColor=texture2D(image,uv);}";
    const GLuint vs = Compile(GL_VERTEX_SHADER, vertex), fs = Compile(GL_FRAGMENT_SHADER, fragment);
    if (!vs || !fs)
    {
      glDeleteShader(vs);
      glDeleteShader(fs);
      return false;
    }
    program = glCreateProgram();
    glAttachShader(program, vs);
    glAttachShader(program, fs);
    glBindAttribLocation(program, 0, "position");
    glLinkProgram(program);
    glDeleteShader(vs);
    glDeleteShader(fs);
    GLint linked = 0;
    glGetProgramiv(program, GL_LINK_STATUS, &linked);
    if (linked)
    {
      sampler = glGetUniformLocation(program, "image");
      glGenFramebuffers(1, &fbo);
    }
    if (!linked || sampler < 0 || !fbo || glGetError() != GL_NO_ERROR)
    {
      Delete();
      return false;
    }
    return true;
  }
};

std::mutex conversionLock;
std::map<EGLContext, ConversionResources> conversionResources;

// Called only with the dedicated upload-context lease held. A full target clear
// initializes edge blocks; the draw samples the decoded source once. Completion
// for sharing with the GUI is provided by CGLTexture::SyncGPU after this returns.
bool RenderTexture(GLuint source, GLuint destination, unsigned int width, unsigned int height)
{
  const EGLContext context = eglGetCurrentContext();
  if (context == EGL_NO_CONTEXT)
    return false;
  std::lock_guard<std::mutex> lock(conversionLock);
  auto& resources = conversionResources[context];
  if (!resources.Initialize())
    return false;
  glActiveTexture(GL_TEXTURE0);
  glBindTexture(GL_TEXTURE_2D, source);
  GLint minimum = GL_LINEAR, magnification = GL_LINEAR;
  glGetTexParameteriv(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, &minimum);
  glGetTexParameteriv(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, &magnification);
  struct Cleanup
  {
    GLuint source;
    GLint minimum, magnification;
    ~Cleanup()
    {
      // The reusable FBO must not keep an evicted texture's storage alive.
      glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, 0, 0);
      glBindFramebuffer(GL_FRAMEBUFFER, 0);
      glUseProgram(0);
      glDisableVertexAttribArray(0);
      glBindTexture(GL_TEXTURE_2D, source);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, minimum);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, magnification);
    }
  } cleanup{source, minimum, magnification};
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
  glBindFramebuffer(GL_FRAMEBUFFER, resources.fbo);
  glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, destination, 0);
  if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE)
    return false;
  glViewport(0, 0, width, height);
  glDisable(GL_SCISSOR_TEST);
  glDisable(GL_BLEND);
  glDisable(GL_DITHER);
  glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
  glUseProgram(resources.program);
  glUniform1i(resources.sampler, 0);
  glBindBuffer(GL_ARRAY_BUFFER, 0);
  const GLfloat vertices[]{-1, -1, 1, -1, -1, 1, 1, 1};
  glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 0, vertices);
  glEnableVertexAttribArray(0);
  glClearColor(0, 0, 0, 0);
  glClear(GL_COLOR_BUFFER_BIT);
  glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
  return glGetError() == GL_NO_ERROR;
}
} // namespace

bool CAMLAFBCImport::Supported()
{
  const auto* renderer = reinterpret_cast<const char*>(glGetString(GL_RENDERER));
  return Driver().supported && renderer && std::strncmp(renderer, "Mali-G52", 8) == 0;
}

bool CAMLAFBCImport::Import(int fd, unsigned int width, unsigned int height, Layout layout)
{
  static_assert(sizeof(NativePixmap) == 68);
  static_assert(offsetof(NativePixmap, format) == 0x30);
  static_assert(offsetof(NativePixmap, fd) == 0x38);
  Release();
  const auto& driver = Driver();
  if (!driver.supported || fd < 0 || !width || !height)
    return false;
  m_pixmap = {};
  m_pixmap.width = width;
  m_pixmap.height = height;
  m_pixmap.plane[0].pitch = width * 4;
  m_pixmap.format = layout == Layout::WIDE_BLOCK ? AFBC_RGBA8_WIDE_SPLIT : AFBC_RGBA8;
  m_pixmap.fd[0] = fd;
  m_display = eglGetCurrentDisplay();
  m_mapping = driver.createMapping(&m_pixmap);
  if (m_mapping == UINT32_MAX)
    return false;
  const EGLint attributes[]{EGL_IMAGE_PRESERVED_KHR, EGL_FALSE, EGL_NONE};
  m_image = driver.createImage(m_display, EGL_NO_CONTEXT, EGL_NATIVE_PIXMAP_KHR,
                              reinterpret_cast<EGLClientBuffer>(static_cast<uintptr_t>(m_mapping)), attributes);
  if (m_image != EGL_NO_IMAGE_KHR)
    return true;
  Release();
  return false;
}

bool CAMLAFBCImport::BindTexture() const
{
  if (m_image == EGL_NO_IMAGE_KHR)
    return false;
  Driver().bindImage(GL_TEXTURE_2D, m_image);
  return glGetError() == GL_NO_ERROR;
}

void CAMLAFBCImport::Release()
{
  const auto& driver = Driver();
  if (m_image != EGL_NO_IMAGE_KHR)
    driver.destroyImage(m_display, m_image);
  if (m_mapping != UINT32_MAX)
    driver.destroyMapping(m_mapping);
  m_image = EGL_NO_IMAGE_KHR;
  m_mapping = UINT32_MAX;
}

bool CAMLAFBCTexture::HasConversionResources(EGLContext context)
{
  std::lock_guard<std::mutex> lock(conversionLock);
  return conversionResources.count(context) != 0;
}

void CAMLAFBCTexture::ReleaseConversionResources(EGLContext context)
{
  std::lock_guard<std::mutex> lock(conversionLock);
  const auto it = conversionResources.find(context);
  if (it == conversionResources.end())
    return;
  // A failed/poisoned EGL bind must never delete names in another context.
  // In that case EGL context/share-group teardown reclaims the GL objects.
  if (context != EGL_NO_CONTEXT && eglGetCurrentContext() == context)
    it->second.Delete();
  conversionResources.erase(it);
}

bool CAMLAFBCTexture::Allocate(unsigned int width, unsigned int height, std::string& error)
{
  static_assert(sizeof(NativePixmap) == 68);
  static_assert(offsetof(NativePixmap, format) == 0x30);
  static_assert(offsetof(NativePixmap, fd) == 0x38);
  m_length = AllocationSize(width, height);
  m_ion = open("/dev/ion", O_RDWR | O_CLOEXEC);
  IonAllocation allocation{m_length, 4096, 1, 0, -1};
  if (m_ion < 0 || ioctl(m_ion, _IOWR('I', 0, IonAllocation), &allocation) < 0)
  {
    error = "uncached system-heap allocation failed";
    return false;
  }
  m_handle = allocation.handle;
  IonFd share{m_handle, -1};
  if (ioctl(m_ion, _IOWR('I', 4, IonFd), &share) < 0)
    return false;
  m_fd = share.fd;
  // The fixed system heap supplies zeroed pages with a clean kernel alias.
  // GPU-only initialization avoids CPU mapping and DMA-BUF CPU-access syncs.
  m_pixmap.width = width;
  m_pixmap.height = height;
  m_pixmap.plane[0].pitch = width * 4;
  m_pixmap.format = AFBC_RGBA8;
  m_pixmap.fd[0] = m_fd;
  m_display = eglGetCurrentDisplay();
  const auto& driver = Driver();
  m_mapping = driver.createMapping(&m_pixmap);
  if (m_mapping == UINT32_MAX)
    return false;
  const EGLint attributes[]{EGL_IMAGE_PRESERVED_KHR, EGL_FALSE, EGL_NONE};
  m_image = driver.createImage(m_display, EGL_NO_CONTEXT, EGL_NATIVE_PIXMAP_KHR,
                              reinterpret_cast<EGLClientBuffer>(static_cast<uintptr_t>(m_mapping)), attributes);
  if (m_image == EGL_NO_IMAGE_KHR)
    return false;
  glGenTextures(1, &m_texture);
  glBindTexture(GL_TEXTURE_2D, m_texture);
  Parameters();
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
  driver.bindImage(GL_TEXTURE_2D, m_image);
  return glGetError() == GL_NO_ERROR;
}

std::shared_ptr<CAMLAFBCTexture> CAMLAFBCTexture::Create(GLuint source, unsigned int width,
                                                      unsigned int height, std::string& error)
{
  if (failed.load())
    return {};
  const auto* renderer = reinterpret_cast<const char*>(glGetString(GL_RENDERER));
  if (!Driver().supported || !renderer || std::strncmp(renderer, "Mali-G52", 8) != 0)
  {
    if (!reportedUnsupported.exchange(true))
      error = "requires the verified ARM32 G52 r12p0 fbdev library";
    return {};
  }
  static const bool kernelSync = AML_AFBC::KernelSyncsDevicelessPages();
  if (!kernelSync)
  {
    if (!reportedKernel.exchange(true))
      error = std::string("requires the kernel's arm64 ION cache sync (") +
              AML_AFBC::KERNEL_SYNC_PARAMETER + " is not Y)";
    return {};
  }
  if (!source || !AllocationSize(width, height))
    return {};
  try
  {
    auto backing = std::shared_ptr<CAMLAFBCTexture>(new CAMLAFBCTexture);
    error = "allocation/import failed";
    if (backing->Allocate(width, height, error))
    {
      error = "AFBC conversion failed";
      if (RenderTexture(source, backing->m_texture, width, height))
      {
        error.clear();
        return backing;
      }
    }
  }
  catch (const std::exception& exception)
  {
    error = exception.what();
  }
  // No retry loop or warm-up acceptance: retain ordinary storage after any
  // failure and stop trying AFBC for this process. Existing textures stay valid.
  failed.store(true);
  for (unsigned int i = 0; i < 16 && glGetError() != GL_NO_ERROR; ++i) {}
  glBindFramebuffer(GL_FRAMEBUFFER, 0);
  glUseProgram(0);
  glBindTexture(GL_TEXTURE_2D, source);
  return {};
}

GLuint CAMLAFBCTexture::TakeTexture()
{
  GLuint texture = m_texture;
  m_texture = 0;
  return texture;
}

CAMLAFBCTexture::~CAMLAFBCTexture()
{
  if (m_texture)
    glDeleteTextures(1, &m_texture); // Only an unpublished conversion failure.
  const auto& driver = Driver();
  if (m_image != EGL_NO_IMAGE_KHR)
    driver.destroyImage(m_display, m_image);
  if (m_mapping != UINT32_MAX)
    driver.destroyMapping(m_mapping);
  if (m_fd >= 0)
    close(m_fd);
  if (m_handle >= 0)
  {
    IonHandle handle{m_handle};
    ioctl(m_ion, _IOWR('I', 1, IonHandle), &handle);
  }
  if (m_ion >= 0)
    close(m_ion);
}
