#!/usr/bin/env python3
"""Check the "Suppress LLDV VSIF" setting: policy unit tests, a behavioural
harness around the real aml_apply_lldv_vsif_suppress(), and settings/strings
structure.  Usage: python3 tools/test-lldv-vsif-suppress-setting.py [kodi-root]"""
import pathlib
import runpy
import subprocess
import sys
import tempfile

ROOT = (pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1
        else pathlib.Path(__file__).resolve().parents[1])
POLICY_TEST = r'''// Host unit test for AMLLldvVsifPolicy.h: g++ -std=c++17 -I<kodi>/xbmc/utils
#include "AMLLldvVsifPolicy.h"

#include <cstdio>
#include <cstdlib>

using namespace AML_LLDV_VSIF;

static int failures = 0;
#define CHECK(c) \
  do { if (!(c)) { std::printf("FAIL line %d: %s\n", __LINE__, #c); ++failures; } } while (0)

int main()
{
  CHECK(ParseKernelBool("Y\n") == std::optional<bool>(true));
  CHECK(ParseKernelBool("N\n") == std::optional<bool>(false));
  CHECK(ParseKernelBool("1") == std::optional<bool>(true));
  CHECK(ParseKernelBool("0") == std::optional<bool>(false));
  CHECK(!ParseKernelBool("").has_value());
  CHECK(!ParseKernelBool("maybe").has_value());

  // Kernel booted with suppression on (boot argument), setting absent (default off).
  Plan p = Decide(true, false, false, false, true);
  CHECK(p.action == Action::ADOPT_KERNEL && p.value);
  // Normal kernel default off: adoption keeps the normal default.
  p = Decide(true, false, false, false, false);
  CHECK(p.action == Action::ADOPT_KERNEL && !p.value);
  // First run with an unreadable parameter must not write anything.
  p = Decide(true, false, false, false, std::nullopt);
  CHECK(p.action == Action::READ_ERROR);

  // A persisted On choice without the marker is kept, even if the kernel boots Off or is unreadable.
  p = Decide(true, false, false, true, false);
  CHECK(p.action == Action::WRITE && p.value);
  p = Decide(true, false, false, true, std::nullopt);
  CHECK(p.action == Action::WRITE && p.value);

  // After adoption the setting owns the kernel value, including across reboots that
  // reset the kernel default to on.
  p = Decide(true, true, false, false, true);
  CHECK(p.action == Action::WRITE && !p.value);
  p = Decide(true, true, false, true, false);
  CHECK(p.action == Action::WRITE && p.value);
  // A user change takes ownership even if adoption never completed.
  p = Decide(true, false, true, true, std::nullopt);
  CHECK(p.action == Action::WRITE && p.value);
  p = Decide(true, false, true, false, true);
  CHECK(p.action == Action::WRITE && !p.value);

  // Unsupported kernel: surfaced only when the user asked for suppression.
  CHECK(Decide(false, true, false, true, std::nullopt).action == Action::UNSUPPORTED);
  CHECK(Decide(false, false, true, true, std::nullopt).action == Action::UNSUPPORTED);
  CHECK(Decide(false, true, false, false, std::nullopt).action == Action::NONE);
  CHECK(Decide(false, false, false, false, std::nullopt).action == Action::NONE);

  CHECK(Verified(true, true));
  CHECK(Verified(false, false));
  CHECK(!Verified(true, false));
  CHECK(!Verified(false, std::nullopt));

  std::printf(failures ? "policy tests FAILED (%d)\n" : "policy tests passed\n", failures);
  return failures ? EXIT_FAILURE : EXIT_SUCCESS;
}

'''

