/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "guilib/AMLAFBCTexturePolicy.h"

#include <cassert>
#include <cstdio>
#include <fstream>
#include <iostream>
#include <string>
using namespace AML_AFBC;
int main()
{
  // afbcfanart alone admits opaque, roughly 16:9 background fanart.
  assert(Classify(1920, 1080, true, false, true, false) == ImageClass::Background);
  assert(Classify(480, 720, false, false, true, false) == ImageClass::None);
  assert(Classify(1920, 1080, true, true, true, false) == ImageClass::None);
  assert(Classify(1920, 1080, false, false, true, false) == ImageClass::None);
  // Selective artwork includes portrait, square, landscape and alpha images.
  assert(Classify(480, 720, false, false, false, true) == ImageClass::Artwork);
  assert(Classify(300, 450, false, false, false, true) == ImageClass::Artwork);
  assert(Classify(512, 512, false, true, false, true) == ImageClass::Artwork);
  assert(Classify(1920, 1080, true, true, false, true) == ImageClass::Background);
  assert(Classify(256, 512, false, false, false, true) == ImageClass::Artwork);
  assert(Classify(256, 511, false, false, false, true) == ImageClass::None);
  assert(Classify(256, 256, false, false, false, true) == ImageClass::None);
  assert(Classify(2048, 64, false, false, false, true) == ImageClass::None);
  assert(Classify(2048, 128, false, false, false, true) == ImageClass::Artwork);
  assert(Classify(1, 1, false, true, false, true) == ImageClass::None);
  assert(Classify(4096, 4096, false, false, false, true) == ImageClass::Artwork);
  assert(Classify(4097, 4096, false, false, false, true) == ImageClass::None);
  assert(Classify(0, 1080, true, false, true, true) == ImageClass::None);
  assert(Classify(1920, 1080, true, false, false, false) == ImageClass::None);
  // Conversion needs the kernel's ION cache sync to report itself as Y.
  const std::string parameter = std::string(P_tmpdir) + "/afbc-kernel-sync-test";
  std::remove(parameter.c_str());
  assert(!KernelSyncsDevicelessPages(parameter.c_str()));
  std::ofstream(parameter) << "N\n";
  assert(!KernelSyncsDevicelessPages(parameter.c_str()));
  std::ofstream(parameter) << "";
  assert(!KernelSyncsDevicelessPages(parameter.c_str()));
  std::ofstream(parameter) << "Y\n";
  assert(KernelSyncsDevicelessPages(parameter.c_str()));
  std::remove(parameter.c_str());
  std::cout << "PASS fanart-only and selective artwork policy, size boundaries, alpha, disabled "
               "and bounds; kernel sync missing, empty, N and Y\n";
}
