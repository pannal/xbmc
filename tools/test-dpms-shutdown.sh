#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Full production Stop/Cleanup/StopBeforeRun and AML teardown, real DPMS queue
# and adapter, with recording services/native resources (no hardware simulation).
set -Eeuo pipefail
ulimit -c 0
root=$(cd "$(dirname "$0")/.." && pwd)
source_root=${DPMS_SOURCE_ROOT:-$root}
out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
mkdir -p "$out/stubs/utils"
cat > "$out/stubs/utils/log.h" <<'LOG'
#pragma once
#include <atomic>
#include <cstring>
constexpr int LOGERROR = 1, LOGDEBUG = 2, LOGINFO = 3;
struct CLog {
  static inline std::atomic<int> failures{0};
  template<class... T> static void Log(int, const char* text, T&&...) {
    if (std::strstr(text, "failed to clear display")) ++failures;
  }
};
LOG
cp "$root/tools/dpms-shutdown-test.cpp" "$out/test.cpp"
extract() {
  awk -v pattern="$2" '$0 ~ pattern {copy=1} copy {print} copy && /^}/ {copy=0}' "$source_root/$1" >> "$out/test.cpp"
}
extract xbmc/application/ApplicationPowerHandling.cpp '^(bool|void) CApplicationPowerHandling::(ToggleDPMS|WakeDPMS|ProcessDPMS|ResetDPMS|ReleaseDPMS|ReleaseAutomaticDPMS|WakeUpScreenSaverAndDPMS|WakeUpScreenSaver|AnnounceScreenSaverDeactivated|PlaybackPreventsDPMS|CheckOSScreenSaverInhibitionSetting)[(]'
extract xbmc/windowing/amlogic/WinSystemAmlogicGLESContext.cpp '^bool CWinSystemAmlogicGLESContext::(PrepareForShutdown|DestroyWindowSystem)[(]'
extract xbmc/application/ApplicationPlayer.cpp '^bool CApplicationPlayer::(ClosePlayer|OpenFile)[(]'
extract xbmc/application/Application.cpp '^bool CApplication::(Stop|Cleanup|StopBeforeRun)[(]'
extract xbmc/platform/xbmc.cpp '^extern "C" int XBMC_Run[(]'
cat >> "$out/test.cpp" <<'CPP'
void CApplication::RetryFrame()
{
  const auto appPlayer = GetComponent<CApplicationPlayer>();
CPP
awk '/^  if [(]m_pendingStop && !appPlayer->HasPlayer[(]\)\)/ {copy=1} copy {print} copy && /^  }/ {exit}' "$source_root/xbmc/application/Application.cpp" >> "$out/test.cpp"
echo '}' >> "$out/test.cpp"
cat >> "$out/test.cpp" <<'CPP'
void CApplication::Message(int msg)
{
  switch (msg)
  {
CPP
# Actual quit/power/reboot cases, without unrelated message handlers.
awk '/^  case TMSG_POWERDOWN:/ {copy=1} /^  case TMSG_SHUTDOWN:/ {copy=0} /^  case TMSG_RESTART:/ {copy=1} /^  case TMSG_RESTARTAPP:/ {copy=0} copy {print}' "$source_root/xbmc/application/Application.cpp" >> "$out/test.cpp"
printf '  }\n}\n' >> "$out/test.cpp"
flags=(-std=c++17 -DHAS_LIBAMCODEC -Wall -Wextra -Werror -Wno-unused-parameter -pthread
  -fsanitize=address,undefined -fno-sanitize=vptr -fno-sanitize-recover=undefined
  -fno-omit-frame-pointer -fno-pie -no-pie)
compile() {
  "${CXX:-g++}" "${flags[@]}" -I"$out/stubs" -I"$source_root/xbmc" "$1" \
    "$source_root/xbmc/windowing/amlogic/AMLDPMSSupport.cpp" -Wl,--wrap=read -o "$2"
}
compile "$out/test.cpp" "$out/test"
if [[ ${1:-} == --case ]]; then "$out/test" "$2"; exit; fi
if [[ ${1:-} == --expect-unfixed ]]; then
  for scenario in persistent never-slept disabled readback quit reboot direct native-retry \
                  late-sleep-native late-sleep-admission; do
    if "$out/test" "$scenario" > "$out/failure.log" 2>&1; then
      echo "Unfixed source unexpectedly passed: $scenario" >&2; exit 1
    fi
    grep -q 'Assertion' "$out/failure.log"
    echo "Confirmed unfixed assertion: $scenario"
  done
  exit
fi
for scenario in persistent never-slept disabled recovery readback native-retry admission-retry \
                player-retry bluray-retry native-recovery quit reboot direct late-failure success absent emfile \
                legacy-auto legacy-manual legacy-pending-sleep legacy-manual-pending-wake \
                legacy-pending-toggles legacy-failed-release legacy-reentrant-manual \
                legacy-auto-no-saver legacy-failed-no-saver legacy-window-auto legacy-window-failed \
                late-sleep-native late-sleep-admission python-manual window-saver window-success \
                detector-retry detector-failed-release detector-cleanup; do
  "$out/test" "$scenario"
done
set +e
"$out/test" startup
status=$?
set -e
[[ $status == 255 ]] || { echo "Failed startup returned $status, expected 255" >&2; exit 1; }
echo 'PASS failed-startup status 255'
# Runtime UBSAN_OPTIONS alone is unreliable with EMFILE. Prove compiler-enforced
# termination with a deliberate signed overflow while every descriptor is held.
cat > "$out/ubsan.cpp" <<'CPP'
#include <cassert>
#include <climits>
#include <fcntl.h>
#include <sys/resource.h>
int main() {
  rlimit limit{64,64}; assert(setrlimit(RLIMIT_NOFILE, &limit) == 0);
  while (open("/dev/null", O_RDONLY) >= 0) {}
  volatile int value = INT_MAX;
  return (value + 1) == INT_MIN ? 0 : 2;
}
CPP
"${CXX:-g++}" "${flags[@]}" "$out/ubsan.cpp" -o "$out/ubsan"
set +e
"$out/ubsan" > "$out/ubsan.log" 2>&1
status=$?
set -e
[[ $status != 0 ]] && grep -q 'signed integer overflow' "$out/ubsan.log"
echo "PASS compiler-enforced UBSan control under EMFILE (status $status)"
if [[ ${1:-} == --negative-controls ]]; then
  [[ $(grep -c '^  m_shutdownRequested = true;$' "$out/test.cpp") == 1 ]]
  sed '/^  m_shutdownRequested = true;$/d' "$out/test.cpp" > "$out/no-fence.cpp"
  compile "$out/no-fence.cpp" "$out/no-fence"
  for scenario in late-sleep-native late-sleep-admission; do
    if "$out/no-fence" "$scenario" > "$out/no-fence.log" 2>&1; then
      echo "Missing shutdown fence unexpectedly passed: $scenario" >&2; exit 1
    fi
    grep -q 'Assertion' "$out/no-fence.log"
    grep -Fq 'f.Node() == 0 && !f.power->IsDPMSActive() && f.power->render' "$out/no-fence.log"
    echo "Rejected missing shutdown fence: $scenario"
  done
  expect_assertion() {
    local control=$1 scenario=$2 assertion=$3
    if "$out/$control" "$scenario" > "$out/$control-$scenario.log" 2>&1; then
      echo "Negative control unexpectedly passed: $control / $scenario" >&2; exit 1
    fi
    grep -q 'Assertion' "$out/$control-$scenario.log"
    grep -Fq "$assertion" "$out/$control-$scenario.log"
    echo "Rejected $control: $scenario"
  }
  # Recreate the old fixture mistake: a dialog must not become a history window.
  [[ $(grep -c 'manager.ActivateWindow(WINDOW_SCREENSAVER);' "$out/test.cpp") == 1 ]]
  sed '/manager.ActivateWindow(WINDOW_SCREENSAVER);/a\      CServiceBroker::gui.manager.active = WINDOW_SCREENSAVER;' \
    "$out/test.cpp" > "$out/dialog-as-window.cpp"
  compile "$out/dialog-as-window.cpp" "$out/dialog-as-window"
  for scenario in window-saver window-success legacy-window-auto legacy-window-failed; do
    expect_assertion dialog-as-window "$scenario" '!Saw("previous-window")'
  done
  # Production Cleanup must reach window destruction even without a GUI process.
  [[ $(grep -c '^      m_pGUI->GetWindowManager().DestroyWindows();$' "$out/test.cpp") == 1 ]]
  sed 's/^      m_pGUI->GetWindowManager().DestroyWindows();$/      {} \/\/ Negative control: omit window destruction./' \
    "$out/test.cpp" > "$out/no-window-destroy.cpp"
  compile "$out/no-window-destroy.cpp" "$out/no-window-destroy"
  expect_assertion no-window-destroy window-saver 'Saw("windows-destroy")'
  # The legacy Stop helper must exclude persistent backends, which already had
  # an admission-time release attempt. A failed attempt makes the extra call visible.
  [[ $(grep -c 'if (!dpms || dpms->HasPersistentPowerSaving())' "$out/test.cpp") == 1 ]]
  sed 's/if (!dpms || dpms->HasPersistentPowerSaving())/if (!dpms)/' \
    "$out/test.cpp" > "$out/no-persistent-exclusion.cpp"
  compile "$out/no-persistent-exclusion.cpp" "$out/no-persistent-exclusion"
  expect_assertion no-persistent-exclusion persistent 'backend->releaseAttempts == 1'
  # Both production entry points must wait for explicit detector jobs before
  # releasing native/display resources or dependent services.
  [[ $(grep -c 'if (!aml_detector_retire_jobs())' "$out/test.cpp") == 2 ]]
  sed '/^bool CApplication::Stop(int /,/^}/s/if (!aml_detector_retire_jobs())/if (false)/' \
    "$out/test.cpp" > "$out/no-detector-stop.cpp"
  compile "$out/no-detector-stop.cpp" "$out/no-detector-stop"
  expect_assertion no-detector-stop detector-retry '!f.application.m_bStop'
  sed '/^bool CApplication::Cleanup()/,/^}/s/if (!aml_detector_retire_jobs())/if (false)/' \
    "$out/test.cpp" > "$out/no-detector-cleanup.cpp"
  compile "$out/no-detector-cleanup.cpp" "$out/no-detector-cleanup"
  expect_assertion no-detector-cleanup detector-cleanup '!f.application.Cleanup()'
fi