APPLY_TEST = """\"\"\"Generate and compile-run a behavioural harness around the real aml_apply_lldv_vsif_suppress.
Usage: python3 test_apply.py <kodi-root>\"\"\"
import re, sys, pathlib, subprocess, tempfile

root = pathlib.Path(sys.argv[1])
src = (root / "xbmc/utils/AMLUtils.cpp").read_text(encoding="utf-8")
m = re.search(r"bool aml_lldv_vsif_suppress_supported\\(\\)\\n\\{.*?\\nvoid aml_apply_lldv_vsif_suppress\\(bool userChange\\)\\n\\{.*?\\n\\}\\n", src, re.S)
if not m:
    sys.exit("functions not found")

harness = r'''
#include <map>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>
#include <cstdio>
#include "AMLLldvVsifPolicy.h"

enum { LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR };
struct CLog { template <typename... A> static void Log(int, const char*, A&&...) {} };
struct CSettings {
  static constexpr const char* SETTING_COREELEC_AMLOGIC_LLDV_VSIF_SUPPRESS = "s";
  static constexpr const char* SETTING_COREELEC_AMLOGIC_LLDV_VSIF_SUPPRESS_ADOPTED = "a";
};
struct FakeSettings {
  std::map<std::string, bool> v;
  bool GetBool(const std::string& k) { return v[k]; }
  void SetBool(const std::string& k, bool b) { v[k] = b; }
};
static FakeSettings g_settings;
static FakeSettings* settings() { return &g_settings; }

// Kernel parameter model.
static bool k_exists = true, k_readable = true, k_throw = false, k_ignore = false;
static std::string k_value = "Y\\n";
static int k_writes = 0;
struct CSysfsPath {
  explicit CSysfsPath(const std::string&) {}
  bool Exists() const { return k_exists; }
  template <typename T> std::optional<T> Get() const {
    if (!k_exists || !k_readable) return std::nullopt;
    return k_value;
  }
  void Set(int v) {
    ++k_writes;
    if (k_throw) throw std::runtime_error("EINVAL");
    if (!k_ignore) k_value = v ? "Y\\n" : "N\\n";
  }
};
struct CGUIDialogKaiToast {
  enum Type { Info, Warning, Error };
  static std::vector<int> toasts;
  static void QueueNotification(Type t, const std::string&, const std::string&) { toasts.push_back(t); }
};
std::vector<int> CGUIDialogKaiToast::toasts;
struct { std::string Get(int) { return ""; } } g_localizeStrings;
static const char* AML_LLDV_VSIF_SUPPRESS = "/sys/module/hdmi_tx_hw/parameters/lldv_vsif_suppress";
static std::mutex s_lldvVsifMutex;
''' + m.group(0) + r'''
static int failures = 0;
#define CHECK(c) do { if (!(c)) { std::printf("FAIL %s:%d %s\\n", name, __LINE__, #c); ++failures; } } while (0)
static void reset(bool setting, bool adopted, const char* kernel) {
  g_settings.v = {{"s", setting}, {"a", adopted}};
  k_exists = true; k_readable = true; k_throw = false; k_ignore = false;
  k_value = kernel; k_writes = 0; CGUIDialogKaiToast::toasts.clear();
}
static bool S() { return g_settings.v["s"]; }
static bool A() { return g_settings.v["a"]; }
static size_t T() { return CGUIDialogKaiToast::toasts.size(); }

int main() {
  const char* name;
  name = "first start adopts kernel Y";
  reset(false, false, "Y\\n"); aml_apply_lldv_vsif_suppress(false);
  CHECK(S() && A() && k_writes == 0 && k_value == "Y\\n" && T() == 0);

  name = "normal kernel first start adopts N";
  reset(false, false, "N\\n"); aml_apply_lldv_vsif_suppress(false);
  CHECK(!S() && A() && k_writes == 0 && T() == 0);

  name = "persisted On without marker kept over kernel N";
  reset(true, false, "N\\n"); aml_apply_lldv_vsif_suppress(false);
  CHECK(S() && A() && k_writes == 1 && k_value == "Y\\n" && T() == 0);

  name = "unsupported kernel default Off is silent";
  reset(false, false, "N\\n"); k_exists = false; aml_apply_lldv_vsif_suppress(false);
  CHECK(!S() && !A() && k_writes == 0 && T() == 0);

  name = "unsupported kernel with On warns";
  reset(true, true, "N\\n"); k_exists = false; aml_apply_lldv_vsif_suppress(false);
  CHECK(k_writes == 0 && T() == 1 && CGUIDialogKaiToast::toasts[0] == CGUIDialogKaiToast::Warning);

  name = "user activation on unsupported kernel warns";
  reset(true, false, "N\\n"); k_exists = false; aml_apply_lldv_vsif_suppress(true);
  CHECK(!A() && T() == 1);

  name = "first-start read error: no write, no adoption, error";
  reset(false, false, "N\\n"); k_readable = false; aml_apply_lldv_vsif_suppress(false);
  CHECK(!S() && !A() && k_writes == 0 && T() == 1 && CGUIDialogKaiToast::toasts[0] == CGUIDialogKaiToast::Error);

  name = "write exception with stale matching readback is an error, not adopted";
  reset(true, false, "Y\\n"); k_throw = true; aml_apply_lldv_vsif_suppress(true);
  CHECK(!A() && T() == 1 && CGUIDialogKaiToast::toasts[0] == CGUIDialogKaiToast::Error);

  name = "migration write exception keeps marker absent";
  reset(true, false, "N\\n"); k_throw = true; aml_apply_lldv_vsif_suppress(false);
  CHECK(S() && !A() && k_value == "N\\n" && T() == 1);

  name = "kernel ignores write -> readback mismatch error, not adopted";
  reset(false, false, "Y\\n"); k_ignore = true; aml_apply_lldv_vsif_suppress(true);
  CHECK(!A() && T() == 1 && k_value == "Y\\n");

  name = "user turns off: verified write, adopted";
  reset(false, false, "Y\\n"); aml_apply_lldv_vsif_suppress(true);
  CHECK(!S() && A() && k_value == "N\\n" && T() == 0);

  name = "adopted Off re-applied after kernel reboot default Y";
  reset(false, true, "Y\\n"); aml_apply_lldv_vsif_suppress(false);
  CHECK(k_value == "N\\n" && T() == 0);

  std::printf(failures ? "apply harness FAILED (%d)\\n" : "apply harness passed (12 cases)\\n", failures);
  return failures ? 1 : 0;
}
'''
with tempfile.TemporaryDirectory() as d:
    cpp = pathlib.Path(d) / "h.cpp"
    cpp.write_text(harness, encoding="utf-8")
    exe = pathlib.Path(d) / "h"
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wno-unused-function", "-I", str(root / "xbmc/utils"),
                    str(cpp), "-o", str(exe)], check=True)
    sys.exit(subprocess.run([str(exe)]).returncode)
"""

STRUCTURE_TEST = """\"\"\"Source-structure checks for the LLDV VSIF suppression setting.\"\"\"
import re, sys, pathlib, xml.etree.ElementTree as ET

root = pathlib.Path(sys.argv[1])
fail = []
def check(cond, msg):
    if not cond:
        fail.append(msg)

# settings.xml: new toggle immediately follows the seamless toggle, default off, independent.
tree = ET.parse(root / "system/settings/settings.xml")
for group in tree.iter("group"):
    ids = [s.get("id") for s in group.findall("setting")]
    if "coreelec.amlogic.hdr10.seamless.transitions" in ids:
        i = ids.index("coreelec.amlogic.hdr10.seamless.transitions")
        check(ids[i + 1:i + 2] == ["coreelec.amlogic.lldv.vsif.suppress"], "toggle not adjacent to seamless")
        break
else:
    fail.append("seamless setting not found")
by_id = {s.get("id"): s for s in tree.iter("setting")}
t = by_id.get("coreelec.amlogic.lldv.vsif.suppress")
check(t is not None and t.findtext("default") == "false", "toggle default not false")
check(t is not None and t.get("parent") is None and t.find("dependencies") is None,
      "toggle must not depend on/attach to other settings")
a = by_id.get("coreelec.amlogic.lldv.vsif.suppress.adopted")
check(a is not None and a.findtext("default") == "false" and a.findtext("level") == "4"
      and a.findtext("visible") == "false", "adopted marker must be hidden internal default false")

po = (root / "addons/resource.language.en_gb/resources/strings.po").read_text(encoding="utf-8")
for n in (60652, 60653, 60654, 60655):
    check(po.count(f'msgctxt "#{n}"') == 1, f"string {n} missing/duplicated")

aml = (root / "xbmc/utils/AMLUtils.cpp").read_text(encoding="utf-8")
m = re.search(r"void aml_apply_lldv_vsif_suppress\\(bool userChange\\)\\n\\{(.*?)\\n\\}\\n", aml, re.S)
check(m is not None, "apply function missing")
body = m.group(1) if m else ""
check("seamless_mode_refresh" not in body and "AML_SEAMLESS_MODE_REFRESH" not in body
      and "SEAMLESS_TRANSITIONS" not in body, "apply must not touch the seamless gate")
check("Verified(" in body and "QueueNotification" in body, "write must be verified and failures surfaced")
seam = re.search(r"void aml_apply_hdr10_seamless_transitions\\(\\)\\n\\{(.*?)\\n\\}\\n", aml, re.S).group(1)
check("LLDV_VSIF" not in seam, "seamless apply must not touch VSIF suppression")
mark = "SetBool(CSettings::SETTING_COREELEC_AMLOGIC_LLDV_VSIF_SUPPRESS_ADOPTED, true)"
write_at = body.find("CSysfsPath(AML_LLDV_VSIF_SUPPRESS).Set(")
fail_ret = body.find("return;", body.find("if (!Verified("))
check(write_at > 0 and body.rfind(mark) > fail_ret > write_at and body.count(mark) == 2,
      "adoption marker must be set only after verified write (or on pure adoption)")
catch = re.search(r"catch \\(const std::exception& e\\)\\n  \\{(.*?)\\n  \\}", body, re.S)
check(catch is not None and "QueueNotification" in catch.group(1) and "return;" in catch.group(1)
      and mark not in catch.group(1),
      "write exception must surface an error and return without marking adoption")
check("READ_ERROR:" in body and mark not in body[body.find("READ_ERROR:"):body.find("ADOPT_KERNEL:")],
      "read error must not mark adoption")
check("Disable for displays that require Dolby Vision signaling" in po
      and "Enable for manually configured LLDV setups, such as older JVC projectors" in po
      and "Stops sending the Dolby Vision low-latency (LLDV) vendor-specific infoframe" in po
      and "Other Dolby Vision, HDR10+ and ALLM VSIFs are unchanged" in po
      and "declared in the AVI infoframe instead of an HDMI 1.4 VSIF" in po,
      "help text must use the confirmed polarity and LLDV-only scope")
check('"/sys/module/hdmi_tx_hw/parameters/lldv_vsif_suppress"' in aml, "wrong parameter path")

dv = (root / "xbmc/windowing/amlogic/DolbyVisionAML.cpp").read_text(encoding="utf-8")
check("settingSet.insert(CSettings::SETTING_COREELEC_AMLOGIC_LLDV_VSIF_SUPPRESS);" in dv, "callback not registered")
check("aml_apply_lldv_vsif_suppress(false);" in dv, "not applied at startup")
check("aml_apply_lldv_vsif_suppress(true);" in dv, "not applied on change")
osc = re.search(r"// Queue independent native switches.*?\\{\\n    schedule_native_setting_apply", dv, re.S)
check(osc is not None and "SETTING_COREELEC_AMLOGIC_LLDV_VSIF_SUPPRESS" in osc.group(0),
      "change must route via independent native path (no DV preset flip)")

print("\\n".join("FAIL: " + f for f in fail) or "structure checks passed")
sys.exit(1 if fail else 0)
"""

with tempfile.TemporaryDirectory() as d:
    cpp = pathlib.Path(d) / "policy.cpp"
    exe = pathlib.Path(d) / "policy"
    cpp.write_text(POLICY_TEST, encoding="utf-8")
    subprocess.run(["g++", "-std=c++17", "-Wall", "-I", str(ROOT / "xbmc/utils"),
                    str(cpp), "-o", str(exe)], check=True)
    subprocess.run([str(exe)], check=True)

HERE = pathlib.Path(__file__).resolve().parent
for part in ("APPLY", "STRUCTURE"):
    with tempfile.TemporaryDirectory() as d:
        script = pathlib.Path(d) / f"{part.lower()}.py"
        script.write_text(globals()[part + "_TEST"], encoding="utf-8")
        sys.argv = [str(script), str(ROOT)]
        try:
            runpy.run_path(str(script), run_name="__main__")
        except SystemExit as e:
            if e.code not in (None, 0):
                raise